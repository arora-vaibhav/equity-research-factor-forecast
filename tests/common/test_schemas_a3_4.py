"""Tests for Phase A.3.4 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import RawEdgarInsiderRow, RawEdgarFilingRow


class TestRawEdgarInsiderRow:
    def test_minimal_valid_purchase(self):
        r = RawEdgarInsiderRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            filer_name="COOK TIMOTHY D",
            filer_title="Chief Executive Officer",
            filer_is_officer=True,
            filer_is_director=True,
            filer_is_10pct_owner=False,
            transaction_date="2026-03-15",
            transaction_code="P",
            transaction_code_description="Open-market or private purchase",
            shares=1000.0,
            price_per_share=175.25,
            total_value=175250.0,
            shares_after_transaction=3279000.0,
            source_filing_accn="0000320193-26-000045",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.transaction_code == "P"
        assert r.cik == "0000320193"
        assert r.is_opportunistic is None
        assert r.is_derivative is False

    def test_full_valid_with_classifier_output(self):
        r = RawEdgarInsiderRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            filer_name="COOK TIMOTHY D",
            filer_title="Chief Executive Officer",
            filer_is_officer=True,
            filer_is_director=True,
            filer_is_10pct_owner=False,
            transaction_date="2026-03-15",
            transaction_code="S",
            transaction_code_description="Open-market or private sale",
            shares=500000.0,
            price_per_share=175.25,
            total_value=87625000.0,
            shares_after_transaction=2779000.0,
            is_opportunistic=True,
            opportunistic_classifier_version="1.0",
            is_derivative=False,
            source_filing_accn="0000320193-26-000045",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.is_opportunistic is True
        assert r.opportunistic_classifier_version == "1.0"

    def test_all_standard_transaction_codes_accepted(self):
        codes = ["P", "S", "A", "M", "D", "F", "G", "J", "C",
                 "E", "H", "I", "O", "X", "V", "W", "Z", "K", "L", "U"]
        for code in codes:
            r = RawEdgarInsiderRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                filer_name="X",
                transaction_date="2026-03-15",
                transaction_code=code,
                shares=1.0,
                source_filing_accn="0000320193-26-000001",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
            assert r.transaction_code == code

    def test_unknown_transaction_code_rejected(self):
        with pytest.raises(Exception):
            RawEdgarInsiderRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                filer_name="X",
                transaction_date="2026-03-15",
                transaction_code="ZZ",
                shares=1.0,
                source_filing_accn="0000320193-26-000001",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_cik_normalized_to_10_digit_string(self):
        r = RawEdgarInsiderRow(
            run_id="r1",
            ticker="AAPL",
            cik="320193",
            filer_name="X",
            transaction_date="2026-03-15",
            transaction_code="P",
            shares=1.0,
            source_filing_accn="0000320193-26-000001",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.cik == "0000320193"

    def test_filer_name_uppercased(self):
        r = RawEdgarInsiderRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            filer_name="cook timothy d",
            transaction_date="2026-03-15",
            transaction_code="P",
            shares=1.0,
            source_filing_accn="0000320193-26-000001",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.filer_name == "COOK TIMOTHY D"

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            RawEdgarInsiderRow(
                run_id="r1",
                ticker="not-a-ticker",
                cik="0000320193",
                filer_name="X",
                transaction_date="2026-03-15",
                transaction_code="P",
                shares=1.0,
                source_filing_accn="0000320193-26-000001",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_shares_stored_as_positive(self):
        r = RawEdgarInsiderRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            filer_name="X",
            transaction_date="2026-03-15",
            transaction_code="D",
            shares=500.0,
            source_filing_accn="0000320193-26-000001",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.shares == 500.0


class TestRawEdgarFilingRow:
    def test_minimal_valid_8k(self):
        r = RawEdgarFilingRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            form_type="8-K",
            filing_date="2026-02-01",
            accepted_at="2026-02-01T16:30:01Z",
            item_codes="2.02,9.01",
            source_filing_accn="0000320193-26-000010",
            primary_doc_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000010/aapl-20260201.htm",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.form_type == "8-K"
        assert r.item_codes == "2.02,9.01"

    def test_form_4_envelope_record(self):
        r = RawEdgarFilingRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            form_type="4",
            filing_date="2026-03-15",
            accepted_at="2026-03-15T18:00:00Z",
            item_codes=None,
            source_filing_accn="0000320193-26-000045",
            primary_doc_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/wk-form4_1742068800.xml",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.form_type == "4"
        assert r.item_codes is None

    def test_10k_record(self):
        r = RawEdgarFilingRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            form_type="10-K",
            filing_date="2024-11-01",
            accepted_at="2024-10-31T18:09:50Z",
            item_codes=None,
            source_filing_accn="0000320193-24-000158",
            primary_doc_url=None,
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.form_type == "10-K"

    def test_allowed_form_types(self):
        for ft in ["8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A", "4", "4/A"]:
            r = RawEdgarFilingRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                form_type=ft,
                filing_date="2024-11-01",
                source_filing_accn="0000320193-24-000158",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
            assert r.form_type == ft

    def test_unknown_form_type_rejected(self):
        with pytest.raises(Exception):
            RawEdgarFilingRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                form_type="S-1",
                filing_date="2024-11-01",
                source_filing_accn="0000320193-24-000158",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_cik_padded(self):
        r = RawEdgarFilingRow(
            run_id="r1",
            ticker="AAPL",
            cik="320193",
            form_type="8-K",
            filing_date="2026-02-01",
            source_filing_accn="0000320193-26-000010",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.cik == "0000320193"
