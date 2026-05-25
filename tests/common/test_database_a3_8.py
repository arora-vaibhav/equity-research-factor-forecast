"""Tests for Phase A.3.8 database migration + insert helpers.

Covers:
  * migrate_to_v11 (schema bump, table DDL, indexes, idempotence, v10 preservation)
  * insert_raw_gdelt (INSERT OR IGNORE on (gkg_record_id, ticker))
  * insert_raw_pytrends (INSERT OR IGNORE on (term, observation_date, geo))
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import (
    ProviderCallLog,
    RawGdeltMention,
    RawPytrendsObservation,
)


# ---------------------------------------------------------------------------
# migrate_to_v11
# ---------------------------------------------------------------------------


class TestMigrateToV11:
    def test_migration_bumps_schema_version_to_11(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v11()
        assert db.get_schema_version() == 11

    def test_idempotent(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v11()
        db.migrate_to_v11()
        db.migrate_to_v11()
        assert db.get_schema_version() == 11

    def test_creates_raw_gdelt_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v11()
        with sqlite3.connect(db_path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(raw_gdelt)")}
        assert cols == {
            "run_id",
            "gkg_record_id",
            "ticker",
            "mention_timestamp",
            "match_method",
            "source_url",
            "gkg_tone_json",
            "scrape_timestamp",
        }

    def test_creates_raw_pytrends_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v11()
        with sqlite3.connect(db_path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(raw_pytrends)")}
        assert cols == {
            "run_id",
            "term",
            "observation_date",
            "svi",
            "geo",
            "source_filename",
            "scrape_timestamp",
        }

    def test_gdelt_indexes_present(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v11()
        with sqlite3.connect(db_path) as c:
            idx = {r[1] for r in c.execute("PRAGMA index_list(raw_gdelt)")}
        assert "idx_raw_gdelt_ticker" in idx
        assert "idx_raw_gdelt_mention_ts" in idx

    def test_pytrends_indexes_present(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v11()
        with sqlite3.connect(db_path) as c:
            idx = {r[1] for r in c.execute("PRAGMA index_list(raw_pytrends)")}
        assert "idx_raw_pytrends_term" in idx
        assert "idx_raw_pytrends_date" in idx

    def test_gdelt_pk_unique(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v11()
        with sqlite3.connect(db_path) as c:
            c.execute(
                "INSERT INTO raw_gdelt (run_id, gkg_record_id, ticker, "
                "mention_timestamp, match_method, scrape_timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("r1", "rec-1", "AAPL", "2026-05-22T00:15:00Z",
                 "cashtag", "2026-05-22T00:20:00Z"),
            )
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO raw_gdelt (run_id, gkg_record_id, ticker, "
                    "mention_timestamp, match_method, scrape_timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("r1", "rec-1", "AAPL", "2026-05-22T00:15:00Z",
                     "cashtag", "2026-05-22T00:20:00Z"),
                )

    def test_pytrends_pk_unique(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v11()
        with sqlite3.connect(db_path) as c:
            c.execute(
                "INSERT INTO raw_pytrends (run_id, term, observation_date, "
                "svi, geo, source_filename, scrape_timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("r1", "recession", "2026-05-17", 42.0, "US",
                 "f.json", "2026-05-22T00:20:00Z"),
            )
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO raw_pytrends (run_id, term, observation_date, "
                    "svi, geo, source_filename, scrape_timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("r1", "recession", "2026-05-17", 99.0, "US",
                     "f.json", "2026-05-22T00:20:00Z"),
                )

    def test_match_method_check_constraint(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v11()
        with sqlite3.connect(db_path) as c, pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO raw_gdelt (run_id, gkg_record_id, ticker, "
                "mention_timestamp, match_method, scrape_timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("r1", "rec-X", "AAPL", "2026-05-22T00:15:00Z",
                 "other", "2026-05-22T00:20:00Z"),
            )

    def test_svi_check_constraint(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v11()
        with sqlite3.connect(db_path) as c, pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO raw_pytrends (run_id, term, observation_date, "
                "svi, geo, source_filename, scrape_timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("r1", "recession", "2026-05-17", -1.0, "US",
                 "f.json", "2026-05-22T00:20:00Z"),
            )

    def test_preserves_v10_data(self, tmp_path: Path) -> None:
        """Inserting a provider_call_log row before v11 migration, then
        running migrate_to_v11, leaves that row in place."""
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        row = ProviderCallLog(
            source="yahoo",
            ticker="AAPL",
            field="fundamentals",
            started_at="2026-05-22T00:00:00Z",
            finished_at="2026-05-22T00:00:01Z",
            status="ok",
            bytes_returned=512,
        )
        db.insert_provider_call_log(row)
        db.migrate_to_v11()
        with sqlite3.connect(tmp_path / "test.db") as c:
            cnt = c.execute(
                "SELECT COUNT(*) FROM provider_call_log"
            ).fetchone()[0]
        assert cnt == 1


# ---------------------------------------------------------------------------
# insert_raw_gdelt
# ---------------------------------------------------------------------------


class TestInsertRawGdelt:
    def _row(self, **overrides) -> RawGdeltMention:
        base = dict(
            run_id="r1",
            gkg_record_id="20260522001500-1234",
            ticker="AAPL",
            mention_timestamp="2026-05-22T00:15:00Z",
            match_method="cashtag",
            scrape_timestamp="2026-05-22T00:20:00Z",
        )
        base.update(overrides)
        return RawGdeltMention(**base)

    def test_inserts_one_row(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v11()
        db.insert_raw_gdelt([self._row()])
        with sqlite3.connect(tmp_path / "test.db") as c:
            cnt = c.execute("SELECT COUNT(*) FROM raw_gdelt").fetchone()[0]
        assert cnt == 1

    def test_empty_list_noop(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v11()
        db.insert_raw_gdelt([])  # should not raise
        with sqlite3.connect(tmp_path / "test.db") as c:
            cnt = c.execute("SELECT COUNT(*) FROM raw_gdelt").fetchone()[0]
        assert cnt == 0

    def test_insert_or_ignore_on_pk_collision(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v11()
        db.insert_raw_gdelt([self._row()])
        # Second call with same PK is silently dropped (no exception).
        db.insert_raw_gdelt([self._row()])
        with sqlite3.connect(tmp_path / "test.db") as c:
            cnt = c.execute("SELECT COUNT(*) FROM raw_gdelt").fetchone()[0]
        assert cnt == 1


# ---------------------------------------------------------------------------
# insert_raw_pytrends
# ---------------------------------------------------------------------------


class TestInsertRawPytrends:
    def _row(self, **overrides) -> RawPytrendsObservation:
        base = dict(
            run_id="r1",
            term="recession",
            observation_date="2026-05-17",
            svi=42.0,
            source_filename="pytrends_today_12-m.json",
            scrape_timestamp="2026-05-22T00:20:00Z",
        )
        base.update(overrides)
        return RawPytrendsObservation(**base)

    def test_inserts_one_row(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v11()
        db.insert_raw_pytrends([self._row()])
        with sqlite3.connect(tmp_path / "test.db") as c:
            cnt = c.execute("SELECT COUNT(*) FROM raw_pytrends").fetchone()[0]
        assert cnt == 1

    def test_empty_list_noop(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v11()
        db.insert_raw_pytrends([])
        with sqlite3.connect(tmp_path / "test.db") as c:
            cnt = c.execute("SELECT COUNT(*) FROM raw_pytrends").fetchone()[0]
        assert cnt == 0

    def test_insert_or_ignore_on_pk_collision(self, tmp_path: Path) -> None:
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v11()
        db.insert_raw_pytrends([self._row()])
        # Different svi but same PK → silently ignored, original kept.
        db.insert_raw_pytrends([self._row(svi=99.0)])
        with sqlite3.connect(tmp_path / "test.db") as c:
            cnt, svi = c.execute(
                "SELECT COUNT(*), MAX(svi) FROM raw_pytrends"
            ).fetchone()
        assert cnt == 1
        assert svi == 42.0
