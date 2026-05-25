"""Diether-Lee-Werner (2009) short-interest delta.

Reference: Diether, K. B., Lee, K.-H., Werner, I. M. (2009).
"Short-Sale Strategies and Return Predictability." Review of Financial
Studies, 22(2), 575-607.

Operational rule (per spec section 7.2 signal 2):
  For each ticker, compute the change in short-interest shares between
  an `as_of_date` observation and a `lookback_days`-prior observation.
  Multiple observations on the same date (e.g. from multiple exchanges)
  are summed. Multiply the raw share delta by a days-to-cover (DTC)
  coefficient when `avg_daily_volume` is available; fall back to the
  raw share delta otherwise.

Sign convention: POSITIVE return value == short interest RISING (which
the Diether-Lee-Werner literature reads as bearish for forward returns).
This module is sign-neutral; the composite-time sign flip happens in
news_activity.py / factors.py at A.3.10 per the spec.

Pure function -- no I/O, no DB, deterministic.
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from typing import Any


SHORT_INTEREST_DELTA_VERSION = "1.0"


def _parse_date(s: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(s)
    except (ValueError, TypeError) as e:
        raise ValueError(f"invalid date string: {s!r}") from e


def compute_short_interest_delta(
    rows: list[dict[str, Any]],
    *,
    as_of_date: str,
    lookback_days: int = 30,
) -> dict[str, float]:
    """Compute the Diether-Lee-Werner short-interest delta per ticker.

    Parameters
    ----------
    rows
        Per-(ticker, settlement_date) records from ``raw_finra``. Each
        dict has at minimum:
          - 'ticker': str
          - 'settlement_date': str ('YYYY-MM-DD')
          - 'short_interest_shares': float
          - 'avg_daily_volume': Optional[float]
        Multiple observations on the same date for the same ticker
        (e.g., from multiple exchanges) are summed.
    as_of_date
        The focal date ('YYYY-MM-DD'). The "as-of" observation is the
        last observation on or before this date.
    lookback_days
        Window length in days (default 30). Must be positive.

    Returns
    -------
    dict[str, float]
        Per-ticker signed scalar. Positive == rising short interest.
        NaN for tickers that lack BOTH an as-of and a lookback-prior
        observation, or whose two observations coincide on the same
        date. Tickers entirely absent from `rows` are omitted from
        the output.
    """
    if lookback_days <= 0:
        raise ValueError(f"lookback_days must be positive, got {lookback_days}")
    as_of = _parse_date(as_of_date)
    lookback_cutoff = as_of - _dt.timedelta(days=lookback_days)

    # ticker -> date -> (sum_short_interest, sum_adv_or_None)
    per_ticker_dates: dict[str, dict[_dt.date, tuple[float, float | None]]] = defaultdict(dict)
    for r in rows:
        ticker = r["ticker"]
        d = _parse_date(r["settlement_date"])
        si = float(r["short_interest_shares"])
        adv = r.get("avg_daily_volume")
        adv_f = float(adv) if adv is not None else None
        existing = per_ticker_dates[ticker].get(d)
        if existing is None:
            per_ticker_dates[ticker][d] = (si, adv_f)
        else:
            prev_si, prev_adv = existing
            if prev_adv is not None and adv_f is not None:
                new_adv: float | None = prev_adv + adv_f
            elif prev_adv is not None:
                new_adv = prev_adv
            else:
                new_adv = adv_f
            per_ticker_dates[ticker][d] = (prev_si + si, new_adv)

    out: dict[str, float] = {}
    for ticker, by_date in per_ticker_dates.items():
        dates_sorted = sorted(by_date.keys())
        as_of_obs: _dt.date | None = None
        for d in reversed(dates_sorted):
            if d <= as_of:
                as_of_obs = d
                break
        lookback_obs: _dt.date | None = None
        for d in reversed(dates_sorted):
            if d <= lookback_cutoff:
                lookback_obs = d
                break
        if as_of_obs is None or lookback_obs is None or as_of_obs == lookback_obs:
            out[ticker] = float("nan")
            continue

        si_now, adv_now = by_date[as_of_obs]
        si_prior, _ = by_date[lookback_obs]
        raw_delta = si_now - si_prior

        # DTC multiplier: use as-of-date ADV when available; the DTC
        # coefficient is short_interest_shares / avg_daily_volume.
        if adv_now is not None and adv_now > 0:
            dtc = si_now / adv_now
            value = raw_delta * dtc
        else:
            value = raw_delta
        out[ticker] = float(value)

    return out
