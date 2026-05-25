"""Tests for Phase A.3.9 database migration + insert helper.

Covers:
  * migrate_to_v12 (initial ship, schema bump, table DDL, indexes,
    idempotence, v11 preservation).
  * migrate_to_v13 (R2 reconciliation 2026-05-23: rename
    'item_1a_found' -> 'item_1a_extracted', accept new statuses
    'dictionary_missing' + 'fetch_failed', NULL-allowed counts;
    idempotent; preserves v12 rows with status remap).
  * insert_raw_edgar_filing_tone (INSERT OR IGNORE on
    (cik, source_filing_accn, lm_dictionary_version)).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import (
    RawEdgarFilingToneRow,
    RawGdeltMention,
)


# ---------------------------------------------------------------------------
# migrate_to_v12
# ---------------------------------------------------------------------------


class TestMigrateToV12:
    def test_migration_bumps_schema_version_to_12(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v12()
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()
            assert row[0] == 12

    def test_migration_creates_raw_edgar_filing_tone(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v12()
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='raw_edgar_filing_tone'"
            ).fetchone()
            assert row is not None

    def test_table_has_expected_columns(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v12()
        with db.get_connection() as conn:
            cols = {
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(raw_edgar_filing_tone)"
                ).fetchall()
            }
        assert cols == {
            "run_id", "ticker", "cik", "source_filing_accn", "form_type",
            "filing_date", "n_positive", "n_negative", "n_uncertainty",
            "n_litigious", "total_words", "net_tone", "extraction_status",
            "lm_dictionary_version", "scrape_timestamp",
        }

    def test_pk_is_cik_accn_dictversion(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v12()
        with db.get_connection() as conn:
            pk_cols = [
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(raw_edgar_filing_tone)"
                ).fetchall()
                if r[5] > 0
            ]
        assert set(pk_cols) == {"cik", "source_filing_accn", "lm_dictionary_version"}

    def test_indexes_created(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v12()
        with db.get_connection() as conn:
            idx = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='raw_edgar_filing_tone'"
                ).fetchall()
            }
        assert "idx_raw_edgar_filing_tone_ticker" in idx
        assert "idx_raw_edgar_filing_tone_filing_date" in idx

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v12()
        db.migrate_to_v12()
        db.migrate_to_v12()
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT version FROM schema_version WHERE version = 12"
            ).fetchall()
        assert len(rows) == 1

    def test_migration_preserves_v11_data(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v11()
        db.insert_raw_gdelt([
            RawGdeltMention(
                run_id="r0",
                gkg_record_id="20260522001500-1",
                ticker="AAPL",
                mention_timestamp="2026-05-22T00:15:00Z",
                match_method="cashtag",
                source_url="https://example.com/a",
                gkg_tone_json=None,
                scrape_timestamp="2026-05-22T00:20:00Z",
            )
        ])
        db.migrate_to_v12()
        with db.get_connection() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM raw_gdelt").fetchone()[0]
            assert cnt == 1


# ---------------------------------------------------------------------------
# migrate_to_v13 -- R2 reconciliation 2026-05-23
# ---------------------------------------------------------------------------


class TestMigrateToV13:
    def test_migration_bumps_schema_version_to_13(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()
            assert row[0] == 13

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        db.migrate_to_v13()
        db.migrate_to_v13()
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT version FROM schema_version WHERE version = 13"
            ).fetchall()
        assert len(rows) == 1

    def test_columns_preserved_after_rebuild(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        with db.get_connection() as conn:
            cols = {
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(raw_edgar_filing_tone)"
                ).fetchall()
            }
        assert cols == {
            "run_id", "ticker", "cik", "source_filing_accn", "form_type",
            "filing_date", "n_positive", "n_negative", "n_uncertainty",
            "n_litigious", "total_words", "net_tone", "extraction_status",
            "lm_dictionary_version", "scrape_timestamp",
        }

    def test_indexes_recreated(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        with db.get_connection() as conn:
            idx = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='raw_edgar_filing_tone'"
                ).fetchall()
            }
        assert "idx_raw_edgar_filing_tone_ticker" in idx
        assert "idx_raw_edgar_filing_tone_filing_date" in idx

    def test_pk_preserved(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        with db.get_connection() as conn:
            pk_cols = [
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(raw_edgar_filing_tone)"
                ).fetchall()
                if r[5] > 0
            ]
        assert set(pk_cols) == {"cik", "source_filing_accn", "lm_dictionary_version"}

    def test_legacy_item_1a_found_remapped_to_extracted(
        self, tmp_path: Path
    ) -> None:
        """Pre-seed a v12 row with legacy 'item_1a_found' status, migrate
        to v13, and verify the status was remapped."""
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v12()
        # Bypass Pydantic (which rejects the legacy literal now) by
        # writing through the raw SQLite connection.
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO raw_edgar_filing_tone
                (run_id, ticker, cik, source_filing_accn, form_type,
                 filing_date, n_positive, n_negative, n_uncertainty,
                 n_litigious, total_words, net_tone, extraction_status,
                 lm_dictionary_version, scrape_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "r0", "AAPL", "0000320193", "0000320193-26-000001",
                    "10-K", "2025-11-01",
                    100, 80, 30, 20, 12000, (100 - 80) / 12000,
                    "item_1a_found",
                    "2024", "2026-05-22T00:00:00Z",
                ),
            )
            conn.commit()

        db.migrate_to_v13()

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT extraction_status FROM raw_edgar_filing_tone"
            ).fetchone()
        assert row[0] == "item_1a_extracted"

    def test_v13_accepts_new_statuses(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        for status in (
            "item_1a_extracted",
            "full_document_fallback",
            "empty_document",
            "dictionary_missing",
            "fetch_failed",
        ):
            row = RawEdgarFilingToneRow(
                run_id="r1", ticker="AAPL", cik="0000320193",
                source_filing_accn=f"acc-{status}",
                form_type="10-K", filing_date="2025-11-01",
                extraction_status=status,
                lm_dictionary_version="2024",
                scrape_timestamp="2026-05-22T00:00:00Z",
            )
            db.insert_raw_edgar_filing_tone([row])
        with db.get_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM raw_edgar_filing_tone"
            ).fetchone()[0]
        assert cnt == 5

    def test_v13_rejects_legacy_item_1a_found(self, tmp_path: Path) -> None:
        """INSERT OR IGNORE with the legacy literal is silently dropped
        by the v13 CHECK constraint."""
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO raw_edgar_filing_tone
                (run_id, ticker, cik, source_filing_accn, form_type,
                 filing_date, n_positive, n_negative, n_uncertainty,
                 n_litigious, total_words, net_tone, extraction_status,
                 lm_dictionary_version, scrape_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "r1", "AAPL", "0000320193", "0000320193-26-000001",
                    "10-K", "2025-11-01",
                    100, 80, 30, 20, 12000, (100 - 80) / 12000,
                    "item_1a_found",  # legacy literal
                    "2024", "2026-05-22T00:00:00Z",
                ),
            )
            conn.commit()
            cnt = conn.execute(
                "SELECT COUNT(*) FROM raw_edgar_filing_tone"
            ).fetchone()[0]
        assert cnt == 0

    def test_v13_allows_null_counts_for_audit_rows(
        self, tmp_path: Path
    ) -> None:
        """dictionary_missing and fetch_failed rows can carry NULL counts."""
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        row = RawEdgarFilingToneRow(
            run_id="r1", ticker="AAPL", cik="0000320193",
            source_filing_accn="acc-1", form_type="10-K",
            filing_date="2025-11-01",
            extraction_status="dictionary_missing",
            lm_dictionary_version="2024",
            scrape_timestamp="2026-05-22T00:00:00Z",
        )
        db.insert_raw_edgar_filing_tone([row])
        with db.get_connection() as conn:
            persisted = conn.execute(
                "SELECT n_positive, n_negative, total_words, net_tone "
                "FROM raw_edgar_filing_tone"
            ).fetchone()
        assert persisted == (None, None, None, None)


# ---------------------------------------------------------------------------
# insert_raw_edgar_filing_tone
# ---------------------------------------------------------------------------


def _row(**overrides):
    base = dict(
        run_id="r1",
        ticker="AAPL",
        cik="0000320193",
        source_filing_accn="0000320193-26-000010",
        form_type="10-K",
        filing_date="2026-04-30",
        n_positive=120,
        n_negative=80,
        n_uncertainty=40,
        n_litigious=25,
        total_words=15000,
        net_tone=(120 - 80) / 15000,
        extraction_status="item_1a_extracted",
        lm_dictionary_version="2024",
        scrape_timestamp="2026-05-22T01:00:00Z",
    )
    base.update(overrides)
    return RawEdgarFilingToneRow(**base)


class TestInsertRawEdgarFilingTone:
    def test_insert_round_trips(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        db.insert_raw_edgar_filing_tone([_row()])
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT ticker, n_positive, n_negative, net_tone, extraction_status "
                "FROM raw_edgar_filing_tone"
            ).fetchall()
        assert len(rows) == 1
        ticker, n_pos, n_neg, net_tone, status = rows[0]
        assert ticker == "AAPL"
        assert n_pos == 120
        assert n_neg == 80
        assert abs(net_tone - 40 / 15000) < 1e-9
        assert status == "item_1a_extracted"

    def test_empty_input_no_op(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        db.insert_raw_edgar_filing_tone([])
        with db.get_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM raw_edgar_filing_tone"
            ).fetchone()[0]
        assert cnt == 0

    def test_insert_or_ignore_on_pk_collision(self, tmp_path: Path) -> None:
        """Same (cik, accn, dictversion) twice -- only the first wins."""
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        db.insert_raw_edgar_filing_tone([_row(n_positive=120)])
        db.insert_raw_edgar_filing_tone([_row(n_positive=999)])
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT n_positive FROM raw_edgar_filing_tone"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 120

    def test_different_dict_versions_coexist(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        db.insert_raw_edgar_filing_tone([_row(lm_dictionary_version="2024")])
        db.insert_raw_edgar_filing_tone([_row(lm_dictionary_version="2025")])
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT lm_dictionary_version FROM raw_edgar_filing_tone "
                "ORDER BY lm_dictionary_version"
            ).fetchall()
        assert [r[0] for r in rows] == ["2024", "2025"]

    def test_accepts_dict_payload(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        d = _row().model_dump()
        db.insert_raw_edgar_filing_tone([d])
        with db.get_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM raw_edgar_filing_tone"
            ).fetchone()[0]
        assert cnt == 1

    def test_check_constraint_rejects_bad_extraction_status(
        self, tmp_path: Path
    ) -> None:
        """Unknown status -- INSERT OR IGNORE swallows the CHECK violation,
        so no row should be inserted."""
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v13()
        bad = _row().model_dump()
        bad["extraction_status"] = "not_a_real_status"
        db.insert_raw_edgar_filing_tone([bad])
        with db.get_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM raw_edgar_filing_tone"
            ).fetchone()[0]
        assert cnt == 0
