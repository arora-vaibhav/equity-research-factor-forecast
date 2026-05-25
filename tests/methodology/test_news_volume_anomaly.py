"""Tests for Tetlock (2007) news-volume anomaly module.

Caller: pytest. No data files; pure-function over synthetic mention rows
shaped {'ticker', 'mention_timestamp'}.
"""
from __future__ import annotations

import datetime as _dt
import math

from src.methodology.news_volume_anomaly import (
    NEWS_VOLUME_ANOMALY_VERSION,
    compute_news_volume_anomaly,
)


def test_version_constant():
    assert NEWS_VOLUME_ANOMALY_VERSION == "1.0"


def _row(ticker: str, day: _dt.date) -> dict:
    return {"ticker": ticker,
            "mention_timestamp": day.isoformat() + "T12:00:00Z"}


def _build_steady(ticker: str, mentions_per_day: int, days: int,
                  end_day: _dt.date) -> list[dict]:
    rows = []
    d = end_day - _dt.timedelta(days=days - 1)
    while d <= end_day:
        for _ in range(mentions_per_day):
            rows.append(_row(ticker, d))
        d = d + _dt.timedelta(days=1)
    return rows


def test_empty_input_returns_empty_dict():
    assert compute_news_volume_anomaly([], as_of_date="2026-05-20") == {}


def test_nan_when_baseline_too_short():
    """Only 10 days of baseline observations; threshold is 30."""
    as_of = _dt.date(2026, 5, 20)
    rows = _build_steady("AAPL", mentions_per_day=1, days=10,
                         end_day=as_of - _dt.timedelta(days=10))
    out = compute_news_volume_anomaly(rows, as_of_date=as_of.isoformat(),
                                      recent_window_days=10, baseline_window_days=20)
    assert math.isnan(out["AAPL"])


def test_recent_matches_baseline_returns_zero():
    """Steady 1 mention/day throughout. The constant series has sigma=0,
    so the module reports 0.0 (degenerate-flat handling)."""
    as_of = _dt.date(2026, 5, 20)
    rows = _build_steady("AAPL", mentions_per_day=1, days=60, end_day=as_of)
    out = compute_news_volume_anomaly(rows, as_of_date=as_of.isoformat(),
                                      recent_window_days=10, baseline_window_days=50)
    assert out["AAPL"] == 0.0


def test_positive_z_when_recent_above_baseline():
    """Baseline sparse with some variance, recent spike."""
    as_of = _dt.date(2026, 5, 20)
    baseline_end = as_of - _dt.timedelta(days=5)
    baseline_start = baseline_end - _dt.timedelta(days=39)
    rows: list[dict] = []
    d = baseline_start
    while d <= baseline_end:
        if (d - baseline_start).days % 4 == 0:
            rows.append(_row("AAPL", d))
        d = d + _dt.timedelta(days=1)
    # Recent: heavy mentions every day.
    d = as_of - _dt.timedelta(days=4)
    while d <= as_of:
        for _ in range(5):
            rows.append(_row("AAPL", d))
        d = d + _dt.timedelta(days=1)
    out = compute_news_volume_anomaly(rows, as_of_date=as_of.isoformat(),
                                      recent_window_days=5, baseline_window_days=40)
    assert out["AAPL"] > 0


def test_negative_z_when_recent_below_baseline():
    """Baseline busy with variance, recent quiet."""
    as_of = _dt.date(2026, 5, 20)
    baseline_end = as_of - _dt.timedelta(days=5)
    baseline_start = baseline_end - _dt.timedelta(days=39)
    rows: list[dict] = []
    d = baseline_start
    i = 0
    while d <= baseline_end:
        # Vary 3..7 mentions/day to ensure sigma > 0.
        n = 3 + (i % 5)
        for _ in range(n):
            rows.append(_row("AAPL", d))
        d = d + _dt.timedelta(days=1)
        i += 1
    # Recent window has at most one mention total.
    rows.append(_row("AAPL", as_of - _dt.timedelta(days=2)))
    out = compute_news_volume_anomaly(rows, as_of_date=as_of.isoformat(),
                                      recent_window_days=5, baseline_window_days=40)
    assert out["AAPL"] < 0


def test_multiple_tickers_independent():
    as_of = _dt.date(2026, 5, 20)
    rows = _build_steady("AAPL", 1, 60, as_of) + _build_steady("MSFT", 2, 60, as_of)
    out = compute_news_volume_anomaly(rows, as_of_date=as_of.isoformat(),
                                      recent_window_days=10, baseline_window_days=50)
    assert "AAPL" in out and "MSFT" in out


def test_tickers_absent_from_rows_not_in_output():
    as_of = _dt.date(2026, 5, 20)
    rows = _build_steady("AAPL", 1, 60, as_of)
    out = compute_news_volume_anomaly(rows, as_of_date=as_of.isoformat())
    assert "MSFT" not in out


def test_deterministic_on_shuffled_input():
    as_of = _dt.date(2026, 5, 20)
    rows = _build_steady("AAPL", 1, 60, as_of)
    a = compute_news_volume_anomaly(rows, as_of_date=as_of.isoformat(),
                                    recent_window_days=10, baseline_window_days=50)
    b = compute_news_volume_anomaly(list(reversed(rows)),
                                    as_of_date=as_of.isoformat(),
                                    recent_window_days=10, baseline_window_days=50)
    assert a == b
