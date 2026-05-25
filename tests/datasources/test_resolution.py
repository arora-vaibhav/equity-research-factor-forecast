"""Tests for priority-weighted resolution functions (spec §9)."""
from __future__ import annotations

import math
import pytest

from src.common.datasources.resolution import (
    priority_weight,
    weighted_average_value,
    highest_priority_value,
    freshest_value,
    Observation,
)


class TestPriorityWeight:
    def test_rank_1_weight(self):
        assert priority_weight(rank=1, decay=0.5) == 0.5

    def test_rank_2_weight(self):
        assert priority_weight(rank=2, decay=0.5) == 0.25

    def test_rank_3_weight(self):
        assert priority_weight(rank=3, decay=0.5) == 0.125

    def test_invalid_rank_raises(self):
        with pytest.raises(ValueError):
            priority_weight(rank=0, decay=0.5)


class TestWeightedAverageValue:
    def test_three_sources_all_present(self):
        obs = [
            Observation(source="edgar", rank=1, value=24.8),
            Observation(source="yahoo", rank=2, value=25.1),
            Observation(source="finviz", rank=3, value=24.5),
        ]
        result = weighted_average_value(obs, decay=0.5)
        # raw weights: 0.5, 0.25, 0.125 -> normalized: 4/7, 2/7, 1/7
        expected = (4/7)*24.8 + (2/7)*25.1 + (1/7)*24.5
        assert math.isclose(result.value, expected, rel_tol=1e-9)
        assert result.contributors == ["edgar", "yahoo", "finviz"]

    def test_missing_source_renormalizes(self):
        obs = [
            Observation(source="edgar", rank=1, value=24.8),
            Observation(source="finviz", rank=3, value=24.5),
        ]
        result = weighted_average_value(obs, decay=0.5)
        # raw weights: 0.5, 0.125 -> normalized: 4/5, 1/5
        expected = (4/5)*24.8 + (1/5)*24.5
        assert math.isclose(result.value, expected, rel_tol=1e-9)

    def test_outlier_dampened_by_majority(self):
        obs = [
            Observation(source="edgar",         rank=1, value=25.0),
            Observation(source="stockanalysis", rank=2, value=25.1),
            Observation(source="yahoo",         rank=3, value=24.9),
            Observation(source="finviz",        rank=4, value=150.0),
        ]
        result = weighted_average_value(obs, decay=0.5)
        # outlier rank=4 contributes ~1/15 of weight
        assert 30.0 < result.value < 40.0

    def test_empty_observations_returns_none(self):
        result = weighted_average_value([], decay=0.5)
        assert result.value is None
        assert result.contributors == []

    def test_all_null_values_returns_none(self):
        obs = [
            Observation(source="edgar", rank=1, value=None),
            Observation(source="yahoo", rank=2, value=None),
        ]
        result = weighted_average_value(obs, decay=0.5)
        assert result.value is None

    def test_single_source(self):
        obs = [Observation(source="edgar", rank=1, value=42.0)]
        result = weighted_average_value(obs, decay=0.5)
        assert result.value == 42.0
        assert result.contributors == ["edgar"]


class TestHighestPriorityValue:
    def test_picks_lowest_rank(self):
        obs = [
            Observation(source="yahoo",  rank=2, value="Technology"),
            Observation(source="edgar",  rank=1, value="Information Technology"),
            Observation(source="finviz", rank=3, value="Tech"),
        ]
        result = highest_priority_value(obs)
        assert result.value == "Information Technology"
        assert result.contributors == ["edgar"]

    def test_skips_null_high_priority(self):
        obs = [
            Observation(source="edgar",  rank=1, value=None),
            Observation(source="yahoo",  rank=2, value="Technology"),
        ]
        result = highest_priority_value(obs)
        assert result.value == "Technology"
        assert result.contributors == ["yahoo"]

    def test_empty_returns_none(self):
        result = highest_priority_value([])
        assert result.value is None


class TestFreshestValue:
    def test_picks_latest_fetched_at(self):
        obs = [
            Observation(source="finviz", rank=2, value=154.20,
                        fetched_at="2026-05-21T08:00:00Z"),
            Observation(source="yahoo",  rank=1, value=154.18,
                        fetched_at="2026-05-21T07:00:00Z"),
        ]
        result = freshest_value(obs)
        assert result.value == 154.20
        assert result.contributors == ["finviz"]

    def test_tie_broken_by_priority(self):
        obs = [
            Observation(source="finviz", rank=2, value=154.20,
                        fetched_at="2026-05-21T08:00:00Z"),
            Observation(source="yahoo",  rank=1, value=154.18,
                        fetched_at="2026-05-21T08:00:00Z"),
        ]
        result = freshest_value(obs)
        assert result.value == 154.18
        assert result.contributors == ["yahoo"]

    def test_missing_fetched_at_skipped(self):
        obs = [
            Observation(source="finviz", rank=2, value=154.20, fetched_at=None),
            Observation(source="yahoo",  rank=1, value=154.18,
                        fetched_at="2026-05-21T07:00:00Z"),
        ]
        result = freshest_value(obs)
        assert result.value == 154.18
