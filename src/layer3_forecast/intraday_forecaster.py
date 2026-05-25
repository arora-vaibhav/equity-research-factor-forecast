"""Intraday hourly directional forecaster.

I added intraday labels to predict 1-hour direction on hourly bars
because signal-to-noise is materially better at the 5-30 min horizon
than at daily.

Stack: yfinance 1h bars (730d cap) + standard tech indicators on
hourly + LightGBM next-1h binary classifier + purged walk-forward.

Pure functions; no DB.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


INTRADAY_FORECASTER_VERSION = "1.0"


@dataclass
class IntradayResult:
    ticker: str
    n_features: int
    n_bars: int
    n_folds: int
    mean_directional_accuracy: float
    std_directional_accuracy: float
    mean_balanced_accuracy: float
    fold_accuracies: list[float] = field(default_factory=list)
    top_features: list[str] = field(default_factory=list)
    intraday_forecaster_version: str = INTRADAY_FORECASTER_VERSION


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
    return macd - macd.ewm(span=9, adjust=False).mean()


def build_intraday_features(ohlcv: pd.DataFrame) -> pd.DataFrame:
    if ohlcv is None or ohlcv.empty:
        return pd.DataFrame()
    cols_needed = ["open", "high", "low", "close", "volume"]
    if not all(c in ohlcv.columns for c in cols_needed):
        return pd.DataFrame()
    c = ohlcv["close"]
    log_ret = np.log(c / c.shift(1))

    feats = pd.DataFrame({
        "logret_lag1": log_ret,
        "logret_lag2": log_ret.shift(1),
        "logret_lag3": log_ret.shift(2),
        "logret_lag5": log_ret.shift(4),
        "rsi_14":      _rsi(c, 14),
        "rsi_4":       _rsi(c, 4),
        "macd_hist":   _macd_hist(c),
        "vol_5":       log_ret.rolling(5).std(),
        "vol_20":      log_ret.rolling(20).std(),
        "intraday_range": (ohlcv["high"] - ohlcv["low"]) / c.replace(0, np.nan),
        "body_to_range":  (c - ohlcv["open"]) / (ohlcv["high"] - ohlcv["low"]).replace(0, np.nan),
        "close_in_range": (c - ohlcv["low"]) / (ohlcv["high"] - ohlcv["low"]).replace(0, np.nan),
        "vol_z_30": (
            (ohlcv["volume"] - ohlcv["volume"].rolling(30, min_periods=10).mean())
            / ohlcv["volume"].rolling(30, min_periods=10).std(ddof=1).replace(0, np.nan)
        ),
        "rel_volume_20": (
            ohlcv["volume"] / ohlcv["volume"].rolling(20, min_periods=5).mean().replace(0, np.nan)
        ),
    }, index=ohlcv.index)

    # session-aware features
    try:
        feats["hour"] = ohlcv.index.hour.astype(float)
        feats["dow"] = ohlcv.index.dayofweek.astype(float)
    except Exception:
        pass

    # cumulative session VWAP deviation
    try:
        sess = pd.Series(ohlcv.index.date, index=ohlcv.index)
        tp = (ohlcv["high"] + ohlcv["low"] + ohlcv["close"]) / 3.0
        cum_tp_vol = (tp * ohlcv["volume"]).groupby(sess).cumsum()
        cum_vol = ohlcv["volume"].groupby(sess).cumsum()
        session_vwap = cum_tp_vol / cum_vol.replace(0, np.nan)
        feats["vwap_dev_pct"] = (c - session_vwap) / session_vwap.replace(0, np.nan)
    except Exception:
        feats["vwap_dev_pct"] = float("nan")

    return feats


def next_hour_binary_labels(close: pd.Series) -> pd.Series:
    nxt = close.shift(-1) / close - 1.0
    # Drop the final NaN row (last bar has no next), then convert.
    sign = np.sign(nxt).replace(0, 1)
    sign = sign.dropna().astype(int).rename("y_next_hour")
    return sign


def train_intraday_lightgbm(
    features: pd.DataFrame,
    labels: pd.Series,
    *,
    ticker: str,
    n_folds: int = 5,
    embargo: int = 4,
    label_horizon: int = 1,
    n_estimators: int = 1000,
    early_stopping_rounds: int = 50,
    num_leaves: int = 31,
    max_depth: int = 5,
    learning_rate: float = 0.02,
    seed: int = 42,
) -> Optional[IntradayResult]:
    import lightgbm as lgb

    df = features.copy()
    df["__y__"] = labels
    df = df.dropna(how="any")
    if len(df) < 200:
        return None
    y_full = df["__y__"].astype(int).to_numpy()
    X_full = df.drop(columns=["__y__"]).to_numpy(dtype=np.float32)
    cols = list(df.drop(columns=["__y__"]).columns)
    n = len(df)

    fold_size = max((n - 100) // (n_folds + 1), 30)
    initial_train_end = 100 + fold_size

    fold_accs: list[float] = []
    bal_accs: list[float] = []
    feat_gain = {c: 0.0 for c in cols}

    for k in range(n_folds):
        test_start = initial_train_end + k * fold_size
        test_end = min(test_start + fold_size, n)
        if test_start >= n - 30:
            break
        train_end = test_start - label_horizon - embargo
        if train_end <= 100:
            continue
        X_tr, y_tr = X_full[:train_end], y_full[:train_end]
        X_te, y_te = X_full[test_start:test_end], y_full[test_start:test_end]
        classes = sorted(np.unique(np.concatenate([y_tr, y_te])))
        if len(classes) < 2:
            continue
        class_map = {c: i for i, c in enumerate(classes)}
        y_tr_lgb = np.array([class_map[int(v)] for v in y_tr], dtype=np.int32)
        y_te_lgb = np.array([class_map[int(v)] for v in y_te], dtype=np.int32)

        params = {
            "objective": "binary", "metric": "binary_logloss",
            "num_leaves": num_leaves, "max_depth": max_depth,
            "learning_rate": learning_rate, "feature_fraction": 0.7,
            "bagging_fraction": 0.8, "bagging_freq": 5,
            "verbose": -1, "seed": seed,
        }
        dtrain = lgb.Dataset(X_tr, label=y_tr_lgb, feature_name=cols)
        dvalid = lgb.Dataset(X_te, label=y_te_lgb, reference=dtrain, feature_name=cols)
        model = lgb.train(
            params, dtrain, num_boost_round=n_estimators,
            valid_sets=[dvalid],
            callbacks=[lgb.early_stopping(early_stopping_rounds), lgb.log_evaluation(0)],
        )
        proba = model.predict(X_te)
        pred_lgb = (proba > 0.5).astype(int)
        inv = {i: c for c, i in class_map.items()}
        pred = np.array([inv[int(p)] for p in pred_lgb])

        acc = float((pred == y_te).mean())
        fold_accs.append(acc)
        try:
            from sklearn.metrics import balanced_accuracy_score
            bal_accs.append(float(balanced_accuracy_score(y_te, pred)))
        except Exception:
            pass
        gain = model.feature_importance(importance_type="gain")
        for c, g in zip(cols, gain):
            feat_gain[c] += float(g)

    top = sorted(feat_gain.items(), key=lambda kv: kv[1], reverse=True)[:8]
    return IntradayResult(
        ticker=ticker, n_features=len(cols), n_bars=n,
        n_folds=len(fold_accs),
        mean_directional_accuracy=float(np.mean(fold_accs)) if fold_accs else float("nan"),
        std_directional_accuracy=float(np.std(fold_accs)) if fold_accs else float("nan"),
        mean_balanced_accuracy=float(np.mean(bal_accs)) if bal_accs else float("nan"),
        fold_accuracies=fold_accs,
        top_features=[k for k, _ in top],
    )


def run_intraday_for_ticker(
    ticker: str, *, n_folds: int = 5,
) -> Optional[IntradayResult]:
    """Pull hourly bars via multi_timeframe, build features, train."""
    from src.methodology.multi_timeframe import fetch_yf_interval
    df = fetch_yf_interval(ticker, "1h")
    if df is None or df.empty:
        return None
    feats = build_intraday_features(df)
    if feats.empty:
        return None
    labels = next_hour_binary_labels(df["close"])
    return train_intraday_lightgbm(
        feats, labels, ticker=ticker, n_folds=n_folds,
    )
