"""Tests for orchestrator _route_field_to_call A.3.9 additions.

Verifies that field='lm_tone' routes to fetch_filing_text_for_ticker
on the chosen source (EDGAR in production wiring).

Caller: pytest. No live network calls.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.common.orchestrator.fetch_orchestrator import _route_field_to_call


def _make_src(**kwargs):
    src = MagicMock()
    for method, retval in kwargs.items():
        getattr(src, method).return_value = retval
    return src


def test_lm_tone_routes_to_fetch_filing_text_for_ticker():
    """field='lm_tone' -> fetch_filing_text_for_ticker(ticker, run_id)."""
    src = _make_src(fetch_filing_text_for_ticker=7)
    result = _route_field_to_call(src, "lm_tone", "AAPL", "r1")
    src.fetch_filing_text_for_ticker.assert_called_once_with("AAPL", "r1")
    assert result == 7


def test_lm_tone_per_ticker_field():
    """lm_tone is per-ticker; ticker must be forwarded."""
    src = _make_src(fetch_filing_text_for_ticker=0)
    _route_field_to_call(src, "lm_tone", "MSFT", "r2")
    src.fetch_filing_text_for_ticker.assert_called_once_with("MSFT", "r2")


def test_lm_tone_does_not_invoke_other_fetchers():
    src = _make_src(
        fetch_filing_text_for_ticker=1,
        fetch_filings_for_ticker=99,
        fetch_universe=99,
    )
    _route_field_to_call(src, "lm_tone", "AAPL", "r1")
    src.fetch_filing_text_for_ticker.assert_called_once()
    src.fetch_filings_for_ticker.assert_not_called()
    src.fetch_universe.assert_not_called()
