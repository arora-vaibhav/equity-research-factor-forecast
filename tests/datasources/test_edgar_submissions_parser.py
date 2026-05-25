"""Tests for the pure-function EDGAR submissions parser (Phase A.3.4)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.common.datasources.edgar_submissions_parser import (
    parse_submissions,
    DEFAULT_FORM_TYPES,
)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "submissions_CIK0000320193.json"


@pytest.fixture
def blob():
    return json.loads(FIXTURE.read_text())


def test_parse_submissions_returns_rows_and_form4_accns(blob):
    rows, form4_accns = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    assert len(rows) > 0
    assert len(form4_accns) > 0


def test_parse_submissions_filters_by_default_form_types(blob):
    """Default form filter excludes DEF 14A."""
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    forms = sorted({r.form_type for r in rows})
    assert "DEF 14A" not in forms
    assert "4" in forms
    assert "8-K" in forms
    assert "10-K" in forms
    assert "10-Q" in forms


def test_parse_submissions_extracts_form4_accns(blob):
    _, form4_accns = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    assert sorted(form4_accns) == [
        "0000320193-26-000030", "0000320193-26-000045"
    ]


def test_parse_submissions_extracts_item_codes_for_8k(blob):
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    eight_k = next(r for r in rows if r.form_type == "8-K")
    assert eight_k.item_codes == "2.02,9.01"


def test_parse_submissions_form_4_item_codes_normalize_to_none(blob):
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    form4_rows = [r for r in rows if r.form_type == "4"]
    assert all(r.item_codes is None for r in form4_rows)


def test_parse_submissions_constructs_primary_doc_url(blob):
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    eight_k = next(r for r in rows if r.form_type == "8-K")
    assert eight_k.primary_doc_url is not None
    assert "Archives/edgar/data/320193" in eight_k.primary_doc_url
    assert "000032019326000010" in eight_k.primary_doc_url
    assert "aapl-20260201.htm" in eight_k.primary_doc_url


def test_parse_submissions_respects_last_n_days(blob):
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
        last_n_days=90,
    )
    dates = sorted({r.filing_date for r in rows})
    # Floor relative to max filingDate 2026-04-10 minus 90 days = 2026-01-10
    for d in dates:
        assert d >= "2026-01-10"


def test_parse_submissions_custom_form_types(blob):
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
        form_types={"8-K"},
    )
    forms = {r.form_type for r in rows}
    assert forms == {"8-K"}


def test_parse_submissions_extracts_cik_from_top_level(blob):
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    for r in rows:
        assert r.cik == "0000320193"


def test_default_form_types_constant_includes_all_required():
    required = {"8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A", "4", "4/A"}
    assert required.issubset(DEFAULT_FORM_TYPES)


def test_parse_submissions_empty_filings_block_returns_empty():
    blob = {
        "cik": "320193",
        "name": "Empty Co",
        "tickers": ["EMP"],
        "filings": {"recent": {
            "accessionNumber": [],
            "filingDate": [],
            "acceptanceDateTime": [],
            "form": [],
            "items": [],
            "primaryDocument": [],
        }},
    }
    rows, form4_accns = parse_submissions(
        blob, ticker="EMP",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    assert rows == []
    assert form4_accns == []
