"""Tests for BaseDataSource watermark helpers added in A.3.1."""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


class _StubSource(BaseDataSource):
    name = "stub"
    cadence = "weekly"
    provides = {"historical_price"}

    def health_check(self) -> SourceHealthStatus:
        return SourceHealthStatus(source=self.name, status=SourceStatus.OK, checked_at="t")


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v3()
    return mgr


def test_get_fetch_gap_returns_full_lookback_when_no_watermark(db):
    src = _StubSource()
    start, end = src.get_fetch_gap("AAPL", "historical_price", db, max_lookback_days=365)
    today = datetime.date.today()
    assert end == today
    assert start == today - datetime.timedelta(days=365)


def test_get_fetch_gap_returns_delta_when_watermark_exists(db):
    src = _StubSource()
    db.upsert_watermark(
        source="stub", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-15", success=True,
    )
    start, end = src.get_fetch_gap("AAPL", "historical_price", db)
    assert start == datetime.date(2026, 5, 16)
    assert end == datetime.date.today()


def test_get_fetch_gap_returns_inverted_window_when_already_current(db):
    src = _StubSource()
    today_iso = datetime.date.today().isoformat()
    db.upsert_watermark(
        source="stub", ticker="AAPL", field="historical_price",
        last_observation_date=today_iso, success=True,
    )
    start, end = src.get_fetch_gap("AAPL", "historical_price", db)
    # start > end signals "nothing to fetch"
    assert start > end


def test_update_watermark_success_writes_through(db):
    src = _StubSource()
    src.update_watermark(
        ticker="AAPL", field="historical_price",
        last_observation_date=datetime.date(2026, 5, 20),
        db=db, success=True,
    )
    w = db.get_watermark("stub", "AAPL", "historical_price")
    assert w is not None
    assert w["last_observation_date"] == "2026-05-20"
    assert w["fetch_count"] == 1


def test_update_watermark_failure_records_error(db):
    src = _StubSource()
    src.update_watermark(
        ticker="AAPL", field="historical_price",
        last_observation_date=None,
        db=db, success=False, error_message="429",
    )
    w = db.get_watermark("stub", "AAPL", "historical_price")
    assert w is not None
    assert w["error_count"] == 1
    assert w["last_error_message"] == "429"
