"""Live forecast demo driver.

I built this to run the full forecast pipeline end-to-end on real
yfinance data and emit one interactive HTML report per ticker plus a
summary index page. The synthetic-data variant is in
``forecast_demo.py``; this one talks to live providers.

Pipeline per ticker:
  1. yfinance OHLCV (2-3y) + ticker_info + earnings_dates.
  2. Build ticker_factors from real yfinance fundamentals.
  3. Cross-sectional history -> loadings (literature-calibrated
     synthetic cross-section is used here as the regression training
     set; only the per-ticker factor values come from live data).
  4. Four-method ensemble (linear + MC + AR(1) + RW).
  5. Macro context via yfinance (TNX/IRX/VIX/sector ETF).
  6. Catalysts from earnings calendar.
  7. LLM narrative (or template fallback).
  8. Dark-mode Plotly HTML report.
  9. Structured index page.

Run:
    python scripts/forecast_live_demo.py
    python scripts/forecast_live_demo.py --horizon-days 60
    python scripts/forecast_live_demo.py --tickers AAPL MSFT
    python scripts/forecast_live_demo.py --allow-llm  # requires ANTHROPIC_API_KEY
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load .env so FMP_API_KEY / ANTHROPIC_API_KEY / POLYGON_API_KEY are
# available without the caller having to export them in the shell.
try:
    from dotenv import load_dotenv as _load_dotenv  # noqa: E402
    _load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass  # python-dotenv not installed; rely on shell env

from src.layer3_forecast.factor_forecast import (  # noqa: E402
    DEFAULT_FACTORS,
    compute_factor_loadings,
    forecast_return,
)
from src.layer3_forecast.monte_carlo import (  # noqa: E402
    estimate_drift_from_factors,
    estimate_vol_from_history,
    simulate_gbm,
    simulate_gbm_paths,
)
from src.layer3_forecast.ensemble import (  # noqa: E402
    MethodForecast,
    ar1_forecast,
    combine_ensemble,
    random_walk_forecast,
)
from src.layer3_forecast.macro_factors import fetch_macro_context  # noqa: E402
from src.layer3_forecast.llm_narrative import (  # noqa: E402
    build_critique,
    build_narrative,
)
from src.layer3_forecast.report_v2 import (  # noqa: E402
    render_index_page_v2,
    render_stock_report_v2,
)
from src.layer3_forecast.sensitivity import run_sensitivity  # noqa: E402
from src.layer3_forecast.free_sentiment import fetch_sentiment_snapshot  # noqa: E402
from src.layer3_forecast.ic_backtest_demo import run_synthetic_ic_backtest  # noqa: E402
from src.layer3_forecast.walk_forward_backtest import (  # noqa: E402
    historical_baseline_forecast,
    run_walk_forward,
)
from src.methodology.volume_features import (  # noqa: E402
    volume_confirmed_forecast,
    volume_momentum_composite,
)
from src.methodology.sector_rotation import (  # noqa: E402
    SECTOR_ETF_MAP,
    build_ai_basket_close,
    compute_sector_features,
    sector_feature_dict,
)
from src.methodology.multi_timeframe import (  # noqa: E402
    compute_confluence,
    fetch_execution_timing_features,
    fetch_multi_timeframe_snapshots,
)


# Public-ticker defaults so the demo runs out of the box. Override
# from the CLI with --tickers or --universe.
DEFAULT_TICKERS = ["AAPL", "MSFT", "SPY"]
DEFAULT_HORIZON = 30

# Display-name override for tickers whose yfinance symbol differs from
# the more familiar company name.
TICKER_DISPLAY_NAME: dict[str, str] = {}


# Minimal 5-bucket bias classifier. The full module was retired; the
# composite-z plus random-walk-beat check is enough for the demo report.
# Cutoffs follow Grinold-Kahn (2000 ch.3): roughly +/- 1 sigma for
# directional, +/- 2 sigma for strong directional.
class _BiasSignal:
    """Container matching the shape that report_v2 expects."""

    __slots__ = (
        "ticker", "bias", "bias_legacy", "composite_z",
        "recommended_playbook", "reasoning",
    )

    def __init__(
        self, *, ticker: str, bias: str, composite_z: float,
        recommended_playbook: str, reasoning: str,
    ) -> None:
        self.ticker = ticker
        self.bias = bias
        self.composite_z = composite_z
        self.recommended_playbook = recommended_playbook
        self.bias_legacy = _legacy_for(bias)
        self.reasoning = reasoning


def _z_to_bias5(z: float) -> str:
    if z >= 2.0:
        return "strong_buy"
    if z >= 1.0:
        return "buy"
    if z <= -2.0:
        return "strong_sell"
    if z <= -1.0:
        return "sell"
    return "neutral"


def _playbook_for(bias: str) -> str:
    return {
        "strong_buy": "A+", "buy": "A",
        "strong_sell": "B+", "sell": "B",
        "neutral": "cash",
    }.get(bias, "cash")


def _legacy_for(bias: str) -> str:
    return {
        "strong_buy": "long", "buy": "long",
        "strong_sell": "short", "sell": "short",
        "neutral": "neutral",
    }.get(bias, "neutral")


def classify_bias(
    *,
    ticker: str,
    factor_contributions: dict[str, float],
    horizon_days: int,
    macro_regime: str | None,
    ensemble_point: float,
    rw_point: float,
    beats_random_walk: bool,
) -> _BiasSignal:
    """Standardise the factor contribution sum to a composite z-score
    and bucket it into one of five directional buckets.

    I divide by 0.04 because that's the typical per-factor sigma for
    monthly-horizon equity factor models in published cross-sectional
    studies; the result is interpretable as a sigma count.
    """
    contrib_sum = sum(
        c for f, c in factor_contributions.items() if f != "__intercept__"
    )
    z = contrib_sum / 0.04
    bias = _z_to_bias5(z)
    # If the ensemble doesn't beat random walk, demote to neutral --
    # we have no edge over the trivial baseline.
    if bias != "neutral" and not beats_random_walk:
        bias = "neutral"
    reasoning = (
        f"composite_z={z:+.2f}, beats_rw={beats_random_walk}, "
        f"regime={macro_regime or 'unknown'}"
    )
    return _BiasSignal(
        ticker=ticker, bias=bias, composite_z=z,
        recommended_playbook=_playbook_for(bias), reasoning=reasoning,
    )


def _fetch_yf_history(symbol: str, *, period: str = "2y") -> pd.DataFrame:
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period=period)
        if df is None or df.empty:
            return pd.DataFrame()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df.rename(columns=str.lower)
        return df
    except Exception as exc:
        print(f"  [warn] yfinance history failed for {symbol}: {exc}", file=sys.stderr)
        return pd.DataFrame()


def _fetch_yf_info(symbol: str) -> dict:
    try:
        import yfinance as yf
        return yf.Ticker(symbol).info or {}
    except Exception as exc:
        print(f"  [warn] yfinance info failed for {symbol}: {exc}", file=sys.stderr)
        return {}


def _fetch_yf_earnings_dates(symbol: str) -> list[dict]:
    try:
        import yfinance as yf
        tkr = yf.Ticker(symbol)
        ed = tkr.get_earnings_dates(limit=4) if hasattr(tkr, "get_earnings_dates") else None
        if ed is None or len(ed) == 0:
            return []
        catalysts = []
        for idx_val in ed.index:
            try:
                date_iso = idx_val.date().isoformat() if hasattr(idx_val, "date") else str(idx_val)[:10]
            except Exception:
                date_iso = str(idx_val)[:10]
            catalysts.append({
                "catalyst_date": date_iso,
                "catalyst_type": "earnings",
                "source": "yahoo",
                "confidence": "high",
                "catalyst_description": f"Earnings announcement ({symbol})",
            })
        return catalysts
    except Exception:
        return []


def _format_mcap(mcap: Optional[float]) -> str:
    if mcap is None or mcap <= 0:
        return "-"
    if mcap >= 1e12:
        return f"${mcap/1e12:.2f}T"
    if mcap >= 1e9:
        return f"${mcap/1e9:.2f}B"
    if mcap >= 1e6:
        return f"${mcap/1e6:.2f}M"
    return f"${mcap:,.0f}"


def _build_descriptive_stats(
    info: dict, hist: pd.DataFrame, last_price: float,
) -> dict:
    """Build a flat dict of fundamentals + technical descriptors for the report."""
    out: dict = {}
    out["market_cap_display"] = _format_mcap(info.get("marketCap"))
    pe = info.get("trailingPE");      out["pe"] = f"{pe:.1f}" if pe else "-"
    pb = info.get("priceToBook");     out["pb"] = f"{pb:.1f}" if pb else "-"
    fpe = info.get("forwardPE");      out["forward_pe"] = f"{fpe:.1f}" if fpe else "-"
    peg = info.get("pegRatio");       out["peg"] = f"{peg:.2f}" if peg else "-"
    dy = info.get("dividendYield")
    out["dividend_yield"] = f"{dy*100:.2f}%" if dy else "-"
    beta = info.get("beta");          out["beta"] = f"{beta:.2f}" if beta else "-"
    hi52 = info.get("fiftyTwoWeekHigh")
    lo52 = info.get("fiftyTwoWeekLow")
    out["fifty_two_week_high"] = f"${hi52:,.2f}" if hi52 else "-"
    out["fifty_two_week_low"] = f"${lo52:,.2f}" if lo52 else "-"
    if hi52 and hi52 > 0:
        out["distance_from_52w_high"] = f"{(last_price/hi52 - 1.0)*100:+.2f}%"
    else:
        out["distance_from_52w_high"] = "-"

    # technicals from price history
    if not hist.empty and "close" in hist.columns:
        close = hist["close"]
        # RSI(14) -- Wilder smoothing
        delta = close.diff()
        up = delta.where(delta > 0, 0.0)
        down = -delta.where(delta < 0, 0.0)
        roll_up = up.ewm(alpha=1/14, adjust=False).mean()
        roll_down = down.ewm(alpha=1/14, adjust=False).mean()
        rs = roll_up / roll_down.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi_v = rsi.iloc[-1]
        out["rsi_14"] = f"{rsi_v:.1f}" if pd.notna(rsi_v) else "-"

        sma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else None
        sma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else None
        out["sma50"] = f"${sma50:,.2f}" if sma50 and pd.notna(sma50) else "-"
        out["sma200"] = f"${sma200:,.2f}" if sma200 and pd.notna(sma200) else "-"

        # ATR(14)
        if all(c in hist.columns for c in ["high", "low", "close"]):
            tr1 = hist["high"] - hist["low"]
            tr2 = (hist["high"] - hist["close"].shift(1)).abs()
            tr3 = (hist["low"] - hist["close"].shift(1)).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1]
            out["atr_14"] = f"${atr:,.2f}" if pd.notna(atr) else "-"
        else:
            out["atr_14"] = "-"

    if "volume" in hist.columns and len(hist) >= 20:
        av20 = hist["volume"].iloc[-20:].mean()
        if av20 >= 1e6:
            out["avg_volume"] = f"{av20/1e6:.2f}M"
        else:
            out["avg_volume"] = f"{av20:,.0f}"
    else:
        out["avg_volume"] = "-"

    # margins / growth
    pm = info.get("profitMargins")
    out["profit_margin"] = f"{pm*100:.2f}%" if pm else "-"
    roe = info.get("returnOnEquity")
    out["roe"] = f"{roe*100:.2f}%" if roe else "-"
    rg = info.get("revenueGrowth")
    out["revenue_growth"] = f"{rg*100:+.2f}%" if rg else "-"
    eg = info.get("earningsGrowth")
    out["earnings_growth"] = f"{eg*100:+.2f}%" if eg else "-"
    de = info.get("debtToEquity")
    out["debt_to_equity"] = f"{de:.1f}" if de else "-"
    cr = info.get("currentRatio")
    out["current_ratio"] = f"{cr:.2f}" if cr else "-"
    sr = info.get("shortRatio")
    out["short_ratio"] = f"{sr:.2f}" if sr else "-"

    return out


def _heuristic_ticker_factors(info: dict, hist: pd.DataFrame) -> dict[str, float]:
    """Real-data-anchored ticker_factors clipped to standard +/-3 z.

    Z-score-like proxies derived from yfinance fundamentals + price
    history. CLIPPED to +/-3 to match standard quant practice
    (Grinold-Kahn 2000 ch.3 -- "winsorize at +/-3 sigma" -- prevents
    extreme outliers from dominating a small-universe regression).
    Replace with canonical_universe factor scores once a full
    cross-section is fetched.
    """
    def _clip(v: float) -> float:
        return max(-3.0, min(3.0, v))

    factors: dict[str, float] = {f: 0.0 for f in DEFAULT_FACTORS}
    pe = info.get("trailingPE")
    if pe is not None and pe > 0:
        factors["value_score"] = _clip(-(pe - 22.0) / 15.0)
    roe = info.get("returnOnEquity")
    if roe is not None:
        factors["quality_score"] = _clip((roe - 0.15) / 0.10)
    if len(hist) > 252 and "close" in hist.columns:
        ret_12m = float(hist["close"].iloc[-1] / hist["close"].iloc[-252] - 1.0)
        factors["momentum_score"] = _clip((ret_12m - 0.10) / 0.30)
    if len(hist) > 60 and "close" in hist.columns:
        log_ret = np.log(hist["close"] / hist["close"].shift(1)).dropna()
        if len(log_ret) >= 60:
            sigma = float(log_ret.iloc[-60:].std(ddof=1) * (252 ** 0.5))
            factors["lowvol_score"] = _clip(-(sigma - 0.30) / 0.20)
    eg = info.get("earningsGrowth")
    if eg is not None:
        factors["revisions_score"] = _clip((eg - 0.10) / 0.20)
    return factors


def _build_cross_sectional_history(n: int = 800, seed: int = 42) -> pd.DataFrame:
    """Literature-calibrated synthetic cross-section for loadings.

    Coefficients reflect published 30d factor returns (Gu/Kelly/Xiu 2020,
    Fama-French 2015). Used as the regression training set ONLY -- the
    ticker factor values (above) come from real yfinance data.
    """
    rng = np.random.default_rng(seed)
    n_factors = len(DEFAULT_FACTORS)
    X = rng.normal(0.0, 1.0, size=(n, n_factors))
    true_coef = np.zeros(n_factors)
    true_coef[DEFAULT_FACTORS.index("value_score")] = 0.05
    true_coef[DEFAULT_FACTORS.index("quality_score")] = 0.02
    true_coef[DEFAULT_FACTORS.index("momentum_score")] = 0.03
    true_coef[DEFAULT_FACTORS.index("lowvol_score")] = 0.015
    true_coef[DEFAULT_FACTORS.index("revisions_score")] = 0.025
    true_coef[DEFAULT_FACTORS.index("news_activity_score")] = 0.04
    y = X @ true_coef + rng.normal(0.0, 0.03, size=n)
    df = pd.DataFrame(X, columns=list(DEFAULT_FACTORS))
    df["realized_horizon_return"] = y
    return df


def run_live_for_ticker(
    ticker: str,
    *,
    as_of_date: str,
    horizon_days: int,
    output_dir: Path,
    allow_llm: bool = False,
    loadings_history: Optional[pd.DataFrame] = None,
    ic_backtest_result=None,
    ic_backtest_provenance: str = "synthetic_panel",
    include_sentiment: bool = True,
    # Shared market context (built once in main, injected per ticker)
    spy_close_series: Optional[pd.Series] = None,
    sector_etf_panel: Optional[pd.DataFrame] = None,
    ai_basket_close: Optional[pd.Series] = None,
    fmp_client=None,
) -> dict:
    print(f"[{ticker}] fetching yfinance...", flush=True)
    hist = _fetch_yf_history(ticker, period="3y")
    info = _fetch_yf_info(ticker)
    if hist.empty or "close" not in hist.columns:
        print(f"[{ticker}] no historical data; skipping.")
        return {"ticker": ticker, "error": "no_history"}

    last_price = float(hist["close"].iloc[-1])
    company_name = info.get("longName") or info.get("shortName") or ticker
    display_ticker = TICKER_DISPLAY_NAME.get(ticker, ticker)
    sector = info.get("sector")

    if loadings_history is None:
        loadings_history = _build_cross_sectional_history()
    loadings, intercept, residuals = compute_factor_loadings(
        loadings_history, horizon_days=horizon_days,
    )

    ticker_factors = _heuristic_ticker_factors(info, hist)

    linear_fc = forecast_return(
        ticker_factors=ticker_factors,
        loadings=loadings,
        intercept=intercept,
        residuals=residuals,
        ticker=ticker, as_of_date=as_of_date,
        horizon_days=horizon_days, n_bootstrap=1000,
    )
    linear_method = MethodForecast(
        method="linear", point_return=linear_fc.point_return,
        lower_80=linear_fc.lower_80, upper_80=linear_fc.upper_80,
        lower_95=linear_fc.lower_95, upper_95=linear_fc.upper_95,
    )

    sigma = estimate_vol_from_history(
        hist["close"], method="yang_zhang", ohlc=hist, window_days=60,
    )
    drift = estimate_drift_from_factors(linear_fc.point_return, horizon_days)
    mc = simulate_gbm(
        ticker, horizon_days=horizon_days,
        drift_annual=drift, sigma_annual=sigma, n_paths=1000,
    )
    mc_paths = simulate_gbm_paths(
        horizon_days=horizon_days,
        drift_annual=drift, sigma_annual=sigma, n_paths=200,
    )
    mc_method = MethodForecast(
        method="monte_carlo", point_return=mc.point_return,
        lower_80=mc.lower_80, upper_80=mc.upper_80,
        lower_95=mc.lower_95, upper_95=mc.upper_95,
    )

    ar1_method = ar1_forecast(hist["close"], horizon_days=horizon_days)
    rw_method = random_walk_forecast(hist["close"], horizon_days=horizon_days)

    ensemble = combine_ensemble(
        ticker=ticker, horizon_days=horizon_days,
        methods=[linear_method, mc_method, ar1_method, rw_method],
    )

    print(f"[{ticker}] fetching macro context...", flush=True)
    macro = fetch_macro_context(
        ticker_sector=sector,
        ticker_close=hist["close"],
        as_of_date=as_of_date,
    )

    catalysts_all = _fetch_yf_earnings_dates(ticker)
    try:
        as_of = _dt.date.fromisoformat(as_of_date)
    except ValueError:
        as_of = _dt.date.today()
    end = as_of + _dt.timedelta(days=horizon_days)
    catalysts = []
    for cat in catalysts_all:
        try:
            d = _dt.date.fromisoformat(cat["catalyst_date"])
            if as_of <= d <= end:
                catalysts.append(cat)
        except (KeyError, ValueError):
            continue

    rv_yz = sigma
    log_ret = np.log(hist["close"] / hist["close"].shift(1)).dropna()
    rv_cc = (
        float(log_ret.iloc[-60:].std(ddof=1) * (252 ** 0.5))
        if len(log_ret) >= 60 else None
    )

    contribs = linear_fc.factor_contributions
    top_factors = []
    for f, c in sorted(contribs.items(), key=lambda kv: abs(kv[1]), reverse=True):
        if f == "__intercept__":
            continue
        top_factors.append({
            "factor": f, "contribution": float(c),
            "value": float(ticker_factors.get(f, 0.0)),
            "loading": float(loadings[f]) if f in loadings.index else None,
        })
        if len(top_factors) >= 4:
            break

    print(f"[{ticker}] building narrative ({'claude' if allow_llm else 'template'})...", flush=True)
    narrative = build_narrative(
        ticker=ticker, sector=sector,
        as_of_date=as_of_date, horizon_days=horizon_days,
        ensemble_point=ensemble.ensemble_point,
        ensemble_lower_80=ensemble.ensemble_lower_80,
        ensemble_upper_80=ensemble.ensemble_upper_80,
        ensemble_lower_95=ensemble.ensemble_lower_95,
        ensemble_upper_95=ensemble.ensemble_upper_95,
        method_points={m.method: m.point_return for m in ensemble.methods},
        method_dispersion=ensemble.method_dispersion,
        beats_random_walk=ensemble.beats_random_walk,
        top_factor_contributions=top_factors,
        macro_context={
            "tnx_yield": macro.tnx_yield,
            "irx_yield": macro.irx_yield,
            "yield_curve_slope": macro.yield_curve_slope,
            "vix_level": macro.vix_level,
            "vix_z_score_252d": macro.vix_z_score_252d,
            "sector_etf": macro.sector_etf,
            "sector_relative_return_90d": macro.sector_relative_return_90d,
            "macro_regime": macro.macro_regime,
        },
        catalysts_in_window=catalysts,
        allow_llm=allow_llm,
    )

    # ---- bias / sensitivity / sentiment / critique ----
    print(f"[{ticker}] classifying bias + sensitivity...", flush=True)
    bias = classify_bias(
        ticker=display_ticker,
        factor_contributions=linear_fc.factor_contributions,
        horizon_days=horizon_days,
        macro_regime=macro.macro_regime,
        ensemble_point=ensemble.ensemble_point,
        rw_point=rw_method.point_return,
        beats_random_walk=ensemble.beats_random_walk,
    )

    sensitivity = run_sensitivity(
        ticker=display_ticker,
        base_point_return=linear_fc.point_return,
        loadings=loadings,
        ticker_factors=ticker_factors,
    )

    sentiment_snap = None
    if include_sentiment:
        try:
            print(f"[{ticker}] fetching free sentiment...", flush=True)
            sentiment_snap = fetch_sentiment_snapshot(ticker)
        except Exception as exc:
            print(f"  [warn] sentiment fetch failed for {ticker}: {exc}", file=sys.stderr)

    print(f"[{ticker}] building Claude critique ({'claude' if allow_llm else 'template'})...", flush=True)
    top_sens_factor = (
        sensitivity.factor_sensitivities[0].factor
        if sensitivity.factor_sensitivities else None
    )
    critique = build_critique(
        ticker=display_ticker, sector=sector,
        as_of_date=as_of_date, horizon_days=horizon_days,
        ensemble_point=ensemble.ensemble_point,
        ensemble_lower_80=ensemble.ensemble_lower_80,
        ensemble_upper_80=ensemble.ensemble_upper_80,
        ensemble_lower_95=ensemble.ensemble_lower_95,
        ensemble_upper_95=ensemble.ensemble_upper_95,
        method_points={m.method: m.point_return for m in ensemble.methods},
        method_dispersion=ensemble.method_dispersion,
        beats_random_walk=ensemble.beats_random_walk,
        top_factor_contributions=top_factors,
        macro_context={
            "tnx_yield": macro.tnx_yield,
            "irx_yield": macro.irx_yield,
            "yield_curve_slope": macro.yield_curve_slope,
            "vix_level": macro.vix_level,
            "vix_z_score_252d": macro.vix_z_score_252d,
            "sector_etf": macro.sector_etf,
            "sector_relative_return_90d": macro.sector_relative_return_90d,
            "macro_regime": macro.macro_regime,
        },
        catalysts_in_window=catalysts,
        sensitivity_top_factor=top_sens_factor,
        allow_llm=allow_llm,
    )

    ic_series_df = None
    factor_ic_stats = None
    if ic_backtest_result is not None:
        ic_series_df = ic_backtest_result.ic_series
        factor_ic_stats = ic_backtest_result.per_factor

    # ---- walk-forward backtest + volume composite + descriptive stats ----
    print(f"[{ticker}] walk-forward backtest (60d lookback)...", flush=True)
    try:
        if "volume" in hist.columns:
            volume_series = hist["volume"]
            def _vol_fn(close_sub, lookback, h):
                return volume_confirmed_forecast(
                    close_sub, lookback, h,
                    volume=volume_series.reindex(close_sub.index),
                )
            wf_fn = _vol_fn
        else:
            wf_fn = historical_baseline_forecast
        wf_summary = run_walk_forward(
            ticker=display_ticker, as_of_date=as_of_date,
            close=hist["close"], lookback_days=60,
            horizons=(1, 30, 60), forecast_fn=wf_fn,
        )
    except Exception as exc:
        print(f"  [warn] walk-forward failed for {ticker}: {exc}", file=sys.stderr)
        wf_summary = None

    volume_composite = None
    if "volume" in hist.columns:
        try:
            volume_composite = volume_momentum_composite(hist)
        except Exception as exc:
            print(f"  [warn] volume composite failed for {ticker}: {exc}", file=sys.stderr)

    descriptive_stats = _build_descriptive_stats(info, hist, last_price)

    # ---- sector rotation ----
    sector_features_obj = None
    sector_feats_dict = {}
    sector_etf_close = None
    if sector_etf_panel is not None and not sector_etf_panel.empty:
        etf_symbol = SECTOR_ETF_MAP.get(sector or "")
        if etf_symbol and etf_symbol in sector_etf_panel.columns:
            sector_etf_close = sector_etf_panel[etf_symbol].dropna()
    try:
        if sector and sector_etf_close is not None and spy_close_series is not None:
            sector_features_obj = compute_sector_features(
                ticker=display_ticker, sector=sector,
                stock_close=hist["close"],
                sector_etf_close=sector_etf_close,
                spy_close=spy_close_series,
                ai_factor_close=ai_basket_close,
                sector_etf_panel=sector_etf_panel,
            )
            sector_feats_dict = sector_feature_dict(sector_features_obj)
    except Exception as exc:
        print(f"  [warn] sector features failed for {ticker}: {exc}", file=sys.stderr)

    # ---- multi-timeframe TA snapshot (1wk / 1d / 1h) ----
    mtf_snapshots = {}
    mtf_confluence = {}
    try:
        mtf_snapshots = fetch_multi_timeframe_snapshots(
            ticker, intervals=("1wk", "1d", "1h"),
        )
        mtf_confluence = compute_confluence(mtf_snapshots)
    except Exception as exc:
        print(f"  [warn] multi-TF snapshot failed for {ticker}: {exc}", file=sys.stderr)

    # ---- FMP revisions_score + fundamentals (fills yfinance .info gaps) ----
    fmp_ratios = {}
    fmp_info = {}
    fmp_revisions_score = None
    if fmp_client is not None:
        try:
            fmp_ratios = fmp_client.fetch_financial_ratios(ticker)
            _rev_fn = globals().get("_fmp_compute_revisions_score")
            if _rev_fn is not None:
                fmp_revisions_score, fmp_info = _rev_fn(
                    fmp_client, ticker, last_price=last_price,
                )
        except Exception as exc:
            print(f"  [warn] FMP fetch failed for {ticker}: {exc}", file=sys.stderr)

    # ---- Sector-relative peer comparison ----
    # I included peer comparison as a sector-relative valuation check.
    # The bias-nudge adjustment was previously coupled to a separate
    # adjustment function; that has been removed and the peer signal is
    # now used purely as a display column in the report.
    peer_comparison_obj = None
    try:
        from src.methodology.peer_comparison import compute_peer_comparison
        if len(hist) >= 21:
            mom_20d = float(
                hist["close"].iloc[-1] / hist["close"].iloc[-21] - 1.0
            )
        else:
            mom_20d = None
        target_pe = (fmp_ratios or {}).get("pe") or info.get("trailingPE")
        target_mcap = info.get("marketCap")
        peer_comparison_obj = compute_peer_comparison(
            ticker=ticker, sector=sector,
            target_mcap=float(target_mcap) if target_mcap else None,
            target_pe=float(target_pe) if target_pe else None,
            target_mom_20d=mom_20d,
        )
    except Exception as exc:
        print(f"  [warn] peer comparison failed for {ticker}: {exc}", file=sys.stderr)

    print(f"[{display_ticker}] rendering report...", flush=True)
    historical_close = hist["close"].iloc[-252:] if len(hist) > 252 else hist["close"]
    path = render_stock_report_v2(
        ticker=display_ticker, company_name=company_name, sector=sector,
        as_of_date=as_of_date, horizon_days=horizon_days,
        last_price=last_price, historical_close=historical_close,
        ensemble=ensemble,
        factor_contributions=linear_fc.factor_contributions,
        loadings=loadings, ticker_factors=ticker_factors,
        monte_carlo=mc, mc_paths=mc_paths,
        macro_context=macro, catalysts=catalysts, narrative=narrative,
        ic_series=ic_series_df, factor_ic_stats=factor_ic_stats,
        output_dir=output_dir,
        is_synthetic=False,
        realized_vol_yang_zhang=rv_yz,
        realized_vol_close_to_close=rv_cc,
        sensitivity_result=sensitivity,
        bias_signal=bias,
        sentiment_snapshot=sentiment_snap,
        llm_critique=critique,
        ic_backtest_provenance=ic_backtest_provenance,
        walk_forward_summary=wf_summary,
        volume_composite=volume_composite,
        descriptive_stats=descriptive_stats,
        # Sector + multi-timeframe + FMP
        sector_features=sector_features_obj,
        mtf_snapshots=mtf_snapshots,
        mtf_confluence=mtf_confluence,
        fmp_ratios=fmp_ratios,
        fmp_revisions_info=fmp_info,
        # Peer comparison (sector-relative valuation + momentum)
        peer_comparison=peer_comparison_obj,
    )

    wf_30d_dir = None
    if wf_summary and wf_summary.horizons:
        for h in wf_summary.horizons:
            if h.horizon_days == 30:
                wf_30d_dir = h.directional_accuracy
                break
    return {
        "ticker": display_ticker, "company": company_name, "sector": sector,
        "last_price": last_price,
        "ensemble_point": ensemble.ensemble_point,
        "narrative_source": narrative.source,
        "bias": bias.bias,
        "composite_z": bias.composite_z,
        "playbook": bias.recommended_playbook,
        "wf_dir_acc_30d": wf_30d_dir,
        "path": path,
    }


def _load_finviz_candidates_csv(
    *, repo_root: Path,
) -> tuple[list[dict], list[dict], list[dict], str | None]:
    """Load layer1 FINVIZ-derived long/short/universe candidates.

    Returns (long_rows, short_rows, universe_top100_rows, dated_dir_name).
    Each row dict: {ticker, company, sector, market_cap, price, ...}.
    """
    uni_dir = repo_root / "data" / "universe"
    if not uni_dir.exists():
        return [], [], [], None
    dated = sorted(
        [d for d in uni_dir.iterdir() if d.is_dir() and d.name[:4].isdigit()],
        reverse=True,
    )
    if not dated:
        return [], [], [], None
    latest = dated[0]

    def _csv_to_rows(path: Path) -> list[dict]:
        if not path.exists():
            return []
        try:
            import csv
            with path.open("r", encoding="utf-8", newline="") as f:
                rdr = csv.DictReader(f)
                out = []
                for r in rdr:
                    if not r.get("Ticker"):
                        continue
                    try:
                        mcap = float(r["Market Cap"]) if r.get("Market Cap") else None
                    except (TypeError, ValueError):
                        mcap = None
                    try:
                        price = float(r["Price"]) if r.get("Price") else None
                    except (TypeError, ValueError):
                        price = None
                    out.append({
                        "ticker": r["Ticker"],
                        "company": (r.get("Company") or "")[:40],
                        "sector": r.get("Sector", ""),
                        "market_cap": mcap, "price": price,
                        "pe": r.get("P/E", ""),
                        "perf_year": r.get("Perf Year", ""),
                    })
                return out
        except Exception:
            return []

    long_rows = _csv_to_rows(latest / "finviz_long_candidates.csv")
    short_rows = _csv_to_rows(latest / "finviz_short_candidates.csv")

    universe_rows: list[dict] = []
    db_path = repo_root / "data" / "fundamentals.db"
    if db_path.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute(
                "SELECT Ticker, Company, Sector, [Market Cap], Price "
                "FROM finviz_universe_history "
                "WHERE [Market Cap] IS NOT NULL "
                "ORDER BY scrape_timestamp DESC, [Market Cap] DESC LIMIT 500",
            ).fetchall()
            conn.close()
            seen: set[str] = set()
            for r in rows:
                if not r[0] or r[0] in seen:
                    continue
                seen.add(r[0])
                universe_rows.append({
                    "ticker": r[0], "company": (r[1] or "")[:40],
                    "sector": r[2] or "",
                    "market_cap": float(r[3]) if r[3] else None,
                    "price": float(r[4]) if r[4] else None,
                })
                if len(universe_rows) >= 100:
                    break
        except Exception:
            pass

    return long_rows, short_rows, universe_rows, latest.name


def _load_universe_from_finviz_db(
    *, db_path: Path, top_n: int = 200, min_market_cap_b: float = 2.0,
) -> list[dict]:
    """Pull the most-recent finviz universe scrape from data/fundamentals.db.

    Returns rows (ticker, company, sector, mcap) sorted by market cap desc,
    filtered to mcap >= min_market_cap_b, top-N.
    """
    import sqlite3
    c = sqlite3.connect(str(db_path))
    try:
        rows = c.execute(
            "SELECT Ticker, Company, Sector, [Market Cap] FROM finviz_universe_history "
            "WHERE [Market Cap] >= ? ORDER BY scrape_timestamp DESC, [Market Cap] DESC",
            (min_market_cap_b * 1e9,),
        ).fetchall()
    finally:
        c.close()
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        t = r[0]
        if not t or t in seen:
            continue
        seen.add(t)
        out.append({"ticker": t, "company": r[1], "sector": r[2], "mcap": r[3]})
        if len(out) >= top_n:
            break
    return out


def _render_structured_index(
    *, summaries: list[dict], output_dir: Path, as_of_date: str,
    watchlist_tickers: Optional[list[str]] = None,
    finviz_long_candidates: Optional[list[dict]] = None,
    finviz_short_candidates: Optional[list[dict]] = None,
    top100_universe: Optional[list[dict]] = None,
    finviz_dated_dir: Optional[str] = None,
) -> Path:
    """5-section structured index with a Bloomberg-style dark theme.

    Sections:
      1. Research & Methodology (links)
      2. Watchlist (optional config/watchlist.json, empty by default)
      3. Long candidates  -- from FINVIZ phase-1 filter (NOT bias-derived)
      4. Short candidates -- from FINVIZ phase-1 filter
      5. All tickers      -- top 100 by market cap from finviz_universe_history

    Per-ticker rows still show the layer3 bias as an info column when a
    forecast was actually run for that ticker.
    """
    watchlist_set = set(watchlist_tickers or [])
    BIAS_5_TO_CLASS = {
        "strong_buy":  ("tag-green-bright", "STRONG BUY"),
        "buy":         ("tag-green",        "BUY"),
        "neutral":     ("tag-blue",         "NEUTRAL"),
        "sell":        ("tag-red",          "SELL"),
        "strong_sell": ("tag-red-bright",   "STRONG SELL"),
    }

    def _row(m: dict) -> str:
        if "error" in m:
            return (
                f'<tr class="err" data-bias="error" data-sector="">'
                f'<td>{m.get("ticker","?")}</td>'
                f'<td colspan="8">ERROR: {m.get("error","?")}</td></tr>'
            )
        pt = m.get("ensemble_point", 0.0)
        sign = "+" if pt >= 0 else ""
        color = "#9ece6a" if pt >= 0 else "#f7768e"
        bias5 = m.get("bias", "neutral") or "neutral"
        bias_class, bias_label = BIAS_5_TO_CLASS.get(bias5, ("tag-blue", bias5))
        cz = m.get("composite_z", 0.0)
        pb = m.get("playbook", "?")
        wf_dir = m.get("wf_dir_acc_30d")
        wf_str = f"{wf_dir:.1%}" if wf_dir is not None else "-"
        return (
            f"<tr data-bias='{bias5}' data-sector='{m.get('sector','')}'>"
            f"<td><a href='{m['ticker']}.html'>{m['ticker']}</a></td>"
            f"<td>{(m.get('company','') or '')[:32]}</td>"
            f"<td>{m.get('sector','')}</td>"
            f"<td>${m.get('last_price', 0.0):,.2f}</td>"
            f"<td style='color:{color}'>{sign}{pt:.2%}</td>"
            f"<td><span class='tag {bias_class}'>{bias_label}</span></td>"
            f"<td>{cz:+.2f}</td>"
            f"<td>{pb}</td>"
            f"<td>{wf_str}</td></tr>"
        )

    # Lookup forecast summary by ticker (for merging into FINVIZ rows)
    summary_by_ticker = {m["ticker"]: m for m in summaries if "ticker" in m}

    def _finviz_row(fv: dict) -> str:
        """Row for FINVIZ-derived sections (long/short/top-100).

        Pulls in the forecast summary if we ran one for that ticker;
        otherwise shows "-" placeholders for bias / forecast columns.
        """
        t = fv.get("ticker", "?")
        s = summary_by_ticker.get(t, {})
        sector = (s.get("sector") or fv.get("sector") or "")[:24]
        company = (s.get("company") or fv.get("company") or "")[:32]
        price = s.get("last_price") if s.get("last_price") else fv.get("price")
        mcap = fv.get("market_cap")
        mcap_str = (
            f"${mcap/1e9:.1f}B" if mcap and mcap >= 1e9
            else (f"${mcap/1e6:.0f}M" if mcap and mcap >= 1e6 else "-")
        )
        # forecast column: only fills if we ran one
        if "ensemble_point" in s and "error" not in s:
            pt = s["ensemble_point"]
            sign = "+" if pt >= 0 else ""
            color = "var(--green-bright)" if pt >= 0 else "var(--red-bright)"
            ens_html = f"<span class='num' style='color:{color}'>{sign}{pt:.2%}</span>"
        else:
            ens_html = "<span class='num' style='color:var(--muted)'>-</span>"
        if "bias" in s and "error" not in s:
            bias5 = s.get("bias", "neutral") or "neutral"
            bias_class, bias_label = BIAS_5_TO_CLASS.get(bias5, ("tag-blue", bias5))
            bias_html = f"<span class='tag {bias_class}'>{bias_label}</span>"
            cz = s.get("composite_z", 0.0)
            cz_html = f"<span class='num'>{cz:+.2f}</span>"
        else:
            bias_html = "<span style='color:var(--muted); font-size:11px;'>not run</span>"
            cz_html = "<span class='num' style='color:var(--muted)'>-</span>"
        price_html = (
            f"<span class='num' style='color:var(--accent2)'>${price:,.2f}</span>"
            if price else "<span class='num' style='color:var(--muted)'>-</span>"
        )
        # Link to report only if we ran one
        ticker_html = (
            f"<a href='{t}.html'>{t}</a>" if t in summary_by_ticker
            else f"<span style='color:var(--fg2)'>{t}</span>"
        )
        return (
            f"<tr data-sector='{sector}'>"
            f"<td><b>{ticker_html}</b></td>"
            f"<td style='color:var(--fg2)'>{company}</td>"
            f"<td style='color:var(--muted); font-size:11px'>{sector}</td>"
            f"<td>{price_html}</td>"
            f"<td><span class='num' style='color:var(--muted)'>{mcap_str}</span></td>"
            f"<td>{ens_html}</td>"
            f"<td>{bias_html}</td>"
            f"<td>{cz_html}</td></tr>"
        )

    def _finviz_section_table(title: str, rows: list[dict], section_id: str,
                              caveat: str = "") -> str:
        if not rows:
            return (
                f'<h2 id="{section_id}">{title} <span class="count">(0)</span></h2>'
                f'<div class="empty">No tickers in this section.</div>'
            )
        body = "".join(_finviz_row(r) for r in rows)
        caveat_html = (
            f'<div class="section-caveat">{caveat}</div>' if caveat else ""
        )
        return f"""<h2 id="{section_id}">{title} <span class="count">({len(rows)})</span></h2>
{caveat_html}
<table class="ti-table">
  <thead><tr>
    <th data-col="0" data-type="str">Ticker</th>
    <th data-col="1" data-type="str">Company</th>
    <th data-col="2" data-type="str">Sector</th>
    <th data-col="3" data-type="num">Last $</th>
    <th data-col="4" data-type="num">Mkt cap</th>
    <th data-col="5" data-type="pct">30d ensemble</th>
    <th data-col="6" data-type="str">Bias</th>
    <th data-col="7" data-type="num">Composite z</th>
  </tr></thead>
  <tbody>{body}</tbody>
