"""Tests for ticker->CIK resolver (Phase A.3.3)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.sec_cik_lookup import (
    SecCikLookup,
    TickerNotFoundError,
)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "company_tickers.json"


@pytest.fixture
def fixture_blob():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v5()
    return mgr


def test_resolve_returns_padded_cik_for_known_ticker(db, mocker, fixture_blob):
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    lookup = SecCikLookup(db=db)
    assert lookup.resolve("AAPL") == "0000320193"
    assert lookup.resolve("MSFT") == "0000789019"


def test_resolve_is_case_insensitive(db, mocker, fixture_blob):
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    assert SecCikLookup(db=db).resolve("aapl") == "0000320193"


def test_resolve_unknown_ticker_raises(db, mocker, fixture_blob):
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    with pytest.raises(TickerNotFoundError):
        SecCikLookup(db=db).resolve("FAKE")


def test_resolve_fetches_only_once_per_process(db, mocker, fixture_blob):
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    get_mock = mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    lookup = SecCikLookup(db=db)
    lookup.resolve("AAPL")
    lookup.resolve("MSFT")
    lookup.resolve("GOOGL")
    assert get_mock.call_count == 1


def test_resolve_persists_to_sec_ticker_cik_map(db, mocker, fixture_blob):
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    SecCikLookup(db=db).resolve("AAPL")
    assert db.get_cik_for_ticker("AAPL") == "0000320193"
    assert db.get_cik_for_ticker("MSFT") == "0000789019"


def test_resolve_uses_db_cache_when_watermark_fresh(db, mocker):
    """If watermark (sec, *, ticker_cik_map) is from today, skip the HTTP call
    and use the DB snapshot."""
    db.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."}
    ])
    import datetime as _dt
    today = _dt.date.today().isoformat()
    db.upsert_watermark(
        source="sec", ticker="*", field="ticker_cik_map",
        last_observation_date=today, success=True,
    )
    get_mock = mocker.patch("src.common.datasources.sec_cik_lookup.requests.get")
    assert SecCikLookup(db=db).resolve("AAPL") == "0000320193"
    get_mock.assert_not_called()


def test_resolve_refetches_when_watermark_stale(db, mocker, fixture_blob):
    """If watermark is older than 1 day, refresh."""
    db.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000000000", "company_name": "Stale"}
    ])
    db.upsert_watermark(
        source="sec", ticker="*", field="ticker_cik_map",
        last_observation_date="2020-01-01", success=True,
    )
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    assert SecCikLookup(db=db).resolve("AAPL") == "0000320193"


def test_resolve_uses_sec_user_agent_header(db, mocker, fixture_blob):
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    get_mock = mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    SecCikLookup(db=db).resolve("AAPL")
    kwargs = get_mock.call_args.kwargs
    headers = kwargs.get("headers") or {}
    assert "User-Agent" in headers
    assert "@" in headers["User-Agent"]
