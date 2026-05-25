"""Tests for Diether-Lee-Werner (2009) short-interest delta module."""
from __future__ import annotations

import math

import pytest

from src.methodology.short_interest_delta import (
    SHORT_INTEREST_DELTA_VERSION,
    compute_short_interest_delta,
)


def test_version_constant():
    assert SHORT_INTEREST_DELTA_VERSION == "1.0"


def test_equal_observations_return_zero():
    rows = [
        {"ticker": "AAPL", "settlement_date": "2026-04-15",
         "short_interest_shares": 1_000_000.0, "avg_daily_volume": 500_000.0},
        {"ticker": "AAPL", "settlement_date": "2026-05-15",
         "short_interest_shares": 1_000_000.0, "avg_daily_volume": 500_000.0},
    ]
    out = compute_short_interest_delta(rows, as_of_date="2026-05-15", lookback_days=30)
    assert out["AAPL"] == 0.0


def test_rising_short_interest_positive():
    rows = [
        {"ticker": "AAPL", "settlement_date": "2026-04-15",
         "short_interest_shares": 1_000_000.0, "avg_daily_volume": 500_000.0},
        {"ticker": "AAPL", "settlement_date": "2026-05-15",
         "short_interest_shares": 1_100_000.0, "avg_daily_volume": 500_000.0},
    ]
    out = compute_short_interest_delta(rows, as_of_date="2026-05-15", lookback_days=30)
    # raw_delta = 100k, dtc = 1.1M/500k = 2.2, signal = 220k
    assert out["AAPL"] == pytest.approx(100_000.0 * (1_100_000.0 / 500_000.0))


def test_falling_short_interest_negative():
    rows = [
        {"ticker": "AAPL", "settlement_date": "2026-04-15",
         "short_interest_shares": 1_000_000.0, "avg_daily_volume": 500_000.0},
        {"ticker": "AAPL", "settlement_date": "2026-05-15",
         "short_interest_shares": 900_000.0, "avg_daily_volume": 500_000.0},
    ]
    out = compute_short_interest_delta(rows, as_of_date="2026-05-15", lookback_days=30)
    assert out["AAPL"] < 0


def test_nan_when_only_one_observation():
    rows = [
        {"ticker": "AAPL", "settlement_date": "2026-05-15",
         "short_interest_shares": 1_000_000.0, "avg_daily_volume": 500_000.0},
    ]
    out = compute_short_interest_delta(rows, as_of_date="2026-05-15", lookback_days=30)
    assert math.isnan(out["AAPL"])


def test_adv_missing_falls_back_to_raw_delta():
    rows = [
        {"ticker": "AAPL", "settlement_date": "2026-04-15",
         "short_interest_shares": 1_000_000.0, "avg_daily_volume": None},
        {"ticker": "AAPL", "settlement_date": "2026-05-15",
         "short_interest_shares": 1_100_000.0, "avg_daily_volume": None},
    ]
    out = compute_short_interest_delta(rows, as_of_date="2026-05-15", lookback_days=30)
    assert out["AAPL"] == 100_000.0


def test_multiple_tickers_independent():
    rows = [
        {"ticker": "AAPL", "settlement_date": "2026-04-15",
         "short_interest_shares": 1_000_000.0, "avg_daily_volume": None},
        {"ticker": "AAPL", "settlement_date": "2026-05-15",
         "short_interest_shares": 1_100_000.0, "avg_daily_volume": None},
        {"ticker": "MSFT", "settlement_date": "2026-04-15",
         "short_interest_shares": 2_000_000.0, "avg_daily_volume": None},
        {"ticker": "MSFT", "settlement_date": "2026-05-15",
         "short_interest_shares": 1_800_000.0, "avg_daily_volume": None},
    ]
    out = compute_short_interest_delta(rows, as_of_date="2026-05-15", lookback_days=30)
    assert out["AAPL"] == 100_000.0
    assert out["MSFT"] == -200_000.0


def test_same_date_exchanges_summed():
    """Two exchange observations on the same date sum into one bucket."""
    rows = [
        {"ticker": "AAPL", "settlement_date": "2026-04-15",
         "short_interest_shares": 500_000.0, "avg_daily_volume": None},
        {"ticker": "AAPL", "settlement_date": "2026-04-15",
         "short_interest_shares": 500_000.0, "avg_daily_volume": None},
        {"ticker": "AAPL", "settlement_date": "2026-05-15",
         "short_interest_shares": 600_000.0, "avg_daily_volume": None},
        {"ticker": "AAPL", "settlement_date": "2026-05-15",
         "short_interest_shares": 500_000.0, "avg_daily_volume": None},
    ]
    out = compute_short_interest_delta(rows, as_of_date="2026-05-15", lookback_days=30)
    # 1.1M - 1.0M = 100k
    assert out["AAPL"] == 100_000.0


def test_bad_date_raises():
    rows = [
        {"ticker": "AAPL", "settlement_date": "not-a-date",
         "short_interest_shares": 1.0, "avg_daily_volume": None},
    ]
    with pytest.raises(ValueError):
        compute_short_interest_delta(rows, as_of_date="2026-05-15", lookback_days=30)


def test_negative_lookback_raises():
    with pytest.raises(ValueError):
        compute_short_interest_delta([], as_of_date="2026-05-15", lookback_days=-1)


def test_deterministic_on_shuffled_input():
    rows = [
        {"ticker": "AAPL", "settlement_date": "2026-04-15",
         "short_interest_shares": 1_000_000.0, "avg_daily_volume": None},
        {"ticker": "AAPL", "settlement_date": "2026-05-15",
         "short_interest_shares": 1_100_000.0, "avg_daily_volume": None},
    ]
    out1 = compute_short_interest_delta(rows, as_of_date="2026-05-15", lookback_days=30)
    out2 = compute_short_interest_delta(list(reversed(rows)), as_of_date="2026-05-15", lookback_days=30)
    assert out1 == out2


def test_empty_rows_returns_empty_dict():
    assert compute_short_interest_delta([], as_of_date="2026-05-15", lookback_days=30) == {}