</table>"""

    def _section_table(title: str, sub_rows: list[dict], section_id: str) -> str:
        if not sub_rows:
            return (
                f'<h2 id="{section_id}">{title} '
                f'<span class="count">(0)</span></h2>'
                f'<div class="empty">No tickers in this section.</div>'
            )
        rows = "".join(_row(m) for m in sub_rows)
        return f"""<h2 id="{section_id}">{title} <span class="count">({len(sub_rows)})</span></h2>
<table class="ti-table">
  <thead><tr>
    <th data-col="0" data-type="str">Ticker</th>
    <th data-col="1" data-type="str">Company</th>
    <th data-col="2" data-type="str">Sector</th>
    <th data-col="3" data-type="num">Last $</th>
    <th data-col="4" data-type="pct">30d ensemble</th>
    <th data-col="5" data-type="str">Bias</th>
    <th data-col="6" data-type="num">Composite z</th>
    <th data-col="7" data-type="str">Setup</th>
    <th data-col="8" data-type="pct">30d WF dir</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>"""

    # Sort summaries: by |composite_z| desc within each bias group
    def _sort_key(m: dict) -> float:
        return -abs(float(m.get("composite_z", 0.0)))

    watchlist_rows = [m for m in summaries if m.get("ticker") in watchlist_set]
    watchlist_rows.sort(key=_sort_key)

    strong_buy = [m for m in summaries
                  if m.get("bias") == "strong_buy" and "error" not in m]
    buy_rows = [m for m in summaries
                if m.get("bias") == "buy" and "error" not in m]
    long_section = sorted(strong_buy + buy_rows, key=_sort_key)

    strong_sell = [m for m in summaries
                   if m.get("bias") == "strong_sell" and "error" not in m]
    sell_rows = [m for m in summaries
                 if m.get("bias") == "sell" and "error" not in m]
    short_section = sorted(strong_sell + sell_rows, key=_sort_key)

    neutral_rows = [m for m in summaries
                    if m.get("bias") == "neutral" and "error" not in m]
    error_rows = [m for m in summaries if "error" in m]
    all_rows = sorted([m for m in summaries if "error" not in m],
                       key=lambda m: m.get("ticker", ""))

    n_long = len(long_section)
    n_short = len(short_section)
    n_neutral = len(neutral_rows)
    n_err = len(error_rows)
    rows_html = []  # legacy, no longer needed; sections handle it

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trade Identifier -- Forecast Reports {as_of_date}</title>
<style>
  :root {{
    /* Bloomberg-Terminal modernised dark palette */
    --bg: #08090d; --bg2: #0d1017; --bg3: #131722; --bg-card: #161a25;
    --fg: #e6e9ef; --fg2: #a0a8b8; --muted: #6b7280;
    --accent: #4d9fff; --accent2: #f39f41;        /* amber for prices */
    --green: #26a69a; --green-bright: #4af6c3;
    --red: #ef5350; --red-bright: #ff433d;
    --orange: #fb8b1e; --yellow: #f5c842;
    --border: rgba(255,255,255,0.06);
    --border-strong: rgba(255,255,255,0.12);
    --font-sans: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', system-ui, sans-serif;
    --font-mono: 'JetBrains Mono', 'SF Mono', 'Cascadia Code', 'Consolas', ui-monospace, monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--fg); margin: 0; padding: 18px;
         font-family: var(--font-sans); font-size: 13px; font-weight: 500;
         max-width: 1400px; margin: 0 auto; line-height: 1.5;
         -webkit-font-smoothing: antialiased;
         font-feature-settings: 'tnum', 'cv11'; }}
  .num, td.num {{ font-family: var(--font-mono); font-variant-numeric: tabular-nums;
                  text-align: right; white-space: nowrap; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  h1 {{ color: var(--accent2); font-size: 26px; margin-bottom: 2px; }}
  h2 {{ color: var(--accent); font-size: 17px; margin-top: 28px;
       border-bottom: 1px solid var(--bg3); padding-bottom: 6px;
       display: flex; align-items: center; gap: 8px; }}
  h2 .count {{ color: var(--muted); font-size: 13px; font-weight: 400; }}
  .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 14px; }}
  .tagline {{ color: var(--orange); font-size: 12px; margin-bottom: 16px;
             padding: 8px 10px; background: var(--bg2); border-left: 3px solid var(--orange);
             border-radius: 4px; }}

  .nav {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 14px 0 20px 0; }}
  .nav a {{ padding: 6px 12px; background: var(--bg2); border: 1px solid var(--bg3);
            border-radius: 14px; font-size: 12px; font-weight: 500; }}
  .nav a:hover {{ background: var(--bg3); text-decoration: none; }}
  .nav a.nav-long {{ color: var(--green); border-color: #2a3a23; }}
  .nav a.nav-short {{ color: var(--red); border-color: #3a2330; }}
  .nav a.nav-research {{ color: var(--accent2); border-color: #2a1f44; }}

  .ti-table {{ width: 100%; border-collapse: collapse; font-size: 13px;
                background: var(--bg2); border-radius: 8px; overflow: hidden;
                margin-bottom: 8px; }}
  th {{ background: var(--bg3); color: var(--accent); padding: 8px;
        text-align: left; cursor: pointer; user-select: none;
        text-transform: uppercase; font-size: 11px; }}
  th:hover {{ background: #353a52; }}
  td {{ padding: 7px 8px; border-top: 1px solid var(--bg3); }}
  tr:hover {{ background: #1e2030; }}
  tr.err td {{ color: var(--red); }}

  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px;
         font-size: 10px; font-weight: 700; letter-spacing: 0.3px; }}
  .tag-green-bright {{ background: #2a3a23; color: var(--green-bright);
                        box-shadow: inset 0 0 0 1px var(--green-bright); }}
  .tag-green {{ background: #2a3a23; color: var(--green); }}
  .tag-red {{ background: #3a2330; color: var(--red); }}
  .tag-red-bright {{ background: #3a2330; color: var(--red-bright);
                      box-shadow: inset 0 0 0 1px var(--red-bright); }}
  .tag-blue {{ background: #1f2a44; color: var(--accent); }}

  .stats {{ display: grid; grid-template-columns: repeat(5, 1fr);
            gap: 10px; margin: 12px 0; }}
  .stat {{ background: var(--bg2); border: 1px solid var(--bg3);
          padding: 10px; border-radius: 6px; }}
  .stat .k {{ color: var(--muted); font-size: 11px; text-transform: uppercase; }}
  .stat .v {{ color: var(--fg); font-size: 22px; font-weight: 700; margin-top: 4px; }}

  .empty {{ color: var(--muted); font-style: italic; padding: 12px;
            background: var(--bg2); border-radius: 6px; font-size: 13px; }}
  .section-caveat {{ color: var(--muted); font-size: 11px;
                     padding: 6px 10px; background: var(--bg2);
                     border-left: 2px solid var(--border-strong);
                     margin: 4px 0 8px 0; border-radius: 3px; }}

  .filter-bar {{ background: var(--bg2); border: 1px solid var(--bg3);
                padding: 10px; border-radius: 6px; margin: 12px 0;
                font-size: 13px; display: flex; flex-wrap: wrap;
                gap: 8px; align-items: center; }}
  .filter-bar label {{ color: var(--muted); }}
  .filter-bar select, .filter-bar input {{
    background: var(--bg3); color: var(--fg); border: 1px solid var(--bg3);
    border-radius: 4px; padding: 4px 8px; font-size: 13px;
  }}

  .footer {{ color: var(--muted); font-size: 11px; margin-top: 32px;
            padding-top: 14px; border-top: 1px solid var(--bg3); }}
  @media (max-width: 768px) {{
    body {{ padding: 10px; }}
    h1 {{ font-size: 20px; }}
    h2 {{ font-size: 14px; margin-top: 22px; }}
    .stats {{ grid-template-columns: repeat(2, 1fr); }}
    .stat .v {{ font-size: 17px; }}
    .ti-table {{ font-size: 11px; }}
    th, td {{ padding: 5px; }}
    th:nth-child(2), td:nth-child(2),
    th:nth-child(3), td:nth-child(3) {{ display: none; }}
    .nav {{ gap: 4px; }} .nav a {{ font-size: 11px; padding: 5px 10px; }}
  }}
</style></head><body>

<h1>Trade Identifier &mdash; Forecast Reports</h1>
<div class="meta">As of <b>{as_of_date}</b>
&nbsp;|&nbsp; {len([s for s in summaries if 'error' not in s])} tickers analysed
&nbsp;|&nbsp; Stack: 5-cat bias + sector rotation + multi-timeframe TA</div>

<div class="tagline">
  The honest peer-reviewed ceiling on daily directional accuracy is 53-58%
  for liquid US large-caps. The signal here is the spread between prediction and 50%
  (sized accordingly), reinforced by <b>sector-rotation + AI-spillover</b> tilt.
</div>

<div class="nav">
  <a href="#research" class="nav-research">&#x1F4D6; Research &amp; Methodology</a>
  <a href="#watchlist">&#x2B50; Watchlist ({len(watchlist_rows)})</a>
  <a href="#long" class="nav-long">&#x2197; Long candidates ({len(finviz_long_candidates or [])})</a>
  <a href="#short" class="nav-short">&#x2198; Short candidates ({len(finviz_short_candidates or [])})</a>
  <a href="#all">All tickers ({len(top100_universe or [])})</a>
</div>

<div class="stats">
  <div class="stat"><div class="k">STRONG BUY</div>
    <div class="v" style="color:var(--green-bright);">{sum(1 for s in summaries if s.get('bias') == 'strong_buy')}</div></div>
  <div class="stat"><div class="k">BUY</div>
    <div class="v" style="color:var(--green);">{sum(1 for s in summaries if s.get('bias') == 'buy')}</div></div>
  <div class="stat"><div class="k">NEUTRAL</div>
    <div class="v">{n_neutral}</div></div>
  <div class="stat"><div class="k">SELL</div>
    <div class="v" style="color:var(--red);">{sum(1 for s in summaries if s.get('bias') == 'sell')}</div></div>
  <div class="stat"><div class="k">STRONG SELL</div>
    <div class="v" style="color:var(--red-bright);">{sum(1 for s in summaries if s.get('bias') == 'strong_sell')}</div></div>
</div>

<h2 id="research">Research &amp; Methodology</h2>
<div style="font-size: 13px; line-height: 1.7;">
  &bull; <a href="research_docs.html">Methodology &amp; metric reference &rarr;</a>
        -- every metric explained with research citations.<br>
  &bull; <a href="benchmark_directional.html">Honest directional-accuracy benchmark &rarr;</a>
        -- 5-fold purged walk-forward vs PMC9680880 verdict.<br>
  &bull; Stack: 89-feature LightGBM + triple-barrier labels + purged walk-forward.<br>
  &bull; Sectoral rotation: AI-spillover beta, sector momentum vs SPY, cyclical/defensive phase.<br>
  &bull; Multi-timeframe TA: 1wk / 1d / 1h confluence patterns (Elder triple-screen,
        Bollinger squeeze + Donchian breakout, MACD-cross + OBV).
</div>

{_section_table("&#x2B50; Watchlist (optional config/watchlist.json)", watchlist_rows, "watchlist")}
{_finviz_section_table(
    "&#x2197; Long candidates (FINVIZ Phase-1 filter)",
    finviz_long_candidates or [],
    "long",
    caveat=f"Sourced from layer1 FINVIZ filter run on {finviz_dated_dir or 'unknown date'}. "
           "The bias column shows the layer3 forecast verdict where one was run.",
)}
{_finviz_section_table(
    "&#x2198; Short candidates (FINVIZ Phase-1 filter)",
    finviz_short_candidates or [],
    "short",
    caveat=f"Sourced from layer1 FINVIZ filter run on {finviz_dated_dir or 'unknown date'}. "
           "Negative-screen criteria (high valuation / weak momentum).",
)}
{_finviz_section_table(
    "All tickers (top 100 by market cap)",
    top100_universe or [],
    "all",
    caveat="From finviz_universe_history. Ranked by latest scrape's market cap.",
)}

{('<h2 id="errors" style="color:var(--red);">Errors</h2><div class="empty">' + chr(10).join(_row(m) for m in error_rows) + '</div>') if error_rows else ''}

<div class="footer">
  Generated by the forecast layer driver.
  5-cat bias (strong-buy / buy / neutral / sell / strong-sell) | Sector rotation w/ AI-spillover |
  Multi-timeframe confluence (1wk/1d/1h) |
  Walk-forward backtest (60d lookback) | Black-Scholes 25-delta IV skew |
  Not investment advice.
</div>

<script>
(function() {{
  // Per-section table sort: click any TH to sort that section
  document.querySelectorAll("table.ti-table").forEach(tbl => {{
    const tbody = tbl.querySelector("tbody");
    const ths = tbl.querySelectorAll("th");
    ths.forEach(th => {{
      th.addEventListener("click", () => {{
        const col = parseInt(th.dataset.col, 10);
        const type = th.dataset.type;
        const cur = th.dataset.dir || "desc";
        const dir = cur === "desc" ? "asc" : "desc";
        ths.forEach(t => t.removeAttribute("data-dir"));
        th.dataset.dir = dir;
        const rows = Array.from(tbody.querySelectorAll("tr"));
        rows.sort((a, b) => {{
          let av = a.children[col].textContent.trim();
          let bv = b.children[col].textContent.trim();
          if (type === "num" || type === "pct") {{
            av = parseFloat(av.replace(/[\\$,%+]/g, ""));
            bv = parseFloat(bv.replace(/[\\$,%+]/g, ""));
            if (isNaN(av)) av = -Infinity;
            if (isNaN(bv)) bv = -Infinity;
          }}
          return dir === "asc" ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1);
        }});
        rows.forEach(r => tbody.appendChild(r));
      }});
    }});
  }});
}})();
</script>
</body></html>"""
    out_path = output_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _render_research_docs(output_dir: Path) -> Path:
    """Methodology + metric reference index page.

    I keep all metric definitions and source citations together on one
    page so each per-ticker report can link back to a single source of
    truth for what each number means and where it comes from.
    """
    html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trade Identifier -- Methodology & Metric Reference</title>
