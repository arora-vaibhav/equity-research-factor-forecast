"""Tests for Chan-Jegadeesh-Lakonishok (1996) analyst-revision velocity module.

Caller: pytest. No data files read/written; pure-function exercise with
synthetic dicts shaped {'ticker', 'field_name', 'value', 'scrape_timestamp'}.
"""
from __future__ import annotations

import math

import pytest

from src.methodology.revisions_velocity import (
    REVISIONS_VELOCITY_VERSION,
    compute_revisions_velocity,
)


def test_version_constant():
    assert REVISIONS_VELOCITY_VERSION == "1.0"


def _mkrow(ticker: str, field: str, value: float, ts: str) -> dict:
    return {
        "ticker": ticker,
        "field_name": field,
        "value": value,
        "scrape_timestamp": ts,
    }


def test_net_upward_signal_positive():
    rows = [
        _mkrow("AAPL", "eps_revision_direction_count", 3.0, "2026-05-01"),
        _mkrow("AAPL", "eps_revision_direction_count", 4.0, "2026-05-10"),
        _mkrow("AAPL", "eps_revision_direction_count", 5.0, "2026-05-15"),
    ]
    out = compute_revisions_velocity(rows, as_of_date="2026-05-20", lookback_days=90,
                                     dispersion_penalty_lambda=0.0)
    assert out["AAPL"] > 0


def test_net_downward_signal_negative():
    rows = [
        _mkrow("AAPL", "eps_revision_direction_count", -3.0, "2026-05-01"),
        _mkrow("AAPL", "eps_revision_direction_count", -4.0, "2026-05-10"),
        _mkrow("AAPL", "eps_revision_direction_count", -5.0, "2026-05-15"),
    ]
    out = compute_revisions_velocity(rows, as_of_date="2026-05-20", lookback_days=90,
                                     dispersion_penalty_lambda=0.0)
    assert out["AAPL"] < 0


def test_zero_net_returns_zero():
    rows = [
        _mkrow("AAPL", "eps_revision_direction_count", 0.0, "2026-05-01"),
        _mkrow("AAPL", "eps_revision_direction_count", 0.0, "2026-05-10"),
        _mkrow("AAPL", "eps_revision_direction_count", 0.0, "2026-05-15"),
    ]
    out = compute_revisions_velocity(rows, as_of_date="2026-05-20", lookback_days=90,
                                     dispersion_penalty_lambda=0.0)
    assert out["AAPL"] == 0.0


def test_dispersion_penalty_reduces_magnitude():
    """Two runs with identical direction counts but one has dispersed TPs."""
    base_dir = [
        _mkrow("AAPL", "eps_revision_direction_count", 5.0, "2026-05-01"),
        _mkrow("AAPL", "eps_revision_direction_count", 5.0, "2026-05-10"),
        _mkrow("AAPL", "eps_revision_direction_count", 5.0, "2026-05-15"),
    ]
    no_dispersion = base_dir + [
        _mkrow("AAPL", "eps_estimate_current", 2.0, "2026-05-01"),
        _mkrow("AAPL", "eps_estimate_current", 2.0, "2026-05-10"),
        _mkrow("AAPL", "eps_estimate_current", 2.0, "2026-05-15"),
    ]
    with_dispersion = base_dir + [
        _mkrow("AAPL", "eps_estimate_current", 1.0, "2026-05-01"),
        _mkrow("AAPL", "eps_estimate_current", 2.0, "2026-05-10"),
        _mkrow("AAPL", "eps_estimate_current", 3.0, "2026-05-15"),
    ]
    out_none = compute_revisions_velocity(no_dispersion, as_of_date="2026-05-20",
                                          lookback_days=90,
                                          dispersion_penalty_lambda=0.5)
    out_disp = compute_revisions_velocity(with_dispersion, as_of_date="2026-05-20",
                                          lookback_days=90,
                                          dispersion_penalty_lambda=0.5)
    assert out_disp["AAPL"] < out_none["AAPL"]


def test_lambda_zero_disables_penalty():
    rows = [
        _mkrow("AAPL", "eps_revision_direction_count", 5.0, "2026-05-01"),
        _mkrow("AAPL", "eps_revision_direction_count", 5.0, "2026-05-10"),
        _mkrow("AAPL", "eps_revision_direction_count", 5.0, "2026-05-15"),
        _mkrow("AAPL", "eps_estimate_current", 1.0, "2026-05-01"),
        _mkrow("AAPL", "eps_estimate_current", 3.0, "2026-05-10"),
        _mkrow("AAPL", "eps_estimate_current", 5.0, "2026-05-15"),
    ]
    out = compute_revisions_velocity(rows, as_of_date="2026-05-20", lookback_days=90,
                                     dispersion_penalty_lambda=0.0)
    assert out["AAPL"] == 5.0


def test_nan_when_fewer_than_3_observations():
    rows = [
        _mkrow("AAPL", "eps_revision_direction_count", 1.0, "2026-05-01"),
        _mkrow("AAPL", "eps_revision_direction_count", 1.0, "2026-05-10"),
    ]
    out = compute_revisions_velocity(rows, as_of_date="2026-05-20", lookback_days=90)
    assert math.isnan(out["AAPL"])


def test_multiple_tickers_independent():
    rows = [
        _mkrow("AAPL", "eps_revision_direction_count", 3.0, "2026-05-01"),
        _mkrow("AAPL", "eps_revision_direction_count", 3.0, "2026-05-10"),
        _mkrow("AAPL", "eps_revision_direction_count", 3.0, "2026-05-15"),
        _mkrow("MSFT", "eps_revision_direction_count", -2.0, "2026-05-01"),
        _mkrow("MSFT", "eps_revision_direction_count", -2.0, "2026-05-10"),
        _mkrow("MSFT", "eps_revision_direction_count", -2.0, "2026-05-15"),
    ]
    out = compute_revisions_velocity(rows, as_of_date="2026-05-20", lookback_days=90,
                                     dispersion_penalty_lambda=0.0)
    assert out["AAPL"] == 3.0
    assert out["MSFT"] == -2.0


def test_negative_lambda_raises():
    with pytest.raises(ValueError):
        compute_revisions_velocity([], as_of_date="2026-05-20",
                                   dispersion_penalty_lambda=-0.1)
