"""Tests for the pure-function XBRL companyfacts parser (Phase A.3.3)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.common.datasources.edgar_xbrl_parser import (
    parse_companyfacts,
    extract_concept_observations,
    compute_ttm_from_quarters,
)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "CIK0000320193.json"


@pytest.fixture
def blob():
    return json.loads(FIXTURE.read_text())


def test_parse_companyfacts_returns_rows(blob):
    rows = parse_companyfacts(
        blob,
        run_id="r1",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    assert len(rows) > 0


def test_parse_companyfacts_extracts_2023_annual(blob):
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    fy23 = next(r for r in rows if r.fiscal_year == 2023 and r.fiscal_period == "annual")
    assert fy23.form_type == "10-K"
    assert fy23.filing_date == "2023-11-03"
    assert fy23.revenue_ttm == 383285000000.0
    assert fy23.net_income_ttm == 96995000000.0
    assert fy23.ebit_ttm == 114301000000.0
    assert fy23.total_assets == 352755000000.0
    assert fy23.total_liabilities == 290437000000.0
    assert fy23.total_equity == 62146000000.0
    assert fy23.cash_and_equivalents == 29965000000.0
    # total_debt = LongTermDebt + LongTermDebtNoncurrent
    assert fy23.total_debt == 95281000000.0 + 15807000000.0
    assert fy23.operating_cashflow == 110543000000.0
    assert fy23.capex == -10959000000.0  # stored as negative per spec
    # fcf = ocf - abs(capex)
    assert fy23.fcf_ttm == 110543000000.0 - 10959000000.0


def test_parse_companyfacts_extracts_quarterly_rows(blob):
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    quarters = [r for r in rows if r.fiscal_period in {"Q1", "Q2", "Q3"} and r.fiscal_year == 2024]
    assert len(quarters) == 3
    q1 = next(r for r in quarters if r.fiscal_period == "Q1")
    assert q1.form_type == "10-Q"
    assert q1.revenue_ttm == 119575000000.0


def test_parse_companyfacts_computes_margins(blob):
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    fy23 = next(r for r in rows if r.fiscal_year == 2023 and r.fiscal_period == "annual")
    assert fy23.operating_margin is not None
    assert abs(fy23.operating_margin - (114301000000.0 / 383285000000.0)) < 1e-6
    assert fy23.net_profit_margin is not None
    assert abs(fy23.net_profit_margin - (96995000000.0 / 383285000000.0)) < 1e-6


def test_parse_companyfacts_computes_debt_to_equity(blob):
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    fy23 = next(r for r in rows if r.fiscal_year == 2023 and r.fiscal_period == "annual")
    total_debt = 95281000000.0 + 15807000000.0
    assert abs(fy23.total_debt_to_equity - (total_debt / 62146000000.0)) < 1e-6


def test_parse_companyfacts_computes_interest_coverage(blob):
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    fy23 = next(r for r in rows if r.fiscal_year == 2023 and r.fiscal_period == "annual")
    assert fy23.interest_coverage is not None
    assert abs(fy23.interest_coverage - (114301000000.0 / 3933000000.0)) < 1e-3


def test_parse_companyfacts_handles_zero_interest_expense(blob):
    """When InterestExpense == 0, interest_coverage must be None (not inf/NaN)."""
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    fy24 = next(r for r in rows if r.fiscal_year == 2024 and r.fiscal_period == "annual")
    assert fy24.interest_coverage is None


def test_parse_companyfacts_carries_cik(blob):
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    for r in rows:
        assert r.cik == "0000320193"


def test_extract_concept_observations_returns_empty_for_missing(blob):
    obs = extract_concept_observations(blob, "us-gaap", "NotARealConcept")
    assert obs == []


def test_extract_concept_observations_returns_usd_list(blob):
    obs = extract_concept_observations(blob, "us-gaap", "Revenues")
    assert len(obs) == 6
    assert any(o["fy"] == 2023 and o["fp"] == "FY" for o in obs)


def test_compute_ttm_from_quarters_sums_latest_4():
    quarters = [
        {"end": "2023-12-30", "val": 100},
        {"end": "2024-03-30", "val": 90},
        {"end": "2024-06-29", "val": 80},
        {"end": "2024-09-28", "val": 95},
    ]
    assert compute_ttm_from_quarters(quarters) == 365


def test_compute_ttm_from_quarters_returns_none_when_fewer_than_4():
    assert compute_ttm_from_quarters([{"end": "2024-01-01", "val": 100}]) is None
    assert compute_ttm_from_quarters([]) is None


def test_parse_companyfacts_handles_empty_facts():
    blob = {"cik": 320193, "entityName": "Empty Co", "facts": {"us-gaap": {}}}
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="EMP",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    assert rows == []


def test_parse_companyfacts_skips_periods_with_no_revenue(blob):
    """If a fiscal_period has revenue but lacks the other concepts, the row
    should still be emitted with revenue populated and the other fields
    gracefully None. This guards against over-strict filtering."""
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    fy22 = [r for r in rows if r.fiscal_year == 2022 and r.fiscal_period == "annual"]
    assert len(fy22) == 1
    assert fy22[0].revenue_ttm == 394328000000.0
    assert fy22[0].net_income_ttm is None  # gracefully None
