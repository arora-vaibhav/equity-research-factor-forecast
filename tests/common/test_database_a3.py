"""Tests for Phase A.3.1 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {r[0] for r in rows}


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as c:
        rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def test_migrate_to_v3_creates_fetch_watermarks_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    assert "fetch_watermarks" in _table_names(db_path)


def test_fetch_watermarks_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    cols = _columns(db_path, "fetch_watermarks")
    expected = {
        "source", "ticker", "field",
        "last_fetched_at", "last_observation_date",
        "fetch_count", "error_count", "last_error_message",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v3_bumps_schema_version_to_3(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3]


def test_migrate_to_v3_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.migrate_to_v3()
    mgr.migrate_to_v3()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3]


def test_get_schema_version_returns_3_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    assert mgr.get_schema_version() == 3


def test_insert_historical_price_now_uses_insert_or_ignore(tmp_path: Path):
    """Per Principle 6: re-inserting same (ticker, date) does NOT overwrite.
    Previously INSERT OR REPLACE; A.3.1 switches to INSERT OR IGNORE."""
    from src.common.schemas import HistoricalPriceRow
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()

    first = HistoricalPriceRow(
        ticker="AAPL", observation_date="2026-05-20",
        open=100.0, high=110.0, low=99.0, close=105.0,
        volume=1_000_000, adj_close=105.0,
        source="yahoo", scrape_timestamp="2026-05-21T08:00:00Z",
    )
    mgr.insert_historical_price([first])

    second = HistoricalPriceRow(
        ticker="AAPL", observation_date="2026-05-20",
        open=200.0, high=220.0, low=199.0, close=210.0,
        volume=2_000_000, adj_close=210.0,
        source="yahoo", scrape_timestamp="2026-05-21T09:00:00Z",
    )
    mgr.insert_historical_price([second])

    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT close FROM historical_price WHERE ticker='AAPL' AND observation_date='2026-05-20'"
        ).fetchall()
    assert rows == [(105.0,)], f"first write wins; got {rows}"


def test_migrate_to_v3_preserves_v2_data(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO canonical_universe (run_id, ticker) VALUES (?, ?)",
            ("sentinel-r", "TSLA"),
        )
        c.commit()
    mgr.migrate_to_v3()
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT run_id, ticker FROM canonical_universe WHERE run_id='sentinel-r'"
        ).fetchall()
    assert rows == [("sentinel-r", "TSLA")]


# Task 3: watermark CRUD helpers
from src.common.schemas import FetchWatermark  # noqa: E402


def test_get_watermark_returns_none_when_absent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    assert mgr.get_watermark("yahoo", "AAPL", "historical_price") is None


def test_upsert_watermark_insert_then_get_roundtrips(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-20",
        success=True, error_message=None,
    )
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w is not None
    assert w["source"] == "yahoo"
    assert w["ticker"] == "AAPL"
    assert w["field"] == "historical_price"
    assert w["last_observation_date"] == "2026-05-20"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0
    assert w["last_error_message"] is None


def test_upsert_watermark_second_call_increments_fetch_count(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-20", success=True,
    )
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-21", success=True,
    )
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w["fetch_count"] == 2
    assert w["error_count"] == 0
    assert w["last_observation_date"] == "2026-05-21"


def test_upsert_watermark_failure_increments_error_count(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date=None,
        success=False, error_message="429 too many requests",
    )
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w["fetch_count"] == 0
    assert w["error_count"] == 1
    assert w["last_error_message"] == "429 too many requests"
    assert w["last_observation_date"] is None


def test_upsert_watermark_failure_does_not_advance_observation_date(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-20", success=True,
    )
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date=None,
        success=False, error_message="timeout",
    )
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w["last_observation_date"] == "2026-05-20"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 1


# Task 4: force_refetch
from src.common.schemas import HistoricalPriceRow  # noqa: E402


def test_force_refetch_deletes_historical_price_rows_from_date(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    rows = [
        HistoricalPriceRow(
            ticker="AAPL", observation_date=f"2026-05-{20 + i}",
            open=100.0, high=110.0, low=99.0, close=100.0 + i,
            volume=1_000_000, adj_close=100.0 + i,
            source="yahoo", scrape_timestamp="2026-05-21T08:00:00Z",
        )
        for i in range(5)
    ]
    mgr.insert_historical_price(rows)
    mgr.upsert_watermark("yahoo", "AAPL", "historical_price",
                          last_observation_date="2026-05-24", success=True)

    deleted = mgr.force_refetch("yahoo", "AAPL", "historical_price", from_date="2026-05-22")
    assert deleted == 3

    with sqlite3.connect(db_path) as c:
        remaining = sorted(r[0] for r in c.execute(
            "SELECT observation_date FROM historical_price WHERE ticker='AAPL'"
        ))
    assert remaining == ["2026-05-20", "2026-05-21"]


def test_force_refetch_resets_watermark_last_observation_date(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.upsert_watermark("yahoo", "AAPL", "historical_price",
                          last_observation_date="2026-05-24", success=True)
    mgr.force_refetch("yahoo", "AAPL", "historical_price", from_date="2026-05-22")
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w["last_observation_date"] == "2026-05-21"


def test_force_refetch_logs_to_source_run_log(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.force_refetch("yahoo", "AAPL", "historical_price", from_date="2026-05-22")
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT source, status FROM source_run_log WHERE source='yahoo' AND status='force_refetch'"
        ).fetchall()
    assert len(rows) == 1


def test_force_refetch_with_no_existing_rows_does_not_raise(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    deleted = mgr.force_refetch("yahoo", "ZZZZ", "historical_price", from_date="2026-05-22")
    assert deleted == 0
