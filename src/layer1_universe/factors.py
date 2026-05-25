from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

_DEFAULT_WEIGHTS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "scoring.yaml"


def _safe_div(a, b):
    """Division returning NaN where denominator is zero, NaN, or inputs are NaN."""
    a_arr = np.asarray(a, dtype=float)
    b_arr = np.asarray(b, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(
            (b_arr == 0) | np.isnan(a_arr) | np.isnan(b_arr),
            np.nan,
            a_arr / np.where(b_arr == 0, 1, b_arr),
        )
    if np.ndim(out) == 0:
        return float(out)
    if isinstance(a, pd.Series):
        return pd.Series(out, index=a.index)
    if isinstance(b, pd.Series):
        return pd.Series(out, index=b.index)
    return out


def winsorize(series: pd.Series, lower_pct: float = 0.01, upper_pct: float = 0.99) -> pd.Series:
    """Clip series at the given lower/upper quantiles, preserving NaNs."""
    s = pd.Series(series, copy=True).astype(float)
    if s.dropna().empty:
        return s
    lo = s.quantile(lower_pct)
    hi = s.quantile(upper_pct)
    return s.clip(lower=lo, upper=hi)


def z_score(series: pd.Series) -> pd.Series:
    """Cross-sectional z-score with ddof=0; returns NaN if std is zero or all NaN."""
    s = pd.Series(series, copy=True).astype(float)
    if s.dropna().empty:
        return pd.Series(np.nan, index=s.index)
    mean = s.mean(skipna=True)
    std = s.std(ddof=0, skipna=True)
    if std == 0 or pd.isna(std):
        return pd.Series(np.nan, index=s.index)
    return (s - mean) / std


def sector_z_score(df: pd.DataFrame, value_col: str, sector_col: str = "sector") -> pd.Series:
    """Z-score `value_col` within each `sector_col` group; singletons -> NaN."""
    out = pd.Series(np.nan, index=df.index, dtype=float)
    if value_col not in df.columns or sector_col not in df.columns:
        return out
    for _, idx in df.groupby(sector_col, dropna=False).groups.items():
        sub = df.loc[idx, value_col].astype(float)
        if sub.dropna().shape[0] < 2:
            out.loc[idx] = np.nan
            continue
        out.loc[idx] = z_score(sub).values
    return out


def compute_value_score(df: pd.DataFrame, weights: dict | None = None) -> pd.Series:
    """Cross-sectional value composite from earnings_yield, ebit_yield, fcf_yield, book_to_market (expects: pe_ratio, ebit_ttm, market_cap_usd, total_debt, cash, ocf_ttm, capex_ttm, book_value, sector)."""
    if weights is None:
        try:
            cfg = load_scoring_weights()
            weights = cfg.get("value_subweights", {})
        except Exception:
            weights = {}
    w_ey = float(weights.get("earnings_yield", 0.25))
    w_ebit = float(weights.get("ebit_yield", 0.25))
    w_fcf = float(weights.get("fcf_yield", 0.25))
    w_bm = float(weights.get("book_to_market", 0.25))

    work = df.copy()

    pe = work.get("pe_ratio", pd.Series(np.nan, index=work.index)).astype(float)
    ey = pd.Series(np.where((pe > 0) & pe.notna(), 1.0 / pe.replace(0, np.nan), np.nan), index=work.index)

    mc = work.get("market_cap_usd", pd.Series(np.nan, index=work.index)).astype(float)
    td = work.get("total_debt", pd.Series(np.nan, index=work.index)).astype(float)
    cash = work.get("cash", pd.Series(np.nan, index=work.index)).astype(float)
    ebit = work.get("ebit_ttm", pd.Series(np.nan, index=work.index)).astype(float)
    ev = mc + td - cash
    ebit_y = _safe_div(ebit, ev)
    if isinstance(ebit_y, pd.Series):
        ebit_y = ebit_y.reindex(work.index)
    else:
        ebit_y = pd.Series(ebit_y, index=work.index)

    ocf = work.get("ocf_ttm", pd.Series(np.nan, index=work.index)).astype(float)
    capex = work.get("capex_ttm", pd.Series(np.nan, index=work.index)).astype(float)
    fcf_y = _safe_div(ocf - capex, mc)
    if isinstance(fcf_y, pd.Series):
        fcf_y = fcf_y.reindex(work.index)
    else:
        fcf_y = pd.Series(fcf_y, index=work.index)

    bv = work.get("book_value", pd.Series(np.nan, index=work.index)).astype(float)
    bm = _safe_div(bv, mc)
    if isinstance(bm, pd.Series):
        bm = bm.reindex(work.index)
    else:
        bm = pd.Series(bm, index=work.index)

    work["_ey"] = winsorize(ey)
    work["_ebit_y"] = winsorize(ebit_y)
    work["_fcf_y"] = winsorize(fcf_y)
    work["_bm"] = winsorize(bm)

    if "sector" not in work.columns:
        work["sector"] = "UNKNOWN"

    ey_z = sector_z_score(work, "_ey")
    ebit_z = sector_z_score(work, "_ebit_y")
    fcf_z = sector_z_score(work, "_fcf_y")
    bm_z = sector_z_score(work, "_bm")

    score = w_ey * ey_z + w_ebit * ebit_z + w_fcf * fcf_z + w_bm * bm_z
    return score


def historical_value_percentile(pe_series: pd.Series, lookback_years: int = 5) -> float:
    """Fraction of historical PE values strictly below the current (last) PE."""
    s = pd.Series(pe_series).astype(float).dropna()
    min_obs = max(2, int(lookback_years * 252 * 0.5))
    if len(s) < min_obs:
        return float("nan")
    current = s.iloc[-1]
    if pd.isna(current):
        return float("nan")
    return float((s < current).sum()) / float(len(s))


def compute_altman_z(df: pd.DataFrame) -> pd.Series:
    """Altman Z-score: 1.2A + 1.4B + 3.3C + 0.6D + 1.0E (expects working_capital, retained_earnings, ebit_ttm, market_cap_usd, total_liabilities, revenue, total_assets)."""
    ta = df.get("total_assets", pd.Series(np.nan, index=df.index)).astype(float)
    wc = df.get("working_capital", pd.Series(np.nan, index=df.index)).astype(float)
    re = df.get("retained_earnings", pd.Series(np.nan, index=df.index)).astype(float)
    ebit = df.get("ebit_ttm", pd.Series(np.nan, index=df.index)).astype(float)
    mc = df.get("market_cap_usd", pd.Series(np.nan, index=df.index)).astype(float)
    tl = df.get("total_liabilities", pd.Series(np.nan, index=df.index)).astype(float)
    rev = df.get("revenue", pd.Series(np.nan, index=df.index)).astype(float)

    A = _safe_div(wc, ta)
    B = _safe_div(re, ta)
    C = _safe_div(ebit, ta)
    D = _safe_div(mc, tl)
    E = _safe_div(rev, ta)

    A = A if isinstance(A, pd.Series) else pd.Series(A, index=df.index)
    B = B if isinstance(B, pd.Series) else pd.Series(B, index=df.index)
    C = C if isinstance(C, pd.Series) else pd.Series(C, index=df.index)
    D = D if isinstance(D, pd.Series) else pd.Series(D, index=df.index)
    E = E if isinstance(E, pd.Series) else pd.Series(E, index=df.index)

    return 1.2 * A + 1.4 * B + 3.3 * C + 0.6 * D + 1.0 * E


def compute_quality_score(df: pd.DataFrame, weights: dict | None = None) -> pd.Series:
    """Pillar-weighted quality score (0.40 profitability + 0.25 growth + 0.20 safety + 0.15 capital) over enriched fundamental columns."""
    if weights is None:
        try:
            cfg = load_scoring_weights()
            weights = cfg.get("quality_pillars", {})
        except Exception:
            weights = {}
    w_prof = float(weights.get("profitability", 0.40))
    w_grow = float(weights.get("growth", 0.25))
    w_safe = float(weights.get("safety", 0.20))
    w_cap = float(weights.get("capital", 0.15))

    work = df.copy()
    if "sector" not in work.columns:
        work["sector"] = "UNKNOWN"

    def _sz(col: str) -> pd.Series:
        if col in work.columns:
            work[f"_w_{col}"] = winsorize(work[col].astype(float))
            return sector_z_score(work, f"_w_{col}").fillna(0.0)
        return pd.Series(0.0, index=work.index)

    def _z(series: pd.Series) -> pd.Series:
        return z_score(winsorize(series.astype(float))).fillna(0.0)

    prof = (_sz("roe") + _sz("operating_margin") + _sz("net_profit_margin")) / 3.0

    rev_yoy = work.get("revenue_yoy", pd.Series(np.nan, index=work.index)).astype(float)
    eps_yoy = work.get("eps_yoy", pd.Series(np.nan, index=work.index)).astype(float)
    grow = (_z(rev_yoy) + _z(eps_yoy)) / 2.0

    dte = work.get("debt_to_equity", pd.Series(np.nan, index=work.index)).astype(float)
    evol = work.get("earnings_volatility", pd.Series(np.nan, index=work.index)).astype(float)
    altman = compute_altman_z(work)
    safety = (_z(-dte) + _z(-evol) + _z(altman)) / 3.0

    sc_yoy = work.get("shares_change_yoy", pd.Series(np.nan, index=work.index)).astype(float)
    mc = work.get("market_cap_usd", pd.Series(np.nan, index=work.index)).astype(float)
    ocf = work.get("ocf_ttm", pd.Series(np.nan, index=work.index)).astype(float)
    capex = work.get("capex_ttm", pd.Series(np.nan, index=work.index)).astype(float)
    fcf_y_raw = _safe_div(ocf - capex, mc)
    fcf_y = fcf_y_raw if isinstance(fcf_y_raw, pd.Series) else pd.Series(fcf_y_raw, index=work.index)
    cap = (_z(-sc_yoy) + _z(fcf_y)) / 2.0

    return w_prof * prof + w_grow * grow + w_safe * safety + w_cap * cap


def cross_sectional_momentum_days(prices: pd.Series, lookback_days: int, skip_days: int) -> float:
    """Return price momentum: P(t-skip)/P(t-skip-lookback) - 1, NaN if insufficient history."""
    s = pd.Series(prices).astype(float).dropna()
    needed = lookback_days + skip_days + 1
    if len(s) < needed:
        return float("nan")
    end_price = s.iloc[-1 - skip_days]
    start_price = s.iloc[-1 - skip_days - lookback_days]
    if pd.isna(end_price) or pd.isna(start_price) or start_price == 0:
        return float("nan")
    return float(end_price / start_price - 1.0)


def compute_momentum_score(prices_df: pd.DataFrame, weights: dict | None = None) -> pd.Series:
    """Weighted cross-sectional momentum from 3m/6m/12m windows on a date-indexed price panel."""
    if weights is None:
        try:
            cfg = load_scoring_weights()
            weights = cfg.get("momentum_horizons", {})
        except Exception:
            weights = {}
    w3 = float(weights.get("mom_3m", 1.0 / 3.0))
    w6 = float(weights.get("mom_6m", 1.0 / 3.0))
    w12 = float(weights.get("mom_12m", 1.0 / 3.0))

    tickers = list(prices_df.columns)
    mom3 = pd.Series({t: cross_sectional_momentum_days(prices_df[t], 63, 1) for t in tickers})
    mom6 = pd.Series({t: cross_sectional_momentum_days(prices_df[t], 126, 21) for t in tickers})
    mom12 = pd.Series({t: cross_sectional_momentum_days(prices_df[t], 252, 21) for t in tickers})

    z3 = z_score(winsorize(mom3))
    z6 = z_score(winsorize(mom6))
    z12 = z_score(winsorize(mom12))

    return w3 * z3 + w6 * z6 + w12 * z12


def compute_residual_momentum(stock_returns: pd.Series, market_returns: pd.Series, lookback_months: int = 12) -> float:
    """OLS residual cumulative momentum of stock vs market over lookback_months*21d, skipping last 21d."""
    import statsmodels.api as sm

    s = pd.Series(stock_returns).astype(float)
    m = pd.Series(market_returns).astype(float)
    joined = pd.concat([s.rename("s"), m.rename("m")], axis=1).dropna()
    window = lookback_months * 21
    if len(joined) < window + 1:
        return float("nan")
    win = joined.iloc[-window:]
    X = sm.add_constant(win["m"].values)
    y = win["s"].values
    try:
        model = sm.OLS(y, X).fit()
    except Exception:
        return float("nan")
    resid = pd.Series(model.resid, index=win.index)
    if len(resid) <= 21:
        return float("nan")
    cum = resid.iloc[:-21]
    return float((1.0 + cum).prod() - 1.0)


def realized_vol(prices: pd.Series, lookback_days: int = 252) -> float:
    """Annualized stdev of daily log returns over the trailing window."""
    s = pd.Series(prices).astype(float).dropna()
    if len(s) < 2:
        return float("nan")
    s = s.iloc[-(lookback_days + 1):]
    log_ret = np.log(s / s.shift(1)).dropna()
    if log_ret.empty:
        return float("nan")
    return float(log_ret.std(ddof=0) * np.sqrt(252))


def beta(stock_returns: pd.Series, market_returns: pd.Series, lookback_days: int = 252) -> float:
    """Cov(stock, market) / Var(market) over the trailing window."""
    s = pd.Series(stock_returns).astype(float)
    m = pd.Series(market_returns).astype(float)
    joined = pd.concat([s.rename("s"), m.rename("m")], axis=1).dropna()
    if len(joined) < 2:
        return float("nan")
    joined = joined.iloc[-lookback_days:]
    var_m = joined["m"].var(ddof=0)
    if var_m == 0 or pd.isna(var_m):
        return float("nan")
    cov = joined["s"].cov(joined["m"])
    return float(cov / var_m)


def idiosyncratic_vol(stock_returns: pd.Series, market_returns: pd.Series, lookback_days: int = 252) -> float:
    """Annualized stdev of OLS residuals from regressing stock on market."""
    import statsmodels.api as sm

    s = pd.Series(stock_returns).astype(float)
    m = pd.Series(market_returns).astype(float)
    joined = pd.concat([s.rename("s"), m.rename("m")], axis=1).dropna()
    if len(joined) < 3:
        return float("nan")
    joined = joined.iloc[-lookback_days:]
    X = sm.add_constant(joined["m"].values)
    try:
        model = sm.OLS(joined["s"].values, X).fit()
    except Exception:
        return float("nan")
    resid = pd.Series(model.resid)
    if resid.empty:
        return float("nan")
    return float(resid.std(ddof=0) * np.sqrt(252))


def compute_lowvol_score(
    prices_df: pd.DataFrame,
    market_returns: pd.Series | None = None,
    weights: dict | None = None,
    *,
    ohlcv_by_ticker: dict[str, pd.DataFrame] | None = None,
    vol_window_days: int = 60,
) -> pd.Series:
    """Cross-sectional low-vol composite: -0.40 rv_z - 0.25 beta_z - 0.35 idio_z.

    Realized-vol estimator (`rv`):
      * If `ohlcv_by_ticker` is provided AND a ticker has a Yang-Zhang
        result with non-NaN trailing-window value, use that (spec §8
        per A.3.10).
      * Otherwise fall back to close-to-close `realized_vol` on
        `prices_df[ticker]` (the original behaviour). This keeps the
        existing notebook callers working unchanged while opt-in YZ
        takes effect when OHLCV is plumbed through.

    `ohlcv_by_ticker` is `{ticker: DataFrame with Open/High/Low/Close,
    indexed by date}`. The latest non-NaN YZ vol from
    `yang_zhang_vol(ohlc, window_days=vol_window_days)` is taken per
    ticker.
    """
    tickers = list(prices_df.columns)

    # 1) Realized-vol per ticker. Yang-Zhang first, close-to-close fallback.
    rv_values: dict[str, float] = {}
    if ohlcv_by_ticker:
        from src.methodology.yang_zhang_vol import yang_zhang_vol
        for t in tickers:
            ohlc = ohlcv_by_ticker.get(t)
            yz_val = np.nan
            if ohlc is not None and len(ohlc) >= vol_window_days:
                try:
                    yz_series = yang_zhang_vol(ohlc, window_days=vol_window_days)
                    last_valid = yz_series.dropna()
                    if len(last_valid) > 0:
                        yz_val = float(last_valid.iloc[-1])
                except Exception:
                    yz_val = np.nan
            if np.isfinite(yz_val):
                rv_values[t] = yz_val
            else:
                rv_values[t] = realized_vol(prices_df[t])
    else:
        for t in tickers:
            rv_values[t] = realized_vol(prices_df[t])
    rv = pd.Series(rv_values)

    if market_returns is not None:
        mret = pd.Series(market_returns).astype(float)
        rets = prices_df.apply(lambda c: np.log(c / c.shift(1)))
        betas = pd.Series({t: beta(rets[t], mret) for t in tickers})
        idio = pd.Series({t: idiosyncratic_vol(rets[t], mret) for t in tickers})
    else:
        betas = pd.Series({t: np.nan for t in tickers})
        idio = pd.Series({t: np.nan for t in tickers})

    rv_z = z_score(winsorize(rv))
    beta_z = z_score(winsorize(betas))
    idio_z = z_score(winsorize(idio))

    score = -0.40 * rv_z - 0.25 * beta_z
    if market_returns is not None:
        score = score - 0.35 * idio_z
    return score


def revision_score(df: pd.DataFrame) -> pd.Series:
    """0.50 * z(winsorize(revision_magnitude)) + 0.50 * z(revision_breadth)."""
    mag = df.get("revision_magnitude", pd.Series(np.nan, index=df.index)).astype(float)
    brd = df.get("revision_breadth", pd.Series(np.nan, index=df.index)).astype(float)
    mag_z = z_score(winsorize(mag))
    brd_z = z_score(brd)
    return 0.50 * mag_z + 0.50 * brd_z


def earnings_surprise(actual: float, estimate: float) -> float:
    """(actual - estimate) / abs(estimate); 0.0 if estimate == 0."""
    if estimate is None or pd.isna(estimate) or estimate == 0:
        return 0.0
    if actual is None or pd.isna(actual):
        return float("nan")
    return float((actual - estimate) / abs(estimate))


def earnings_momentum_revisions(df: pd.DataFrame) -> pd.Series:
    """0.60 * revision_score + 0.40 * z(earnings_surprise); NaN revisions treated as 0."""
    rev = revision_score(df).fillna(0.0)
    if "earnings_surprise" in df.columns:
        sup = df["earnings_surprise"].astype(float)
    elif {"actual_eps", "estimate_eps"}.issubset(df.columns):
        sup = pd.Series(
            [earnings_surprise(a, e) for a, e in zip(df["actual_eps"], df["estimate_eps"])],
            index=df.index,
            dtype=float,
        )
    else:
        sup = pd.Series(np.nan, index=df.index, dtype=float)
    sup_z = z_score(winsorize(sup)).fillna(0.0)
    return 0.60 * rev + 0.40 * sup_z


def sentiment_score(df: pd.DataFrame, playbook: str) -> pd.Series:
    """Long/short-bias sentiment composite over short_interest, put_call_skew, analyst_dispersion.

    `playbook` is the screen identifier ("A" = long-bias, "B" = short / mean-reversion).
    """
    si = df.get("short_interest", pd.Series(np.nan, index=df.index)).astype(float)
    pcs = df.get("put_call_skew", pd.Series(np.nan, index=df.index)).astype(float)
    ad = df.get("analyst_dispersion", pd.Series(np.nan, index=df.index)).astype(float)

    si_z = z_score(winsorize(si)).fillna(0.0)
    pcs_z = z_score(winsorize(pcs)).fillna(0.0)
    ad_z = z_score(winsorize(ad)).fillna(0.0)

    if str(playbook).upper() == "B":
        return 0.35 * (si_z) + 0.30 * (-pcs_z) + 0.35 * ad_z
    return 0.35 * (-si_z) + 0.30 * pcs_z + 0.35 * ad_z


def z_to_score(z: float, max_pts: float = 5.0) -> float:
    """Clip z to [-2, 2] then linearly map to [0, max_pts]."""
    if z is None or pd.isna(z):
        return float("nan")
    clipped = max(-2.0, min(2.0, float(z)))
    return float((clipped + 2.0) / 4.0 * max_pts)


def compute_directional_thesis_score(df: pd.DataFrame, playbook: str) -> pd.Series:
    """Sum of four 0..5 sub-scores: valuation_history, earnings_momentum_revisions, technical_setup (0), sentiment."""
    pb = str(playbook).upper()

    vh = df.get("valuation_history_pct", pd.Series(np.nan, index=df.index)).astype(float)
    if pb == "B":
        vh_z = z_score(vh)
    else:
        vh_z = z_score(-vh)
    vh_score = vh_z.apply(z_to_score)

    emr = earnings_momentum_revisions(df)
    emr_score = emr.apply(z_to_score)

    tech_score = pd.Series(0.0, index=df.index, dtype=float)

    sent = sentiment_score(df, playbook)
    sent_score = sent.apply(z_to_score)

    total = vh_score.fillna(0.0) + emr_score.fillna(0.0) + tech_score + sent_score.fillna(0.0)
    return total


def compute_composite_score(factor_scores_df: pd.DataFrame, weights: dict) -> pd.Series:
    """Weighted average across factor columns, re-normalizing weights per row to skip NaNs."""
    cols = [c for c in factor_scores_df.columns if c in weights]
    if not cols:
        return pd.Series(np.nan, index=factor_scores_df.index, dtype=float)
    w = np.array([float(weights[c]) for c in cols], dtype=float)
    vals = factor_scores_df[cols].astype(float).values
    mask = ~np.isnan(vals)
    w_mat = np.where(mask, w[np.newaxis, :], 0.0)
    v_mat = np.where(mask, vals, 0.0)
    num = (w_mat * v_mat).sum(axis=1)
    den = w_mat.sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(den == 0, np.nan, num / np.where(den == 0, 1, den))
    return pd.Series(out, index=factor_scores_df.index, dtype=float)


def load_scoring_weights(path: str | Path = "config/scoring.yaml") -> dict:
    """Load the scoring weights YAML file into a dict."""
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = _DEFAULT_WEIGHTS_PATH
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# A.3.10: top-level factor pipeline entry
# ---------------------------------------------------------------------------


def compute_factor_scores(
    canonical_df: pd.DataFrame,
    *,
    raw_inputs: dict | None = None,
    weights: dict | None = None,
    prices_df: pd.DataFrame | None = None,
    market_returns: pd.Series | None = None,
    ohlcv_by_ticker: dict[str, pd.DataFrame] | None = None,
    as_of_date: str | None = None,
) -> pd.DataFrame:
    """A.3.10 top-level factor pipeline.

    Consumes the v2 canonical universe + the raw_* sub-signal tables
    and returns a per-ticker DataFrame with the six factor sub-scores
    PLUS the new `news_activity_score` column from spec §7. Designed to
    be called once per Layer 1 run; not optimised for repeated calls
    (no caching).

    Parameters
    ----------
    canonical_df
        Per-ticker wide frame indexed by ticker (or with a 'ticker'
        column). Must carry the columns the existing compute_*_score
        functions expect (earnings_yield, ebit_yield, fcf_yield,
        book_to_market for value; operating_margin, net_profit_margin,
        revenue_growth_yoy, eps_growth_yoy, etc. for quality).
    raw_inputs
        Dict from `load_news_activity_inputs(db, as_of_date)`. Pass
        `{}` (or omit) to skip the news_activity column entirely --
        the column is then NaN for every ticker.
    weights
        Scoring config (output of `load_scoring_weights`). Default
        loads from `config/scoring.yaml`.
    prices_df
        Close-price DataFrame (columns = tickers) for the momentum +
        lowvol legs. Required for those columns to be non-NaN.
    market_returns
        Market index returns aligned with prices_df. Required for the
        idio_vol component of compute_lowvol_score.
    ohlcv_by_ticker
        Per-ticker OHLCV frames used by compute_lowvol_score for
        Yang-Zhang vol estimation. Optional; if omitted, lowvol falls
        back to close-to-close.
    as_of_date
        Required when raw_inputs is non-empty (news_activity needs an
        anchor date). Format 'YYYY-MM-DD'.

    Returns
    -------
    pd.DataFrame
        Indexed by ticker. Columns:
          - value_score, quality_score, momentum_score, lowvol_score,
            revisions_score (existing factors)
          - news_activity_score (A.3.10 addition; NaN when raw_inputs
            is empty or as_of_date is None)

    Notes
    -----
    This is the v2 entry point. Existing per-function callers (e.g.,
    notebook drivers) keep working unchanged. The new entry simplifies
    the integration test and the dual-write pipeline scheduled for
    Wave 4.
    """
    if weights is None:
        weights = load_scoring_weights()

    # Resolve ticker index. canonical_df may use ticker as a column or
    # as the index; normalise to indexed-by-ticker for downstream.
    df = canonical_df.copy()
    if "ticker" in df.columns:
        df = df.set_index("ticker", drop=False)
    tickers = list(df.index)

    factor_df = pd.DataFrame(index=tickers)

    # 1) Value (from canonical fundamentals)
    try:
        factor_df["value_score"] = compute_value_score(df, weights.get("value_subweights"))
    except Exception:
        factor_df["value_score"] = float("nan")

    # 2) Quality
    try:
        factor_df["quality_score"] = compute_quality_score(df, weights.get("quality_pillars"))
    except Exception:
        factor_df["quality_score"] = float("nan")

    # 3) Momentum + lowvol require prices.
    if prices_df is not None and len(prices_df.columns) > 0:
        try:
            factor_df["momentum_score"] = compute_momentum_score(
                prices_df, weights.get("momentum_horizons")
            )
        except Exception:
            factor_df["momentum_score"] = float("nan")
        try:
            factor_df["lowvol_score"] = compute_lowvol_score(
                prices_df,
                market_returns=market_returns,
                weights=weights.get("lowvol_subweights"),
                ohlcv_by_ticker=ohlcv_by_ticker,
                vol_window_days=int(
                    (weights.get("realized_vol") or {}).get("window_days", 60)
                ),
            )
        except Exception:
            factor_df["lowvol_score"] = float("nan")
    else:
        factor_df["momentum_score"] = float("nan")
        factor_df["lowvol_score"] = float("nan")

    # 4) Revisions (from canonical fields if present)
    try:
        factor_df["revisions_score"] = revision_score(df)
    except Exception:
        factor_df["revisions_score"] = float("nan")

    # 5) news_activity_score (A.3.10)
    if raw_inputs and as_of_date:
        from src.layer1_universe.news_activity import compute_news_activity_score
        subweights = (weights or {}).get("news_activity_subweights")
        na = compute_news_activity_score(
            tickers=tickers,
            as_of_date=as_of_date,
            subweights=subweights,
            **{k: raw_inputs.get(k) for k in (
                "insider_rows", "short_interest_rows", "revisions_rows",
                "news_volume_rows", "filing_rows", "tone_rows", "fears_rows",
            )},
        )
        factor_df["news_activity_score"] = pd.Series(na)
    else:
        factor_df["news_activity_score"] = float("nan")

    return factor_df
