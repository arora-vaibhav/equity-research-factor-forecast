"""End-to-end orchestration test for A.3.2: Finviz -> Yahoo handoff with watermarks.

Unit-level integration test using mocks. A separate @pytest.mark.integration
test against real network is deferred to A.5.
"""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src.common.database import DatabaseManager
from src.common.datasources.finviz_source import FinvizSource
from src.common.datasources.yahoo_source import YahooSource


def _fake_yahoo_info():
    return {
        "longName": "Apple Inc",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "exchange": "NMS",
        "marketCap": 3_000_000_000_000,
        "regularMarketPrice": 150.0,
        "trailingPE": 24.5,
        "forwardPE": 22.1,
        "operatingMargins": 0.30,
        "profitMargins": 0.25,
    }


def _fake_yahoo_history():
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


def test_finviz_to_yahoo_handoff_with_watermarks(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v4()

    fake_finviz = pd.DataFrame([
        {"Ticker": "AAPL", "Company": "Apple Inc"},
        {"Ticker": "MSFT", "Company": "Microsoft Corp"},
    ])
    mock_ov = mocker.MagicMock()
    mock_ov.screener_view.return_value = fake_finviz
    mocker.patch(
        "src.common.datasources.finviz_source.Overview",
        return_value=mock_ov,
    )

    mock_ticker = mocker.MagicMock()
    mock_ticker.info = _fake_yahoo_info()
    mock_ticker.history.return_value = _fake_yahoo_history()
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )

    finviz = FinvizSource()
    universe_df = finviz.fetch_universe(run_id="run-1")
    assert len(universe_df) == 2

    yahoo = YahooSource()
    fundamentals_df = yahoo.fetch_universe(
        run_id="run-1",
        ticker_list=universe_df["Ticker"].tolist(),
    )
    assert len(fundamentals_df) == 2

    from src.common.schemas import RawYahooRow
    rows = [RawYahooRow(**rec) for rec in fundamentals_df.to_dict(orient="records")]
    db.insert_raw_yahoo(rows)

    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_yahoo WHERE run_id='run-1'").fetchone()[0]
    assert n == 2

    for ticker in ["AAPL", "MSFT"]:
        inserted = yahoo.fetch_historical_price(ticker, db=db)
        assert inserted == 3

    with sqlite3.connect(db_path) as c:
        n_prices = c.execute("SELECT COUNT(*) FROM historical_price").fetchone()[0]
    assert n_prices == 6

    for ticker in ["AAPL", "MSFT"]:
        w = db.get_watermark("yahoo", ticker, "historical_price")
        assert w is not None
        assert w["last_observation_date"] == "2026-05-20"
        assert w["fetch_count"] == 1

    # Re-running INSERTs the same days; INSERT OR IGNORE protects against duplication.
    yahoo.fetch_historical_price("AAPL", db=db)
    with sqlite3.connect(db_path) as c:
        n_prices_after = c.execute("SELECT COUNT(*) FROM historical_price").fetchone()[0]
    assert n_prices_after == 6
