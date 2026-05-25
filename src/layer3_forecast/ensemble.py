"""Multi-method forecast ensemble.

Combines four forecast methods per v2 plan:
  (a) Linear multi-factor regression (F.1 factor_forecast)
  (b) GBM Monte Carlo with Yang-Zhang vol
  (c) AR(1) momentum baseline
  (d) Random-walk baseline (humility line)

Ensemble weights default to 1/N. When `weight_history` provides past
per-method realized errors, weights become inverse-MAE.

Pure function; no DB, no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


ENSEMBLE_VERSION = "1.0"


@dataclass
class MethodForecast:
    method: str  # 'linear', 'monte_carlo', 'ar1', 'random_walk'
    point_return: float
    lower_80: float
    upper_80: float
    lower_95: float
    upper_95: float
    weight: float = 1.0
    extras: dict = field(default_factory=dict)


@dataclass
class EnsembleResult:
    ticker: str
    horizon_days: int
    methods: list[MethodForecast]
    ensemble_point: float
    ensemble_lower_80: float
    ensemble_upper_80: float
    ensemble_lower_95: float
    ensemble_upper_95: float
    method_dispersion: float
    beats_random_walk: bool
    ensemble_version: str = ENSEMBLE_VERSION


def ar1_forecast(
    close: pd.Series,
    *,
    horizon_days: int,
    n_train: int = 252,
) -> MethodForecast:
    """AR(1) on log-returns."""
    log_ret = np.log(close / close.shift(1)).dropna()
    if len(log_ret) < 30:
        return MethodForecast(
            method="ar1", point_return=0.0,
            lower_80=0.0, upper_80=0.0,
            lower_95=0.0, upper_95=0.0,
            extras={"reason": "insufficient_history"},
        )
    y = log_ret.iloc[-n_train:].to_numpy()
    y_lag = y[:-1]
    y_now = y[1:]
    if len(y_lag) < 10 or float(np.var(y_lag)) < 1e-12:
        phi = 0.0
        resid_std = float(np.std(y))
    else:
        phi = float(np.dot(y_lag, y_now) / np.dot(y_lag, y_lag))
        resid = y_now - phi * y_lag
        resid_std = float(np.std(resid, ddof=1))
    y_last = float(y[-1])
    if abs(phi) < 1.0 - 1e-9:
        cum_log = y_last * phi * (1.0 - phi ** horizon_days) / (1.0 - phi)
    else:
        cum_log = y_last * horizon_days
    if abs(phi) < 1.0 - 1e-9:
        var_cum = (resid_std ** 2) * (1.0 - phi ** (2 * horizon_days)) / (1.0 - phi ** 2)
    else:
        var_cum = (resid_std ** 2) * horizon_days
    std_cum = math.sqrt(max(var_cum, 1e-12))
    point = math.exp(cum_log) - 1.0
    lower_80 = math.exp(cum_log - 1.2816 * std_cum) - 1.0
    upper_80 = math.exp(cum_log + 1.2816 * std_cum) - 1.0
    lower_95 = math.exp(cum_log - 1.96 * std_cum) - 1.0
    upper_95 = math.exp(cum_log + 1.96 * std_cum) - 1.0
    return MethodForecast(
        method="ar1", point_return=point,
        lower_80=lower_80, upper_80=upper_80,
        lower_95=lower_95, upper_95=upper_95,
        extras={"phi": phi, "resid_std": resid_std},
    )


def random_walk_forecast(
    close: pd.Series,
    *,
    horizon_days: int,
) -> MethodForecast:
    """Random-walk baseline: point return = 0; intervals from stdev * sqrt(h)."""
    log_ret = np.log(close / close.shift(1)).dropna()
    if len(log_ret) < 5:
        return MethodForecast(
            method="random_walk", point_return=0.0,
            lower_80=0.0, upper_80=0.0,
            lower_95=0.0, upper_95=0.0,
        )
    sigma_daily = float(log_ret.iloc[-252:].std(ddof=1))
    std_cum = sigma_daily * math.sqrt(horizon_days)
    lower_80 = math.exp(-1.2816 * std_cum) - 1.0
    upper_80 = math.exp(+1.2816 * std_cum) - 1.0
    lower_95 = math.exp(-1.96 * std_cum) - 1.0
    upper_95 = math.exp(+1.96 * std_cum) - 1.0
    return MethodForecast(
        method="random_walk", point_return=0.0,
        lower_80=lower_80, upper_80=upper_80,
        lower_95=lower_95, upper_95=upper_95,
        extras={"sigma_daily": sigma_daily},
    )


def combine_ensemble(
    ticker: str,
    horizon_days: int,
    methods: list[MethodForecast],
    *,
    weight_history: Optional[dict[str, float]] = None,
) -> EnsembleResult:
    """Combine method forecasts with optional inverse-MAE weights."""
    if not methods:
        raise ValueError("methods is empty")

    if weight_history:
        eps = 1e-4
        maes = []
        for m in methods:
            mae = weight_history.get(m.method)
            if mae is None or not math.isfinite(mae):
                mae = float("inf")
            maes.append(mae)
        finite_maes = [v for v in maes if math.isfinite(v)]
        median_mae = (
            float(np.median(finite_maes)) if finite_maes else 1.0
        )
        raw_w = []
        for mae in maes:
            if not math.isfinite(mae):
                mae = median_mae
            raw_w.append(1.0 / (mae + eps))
        wsum = sum(raw_w)
        weights = [w / wsum for w in raw_w]
    else:
        weights = [1.0 / len(methods)] * len(methods)

    for m, w in zip(methods, weights):
        m.weight = w

    points = np.array([m.point_return for m in methods])
    lo80 = np.array([m.lower_80 for m in methods])
    hi80 = np.array([m.upper_80 for m in methods])
    lo95 = np.array([m.lower_95 for m in methods])
    hi95 = np.array([m.upper_95 for m in methods])
    w = np.array(weights)

    ens_point = float(np.dot(w, points))
    ens_lo80 = float(np.dot(w, lo80))
    ens_hi80 = float(np.dot(w, hi80))
    ens_lo95 = float(np.dot(w, lo95))
    ens_hi95 = float(np.dot(w, hi95))

    dispersion = float(np.std(points, ddof=0))
    rw = next((m for m in methods if m.method == "random_walk"), None)
    if rw is not None:
        beats_rw = abs(ens_point - rw.point_return) > 0.005
    else:
        beats_rw = True

    return EnsembleResult(
        ticker=ticker,
        horizon_days=horizon_days,
        methods=methods,
        ensemble_point=ens_point,
        ensemble_lower_80=ens_lo80,
        ensemble_upper_80=ens_hi80,
        ensemble_lower_95=ens_lo95,
        ensemble_upper_95=ens_hi95,
        method_dispersion=dispersion,
        beats_random_walk=beats_rw,
    )
