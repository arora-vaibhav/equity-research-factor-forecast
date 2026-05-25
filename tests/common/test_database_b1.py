"""Tests for Phase B.1 database migration + insert helpers.

Covers:
  * migrate_to_v14 (schema bump, raw_catalysts + catalyst_calendar
    DDL, indexes, CHECK constraints, idempotence, v13 preservation).
  * insert_raw_catalysts (INSERT OR IGNORE on natural PK).
  * insert_catalyst_calendar (INSERT OR IGNORE on per-run PK).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import CatalystRow, RawEdgarFilingToneRow


class TestMigrateToV14:
    def test_bumps_schema_version_to_14(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v14()
        with db.get_connection() as conn:
            v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert v == 14

    def test_creates_both_catalyst_tables(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v14()
        with db.get_connection() as conn:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('raw_catalysts','catalyst_calendar')"
                ).fetchall()
            }
        assert names == {"raw_catalysts", "catalyst_calendar"}

    def test_idempotent(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v14()
        db.migrate_to_v14()
        db.migrate_to_v14()
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT version FROM schema_version WHERE version = 14"
            ).fetchall()
        assert len(rows) == 1

    def test_preserves_v13_data(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v13()
        db.insert_raw_edgar_filing_tone([
            RawEdgarFilingToneRow(
                run_id="r", ticker="AAPL", cik="0000320193",
                source_filing_accn="acc-1", form_type="10-K",
                filing_date="2025-11-01",
                extraction_status="item_1a_extracted",
                lm_dictionary_version="2024",
                scrape_timestamp="2026-05-22T00:00:00Z",
            ),
        ])
        db.migrate_to_v14()
        with db.get_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM raw_edgar_filing_tone"
            ).fetchone()[0]
        assert cnt == 1

    def test_catalyst_type_check_rejects_bad_value(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v14()
        with pytest.raises(sqlite3.IntegrityError):
            with db.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO catalyst_calendar
                    (run_id, ticker, catalyst_type, catalyst_date,
                     source, confidence, scrape_timestamp)
                    VALUES ('r','AAPL','made_up','2026-01-01',
                            'yahoo','high','2026-05-22T00:00:00Z')
                    """
                )
                conn.commit()

    def test_confidence_check_rejects_bad_value(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v14()
        with pytest.raises(sqlite3.IntegrityError):
            with db.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO catalyst_calendar
                    (run_id, ticker, catalyst_type, catalyst_date,
                     source, confidence, scrape_timestamp)
                    VALUES ('r','AAPL','earnings','2026-01-01',
                            'yahoo','SUPER_CONFIDENT','2026-05-22T00:00:00Z')
                    """
                )
                conn.commit()


def _catalyst(**overrides):
    base = dict(
        run_id="r1",
        ticker="AAPL",
        catalyst_type="earnings",
        catalyst_date="2026-08-01",
        catalyst_description="Earnings announcement (AAPL)",
        source="yahoo",
        source_url="https://finance.yahoo.com/quote/AAPL/earnings",
        confidence="high",
        scrape_timestamp="2026-05-22T00:00:00Z",
    )
    base.update(overrides)
    return CatalystRow(**base)


class TestInsertCatalysts:
    def test_insert_raw_catalysts_round_trips(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v14()
        db.insert_raw_catalysts([_catalyst()])
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT ticker, catalyst_type, catalyst_date FROM raw_catalysts"
            ).fetchone()
        assert row == ("AAPL", "earnings", "2026-08-01")

    def test_insert_raw_catalysts_empty_no_op(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v14()
        db.insert_raw_catalysts([])
        with db.get_connection() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM raw_catalysts").fetchone()[0]
        assert cnt == 0

    def test_insert_raw_catalysts_pk_dedup(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v14()
        db.insert_raw_catalysts([_catalyst()])
        db.insert_raw_catalysts([
            _catalyst(catalyst_description="DIFFERENT DESCRIPTION"),
        ])
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT catalyst_description FROM raw_catalysts"
            ).fetchall()
        assert len(rows) == 1
        assert "Earnings announcement" in rows[0][0]

    def test_insert_catalyst_calendar_per_run_pk(self, tmp_path):
        """Same ticker/type/date across runs -> two rows."""
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v14()
        db.insert_catalyst_calendar([_catalyst(run_id="r1")])
        db.insert_catalyst_calendar([_catalyst(run_id="r2")])
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT run_id FROM catalyst_calendar ORDER BY run_id"
            ).fetchall()
        assert [r[0] for r in rows] == ["r1", "r2"]

    def test_insert_catalyst_calendar_pk_dedup_within_run(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v14()
        db.insert_catalyst_calendar([_catalyst()])
        db.insert_catalyst_calendar([_catalyst()])
        with db.get_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM catalyst_calendar"
            ).fetchone()[0]
        assert cnt == 1

    def test_check_constraint_blocks_bad_catalyst_type(self, tmp_path):
        """INSERT OR IGNORE swallows the CHECK violation."""
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v14()
        bad = _catalyst().model_dump()
        bad["catalyst_type"] = "totally_made_up"
        db.insert_catalyst_calendar([bad])
        with db.get_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM catalyst_calendar"
            ).fetchone()[0]
        assert cnt == 0

    def test_accepts_dict_payload(self, tmp_path):
        db = DatabaseManager(db_path=str(tmp_path / "t.db"))
        db.migrate_to_v14()
        db.insert_raw_catalysts([_catalyst().model_dump()])
        with db.get_connection() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM raw_catalysts").fetchone()[0]
        assert cnt == 1
