"""Tests for Phase A.3.5 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawFredObservation, RawFinraShortInterest


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


# --- migrate_to_v7 ----------------------------------------------------


def test_migrate_to_v7_creates_raw_fred_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    assert "raw_fred" in _table_names(db_path)


def test_migrate_to_v7_creates_raw_finra_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    assert "raw_finra" in _table_names(db_path)


def test_raw_fred_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    cols = _columns(db_path, "raw_fred")
    expected = {
        "run_id", "series_id", "observation_date", "value",
        "realtime_start", "realtime_end",
        "source_filename", "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_raw_finra_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    cols = _columns(db_path, "raw_finra")
    expected = {
        "run_id", "ticker", "settlement_date", "exchange",
        "short_interest_shares", "avg_daily_volume", "days_to_cover",
        "source_filename", "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v7_bumps_schema_version_to_7(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6, 7]


def test_migrate_to_v7_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.migrate_to_v7()
    mgr.migrate_to_v7()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6, 7]


def test_get_schema_version_returns_7_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    assert mgr.get_schema_version() == 7


def test_migrate_to_v7_preserves_v6_data(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."}
    ])
    mgr.migrate_to_v7()
    assert mgr.get_cik_for_ticker("AAPL") == "0000320193"


# --- insert_raw_fred --------------------------------------------------


def _fred(**overrides):
    base = dict(
        run_id="r1", series_id="DGS10",
        observation_date="2026-05-15", value=4.21,
        realtime_start=None, realtime_end=None,
        source_filename="fredgraph.csv?id=DGS10",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawFredObservation(**base)


def test_insert_raw_fred_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_fred([_fred()])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT series_id, observation_date, value FROM raw_fred"
        ).fetchone()
    assert row == ("DGS10", "2026-05-15", 4.21)


def test_insert_raw_fred_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_fred([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_fred").fetchone()[0]
    assert n == 0


def test_insert_raw_fred_uses_insert_or_ignore(tmp_path: Path):
    """PK = (series_id, observation_date). First write wins on collision."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_fred([_fred(value=4.21)])
    mgr.insert_raw_fred([_fred(value=9.99)])
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_fred WHERE series_id='DGS10'"
        ).fetchone()[0]
    assert v == 4.21


def test_insert_raw_fred_handles_null_value(tmp_path: Path):
    """FRED '.' missing-observation maps to NULL in storage."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_fred([_fred(value=None, observation_date="2026-01-01")])
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_fred WHERE observation_date='2026-01-01'"
        ).fetchone()[0]
    assert v is None


def test_insert_raw_fred_supports_multiple_series_same_date(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_fred([
        _fred(series_id="DGS10", value=4.21),
        _fred(series_id="DGS3MO", value=5.10),
        _fred(series_id="VIXCLS", value=14.2),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_fred").fetchone()[0]
    assert n == 3


# --- insert_raw_finra -------------------------------------------------


def _finra(**overrides):
    base = dict(
        run_id="r1", ticker="AAPL",
        settlement_date="2026-05-15", exchange="NSDQ",
        short_interest_shares=12_345_678.0,
        avg_daily_volume=85_000_000.0,
        days_to_cover=0.145,
        source_filename="FNSQshvol20260515.txt",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawFinraShortInterest(**base)


def test_insert_raw_finra_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_finra([_finra()])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT ticker, exchange, short_interest_shares FROM raw_finra"
        ).fetchone()
    assert row == ("AAPL", "NSDQ", 12_345_678.0)


def test_insert_raw_finra_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_finra([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_finra").fetchone()[0]
    assert n == 0


def test_insert_raw_finra_uses_insert_or_ignore(tmp_path: Path):
    """PK = (ticker, settlement_date, exchange). First write wins."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_finra([_finra(short_interest_shares=100.0)])
    mgr.insert_raw_finra([_finra(short_interest_shares=9999.0)])
    with sqlite3.connect(db_path) as c:
        v = c.execute("SELECT short_interest_shares FROM raw_finra").fetchone()[0]
    assert v == 100.0


def test_insert_raw_finra_supports_multiple_exchanges_same_ticker_date(tmp_path: Path):
    """Same ticker on the same settlement_date across different exchanges
    is allowed (the exchange column is part of the PK)."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_finra([
        _finra(exchange="NSDQ"),
        _finra(exchange="NYSE"),
        _finra(exchange="NYAX"),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_finra WHERE ticker='AAPL'").fetchone()[0]
    assert n == 3
