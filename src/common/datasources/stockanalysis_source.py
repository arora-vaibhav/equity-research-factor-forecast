"""StockanalysisSource — adapter for stockanalysis.com.

Used primarily for multi-year ratio history (P/E, EV/EBITDA, P/B, P/S,
dividend yield, ROE, ROA, profit/operating margins, FCF yield, current
ratio, debt/equity) needed to compute the `pe_5y_percentile` and
`ev_ebitda_5y_percentile` features in `canonical_universe` (A.3.10).

A.1 deliverable: health_check via a known stable URL.
A.3.6 deliverable: fetch_ratio_history — pulls three ratio pages
(annual + quarterly + trailing/TTM), parses each via the pure
`stockanalysis_parser.parse_ratios_page`, persists rows to
`raw_stockanalysis_ratios` via INSERT OR IGNORE, and updates the
per-ticker watermark `(stockanalysis, <ticker>, ratio_history)`.

URL pattern (verified from live pages)
--------------------------------------
  Annual:    https://stockanalysis.com/stocks/<ticker>/financials/ratios/
  Quarterly: https://stockanalysis.com/stocks/<ticker>/financials/ratios/?p=quarterly
  TTM:       https://stockanalysis.com/stocks/<ticker>/financials/ratios/?p=trailing

If these change in the future, only the URL-format constants below need
to be updated — the parser is URL-pattern-independent.

90-day refresh-skip semantics
-----------------------------
Ratios update with each quarterly earnings release, so refreshing more
often than ~90 days is bandwidth-and-politeness waste. Per spec §6.6,
the watermark `(stockanalysis, <ticker>, ratio_history)` short-circuits
the fetch if the previous successful pull is younger than 90 days.
Caller can force a refresh by clearing the watermark.

Polite delay
------------
1.0 second between each of the three URL pulls per ticker (annual ->
quarterly -> TTM). stockanalysis.com is a static content site without a
documented rate limit, but bursty behavior is rude and risks soft IP
limiting.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)
from src.common.datasources.stockanalysis_parser import parse_ratios_page


_log = logging.getLogger(__name__)


_PROBE_URL = "https://stockanalysis.com/stocks/aapl/financials/ratios/"
_ANNUAL_URL_FMT = "https://stockanalysis.com/stocks/{ticker_lower}/financials/ratios/"
_QUARTERLY_URL_FMT = (
    "https://stockanalysis.com/stocks/{ticker_lower}/financials/ratios/?p=quarterly"
)
_TTM_URL_FMT = (
    "https://stockanalysis.com/stocks/{ticker_lower}/financials/ratios/?p=trailing"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Trade Identifier; research@example.com)"
    ),
}

# Polite inter-URL delay (seconds) when pulling annual -> quarterly -> ttm.
_INTER_URL_DELAY_SEC = 1.0

# Per spec §6.6: refresh-skip window for the ratio_history watermark.
_REFRESH_SKIP_DAYS = 90

# HTTP timeout per fetch (seconds).
_HTTP_TIMEOUT_SEC = 15


class StockanalysisSource(BaseDataSource):
    name = "stockanalysis"
    cadence = "weekly"
    provides = {
        "pe_ttm", "pe_forward",
        "ebit_ttm", "fcf_ttm",
        "operating_margin", "net_profit_margin",
        "roe", "roic",
        "pe_5y_history_raw",
        "ev_ebitda_5y_history_raw",
    }

    # ------------------------------------------------------------------
    # A.1: health_check (unchanged)
    # ------------------------------------------------------------------

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            resp = requests.get(_PROBE_URL, headers=_HEADERS, timeout=10)
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        if not resp.ok:
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"HTTP {resp.status_code}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK,
            checked_at=now,
            message=f"HTTP 200 ({len(resp.content)} bytes)",
            response_time_ms=(time.monotonic() - started) * 1000,
        )

    # ------------------------------------------------------------------
    # A.3.6: ratio history fetch
    # ------------------------------------------------------------------

    def _build_urls(self, ticker: str) -> list[tuple[str, str]]:
        """Return a list of (period_type, url) for the three pages we pull."""
        t = ticker.lower()
        return [
            ("annual", _ANNUAL_URL_FMT.format(ticker_lower=t)),
            ("quarterly", _QUARTERLY_URL_FMT.format(ticker_lower=t)),
            ("ttm", _TTM_URL_FMT.format(ticker_lower=t)),
        ]

    def _http_get(self, url: str) -> Optional[str]:
        """Fetch one URL with a UA header. Returns text on 200, or None
        on any failure (HTTP error, network error). Caller treats None
        as a soft per-URL failure."""
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001
            _log.warning("stockanalysis http error url=%s: %s", url, exc)
            return None
        if not resp.ok:
            _log.warning(
                "stockanalysis http non-200 url=%s status=%s",
                url, resp.status_code,
            )
            return None
        return resp.text

    def fetch_ratio_history(
        self,
        ticker: str,
        run_id: str,
        db=None,
        *,
        refresh_after_days: int = _REFRESH_SKIP_DAYS,
    ) -> int:
        """Pull annual + quarterly + TTM ratio pages for one ticker.

        Pipeline:
          0. Check watermark (stockanalysis, <ticker>, ratio_history).
             If the previous successful pull is within `refresh_after_days`,
             return 0 immediately.
          1. For each (period_type, url) in (annual, quarterly, ttm):
               a. HTTP GET with polite 1.0s inter-URL delay
               b. parse_ratios_page(html, ticker, period_type, url) ->
                  list[RawStockanalysisRatioRow]
               c. db.insert_raw_stockanalysis_ratios(rows)
               d. Track per-URL success/failure
          2. Update watermark — if ALL three URLs succeeded, success=True;
             if any URL failed (network, HTTP, parse), success=False and
             error_message records which URLs failed.

        Returns
        -------
        int : net new rows inserted across all three URLs.
        """
        if db is None:
            raise ValueError("db is required")

        field = "ratio_history"
        ticker = ticker.strip().upper()

        # 0. 90-day refresh-skip
        w = db.get_watermark(self.name, ticker, field)
        if w and w.get("last_observation_date"):
            try:
                last = _dt.date.fromisoformat(w["last_observation_date"])
                age_days = (_dt.date.today() - last).days
                if age_days < refresh_after_days and w.get("error_count", 0) == 0:
                    # Recently and cleanly refreshed; skip.
                    return 0
            except (TypeError, ValueError):
                pass

        scrape_ts = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )

        urls = self._build_urls(ticker)
        total_inserted = 0
        url_errors: list[str] = []
        import sqlite3

        for i, (period_type, url) in enumerate(urls):
            if i > 0:
                time.sleep(_INTER_URL_DELAY_SEC)

            html = self._http_get(url)
            if html is None:
                url_errors.append(f"http:{period_type}")
                continue

            rows = parse_ratios_page(
                html=html,
                ticker=ticker,
                period_type=period_type,
                source_url=url,
                run_id=run_id,
                scrape_timestamp=scrape_ts,
            )
            if not rows:
                url_errors.append(f"parse:{period_type}")
                continue

            # Count net new rows by before/after.
            with sqlite3.connect(db.db_path) as c:
                n_before = c.execute(
                    "SELECT COUNT(*) FROM raw_stockanalysis_ratios "
                    "WHERE ticker=? AND period_type=?",
                    (ticker, period_type),
                ).fetchone()[0]
            db.insert_raw_stockanalysis_ratios(rows)
            with sqlite3.connect(db.db_path) as c:
                n_after = c.execute(
                    "SELECT COUNT(*) FROM raw_stockanalysis_ratios "
                    "WHERE ticker=? AND period_type=?",
                    (ticker, period_type),
                ).fetchone()[0]
            total_inserted += (n_after - n_before)

        # Watermark advance.
        if url_errors:
            self.update_watermark(
                ticker=ticker, field=field,
                last_observation_date=_dt.date.today(),
                db=db, success=False,
                error_message="; ".join(url_errors),
            )
        else:
            self.update_watermark(
                ticker=ticker, field=field,
                last_observation_date=_dt.date.today(),
                db=db, success=True,
            )

        return total_inserted
