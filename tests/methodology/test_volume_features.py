"""Unit tests for src.methodology.volume_features."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.methodology.volume_features import (
    VOLUME_FEATURES_VERSION,
    chaikin_money_flow,
    compute_all_volume_features,
    money_flow_index,
    on_balance_volume,
    relative_volume,
    volume_confirmed_forecast,
    volume_momentum_composite,
    volume_price_trend,
    volume_weighted_momentum,
    volume_zscore,
)


def _ohlcv(n: int = 250, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    log_ret = rng.normal(0.0005, 0.02, size=n)
    close = 100.0 * np.exp(np.cumsum(log_ret))
    high = close * (1 + np.abs(rng.normal(0, 0.005, size=n)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, size=n)))
    volume = rng.lognormal(mean=15, sigma=0.5, size=n)
    dates = pd.bdate_range(end="2026-05-23", periods=n)
    return pd.DataFrame(
        {"high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )


class TestOnBalanceVolume:
    def test_obv_starts_zero_and_accumulates(self) -> None:
        df = _ohlcv(n=100)
        obv = on_balance_volume(df)
        assert obv.name == "obv"
        assert len(obv) == len(df)
        assert obv.iloc[0] == 0.0

    def test_obv_increases_on_up_day(self) -> None:
        df = pd.DataFrame({
            "close":  [100, 101, 102, 103, 104],
            "volume": [1000, 2000, 3000, 4000, 5000],
        }, index=pd.bdate_range(end="2026-05-23", periods=5))
        obv = on_balance_volume(df)
        assert obv.iloc[-1] == 2000 + 3000 + 4000 + 5000

    def test_obv_decreases_on_down_day(self) -> None:
        df = pd.DataFrame({
            "close":  [100, 99, 98, 97],
            "volume": [1000, 2000, 3000, 4000],
        }, index=pd.bdate_range(end="2026-05-23", periods=4))
        obv = on_balance_volume(df)
        assert obv.iloc[-1] == -2000 - 3000 - 4000

    def test_missing_columns_raises(self) -> None:
        df = pd.DataFrame({"close": [100, 101]})
        with pytest.raises(ValueError, match="missing columns"):
            on_balance_volume(df)


class TestVolumePriceTrend:
    def test_vpt_responds_to_pct_change(self) -> None:
        df = pd.DataFrame({
            "close":  [100, 105, 100],
            "volume": [1000, 1000, 1000],
        }, index=pd.bdate_range(end="2026-05-23", periods=3))
        vpt = volume_price_trend(df)
        assert vpt.iloc[-1] == pytest.approx(50.0 - 1000.0 * 5.0 / 105.0, rel=1e-3)


class TestMoneyFlowIndex:
    def test_mfi_in_0_100_range_when_data_sufficient(self) -> None:
        df = _ohlcv(n=80)
        mfi = money_flow_index(df, window=14)
        valid = mfi.dropna()
        assert len(valid) > 0
        for v in valid:
            assert 0.0 <= v <= 100.0

    def test_mfi_needs_window_for_first_value(self) -> None:
        df = _ohlcv(n=80)
        mfi = money_flow_index(df, window=14)
        # First window-1=13 entries are NaN (rolling needs `window`
        # observations including current row to emit the first value).
        assert mfi.iloc[:13].isna().all()
        assert mfi.iloc[13:].notna().any()


class TestVolumeWeightedMomentum:
    def test_returns_series_with_lookback_burn_in(self) -> None:
        df = _ohlcv(n=100)
        vwmom = volume_weighted_momentum(df, lookback=20)
        assert vwmom.name == "vwmom"
        assert len(vwmom) == len(df)
        assert vwmom.dropna().shape[0] > 50


class TestVolumeZscore:
    def test_returns_zscores_relative_to_window(self) -> None:
        df = _ohlcv(n=120)
        z = volume_zscore(df, window=60)
        valid = z.dropna()
        assert -0.5 < valid.mean() < 0.5


class TestRelativeVolume:
    def test_relative_volume_centered_around_1(self) -> None:
        df = _ohlcv(n=100)
        rv = relative_volume(df, window=20)
        valid = rv.dropna()
        assert 0.5 < valid.mean() < 2.0


class TestChaikinMoneyFlow:
    def test_cmf_in_minus1_to_1(self) -> None:
        df = _ohlcv(n=100)
        cmf = chaikin_money_flow(df, window=20)
        valid = cmf.dropna()
        for v in valid:
            assert -1.0 <= v <= 1.0


class TestComputeAllVolumeFeatures:
    def test_returns_all_seven_columns(self) -> None:
        df = _ohlcv(n=100)
        out = compute_all_volume_features(df)
        expected = {"obv", "vpt", "mfi", "vwmom", "volume_z", "rel_volume", "cmf"}
        assert set(out.columns) == expected
        assert len(out) == len(df)


class TestVolumeMomentumComposite:
    def test_composite_within_clip_range(self) -> None:
        df = _ohlcv(n=300)
        comp = volume_momentum_composite(df)
        assert -3.0 <= comp["composite"] <= 3.0
        assert comp["n_signals"] >= 1
        assert "obv_z" in comp
        assert "mfi" in comp

    def test_insufficient_history_returns_zero_signals(self) -> None:
        df = _ohlcv(n=10)
        comp = volume_momentum_composite(df)
        assert comp["n_signals"] == 0


class TestVolumeConfirmedForecast:
    def test_returns_tuple_of_5(self) -> None:
        df = _ohlcv(n=200)
        result = volume_confirmed_forecast(
            df["close"], 60, 30, volume=df["volume"],
        )
        assert len(result) == 5
        for v in result:
            assert isinstance(v, float)

    def test_default_to_baseline_when_no_volume(self) -> None:
        df = _ohlcv(n=200)
        pt, lo80, hi80, lo95, hi95 = volume_confirmed_forecast(
            df["close"], 60, 30, volume=None,
        )
        assert lo80 < hi80
        assert lo95 <= lo80 and hi95 >= hi80


def test_module_version_exported() -> None:
    assert VOLUME_FEATURES_VERSION == "1.0"
