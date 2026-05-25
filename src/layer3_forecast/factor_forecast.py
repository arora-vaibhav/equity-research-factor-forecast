"""Multi-factor return-forecast layer (Phase F.1).

Synthesises all aggregated factors from Phase A.3 (fundamentals +
technicals + catalysts + news_activity) into a forward return forecast
with confidence intervals and per-factor attribution.

Methodology choice: linear multi-factor expected-return regression +
bootstrapped 80% / 95% intervals. Rationale:

  * Gu, Kelly, Xiu 2020 *RFS* "Empirical Asset Pricing via Machine
    Learning": linear factor models match or exceed deep-learning
    approaches for monthly-to-quarterly horizons when factors are
    well-engineered.
  * Krauss, Do, Huck 2017 *EJOR*: same conclusion on US equities.
  * Bootstrap intervals (Efron 1979) are non-parametric and
    well-behaved on non-Gaussian residuals -- crucial for finance.
  * Per-factor attribution via additive linear contribution
    (coef * x). Equivalent to Shapley values for the linear case
    (Lundberg & Lee 2017 SHAP Theorem 2).

Pure functions. No DB. Caller hands in numpy/pandas data; module
returns ForecastResult dataclass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import numpy as np
import pandas as pd


FORECAST_VERSION = "1.0"

DEFAULT_FACTORS: tuple[str, ...] = (
    "value_score",
    "quality_score",
    "momentum_score",
    "lowvol_score",
    "revisions_score",
    "news_activity_score",
)


@dataclass
class ForecastResult:
    """Forward-return forecast for one ticker."""
    ticker: str
    as_of_date: str
    horizon_days: int
    point_return: float
    lower_80: float
    upper_80: float
    lower_95: float
    upper_95: float
    factor_contributions: dict[str, float] = field(default_factory=dict)
    n_train: int = 0
    n_bootstrap: int = 0
    forecast_version: str = FORECAST_VERSION


def compute_factor_loadings(
    history: pd.DataFrame,
    *,
    horizon_days: int,
    factors: tuple[str, ...] = DEFAULT_FACTORS,
) -> tuple[pd.Series, float, np.ndarray]:
    """Cross-sectional OLS regression: realized_return ~ factors.

    Returns (loadings, intercept, residuals).
      - loadings: per-factor coefficient indexed by factor name
      - intercept: regression intercept (alpha)
      - residuals: in-sample residuals for the bootstrap
    """
    if "realized_horizon_return" not in history.columns:
        raise ValueError(
            "history must contain a 'realized_horizon_return' column"
        )
    missing = [f for f in factors if f not in history.columns]
    if missing:
        raise ValueError(f"history missing factor columns: {missing}")

    df = history.dropna(subset=list(factors) + ["realized_horizon_return"])
    if len(df) < len(factors) + 2:
        loadings = pd.Series([0.0] * len(factors), index=list(factors))
        return loadings, 0.0, np.array([])

    X = df[list(factors)].to_numpy(dtype=float)
    y = df["realized_horizon_return"].to_numpy(dtype=float)
    X_aug = np.hstack([np.ones((X.shape[0], 1)), X])
    coef, *_ = np.linalg.lstsq(X_aug, y, rcond=None)
    intercept = float(coef[0])
    loadings = pd.Series(coef[1:], index=list(factors))

    y_hat = X_aug @ coef
    residuals = y - y_hat
    return loadings, intercept, residuals


def forecast_return(
    ticker_factors: Mapping[str, float],
    loadings: pd.Series,
    intercept: float,
    residuals: np.ndarray,
    *,
    ticker: str = "",
    as_of_date: str = "",
    horizon_days: int = 30,
    n_bootstrap: int = 1000,
    rng_seed: int = 42,
) -> ForecastResult:
    """Point forecast + bootstrap intervals + per-factor attribution."""
    if not isinstance(loadings, pd.Series):
        raise TypeError("loadings must be a pandas Series")

    factor_vals = {
        name: float(ticker_factors.get(name, 0.0)) for name in loadings.index
    }
    x = np.array([factor_vals[n] for n in loadings.index], dtype=float)
    coef = loadings.to_numpy(dtype=float)
    point = intercept + float(np.dot(x, coef))

    contributions = {
        name: float(coef[i] * x[i]) for i, name in enumerate(loadings.index)
    }
    contributions["__intercept__"] = float(intercept)

    if len(residuals) >= 30 and n_bootstrap >= 1:
        rng = np.random.default_rng(rng_seed)
        sampled = rng.choice(residuals, size=n_bootstrap, replace=True)
        sim_returns = point + sampled
        lower_80 = float(np.percentile(sim_returns, 10.0))
        upper_80 = float(np.percentile(sim_returns, 90.0))
        lower_95 = float(np.percentile(sim_returns, 2.5))
        upper_95 = float(np.percentile(sim_returns, 97.5))
    else:
        lower_80 = upper_80 = lower_95 = upper_95 = point
        n_bootstrap = 0

    return ForecastResult(
        ticker=ticker,
        as_of_date=as_of_date,
        horizon_days=horizon_days,
        point_return=point,
        lower_80=lower_80,
        upper_80=upper_80,
        lower_95=lower_95,
        upper_95=upper_95,
        factor_contributions=contributions,
        n_train=int(len(residuals)),
        n_bootstrap=n_bootstrap,
    )
