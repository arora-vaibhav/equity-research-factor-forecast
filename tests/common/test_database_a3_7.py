"""Tests for Phase A.3.7 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawOpenBBRow


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


# --- migrate_to_v9 ----------------------------------------------------


def test_migrate_to_v9_creates_raw_openbb_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    assert "raw_openbb" in _table_names(db_path)


def test_raw_openbb_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    cols = _columns(db_path, "raw_openbb")
    expected = {
        "run_id", "ticker", "field_name", "provider_used",
        "value", "unit", "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v9_bumps_schema_version_to_9(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_migrate_to_v9_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.migrate_to_v9()
    mgr.migrate_to_v9()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_get_schema_version_returns_9_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    assert mgr.get_schema_version() == 9


def test_migrate_to_v9_preserves_v8_data(tmp_path: Path):
    """raw_stockanalysis_ratios from v8 must survive the v9 migration."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    from src.common.schemas import RawStockanalysisRatioRow
    mgr.insert_raw_stockanalysis_ratios([
        RawStockanalysisRatioRow(
            run_id="r1", ticker="AAPL", metric="pe_ratio",
            period_end_date="2024-12-31", period_type="annual",
            value=28.45,
            source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
            scrape_timestamp="2026-05-21T08:00:00Z",
        ),
    ])
    mgr.migrate_to_v9()
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_stockanalysis_ratios WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert v == 28.45


# --- insert_raw_openbb ----------------------------------------------------


def _row(**overrides) -> RawOpenBBRow:
    base = dict(
        run_id="r1",
        ticker="AAPL",
        field_name="pe_ratio",
        provider_used="fmp",
        value=28.45,
        unit="ratio",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawOpenBBRow(**base)


def test_insert_raw_openbb_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([_row()])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT ticker, field_name, provider_used, value, unit "
            "FROM raw_openbb"
        ).fetchone()
    assert row == ("AAPL", "pe_ratio", "fmp", 28.45, "ratio")


def test_insert_raw_openbb_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_openbb").fetchone()[0]
    assert n == 0


def test_insert_raw_openbb_uses_insert_or_ignore(tmp_path: Path):
    """PK = (run_id, ticker, field_name, provider_used). First write wins on collision."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([_row(value=28.45)])
    mgr.insert_raw_openbb([_row(value=999.99)])
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_openbb "
            "WHERE run_id='r1' AND ticker='AAPL' AND field_name='pe_ratio' "
            "AND provider_used='fmp'"
        ).fetchone()[0]
    assert v == 28.45


def test_insert_raw_openbb_allows_same_field_from_two_providers(tmp_path: Path):
    """Long-format key INTENTIONALLY admits two rows for the same
    (run_id, ticker, field_name) when the providers differ — that's the
    cross-vendor cross-validation substrate the materialization layer needs."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([
        _row(provider_used="fmp", value=28.45),
        _row(provider_used="polygon", value=28.50),
        _row(provider_used="tiingo", value=28.42),
    ])
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT provider_used, value FROM raw_openbb "
            "WHERE run_id='r1' AND ticker='AAPL' AND field_name='pe_ratio' "
            "ORDER BY provider_used"
        ).fetchall()
    assert rows == [("fmp", 28.45), ("polygon", 28.50), ("tiingo", 28.42)]


def test_insert_raw_openbb_supports_multiple_fields_same_provider(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([
        _row(field_name="pe_ratio", value=28.45, unit="ratio"),
        _row(field_name="market_cap", value=3.0e12, unit="usd"),
        _row(field_name="last_price", value=180.50, unit="usd"),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM raw_openbb WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert n == 3


def test_insert_raw_openbb_handles_null_value(tmp_path: Path):
    """String-valued canonical fields (sector / industry / etc.) land in
    raw_openbb with value=NULL and the category in unit."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([
        _row(field_name="sector", value=None, unit="sector:Technology"),
    ])
    with sqlite3.connect(db_path) as c:
        v, u = c.execute(
            "SELECT value, unit FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='sector'"
        ).fetchone()
    assert v is None
    assert u == "sector:Technology"


def test_insert_raw_openbb_supports_multiple_runs_same_ticker_same_field_same_provider(tmp_path: Path):
    """The run_id discriminator in the PK means re-pulls under a fresh
    run_id append, not collide. This is intentional — it lets us reconstruct
    a per-run snapshot of provider state."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([_row(run_id="r1", value=28.45)])
    mgr.insert_raw_openbb([_row(run_id="r2", value=29.00)])
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT run_id, value FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='pe_ratio' AND provider_used='fmp' "
            "ORDER BY run_id"
        ).fetchall()
    assert rows == [("r1", 28.45), ("r2", 29.00)]
