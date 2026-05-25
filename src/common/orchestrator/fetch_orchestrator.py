"""Rate-Limited Fetch Orchestrator core (Phase A.3.7.5).

Single tick == one ``RateLimitedFetchOrchestrator.tick(budget_seconds)`` call.
Within a tick the orchestrator:

  1. Reclaims dispatched rows older than 5 minutes (crash safety).
  2. Dequeues batches of highest-priority pending fetches via the atomic
     ``BEGIN IMMEDIATE`` helper on ``DatabaseManager``.
  3. For each claimed entry: checks the source's sliding-window quota
     headroom; if exhausted, requeues the entry and skips further entries
     for that source for the rest of the tick.
  4. Records quota usage BEFORE the HTTP call (conservative - a crash
     consumes the slot rather than risking an over-run).
  5. Dispatches the fetch via a (source, field) -> ``BaseDataSource``
     method mapping. Source classes do not expose a uniform
     ``fetch_one(field, ticker)`` so the orchestrator routes by ``field``:

         field == 'fundamentals'                   -> fetch_fundamentals_for_ticker
         field in {'filings','form4','form_8k'}    -> fetch_filings_for_ticker
         field == 'macro_series' or 'macro:<id>'   -> fetch_macro_series
         field == 'news_volume'                    -> fetch_news_volume  (A.3.8 GDELT)
         field == 'fears'                          -> fetch_search_interest  (A.3.8 pytrends)
         field == 'lm_tone'                        -> fetch_filing_text_for_ticker  (A.3.9 EDGAR)
         <anything else>                           -> fetch_universe

     ``NotImplementedError`` from a source is treated as a recoverable
     failure ('source does not provide this field'), NOT a crash.
  6. Logs the call into ``provider_call_log`` with status / bytes /
     error_message / http_status, then marks the queue row done|error.
  7. Exits when the queue is empty, the budget is exhausted, or every
     remaining entry's source is quota-saturated.

Public surface: ``RateLimitedFetchOrchestrator`` only. The companion
config loader lives in ``config.py`` and the quota / priority math live
in ``quota_window.py`` and ``queue_priority.py``.

Why no in-tick retry: quota saturation IS the retry policy. A failed
fetch becomes status='error' with last_error set; the upstream caller
re-enqueues with whatever back-off makes sense for the failure mode.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from src.common.orchestrator.config import load_orchestrator_config
from src.common.orchestrator.quota_window import (
    has_headroom,
    record_quota_use,
)


# Per-tick dequeue batch size. Large enough to amortise the BEGIN IMMEDIATE
# lock cost; small enough that requeue-on-quota-exhaustion doesn't churn
# the queue. 10 is a v1 default - tune via A.3.10 telemetry.
_DEFAULT_BATCH = 10

# Reclaim threshold for orphaned dispatched rows.
_DEFAULT_ORPHAN_STALE_SECONDS = 300


def _utcnow() -> _dt.datetime:
    return _dt.datetime.utcnow()


def _estimate_bytes(payload: Any) -> int:
    """Best-effort response-body size for the call log.

    Pandas DataFrames report their pandas-internal byte usage; everything
    else is JSON-serialised. Falls back to 0 on any error - we never
    raise from telemetry.
    """
    if payload is None:
        return 0
    try:
        import pandas as pd  # local import: pandas is heavy
        if isinstance(payload, pd.DataFrame):
            return int(payload.memory_usage(deep=True).sum())
    except Exception:
        pass
    try:
        return len(json.dumps(payload, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        try:
            return sys.getsizeof(payload)
        except Exception:
            return 0


def _route_field_to_call(
    src_obj, field: str, ticker: Optional[str], run_id: str,
) -> Any:
    """Dispatch ``field`` to the appropriate ``BaseDataSource`` method.

    See module docstring for the routing table. Raises whatever the
    underlying source raises (including ``NotImplementedError``); the
    caller catches and logs.
    """
    if field == "fundamentals":
        return src_obj.fetch_fundamentals_for_ticker(ticker, run_id)
    if field in ("filings", "form4", "form_8k"):
        return src_obj.fetch_filings_for_ticker(ticker, run_id)
    if field == "macro_series" or field.startswith("macro:"):
        series_ids = [field.split(":", 1)[1]] if field.startswith("macro:") else []
        return src_obj.fetch_macro_series(series_ids, run_id)
    # A.3.8: GDELT news-volume and pytrends FEARS signal routing.
    if field == "news_volume":
        return src_obj.fetch_news_volume(ticker, run_id)
    if field == "fears":
        return src_obj.fetch_search_interest(run_id)
    # A.3.9: Loughran-McDonald tone on EDGAR 10-K / 10-Q Item 1A.
    if field == "lm_tone":
        return src_obj.fetch_filing_text_for_ticker(ticker, run_id)
    return src_obj.fetch_universe(run_id)


class RateLimitedFetchOrchestrator:
    """Single-process tick-driven orchestrator.

    Parameters
    ----------
    db
        ``DatabaseManager`` instance (must already be at schema v10+).
    registry
        ``DataSourceRegistry`` instance whose ``enabled_sources()`` returns
        the live ``BaseDataSource`` objects to dispatch to.
    config_path
        Path to ``config/orchestrator.yaml``. Loaded once at construction
        and held in memory; re-instantiate to reload.
    clock
        Optional callable returning a ``datetime.datetime`` (UTC, naive).
        Defaults to ``datetime.utcnow``. Tests inject a controllable clock
        so they can wind time forward without sleeping.
    """

    def __init__(
        self,
        db,
        registry,
        config_path: Path | str,
        clock: Optional[Callable[[], _dt.datetime]] = None,
    ):
        self.db = db
        self.registry = registry
        self.config = load_orchestrator_config(config_path)
        self._clock: Callable[[], _dt.datetime] = clock or _utcnow

    # ---------------------------------------------------------------- API

    def tick(
        self,
        budget_seconds: int = 60,
        batch_size: int = _DEFAULT_BATCH,
    ) -> dict:
        """Execute one orchestrator tick.

        Returns a stats dict with keys:
          * attempted, succeeded, failed - call counts
          * skipped_quota - entries requeued because a source was saturated
          * orphans_reclaimed - rows reset to pending by the sweep
          * exited_at_deadline - True iff the time budget was the exit cause

        Never raises - all per-entry errors become row-level status='error'
        and the tick continues.
        """
        deadline = self._clock() + _dt.timedelta(seconds=budget_seconds)

        orphans = self.db.reclaim_orphaned_dispatched(
            stale_seconds=_DEFAULT_ORPHAN_STALE_SECONDS
        )

        stats = {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped_quota": 0,
            "orphans_reclaimed": int(orphans),
            "exited_at_deadline": False,
        }

        sources_exhausted: set[str] = set()
        provider_quotas = {
            name: cfg.model_dump() for name, cfg in self.config.providers.items()
        }
        spm = dict(self.config.source_providers)

        while True:
            if self._clock() >= deadline:
                stats["exited_at_deadline"] = True
                break

            entries = self.db.dequeue_next(limit=batch_size)
            if not entries:
                break

            dispatched_this_batch = 0
            for entry in entries:
                if self._clock() >= deadline:
                    self._requeue(entry)
                    stats["exited_at_deadline"] = True
                    continue

                source = entry["source"]
                if source in sources_exhausted:
                    self._requeue(entry)
                    stats["skipped_quota"] += 1
                    continue

                now = self._clock()
                if not has_headroom(
                    self.db, provider_quotas, source, spm,
                    calls_needed=1, now=now,
                ):
                    sources_exhausted.add(source)
                    self._requeue(entry)
                    stats["skipped_quota"] += 1
                    continue

                # Record quota use for every provider this source consumes,
                # BEFORE the call. Conservative: over-counts on crash,
                # never under-counts.
                for prov in spm.get(source, []):
                    record_quota_use(self.db, provider=prov, calls=1, at=now)

                self._execute_one(entry, stats)
                dispatched_this_batch += 1

            # Stall guard: if a full batch came back with zero dispatches,
            # the queue head is entirely quota-saturated sources. Re-dequeue
            # would just claim the same rows we just requeued. Break instead
            # of looping (would otherwise hang under a frozen test clock or
            # spin-wait until the real deadline elapses).
            if dispatched_this_batch == 0:
                break

        return stats

    # ----------------------------------------------------------- internals

    def _execute_one(self, entry: dict, stats: dict) -> None:
        source = entry["source"]
        ticker = entry["ticker"]
        field = entry["field"]
        queue_id = entry["queue_id"]

        src_obj = self._get_source(source)

        started = self._clock()
        success = False
        err: Optional[str] = None
        bytes_returned = 0
        http_status: Optional[int] = None

        if src_obj is None:
            err = f"unknown source '{source}' (not in registry)"
        else:
            run_id = f"orch-{started.isoformat()}"
            try:
                payload = _route_field_to_call(src_obj, field, ticker, run_id)
                bytes_returned = _estimate_bytes(payload)
                http_status = 200
                success = True
            except NotImplementedError as e:
                err = f"not_implemented: {e}"
            except Exception as e:  # noqa: BLE001 - per-entry isolation is the point
                err = f"{type(e).__name__}: {e}"

        finished = self._clock()
        stats["attempted"] += 1
        if success:
            stats["succeeded"] += 1
        else:
            stats["failed"] += 1

        from src.common.schemas import ProviderCallLog
        try:
            self.db.insert_provider_call_log(ProviderCallLog(
                source=source,
                ticker=ticker,
                field=field,
                started_at=started.isoformat(),
                finished_at=finished.isoformat(),
                status="ok" if success else "failed",
                bytes_returned=int(bytes_returned),
                error_message=err,
                http_status=http_status,
            ))
        except Exception:
            # Telemetry insertion must never break the tick. If the audit
            # row can't land we still mark the queue terminal so we don't
            # loop the same entry.
            pass

        self.db.mark_fetch_done(queue_id, success=success, error=err)

    def _get_source(self, name: str):
        """Linear scan of enabled sources by name. Sources are few - O(n)
        scan is cheaper than maintaining a parallel dict."""
        try:
            for s in self.registry.enabled_sources():
                if getattr(s, "name", None) == name:
                    return s
        except Exception:
            return None
        return None

    def _requeue(self, entry: dict) -> None:
        """Reset a claimed (status='dispatched') row back to pending.

        Used when the tick exits before dispatching (deadline reached) or
        when a source's quota is saturated mid-tick. Clears dispatched_at
        so the orphan-reclaim sweep doesn't catch it on the next tick.
        """
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE fetch_queue SET status='pending', dispatched_at=NULL "
                "WHERE queue_id=?",
                (entry["queue_id"],),
            )
            conn.commit()


__all__ = ["RateLimitedFetchOrchestrator"]
