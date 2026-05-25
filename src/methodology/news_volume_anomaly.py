"""Tetlock (2007) abnormal-news-volume z-score.

Reference: Tetlock, P. C. (2007). "Giving Content to Investor Sentiment:
The Role of Media in the Stock Market." Journal of Finance, 62(3),
1139-1168.

Operational rule (per spec section 7.2 signal 4):
  Bucket GDELT GKG mentions by ticker x calendar-day (UTC). For each
  ticker, compare the mean daily-mention count over the trailing
  `recent_window_days` to the per-ticker mean and standard deviation
  over the trailing `baseline_window_days`:

      z = (mean_recent - mu_baseline) / sigma_baseline

  Zero-mention days inside the baseline window ARE included once the
  ticker's first observation is reached (they are the base-rate of
  "no news today" and define the null-hypothesis distribution).
  Pre-first-observation phantom zeros are excluded so freshly-listed
  tickers are not penalised for periods before they had any history.

Sign convention: POSITIVE z == elevated mention volume relative to
baseline. Pure function -- no I/O, no DB, deterministic.
"""
from __future__ import annotations

import datetime as _dt
import math
from collections import defaultdict
from typing import Any


NEWS_VOLUME_ANOMALY_VERSION = "1.0"


def _parse_iso_date(s: str) -> _dt.date:
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS[Z]' to a date."""
    s = str(s)
    if "T" in s:
        s = s.split("T", 1)[0]
    return _dt.date.fromisoformat(s)


def compute_news_volume_anomaly(
    rows: list[dict[str, Any]],
    *,
    as_of_date: str,
    recent_window_days: int = 30,
    baseline_window_days: int = 365,
) -> dict[str, float]:
    """Tetlock 2007 abnormal-news z-score per ticker.

    Parameters
    ----------
    rows
        Per-mention rows from ``raw_gdelt``. Each dict has at minimum:
          - 'ticker': str
          - 'mention_timestamp': str (ISO 8601 UTC or 'YYYY-MM-DD')
        Multiple mentions on the same calendar day collapse to one
        count per ticker x day for the baseline-distribution sample.
    as_of_date
        Focal date 'YYYY-MM-DD'.
    recent_window_days
        Trailing window for the numerator (default 30).
    baseline_window_days
        Trailing window for the baseline mean/std (default 365). The
        baseline ENDS at the recent-window's earliest day -- the
        windows are disjoint.

    Returns
    -------
    dict[str, float]
        Per-ticker z-score. NaN if the baseline window has fewer than
        30 distinct days of observable history (sample too small for
        a stable mean/std). Tickers entirely absent from `rows` are
        omitted.
    """
    if recent_window_days <= 0:
        raise ValueError("recent_window_days must be positive")
    if baseline_window_days <= 0:
        raise ValueError("baseline_window_days must be positive")

    as_of = _parse_iso_date(as_of_date)
    recent_start = as_of - _dt.timedelta(days=recent_window_days - 1)
    baseline_end = recent_start - _dt.timedelta(days=1)
    baseline_start = baseline_end - _dt.timedelta(days=baseline_window_days - 1)

    # ticker -> date -> count (collapse same-day mentions)
    per_ticker_day: dict[str, dict[_dt.date, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        try:
            d = _parse_iso_date(r["mention_timestamp"])
        except (ValueError, TypeError, KeyError):
            continue
        per_ticker_day[r["ticker"]][d] += 1

    out: dict[str, float] = {}
    for ticker, by_day in per_ticker_day.items():
        # Build the baseline daily-count series, including zero days
        # from the ticker's first observation onward.
        first_obs_day: _dt.date | None = None
        for k in sorted(by_day.keys()):
            if k <= baseline_end:
                first_obs_day = k
                break
        if first_obs_day is None:
            out[ticker] = float("nan")
            continue

        effective_start = max(baseline_start, first_obs_day)
        baseline_days: list[int] = []
        d = effective_start
        while d <= baseline_end:
            baseline_days.append(by_day.get(d, 0))
            d = d + _dt.timedelta(days=1)

        n = len(baseline_days)
        if n < 30:
            out[ticker] = float("nan")
            continue
        mu = sum(baseline_days) / n
        var = sum((x - mu) ** 2 for x in baseline_days) / n
        sigma = math.sqrt(var)

        # Recent-window mean.
        recent_total = 0
        d = recent_start
        while d <= as_of:
            recent_total += by_day.get(d, 0)
            d = d + _dt.timedelta(days=1)
        mean_recent = recent_total / recent_window_days

        if sigma == 0:
            # Degenerate baseline (all-equal). If recent matches the
            # constant baseline, signal is exactly zero. Otherwise the
            # standardisation is undefined -> NaN rather than inf.
            out[ticker] = 0.0 if mean_recent == mu else float("nan")
            continue
        out[ticker] = float((mean_recent - mu) / sigma)

    return out
