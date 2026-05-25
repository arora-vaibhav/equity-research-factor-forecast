"""Tests for BaseDataSource contract and helper types."""
from __future__ import annotations

import pytest

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


class TestSourceStatus:
    def test_has_required_values(self):
        assert SourceStatus.OK.value == "ok"
        assert SourceStatus.PARTIAL.value == "partial"
        assert SourceStatus.FAILED.value == "failed"


class TestSourceHealthStatus:
    def test_basic_fields(self):
        status = SourceHealthStatus(
            source="finviz",
            status=SourceStatus.OK,
            checked_at="2026-05-21T08:00:00Z",
            message="200 OK",
            response_time_ms=143.0,
        )
        assert status.source == "finviz"
        assert status.status == SourceStatus.OK
        assert status.message == "200 OK"
        assert status.response_time_ms == 143.0

    def test_is_healthy_property(self):
        ok = SourceHealthStatus(source="x", status=SourceStatus.OK, checked_at="t")
        partial = SourceHealthStatus(source="x", status=SourceStatus.PARTIAL, checked_at="t")
        failed = SourceHealthStatus(source="x", status=SourceStatus.FAILED, checked_at="t")
        assert ok.is_healthy is True
        assert partial.is_healthy is True
        assert failed.is_healthy is False


class TestBaseDataSource:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            BaseDataSource()  # type: ignore[abstract]

    def test_subclass_must_implement_health_check(self):
        class IncompleteSource(BaseDataSource):
            name = "incomplete"
            cadence = "weekly"
            provides = set()
        with pytest.raises(TypeError):
            IncompleteSource()  # type: ignore[abstract]

    def test_subclass_with_health_check_works(self):
        class GoodSource(BaseDataSource):
            name = "good"
            cadence = "weekly"
            provides = {"pe_ttm"}
            def health_check(self) -> SourceHealthStatus:
                return SourceHealthStatus(
                    source=self.name,
                    status=SourceStatus.OK,
                    checked_at="2026-05-21T08:00:00Z",
                )
        src = GoodSource()
        assert src.name == "good"
        assert src.health_check().is_healthy

    def test_optional_methods_raise_not_implemented_by_default(self):
        class GoodSource(BaseDataSource):
            name = "good"
            cadence = "weekly"
            provides = set()
            def health_check(self) -> SourceHealthStatus:
                return SourceHealthStatus(source="good", status=SourceStatus.OK, checked_at="t")
        src = GoodSource()
        with pytest.raises(NotImplementedError):
            src.fetch_universe(run_id="r")
        with pytest.raises(NotImplementedError):
            src.fetch_fundamentals_for_ticker("AAPL", run_id="r")
        with pytest.raises(NotImplementedError):
            src.fetch_filings_for_ticker("AAPL", run_id="r")
        with pytest.raises(NotImplementedError):
            src.fetch_macro_series(["DGS10"], run_id="r")
