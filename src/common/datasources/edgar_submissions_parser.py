"""Pure-function parser for the SEC EDGAR submissions index JSON.

Input: the JSON returned by
  https://data.sec.gov/submissions/CIK{padded}.json

This file contains a per-issuer list of recent SEC filings. The
`filings.recent` block is a column-oriented structure: each filing
field (accessionNumber, filingDate, form, etc.) is a list, and the
i-th element across all lists describes the i-th filing.

Output: a tuple of
  - list[RawEdgarFilingRow]: one envelope row per filing whose form
    matches the filter
  - list[str]: the subset of accession numbers whose form_type == "4"
    (or "4/A"), so the orchestrator knows which Form 4 XML docs to
    fetch next.

The primary_doc_url for each filing is constructed by EDGAR's
deterministic Archive path scheme:
  https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accn_no_dashes}/{primary_doc}

Pure-function — no HTTP, no DB, no globals.
"""
from __future__ import annotations

import datetime as _dt
from typing import Iterable, Optional

from src.common.schemas import RawEdgarFilingRow


# Default filter for which forms to record in raw_edgar_filings.
DEFAULT_FORM_TYPES: frozenset[str] = frozenset({
    "8-K", "8-K/A",
    "10-K", "10-K/A",
    "10-Q", "10-Q/A",
    "4", "4/A",
})


_ARCHIVE_URL_FMT = (
    "https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accn_clean}/{primary_doc}"
)


def _build_primary_doc_url(
    cik_unpadded: str, accn: str, primary_doc: str
) -> Optional[str]:
    """Construct the canonical Archives URL for a filing's primary document.

    accn format from SEC: "NNNNNNNNNN-YY-NNNNNN" (e.g., "0000320193-26-000045").
    The Archives path uses the accn with dashes stripped:
      .../Archives/edgar/data/{cik_unpadded}/000032019326000045/aapl-...xml
    """
    if not accn or not primary_doc:
        return None
    accn_clean = accn.replace("-", "")
    return _ARCHIVE_URL_FMT.format(
        cik_unpadded=cik_unpadded.lstrip("0") or "0",
        accn_clean=accn_clean,
        primary_doc=primary_doc,
    )


def _date_floor(blob_dates: list[str], last_n_days: Optional[int]) -> Optional[str]:
    """Lower-bound date (ISO) for last_n_days filtering, anchored on max date."""
    if not last_n_days:
        return None
    if not blob_dates:
        return None
    try:
        latest = max(_dt.date.fromisoformat(d) for d in blob_dates if d)
    except ValueError:
        return None
    return (latest - _dt.timedelta(days=last_n_days)).isoformat()


def parse_submissions(
    blob: dict,
    *,
    ticker: str,
    run_id: str,
    scrape_timestamp: str,
    form_types: Optional[Iterable[str]] = None,
    last_n_days: Optional[int] = None,
) -> tuple[list[RawEdgarFilingRow], list[str]]:
    """Parse the submissions JSON.

    Parameters
    ----------
    blob
        The full JSON dict from /submissions/CIK{padded}.json.
    ticker
        Issuer ticker, stamped onto every emitted row.
    run_id, scrape_timestamp
        Provenance fields.
    form_types
        Iterable of form-type strings to include. Default
        DEFAULT_FORM_TYPES.
    last_n_days
        If set, drop filings older than `last_n_days` from the latest
        filing in the blob. None = no time filter.

    Returns
    -------
    (filing_rows, form4_accns)
    """
    types_filter = frozenset(form_types) if form_types is not None else DEFAULT_FORM_TYPES

    cik_raw = str(blob.get("cik", "")).strip()
    if not cik_raw:
        return [], []
    cik_padded = cik_raw.lstrip("0").zfill(10) if cik_raw.isdigit() else cik_raw
    cik_unpadded = cik_padded.lstrip("0") or "0"

    recent = (blob.get("filings") or {}).get("recent") or {}
    accns: list[str] = recent.get("accessionNumber") or []
    filing_dates: list[str] = recent.get("filingDate") or []
    accepted_ats: list[str] = recent.get("acceptanceDateTime") or []
    forms: list[str] = recent.get("form") or []
    items_list: list[str] = recent.get("items") or []
    primary_docs: list[str] = recent.get("primaryDocument") or []

    floor = _date_floor(filing_dates, last_n_days)

    rows: list[RawEdgarFilingRow] = []
    form4_accns: list[str] = []

    n = min(len(accns), len(filing_dates), len(forms))
    for i in range(n):
        form_type = (forms[i] or "").strip()
        if form_type not in types_filter:
            continue
        filing_date = (filing_dates[i] or "").strip()
        if floor and filing_date and filing_date < floor:
            continue
        accn = (accns[i] or "").strip()
        if not accn:
            continue

        accepted_at = accepted_ats[i] if i < len(accepted_ats) else None
        items_raw = items_list[i] if i < len(items_list) else None
        item_codes = items_raw.strip() if items_raw else None
        if item_codes == "":
            item_codes = None
        primary_doc = (primary_docs[i] or "").strip() if i < len(primary_docs) else ""
        primary_doc_url = _build_primary_doc_url(cik_unpadded, accn, primary_doc)

        try:
            row = RawEdgarFilingRow(
                run_id=run_id,
                ticker=ticker,
                cik=cik_padded,
                form_type=form_type,
                filing_date=filing_date,
                accepted_at=accepted_at,
                item_codes=item_codes,
                source_filing_accn=accn,
                primary_doc_url=primary_doc_url,
                scrape_timestamp=scrape_timestamp,
            )
        except Exception:
            continue
        rows.append(row)

        if form_type in ("4", "4/A"):
            form4_accns.append(accn)

    return rows, form4_accns
