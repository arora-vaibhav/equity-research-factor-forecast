"""Unit tests for StockanalysisSource."""
from __future__ import annotations

import pytest

from src.common.datasources.base import SourceStatus
from src.common.datasources.stockanalysis_source import StockanalysisSource


class TestStockanalysisSourceMeta:
    def test_name(self):
        assert StockanalysisSource().name == "stockanalysis"

    def test_cadence(self):
        assert StockanalysisSource().cadence == "weekly"

    def test_provides_history_fields(self):
        src = StockanalysisSource()
        for f in ("pe_ttm", "pe_5y_history_raw", "ev_ebitda_5y_history_raw"):
            assert f in src.provides


class TestStockanalysisSourceHealthCheck:
    def test_health_check_ok(self, mocker):
        mock_resp = mocker.MagicMock(status_code=200, ok=True)
        mock_resp.content = b"<html><body>AAPL ratios</body></html>"
        mocker.patch(
            "src.common.datasources.stockanalysis_source.requests.get",
            return_value=mock_resp,
        )
        result = StockanalysisSource().health_check()
        assert result.status == SourceStatus.OK

    def test_health_check_fails_on_404(self, mocker):
        mock_resp = mocker.MagicMock(status_code=404, ok=False)
        mocker.patch(
            "src.common.datasources.stockanalysis_source.requests.get",
            return_value=mock_resp,
        )
        result = StockanalysisSource().health_check()
        assert result.status == SourceStatus.FAILED
