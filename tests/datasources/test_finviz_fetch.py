"""Tests for FinvizSource.fetch_universe (Phase A.3.2)."""
from __future__ import annotations

import pandas as pd
import pytest

from src.common.datasources.finviz_source import FinvizSource


def test_fetch_universe_returns_dataframe(mocker):
    fake_df = pd.DataFrame([
        {"Ticker": "AAPL", "Company": "Apple Inc", "Sector": "Technology",
         "Market Cap": "3000B", "P/E": "24.5"},
        {"Ticker": "MSFT", "Company": "Microsoft Corp", "Sector": "Technology",
         "Market Cap": "2800B", "P/E": "30.5"},
    ])
    mock_ov = mocker.MagicMock()
    mock_ov.screener_view.return_value = fake_df
    mocker.patch(
        "src.common.datasources.finviz_source.Overview",
        return_value=mock_ov,
    )

    src = FinvizSource()
    df = src.fetch_universe(run_id="r1")
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
    assert "Ticker" in df.columns
    assert "run_id" in df.columns
    assert (df["run_id"] == "r1").all()


def test_fetch_universe_stamps_scrape_timestamp(mocker):
    fake_df = pd.DataFrame([{"Ticker": "AAPL", "Company": "Apple Inc"}])
    mock_ov = mocker.MagicMock()
    mock_ov.screener_view.return_value = fake_df
    mocker.patch(
        "src.common.datasources.finviz_source.Overview",
        return_value=mock_ov,
    )

    df = FinvizSource().fetch_universe(run_id="r1")
    assert "scrape_timestamp" in df.columns
    ts = df["scrape_timestamp"].iloc[0]
    assert isinstance(ts, str)
    assert "T" in ts


def test_fetch_universe_applies_market_cap_filter(mocker):
    fake_df = pd.DataFrame([{"Ticker": "AAPL"}])
    mock_ov = mocker.MagicMock()
    mock_ov.screener_view.return_value = fake_df
    mocker.patch(
        "src.common.datasources.finviz_source.Overview",
        return_value=mock_ov,
    )
    FinvizSource().fetch_universe(run_id="r1")
    assert mock_ov.set_filter.called
    call_args_list = mock_ov.set_filter.call_args_list
    saw_market_cap = any(
        "Market Cap." in (call.kwargs.get("filters_dict") or {})
        for call in call_args_list
    )
    assert saw_market_cap


def test_fetch_universe_empty_result_returns_empty_df(mocker):
    mock_ov = mocker.MagicMock()
    mock_ov.screener_view.return_value = pd.DataFrame()
    mocker.patch(
        "src.common.datasources.finviz_source.Overview",
        return_value=mock_ov,
    )
    df = FinvizSource().fetch_universe(run_id="r1")
    assert df.empty


def test_fetch_universe_propagates_upstream_exception(mocker):
    mocker.patch(
        "src.common.datasources.finviz_source.Overview",
        side_effect=RuntimeError("upstream blocked"),
    )
    with pytest.raises(RuntimeError, match="upstream blocked"):
        FinvizSource().fetch_universe(run_id="r1")
