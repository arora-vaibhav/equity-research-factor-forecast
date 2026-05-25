"""FredSource — Federal Reserve Economic Data (St. Louis Fed).

Uses the public `fredgraph.csv` endpoint, which is keyless and stable.
A.1 deliverable: health_check via DGS10 (10y Treasury).
A.3.5 deliverable: fetch_macro_series — pulls the spec-required macro
series (DGS10, DGS3MO, DGS2, VIXCLS, CPIAUCSL by default), parses the
public CSV, and persists to raw_fred via INSERT OR IGNORE. Per-series
watermark `(fred, *, series:<id>)` tracks the latest observation_date
seen.

FRED CSV format
---------------
  DATE,<SERIES_ID>
  YYYY-MM-DD,<value or '.' for missing>
  ...

The `.` sentinel represents missing observations (non-trading days,
holidays, etc.). The parser maps `.` -> None (stored as NULL in SQLite).

Vintage-data note (look-ahead bias)
-----------------------------------
The `fredgraph.csv` endpoint returns the CURRENTLY REVISED series, not
the as-originally-published vintage. CPI in particular gets revised.
For research-grade look-ahead-bias avoidance we'd need ALFRED (the
archival FRED API with `realtime_start`/`realtime_end` per observation).
For v1 we leave the realtime_start/realtime_end columns None and
document the bias in the methodology handbook. When ALFRED integration
is added post-A.3.10, the same `raw_fred` schema accommodates the
finer-grained data with no migration.

SEC-style fair-access policy does not apply here (FRED has no published
rate limit on the public CSV endpoint), but we still serialize series
pulls with a polite 250ms delay between requests to avoid bursty
behavior.
"""
from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timezone
from typing import Iterable

import requests

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_PROBE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
_CSV_URL_FMT = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

# Default macro series per spec §6.4 + v2 spec §4 (Layer 1 macro context).
DEFAULT_MACRO_SERIES = ("DGS10", "DGS3MO", "DGS2", "VIXCLS", "CPIAUCSL")

# Polite inter-series delay (seconds). FRED has no documented limit, but
# burst-friendly behavior is courteous and matches our SEC pattern.
_INTER_SERIES_DELAY_SEC = 0.25

# FRED missing-observation sentinel.
_FRED_MISSING_SENTINEL = "."


