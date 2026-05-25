"""Tests for Lee-So (2017) 8-K filing-density z-score module.

Caller: pytest. No data files read/written; pure-function exercise
with synthetic rows shaped {'ticker', 'form_type', 'filing_date', 'item_codes'}.
"""
from __future__ import annotations

import datetime as _dt
import math

import pytest

from src.methodology.filing_density import (
    FILING_DENSITY_VERSION,
    compute_filing_density,
)


def test_version_constant():
    assert FILING_DENSITY_VERSION == "1.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _8k(ticker: str, day: _dt.date, item_codes: str | None = None) -> dict:
    return {
        "ticker": ticker,
        "form_type": "8-K",
        "filing_date": day.isoformat(),
        "item_codes": item_codes,
    }


def _build_8k_burst(
    ticker: str,
    start: _dt.date,
    end: _dt.date,
    per_day: int = 1,
    item_codes: str | None = None,
) -> list[dict]:
    """One 8-K row per day (or `per_day` rows) over [start, end]."""
    rows = []
    d = start
    while d <= end:
        for _ in range(per_day):
            rows.append(_8k(ticker, d, item_codes))
        d = d + _dt.timedelta(days=1)
    return rows


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_input_returns_empty_dict():
    assert compute_filing_density([], as_of_date="2026-05-20") == {}


def test_non_8k_forms_excluded():
    """10-K, 10-Q, DEF 14A rows are all silently dropped."""
    rows = [
        {"ticker": "AAPL", "form_type": "10-K",    "filing_date": "2026-05-01", "item_codes": None},
        {"ticker": "AAPL", "form_type": "10-Q",    "filing_date": "2026-05-10", "item_codes": None},
        {"ticker": "AAPL", "form_type": "DEF 14A", "filing_date": "2026-05-15", "item_codes": None},
    ]
    out = compute_filing_density(rows, as_of_date="2026-05-20")
    assert "AAPL" not in out


def test_routine_8k_items_excluded():
    """Items 2.02 (earnings) and 7.01 (Reg-FD) are treated as routine and skipped."""
    as_of = _dt.date(2026, 5, 20)
    rows = (
        [_8k("AAPL", as_of - _dt.timedelta(days=i), "2.02") for i in range(5)]
        + [_8k("AAPL", as_of - _dt.timedelta(days=i + 10), "7.01") for i in range(5)]
    )
    out = compute_filing_density(rows, as_of_date=as_of.isoformat())
    assert "AAPL" not in out


def test_nan_when_baseline_too_short():
    """baseline_window_days=25 → n_windows=25 < 30 minimum → NaN."""
    as_of = _dt.date(2026, 5, 20)
    # A handful of 8-K rows so AAPL appears in per_ticker but n_windows < 30.
    rows = [_8k("AAPL", as_of - _dt.timedelta(days=i)) for i in range(15)]
    out = compute_filing_density(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=5, baseline_window_days=25,
    )
    assert math.isnan(out["AAPL"])


def test_invalid_recent_window_raises():
    with pytest.raises(ValueError):
        compute_filing_density([], as_of_date="2026-05-20", recent_window_days=0)


def test_invalid_baseline_window_raises():
    with pytest.raises(ValueError):
        compute_filing_density([], as_of_date="2026-05-20", baseline_window_days=0)


# ---------------------------------------------------------------------------
# Z-score direction and degenerate flat case
# ---------------------------------------------------------------------------

def test_flat_series_returns_zero():
    """Constant 1 filing/day for entire history → sigma=0, recent==mu → 0.0."""
    as_of = _dt.date(2026, 5, 20)
    # 51 days covers both recent (5-day) and baseline (40-day) windows.
    rows = _build_8k_burst("AAPL", as_of - _dt.timedelta(days=50), as_of)
    out = compute_filing_density(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=5, baseline_window_days=40,
    )
    assert out["AAPL"] == 0.0


def test_positive_z_when_recent_elevated():
    """Sparse baseline (1 filing every 4th day) then dense recent window → z > 0."""
    as_of = _dt.date(2026, 5, 20)
    # With recent_window_days=5, baseline_window_days=40:
    #   baseline_start = as_of - 44,  baseline_end = as_of - 5
    baseline_end   = as_of - _dt.timedelta(days=5)
    baseline_start = baseline_end - _dt.timedelta(days=39)
    rows: list[dict] = []
    d = baseline_start
    while d <= baseline_end:
        if (d - baseline_start).days % 4 == 0:  # ~10 filings, sparse → sigma > 0
            rows.append(_8k("AAPL", d))
        d = d + _dt.timedelta(days=1)
    # Dense recent: 1 filing/day for all 5 days in recent window
    rows += _build_8k_burst("AAPL", as_of - _dt.timedelta(days=4), as_of)
    out = compute_filing_density(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=5, baseline_window_days=40,
    )
    assert out["AAPL"] > 0


def test_negative_z_when_recent_quiet():
    """Dense baseline (3-7 filings/day for variance) then silent recent window → z < 0."""
    as_of = _dt.date(2026, 5, 20)
    baseline_end   = as_of - _dt.timedelta(days=5)
    baseline_start = baseline_end - _dt.timedelta(days=39)
    rows: list[dict] = []
    d = baseline_start
    i = 0
    while d <= baseline_end:
        for _ in range(3 + (i % 5)):  # 3, 4, 5, 6, 7, repeating → sigma > 0
            rows.append(_8k("AAPL", d))
        d = d + _dt.timedelta(days=1)
        i += 1
    # No filings in the recent window → recent_cnt = 0 << mu
    out = compute_filing_density(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=5, baseline_window_days=40,
    )
    assert out["AAPL"] < 0


# ---------------------------------------------------------------------------
# Multi-ticker, isolation, and determinism
# ---------------------------------------------------------------------------

def test_multiple_tickers_independent():
    as_of = _dt.date(2026, 5, 20)
    rows = (
        _build_8k_burst("AAPL", as_of - _dt.timedelta(days=50), as_of)
        + _build_8k_burst("MSFT", as_of - _dt.timedelta(days=50), as_of)
    )
    out = compute_filing_density(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=5, baseline_window_days=40,
    )
    assert "AAPL" in out
    assert "MSFT" in out


def test_tickers_absent_from_rows_not_in_output():
    as_of = _dt.date(2026, 5, 20)
    rows = _build_8k_burst("AAPL", as_of - _dt.timedelta(days=50), as_of)
    out = compute_filing_density(rows, as_of_date=as_of.isoformat())
    assert "MSFT" not in out


def test_deterministic_on_shuffled_input():
    as_of = _dt.date(2026, 5, 20)
    rows = _build_8k_burst("AAPL", as_of - _dt.timedelta(days=50), as_of)
    a = compute_filing_density(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=5, baseline_window_days=40,
    )
    b = compute_filing_density(
        list(reversed(rows)), as_of_date=as_of.isoformat(),
        recent_window_days=5, baseline_window_days=40,
    )
    assert a == b
