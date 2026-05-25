"""Tests for Da-Engelberg-Gao (2015) FEARS contrarian signal module.

Caller: pytest. No data files read/written; pure-function exercise
with synthetic rows shaped {'term', 'observation_date', 'svi'}.

Sign convention (contrarian-bullish, per D-E-G 2015 Table 4 multi-day
mean reversion at T+2..T+10):
  compute_fears_signal returns  log(mean_recent) - log(mean_baseline).
  HIGH recent SVI (elevated fear) -> POSITIVE score (bullish, retail
  panic mean-reverts to positive returns at swing-trade horizons).
  LOW recent SVI (calm market) -> NEGATIVE score.
  Matches the news_activity composite's "+ve == bullish" convention.
"""
from __future__ import annotations

import datetime as _dt
import math

import pytest

from src.methodology.fears_signal import (
    DEFAULT_FEARS_BASKET,
    FEARS_SIGNAL_VERSION,
    compute_fears_signal,
)


def test_version_constant():
    assert FEARS_SIGNAL_VERSION == "1.0"


def test_default_basket_contains_expected_terms():
    assert set(DEFAULT_FEARS_BASKET) == {"recession", "bankruptcy", "unemployment"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _svi(term: str, day: _dt.date, svi: float) -> dict:
    return {"term": term, "observation_date": day.isoformat(), "svi": svi}


def _fill(term: str, start: _dt.date, end: _dt.date, svi: float) -> list[dict]:
    """Daily rows for `term` over [start, end] at constant `svi`."""
    rows = []
    d = start
    while d <= end:
        rows.append(_svi(term, d, svi))
        d = d + _dt.timedelta(days=1)
    return rows


def _full_windows(
    as_of: _dt.date,
    recent_window_days: int,
    baseline_window_days: int,
    recent_svi: float,
    baseline_svi: float,
    basket: tuple[str, ...] | None = None,
) -> list[dict]:
    """Build complete recent + baseline rows for all basket terms.

    Mirrors compute_fears_signal's own window arithmetic so every row lands
    inside the expected window.
    """
    b = basket or DEFAULT_FEARS_BASKET
    recent_start = as_of - _dt.timedelta(days=recent_window_days - 1)
    baseline_end = recent_start - _dt.timedelta(days=1)
    baseline_start = baseline_end - _dt.timedelta(days=baseline_window_days - 1)
    rows = []
    for term in b:
        rows += _fill(term, baseline_start, baseline_end, baseline_svi)
        rows += _fill(term, recent_start, as_of, recent_svi)
    return rows


# ---------------------------------------------------------------------------
# NaN / error conditions
# ---------------------------------------------------------------------------

def test_empty_input_returns_nan():
    result = compute_fears_signal([], as_of_date="2026-05-20")
    assert math.isnan(result)


def test_nan_when_baseline_too_short():
    """25 days of baseline history < 30 minimum → NaN."""
    as_of = _dt.date(2026, 5, 20)
    rows = _full_windows(
        as_of,
        recent_window_days=7, baseline_window_days=25,
        recent_svi=50.0, baseline_svi=50.0,
    )
    result = compute_fears_signal(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=7, baseline_window_days=25,
    )
    assert math.isnan(result)


def test_nan_when_recent_window_empty():
    """No rows in the recent window → NaN."""
    as_of = _dt.date(2026, 5, 20)
    recent_start = as_of - _dt.timedelta(days=6)
    baseline_end = recent_start - _dt.timedelta(days=1)
    baseline_start = baseline_end - _dt.timedelta(days=364)
    rows = []
    for term in DEFAULT_FEARS_BASKET:
        rows += _fill(term, baseline_start, baseline_end, 50.0)
    # Deliberately omit recent-window rows.
    result = compute_fears_signal(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=7, baseline_window_days=365,
    )
    assert math.isnan(result)


def test_nan_when_recent_svi_zero():
    """Zero mean SVI in recent window → log undefined → NaN."""
    as_of = _dt.date(2026, 5, 20)
    rows = _full_windows(
        as_of,
        recent_window_days=7, baseline_window_days=40,
        recent_svi=0.0, baseline_svi=50.0,
    )
    result = compute_fears_signal(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=7, baseline_window_days=40,
    )
    assert math.isnan(result)


def test_nan_when_baseline_svi_zero():
    """Zero mean SVI in baseline → log undefined → NaN."""
    as_of = _dt.date(2026, 5, 20)
    rows = _full_windows(
        as_of,
        recent_window_days=7, baseline_window_days=40,
        recent_svi=50.0, baseline_svi=0.0,
    )
    result = compute_fears_signal(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=7, baseline_window_days=40,
    )
    assert math.isnan(result)


def test_invalid_recent_window_raises():
    with pytest.raises(ValueError):
        compute_fears_signal([], as_of_date="2026-05-20", recent_window_days=0)


def test_invalid_baseline_window_raises():
    with pytest.raises(ValueError):
        compute_fears_signal([], as_of_date="2026-05-20", baseline_window_days=0)


# ---------------------------------------------------------------------------
# Sign and magnitude
# ---------------------------------------------------------------------------

def test_equal_windows_returns_zero():
    """Same SVI in recent and baseline → log(baseline/recent) = 0."""
    as_of = _dt.date(2026, 5, 20)
    rows = _full_windows(
        as_of,
        recent_window_days=7, baseline_window_days=40,
        recent_svi=50.0, baseline_svi=50.0,
    )
    result = compute_fears_signal(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=7, baseline_window_days=40,
    )
    assert result == pytest.approx(0.0)


def test_high_recent_svi_returns_positive():
    """High recent SVI (elevated fear) -> POSITIVE score (contrarian bullish)."""
    as_of = _dt.date(2026, 5, 20)
    rows = _full_windows(
        as_of,
        recent_window_days=7, baseline_window_days=40,
        recent_svi=80.0, baseline_svi=30.0,
    )
    result = compute_fears_signal(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=7, baseline_window_days=40,
    )
    assert result > 0


def test_low_recent_svi_returns_negative():
    """Low recent SVI (calm market) -> NEGATIVE score (no contrarian opportunity)."""
    as_of = _dt.date(2026, 5, 20)
    rows = _full_windows(
        as_of,
        recent_window_days=7, baseline_window_days=40,
        recent_svi=20.0, baseline_svi=70.0,
    )
    result = compute_fears_signal(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=7, baseline_window_days=40,
    )
    assert result < 0


def test_score_magnitude_matches_log_ratio():
    """Exact value: score = log(mean_recent) - log(mean_baseline)."""
    as_of = _dt.date(2026, 5, 20)
    rows = _full_windows(
        as_of,
        recent_window_days=7, baseline_window_days=40,
        recent_svi=100.0, baseline_svi=50.0,
    )
    result = compute_fears_signal(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=7, baseline_window_days=40,
    )
    # log(100) - log(50) = log(2) ~ 0.693, POSITIVE (high recent fear)
    expected = math.log(100.0) - math.log(50.0)
    assert result == pytest.approx(expected, rel=1e-6)


def test_non_basket_terms_ignored():
    """Rows with terms outside the default basket do not influence the score."""
    as_of = _dt.date(2026, 5, 20)
    rows = _full_windows(
        as_of,
        recent_window_days=7, baseline_window_days=40,
        recent_svi=50.0, baseline_svi=50.0,
    )
    # Noise terms with extreme SVI — must not move the result.
    noise_start = as_of - _dt.timedelta(days=46)
    for term in ("inflation", "bubble", "crash"):
        rows += _fill(term, noise_start, as_of, 9999.0)
    result = compute_fears_signal(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=7, baseline_window_days=40,
    )
    assert result == pytest.approx(0.0)


def test_custom_fears_basket():
    """Caller-supplied basket overrides the default."""
    as_of = _dt.date(2026, 5, 20)
    custom: tuple[str, ...] = ("inflation", "crisis")
    rows = _full_windows(
        as_of,
        recent_window_days=7, baseline_window_days=40,
        recent_svi=60.0, baseline_svi=30.0,
        basket=custom,
    )
    result = compute_fears_signal(
        rows, as_of_date=as_of.isoformat(),
        recent_window_days=7, baseline_window_days=40,
        fears_basket=custom,
    )
    assert result > 0  # high recent SVI -> POSITIVE score (contrarian bullish)
