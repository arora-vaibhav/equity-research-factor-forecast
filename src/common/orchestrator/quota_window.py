"""Sliding-window provider quota math (Phase A.3.7.5).

Semantics (load-bearing):
  * Quota usage is bucketed at one-second granularity. Two calls in the
    same wall-clock second collapse into a single ``provider_quota_state``
    row with calls_used=2.
  * "Calls used in the last W seconds" is the SQL aggregate
    ``SUM(calls_used) WHERE window_start > now - W``. The lower bound is
    STRICT ``>``: a bucket exactly at ``now - W`` has just slid out of
    the window and is excluded. Token-bucket rate-limit convention.
  * The orchestrator records quota usage BEFORE the HTTP call
    (conservative). If the call crashes the quota slot is still consumed
    so we never overrun a provider's rate limit because of a failed call.

The ``provider_quota_state`` table is keyed by ``(source, provider,
window_start)`` for Wave-1 reasons (per-source attribution was a wider
design that v1 does not exercise). The orchestrator records under a
fixed sentinel source value and aggregates across all sources by
provider, so the source column is effectively ignored at read time.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Optional


# Sentinel source value used by ``record_quota_use``. Aggregation in
# ``get_calls_in_window`` ignores the source column entirely, so this is
# only a placeholder to satisfy the (source, provider, window_start)
# primary key. Future enhancements can adopt distinct source labels.
_ORCH_SOURCE_SENTINEL = "_orchestrator"


def _floor_second(dt: _dt.datetime) -> _dt.datetime:
    """Strip microseconds. Two calls in the same wall-clock second share
    a bucket so quota accounting stays consistent."""
    return dt.replace(microsecond=0)


def record_quota_use(
    db,
    provider: str,
    calls: int = 1,
    at: Optional[_dt.datetime] = None,
) -> None:
    """Increment the provider's quota usage at the second-bucket of ``at``.

    Implementation: read the current ``calls_used`` for the bucket (0 if
    absent), add ``calls``, write back via the wave-1 helper
    ``upsert_provider_quota_state`` which REPLACES on conflict. The
    read-then-write is wrapped by SQLite's per-connection serialization
    inside the orchestrator's single-writer model.
    """
    when = _floor_second(at) if at is not None else _floor_second(_dt.datetime.utcnow())
    ws = when.isoformat()
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT calls_used FROM provider_quota_state "
            "WHERE source=? AND provider=? AND window_start=?",
            (_ORCH_SOURCE_SENTINEL, provider, ws),
        ).fetchone()
        prior = int(row[0]) if row else 0
    # Lazy import to avoid a top-level cycle.
    from src.common.schemas import ProviderQuotaState
    state = ProviderQuotaState(
        source=_ORCH_SOURCE_SENTINEL,
        provider=provider,
        window_start=ws,
        calls_used=prior + calls,
    )
    db.upsert_provider_quota_state(state)


def get_calls_in_window(
    db,
    provider: str,
    window_seconds: int,
    now: Optional[_dt.datetime] = None,
) -> int:
    """Return total calls used in the half-open interval ``(now - W, now]``.

    Strict ``>`` boundary: a row at ``window_start = now - W`` has just
    slid out and is excluded. Aggregates across all source values so the
    answer is the true per-provider usage regardless of which source
    consumed it.
    """
    when = _floor_second(now) if now is not None else _floor_second(_dt.datetime.utcnow())
    cutoff_iso = (when - _dt.timedelta(seconds=window_seconds)).isoformat()
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(calls_used), 0) FROM provider_quota_state "
            "WHERE provider=? AND window_start > ?",
            (provider, cutoff_iso),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def remaining(
    db,
    provider_quotas: dict,
    source: str,
    source_providers: dict,
    now: Optional[_dt.datetime] = None,
) -> int:
    """Return remaining calls a source can make right now, taken as the
    MIN across every provider that source consumes.

    Multi-provider sources (e.g. openbb -> [fmp_stable, polygon, tiingo])
    are gated by the bottleneck provider. The orchestrator dispatches one
    atomic source call per "remaining" unit returned here.

    Unknown source returns 0 (defensive - can't dispatch what we don't
    know how to bucket).
    """
    providers = source_providers.get(source)
    if not providers:
        return 0
    per_provider: list[int] = []
    for prov in providers:
        q = provider_quotas.get(prov)
        if q is None:
            # No quota config -> treat as zero (safer than allowing
            # unbounded calls).
            per_provider.append(0)
            continue
        used = get_calls_in_window(
            db, provider=prov,
            window_seconds=int(q["quota_window_seconds"]), now=now,
        )
        per_provider.append(max(0, int(q["quota_calls"]) - used))
    return min(per_provider) if per_provider else 0


def has_headroom(
    db,
    provider_quotas: dict,
    source: str,
    source_providers: dict,
    calls_needed: int = 1,
    now: Optional[_dt.datetime] = None,
) -> bool:
    """True iff ``source`` can issue ``calls_needed`` more calls without
    exceeding ANY of its providers' quotas.

    Unknown source returns False (defensive - same rationale as
    ``remaining``).
    """
    providers = source_providers.get(source)
    if not providers:
        return False
    for prov in providers:
        q = provider_quotas.get(prov)
        if q is None:
            return False
        used = get_calls_in_window(
            db, provider=prov,
            window_seconds=int(q["quota_window_seconds"]), now=now,
        )
        if used + calls_needed > int(q["quota_calls"]):
            return False
    return True


@dataclass
class QuotaWindow:
    """Convenience binding of (db, config, now) so the orchestrator's
    per-call quota checks don't re-pass the same args every time.

    Holds a snapshot of ``now`` so multiple ``remaining`` calls within a
    single tick report consistent results.
    """

    db: object
    provider_quotas: dict
    source_providers: dict
    now: _dt.datetime

    def remaining(self, source: str) -> int:
        return remaining(
            self.db, provider_quotas=self.provider_quotas, source=source,
            source_providers=self.source_providers, now=self.now,
        )

    def has_headroom(self, source: str, calls_needed: int = 1) -> bool:
        return has_headroom(
            self.db, provider_quotas=self.provider_quotas, source=source,
            source_providers=self.source_providers,
            calls_needed=calls_needed, now=self.now,
        )

    def remaining_all(self) -> dict[str, int]:
        """Diagnostic helper: remaining for every source in source_providers."""
        return {s: self.remaining(s) for s in self.source_providers}
