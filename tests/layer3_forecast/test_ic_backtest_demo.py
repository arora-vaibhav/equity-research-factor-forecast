"""Unit tests for src.layer3_forecast.ic_backtest_demo."""
from __future__ import annotations

import pandas as pd
import pytest

from src.layer3_forecast.factor_backtest import BacktestResult
from src.layer3_forecast.factor_forecast import DEFAULT_FACTORS
from src.layer3_forecast.ic_backtest_demo import (
    build_synthetic_panel,
    run_synthetic_ic_backtest,
)


class TestBuildSyntheticPanel:
    def test_shape_matches_n_dates_x_n_tickers(self) -> None:
        df = build_synthetic_panel(n_dates=10, n_tickers=5, seed=1)
        assert len(df) == 10 * 5

    def test_required_columns_present(self) -> None:
        df = build_synthetic_panel(n_dates=5, n_tickers=3, seed=1)
        expected_cols = {"date", "ticker", "realized_horizon_return"} | set(DEFAULT_FACTORS)
        assert expected_cols.issubset(set(df.columns))

    def test_deterministic_with_seed(self) -> None:
        a = build_synthetic_panel(n_dates=5, n_tickers=3, seed=42)
        b = build_synthetic_panel(n_dates=5, n_tickers=3, seed=42)
        pd.testing.assert_frame_equal(a, b)

    def test_different_seeds_yield_different_panels(self) -> None:
        a = build_synthetic_panel(n_dates=5, n_tickers=3, seed=1)
        b = build_synthetic_panel(n_dates=5, n_tickers=3, seed=2)
        assert not a.equals(b)

    def test_realized_return_finite_and_bounded(self) -> None:
        df = build_synthetic_panel(n_dates=20, n_tickers=10, seed=1)
        rr = df["realized_horizon_return"]
        assert rr.notna().all()
        # Expected order of magnitude: factor coefs ~0.04 each, 6 factors,
        # factor z standard normal -> sigma ~ sqrt(6)*0.04 ~ 0.10 + 0.03 noise.
        assert rr.abs().max() < 1.5

    def test_factor_value_finite(self) -> None:
        df = build_synthetic_panel(n_dates=10, n_tickers=5, seed=1)
        for f in DEFAULT_FACTORS:
            assert df[f].notna().all()


class TestRunSyntheticIcBacktest:
    def test_returns_backtest_result(self) -> None:
        result = run_synthetic_ic_backtest(horizon_days=30, n_dates=30, n_tickers=20, seed=1)
        assert isinstance(result, BacktestResult)
        assert result.horizon_days == 30

    def test_all_factors_have_stats(self) -> None:
        result = run_synthetic_ic_backtest(horizon_days=30, n_dates=30, n_tickers=20, seed=1)
        assert set(result.per_factor.keys()) == set(DEFAULT_FACTORS)

    def test_factor_with_positive_true_coef_has_positive_mean_ic(self) -> None:
        """value_score has true_coef +0.045 in the synthetic build;
        mean IC should be clearly positive on a 60d x 50-ticker panel."""
        result = run_synthetic_ic_backtest(
            horizon_days=30, n_dates=60, n_tickers=50, seed=42,
        )
        assert result.per_factor["value_score"].mean_ic > 0.0
        assert result.per_factor["news_activity_score"].mean_ic > 0.0

    def test_ic_series_indexed_by_date(self) -> None:
        result = run_synthetic_ic_backtest(
            horizon_days=30, n_dates=30, n_tickers=20, seed=1,
        )
        assert isinstance(result.ic_series, pd.DataFrame)
        assert not result.ic_series.empty
        for f in DEFAULT_FACTORS:
            assert f in result.ic_series.columns

    def test_icir_is_finite(self) -> None:
        result = run_synthetic_ic_backtest(
            horizon_days=30, n_dates=60, n_tickers=50, seed=1,
        )
        for f, stats in result.per_factor.items():
            assert stats.icir == stats.icir  # not NaN
