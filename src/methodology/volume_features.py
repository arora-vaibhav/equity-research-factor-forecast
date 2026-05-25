"""Volume-momentum features for forecast accuracy.

I added these because volume information was previously under-weighted
relative to its documented predictive content on price momentum.

Implements the canonical volume-price momentum indicators that have
material predictive content per the literature:

  * OBV  -- On-Balance Volume (Granville 1963). Cumulative volume signed
    by daily close direction; widely cited as the simplest volume-flow
    indicator.
  * VPT  -- Volume Price Trend. Like OBV but volume weighted by % price
    change instead of just sign.
  * MFI  -- Money Flow Index (Quong & Soudack 1989). 14-period
    volume-weighted RSI. Range 0-100; > 80 overbought, < 20 oversold.
  * VWMOM -- Volume-Weighted Momentum (Campbell, Grossman & Wang 1993
    RFS "Trading Volume and Serial Correlation in Stock Returns" --
    price moves on high relative volume are more informative).
  * VOL_Z -- Volume z-score over a rolling window (anomaly detector).
  * REL_VOL -- Relative volume vs 20-day ADV.
  * CMF -- Chaikin Money Flow (Marc Chaikin 1980s).

All functions are pure; take pandas DataFrames with OHLCV columns and
return a single pandas Series indexed by date.

Pure function module; no DB.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


VOLUME_FEATURES_VERSION = "1.0"


def _need(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV frame missing columns: {missing}")


def on_balance_volume(ohlcv: pd.DataFrame) -> pd.Series:
    """OBV per Granville 1963.

    OBV_t = OBV_{t-1} + sign(close_t - close_{t-1}) * volume_t
    """
    _need(ohlcv, ["close", "volume"])
    df = ohlcv[["close", "volume"]].copy()
    delta = df["close"].diff()
    sign = np.where(delta > 0, 1.0, np.where(delta < 0, -1.0, 0.0))
    obv = (sign * df["volume"].to_numpy()).cumsum()
    return pd.Series(obv, index=df.index, name="obv")


def volume_price_trend(ohlcv: pd.DataFrame) -> pd.Series:
    """VPT (cumulative): VPT_t = VPT_{t-1} + volume_t * pct_change_close_t."""
    _need(ohlcv, ["close", "volume"])
    pct = ohlcv["close"].pct_change().fillna(0.0)
    vpt = (pct * ohlcv["volume"]).cumsum()
    return pd.Series(vpt.values, index=ohlcv.index, name="vpt")


def money_flow_index(ohlcv: pd.DataFrame, *, window: int = 14) -> pd.Series:
    """MFI(14) per Quong & Soudack 1989."""
    _need(ohlcv, ["high", "low", "close", "volume"])
    tp = (ohlcv["high"] + ohlcv["low"] + ohlcv["close"]) / 3.0
    rmf = tp * ohlcv["volume"]
    delta = tp.diff()
    pos_flow = rmf.where(delta > 0, 0.0)
    neg_flow = rmf.where(delta < 0, 0.0)
    pos_sum = pos_flow.rolling(window=window, min_periods=window).sum()
    neg_sum = neg_flow.rolling(window=window, min_periods=window).sum()
    mfr = pos_sum / neg_sum.replace(0.0, np.nan)
    mfi = 100.0 - (100.0 / (1.0 + mfr))
    return mfi.rename("mfi")


def volume_weighted_momentum(
    ohlcv: pd.DataFrame, *, lookback: int = 20,
) -> pd.Series:
    """Volume-confirmed momentum (Campbell-Grossman-Wang 1993 idea).

    Weight each daily log-return by relative volume (volume / 20d ADV),
    then sum over the lookback window. High readings = strong momentum
    confirmed by abnormally heavy volume.
    """
    _need(ohlcv, ["close", "volume"])
    log_ret = np.log(ohlcv["close"] / ohlcv["close"].shift(1))
    adv20 = ohlcv["volume"].rolling(window=20, min_periods=5).mean()
    rel_vol = ohlcv["volume"] / adv20.replace(0.0, np.nan)
    weighted = log_ret * rel_vol
    vwmom = weighted.rolling(window=lookback, min_periods=5).sum()
    return vwmom.rename("vwmom")


def volume_zscore(ohlcv: pd.DataFrame, *, window: int = 60) -> pd.Series:
    """z-score of today's volume vs the rolling-60-day mean / std."""
    _need(ohlcv, ["volume"])
    v = ohlcv["volume"]
    mean = v.rolling(window=window, min_periods=window // 2).mean()
    std = v.rolling(window=window, min_periods=window // 2).std(ddof=1)
    z = (v - mean) / std.replace(0.0, np.nan)
    return z.rename("volume_z")


def relative_volume(ohlcv: pd.DataFrame, *, window: int = 20) -> pd.Series:
    """volume / 20d-ADV."""
    _need(ohlcv, ["volume"])
    adv = ohlcv["volume"].rolling(window=window, min_periods=5).mean()
    rv = ohlcv["volume"] / adv.replace(0.0, np.nan)
    return rv.rename("rel_volume")


def chaikin_money_flow(
    ohlcv: pd.DataFrame, *, window: int = 20,
) -> pd.Series:
    """CMF: sum_{N}(money_flow_multiplier * volume) / sum_{N}(volume)
    where multiplier = ((C - L) - (H - C)) / (H - L).
    """
    _need(ohlcv, ["high", "low", "close", "volume"])
    hl = (ohlcv["high"] - ohlcv["low"]).replace(0.0, np.nan)
    mfm = ((ohlcv["close"] - ohlcv["low"]) - (ohlcv["high"] - ohlcv["close"])) / hl
    mfv = mfm * ohlcv["volume"]
    cmf = (
        mfv.rolling(window=window, min_periods=5).sum()
        / ohlcv["volume"].rolling(window=window, min_periods=5).sum().replace(0.0, np.nan)
    )
    return cmf.rename("cmf")


def compute_all_volume_features(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """One-stop call: returns a DataFrame with all volume features as columns."""
    return pd.concat([
        on_balance_volume(ohlcv),
        volume_price_trend(ohlcv),
        money_flow_index(ohlcv),
        volume_weighted_momentum(ohlcv),
        volume_zscore(ohlcv),
        relative_volume(ohlcv),
        chaikin_money_flow(ohlcv),
    ], axis=1)


def volume_momentum_composite(ohlcv: pd.DataFrame) -> dict[str, float]:
    """Single-ticker current-state volume-momentum composite.

    Returns the most-recent value for each feature plus a single
    composite z-score (mean of standardised features clipped to +/-3
    per Grinold-Kahn 2000 ch.3 winsorisation convention).
    """
    feats = compute_all_volume_features(ohlcv)
    if feats.empty:
        return {"composite": 0.0, "n_signals": 0}

    out: dict[str, float] = {}
    z_components: list[float] = []
    for col in feats.columns:
        s = feats[col].dropna()
        if len(s) < 60:
            out[col] = float("nan")
            continue
        recent = float(s.iloc[-1])
        window = s.iloc[-252:] if len(s) >= 252 else s
        mean = float(window.mean())
        std = float(window.std(ddof=1))
        if std < 1e-12:
            out[col] = recent
            continue
        z = (recent - mean) / std
        z = max(-3.0, min(3.0, z))
        out[col] = recent
        out[f"{col}_z"] = z
        z_components.append(z)

    composite = float(np.mean(z_components)) if z_components else 0.0
    out["composite"] = composite
    out["n_signals"] = len(z_components)
    return out


def volume_confirmed_forecast(
    close: pd.Series,
    lookback: int = 60,
    horizon_days: int = 30,
    *,
    volume: pd.Series | None = None,
) -> tuple[float, float, float, float, float]:
    """Walk-forward-compatible forecaster: AR(1) augmented by volume-confirmation.

    The AR(1) phi is multiplied by (1 + cgw_corr), where cgw_corr is the
    rank correlation of |return| and volume over the lookback window
    (Campbell-Grossman-Wang 1993). Intuition: when high-volume days
    coincide with big moves, momentum persists more.

    Returns the standard (point, lo80, hi80, lo95, hi95) tuple.
    """
    if len(close) < 30:
        return 0.0, -0.02, 0.02, -0.04, 0.04
    log_ret = np.log(close / close.shift(1)).dropna()
    if len(log_ret) < 20:
        return 0.0, -0.02, 0.02, -0.04, 0.04
    effective_lookback = min(lookback, len(log_ret))
    window = log_ret.iloc[-effective_lookback:].to_numpy()

    x = window[:-1]; y = window[1:]
    if np.std(x) < 1e-12:
        phi = 0.0; const = float(np.mean(window))
    else:
        x_centered = x - x.mean(); y_centered = y - y.mean()
        phi = float(np.sum(x_centered * y_centered) / np.sum(x_centered**2))
        const = float(y.mean() - phi * x.mean())

    if volume is not None and len(volume) >= lookback:
        vol_window = volume.reindex(log_ret.index).iloc[-lookback:].dropna()
        ret_window = log_ret.iloc[-lookback:].reindex(vol_window.index)
        if len(vol_window) >= 5 and ret_window.notna().sum() >= 5:
            v_ranks = vol_window.rank().to_numpy()
            r_ranks = ret_window.abs().rank().to_numpy()
            if np.std(v_ranks) > 1e-12 and np.std(r_ranks) > 1e-12:
                cgw = float(np.corrcoef(v_ranks, r_ranks)[0, 1])
                phi = phi * (1.0 + max(-0.5, min(0.5, cgw)))

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
