"""Unit tests for EdgarSource."""
from __future__ import annotations

import pytest

from src.common.datasources.base import SourceStatus
from src.common.datasources.edgar_source import EdgarSource


class TestEdgarSourceMeta:
    def test_name(self):
        assert EdgarSource().name == "edgar"

    def test_cadence(self):
        assert EdgarSource().cadence == "weekly"

    def test_provides_includes_filings_and_fundamentals(self):
        src = EdgarSource()
        for f in ("cik", "company_name", "revenue_ttm", "net_income_ttm",
                  "total_assets", "operating_margin", "net_profit_margin"):
            assert f in src.provides

    def test_user_agent_required(self):
        # SEC requires a real User-Agent per their fair-access policy.
        src = EdgarSource()
        assert "User-Agent" in src._headers()
        assert "@" in src._headers()["User-Agent"]  # must contain a contact email


class TestEdgarSourceHealthCheck:
    def test_health_check_ok(self, mocker):
        mock_resp = mocker.MagicMock(status_code=200, ok=True)
        mock_resp.json.return_value = {"name": "SEC"}
        mock_resp.content = b'{"name":"SEC"}'
        mocker.patch(
            "src.common.datasources.edgar_source.requests.get",
            return_value=mock_resp,
        )
        result = EdgarSource().health_check()
        assert result.source == "edgar"
        assert result.status == SourceStatus.OK

    def test_health_check_failed_on_non_200(self, mocker):
        mock_resp = mocker.MagicMock(status_code=503, ok=False)
        mocker.patch(
            "src.common.datasources.edgar_source.requests.get",
            return_value=mock_resp,
        )
        result = EdgarSource().health_check()
        assert result.status == SourceStatus.FAILED
        assert "503" in result.message

    def test_health_check_failed_on_exception(self, mocker):
        mocker.patch(
            "src.common.datasources.edgar_source.requests.get",
            side_effect=ConnectionError("dns failed"),
        )
        result = EdgarSource().health_check()
        assert result.status == SourceStatus.FAILED
