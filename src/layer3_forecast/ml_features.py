"""ML feature engineering pipeline.

Closes the feature gap to the PMC9680880 paper (41 features) and adds
multi-timeframe + microstructure + regime features it did not have.

~60 leakage-free features per ticker per day; designed as input to a
LightGBM classifier with triple-barrier labels and purged walk-forward.

Anti-leakage rules:
  * Every feature value at row t uses only data in [t - lookback, t].
  * No global statistics; rolling z-scores only.
  * NaN burn-in at the head; never forward/back-fill across it.

Research:
  * Qlib Alpha158/Alpha360 (Microsoft) -- canonical feature set
  * Lopez de Prado AFML Ch.2-5 -- triple-barrier labels
  * Yang-Zhang 2000 / Parkinson 1980 / Garman-Klass 1980 -- realised vol
  * Granville / Quong-Soudack / CGW -- volume

Pure functions; no DB.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


ML_FEATURES_VERSION = "1.0"


def _need(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV frame missing columns: {missing}")


def lagged_log_returns(close: pd.Series, lags=(1, 2, 3, 5, 10, 20)) -> pd.DataFrame:
    log_ret = np.log(close / close.shift(1))
    out = {}
    for lag in lags:
        out[f"logret_lag{lag}"] = log_ret.shift(lag - 1) if lag > 1 else log_ret
    return pd.DataFrame(out, index=close.index)


def rolling_vol_features(close: pd.Series) -> pd.DataFrame:
    log_ret = np.log(close / close.shift(1))
    return pd.DataFrame({
        "vol_5":   log_ret.rolling(5).std(),
        "vol_20":  log_ret.rolling(20).std(),
        "vol_60":  log_ret.rolling(60).std(),
        "skew_20": log_ret.rolling(20).skew(),
        "kurt_20": log_ret.rolling(20).kurt(),
    }, index=close.index)


def parkinson_vol(ohlcv: pd.DataFrame, window: int = 20) -> pd.Series:
    _need(ohlcv, ["high", "low"])
    hl = np.log(ohlcv["high"] / ohlcv["low"])
    return np.sqrt((hl ** 2).rolling(window).mean() / (4 * np.log(2))).rename("parkinson_vol")


def garman_klass_vol(ohlcv: pd.DataFrame, window: int = 20) -> pd.Series:
    _need(ohlcv, ["high", "low", "close", "open"])
    hl = np.log(ohlcv["high"] / ohlcv["low"]) ** 2
    co = np.log(ohlcv["close"] / ohlcv["open"]) ** 2
    val = (0.5 * hl - (2 * np.log(2) - 1) * co).rolling(window).mean()
    return np.sqrt(val.clip(lower=0)).rename("garman_klass_vol")


def yang_zhang_vol(ohlcv: pd.DataFrame, window: int = 20) -> pd.Series:
    _need(ohlcv, ["high", "low", "close", "open"])
    n = window
    log_oc = np.log(ohlcv["open"] / ohlcv["close"].shift(1))
    log_co = np.log(ohlcv["close"] / ohlcv["open"])
    log_ho = np.log(ohlcv["high"] / ohlcv["open"])
    log_lo = np.log(ohlcv["low"] / ohlcv["open"])
    sigma_o = log_oc.rolling(n).var()
    sigma_c = log_co.rolling(n).var()
    sigma_rs = (log_ho * (log_ho - log_co) + log_lo * (log_lo - log_co)).rolling(n).mean()
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    val = sigma_o + k * sigma_c + (1 - k) * sigma_rs
    return np.sqrt(val.clip(lower=0)).rename("yang_zhang_vol")


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100.0 - (100.0 / (1.0 + rs))).rename(f"rsi_{period}")


def macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.Series:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=sig, adjust=False).mean()
    return (macd - signal).rename("macd_hist")


def bollinger_features(close: pd.Series, window: int = 20, n_std: float = 2.0) -> pd.DataFrame:
    mean = close.rolling(window).mean()
    std = close.rolling(window).std(ddof=1)
    upper = mean + n_std * std
    lower = mean - n_std * std
    pctb = (close - lower) / (upper - lower).replace(0, np.nan)
    bandwidth = (upper - lower) / mean.replace(0, np.nan)
    return pd.DataFrame({"bb_pctb": pctb, "bb_bandwidth": bandwidth}, index=close.index)


def atr_pct(ohlcv: pd.DataFrame, window: int = 14) -> pd.Series:
    _need(ohlcv, ["high", "low", "close"])
    tr1 = ohlcv["high"] - ohlcv["low"]
    tr2 = (ohlcv["high"] - ohlcv["close"].shift(1)).abs()
    tr3 = (ohlcv["low"] - ohlcv["close"].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / window, adjust=False).mean()
    return (atr / ohlcv["close"].replace(0, np.nan)).rename("atr_pct")


def adx(ohlcv: pd.DataFrame, window: int = 14) -> pd.Series:
    _need(ohlcv, ["high", "low", "close"])
    high, low, close = ohlcv["high"], ohlcv["low"], ohlcv["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / window, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / window, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / window, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / window, adjust=False).mean().rename("adx_14")


def stochastic_k(ohlcv: pd.DataFrame, window: int = 14) -> pd.Series:
    _need(ohlcv, ["high", "low", "close"])
    low_n = ohlcv["low"].rolling(window).min()
    high_n = ohlcv["high"].rolling(window).max()
    return ((ohlcv["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100).rename("stoch_k")


def microstructure_features(ohlcv: pd.DataFrame) -> pd.DataFrame:
    _need(ohlcv, ["high", "low", "close", "open"])
    prev_close = ohlcv["close"].shift(1)
    overnight_ret = np.log(ohlcv["open"] / prev_close)
    intraday_range = (ohlcv["high"] - ohlcv["low"]) / ohlcv["close"].replace(0, np.nan)
    gap = (ohlcv["open"] - prev_close) / prev_close.replace(0, np.nan)
    body = (ohlcv["close"] - ohlcv["open"]) / (ohlcv["high"] - ohlcv["low"]).replace(0, np.nan)
    upper_wick = (ohlcv["high"] - ohlcv[["open", "close"]].max(axis=1)) / (
        ohlcv["high"] - ohlcv["low"]
    ).replace(0, np.nan)
    lower_wick = (ohlcv[["open", "close"]].min(axis=1) - ohlcv["low"]) / (
        ohlcv["high"] - ohlcv["low"]
    ).replace(0, np.nan)
    close_in_range = (ohlcv["close"] - ohlcv["low"]) / (
        ohlcv["high"] - ohlcv["low"]
    ).replace(0, np.nan)
    return pd.DataFrame({
        "overnight_ret":  overnight_ret,
        "intraday_range": intraday_range,
        "gap_pct":        gap,
        "body_to_range":  body,
        "upper_wick":     upper_wick,
        "lower_wick":     lower_wick,
        "close_in_range": close_in_range,
    }, index=ohlcv.index)


def calendar_features(idx: pd.DatetimeIndex) -> pd.DataFrame:
    d = pd.DataFrame(index=idx)
    d["dow"] = idx.dayofweek
    d["month"] = idx.month
    d["day_of_month"] = idx.day
    return d


def cross_asset_features(
    close: pd.Series,
    *,
    spy_close: Optional[pd.Series] = None,
    vix_close: Optional[pd.Series] = None,
    sector_close: Optional[pd.Series] = None,
) -> pd.DataFrame:
    out = pd.DataFrame(index=close.index)
    if spy_close is not None and not spy_close.empty:
        spy_aligned = spy_close.reindex(close.index, method="ffill")
        spy_ret = np.log(spy_aligned / spy_aligned.shift(1))
        out["spy_ret"] = spy_ret
        out["spy_ret_lag1"] = spy_ret.shift(1)
        out["spy_vol_20"] = spy_ret.rolling(20).std()
    if vix_close is not None and not vix_close.empty:
        vix_aligned = vix_close.reindex(close.index, method="ffill")
        out["vix_level"] = vix_aligned
        out["vix_chg_1d"] = vix_aligned.pct_change()
        out["vix_chg_5d"] = vix_aligned.pct_change(5)
        mean = vix_aligned.rolling(252, min_periods=60).mean()
        std = vix_aligned.rolling(252, min_periods=60).std(ddof=1)
        out["vix_z_252"] = (vix_aligned - mean) / std.replace(0, np.nan)
    if sector_close is not None and not sector_close.empty:
        sec_aligned = sector_close.reindex(close.index, method="ffill")
        sec_ret = np.log(sec_aligned / sec_aligned.shift(1))
        out["sector_ret"] = sec_ret
        out["stock_minus_sector_ret"] = np.log(close / close.shift(1)) - sec_ret
    return out


def triple_barrier_labels(
    close: pd.Series,
    *,
    pt_sigma: float = 2.0,
    sl_sigma: float = 2.0,
    vertical_days: int = 5,
    sigma_span: int = 100,
    deadzone_sigma: float = 0.5,
) -> pd.Series:
    """Per AFML Ch.3 -- triple-barrier labels {-1, 0, +1}."""
    log_ret = np.log(close / close.shift(1))
    sigma = log_ret.ewm(span=sigma_span).std()

    n = len(close)
    y = np.full(n, np.nan, dtype=float)
    close_arr = close.to_numpy()
    sigma_arr = sigma.to_numpy()

    for i in range(n - vertical_days):
        s = sigma_arr[i]
        if not np.isfinite(s) or s <= 0:
            continue
        ref = close_arr[i]
        up_barrier = ref * (1 + pt_sigma * s)
        dn_barrier = ref * (1 - sl_sigma * s)
        path = close_arr[i + 1: i + 1 + vertical_days]
        hit_up_mask = path >= up_barrier
        hit_dn_mask = path <= dn_barrier
        hit_up = int(np.argmax(hit_up_mask)) if hit_up_mask.any() else None
        hit_dn = int(np.argmax(hit_dn_mask)) if hit_dn_mask.any() else None
        if hit_up is not None and (hit_dn is None or hit_up < hit_dn):
            y[i] = 1.0
        elif hit_dn is not None and (hit_up is None or hit_dn < hit_up):
            y[i] = -1.0
        else:
            terminal = path[-1] / ref - 1.0
            if terminal > deadzone_sigma * s:
                y[i] = 1.0
            elif terminal < -deadzone_sigma * s:
                y[i] = -1.0
            else:
                y[i] = 0.0
    return pd.Series(y, index=close.index, name="y_tb")


def binary_next_day_labels(close: pd.Series) -> pd.Series:
    """Naive next-day-sign label (binary) for back-compat comparisons."""
    return ((close.shift(-1) / close - 1.0) > 0).astype(int).rename("y_binary")


def causal_rolling_zscore(df: pd.DataFrame, window: int = 252) -> pd.DataFrame:
    mu = df.rolling(window, min_periods=window // 4).mean()
    sd = df.rolling(window, min_periods=window // 4).std(ddof=1)
    return (df - mu) / sd.replace(0, np.nan)


def build_features(
    ohlcv: pd.DataFrame,
    *,
    spy_close: Optional[pd.Series] = None,
    vix_close: Optional[pd.Series] = None,
    sector_close: Optional[pd.Series] = None,
    z_window: int = 252,
) -> pd.DataFrame:
    """One-stop call: ~60 features per row, all causal, ready for ML."""
    _need(ohlcv, ["high", "low", "close", "open", "volume"])
    close = ohlcv["close"]

    blocks = [
        lagged_log_returns(close),
        rolling_vol_features(close),
        parkinson_vol(ohlcv).to_frame(),
        garman_klass_vol(ohlcv).to_frame(),
        yang_zhang_vol(ohlcv).to_frame(),
        rsi(close, 2).to_frame(),
        rsi(close, 14).to_frame(),
        macd_hist(close).to_frame(),
        bollinger_features(close),
        atr_pct(ohlcv).to_frame(),
        adx(ohlcv).to_frame(),
        stochastic_k(ohlcv).to_frame(),
        microstructure_features(ohlcv),
        cross_asset_features(
            close, spy_close=spy_close, vix_close=vix_close, sector_close=sector_close,
        ),
    ]
    from src.methodology.volume_features import compute_all_volume_features
    blocks.append(compute_all_volume_features(ohlcv))

    df = pd.concat(blocks, axis=1)
    cal = calendar_features(ohlcv.index)
    z = causal_rolling_zscore(df, window=z_window)
    z.columns = [f"{c}_z" for c in z.columns]
    out = pd.concat([df, z, cal], axis=1)
    out.attrs["ml_features_version"] = ML_FEATURES_VERSION
    return out
