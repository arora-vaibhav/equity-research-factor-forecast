"""Technical setup feature extractors.

I use two binary feature blocks to summarise the latest bar of a
price series. Each block is four True/False checks; the helper
``confluence_score`` sums them and ``is_active`` thresholds the sum.

Bullish setup features (``bullish_setup_features``):
  1. within 5% of nearest support
  2. base breakout: close > prior 30-day high
  3. RSI not overbought (< 70)
  4. volume confirmation (today's volume >= 1.2x 20-day average)

Bearish setup features (``bearish_setup_features``):
  1. extended >= 15% above 200-day MA
  2. lower-high pattern (last swing high lower than previous)
  3. RSI overbought (> 70)
  4. volume distribution (today's volume >= 1.2x avg AND red bar)

The active threshold of 2 follows Murphy's two-or-more confirmation
rule (Murphy 1999, "Technical Analysis of the Financial Markets",
ch. 17): a single indicator firing in isolation is not treated as a
setup; two or more independent confirmations are required.

Pure functions. No DB, no I/O.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

import pandas as pd


SETUPS_VERSION = "1.0"

ACTIVE_CONFLUENCE_THRESHOLD = 2


def _safe_last(series: pd.Series) -> float:
    """Return the last non-NaN value of `series` or NaN if all NaN."""
    s = series.dropna()
    if len(s) == 0:
        return float("nan")
    return float(s.iloc[-1])


def bullish_setup_features(
    prices: pd.DataFrame,
    indicators: Mapping[str, Any],
) -> dict[str, bool]:
    """Evaluate the 4 bullish setup features on the latest bar."""
    cols = {c.lower(): c for c in prices.columns}
    close = prices[cols.get("close", "close")]
    high = prices[cols.get("high", "high")]
    last_close = _safe_last(close)

    out: dict[str, bool] = {
        "within_5pct_of_support": False,
        "base_breakout": False,
        "rsi_not_overbought": False,
        "volume_confirmation": False,
    }

    pivots = indicators.get("pivots")
    if isinstance(pivots, pd.DataFrame) and "nearest_support" in pivots.columns:
        support = _safe_last(pivots["nearest_support"])
        if math.isfinite(support) and math.isfinite(last_close) and support > 0:
            distance_pct = (last_close - support) / last_close
            out["within_5pct_of_support"] = 0.0 <= distance_pct <= 0.05

    if math.isfinite(last_close) and len(high) >= 31:
        prior_30_high = float(high.iloc[-31:-1].max())
        out["base_breakout"] = last_close > prior_30_high

    rsi = indicators.get("rsi")
    if isinstance(rsi, pd.Series):
        last_rsi = _safe_last(rsi)
        if math.isfinite(last_rsi):
            out["rsi_not_overbought"] = last_rsi < 70.0

    vol_prof = indicators.get("volume_profile")
    if isinstance(vol_prof, pd.DataFrame) and "volume_ratio" in vol_prof.columns:
        last_ratio = _safe_last(vol_prof["volume_ratio"])
        if math.isfinite(last_ratio):
            out["volume_confirmation"] = last_ratio >= 1.2

    return out


def bearish_setup_features(
    prices: pd.DataFrame,
    indicators: Mapping[str, Any],
) -> dict[str, bool]:
    """Evaluate the 4 bearish setup features on the latest bar."""
    cols = {c.lower(): c for c in prices.columns}
    close = prices[cols.get("close", "close")]
    open_ = prices[cols.get("open", "open")]
    last_close = _safe_last(close)
    last_open = _safe_last(open_)

    out: dict[str, bool] = {
        "extended_15pct_above_200dma": False,
        "lower_high": False,
        "rsi_overbought": False,
        "volume_distribution": False,
    }

    sma_200 = indicators.get("sma_200")
    if isinstance(sma_200, pd.Series):
        last_sma = _safe_last(sma_200)
        if math.isfinite(last_sma) and last_sma > 0 and math.isfinite(last_close):
            extension = (last_close - last_sma) / last_sma
            out["extended_15pct_above_200dma"] = extension >= 0.15

    pivots = indicators.get("pivots")
    if isinstance(pivots, pd.DataFrame) and "is_swing_high" in pivots.columns:
        swing_high_dates = pivots.index[pivots["is_swing_high"]]
        if len(swing_high_dates) >= 2 and "high" in cols:
            high_col = cols["high"]
            most_recent_high_val = float(prices.loc[swing_high_dates[-1], high_col])
            prev_high_val = float(prices.loc[swing_high_dates[-2], high_col])
            out["lower_high"] = most_recent_high_val < prev_high_val

    rsi = indicators.get("rsi")
    if isinstance(rsi, pd.Series):
        last_rsi = _safe_last(rsi)
        if math.isfinite(last_rsi):
            out["rsi_overbought"] = last_rsi > 70.0

    vol_prof = indicators.get("volume_profile")
    if isinstance(vol_prof, pd.DataFrame) and "volume_ratio" in vol_prof.columns:
        last_ratio = _safe_last(vol_prof["volume_ratio"])
        is_red = (
            math.isfinite(last_open)
            and math.isfinite(last_close)
            and last_close < last_open
        )
        if math.isfinite(last_ratio):
            out["volume_distribution"] = last_ratio >= 1.2 and is_red

    return out


def confluence_score(setups: Mapping[str, bool]) -> int:
    """Count of True criteria. Range 0-4 for the setup feature blocks."""
    return int(sum(1 for v in setups.values() if bool(v)))


def is_active(setups: Mapping[str, bool], threshold: int = ACTIVE_CONFLUENCE_THRESHOLD) -> bool:
    """True iff confluence_score(setups) >= threshold (default 2)."""
    return confluence_score(setups) >= threshold
