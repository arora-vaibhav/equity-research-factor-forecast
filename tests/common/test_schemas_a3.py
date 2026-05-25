"""Tests for Phase A.3 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import FetchWatermark


class TestFetchWatermark:
    def test_minimal_valid(self):
        w = FetchWatermark(
            source="yahoo",
            ticker="AAPL",
            field="historical_price",
            last_fetched_at="2026-05-21T08:00:00Z",
        )
        assert w.source == "yahoo"
        assert w.ticker == "AAPL"
        assert w.field == "historical_price"
        assert w.fetch_count == 0
        assert w.error_count == 0
        assert w.last_observation_date is None
        assert w.last_error_message is None

    def test_full_valid(self):
        w = FetchWatermark(
            source="edgar",
            ticker="MSFT",
            field="fundamentals_xbrl",
            last_fetched_at="2026-05-21T08:00:00Z",
            last_observation_date="2026-05-20",
            fetch_count=42,
            error_count=2,
            last_error_message="connection timeout",
        )
        assert w.fetch_count == 42
        assert w.error_count == 2

    def test_wildcard_ticker_allowed(self):
        # Some watermarks aren't ticker-scoped (e.g., FRED macro series).
        # Convention: ticker='*'. Must not raise.
        w = FetchWatermark(
            source="fred",
            ticker="*",
            field="series:DGS10",
            last_fetched_at="2026-05-21T08:00:00Z",
        )
        assert w.ticker == "*"

    def test_negative_fetch_count_rejected(self):
        with pytest.raises(Exception):
            FetchWatermark(
                source="yahoo",
                ticker="AAPL",
                field="price",
                last_fetched_at="2026-05-21T08:00:00Z",
                fetch_count=-1,
            )

    def test_negative_error_count_rejected(self):
        with pytest.raises(Exception):
            FetchWatermark(
                source="yahoo",
                ticker="AAPL",
                field="price",
                last_fetched_at="2026-05-21T08:00:00Z",
                error_count=-1,
            )
