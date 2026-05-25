"""Technical indicators -- pure functions.

References:
  Wilder, J.W. (1978). "New Concepts in Technical Trading Systems."
    -- canonical source for RSI and ATR.
  Appel, G. (1979). "The Moving Average Convergence-Divergence."
    -- MACD original derivation.
  Murphy, J.J. (1999). "Technical Analysis of the Financial Markets,"
    NY Institute of Finance. -- pivot points + practitioner standards.

I hand-code these against the textbook formulas rather than depending
on a third-party library. Test golden values come from Wilder/Appel
directly.

All functions:
  * accept a pandas Series (close-only) or DataFrame (OHLC)
  * return a pandas Series / DataFrame aligned to the input index
  * propagate NaN for periods with insufficient history
"""
from __future__ import annotations

import numpy as np
import pandas as pd


TECHNICAL_INDICATORS_VERSION = "1.0"


def sma(close: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    if window < 1:
        raise ValueError("window must be >= 1")
    return close.rolling(window=window, min_periods=window).mean()


def ema(close: pd.Series, window: int) -> pd.Series:
    """Exponential moving average (MACD-style; alpha = 2/(period+1))."""
    if window < 1:
        raise ValueError("window must be >= 1")
    return close.ewm(span=window, adjust=False, min_periods=window).mean()


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (1978) -- alpha = 1/period."""
    if period < 1:
        raise ValueError("period must be >= 1")
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rsi_14(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI (1978 ch.4).

    gain_t = max(close_t - close_{t-1}, 0)
    loss_t = max(close_{t-1} - close_t, 0)
    avg_gain = Wilder-smoothed gain over `period`
    avg_loss = Wilder-smoothed loss over `period`
    RS  = avg_gain / avg_loss
    RSI = 100 - 100 / (1 + RS)
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _wilder_smooth(gain, period)
    avg_loss = _wilder_smooth(loss, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        rsi = 100.0 - 100.0 / (1.0 + rs)
    # When avg_loss == 0 and avg_gain > 0 -> RS=inf -> RSI=100.
    rsi = rsi.where(~(avg_loss == 0.0) | (avg_gain == 0.0), 100.0)
    return rsi


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """Appel's MACD (1979).

    macd_line   = EMA(close, fast) - EMA(close, slow)
    signal_line = EMA(macd_line, signal)
    histogram   = macd_line - signal_line
    """
    if fast >= slow:
        raise ValueError("fast period must be < slow period")
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(
        span=signal, adjust=False, min_periods=signal,
    ).mean()
    hist = macd_line - signal_line
    return pd.DataFrame({
        "macd": macd_line,
        "signal": signal_line,
        "histogram": hist,
    })


def atr_14(ohlc: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR (1978 ch.4).

    TR = max(high - low, |high - close_{t-1}|, |low - close_{t-1}|)
    ATR = Wilder-smoothed TR over `period`.
    """
    cols = {c.lower(): c for c in ohlc.columns}
    needed = {"high", "low", "close"}
    missing = needed - set(cols)
    if missing:
        raise ValueError(f"atr_14: OHLC columns missing: {sorted(missing)}")
    h = ohlc[cols["high"]]
    l_ = ohlc[cols["low"]]
    c = ohlc[cols["close"]]
    prev_c = c.shift(1)
    tr = pd.concat([
        h - l_,
        (h - prev_c).abs(),
        (l_ - prev_c).abs(),
    ], axis=1).max(axis=1)
    return _wilder_smooth(tr, period)


def volume_profile(
    volume: pd.Series,
    window: int = 20,
) -> pd.DataFrame:
    """`window`-day average volume + ratio of current to that average.

    Columns: avg_volume, volume_ratio. ratio > 1 == above-average.
    """
    avg = volume.rolling(window=window, min_periods=window).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = volume / avg
    return pd.DataFrame({"avg_volume": avg, "volume_ratio": ratio})


def pivot_points(
    ohlc: pd.DataFrame,
    lookback: int = 20,
) -> pd.DataFrame:
    """Swing-high / swing-low pivots via local-extrema scan.

    A bar at index t is a swing-high if its high is the max within
    [t - lookback, t + lookback]. Swing-low analogously on lows.

    Returns DataFrame with columns is_swing_high, is_swing_low,
    nearest_support, nearest_resistance aligned to `ohlc.index`.
    """
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    cols = {c.lower(): c for c in ohlc.columns}
    needed = {"high", "low", "close"}
    missing = needed - set(cols)
    if missing:
        raise ValueError(f"pivot_points: OHLC columns missing: {sorted(missing)}")
    h = ohlc[cols["high"]]
    l_ = ohlc[cols["low"]]
    c = ohlc[cols["close"]]

    is_swing_high = pd.Series(False, index=ohlc.index)
    is_swing_low = pd.Series(False, index=ohlc.index)
    n = len(ohlc)
    for i in range(lookback, n - lookback):
        window_h = h.iloc[i - lookback : i + lookback + 1]
        window_l = l_.iloc[i - lookback : i + lookback + 1]
        if h.iloc[i] == window_h.max():
            is_swing_high.iloc[i] = True
        if l_.iloc[i] == window_l.min():
            is_swing_low.iloc[i] = True

    nearest_support = pd.Series(np.nan, index=ohlc.index, dtype=float)
    nearest_resistance = pd.Series(np.nan, index=ohlc.index, dtype=float)
    last_low = np.nan
    last_high = np.nan
    for i in range(n):
        if is_swing_low.iloc[i] and l_.iloc[i] < c.iloc[i]:
            last_low = float(l_.iloc[i])
        if is_swing_high.iloc[i] and h.iloc[i] > c.iloc[i]:
            last_high = float(h.iloc[i])
        nearest_support.iloc[i] = last_low
        nearest_resistance.iloc[i] = last_high

    return pd.DataFrame({
        "is_swing_high": is_swing_high,
        "is_swing_low": is_swing_low,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
    })
