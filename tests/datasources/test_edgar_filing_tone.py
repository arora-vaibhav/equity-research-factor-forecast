"""End-to-end test for EdgarSource.fetch_filing_text_for_ticker (A.3.9).

Verifies:
  * Pre-seeded raw_edgar_filings rows are picked up.
  * Each pending filing's primary_doc_url is fetched (mocked).
  * Item 1A is extracted and scored against a real LM mini-dictionary.
  * RawEdgarFilingToneRow records land in raw_edgar_filing_tone with
    correct PK semantics.
  * The (edgar, ticker, filing_tone) watermark advances to the latest
    filing_date scored.
  * Re-running is a no-op (INSERT OR IGNORE + the NOT EXISTS filter
    in the SELECT short-circuits).

No live network -- HTTP is faked via mocker.patch on the module-level
`requests.get` in edgar_full_text.

Mini LM dictionary is written into tmp_path and pointed at via
`lm_dictionary_path` so the test doesn't depend on the real 9 MB CSV.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.edgar_source import EdgarSource
from src.common.schemas import RawEdgarFilingRow


MINI_CSV = (
    "Word,Negative,Positive,Uncertainty,Litigious,Strong_Modal,Weak_Modal,Constraining\n"
    "LOSS,2009,0,0,0,0,0,0\n"
    "BANKRUPTCY,2009,0,0,0,0,0,0\n"
    "LITIGATION,2009,0,0,0,2011,0,0\n"
    "STRONG,0,2009,0,0,0,0,0\n"
    "BENEFICIAL,0,2009,0,0,0,0,0\n"
    "MAYBE,0,0,2009,0,0,2011,0\n"
)

FILING_HTML = """
<html><body>
<p>Table of Contents</p>
<p>Item 1A. Risk Factors ... 12</p>
<p>Item 2. Properties ... 30</p>
<hr>
<p>Item 1A. Risk Factors</p>
<p>The company faces significant risks of loss and litigation in
the markets it serves. Bankruptcy outcomes maybe materialize.
Beneficial outcomes remain possible if execution stays strong.</p>
<p>Item 2. Properties</p>
<p>None.</p>
</body></html>
"""


def _seed_cik(db: DatabaseManager, ticker: str, cik: str) -> None:
    """Insert a ticker->CIK row so EdgarSource._resolve_cik doesn't need
    to hit SEC for the ticker map."""
    db.upsert_sec_ticker_cik_map(
        [{"ticker": ticker, "cik": cik, "company_name": "Test Co", "snapshot_at": "2026-05-22"}]
    )


def _seed_filings(
    db: DatabaseManager,
    ticker: str,
    cik: str,
    accns: list[tuple[str, str, str]],
) -> None:
    """Pre-seed raw_edgar_filings with envelope rows.

    `accns` is a list of (accn, form_type, filing_date) tuples.
    """
    rows = []
    for accn, form_type, filing_date in accns:
        rows.append(
            RawEdgarFilingRow(
                run_id="seed",
                ticker=ticker,
                cik=cik,
                form_type=form_type,
                filing_date=filing_date,
                accepted_at=None,
                item_codes=None,
                source_filing_accn=accn,
                primary_doc_url=(
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{int(cik)}/{accn.replace('-', '')}/{ticker.lower()}.htm"
                ),
                scrape_timestamp="2026-05-22T00:00:00Z",
            )
        )
    db.insert_raw_edgar_filings(rows)


@pytest.fixture
def mini_dict_path(tmp_path: Path) -> str:
    csv = tmp_path / "lm.csv"
    csv.write_text(MINI_CSV, encoding="utf-8")
    # Bust the lru_cache so fixture-fresh dicts don't collide across runs.
    from src.methodology.lm_dictionary import load_lm_dictionary
    load_lm_dictionary.cache_clear()
    return str(csv)


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    return DatabaseManager(db_path=str(tmp_path / "test.db"))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_scores_pending_filings_and_persists(mocker, db, mini_dict_path):
    db.migrate_to_v13()
    _seed_cik(db, "AAPL", "0000320193")
    _seed_filings(
        db,
        ticker="AAPL",
        cik="0000320193",
        accns=[
            ("0000320193-26-000001", "10-Q", "2025-08-01"),
            ("0000320193-26-000010", "10-K", "2025-11-01"),
        ],
    )

    # Patch the function directly so the EdgarSource's local-import
    # of fetch_filing_text gets the fake. Patching requests.get inside
    # edgar_full_text would work too but is more brittle.
    mocker.patch(
        "src.common.datasources.edgar_full_text.fetch_filing_text",
        return_value=FILING_HTML,
    )

    src = EdgarSource()
    n_processed, n_inserted = src.fetch_filing_text_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
        lm_dictionary_path=mini_dict_path,
    )

    assert n_processed == 2
    assert n_inserted == 2

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT source_filing_accn, n_positive, n_negative, "
            "extraction_status, lm_dictionary_version "
            "FROM raw_edgar_filing_tone ORDER BY filing_date"
        ).fetchall()
    assert len(rows) == 2
    for accn, n_pos, n_neg, status, dict_v in rows:
        # FILING_HTML contains BENEFICIAL + STRONG (positive) and
        # LOSS + LITIGATION + BANKRUPTCY (negative).
        assert n_pos >= 2
        assert n_neg >= 3
        assert status == "item_1a_extracted"
        assert dict_v == "2024"


def test_watermark_advances_to_latest_filing_date(mocker, db, mini_dict_path):
    db.migrate_to_v13()
    _seed_cik(db, "AAPL", "0000320193")
    _seed_filings(
        db,
        ticker="AAPL",
        cik="0000320193",
        accns=[
            ("0000320193-26-000001", "10-Q", "2025-08-01"),
            ("0000320193-26-000010", "10-K", "2025-11-01"),
        ],
    )

    mocker.patch(
        "src.common.datasources.edgar_full_text.fetch_filing_text",
        return_value=FILING_HTML,
    )

    src = EdgarSource()
    src.fetch_filing_text_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
        lm_dictionary_path=mini_dict_path,
    )

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT last_observation_date, last_fetched_at "
            "FROM fetch_watermarks "
            "WHERE source='edgar' AND ticker='AAPL' AND field='filing_tone'"
        ).fetchone()
    assert row is not None
    last_obs, last_fetched = row
    assert last_obs == "2025-11-01"
    assert last_fetched is not None


def test_re_run_is_no_op(mocker, db, mini_dict_path):
    db.migrate_to_v13()
    _seed_cik(db, "AAPL", "0000320193")
    _seed_filings(
        db,
        ticker="AAPL",
        cik="0000320193",
        accns=[("0000320193-26-000010", "10-K", "2025-11-01")],
    )

    mock_fetch = mocker.patch(
        "src.common.datasources.edgar_full_text.fetch_filing_text",
        return_value=FILING_HTML,
    )

    src = EdgarSource()
    n_proc1, n_ins1 = src.fetch_filing_text_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
        lm_dictionary_path=mini_dict_path,
    )
    n_proc2, n_ins2 = src.fetch_filing_text_for_ticker(
        ticker="AAPL", run_id="r2", db=db,
        lm_dictionary_path=mini_dict_path,
    )
    assert (n_proc1, n_ins1) == (1, 1)
    assert (n_proc2, n_ins2) == (0, 0)
    # Second call must NOT make an HTTP request (NOT EXISTS filter
    # excludes the row before we even reach the network).
    assert mock_fetch.call_count == 1


def test_form_type_filter_drops_8k_filings(mocker, db, mini_dict_path):
    db.migrate_to_v13()
    _seed_cik(db, "AAPL", "0000320193")
    _seed_filings(
        db,
        ticker="AAPL",
        cik="0000320193",
        accns=[
            ("0000320193-26-000005", "8-K", "2025-10-15"),
            ("0000320193-26-000010", "10-K", "2025-11-01"),
        ],
    )

    mock_fetch = mocker.patch(
        "src.common.datasources.edgar_full_text.fetch_filing_text",
        return_value=FILING_HTML,
    )

    src = EdgarSource()
    n_proc, n_ins = src.fetch_filing_text_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
        lm_dictionary_path=mini_dict_path,
    )
    assert n_proc == 1  # only the 10-K is in pending
    assert n_ins == 1
    assert mock_fetch.call_count == 1


def test_empty_response_persists_with_empty_document_status(
    mocker, db, mini_dict_path
):
    db.migrate_to_v13()
    _seed_cik(db, "AAPL", "0000320193")
    _seed_filings(
        db,
        ticker="AAPL",
        cik="0000320193",
        accns=[("0000320193-26-000010", "10-K", "2025-11-01")],
    )

    mocker.patch(
        "src.common.datasources.edgar_full_text.fetch_filing_text",
        return_value="",
    )

    src = EdgarSource()
    n_proc, n_ins = src.fetch_filing_text_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
        lm_dictionary_path=mini_dict_path,
    )
    assert (n_proc, n_ins) == (1, 1)
    with db.get_connection() as conn:
        status = conn.execute(
            "SELECT extraction_status FROM raw_edgar_filing_tone"
        ).fetchone()[0]
    # Empty body from fetch_filing_text is treated as fetch_failed
    # per R2 reconciliation 2026-05-23 (row persisted with status
    # 'fetch_failed' instead of falling through to empty_document
    # extraction). Prior behavior: 'empty_document'.
    assert status == "fetch_failed"


def test_missing_dictionary_persists_audit_rows(db, tmp_path):
    """When the dictionary file is missing, each pending filing gets a
    'dictionary_missing' audit row (R2 reconciliation 2026-05-23 --
    previously the call returned 0 and wrote nothing). Watermark also
    records the error."""
    db.migrate_to_v13()
    _seed_cik(db, "AAPL", "0000320193")
    _seed_filings(
        db,
        ticker="AAPL",
        cik="0000320193",
        accns=[
            ("0000320193-26-000001", "10-Q", "2025-08-01"),
            ("0000320193-26-000010", "10-K", "2025-11-01"),
        ],
    )

    src = EdgarSource()
    n_proc, n_ins = src.fetch_filing_text_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
        lm_dictionary_path=str(tmp_path / "missing.csv"),
    )
    assert (n_proc, n_ins) == (2, 2)

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT extraction_status, n_positive, net_tone "
            "FROM raw_edgar_filing_tone"
        ).fetchall()
    assert len(rows) == 2
    for status, n_pos, net_tone in rows:
        assert status == "dictionary_missing"
        assert n_pos is None
        assert net_tone is None

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT last_error_message FROM fetch_watermarks "
            "WHERE source='edgar' AND ticker='AAPL' AND field='filing_tone'"
        ).fetchone()
    assert row is not None
    assert "lm_dictionary_missing" in (row[0] or "")


def test_fetch_failure_persists_audit_row(mocker, db, mini_dict_path):
    """When fetch_filing_text raises, the filing gets a 'fetch_failed'
    audit row instead of being silently skipped. The next call sees
    nothing pending (NOT EXISTS short-circuits)."""
    db.migrate_to_v13()
    _seed_cik(db, "AAPL", "0000320193")
    _seed_filings(
        db,
        ticker="AAPL",
        cik="0000320193",
        accns=[("0000320193-26-000010", "10-K", "2025-11-01")],
    )
    mocker.patch(
        "src.common.datasources.edgar_full_text.fetch_filing_text",
        side_effect=RuntimeError("network exhausted"),
    )

    src = EdgarSource()
    n_proc, n_ins = src.fetch_filing_text_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
        lm_dictionary_path=mini_dict_path,
    )
    assert (n_proc, n_ins) == (1, 1)
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT extraction_status, n_positive, net_tone "
            "FROM raw_edgar_filing_tone"
        ).fetchone()
    assert row[0] == "fetch_failed"
    assert row[1] is None
    assert row[2] is None
