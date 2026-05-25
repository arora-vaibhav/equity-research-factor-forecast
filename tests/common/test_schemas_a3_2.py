"""Tests for Phase A.3.2 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import RawYahooRow


class TestRawYahooRow:
    def test_minimal_valid(self):
        r = RawYahooRow(
            run_id="r1",
            ticker="AAPL",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.run_id == "r1"
        assert r.ticker == "AAPL"
        assert r.market_cap is None
        assert r.pe_ttm is None

    def test_full_valid(self):
        r = RawYahooRow(
            run_id="r1",
            ticker="AAPL",
            market_cap=3_000_000_000_000.0,
            pe_ttm=24.5,
            pe_forward=22.1,
            ebit_ttm=120_000_000_000.0,
            fcf_ttm=110_000_000_000.0,
            total_debt=100_000_000_000.0,
            cash=50_000_000_000.0,
            book_value=70.0,
            operating_margin=0.30,
            net_profit_margin=0.25,
            revenue_growth_yoy=0.08,
            eps_growth_yoy=0.10,
            company_name="Apple Inc",
            sector="Technology",
            industry="Consumer Electronics",
            exchange="NASDAQ",
            price=150.0,
            avg_daily_volume=50_000_000,
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.market_cap > 0
        assert -1.0 <= r.operating_margin <= 1.0

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            RawYahooRow(
                run_id="r1",
                ticker="not-a-ticker",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_negative_market_cap_rejected(self):
        with pytest.raises(Exception):
            RawYahooRow(
                run_id="r1",
                ticker="AAPL",
                market_cap=-100.0,
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_operating_margin_out_of_range_rejected(self):
        with pytest.raises(Exception):
            RawYahooRow(
                run_id="r1",
                ticker="AAPL",
                operating_margin=1.5,
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
