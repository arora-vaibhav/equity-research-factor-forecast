"""Tests for Phase A.3.7.5 Pydantic models.

Covers `ProviderCallLog`, `ProviderQuotaState`, `FetchQueueEntry`.
"""
from __future__ import annotations

import pytest

from src.common.schemas import (
    FetchQueueEntry,
    ProviderCallLog,
    ProviderQuotaState,
)


class TestProviderCallLog:
    def test_minimal_valid(self) -> None:
        r = ProviderCallLog(
            source="yahoo",
            ticker="AAPL",
            field="fundamentals",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:01Z",
            status="ok",
            bytes_returned=4096,
            http_status=200,
        )
        assert r.source == "yahoo"
        assert r.ticker == "AAPL"
        assert r.status == "ok"
        assert r.bytes_returned == 4096
        assert r.http_status == 200
        assert r.error_message is None

    def test_call_id_optional(self) -> None:
        r = ProviderCallLog(
            source="polygon",
            ticker="AAPL",
            field="multi_provider",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:00Z",
            status="ok",
            bytes_returned=0,
        )
        assert r.call_id is None

    def test_ticker_uppercased(self) -> None:
        r = ProviderCallLog(
            source="yahoo",
            ticker="aapl",
            field="fundamentals",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:01Z",
            status="ok",
            bytes_returned=0,
        )
        assert r.ticker == "AAPL"

    def test_ticker_none_for_macro_field(self) -> None:
        r = ProviderCallLog(
            source="fred",
            ticker=None,
            field="macro:UNRATE",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:01Z",
            status="ok",
            bytes_returned=512,
        )
        assert r.ticker is None

    def test_invalid_ticker_rejected(self) -> None:
        with pytest.raises(Exception):
            ProviderCallLog(
                source="yahoo",
                ticker="not-a-ticker!",
                field="fundamentals",
                started_at="2026-05-21T08:00:00Z",
                finished_at="2026-05-21T08:00:01Z",
                status="ok",
                bytes_returned=0,
            )

    def test_status_literal_enforced(self) -> None:
        with pytest.raises(Exception):
            ProviderCallLog(
                source="yahoo",
                ticker="AAPL",
                field="fundamentals",
                started_at="2026-05-21T08:00:00Z",
                finished_at="2026-05-21T08:00:01Z",
                status="weird-state",
                bytes_returned=0,
            )

    def test_error_status_carries_error_message(self) -> None:
        r = ProviderCallLog(
            source="polygon",
            ticker="AAPL",
            field="multi_provider",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:12Z",
            status="failed",
            bytes_returned=0,
            error_message="HTTPError 429: rate limited",
            http_status=429,
        )
        assert r.status == "failed"
        assert r.error_message == "HTTPError 429: rate limited"
        assert r.http_status == 429

    def test_bytes_returned_nonneg(self) -> None:
        with pytest.raises(Exception):
            ProviderCallLog(
                source="yahoo",
                ticker="AAPL",
                field="fundamentals",
                started_at="2026-05-21T08:00:00Z",
                finished_at="2026-05-21T08:00:01Z",
                status="ok",
                bytes_returned=-1,
            )

    def test_http_status_optional(self) -> None:
        r = ProviderCallLog(
            source="yahoo",
            ticker="AAPL",
            field="fundamentals",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:30Z",
            status="failed",
            bytes_returned=0,
            error_message="ConnectionTimeoutError",
            http_status=None,
        )
        assert r.http_status is None

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(Exception):
            ProviderCallLog(
                source="yahoo",
                ticker="AAPL",
                field="fundamentals",
                started_at="2026-05-21T08:00:00Z",
                finished_at="2026-05-21T08:00:01Z",
                status="ok",
                bytes_returned=0,
                undeclared_field="oops",
            )


