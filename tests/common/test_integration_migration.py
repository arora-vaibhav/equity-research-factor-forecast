"""End-to-end migration test: A.1 data preservation + idempotency + concurrency."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager


def test_v2_migration_preserves_v1_data(tmp_path: Path):
    """Simulate an A.1-populated database, then migrate to v2, assert all
    pre-existing data is intact."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))

    mgr.migrate()
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO runs (run_id, run_timestamp, run_type, status) VALUES (?, ?, ?, ?)",
            ("legacy-run-1", "2026-05-17T10:00:00Z", "layer1", "ok"),
        )
        c.execute(
            """
            INSERT INTO universe_members
              (run_id, ticker, sector, market_cap_usd, pe_ratio)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("legacy-run-1", "MSFT", "Technology", 3.0e12, 30.5),
        )
        c.execute(
            """
            INSERT INTO candidate_results
              (run_id, ticker, playbook, composite_score, eligible, reasoning)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("legacy-run-1", "MSFT", "A", 78.0, 1, "fundamental: cheap; technical: bouncing"),
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS finviz_universe_history (
              Ticker TEXT, Company TEXT, scrape_timestamp TEXT
            )
            """
        )
        c.execute(
            "INSERT INTO finviz_universe_history (Ticker, Company, scrape_timestamp) VALUES (?, ?, ?)",
            ("MSFT", "Microsoft Corp", "2026-05-17T10:00:00Z"),
        )
        c.commit()

    mgr.migrate_to_v2()

    with sqlite3.connect(db_path) as c:
        run_rows = c.execute("SELECT run_id FROM runs WHERE run_id='legacy-run-1'").fetchall()
        assert run_rows == [("legacy-run-1",)]
        univ_rows = c.execute(
            "SELECT ticker, sector, pe_ratio FROM universe_members WHERE run_id='legacy-run-1'"
        ).fetchall()
        assert univ_rows == [("MSFT", "Technology", 30.5)]
        cand_rows = c.execute(
            "SELECT ticker, playbook, composite_score FROM candidate_results WHERE run_id='legacy-run-1'"
        ).fetchall()
        assert cand_rows == [("MSFT", "A", 78.0)]
        finviz_rows = c.execute(
            "SELECT Ticker, Company FROM finviz_universe_history WHERE Ticker='MSFT'"
        ).fetchall()
        assert finviz_rows == [("MSFT", "Microsoft Corp")]
        view_rows = c.execute("SELECT Ticker FROM raw_finviz WHERE Ticker='MSFT'").fetchall()
        assert view_rows == [("MSFT",)]


def test_v2_migration_double_call_is_safe(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    mgr.migrate_to_v2()
    mgr.migrate_to_v2()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2]


def test_v2_migration_on_brand_new_db_works(tmp_path: Path):
    db_path = tmp_path / "fresh.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    assert mgr.get_schema_version() == 2


def test_schema_version_is_2_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    assert mgr.get_schema_version() == 2


def test_partial_migration_recovers(tmp_path: Path):
    db_path = tmp_path / "partial.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate()
    assert mgr.get_schema_version() == 1
    mgr.migrate_to_v2()
    assert mgr.get_schema_version() == 2
    with sqlite3.connect(db_path) as c:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    for t in (
        "canonical_universe", "thesis_objects", "field_provenance",
        "source_run_log", "historical_price", "historical_iv",
        "historical_earnings_reactions", "posterior_cache", "agent_response_cache",
    ):
        assert t in tables, f"missing {t}"
