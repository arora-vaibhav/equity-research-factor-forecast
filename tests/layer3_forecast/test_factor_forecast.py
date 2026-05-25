"""Tests for src.layer3_forecast.factor_forecast (Phase F.1)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.layer3_forecast.factor_forecast import (
    DEFAULT_FACTORS,
    FORECAST_VERSION,
    compute_factor_loadings,
    forecast_return,
)


def test_version_constant():
    assert FORECAST_VERSION == "1.0"


def test_default_factors_includes_six_canonical():
    assert "value_score" in DEFAULT_FACTORS
    assert "news_activity_score" in DEFAULT_FACTORS
    assert len(DEFAULT_FACTORS) == 6


def _build_history(n=200, seed=42, signal_strength=0.05):
    rng = np.random.default_rng(seed)
    n_factors = len(DEFAULT_FACTORS)
    X = rng.normal(0.0, 1.0, size=(n, n_factors))
    true_coef = np.zeros(n_factors)
    true_coef[DEFAULT_FACTORS.index("value_score")] = signal_strength
    true_coef[DEFAULT_FACTORS.index("momentum_score")] = signal_strength * 0.6
    y = X @ true_coef + rng.normal(0.0, 0.02, size=n)
    df = pd.DataFrame(X, columns=list(DEFAULT_FACTORS))
    df["realized_horizon_return"] = y
    return df


def test_compute_factor_loadings_recovers_signal():
    df = _build_history(n=500)
    loadings, intercept, residuals = compute_factor_loadings(df, horizon_days=30)
    assert loadings["value_score"] == pytest.approx(0.05, abs=0.01)
    assert loadings["momentum_score"] == pytest.approx(0.03, abs=0.01)
    assert abs(loadings["quality_score"]) < 0.02
    assert len(residuals) == len(df)


def test_compute_factor_loadings_missing_target_raises():
    df = pd.DataFrame({"value_score": [0.0]})
    with pytest.raises(ValueError, match="realized_horizon_return"):
        compute_factor_loadings(df, horizon_days=30)


def test_compute_factor_loadings_missing_factor_raises():
    df = pd.DataFrame({"realized_horizon_return": [0.0] * 5})
    with pytest.raises(ValueError, match="missing factor"):
        compute_factor_loadings(df, horizon_days=30)


def test_compute_factor_loadings_insufficient_rows_returns_zero():
    df = pd.DataFrame({f: [0.0, 0.1] for f in DEFAULT_FACTORS})
    df["realized_horizon_return"] = [0.0, 0.1]
    loadings, intercept, residuals = compute_factor_loadings(df, horizon_days=30)
    assert (loadings == 0.0).all()
    assert intercept == 0.0
    assert len(residuals) == 0


def test_forecast_return_point_uses_loadings():
    loadings = pd.Series({f: 0.0 for f in DEFAULT_FACTORS})
    loadings["value_score"] = 0.10
    residuals = np.array([0.0] * 100)
    result = forecast_return(
        ticker_factors={"value_score": 1.0},
        loadings=loadings, intercept=0.0, residuals=residuals,
        ticker="AAPL", as_of_date="2026-05-23", horizon_days=30,
    )
    assert result.point_return == pytest.approx(0.10, abs=1e-9)
    assert result.factor_contributions["value_score"] == pytest.approx(0.10)
    assert result.lower_80 == pytest.approx(0.10, abs=1e-9)
    assert result.upper_80 == pytest.approx(0.10, abs=1e-9)


def test_forecast_return_intervals_widen_with_noise():
    loadings = pd.Series({f: 0.0 for f in DEFAULT_FACTORS})
    rng = np.random.default_rng(1)
    residuals_small = rng.normal(0.0, 0.01, size=200)
    residuals_big = rng.normal(0.0, 0.10, size=200)
    r_small = forecast_return(
        ticker_factors={}, loadings=loadings, intercept=0.0,
        residuals=residuals_small,
    )
    r_big = forecast_return(
        ticker_factors={}, loadings=loadings, intercept=0.0,
        residuals=residuals_big,
    )
    width_small = r_small.upper_80 - r_small.lower_80
    width_big = r_big.upper_80 - r_big.lower_80
    assert width_big > width_small * 3.0


def test_forecast_return_attribution_sums_to_point_minus_intercept():
    loadings = pd.Series({
        "value_score": 0.05, "quality_score": 0.02,
        "momentum_score": 0.03, "lowvol_score": 0.0,
        "revisions_score": 0.01, "news_activity_score": 0.04,
    })
    factors = {
        "value_score": 1.0, "quality_score": 0.5,
        "momentum_score": -0.5, "news_activity_score": 1.0,
    }
    residuals = np.zeros(100)
    result = forecast_return(
        ticker_factors=factors, loadings=loadings,
        intercept=0.01, residuals=residuals,
    )
    contribs = result.factor_contributions
    summed = sum(v for k, v in contribs.items() if k != "__intercept__")
    expected = (
        0.05 * 1.0 + 0.02 * 0.5 + 0.03 * -0.5 + 0.0 + 0.0 + 0.04 * 1.0
    )
    assert summed == pytest.approx(expected, abs=1e-9)
    assert contribs["__intercept__"] == pytest.approx(0.01)


def test_forecast_return_missing_factors_treated_as_zero():
    loadings = pd.Series({f: 0.05 for f in DEFAULT_FACTORS})
    residuals = np.zeros(100)
    result = forecast_return(
        ticker_factors={"value_score": 1.0},
        loadings=loadings, intercept=0.0, residuals=residuals,
    )
    assert result.point_return == pytest.approx(0.05, abs=1e-9)


def test_forecast_return_insufficient_residuals_collapses_intervals():
    loadings = pd.Series({f: 0.0 for f in DEFAULT_FACTORS})
    residuals = np.array([0.01, 0.02])
    result = forecast_return(
        ticker_factors={}, loadings=loadings, intercept=0.05,
        residuals=residuals,
    )
    assert result.point_return == pytest.approx(0.05)
    assert result.lower_80 == result.point_return
    assert result.upper_80 == result.point_return
    assert result.n_bootstrap == 0