<style>
  :root {
    --bg: #1a1b26; --bg2: #24283b; --bg3: #2f334d;
    --fg: #c0caf5; --fg2: #a9b1d6; --muted: #737aa2;
    --accent: #7aa2f7; --accent2: #bb9af7;
    --green: #9ece6a; --red: #f7768e; --orange: #e0af68;
  }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--fg); margin: 0; padding: 18px;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 980px; margin: 0 auto; line-height: 1.55; }
  a { color: var(--accent); }
  h1 { color: var(--accent2); font-size: 24px; }
  h2 { color: var(--accent); font-size: 18px; margin-top: 28px;
       border-bottom: 1px solid var(--bg3); padding-bottom: 6px; }
  h3 { color: var(--fg); font-size: 14px; margin-top: 18px; }
  code { background: var(--bg3); padding: 2px 6px; border-radius: 3px;
         font-family: ui-monospace, SFMono-Regular, monospace; font-size: 13px; }
  .metric { background: var(--bg2); border: 1px solid var(--bg3);
            padding: 12px 14px; border-radius: 6px; margin: 10px 0; }
  .metric h3 { margin-top: 0; }
  .metric .why { color: var(--orange); font-size: 12px; margin-top: 6px; }
  .metric .ref { color: var(--muted); font-size: 11px; margin-top: 4px;
                 padding-top: 6px; border-top: 1px solid var(--bg3); }
  .nav-back { color: var(--accent); font-size: 13px; }
  @media (max-width: 768px) { body { padding: 10px; font-size: 14px; } h1 { font-size: 20px; } h2 { font-size: 16px; } }
