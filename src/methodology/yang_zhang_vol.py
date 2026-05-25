"""Yang-Zhang (2000) realized-volatility estimator.

Reference:
  Yang, D. & Zhang, Q. (2000). "Drift-Independent Volatility Estimation
  Based on High, Low, Open, and Close Prices." Journal of Business
  73(3), 477-491.

Operational formula per spec section 8.1:

  sigma2_YZ = sigma2_overnight + k * sigma2_oc + (1 - k) * sigma2_RS

  sigma2_overnight = mean((log(O_t / C_{t-1}))^2)
  sigma2_oc        = mean((log(C_t / O_t))^2)
  sigma2_RS        = mean( log(H_t/C_t)*log(H_t/O_t) + log(L_t/C_t)*log(L_t/O_t) )
  k                = 0.34 / (1.34 + (n + 1) / (n - 1))

The Yang-Zhang estimator is drift-independent and uses all four
OHLC fields, so it carries ~14x more information than close-to-close
under GBM assumptions. Robust to overnight gaps because the overnight
return component is captured explicitly rather than being smeared
across the day's close-to-close path.

Annualized output (* sqrt(252)) since downstream lowvol scoring is a
relative comparison.

Pure function. Returns a pd.Series aligned with the input index where
each entry is the YZ vol *ending* at that date over the trailing
`window_days` observations. Entries with fewer than `window_days`
observations of history are NaN.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


YANG_ZHANG_VERSION = "1.0"

_TRADING_DAYS_PER_YEAR = 252


def _required_columns(df: pd.DataFrame) -> None:
    """Validate that df carries the OHLC columns we need.

    Accepts canonical column names (lowercase) or Yahoo-style
    capitalised names ('Open', 'High', 'Low', 'Close'). Missing
    columns raise ValueError.
    """
    cols = {c.lower() for c in df.columns}
    required = {"open", "high", "low", "close"}
    missing = required - cols
    if missing:
        raise ValueError(
            f"yang_zhang_vol: OHLC columns missing: {sorted(missing)}. "
            f"Got columns: {list(df.columns)}"
        )


def _ohlc_lowercase(df: pd.DataFrame) -> pd.DataFrame:
    """Return df with columns renamed to lowercase 'open','high','low','close'."""
    rename_map = {}
    for col in df.columns:
        lower = col.lower()
        if lower in {"open", "high", "low", "close"}:
            rename_map[col] = lower
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


def yang_zhang_vol(
    ohlc_df: pd.DataFrame,
    window_days: int = 60,
) -> pd.Series:
    """Annualized Yang-Zhang realized vol over rolling `window_days`.

    Parameters
    ----------
    ohlc_df
        Frame indexed by date (sorted ascending) with columns
        Open / High / Low / Close (case-insensitive).
    window_days
        Trailing-window size in trading days. Default 60 per spec
        §8.3 / §12.1 realized_vol.window_days.

    Returns
    -------
    pd.Series
        Annualized YZ vol (decimal, e.g. 0.25 = 25%) ending at each
        row's date. NaN where the trailing window has fewer than
        `window_days` valid rows or where intermediate log-ratios are
        non-finite (zero / negative price).

    Raises
    ------
    ValueError
        If `window_days` is < 2 or OHLC columns are missing.
    """
    if window_days < 2:
        raise ValueError(f"window_days must be >= 2, got {window_days}")

    if len(ohlc_df) == 0:
        return pd.Series(dtype=float, index=ohlc_df.index)

    _required_columns(ohlc_df)
    df = _ohlc_lowercase(ohlc_df).copy()

    # Element-wise log-ratios. NaN propagates naturally where a price
    # is missing.
    prev_close = df["close"].shift(1)
    with np.errstate(divide="ignore", invalid="ignore"):
        log_oc_prev = np.log(df["open"] / prev_close)
        log_co = np.log(df["close"] / df["open"])
        log_hc = np.log(df["high"] / df["close"])
        log_ho = np.log(df["high"] / df["open"])
        log_lc = np.log(df["low"] / df["close"])
        log_lo = np.log(df["low"] / df["open"])

    overnight_sq = log_oc_prev * log_oc_prev
    oc_sq = log_co * log_co
    rs_term = log_hc * log_ho + log_lc * log_lo

    # k constant depends on window size only.
    n = float(window_days)
    k = 0.34 / (1.34 + (n + 1.0) / (n - 1.0))

    # Rolling means -- min_periods enforces full-window requirement.
    # The overnight term loses the first observation to .shift(1), so
    # any window straddling row 0 will be NaN by construction.
    sigma2_overnight = overnight_sq.rolling(window_days, min_periods=window_days).mean()
    sigma2_oc = oc_sq.rolling(window_days, min_periods=window_days).mean()
    sigma2_rs = rs_term.rolling(window_days, min_periods=window_days).mean()

    sigma2_yz = sigma2_overnight + k * sigma2_oc + (1.0 - k) * sigma2_rs

    # Clip non-finite / negative variances to NaN. Negative can occur
    # from extreme RS terms in pathological data.
    sigma2_yz = sigma2_yz.where(sigma2_yz >= 0)

    sigma_daily = np.sqrt(sigma2_yz)
    annualized = sigma_daily * math.sqrt(_TRADING_DAYS_PER_YEAR)
    annualized.name = "yang_zhang_vol"
    return annualized