class TestProviderQuotaState:
    def test_minimal_valid(self) -> None:
        r = ProviderQuotaState(
            source="polygon",
            provider="polygon",
            window_start="2026-05-21T08:00:00Z",
            calls_used=3,
        )
        assert r.source == "polygon"
        assert r.provider == "polygon"
        assert r.calls_used == 3

    def test_source_differs_from_provider(self) -> None:
        r = ProviderQuotaState(
            source="openbb",
            provider="fmp_stable",
            window_start="2026-05-21T08:00:00Z",
            calls_used=1,
        )
        assert r.source == "openbb"
        assert r.provider == "fmp_stable"

    def test_calls_used_nonneg(self) -> None:
        with pytest.raises(Exception):
            ProviderQuotaState(
                source="polygon",
                provider="polygon",
                window_start="2026-05-21T08:00:00Z",
                calls_used=-1,
            )

    def test_window_start_required(self) -> None:
        with pytest.raises(Exception):
            ProviderQuotaState(
                source="polygon",
                provider="polygon",
                window_start=None,  # type: ignore[arg-type]
                calls_used=0,
            )

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(Exception):
            ProviderQuotaState(
                source="polygon",
                provider="polygon",
                window_start="2026-05-21T08:00:00Z",
                calls_used=0,
                bonus_column="nope",
            )


class TestFetchQueueEntry:
    def test_minimal_valid_pending(self) -> None:
        r = FetchQueueEntry(
            source="yahoo",
            ticker="AAPL",
            field="fundamentals",
            priority_score=0.0,
            created_at="2026-05-21T08:00:00Z",
            status="pending",
        )
        assert r.source == "yahoo"
        assert r.status == "pending"
        assert r.priority_score == 0.0
        assert r.dispatched_at is None
        assert r.completed_at is None
        assert r.queue_id is None
        assert r.last_error is None

    def test_all_status_values_accepted(self) -> None:
        for s in ("pending", "dispatched", "done", "error"):
            r = FetchQueueEntry(
                source="yahoo",
                ticker="AAPL",
                field="fundamentals",
                priority_score=0.0,
                created_at="2026-05-21T08:00:00Z",
                status=s,
            )
            assert r.status == s

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(Exception):
            FetchQueueEntry(
                source="yahoo",
                ticker="AAPL",
                field="fundamentals",
                priority_score=0.0,
                created_at="2026-05-21T08:00:00Z",
                status="weird",
            )

    def test_ticker_uppercased(self) -> None:
        r = FetchQueueEntry(
            source="yahoo",
            ticker="aapl",
            field="fundamentals",
            priority_score=0.0,
            created_at="2026-05-21T08:00:00Z",
            status="pending",
        )
        assert r.ticker == "AAPL"

    def test_ticker_none_for_macro(self) -> None:
        r = FetchQueueEntry(
            source="fred",
            ticker=None,
            field="macro:UNRATE",
            priority_score=0.0,
            created_at="2026-05-21T08:00:00Z",
            status="pending",
        )
        assert r.ticker is None

    def test_dispatched_carries_dispatched_at(self) -> None:
        r = FetchQueueEntry(
            source="yahoo",
            ticker="AAPL",
            field="fundamentals",
            priority_score=10.0,
            created_at="2026-05-21T08:00:00Z",
            dispatched_at="2026-05-21T08:00:05Z",
            status="dispatched",
        )
        assert r.dispatched_at == "2026-05-21T08:00:05Z"
        assert r.completed_at is None

    def test_done_carries_completed_at(self) -> None:
        r = FetchQueueEntry(
            source="yahoo",
            ticker="AAPL",
            field="fundamentals",
            priority_score=5.0,
            created_at="2026-05-21T08:00:00Z",
            dispatched_at="2026-05-21T08:00:05Z",
            completed_at="2026-05-21T08:00:07Z",
            status="done",
        )
        assert r.completed_at == "2026-05-21T08:00:07Z"

    def test_error_carries_last_error(self) -> None:
        r = FetchQueueEntry(
            source="yahoo",
            ticker="AAPL",
            field="fundamentals",
            priority_score=0.0,
            created_at="2026-05-21T08:00:00Z",
            dispatched_at="2026-05-21T08:00:05Z",
            completed_at="2026-05-21T08:00:07Z",
            status="error",
            last_error="HTTPError 500",
        )
        assert r.status == "error"
        assert r.last_error == "HTTPError 500"

    def test_priority_score_can_be_negative(self) -> None:
        r = FetchQueueEntry(
            source="yahoo",
            ticker="AAPL",
            field="fundamentals",
            priority_score=-50.0,
            created_at="2026-05-21T08:00:00Z",
            status="pending",
        )
        assert r.priority_score == -50.0

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(Exception):
            FetchQueueEntry(
                source="yahoo",
                ticker="AAPL",
                field="fundamentals",
                priority_score=0.0,
                created_at="2026-05-21T08:00:00Z",
                status="pending",
                surprise="bad",
            )