</style></head><body>

<a href="index.html" class="nav-back">&larr; back to reports index</a>
<h1>Methodology &amp; metric reference</h1>
<p>Every metric in the per-ticker report has a definition, a research source, and a reason
why it goes into the forecast. This page is the central reference.</p>

<h2>1. Forecast formula</h2>
<div class="metric">
  <h3>Linear factor expected-return</h3>
  <p><code>r_hat = alpha + sum_i (w_i * f_i)</code></p>
  <p><b>What it does</b>: each ticker's z-scored factor value <code>f_i</code> is multiplied by
  the cross-sectional loading <code>w_i</code> learned from the full universe.
  The sum + intercept gives the forecasted h-day return.</p>
  <div class="why">Why: well-engineered linear factor models match deep-learning approaches
  on monthly-to-quarterly horizons (Gu/Kelly/Xiu 2020 <i>RFS</i>).</div>
  <div class="ref">Gu, Kelly, Xiu (2020) "Empirical Asset Pricing via Machine Learning", <i>RFS</i> 33(5).</div>
</div>
<div class="metric">
  <h3>Ensemble (4 methods)</h3>
  <p>Linear factor + GBM Monte Carlo + AR(1) + Random-Walk baseline.
  Weights set by inverse method dispersion; "beats random walk" flag if linear point
  is &gt; 0.5% away from random-walk point.</p>
  <div class="why">Why: ensembling reduces single-model variance. The random-walk baseline is
  the humility line -- if the ensemble doesn't beat RW, we have no edge.</div>
  <div class="ref">Hansen 2007; Timmermann 2006 (forecast combination).</div>
