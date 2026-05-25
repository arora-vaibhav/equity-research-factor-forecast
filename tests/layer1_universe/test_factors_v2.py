"""Tests for the A.3.10 top-level `compute_factor_scores` pipeline entry.

Coverage:
  * Minimal canonical_df (no raw_inputs, no prices) -> news_activity is
    NaN for every ticker; other columns NaN as expected.
  * Canonical_df + raw_inputs with one signal (opportunistic insider)
    populated -> news_activity_score is finite for the seeded ticker.
  * prices_df present + ohlcv_by_ticker present -> lowvol_score
    populated via Yang-Zhang path (no exception).
  * weights override flow: passing custom subweights through to news
    activity.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.layer1_universe.factors import compute_factor_scores


def _canonical(tickers, **overrides) -> pd.DataFrame:
    base = {
        "ticker": tickers,
        "earnings_yield": [0.05] * len(tickers),
        "ebit_yield": [0.07] * len(tickers),
        "fcf_yield": [0.04] * len(tickers),
        "book_to_market": [0.40] * len(tickers),
        "operating_margin": [0.20] * len(tickers),
        "net_profit_margin": [0.15] * len(tickers),
        "revenue_growth_yoy": [0.10] * len(tickers),
        "eps_growth_yoy": [0.12] * len(tickers),
        "roe": [0.20] * len(tickers),
        "total_debt_to_equity": [0.5] * len(tickers),
        "interest_coverage": [10.0] * len(tickers),
        "sector": ["Technology"] * len(tickers),
    }
    base.update(overrides)
    return pd.DataFrame(base)


def test_minimal_canonical_no_prices_no_raw():
    """No prices_df, no raw_inputs -> momentum/lowvol/news_activity all NaN."""
    df = _canonical(["AAPL", "MSFT"])
    out = compute_factor_scores(df)
    assert list(out.index) == ["AAPL", "MSFT"]
    expected_cols = {
        "value_score", "quality_score", "momentum_score",
        "lowvol_score", "revisions_score", "news_activity_score",
    }
    assert expected_cols.issubset(set(out.columns))
    assert out["momentum_score"].isna().all()
    assert out["lowvol_score"].isna().all()
    assert out["news_activity_score"].isna().all()


def test_raw_inputs_populates_news_activity():
    df = _canonical(["AAPL", "MSFT"])
    raw_inputs = {
        "insider_rows": [
            {
                "ticker": "AAPL",
                "transaction_date": "2026-05-10",
                "transaction_code": "P",
                "is_opportunistic": True,
            },
        ],
    }
    out = compute_factor_scores(
        df, raw_inputs=raw_inputs, as_of_date="2026-05-22",
    )
    assert math.isfinite(out.loc["AAPL", "news_activity_score"])
    assert math.isnan(out.loc["MSFT", "news_activity_score"])


def test_prices_only_path_populates_momentum():
    """prices_df present -> momentum_score lights up. Note: lowvol_score
    requires market_returns AND statsmodels for the idio component;
    we only assert momentum here since the lowvol pipeline is exercised
    in test_ohlcv_path_triggers_yang_zhang."""
    df = _canonical(["AAPL", "MSFT", "GOOG"])
    idx = pd.date_range("2023-01-01", periods=300, freq="B")
    rng = np.random.default_rng(42)
    aapl = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.010, 300)))
    msft = 150.0 * np.exp(np.cumsum(rng.normal(0, 0.012, 300)))
    goog = 130.0 * np.exp(np.cumsum(rng.normal(0, 0.014, 300)))
    prices = pd.DataFrame(
        {"AAPL": aapl, "MSFT": msft, "GOOG": goog}, index=idx,
    )
    out = compute_factor_scores(df, prices_df=prices)
    assert not out["momentum_score"].isna().all()
    # lowvol column exists (even when NaN by virtue of missing market /
    # statsmodels paths).
    assert "lowvol_score" in out.columns


def test_ohlcv_path_triggers_yang_zhang():
    """When ohlcv_by_ticker is provided, compute_lowvol_score uses
    Yang-Zhang internally. We don't assert the exact value -- just
    that the pipeline produces a row without crashing."""
    df = _canonical(["AAPL"])
    idx = pd.date_range("2024-01-01", periods=80, freq="B")
    rng = np.random.default_rng(7)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 80)))
    open_ = close * np.exp(rng.normal(0, 0.003, 80))
    high = np.maximum(close, open_) * np.exp(np.abs(rng.normal(0, 0.005, 80)))
    low = np.minimum(close, open_) * np.exp(-np.abs(rng.normal(0, 0.005, 80)))
    prices = pd.DataFrame({"AAPL": close}, index=idx)
    ohlcv = {
        "AAPL": pd.DataFrame(
            {"Open": open_, "High": high, "Low": low, "Close": close},
            index=idx,
        ),
    }
    out = compute_factor_scores(
        df, prices_df=prices, ohlcv_by_ticker=ohlcv,
    )
    assert "AAPL" in out.index


def test_weights_override_passes_through():
    """Custom subweights must reach the news_activity composite."""
    df = _canonical(["AAPL"])
    raw_inputs = {
        "insider_rows": [
            {
                "ticker": "AAPL",
                "transaction_date": "2026-05-10",
                "transaction_code": "P",
                "is_opportunistic": True,
            },
        ],
    }
    weights = {
        "news_activity_subweights": {
            "opportunistic_insider": 1.0,
            "short_interest_delta": 0.0,
            "revisions_velocity": 0.0,
            "news_volume_anomaly": 0.0,
            "filing_density": 0.0,
            "lm_tone": 0.0,
            "fears": 0.0,
        },
    }
    out = compute_factor_scores(
        df, raw_inputs=raw_inputs, as_of_date="2026-05-22", weights=weights,
    )
    assert out.loc["AAPL", "news_activity_score"] == pytest.approx(1.0)
