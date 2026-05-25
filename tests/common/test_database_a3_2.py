"""Tests for Phase A.3.2 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawYahooRow


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


def test_migrate_to_v4_creates_raw_yahoo_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    assert "raw_yahoo" in _table_names(db_path)


def test_raw_yahoo_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    cols = _columns(db_path, "raw_yahoo")
    expected = {
        "run_id", "ticker",
        "company_name", "sector", "industry", "exchange",
        "market_cap", "price", "avg_daily_volume",
        "pe_ttm", "pe_forward", "ebit_ttm", "fcf_ttm",
        "total_debt", "cash", "book_value",
        "operating_margin", "net_profit_margin",
        "revenue_growth_yoy", "eps_growth_yoy",
        "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v4_bumps_schema_version_to_4(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4]


def test_migrate_to_v4_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    mgr.migrate_to_v4()
    mgr.migrate_to_v4()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4]


def test_get_schema_version_returns_4_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    assert mgr.get_schema_version() == 4


def test_migrate_to_v4_preserves_v3_data(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-20", success=True,
    )
    mgr.migrate_to_v4()
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w is not None
    assert w["last_observation_date"] == "2026-05-20"


def test_insert_raw_yahoo_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    row = RawYahooRow(
        run_id="r1", ticker="AAPL",
        company_name="Apple Inc", sector="Technology",
        market_cap=3.0e12, pe_ttm=24.5,
        operating_margin=0.30,
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    mgr.insert_raw_yahoo([row])
    with sqlite3.connect(db_path) as c:
        result = c.execute(
            "SELECT ticker, company_name, pe_ttm FROM raw_yahoo WHERE run_id='r1'"
        ).fetchone()
    assert result == ("AAPL", "Apple Inc", 24.5)


def test_insert_raw_yahoo_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    mgr.insert_raw_yahoo([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_yahoo").fetchone()[0]
    assert n == 0
