"""Macro / industry / sector context for the forecast report.

I added this so the forecast does not see only single-stock factors:
industry and macro context also condition the signal.

Pulls four macro proxies via yfinance:
  * ^TNX  -- 10-year Treasury yield
  * ^IRX  -- 13-week Treasury (3-month)
  * ^VIX  -- volatility index
  * sector ETF (XLK / XLY / XLP / ...) by sector string

Computes:
  * yield_curve_slope = TNX - IRX
  * vix_level + vix_z_score over trailing 252d
  * sector_relative_return over trailing 90d
  * macro_regime: 4-state categorical
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd


MACRO_FACTORS_VERSION = "1.0"

SECTOR_ETF_BY_SECTOR: dict[str, str] = {
    "Technology":           "XLK",
    "Consumer Cyclical":    "XLY",
    "Consumer Defensive":   "XLP",
    "Industrials":          "XLI",
    "Energy":               "XLE",
    "Financial Services":   "XLF",
    "Utilities":            "XLU",
    "Healthcare":           "XLV",
    "Basic Materials":      "XLB",
    "Real Estate":          "XLRE",
    "Communication Services": "XLC",
}


@dataclass
class MacroContext:
    as_of_date: str
    tnx_yield: float
    irx_yield: float
    yield_curve_slope: float
    vix_level: float
    vix_z_score_252d: float
    sector_etf: Optional[str]
    sector_relative_return_90d: float
    macro_regime: str
    macro_factors_version: str = MACRO_FACTORS_VERSION


def _yfinance_history(
    symbol: str,
    period: str = "1y",
    *,
    ticker_factory: Optional[Callable] = None,
) -> pd.DataFrame:
    try:
        if ticker_factory is None:
            import yfinance
            ticker_factory = yfinance.Ticker
        df = ticker_factory(symbol).history(period=period)
        if df is None or df.empty:
            return pd.DataFrame()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        return df
    except Exception:
        return pd.DataFrame()


def _last_value(series: pd.Series) -> float:
    s = series.dropna()
    if len(s) == 0:
        return float("nan")
    return float(s.iloc[-1])


def _vix_z(vix_series: pd.Series) -> tuple[float, float]:
    s = vix_series.dropna()
    if len(s) < 30:
        return (float("nan"), float("nan"))
    last = float(s.iloc[-1])
    window = s.iloc[-252:] if len(s) >= 30 else s
    mu = float(window.mean())
    sd = float(window.std(ddof=1))
    z = (last - mu) / sd if sd > 1e-9 else 0.0
    return (last, z)


def _classify_regime(slope: float, vix_z: float, vix_level: float) -> str:
    if not math.isfinite(slope) or not math.isfinite(vix_z):
        return "neutral"
    if vix_z > 1.0 or slope < 0.0:
        return "risk_off"
    if slope > 0.5 and vix_z < 0.0:
        return "risk_on_growth"
    if slope > 0.0 and vix_z < 0.0:
        return "risk_on_late"
    return "neutral"


def fetch_macro_context(
    ticker_sector: Optional[str],
    ticker_close: pd.Series,
    *,
    as_of_date: Optional[str] = None,
    ticker_factory: Optional[Callable] = None,
) -> MacroContext:
    """Pull macro proxies + compute regime tag."""
    as_of_date = as_of_date or _dt.date.today().isoformat()

    tnx_df = _yfinance_history("^TNX", ticker_factory=ticker_factory)
    irx_df = _yfinance_history("^IRX", ticker_factory=ticker_factory)
    vix_df = _yfinance_history("^VIX", ticker_factory=ticker_factory)

    tnx_yield = (
        _last_value(tnx_df["Close"]) if "Close" in tnx_df.columns else float("nan")
    )
    irx_yield = (
        _last_value(irx_df["Close"]) if "Close" in irx_df.columns else float("nan")
    )
    vix_level, vix_z = (
        _vix_z(vix_df["Close"]) if "Close" in vix_df.columns else (float("nan"), float("nan"))
    )

    slope = (
        tnx_yield - irx_yield
        if math.isfinite(tnx_yield) and math.isfinite(irx_yield)
        else float("nan")
    )

    sector_etf = SECTOR_ETF_BY_SECTOR.get(ticker_sector or "", None)
    sector_relative_ret = float("nan")
    if sector_etf is not None:
        sec_df = _yfinance_history(sector_etf, ticker_factory=ticker_factory)
        if "Close" in sec_df.columns and len(sec_df) > 0 and len(ticker_close) > 0:
            ticker_window = ticker_close.iloc[-90:] if len(ticker_close) >= 90 else ticker_close
            sec_window = sec_df["Close"].iloc[-90:] if len(sec_df) >= 90 else sec_df["Close"]
            if len(ticker_window) >= 2 and len(sec_window) >= 2:
                t_ret = (float(ticker_window.iloc[-1]) / float(ticker_window.iloc[0])) - 1.0
                s_ret = (float(sec_window.iloc[-1]) / float(sec_window.iloc[0])) - 1.0
                sector_relative_ret = t_ret - s_ret

    regime = _classify_regime(slope, vix_z, vix_level)

    return MacroContext(
        as_of_date=as_of_date,
        tnx_yield=tnx_yield,
        irx_yield=irx_yield,
        yield_curve_slope=slope,
        vix_level=vix_level,
        vix_z_score_252d=vix_z,
        sector_etf=sector_etf,
        sector_relative_return_90d=sector_relative_ret,
        macro_regime=regime,
    )
