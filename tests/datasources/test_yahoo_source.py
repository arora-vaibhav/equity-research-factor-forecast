"""Unit tests for YahooSource."""
from __future__ import annotations

import pytest

from src.common.datasources.base import SourceStatus
from src.common.datasources.yahoo_source import YahooSource


class TestYahooSourceMeta:
    def test_name(self):
        assert YahooSource().name == "yahoo"

    def test_cadence(self):
        assert YahooSource().cadence == "weekly"

    def test_provides_includes_expected_fields(self):
        src = YahooSource()
        for f in ("pe_ttm", "pe_forward", "ebit_ttm", "fcf_ttm",
                  "operating_margin", "net_profit_margin",
                  "total_debt_to_equity", "roe", "roic",
                  "company_name", "sector", "industry",
                  "market_cap_usd", "price", "avg_daily_volume"):
            assert f in src.provides, f"missing {f}"


class TestYahooSourceHealthCheck:
    def test_health_check_ok(self, mocker):
        mock_ticker = mocker.MagicMock()
        mock_ticker.fast_info = {"last_price": 150.0}
        mocker.patch(
            "src.common.datasources.yahoo_source.yf.Ticker",
            return_value=mock_ticker,
        )
        result = YahooSource().health_check()
        assert result.source == "yahoo"
        assert result.status == SourceStatus.OK

    def test_health_check_failed(self, mocker):
        mocker.patch(
            "src.common.datasources.yahoo_source.yf.Ticker",
            side_effect=RuntimeError("network down"),
        )
        result = YahooSource().health_check()
        assert result.status == SourceStatus.FAILED
        assert "network down" in result.message
