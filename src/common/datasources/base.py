"""BaseDataSource ABC and supporting types.

Per spec §6 (BaseDataSource contract): each data source declares its name,
cadence, and the canonical fields it provides. It implements health_check()
mandatorily and the four fetch methods optionally — the ones it can't
satisfy raise NotImplementedError, which the registry treats as the source
being silent for that data category.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pandas as pd


class SourceStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class SourceHealthStatus:
    """Result of a source's health_check() call."""
    source: str
    status: SourceStatus
    checked_at: str  # ISO 8601 UTC
    message: str = ""
    response_time_ms: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        """True if status is OK or PARTIAL (i.e., usable). False if FAILED."""
        return self.status in (SourceStatus.OK, SourceStatus.PARTIAL)


class BaseDataSource(ABC):
    """Abstract base for every data source in src/common/datasources/.

    Subclasses MUST set:
      - name: str (short identifier, e.g. "finviz")
      - cadence: str ("weekly" | "biweekly" | "monthly")
      - provides: set[str] (canonical field names this source can populate)

    Subclasses MUST implement health_check().
    Subclasses MAY override any of the fetch_* methods; the defaults
    raise NotImplementedError to signal the source doesn't provide that
    data category.
    """

    name: str = ""
    cadence: str = ""
    provides: set[str] = set()

    @abstractmethod
    def health_check(self) -> SourceHealthStatus:
        """Probe the upstream service and report status.

        Implementations should make a cheap, idempotent call (e.g., HEAD,
        or fetch a known-tiny endpoint). Must not raise — convert exceptions
        into SourceStatus.FAILED.
        """
        raise NotImplementedError

    def fetch_universe(self, run_id: str) -> pd.DataFrame:
        """Return a wide DataFrame: one row per ticker, source-native columns."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide fetch_universe()"
        )

    def fetch_fundamentals_for_ticker(
        self, ticker: str, run_id: str
    ) -> dict[str, Any]:
        """Return a dict of canonical-field-name -> value for one ticker."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide fetch_fundamentals_for_ticker()"
        )

    def fetch_filings_for_ticker(
        self, ticker: str, run_id: str
    ) -> list[dict[str, Any]]:
        """Return a list of filing records (10-K/10-Q, Form 4, etc.)."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide fetch_filings_for_ticker()"
        )

    def fetch_macro_series(
        self, series_ids: list[str], run_id: str
    ) -> pd.DataFrame:
        """Return a long-format DataFrame: series_id, observation_date, value."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide fetch_macro_series()"
        )

    # ------------------------------------------------------------------
    # Phase A.3.1: watermark helpers (Principle 6 — persistent accumulation)
    # ------------------------------------------------------------------

    def get_fetch_gap(
        self,
        ticker: str,
        field: str,
        db,
        max_lookback_days: int = 365 * 5,
    ) -> tuple:
        """Return (start_date, end_date) of data still needed.

        If a watermark exists, start_date = last_observation_date + 1 day.
        If no watermark, start_date = today - max_lookback_days.
        end_date is always today.

        If start > end the caller should treat as "nothing to fetch".
        """
        import datetime
        today = datetime.date.today()
        w = db.get_watermark(self.name, ticker, field)
        if w is None or w.get("last_observation_date") is None:
            return (today - datetime.timedelta(days=max_lookback_days), today)
        last_obs = datetime.date.fromisoformat(w["last_observation_date"])
        return (last_obs + datetime.timedelta(days=1), today)

    def update_watermark(
        self,
        ticker: str,
        field: str,
        last_observation_date,
        db,
        success: bool = True,
        error_message: str | None = None,
    ) -> None:
        """Convenience wrapper over DatabaseManager.upsert_watermark.

        Translates a datetime.date into ISO string for storage.
        """
        if last_observation_date is None:
            obs_iso = None
        else:
            obs_iso = (
                last_observation_date.isoformat()
                if hasattr(last_observation_date, "isoformat")
                else str(last_observation_date)
            )
        db.upsert_watermark(
            source=self.name,
            ticker=ticker,
            field=field,
            last_observation_date=obs_iso,
            success=success,
            error_message=error_message,
        )
