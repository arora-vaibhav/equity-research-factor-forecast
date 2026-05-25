"""Sensitivity analysis -- one-factor-at-a-time (OFAT) tornado + stress.

A tornado plot showing how the forecast point return moves when each
factor input shifts by +/-1 std. Standard practice (MSCI / Two Sigma
Venn) for a single-stock report.

For the linear forecast r_hat = alpha + sum_i (w_i * f_i):
  marginal sensitivity = w_i
  +1 sd shock impact   = w_i * sigma_i (default sigma=1 for z-scored
                                          factors)

Pure function; no DB, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


SENSITIVITY_VERSION = "1.0"


@dataclass
class SensitivityRow:
    factor: str
    loading: float
    ticker_value: float
    impact_plus_1sd: float
    impact_minus_1sd: float


@dataclass
class StressScenario:
    name: str
    description: str
    impact: float


@dataclass
class SensitivityResult:
    ticker: str
    base_point_return: float
    factor_sensitivities: list[SensitivityRow] = field(default_factory=list)
    stress_scenarios: list[StressScenario] = field(default_factory=list)
    sensitivity_version: str = SENSITIVITY_VERSION


def compute_tornado(
    *,
    ticker: str,
    base_point_return: float,
    loadings: pd.Series,
    ticker_factors: dict[str, float],
    factor_sigmas: dict[str, float] | None = None,
    one_sd: float = 1.0,
) -> list[SensitivityRow]:
    """OFAT tornado: +/-1 sigma shock per factor. Sorted by |impact|."""
    rows: list[SensitivityRow] = []
    for factor in loadings.index:
        w = float(loadings[factor])
        sigma = float((factor_sigmas or {}).get(factor, 1.0))
        impact_plus = w * one_sd * sigma
        rows.append(SensitivityRow(
            factor=factor,
            loading=w,
            ticker_value=float(ticker_factors.get(factor, 0.0)),
            impact_plus_1sd=impact_plus,
            impact_minus_1sd=-impact_plus,
        ))
    rows.sort(key=lambda r: abs(r.impact_plus_1sd), reverse=True)
    return rows


def compute_stress_scenarios(
    *,
    base_point_return: float,
    loadings: pd.Series,
    ticker_factors: dict[str, float],
) -> list[StressScenario]:
    """Three canned stress scenarios for a single-stock report."""
    scenarios: list[StressScenario] = []

    def _shock(shocks: dict[str, float]) -> float:
        delta = 0.0
        for factor, dz in shocks.items():
            if factor in loadings.index:
                delta += float(loadings[factor]) * dz
        return delta

    scenarios.append(StressScenario(
        name="rates_+100bps",
        description="Yield curve +100bps (lowvol -1sd, value -0.5sd)",
        impact=base_point_return + _shock({"lowvol_score": -1.0, "value_score": -0.5}),
    ))
    scenarios.append(StressScenario(
        name="recession_risk_off",
        description="Recession (momentum -1.5sd, lowvol +1sd, news -1sd)",
        impact=base_point_return + _shock({
            "momentum_score": -1.5, "lowvol_score": 1.0,
            "news_activity_score": -1.0,
        }),
    ))
    scenarios.append(StressScenario(
        name="quality_flight",
        description="Quality flight (quality +1sd, revisions +1sd, momentum -0.5sd)",
        impact=base_point_return + _shock({
            "quality_score": 1.0, "revisions_score": 1.0,
            "momentum_score": -0.5,
        }),
    ))
    return scenarios


def run_sensitivity(
    *,
    ticker: str,
    base_point_return: float,
    loadings: pd.Series,
    ticker_factors: dict[str, float],
    factor_sigmas: dict[str, float] | None = None,
) -> SensitivityResult:
    tornado = compute_tornado(
        ticker=ticker, base_point_return=base_point_return,
        loadings=loadings, ticker_factors=ticker_factors,
        factor_sigmas=factor_sigmas,
    )
    stress = compute_stress_scenarios(
        base_point_return=base_point_return,
        loadings=loadings, ticker_factors=ticker_factors,
    )
    return SensitivityResult(
        ticker=ticker, base_point_return=base_point_return,
        factor_sensitivities=tornado, stress_scenarios=stress,
    )
