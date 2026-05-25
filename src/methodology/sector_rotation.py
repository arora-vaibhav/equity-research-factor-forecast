"""Sectoral rotation factor.

I added this because AI/semis have been a persistent regime driver: any
ticker in the basket tends to move with the basket regardless of single-
name fundamentals, so a sector overlay is necessary at every stage
(computation, forecasting, reporting, daily prediction).

What this module computes (per Stovall sector-rotation framework +
Asness-Moskowitz "Value and Momentum Everywhere" 2013 + practitioner
research):

  1. Sector-relative momentum (stock return - sector ETF) 5/20/60d.
  2. Sector momentum (sector ETF z-score vs SPY) 1m/3m/6m.
  3. Sector breadth (% of sector tickers above SMA50).
  4. Sector dispersion (cross-sectional 20d return std).
  5. AI-factor beta + spillover (rolling regression on AI basket excess).
  6. Sector rotation phase (cyclical/defensive/equal via XLY-XLP).
  7. Sector correlation regime (avg pairwise XL* corr; high = risk-off).

All free data via yfinance. Pure functions; no DB.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


SECTOR_ROTATION_VERSION = "1.0"

SECTOR_ETF_MAP: dict[str, str] = {
    "Technology":             "XLK",
    "Communication Services": "XLC",
    "Financial":              "XLF",
    "Financial Services":     "XLF",
    "Healthcare":             "XLV",
    "Health Care":            "XLV",
    "Industrials":            "XLI",
    "Consumer Cyclical":      "XLY",
    "Consumer Defensive":     "XLP",
    "Energy":                 "XLE",
    "Basic Materials":        "XLB",
    "Materials":              "XLB",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
}

AI_BASKET: list[str] = [
    "NVDA", "AMD", "AVGO", "GOOGL", "MSFT", "META",
    "TSM", "MU", "ORCL", "ARM", "PLTR", "SMCI",
]

CYCLICAL_ETFS: list[str] = ["XLY", "XLI", "XLF", "XLB", "XLK"]
DEFENSIVE_ETFS: list[str] = ["XLP", "XLU", "XLV", "XLRE"]


@dataclass
class SectorFeatures:
    ticker: str
    sector: Optional[str]
    sector_etf: Optional[str]
    rel_ret_5d:  float = float("nan")
    rel_ret_20d: float = float("nan")
    rel_ret_60d: float = float("nan")
    sector_mom_1m_z:  float = float("nan")
    sector_mom_3m_z:  float = float("nan")
    sector_mom_6m_z:  float = float("nan")
    ai_factor_beta_60d:  float = float("nan")
    ai_factor_excess:    float = float("nan")
    ai_spillover_score:  float = float("nan")
    sector_rotation_phase: str = "unknown"
    risk_off_corr_regime: float = float("nan")
    sector_breadth: float = float("nan")
    sector_dispersion_20d: float = float("nan")
    version: str = SECTOR_ROTATION_VERSION


def _log_ret(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1))


def _safe_align(*series: pd.Series) -> tuple[pd.Series, ...]:
    """Align several series on their common index. Names disambiguated
    to avoid duplicate-column issues on concat."""
    if not series:
        return tuple()
    # Rename to unique names to avoid concat collisions
    renamed = [s.rename(f"_s{i}") for i, s in enumerate(series)]
    aligned = pd.concat(renamed, axis=1, join="inner").dropna()
    return tuple(aligned.iloc[:, i] for i in range(len(series)))


def sector_etf_for(sector: Optional[str]) -> Optional[str]:
    return SECTOR_ETF_MAP.get(sector) if sector else None


def compute_relative_returns(
    stock_close: pd.Series, sector_close: pd.Series,
) -> dict[str, float]:
    if stock_close.empty or sector_close.empty:
        return {"rel_ret_5d": float("nan"), "rel_ret_20d": float("nan"),
                "rel_ret_60d": float("nan")}
    s_ret = _log_ret(stock_close).dropna()
    sec_ret = _log_ret(sector_close).dropna()
    s_ret, sec_ret = _safe_align(s_ret, sec_ret)
    if len(s_ret) < 5:
        return {"rel_ret_5d": float("nan"), "rel_ret_20d": float("nan"),
                "rel_ret_60d": float("nan")}
    out = {}
    for k, n in (("rel_ret_5d", 5), ("rel_ret_20d", 20), ("rel_ret_60d", 60)):
        if len(s_ret) >= n:
            out[k] = float(s_ret.iloc[-n:].sum() - sec_ret.iloc[-n:].sum())
        else:
            out[k] = float("nan")
    return out


def compute_sector_momentum_vs_spy(
    sector_close: pd.Series, spy_close: pd.Series,
) -> dict[str, float]:
    if sector_close.empty or spy_close.empty:
        return {"sector_mom_1m_z": float("nan"),
                "sector_mom_3m_z": float("nan"),
                "sector_mom_6m_z": float("nan")}
    sec_ret = _log_ret(sector_close).dropna()
    spy_ret = _log_ret(spy_close).dropna()
    sec_ret, spy_ret = _safe_align(sec_ret, spy_ret)
    excess = sec_ret - spy_ret
    out = {}
    for k, n in (("sector_mom_1m_z", 21), ("sector_mom_3m_z", 63), ("sector_mom_6m_z", 126)):
        if len(excess) < max(n, 252):
            out[k] = float("nan")
            continue
        roll = excess.rolling(n).sum()
        last = float(roll.iloc[-1])
        recent = roll.iloc[-252:].dropna()
        if len(recent) < 60 or recent.std() < 1e-12:
            out[k] = float("nan")
        else:
            out[k] = (last - float(recent.mean())) / float(recent.std())
    return out


def compute_ai_factor_beta(
    stock_close: pd.Series, ai_factor_close: pd.Series, spy_close: pd.Series,
    *, window: int = 60,
) -> dict[str, float]:
    if stock_close.empty or ai_factor_close.empty or spy_close.empty:
        return {"ai_factor_beta_60d": float("nan"),
                "ai_factor_excess": float("nan"),
                "ai_spillover_score": float("nan")}
    s_ret = _log_ret(stock_close).dropna()
    ai_ret = _log_ret(ai_factor_close).dropna()
    spy_ret = _log_ret(spy_close).dropna()
    s_ret, ai_ret, spy_ret = _safe_align(s_ret, ai_ret, spy_ret)
    if len(s_ret) < window:
        return {"ai_factor_beta_60d": float("nan"),
                "ai_factor_excess": float("nan"),
                "ai_spillover_score": float("nan")}
    ai_excess = ai_ret - spy_ret
    s_excess = s_ret - spy_ret
    x = ai_excess.iloc[-window:].to_numpy()
    y = s_excess.iloc[-window:].to_numpy()
    beta = 0.0 if np.std(x) < 1e-12 else float(np.cov(x, y, ddof=1)[0, 1] / np.var(x, ddof=1))
    ai_recent = float(ai_excess.iloc[-20:].sum())
    return {
        "ai_factor_beta_60d": beta,
        "ai_factor_excess":   float(ai_excess.iloc[-1]),
        "ai_spillover_score": beta * ai_recent,
    }


def classify_sector_rotation_phase(
    sector_etf_panel: pd.DataFrame, *, lookback: int = 63,
) -> str:
    if sector_etf_panel is None or sector_etf_panel.empty:
        return "unknown"
    log_rets = np.log(sector_etf_panel / sector_etf_panel.shift(1))
    recent = log_rets.iloc[-lookback:].sum()
    cyc_set = recent.reindex(CYCLICAL_ETFS).dropna()
    def_set = recent.reindex(DEFENSIVE_ETFS).dropna()
    if cyc_set.empty or def_set.empty:
        return "unknown"
    spread = float(cyc_set.mean()) - float(def_set.mean())
    if spread >  0.02: return "cyclical"
    if spread < -0.02: return "defensive"
    return "equal"


def compute_risk_off_corr_regime(
    sector_etf_panel: pd.DataFrame, *, lookback: int = 60,
) -> float:
    if sector_etf_panel is None or sector_etf_panel.empty:
        return float("nan")
    log_rets = np.log(sector_etf_panel / sector_etf_panel.shift(1))
    recent = log_rets.iloc[-lookback:].dropna(axis=1, thresh=lookback // 2)
    if recent.shape[1] < 3:
        return float("nan")
    corr = recent.corr()
    n = corr.shape[0]
    mask = np.triu(np.ones((n, n), dtype=bool), k=1)
    return float(corr.values[mask].mean())


def compute_sector_breadth(
    universe_close_panel: pd.DataFrame, *, sma: int = 50,
) -> dict[str, float]:
    if universe_close_panel is None or universe_close_panel.empty:
        return {"sector_breadth": float("nan")}
    if len(universe_close_panel) < sma + 5:
        return {"sector_breadth": float("nan")}
    sma_panel = universe_close_panel.rolling(sma).mean()
    above = (universe_close_panel > sma_panel).iloc[-1]
    return {"sector_breadth": float(above.mean())}


def compute_sector_dispersion(
    universe_close_panel: pd.DataFrame, *, window: int = 20,
) -> dict[str, float]:
    if universe_close_panel is None or universe_close_panel.empty:
        return {"sector_dispersion_20d": float("nan")}
    if len(universe_close_panel) < window + 5:
        return {"sector_dispersion_20d": float("nan")}
    rets = universe_close_panel.iloc[-window-1:].pct_change().iloc[1:].sum()
    return {"sector_dispersion_20d": float(rets.std(ddof=1))}


def build_ai_basket_close(ticker_close_panel: pd.DataFrame) -> pd.Series:
    """Equal-weighted normalized AI basket index (starts at 100)."""
    if ticker_close_panel is None or ticker_close_panel.empty:
        return pd.Series(dtype=float)
    cols = [c for c in AI_BASKET if c in ticker_close_panel.columns]
    if len(cols) < 3:
        return pd.Series(dtype=float)
    sub = ticker_close_panel[cols].dropna(how="all").ffill()
    sub = sub / sub.iloc[0]
    basket = sub.mean(axis=1) * 100.0
    basket.name = "ai_basket"
    return basket


def compute_sector_features(
    *,
    ticker: str,
    sector: Optional[str],
    stock_close: pd.Series,
    sector_etf_close: Optional[pd.Series] = None,
    spy_close: Optional[pd.Series] = None,
    ai_factor_close: Optional[pd.Series] = None,
    sector_etf_panel: Optional[pd.DataFrame] = None,
    universe_close_panel: Optional[pd.DataFrame] = None,
) -> SectorFeatures:
    """One-stop SectorFeatures snapshot. Pass whatever's available; the
    rest stays NaN. Inputs are date-indexed pandas Series / DataFrames."""
    sector_etf = sector_etf_for(sector)
    out = SectorFeatures(ticker=ticker, sector=sector, sector_etf=sector_etf)

    if sector_etf_close is not None and not sector_etf_close.empty:
        rel = compute_relative_returns(stock_close, sector_etf_close)
        out.rel_ret_5d = rel["rel_ret_5d"]
        out.rel_ret_20d = rel["rel_ret_20d"]
        out.rel_ret_60d = rel["rel_ret_60d"]

    if (sector_etf_close is not None and spy_close is not None
            and not sector_etf_close.empty and not spy_close.empty):
        mom = compute_sector_momentum_vs_spy(sector_etf_close, spy_close)
        out.sector_mom_1m_z = mom["sector_mom_1m_z"]
        out.sector_mom_3m_z = mom["sector_mom_3m_z"]
        out.sector_mom_6m_z = mom["sector_mom_6m_z"]

    if (ai_factor_close is not None and spy_close is not None
            and not ai_factor_close.empty and not spy_close.empty):
        ai = compute_ai_factor_beta(stock_close, ai_factor_close, spy_close)
        out.ai_factor_beta_60d = ai["ai_factor_beta_60d"]
        out.ai_factor_excess   = ai["ai_factor_excess"]
        out.ai_spillover_score = ai["ai_spillover_score"]

    if sector_etf_panel is not None and not sector_etf_panel.empty:
        out.sector_rotation_phase = classify_sector_rotation_phase(sector_etf_panel)
        out.risk_off_corr_regime  = compute_risk_off_corr_regime(sector_etf_panel)

    if universe_close_panel is not None and not universe_close_panel.empty:
        b = compute_sector_breadth(universe_close_panel)
        d = compute_sector_dispersion(universe_close_panel)
        out.sector_breadth        = b["sector_breadth"]
        out.sector_dispersion_20d = d["sector_dispersion_20d"]

    return out


def sector_feature_dict(sf: SectorFeatures) -> dict[str, float]:
    """Flatten SectorFeatures to a feature-dict for the ML matrix."""
    out = {
        "sector_rel_ret_5d":     sf.rel_ret_5d,
        "sector_rel_ret_20d":    sf.rel_ret_20d,
        "sector_rel_ret_60d":    sf.rel_ret_60d,
        "sector_mom_1m_z":       sf.sector_mom_1m_z,
        "sector_mom_3m_z":       sf.sector_mom_3m_z,
        "sector_mom_6m_z":       sf.sector_mom_6m_z,
        "ai_factor_beta_60d":    sf.ai_factor_beta_60d,
        "ai_factor_excess":      sf.ai_factor_excess,
        "ai_spillover_score":    sf.ai_spillover_score,
        "risk_off_corr_regime":  sf.risk_off_corr_regime,
        "sector_breadth":        sf.sector_breadth,
        "sector_dispersion_20d": sf.sector_dispersion_20d,
    }
    for p in ("cyclical", "defensive", "equal"):
        out[f"sector_phase_{p}"] = 1.0 if sf.sector_rotation_phase == p else 0.0
    return out
