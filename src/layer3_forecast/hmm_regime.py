"""HMM regime-conditioning for the ML forecaster (v6).

Per paper #2 ("Regime-Aware LightGBM", MDPI Electronics 15(6):1334):
3-state Gaussian HMM on (return, abs return, realized vol), refit
every 63 bars, exposing regime_id + 3 regime-probability columns to
LightGBM as features. Expected lift: +0.8 to +1.5 pp.

Anti-leakage: regime probabilities at row t use only data up to t.

Pure functions; no DB.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


HMM_REGIME_VERSION = "1.0"


@dataclass
class HMMRegimeFeatures:
    regime_id: pd.Series
    regime_prob_0: pd.Series
    regime_prob_1: pd.Series
    regime_prob_2: pd.Series
    hmm_regime_version: str = HMM_REGIME_VERSION


def _hmm_input_features(close: pd.Series) -> pd.DataFrame:
    log_ret = np.log(close / close.shift(1))
    abs_ret = log_ret.abs()
    vol_20 = log_ret.rolling(20).std()
    return pd.DataFrame({
        "log_ret": log_ret, "abs_ret": abs_ret, "vol_20": vol_20,
    }).dropna()


def compute_regime_features(
    close: pd.Series,
    *,
    n_states: int = 3,
    refit_cadence: int = 63,
    seed: int = 42,
) -> Optional[HMMRegimeFeatures]:
    """Causal regime probabilities; refit every `refit_cadence` bars."""
    from hmmlearn.hmm import GaussianHMM
    feats = _hmm_input_features(close)
    if len(feats) < 200:
        return None

    X = feats.to_numpy()
    n = len(X)
    regime_id = np.full(n, -1, dtype=int)
    regime_proba = np.full((n, n_states), np.nan)

    warmup = max(120, refit_cadence + 30)
    if warmup >= n:
        return None

    model = None
    last_fit_at = -1
    for t in range(warmup, n):
        if last_fit_at < 0 or (t - last_fit_at) >= refit_cadence:
            try:
                m = GaussianHMM(
                    n_components=n_states, covariance_type="full",
                    n_iter=100, random_state=seed,
                )
                m.fit(X[:t])
                model = m
                last_fit_at = t
            except Exception:
                continue
        if model is None:
            continue
        try:
            post = model.predict_proba(X[:t + 1])[-1]
            regime_proba[t] = post
            regime_id[t] = int(np.argmax(post))
        except Exception:
            continue

    # Stable labeling: state 0 = lowest mean return (bear), state n-1 = bull
    if model is not None:
        try:
            order = np.argsort(model.means_[:, 0])
            inv_order = np.argsort(order)
            regime_proba = regime_proba[:, order]
            mask = regime_id >= 0
            regime_id = np.where(mask, inv_order[regime_id], regime_id)
        except Exception:
            pass

    idx = feats.index
    return HMMRegimeFeatures(
        regime_id=pd.Series(regime_id, index=idx, name="hmm_regime_id"),
        regime_prob_0=pd.Series(regime_proba[:, 0], index=idx, name="hmm_regime_prob_0"),
        regime_prob_1=pd.Series(regime_proba[:, 1], index=idx, name="hmm_regime_prob_1"),
        regime_prob_2=pd.Series(regime_proba[:, 2], index=idx, name="hmm_regime_prob_2"),
    )


def regime_feature_columns(rf: HMMRegimeFeatures) -> pd.DataFrame:
    return pd.concat([
        rf.regime_id.astype(float),
        rf.regime_prob_0, rf.regime_prob_1, rf.regime_prob_2,
    ], axis=1)
