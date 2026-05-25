"""Tests for EdgarSource.fetch_filings_for_ticker (Phase A.3.4)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.edgar_source import EdgarSource


FIXTURE_SUB = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "submissions_CIK0000320193.json"
FIXTURE_F4 = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "form4_sample.xml"
FIXTURE_CT = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "company_tickers.json"


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v6()
    return mgr


@pytest.fixture
def sub_blob():
    return json.loads(FIXTURE_SUB.read_text())


@pytest.fixture
def form4_bytes() -> bytes:
    return FIXTURE_F4.read_bytes()


@pytest.fixture
def ct_blob():
    return json.loads(FIXTURE_CT.read_text())


def _seed_cik_map(db: DatabaseManager) -> None:
    """Pre-seed the CIK map + fresh watermark so SecCikLookup doesn't HTTP."""
    db.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."},
        {"ticker": "MSFT", "cik": "0000789019", "company_name": "Microsoft Corp"},
    ])
    import datetime as _dt
    today = _dt.date.today().isoformat()
    db.upsert_watermark(
        source="sec", ticker="*", field="ticker_cik_map",
        last_observation_date=today, success=True,
    )


def _wire_filings_mocks(mocker, sub_blob, form4_bytes):
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "submissions/CIK" in url:
            resp.json.return_value = sub_blob
            resp.content = json.dumps(sub_blob).encode()
        elif url.endswith(".xml"):
            resp.content = form4_bytes
        else:
            resp.ok = False
            resp.status_code = 404
            resp.content = b""
        return resp
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )


def test_fetch_filings_writes_filings_rows(db, mocker, sub_blob, form4_bytes):
    _seed_cik_map(db)
    _wire_filings_mocks(mocker, sub_blob, form4_bytes)
    src = EdgarSource()
    n_filings, n_insider = src.fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    assert n_filings > 0
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT form_type, COUNT(*) FROM raw_edgar_filings "
            "WHERE ticker='AAPL' GROUP BY form_type ORDER BY form_type"
        ).fetchall()
    forms = dict(rows)
    assert forms.get("4", 0) == 2
    assert forms.get("8-K", 0) == 1
    assert forms.get("10-K", 0) >= 1


def test_fetch_filings_writes_insider_rows_from_form4(db, mocker, sub_blob, form4_bytes):
    _seed_cik_map(db)
    _wire_filings_mocks(mocker, sub_blob, form4_bytes)
    n_filings, n_insider = EdgarSource().fetch_filings_for_ticker(
        "AAPL", run_id="r1", db=db,
    )
    assert n_insider > 0
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT transaction_code, COUNT(*) FROM raw_edgar_insider "
            "WHERE ticker='AAPL' GROUP BY transaction_code"
        ).fetchall()
    codes = dict(rows)
    # Two Form 4 envelopes resolve to the same XML mock; INSERT OR IGNORE
    # collapses identical (cik, accn, filer, date, code) tuples per accn,
    # so 2 distinct accns x (1 P + 1 S) = 4 rows.
    assert codes.get("P", 0) >= 1
    assert codes.get("S", 0) >= 1


def test_fetch_filings_populates_is_opportunistic(db, mocker, sub_blob, form4_bytes):
    """All parsed insider rows must have is_opportunistic set + version stamped."""
    _seed_cik_map(db)
    _wire_filings_mocks(mocker, sub_blob, form4_bytes)
    EdgarSource().fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    with sqlite3.connect(db.db_path) as c:
        n_null = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider WHERE is_opportunistic IS NULL"
        ).fetchone()[0]
        versions = {
            r[0] for r in c.execute(
                "SELECT opportunistic_classifier_version FROM raw_edgar_insider"
            )
        }
    assert n_null == 0
    assert versions == {"1.0"}


