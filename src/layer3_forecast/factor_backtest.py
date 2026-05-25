"""Factor backtest -- per-factor Information Coefficient + attribution P&L.

I want a way to backtest existing data factors and assess how much
contribution each one had over a given time frame.

Methodology (Grinold-Kahn 2000 ch.4):

  IC_t(f) = Spearman_rank_corr(factor_f at time t,
                               realized_return at t+h across the
                               cross-section)

  Cumulative attribution P&L for factor f over a period:
      sum_t mean_cross_section( (factor_f_t - mean_f_t) *
                                (return_{t+h} - mean_return_{t+h}) )

A factor with consistently positive IC is producing alpha; ~0 = no
skill; negative = sign-flipped.

Pure functions; no DB, no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


FACTOR_BACKTEST_VERSION = "1.0"


@dataclass
class FactorICStats:
    factor: str
    mean_ic: float
    std_ic: float
    icir: float
    n_periods: int
    fraction_positive: float
    cumulative_attribution: float


@dataclass
class BacktestResult:
    horizon_days: int
    per_factor: dict[str, FactorICStats] = field(default_factory=dict)
    ic_series: pd.DataFrame = field(default_factory=pd.DataFrame)
    backtest_version: str = FACTOR_BACKTEST_VERSION


def _spearman_corr(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman rank correlation; NaN-tolerant."""
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 3:
        return float("nan")
    xr = pd.Series(x[mask]).rank().to_numpy()
    yr = pd.Series(y[mask]).rank().to_numpy()
    if np.std(xr) < 1e-12 or np.std(yr) < 1e-12:
        return float("nan")
    return float(np.corrcoef(xr, yr)[0, 1])


def compute_factor_ic_series(
    panel: pd.DataFrame,
    *,
    factors: list[str],
    horizon_days: int,
) -> pd.DataFrame:
    """Compute IC per (date, factor) on a long panel.

    `panel` columns: 'date', 'ticker', each factor, 'realized_horizon_return'.
    Returns DataFrame indexed by date with one column per factor.
    """
    if "date" not in panel.columns:
        raise ValueError("panel must have a 'date' column")
    if "realized_horizon_return" not in panel.columns:
        raise ValueError("panel must have a 'realized_horizon_return' column")
    missing = [f for f in factors if f not in panel.columns]
    if missing:
        raise ValueError(f"panel missing factor columns: {missing}")

    panel = panel.dropna(subset=["realized_horizon_return"]).copy()
    panel["date"] = pd.to_datetime(panel["date"])
    out_rows: list[dict] = []
    for date, sub in panel.groupby("date"):
        if len(sub) < 5:
            continue
        row: dict = {"date": date}
        for f in factors:
            row[f] = _spearman_corr(
                sub[f].to_numpy(dtype=float),
                sub["realized_horizon_return"].to_numpy(dtype=float),
            )
        out_rows.append(row)
    if not out_rows:
        return pd.DataFrame(columns=["date"] + factors)
    df = pd.DataFrame(out_rows).set_index("date").sort_index()
    return df


def compute_factor_ic_stats(
    panel: pd.DataFrame,
    *,
    factors: list[str],
    horizon_days: int,
) -> BacktestResult:
    """Roll up IC series into per-factor stats."""
    ic_df = compute_factor_ic_series(
        panel, factors=factors, horizon_days=horizon_days,
    )
    per_factor: dict[str, FactorICStats] = {}
    if ic_df.empty:
        for f in factors:
            per_factor[f] = FactorICStats(
                factor=f, mean_ic=float("nan"), std_ic=float("nan"),
                icir=float("nan"), n_periods=0,
                fraction_positive=float("nan"),
                cumulative_attribution=0.0,
            )
        return BacktestResult(
            horizon_days=horizon_days, per_factor=per_factor, ic_series=ic_df,
        )

    for f in factors:
        s = ic_df[f].dropna()
        if len(s) < 2:
            per_factor[f] = FactorICStats(
                factor=f, mean_ic=float("nan"), std_ic=float("nan"),
                icir=float("nan"), n_periods=len(s),
                fraction_positive=float("nan"),
                cumulative_attribution=0.0,
            )
            continue
        mean_ic = float(s.mean())
        std_ic = float(s.std(ddof=1)) if len(s) > 1 else float("nan")
        icir = (
            mean_ic / std_ic
            if (std_ic and math.isfinite(std_ic) and std_ic > 0)
            else float("nan")
        )
        frac_pos = float((s > 0).mean())
        attrib_terms = []
        panel_local = panel.dropna(subset=["realized_horizon_return"])
        panel_local = panel_local[panel_local[f].notna()]
        for date, sub in panel_local.groupby("date"):
            if len(sub) < 5:
                continue
            f_dev = sub[f] - sub[f].mean()
            r_dev = sub["realized_horizon_return"] - sub["realized_horizon_return"].mean()
            attrib_terms.append(float((f_dev * r_dev).mean()))
        cumulative = float(np.nansum(attrib_terms)) if attrib_terms else 0.0
        per_factor[f] = FactorICStats(
            factor=f, mean_ic=mean_ic, std_ic=std_ic, icir=icir,
            n_periods=len(s), fraction_positive=frac_pos,
            cumulative_attribution=cumulative,
        )

    return BacktestResult(
        horizon_days=horizon_days, per_factor=per_factor, ic_series=ic_df,
    )
