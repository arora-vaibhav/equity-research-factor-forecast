"""Pure-function SEC Form 4 XML parser.

Form 4 is the SEC's disclosure for insider transactions in the
issuer's equity. The XML schema is the EDGAR Ownership XML
specification (Form 345 / version X0306+). Reference:
  https://www.sec.gov/info/edgar/specifications/form-345.html

Input: the raw XML bytes of the primary Form 4 document.
Output: a list of RawEdgarInsiderRow, one per parsed transaction.

Default behavior parses only `<nonDerivativeTransaction>` blocks
(equity buys/sells). With include_derivative=True, also emits a row
per `<derivativeTransaction>` block tagged with is_derivative=True.

The parser is intentionally tolerant of missing optional fields
(e.g., transactionPricePerShare may be absent for code A grants).
Required fields (transactionCode, transactionShares, transactionDate)
that are missing cause the affected block to be silently skipped.

`is_opportunistic` and `opportunistic_classifier_version` are NOT set
by this parser; they remain None on the produced rows. Those fields
are populated post-parse by
`src.methodology.opportunistic_insider.classify_transactions` running
across the aggregated batch of Form 4 line items.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional

from src.common.schemas import RawEdgarInsiderRow


class Form4ParseError(RuntimeError):
    """Raised when the XML is not parseable or required Form 4 nodes are missing."""


def _text(node: Optional[ET.Element]) -> Optional[str]:
    if node is None or node.text is None:
        return None
    s = node.text.strip()
    return s or None


def _value_text(parent: Optional[ET.Element], tag: str) -> Optional[str]:
    """Find <tag><value>...</value></tag> and return the value text."""
    if parent is None:
        return None
    child = parent.find(tag)
    if child is None:
        return None
    v = child.find("value")
    if v is not None:
        return _text(v)
    return _text(child)


def _direct_text(parent: Optional[ET.Element], tag: str) -> Optional[str]:
    if parent is None:
        return None
    child = parent.find(tag)
    return _text(child)


def _bool_from_01(s: Optional[str]) -> Optional[bool]:
    if s is None:
        return None
    s = s.strip().lower()
    if s in ("1", "true", "y", "yes"):
        return True
    if s in ("0", "false", "n", "no"):
        return False
    return None


def _float_or_none(s: Optional[str]) -> Optional[float]:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _extract_reporting_owner(root: ET.Element) -> dict:
    owner = root.find("reportingOwner")
    if owner is None:
        raise Form4ParseError("missing <reportingOwner>")
    rid = owner.find("reportingOwnerId")
    rel = owner.find("reportingOwnerRelationship")

    name = _direct_text(rid, "rptOwnerName") or "UNKNOWN"
    title = _direct_text(rel, "officerTitle")
    is_officer = _bool_from_01(_direct_text(rel, "isOfficer"))
    is_director = _bool_from_01(_direct_text(rel, "isDirector"))
    is_10pct = _bool_from_01(_direct_text(rel, "isTenPercentOwner"))
    return {
        "filer_name": name,
        "filer_title": title,
        "filer_is_officer": is_officer,
        "filer_is_director": is_director,
        "filer_is_10pct_owner": is_10pct,
    }


_TXN_CODE_DESCRIPTIONS = {
    "P": "Open-market or private purchase",
    "S": "Open-market or private sale",
    "A": "Grant or award (Rule 16b-3)",
    "M": "Exercise or conversion of derivative security",
    "D": "Disposition to issuer (Rule 16b-3)",
    "F": "Payment of exercise price or tax via share withholding",
    "G": "Bona fide gift",
    "J": "Other acquisition or disposition",
    "C": "Conversion of derivative security",
    "E": "Expiration of short derivative position",
    "H": "Expiration of long derivative position",
    "I": "Discretionary transaction (Rule 16b-3(f))",
    "O": "Exercise of out-of-money derivative",
    "X": "Exercise of in/at-money derivative",
    "V": "Transaction voluntarily reported early",
    "W": "Acquisition by will or laws of descent",
    "Z": "Voting trust deposit or withdrawal",
    "K": "Equity swap or similar instrument",
    "L": "Small acquisition (Rule 16a-6)",
    "U": "Disposition via tender in change-of-control",
}


def _parse_transaction(
    txn_node: ET.Element,
    owner_info: dict,
    is_derivative: bool,
    *,
    run_id: str,
    ticker: str,
    cik: str,
    accn: str,
    scrape_timestamp: str,
) -> Optional[RawEdgarInsiderRow]:
    txn_date = _value_text(txn_node, "transactionDate")
    coding = txn_node.find("transactionCoding")
    txn_code = _direct_text(coding, "transactionCode") if coding is not None else None
    amounts = txn_node.find("transactionAmounts")
    shares_s = _value_text(amounts, "transactionShares")
    price_s = _value_text(amounts, "transactionPricePerShare")
    post = txn_node.find("postTransactionAmounts")
    post_shares_s = _value_text(post, "sharesOwnedFollowingTransaction") if post is not None else None

    if not txn_date or not txn_code or shares_s is None:
        return None

    shares = _float_or_none(shares_s)
    if shares is None:
        return None
    price = _float_or_none(price_s)
    post_shares = _float_or_none(post_shares_s)

    total_value = None
    if price is not None and shares is not None:
        total_value = round(price * shares, 4)

    try:
        return RawEdgarInsiderRow(
            run_id=run_id,
            ticker=ticker,
            cik=cik,
            filer_name=owner_info["filer_name"],
            filer_title=owner_info.get("filer_title"),
            filer_is_officer=owner_info.get("filer_is_officer"),
            filer_is_director=owner_info.get("filer_is_director"),
            filer_is_10pct_owner=owner_info.get("filer_is_10pct_owner"),
            transaction_date=txn_date,
            transaction_code=txn_code,
            transaction_code_description=_TXN_CODE_DESCRIPTIONS.get(txn_code),
            shares=abs(shares),
            price_per_share=price,
            total_value=total_value,
            shares_after_transaction=post_shares,
            is_opportunistic=None,
            opportunistic_classifier_version=None,
            is_derivative=is_derivative,
            source_filing_accn=accn,
            scrape_timestamp=scrape_timestamp,
        )
    except Exception:
        return None


def parse_form4_xml(
    xml_bytes: bytes,
    *,
    cik: str,
    accn: str,
    ticker: str | None,
    scrape_timestamp: str,
    run_id: str,
    include_derivative: bool = False,
) -> list[RawEdgarInsiderRow]:
    """Parse a Form 4 XML document into a list of RawEdgarInsiderRow.

    Parameters
    ----------
    xml_bytes
        The raw XML bytes.
    cik
        Issuer CIK (10-digit padded).
    accn
        SEC accession number, used as source_filing_accn.
    ticker
        Issuer ticker symbol (required).
    scrape_timestamp
        ISO-8601 UTC scrape time.
    run_id
        Run identifier.
    include_derivative
        If True, also emit rows for <derivativeTransaction> blocks
        tagged with is_derivative=True. Default False.

    Raises
    ------
    Form4ParseError
        If the XML is malformed, missing <reportingOwner>, or otherwise
        not a Form 4 ownership document.
    """
    if ticker is None:
        raise Form4ParseError("ticker is required for Form 4 parse (PK constraint)")

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise Form4ParseError(f"malformed XML: {exc}") from exc

    if root.tag != "ownershipDocument":
        raise Form4ParseError(f"not a Form 4 ownershipDocument; root tag = {root.tag!r}")

    owner_info = _extract_reporting_owner(root)

    out: list[RawEdgarInsiderRow] = []

    nd_table = root.find("nonDerivativeTable")
    if nd_table is not None:
        for txn in nd_table.findall("nonDerivativeTransaction"):
            row = _parse_transaction(
                txn, owner_info, is_derivative=False,
                run_id=run_id, ticker=ticker, cik=cik,
                accn=accn, scrape_timestamp=scrape_timestamp,
            )
            if row is not None:
                out.append(row)

    if include_derivative:
        d_table = root.find("derivativeTable")
        if d_table is not None:
            for txn in d_table.findall("derivativeTransaction"):
                row = _parse_transaction(
                    txn, owner_info, is_derivative=True,
                    run_id=run_id, ticker=ticker, cik=cik,
                    accn=accn, scrape_timestamp=scrape_timestamp,
                )
                if row is not None:
                    out.append(row)

    return out