class FredSource(BaseDataSource):
    name = "fred"
    cadence = "weekly"
    provides = {
        "risk_free_rate_10y",
        "risk_free_rate_3m",
        "risk_free_rate_2y",
        "vix_close",
        "cpi_yoy",
    }

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
        lines = resp.text.strip().split("\n")
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK,
            checked_at=now,
            message=f"DGS10 rows={len(lines) - 1}",
            response_time_ms=(time.monotonic() - started) * 1000,
        )

    # ------------------------------------------------------------------
    # Phase A.3.5: macro series ingest
    # ------------------------------------------------------------------

    def _parse_fred_csv(
        self,
        csv_text: str,
        *,
        series_id: str,
        run_id: str,
        source_filename: str,
        scrape_timestamp: str,
    ) -> list:
        """Parse the FRED public CSV into a list of RawFredObservation."""
        from src.common.schemas import RawFredObservation

        rows: list = []
        if not csv_text:
            return rows
        buf = io.StringIO(csv_text)
        reader = csv.reader(buf)
        try:
            header = next(reader)
        except StopIteration:
            return rows
        if not header or header[0].strip().upper() != "DATE":
            # Not a parseable FRED CSV.
            return rows

        for raw_row in reader:
            if not raw_row or len(raw_row) < 2:
                continue
            date_s = (raw_row[0] or "").strip()
            value_s = (raw_row[1] or "").strip()
            if not date_s:
                continue

            if value_s == _FRED_MISSING_SENTINEL or value_s == "":
                value = None
            else:
                try:
                    value = float(value_s)
                except (TypeError, ValueError):
                    # Malformed value — store as missing rather than drop.
                    value = None

            try:
                obs = RawFredObservation(
                    run_id=run_id,
                    series_id=series_id,
                    observation_date=date_s,
                    value=value,
                    realtime_start=None,
                    realtime_end=None,
                    source_filename=source_filename,
                    scrape_timestamp=scrape_timestamp,
                )
            except Exception:
                # Pydantic validation failed (e.g., empty date). Skip row.
                continue
            rows.append(obs)
        return rows

    def fetch_macro_series(
        self,
        run_id: str,
        db=None,
        *,
        series_ids: Iterable[str] | None = None,
    ) -> int:
        """Pull each FRED macro series and persist to raw_fred.

        End-to-end pipeline (per series):
          1. GET https://fred.stlouisfed.org/graph/fredgraph.csv?id=<id>
          2. _parse_fred_csv() -> list[RawFredObservation]
          3. INSERT OR IGNORE into raw_fred
          4. update_watermark(fred, *, series:<id>) with max observation_date

        Per-series failures (HTTP error, parse error) are recorded in the
        watermark's error_count and the loop continues with the remaining
        series — Principle 6's per-source resilience.

        Polite 250ms delay between series (FRED is generous, but bursty
        behavior on a shared endpoint is rude).

        Parameters
        ----------
        run_id : provenance tag stamped on every emitted row
        db     : DatabaseManager (required)
        series_ids : iterable of series IDs to pull; default
                     DEFAULT_MACRO_SERIES (5 series per spec §6.4)

        Returns
        -------
        int : total NEW rows inserted across all series (net of
              INSERT OR IGNORE collisions).
        """
        if db is None:
            raise ValueError("db is required")

        ids = list(series_ids) if series_ids is not None else list(DEFAULT_MACRO_SERIES)

        scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        total_inserted = 0
        import sqlite3
        import datetime as _dt

        for i, sid in enumerate(ids):
            field = f"series:{sid}"
            url = _CSV_URL_FMT.format(series_id=sid)

            if i > 0:
                time.sleep(_INTER_SERIES_DELAY_SEC)

            # 1. Fetch
            try:
                resp = requests.get(url, timeout=15)
            except Exception as exc:  # noqa: BLE001
                self.update_watermark(
                    ticker="*", field=field,
                    last_observation_date=None, db=db, success=False,
                    error_message=f"http: {type(exc).__name__}: {exc}",
                )
                continue
            if not resp.ok:
                self.update_watermark(
                    ticker="*", field=field,
                    last_observation_date=None, db=db, success=False,
                    error_message=f"http {resp.status_code}",
                )
                continue

            # 2. Parse
            parsed = self._parse_fred_csv(
                resp.text,
                series_id=sid,
                run_id=run_id,
                source_filename=f"fredgraph.csv?id={sid}",
                scrape_timestamp=scrape_ts,
            )
            if not parsed:
                self.update_watermark(
                    ticker="*", field=field,
                    last_observation_date=None, db=db, success=False,
                    error_message="parse: no rows extracted",
                )
                continue

            # 3. Insert OR IGNORE; count net new rows
            with sqlite3.connect(db.db_path) as c:
                n_before = c.execute(
                    "SELECT COUNT(*) FROM raw_fred WHERE series_id=?", (sid,)
                ).fetchone()[0]
            db.insert_raw_fred(parsed)
            with sqlite3.connect(db.db_path) as c:
                n_after = c.execute(
                    "SELECT COUNT(*) FROM raw_fred WHERE series_id=?", (sid,)
                ).fetchone()[0]
            n_inserted = n_after - n_before
            total_inserted += n_inserted

            # 4. Watermark advance to max observation_date observed
            try:
                latest_iso = max(
                    r.observation_date for r in parsed if r.observation_date
                )
                last_obs = _dt.date.fromisoformat(latest_iso)
            except (ValueError, TypeError):
                last_obs = None
            self.update_watermark(
                ticker="*", field=field,
                last_observation_date=last_obs, db=db, success=True,
            )

        return total_inserted
