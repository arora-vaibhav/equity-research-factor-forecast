"""Per-ticker walk-forward backtest -- measures forecast accuracy on real history.

The primary objective is forecast accuracy. I do deep iterative
backtesting for each ticker (60-day prior data, then check the +1 /
+30 / +60 day predictions against realized prices; when the call
misses, surface what the model lacked).

The benchmark target is the PMC9680880 LASSO-LSTM paper (71.6%-77.2%
directional accuracy on 1-day-ahead for AAPL/MSFT/BAC). Beating that
requires measuring our own directional accuracy first; this module does
that.

Methodology:
  * Walk-forward backtest with a configurable lookback window (default
    60 trading days).
  * At each step t, fit on [t - lookback, t-1] and predict t+1 / t+30 /
    t+60.
  * Realised price at t+1 / t+30 / t+60 is the ground truth.
  * Metrics per horizon:
      - MAE  (mean absolute error in % return units)
      - MAPE (mean absolute % error vs realized)
      - RMSE
      - Directional accuracy (sign(pred) == sign(realized))
      - Hit-rate on 80% CI (realized inside model band)
  * Returns per-ticker BacktestSummary so the report can show it.

Forecaster is plugged in via a callable (default = AR(1) historical
baseline matching the v2/v3 ensemble's AR(1) leg) so we can swap in a
volume-enriched forecaster later without rewriting this module.

Research provenance:
  * Walk-forward standard practice -- see "Advances in Financial Machine
    Learning" (Lopez de Prado 2018) ch.7 "Backtesting".
  * Directional accuracy is the benchmark vs PMC9680880.
  * 80% CI hit-rate matches Christoffersen 1998 unconditional-coverage
    test convention.

Pure function module; no DB.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd


WALK_FORWARD_VERSION = "1.0"


@dataclass
class HorizonAccuracy:
    """Accuracy stats for one prediction horizon."""
    horizon_days: int
    n_predictions: int
    mae_return: float
    rmse_return: float
    mape_return: float
    directional_accuracy: float
    ci80_hit_rate: float
    ci95_hit_rate: float
    mean_realized_return: float
    mean_predicted_return: float
    pearson_pred_vs_realized: float
    spearman_pred_vs_realized: float


@dataclass
class BacktestSummary:
    """Per-ticker walk-forward summary across all horizons."""
    ticker: str
    as_of_date: str
    lookback_days: int
    horizons: list[HorizonAccuracy] = field(default_factory=list)
    n_walks: int = 0
    walk_forward_version: str = WALK_FORWARD_VERSION


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman rank correlation; NaN-tolerant."""
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 3:
        return float("nan")
    xr = pd.Series(x[mask]).rank().to_numpy()
    yr = pd.Series(y[mask]).rank().to_numpy()
    if np.std(xr) < 1e-12 or np.std(yr) < 1e-12:
        return float("nan")
    return float(np.corrcoef(xr, yr)[0, 1])


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 3:
        return float("nan")
    if np.std(x[mask]) < 1e-12 or np.std(y[mask]) < 1e-12:
        return float("nan")
    return float(np.corrcoef(x[mask], y[mask])[0, 1])


def historical_baseline_forecast(
    close: pd.Series,
    lookback: int = 60,
    horizon_days: int = 30,
) -> tuple[float, float, float, float, float]:
    """A simple AR(1)-style baseline forecaster used as the default callable.

    Fits log-returns AR(1) on the last `min(lookback, available)` days
    of log-returns and projects forward `horizon_days`. Returns
    (point, lo80, hi80, lo95, hi95) all in decimal-return units.
    """
    if len(close) < 30:
        return 0.0, -0.02, 0.02, -0.04, 0.04
    log_ret = np.log(close / close.shift(1)).dropna()
    if len(log_ret) < 20:
        return 0.0, -0.02, 0.02, -0.04, 0.04
    effective_lookback = min(lookback, len(log_ret))
    window = log_ret.iloc[-effective_lookback:].to_numpy()

    x = window[:-1]
    y = window[1:]
    if np.std(x) < 1e-12:
        phi = 0.0
        const = float(np.mean(window))
    else:
        x_centered = x - x.mean()
        y_centered = y - y.mean()
        phi = float(np.sum(x_centered * y_centered) / np.sum(x_centered**2))
        const = float(y.mean() - phi * x.mean())

    last_r = float(window[-1])
    projected = []
    prev = last_r
    for _ in range(horizon_days):
        nxt = const + phi * prev
        projected.append(nxt)
        prev = nxt
    sum_log = float(np.sum(projected))
    point = float(np.exp(sum_log) - 1.0)

    sigma_step = float(np.std(window, ddof=1))
    sigma_h = sigma_step * np.sqrt(horizon_days)
    lo80 = float(np.exp(sum_log - 1.282 * sigma_h) - 1.0)
    hi80 = float(np.exp(sum_log + 1.282 * sigma_h) - 1.0)
    lo95 = float(np.exp(sum_log - 1.96 * sigma_h) - 1.0)
    hi95 = float(np.exp(sum_log + 1.96 * sigma_h) - 1.0)
    return point, lo80, hi80, lo95, hi95


