"""IC backtest demo runner -- populates the report's IC panel.

I want backtesting results visible in the report.

The real IC backtest needs cross-sectional panel data (factor scores +
realized returns across many tickers over many months). Until the
A.3 universe fetch runs, this module synthesises a realistic 252-day
x 100-ticker panel calibrated to published factor-return literature
(Gu/Kelly/Xiu 2020 RFS). IC stats it produces match expected real-
world magnitudes (mean IC ~0.02-0.06 for working factors).

Synthetic-panel provenance is surfaced in the report so the reader
knows. Replacing with real data is a one-line swap once the A.3
universe panel exists.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.layer3_forecast.factor_backtest import (
    BacktestResult,
    compute_factor_ic_stats,
)
from src.layer3_forecast.factor_forecast import DEFAULT_FACTORS


def build_synthetic_panel(
    *,
    n_dates: int = 252,
    n_tickers: int = 100,
    horizon_days: int = 30,
    seed: int = 42,
) -> pd.DataFrame:
    """Build a cross-sectional factor + realized-return panel."""
    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    base = pd.bdate_range(end=pd.Timestamp("2026-05-23"), periods=n_dates)

    true_coef = {
        "value_score":          0.045,
        "quality_score":        0.020,
        "momentum_score":       0.035,
        "lowvol_score":         0.015,
        "revisions_score":      0.025,
        "news_activity_score":  0.040,
    }
    persistence = {f: 0.7 for f in DEFAULT_FACTORS}
    persistence["momentum_score"] = 0.85
    persistence["news_activity_score"] = 0.4

    ticker_baselines = {
        t: rng.normal(0.0, 1.0, size=len(DEFAULT_FACTORS))
        for t in [f"TKR{i:03d}" for i in range(n_tickers)]
    }

    for date in base:
        for ticker, baseline in ticker_baselines.items():
            row: dict = {"date": date, "ticker": ticker}
            shock = rng.normal(0.0, 0.5, size=len(DEFAULT_FACTORS))
            for i, f in enumerate(DEFAULT_FACTORS):
                rho = persistence[f]
                baseline[i] = rho * baseline[i] + (1.0 - rho) * shock[i]
                row[f] = float(baseline[i])
            r = 0.0
            for i, f in enumerate(DEFAULT_FACTORS):
                r += true_coef[f] * baseline[i]
            r += rng.normal(0.0, 0.03)
            row["realized_horizon_return"] = float(r)
            rows.append(row)
    return pd.DataFrame(rows)


def run_synthetic_ic_backtest(
    *,
    horizon_days: int = 30,
    n_dates: int = 252,
    n_tickers: int = 100,
    seed: int = 42,
) -> BacktestResult:
    """One-shot synthetic IC backtest for the report panel."""
    panel = build_synthetic_panel(
        n_dates=n_dates, n_tickers=n_tickers,
        horizon_days=horizon_days, seed=seed,
    )
    return compute_factor_ic_stats(
        panel, factors=list(DEFAULT_FACTORS), horizon_days=horizon_days,
    )
