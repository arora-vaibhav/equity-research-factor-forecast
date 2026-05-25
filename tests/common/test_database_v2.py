"""Tests for migrate_to_v2() in DatabaseManager."""
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


def _view_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()
    return {r[0] for r in rows}


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as c:
        rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def test_migrate_to_v2_creates_all_expected_tables(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()

    expected_new_tables = {
        "canonical_universe",
        "thesis_objects",
        "field_provenance",
        "source_run_log",
        "historical_price",
        "historical_iv",
        "historical_earnings_reactions",
        "posterior_cache",
        "agent_response_cache",
    }
    actual = _table_names(db_path)
    missing = expected_new_tables - actual
    assert not missing, f"missing v2 tables: {missing}"


def test_migrate_to_v2_creates_raw_finviz_view(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    assert "raw_finviz" in _view_names(db_path)


def test_migrate_to_v2_bumps_schema_version_to_2(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2]


def test_migrate_to_v2_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO canonical_universe (run_id, ticker) VALUES (?, ?)",
            ("sentinel", "AAPL"),
        )
        c.commit()
    mgr.migrate_to_v2()  # second call must be safe
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT run_id, ticker FROM canonical_universe WHERE run_id='sentinel'"
        ).fetchall()
    assert rows == [("sentinel", "AAPL")]
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2]


def test_canonical_universe_has_v2_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    cols = _columns(db_path, "canonical_universe")
    expected_subset = {
        "run_id", "ticker", "company_name", "sector", "industry", "exchange", "cik",
        "market_cap_usd", "price", "avg_daily_volume",
        "pe_ttm", "pe_forward", "ebit_ttm", "fcf_ttm",
        "operating_margin", "net_profit_margin", "roe", "roic",
        "total_debt_to_equity", "interest_coverage",
        "revenue_growth_yoy", "eps_growth_yoy",
        "perf_1m", "perf_3m", "perf_6m", "perf_12m",
        "rsi_14", "dist_52w_high", "dist_52w_low", "dist_200dma",
        "short_interest_pct_float", "news_activity_score",
        "pe_5y_percentile", "ev_ebitda_5y_percentile",
        "data_quality_score", "materialized_at",
    }
    missing = expected_subset - cols
    assert not missing, f"missing columns: {missing}"


def test_field_provenance_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    cols = _columns(db_path, "field_provenance")
    expected = {
        "run_id", "ticker", "field", "source",
        "raw_value", "parsed_value", "weight",
        "contributed_to_canonical", "disagreement_pct", "fetched_at",
    }
    missing = expected - cols
    assert not missing


def test_raw_finviz_view_selects_from_finviz_universe_history(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO finviz_universe_history (Ticker, Company, scrape_timestamp) VALUES (?, ?, ?)",
            ("AAPL", "Apple Inc", "2026-05-21T08:00:00Z"),
        )
        c.commit()
        rows = c.execute("SELECT Ticker FROM raw_finviz").fetchall()
    assert ("AAPL",) in rows


def test_get_schema_version_returns_2_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    assert mgr.get_schema_version() == 2


from src.common.schemas import (  # noqa: E402
    CanonicalUniverseRow,
    FieldProvenanceRow,
    SourceRunLogRow,
    HistoricalPriceRow,
)


def test_insert_canonical_universe_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    row = CanonicalUniverseRow(
        run_id="r1", ticker="AAPL",
        company_name="Apple Inc", sector="Technology",
        pe_ttm=24.5, operating_margin=0.30,
    )
    mgr.insert_canonical_universe([row])
    with sqlite3.connect(db_path) as c:
        result = c.execute(
            "SELECT ticker, company_name, pe_ttm FROM canonical_universe WHERE run_id='r1'"
        ).fetchall()
    assert result == [("AAPL", "Apple Inc", 24.5)]


def test_insert_field_provenance_persists_rows(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    rows = [
        FieldProvenanceRow(
            run_id="r1", ticker="AAPL", field="pe_ttm", source="edgar",
            raw_value="24.5", parsed_value=24.5,
            weight=0.5, contributed_to_canonical=True,
            disagreement_pct=0.02, fetched_at="2026-05-21T08:00:00Z",
        ),
        FieldProvenanceRow(
            run_id="r1", ticker="AAPL", field="pe_ttm", source="yahoo",
            raw_value="25.1", parsed_value=25.1,
            weight=0.25, contributed_to_canonical=True,
            disagreement_pct=0.024, fetched_at="2026-05-21T08:00:01Z",
        ),
    ]
    mgr.insert_field_provenance(rows)
    with sqlite3.connect(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM field_provenance WHERE run_id='r1' AND ticker='AAPL'"
        ).fetchone()[0]
    assert n == 2


def test_insert_source_run_log_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    row = SourceRunLogRow(
        run_id="r1", source="finviz",
        started_at="2026-05-21T08:00:00Z",
        finished_at="2026-05-21T08:00:02Z",
        status="ok", rows_fetched=915,
    )
    mgr.insert_source_run_log([row])
    with sqlite3.connect(db_path) as c:
        result = c.execute(
            "SELECT source, status, rows_fetched FROM source_run_log WHERE run_id='r1'"
        ).fetchone()
    assert result == ("finviz", "ok", 915)


def test_insert_historical_price_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    row = HistoricalPriceRow(
        ticker="AAPL", observation_date="2026-05-20",
        open=148.0, high=152.0, low=147.5, close=151.2,
        volume=50_000_000, adj_close=151.2,
        source="yahoo", scrape_timestamp="2026-05-21T08:00:00Z",
    )
    mgr.insert_historical_price([row])
    with sqlite3.connect(db_path) as c:
        result = c.execute(
            "SELECT ticker, observation_date, close FROM historical_price"
        ).fetchone()
    assert result == ("AAPL", "2026-05-20", 151.2)


def test_insert_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    mgr.insert_canonical_universe([])
    mgr.insert_field_provenance([])
    mgr.insert_source_run_log([])
    mgr.insert_historical_price([])
    with sqlite3.connect(db_path) as c:
        for table in (
            "canonical_universe", "field_provenance",
            "source_run_log", "historical_price",
        ):
            n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n == 0, f"{table} should be empty"
