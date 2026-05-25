"""FinraSource — FINRA short-interest / short-volume CDN.

FINRA publishes per-market-center short-volume data daily and the
official short-interest report twice monthly. A.1 deliverable:
health_check by probing the public landing page.

A.3.5 deliverable: fetch_short_interest — pulls the daily short-volume
file from FINRA's CDN, parses the pipe-delimited per-ticker records,
and persists to raw_finra via INSERT OR IGNORE. Watermark
`(finra, *, short_interest_biweekly)` tracks the latest settlement date
seen; refresh is skipped within 14 days per spec §6.5.

URL pattern (verify before production)
--------------------------------------
The FINRA CDN organizes daily short-volume files under:
  https://cdn.finra.org/equity/regsho/daily/<MARKET><FILE>{YYYYMMDD}.txt

Per-market-center file stems:
  - FNRA : NYSE
  - FNSQ : Nasdaq          <-- A.3.5 default (broadest coverage)
  - FNYX : NYSE American
  - FORF : FINRA / OTC

The default pull uses the Nasdaq daily file (FNSQ) because Nasdaq has
the broadest single-file coverage of the large-cap universe we care
about for Layer 1. Future enhancement (post-A.3.10): pull all four and
aggregate by exchange. The `exchange` column in raw_finra accommodates
that without schema change.

Note: this is NOT the FINRA biweekly "Short Interest" report (which
lives at the FINRA Query API and requires JSON aggregation). The daily
file gives us per-day per-ticker short-volume + total-volume, which is
sufficient for Signal 2 (Diether-Lee-Werner 2009 days-to-cover proxy)
at biweekly cadence after materialization-layer aggregation.

File format (pipe-delimited)
----------------------------
  Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
  YYYYMMDD|<ticker>|<int>|<int>|<int>|<exchange>
  ...
"""
from __future__ import annotations

import datetime as _dt
import time
from datetime import datetime, timezone

import requests

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_PROBE_URL = "https://www.finra.org/finra-data/short-sale-volume-data"
_DAILY_FILE_URL_FMT = (
    "https://cdn.finra.org/equity/regsho/daily/{stem}shvol{yyyymmdd}.txt"
)

# Default per-market-center file stem. FNSQ = Nasdaq daily short-volume.
_DEFAULT_FILE_STEM = "FNSQ"

# Canonical exchange code stamped on raw_finra rows when pulling each
# file stem. Future enhancement may pull all four and stamp accordingly.
_STEM_TO_EXCHANGE = {
    "FNRA": "NYSE",
    "FNSQ": "NSDQ",
    "FNYX": "NYAX",
    "FORF": "ORF",
}

# Per spec §6.5: skip biweekly refresh if previous successful pull was
# within this many days. Caller can force a refresh by passing an
# explicit settlement_date.
_REFRESH_SKIP_DAYS = 14