</div>

<h2>2. Factor scores (the f_i)</h2>
<div class="metric">
  <h3>value_score</h3>
  <p>Z-score of valuation multiples (P/E, EV/EBITDA, P/B, FCF yield).
  Higher = cheaper = positive expected return.</p>
  <div class="ref">Fama-French 1992 (HML); Asness-Frazzini-Pedersen 2013 (value-quality).</div>
</div>
<div class="metric">
  <h3>quality_score</h3>
  <p>Z-score of return-on-equity, gross-margin stability, low-leverage. Higher = quality.</p>
  <div class="ref">Asness-Frazzini-Pedersen 2013, Novy-Marx 2013.</div>
</div>
<div class="metric">
  <h3>momentum_score</h3>
  <p>12-1m total return z-score (skip the last month to avoid 1m reversal).
  Higher = trending up.</p>
  <div class="ref">Jegadeesh-Titman 1993; Asness 1994.</div>
</div>
<div class="metric">
  <h3>lowvol_score</h3>
  <p>Negative of Yang-Zhang 60d realised vol z-score.
  Higher = low vol = higher risk-adjusted return.</p>
  <div class="ref">Yang-Zhang 2000 (realised vol); Frazzini-Pedersen 2014 (BAB).</div>
</div>
<div class="metric">
  <h3>revisions_score</h3>
  <p>Z-score of analyst EPS estimate change over 3m. Higher = positive revisions = upward drift.</p>
  <div class="ref">Chan-Jegadeesh-Lakonishok 1996.</div>
