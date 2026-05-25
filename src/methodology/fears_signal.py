"""Da-Engelberg-Gao (2015) FEARS contrarian signal.

Reference: Da, Z., Engelberg, J., Gao, P. (2015). "The Sum of All
FEARS Investor Sentiment and Asset Prices." Review of Financial
Studies, 28(1), 1-32.

Operational rule (per spec section 7.2 signal 7):
  Aggregate the FEARS basket SVI series (default 'recession',
  'bankruptcy', 'unemployment') by arithmetic mean per
  observation_date. Compare log mean SVI over the trailing
  `recent_window_days` to the trailing `baseline_window_days` mean:

      score = log(SVI_recent) - log(SVI_baseline)

CONTRARIAN SIGN CONVENTION: Da-Engelberg-Gao 2015 (Table 4) finds
HIGH FEARS PREDICTS POSITIVE returns at 2-5 day horizons (retail
panic-selling at T is followed by mean-reversion at T+2..T+10).
For Trade Identifier's swing-trade horizon, this is contrarian
BULLISH. The natural log-ratio reading IS the bullish direction:
HIGH recent SVI -> POSITIVE score, consistent with the rest of the
news_activity composite's "positive == bullish" convention.
Downstream code (factors.py at A.3.10) consumes this scalar
directly with no additional sign flip.

Note: the spec section 7.2 formula carried a leading minus sign
that contradicted the spec's own "+ve == bullish (HIGH fear)"
claim. The implementation follows the docstring's stated semantics
(positive == bullish high-fear) and drops the spurious negation.

This signal is UNIVERSE-LEVEL (returns a single float, not a
per-ticker dict). A.3.10's news_activity.py applies the same scalar
to every ticker in the universe, consistent with Da-Engelberg-Gao's
universe-wide interpretation.

Pure function -- no I/O, no DB, deterministic.
"""
from __future__ import annotations

import datetime as _dt
import math
from collections import defaultdict
from typing import Any


FEARS_SIGNAL_VERSION = "1.0"

# Da-Engelberg-Gao 2015 section III basket (US-only).
DEFAULT_FEARS_BASKET: tuple[str, ...] = ("recession", "bankruptcy", "unemployment")


def _parse_iso_date(s: str) -> _dt.date:
    s = str(s)
    if "T" in s:
        s = s.split("T", 1)[0]
    return _dt.date.fromisoformat(s)


def compute_fears_signal(
    rows: list[dict[str, Any]],
    *,
    as_of_date: str,
    recent_window_days: int = 7,
    baseline_window_days: int = 365,
    fears_basket: tuple[str, ...] | None = None,
) -> float:
    """Da-Engelberg-Gao 2015 FEARS contrarian signal (universe scalar).

    Parameters
    ----------
    rows
        Per-(term, observation_date) rows from ``raw_pytrends``. Each
        dict has at minimum:
          - 'term': str (lowercased)
          - 'observation_date': str ('YYYY-MM-DD')
          - 'svi': float (>= 0)
    as_of_date
        Focal date 'YYYY-MM-DD'.
    recent_window_days
        Trailing-window length for the numerator (default 7).
    baseline_window_days
        Trailing-window length for the denominator (default 365). The
        baseline ENDS at the recent-window's earliest day; the
        windows are disjoint.
    fears_basket
        Override the term basket; default DEFAULT_FEARS_BASKET.

    Returns
    -------
    float
        Single contrarian-bullish scalar following the natural
        log-ratio reading:
        +ve == bullish (high recent fear-search relative to baseline,
        which mean-reverts to positive returns per D-E-G 2015).
        NaN if baseline has fewer than 30 days of observable history,
        or if either window has zero mean SVI (log undefined).
    """
    if recent_window_days <= 0:
        raise ValueError("recent_window_days must be positive")
    if baseline_window_days <= 0:
        raise ValueError("baseline_window_days must be positive")

    basket = tuple(t.lower() for t in (fears_basket or DEFAULT_FEARS_BASKET))
    basket_set = set(basket)

    as_of = _parse_iso_date(as_of_date)
    recent_start = as_of - _dt.timedelta(days=recent_window_days - 1)
    baseline_end = recent_start - _dt.timedelta(days=1)
    baseline_start = baseline_end - _dt.timedelta(days=baseline_window_days - 1)

    # date -> list of svi values for basket terms
    by_date: dict[_dt.date, list[float]] = defaultdict(list)
    for r in rows:
        term = str(r.get("term", "")).lower()
        if term not in basket_set:
            continue
        try:
            d = _parse_iso_date(r["observation_date"])
        except (ValueError, TypeError, KeyError):
            continue
        svi = r.get("svi")
        if svi is None:
            continue
        by_date[d].append(float(svi))

    # date -> mean-SVI across basket terms on that date
    daily_mean: dict[_dt.date, float] = {
        d: sum(vs) / len(vs) for d, vs in by_date.items() if vs
    }

    recent_vals = [
        v for d, v in daily_mean.items() if recent_start <= d <= as_of
    ]
    baseline_vals = [
        v for d, v in daily_mean.items()
        if baseline_start <= d <= baseline_end
    ]

    if len(baseline_vals) < 30:
        return float("nan")
    if not recent_vals:
        return float("nan")

    mean_recent = sum(recent_vals) / len(recent_vals)
    mean_baseline = sum(baseline_vals) / len(baseline_vals)
    if mean_recent <= 0 or mean_baseline <= 0:
        return float("nan")

    # log(SVI_recent / SVI_baseline) — natural log-ratio reading.
    # HIGH recent fear -> POSITIVE output (contrarian bullish per D-E-G 2015
    # Table 4 multi-day mean reversion at T+2..T+10).
    return float(math.log(mean_recent) - math.log(mean_baseline))
