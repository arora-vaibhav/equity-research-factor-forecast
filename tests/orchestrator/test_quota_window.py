"""Tests for src.common.orchestrator.quota_window - sliding-window quota math.

Covers:
  * record_quota_use: writes a provider_quota_state row at the floor-to-
    second bucket of the call time.
  * get_calls_in_window: SUM(calls_used) WHERE window_start > now - W,
    using strict ``>`` so a bucket exactly at the boundary has slid out.
  * remaining: min across all providers a source consumes.
  * has_headroom: True iff every provider has remaining >= calls_needed.

DB integration is real (tmp_path SQLite), not mocked - the helpers are
thin wrappers over SQL.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.orchestrator.quota_window import (
    get_calls_in_window,
    has_headroom,
    record_quota_use,
    remaining,
)


def _db(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(db_path=str(tmp_path / "quota.db"))
    db.migrate_to_v10()
    return db


class TestRecordQuotaUse:
    def test_first_call_creates_row(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        t = _dt.datetime(2026, 5, 21, 8, 0, 0)
        record_quota_use(db, provider="polygon", calls=1, at=t)
        with sqlite3.connect(db.db_path) as c:
            rows = c.execute(
                "SELECT provider, window_start, calls_used "
                "FROM provider_quota_state"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "polygon"
        assert rows[0][1] == "2026-05-21T08:00:00"
        assert rows[0][2] == 1

    def test_microseconds_floored_to_second(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        t = _dt.datetime(2026, 5, 21, 8, 0, 0, microsecond=999_999)
        record_quota_use(db, provider="polygon", calls=1, at=t)
        with sqlite3.connect(db.db_path) as c:
            ws = c.execute(
                "SELECT window_start FROM provider_quota_state"
            ).fetchone()[0]
        assert ws == "2026-05-21T08:00:00"

    def test_same_bucket_increments(self, tmp_path: Path) -> None:
        """Two calls in the same second bump calls_used to 2 (not two rows)."""
        db = _db(tmp_path)
        t = _dt.datetime(2026, 5, 21, 8, 0, 0)
        record_quota_use(db, provider="polygon", calls=1, at=t)
        record_quota_use(db, provider="polygon", calls=1, at=t)
        with sqlite3.connect(db.db_path) as c:
            rows = c.execute(
                "SELECT calls_used FROM provider_quota_state"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 2

    def test_different_seconds_create_separate_rows(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        t1 = _dt.datetime(2026, 5, 21, 8, 0, 0)
        t2 = _dt.datetime(2026, 5, 21, 8, 0, 1)
        record_quota_use(db, provider="polygon", calls=1, at=t1)
        record_quota_use(db, provider="polygon", calls=1, at=t2)
        with sqlite3.connect(db.db_path) as c:
            n = c.execute(
                "SELECT COUNT(*) FROM provider_quota_state"
            ).fetchone()[0]
        assert n == 2

    def test_at_defaults_to_now(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        before = _dt.datetime.utcnow()
        record_quota_use(db, provider="polygon")  # no at=, no calls=
        after = _dt.datetime.utcnow()
        with sqlite3.connect(db.db_path) as c:
            ws_str = c.execute(
                "SELECT window_start FROM provider_quota_state"
            ).fetchone()[0]
        ws = _dt.datetime.fromisoformat(ws_str)
        assert before.replace(microsecond=0) <= ws <= after.replace(microsecond=0)

    def test_custom_calls_count(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        t = _dt.datetime(2026, 5, 21, 8, 0, 0)
        record_quota_use(db, provider="polygon", calls=3, at=t)
        with sqlite3.connect(db.db_path) as c:
            n = c.execute(
                "SELECT calls_used FROM provider_quota_state"
            ).fetchone()[0]
        assert n == 3


class TestGetCallsInWindow:
    def test_empty_table_returns_zero(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        n = get_calls_in_window(
            db, provider="polygon", window_seconds=60,
            now=_dt.datetime(2026, 5, 21, 8, 0, 0),
        )
        assert n == 0

    def test_call_inside_window_counted(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        # Call 30 seconds before "now"; should count.
        record_quota_use(
            db, provider="polygon", calls=1,
            at=_dt.datetime(2026, 5, 21, 8, 0, 30),
        )
        n = get_calls_in_window(
            db, provider="polygon", window_seconds=60,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        )
        assert n == 1

    def test_call_outside_window_excluded(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        # Call 120 seconds before "now"; window is 60s; should NOT count.
        record_quota_use(
            db, provider="polygon", calls=1,
            at=_dt.datetime(2026, 5, 21, 7, 59, 0),
        )
        n = get_calls_in_window(
            db, provider="polygon", window_seconds=60,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        )
        assert n == 0

    def test_strict_greater_than_boundary(self, tmp_path: Path) -> None:
        """A call at exactly window_start = now - W has just slid out
        and is NOT counted (strict > predicate)."""
        db = _db(tmp_path)
        # Call at 08:00:00; now=08:01:00, window=60s -> cutoff=08:00:00
        # so this call is on the boundary and excluded.
        record_quota_use(
            db, provider="polygon", calls=1,
            at=_dt.datetime(2026, 5, 21, 8, 0, 0),
        )
        n = get_calls_in_window(
            db, provider="polygon", window_seconds=60,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        )
        assert n == 0

    def test_sums_across_buckets(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        for second in range(5):
            record_quota_use(
                db, provider="polygon", calls=1,
                at=_dt.datetime(2026, 5, 21, 8, 0, 30 + second),
            )
        n = get_calls_in_window(
            db, provider="polygon", window_seconds=60,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        )
        assert n == 5

    def test_provider_isolation(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        record_quota_use(
            db, provider="polygon", calls=3,
            at=_dt.datetime(2026, 5, 21, 8, 0, 30),
        )
        record_quota_use(
            db, provider="fmp_stable", calls=10,
            at=_dt.datetime(2026, 5, 21, 8, 0, 30),
        )
        n_poly = get_calls_in_window(
            db, provider="polygon", window_seconds=60,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        )
        n_fmp = get_calls_in_window(
            db, provider="fmp_stable", window_seconds=60,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        )
        assert n_poly == 3
        assert n_fmp == 10

    def test_now_defaults_to_utcnow(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        # Record at "now"; window 3600s should certainly include it.
        record_quota_use(db, provider="polygon", calls=1)
        n = get_calls_in_window(db, provider="polygon", window_seconds=3600)
        assert n == 1


class TestRemaining:
    def test_single_provider_source(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        provider_quotas = {
            "yahoo": {"quota_calls": 2000, "quota_window_seconds": 3600}
        }
        source_providers = {"yahoo": ["yahoo"]}
        record_quota_use(
            db, provider="yahoo", calls=5,
            at=_dt.datetime(2026, 5, 21, 8, 0, 0),
        )
        r = remaining(
            db, provider_quotas=provider_quotas, source="yahoo",
            source_providers=source_providers,
            now=_dt.datetime(2026, 5, 21, 8, 30, 0),
        )
        assert r == 1995

    def test_multi_provider_returns_min(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        provider_quotas = {
            "fmp_stable": {"quota_calls": 250, "quota_window_seconds": 86400},
            "polygon": {"quota_calls": 5, "quota_window_seconds": 60},
            "tiingo": {"quota_calls": 1000, "quota_window_seconds": 86400},
        }
        source_providers = {"openbb": ["fmp_stable", "polygon", "tiingo"]}
        # Polygon is the tight constraint: 1 used, 4 remaining.
        record_quota_use(
            db, provider="polygon", calls=1,
            at=_dt.datetime(2026, 5, 21, 8, 0, 30),
        )
        record_quota_use(
            db, provider="fmp_stable", calls=10,
            at=_dt.datetime(2026, 5, 21, 8, 0, 30),
        )
        r = remaining(
            db, provider_quotas=provider_quotas, source="openbb",
            source_providers=source_providers,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        )
        # min(250-10, 5-1, 1000-0) = min(240, 4, 1000) = 4
        assert r == 4

    def test_saturated_returns_zero(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        provider_quotas = {
            "polygon": {"quota_calls": 5, "quota_window_seconds": 60}
        }
        source_providers = {"polygon": ["polygon"]}
        record_quota_use(
            db, provider="polygon", calls=5,
            at=_dt.datetime(2026, 5, 21, 8, 0, 30),
        )
        r = remaining(
            db, provider_quotas=provider_quotas, source="polygon",
            source_providers=source_providers,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        )
        assert r == 0


class TestHasHeadroom:
    def test_fresh_provider_has_headroom(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        provider_quotas = {
            "polygon": {"quota_calls": 5, "quota_window_seconds": 60}
        }
        source_providers = {"polygon": ["polygon"]}
        assert has_headroom(
            db, provider_quotas=provider_quotas, source="polygon",
            source_providers=source_providers, calls_needed=1,
            now=_dt.datetime(2026, 5, 21, 8, 0, 0),
        ) is True

    def test_saturated_provider_no_headroom(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        provider_quotas = {
            "polygon": {"quota_calls": 5, "quota_window_seconds": 60}
        }
        source_providers = {"polygon": ["polygon"]}
        record_quota_use(
            db, provider="polygon", calls=5,
            at=_dt.datetime(2026, 5, 21, 8, 0, 30),
        )
        assert has_headroom(
            db, provider_quotas=provider_quotas, source="polygon",
            source_providers=source_providers, calls_needed=1,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        ) is False

    def test_exact_boundary_allowed(self, tmp_path: Path) -> None:
        """4 used + want 1 more = exactly at limit; allowed."""
        db = _db(tmp_path)
        provider_quotas = {
            "polygon": {"quota_calls": 5, "quota_window_seconds": 60}
        }
        source_providers = {"polygon": ["polygon"]}
        record_quota_use(
            db, provider="polygon", calls=4,
            at=_dt.datetime(2026, 5, 21, 8, 0, 30),
        )
        assert has_headroom(
            db, provider_quotas=provider_quotas, source="polygon",
            source_providers=source_providers, calls_needed=1,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        ) is True
        # But one more on top is over the limit.
        assert has_headroom(
            db, provider_quotas=provider_quotas, source="polygon",
            source_providers=source_providers, calls_needed=2,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        ) is False

    def test_multi_provider_all_must_have_headroom(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        provider_quotas = {
            "fmp_stable": {"quota_calls": 250, "quota_window_seconds": 86400},
            "polygon": {"quota_calls": 5, "quota_window_seconds": 60},
            "tiingo": {"quota_calls": 1000, "quota_window_seconds": 86400},
        }
        source_providers = {"openbb": ["fmp_stable", "polygon", "tiingo"]}
        # Only polygon is saturated.
        record_quota_use(
            db, provider="polygon", calls=5,
            at=_dt.datetime(2026, 5, 21, 8, 0, 30),
        )
        assert has_headroom(
            db, provider_quotas=provider_quotas, source="openbb",
            source_providers=source_providers, calls_needed=1,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        ) is False

    def test_unknown_source_returns_false(self, tmp_path: Path) -> None:
        """Defensive: source not in source_providers map cannot be dispatched."""
        db = _db(tmp_path)
        assert has_headroom(
            db, provider_quotas={}, source="nonexistent",
            source_providers={}, calls_needed=1,
        ) is False


class TestQuotaWindowDataclass:
    """Verify the QuotaWindow convenience dataclass binds db + config."""

    def test_quotawindow_remaining_and_headroom(self, tmp_path: Path) -> None:
        from src.common.orchestrator.quota_window import QuotaWindow
        db = _db(tmp_path)
        provider_quotas = {
            "polygon": {"quota_calls": 5, "quota_window_seconds": 60}
        }
        source_providers = {"polygon": ["polygon"]}
        qw = QuotaWindow(
            db=db, provider_quotas=provider_quotas,
            source_providers=source_providers,
            now=_dt.datetime(2026, 5, 21, 8, 0, 0),
        )
        assert qw.remaining("polygon") == 5
        assert qw.has_headroom("polygon", 1) is True
