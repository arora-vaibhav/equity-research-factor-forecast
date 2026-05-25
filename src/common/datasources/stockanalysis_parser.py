"""Pure HTML parser for stockanalysis.com ratio history pages.

The function `parse_ratios_page` is intentionally I/O-free: it takes a
ready HTML string and returns a list of `RawStockanalysisRatioRow`. All
HTTP, retries, watermarks, and database writes live in
`StockanalysisSource.fetch_ratio_history`.

DOM contract (load-bearing)
---------------------------
stockanalysis.com publishes ratio data in a single `<table>` per page.
Two patterns are observed in the wild:

  1. The table carries a class hint such as `ratios-table` or similar.
  2. The page may render multiple tables; the ratio table is the LARGEST
     by `<tr>` count and always contains the metric-name labels in the
     first column.

This parser is robust to both: it iterates through every `<table>` in the
document, scores each by row-count, picks the largest, and falls back to
empty-list-with-log if no candidate has at least one header row + one
data row.

Header date normalization
-------------------------
Period header labels are normalized to `YYYY-MM-DD` ISO dates:

  * Annual: a 4-digit year `YYYY` -> `YYYY-12-31`. Aligned to the
    calendar fiscal-year end. Companies with off-calendar FYs (Apple,
    Walmart) accept this approximation in v1; materialization layer can
    refine via EDGAR period-end alignment in A.3.8+.
  * Quarterly: `Q1 YYYY` -> `YYYY-03-31`, `Q2 YYYY` -> `YYYY-06-30`,
    `Q3 YYYY` -> `YYYY-09-30`, `Q4 YYYY` -> `YYYY-12-31`.
  * TTM: literal `TTM` -> today's UTC date (scrape moment); if a
    specific period like `Q3 2024` is in the TTM header, that wins.

Metric-name normalization
-------------------------
Metric-name labels are matched against a whitelist that maps the
human-readable HTML label to the canonical Literal in
`RawStockanalysisRatioRow.metric`. Anything not on the whitelist is
skipped silently (so a new metric showing up in a future layout doesn't
trip Pydantic Literal validation). To support a new metric: add an entry
to `_METRIC_LABEL_MAP` AND extend the Literal in schemas.py.

Value parsing
-------------
Per-cell values support three formats:

  * Decimal float like `28.45` -> `28.45`
  * Percentage like `25.31%` -> `0.2531` (decimal)
  * Currency like `$1.50` -> `1.50` (rare in ratio pages but tolerated)
  * Missing sentinels `n/a`, `-`, empty string -> None

Unparseable values become None (the row is still emitted because the
period+metric pair is useful provenance even with a missing value).
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from src.common.schemas import RawStockanalysisRatioRow


_log = logging.getLogger(__name__)


# Human-readable label -> canonical metric name. Case-insensitive lookup
# happens in _normalize_metric_label.
_METRIC_LABEL_MAP = {
    "pe ratio": "pe_ratio",
    "p/e ratio": "pe_ratio",
    "p/e": "pe_ratio",
    "pb ratio": "pb_ratio",
    "p/b ratio": "pb_ratio",
    "p/b": "pb_ratio",
    "ps ratio": "ps_ratio",
    "p/s ratio": "ps_ratio",
    "p/s": "ps_ratio",
    "ev/ebitda": "ev_ebitda",
    "ev / ebitda": "ev_ebitda",
    "dividend yield": "dividend_yield",
    "roe": "roe",
    "return on equity": "roe",
    "roa": "roa",
    "return on assets": "roa",
    "profit margin": "profit_margin",
    "net margin": "profit_margin",
    "operating margin": "operating_margin",
    "fcf yield": "fcf_yield",
    "free cash flow yield": "fcf_yield",
    "current ratio": "current_ratio",
    "debt / equity": "debt_to_equity",
    "debt/equity": "debt_to_equity",
    "debt to equity": "debt_to_equity",
}

_PERCENT_RE = re.compile(r"^\s*(-?[\d,]*\.?\d+)\s*%\s*$")
_CURRENCY_RE = re.compile(r"^\s*\$\s*(-?[\d,]*\.?\d+)\s*$")
_PLAIN_NUMBER_RE = re.compile(r"^\s*(-?[\d,]*\.?\d+)\s*$")

_MISSING_TOKENS = {"", "-", "—", "n/a", "na", "nan", "null"}

_QUARTER_RE = re.compile(r"^\s*Q\s*([1-4])\s+(\d{4})\s*$", re.IGNORECASE)
_YEAR_ONLY_RE = re.compile(r"^\s*(\d{4})\s*$")

_QUARTER_END = {
    1: "03-31",
    2: "06-30",
    3: "09-30",
    4: "12-31",
}


def _normalize_metric_label(label: str) -> Optional[str]:
    """Map a HTML row-label to the canonical metric name, or None if
    the label is not in our whitelist."""
    if not label:
        return None
    key = label.strip().lower()
    # Drop trailing punctuation like a trailing colon.
    key = key.rstrip(":").strip()
    return _METRIC_LABEL_MAP.get(key)


def _normalize_period_header(label: str, *, period_type: str) -> Optional[str]:
    """Map an HTML column-header label to a `YYYY-MM-DD` period-end date.

    Returns None if the header is not parseable (e.g., 'Metric',
    'Fiscal Year', 'Quarter').
    """
    if not label:
        return None
    text = label.strip()
    if not text:
        return None

    # Quarterly: "Q3 2024" -> "2024-09-30"
    m = _QUARTER_RE.match(text)
    if m:
        q = int(m.group(1))
        year = m.group(2)
        return f"{year}-{_QUARTER_END[q]}"

    # Annual: "2024" -> "2024-12-31"
    m = _YEAR_ONLY_RE.match(text)
    if m:
        return f"{m.group(1)}-12-31"

    # TTM: literal "TTM" with no embedded date -> today (UTC).
    if text.upper() == "TTM":
        if period_type == "ttm":
            return _dt.date.today().isoformat()
        return None

    # Anything else (e.g., "Metric", "Fiscal Year", "Quarter") is a
    # non-period header and should not yield a date.
    return None


def _parse_value(cell: str) -> Optional[float]:
    """Parse one HTML cell value to float or None.

    Handles percent ('25.31%' -> 0.2531), currency ('$1.50' -> 1.5),
    plain decimal ('28.45' -> 28.45), and missing-value sentinels
    ('n/a', '-', '', etc.) -> None.
    """
    if cell is None:
        return None
    s = cell.strip()
    if s.lower() in _MISSING_TOKENS:
        return None

    m = _PERCENT_RE.match(s)
    if m:
        try:
            return float(m.group(1).replace(",", "")) / 100.0
        except ValueError:
            return None

    m = _CURRENCY_RE.match(s)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None

    m = _PLAIN_NUMBER_RE.match(s)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None

    return None


def _select_ratio_table(soup: BeautifulSoup):
    """Pick the largest <table> with at least one header row and one
    data row, or return None.

    A table is a candidate if:
      - It has a <thead> OR a first <tr> we can treat as header.
      - It has at least one data row.
    The "ratio table" is whichever candidate has the most rows.
    """
    candidates = []
    for table in soup.find_all("table"):
        all_rows = table.find_all("tr")
        if len(all_rows) < 2:
            continue
        candidates.append((len(all_rows), table))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _extract_header_cells(table) -> list[str]:
    """Pull the column-header labels from the first <tr> (preferring
    one inside <thead> if present)."""
    thead = table.find("thead")
    if thead is not None:
        first_row = thead.find("tr")
    else:
        first_row = table.find("tr")
    if first_row is None:
        return []
    cells = first_row.find_all(["th", "td"])
    return [c.get_text(strip=True) for c in cells]


def _extract_data_rows(table) -> list[list[str]]:
    """Pull all data rows (skipping the header row if it's at the top
    of <tbody> rather than in <thead>)."""
    tbody = table.find("tbody")
    if tbody is not None:
        trs = tbody.find_all("tr")
    else:
        # Fallback: every <tr> except the first.
        all_trs = table.find_all("tr")
        trs = all_trs[1:] if len(all_trs) > 1 else []
    out: list[list[str]] = []
    for tr in trs:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        out.append([c.get_text(strip=True) for c in cells])
    return out


def parse_ratios_page(
    html: str,
    ticker: str,
    period_type: str,
    source_url: str,
    *,
    run_id: str = "",
    scrape_timestamp: Optional[str] = None,
) -> list[RawStockanalysisRatioRow]:
    """Parse a stockanalysis.com ratio-history page into long-format rows.

    Returns an empty list if the HTML structure is unparseable. Logs the
    failure mode at WARNING level. Never raises on layout surprises —
    the caller treats the empty list as a soft failure and records an
    error_count increment on the watermark.

    Parameters
    ----------
    html : the page HTML as a string. Caller supplies; this function does no I/O.
    ticker : ticker symbol; will be uppercased and validated by Pydantic.
    period_type : one of {'annual', 'quarterly', 'ttm'}.
    source_url : the URL the HTML came from; stamped on every emitted row.
    run_id : optional provenance tag.
    scrape_timestamp : ISO-8601 UTC; defaults to now() if not provided.
    """
    if scrape_timestamp is None:
        scrape_timestamp = (
            _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
        )

    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "stockanalysis_parser: BeautifulSoup raised on ticker=%s url=%s: %s",
            ticker, source_url, exc,
        )
        return []

    table = _select_ratio_table(soup)
    if table is None:
        _log.warning(
            "stockanalysis_parser: no <table> found for ticker=%s url=%s",
            ticker, source_url,
        )
        return []

    header_cells = _extract_header_cells(table)
    if len(header_cells) < 2:
        _log.warning(
            "stockanalysis_parser: header row too short (%d cells) for ticker=%s",
            len(header_cells), ticker,
        )
        return []

    # The first header cell is the metric-name column label (e.g.,
    # "Fiscal Year", "Quarter", "Metric"). The remaining cells are the
    # period columns to normalize.
    period_dates: list[Optional[str]] = [None]  # placeholder for col 0
    for h in header_cells[1:]:
        period_dates.append(_normalize_period_header(h, period_type=period_type))

    # If NONE of the period headers parsed, the table isn't a ratio table.
    if not any(p for p in period_dates[1:]):
        _log.warning(
            "stockanalysis_parser: no parseable period headers in %s for ticker=%s",
            header_cells, ticker,
        )
        return []

    data_rows = _extract_data_rows(table)
    if not data_rows:
        _log.warning(
            "stockanalysis_parser: header present but no data rows for ticker=%s",
            ticker,
        )
        return []

    emitted: list[RawStockanalysisRatioRow] = []
    for row_cells in data_rows:
        if len(row_cells) < 2:
            continue
        metric_label = row_cells[0]
        canonical_metric = _normalize_metric_label(metric_label)
        if canonical_metric is None:
            # Not on our whitelist; skip silently.
            continue
        # Each data column corresponds 1:1 with the matching position
        # in period_dates. row_cells[0] is the label; row_cells[i] for
        # i>=1 is the value at period_dates[i].
        for i, val_text in enumerate(row_cells[1:], start=1):
            if i >= len(period_dates):
                break
            period_end_date = period_dates[i]
            if period_end_date is None:
                continue
            value = _parse_value(val_text)
            try:
                emitted.append(
                    RawStockanalysisRatioRow(
                        run_id=run_id,
                        ticker=ticker,
                        metric=canonical_metric,
                        period_end_date=period_end_date,
                        period_type=period_type,
                        value=value,
                        source_url=source_url,
                        scrape_timestamp=scrape_timestamp,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "stockanalysis_parser: row rejected (%s) "
                    "ticker=%s metric=%s period=%s",
                    exc, ticker, canonical_metric, period_end_date,
                )
                # If the ticker itself is invalid, the FIRST row failure
                # signals a fundamental problem — bail out rather than
                # spamming the log with one rejection per row.
                if "ticker" in str(exc).lower():
                    return []
                continue

    return emitted
