"""PytrendsSource — Google Trends FEARS basket fetcher.

A.3.8 deliverable: `fetch_search_interest` pulls the Da-Engelberg-Gao 2015
FEARS basket via the `pytrends` library and persists to `raw_pytrends`.

Throttle note
-------------
Google Trends 429s aggressively. We sleep `_INTER_TERM_DELAY_SEC` between
individual-term requests (5/min ceiling matches the orchestrator quota in
config/orchestrator.yaml). Each basket term is pulled in a separate
`TrendReq.build_payload` + `interest_over_time()` call so the sleep is
interposed between calls, not after the whole basket.

Watermark
---------
`(pytrends, *, fears_basket)` uses the `*` ticker sentinel (universe-level
signal; Da-Engelberg-Gao uses a single scalar for the whole market). Stale
threshold is 7 days — weekly granularity is the finest pytrends exposes.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_log = logging.getLogger(__name__)

# Late import so tests can patch src.common.datasources.pytrends_source.TrendReq
try:
    from pytrends.request import TrendReq  # type: ignore[import]
except ImportError:
    TrendReq = None  # type: ignore[assignment,misc]


class PytrendsSource(BaseDataSource):
    name = "pytrends"
    cadence = "weekly"
    provides = {"fears_basket"}

    # Da-Engelberg-Gao 2015 §III basket.  Override at construction
    # for sensitivity testing post-A.3.10.
    DEFAULT_FEARS_BASKET: tuple[str, ...] = (
        "recession", "bankruptcy", "unemployment",
    )

    # Google Trends 429s readily.  1 call per 12s = 5/min sliding window
    # matches the orchestrator quota in config/orchestrator.yaml.
    _INTER_TERM_DELAY_SEC: float = 12.0

    _WATERMARK_TICKER: str = "*"   # Universe-level; no per-ticker split
    _WATERMARK_FIELD: str = "fears_basket"
    _WATERMARK_STALE_DAYS: int = 7

    # ------------------------------------------------------------------
    # health_check — import-only probe, no live network request.
    # ------------------------------------------------------------------

    def health_check(self) -> SourceHealthStatus:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if TrendReq is None:
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message="pytrends not installed",
            )
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK,
            checked_at=now,
            message="pytrends importable",
        )

    # ------------------------------------------------------------------
    # A.3.8 fetch method
    # ------------------------------------------------------------------

    def fetch_search_interest(
        self,
        run_id: str,
        db=None,
        *,
        terms: Optional[tuple[str, ...]] = None,
        geo: str = "US",
        timeframe: str = "today 12-m",
    ) -> int:
        """Pull Google Trends SVI for the FEARS basket; persist to raw_pytrends.

        Parameters
        ----------
        run_id
            Provenance tag stamped on every emitted row.
        db
            DatabaseManager (required).
        terms
            Basket override; default DEFAULT_FEARS_BASKET.
        geo
            ISO country code; default 'US' (Da-Engelberg-Gao §III).
        timeframe
            pytrends timeframe string; 'today 12-m' gives weekly
            granularity over the last year, matching the
            baseline_window_days=365 default in fears_signal.py.

        Returns
        -------
        int
            Count of RawPytrendsObservation rows inserted.  Returns 0 on
            7-day watermark short-circuit or on total failure.
        """
        if db is None:
            raise ValueError("db is required")

        basket = tuple(t.lower() for t in (terms or self.DEFAULT_FEARS_BASKET))
        ticker = self._WATERMARK_TICKER
        field = self._WATERMARK_FIELD

        # 7-day watermark short-circuit.
        w = db.get_watermark(self.name, ticker, field)
        if w and w.get("last_observation_date") and w.get("error_count", 0) == 0:
            try:
                last = _dt.date.fromisoformat(str(w["last_observation_date"]))
                if (_dt.date.today() - last).days < self._WATERMARK_STALE_DAYS:
                    return 0
            except (TypeError, ValueError):
                pass

        if TrendReq is None:
            _log.warning("pytrends not installed; skipping fetch")
            self.update_watermark(
                ticker=ticker, field=field,
                last_observation_date=_dt.date.today(),
                db=db, success=False,
                error_message="pytrends not installed",
            )
            return 0

        scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        total_inserted = 0
        max_date: Optional[str] = None

        from src.common.schemas import RawPytrendsObservation  # local to avoid circular

        for i, term in enumerate(basket):
            if i > 0:
                time.sleep(self._INTER_TERM_DELAY_SEC)

            try:
                pt = TrendReq(hl="en-US", tz=360)
                pt.build_payload(
                    kw_list=[term],
                    cat=0,
                    timeframe=timeframe,
                    geo=geo,
                )
                df = pt.interest_over_time()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "pytrends fetch failed term=%s: %s (%s)",
                    term, exc, type(exc).__name__,
                )
                self.update_watermark(
                    ticker=ticker, field=field,
                    last_observation_date=_dt.date.today(),
                    db=db, success=False,
                    error_message=f"{type(exc).__name__}: {exc}",
                )
                return total_inserted

            if df is None or (hasattr(df, "empty") and df.empty):
                _log.info("pytrends: empty response for term=%s", term)
                continue

            # Drop the 'isPartial' flag column if present.
            if hasattr(df, "columns") and "isPartial" in df.columns:
                df = df.drop(columns=["isPartial"])

            rows = []
            for date_idx, row_data in df.iterrows():
                obs_date = (
                    date_idx.strftime("%Y-%m-%d")
                    if hasattr(date_idx, "strftime")
                    else str(date_idx)[:10]
                )
                # Prefer column named after the term; fall back to first column.
                if hasattr(df, "columns") and term in df.columns:
                    raw_val = row_data[term]
                    svi_val = float(
                        raw_val.item() if hasattr(raw_val, "item") else raw_val
                    )
                elif hasattr(row_data, "iloc"):
                    svi_val = float(row_data.iloc[0])
                else:
                    svi_val = float(row_data)

                try:
                    obs = RawPytrendsObservation(
                        run_id=run_id,
                        term=term,
                        observation_date=obs_date,
                        svi=svi_val,
                        geo=geo,
                        source_filename=f"pytrends:{timeframe}:{geo}",
                        scrape_timestamp=scrape_ts,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "pytrends row build failed term=%s date=%s: %s",
                        term, obs_date, exc,
                    )
                    continue

                rows.append(obs)
                if max_date is None or obs_date > max_date:
                    max_date = obs_date

            if rows:
                db.insert_raw_pytrends(rows)
                total_inserted += len(rows)

        if total_inserted > 0:
            # Stamp TODAY as the fetch date (not the max data point date).
            # pytrends weekly data is always a few days old; using the data
            # date would make the 7-day stale window fire on every call.
            self.update_watermark(
                ticker=ticker, field=field,
                last_observation_date=_dt.date.today(),
                db=db, success=True,
            )

        return total_inserted

    # BaseDataSource stubs — pytrends has no per-ticker or universe concepts.

    def fetch_universe(self, run_id: str):
        raise NotImplementedError(
            f"{type(self).__name__}: use fetch_search_interest instead"
        )

    def fetch_fundamentals_for_ticker(self, ticker: str, run_id: str):
        raise NotImplementedError(
            f"{type(self).__name__}: use fetch_search_interest instead"
        )
