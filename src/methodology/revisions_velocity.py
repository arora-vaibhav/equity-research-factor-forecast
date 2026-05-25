"""Chan-Jegadeesh-Lakonishok (1996) analyst-revision velocity.

Reference: Chan, L. K. C., Jegadeesh, N., Lakonishok, J. (1996).
"Momentum Strategies." Journal of Finance, 51(5), 1681-1713.

Operational rule (per spec section 7.2 signal 3):
  For each ticker, compute the net analyst-revision velocity over
  `lookback_days`, penalised by EPS-target dispersion:

      score = (Up - Down) / N  -  lambda * sigma(TP) / |mu(TP)|

  where Up / Down / N are aggregated from the
  `eps_revision_direction_count` rows in raw_openbb (caller supplies
  the integer net count - positive = net upward, negative = net
  downward), and TP is the set of `eps_estimate_current` values seen
  within the lookback window.

Sign convention: POSITIVE return value == net UPWARD revisions
(bullish). Pure function -- no I/O, no DB, deterministic.
"""
from __future__ import annotations

import datetime as _dt
import math
from collections import defaultdict
from typing import Any


REVISIONS_VELOCITY_VERSION = "1.0"


def _parse_iso_date(s: str) -> _dt.date:
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS[Z]' to a date."""
    s = str(s)
    if "T" in s:
        s = s.split("T", 1)[0]
    return _dt.date.fromisoformat(s)


def compute_revisions_velocity(
    rows: list[dict[str, Any]],
    *,
    as_of_date: str,
    lookback_days: int = 90,
    dispersion_penalty_lambda: float = 0.5,
) -> dict[str, float]:
    """Compute Chan-Jegadeesh-Lakonishok revision velocity per ticker.

    Parameters
    ----------
    rows
        Long-format analyst-estimate rows from ``raw_openbb`` filtered
        to the three field_names this module consumes:
          - eps_estimate_current
          - eps_estimate_30d_ago
          - eps_revision_direction_count  (signed net count: positive
            = net upward, negative = net downward)
        Each dict has at minimum:
          - 'ticker': str
          - 'field_name': str
          - 'value': Optional[float]
          - 'scrape_timestamp': str (ISO 8601 UTC or 'YYYY-MM-DD')
    as_of_date
        Focal date 'YYYY-MM-DD'.
    lookback_days
        Window length in days (default 90).
    dispersion_penalty_lambda
        Coefficient on the sigma/mu dispersion term. Default 0.5; set
        to 0.0 to disable the penalty entirely. Must be >= 0.

    Returns
    -------
    dict[str, float]
        Per-ticker signed scalar. Positive == net-upward revisions.
        NaN if fewer than 3 observations within the lookback window
        for that ticker (insufficient sample).
    """
    if lookback_days <= 0:
        raise ValueError(f"lookback_days must be positive, got {lookback_days}")
    if dispersion_penalty_lambda < 0:
        raise ValueError(
            f"dispersion_penalty_lambda must be >= 0, "
            f"got {dispersion_penalty_lambda}"
        )

    as_of = _parse_iso_date(as_of_date)
    cutoff = as_of - _dt.timedelta(days=lookback_days)

    # ticker -> list of (date, field_name, value)
    per_ticker: dict[str, list[tuple[_dt.date, str, float]]] = defaultdict(list)
    for r in rows:
        v = r.get("value")
        if v is None:
            continue
        try:
            d = _parse_iso_date(r["scrape_timestamp"])
        except (ValueError, TypeError):
            continue
        if d < cutoff or d > as_of:
            continue
        per_ticker[r["ticker"]].append((d, r["field_name"], float(v)))

    out: dict[str, float] = {}
    for ticker, obs in per_ticker.items():
        if len(obs) < 3:
            out[ticker] = float("nan")
            continue

        # Aggregate net direction counts.
        net_directions = [v for _, fn, v in obs if fn == "eps_revision_direction_count"]
        if not net_directions:
            out[ticker] = float("nan")
            continue
        sum_direction = sum(net_directions)
        n_direction = len(net_directions)
        velocity = sum_direction / n_direction

        # Dispersion penalty on the EPS-current target series.
        tp_values = [v for _, fn, v in obs if fn == "eps_estimate_current"]
        penalty = 0.0
        if dispersion_penalty_lambda > 0 and len(tp_values) >= 2:
            mu = sum(tp_values) / len(tp_values)
            if abs(mu) > 1e-9:
                var = sum((x - mu) ** 2 for x in tp_values) / len(tp_values)
                sigma = math.sqrt(var)
                penalty = dispersion_penalty_lambda * (sigma / abs(mu))

        out[ticker] = float(velocity - penalty)

    return out
