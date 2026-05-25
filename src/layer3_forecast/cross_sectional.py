"""Cross-sectional rank-target trainer.

I added this so the model predicts relative rank within the universe
rather than absolute next-day direction.

What this changes vs the prior LightGBM:
  prior predicts: P(stock_X up tomorrow)         -- absolute direction
  this  predicts: rank of stock_X tomorrow within universe -- RELATIVE

Why relative beats absolute on daily horizons:
  * Removes market beta noise.
  * Long-short portfolio implementable: long top decile, short bottom
    decile -> beta-neutral payoff.
  * Standard hedge-fund convention (Asness-Moskowitz-Pedersen 2013;
    Gu-Kelly-Xiu 2020 RFS treats it this way).

Pure functions; no DB.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


CROSS_SECTIONAL_VERSION = "1.0"


@dataclass
class CrossSectionalResult:
    n_tickers: int
    n_dates: int
    n_folds: int
    horizon_days: int
    mean_rank_ic: float         # Spearman(pred_rank, y_rank) per date, averaged
    std_rank_ic: float
    mean_ls_spread_ann: float   # long-short top/bottom decile spread, annualised
    fold_rank_ics: list[float] = field(default_factory=list)
    top_features: list[str] = field(default_factory=list)
    cross_sectional_version: str = CROSS_SECTIONAL_VERSION


def make_panel_returns(
    ticker_close_panel: pd.DataFrame, *, horizon_days: int = 1,
) -> pd.DataFrame:
    """Per-(date, ticker) forward h-day log return."""
    log_close = np.log(ticker_close_panel)
    return (log_close.shift(-horizon_days) - log_close).iloc[:-horizon_days]


def make_rank_targets(forward_returns_panel: pd.DataFrame) -> pd.DataFrame:
    """Per-date cross-sectional rank, centred at 0.

    Smaller rank = better forward performer. Range ~ [-0.5, +0.5].
    """
    ranks = forward_returns_panel.rank(axis=1, pct=True, ascending=False)
    return ranks - 0.5


def stack_panel_long(
    feature_panels: dict[str, pd.DataFrame],
    rank_target_panel: pd.DataFrame,
) -> pd.DataFrame:
    """Pivot wide panels into long (date, ticker) -> feature dict."""
    pieces = []
    for name, panel in feature_panels.items():
        pieces.append(panel.stack().rename(name))
    pieces.append(rank_target_panel.stack().rename("y_rank"))
    df = pd.concat(pieces, axis=1).dropna()
    df.index.names = ["date", "ticker"]
    return df.reset_index()


def train_cross_sectional_ranker(
    long_df: pd.DataFrame,
    *,
    n_folds: int = 5,
    horizon_days: int = 1,
    embargo_days: int = 5,
    num_leaves: int = 63,
    max_depth: int = 6,
    learning_rate: float = 0.02,
    n_estimators: int = 1000,
    early_stopping_rounds: int = 50,
    min_data_in_leaf: int = 200,
    seed: int = 42,
) -> CrossSectionalResult:
    """Walk-forward LightGBM regressor on rank targets."""
    import lightgbm as lgb

    feat_cols = [c for c in long_df.columns
                 if c not in ("date", "ticker", "y_rank")]
    df = long_df.dropna(subset=feat_cols + ["y_rank"]).copy()
    df = df.sort_values(["date", "ticker"])
    dates = sorted(df["date"].unique())
    n_dates = len(dates)
    if n_dates < 200 or len(feat_cols) < 3:
        return CrossSectionalResult(
            n_tickers=df["ticker"].nunique(),
            n_dates=n_dates, n_folds=0, horizon_days=horizon_days,
            mean_rank_ic=float("nan"), std_rank_ic=float("nan"),
            mean_ls_spread_ann=float("nan"),
        )

    fold_size = max((n_dates - 100) // (n_folds + 1), 30)
    initial_train_end = 100 + fold_size
    fold_rank_ics: list[float] = []
    ls_spreads: list[float] = []
    feat_gain = {c: 0.0 for c in feat_cols}

    for k in range(n_folds):
        test_start = initial_train_end + k * fold_size
        test_end = min(test_start + fold_size, n_dates)
        if test_start >= n_dates - 10:
            break
        train_end = test_start - horizon_days - embargo_days
        if train_end <= 100:
            continue

        train_dates = dates[:train_end]
        test_dates = dates[test_start:test_end]
        tr = df[df["date"].isin(train_dates)]
        te = df[df["date"].isin(test_dates)]
        if len(tr) < 200 or len(te) < 50:
            continue

        params = {
            "objective": "regression_l1", "metric": "l1",
            "num_leaves": num_leaves, "max_depth": max_depth,
            "learning_rate": learning_rate,
            "min_data_in_leaf": min_data_in_leaf,
            "feature_fraction": 0.7, "bagging_fraction": 0.8,
            "bagging_freq": 5, "lambda_l2": 1.0,
            "verbose": -1, "seed": seed,
        }
        dtrain = lgb.Dataset(
            tr[feat_cols].to_numpy(dtype=np.float32),
            label=tr["y_rank"].to_numpy(dtype=np.float32),
            feature_name=feat_cols,
        )
        dvalid = lgb.Dataset(
            te[feat_cols].to_numpy(dtype=np.float32),
            label=te["y_rank"].to_numpy(dtype=np.float32),
            reference=dtrain, feature_name=feat_cols,
        )
        model = lgb.train(
            params, dtrain, num_boost_round=n_estimators,
            valid_sets=[dvalid],
            callbacks=[lgb.early_stopping(early_stopping_rounds),
                       lgb.log_evaluation(0)],
        )
        pred = model.predict(te[feat_cols].to_numpy(dtype=np.float32))
        te = te.copy()
        te["pred_rank"] = pred

        date_ics = []
        per_date_ls = []
        for d, sub in te.groupby("date"):
            if len(sub) < 10:
                continue
            rho = sub[["pred_rank", "y_rank"]].corr(method="spearman").iloc[0, 1]
            if np.isfinite(rho):
                date_ics.append(float(rho))
            n_decile = max(len(sub) // 10, 1)
            # smaller pred_rank = predicted top performer
            top = sub.nsmallest(n_decile, "pred_rank")
            bot = sub.nlargest(n_decile, "pred_rank")
            top_real = float(top["y_rank"].mean())
            bot_real = float(bot["y_rank"].mean())
            # y_rank centred at 0; smaller = better forward perf.
            # Spread (bot - top) > 0 when prediction works.
            per_date_ls.append(bot_real - top_real)
        if date_ics:
            fold_rank_ics.append(float(np.mean(date_ics)))
        if per_date_ls:
            ann = float(np.mean(per_date_ls)) * 252
            ls_spreads.append(ann)

        gain = model.feature_importance(importance_type="gain")
        for c, g in zip(feat_cols, gain):
            feat_gain[c] += float(g)

    top = sorted(feat_gain.items(), key=lambda kv: kv[1], reverse=True)[:8]
    return CrossSectionalResult(
        n_tickers=df["ticker"].nunique(),
        n_dates=n_dates, n_folds=len(fold_rank_ics),
        horizon_days=horizon_days,
        mean_rank_ic=float(np.mean(fold_rank_ics)) if fold_rank_ics else float("nan"),
        std_rank_ic=float(np.std(fold_rank_ics)) if fold_rank_ics else float("nan"),
        mean_ls_spread_ann=float(np.mean(ls_spreads)) if ls_spreads else float("nan"),
        fold_rank_ics=fold_rank_ics,
        top_features=[k for k, _ in top],
    )
