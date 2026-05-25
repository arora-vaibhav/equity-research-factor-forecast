"""Unit tests for FinraSource."""
from __future__ import annotations

import pytest

from src.common.datasources.base import SourceStatus
from src.common.datasources.finra_source import FinraSource


class TestFinraSourceMeta:
    def test_name(self):
        assert FinraSource().name == "finra"

    def test_cadence_is_biweekly(self):
        assert FinraSource().cadence == "biweekly"

    def test_provides_short_interest(self):
        assert "short_interest_pct_float" in FinraSource().provides


class TestFinraSourceHealthCheck:
    def test_health_check_ok(self, mocker):
        mock_resp = mocker.MagicMock(status_code=200, ok=True)
        mock_resp.text = "OK"
        mocker.patch(
            "src.common.datasources.finra_source.requests.get",
            return_value=mock_resp,
        )
        result = FinraSource().health_check()
        assert result.status == SourceStatus.OK

    def test_health_check_failed_on_5xx(self, mocker):
        mock_resp = mocker.MagicMock(status_code=502, ok=False)
        mocker.patch(
            "src.common.datasources.finra_source.requests.get",
            return_value=mock_resp,
        )
        result = FinraSource().health_check()
        assert result.status == SourceStatus.FAILED
