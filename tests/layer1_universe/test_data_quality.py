"""Tests for src.layer1_universe.data_quality (Phase A.5).

Coverage:
  * detect_field_corruption -- per-field bounds (market_cap, pe, rsi
    etc.); None/NaN/Inf treated as missing; unknown fields pass
    through; non-numeric where numeric expected flagged.
  * flag_corrupt_observations -- mixed input emits weight=0 audit
    rows for corrupt entries, skips sane entries; preserves
    raw_value; required field guards.
"""
from __future__ import annotations

import math

import pytest

from src.layer1_universe.data_quality import (
    DATA_QUALITY_VERSION,
    FIELD_BOUNDS,
    detect_field_corruption,
    flag_corrupt_observations,
)


def test_version_constant():
    assert DATA_QUALITY_VERSION == "1.0"


# ---------------------------------------------------------------------------
# detect_field_corruption -- per-field bounds
# ---------------------------------------------------------------------------


class TestDetectFieldCorruption:
    def test_unknown_field_passes(self):
        is_c, reason = detect_field_corruption("AAPL", "made_up_field", 1e20)
        assert is_c is False
        assert reason is None

    def test_none_value_is_missing_not_corrupt(self):
        is_c, reason = detect_field_corruption("AAPL", "market_cap_usd", None)
        assert is_c is False
        assert reason is None

    def test_nan_value_is_missing_not_corrupt(self):
        is_c, _ = detect_field_corruption("AAPL", "market_cap_usd", float("nan"))
        assert is_c is False

    def test_inf_value_is_missing_not_corrupt(self):
        is_c, _ = detect_field_corruption("AAPL", "market_cap_usd", float("inf"))
        assert is_c is False

    def test_non_numeric_string_is_corrupt(self):
        is_c, reason = detect_field_corruption("AAPL", "market_cap_usd", "garbage")
        assert is_c is True
        assert "non_numeric" in reason

    def test_market_cap_negative_corrupt(self):
        is_c, reason = detect_field_corruption("AAPL", "market_cap_usd", -1.0)
        assert is_c is True
        assert "below_low_bound" in reason

    def test_market_cap_too_large_corrupt(self):
        is_c, reason = detect_field_corruption(
            "AAPL", "market_cap_usd", 1e15,
        )
        assert is_c is True
        assert "above_high_bound" in reason

    def test_market_cap_in_range_passes(self):
        is_c, _ = detect_field_corruption(
            "AAPL", "market_cap_usd", 3_000_000_000_000.0,
        )
        assert is_c is False

    def test_rsi_above_100_corrupt(self):
        is_c, _ = detect_field_corruption("AAPL", "rsi_14", 150.0)
        assert is_c is True

    def test_rsi_below_0_corrupt(self):
        is_c, _ = detect_field_corruption("AAPL", "rsi_14", -5.0)
        assert is_c is True

    def test_rsi_in_range_passes(self):
        is_c, _ = detect_field_corruption("AAPL", "rsi_14", 55.0)
        assert is_c is False

    def test_pe_negative_allowed(self):
        is_c, _ = detect_field_corruption("XYZ", "pe_ttm", -50.0)
        assert is_c is False

    def test_pe_too_negative_corrupt(self):
        is_c, _ = detect_field_corruption("XYZ", "pe_ttm", -5000.0)
        assert is_c is True

    def test_dist_52w_high_must_be_non_positive(self):
        is_c, _ = detect_field_corruption("AAPL", "dist_52w_high", 0.10)
        assert is_c is True

    def test_short_interest_above_100pct_corrupt(self):
        is_c, _ = detect_field_corruption(
            "GME", "short_interest_pct_float", 1.5,
        )
        assert is_c is True

    def test_custom_bounds_override(self):
        # Override the bounds: market_cap = 0 is normally corrupt
        # but with a custom bound (0, None) it's allowed.
        is_c, _ = detect_field_corruption(
            "X", "market_cap_usd", 0.0,
            bounds={"market_cap_usd": (0.0, None)},
        )
        assert is_c is False


