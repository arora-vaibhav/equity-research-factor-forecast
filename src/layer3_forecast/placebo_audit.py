"""Falsification placebo audit -- Paper #3 (Spurious Predictability
in Financial ML, arXiv 2604.15531).

I want to establish with confidence that the chosen approach is the
best available before reporting an accuracy lift; without this gate
every reported lift is suspect.

Method:
  1. Run forecaster on REAL labels -> real_acc.
  2. Run same pipeline on placebos: shuffled labels, Gaussian-IID
     returns, GARCH(1,1) returns.
  3. Absolute Magnitude Gap (AMG) = real_acc - max(placebo_acc).
  4. AMG > 0 -> audit PASSES (real signal exists).

This is the gate that distinguishes honest 54% from spurious 75%.

Pure functions; no DB.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd


PLACEBO_AUDIT_VERSION = "1.0"


@dataclass
class PlaceboResult:
    placebo_name: str
    n_seeds: int
    accuracies: list[float] = field(default_factory=list)
    mean: float = float("nan")
    max: float = float("nan")
    std: float = float("nan")


@dataclass
class AuditResult:
    ticker: str
    real_directional_accuracy: float
    placebos: list[PlaceboResult] = field(default_factory=list)
    absolute_magnitude_gap: float = float("nan")
    passes_gate: bool = False
    n_seeds: int = 0
    audit_version: str = PLACEBO_AUDIT_VERSION


def shuffled_label_placebo(y: pd.Series, seed: int = 42) -> pd.Series:
    rng = np.random.default_rng(seed)
    arr = y.dropna().to_numpy().copy()
    rng.shuffle(arr)
    out = pd.Series(np.nan, index=y.index)
    out.loc[y.dropna().index] = arr
    return out


def gaussian_iid_placebo_returns(
    n: int, sigma: float = 0.015, seed: int = 42,
) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(0.0, sigma, size=n))


def garch_placebo_returns(
    n: int, omega: float = 1e-5, alpha: float = 0.05, beta: float = 0.90,
    seed: int = 42,
) -> pd.Series:
    rng = np.random.default_rng(seed)
    eps = np.zeros(n); sig2 = np.zeros(n)
    sig2[0] = omega / max(1 - alpha - beta, 1e-6)
    for t in range(1, n):
        sig2[t] = omega + alpha * eps[t - 1] ** 2 + beta * sig2[t - 1]
        eps[t] = rng.normal(0, np.sqrt(sig2[t]))
    return pd.Series(eps)


def derive_binary_from_returns(log_rets: pd.Series) -> pd.Series:
    return np.sign(log_rets.shift(-1).dropna()).astype(int).replace(0, 1)


def run_placebo_audit(
    *,
    ticker: str,
    real_directional_accuracy: float,
    placebo_runner: Callable[[pd.Series], float],
    n_seeds: int = 5,
    placebo_kinds: tuple[str, ...] = ("shuffled", "gaussian_iid", "garch"),
    n_samples: int = 1000,
    real_y: Optional[pd.Series] = None,
) -> AuditResult:
    """`placebo_runner` is a closure: given a placebo label Series,
    return directional accuracy as a float."""
    out = AuditResult(
        ticker=ticker,
        real_directional_accuracy=real_directional_accuracy,
        n_seeds=n_seeds,
    )

    placebo_max = -float("inf")
    for kind in placebo_kinds:
        accs = []
        for seed in range(n_seeds):
            try:
                if kind == "shuffled" and real_y is not None:
                    y_placebo = shuffled_label_placebo(real_y, seed=seed)
                elif kind == "gaussian_iid":
                    rets = gaussian_iid_placebo_returns(n_samples, seed=seed)
                    y_placebo = derive_binary_from_returns(rets)
                elif kind == "garch":
                    rets = garch_placebo_returns(n_samples, seed=seed)
                    y_placebo = derive_binary_from_returns(rets)
                else:
                    continue
                acc = float(placebo_runner(y_placebo))
                if np.isfinite(acc):
                    accs.append(acc)
            except Exception:
                continue

        pr = PlaceboResult(placebo_name=kind, n_seeds=len(accs), accuracies=accs)
        if accs:
            pr.mean = float(np.mean(accs))
            pr.max = float(np.max(accs))
            pr.std = float(np.std(accs))
            placebo_max = max(placebo_max, pr.max)
        out.placebos.append(pr)

    if np.isfinite(placebo_max):
        out.absolute_magnitude_gap = real_directional_accuracy - placebo_max
        out.passes_gate = out.absolute_magnitude_gap > 0.0
    return out
