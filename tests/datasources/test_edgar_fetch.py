"""Tests for EdgarSource fetch methods (Phase A.3.3)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.edgar_source import EdgarSource


FIXTURE_CF = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "CIK0000320193.json"
FIXTURE_CT = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "company_tickers.json"


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v5()
    return mgr


@pytest.fixture
def cf_blob():
    return json.loads(FIXTURE_CF.read_text())


@pytest.fixture
def ct_blob():
    return json.loads(FIXTURE_CT.read_text())


def _mock_sec_get(mocker, ct_blob, cf_blob):
    """Wire requests.get to return company_tickers.json or the companyfacts
    blob based on URL substring."""
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.ok = True
        resp.status_code = 200
        if "company_tickers" in url:
            resp.json.return_value = ct_blob
        elif "companyfacts" in url:
            resp.json.return_value = cf_blob
        else:
            resp.ok = False
            resp.status_code = 404
        return resp
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        side_effect=side_effect,
    )
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )


def test_fetch_fundamentals_writes_rows_to_db(db, mocker, ct_blob, cf_blob):
    _mock_sec_get(mocker, ct_blob, cf_blob)
    src = EdgarSource()
    n = src.fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT fiscal_year, fiscal_period FROM raw_edgar_fundamentals "
            "WHERE ticker='AAPL' ORDER BY fiscal_year, fiscal_period"
        ).fetchall()
    assert (2023, "annual") in rows
    assert (2024, "annual") in rows
    assert (2024, "Q1") in rows


def test_fetch_fundamentals_updates_watermark_on_success(db, mocker, ct_blob, cf_blob):
    _mock_sec_get(mocker, ct_blob, cf_blob)
    EdgarSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    w = db.get_watermark("edgar", "AAPL", "xbrl_fundamentals")
    assert w is not None
    # last_observation_date is the most recent filing_date in the blob
    assert w["last_observation_date"] == "2024-11-01"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_fetch_fundamentals_skips_when_already_fetched_today(db, mocker, ct_blob, cf_blob):
    """Re-running on a ticker whose watermark was last_fetched_at today should
    be a no-op (returns 0, does not call companyfacts)."""
    # Seed CIK map + watermark as if a previous fetch already happened today
    db.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."}
    ])
    import datetime as _dt
    today = _dt.date.today().isoformat()
    db.upsert_watermark(
        source="sec", ticker="*", field="ticker_cik_map",
        last_observation_date=today, success=True,
    )
    db.upsert_watermark(
        source="edgar", ticker="AAPL", field="xbrl_fundamentals",
        last_observation_date="2024-11-01", success=True,
    )
    # Do NOT mock companyfacts — assert it is never called
    get_mock = mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
    )
    n = EdgarSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    assert n == 0
    get_mock.assert_not_called()


def test_fetch_fundamentals_records_failure_on_exception(db, mocker, ct_blob):
    # CIK lookup succeeds
    db.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."}
    ])
    import datetime as _dt
    today = _dt.date.today().isoformat()
    db.upsert_watermark(
        source="sec", ticker="*", field="ticker_cik_map",
        last_observation_date=today, success=True,
    )
    # companyfacts blows up
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=RuntimeError("sec 503"),
    )
    n = EdgarSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    assert n == 0
    w = db.get_watermark("edgar", "AAPL", "xbrl_fundamentals")
    assert w is not None
    assert w["error_count"] >= 1
    assert "sec 503" in (w["last_error_message"] or "")


def test_fetch_fundamentals_unknown_ticker_records_failure(db, mocker, ct_blob, cf_blob):
    _mock_sec_get(mocker, ct_blob, cf_blob)
    src = EdgarSource()
    n = src.fetch_fundamentals_for_ticker("NOTREAL", run_id="r1", db=db)
    assert n == 0
    w = db.get_watermark("edgar", "NOTREAL", "xbrl_fundamentals")
    assert w is not None
    assert w["error_count"] >= 1


def test_fetch_fundamentals_uses_sec_user_agent(db, mocker, ct_blob, cf_blob):
    captured_calls = []
    def side_effect(url, **kwargs):
        captured_calls.append((url, kwargs.get("headers", {})))
        resp = mocker.MagicMock()
        resp.ok = True
        resp.status_code = 200
        if "company_tickers" in url:
            resp.json.return_value = ct_blob
        elif "companyfacts" in url:
            resp.json.return_value = cf_blob
        return resp
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        side_effect=side_effect,
    )
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )
    EdgarSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    cf_call = next(c for c in captured_calls if "companyfacts" in c[0])
    headers = cf_call[1]
    assert "User-Agent" in headers
    assert "@" in headers["User-Agent"]


def test_fetch_fundamentals_calls_correct_companyfacts_url(db, mocker, ct_blob, cf_blob):
    captured_urls = []
    def side_effect(url, **kwargs):
        captured_urls.append(url)
        resp = mocker.MagicMock()
        resp.ok = True
        resp.status_code = 200
        if "company_tickers" in url:
            resp.json.return_value = ct_blob
        elif "companyfacts" in url:
            resp.json.return_value = cf_blob
        return resp
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        side_effect=side_effect,
    )
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )
    EdgarSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    cf_urls = [u for u in captured_urls if "companyfacts" in u]
    assert len(cf_urls) == 1
    assert "CIK0000320193.json" in cf_urls[0]


def test_fetch_fundamentals_insert_or_ignore_protects_against_dup(db, mocker, ct_blob, cf_blob):
    _mock_sec_get(mocker, ct_blob, cf_blob)
    src = EdgarSource()
    n1 = src.fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    # Force a re-fetch by clearing the same-day short-circuit (set last_fetched_at to yesterday)
    import datetime as _dt
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat() + "T00:00:00"
    with sqlite3.connect(db.db_path) as c:
        c.execute(
            "UPDATE fetch_watermarks SET last_fetched_at = ? WHERE source='edgar' AND ticker='AAPL'",
            (yesterday,),
        )
        c.commit()
    n2 = src.fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    with sqlite3.connect(db.db_path) as c:
        total = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_fundamentals WHERE run_id='r1' AND ticker='AAPL'"
        ).fetchone()[0]
    assert total == n1, "INSERT OR IGNORE should prevent duplicates on the same PK"


# Task 6: fetch_universe batch
def test_fetch_universe_returns_dataframe_aggregating_all_tickers(db, mocker, ct_blob, cf_blob):
    _mock_sec_get(mocker, ct_blob, cf_blob)
    df = EdgarSource().fetch_universe(
        run_id="r1", ticker_list=["AAPL", "MSFT"], db=db,
    )
    import pandas as pd
    assert isinstance(df, pd.DataFrame)
    # 2 tickers; both resolve to the same companyfacts mock so we get rows
    # for each ticker.
    assert "ticker" in df.columns
    assert sorted(df["ticker"].unique().tolist()) == ["AAPL", "MSFT"]


def test_fetch_universe_skips_bad_tickers(db, mocker, ct_blob, cf_blob):
    _mock_sec_get(mocker, ct_blob, cf_blob)
    df = EdgarSource().fetch_universe(
        run_id="r1", ticker_list=["AAPL", "FAKE", "MSFT"], db=db,
    )
    assert "FAKE" not in df["ticker"].unique().tolist()
    assert "AAPL" in df["ticker"].unique().tolist()
    assert "MSFT" in df["ticker"].unique().tolist()


def test_fetch_universe_empty_list_returns_empty_dataframe(db, mocker):
    mocker.patch("src.common.datasources.edgar_source.requests.get")
    df = EdgarSource().fetch_universe(run_id="r1", ticker_list=[], db=db)
    assert df.empty


def test_fetch_universe_continues_on_per_ticker_exception(db, mocker, cf_blob):
    """If one ticker's companyfacts call raises, the batch continues for others."""
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

    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        if "CIK0000320193" in url:
            resp.ok = True
            resp.status_code = 200
            resp.json.return_value = cf_blob
        elif "CIK0000789019" in url:
            raise RuntimeError("MSFT blew up")
        else:
            resp.ok = False
            resp.status_code = 404
        return resp

    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )
    df = EdgarSource().fetch_universe(
        run_id="r1", ticker_list=["AAPL", "MSFT"], db=db,
    )
    tickers = df["ticker"].unique().tolist()
    assert "AAPL" in tickers
    assert "MSFT" not in tickers