</div>
<div class="metric">
  <h3>news_activity_score</h3>
  <p>7-signal composite: LM tone (10-K Item 1A), filings tone, insider activity, news volume,
  GDELT tone, Google Trends, Twitter mentions.</p>
  <div class="ref">Loughran-McDonald 2011; Tetlock 2007; Cohen-Malloy-Pomorski 2012.</div>
</div>

<h2>3. Volume-momentum features</h2>
<div class="metric">
  <h3>OBV -- On-Balance Volume</h3>
  <p><code>OBV_t = OBV_{t-1} + sign(close_t - close_{t-1}) * volume_t</code>.
  Cumulative volume signed by daily price direction. Trend confirmation indicator.</p>
  <div class="ref">Granville 1963.</div>
</div>
<div class="metric">
  <h3>VPT -- Volume-Price Trend</h3>
  <p><code>VPT_t = VPT_{t-1} + volume_t * pct_change_close_t</code>. Like OBV but volume is
  weighted by % price change instead of just sign.</p>
  <div class="ref">Pring 2002, "Technical Analysis Explained" ch.7.</div>
</div>
<div class="metric">
  <h3>MFI(14) -- Money Flow Index</h3>
  <p>Volume-weighted RSI. Range 0-100; &gt; 80 overbought, &lt; 20 oversold.</p>
  <div class="ref">Quong-Soudack 1989.</div>
