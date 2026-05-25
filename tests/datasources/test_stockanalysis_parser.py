"""Tests for stockanalysis_parser.parse_ratios_page (Phase A.3.6)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.datasources.stockanalysis_parser import parse_ratios_page


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "stockanalysis"
ANNUAL_HTML = (FIXTURE_DIR / "aapl_ratios_annual.html").read_text(encoding="utf-8")
QUARTERLY_HTML = (FIXTURE_DIR / "aapl_ratios_quarterly.html").read_text(encoding="utf-8")
TTM_HTML = (FIXTURE_DIR / "aapl_ratios_ttm.html").read_text(encoding="utf-8")
MALFORMED_HTML = (FIXTURE_DIR / "malformed.html").read_text(encoding="utf-8")


# --- annual -----------------------------------------------------------


def test_parse_annual_extracts_pe_ratio_per_year():
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    pe_rows = [r for r in rows if r.metric == "pe_ratio"]
    by_date = {r.period_end_date: r.value for r in pe_rows}
    assert by_date["2020-12-31"] == 33.21
    assert by_date["2021-12-31"] == 28.50
    assert by_date["2022-12-31"] == 24.10
    assert by_date["2023-12-31"] == 26.80
    assert by_date["2024-12-31"] == 28.45


def test_parse_annual_marks_period_type():
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    assert rows
    assert all(r.period_type == "annual" for r in rows)


def test_parse_annual_percentage_normalized_to_decimal():
    """Dividend Yield '0.65%' -> 0.0065 (decimal)."""
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    dy = next(r for r in rows
              if r.metric == "dividend_yield" and r.period_end_date == "2020-12-31")
    assert abs(dy.value - 0.0065) < 1e-9


def test_parse_annual_skips_unsupported_metrics():
    """The 'Some Unsupported Metric' row must NOT appear in output."""
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    metric_names = {r.metric for r in rows}
    for m in metric_names:
        assert m in {
            "pe_ratio", "pb_ratio", "ps_ratio", "ev_ebitda",
            "dividend_yield", "roe", "roa",
            "profit_margin", "operating_margin",
            "fcf_yield", "current_ratio", "debt_to_equity",
        }


def test_parse_annual_handles_na_as_none():
    """FCF Yield 2024 is 'n/a' in the fixture -> value None."""
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    fcf = next(r for r in rows
               if r.metric == "fcf_yield" and r.period_end_date == "2024-12-31")
    assert fcf.value is None


def test_parse_annual_ticker_uppercased():
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="aapl", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    assert rows
    assert all(r.ticker == "AAPL" for r in rows)


def test_parse_annual_returns_full_metric_x_year_cardinality():
    """12 supported metrics x 5 years = 60 rows from the annual fixture."""
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    assert len(rows) == 60


def test_parse_annual_records_source_url_on_every_row():
    src = "https://stockanalysis.com/stocks/aapl/financials/ratios/"
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url=src,
    )
    assert rows
    assert all(r.source_url == src for r in rows)


def test_parse_annual_run_id_threaded_through():
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/x", run_id="my-run-42",
    )
    assert rows
    assert all(r.run_id == "my-run-42" for r in rows)


# --- quarterly --------------------------------------------------------


def test_parse_quarterly_extracts_per_quarter_dates():
    rows = parse_ratios_page(
        html=QUARTERLY_HTML, ticker="AAPL", period_type="quarterly",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/?p=quarterly",
    )
    pe_rows = [r for r in rows if r.metric == "pe_ratio"]
    by_date = {r.period_end_date: r.value for r in pe_rows}
    assert by_date["2023-09-30"] == 27.80
    assert by_date["2023-12-31"] == 28.10
    assert by_date["2024-03-31"] == 27.90
    assert by_date["2024-06-30"] == 28.20
    assert by_date["2024-09-30"] == 30.10


def test_parse_quarterly_marks_period_type():
    rows = parse_ratios_page(
        html=QUARTERLY_HTML, ticker="AAPL", period_type="quarterly",
        source_url="https://stockanalysis.com/x?p=quarterly",
    )
    assert rows
    assert all(r.period_type == "quarterly" for r in rows)


# --- ttm --------------------------------------------------------------


def test_parse_ttm_returns_single_period_per_metric():
    rows = parse_ratios_page(
        html=TTM_HTML, ticker="AAPL", period_type="ttm",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/?p=trailing",
    )
    # 10 supported-metric rows in TTM fixture x 1 column = 10 rows.
    assert len(rows) == 10
    assert {r.period_type for r in rows} == {"ttm"}


def test_parse_ttm_period_end_date_is_iso_format():
    rows = parse_ratios_page(
        html=TTM_HTML, ticker="AAPL", period_type="ttm",
        source_url="https://stockanalysis.com/x?p=trailing",
    )
    # YYYY-MM-DD shape; can't assert exact date because parser falls back
    # to today() for literal 'TTM' headers.
    for r in rows:
        assert len(r.period_end_date) == 10
        assert r.period_end_date[4] == "-"
        assert r.period_end_date[7] == "-"


# --- malformed --------------------------------------------------------


def test_parse_malformed_returns_empty_list():
    """The parser must NOT crash on unexpected HTML; it returns []."""
    rows = parse_ratios_page(
        html=MALFORMED_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/x",
    )
    assert rows == []


def test_parse_empty_html_returns_empty_list():
    assert parse_ratios_page(
        html="", ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/x",
    ) == []


def test_parse_html_with_no_table_returns_empty_list():
    """HTML present but no <table>: empty result, no exception."""
    rows = parse_ratios_page(
        html="<html><body><p>no table here</p></body></html>",
        ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/x",
    )
    assert rows == []


def test_parse_html_with_table_but_no_data_rows_returns_empty_list():
    """Header-only table: parser sees the columns but no data rows."""
    html = """
    <html><body>
      <table>
        <thead><tr><th>Metric</th><th>2024</th></tr></thead>
        <tbody></tbody>
      </table>
    </body></html>
    """
    rows = parse_ratios_page(
        html=html, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/x",
    )
    assert rows == []


def test_parse_invalid_ticker_returns_empty_list():
    """Malformed ticker means downstream validation fails per row;
    parser swallows and returns empty rather than crashing."""
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="not-a-ticker", period_type="annual",
        source_url="https://stockanalysis.com/x",
    )
    assert rows == []
