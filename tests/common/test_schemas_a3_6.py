"""Tests for Phase A.3.6 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import RawStockanalysisRatioRow


class TestRawStockanalysisRatioRow:
    def test_minimal_valid_annual(self):
        r = RawStockanalysisRatioRow(
            run_id="r1",
            ticker="AAPL",
            metric="pe_ratio",
            period_end_date="2024-12-31",
            period_type="annual",
            value=28.45,
            source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"
        assert r.metric == "pe_ratio"
        assert r.period_type == "annual"
        assert r.value == 28.45

    def test_minimal_valid_quarterly(self):
        r = RawStockanalysisRatioRow(
            run_id="r1",
            ticker="AAPL",
            metric="ev_ebitda",
            period_end_date="2024-09-30",
            period_type="quarterly",
            value=21.10,
            source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/?p=quarterly",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.period_type == "quarterly"
        assert r.period_end_date == "2024-09-30"

    def test_minimal_valid_ttm(self):
        r = RawStockanalysisRatioRow(
            run_id="r1",
            ticker="AAPL",
            metric="pe_ratio",
            period_end_date="2026-05-21",
            period_type="ttm",
            value=30.12,
            source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/?p=trailing",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.period_type == "ttm"

    def test_ticker_uppercased(self):
        r = RawStockanalysisRatioRow(
            run_id="r1",
            ticker="aapl",
            metric="pe_ratio",
            period_end_date="2024-12-31",
            period_type="annual",
            value=28.45,
            source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            RawStockanalysisRatioRow(
                run_id="r1",
                ticker="not-a-ticker",
                metric="pe_ratio",
                period_end_date="2024-12-31",
                period_type="annual",
                value=28.45,
                source_url="https://stockanalysis.com/x",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_unknown_metric_rejected(self):
        """metric is a Literal of supported names; unknown ones must raise."""
        with pytest.raises(Exception):
            RawStockanalysisRatioRow(
                run_id="r1",
                ticker="AAPL",
                metric="not_a_metric",
                period_end_date="2024-12-31",
                period_type="annual",
                value=28.45,
                source_url="https://stockanalysis.com/x",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_unknown_period_type_rejected(self):
        with pytest.raises(Exception):
            RawStockanalysisRatioRow(
                run_id="r1",
                ticker="AAPL",
                metric="pe_ratio",
                period_end_date="2024-12-31",
                period_type="monthly",
                value=28.45,
                source_url="https://stockanalysis.com/x",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_value_can_be_none(self):
        """Cells reported as 'n/a' or '-' in the HTML must map to None."""
        r = RawStockanalysisRatioRow(
            run_id="r1",
            ticker="AAPL",
            metric="pe_ratio",
            period_end_date="2015-12-31",
            period_type="annual",
            value=None,
            source_url="https://stockanalysis.com/x",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.value is None

    def test_period_end_date_required(self):
        with pytest.raises(Exception):
            RawStockanalysisRatioRow(
                run_id="r1",
                ticker="AAPL",
                metric="pe_ratio",
                period_end_date="",
                period_type="annual",
                value=28.45,
                source_url="https://stockanalysis.com/x",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_all_supported_metrics_accepted(self):
        """Round-trip every supported metric name to confirm the Literal."""
        supported = [
            "pe_ratio", "pb_ratio", "ps_ratio", "ev_ebitda",
            "dividend_yield", "roe", "roa",
            "profit_margin", "operating_margin",
            "fcf_yield", "current_ratio", "debt_to_equity",
        ]
        for m in supported:
            r = RawStockanalysisRatioRow(
                run_id="r1",
                ticker="AAPL",
                metric=m,
                period_end_date="2024-12-31",
                period_type="annual",
                value=1.0,
                source_url="https://stockanalysis.com/x",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
            assert r.metric == m

    def test_period_type_annual_quarterly_ttm_accepted(self):
        for pt in ["annual", "quarterly", "ttm"]:
            r = RawStockanalysisRatioRow(
                run_id="r1",
                ticker="AAPL",
                metric="pe_ratio",
                period_end_date="2024-12-31",
                period_type=pt,
                value=1.0,
                source_url="https://stockanalysis.com/x",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
            assert r.period_type == pt
