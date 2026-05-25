"""Tests for src.methodology.technical_indicators (Phase B.2).

Golden values hand-computed from Wilder 1978 / Appel 1979 worked
examples + standard pandas semantics for SMA/EMA.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.methodology.technical_indicators import (
    TECHNICAL_INDICATORS_VERSION,
    atr_14,
    ema,
    macd,
    pivot_points,
    rsi_14,
    sma,
    volume_profile,
)


def test_version_constant():
    assert TECHNICAL_INDICATORS_VERSION == "1.0"


def test_sma_basic():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = sma(s, window=3)
    assert math.isnan(out.iloc[0])
    assert math.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)
    assert out.iloc[3] == pytest.approx(3.0)
    assert out.iloc[4] == pytest.approx(4.0)


def test_sma_window_zero_raises():
    with pytest.raises(ValueError):
        sma(pd.Series([1.0]), window=0)


def test_ema_basic():
    """EMA span=3 -> alpha=0.5. Manual: 1, 1.5, 2.25, 3.125, 4.0625"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ema(s, window=3)
    assert math.isnan(out.iloc[0])
    assert math.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.25)
    assert out.iloc[3] == pytest.approx(3.125)
    assert out.iloc[4] == pytest.approx(4.0625)


def test_rsi_monotone_up_gives_high_value():
    s = pd.Series([float(i) for i in range(1, 30)])
    out = rsi_14(s, period=14)
    last = out.dropna().iloc[-1]
    assert last == pytest.approx(100.0)


def test_rsi_monotone_down_gives_low_value():
    s = pd.Series([float(30 - i) for i in range(30)])
    out = rsi_14(s, period=14)
    last = out.dropna().iloc[-1]
    assert last == pytest.approx(0.0)


def test_rsi_mid_range_on_random_walk():
    rng = np.random.default_rng(42)
    s = pd.Series(100.0 + rng.normal(0.0, 1.0, 60).cumsum())
    out = rsi_14(s, period=14).dropna()
    assert out.min() >= 0.0
    assert out.max() <= 100.0
    assert 20.0 < out.mean() < 80.0


def test_macd_columns():
    s = pd.Series(100.0 + np.arange(50, dtype=float))
    out = macd(s)
    assert list(out.columns) == ["macd", "signal", "histogram"]
    valid = out.dropna()
    assert ((valid["histogram"] - (valid["macd"] - valid["signal"])).abs() < 1e-9).all()


def test_macd_trending_up_gives_positive_line():
    s = pd.Series(100.0 + np.arange(80, dtype=float))
    out = macd(s)
    assert out["macd"].dropna().iloc[-1] > 0


def test_macd_fast_ge_slow_raises():
    s = pd.Series([1.0] * 30)
    with pytest.raises(ValueError):
        macd(s, fast=26, slow=12)


def test_atr_14_constant_range_returns_constant_atr():
    n = 30
    df = pd.DataFrame({
        "open": [100.0] * n,
        "high": [100.5] * n,
        "low": [99.5] * n,
        "close": [100.0] * n,
    })
    out = atr_14(df, period=14)
    last = out.dropna().iloc[-1]
    assert last == pytest.approx(1.0, abs=0.05)


def test_atr_14_missing_column_raises():
    df = pd.DataFrame({"high": [1.0], "low": [0.5]})
    with pytest.raises(ValueError, match="OHLC columns missing"):
        atr_14(df)


def test_volume_profile_above_average_ratio():
    """20 days of vol=100, then one day at 200. At index 20 the rolling
    window is positions 1..20 inclusive (the new spike + 19 hundreds)
    so avg = 105 and ratio = 200/105 ~ 1.905. The point is `ratio > 1`."""
    vols = pd.Series([100.0] * 20 + [200.0])
    out = volume_profile(vols, window=20)
    assert out["avg_volume"].iloc[19] == pytest.approx(100.0)
    assert out["volume_ratio"].iloc[20] > 1.5


def test_volume_profile_returns_nan_before_window_filled():
    vols = pd.Series([100.0] * 10)
    out = volume_profile(vols, window=20)
    assert out["avg_volume"].isna().all()


def test_pivot_points_identifies_swing_high():
    n = 50
    h = np.array(
        [100.0 + i for i in range(25)]
        + [125.0]
        + [125.0 - i for i in range(24)]
    )
    df = pd.DataFrame({
        "open": h,
        "high": h,
        "low": h - 1.0,
        "close": h - 0.5,
    })
    out = pivot_points(df, lookback=5)
    assert bool(out["is_swing_high"].iloc[25]) is True


def test_pivot_points_missing_column_raises():
    df = pd.DataFrame({"high": [1.0]})
    with pytest.raises(ValueError, match="OHLC columns missing"):
        pivot_points(df)


def test_pivot_points_zero_lookback_raises():
    df = pd.DataFrame({"high": [1.0], "low": [0.5], "close": [0.75]})
    with pytest.raises(ValueError, match="lookback must be >= 1"):
        pivot_points(df, lookback=0)