# ---------------------------------------------------------------------------
# flag_corrupt_observations
# ---------------------------------------------------------------------------


class TestFlagCorruptObservations:
    def test_empty_input(self):
        out = flag_corrupt_observations(
            [], run_id="r1", fetched_at="2026-05-23T00:00:00Z",
        )
        assert out == []

    def test_mixed_input_emits_only_corrupt_audit_rows(self):
        observations = [
            {
                "ticker": "AAPL", "field": "market_cap_usd",
                "source": "yahoo",
                "raw_value": "3000000000000", "parsed_value": 3e12,
            },  # sane
            {
                "ticker": "MSFT", "field": "price",
                "source": "yahoo",
                "raw_value": "-5.00", "parsed_value": -5.0,
            },  # corrupt -- negative price
            {
                "ticker": "GOOG", "field": "rsi_14",
                "source": "finviz",
                "raw_value": "200", "parsed_value": 200.0,
            },  # corrupt -- RSI > 100
        ]
        out = flag_corrupt_observations(
            observations,
            run_id="r1", fetched_at="2026-05-23T00:00:00Z",
        )
        assert len(out) == 2
        tickers = {r.ticker for r in out}
        assert tickers == {"MSFT", "GOOG"}
        for row in out:
            assert row.weight == 0.0
            assert row.contributed_to_canonical is False
            assert row.run_id == "r1"
            assert row.fetched_at == "2026-05-23T00:00:00Z"
            assert row.disagreement_pct is None

    def test_preserves_raw_value_string(self):
        observations = [
            {
                "ticker": "MSFT", "field": "price",
                "source": "yahoo",
                "raw_value": "  -5.00  ", "parsed_value": -5.0,
            },
        ]
        out = flag_corrupt_observations(
            observations, run_id="r1", fetched_at="2026-05-23T00:00:00Z",
        )
        assert len(out) == 1
        assert "-5.00" in out[0].raw_value

    def test_missing_required_keys_skipped(self):
        observations = [
            {"ticker": "MSFT"},  # missing field + source
            {"field": "price"},  # missing ticker
            {
                "ticker": "GOOG", "field": "rsi_14",
                "source": "finviz",
                "raw_value": "200", "parsed_value": 200.0,
            },
        ]
        out = flag_corrupt_observations(
            observations, run_id="r1", fetched_at="2026-05-23T00:00:00Z",
        )
        assert len(out) == 1
        assert out[0].ticker == "GOOG"

    def test_parsed_value_preserved_for_audit(self):
        observations = [
            {
                "ticker": "MSFT", "field": "price",
                "source": "yahoo",
                "raw_value": "-5.00", "parsed_value": -5.0,
            },
        ]
        out = flag_corrupt_observations(
            observations, run_id="r1", fetched_at="2026-05-23T00:00:00Z",
        )
        assert out[0].parsed_value == -5.0

    def test_non_finite_parsed_yields_none(self):
        observations = [
            {
                "ticker": "AAPL", "field": "market_cap_usd",
                "source": "yahoo",
                "raw_value": "not_a_number", "parsed_value": "not_a_number",
            },
        ]
        out = flag_corrupt_observations(
            observations, run_id="r1", fetched_at="2026-05-23T00:00:00Z",
        )
        assert len(out) == 1
        assert out[0].parsed_value is None


# ---------------------------------------------------------------------------
# FIELD_BOUNDS sanity
# ---------------------------------------------------------------------------


def test_field_bounds_keys_subset_of_canonical_columns():
    """The bounds map should reference canonical_universe field names."""
    expected_subset = {
        "market_cap_usd", "price", "avg_daily_volume",
        "pe_ttm", "pe_forward",
        "operating_margin", "net_profit_margin",
        "rsi_14", "dist_52w_high", "dist_52w_low",
        "short_interest_pct_float", "news_activity_score",
    }
    assert expected_subset.issubset(FIELD_BOUNDS.keys())
