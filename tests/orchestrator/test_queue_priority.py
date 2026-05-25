"""Tests for src.common.orchestrator.queue_priority - pure priority math.

Composite score: ``staleness_hours * weight(source, field) + caller_priority``.

Weight fallback for unknown (source, field) is **1.0**, NOT 0 - silently
deprioritizing unknown work would let stale FRED macro fetches starve
high-value unknown-but-staleness-aged fetches.
"""
from __future__ import annotations

import datetime as _dt
import math

from src.common.orchestrator.queue_priority import (
    compute_priority_score,
    prioritize_queue,
    staleness_hours,
    weight_for,
)


WEIGHTS_SPEC = {
    "edgar": {"form4": 100, "form_8k": 80, "fundamentals": 40},
    "openbb": {"fundamentals": 30},
    "yahoo": {"prices_daily": 25},
    "finviz": {"snapshot": 20},
    "stockanalysis": {"ratios_10y": 15},
    "finra": {"short_volume": 10, "margin_debt": 10},
    "fred": {"macro_series": 5},
}


class TestWeightFor:
    def test_known_weight(self) -> None:
        assert weight_for(WEIGHTS_SPEC, "edgar", "form4") == 100.0
        assert weight_for(WEIGHTS_SPEC, "fred", "macro_series") == 5.0

    def test_unknown_source_falls_back_to_one(self) -> None:
        assert weight_for(WEIGHTS_SPEC, "unknownsource", "anything") == 1.0

    def test_unknown_field_in_known_source_falls_back_to_one(self) -> None:
        # edgar is in weights but 'random_field' isn't.
        assert weight_for(WEIGHTS_SPEC, "edgar", "random_field") == 1.0

    def test_empty_weights_table(self) -> None:
        assert weight_for({}, "edgar", "form4") == 1.0


class TestStalenessHours:
    def test_never_fetched_is_infinity(self) -> None:
        now = _dt.datetime(2026, 5, 21, 12, 0, 0)
        assert staleness_hours(None, now) == math.inf

    def test_one_hour_ago(self) -> None:
        now = _dt.datetime(2026, 5, 21, 12, 0, 0)
        one_hour_ago = _dt.datetime(2026, 5, 21, 11, 0, 0)
        assert staleness_hours(one_hour_ago, now) == 1.0

    def test_fractional_hours(self) -> None:
        now = _dt.datetime(2026, 5, 21, 12, 30, 0)
        last = _dt.datetime(2026, 5, 21, 12, 0, 0)
        assert staleness_hours(last, now) == 0.5

    def test_negative_when_last_is_future(self) -> None:
        """A misconfigured watermark in the future yields negative staleness
        (low priority, which is the right semantic - it just got fetched)."""
        now = _dt.datetime(2026, 5, 21, 12, 0, 0)
        future = _dt.datetime(2026, 5, 21, 13, 0, 0)
        assert staleness_hours(future, now) == -1.0


class TestComputePriorityScore:
    def test_basic(self) -> None:
        # 10h stale * weight 100 + caller 0 = 1000
        s = compute_priority_score(
            staleness_hours=10.0, weight=100.0, caller_priority=0.0,
        )
        assert s == 1000.0

    def test_caller_priority_added(self) -> None:
        s = compute_priority_score(
            staleness_hours=10.0, weight=25.0, caller_priority=1000.0,
        )
        assert s == 1250.0

    def test_zero_staleness_zero_score_when_no_caller_priority(self) -> None:
        s = compute_priority_score(
            staleness_hours=0.0, weight=100.0, caller_priority=0.0,
        )
        assert s == 0.0

    def test_caller_priority_alone(self) -> None:
        s = compute_priority_score(
            staleness_hours=0.0, weight=100.0, caller_priority=500.0,
        )
        assert s == 500.0

    def test_negative_caller_priority_demotes(self) -> None:
        s = compute_priority_score(
            staleness_hours=1.0, weight=25.0, caller_priority=-100.0,
        )
        assert s == -75.0

    def test_infinite_staleness_returns_infinity(self) -> None:
        s = compute_priority_score(
            staleness_hours=math.inf, weight=10.0, caller_priority=0.0,
        )
        assert s == math.inf


class TestPrioritizeQueue:
    """``prioritize_queue`` sorts (entry, staleness_hours) tuples by
    DESCENDING composite score. Stable for ties."""

    def _entry(self, source: str, field: str, ticker: str | None = "AAPL",
               caller_priority: float = 0.0, queue_id: int | None = None):
        from types import SimpleNamespace
        return SimpleNamespace(
            queue_id=queue_id, source=source, field=field, ticker=ticker,
            priority_score=caller_priority,
        )

    def test_insider_beats_macro_at_equal_staleness(self) -> None:
        items = [
            (self._entry("fred", "macro_series", ticker=None), 24.0),
            (self._entry("edgar", "form4"), 24.0),
        ]
        ordered = prioritize_queue(items, weights_table=WEIGHTS_SPEC)
        assert ordered[0][0].source == "edgar"
        assert ordered[1][0].source == "fred"

    def test_staleness_breaks_ties_within_same_weight(self) -> None:
        items = [
            (self._entry("yahoo", "prices_daily", ticker="AAPL"), 5.0),
            (self._entry("yahoo", "prices_daily", ticker="MSFT"), 50.0),
        ]
        ordered = prioritize_queue(items, weights_table=WEIGHTS_SPEC)
        assert ordered[0][0].ticker == "MSFT"

    def test_caller_priority_overrides(self) -> None:
        """A FRED macro fetch with manual priority=10000 leapfrogs an
        otherwise higher-weighted edgar form4."""
        items = [
            (self._entry("edgar", "form4", caller_priority=0), 24.0),
            (self._entry("fred", "macro_series", ticker=None,
                         caller_priority=10_000), 1.0),
        ]
        ordered = prioritize_queue(items, weights_table=WEIGHTS_SPEC)
        assert ordered[0][0].source == "fred"

    def test_empty(self) -> None:
        assert prioritize_queue([], weights_table=WEIGHTS_SPEC) == []

    def test_stable_sort_preserves_order_on_tie(self) -> None:
        a = self._entry("yahoo", "prices_daily", ticker="AAPL", queue_id=1)
        b = self._entry("yahoo", "prices_daily", ticker="MSFT", queue_id=2)
        items = [(a, 10.0), (b, 10.0)]
        ordered = prioritize_queue(items, weights_table=WEIGHTS_SPEC)
        assert ordered[0][0].queue_id == 1
        assert ordered[1][0].queue_id == 2

    def test_never_fetched_pushed_to_front(self) -> None:
        """staleness_hours=inf for never-fetched should make those win
        against any finite-staleness entry of the same weight."""
        items = [
            (self._entry("yahoo", "prices_daily", ticker="OLD"), 100.0),
            (self._entry("yahoo", "prices_daily", ticker="NEW"), math.inf),
        ]
        ordered = prioritize_queue(items, weights_table=WEIGHTS_SPEC)
        assert ordered[0][0].ticker == "NEW"

    def test_unknown_source_field_uses_weight_one(self) -> None:
        """Two equally-stale entries; one in weights table (weight=100),
        the other unknown (falls back to weight=1). Known entry wins
        but unknown still gets a positive score (not silently zeroed)."""
        items = [
            (self._entry("unknownsrc", "unknownfield"), 10.0),
            (self._entry("edgar", "form4"), 10.0),
        ]
        ordered = prioritize_queue(items, weights_table=WEIGHTS_SPEC)
        assert ordered[0][0].source == "edgar"
        assert ordered[1][0].source == "unknownsrc"