ForecastFn = Callable[
    [pd.Series, int, int],
    tuple[float, float, float, float, float],
]


def _evaluate_horizon(
    close: pd.Series,
    *,
    horizon_days: int,
    lookback: int,
    forecast_fn: ForecastFn,
    step: int = 1,
    min_predictions: int = 5,
) -> Optional[HorizonAccuracy]:
    n = len(close)
    # Need lookback+1 closes to derive `lookback` log-returns, then
    # horizon_days more closes to compare against realised.
    if n < lookback + 1 + horizon_days + min_predictions:
        return None

    preds = []
    realized = []
    lo80s, hi80s = [], []
    lo95s, hi95s = [], []
    # Anchor t is the index where the forecast is issued.
    # Train window = closes [t - lookback - 1, t)  (lookback+1 closes
    # -> lookback log-returns). Forecast covers t-1 -> t-1 + horizon.
    for t in range(lookback + 1, n - horizon_days, step):
        train_close = close.iloc[t - lookback - 1:t]
        try:
            pt, lo80, hi80, lo95, hi95 = forecast_fn(
                train_close, lookback, horizon_days,
            )
        except Exception:
            continue
        p_t = float(close.iloc[t - 1])
        p_th = float(close.iloc[t - 1 + horizon_days])
        if p_t <= 0:
            continue
        r_real = (p_th / p_t) - 1.0
        preds.append(pt)
        realized.append(r_real)
        lo80s.append(lo80); hi80s.append(hi80)
        lo95s.append(lo95); hi95s.append(hi95)

    if len(preds) < min_predictions:
        return None

    preds_arr = np.asarray(preds, dtype=float)
    real_arr = np.asarray(realized, dtype=float)
    lo80_arr = np.asarray(lo80s, dtype=float)
    hi80_arr = np.asarray(hi80s, dtype=float)
    lo95_arr = np.asarray(lo95s, dtype=float)
    hi95_arr = np.asarray(hi95s, dtype=float)

    errs = preds_arr - real_arr
    mae = float(np.mean(np.abs(errs)))
    rmse = float(np.sqrt(np.mean(errs**2)))
    safe_real = np.where(np.abs(real_arr) > 1e-6, real_arr, np.nan)
    mape = float(np.nanmean(np.abs(errs / safe_real)))
    dir_acc = float(np.mean(np.sign(preds_arr) == np.sign(real_arr)))
    ci80_hit = float(np.mean((real_arr >= lo80_arr) & (real_arr <= hi80_arr)))
    ci95_hit = float(np.mean((real_arr >= lo95_arr) & (real_arr <= hi95_arr)))

    return HorizonAccuracy(
        horizon_days=horizon_days,
        n_predictions=int(len(preds_arr)),
        mae_return=mae,
        rmse_return=rmse,
        mape_return=mape,
        directional_accuracy=dir_acc,
        ci80_hit_rate=ci80_hit,
        ci95_hit_rate=ci95_hit,
        mean_realized_return=float(np.mean(real_arr)),
        mean_predicted_return=float(np.mean(preds_arr)),
        pearson_pred_vs_realized=_pearson(preds_arr, real_arr),
        spearman_pred_vs_realized=_spearman(preds_arr, real_arr),
    )


def run_walk_forward(
    *,
    ticker: str,
    as_of_date: str,
    close: pd.Series,
    lookback_days: int = 60,
    horizons: tuple[int, ...] = (1, 30, 60),
    forecast_fn: Optional[ForecastFn] = None,
    step: int = 1,
) -> BacktestSummary:
    """Run walk-forward backtest on a single ticker.

    `close` is a date-indexed price series (most recent last).
    `horizons` is the tuple of forward-looking horizons in trading days.

    Defaults: lookback=60, horizons=(1, 30, 60).
    Returns BacktestSummary with one HorizonAccuracy per horizon.
    """
    if forecast_fn is None:
        forecast_fn = historical_baseline_forecast

    summary = BacktestSummary(
        ticker=ticker, as_of_date=as_of_date,
        lookback_days=lookback_days,
    )
    n_walks_total = 0
    for h in horizons:
        ha = _evaluate_horizon(
            close, horizon_days=h, lookback=lookback_days,
            forecast_fn=forecast_fn, step=step,
        )
        if ha is not None:
            summary.horizons.append(ha)
            n_walks_total = max(n_walks_total, ha.n_predictions)
    summary.n_walks = n_walks_total
    return summary
