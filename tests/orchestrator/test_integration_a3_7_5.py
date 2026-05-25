"""Integration test for the A.3.7.5 Rate-Limited Fetch Orchestrator.

Exercises the full pipeline:

    DatabaseManager (real SQLite, tmp_path)
        + DataSourceRegistry (fake, in-memory sources)
        + RateLimitedFetchOrchestrator
        + orchestrator_tick CLI (via main() argv, not subprocess)

Acceptance criteria (per spec §15 row A.3.7.5):
  * Multiple sources can be enqueued and tick() drains them across one
    or more invocations honouring per-source quotas.
  * Audit row lands in ``provider_call_log`` for every attempted call.
  * ``provider_quota_state`` reflects consumption.
  * Idempotent: re-running tick() against an empty queue is a no-op.
  * CLI returns exit 0 and prints a stats line.
  * NotImplementedError on a source does NOT poison the rest of the tick.
"""
from __future__ import annotations

import datetime as _dt
import io
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from typing import Optional

import yaml

from scripts.orchestrator_tick import main as cli_main
from src.common.database import DatabaseManager
from src.common.orchestrator.fetch_orchestrator import RateLimitedFetchOrchestrator
from src.common.schemas import FetchQueueEntry


class _FakeSource:
    """Minimal source double - records calls; never hits HTTP."""

    def __init__(self, name: str, raise_on: Optional[dict] = None):
        self.name = name
        self.calls: list[tuple] = []
        self._raise_on = raise_on or {}

    def _maybe_raise(self, key):
        if key in self._raise_on:
            raise self._raise_on[key]

    def fetch_universe(self, run_id):
        self.calls.append(("universe", run_id))
        self._maybe_raise("universe")
        return [{"ticker": "AAPL"}]

    def fetch_fundamentals_for_ticker(self, ticker, run_id):
        self.calls.append(("fundamentals", ticker))
        self._maybe_raise("fundamentals")
        return {"pe": 25.0}

    def fetch_filings_for_ticker(self, ticker, run_id):
        self.calls.append(("filings", ticker))
        self._maybe_raise("filings")
        return [{"form": "10-K"}]

    def fetch_macro_series(self, series_ids, run_id):
        self.calls.append(("macro", tuple(series_ids)))
        self._maybe_raise("macro")
        return {"series": list(series_ids), "obs": [1.0, 2.0]}


class _FakeRegistry:
    def __init__(self, sources: list[_FakeSource]):
        self._sources = sources

    def enabled_sources(self):
        return list(self._sources)


def _write_config(tmp_path: Path) -> Path:
    cfg = {
        "providers": {
            "fmp_stable": {"quota_calls": 250, "quota_window_seconds": 86400},
            "polygon":    {"quota_calls": 5,   "quota_window_seconds": 60},
            "tiingo":     {"quota_calls": 1000,"quota_window_seconds": 86400},
            "sec_edgar":  {"quota_calls": 10,  "quota_window_seconds": 1},
            "fred":       {"quota_calls": 120, "quota_window_seconds": 60},
            "yahoo":      {"quota_calls": 2000,"quota_window_seconds": 3600},
        },
        "source_providers": {
            "openbb": ["fmp_stable", "polygon", "tiingo"],
            "edgar":  ["sec_edgar"],
            "fred":   ["fred"],
            "yahoo":  ["yahoo"],
        },
        "weights": {
            "edgar":  {"form4": 100, "fundamentals": 40},
            "openbb": {"fundamentals": 30},
            "yahoo":  {"prices_daily": 25},
            "fred":   {"macro_series": 5},
        },
    }
    p = tmp_path / "orchestrator.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _make_db(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(db_path=str(tmp_path / "integration.db"))
    db.migrate_to_v10()
    return db


def _enqueue(db, source, field, ticker=None, priority=0.0):
    db.enqueue_fetch(FetchQueueEntry(
        source=source, ticker=ticker, field=field,
        priority_score=priority,
        created_at=_dt.datetime.utcnow().isoformat(),
        dispatched_at=None, completed_at=None,
        status="pending", last_error=None,
    ))