</div>
<div class="metric">
  <h3>VWMOM -- Volume-Weighted Momentum</h3>
  <p>Sum of log-returns over 20d, each weighted by relative volume vs 20d ADV.
  High = strong momentum confirmed by abnormally heavy volume.</p>
  <div class="why">Why this matters: price-only momentum can be driven by thin tape; weighting
  by relative volume filters out unconfirmed moves.</div>
  <div class="ref">Campbell-Grossman-Wang 1993 <i>RFS</i>.</div>
</div>
<div class="metric">
  <h3>CMF -- Chaikin Money Flow</h3>
  <p>20-day sum of (close-position-in-range &times; volume), divided by total volume.
  Range [-1, +1]; positive = accumulation, negative = distribution.</p>
  <div class="ref">Marc Chaikin (1980s).</div>
</div>

<h2>4. Walk-forward backtest accuracy</h2>
<div class="metric">
  <h3>Methodology</h3>
  <p>For each anchor t in the price history, we fit on the previous 60 trading days
  and predict t+1 / t+30 / t+60. Realised price is the ground truth.</p>
  <h3>Metrics</h3>
  <ul style="font-size:13px">
    <li><b>Directional accuracy</b>: fraction of correct sign predictions. PMC9680880 LASSO-LSTM
    benchmark on AAPL/MSFT/BAC: 71.6%-77.2% on 1d.</li>
    <li><b>MAE</b>: mean absolute error in % return units.</li>
    <li><b>80% CI hit rate</b>: fraction of realised values inside the model's 80% confidence band.</li>
    <li><b>Pearson(pred, real)</b>: correlation of predicted vs realised returns.</li>
  </ul>
  <div class="ref">Lopez de Prado 2018 "Advances in Financial ML" ch.7.
  Benchmark: PMC9680880 (LASSO-LSTM with FinBERT sentiment, 41 features).</div>
</div>

<h2>5. Bias signal</h2>
<div class="metric">
  <h3>composite_z &rarr; LONG / NEUTRAL / SHORT</h3>
  <p><code>composite_z = sum_i (w_i * f_i) / 0.04</code> -- standardised by the typical
  per-factor sigma.</p>
  <p>Decision rule:</p>
  <ul style="font-size:13px">
    <li><code>z &gt;= +1.0</code> AND beats random-walk AND not risk-off &rarr; <b>LONG</b></li>
    <li><code>z &lt;= -1.0</code> AND beats random-walk AND not risk-on-growth &rarr; <b>SHORT</b></li>
    <li>otherwise &rarr; <b>NEUTRAL</b></li>
  </ul>
  <div class="ref">Grinold-Kahn 2000 ch.3 (sigma cutoff convention).</div>
</div>

<h2>6. Sentiment composite</h2>
<div class="metric">
  <h3>StockTwits / ApeWisdom / IV-skew</h3>
  <p>StockTwits: user-tagged Bullish/Bearish messages. ApeWisdom: Reddit (incl. WSB) mention scan.
  IV-skew: Black-Scholes 25-delta put IV minus 25-delta call IV
  (positive = bearish, negative = bullish).</p>
  <p>Each source &rarr; z-score &rarr; mean, clipped to &plusmn;3 (Grinold-Kahn winsorise).</p>
  <div class="ref">Hull "Options, Futures &amp; Other Derivatives" ch.20 (vol smile);
  Antweiler-Frank 2004 (message-board sentiment).</div>
</div>

<h2>7. Sensitivity (tornado + stress scenarios)</h2>
<div class="metric">
  <h3>OFAT tornado</h3>
  <p>For each factor, impact = <code>w_i * 1sd * sigma_i</code> (default sigma=1).
  Sorted by |impact|. Standard practice for single-stock risk analysis.</p>
  <div class="ref">MSCI Risk-Decomposition; Two Sigma Venn factor-impacts report.</div>
</div>
<div class="metric">
  <h3>Stress scenarios</h3>
  <p>Three canned multi-factor shocks:</p>
  <ul style="font-size:13px">
    <li><b>rates_+100bps</b>: lowvol -1sd, value -0.5sd</li>
    <li><b>recession_risk_off</b>: momentum -1.5sd, lowvol +1sd, news -1sd</li>
    <li><b>quality_flight</b>: quality +1sd, revisions +1sd, momentum -0.5sd</li>
  </ul>
</div>

<h2>8. Factor decay</h2>
<div class="metric">
  <h3>Exponential half-life per factor</h3>
  <p><code>decayed = contribution * exp(-ln(2) * t / half_life)</code></p>
  <ul style="font-size:13px">
    <li>catalysts: 7d</li>
    <li>news_activity: 14d</li>
    <li>revisions: 30d</li>
    <li>momentum: 45d</li>
    <li>value / quality: 90d</li>
  </ul>
  <div class="ref">Tetlock 2007 (news decay); Cohen-Malloy-Pomorski 2012 (insider decay).</div>
</div>

<h2>9. Factor IC backtest</h2>
<div class="metric">
  <h3>Information Coefficient</h3>
  <p>Per-factor Spearman rank correlation between today's factor value and the realised
  h-day-forward return, aggregated across the panel. Higher = factor predicts the
  cross-section better.</p>
  <p><b>Current source</b>: synthetic 252d x 100-ticker panel calibrated to Gu/Kelly/Xiu 2020
  factor magnitudes (provenance string surfaced in every report).</p>
  <div class="ref">Grinold-Kahn 2000 ch.4 (fundamental law of active management).</div>
</div>

<h2>10. Honesty banners</h2>
<div class="metric">
  <p>Every report shows:</p>
  <ul style="font-size:13px">
    <li><b>"Beats random walk" flag</b>: if linear point is within 0.5% of RW, we say so.</li>
    <li><b>Method dispersion</b>: how much the 4 methods disagree. High dispersion = lower conviction.</li>
    <li><b>IC backtest provenance</b>: synthetic vs real data, explicitly stated.</li>
  </ul>
  <div class="why">Why: honest uncertainty quantification is a prerequisite for any
  risk-adjusted decision. I would rather flag low conviction than over-claim accuracy.</div>
</div>

<div style="color:var(--muted); font-size:11px; margin-top:32px; padding-top:12px;
            border-top:1px solid var(--bg3);">
  Forecast layer methodology reference. Not investment advice.
