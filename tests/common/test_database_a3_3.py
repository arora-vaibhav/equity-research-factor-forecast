"""Tests for Phase A.3.3 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawEdgarFundamentalsRow


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


# --- migrate_to_v5 ----------------------------------------------------


def test_migrate_to_v5_creates_raw_edgar_fundamentals_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    assert "raw_edgar_fundamentals" in _table_names(db_path)


def test_migrate_to_v5_creates_sec_ticker_cik_map_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    assert "sec_ticker_cik_map" in _table_names(db_path)


def test_raw_edgar_fundamentals_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    cols = _columns(db_path, "raw_edgar_fundamentals")
    expected = {
        "run_id", "ticker", "cik",
        "fiscal_year", "fiscal_period",
        "filing_date", "accepted_at", "form_type",
        "revenue_ttm", "ebit_ttm", "net_income_ttm",
        "total_assets", "total_liabilities", "total_equity",
        "cash_and_equivalents", "total_debt",
        "operating_cashflow", "capex", "fcf_ttm",
        "operating_margin", "net_profit_margin",
        "total_debt_to_equity", "interest_coverage",
        "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_sec_ticker_cik_map_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    cols = _columns(db_path, "sec_ticker_cik_map")
    expected = {"ticker", "cik", "company_name", "snapshot_at"}
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v5_bumps_schema_version_to_5(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5]


def test_migrate_to_v5_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.migrate_to_v5()
    mgr.migrate_to_v5()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5]


def test_get_schema_version_returns_5_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    assert mgr.get_schema_version() == 5


def test_migrate_to_v5_preserves_v4_data(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-20", success=True,
    )
    mgr.migrate_to_v5()
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w is not None
    assert w["last_observation_date"] == "2026-05-20"


# --- insert_raw_edgar_fundamentals ------------------------------------


def _row(**overrides):
    base = dict(
        run_id="r1", ticker="AAPL", cik="0000320193",
        fiscal_year=2023, fiscal_period="annual",
        filing_date="2023-11-03", form_type="10-K",
        revenue_ttm=383285000000.0,
        net_income_ttm=96995000000.0,
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawEdgarFundamentalsRow(**base)


def test_insert_raw_edgar_fundamentals_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.insert_raw_edgar_fundamentals([_row()])
    with sqlite3.connect(db_path) as c:
        result = c.execute(
            "SELECT ticker, fiscal_year, fiscal_period, revenue_ttm "
            "FROM raw_edgar_fundamentals WHERE run_id='r1'"
        ).fetchone()
    assert result == ("AAPL", 2023, "annual", 383285000000.0)


def test_insert_raw_edgar_fundamentals_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.insert_raw_edgar_fundamentals([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_edgar_fundamentals").fetchone()[0]
    assert n == 0


def test_insert_raw_edgar_fundamentals_uses_insert_or_ignore(tmp_path: Path):
    """Principle 5/6: once a (run_id, ticker, fiscal_period, fiscal_year) row
    is written, a second insert with the same PK is silently ignored (i.e.,
    original value preserved). Restated financials do not overwrite the first
    observation."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.insert_raw_edgar_fundamentals([_row(revenue_ttm=383285000000.0)])
    # Try to overwrite with a "restated" value
    mgr.insert_raw_edgar_fundamentals([_row(revenue_ttm=999999999999.0)])
    with sqlite3.connect(db_path) as c:
        rev = c.execute(
            "SELECT revenue_ttm FROM raw_edgar_fundamentals WHERE run_id='r1' AND ticker='AAPL'"
        ).fetchone()[0]
    assert rev == 383285000000.0


def test_insert_raw_edgar_fundamentals_supports_multiple_periods_same_ticker(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.insert_raw_edgar_fundamentals([
        _row(fiscal_period="annual", fiscal_year=2023),
        _row(fiscal_period="Q1", fiscal_year=2024, form_type="10-Q",
             filing_date="2024-02-02"),
        _row(fiscal_period="Q2", fiscal_year=2024, form_type="10-Q",
             filing_date="2024-05-03"),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_fundamentals WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert n == 3


# --- CIK map helpers --------------------------------------------------


def test_upsert_sec_ticker_cik_map_inserts(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."},
        {"ticker": "MSFT", "cik": "0000789019", "company_name": "Microsoft Corp"},
    ])
    assert mgr.get_cik_for_ticker("AAPL") == "0000320193"
    assert mgr.get_cik_for_ticker("MSFT") == "0000789019"


def test_upsert_sec_ticker_cik_map_updates_existing(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."}
    ])
    mgr.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc. (Updated)"}
    ])
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT company_name FROM sec_ticker_cik_map WHERE ticker='AAPL'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Apple Inc. (Updated)"


def test_get_cik_for_ticker_returns_none_when_absent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    assert mgr.get_cik_for_ticker("NOPE") is None
