"""Tests for Phase A.3.7.5 database migration + orchestrator helpers.

Covers:
  * migrate_to_v10 (schema bump, table DDL, indexes, idempotence)
  * insert_provider_call_log (per-row INSERT, returns rowid)
  * upsert_provider_quota_state (UPSERT on (source, provider, window_start))
  * enqueue_fetch (INSERT OR IGNORE on (source, field, ticker) WHERE pending)
  * dequeue_next (BEGIN IMMEDIATE atomic claim, ORDER BY priority_score DESC)
  * mark_fetch_done (success -> 'done', failure -> 'error' + last_error)
  * reclaim_orphaned_dispatched (stale_seconds default 300)
"""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import (
    FetchQueueEntry,
    ProviderCallLog,
    ProviderQuotaState,
)


# ---------------------------------------------------------------------------
# migrate_to_v10
# ---------------------------------------------------------------------------


class TestMigrateToV10:
    def test_migration_bumps_schema_version_to_10(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        assert db.get_schema_version() == 10

    def test_idempotent(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.migrate_to_v10()
        db.migrate_to_v10()
        assert db.get_schema_version() == 10

    def test_creates_provider_call_log_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        with sqlite3.connect(db_path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(provider_call_log)")}
        assert cols == {
            "call_id", "source", "ticker", "field",
            "started_at", "finished_at",
            "status", "bytes_returned", "error_message", "http_status",
        }

    def test_creates_provider_quota_state_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        with sqlite3.connect(db_path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(provider_quota_state)")}
        assert cols == {"source", "provider", "window_start", "calls_used"}

    def test_creates_fetch_queue_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        with sqlite3.connect(db_path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(fetch_queue)")}
        assert cols == {
            "queue_id", "source", "ticker", "field",
            "priority_score", "created_at", "dispatched_at", "completed_at",
            "status", "last_error",
        }

    def test_provider_call_log_has_source_started_index(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        with sqlite3.connect(db_path) as c:
            idx = {r[1] for r in c.execute("PRAGMA index_list(provider_call_log)")}
        assert "idx_call_source_started" in idx

    def test_quota_state_pk_is_source_provider_window(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        with sqlite3.connect(db_path) as c:
            c.execute(
                "INSERT INTO provider_quota_state (source, provider, window_start, calls_used) "
                "VALUES (?, ?, ?, ?)",
                ("polygon", "polygon", "2026-05-21T08:00:00Z", 1),
            )
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO provider_quota_state (source, provider, window_start, calls_used) "
                    "VALUES (?, ?, ?, ?)",
                    ("polygon", "polygon", "2026-05-21T08:00:00Z", 1),
                )

    def test_fetch_queue_dispatch_index_present(self, tmp_path: Path) -> None:
        """Index on (status, priority_score DESC) is critical for dequeue
        performance; without it dequeue degrades to full scan."""
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        with sqlite3.connect(db_path) as c:
            idx = {r[1] for r in c.execute("PRAGMA index_list(fetch_queue)")}
        assert "idx_queue_status_priority" in idx


# ---------------------------------------------------------------------------
# insert_provider_call_log
# ---------------------------------------------------------------------------


class TestInsertProviderCallLog:
    def test_insert_single_row_returns_rowid(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        row = ProviderCallLog(
            source="yahoo", ticker="AAPL", field="fundamentals",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:01Z",
            status="ok", bytes_returned=4096, http_status=200,
        )
        rowid = db.insert_provider_call_log(row)
        assert isinstance(rowid, int)
        assert rowid >= 1

    def test_call_id_autoincrement(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        ids = []
        for i in range(3):
            r = ProviderCallLog(
                source="yahoo", ticker="AAPL", field="fundamentals",
                started_at=f"2026-05-21T08:00:0{i}Z",
                finished_at=f"2026-05-21T08:00:0{i}Z",
                status="ok", bytes_returned=0,
            )
            ids.append(db.insert_provider_call_log(r))
        assert ids[0] < ids[1] < ids[2]

    def test_macro_row_with_null_ticker(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.insert_provider_call_log(
            ProviderCallLog(
                source="fred", ticker=None, field="macro:UNRATE",
                started_at="2026-05-21T08:00:00Z",
                finished_at="2026-05-21T08:00:00Z",
                status="ok", bytes_returned=128,
            )
        )
        with sqlite3.connect(db.db_path) as c:
            row = c.execute("SELECT ticker, field FROM provider_call_log").fetchone()
        assert row == (None, "macro:UNRATE")

    def test_failed_call_carries_error_message_and_http_status(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.insert_provider_call_log(
            ProviderCallLog(
                source="polygon", ticker="AAPL", field="multi_provider",
                started_at="2026-05-21T08:00:00Z",
                finished_at="2026-05-21T08:00:12Z",
                status="failed", bytes_returned=0,
                error_message="HTTPError 429", http_status=429,
            )
        )
        with sqlite3.connect(db.db_path) as c:
            row = c.execute(
                "SELECT status, error_message, http_status FROM provider_call_log"
            ).fetchone()
        assert row == ("failed", "HTTPError 429", 429)


# ---------------------------------------------------------------------------
# upsert_provider_quota_state
# ---------------------------------------------------------------------------


class TestUpsertProviderQuotaState:
    def test_initial_insert(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.upsert_provider_quota_state(
            ProviderQuotaState(
                source="polygon", provider="polygon",
                window_start="2026-05-21T08:00:00Z", calls_used=1,
            )
        )
        with sqlite3.connect(db.db_path) as c:
            n = c.execute(
                "SELECT calls_used FROM provider_quota_state "
                "WHERE source='polygon' AND provider='polygon' "
                "AND window_start='2026-05-21T08:00:00Z'"
            ).fetchone()[0]
        assert n == 1

    def test_upsert_replaces_existing(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.upsert_provider_quota_state(
            ProviderQuotaState(
                source="polygon", provider="polygon",
                window_start="2026-05-21T08:00:00Z", calls_used=1,
            )
        )
        db.upsert_provider_quota_state(
            ProviderQuotaState(
                source="polygon", provider="polygon",
                window_start="2026-05-21T08:00:00Z", calls_used=3,
            )
        )
        with sqlite3.connect(db.db_path) as c:
            rows = c.execute(
                "SELECT calls_used FROM provider_quota_state "
                "WHERE source='polygon' AND provider='polygon' "
                "AND window_start='2026-05-21T08:00:00Z'"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 3

    def test_multi_provider_isolation(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.upsert_provider_quota_state(
            ProviderQuotaState(
                source="openbb", provider="fmp_stable",
                window_start="2026-05-21T08:00:00Z", calls_used=2,
            )
        )
        db.upsert_provider_quota_state(
            ProviderQuotaState(
                source="openbb", provider="polygon",
                window_start="2026-05-21T08:00:00Z", calls_used=5,
            )
        )
        with sqlite3.connect(db.db_path) as c:
            rows = sorted(c.execute(
                "SELECT provider, calls_used FROM provider_quota_state "
                "WHERE source='openbb'"
            ).fetchall())
        assert rows == [("fmp_stable", 2), ("polygon", 5)]


# ---------------------------------------------------------------------------
# enqueue_fetch
# ---------------------------------------------------------------------------


class TestEnqueueFetch:
    def _entry(self, **overrides) -> FetchQueueEntry:
        base = dict(
            source="yahoo", ticker="AAPL", field="fundamentals",
            priority_score=0.0,
            created_at="2026-05-21T08:00:00Z",
            status="pending",
        )
        base.update(overrides)
        return FetchQueueEntry(**base)

    def test_enqueue_returns_rowid(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        qid = db.enqueue_fetch(self._entry())
        assert isinstance(qid, int) and qid >= 1

    def test_dedup_returns_existing_id(self, tmp_path: Path) -> None:
        """INSERT OR IGNORE on (source, field, ticker) WHERE status='pending'.
        Second enqueue of same (source, field, ticker) should be no-op."""
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        qid1 = db.enqueue_fetch(self._entry())
        qid2 = db.enqueue_fetch(self._entry(priority_score=99.0))
        assert qid1 == qid2
        with sqlite3.connect(db.db_path) as c:
            n = c.execute("SELECT COUNT(*) FROM fetch_queue").fetchone()[0]
        assert n == 1

    def test_distinct_field_creates_new_row(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.enqueue_fetch(self._entry(field="fundamentals"))
        db.enqueue_fetch(self._entry(field="revisions"))
        with sqlite3.connect(db.db_path) as c:
            n = c.execute("SELECT COUNT(*) FROM fetch_queue").fetchone()[0]
        assert n == 2


# ---------------------------------------------------------------------------
# dequeue_next
# ---------------------------------------------------------------------------


class TestDequeueNext:
    def _entry(self, **overrides) -> FetchQueueEntry:
        base = dict(
            source="yahoo", ticker="AAPL", field="fundamentals",
            priority_score=0.0,
            created_at="2026-05-21T08:00:00Z",
            status="pending",
        )
        base.update(overrides)
        return FetchQueueEntry(**base)

    def test_empty_queue_returns_empty_list(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        assert db.dequeue_next(limit=5) == []

    def test_returns_highest_priority_first(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.enqueue_fetch(self._entry(ticker="AAPL", priority_score=1.0))
        db.enqueue_fetch(self._entry(ticker="MSFT", priority_score=10.0))
        db.enqueue_fetch(self._entry(ticker="GOOG", priority_score=5.0))
        rows = db.dequeue_next(limit=3)
        assert [r["ticker"] for r in rows] == ["MSFT", "GOOG", "AAPL"]

    def test_marks_dispatched_with_dispatched_at(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.enqueue_fetch(self._entry())
        rows = db.dequeue_next(limit=1)
        assert len(rows) == 1
        queue_id = rows[0]["queue_id"]
        with sqlite3.connect(db.db_path) as c:
            row = c.execute(
                "SELECT status, dispatched_at FROM fetch_queue WHERE queue_id=?",
                (queue_id,),
            ).fetchone()
        assert row[0] == "dispatched"
        assert row[1] is not None

    def test_limit_caps_returned_rows(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        for i in range(5):
            db.enqueue_fetch(self._entry(field=f"f{i}", priority_score=float(i)))
        rows = db.dequeue_next(limit=2)
        assert len(rows) == 2

    def test_already_dispatched_skipped(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.enqueue_fetch(self._entry())
        first = db.dequeue_next(limit=1)
        assert len(first) == 1
        second = db.dequeue_next(limit=1)
        assert second == []


# ---------------------------------------------------------------------------
# mark_fetch_done
# ---------------------------------------------------------------------------


class TestMarkFetchDone:
    def _enqueue_and_claim(self, db: DatabaseManager) -> int:
        db.enqueue_fetch(
            FetchQueueEntry(
                source="yahoo", ticker="AAPL", field="fundamentals",
                priority_score=0.0,
                created_at="2026-05-21T08:00:00Z",
                status="pending",
            )
        )
        rows = db.dequeue_next(limit=1)
        return rows[0]["queue_id"]

    def test_success_marks_done(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        qid = self._enqueue_and_claim(db)
        db.mark_fetch_done(qid, success=True, error=None)
        with sqlite3.connect(db.db_path) as c:
            row = c.execute(
                "SELECT status, completed_at, last_error "
                "FROM fetch_queue WHERE queue_id=?",
                (qid,),
            ).fetchone()
        assert row[0] == "done"
        assert row[1] is not None
        assert row[2] is None

    def test_failure_marks_error_with_last_error(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        qid = self._enqueue_and_claim(db)
        db.mark_fetch_done(qid, success=False, error="HTTPError 500: server")
        with sqlite3.connect(db.db_path) as c:
            row = c.execute(
                "SELECT status, completed_at, last_error "
                "FROM fetch_queue WHERE queue_id=?",
                (qid,),
            ).fetchone()
        assert row[0] == "error"
        assert row[1] is not None
        assert row[2] == "HTTPError 500: server"


# ---------------------------------------------------------------------------
# reclaim_orphaned_dispatched
# ---------------------------------------------------------------------------


class TestReclaimOrphanedDispatched:
    def test_reclaims_stale_dispatched(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        old_iso = (
            datetime.datetime.utcnow() - datetime.timedelta(minutes=10)
        ).isoformat()
        with sqlite3.connect(db.db_path) as c:
            c.execute(
                "INSERT INTO fetch_queue (source, ticker, field, priority_score, "
                "created_at, dispatched_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("yahoo", "AAPL", "fundamentals", 0.0, old_iso, old_iso, "dispatched"),
            )
            c.commit()
        n = db.reclaim_orphaned_dispatched(stale_seconds=300)
        assert n == 1
        with sqlite3.connect(db.db_path) as c:
            row = c.execute(
                "SELECT status, dispatched_at FROM fetch_queue"
            ).fetchone()
        assert row[0] == "pending"
        assert row[1] is None

    def test_skips_recent_dispatched(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        recent_iso = datetime.datetime.utcnow().isoformat()
        with sqlite3.connect(db.db_path) as c:
            c.execute(
                "INSERT INTO fetch_queue (source, ticker, field, priority_score, "
                "created_at, dispatched_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("yahoo", "AAPL", "fundamentals", 0.0, recent_iso, recent_iso, "dispatched"),
            )
            c.commit()
        n = db.reclaim_orphaned_dispatched(stale_seconds=300)
        assert n == 0

    def test_default_stale_seconds_is_300(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        # 4 minutes old: NOT stale at default 300s threshold
        four_min_iso = (
            datetime.datetime.utcnow() - datetime.timedelta(minutes=4)
        ).isoformat()
        # 6 minutes old: stale
        six_min_iso = (
            datetime.datetime.utcnow() - datetime.timedelta(minutes=6)
        ).isoformat()
        with sqlite3.connect(db.db_path) as c:
            c.executemany(
                "INSERT INTO fetch_queue (source, ticker, field, priority_score, "
                "created_at, dispatched_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("yahoo", "AAPL", "fundamentals", 0.0, four_min_iso, four_min_iso, "dispatched"),
                    ("yahoo", "MSFT", "fundamentals", 0.0, six_min_iso, six_min_iso, "dispatched"),
                ],
            )
            c.commit()
        n = db.reclaim_orphaned_dispatched()  # default stale_seconds=300
        assert n == 1
