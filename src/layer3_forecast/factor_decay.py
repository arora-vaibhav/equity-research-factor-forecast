"""Time-varying factor contribution decay.

Catalysts and news have diminishing factor contribution each day
forward; this module models that decay so stale signals do not
dominate the forecast.

Implementation: exponential half-life per factor type. Literature
anchors:
  * Tetlock 2007 JF: news impact dissipates over 5-15 days.
  * Cohen-Malloy-Pomorski 2012: opportunistic insider half-life ~30d.
  * Fundamental factors (P/E, FCF yield): quarterly persistence
    (~63 trading days half-life ~= 90 calendar days).

Pure functions; no DB, no I/O.
"""
from __future__ import annotations

import math
from typing import Mapping


FACTOR_DECAY_VERSION = "1.0"

DEFAULT_HALF_LIFE_DAYS: dict[str, float] = {
    # Fundamentals -- slow decay (quarterly persistence).
    "value_score":          90.0,
    "quality_score":        90.0,
    "lowvol_score":         60.0,
    # Mixed signal -- medium decay.
    "momentum_score":       30.0,
    "revisions_score":      21.0,
    # News-activity composite -- fast decay (Tetlock 2007).
    "news_activity_score":  14.0,
    # Sub-signals (if reported separately).
    "opportunistic_insider": 30.0,
    "filing_density":        14.0,
    "lm_tone":               21.0,
    "short_interest_delta":  30.0,
    "news_volume_anomaly":   7.0,
    "fears":                 7.0,
}


def decay_factor(half_life_days: float, days_forward: float) -> float:
    """Exponential decay: decay = exp(-d * ln(2) / H)."""
    if half_life_days <= 0:
        return 0.0
    if days_forward <= 0:
        return 1.0
    return math.exp(-days_forward * math.log(2.0) / half_life_days)


def decayed_contributions(
    contributions: Mapping[str, float],
    *,
    days_forward: float,
    half_lives: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """Apply per-factor exponential decay to a contributions dict."""
    half_lives = dict(half_lives) if half_lives else {}
    out: dict[str, float] = {}
    for factor, contrib in contributions.items():
        if factor == "__intercept__":
            out[factor] = float(contrib)
            continue
        h = half_lives.get(factor, DEFAULT_HALF_LIFE_DAYS.get(factor, 30.0))
        out[factor] = float(contrib) * decay_factor(h, days_forward)
    return out


def decay_grid(
    contributions: Mapping[str, float],
    *,
    horizon_days: int,
    step_days: int = 5,
    half_lives: Mapping[str, float] | None = None,
) -> dict[str, list[float]]:
    """Build a (factor x days-forward) decay grid for the report heatmap.

    Returns {factor_name: [contribution_at_d0, d_step, 2*step, ...]}.
    """
    if step_days < 1:
        raise ValueError("step_days must be >= 1")
    if horizon_days < 1:
        raise ValueError("horizon_days must be >= 1")
    grid: dict[str, list[float]] = {f: [] for f in contributions}
    for d in range(0, horizon_days + 1, step_days):
        decayed = decayed_contributions(
            contributions, days_forward=d, half_lives=half_lives,
        )
        for factor in contributions:
            grid[factor].append(decayed[factor])
    return grid