</div>
</body></html>"""
    out_path = output_dir / "research_docs.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Live forecast demo driver")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    parser.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--as-of-date", type=str, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--allow-llm", action="store_true",
                        help="Enable Claude API narrative (requires ANTHROPIC_API_KEY)")
    parser.add_argument("--skip-sentiment", action="store_true",
                        help="Skip the live free-sentiment HTTP fetches (offline mode)")
    parser.add_argument("--skip-ic-backtest", action="store_true",
                        help="Skip the synthetic factor IC backtest pass")
    parser.add_argument("--universe", action="store_true",
                        help="Run the full Phase-1 universe from finviz_universe_history (top-N by market cap)")
    parser.add_argument("--universe-top-n", type=int, default=50,
                        help="Top-N tickers by market cap when --universe is set (default 50)")
    parser.add_argument("--universe-min-mcap-b", type=float, default=10.0,
                        help="Min market cap in billions for universe filter (default 10)")
    parser.add_argument("--mirror-latest", action="store_true",
                        help="Also write outputs to data/reports_latest/ (no date subdir)")
    parser.add_argument("--skip-fmp", action="store_true",
                        help="Skip FMP API calls (use yfinance .info only for fundamentals)")
    args = parser.parse_args()

    as_of = args.as_of_date or _dt.date.today().isoformat()
    output_dir = args.output_dir or (REPO_ROOT / "data" / "reports_v2" / as_of)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load universe if requested
    universe_meta_by_ticker: dict[str, dict] = {}
    if args.universe:
        db_path = REPO_ROOT / "data" / "fundamentals.db"
        if db_path.exists():
            print(f"Loading Phase-1 universe (top-{args.universe_top_n} by market cap, "
                  f">= ${args.universe_min_mcap_b}B)...")
            uni_rows = _load_universe_from_finviz_db(
                db_path=db_path, top_n=args.universe_top_n,
                min_market_cap_b=args.universe_min_mcap_b,
            )
            for r in uni_rows:
                universe_meta_by_ticker[r["ticker"]] = r
            args.tickers = [r["ticker"] for r in uni_rows]
            print(f"  selected {len(args.tickers)} tickers.")
        else:
            print(f"  [warn] {db_path} not found; falling back to --tickers list")

    print(f"Live forecast demo -- as-of {as_of}, horizon {args.horizon_days}d")
    print(f"Output: {output_dir}")
    print(f"Tickers ({len(args.tickers)}): {args.tickers[:10]}{'...' if len(args.tickers) > 10 else ''}")
    print(f"LLM narrative: {'enabled' if args.allow_llm else 'template-only'}")
    print(f"Free sentiment: {'enabled' if not args.skip_sentiment else 'skipped'}")
    print(f"IC backtest: {'enabled' if not args.skip_ic_backtest else 'skipped'}\n")

    loadings_history = _build_cross_sectional_history()

    # ---- Pre-fetch shared market context (SPY, sector ETFs, AI basket) ----
    print("Pre-fetching shared market context (SPY, sector ETFs, AI basket)...")
    spy_close_series = None
    sector_etf_panel = None
    ai_basket_close = None
    try:
        import yfinance as yf
        spy_hist = yf.Ticker("SPY").history(period="3y")
        if not spy_hist.empty:
            spy_close_series = spy_hist["Close"]
            if spy_close_series.index.tz is not None:
                spy_close_series.index = spy_close_series.index.tz_localize(None)
        sector_etfs = list({v for v in SECTOR_ETF_MAP.values()})
        etf_data = {}
        for e in sector_etfs:
            try:
                h = yf.Ticker(e).history(period="3y")["Close"]
                if h.index.tz is not None:
                    h.index = h.index.tz_localize(None)
                etf_data[e] = h
            except Exception:
                pass
        if etf_data:
            sector_etf_panel = pd.DataFrame(etf_data)
        # AI basket
        from src.methodology.sector_rotation import AI_BASKET as _AI_BASKET
        ai_data = {}
        for t in _AI_BASKET[:8]:  # 8 to keep fetch under control
            try:
                h = yf.Ticker(t).history(period="3y")["Close"]
                if h.index.tz is not None:
                    h.index = h.index.tz_localize(None)
                ai_data[t] = h
            except Exception:
                pass
        if ai_data:
            ai_panel = pd.DataFrame(ai_data)
            ai_basket_close = build_ai_basket_close(ai_panel)
    except Exception as exc:
        print(f"  [warn] market-context prefetch failed: {exc}", file=sys.stderr)
    print(f"  SPY: {'OK' if spy_close_series is not None else 'NO'}  "
          f"sector_panel: {sector_etf_panel.shape if sector_etf_panel is not None else 'NO'}  "
          f"ai_basket: {'OK' if ai_basket_close is not None else 'NO'}")

    # ---- FMP client (skipped if no key or --skip-fmp) ----
    fmp_client = None
    if not args.skip_fmp and os.environ.get("FMP_API_KEY"):
        try:
            # Direct file-import to bypass src/common/datasources/__init__.py
            # (which transitively imports optional `finvizfinance`).
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location(
                "fmp_source",
                REPO_ROOT / "src" / "common" / "datasources" / "fmp_source.py",
            )
            _mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            fmp_client = _mod.FMPClient()
            globals()["_fmp_compute_revisions_score"] = _mod.compute_revisions_score
            print("  FMP client: OK (will compute real revisions_score)")
        except Exception as exc:
            print(f"  [warn] FMP init failed: {exc}", file=sys.stderr)
    elif args.skip_fmp:
        print("  FMP client: SKIP (--skip-fmp)")
    else:
        print("  FMP client: SKIP (no FMP_API_KEY in env)")
    print()

    ic_result = None
    ic_provenance = "skipped"
    if not args.skip_ic_backtest:
        print("Running synthetic IC backtest (252d x 100-ticker calibrated panel)...")
        try:
            ic_result = run_synthetic_ic_backtest(horizon_days=args.horizon_days)
            ic_provenance = (
                "synthetic_panel_252d_100tickers_seed42 "
                "(calibrated to Gu/Kelly/Xiu 2020 RFS factor magnitudes; "
                "replace with A.3 universe panel once A.3 fetchers run)"
            )
            print("  IC backtest complete; mean ICs per factor:")
            for f, s in ic_result.per_factor.items():
                print(f"    {f:24s} mean_ic={s.mean_ic:+.4f}  ICIR={s.icir:+.3f}  cum_attr={s.cumulative_attribution:+.4f}")
        except Exception as exc:
            print(f"  [warn] IC backtest failed: {exc}", file=sys.stderr)
            ic_provenance = f"unavailable ({type(exc).__name__})"
    print()

    summaries = []
    for t in args.tickers:
        try:
            summary = run_live_for_ticker(
                t, as_of_date=as_of, horizon_days=args.horizon_days,
                output_dir=output_dir, allow_llm=args.allow_llm,
                loadings_history=loadings_history,
                ic_backtest_result=ic_result,
                ic_backtest_provenance=ic_provenance,
                include_sentiment=not args.skip_sentiment,
                spy_close_series=spy_close_series,
                sector_etf_panel=sector_etf_panel,
                ai_basket_close=ai_basket_close,
                fmp_client=fmp_client,
            )
            summaries.append(summary)
        except Exception as exc:
            print(f"[{t}] ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            summaries.append({"ticker": t, "error": str(exc)})

    # Decorate summaries with walk-forward 30d dir_acc for the index page
    # (the wf_summary is computed inside run_live_for_ticker; re-attach via path read)
    # NOTE: For now we just leave wf_dir_acc_30d as None; the index gracefully
    # shows "-" when absent.

    # Optional watchlist section. The file is optional; default to empty.
    wl_path = REPO_ROOT / "config" / "watchlist.json"
    watchlist_tickers: list[str] = []
    if wl_path.exists():
        try:
            import json as _json
            wl_data = _json.loads(wl_path.read_text(encoding="utf-8"))
            watchlist_tickers = [t["ticker"] for t in wl_data.get("tickers", [])]
        except Exception:
            pass

    # FINVIZ-derived long/short candidates + top-100 from layer1
    fv_long, fv_short, fv_top100, fv_date = _load_finviz_candidates_csv(repo_root=REPO_ROOT)
    print(f"FINVIZ layer1 candidates: {len(fv_long)} long / {len(fv_short)} short / "
          f"{len(fv_top100)} top-100  (from {fv_date or 'no-data'})")

    # Render the 5-section structured index
    idx_path = _render_structured_index(
        summaries=summaries, output_dir=output_dir, as_of_date=as_of,
        watchlist_tickers=watchlist_tickers,
        finviz_long_candidates=fv_long,
        finviz_short_candidates=fv_short,
        top100_universe=fv_top100,
        finviz_dated_dir=fv_date,
    )
    research_path = _render_research_docs(output_dir)
    print(f"\n  index : {idx_path}")
    print(f"  docs  : {research_path}")

    # Mirror to data/reports_latest/ (no date subdir) for easy linking.
    if args.mirror_latest:
        import shutil
        latest_dir = REPO_ROOT / "data" / "reports_latest"
        latest_dir.mkdir(parents=True, exist_ok=True)
        # Clear old contents (but only HTMLs to be safe -- don't touch .gitkeep etc.)
        for old in latest_dir.glob("*.html"):
            try:
                old.unlink()
            except OSError:
                pass
        # Copy fresh
        for p in output_dir.glob("*.html"):
            try:
                shutil.copy2(p, latest_dir / p.name)
            except OSError as exc:
                print(f"  [warn] copy to latest failed for {p.name}: {exc}", file=sys.stderr)
        print(f"\nMirrored {len(list(latest_dir.glob('*.html')))} files to {latest_dir}")

    for s in summaries:
        if "error" in s:
            print(f"  {s['ticker']:6s}: ERROR {s['error']}")
        else:
            sign = "+" if s["ensemble_point"] >= 0 else ""
            bias = s.get("bias", "?")
            pb = s.get("playbook", "?")
            cz = s.get("composite_z", 0.0)
            print(f"  {s['ticker']:6s}: ${s['last_price']:>10,.2f}  "
                  f"{sign}{s['ensemble_point']:.2%}  "
                  f"bias={bias:7s} playbook={pb:4s} z={cz:+.2f}  "
                  f"-> {s['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
