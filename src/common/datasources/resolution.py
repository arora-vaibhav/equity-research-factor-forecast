"""Priority-weighted resolution functions (spec §9).

Three resolution rules:
  - weighted_average_value: numeric fundamentals
  - highest_priority_value: categorical / identifiers
  - freshest_value:         snapshot fields (price, volume, etc.)

All three accept a list of Observation records (one per source for one
ticker x field) and return a ResolutionResult containing the canonical
value plus the list of contributing source names.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Observation:
    """One source's observation of one (ticker, field) pair."""
    source: str
    rank: int               # priority rank for this field's category (1 = highest)
    value: Any
    fetched_at: str | None = None  # ISO 8601 UTC; required for freshest_value


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of applying a resolution rule to a list of observations."""
    value: Any
    contributors: list[str]


def priority_weight(rank: int, decay: float = 0.5) -> float:
    """Raw priority weight for rank N: decay^N.

    With default decay=0.5: rank 1 -> 0.5, rank 2 -> 0.25, rank 3 -> 0.125.
    Caller normalizes across contributing observations.
    """
    if rank < 1:
        raise ValueError(f"rank must be >= 1, got {rank}")
    return decay ** rank


def weighted_average_value(
    observations: list[Observation],
    decay: float = 0.5,
) -> ResolutionResult:
    """Priority-weighted average of numeric observations.

    Steps:
      1. Drop observations whose value is None.
      2. Compute raw weight per surviving observation: decay^rank.
      3. Normalize weights to sum to 1.
      4. canonical_value = sum(w_i * value_i).
    """
    surviving = [o for o in observations if o.value is not None]
    if not surviving:
        return ResolutionResult(value=None, contributors=[])
    raw_weights = [priority_weight(o.rank, decay) for o in surviving]
    total = sum(raw_weights)
    if total == 0:
        return ResolutionResult(value=None, contributors=[])
    normalized = [w / total for w in raw_weights]
    canonical = sum(w * float(o.value) for w, o in zip(normalized, surviving))
    return ResolutionResult(
        value=canonical,
        contributors=[o.source for o in surviving],
    )


def highest_priority_value(observations: list[Observation]) -> ResolutionResult:
    """Take the value from the highest-priority (lowest rank) non-null source."""
    surviving = [o for o in observations if o.value is not None]
    if not surviving:
        return ResolutionResult(value=None, contributors=[])
    chosen = min(surviving, key=lambda o: o.rank)
    return ResolutionResult(value=chosen.value, contributors=[chosen.source])


def freshest_value(observations: list[Observation]) -> ResolutionResult:
    """Take the value from the source with the most recent fetched_at.

    Ties broken by priority rank (lower rank wins).
    Observations with fetched_at=None or value=None are skipped.
    """
    surviving = [
        o for o in observations if o.value is not None and o.fetched_at is not None
    ]
    if not surviving:
        return ResolutionResult(value=None, contributors=[])
    chosen = min(
        surviving,
        key=lambda o: (-_iso_to_sortable(o.fetched_at), o.rank),
    )
    return ResolutionResult(value=chosen.value, contributors=[chosen.source])


def _iso_to_sortable(iso: str) -> float:
    """Convert ISO 8601 UTC string to a numeric sortable POSIX timestamp."""
    from datetime import datetime
    s = iso.replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()
