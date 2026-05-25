"""Pure-function priority scoring for the orchestrator dispatch queue
(Phase A.3.7.5).

Composite score:

    score = staleness_hours * weight(source, field) + caller_priority

Higher score dispatches first. ``caller_priority`` is the float stored in
``FetchQueueEntry.priority_score`` (caller-controllable; default 0.0).

Design choices:
  * ``weight_for`` falls back to **1.0** for unknown (source, field) -
    NOT 0. Silently dropping unknown work to zero weight would let stale
    FRED macro fetches starve unknown-but-aged fetches.
  * ``staleness_hours(None, now)`` returns ``+inf`` so never-fetched
    entries dispatch first.
  * ``prioritize_queue`` uses Python's stable ``sorted`` so equal-score
    entries preserve input order (callers pre-sort to choose tiebreaker).
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Iterable, Optional


def weight_for(weights_cfg: dict, source: str, field: str) -> float:
    """Return the weight for ``(source, field)``. Unknown -> 1.0."""
    src = weights_cfg.get(source)
    if not isinstance(src, dict):
        return 1.0
    val = src.get(field)
    if val is None:
        return 1.0
    return float(val)


def staleness_hours(
    last_observation_at: Optional[_dt.datetime],
    now: _dt.datetime,
) -> float:
    """Return ``(now - last_observation_at).total_seconds() / 3600``.

    Returns ``+inf`` if ``last_observation_at`` is None (never fetched =
    maximum priority). Returns negative values if ``last_observation_at``
    is in the future relative to ``now`` (a misconfigured watermark; the
    fetch is "fresher than now", so it's correctly de-prioritized).
    """
    if last_observation_at is None:
        return math.inf
    delta = now - last_observation_at
    return delta.total_seconds() / 3600.0


def compute_priority_score(
    staleness_hours: float,
    weight: float,
    caller_priority: float = 0.0,
) -> float:
    """``staleness_hours * weight + caller_priority``.

    Note: ``inf * 0`` is NaN in IEEE-754, so callers should avoid
    weight=0 anywhere (``weight_for`` falls back to 1.0).
    """
    return staleness_hours * weight + caller_priority


def prioritize_queue(
    items: Iterable,
    weights_table: dict,
) -> list:
    """Sort ``items`` (iterable of ``(entry, staleness_hours)`` tuples)
    by descending composite score.

    Each ``entry`` must expose ``.source``, ``.field``, and
    ``.priority_score`` (matching ``FetchQueueEntry`` from
    ``src.common.schemas``). Stable sort - input order preserved on ties.
    """
    items_list = list(items)
    return sorted(
        items_list,
        key=lambda pair: -compute_priority_score(
            staleness_hours=pair[1],
            weight=weight_for(weights_table, pair[0].source, pair[0].field),
            caller_priority=float(pair[0].priority_score),
        ),
    )
