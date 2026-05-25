"""GdeltSource — GDELT GKG news-mention volume fetcher.

A.3.8 deliverable: `fetch_news_volume(ticker, run_id, db)` pulls the
GDELT GKG 15-minute CSV files for the trailing `lookback_hours` and
emits one `RawGdeltMention` row per (gkg_record_id, ticker) match.

Ticker resolution (in priority order)
--------------------------------------
1. Cashtag in the SOCIALEMBED column ('$AAPL') → match_method='cashtag'
2. Company-name alias in V1ORGANIZATIONS → match_method='alias'
   Aliases are loaded from `config/gdelt_alias.yaml` at init.

Both paths use INSERT OR IGNORE.  The watermark
(gdelt, ticker, news_volume) advances to the max mention_timestamp
observed.

GKG file format
---------------
GDELT V2 publishes GKG files every 15 minutes.  The MASTERFILELIST.TXT
lists all files:
  http://data.gdeltproject.org/gdeltv2/masterfilelist.txt

Format (tab-separated, 3 fields per line):
  <epoch_ms>\\t<encoder>\\t<url>

Production GKG archives are .csv.zip; the test fixture uses .csv.gz for
stdlib-gzip convenience.  `_open_gkg()` accepts both.

GKG V2.1 columns (0-indexed, tab-delimited, 27+ columns):
  0  GKGRECORDID
  1  V21DATE          YYYYMMDDHHMMSS
  4  V2DOCUMENTIDENTIFIER  source URL
 13  V1ORGANIZATIONS  semicolon-separated org names
 21  V21SOCIALIMAGEEMBEDS  may contain $CASHTAG mentions

Column indices are defined as module constants so updates are visible.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_log = logging.getLogger(__name__)

_MASTERFILELIST_URL = (
    "http://data.gdeltproject.org/gdeltv2/masterfilelist.txt"
)

# GKG V2.1 column indices (0-indexed, tab-delimited).
_COL_GKGID = 0
_COL_DATE = 1
_COL_URL = 4
_COL_V1ORGANIZATIONS = 13
_COL_SOCIALEMBED = 21
_MIN_COLS = 22  # Need at least 22 columns to read SOCIALEMBED

# Cashtag pattern: $AAPL, $MSFT, etc.
_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})(?:[^A-Z]|$)")

# Polite inter-file delay (seconds).
_INTER_FILE_DELAY_SEC = 0.25

# HTTP timeout for individual GKG files.
_HTTP_TIMEOUT_SEC = 30

_DEFAULT_ALIAS_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "gdelt_alias.yaml"
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Trade Identifier; research@example.com)",
}


class GdeltSource(BaseDataSource):
    name = "gdelt"
    cadence = "hourly"
    provides = {"news_volume"}

    def __init__(self, alias_path: Path | str | None = None):
        self._alias_path = Path(alias_path) if alias_path else _DEFAULT_ALIAS_PATH
        self._alias_table: dict[str, list[str]] = self._load_alias_table()

    # ------------------------------------------------------------------
    # health_check — cheap HEAD to masterfilelist.
    # ------------------------------------------------------------------

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            resp = requests.head(
                _MASTERFILELIST_URL, headers=_HEADERS, timeout=10,
            )
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
            message=f"HTTP {resp.status_code}",
            response_time_ms=(time.monotonic() - started) * 1000,
        )

    # ------------------------------------------------------------------
    # A.3.8 fetch method
    # ------------------------------------------------------------------

    def fetch_news_volume(
        self,
        ticker: str,
        run_id: str,
        db=None,
        *,
        lookback_hours: int = 24,
    ) -> int:
        """Pull GDELT GKG files for trailing `lookback_hours`; emit mentions.

        Parameters
        ----------
        ticker
            Uppercase ticker symbol to match (e.g. 'AAPL').
        run_id
            Provenance tag stamped on every emitted row.
        db
            DatabaseManager (required).
        lookback_hours
            How many hours of GKG files to scan; default 24.

        Returns
        -------
        int
            Count of net-new RawGdeltMention rows inserted.
        """
        if db is None:
            raise ValueError("db is required")

        ticker = ticker.strip().upper()
        field = "news_volume"

        # Fetch and parse the masterfilelist.
        try:
            resp = requests.get(
                _MASTERFILELIST_URL, headers=_HEADERS, timeout=_HTTP_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("gdelt masterfilelist fetch failed: %s", exc)
            self.update_watermark(
                ticker=ticker, field=field,
                last_observation_date=None,
                db=db, success=False,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            return 0

        if not resp.ok:
            _log.warning("gdelt masterfilelist HTTP %s", resp.status_code)
            self.update_watermark(
                ticker=ticker, field=field,
                last_observation_date=None,
                db=db, success=False,
                error_message=f"HTTP {resp.status_code}",
            )
            return 0

        cutoff = (
            datetime.utcnow().timestamp() - lookback_hours * 3600
        )
        gkg_urls = self._parse_masterfilelist(resp.text, cutoff=cutoff)

        if not gkg_urls:
            _log.info("gdelt: no files in lookback window for %s", ticker)
            return 0

        import sqlite3 as _sqlite3

        scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        total_inserted = 0
        max_ts: Optional[str] = None

        for url in gkg_urls:
            time.sleep(_INTER_FILE_DELAY_SEC)
            rows = self._fetch_and_parse_gkg(
                url=url, ticker=ticker, run_id=run_id, scrape_ts=scrape_ts,
            )
            if rows:
                # Count net-new rows (INSERT OR IGNORE silently drops duplicates).
                with _sqlite3.connect(db.db_path) as _c:
                    n_before = _c.execute(
                        "SELECT COUNT(*) FROM raw_gdelt WHERE ticker=?",
                        (ticker,),
                    ).fetchone()[0]
                db.insert_raw_gdelt(rows)
                with _sqlite3.connect(db.db_path) as _c:
                    n_after = _c.execute(
                        "SELECT COUNT(*) FROM raw_gdelt WHERE ticker=?",
                        (ticker,),
                    ).fetchone()[0]
                total_inserted += n_after - n_before
                for r in rows:
                    ts = (
                        r.mention_timestamp if hasattr(r, "mention_timestamp")
                        else r.get("mention_timestamp", "")  # type: ignore[union-attr]
                    )
                    if max_ts is None or ts > max_ts:
                        max_ts = ts

        import datetime as _dt
        if max_ts:
            try:
                obs_date = _dt.date.fromisoformat(max_ts[:10])
            except ValueError:
                obs_date = _dt.date.today()
        else:
            obs_date = _dt.date.today()

        self.update_watermark(
            ticker=ticker, field=field,
            last_observation_date=obs_date,
            db=db,
            success=True,
        )
        return total_inserted

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_alias_table(self) -> dict[str, list[str]]:
        """Load ticker → alias list from YAML.  Returns {} on any error."""
        try:
            with open(self._alias_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return {
                ticker: [str(a) for a in aliases]
                for ticker, aliases in data.get("aliases", {}).items()
            }
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "gdelt: failed to load alias table %s: %s",
                self._alias_path, exc,
            )
            return {}

    @staticmethod
    def _parse_masterfilelist(
        text: str,
        *,
        cutoff: float,
    ) -> list[str]:
        """Parse MASTERFILELIST.TXT and return GKG file URLs within cutoff.

        Format (tab-separated):  <epoch_ms>  <encoder>  <url>
        """
        # Strip UTF-8 BOM (U+FEFF) if present (Windows Set-Content artifact).
        _BOM = "﻿"
        text = text.lstrip(_BOM)
        urls = []
        for line in text.splitlines():
            line = line.strip().lstrip(_BOM)
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            try:
                epoch_ms = float(parts[0])
            except ValueError:
                continue
            if (epoch_ms / 1000.0) < cutoff:
                continue
            url = parts[2].strip()
            if ".gkg." in url:
                urls.append(url)
        return urls

    def _open_gkg(self, content: bytes, url: str) -> Optional[str]:
        """Decompress a GKG archive (.zip or .gz) and return CSV text."""
        if url.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    names = zf.namelist()
                    csv_name = next(
                        (n for n in names if n.endswith(".csv")),
                        names[0] if names else None,
                    )
                    if csv_name is None:
                        return None
                    return zf.read(csv_name).decode("utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                _log.warning("gdelt: zip extract failed: %s", exc)
                return None
        else:
            # .csv.gz or similar — use gzip
            try:
                return gzip.decompress(content).decode("utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                _log.warning("gdelt: gzip decompress failed: %s", exc)
                return None

    def _fetch_and_parse_gkg(
        self,
        *,
        url: str,
        ticker: str,
        run_id: str,
        scrape_ts: str,
    ) -> list:
        """Download one GKG archive and return matching RawGdeltMention rows."""
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001
            _log.warning("gdelt: file fetch failed url=%s: %s", url, exc)
            return []

        if not resp.ok:
            _log.warning("gdelt: HTTP %s for %s", resp.status_code, url)
            return []

        csv_text = self._open_gkg(resp.content, url)
        if not csv_text:
            return []

        return self._parse_gkg_csv(
            csv_text=csv_text,
            ticker=ticker,
            run_id=run_id,
            scrape_ts=scrape_ts,
            source_url=url,
        )

    def _parse_gkg_csv(
        self,
        *,
        csv_text: str,
        ticker: str,
        run_id: str,
        scrape_ts: str,
        source_url: str,
    ) -> list:
        """Parse GKG V2.1 tab-delimited CSV; return matching RawGdeltMention rows."""
        from src.common.schemas import RawGdeltMention

        ticker_upper = ticker.upper()
        aliases = self._alias_table.get(ticker_upper, [])
        aliases_lower = [a.lower() for a in aliases]

        rows = []
        reader = csv.reader(io.StringIO(csv_text), delimiter="\t")
        for cols in reader:
            if len(cols) < _MIN_COLS:
                continue

            gkg_id = cols[_COL_GKGID].strip()
            date_str = cols[_COL_DATE].strip()
            doc_url = cols[_COL_URL].strip() if len(cols) > _COL_URL else ""
            orgs = cols[_COL_V1ORGANIZATIONS].strip()
            social = (
                cols[_COL_SOCIALEMBED].strip()
                if len(cols) > _COL_SOCIALEMBED else ""
            )

            # Parse timestamp (YYYYMMDDHHMMSS …)
            try:
                ts_dt = datetime.strptime(date_str[:14], "%Y%m%d%H%M%S")
                ts_iso = ts_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, TypeError):
                continue

            if not gkg_id:
                continue

            match_method: Optional[str] = None

            # 1. Cashtag matching in social embed
            cashtags = set(_CASHTAG_RE.findall(social.upper()))
            if ticker_upper in cashtags:
                match_method = "cashtag"

            # 2. Alias matching in V1ORGANIZATIONS (if no cashtag match)
            if match_method is None and aliases_lower:
                orgs_lower = orgs.lower()
                for alias_lower in aliases_lower:
                    if alias_lower in orgs_lower:
                        match_method = "alias"
                        break

            if match_method is None:
                continue

            try:
                row = RawGdeltMention(
                    run_id=run_id,
                    gkg_record_id=gkg_id,
                    ticker=ticker_upper,
                    mention_timestamp=ts_iso,
                    match_method=match_method,
                    source_url=doc_url or None,
                    gkg_tone_json=None,
                    scrape_timestamp=scrape_ts,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "gdelt: row build failed gkg_id=%s: %s", gkg_id, exc,
                )
                continue

            rows.append(row)
        return rows

    # BaseDataSource stubs.

    def fetch_universe(self, run_id: str):
        raise NotImplementedError(
            f"{type(self).__name__}: use fetch_news_volume instead"
        )

    def fetch_fundamentals_for_ticker(self, ticker: str, run_id: str):
        raise NotImplementedError(
            f"{type(self).__name__}: use fetch_news_volume instead"
        )
