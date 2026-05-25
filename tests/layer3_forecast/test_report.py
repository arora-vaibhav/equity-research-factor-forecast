"""Tests for src.layer3_forecast.report (Phase F.2)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.layer3_forecast.factor_forecast import (
    DEFAULT_FACTORS,
    ForecastResult,
)
from src.layer3_forecast.report import (
    REPORT_VERSION,
    _build_factor_table,
    _build_reasoning_template,
    render_index_page,
    render_stock_report,
)


def test_version_constant():
    assert REPORT_VERSION == "1.0"


def _make_forecast(point=0.05):
    return ForecastResult(
        ticker="AAPL",
        as_of_date="2026-05-23",
        horizon_days=30,
        point_return=point,
        lower_80=point - 0.02,
        upper_80=point + 0.02,
        lower_95=point - 0.04,
        upper_95=point + 0.04,
        factor_contributions={
            "value_score": 0.02,
            "quality_score": 0.01,
            "momentum_score": -0.01,
            "news_activity_score": 0.03,
            "__intercept__": 0.0,
        },
        n_train=200,
        n_bootstrap=1000,
    )


def _make_loadings():
    return pd.Series({f: 0.01 for f in DEFAULT_FACTORS})


def test_build_factor_table_includes_intercept_row():
    fc = _make_forecast()
    table = _build_factor_table(fc, _make_loadings(), {"value_score": 2.0})
    factors = [r["factor"] for r in table]
    assert "(intercept)" in factors
    assert len(table) == len(DEFAULT_FACTORS) + 1


def test_build_reasoning_template_includes_ticker_and_drivers():
    fc = _make_forecast(point=0.07)
    table = _build_factor_table(fc, _make_loadings(), {})
    text = _build_reasoning_template("AAPL", fc, [], table)
    assert "AAPL" in text
    assert "rising" in text
    assert "+7.00%" in text
    assert "no catalysts in window" in text


def test_build_reasoning_template_falling_when_negative():
    fc = _make_forecast(point=-0.03)
    table = _build_factor_table(fc, _make_loadings(), {})
    text = _build_reasoning_template("AAPL", fc, [], table)
    assert "falling" in text


def test_render_stock_report_writes_html(tmp_path):
    fc = _make_forecast()
    loadings = _make_loadings()
    historical = pd.Series(
        100.0 + np.arange(252, dtype=float) * 0.05,
        index=pd.bdate_range(end="2026-05-23", periods=252),
    )
    out_path = render_stock_report(
        ticker="AAPL",
        forecast=fc,
        loadings=loadings,
        ticker_factors={"value_score": 1.0, "news_activity_score": 0.5},
        historical_close=historical,
        catalysts=[{
            "catalyst_date": "2026-07-15",
            "catalyst_type": "earnings",
            "source": "yahoo",
            "confidence": "high",
            "catalyst_description": "Earnings announcement",
        }],
        company_name="Apple Inc",
        sector="Technology",
        output_dir=tmp_path / "reports",
        allow_llm=False,
    )
    assert out_path.exists()
    html = out_path.read_text(encoding="utf-8")
    assert "AAPL" in html
    assert "Apple Inc" in html
    assert "data:image/png;base64," in html
    assert "earnings" in html
    assert "+5.00%" in html


def test_render_stock_report_handles_missing_historical_gracefully(tmp_path):
    fc = _make_forecast()
    loadings = _make_loadings()
    out_path = render_stock_report(
        ticker="AAPL",
        forecast=fc,
        loadings=loadings,
        ticker_factors={},
        historical_close=None,
        catalysts=None,
        output_dir=tmp_path / "reports",
        allow_llm=False,
    )
    assert out_path.exists()
    html = out_path.read_text(encoding="utf-8")
    assert "(no plot)" in html


def test_render_index_page(tmp_path):
    out_path = render_index_page(
        tickers=["AAPL", "MSFT", "GOOG"],
        output_dir=tmp_path / "reports",
        as_of_date="2026-05-23",
    )
    assert out_path.exists()
    html = out_path.read_text(encoding="utf-8")
    for t in ["AAPL", "MSFT", "GOOG"]:
        assert f'href="{t}.html"' in html