class TestOrchestratorPipeline:
    def test_multi_source_drain(self, tmp_path):
        """Enqueue work for 3 sources; one tick drains all of them and
        every audit + quota row lands correctly."""
        db = _make_db(tmp_path)
        cfg = _write_config(tmp_path)

        openbb = _FakeSource("openbb")
        edgar = _FakeSource("edgar")
        fred = _FakeSource("fred")
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([openbb, edgar, fred]),
            config_path=cfg,
        )

        _enqueue(db, "openbb", "fundamentals", ticker="AAPL", priority=10)
        _enqueue(db, "edgar", "form4", ticker="AAPL", priority=20)
        _enqueue(db, "fred", "macro:UNRATE", priority=5)

        stats = orch.tick(budget_seconds=30)
        assert stats["attempted"] == 3
        assert stats["succeeded"] == 3
        assert stats["failed"] == 0

        assert openbb.calls == [("fundamentals", "AAPL")]
        assert edgar.calls == [("filings", "AAPL")]
        assert fred.calls == [("macro", ("UNRATE",))]

        with sqlite3.connect(db.db_path) as c:
            log_count = c.execute(
                "SELECT COUNT(*) FROM provider_call_log"
            ).fetchone()[0]
            quota_count = c.execute(
                "SELECT COUNT(*) FROM provider_quota_state"
            ).fetchone()[0]
            done_count = c.execute(
                "SELECT COUNT(*) FROM fetch_queue WHERE status='done'"
            ).fetchone()[0]
        assert log_count == 3
        assert quota_count >= 3
        assert done_count == 3

    def test_idempotent_empty_tick(self, tmp_path):
        db = _make_db(tmp_path)
        cfg = _write_config(tmp_path)
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([_FakeSource("yahoo")]),
            config_path=cfg,
        )
        stats1 = orch.tick(budget_seconds=10)
        stats2 = orch.tick(budget_seconds=10)
        assert stats1["attempted"] == 0
        assert stats2["attempted"] == 0
        with sqlite3.connect(db.db_path) as c:
            assert c.execute(
                "SELECT COUNT(*) FROM provider_call_log"
            ).fetchone()[0] == 0

    def test_per_source_failure_isolation(self, tmp_path):
        """A NotImplementedError on edgar must not stop yahoo from
        completing."""
        db = _make_db(tmp_path)
        cfg = _write_config(tmp_path)
        bad = _FakeSource("edgar", raise_on={
            "filings": NotImplementedError("not provided"),
        })
        good = _FakeSource("yahoo")
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([bad, good]),
            config_path=cfg,
        )
        _enqueue(db, "edgar", "form4", ticker="AAPL", priority=20)
        _enqueue(db, "yahoo", "prices_daily", ticker="AAPL", priority=10)

        stats = orch.tick(budget_seconds=10)
        assert stats["attempted"] == 2
        assert stats["succeeded"] == 1
        assert stats["failed"] == 1

        with sqlite3.connect(db.db_path) as c:
            rows = c.execute(
                "SELECT source, status FROM fetch_queue ORDER BY source"
            ).fetchall()
        assert dict(rows) == {"edgar": "error", "yahoo": "done"}


class TestOrchestratorCli:
    def test_cli_runs_against_empty_queue(self, tmp_path):
        """The CLI wires DatabaseManager + DataSourceRegistry + orchestrator
        and prints a stats line."""
        db_path = tmp_path / "cli.db"
        cfg_path = _write_config(tmp_path)
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main([
                "--db", str(db_path),
                "--config", str(cfg_path),
                "--budget", "5",
            ])
        out = buf.getvalue()
        assert rc == 0
        assert "attempted=0" in out
        assert "exited_at_deadline=" in out

    def test_cli_json_output(self, tmp_path):
        db_path = tmp_path / "cli2.db"
        cfg_path = _write_config(tmp_path)
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main([
                "--db", str(db_path),
                "--config", str(cfg_path),
                "--budget", "5",
                "--json",
            ])
        assert rc == 0
        import json as _json
        line = [ln for ln in buf.getvalue().splitlines() if ln.strip()][-1]
        payload = _json.loads(line)
        assert set(payload.keys()) >= {
            "attempted", "succeeded", "failed", "skipped_quota",
            "orphans_reclaimed", "exited_at_deadline",
        }

    def test_cli_returns_1_on_bad_config(self, tmp_path):
        db_path = tmp_path / "cli3.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        bad_cfg = tmp_path / "missing.yaml"
        rc = cli_main([
            "--db", str(db_path),
            "--config", str(bad_cfg),
        ])
        assert rc == 1
