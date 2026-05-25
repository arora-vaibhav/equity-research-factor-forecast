"""Tests for src.common.orchestrator.fetch_orchestrator (Phase A.3.7.5).

Coverage:
  * Construction + config load.
  * Dequeue -> dispatch -> log -> mark done happy path.
  * Field routing (fundamentals/filings/macro/universe -> correct method).
  * NotImplementedError, generic exception -> status='error', not crash.
  * Quota exhaustion -> requeue + skip rest of source for the tick.
  * Time-budget exhaustion -> requeue remaining claimed rows + flag exit.
  * Orphan reclaim is called before dequeue (5-min sweep).
  * Unknown source in queue -> recorded as failed, not raised.
  * Multi-source ordering preserves priority_score DESC from DB.
  * Quota recorded BEFORE call (one row in provider_quota_state per call,
    visible even when the source raises).
  * Empty queue -> tick is a no-op (succeeded/failed counts both zero).

Sources are mocked at the BaseDataSource method boundary - the orchestrator
does not touch ``requests`` directly so mocking the HTTP layer is the
wrong granularity here. The fake registry exposes ``enabled_sources()``
and the fake source carries ``.name`` + the relevant fetch methods.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from typing import Optional

import pytest
import yaml

from src.common.database import DatabaseManager
from src.common.orchestrator.fetch_orchestrator import (
    RateLimitedFetchOrchestrator,
    _estimate_bytes,
    _route_field_to_call,
)
from src.common.schemas import FetchQueueEntry


# --------------------------------------------------------------------- helpers


class _FakeSource:
    """Minimal stand-in for a BaseDataSource.

    Records every call. ``raise_on`` lets a test force a specific method
    to raise. Each method returns a small payload that ``_estimate_bytes``
    can size.
    """

    def __init__(self, name: str, raise_on: Optional[dict] = None):
        self.name = name
        self.calls: list[tuple] = []
        self._raise_on = raise_on or {}

    def _maybe_raise(self, key: str) -> None:
        if key in self._raise_on:
            exc = self._raise_on[key]
            raise exc

    def fetch_universe(self, run_id):
        self.calls.append(("universe", run_id))
        self._maybe_raise("universe")
        return [{"ticker": "AAPL"}, {"ticker": "MSFT"}]

    def fetch_fundamentals_for_ticker(self, ticker, run_id):
        self.calls.append(("fundamentals", ticker, run_id))
        self._maybe_raise("fundamentals")
        return {"pe": 30.0, "ticker": ticker}

    def fetch_filings_for_ticker(self, ticker, run_id):
        self.calls.append(("filings", ticker, run_id))
        self._maybe_raise("filings")
        return [{"form": "10-K", "ticker": ticker}]

    def fetch_macro_series(self, series_ids, run_id):
        self.calls.append(("macro", tuple(series_ids), run_id))
        self._maybe_raise("macro")
        return {"series": series_ids, "values": [1, 2, 3]}


class _FakeRegistry:
    def __init__(self, sources: list[_FakeSource]):
        self._sources = sources

    def enabled_sources(self):
        return list(self._sources)


class _FakeClock:
    """Manually advanceable clock for deterministic budget/window tests."""

    def __init__(self, start: _dt.datetime):
        self.now = start

    def __call__(self) -> _dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + _dt.timedelta(seconds=seconds)


def _write_test_config(tmp_path: Path) -> Path:
    cfg = {
        "providers": {
            "fmp_stable": {"quota_calls": 250, "quota_window_seconds": 86400},
            "polygon":    {"quota_calls": 5,   "quota_window_seconds": 60},
            "tiingo":     {"quota_calls": 1000,"quota_window_seconds": 86400},
            "sec_edgar":  {"quota_calls": 10,  "quota_window_seconds": 1},
            "fred":       {"quota_calls": 120, "quota_window_seconds": 60},
            "yahoo":      {"quota_calls": 2000,"quota_window_seconds": 3600},
            "finviz":     {"quota_calls": 60,  "quota_window_seconds": 60},
        },
        "source_providers": {
            "openbb": ["fmp_stable", "polygon", "tiingo"],
            "edgar":  ["sec_edgar"],
            "fred":   ["fred"],
            "yahoo":  ["yahoo"],
            "finviz": ["finviz"],
        },
        "weights": {
            "edgar":  {"form4": 100, "form_8k": 80, "fundamentals": 40},
            "openbb": {"fundamentals": 30},
            "yahoo":  {"prices_daily": 25},
            "finviz": {"snapshot": 20},
            "fred":   {"macro_series": 5},
        },
    }
    p = tmp_path / "orchestrator.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _db(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(db_path=str(tmp_path / "orch.db"))
    db.migrate_to_v10()
    return db


def _enqueue(db, source, field, ticker, priority=0.0):
    db.enqueue_fetch(FetchQueueEntry(
        source=source, ticker=ticker, field=field,
        priority_score=priority,
        created_at=_dt.datetime.utcnow().isoformat(),
        dispatched_at=None, completed_at=None,
        status="pending", last_error=None,
    ))


# ----------------------------------------------------------------- _route tests


class TestRoute:
    def test_fundamentals_routes_to_fundamentals_method(self):
        s = _FakeSource("openbb")
        _route_field_to_call(s, "fundamentals", "AAPL", "rid")
        assert s.calls == [("fundamentals", "AAPL", "rid")]

    def test_filings_alias_form4_routes_to_filings(self):
        s = _FakeSource("edgar")
        _route_field_to_call(s, "form4", "AAPL", "rid")
        assert s.calls[0][0] == "filings"

    def test_filings_alias_form_8k_routes_to_filings(self):
        s = _FakeSource("edgar")
        _route_field_to_call(s, "form_8k", "AAPL", "rid")
        assert s.calls[0][0] == "filings"

    def test_macro_namespaced_extracts_series_id(self):
        s = _FakeSource("fred")
        _route_field_to_call(s, "macro:UNRATE", None, "rid")
        # macro call key + (series_ids_tuple, run_id)
        kind, series_tuple, _ = s.calls[0]
        assert kind == "macro"
        assert series_tuple == ("UNRATE",)

    def test_macro_plain_series_passes_empty_list(self):
        s = _FakeSource("fred")
        _route_field_to_call(s, "macro_series", None, "rid")
        assert s.calls[0] == ("macro", tuple(), "rid")

    def test_unknown_field_falls_through_to_universe(self):
        s = _FakeSource("yahoo")
        _route_field_to_call(s, "snapshot", None, "rid")
        assert s.calls[0][0] == "universe"


# ------------------------------------------------------------ _estimate_bytes


class TestEstimateBytes:
    def test_none_is_zero(self):
        assert _estimate_bytes(None) == 0

    def test_dict_returns_positive_size(self):
        n = _estimate_bytes({"a": 1, "b": "two"})
        assert n > 0

    def test_dataframe_uses_memory_usage(self):
        import pandas as pd
        df = pd.DataFrame({"x": [1, 2, 3], "y": ["a", "bb", "ccc"]})
        assert _estimate_bytes(df) > 0

    def test_unserialisable_object_returns_zero_or_sizeof(self):
        class _Opaque:
            pass
        n = _estimate_bytes(_Opaque())
        # JSON fails; we fall to sys.getsizeof which is always positive.
        assert n >= 0


# ----------------------------------------------------- end-to-end tick tests


class TestTickHappyPath:
    def test_construction_loads_config(self, tmp_path):
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([]), config_path=cfg,
        )
        assert "fmp_stable" in orch.config.providers
        assert "openbb" in orch.config.source_providers

    def test_empty_queue_is_noop(self, tmp_path):
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([_FakeSource("yahoo")]),
            config_path=cfg,
        )
        stats = orch.tick(budget_seconds=30)
        assert stats["attempted"] == 0
        assert stats["succeeded"] == 0
        assert stats["failed"] == 0

    def test_one_pending_fetches_marks_done_and_logs(self, tmp_path):
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        src = _FakeSource("yahoo")
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([src]), config_path=cfg,
        )
        _enqueue(db, "yahoo", "prices_daily", "AAPL", priority=10.0)

        stats = orch.tick(budget_seconds=30)
        assert stats["attempted"] == 1
        assert stats["succeeded"] == 1
        assert stats["failed"] == 0

        with sqlite3.connect(db.db_path) as c:
            q_status = c.execute(
                "SELECT status FROM fetch_queue"
            ).fetchone()
            log_rows = c.execute(
                "SELECT source, status, http_status FROM provider_call_log"
            ).fetchall()
        assert q_status[0] == "done"
        assert log_rows == [("yahoo", "ok", 200)]

    def test_fundamentals_routes_to_fundamentals_call(self, tmp_path):
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        src = _FakeSource("openbb")
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([src]), config_path=cfg,
        )
        _enqueue(db, "openbb", "fundamentals", "MSFT")
        orch.tick(budget_seconds=10)
        assert any(c[0] == "fundamentals" for c in src.calls)

    def test_macro_routes_to_macro_series(self, tmp_path):
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        src = _FakeSource("fred")
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([src]), config_path=cfg,
        )
        _enqueue(db, "fred", "macro:UNRATE", None)
        orch.tick(budget_seconds=10)
        macro_calls = [c for c in src.calls if c[0] == "macro"]
        assert macro_calls and macro_calls[0][1] == ("UNRATE",)


class TestTickFailureModes:
    def test_not_implemented_marks_error_not_crash(self, tmp_path):
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        src = _FakeSource("yahoo", raise_on={
            "fundamentals": NotImplementedError("no fundamentals here"),
        })
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([src]), config_path=cfg,
        )
        _enqueue(db, "yahoo", "fundamentals", "AAPL")
        stats = orch.tick(budget_seconds=10)
        assert stats["attempted"] == 1
        assert stats["failed"] == 1
        with sqlite3.connect(db.db_path) as c:
            row = c.execute(
                "SELECT status, last_error FROM fetch_queue"
            ).fetchone()
        assert row[0] == "error"
        assert "not_implemented" in row[1]

    def test_generic_exception_marks_error_not_crash(self, tmp_path):
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        src = _FakeSource("yahoo", raise_on={
            "universe": RuntimeError("HTTP 503"),
        })
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([src]), config_path=cfg,
        )
        _enqueue(db, "yahoo", "snapshot", None)
        stats = orch.tick(budget_seconds=10)
        assert stats["failed"] == 1
        with sqlite3.connect(db.db_path) as c:
            err = c.execute(
                "SELECT last_error FROM fetch_queue"
            ).fetchone()[0]
        assert "RuntimeError" in err and "503" in err

    def test_unknown_source_marks_error_not_crash(self, tmp_path):
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([]), config_path=cfg,
        )
        _enqueue(db, "yahoo", "snapshot", None)
        stats = orch.tick(budget_seconds=10)
        assert stats["failed"] == 1
        with sqlite3.connect(db.db_path) as c:
            err = c.execute(
                "SELECT last_error FROM fetch_queue"
            ).fetchone()[0]
        assert "unknown source" in err


class TestQuotaInteraction:
    def test_quota_recorded_before_call_visible_even_on_failure(self, tmp_path):
        """A failed call still consumed the quota slot (conservative)."""
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        src = _FakeSource("yahoo", raise_on={
            "universe": RuntimeError("boom"),
        })
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([src]), config_path=cfg,
        )
        _enqueue(db, "yahoo", "snapshot", None)
        orch.tick(budget_seconds=10)
        with sqlite3.connect(db.db_path) as c:
            total = c.execute(
                "SELECT COALESCE(SUM(calls_used), 0) FROM provider_quota_state "
                "WHERE provider='yahoo'"
            ).fetchone()[0]
        assert total == 1

    def test_quota_exhaustion_requeues_and_skips_source(self, tmp_path):
        """Set polygon quota to 0 effectively by pre-burning it."""
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        # Burn polygon to its 5/60s limit by pre-recording 5 calls.
        from src.common.orchestrator.quota_window import record_quota_use
        t0 = _dt.datetime(2026, 5, 22, 12, 0, 0)
        for _ in range(5):
            record_quota_use(db, provider="polygon", calls=1, at=t0)

        src = _FakeSource("openbb")
        clock = _FakeClock(t0)
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([src]),
            config_path=cfg, clock=clock,
        )
        # openbb needs fmp_stable + polygon + tiingo headroom; polygon is full.
        _enqueue(db, "openbb", "fundamentals", "AAPL")
        _enqueue(db, "openbb", "fundamentals", "MSFT")

        stats = orch.tick(budget_seconds=10)
        assert stats["skipped_quota"] >= 1
        assert stats["attempted"] == 0
        # Entries should be back in pending.
        with sqlite3.connect(db.db_path) as c:
            statuses = [
                r[0] for r in c.execute(
                    "SELECT status FROM fetch_queue ORDER BY queue_id"
                ).fetchall()
            ]
        assert all(s == "pending" for s in statuses)

    def test_quota_partially_available_some_dispatched_then_skip(self, tmp_path):
        """Pre-burn polygon to 4/5; then expect exactly one dispatch then skip."""
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        from src.common.orchestrator.quota_window import record_quota_use
        t0 = _dt.datetime(2026, 5, 22, 12, 0, 0)
        for _ in range(4):
            record_quota_use(db, provider="polygon", calls=1, at=t0)

        src = _FakeSource("openbb")
        clock = _FakeClock(t0)
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([src]),
            config_path=cfg, clock=clock,
        )
        _enqueue(db, "openbb", "fundamentals", "AAPL", priority=10)
        _enqueue(db, "openbb", "fundamentals", "MSFT", priority=5)

        stats = orch.tick(budget_seconds=10)
        # Exactly one fits before polygon saturates; the second is requeued
        # at least once (possibly counted again if a stall-guard iteration
        # re-claims it before the break, so >= 1 rather than == 1).
        assert stats["attempted"] == 1
        assert stats["succeeded"] == 1
        assert stats["skipped_quota"] >= 1


class TestPriorityOrdering:
    def test_higher_priority_dispatches_first(self, tmp_path):
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        src = _FakeSource("yahoo")
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([src]), config_path=cfg,
        )
        _enqueue(db, "yahoo", "snapshot", "AAPL", priority=1.0)
        _enqueue(db, "yahoo", "snapshot", "MSFT", priority=100.0)
        orch.tick(budget_seconds=10)
        # Higher-priority ticker (MSFT, score=100) should land in the log
        # before the lower-priority one (AAPL, score=1).
        with sqlite3.connect(db.db_path) as c:
            tickers = [
                r[0] for r in c.execute(
                    "SELECT ticker FROM provider_call_log "
                    "ORDER BY call_id ASC"
                ).fetchall()
            ]
        assert tickers == ["MSFT", "AAPL"]


class TestBudgetAndOrphans:
    def test_orphans_reclaimed_field_reflects_db_sweep(self, tmp_path):
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        # Inject a stale dispatched row directly.
        stale = (_dt.datetime.utcnow() - _dt.timedelta(seconds=600)).isoformat()
        with sqlite3.connect(db.db_path) as c:
            c.execute(
                "INSERT INTO fetch_queue "
                "(source, ticker, field, priority_score, created_at, "
                " dispatched_at, completed_at, status, last_error) "
                "VALUES ('yahoo','AAPL','snapshot',0.0,?,?,NULL,'dispatched',NULL)",
                (stale, stale),
            )
            c.commit()
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([_FakeSource("yahoo")]),
            config_path=cfg,
        )
        stats = orch.tick(budget_seconds=10)
        assert stats["orphans_reclaimed"] == 1
        # The reclaimed row was reset to pending, then dispatched and
        # completed in the same tick.
        assert stats["succeeded"] == 1

    def test_zero_budget_returns_with_no_attempts(self, tmp_path):
        db = _db(tmp_path)
        cfg = _write_test_config(tmp_path)
        src = _FakeSource("yahoo")
        clock = _FakeClock(_dt.datetime(2026, 5, 22, 12, 0, 0))
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=_FakeRegistry([src]),
            config_path=cfg, clock=clock,
        )
        _enqueue(db, "yahoo", "snapshot", None)
        stats = orch.tick(budget_seconds=0)
        # deadline == now, so the very first while-check exits.
        assert stats["attempted"] == 0
        assert stats["exited_at_deadline"] is True
