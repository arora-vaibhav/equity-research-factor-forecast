"""Scaffold for GDELT global events database — Layer 2."""
from __future__ import annotations

from datetime import datetime, timezone

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)

_LAYER2_MSG = "Activated in Layer 2 — see future Layer 2 design doc"


class GdeltSource(BaseDataSource):
    name = "gdelt"
    cadence = "daily"
    provides = {"global_events_raw", "event_sentiment_raw"}

    def health_check(self) -> SourceHealthStatus:
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.PARTIAL,
            checked_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            message=f"scaffold stub — {_LAYER2_MSG}",
        )

    def fetch_universe(self, run_id: str):
        raise NotImplementedError(f"{type(self).__name__}: {_LAYER2_MSG}")

    def fetch_fundamentals_for_ticker(self, ticker: str, run_id: str):
        raise NotImplementedError(f"{type(self).__name__}: {_LAYER2_MSG}")

    def fetch_filings_for_ticker(self, ticker: str, run_id: str):
        raise NotImplementedError(f"{type(self).__name__}: {_LAYER2_MSG}")

    def fetch_macro_series(self, series_ids, run_id: str):
        raise NotImplementedError(f"{type(self).__name__}: {_LAYER2_MSG}")
