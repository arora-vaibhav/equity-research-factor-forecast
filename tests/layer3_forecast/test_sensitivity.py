"""Unit tests for src.layer3_forecast.sensitivity."""
from __future__ import annotations

import pandas as pd
import pytest

from src.layer3_forecast.sensitivity import (
    SENSITIVITY_VERSION,
    SensitivityResult,
    SensitivityRow,
    StressScenario,
    compute_stress_scenarios,
    compute_tornado,
    run_sensitivity,
)


@pytest.fixture
def sample_loadings() -> pd.Series:
    return pd.Series(
        {
            "value_score":          0.05,
            "quality_score":        0.02,
            "momentum_score":       0.04,
            "lowvol_score":         0.01,
            "revisions_score":      0.025,
            "news_activity_score":  0.06,
        }
    )


@pytest.fixture
def sample_ticker_factors() -> dict[str, float]:
    return {
        "value_score":          0.5,
        "quality_score":       -0.2,
        "momentum_score":       1.1,
        "lowvol_score":         0.0,
        "revisions_score":     -0.4,
        "news_activity_score":  0.7,
    }


class TestComputeTornado:
    def test_returns_one_row_per_loading(self, sample_loadings, sample_ticker_factors):
        rows = compute_tornado(
            ticker="X", base_point_return=0.02,
            loadings=sample_loadings, ticker_factors=sample_ticker_factors,
        )
        assert len(rows) == len(sample_loadings)
        assert {r.factor for r in rows} == set(sample_loadings.index)

    def test_impact_equals_loading_times_one_sd(
        self, sample_loadings, sample_ticker_factors,
    ):
        rows = compute_tornado(
            ticker="X", base_point_return=0.0,
            loadings=sample_loadings, ticker_factors=sample_ticker_factors,
            one_sd=1.0,
        )
        for r in rows:
            assert r.impact_plus_1sd == pytest.approx(r.loading)
            assert r.impact_minus_1sd == pytest.approx(-r.loading)

    def test_sorted_by_abs_impact_descending(
        self, sample_loadings, sample_ticker_factors,
    ):
        rows = compute_tornado(
            ticker="X", base_point_return=0.0,
            loadings=sample_loadings, ticker_factors=sample_ticker_factors,
        )
        abs_impacts = [abs(r.impact_plus_1sd) for r in rows]
        assert abs_impacts == sorted(abs_impacts, reverse=True)

    def test_factor_sigmas_overrides_default(
        self, sample_loadings, sample_ticker_factors,
    ):
        sigmas = {"value_score": 2.0, "momentum_score": 0.5}
        rows = compute_tornado(
            ticker="X", base_point_return=0.0,
            loadings=sample_loadings, ticker_factors=sample_ticker_factors,
            factor_sigmas=sigmas, one_sd=1.0,
        )
        by_name = {r.factor: r for r in rows}
        assert by_name["value_score"].impact_plus_1sd == pytest.approx(
            sample_loadings["value_score"] * 2.0
        )
        assert by_name["momentum_score"].impact_plus_1sd == pytest.approx(
            sample_loadings["momentum_score"] * 0.5
        )
        # untouched factor keeps default sigma=1
        assert by_name["quality_score"].impact_plus_1sd == pytest.approx(
            sample_loadings["quality_score"]
        )

    def test_ticker_value_preserved(
        self, sample_loadings, sample_ticker_factors,
    ):
        rows = compute_tornado(
            ticker="X", base_point_return=0.0,
            loadings=sample_loadings, ticker_factors=sample_ticker_factors,
        )
        by_name = {r.factor: r for r in rows}
        for f, v in sample_ticker_factors.items():
            assert by_name[f].ticker_value == pytest.approx(v)


class TestComputeStressScenarios:
    def test_three_scenarios(self, sample_loadings, sample_ticker_factors):
        scenarios = compute_stress_scenarios(
            base_point_return=0.02,
            loadings=sample_loadings, ticker_factors=sample_ticker_factors,
        )
        names = {s.name for s in scenarios}
        assert "rates_+100bps" in names
        assert "recession_risk_off" in names
        assert "quality_flight" in names
        assert len(scenarios) == 3

    def test_rates_shock_uses_lowvol_and_value(
        self, sample_loadings, sample_ticker_factors,
    ):
        scenarios = compute_stress_scenarios(
            base_point_return=0.0,
            loadings=sample_loadings, ticker_factors=sample_ticker_factors,
        )
        by_name = {s.name: s for s in scenarios}
        # rates_+100bps applies -1sd lowvol + -0.5sd value
        expected = (
            sample_loadings["lowvol_score"] * -1.0
            + sample_loadings["value_score"] * -0.5
        )
        assert by_name["rates_+100bps"].impact == pytest.approx(expected)

    def test_quality_flight_uses_quality_revisions_momentum(
        self, sample_loadings, sample_ticker_factors,
    ):
        scenarios = compute_stress_scenarios(
            base_point_return=0.0,
            loadings=sample_loadings, ticker_factors=sample_ticker_factors,
        )
        by_name = {s.name: s for s in scenarios}
        expected = (
            sample_loadings["quality_score"] * 1.0
            + sample_loadings["revisions_score"] * 1.0
            + sample_loadings["momentum_score"] * -0.5
        )
        assert by_name["quality_flight"].impact == pytest.approx(expected)

    def test_missing_factor_in_loadings_skipped(
        self, sample_ticker_factors,
    ):
        partial_loadings = pd.Series({"value_score": 0.05})
        scenarios = compute_stress_scenarios(
            base_point_return=0.0,
            loadings=partial_loadings, ticker_factors=sample_ticker_factors,
        )
        by_name = {s.name: s for s in scenarios}
        # rates_+100bps only applies to value_score (lowvol_score absent)
        assert by_name["rates_+100bps"].impact == pytest.approx(
            partial_loadings["value_score"] * -0.5
        )


class TestRunSensitivity:
    def test_result_has_tornado_and_scenarios(
        self, sample_loadings, sample_ticker_factors,
    ):
        result = run_sensitivity(
            ticker="X", base_point_return=0.03,
            loadings=sample_loadings, ticker_factors=sample_ticker_factors,
        )
        assert isinstance(result, SensitivityResult)
        assert result.ticker == "X"
        assert result.base_point_return == pytest.approx(0.03)
        assert len(result.factor_sensitivities) == len(sample_loadings)
        assert len(result.stress_scenarios) == 3
        assert result.sensitivity_version == SENSITIVITY_VERSION


class TestSensitivityRowDataclass:
    def test_row_construction(self) -> None:
        r = SensitivityRow(
            factor="value_score", loading=0.05, ticker_value=0.5,
            impact_plus_1sd=0.05, impact_minus_1sd=-0.05,
        )
        assert r.factor == "value_score"
        assert r.loading == 0.05
