"""Tests for src.layer2_catalyst.setups."""
from __future__ import annotations

import pandas as pd
import pytest

from src.layer2_catalyst.setups import (
    ACTIVE_CONFLUENCE_THRESHOLD,
    SETUPS_VERSION,
    bearish_setup_features,
    bullish_setup_features,
    confluence_score,
    is_active,
)


def test_version_constant():
    assert SETUPS_VERSION == "1.0"


def test_active_threshold_is_two():
    """Two-or-more confirmation rule (Murphy 1999, ch. 17)."""
    assert ACTIVE_CONFLUENCE_THRESHOLD == 2


class TestConfluence:
    def test_all_true(self):
        assert confluence_score({"a": True, "b": True, "c": True}) == 3

    def test_all_false(self):
        assert confluence_score({"a": False, "b": False}) == 0

    def test_mixed(self):
        assert confluence_score({"a": True, "b": False, "c": True}) == 2

    def test_is_active_default(self):
        assert is_active({"a": True, "b": True}) is True
        assert is_active({"a": True, "b": False}) is False

    def test_is_active_custom_threshold(self):
        assert is_active({"a": True, "b": True, "c": True}, threshold=3) is True
        assert is_active({"a": True, "b": True}, threshold=3) is False


def _ohlc_frame(closes, opens=None, highs=None, lows=None, volumes=None):
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame(index=idx)
    df["open"] = opens if opens is not None else closes
    df["high"] = highs if highs is not None else [c * 1.01 for c in closes]
    df["low"] = lows if lows is not None else [c * 0.99 for c in closes]
    df["close"] = closes
    df["volume"] = volumes if volumes is not None else [1_000_000] * n
    return df


class TestBullishSetupFeatures:
    def test_within_5pct_of_support_true(self):
        closes = [100.0] * 60
        prices = _ohlc_frame(closes)
        pivots = pd.DataFrame({
            "is_swing_high": [False] * 60,
            "is_swing_low": [False] * 60,
            "nearest_support": [98.0] * 60,
            "nearest_resistance": [110.0] * 60,
        }, index=prices.index)
        out = bullish_setup_features(prices, {"pivots": pivots})
        assert out["within_5pct_of_support"] is True

    def test_within_5pct_of_support_false_when_far(self):
        closes = [100.0] * 60
        prices = _ohlc_frame(closes)
        pivots = pd.DataFrame({
            "nearest_support": [80.0] * 60,
        }, index=prices.index)
        out = bullish_setup_features(prices, {"pivots": pivots})
        assert out["within_5pct_of_support"] is False

    def test_base_breakout_true(self):
        closes = [100.0] * 30 + [110.0]
        prices = _ohlc_frame(closes)
        out = bullish_setup_features(prices, {})
        assert out["base_breakout"] is True

    def test_base_breakout_false_no_break(self):
        closes = [100.0] * 30 + [99.0]
        prices = _ohlc_frame(closes)
        out = bullish_setup_features(prices, {})
        assert out["base_breakout"] is False

    def test_rsi_not_overbought_true(self):
        closes = [100.0] * 60
        prices = _ohlc_frame(closes)
        rsi_series = pd.Series([45.0] * 60, index=prices.index)
        out = bullish_setup_features(prices, {"rsi": rsi_series})
        assert out["rsi_not_overbought"] is True

    def test_rsi_not_overbought_false_when_75(self):
        closes = [100.0] * 60
        prices = _ohlc_frame(closes)
        rsi_series = pd.Series([75.0] * 60, index=prices.index)
        out = bullish_setup_features(prices, {"rsi": rsi_series})
        assert out["rsi_not_overbought"] is False

    def test_volume_confirmation_above_threshold(self):
        closes = [100.0] * 30
        prices = _ohlc_frame(closes)
        vol_prof = pd.DataFrame({
            "avg_volume": [1_000_000] * 30,
            "volume_ratio": [1.5] * 30,
        }, index=prices.index)
        out = bullish_setup_features(prices, {"volume_profile": vol_prof})
        assert out["volume_confirmation"] is True

    def test_confluence_all_four(self):
        closes = [100.0] * 30 + [110.0]
        prices = _ohlc_frame(closes)
        n = len(prices)
        pivots = pd.DataFrame({
            "nearest_support": [105.0] * n,
        }, index=prices.index)
        rsi_series = pd.Series([50.0] * n, index=prices.index)
        vol_prof = pd.DataFrame({
            "avg_volume": [1_000_000] * n,
            "volume_ratio": [1.5] * n,
        }, index=prices.index)
        out = bullish_setup_features(prices, {
            "pivots": pivots, "rsi": rsi_series, "volume_profile": vol_prof,
        })
        assert confluence_score(out) == 4
        assert is_active(out) is True


class TestBearishSetupFeatures:
    def test_extended_above_200dma_true(self):
        n = 50
        closes = [115.0] * n
        prices = _ohlc_frame(closes)
        sma_200 = pd.Series([100.0] * n, index=prices.index)
        out = bearish_setup_features(prices, {"sma_200": sma_200})
        assert out["extended_15pct_above_200dma"] is True

    def test_extended_above_200dma_false_at_10pct(self):
        n = 50
        closes = [110.0] * n
        prices = _ohlc_frame(closes)
        sma_200 = pd.Series([100.0] * n, index=prices.index)
        out = bearish_setup_features(prices, {"sma_200": sma_200})
        assert out["extended_15pct_above_200dma"] is False

    def test_lower_high_pattern(self):
        n = 30
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        highs = [100.0] * n
        highs[5] = 120.0
        highs[20] = 115.0
        prices = pd.DataFrame({
            "open": [100.0] * n,
            "high": highs,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1_000_000] * n,
        }, index=idx)
        is_sh = [False] * n
        is_sh[5] = True
        is_sh[20] = True
        pivots = pd.DataFrame({
            "is_swing_high": is_sh,
            "is_swing_low": [False] * n,
            "nearest_support": [98.0] * n,
            "nearest_resistance": [110.0] * n,
        }, index=idx)
        out = bearish_setup_features(prices, {"pivots": pivots})
        assert out["lower_high"] is True

    def test_rsi_overbought_true(self):
        closes = [100.0] * 30
        prices = _ohlc_frame(closes)
        rsi_series = pd.Series([75.0] * 30, index=prices.index)
        out = bearish_setup_features(prices, {"rsi": rsi_series})
        assert out["rsi_overbought"] is True

    def test_volume_distribution_requires_red_bar(self):
        n = 30
        closes = [99.0] * n
        opens = [100.0] * n
        prices = _ohlc_frame(closes, opens=opens)
        vol_prof = pd.DataFrame({
            "avg_volume": [1_000_000] * n,
            "volume_ratio": [1.5] * n,
        }, index=prices.index)
        out = bearish_setup_features(prices, {"volume_profile": vol_prof})
        assert out["volume_distribution"] is True

    def test_volume_distribution_green_bar_no_signal(self):
        n = 30
        closes = [101.0] * n
        opens = [100.0] * n
        prices = _ohlc_frame(closes, opens=opens)
        vol_prof = pd.DataFrame({
            "avg_volume": [1_000_000] * n,
            "volume_ratio": [1.5] * n,
        }, index=prices.index)
        out = bearish_setup_features(prices, {"volume_profile": vol_prof})
        assert out["volume_distribution"] is False
