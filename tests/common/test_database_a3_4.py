"""Tests for Phase A.3.4 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawEdgarInsiderRow, RawEdgarFilingRow


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


# --- migrate_to_v6 ----------------------------------------------------


def test_migrate_to_v6_creates_raw_edgar_insider_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    assert "raw_edgar_insider" in _table_names(db_path)


def test_migrate_to_v6_creates_raw_edgar_filings_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    assert "raw_edgar_filings" in _table_names(db_path)


def test_raw_edgar_insider_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    cols = _columns(db_path, "raw_edgar_insider")
    expected = {
        "run_id", "ticker", "cik",
        "filer_name", "filer_title",
        "filer_is_officer", "filer_is_director", "filer_is_10pct_owner",
        "transaction_date", "transaction_code", "transaction_code_description",
        "shares", "price_per_share", "total_value", "shares_after_transaction",
        "is_opportunistic", "opportunistic_classifier_version", "is_derivative",
        "source_filing_accn", "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_raw_edgar_filings_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    cols = _columns(db_path, "raw_edgar_filings")
    expected = {
        "run_id", "ticker", "cik", "form_type",
        "filing_date", "accepted_at", "item_codes",
        "source_filing_accn", "primary_doc_url", "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v6_bumps_schema_version_to_6(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6]


def test_migrate_to_v6_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.migrate_to_v6()
    mgr.migrate_to_v6()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6]


def test_get_schema_version_returns_6_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    assert mgr.get_schema_version() == 6


def test_migrate_to_v6_preserves_v5_data(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."}
    ])
    mgr.migrate_to_v6()
    assert mgr.get_cik_for_ticker("AAPL") == "0000320193"


# --- insert_raw_edgar_insider -----------------------------------------


def _insider(**overrides):
    base = dict(
        run_id="r1", ticker="AAPL", cik="0000320193",
        filer_name="COOK TIMOTHY D", filer_title="CEO",
        filer_is_officer=True, filer_is_director=True, filer_is_10pct_owner=False,
        transaction_date="2026-03-15", transaction_code="P",
        transaction_code_description="Open-market purchase",
        shares=1000.0, price_per_share=175.0, total_value=175000.0,
        shares_after_transaction=3279000.0,
        is_opportunistic=True, opportunistic_classifier_version="1.0",
        is_derivative=False,
        source_filing_accn="0000320193-26-000045",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawEdgarInsiderRow(**base)


def test_insert_raw_edgar_insider_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_insider([_insider()])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT filer_name, transaction_code, shares, is_opportunistic "
            "FROM raw_edgar_insider WHERE run_id='r1' AND ticker='AAPL'"
        ).fetchone()
    assert row == ("COOK TIMOTHY D", "P", 1000.0, 1)


def test_insert_raw_edgar_insider_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_insider([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_edgar_insider").fetchone()[0]
    assert n == 0


def test_insert_raw_edgar_insider_uses_insert_or_ignore(tmp_path: Path):
    """PK = (cik, source_filing_accn, filer_name, transaction_date, transaction_code).
    A re-insert with the same PK is silently dropped. First write wins."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_insider([_insider(shares=1000.0)])
    mgr.insert_raw_edgar_insider([_insider(shares=9999.0)])
    with sqlite3.connect(db_path) as c:
        shares = c.execute(
            "SELECT shares FROM raw_edgar_insider WHERE run_id='r1'"
        ).fetchone()[0]
    assert shares == 1000.0


def test_insert_raw_edgar_insider_supports_multiple_line_items_per_filing(tmp_path: Path):
    """One Form 4 can disclose multiple non-derivative transactions for the
    same filer, distinguished by transaction_code."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_insider([
        _insider(transaction_code="M", transaction_date="2026-03-15"),
        _insider(transaction_code="S", transaction_date="2026-03-15"),
        _insider(transaction_code="F", transaction_date="2026-03-15"),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider "
            "WHERE source_filing_accn='0000320193-26-000045'"
        ).fetchone()[0]
    assert n == 3


def test_insert_raw_edgar_insider_handles_none_classifier_fields(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_insider([
        _insider(is_opportunistic=None, opportunistic_classifier_version=None)
    ])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT is_opportunistic, opportunistic_classifier_version "
            "FROM raw_edgar_insider WHERE run_id='r1'"
        ).fetchone()
    assert row == (None, None)


# --- insert_raw_edgar_filings -----------------------------------------


def _filing(**overrides):
    base = dict(
        run_id="r1", ticker="AAPL", cik="0000320193",
        form_type="8-K", filing_date="2026-02-01",
        accepted_at="2026-02-01T16:30:01Z",
        item_codes="2.02,9.01",
        source_filing_accn="0000320193-26-000010",
        primary_doc_url="https://www.sec.gov/x.htm",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawEdgarFilingRow(**base)


def test_insert_raw_edgar_filings_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_filings([_filing()])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT form_type, item_codes FROM raw_edgar_filings WHERE run_id='r1'"
        ).fetchone()
    assert row == ("8-K", "2.02,9.01")


def test_insert_raw_edgar_filings_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_filings([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_edgar_filings").fetchone()[0]
    assert n == 0


def test_insert_raw_edgar_filings_uses_insert_or_ignore(tmp_path: Path):
    """PK = (cik, source_filing_accn). Duplicates silently dropped."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_filings([_filing(item_codes="2.02")])
    mgr.insert_raw_edgar_filings([_filing(item_codes="2.02,9.01")])
    with sqlite3.connect(db_path) as c:
        items = c.execute(
            "SELECT item_codes FROM raw_edgar_filings WHERE source_filing_accn='0000320193-26-000010'"
        ).fetchone()[0]
    assert items == "2.02"


def test_insert_raw_edgar_filings_supports_mixed_form_types(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_filings([
        _filing(form_type="8-K", source_filing_accn="0000320193-26-000010"),
        _filing(form_type="10-K", source_filing_accn="0000320193-26-000050", item_codes=None),
        _filing(form_type="4", source_filing_accn="0000320193-26-000045", item_codes=None),
    ])
    with sqlite3.connect(db_path) as c:
        forms = sorted(r[0] for r in c.execute(
            "SELECT form_type FROM raw_edgar_filings WHERE ticker='AAPL'"
        ))
    assert forms == ["10-K", "4", "8-K"]
