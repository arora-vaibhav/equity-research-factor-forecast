"""Tests for YahooSource fetch methods (Phase A.3.2)."""
from __future__ import annotations

import datetime
from pathlib import Path

import pandas as pd
import pytest

from src.common.database import DatabaseManager
from src.common.datasources.yahoo_source import YahooSource


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v4()
    return mgr


def _fake_history_df():
    idx = pd.DatetimeIndex(["2026-05-18", "2026-05-19", "2026-05-20"])
    return pd.DataFrame(
        {
            "Open":  [148.0, 149.0, 150.0],
            "High":  [152.0, 153.0, 154.0],
            "Low":   [147.5, 148.5, 149.5],
            "Close": [151.2, 152.5, 153.8],
            "Volume": [50_000_000, 48_000_000, 52_000_000],
            "Adj Close": [151.2, 152.5, 153.8],
        },
        index=idx,
    )


def test_fetch_historical_price_writes_rows_to_db(db, mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = _fake_history_df()
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )

    src = YahooSource()
    n = src.fetch_historical_price("AAPL", db=db)
    assert n == 3

    import sqlite3
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT observation_date, close FROM historical_price WHERE ticker='AAPL' ORDER BY observation_date"
        ).fetchall()
    assert rows == [
        ("2026-05-18", 151.2),
        ("2026-05-19", 152.5),
        ("2026-05-20", 153.8),
    ]


def test_fetch_historical_price_updates_watermark_on_success(db, mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = _fake_history_df()
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )

    YahooSource().fetch_historical_price("AAPL", db=db)
    w = db.get_watermark("yahoo", "AAPL", "historical_price")
    assert w is not None
    assert w["last_observation_date"] == "2026-05-20"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_fetch_historical_price_uses_watermark_gap_when_present(db, mocker):
    db.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-19", success=True,
    )
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = _fake_history_df()
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )

    YahooSource().fetch_historical_price("AAPL", db=db)
    call_kwargs = mock_ticker.history.call_args.kwargs
    assert "start" in call_kwargs
    assert str(call_kwargs["start"]) == "2026-05-20"


def test_fetch_historical_price_no_gap_returns_zero_and_skips_call(db, mocker):
    today_iso = datetime.date.today().isoformat()
    db.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date=today_iso, success=True,
    )
    history_mock = mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
    )
    n = YahooSource().fetch_historical_price("AAPL", db=db)
    assert n == 0
    history_mock.assert_not_called()


def test_fetch_historical_price_records_failure_on_exception(db, mocker):
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        side_effect=RuntimeError("yahoo down"),
    )
    src = YahooSource()
    n = src.fetch_historical_price("AAPL", db=db)
    assert n == 0
    w = db.get_watermark("yahoo", "AAPL", "historical_price")
    assert w is not None
    assert w["error_count"] == 1
    assert "yahoo down" in (w["last_error_message"] or "")


# Task 5: fetch_fundamentals_for_ticker
from src.common.schemas import RawYahooRow  # noqa: E402


def _fake_info(overrides=None):
    base = {
        "longName": "Apple Inc",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "exchange": "NMS",
        "marketCap": 3_000_000_000_000,
        "regularMarketPrice": 150.0,
        "averageDailyVolume10Day": 50_000_000,
        "trailingPE": 24.5,
        "forwardPE": 22.1,
        "ebitda": 130_000_000_000,
        "operatingCashflow": 110_000_000_000,
        "capitalExpenditures": -10_000_000_000,
        "totalDebt": 100_000_000_000,
        "totalCash": 50_000_000_000,
        "bookValue": 70.0,
        "operatingMargins": 0.30,
        "profitMargins": 0.25,
        "revenueGrowth": 0.08,
        "earningsGrowth": 0.10,
    }
    if overrides:
        base.update(overrides)
    return base


def test_fetch_fundamentals_returns_raw_yahoo_row(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.info = _fake_info()
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )
    row = YahooSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1")
    assert isinstance(row, RawYahooRow)
    assert row.ticker == "AAPL"
    assert row.company_name == "Apple Inc"
    assert row.sector == "Technology"
    assert row.pe_ttm == 24.5
    assert row.pe_forward == 22.1
    assert row.market_cap == 3_000_000_000_000


def test_fetch_fundamentals_handles_missing_fields(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.info = {"longName": "Acme Corp"}
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )
    row = YahooSource().fetch_fundamentals_for_ticker("ACME", run_id="r1")
    assert row.company_name == "Acme Corp"
    assert row.pe_ttm is None
    assert row.market_cap is None


def test_fetch_fundamentals_returns_none_on_exception(mocker):
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        side_effect=RuntimeError("yahoo unauthorized"),
    )
    row = YahooSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1")
    assert row is None


def test_fetch_fundamentals_computes_fcf_from_ocf_minus_capex(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.info = _fake_info({
        "operatingCashflow": 110_000_000_000,
        "capitalExpenditures": -10_000_000_000,
    })
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )
    row = YahooSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1")
    assert row.fcf_ttm == 100_000_000_000


# Task 6: fetch_universe (batch)
def test_fetch_universe_returns_dataframe_with_one_row_per_ticker(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.info = _fake_info()
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )
    df = YahooSource().fetch_universe(
        run_id="r1",
        ticker_list=["AAPL", "MSFT", "GOOG"],
    )
    assert len(df) == 3
    assert sorted(df["ticker"].tolist()) == ["AAPL", "GOOG", "MSFT"]
    assert (df["run_id"] == "r1").all()


def test_fetch_universe_skips_tickers_with_no_info(mocker):
    def side_effect(ticker):
        t = mocker.MagicMock()
        t.info = _fake_info() if ticker == "GOOD" else {}
        return t
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        side_effect=side_effect,
    )
    df = YahooSource().fetch_universe(
        run_id="r1",
        ticker_list=["GOOD", "BAD"],
    )
    assert len(df) == 1
    assert df["ticker"].iloc[0] == "GOOD"


def test_fetch_universe_empty_list_returns_empty_dataframe(mocker):
    mocker.patch("src.common.datasources.yahoo_source.yf.Ticker")
    df = YahooSource().fetch_universe(run_id="r1", ticker_list=[])
    assert df.empty


def test_fetch_universe_continues_on_per_ticker_exception(mocker):
    def side_effect(ticker):
        if ticker == "BAD":
            raise RuntimeError("yahoo blew up on BAD")
        t = mocker.MagicMock()
        t.info = _fake_info()
        return t
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        side_effect=side_effect,
    )
    df = YahooSource().fetch_universe(
        run_id="r1",
        ticker_list=["AAPL", "BAD", "MSFT"],
    )
    assert len(df) == 2
    assert "BAD" not in df["ticker"].tolist()