class FinraSource(BaseDataSource):
    name = "finra"
    cadence = "biweekly"
    provides = {"short_interest_pct_float"}

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            resp = requests.get(_PROBE_URL, timeout=10)
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
            message="HTTP 200",
            response_time_ms=(time.monotonic() - started) * 1000,
        )

    # ------------------------------------------------------------------
    # Phase A.3.5: short-interest ingest
    # ------------------------------------------------------------------

    def _parse_shvol_text(
        self,
        text: str,
        *,
        settlement_date: _dt.date,
        exchange: str,
        run_id: str,
        source_filename: str,
        scrape_timestamp: str,
    ) -> list:
        """Parse a FINRA daily short-volume pipe-delimited file."""
        from src.common.schemas import RawFinraShortInterest

        rows: list = []
        if not text:
            return rows

        settlement_iso = settlement_date.isoformat()

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue
            # Skip header row: "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market"
            if parts[0].strip().lower() == "date":
                continue
            symbol = parts[1].strip()
            if not symbol:
                continue
            short_vol_s = parts[2].strip()
            total_vol_s = parts[4].strip() if len(parts) > 4 else ""
            try:
                short_vol = float(short_vol_s)
            except (TypeError, ValueError):
                continue
            try:
                total_vol = float(total_vol_s) if total_vol_s else None
            except (TypeError, ValueError):
                total_vol = None

            # days_to_cover = short_interest_shares / avg_daily_volume.
            # We use the daily TotalVolume as our ADV proxy; the
            # materialization layer can later replace this with a true
            # 30-day average by joining against raw_yahoo prices.
            dtc = None
            if total_vol and total_vol > 0:
                try:
                    dtc = short_vol / total_vol
                except ZeroDivisionError:
                    dtc = None

            try:
                row = RawFinraShortInterest(
                    run_id=run_id,
                    ticker=symbol,
                    settlement_date=settlement_iso,
                    exchange=exchange,
                    short_interest_shares=short_vol,
                    avg_daily_volume=total_vol,
                    days_to_cover=dtc,
                    source_filename=source_filename,
                    scrape_timestamp=scrape_timestamp,
                )
            except Exception:
                # Pydantic validation failed (e.g., invalid ticker). Skip.
                continue
            rows.append(row)
        return rows

    def fetch_short_interest(
        self,
        run_id: str,
        db=None,
        *,
        settlement_date: _dt.date | None = None,
        file_stem: str = _DEFAULT_FILE_STEM,
    ) -> int:
        """Pull a FINRA daily short-volume file and persist to raw_finra.

        Watermark logic
        ---------------
        Watermark `(finra, *, short_interest_biweekly)` tracks the latest
        settlement_date seen. If `settlement_date is None`, the source
        first checks the watermark:
          - If the previous successful pull is within 14 days, skip
            (return 0). Per spec §6.5.
          - Else target today() as the settlement date.
        An explicit `settlement_date` from the caller overrides the
        14-day skip — explicit-beats-default for backfills.

        Per-fetch failures (HTTP error, parse error, file not found)
        increment the watermark's error_count and return 0.

        Returns
        -------
        int : net new rows inserted (after INSERT OR IGNORE).
        """
        if db is None:
            raise ValueError("db is required")

        field = "short_interest_biweekly"

        # Resolve the target settlement date.
        if settlement_date is None:
            w = db.get_watermark(self.name, "*", field)
            if w and w.get("last_observation_date"):
                try:
                    last_obs = _dt.date.fromisoformat(w["last_observation_date"])
                    if (_dt.date.today() - last_obs).days < _REFRESH_SKIP_DAYS:
                        # Refresh-skip per §6.5.
                        return 0
                except ValueError:
                    pass
            settlement_date = _dt.date.today()

        exchange = _STEM_TO_EXCHANGE.get(file_stem, file_stem)
        yyyymmdd = settlement_date.strftime("%Y%m%d")
        source_filename = f"{file_stem}shvol{yyyymmdd}.txt"
        url = _DAILY_FILE_URL_FMT.format(stem=file_stem, yyyymmdd=yyyymmdd)
        scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # Fetch
        try:
            resp = requests.get(url, timeout=15)
        except Exception as exc:  # noqa: BLE001
            self.update_watermark(
                ticker="*", field=field,
                last_observation_date=None, db=db, success=False,
                error_message=f"http: {type(exc).__name__}: {exc}",
            )
            return 0
        if not resp.ok:
            self.update_watermark(
                ticker="*", field=field,
                last_observation_date=None, db=db, success=False,
                error_message=f"http {resp.status_code} for {source_filename}",
            )
            return 0

        # Parse
        parsed = self._parse_shvol_text(
            resp.text,
            settlement_date=settlement_date,
            exchange=exchange,
            run_id=run_id,
            source_filename=source_filename,
            scrape_timestamp=scrape_ts,
        )

        if not parsed:
            self.update_watermark(
                ticker="*", field=field,
                last_observation_date=None, db=db, success=False,
                error_message=f"parse: no rows in {source_filename}",
            )
            return 0

        # Insert OR IGNORE; count net new rows
        import sqlite3
        with sqlite3.connect(db.db_path) as c:
            n_before = c.execute(
                "SELECT COUNT(*) FROM raw_finra WHERE settlement_date=? AND exchange=?",
                (settlement_date.isoformat(), exchange),
            ).fetchone()[0]
        db.insert_raw_finra(parsed)
        with sqlite3.connect(db.db_path) as c:
            n_after = c.execute(
                "SELECT COUNT(*) FROM raw_finra WHERE settlement_date=? AND exchange=?",
                (settlement_date.isoformat(), exchange),
            ).fetchone()[0]
        n_inserted = n_after - n_before

        # Watermark advance
        self.update_watermark(
            ticker="*", field=field,
            last_observation_date=settlement_date, db=db, success=True,
        )

        return n_inserted
