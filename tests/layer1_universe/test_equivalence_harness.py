"""Tests for src.layer1_universe.equivalence_harness (A.3 produced;
A.5 added the threshold assertion).

Coverage:
  * equivalence_report on a freshly migrated DB returns status='no_data'.
  * assert_equivalence_within_threshold:
    - empty/no-data report passes.
    - all-within-threshold report passes.
    - any-column-over-threshold report raises AssertionError with the
      column name in the message.
    - categorical disagreement (max_abs_drift=None) raises.
    - columns whitelist limits the check.
    - tightening to 1 % catches drift the 5 % default ignored.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.layer1_universe.equivalence_harness import (
    assert_equivalence_within_threshold,
    equivalence_report,
)


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(db_path=str(tmp_path / "test.db"))
    d.migrate_to_v13()
    return d


def test_report_empty_db_returns_no_data(db):
    rep = equivalence_report("run-xxx", db)
    assert rep["status"] == "no_data"
    assert rep["row_counts"]["v1_universe_members"] == 0
    assert rep["row_counts"]["v2_canonical_universe"] == 0


def _report(per_column: dict) -> dict:
    return {
        "run_id": "run-xxx",
        "generated_at": "2026-05-23T00:00:00Z",
        "row_counts": {
            "v1_universe_members": 1, "v2_canonical_universe": 1,
            "shared_tickers": 1,
        },
        "per_column": per_column,
        "status": "ok",
    }


def test_assert_passes_when_all_under_threshold():
    rep = _report({
        "pe_ttm": {"max_abs_drift": 0.001, "n_disagreements": 1},
        "market_cap_usd": {"max_abs_drift": 0.0, "n_disagreements": 0},
    })
    assert_equivalence_within_threshold(rep, max_drift_pct=0.05)


def test_assert_raises_when_column_exceeds_threshold():
    rep = _report({
        "pe_ttm": {"max_abs_drift": 0.10, "n_disagreements": 1},
        "market_cap_usd": {"max_abs_drift": 0.0, "n_disagreements": 0},
    })
    with pytest.raises(AssertionError, match="pe_ttm"):
        assert_equivalence_within_threshold(rep, max_drift_pct=0.05)


def test_assert_raises_on_categorical_disagreement():
    rep = _report({
        "sector": {"max_abs_drift": None, "n_disagreements": 1},
    })
    with pytest.raises(AssertionError, match="sector"):
        assert_equivalence_within_threshold(rep, max_drift_pct=0.05)


def test_assert_ignores_categorical_when_no_disagreement():
    rep = _report({
        "sector": {"max_abs_drift": None, "n_disagreements": 0},
    })
    assert_equivalence_within_threshold(rep, max_drift_pct=0.05)


def test_columns_whitelist_limits_check():
    rep = _report({
        "pe_ttm": {"max_abs_drift": 0.10, "n_disagreements": 1},
        "market_cap_usd": {"max_abs_drift": 0.0, "n_disagreements": 0},
    })
    # pe_ttm would fail, but limiting to market_cap_usd makes the check pass.
    assert_equivalence_within_threshold(
        rep, max_drift_pct=0.05, columns=["market_cap_usd"],
    )


def test_empty_report_passes():
    rep = _report({})
    assert_equivalence_within_threshold(rep, max_drift_pct=0.05)


def test_tightened_threshold():
    """The 1 % tightened threshold catches drift the 5 % default ignored."""
    rep = _report({
        "pe_ttm": {"max_abs_drift": 0.03, "n_disagreements": 1},
    })
    assert_equivalence_within_threshold(rep, max_drift_pct=0.05)
    with pytest.raises(AssertionError, match="pe_ttm"):
        assert_equivalence_within_threshold(rep, max_drift_pct=0.01)
