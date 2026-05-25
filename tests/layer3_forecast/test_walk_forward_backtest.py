"""Unit tests for src.layer3_forecast.walk_forward_backtest."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.layer3_forecast.walk_forward_backtest import (
    BacktestSummary,
    HorizonAccuracy,
    historical_baseline_forecast,
    run_walk_forward,
)


def _synthetic_close(n: int = 300, seed: int = 42, drift: float = 0.0005, vol: float = 0.02):
    rng = np.random.default_rng(seed)
    log_ret = rng.normal(drift, vol, size=n)
    prices = 100.0 * np.exp(np.cumsum(log_ret))
    dates = pd.bdate_range(end="2026-05-23", periods=n)
    return pd.Series(prices, index=dates)


class TestHistoricalBaselineForecast:
    def test_short_series_returns_default(self) -> None:
        close = _synthetic_close(n=10)
        pt, lo80, hi80, lo95, hi95 = historical_baseline_forecast(close, 60, 30)
        assert pt == 0.0
        assert lo80 == -0.02 and hi80 == 0.02
        assert lo95 == -0.04 and hi95 == 0.04

    def test_returns_tuple_of_5_floats(self) -> None:
        close = _synthetic_close(n=200)
        result = historical_baseline_forecast(close, 60, 30)
        assert len(result) == 5
        for v in result:
            assert isinstance(v, float)

    def test_lo80_below_hi80_and_95_envelope(self) -> None:
        close = _synthetic_close(n=200)
        pt, lo80, hi80, lo95, hi95 = historical_baseline_forecast(close, 60, 30)
        assert lo80 < hi80
        assert lo95 <= lo80
        assert hi95 >= hi80

    def test_horizon_scales_uncertainty(self) -> None:
        close = _synthetic_close(n=300)
        _, lo80_30, hi80_30, _, _ = historical_baseline_forecast(close, 60, 30)
        _, lo80_60, hi80_60, _, _ = historical_baseline_forecast(close, 60, 60)
        width_30 = hi80_30 - lo80_30
        width_60 = hi80_60 - lo80_60
        assert width_60 > width_30


class TestRunWalkForward:
    def test_returns_backtest_summary(self) -> None:
        close = _synthetic_close(n=300)
        summary = run_walk_forward(
            ticker="X", as_of_date="2026-05-23",
            close=close, lookback_days=60, horizons=(1, 30),
        )
        assert isinstance(summary, BacktestSummary)
        assert summary.ticker == "X"
        assert summary.lookback_days == 60

    def test_one_horizon_per_request(self) -> None:
        close = _synthetic_close(n=300)
        summary = run_walk_forward(
            ticker="X", as_of_date="2026-05-23",
            close=close, lookback_days=60, horizons=(1, 30, 60),
        )
        assert {h.horizon_days for h in summary.horizons} == {1, 30, 60}

    def test_horizon_accuracy_metrics_present(self) -> None:
        close = _synthetic_close(n=300)
        summary = run_walk_forward(
            ticker="X", as_of_date="2026-05-23",
            close=close, lookback_days=60, horizons=(30,),
        )
        assert len(summary.horizons) == 1
        ha = summary.horizons[0]
        assert isinstance(ha, HorizonAccuracy)
        assert ha.n_predictions > 0
        assert 0.0 <= ha.directional_accuracy <= 1.0
        assert ha.mae_return > 0
        assert ha.rmse_return >= ha.mae_return

    def test_too_short_series_returns_empty(self) -> None:
        close = _synthetic_close(n=50)
        summary = run_walk_forward(
            ticker="X", as_of_date="2026-05-23",
            close=close, lookback_days=60, horizons=(30,),
        )
        assert summary.horizons == []

    def test_custom_forecast_fn_invoked(self) -> None:
        calls = []
        def fake_fn(close_sub, lookback, h):
            calls.append((len(close_sub), lookback, h))
            return 0.01, -0.05, 0.07, -0.10, 0.12
        close = _synthetic_close(n=200)
        summary = run_walk_forward(
            ticker="X", as_of_date="2026-05-23",
            close=close, lookback_days=60, horizons=(30,),
            forecast_fn=fake_fn, step=10,
        )
        assert len(calls) > 0
        assert all(lb == 60 and h == 30 for (_, lb, h) in calls)
        ha = summary.horizons[0]
        assert ha.mean_predicted_return == pytest.approx(0.01)

    def test_step_param_reduces_walks(self) -> None:
        close = _synthetic_close(n=300)
        s1 = run_walk_forward(
            ticker="X", as_of_date="2026-05-23",
            close=close, lookback_days=60, horizons=(30,), step=1,
        )
        s5 = run_walk_forward(
            ticker="X", as_of_date="2026-05-23",
            close=close, lookback_days=60, horizons=(30,), step=5,
        )
        assert s1.horizons[0].n_predictions > s5.horizons[0].n_predictions

    def test_ci80_hit_rate_reasonable_on_iid_baseline(self) -> None:
        """AR(1) baseline on iid returns: 80% CI hit rate should land 50-99%."""
        rng = np.random.default_rng(7)
        log_ret = rng.normal(0.0, 0.02, size=400)
        close = pd.Series(
            100.0 * np.exp(np.cumsum(log_ret)),
            index=pd.bdate_range(end="2026-05-23", periods=400),
        )
        summary = run_walk_forward(
            ticker="X", as_of_date="2026-05-23",
            close=close, lookback_days=60, horizons=(1,),
        )
        ha = summary.horizons[0]
        assert 0.50 < ha.ci80_hit_rate < 0.99
