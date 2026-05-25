"""Tests for the pure-function Form 4 XML parser (Phase A.3.4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.datasources.edgar_form4_parser import (
    parse_form4_xml,
    Form4ParseError,
)
from src.common.schemas import RawEdgarInsiderRow


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "form4_sample.xml"


@pytest.fixture
def xml_bytes() -> bytes:
    return FIXTURE.read_bytes()


def test_parse_form4_returns_non_derivative_rows(xml_bytes):
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    non_deriv = [r for r in rows if not r.is_derivative]
    assert len(non_deriv) == 2


def test_parse_form4_extracts_filer_info(xml_bytes):
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    assert rows[0].filer_name == "COOK TIMOTHY D"
    assert rows[0].filer_title == "Chief Executive Officer"
    assert rows[0].filer_is_officer is True
    assert rows[0].filer_is_director is True
    assert rows[0].filer_is_10pct_owner is False


def test_parse_form4_extracts_purchase_correctly(xml_bytes):
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    purchase = next(r for r in rows if r.transaction_code == "P")
    assert purchase.transaction_date == "2026-03-15"
    assert purchase.shares == 1000.0
    assert purchase.price_per_share == 175.25
    assert abs(purchase.total_value - 1000.0 * 175.25) < 1e-3
    assert purchase.shares_after_transaction == 3279000.0


def test_parse_form4_extracts_sale_correctly(xml_bytes):
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    sale = next(r for r in rows if r.transaction_code == "S")
    assert sale.shares == 500.0
    assert sale.price_per_share == 176.0
    assert abs(sale.total_value - 500.0 * 176.0) < 1e-3
    assert sale.shares_after_transaction == 3278500.0


def test_parse_form4_optionally_extracts_derivative_with_flag(xml_bytes):
    """Derivative transactions are tagged with is_derivative=True."""
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
        include_derivative=True,
    )
    deriv = [r for r in rows if r.is_derivative]
    assert len(deriv) == 1
    assert deriv[0].transaction_code == "M"
    assert deriv[0].shares == 2500.0


def test_parse_form4_skips_derivative_by_default(xml_bytes):
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    assert all(r.is_derivative is False for r in rows)


def test_parse_form4_stamps_source_accn_and_cik(xml_bytes):
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    for r in rows:
        assert r.cik == "0000320193"
        assert r.source_filing_accn == "0000320193-26-000045"
        assert r.ticker == "AAPL"


def test_parse_form4_initial_is_opportunistic_is_none(xml_bytes):
    """The parser does NOT classify. is_opportunistic is None at parse time."""
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    for r in rows:
        assert r.is_opportunistic is None
        assert r.opportunistic_classifier_version is None


def test_parse_form4_empty_xml_raises():
    with pytest.raises(Form4ParseError):
        parse_form4_xml(
            b"<?xml version='1.0'?><root/>",
            cik="0000320193",
            accn="0000320193-26-000045",
            ticker="AAPL",
            scrape_timestamp="2026-05-21T08:00:00Z",
            run_id="r1",
        )


def test_parse_form4_malformed_xml_raises():
    with pytest.raises(Form4ParseError):
        parse_form4_xml(
            b"not xml at all",
            cik="0000320193",
            accn="0000320193-26-000045",
            ticker="AAPL",
            scrape_timestamp="2026-05-21T08:00:00Z",
            run_id="r1",
        )


def test_parse_form4_returns_empty_when_no_non_derivative_table():
    """A Form 4 with only derivative transactions returns empty (default)."""
    only_deriv_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-03-15</periodOfReport>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>Apple Inc.</issuerName>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001214156</rptOwnerCik>
      <rptOwnerName>SOMEONE ELSE</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
    </reportingOwnerRelationship>
  </reportingOwner>
  <derivativeTable>
    <derivativeTransaction>
      <transactionDate><value>2026-03-15</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
      </transactionAmounts>
    </derivativeTransaction>
  </derivativeTable>
</ownershipDocument>"""
    rows = parse_form4_xml(
        only_deriv_xml,
        cik="0000320193",
        accn="0000320193-26-999999",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    assert rows == []
