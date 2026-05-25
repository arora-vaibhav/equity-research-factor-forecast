"""Unit tests for FredSource."""
from __future__ import annotations

import pytest

from src.common.datasources.base import SourceStatus
from src.common.datasources.fred_source import FredSource


class TestFredSourceMeta:
    def test_name(self):
        assert FredSource().name == "fred"

    def test_cadence(self):
        assert FredSource().cadence == "weekly"

    def test_provides_macro_series(self):
        assert "risk_free_rate_10y" in FredSource().provides
        assert "vix_close" in FredSource().provides


class TestFredSourceHealthCheck:
    def test_health_check_ok(self, mocker):
        mock_resp = mocker.MagicMock(status_code=200, ok=True)
        mock_resp.text = "DATE,DGS10\n2026-05-20,4.30\n"
        mocker.patch(
            "src.common.datasources.fred_source.requests.get",
            return_value=mock_resp,
        )
        result = FredSource().health_check()
        assert result.source == "fred"
        assert result.status == SourceStatus.OK

    def test_health_check_failed_on_non_200(self, mocker):
        mock_resp = mocker.MagicMock(status_code=500, ok=False)
        mocker.patch(
            "src.common.datasources.fred_source.requests.get",
            return_value=mock_resp,
        )
        result = FredSource().health_check()
        assert result.status == SourceStatus.FAILED

    def test_health_check_failed_on_exception(self, mocker):
        mocker.patch(
            "src.common.datasources.fred_source.requests.get",
            side_effect=TimeoutError("read timeout"),
        )
        result = FredSource().health_check()
        assert result.status == SourceStatus.FAILED