def test_fetch_filings_updates_watermark(db, mocker, sub_blob, form4_bytes):
    _seed_cik_map(db)
    _wire_filings_mocks(mocker, sub_blob, form4_bytes)
    EdgarSource().fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    w = db.get_watermark("edgar", "AAPL", "filings")
    assert w is not None
    # Latest included filing date in the fixture (DEF 14A is filtered out).
    assert w["last_observation_date"] == "2026-03-17"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_fetch_filings_skips_when_watermark_matches_latest(db, mocker, sub_blob, form4_bytes):
    """If last_observation_date >= latest filing, don't re-fetch Form 4 XMLs."""
    _seed_cik_map(db)
    db.upsert_watermark(
        source="edgar", ticker="AAPL", field="filings",
        last_observation_date="2026-03-17", success=True,
    )

    submissions_calls = []
    form4_calls = []

    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "submissions/CIK" in url:
            submissions_calls.append(url)
            resp.json.return_value = sub_blob
            resp.content = json.dumps(sub_blob).encode()
        elif url.endswith(".xml"):
            form4_calls.append(url)
            resp.content = form4_bytes
        return resp
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )

    n_filings, n_insider = EdgarSource().fetch_filings_for_ticker(
        "AAPL", run_id="r1", db=db,
    )
    assert n_filings == 0
    assert n_insider == 0
    assert len(submissions_calls) == 1
    assert form4_calls == []


def test_fetch_filings_records_failure_on_submissions_error(db, mocker):
    _seed_cik_map(db)
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=RuntimeError("submissions 503"),
    )
    n_filings, n_insider = EdgarSource().fetch_filings_for_ticker(
        "AAPL", run_id="r1", db=db,
    )
    assert (n_filings, n_insider) == (0, 0)
    w = db.get_watermark("edgar", "AAPL", "filings")
    assert w is not None
    assert w["error_count"] >= 1


def test_fetch_filings_continues_when_one_form4_xml_fails(db, mocker, sub_blob, form4_bytes):
    """If one Form 4 XML fetch fails, the batch continues for other Form 4s."""
    _seed_cik_map(db)
    call_counter = {"f4": 0}

    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "submissions/CIK" in url:
            resp.json.return_value = sub_blob
            resp.content = json.dumps(sub_blob).encode()
        elif url.endswith(".xml"):
            call_counter["f4"] += 1
            if call_counter["f4"] == 1:
                resp.content = b"not xml"
            else:
                resp.content = form4_bytes
        return resp
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )

    n_filings, n_insider = EdgarSource().fetch_filings_for_ticker(
        "AAPL", run_id="r1", db=db,
    )
    assert n_insider >= 2


def test_fetch_filings_uses_sec_user_agent(db, mocker, sub_blob, form4_bytes):
    _seed_cik_map(db)
    captured = []

    def side_effect(url, **kwargs):
        captured.append((url, kwargs.get("headers", {})))
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "submissions/CIK" in url:
            resp.json.return_value = sub_blob
            resp.content = json.dumps(sub_blob).encode()
        elif url.endswith(".xml"):
            resp.content = form4_bytes
        return resp
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )

    EdgarSource().fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    sub_call = next(c for c in captured if "submissions/CIK" in c[0])
    assert "User-Agent" in sub_call[1]
    assert "@" in sub_call[1]["User-Agent"]


def test_fetch_filings_calls_correct_submissions_url(db, mocker, sub_blob, form4_bytes):
    _seed_cik_map(db)
    captured = []

    def side_effect(url, **kwargs):
        captured.append(url)
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "submissions/CIK" in url:
            resp.json.return_value = sub_blob
            resp.content = json.dumps(sub_blob).encode()
        elif url.endswith(".xml"):
            resp.content = form4_bytes
        return resp
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )

    EdgarSource().fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    sub_urls = [u for u in captured if "submissions/CIK" in u]
    assert len(sub_urls) == 1
    assert "CIK0000320193.json" in sub_urls[0]


def test_fetch_filings_insert_or_ignore_protects_against_dup(db, mocker, sub_blob, form4_bytes):
    """Second fetch with cleared watermark re-parses but inserts no new rows."""
    _seed_cik_map(db)
    _wire_filings_mocks(mocker, sub_blob, form4_bytes)
    src = EdgarSource()
    src.fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    with sqlite3.connect(db.db_path) as c:
        n_filings_before = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_filings WHERE ticker='AAPL'"
        ).fetchone()[0]
        n_insider_before = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider WHERE ticker='AAPL'"
        ).fetchone()[0]
    db.upsert_watermark(
        source="edgar", ticker="AAPL", field="filings",
        last_observation_date=None, success=True,
    )
    src.fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    with sqlite3.connect(db.db_path) as c:
        n_filings_after = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_filings WHERE ticker='AAPL'"
        ).fetchone()[0]
        n_insider_after = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert n_filings_after == n_filings_before
    assert n_insider_after == n_insider_before
