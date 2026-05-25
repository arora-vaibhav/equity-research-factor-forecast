"""EDGAR full-text fetcher for 10-K / 10-Q Item 1A Risk Factors extraction.

Two responsibilities:
  * `fetch_filing_text(primary_doc_url, *, headers, session=None)` --
    tenacity-retried SEC GET that returns the raw response text (HTML
    or plain text -- caller doesn't know in advance).
  * `extract_risk_factors_section(html_or_text)` -- locate Item 1A in
    the document and return the text between Item 1A and the first
    following section boundary (Item 1B or Item 2). Permissive: when
    Item 1A markers are not found (older filings, layout variation),
    returns the entire document body with status
    'full_document_fallback'. Added in A.3.9.

Pure-ish: `fetch_filing_text` does network I/O; `extract_risk_factors_section`
is a pure function over text. Caller orchestrates dictionary load,
scoring, and DB hand-off in `EdgarSource.fetch_filing_text_for_ticker`.
"""
from __future__ import annotations

import re
from typing import Optional

import requests
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class _SecHttpError(RuntimeError):
    """Wrapper for retry-eligible HTTP errors from SEC endpoints."""


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
    retry=retry_if_exception_type(_SecHttpError),
)
def _sec_get_with_retry(
    url: str, headers: dict, *, session: Optional[requests.Session] = None
) -> requests.Response:
    """GET an SEC URL with retry on 429/503. Other 4xx fail fast.

    Mirrors `edgar_source._sec_get_with_retry` but is duplicated here
    so this module has no cross-source import.
    """
    do = session.get if session is not None else requests.get
    resp = do(url, headers=headers, timeout=20)
    if resp.status_code in (429, 503):
        raise _SecHttpError(f"transient HTTP {resp.status_code} for {url}")
    return resp


def fetch_filing_text(
    primary_doc_url: str,
    *,
    headers: dict,
    session: Optional[requests.Session] = None,
) -> str:
    """Fetch the filing's primary document and return its text body.

    Parameters
    ----------
    primary_doc_url
        Fully-qualified URL to the filing's primary document (HTML or
        text). Built by `edgar_submissions_parser._build_primary_doc_url`
        for Form 4 already; for 10-K / 10-Q the same builder produces
        URLs of the form
        `https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{filename}`.
    headers
        Outgoing HTTP headers -- must include `User-Agent` per SEC fair-
        access policy.
    session
        Optional `requests.Session` for connection pooling.

    Returns
    -------
    str
        The response body decoded as text. Empty string on non-2xx or
        empty body (caller logs `extraction_status='empty_document'`).

    Notes
    -----
    Does NOT parse HTML here -- returns raw response text. Extraction is
    `extract_risk_factors_section`'s job.
    """
    resp = _sec_get_with_retry(primary_doc_url, headers=headers, session=session)
    if not resp.ok:
        return ""
    return resp.text or ""


# Item 1A start markers. SEC filings vary -- "Item 1A.", "ITEM 1A:",
# "Item 1A Risk Factors", etc. The regex is case-insensitive, allows
# optional punctuation after the number, and tolerates whitespace.
_ITEM_1A_START_RE = re.compile(
    r"\bItem\s*1A\b[\.\s:\-]*\s*(?:Risk\s+Factors?)?",
    re.IGNORECASE,
)

# Section terminators: next item that comes after 1A. Prefer 1B, then
# Item 2 (the next major section). Case-insensitive, punctuation-tolerant.
_ITEM_END_RE = re.compile(
    r"\bItem\s*(?:1B|2)\b[\.\s:\-]",
    re.IGNORECASE,
)


def _html_to_text(blob: str) -> str:
    """Normalize HTML (or plain-text) into whitespace-collapsed string.

    BeautifulSoup `get_text(' ')` flattens tags while inserting spaces
    between previously-adjacent tag content. Non-HTML inputs pass
    through unchanged (BS treats them as a single text node).
    """
    if not blob:
        return ""
    soup = BeautifulSoup(blob, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(" ", strip=False)
    return re.sub(r"\s+", " ", text).strip()


def extract_risk_factors_section(html_or_text: str) -> tuple[str, str]:
    """Locate Item 1A Risk Factors and return (section_text, status).

    Permissive fallback (A.3.9):
      * 'item_1a_extracted' -- start marker found, returned text from
        start to first following Item 1B or Item 2 marker (or document
        end). (Renamed from 'item_1a_found' in R2 reconciliation
        2026-05-23.)
      * 'full_document_fallback' -- start marker not found; entire
        normalized document body returned.
      * 'empty_document' -- input was empty / yielded empty text.

    Why permissive: older 10-Ks (pre-2005) lack Item 1A entirely (the
    section was mandated only in 2005 per SEC FRR Sec. 503); layout
    variation across filers means strict marker matching produces poor
    coverage. The status flag preserves auditability so future analyses
    can stratify by extraction quality.

    The Item 1A start regex skips the first occurrence when it appears
    in the document's Table of Contents -- the TOC reference is
    typically followed almost immediately by Item 1B / Item 2 in the
    outline. When only one occurrence exists, we use it.

    Parameters
    ----------
    html_or_text
        Raw HTML or plain text returned by `fetch_filing_text`.

    Returns
    -------
    (str, str)
        (section_text, extraction_status). section_text is whitespace-
        normalized; callers pass it directly into `score_tone`.
    """
    body = _html_to_text(html_or_text)
    if not body:
        return ("", "empty_document")

    starts = [m.start() for m in _ITEM_1A_START_RE.finditer(body)]
    if not starts:
        return (body, "full_document_fallback")

    # Prefer the LAST match per A.3.9 Reconciliation R1 (2026-05-23).
    # Rationale: a 10-K's Item 1A typically appears in the Table of
    # Contents AND again at the section's actual body start; some
    # filings also list 1A in an exhibits index. The body occurrence
    # is reliably the latest one because all preceding references
    # point forward to it.
    start_idx = starts[-1]

    end_match = _ITEM_END_RE.search(body, pos=start_idx + 1)
    end_idx = end_match.start() if end_match else len(body)

    section = body[start_idx:end_idx].strip()
    if not section:
        return (body, "full_document_fallback")
    return (section, "item_1a_extracted")
