"""Tests for Phase A.3.3 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import RawEdgarFundamentalsRow


class TestRawEdgarFundamentalsRow:
    def test_minimal_valid(self):
        r = RawEdgarFundamentalsRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            fiscal_year=2023,
            fiscal_period="annual",
            filing_date="2023-11-03",
            form_type="10-K",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"
        assert r.cik == "0000320193"
        assert r.fiscal_year == 2023
        assert r.fiscal_period == "annual"
        assert r.revenue_ttm is None
        assert r.operating_margin is None

    def test_full_valid(self):
        r = RawEdgarFundamentalsRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            fiscal_year=2023,
            fiscal_period="annual",
            filing_date="2023-11-03",
            accepted_at="2023-11-03T18:01:14.000Z",
            form_type="10-K",
            revenue_ttm=383285000000.0,
            ebit_ttm=114301000000.0,
            net_income_ttm=96995000000.0,
            total_assets=352755000000.0,
            total_liabilities=290437000000.0,
            total_equity=62146000000.0,
            cash_and_equivalents=29965000000.0,
            total_debt=111088000000.0,
            operating_cashflow=110543000000.0,
            capex=-10959000000.0,
            fcf_ttm=99584000000.0,
            operating_margin=0.2982,
            net_profit_margin=0.2531,
            total_debt_to_equity=1.7876,
            interest_coverage=29.84,
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.revenue_ttm == 383285000000.0
        assert -1.0 <= r.operating_margin <= 1.0
        assert r.total_debt_to_equity > 0

    def test_fiscal_period_must_be_valid_enum(self):
        with pytest.raises(Exception):
            RawEdgarFundamentalsRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                fiscal_year=2023,
                fiscal_period="not-a-period",
                filing_date="2023-11-03",
                form_type="10-K",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_form_type_must_be_valid_enum(self):
        with pytest.raises(Exception):
            RawEdgarFundamentalsRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                fiscal_year=2023,
                fiscal_period="annual",
                filing_date="2023-11-03",
                form_type="S-1",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_cik_normalized_to_10_digit_string(self):
        r = RawEdgarFundamentalsRow(
            run_id="r1",
            ticker="AAPL",
            cik="320193",
            fiscal_year=2023,
            fiscal_period="annual",
            filing_date="2023-11-03",
            form_type="10-K",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.cik == "0000320193"

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            RawEdgarFundamentalsRow(
                run_id="r1",
                ticker="not-a-ticker",
                cik="0000320193",
                fiscal_year=2023,
                fiscal_period="annual",
                filing_date="2023-11-03",
                form_type="10-K",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_operating_margin_out_of_range_rejected(self):
        with pytest.raises(Exception):
            RawEdgarFundamentalsRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                fiscal_year=2023,
                fiscal_period="annual",
                filing_date="2023-11-03",
                form_type="10-K",
                operating_margin=1.5,
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
