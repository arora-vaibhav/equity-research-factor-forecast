"""Tests for Phase A.3.6 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawStockanalysisRatioRow


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


# --- migrate_to_v8 ----------------------------------------------------


def test_migrate_to_v8_creates_raw_stockanalysis_ratios_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    assert "raw_stockanalysis_ratios" in _table_names(db_path)


def test_raw_stockanalysis_ratios_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    cols = _columns(db_path, "raw_stockanalysis_ratios")
    expected = {
        "run_id", "ticker", "metric", "period_end_date", "period_type",
        "value", "source_url", "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v8_bumps_schema_version_to_8(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8]


def test_migrate_to_v8_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.migrate_to_v8()
    mgr.migrate_to_v8()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8]


def test_get_schema_version_returns_8_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    assert mgr.get_schema_version() == 8


def test_migrate_to_v8_preserves_v7_data(tmp_path: Path):
    """raw_fred and raw_finra from v7 must survive the v8 migration."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    # Seed a raw_fred row.
    from src.common.schemas import RawFredObservation
    mgr.insert_raw_fred([
        RawFredObservation(
            run_id="r1", series_id="DGS10",
            observation_date="2026-05-12", value=4.17,
            source_filename="fredgraph.csv?id=DGS10",
            scrape_timestamp="2026-05-21T08:00:00Z",
        ),
    ])
    mgr.migrate_to_v8()
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_fred WHERE series_id='DGS10'"
        ).fetchone()[0]
    assert v == 4.17


# --- insert_raw_stockanalysis_ratios -------------------------------------


def _row(**overrides) -> RawStockanalysisRatioRow:
    base = dict(
        run_id="r1",
        ticker="AAPL",
        metric="pe_ratio",
        period_end_date="2024-12-31",
        period_type="annual",
        value=28.45,
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawStockanalysisRatioRow(**base)


def test_insert_raw_stockanalysis_ratios_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.insert_raw_stockanalysis_ratios([_row()])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT ticker, metric, period_end_date, value "
            "FROM raw_stockanalysis_ratios"
        ).fetchone()
    assert row == ("AAPL", "pe_ratio", "2024-12-31", 28.45)


def test_insert_raw_stockanalysis_ratios_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.insert_raw_stockanalysis_ratios([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_stockanalysis_ratios").fetchone()[0]
    assert n == 0


def test_insert_raw_stockanalysis_ratios_uses_insert_or_ignore(tmp_path: Path):
    """PK = (ticker, metric, period_end_date). First write wins on collision."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.insert_raw_stockanalysis_ratios([_row(value=28.45)])
    mgr.insert_raw_stockanalysis_ratios([_row(value=999.99)])
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_stockanalysis_ratios "
            "WHERE ticker='AAPL' AND metric='pe_ratio' "
            "AND period_end_date='2024-12-31'"
        ).fetchone()[0]
    assert v == 28.45


def test_insert_raw_stockanalysis_ratios_supports_multiple_metrics_same_period(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.insert_raw_stockanalysis_ratios([
        _row(metric="pe_ratio", value=28.45),
        _row(metric="ev_ebitda", value=21.10),
        _row(metric="ps_ratio", value=7.5),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM raw_stockanalysis_ratios WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert n == 3


def test_insert_raw_stockanalysis_ratios_supports_multiple_periods_same_metric(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.insert_raw_stockanalysis_ratios([
        _row(period_end_date="2024-12-31", value=28.45),
        _row(period_end_date="2023-12-31", value=25.10),
        _row(period_end_date="2022-12-31", value=22.00),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM raw_stockanalysis_ratios "
            "WHERE ticker='AAPL' AND metric='pe_ratio'"
        ).fetchone()[0]
    assert n == 3


def test_insert_raw_stockanalysis_ratios_handles_null_value(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.insert_raw_stockanalysis_ratios([_row(value=None, period_end_date="2015-12-31")])
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_stockanalysis_ratios "
            "WHERE period_end_date='2015-12-31'"
        ).fetchone()[0]
    assert v is None
