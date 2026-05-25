"""Geometric Brownian Motion Monte Carlo forecaster.

Per research from huseinzol05/Stock-Prediction-Models (drift-MC +
dynamic-vol-MC are standard baselines).

Drift comes from the linear factor model (point_return / horizon_days
annualised). Volatility comes from Yang-Zhang (A.3.10) over the
trailing 60d, with a dynamic-vol option (EWMA on log returns).

Pure function -- no DB, no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


MONTE_CARLO_VERSION = "1.0"
DEFAULT_N_PATHS = 1000
TRADING_DAYS_PER_YEAR = 252


@dataclass
class MonteCarloResult:
    """Monte Carlo simulation outcome."""
    ticker: str
    horizon_days: int
    n_paths: int
    drift_annual: float
    sigma_annual: float
    point_return: float
    lower_80: float
    upper_80: float
    lower_95: float
    upper_95: float
    bull_scenario_return: float  # 90th percentile
    base_scenario_return: float  # 50th
    bear_scenario_return: float  # 10th
    end_price_paths: np.ndarray = field(default_factory=lambda: np.array([]))
    monte_carlo_version: str = MONTE_CARLO_VERSION


def estimate_drift_from_factors(
    point_return_horizon: float,
    horizon_days: int,
) -> float:
    """Annualise a horizon-return into a GBM drift parameter."""
    if horizon_days <= 0:
        return 0.0
    horizon_years = horizon_days / TRADING_DAYS_PER_YEAR
    r = max(-0.99, point_return_horizon)
    return math.log(1.0 + r) / max(horizon_years, 1e-6)


def estimate_vol_from_history(
    close: pd.Series,
    *,
    method: str = "yang_zhang",
    ohlc: Optional[pd.DataFrame] = None,
    window_days: int = 60,
) -> float:
    """Annualised vol estimate.

    method options:
      * 'yang_zhang' -- preferred when OHLC available (A.3.10).
      * 'close_to_close' -- log-return stdev * sqrt(252).
      * 'ewma' -- RiskMetrics EWMA, lambda=0.94.
    """
    if method == "yang_zhang" and ohlc is not None:
        try:
            from src.methodology.yang_zhang_vol import yang_zhang_vol
            yz = yang_zhang_vol(ohlc, window_days=window_days)
            last = yz.dropna()
            if len(last) > 0:
                return float(last.iloc[-1])
        except Exception:
            pass
    log_ret = np.log(close / close.shift(1)).dropna()
    if len(log_ret) < 5:
        return 0.20
    if method == "ewma":
        lam = 0.94
        ewma = log_ret.ewm(alpha=1.0 - lam, adjust=False).var().iloc[-1]
        sigma_daily = math.sqrt(max(ewma, 1e-12))
    else:
        sigma_daily = float(log_ret.iloc[-window_days:].std(ddof=1))
    return sigma_daily * math.sqrt(TRADING_DAYS_PER_YEAR)


def simulate_gbm(
    ticker: str,
    *,
    horizon_days: int,
    drift_annual: float,
    sigma_annual: float,
    n_paths: int = DEFAULT_N_PATHS,
    rng_seed: int = 42,
) -> MonteCarloResult:
    """Run GBM MC; summarise via percentiles."""
    if horizon_days <= 0:
        raise ValueError("horizon_days must be >= 1")
    rng = np.random.default_rng(rng_seed)
    horizon_years = horizon_days / TRADING_DAYS_PER_YEAR
    Z = rng.standard_normal(n_paths)
    log_terminal = (
        (drift_annual - 0.5 * sigma_annual ** 2) * horizon_years
        + sigma_annual * math.sqrt(horizon_years) * Z
    )
    terminal_returns = np.exp(log_terminal) - 1.0

    return MonteCarloResult(
        ticker=ticker,
        horizon_days=horizon_days,
        n_paths=n_paths,
        drift_annual=drift_annual,
        sigma_annual=sigma_annual,
        point_return=float(np.median(terminal_returns)),
        lower_80=float(np.percentile(terminal_returns, 10.0)),
        upper_80=float(np.percentile(terminal_returns, 90.0)),
        lower_95=float(np.percentile(terminal_returns, 2.5)),
        upper_95=float(np.percentile(terminal_returns, 97.5)),
        bull_scenario_return=float(np.percentile(terminal_returns, 90.0)),
        base_scenario_return=float(np.percentile(terminal_returns, 50.0)),
        bear_scenario_return=float(np.percentile(terminal_returns, 10.0)),
        end_price_paths=terminal_returns,
    )


def simulate_gbm_paths(
    *,
    horizon_days: int,
    drift_annual: float,
    sigma_annual: float,
    n_paths: int = 200,
    rng_seed: int = 42,
) -> np.ndarray:
    """Simulate full daily paths for the fan plot.

    Returns a (n_paths, horizon_days+1) array of price multipliers
    starting at 1.0. Multiply by last_price to get absolute prices.
    """
    if horizon_days <= 0:
        raise ValueError("horizon_days must be >= 1")
    rng = np.random.default_rng(rng_seed)
    dt = 1.0 / TRADING_DAYS_PER_YEAR
    drift_step = (drift_annual - 0.5 * sigma_annual ** 2) * dt
    diffusion_step = sigma_annual * math.sqrt(dt)
    Z = rng.standard_normal((n_paths, horizon_days))
    log_steps = drift_step + diffusion_step * Z
    cum_log = np.cumsum(log_steps, axis=1)
    paths = np.exp(np.column_stack([np.zeros(n_paths), cum_log]))
    return paths
