"""Multi-timeframe technical analysis.

I run TA across 1-week / daily / hourly / 5-min / 1-min bars: the longer
frames anchor the regime, the intraday frames inform entry timing.

yfinance interval / max-history:
  1m   -> 7 days        5m -> 60 days       15m/30m -> 60 days
  60m / 1h -> 730 days  1d -> unlimited     1wk -> unlimited

Each timeframe produces the same canonical TA snapshot at the LAST bar:
  rsi_14, rsi_2, macd_hist + cross flags, bb_pctb, bb_bandwidth,
  bb_squeeze flag, adx_14 + rising flag, stoch_k, willr_14,
  obv_slope_10, mfi_14, cmf_20, atr_pct, sma_50_200_cross,
  ema_9_21_diff_pct, price_above_sma200, donch_pos + breakout flags,
  intraday_range_atr, close_in_range, body_to_range.

Plus 5 cross-timeframe alignment patterns (Elder triple-screen,
RSI/MACD divergence, Bollinger squeeze + Donchian breakout,
SMA-alignment, MACD-cross + OBV confirmation) emitted as
confluence_long / confluence_short.

Disk cache via parquet (data/yf_cache/) keyed by MD5(ticker|interval|
start|end) to avoid yfinance 429 rate-limits on re-runs.

Research provenance:
  * Granville 1963 (OBV), Quong-Soudack 1989 (MFI), Bollinger 1996
    (BBands + squeeze), Wilder 1978 (RSI/ATR/ADX), Appel 1979 (MACD),
    Elder 1993 (triple-screen), Donchian channels.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import pathlib
import time
from typing import Optional

import numpy as np
import pandas as pd


MULTI_TIMEFRAME_VERSION = "1.0"

INTERVAL_MAX_DAYS = {
    "1m":  7, "5m": 60, "15m": 60, "30m": 60,
    "60m": 730, "1h": 730, "1d": 365 * 20,
    "5d": 365 * 20, "1wk": 365 * 20, "1mo": 365 * 20,
}

DEFAULT_LOOKBACK = {
    "1m":  6, "5m": 55, "15m": 55, "30m": 55,
    "1h":  365, "1d":  365 * 5, "1wk": 365 * 10,
}

CACHE_DIR = pathlib.Path("data") / "yf_cache"


def _cache_key(ticker: str, interval: str, start: _dt.date, end: _dt.date) -> str:
    return hashlib.md5(f"{ticker}|{interval}|{start}|{end}".encode()).hexdigest()


def fetch_yf_interval(
    ticker: str,
    interval: str,
    *,
    end: Optional[_dt.date] = None,
    lookback_days: Optional[int] = None,
    max_retries: int = 3,
) -> pd.DataFrame:
    """Fetch one ticker x one interval via yfinance with disk caching."""
    import yfinance as yf

    end = end or _dt.date.today()
    lookback = lookback_days or DEFAULT_LOOKBACK.get(interval, 365)
    lookback = min(lookback, INTERVAL_MAX_DAYS.get(interval, 365))
    start = end - _dt.timedelta(days=lookback)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = CACHE_DIR / f"{_cache_key(ticker, interval, start, end)}.parquet"
    if fp.exists():
        try:
            return pd.read_parquet(fp)
        except Exception:
            pass

    for attempt in range(max_retries):
        try:
            df = yf.Ticker(ticker).history(
                start=str(start), end=str(end), interval=interval, auto_adjust=False,
            )
            if df is None or df.empty:
                raise RuntimeError("empty")
            df.columns = [c.lower() for c in df.columns]
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df.to_parquet(fp)
            return df
        except Exception:
            time.sleep(2 ** attempt)
    return pd.DataFrame()


# --------- Indicator helpers ---------

def _rsi(c: pd.Series, p: int = 14) -> pd.Series:
    delta = c.diff()
    g = delta.where(delta > 0, 0.0)
    l = -delta.where(delta < 0, 0.0)
    ag = g.ewm(alpha=1 / p, adjust=False, min_periods=p).mean()
    al = l.ewm(alpha=1 / p, adjust=False, min_periods=p).mean()
    rs = ag / al.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _macd_hist(c: pd.Series) -> pd.Series:
    f = c.ewm(span=12, adjust=False).mean()
    s = c.ewm(span=26, adjust=False).mean()
    macd = f - s
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd - signal


def _bb(c: pd.Series, w: int = 20, n: float = 2.0) -> pd.DataFrame:
    m = c.rolling(w).mean()
    s = c.rolling(w).std(ddof=1)
    u, lo = m + n * s, m - n * s
    return pd.DataFrame({
        "bb_pctb": (c - lo) / (u - lo).replace(0, np.nan),
        "bb_bandwidth": (u - lo) / m.replace(0, np.nan),
    })


def _adx(df: pd.DataFrame, w: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([(h - l), (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / w, adjust=False).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / w, adjust=False).mean() / atr.replace(0, np.nan)
    mdi = 100 * minus_dm.ewm(alpha=1 / w, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / w, adjust=False).mean()


def _stoch_k(df: pd.DataFrame, w: int = 14) -> pd.Series:
    ln = df["low"].rolling(w).min()
    hn = df["high"].rolling(w).max()
    return (df["close"] - ln) / (hn - ln).replace(0, np.nan) * 100


def _willr(df: pd.DataFrame, w: int = 14) -> pd.Series:
    ln = df["low"].rolling(w).min()
    hn = df["high"].rolling(w).max()
    return (hn - df["close"]) / (hn - ln).replace(0, np.nan) * -100


def _obv_slope(df: pd.DataFrame, w: int = 10) -> pd.Series:
    delta = df["close"].diff()
    sign = np.where(delta > 0, 1.0, np.where(delta < 0, -1.0, 0.0))
    obv = (sign * df["volume"].to_numpy()).cumsum()
    obv_s = pd.Series(obv, index=df.index)
    return (obv_s - obv_s.shift(w)) / w


def _mfi(df: pd.DataFrame, w: int = 14) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    rmf = tp * df["volume"]
    delta = tp.diff()
    pos = rmf.where(delta > 0, 0.0).rolling(w).sum()
    neg = rmf.where(delta < 0, 0.0).rolling(w).sum()
    mfr = pos / neg.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + mfr))


def _cmf(df: pd.DataFrame, w: int = 20) -> pd.Series:
    hl = (df["high"] - df["low"]).replace(0, np.nan)
    mfm = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl
    mfv = mfm * df["volume"]
    return mfv.rolling(w).sum() / df["volume"].rolling(w).sum().replace(0, np.nan)


def _atr_pct(df: pd.DataFrame, w: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / w, adjust=False).mean()
    return atr / df["close"].replace(0, np.nan)


def _donchian(df: pd.DataFrame, w: int = 20) -> dict[str, pd.Series]:
    hi = df["high"].rolling(w).max()
    lo = df["low"].rolling(w).min()
    pos = (df["close"] - lo) / (hi - lo).replace(0, np.nan)
    breakout_up = (df["close"] > hi.shift(1)).astype(int)
    breakout_dn = (df["close"] < lo.shift(1)).astype(int)
    return {"donch_pos": pos, "donch_break_up": breakout_up, "donch_break_dn": breakout_dn}


def compute_ta_snapshot(df: pd.DataFrame) -> dict:
    """Canonical TA snapshot at the LAST bar."""
    if df is None or df.empty:
        return {"available": False}
    if not all(c in df.columns for c in ["open", "high", "low", "close", "volume"]):
        return {"available": False}
    c = df["close"]

    out: dict = {"available": True, "n_bars": int(len(df))}

    rsi14 = _rsi(c, 14); out["rsi_14"] = float(rsi14.iloc[-1]) if rsi14.notna().any() else float("nan")
    rsi2 = _rsi(c, 2);   out["rsi_2"]  = float(rsi2.iloc[-1]) if rsi2.notna().any() else float("nan")
    out["rsi_overbought"] = int(out["rsi_14"] > 70) if np.isfinite(out["rsi_14"]) else 0
    out["rsi_oversold"]   = int(out["rsi_14"] < 30) if np.isfinite(out["rsi_14"]) else 0

    macd = _macd_hist(c)
    out["macd_hist"] = float(macd.iloc[-1]) if macd.notna().any() else float("nan")
    out["macd_cross_up"] = int(
        len(macd) >= 2 and float(macd.iloc[-1]) > 0 and float(macd.iloc[-2]) <= 0
    )
    out["macd_cross_dn"] = int(
        len(macd) >= 2 and float(macd.iloc[-1]) < 0 and float(macd.iloc[-2]) >= 0
    )

    bb = _bb(c)
    out["bb_pctb"] = float(bb["bb_pctb"].iloc[-1]) if bb["bb_pctb"].notna().any() else float("nan")
    out["bb_bandwidth"] = float(bb["bb_bandwidth"].iloc[-1]) if bb["bb_bandwidth"].notna().any() else float("nan")
    bw_q = bb["bb_bandwidth"].rolling(60).quantile(0.20)
    out["bb_squeeze"] = int(
        bw_q.notna().any() and bb["bb_bandwidth"].notna().iloc[-1]
        and float(bb["bb_bandwidth"].iloc[-1]) < float(bw_q.iloc[-1])
    )

    adx14 = _adx(df)
    out["adx_14"] = float(adx14.iloc[-1]) if adx14.notna().any() else float("nan")
    out["adx_rising"] = int(
        adx14.notna().any() and len(adx14) >= 4
        and float(adx14.iloc[-1]) > float(adx14.iloc[-4])
    )

    stk = _stoch_k(df); out["stoch_k"] = float(stk.iloc[-1]) if stk.notna().any() else float("nan")
    wr = _willr(df);    out["willr_14"] = float(wr.iloc[-1]) if wr.notna().any() else float("nan")
    obvs = _obv_slope(df); out["obv_slope_10"] = float(obvs.iloc[-1]) if obvs.notna().any() else float("nan")
    mfi = _mfi(df);     out["mfi_14"] = float(mfi.iloc[-1]) if mfi.notna().any() else float("nan")
    cmf = _cmf(df);     out["cmf_20"] = float(cmf.iloc[-1]) if cmf.notna().any() else float("nan")

    atrp = _atr_pct(df)
    out["atr_pct"] = float(atrp.iloc[-1]) if atrp.notna().any() else float("nan")

    if len(c) >= 50:
        sma50 = c.rolling(50).mean()
        out["sma_50"] = float(sma50.iloc[-1]) if sma50.notna().any() else float("nan")
    if len(c) >= 200:
        sma200 = c.rolling(200).mean()
        out["sma_200"] = float(sma200.iloc[-1]) if sma200.notna().any() else float("nan")
        if sma200.notna().any():
            out["price_above_sma200"] = int(float(c.iloc[-1]) > float(sma200.iloc[-1]))

    ema9 = c.ewm(span=9, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    if ema21.iloc[-1]:
        out["ema_9_21_diff_pct"] = float((ema9.iloc[-1] - ema21.iloc[-1]) / ema21.iloc[-1])

    don = _donchian(df, 20)
    out["donch_pos_20"] = float(don["donch_pos"].iloc[-1]) if don["donch_pos"].notna().any() else float("nan")
    out["donch_break_up"] = int(don["donch_break_up"].iloc[-1]) if don["donch_break_up"].notna().any() else 0
    out["donch_break_dn"] = int(don["donch_break_dn"].iloc[-1]) if don["donch_break_dn"].notna().any() else 0

    if len(df) >= 2:
        last = df.iloc[-1]
        atr_val = float(atrp.iloc[-1]) * float(last["close"]) if atrp.notna().any() else None
        if atr_val and atr_val > 0:
            out["intraday_range_atr"] = float((last["high"] - last["low"]) / atr_val)
        if (last["high"] - last["low"]) > 0:
            out["close_in_range"] = float((last["close"] - last["low"]) / (last["high"] - last["low"]))
            out["body_to_range"] = float((last["close"] - last["open"]) / (last["high"] - last["low"]))

    return out


def compute_confluence(snapshots: dict[str, dict]) -> dict[str, float]:
    """Top-5 cross-timeframe alignment patterns -> confluence_long/short."""
    s1w = snapshots.get("1wk", {})
    s1d = snapshots.get("1d", {})
    s1h = snapshots.get("1h", {})

    patterns_long, patterns_short = {}, {}

    patterns_long["elder_triple"] = int(
        s1w.get("price_above_sma200", 0) == 1
        and s1d.get("rsi_14", 50) < 40
        and s1h.get("macd_hist", 0) > 0
    )
    patterns_short["elder_triple"] = int(
        s1w.get("price_above_sma200", 0) == 0
        and s1d.get("rsi_14", 50) > 60
        and s1h.get("macd_hist", 0) < 0
    )
    patterns_short["daily_overbought_hourly_div"] = int(
        s1d.get("rsi_14", 50) > 70 and s1h.get("macd_hist", 0) < 0
    )
    patterns_long["daily_oversold_hourly_bull"] = int(
        s1d.get("rsi_14", 50) < 30 and s1h.get("macd_hist", 0) > 0
    )
    patterns_long["bb_squeeze_break_up"] = int(
        s1w.get("bb_squeeze", 0) == 1 and s1d.get("donch_break_up", 0) == 1
        and s1d.get("adx_14", 0) > 20
    )
    patterns_short["bb_squeeze_break_dn"] = int(
        s1w.get("bb_squeeze", 0) == 1 and s1d.get("donch_break_dn", 0) == 1
        and s1d.get("adx_14", 0) > 20
    )
    patterns_long["sma_align_long"] = int(
        s1d.get("price_above_sma200", 0) == 1 and s1h.get("rsi_14", 50) > 50
    )
    patterns_short["sma_align_short"] = int(
        s1d.get("price_above_sma200", 1) == 0 and s1h.get("rsi_14", 50) < 50
    )
    patterns_long["macd_cross_obv"] = int(
        s1d.get("macd_cross_up", 0) == 1 and s1h.get("obv_slope_10", 0) > 0
    )
    patterns_short["macd_cross_obv"] = int(
        s1d.get("macd_cross_dn", 0) == 1 and s1h.get("obv_slope_10", 0) < 0
    )

    return {
        "confluence_long":  sum(patterns_long.values())  / max(len(patterns_long), 1),
        "confluence_short": sum(patterns_short.values()) / max(len(patterns_short), 1),
        **{f"long_{k}": v for k, v in patterns_long.items()},
        **{f"short_{k}": v for k, v in patterns_short.items()},
    }


def fetch_multi_timeframe_snapshots(
    ticker: str,
    *,
    intervals: tuple[str, ...] = ("1wk", "1d", "1h"),
) -> dict[str, dict]:
    return {iv: compute_ta_snapshot(fetch_yf_interval(ticker, iv)) for iv in intervals}


def fetch_execution_timing_features(
    ticker: str,
    *,
    intervals: tuple[str, ...] = ("5m", "1m"),
) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for interval in intervals:
        df = fetch_yf_interval(ticker, interval)
        if df is None or df.empty:
            out[interval] = {"available": False}
            continue
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        vwap = (tp * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)
        spread_proxy = (df["high"] - df["low"]) / df["close"].replace(0, np.nan)
        rel_vol = df["volume"] / df["volume"].rolling(30, min_periods=10).mean()
        out[interval] = {
            "available": True,
            "n_bars": int(len(df)),
            "last_close": float(df["close"].iloc[-1]),
            "vwap_session": float(vwap.iloc[-1]) if vwap.notna().any() else float("nan"),
            "vwap_dev_pct": float((df["close"].iloc[-1] / vwap.iloc[-1] - 1) * 100)
                            if vwap.notna().any() else float("nan"),
            "spread_proxy_pct": float(spread_proxy.iloc[-1] * 100)
                                if spread_proxy.notna().any() else float("nan"),
            "rel_vol_30bar": float(rel_vol.iloc[-1]) if rel_vol.notna().any() else float("nan"),
        }
    return out
