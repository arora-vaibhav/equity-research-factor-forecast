"""Tests for `src.methodology.yang_zhang_vol`.

Coverage:
  * Closed-form check on constant-vol GBM synthetic data: estimator
    should land within +/- a few vol points of the input sigma over a
    60-day window.
  * Zero-variance OHLC (all four prices identical, no overnight gap)
    -> zero vol.
  * Insufficient history -> all-NaN.
  * Yang-Zhang vs close-to-close: YZ should not have worse sampling
    variance across many independent realizations of the same DGP.
  * Column normalization handles capitalised Yahoo-style column names.
  * Bad inputs raise ValueError.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.methodology.yang_zhang_vol import (
    YANG_ZHANG_VERSION,
    yang_zhang_vol,
)


def test_version_constant():
    assert YANG_ZHANG_VERSION == "1.0"


def _gbm_ohlc(
    *,
    n_days: int,
    sigma_annual: float,
    seed: int,
    initial_price: float = 100.0,
) -> pd.DataFrame:
    """Generate synthetic OHLC under GBM with given annualised sigma.

    Each day:
      gap     = N(0, sigma_daily * 0.3) -- small overnight noise
      O_t     = C_{t-1} * exp(gap)
      5-pt intraday random walk -> C_t, H_t, L_t

    Close-to-close vol approximately equals sigma_annual.
    """
    rng = np.random.default_rng(seed)
    sigma_daily = sigma_annual / math.sqrt(252.0)
    closes = [initial_price]
    opens = [initial_price]
    highs = [initial_price]
    lows = [initial_price]
    for _ in range(n_days - 1):
        prev_c = closes[-1]
        gap = rng.normal(0.0, sigma_daily * 0.3)
        o = prev_c * math.exp(gap)
        path = [o]
        for _step in range(5):
            path.append(path[-1] * math.exp(rng.normal(0.0, sigma_daily / math.sqrt(5))))
        c = path[-1]
        h = max(path)
        l_ = min(path)
        closes.append(c)
        opens.append(o)
        highs.append(h)
        lows.append(l_)
    idx = pd.date_range("2024-01-01", periods=n_days, freq="B")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes},
        index=idx,
    )


# ---------------------------------------------------------------------------
# Closed-form behaviour
# ---------------------------------------------------------------------------


def test_constant_vol_gbm_recovers_sigma_within_tolerance():
    """On constant-vol GBM with target sigma=25%, the 60-day YZ vol
    should land within +/- 7 vol points of 0.25 across the window
    endpoints. The 5-step intraday path used here underestimates true
    intraday range modestly (the random walk's max-min over 5 steps is
    smaller than the continuous-time supremum), so we allow a wider
    band than the Yang-Zhang theoretical tolerance."""
    df = _gbm_ohlc(n_days=300, sigma_annual=0.25, seed=42)
    yz = yang_zhang_vol(df, window_days=60)
    valid = yz.dropna()
    assert len(valid) > 100
    mean_yz = valid.mean()
    assert 0.18 < mean_yz < 0.32, (mean_yz, valid.describe())


def test_zero_variance_yields_zero_vol():
    n = 70
    flat = pd.DataFrame(
        {"open": [100.0] * n, "high": [100.0] * n, "low": [100.0] * n, "close": [100.0] * n},
        index=pd.date_range("2024-01-01", periods=n, freq="B"),
    )
    yz = yang_zhang_vol(flat, window_days=60)
    valid = yz.dropna()
    assert len(valid) > 0
    assert (valid.abs() < 1e-12).all(), valid


def test_insufficient_history_returns_all_nan():
    df = _gbm_ohlc(n_days=30, sigma_annual=0.25, seed=7)
    yz = yang_zhang_vol(df, window_days=60)
    assert yz.isna().all()


def test_empty_input_returns_empty_series():
    df = pd.DataFrame(columns=["open", "high", "low", "close"])
    yz = yang_zhang_vol(df, window_days=60)
    assert len(yz) == 0


# ---------------------------------------------------------------------------
# Statistical efficiency
# ---------------------------------------------------------------------------


def test_yang_zhang_not_worse_than_close_to_close():
    """Across 20 independent realizations of GBM with sigma=30%, the
    cross-seed std-dev of the final-window YZ estimate should not
    exceed the cross-seed std-dev of the close-to-close estimate by
    more than a tolerance factor. Yang-Zhang 2000 reports ~14x
    efficiency gain on idealised inputs; we allow generous slack."""
    sigma_true = 0.30
    n_days = 200
    yz_estimates = []
    cc_estimates = []
    for seed in range(20):
        df = _gbm_ohlc(n_days=n_days, sigma_annual=sigma_true, seed=seed)
        yz = yang_zhang_vol(df, window_days=60)
        yz_estimates.append(yz.dropna().iloc[-1])
        log_ret = np.log(df["Close"] / df["Close"].shift(1)).dropna()
        cc = log_ret.iloc[-60:].std() * math.sqrt(252)
        cc_estimates.append(cc)
    yz_arr = np.array(yz_estimates)
    cc_arr = np.array(cc_estimates)
    assert yz_arr.std() <= cc_arr.std() * 1.5, (yz_arr.std(), cc_arr.std())


# ---------------------------------------------------------------------------
# Column normalization
# ---------------------------------------------------------------------------


def test_handles_capitalised_yahoo_columns():
    df = _gbm_ohlc(n_days=80, sigma_annual=0.25, seed=1)
    yz = yang_zhang_vol(df, window_days=60)
    assert yz.dropna().shape[0] > 10


def test_handles_lowercase_columns():
    df = _gbm_ohlc(n_days=80, sigma_annual=0.25, seed=1)
    df_lower = df.rename(columns=str.lower)
    yz = yang_zhang_vol(df_lower, window_days=60)
    assert yz.dropna().shape[0] > 10


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_missing_column_raises():
    df = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0]},  # close missing
        index=pd.date_range("2024-01-01", periods=1, freq="B"),
    )
    with pytest.raises(ValueError, match="OHLC columns missing"):
        yang_zhang_vol(df, window_days=60)


def test_window_too_small_raises():
    df = _gbm_ohlc(n_days=10, sigma_annual=0.25, seed=1)
    with pytest.raises(ValueError, match="window_days must be >= 2"):
        yang_zhang_vol(df, window_days=1)
