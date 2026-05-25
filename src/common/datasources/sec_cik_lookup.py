"""Ticker -> CIK lookup, backed by SEC's free `company_tickers.json` endpoint.

The SEC publishes a daily-refreshed JSON file at:
  https://www.sec.gov/files/company_tickers.json

Format (per SEC EDGAR docs):
  {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    ...
  }

This module loads it once per process (in-memory cache) and also persists a
snapshot to the `sec_ticker_cik_map` table. The (sec, *, ticker_cik_map)
watermark gates a daily refresh — if the watermark is from today, we serve
from the DB cache without HTTP.

SEC fair access: User-Agent header with contact email is REQUIRED on every
request to *.sec.gov per https://www.sec.gov/os/accessing-edgar-data.
"""
from __future__ import annotations

import datetime
import os
from typing import Optional

import requests

_CT_URL = "https://www.sec.gov/files/company_tickers.json"
_DEFAULT_UA = "Trade Identifier research@example.com"


class TickerNotFoundError(KeyError):
    """Raised when a ticker is not in the SEC company_tickers.json map."""


class SecCikLookup:
    """Resolve a ticker symbol to its 10-digit zero-padded CIK string."""

    def __init__(self, db):
        self.db = db
        self._cache: Optional[dict[str, str]] = None  # ticker -> padded CIK
        self._loaded_this_process = False

    def _headers(self) -> dict[str, str]:
        ua = os.environ.get("SEC_EDGAR_USER_AGENT", _DEFAULT_UA)
        return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}

    def _is_watermark_fresh(self) -> bool:
        w = self.db.get_watermark("sec", "*", "ticker_cik_map")
        if w is None or w.get("last_observation_date") is None:
            return False
        try:
            last = datetime.date.fromisoformat(w["last_observation_date"])
        except (TypeError, ValueError):
            return False
        return last >= datetime.date.today()

    def _load_from_db(self) -> dict[str, str]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT ticker, cik FROM sec_ticker_cik_map"
            ).fetchall()
        return {t.upper(): c for (t, c) in rows}

    def _fetch_from_sec(self) -> dict[str, dict]:
        resp = requests.get(_CT_URL, headers=self._headers(), timeout=15)
        if not resp.ok:
            raise RuntimeError(f"company_tickers.json HTTP {resp.status_code}")
        return resp.json()

    def _persist_snapshot(self, raw: dict) -> dict[str, str]:
        entries = []
        for _idx, rec in raw.items():
            ticker = str(rec.get("ticker", "")).strip().upper()
            cik = str(rec.get("cik_str", "")).strip()
            if not ticker or not cik:
                continue
            entries.append({
                "ticker": ticker,
                "cik": cik.zfill(10),
                "company_name": rec.get("title"),
            })
        self.db.upsert_sec_ticker_cik_map(entries)
        today_iso = datetime.date.today().isoformat()
        self.db.upsert_watermark(
            source="sec", ticker="*", field="ticker_cik_map",
            last_observation_date=today_iso, success=True,
        )
        return {e["ticker"]: e["cik"] for e in entries}

    def _ensure_loaded(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if self._is_watermark_fresh():
            self._cache = self._load_from_db()
            self._loaded_this_process = True
            return self._cache
        raw = self._fetch_from_sec()
        self._cache = self._persist_snapshot(raw)
        self._loaded_this_process = True
        return self._cache

    def resolve(self, ticker: str) -> str:
        """Return the 10-digit padded CIK for ticker. Raises TickerNotFoundError
        if the ticker is not in the SEC map."""
        t = ticker.strip().upper()
        cache = self._ensure_loaded()
        if t not in cache:
            raise TickerNotFoundError(f"ticker not in SEC company_tickers.json: {t!r}")
        return cache[t]
