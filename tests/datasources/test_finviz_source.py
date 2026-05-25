"""Unit tests for FinvizSource."""
from __future__ import annotations

import pytest

from src.common.datasources.base import SourceStatus
from src.common.datasources.finviz_source import FinvizSource


class TestFinvizSourceMeta:
    def test_name(self):
        assert FinvizSource().name == "finviz"

    def test_cadence(self):
        assert FinvizSource().cadence == "weekly"

    def test_provides_includes_expected_fields(self):
        src = FinvizSource()
        for f in ("pe_ttm", "operating_margin", "net_profit_margin",
                  "perf_12m", "rsi_14", "dist_52w_high", "dist_52w_low",
                  "sector", "industry", "company_name",
                  "market_cap_usd", "price", "avg_daily_volume"):
            assert f in src.provides, f"missing {f}"


class TestFinvizSourceHealthCheck:
    def test_health_check_returns_ok(self, mocker):
        mock_df = mocker.MagicMock(empty=False)
        mock_df.__len__ = lambda self: 10
        mock_ov = mocker.MagicMock()
        mock_ov.screener_view.return_value = mock_df
        mocker.patch(
            "src.common.datasources.finviz_source.Overview",
            return_value=mock_ov,
        )
        result = FinvizSource().health_check()
        assert result.source == "finviz"
        assert result.status == SourceStatus.OK
        assert result.is_healthy

    def test_health_check_handles_exception(self, mocker):
        mocker.patch(
            "src.common.datasources.finviz_source.Overview",
            side_effect=RuntimeError("upstream broke"),
        )
        result = FinvizSource().health_check()
        assert result.status == SourceStatus.FAILED
        assert "upstream broke" in result.message
        assert not result.is_healthy
