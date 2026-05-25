"""Tests for orchestrator _route_field_to_call A.3.8 additions.

Verifies that field='news_volume' routes to fetch_news_volume and
field='fears' routes to fetch_search_interest.

Caller: pytest.  No live network calls.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.common.orchestrator.fetch_orchestrator import _route_field_to_call


def _make_src(**kwargs):
    """Mock BaseDataSource with configurable method return values."""
    src = MagicMock()
    for method, retval in kwargs.items():
        getattr(src, method).return_value = retval
    return src


# ---------------------------------------------------------------------------
# Existing routes (regression)
# ---------------------------------------------------------------------------

def test_fundamentals_route():
    src = _make_src(fetch_fundamentals_for_ticker=42)
    result = _route_field_to_call(src, "fundamentals", "AAPL", "r1")
    src.fetch_fundamentals_for_ticker.assert_called_once_with("AAPL", "r1")
    assert result == 42


def test_macro_series_route():
    src = _make_src(fetch_macro_series=3)
    result = _route_field_to_call(src, "macro_series", None, "r1")
    src.fetch_macro_series.assert_called_once_with([], "r1")
    assert result == 3


# ---------------------------------------------------------------------------
# A.3.8 new routes
# ---------------------------------------------------------------------------

def test_news_volume_routes_to_fetch_news_volume():
    """field='news_volume' -> fetch_news_volume(ticker, run_id)."""
    src = _make_src(fetch_news_volume=5)
    result = _route_field_to_call(src, "news_volume", "AAPL", "r1")
    src.fetch_news_volume.assert_called_once_with("AAPL", "r1")
    assert result == 5


def test_fears_routes_to_fetch_search_interest():
    """field='fears' -> fetch_search_interest(run_id) (no ticker)."""
    src = _make_src(fetch_search_interest=12)
    result = _route_field_to_call(src, "fears", None, "r1")
    src.fetch_search_interest.assert_called_once_with("r1")
    assert result == 12


def test_fears_ignores_ticker_argument():
    """fears is universe-level; ticker arg is ignored."""
    src = _make_src(fetch_search_interest=12)
    _route_field_to_call(src, "fears", "AAPL", "r1")
    src.fetch_search_interest.assert_called_once_with("r1")


def test_news_volume_passes_ticker():
    """news_volume is per-ticker; ticker arg must be forwarded."""
    src = _make_src(fetch_news_volume=3)
    _route_field_to_call(src, "news_volume", "MSFT", "r2")
    src.fetch_news_volume.assert_called_once_with("MSFT", "r2")


def test_unknown_field_falls_through_to_fetch_universe():
    """Unrecognised field falls through to fetch_universe (catch-all)."""
    src = _make_src(fetch_universe=99)
    result = _route_field_to_call(src, "unknown_field", "AAPL", "r1")
    src.fetch_universe.assert_called_once_with("r1")
    assert result == 99
