# Phase A.3.7.5 — Rate-Limited Fetch Orchestrator (per-minute cron-friendly scheduler over the data adapter registry)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]` checkbox syntax for tracking.

**Goal:** Build a centralized scheduler (`RateLimitedFetchOrchestrator`) that wraps the existing 7-source `DataSourceRegistry` (Finviz / Yahoo / EDGAR / FRED / FINRA / Stockanalysis / OpenBB) with **per-provider quota tracking**, **staleness-prioritized work queueing**, and **per-call audit logging**. The orchestrator's single public entry point — `tick(budget_seconds: int)` — is designed to be invoked by cron / Windows Task Scheduler at any cadence (every minute is the canonical case). On each tick the orchestrator (a) slides forward each provider's quota window, (b) refills a queue from staleness in `fetch_watermarks`, (c) dispatches as many enqueued (source, ticker, field) jobs as the current quotas allow within the budget, and (d) writes per-call provenance to a new `provider_call_log` table. Schema migrates v9 → v10 to add `provider_call_log`, `provider_quota_state`, and `fetch_queue`.

**Why this matters:** The 2026-05-21 directive set the long-term vision explicitly: maintain an actionable last-call log; run every minute; compile and add data continuously, creating an updatable, expansive data aggregator and substrate for continuous in-house agents. A.3.1–A.3.7 shipped the seven adapters that each individually know how to fetch their canonical fields; what's missing is the **dispatch layer** that schedules those adapters subject to the real-world constraints of provider rate limits (Polygon: 5/min, FMP-stable: 250/day, Tiingo: 1000/day, FINRA: 100/day, FRED: 120/min, Yahoo: 2000/h, SEC EDGAR: 10/s, stockanalysis.com: 60/min). Without an orchestration layer the operator either (a) blindly hammers a provider and gets 429'd until the daily ceiling resets, or (b) over-conservatively under-fetches. A.3.7.5 is the substrate that makes a long-running "continuous in-house agent" feasible: every minute the agent calls `tick(budget_seconds=60)`, the orchestrator picks the highest-priority stale (ticker, field) pairs and fans out to as many provider calls as quotas allow, and quota usage accretes correctly across hundreds of ticks per day. The `provider_call_log` is the audit trail that makes this replayable and debuggable — the operator can answer "how many Polygon calls did we make on 2026-05-21 and which ones failed" with a single SQL query.

**Architecture:** Additive only. Migration v9 → v10 creates three new tables (`provider_call_log` append-only audit, `provider_quota_state` sliding-window counter, `fetch_queue` pending-work queue) and bumps `schema_version` to 10. A NEW `src/common/orchestrator/` package is created with:

1. `__init__.py` — re-exports `RateLimitedFetchOrchestrator`.
2. `quota_window.py` — pure-function sliding-window math (no I/O); testable in isolation.
3. `queue_priority.py` — pure-function priority scoring (no I/O); testable in isolation.
4. `fetch_orchestrator.py` — `RateLimitedFetchOrchestrator` class that composes the two helpers, owns `tick()`, and is the only module that talks to both the registry and the DB.

The orchestrator does NOT live in `src/common/datasources/` because it is structurally **above** the source layer (it dispatches sources, it isn't one). Placing it in its own `orchestrator/` package keeps the source registry's responsibilities clean — the registry exposes seven adapters; the orchestrator decides when and in what order to call them.

**Concurrency model (load-bearing design decision):** SQLite supports concurrent reads but serializes writes. The orchestrator's `tick()` runs in **WAL mode** (`PRAGMA journal_mode=WAL`) with **per-call short transactions** rather than wrapping the whole `tick()` in a single transaction. Rationale:

1. A single budget-second cron tick may take 60+ seconds of wall clock (real HTTP calls), and SQLite would hold the writer lock for that whole window if `tick()` were one transaction — blocking any other process (notebooks, dashboards, ad-hoc queries) that wants to read fresh data.
2. The "in-flight" state of a queue item is tracked by a column (`status='dispatched'` + `completed_at IS NULL`) rather than by a long-lived transaction. On next tick, any row with `status='dispatched'` AND `dispatched_at` older than 5 minutes is treated as orphaned (a previous tick crashed mid-dispatch) and re-set to `pending`. This gives us crash-safe resumability without distributed locking.
3. Single-writer guarantee comes from the **assumption of one orchestrator process at a time** — the operator runs `tick()` from one cron / Task Scheduler job, not two parallel ones. Multi-process safety would require row-level `BEGIN IMMEDIATE` locking on the queue dispatch and is explicitly **out of scope for v1** (documented under "Out of scope" below).

**Quota window math (load-bearing design decision):** Sliding window, NOT fixed calendar windows. Specifically:

- The `provider_quota_state` table stores one row per `(source, provider, window_start)` where `window_start` is the floor-to-second of the call time. `calls_used` is the count of calls bucketed at that exact `window_start`.
- "How many calls are available right now for Polygon (5/min)?" is computed as: `5 - SUM(calls_used) WHERE source='polygon' AND window_start > NOW() - 60s`. Buckets older than 60s slide out of the window.
- This is **strictly more accurate** than fixed calendar minute-windows (which would refill all 5 calls at the top of each minute and let an operator burn through 10 calls in 2 seconds by straddling a minute boundary). The sliding window is the same algorithm used in token-bucket rate limiters; we encode it as a SQL aggregate over a time predicate instead of an in-memory counter, which gives us crash-safety for free.

**Priority weighting (load-bearing design decision):** The spec's §7 ranks alpha contribution as CMP insider (~82 bps/mo) > Yahoo revisions > FINRA short interest > FRED macro. The orchestrator's `queue_priority.score(...)` reflects this with a per-(source, field) weight table that biases highly-stale CMP insider fetches above lightly-stale FRED macro fetches even when the macro fetch has a longer absolute age. The weight table is defined in `config/orchestrator.yaml` so it can be re-tuned without code changes. The composite score is `staleness_hours * weight + priority`, with caller-supplied `priority` (default 0) as the high-bit tiebreaker — an operator who manually enqueues a "refresh AAPL now" job can override the default ordering by passing `priority=1000`.

**Cron compatibility (load-bearing design decision):** `tick(budget_seconds=60)` MUST be a no-op when there's no work: zero queue items pending, zero stale watermarks, zero errors. The Windows Task Scheduler / Unix cron operator will fire this every minute regardless of whether there's work; firing a hundred no-op ticks per day must be cheap and silent. The implementation enforces this with an early-return path: `if not queue and not _has_stale_watermarks(): return {'dispatched': 0, 'deferred': 0, 'failed': 0, 'quota_remaining': {...}}`.

**Tech Stack:** Python 3.11+, `pyyaml` (already pinned for `config/datasources.yaml`), `pydantic` v2, `pytest`, `pytest-mock`. **No new runtime dependencies.** The orchestrator deliberately does NOT pull in any of the heavier task-queue libraries (Celery, RQ, Dramatiq, Huey, APScheduler) — they would each be ~10–50 MB of transitive deps for a feature surface that requires a SQLite table + a function. The trade-off is documented under Out of Scope below.

**Spec reference:** [docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md](../specs/2026-05-21-phase-a3-layer1-hardening-design.md) §15 row "A.3.7.5" + 2026-05-21 directive.

**Provider quota configuration:** All quota numbers live in `config/orchestrator.yaml` (created in Task 2), NOT hardcoded:

```yaml
provider_quotas:
  polygon:       {calls_per_window: 5,    window_seconds: 60}
  fmp_stable:    {calls_per_window: 250,  window_seconds: 86400}
  tiingo:        {calls_per_window: 1000, window_seconds: 86400}
  sec_edgar:     {calls_per_window: 10,   window_seconds: 1}
  yahoo:         {calls_per_window: 2000, window_seconds: 3600}
  finra:         {calls_per_window: 100,  window_seconds: 86400}
  fred:          {calls_per_window: 120,  window_seconds: 60}
  stockanalysis: {calls_per_window: 60,   window_seconds: 60}

priority_weights:
  edgar:         {insider: 100, fundamentals: 30, filings: 20}
  yahoo:         {revisions: 60, fundamentals: 25, historical_price: 15}
  finra:         {short_interest: 50}
  fred:          {macro: 5}
  stockanalysis: {ratios: 20}
  finviz:        {universe: 10}
  openbb:        {multi_provider: 25}

source_provider_map:
  finviz:        [finviz]
  yahoo:         [yahoo]
  edgar:         [sec_edgar]
  fred:          [fred]
  finra:         [finra]
  stockanalysis: [stockanalysis]
  openbb:        [fmp_stable, polygon, tiingo]
```

The `source_provider_map` is the bridge between the source registry's source names ("openbb") and the per-provider quota buckets ("fmp_stable", "polygon", "tiingo") — OpenBB consumes from all three buckets simultaneously per fetch. A.3.7.5's quota check for `openbb` requires that **all three** providers have at least one call available; if any one is starved, the dispatch is deferred. This is the v1 conservative semantics; a smarter version that knows which providers a given (ticker, field) actually needs is deferred to A.3.10.

**Out of scope for A.3.7.5:**

- Multi-process / distributed orchestrator. v1 assumes a single orchestrator process; running two simultaneously can double-dispatch the same queue item. Multi-process safety would require `BEGIN IMMEDIATE` row locking on `fetch_queue.dequeue_next()` and is deferred to a future sub-phase when horizontal scale is needed.
- Heavy task-queue infrastructure (Celery / RQ / APScheduler / Dramatiq). The single-table SQLite queue covers v1's needs at zero new-dependency cost. If queue volume crosses ~10k pending items the table-based approach starts to feel the heat and migrating to a real queue is a contained refactor.
- Live network in any test. All HTTP is mocked at the source level (we patch the registry's source methods, not `requests.get`). Live-integration validation lives in A.5 acceptance phase.
- Smart per-field provider selection inside OpenBB (e.g., "for `pe_ratio` I only need FMP, no need to spend Polygon quota"). v1 treats `openbb` as one atomic operation across all three providers. Deferred to A.3.10.
- Automatic backoff on consecutive 429 responses. v1's only retry mechanism is "next tick the work re-enqueues if its staleness still exceeds cadence". A burst of 429s simply consumes quota without writing rows; the next tick won't re-fire the same calls because quotas are still saturated. Smarter 429 backoff is deferred.
- A web dashboard / TUI for inspecting the queue + quotas. v1 surfaces everything through SQL queries on the three new tables; the CLI hook prints a human-readable JSON summary of the most recent `tick()`. A future dashboard sub-phase consumes the same tables.
- Cross-tick budget carryover. If a tick under-uses its budget (e.g., 60s budget but quotas saturated after 10s), the unused 50s does NOT carry to the next tick. Each tick is independent.
- Per-ticker priority overrides via UI. The current `priority` column accepts caller-supplied values, but no UI exists yet to set them.

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| `src/common/schemas.py` | Add `ProviderCallLog`, `ProviderQuotaState`, `FetchQueueEntry` Pydantic models | Modify |
| `src/common/database.py` | Add `migrate_to_v10()` + `insert_provider_call_log()` + `enqueue_fetch()` + `dequeue_next()` + `record_quota_use()` + `cleanup_stale_quota_rows()` + `get_quota_calls_in_window()` + `update_queue_status()` + `reclaim_orphaned_dispatched()` | Modify |
| `config/orchestrator.yaml` | NEW — per-provider quotas + priority weights + source→provider map | Create |
| `src/common/orchestrator/__init__.py` | NEW — re-exports `RateLimitedFetchOrchestrator` | Create |
| `src/common/orchestrator/quota_window.py` | NEW — pure-function sliding-window math | Create |
| `src/common/orchestrator/queue_priority.py` | NEW — pure-function priority scoring | Create |
| `src/common/orchestrator/fetch_orchestrator.py` | NEW — `RateLimitedFetchOrchestrator` class + CLI `__main__` entry point | Create |
| `tests/common/test_schemas_a3_7_5.py` | Tests for 3 new Pydantic models | Create |
| `tests/common/test_database_a3_7_5.py` | Tests for `migrate_to_v10` + insert/dequeue/quota helpers | Create |
| `tests/common/orchestrator/__init__.py` | NEW — empty `__init__` for test package | Create |
| `tests/common/orchestrator/test_quota_window.py` | Tests for sliding-window math | Create |
| `tests/common/orchestrator/test_queue_priority.py` | Tests for `prioritize_queue` ordering | Create |
| `tests/common/orchestrator/test_orchestrator_tick.py` | Tests for `tick()` end-to-end with mocked registry | Create |
| `tests/common/orchestrator/test_integration_a3_7_5.py` | End-to-end orchestration acceptance test | Create |
| the build plan | Mark A.3.7.5 shipped (schema v10) | Modify |

---

## Task 0: Pre-flight verification

**Files:** none modified.

- [ ] **Step 1: Verify schema is at v9 from A.3.7**

```
./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager(); print('current schema version:', m.get_schema_version())"
```

Expected: `current schema version: 9`.

- [ ] **Step 2: Verify A.3.7 baseline tests still pass**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: `475 passed, 2 deselected` (±2 tolerance).

- [ ] **Step 3: Verify `pyyaml` is importable**

```
./venv/Scripts/python.exe -c "import yaml; print('pyyaml import ok')"
```

Expected: `pyyaml import ok`. (No `__version__` check — matches A.3.3..A.3.7 pattern of avoiding `__version__` introspection.)

- [ ] **Step 4: Verify `DataSourceRegistry` instantiates cleanly**

```
./venv/Scripts/python.exe -c "from src.common.datasources.registry import DataSourceRegistry; r=DataSourceRegistry(); print('enabled sources:', [s.name for s in r.enabled_sources()])"
```

Expected: prints a list of enabled source names. The orchestrator's tests do NOT depend on which subset is enabled (they mock the registry directly).

- [ ] **Step 5: Verify the `src/common/orchestrator/` package is ABSENT**

```
./venv/Scripts/python.exe -c "import importlib, importlib.util; print('orchestrator present:', importlib.util.find_spec('src.common.orchestrator') is not None)"
```

Expected: `orchestrator present: False`.

- [ ] **Step 6: Verify `config/orchestrator.yaml` is ABSENT**

```
./venv/Scripts/python.exe -c "import pathlib; p=pathlib.Path('config/orchestrator.yaml'); print('orchestrator.yaml present:', p.exists())"
```

Expected: `orchestrator.yaml present: False`.

- [ ] **Step 7: Verify working tree is clean**

Run: `git status -s`

Expected: empty (or only unrelated `.env` / notebooks).

- [ ] **Step 8: Verify WAL-mode compatibility**

```
./venv/Scripts/python.exe -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('PRAGMA journal_mode=WAL'); print('wal toggle:', c.execute('PRAGMA journal_mode').fetchone()[0])"
```

NOTE: in-memory DBs report `memory` (WAL doesn't apply), but the toggle returns without error. On disk-backed DBs WAL toggle returns `wal`. The orchestrator's `migrate_to_v10()` will issue `PRAGMA journal_mode=WAL` on the actual DB file; this step just verifies the venv's sqlite3 supports the PRAGMA syntax.

No code commits at this task.

---

## Task 1: Three Pydantic models (`ProviderCallLog`, `ProviderQuotaState`, `FetchQueueEntry`)

**Files:**
- Modify: `src/common/schemas.py` (append)
- Create: `tests/common/test_schemas_a3_7_5.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/common/test_schemas_a3_7_5.py`:

```python
"""Tests for Phase A.3.7.5 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import (
    ProviderCallLog,
    ProviderQuotaState,
    FetchQueueEntry,
)


class TestProviderCallLog:
    def test_minimal_valid(self):
        r = ProviderCallLog(
            source="yahoo",
            ticker="AAPL",
            field="fundamentals",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:01Z",
            status="ok",
            bytes_returned=4096,
            http_status=200,
        )
        assert r.source == "yahoo"
        assert r.ticker == "AAPL"
        assert r.status == "ok"
        assert r.bytes_returned == 4096
        assert r.http_status == 200
        assert r.error_message is None

    def test_call_id_optional(self):
        r = ProviderCallLog(
            source="polygon", ticker="AAPL", field="multi_provider",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:00Z",
            status="ok", bytes_returned=0,
        )
        assert r.call_id is None

    def test_ticker_uppercased(self):
        r = ProviderCallLog(
            source="yahoo", ticker="aapl", field="fundamentals",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:01Z",
            status="ok", bytes_returned=0,
        )
        assert r.ticker == "AAPL"

    def test_ticker_none_for_macro_field(self):
        r = ProviderCallLog(
            source="fred", ticker=None, field="macro:UNRATE",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:01Z",
            status="ok", bytes_returned=512,
        )
        assert r.ticker is None

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            ProviderCallLog(
                source="yahoo", ticker="not-a-ticker!", field="fundamentals",
                started_at="2026-05-21T08:00:00Z",
                finished_at="2026-05-21T08:00:01Z",
                status="ok", bytes_returned=0,
            )

    def test_status_literal_enforced(self):
        with pytest.raises(Exception):
            ProviderCallLog(
                source="yahoo", ticker="AAPL", field="fundamentals",
                started_at="2026-05-21T08:00:00Z",
                finished_at="2026-05-21T08:00:01Z",
                status="weird-state", bytes_returned=0,
            )

    def test_error_status_carries_error_message(self):
        r = ProviderCallLog(
            source="polygon", ticker="AAPL", field="multi_provider",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:12Z",
            status="failed", bytes_returned=0,
            error_message="HTTPError 429: rate limited",
            http_status=429,
        )
        assert r.status == "failed"
        assert r.error_message == "HTTPError 429: rate limited"
        assert r.http_status == 429

    def test_bytes_returned_nonneg(self):
        with pytest.raises(Exception):
            ProviderCallLog(
                source="yahoo", ticker="AAPL", field="fundamentals",
                started_at="2026-05-21T08:00:00Z",
                finished_at="2026-05-21T08:00:01Z",
                status="ok",
                bytes_returned=-1,
            )

    def test_http_status_optional(self):
        r = ProviderCallLog(
            source="yahoo", ticker="AAPL", field="fundamentals",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:30Z",
            status="failed", bytes_returned=0,
            error_message="ConnectionTimeoutError",
            http_status=None,
        )
        assert r.http_status is None


class TestProviderQuotaState:
    def test_minimal_valid(self):
        r = ProviderQuotaState(
            source="polygon", provider="polygon",
            window_start="2026-05-21T08:00:00Z",
            calls_used=3,
        )
        assert r.source == "polygon"
        assert r.provider == "polygon"
        assert r.calls_used == 3

    def test_source_differs_from_provider(self):
        r = ProviderQuotaState(
            source="openbb", provider="fmp_stable",
            window_start="2026-05-21T08:00:00Z",
            calls_used=1,
        )
        assert r.source == "openbb"
        assert r.provider == "fmp_stable"

    def test_calls_used_nonneg(self):
        with pytest.raises(Exception):
            ProviderQuotaState(
                source="polygon", provider="polygon",
                window_start="2026-05-21T08:00:00Z",
                calls_used=-1,
            )

    def test_window_start_required(self):
        with pytest.raises(Exception):
            ProviderQuotaState(
                source="polygon", provider="polygon",
                window_start=None,  # type: ignore[arg-type]
                calls_used=0,
            )


class TestFetchQueueEntry:
    def test_minimal_valid_pending(self):
        r = FetchQueueEntry(
            source="yahoo", ticker="AAPL", field="fundamentals",
            priority=0, created_at="2026-05-21T08:00:00Z",
            status="pending",
        )
        assert r.source == "yahoo"
        assert r.status == "pending"
        assert r.priority == 0
        assert r.dispatched_at is None
        assert r.completed_at is None
        assert r.queue_id is None

    def test_all_status_values_accepted(self):
        for s in ("pending", "dispatched", "completed", "failed", "deferred"):
            r = FetchQueueEntry(
                source="yahoo", ticker="AAPL", field="fundamentals",
                priority=0, created_at="2026-05-21T08:00:00Z",
                status=s,
            )
            assert r.status == s

    def test_invalid_status_rejected(self):
        with pytest.raises(Exception):
            FetchQueueEntry(
                source="yahoo", ticker="AAPL", field="fundamentals",
                priority=0, created_at="2026-05-21T08:00:00Z",
                status="weird",
            )

    def test_ticker_uppercased(self):
        r = FetchQueueEntry(
            source="yahoo", ticker="aapl", field="fundamentals",
            priority=0, created_at="2026-05-21T08:00:00Z",
            status="pending",
        )
        assert r.ticker == "AAPL"

    def test_ticker_none_for_macro(self):
        r = FetchQueueEntry(
            source="fred", ticker=None, field="macro:UNRATE",
            priority=0, created_at="2026-05-21T08:00:00Z",
            status="pending",
        )
        assert r.ticker is None

    def test_dispatched_carries_dispatched_at(self):
        r = FetchQueueEntry(
            source="yahoo", ticker="AAPL", field="fundamentals",
            priority=0,
            created_at="2026-05-21T08:00:00Z",
            dispatched_at="2026-05-21T08:00:05Z",
            status="dispatched",
        )
        assert r.dispatched_at == "2026-05-21T08:00:05Z"
        assert r.completed_at is None

    def test_completed_carries_completed_at(self):
        r = FetchQueueEntry(
            source="yahoo", ticker="AAPL", field="fundamentals",
            priority=0,
            created_at="2026-05-21T08:00:00Z",
            dispatched_at="2026-05-21T08:00:05Z",
            completed_at="2026-05-21T08:00:07Z",
            status="completed",
        )
        assert r.completed_at == "2026-05-21T08:00:07Z"

    def test_priority_can_be_negative(self):
        r = FetchQueueEntry(
            source="yahoo", ticker="AAPL", field="fundamentals",
            priority=-50,
            created_at="2026-05-21T08:00:00Z",
            status="pending",
        )
        assert r.priority == -50
```

- [ ] **Step 2: Run tests to verify they fail**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_7_5.py -v
```

Expected: `ImportError` on the three new model names.

- [ ] **Step 3: Append models to `src/common/schemas.py`**

At the **end** of `src/common/schemas.py`, append:

```python


# === Phase A.3.7.5 - Rate-Limited Fetch Orchestrator ====================

_CallStatus = Literal["ok", "failed", "deferred"]
_QueueStatus = Literal["pending", "dispatched", "completed", "failed", "deferred"]


class ProviderCallLog(BaseModel):
    """One row per provider HTTP call. Append-only audit trail.

    Spec: docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 15 row A.3.7.5. Distinct from `source_run_log` (which is per-run
    aggregate); this is per-call granularity for replay + debugging.

    `ticker` is Optional because FRED macro pulls have no ticker. `field`
    carries the canonical field name (or a category-tagged label like
    'macro:UNRATE' for macro series).

    `call_id` is autoincremented by SQLite's INTEGER PRIMARY KEY mechanism;
    the model accepts it as None on construction (assigned on INSERT).
    """

    call_id: Optional[int] = None
    source: str
    ticker: Optional[str] = None
    field: str
    started_at: str
    finished_at: str
    status: _CallStatus
    bytes_returned: int = Field(ge=0)
    error_message: Optional[str] = None
    http_status: Optional[int] = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s


class ProviderQuotaState(BaseModel):
    """One row per (source, provider, window_start) bucket of provider
    calls. The orchestrator queries this table with a sliding-window
    aggregate to determine how many calls remain in the current window.

    `source` is the source-registry name (e.g., 'openbb'); `provider` is
    the per-quota bucket name (e.g., 'fmp_stable'). For single-provider
    sources they are equal; for `openbb` they differ.

    `window_start` is the floor-to-second of call time, ISO 8601 UTC.
    `calls_used` is the count of calls bucketed at exactly that
    `window_start`. Rows older than 7 days are pruned by
    `cleanup_stale_quota_rows()`.
    """

    source: str
    provider: str
    window_start: str
    calls_used: int = Field(ge=0)


class FetchQueueEntry(BaseModel):
    """One row per pending / in-flight / completed fetch job.

    `status` transitions: pending -> dispatched -> completed | failed.
    `deferred` is a terminal status for jobs the orchestrator chose to
    skip (e.g., because quota was saturated by the time their turn came
    up); the next staleness sweep will re-enqueue them.

    `queue_id` is autoincremented by SQLite; None on construction.

    `priority` is caller-supplied (default 0). The orchestrator's queue
    priority scorer combines `priority` with the row's staleness +
    per-(source, field) weight to compute the dispatch order.
    """

    queue_id: Optional[int] = None
    source: str
    ticker: Optional[str] = None
    field: str
    priority: int = 0
    created_at: str
    dispatched_at: Optional[str] = None
    completed_at: Optional[str] = None
    status: _QueueStatus

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_7_5.py -v
```

Expected: 20 PASS (9 ProviderCallLog + 4 ProviderQuotaState + 8 FetchQueueEntry — totals approximate; ±2 acceptable).

- [ ] **Step 5: Commit**

```
git add src/common/schemas.py tests/common/test_schemas_a3_7_5.py
git commit -m "feat(schemas): A.3.7.5 - ProviderCallLog + ProviderQuotaState + FetchQueueEntry Pydantic models"
```

---

## Task 2: `migrate_to_v10` + DB helpers + `config/orchestrator.yaml`

**Files:**
- Modify: `src/common/database.py` (append methods after `migrate_to_v9`)
- Create: `config/orchestrator.yaml`
- Create: `tests/common/test_database_a3_7_5.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/common/test_database_a3_7_5.py`:

```python
"""Tests for Phase A.3.7.5 database migration + helpers."""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import (
    ProviderCallLog,
    ProviderQuotaState,
    FetchQueueEntry,
)


class TestMigrateToV10:
    def test_migration_bumps_schema_version_to_10(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        assert db.get_schema_version() == 10

    def test_idempotent(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.migrate_to_v10()
        db.migrate_to_v10()
        assert db.get_schema_version() == 10

    def test_creates_provider_call_log_table(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        with sqlite3.connect(db_path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(provider_call_log)")}
        assert cols == {
            "call_id", "source", "ticker", "field",
            "started_at", "finished_at",
            "status", "bytes_returned", "error_message", "http_status",
        }

    def test_creates_provider_quota_state_table(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        with sqlite3.connect(db_path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(provider_quota_state)")}
        assert cols == {"source", "provider", "window_start", "calls_used"}

    def test_creates_fetch_queue_table(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        with sqlite3.connect(db_path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(fetch_queue)")}
        assert cols == {
            "queue_id", "source", "ticker", "field",
            "priority", "created_at", "dispatched_at", "completed_at",
            "status",
        }

    def test_provider_call_log_has_call_started_index(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        with sqlite3.connect(db_path) as c:
            idx = {r[1] for r in c.execute("PRAGMA index_list(provider_call_log)")}
        assert "idx_call_source_started" in idx

    def test_quota_state_pk_is_source_provider_window(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        with sqlite3.connect(db_path) as c:
            c.execute(
                "INSERT INTO provider_quota_state (source, provider, window_start, calls_used) "
                "VALUES (?, ?, ?, ?)",
                ("polygon", "polygon", "2026-05-21T08:00:00Z", 1),
            )
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO provider_quota_state (source, provider, window_start, calls_used) "
                    "VALUES (?, ?, ?, ?)",
                    ("polygon", "polygon", "2026-05-21T08:00:00Z", 1),
                )

    def test_fetch_queue_pending_index(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        DatabaseManager(db_path=str(db_path)).migrate_to_v10()
        with sqlite3.connect(db_path) as c:
            idx = {r[1] for r in c.execute("PRAGMA index_list(fetch_queue)")}
        assert "idx_queue_status" in idx


class TestInsertProviderCallLog:
    def test_insert_single_row(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        row = ProviderCallLog(
            source="yahoo", ticker="AAPL", field="fundamentals",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:01Z",
            status="ok", bytes_returned=4096, http_status=200,
        )
        db.insert_provider_call_log([row])
        with sqlite3.connect(db.db_path) as c:
            n = c.execute("SELECT COUNT(*) FROM provider_call_log").fetchone()[0]
        assert n == 1

    def test_empty_list_noop(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.insert_provider_call_log([])
        with sqlite3.connect(db.db_path) as c:
            n = c.execute("SELECT COUNT(*) FROM provider_call_log").fetchone()[0]
        assert n == 0

    def test_call_id_autoincrement(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        rows = [
            ProviderCallLog(
                source="yahoo", ticker="AAPL", field="fundamentals",
                started_at=f"2026-05-21T08:00:0{i}Z",
                finished_at=f"2026-05-21T08:00:0{i}Z",
                status="ok", bytes_returned=0,
            )
            for i in range(3)
        ]
        db.insert_provider_call_log(rows)
        with sqlite3.connect(db.db_path) as c:
            ids = sorted(r[0] for r in c.execute("SELECT call_id FROM provider_call_log"))
        assert len(ids) == 3
        assert ids[0] < ids[1] < ids[2]

    def test_macro_row_with_null_ticker(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.insert_provider_call_log([
            ProviderCallLog(
                source="fred", ticker=None, field="macro:UNRATE",
                started_at="2026-05-21T08:00:00Z",
                finished_at="2026-05-21T08:00:00Z",
                status="ok", bytes_returned=128,
            )
        ])
        with sqlite3.connect(db.db_path) as c:
            row = c.execute("SELECT ticker, field FROM provider_call_log").fetchone()
        assert row == (None, "macro:UNRATE")


class TestQueueHelpers:
    def test_enqueue_inserts_pending(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=0)
        with sqlite3.connect(db.db_path) as c:
            row = c.execute("SELECT source, ticker, field, status FROM fetch_queue").fetchone()
        assert row == ("yahoo", "AAPL", "fundamentals", "pending")

    def test_enqueue_deduplicates_pending(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=0)
        db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=0)
        with sqlite3.connect(db.db_path) as c:
            n = c.execute("SELECT COUNT(*) FROM fetch_queue WHERE status='pending'").fetchone()[0]
        assert n == 1

    def test_dequeue_returns_oldest_highest_priority_pending(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=0)
        db.enqueue_fetch(source="yahoo", ticker="MSFT", field="fundamentals", priority=10)
        db.enqueue_fetch(source="yahoo", ticker="GOOG", field="fundamentals", priority=10)
        entry = db.dequeue_next()
        assert entry is not None
        assert entry.ticker == "MSFT"
        assert entry.status == "dispatched"

    def test_dequeue_returns_none_when_empty(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        assert db.dequeue_next() is None

    def test_dequeue_marks_dispatched(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=0)
        entry = db.dequeue_next()
        assert entry is not None
        with sqlite3.connect(db.db_path) as c:
            row = c.execute(
                "SELECT status, dispatched_at FROM fetch_queue WHERE queue_id=?",
                (entry.queue_id,),
            ).fetchone()
        assert row[0] == "dispatched"
        assert row[1] is not None

    def test_update_queue_status_to_completed(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=0)
        entry = db.dequeue_next()
        assert entry is not None
        db.update_queue_status(entry.queue_id, "completed")
        with sqlite3.connect(db.db_path) as c:
            row = c.execute(
                "SELECT status, completed_at FROM fetch_queue WHERE queue_id=?",
                (entry.queue_id,),
            ).fetchone()
        assert row[0] == "completed"
        assert row[1] is not None

    def test_update_queue_status_to_failed(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=0)
        entry = db.dequeue_next()
        assert entry is not None
        db.update_queue_status(entry.queue_id, "failed")
        with sqlite3.connect(db.db_path) as c:
            status = c.execute(
                "SELECT status FROM fetch_queue WHERE queue_id=?",
                (entry.queue_id,),
            ).fetchone()[0]
        assert status == "failed"

    def test_reclaim_orphaned_dispatched(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        old_iso = (
            datetime.datetime.utcnow() - datetime.timedelta(minutes=10)
        ).isoformat()
        with sqlite3.connect(db.db_path) as c:
            c.execute(
                "INSERT INTO fetch_queue (source, ticker, field, priority, created_at, "
                "dispatched_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("yahoo", "AAPL", "fundamentals", 0, old_iso, old_iso, "dispatched"),
            )
            c.commit()
        n_reclaimed = db.reclaim_orphaned_dispatched(stale_after_seconds=300)
        assert n_reclaimed == 1
        with sqlite3.connect(db.db_path) as c:
            status = c.execute("SELECT status FROM fetch_queue").fetchone()[0]
        assert status == "pending"

    def test_reclaim_skips_recent_dispatched(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        recent_iso = datetime.datetime.utcnow().isoformat()
        with sqlite3.connect(db.db_path) as c:
            c.execute(
                "INSERT INTO fetch_queue (source, ticker, field, priority, created_at, "
                "dispatched_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("yahoo", "AAPL", "fundamentals", 0, recent_iso, recent_iso, "dispatched"),
            )
            c.commit()
        n_reclaimed = db.reclaim_orphaned_dispatched(stale_after_seconds=300)
        assert n_reclaimed == 0


class TestQuotaHelpers:
    def test_record_single_use(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.record_quota_use(source="polygon", provider="polygon",
                            window_start="2026-05-21T08:00:00Z", n=1)
        used = db.get_quota_calls_in_window(
            source="polygon", provider="polygon",
            since_iso="2026-05-21T07:59:00Z",
        )
        assert used == 1

    def test_record_increments_existing_bucket(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        for _ in range(3):
            db.record_quota_use(source="polygon", provider="polygon",
                                window_start="2026-05-21T08:00:00Z", n=1)
        used = db.get_quota_calls_in_window(
            source="polygon", provider="polygon",
            since_iso="2026-05-21T07:59:00Z",
        )
        assert used == 3

    def test_buckets_outside_window_excluded(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.record_quota_use(source="polygon", provider="polygon",
                            window_start="2026-05-21T08:02:00Z", n=1)
        db.record_quota_use(source="polygon", provider="polygon",
                            window_start="2026-05-21T08:00:00Z", n=1)
        used = db.get_quota_calls_in_window(
            source="polygon", provider="polygon",
            since_iso="2026-05-21T08:01:00Z",
        )
        assert used == 1

    def test_multi_provider_isolation(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        db.record_quota_use(source="openbb", provider="fmp_stable",
                            window_start="2026-05-21T08:00:00Z", n=2)
        db.record_quota_use(source="openbb", provider="polygon",
                            window_start="2026-05-21T08:00:00Z", n=5)
        assert db.get_quota_calls_in_window(
            source="openbb", provider="fmp_stable",
            since_iso="2026-05-21T07:59:00Z") == 2
        assert db.get_quota_calls_in_window(
            source="openbb", provider="polygon",
            since_iso="2026-05-21T07:59:00Z") == 5

    def test_cleanup_stale_quota_rows(self, tmp_path: Path):
        db = DatabaseManager(db_path=str(tmp_path / "test.db"))
        db.migrate_to_v10()
        old = (datetime.datetime.utcnow() - datetime.timedelta(days=8)).isoformat()
        recent = datetime.datetime.utcnow().isoformat()
        db.record_quota_use(source="polygon", provider="polygon",
                            window_start=old, n=1)
        db.record_quota_use(source="polygon", provider="polygon",
                            window_start=recent, n=1)
        n_pruned = db.cleanup_stale_quota_rows(keep_days=7)
        assert n_pruned == 1
        with sqlite3.connect(db.db_path) as c:
            n_remaining = c.execute(
                "SELECT COUNT(*) FROM provider_quota_state"
            ).fetchone()[0]
        assert n_remaining == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_7_5.py -v
```

Expected: `AttributeError` on `migrate_to_v10`, `insert_provider_call_log`, `enqueue_fetch`, etc.

- [ ] **Step 3: Append `migrate_to_v10` + helpers to `src/common/database.py`**

Locate the existing `migrate_to_v9` method (around line 703) and **append AFTER it** (preserving everything else):

```python
    def migrate_to_v10(self) -> None:
        """Idempotent migration v9 -> v10 per A.3 spec section 15 row A.3.7.5.

        Adds three new tables for the Rate-Limited Fetch Orchestrator:

          - provider_call_log: append-only audit. PK is call_id INTEGER PRIMARY
            KEY AUTOINCREMENT. Indexed on (source, started_at) for quota-window
            queries and on (status) for failure-rate diagnostics.

          - provider_quota_state: sliding-window quota tracker. PK is
            (source, provider, window_start). Window-aging is done by query
            predicate, NOT by row deletion. cleanup_stale_quota_rows() prunes
            rows older than 7 days to keep the table small.

          - fetch_queue: pending-work queue. PK is queue_id INTEGER PRIMARY
            KEY AUTOINCREMENT. status transitions:
              pending -> dispatched -> completed | failed | deferred.

        Also issues PRAGMA journal_mode=WAL so the orchestrator's per-call
        short transactions don't block concurrent readers.

        Safe to call multiple times. Existing data preserved.
        """
        self.migrate_to_v9()

        v10_ddl = [
            """
            CREATE TABLE IF NOT EXISTS provider_call_log (
              call_id INTEGER PRIMARY KEY AUTOINCREMENT,
              source TEXT NOT NULL,
              ticker TEXT,
              field TEXT NOT NULL,
              started_at TEXT NOT NULL,
              finished_at TEXT NOT NULL,
              status TEXT NOT NULL,
              bytes_returned INTEGER NOT NULL DEFAULT 0,
              error_message TEXT,
              http_status INTEGER
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_call_source_started "
            "ON provider_call_log(source, started_at)",
            "CREATE INDEX IF NOT EXISTS idx_call_status "
            "ON provider_call_log(status)",
            """
            CREATE TABLE IF NOT EXISTS provider_quota_state (
              source TEXT NOT NULL,
              provider TEXT NOT NULL,
              window_start TEXT NOT NULL,
              calls_used INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (source, provider, window_start)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_quota_provider_window "
            "ON provider_quota_state(provider, window_start)",
            """
            CREATE TABLE IF NOT EXISTS fetch_queue (
              queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
              source TEXT NOT NULL,
              ticker TEXT,
              field TEXT NOT NULL,
              priority INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              dispatched_at TEXT,
              completed_at TEXT,
              status TEXT NOT NULL DEFAULT 'pending'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_queue_status "
            "ON fetch_queue(status, priority DESC, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_queue_dedup "
            "ON fetch_queue(source, ticker, field, status)",
        ]

        with self.get_connection() as conn:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            cur = conn.cursor()
            for ddl in v10_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 10")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (10, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Phase A.3.7.5: orchestrator helpers
    # ------------------------------------------------------------------

    def insert_provider_call_log(self, rows: list) -> None:
        """Insert ProviderCallLog records into provider_call_log."""
        if not rows:
            return
        values = [
            (
                r.source, r.ticker, r.field,
                r.started_at, r.finished_at,
                r.status, r.bytes_returned,
                r.error_message, r.http_status,
            )
            for r in rows
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO provider_call_log
                  (source, ticker, field, started_at, finished_at,
                   status, bytes_returned, error_message, http_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()

    def enqueue_fetch(
        self,
        source: str,
        ticker: str | None,
        field: str,
        priority: int = 0,
    ) -> int | None:
        """Enqueue (source, ticker, field). Returns queue_id, or None if an
        identical pending row already exists (dedup).
        """
        now_iso = datetime.datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            existing = conn.execute(
                "SELECT queue_id FROM fetch_queue "
                "WHERE source=? AND ticker IS ? AND field=? AND status='pending'",
                (source, ticker, field),
            ).fetchone()
            if existing is not None:
                return None
            cur = conn.execute(
                """
                INSERT INTO fetch_queue
                  (source, ticker, field, priority, created_at, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (source, ticker, field, priority, now_iso),
            )
            conn.commit()
            return cur.lastrowid

    def dequeue_next(self):
        """Atomically claim next-highest-priority pending entry. Marks
        'dispatched' + sets dispatched_at. Returns FetchQueueEntry or None.
        """
        from src.common.schemas import FetchQueueEntry

        now_iso = datetime.datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT queue_id, source, ticker, field, priority,
                           created_at, dispatched_at, completed_at, status
                    FROM fetch_queue
                    WHERE status='pending'
                    ORDER BY priority DESC, created_at ASC, queue_id ASC
                    LIMIT 1
                    """,
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                queue_id = row[0]
                conn.execute(
                    "UPDATE fetch_queue SET status='dispatched', dispatched_at=? "
                    "WHERE queue_id=?",
                    (now_iso, queue_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return FetchQueueEntry(
            queue_id=row[0],
            source=row[1],
            ticker=row[2],
            field=row[3],
            priority=row[4],
            created_at=row[5],
            dispatched_at=now_iso,
            completed_at=row[7],
            status="dispatched",
        )

    def update_queue_status(self, queue_id: int, status: str) -> None:
        """Update queue row to terminal status. Sets completed_at to now."""
        now_iso = datetime.datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE fetch_queue SET status=?, completed_at=? WHERE queue_id=?",
                (status, now_iso, queue_id),
            )
            conn.commit()

    def reclaim_orphaned_dispatched(self, stale_after_seconds: int = 300) -> int:
        """Reset to 'pending' any 'dispatched' row whose dispatched_at is
        older than stale_after_seconds. Returns count reclaimed.
        """
        cutoff = (
            datetime.datetime.utcnow()
            - datetime.timedelta(seconds=stale_after_seconds)
        ).isoformat()
        with self.get_connection() as conn:
            cur = conn.execute(
                "UPDATE fetch_queue SET status='pending', dispatched_at=NULL "
                "WHERE status='dispatched' AND dispatched_at < ?",
                (cutoff,),
            )
            conn.commit()
            return cur.rowcount

    def record_quota_use(
        self, source: str, provider: str, window_start: str, n: int = 1
    ) -> None:
        """Increment calls_used in the (source, provider, window_start) bucket
        by n. Creates the row if absent.
        """
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO provider_quota_state
                  (source, provider, window_start, calls_used)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, provider, window_start)
                DO UPDATE SET calls_used = calls_used + excluded.calls_used
                """,
                (source, provider, window_start, n),
            )
            conn.commit()

    def get_quota_calls_in_window(
        self, source: str, provider: str, since_iso: str
    ) -> int:
        """Return total calls_used across buckets with window_start > since_iso."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(calls_used), 0) FROM provider_quota_state "
                "WHERE source=? AND provider=? AND window_start > ?",
                (source, provider, since_iso),
            ).fetchone()
        return int(row[0])

    def cleanup_stale_quota_rows(self, keep_days: int = 7) -> int:
        """Delete provider_quota_state rows older than keep_days."""
        cutoff = (
            datetime.datetime.utcnow() - datetime.timedelta(days=keep_days)
        ).isoformat()
        with self.get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM provider_quota_state WHERE window_start < ?",
                (cutoff,),
            )
            conn.commit()
            return cur.rowcount
```

- [ ] **Step 4: Create `config/orchestrator.yaml`**

```yaml
# Phase A.3.7.5: Rate-Limited Fetch Orchestrator configuration.
#
# Edit this file (no code change required) to adjust per-provider quotas,
# re-weight priorities, or add a new provider bucket.
#
# Quotas are SLIDING windows: "calls_per_window in any window_seconds"
# means at any wall-clock instant the orchestrator may have made at most
# calls_per_window calls in the preceding window_seconds.

provider_quotas:
  polygon:       {calls_per_window: 5,    window_seconds: 60}
  fmp_stable:    {calls_per_window: 250,  window_seconds: 86400}
  tiingo:        {calls_per_window: 1000, window_seconds: 86400}
  sec_edgar:     {calls_per_window: 10,   window_seconds: 1}
  yahoo:         {calls_per_window: 2000, window_seconds: 3600}
  finra:         {calls_per_window: 100,  window_seconds: 86400}
  fred:          {calls_per_window: 120,  window_seconds: 60}
  stockanalysis: {calls_per_window: 60,   window_seconds: 60}
  finviz:        {calls_per_window: 60,   window_seconds: 60}

priority_weights:
  edgar:         {insider: 100, fundamentals: 30, filings: 20}
  yahoo:         {revisions: 60, fundamentals: 25, historical_price: 15}
  finra:         {short_interest: 50}
  fred:          {macro: 5}
  stockanalysis: {ratios: 20}
  finviz:        {universe: 10}
  openbb:        {multi_provider: 25}

source_provider_map:
  finviz:        [finviz]
  yahoo:         [yahoo]
  edgar:         [sec_edgar]
  fred:          [fred]
  finra:         [finra]
  stockanalysis: [stockanalysis]
  openbb:        [fmp_stable, polygon, tiingo]

refresh_cadence_hours:
  yahoo:
    fundamentals:      24
    historical_price:  24
    revisions:         12
  edgar:
    fundamentals:      168
    filings:           24
    insider:           24
  finra:
    short_interest:    336
  fred:
    macro:             24
  stockanalysis:
    ratios:            168
  finviz:
    universe:          168
  openbb:
    multi_provider:    24

orchestrator:
  stale_dispatch_seconds: 300
  default_budget_seconds: 60
  max_enqueue_per_tick:   200
  quota_retention_days:   7
```

- [ ] **Step 5: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_7_5.py -v
```

Expected: ~26 PASS (8 migrate + 4 insert + 9 queue + 5 quota; ±2 acceptable).

- [ ] **Step 6: Sanity-check fresh DB migration**

```
./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager('data/_v10_check.db'); m.migrate_to_v10(); print('schema_version:', m.get_schema_version())"
```

Expected: `schema_version: 10`. Cleanup temp file:

```
./venv/Scripts/python.exe -c "import pathlib; [pathlib.Path(p).unlink(missing_ok=True) for p in ['data/_v10_check.db','data/_v10_check.db-wal','data/_v10_check.db-shm']]"
```

- [ ] **Step 7: Commit**

```
git add src/common/database.py config/orchestrator.yaml tests/common/test_database_a3_7_5.py
git commit -m "feat(database): A.3.7.5 - migrate_to_v10 + orchestrator helpers + config/orchestrator.yaml"
```

---

## Task 3: `RateLimitedFetchOrchestrator` — quota math + queue priority + `tick()` logic

This is the largest task. It is split into three modules so each piece has a sharp unit-test boundary:

1. `quota_window.py` — pure-function sliding-window math. No DB; takes a callable returning quota usage and returns `available(source, n)` booleans. Easy to test with synthetic inputs.
2. `queue_priority.py` — pure-function priority scoring. No DB; takes a list of candidate (source, ticker, field, staleness_hours) tuples and returns them sorted.
3. `fetch_orchestrator.py` — `RateLimitedFetchOrchestrator` class. Composes (1) and (2); owns `tick()`; talks to the registry and DB.

**Files:**
- Create: `src/common/orchestrator/__init__.py`
- Create: `src/common/orchestrator/quota_window.py`
- Create: `src/common/orchestrator/queue_priority.py`
- Create: `src/common/orchestrator/fetch_orchestrator.py`
- Create: `tests/common/orchestrator/__init__.py`
- Create: `tests/common/orchestrator/test_quota_window.py`
- Create: `tests/common/orchestrator/test_queue_priority.py`
- Create: `tests/common/orchestrator/test_orchestrator_tick.py`

### Task 3.1: `quota_window.py` (sliding-window math)

- [ ] **Step 1: Write the failing test**

Create `tests/common/orchestrator/__init__.py` (empty, just enables the test package):

```python
```

Create `tests/common/orchestrator/test_quota_window.py`:

```python
"""Tests for src.common.orchestrator.quota_window — pure sliding-window math.

No DB; tests pass synthetic usage-lookup callables.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from src.common.orchestrator.quota_window import (
    QuotaWindow,
    floor_to_second,
    since_iso_for_window,
)


def _iso(dt: _dt.datetime) -> str:
    return dt.isoformat()


class TestFloorToSecond:
    def test_floors_microseconds(self):
        dt = _dt.datetime(2026, 5, 21, 8, 0, 0, microsecond=999_999)
        floored = floor_to_second(dt)
        assert floored.microsecond == 0
        assert floored.second == 0
        assert floored.minute == 0

    def test_idempotent_on_already_floored(self):
        dt = _dt.datetime(2026, 5, 21, 8, 0, 0)
        assert floor_to_second(dt) == dt


class TestSinceIsoForWindow:
    def test_subtracts_window_seconds(self):
        now = _dt.datetime(2026, 5, 21, 8, 1, 0)
        # Polygon: 60s window
        result = since_iso_for_window(now, window_seconds=60)
        assert result == "2026-05-21T08:00:00"

    def test_daily_window(self):
        now = _dt.datetime(2026, 5, 21, 8, 0, 0)
        result = since_iso_for_window(now, window_seconds=86400)
        assert result == "2026-05-20T08:00:00"


class TestQuotaWindowAvailable:
    """Verify the quota_available(source, n) logic. Test fixture supplies a
    fake usage-lookup callable that returns precomputed counts."""

    def test_available_when_window_empty(self):
        """5/min limit, 0 used in last 60s -> 5 available."""
        usage = lambda source, provider, since_iso: 0
        qw = QuotaWindow(
            provider_quotas={"polygon": {"calls_per_window": 5, "window_seconds": 60}},
            source_provider_map={"polygon": ["polygon"]},
            usage_lookup=usage,
            now=_dt.datetime(2026, 5, 21, 8, 0, 0),
        )
        assert qw.available("polygon", 1) is True
        assert qw.available("polygon", 5) is True
        assert qw.available("polygon", 6) is False

    def test_saturated_window(self):
        usage = lambda source, provider, since_iso: 5
        qw = QuotaWindow(
            provider_quotas={"polygon": {"calls_per_window": 5, "window_seconds": 60}},
            source_provider_map={"polygon": ["polygon"]},
            usage_lookup=usage,
            now=_dt.datetime(2026, 5, 21, 8, 0, 0),
        )
        assert qw.available("polygon", 1) is False

    def test_exact_boundary(self):
        """4 used + want 1 more = exactly at limit; allowed."""
        usage = lambda source, provider, since_iso: 4
        qw = QuotaWindow(
            provider_quotas={"polygon": {"calls_per_window": 5, "window_seconds": 60}},
            source_provider_map={"polygon": ["polygon"]},
            usage_lookup=usage,
            now=_dt.datetime(2026, 5, 21, 8, 0, 0),
        )
        assert qw.available("polygon", 1) is True
        assert qw.available("polygon", 2) is False

    def test_multi_provider_source_requires_all_available(self):
        """openbb consumes from fmp_stable + polygon + tiingo. If any one is
        saturated, the source is unavailable."""
        # fmp_stable saturated; polygon + tiingo have headroom.
        def usage(source, provider, since_iso):
            return {
                ("openbb", "fmp_stable"): 250,
                ("openbb", "polygon"): 0,
                ("openbb", "tiingo"): 0,
            }[(source, provider)]

        qw = QuotaWindow(
            provider_quotas={
                "fmp_stable":    {"calls_per_window": 250, "window_seconds": 86400},
                "polygon":       {"calls_per_window": 5,   "window_seconds": 60},
                "tiingo":        {"calls_per_window": 1000, "window_seconds": 86400},
            },
            source_provider_map={"openbb": ["fmp_stable", "polygon", "tiingo"]},
            usage_lookup=usage,
            now=_dt.datetime(2026, 5, 21, 8, 0, 0),
        )
        assert qw.available("openbb", 1) is False

    def test_multi_provider_source_available_when_all_have_headroom(self):
        usage = lambda source, provider, since_iso: 1
        qw = QuotaWindow(
            provider_quotas={
                "fmp_stable":    {"calls_per_window": 250, "window_seconds": 86400},
                "polygon":       {"calls_per_window": 5,   "window_seconds": 60},
                "tiingo":        {"calls_per_window": 1000, "window_seconds": 86400},
            },
            source_provider_map={"openbb": ["fmp_stable", "polygon", "tiingo"]},
            usage_lookup=usage,
            now=_dt.datetime(2026, 5, 21, 8, 0, 0),
        )
        assert qw.available("openbb", 1) is True

    def test_unknown_source_returns_false(self):
        """Defensive: a source not in source_provider_map can't be dispatched.
        Returning False is safer than KeyError'ing inside the tick loop."""
        qw = QuotaWindow(
            provider_quotas={},
            source_provider_map={},
            usage_lookup=lambda *a, **k: 0,
            now=_dt.datetime(2026, 5, 21, 8, 0, 0),
        )
        assert qw.available("nonexistent_source", 1) is False

    def test_remaining_returns_dict(self):
        usage = lambda source, provider, since_iso: 2
        qw = QuotaWindow(
            provider_quotas={"polygon": {"calls_per_window": 5, "window_seconds": 60}},
            source_provider_map={"polygon": ["polygon"]},
            usage_lookup=usage,
            now=_dt.datetime(2026, 5, 21, 8, 0, 0),
        )
        r = qw.remaining()
        assert r["polygon"] == 3

    def test_remaining_multi_provider_returns_min(self):
        """For multi-provider source, remaining is the bottleneck (min across
        providers). openbb with fmp_stable: 248 remaining, polygon: 4
        remaining, tiingo: 999 remaining -> overall 4 (polygon is the
        bottleneck)."""
        def usage(source, provider, since_iso):
            return {
                ("openbb", "fmp_stable"): 2,
                ("openbb", "polygon"): 1,
                ("openbb", "tiingo"): 1,
            }[(source, provider)]
        qw = QuotaWindow(
            provider_quotas={
                "fmp_stable":    {"calls_per_window": 250, "window_seconds": 86400},
                "polygon":       {"calls_per_window": 5,   "window_seconds": 60},
                "tiingo":        {"calls_per_window": 1000, "window_seconds": 86400},
            },
            source_provider_map={"openbb": ["fmp_stable", "polygon", "tiingo"]},
            usage_lookup=usage,
            now=_dt.datetime(2026, 5, 21, 8, 0, 0),
        )
        r = qw.remaining()
        assert r["openbb"] == 4  # min(248, 4, 999)


class TestQuotaWindowRollover:
    """Test that calls in the past (before the window) don't count."""

    def test_call_at_exactly_window_boundary_excluded(self):
        """The since_iso_for_window predicate uses strict `>` so a row at
        exactly window_start = now - window is NOT counted (it's on the
        boundary, just slid out)."""
        # Simulate a fake usage_lookup that returns 1 if since_iso > some cutoff,
        # else 0. Verify the cutoff is computed correctly.
        captured = {}
        def usage(source, provider, since_iso):
            captured["since_iso"] = since_iso
            return 0
        qw = QuotaWindow(
            provider_quotas={"polygon": {"calls_per_window": 5, "window_seconds": 60}},
            source_provider_map={"polygon": ["polygon"]},
            usage_lookup=usage,
            now=_dt.datetime(2026, 5, 21, 8, 1, 0),
        )
        qw.available("polygon", 1)
        assert captured["since_iso"] == "2026-05-21T08:00:00"

    def test_daily_window_uses_correct_cutoff(self):
        captured = {}
        def usage(source, provider, since_iso):
            captured["since_iso"] = since_iso
            return 0
        qw = QuotaWindow(
            provider_quotas={"fmp_stable": {"calls_per_window": 250, "window_seconds": 86400}},
            source_provider_map={"fmp_stable": ["fmp_stable"]},
            usage_lookup=usage,
            now=_dt.datetime(2026, 5, 21, 12, 0, 0),
        )
        qw.available("fmp_stable", 1)
        assert captured["since_iso"] == "2026-05-20T12:00:00"
```

- [ ] **Step 2: Run tests to verify they fail**

```
./venv/Scripts/python.exe -m pytest tests/common/orchestrator/test_quota_window.py -v
```

Expected: `ImportError` on `src.common.orchestrator.quota_window`.

- [ ] **Step 3: Create `src/common/orchestrator/__init__.py`**

```python
"""src.common.orchestrator — Phase A.3.7.5 Rate-Limited Fetch Orchestrator.

Exposes:
  - RateLimitedFetchOrchestrator: the main class
  - QuotaWindow: pure-function sliding-window math
  - prioritize_queue, score_entry: pure-function priority scoring

Spec: docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md
section 15 row A.3.7.5.
"""
from src.common.orchestrator.fetch_orchestrator import RateLimitedFetchOrchestrator
from src.common.orchestrator.quota_window import QuotaWindow
from src.common.orchestrator.queue_priority import prioritize_queue, score_entry

__all__ = [
    "RateLimitedFetchOrchestrator",
    "QuotaWindow",
    "prioritize_queue",
    "score_entry",
]
```

- [ ] **Step 4: Create `src/common/orchestrator/quota_window.py`**

```python
"""Pure-function sliding-window quota math for the orchestrator.

No DB, no I/O. The DB integration lives in fetch_orchestrator.py; this
module just takes a callable that returns "calls used since X" and
computes "is there headroom for N more calls?".

Sliding-window semantics: at any wall-clock instant `now`, a quota of
`calls_per_window in window_seconds` is satisfied iff the count of calls
in the half-open interval (now - window_seconds, now] is <=
calls_per_window. The boundary is strict `>` on the lower end so calls
exactly at `now - window_seconds` have just slid out.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Callable


def floor_to_second(dt: _dt.datetime) -> _dt.datetime:
    """Strip microseconds. The orchestrator buckets quota usage at
    one-second granularity; floor first so two calls in the same second
    map to the same provider_quota_state row.
    """
    return dt.replace(microsecond=0)


def since_iso_for_window(now: _dt.datetime, window_seconds: int) -> str:
    """ISO string for the lower bound of the sliding window at `now`.

    The orchestrator uses this string in the SQL predicate
    `WHERE window_start > since_iso_for_window(now, ws)` — strictly
    greater-than, so a bucket at exactly `now - ws` is excluded
    (it's on the boundary).
    """
    return floor_to_second(now - _dt.timedelta(seconds=window_seconds)).isoformat()


@dataclass
class QuotaWindow:
    """Sliding-window quota check for the orchestrator.

    `provider_quotas`: dict[provider_name] -> {calls_per_window, window_seconds}.
    `source_provider_map`: dict[source_name] -> list[provider_name]. For
        single-provider sources the list has one element. For openbb the
        list has three.
    `usage_lookup`: callable (source, provider, since_iso) -> int. In
        production this is bound to
        `DatabaseManager.get_quota_calls_in_window`; in tests it's a fake.
    `now`: the wall-clock used as the window's upper bound. Tests pass a
        fixed value; production uses datetime.datetime.utcnow().
    """

    provider_quotas: dict
    source_provider_map: dict
    usage_lookup: Callable[[str, str, str], int]
    now: _dt.datetime

    def available(self, source: str, n: int = 1) -> bool:
        """True iff source can make `n` more calls without exceeding any of
        its providers' quotas.

        For multi-provider sources (openbb), ALL providers must have at
        least `n` headroom — the orchestrator's atomic dispatch consumes
        one call from each provider per fetch.

        Unknown source returns False (defensive — caller can't dispatch
        what we don't have a quota for).
        """
        providers = self.source_provider_map.get(source)
        if not providers:
            return False
        for provider in providers:
            q = self.provider_quotas.get(provider)
            if q is None:
                # Defensive: no quota config for this provider -> refuse.
                return False
            since_iso = since_iso_for_window(self.now, q["window_seconds"])
            used = self.usage_lookup(source, provider, since_iso)
            if used + n > q["calls_per_window"]:
                return False
        return True

    def remaining(self) -> dict:
        """Return dict[source_name] -> remaining-calls (min across providers
        for multi-provider sources). For diagnostic / CLI summary output.
        """
        out: dict[str, int] = {}
        for source, providers in self.source_provider_map.items():
            per_provider_remaining: list[int] = []
            for provider in providers:
                q = self.provider_quotas.get(provider)
                if q is None:
                    per_provider_remaining.append(0)
                    continue
                since_iso = since_iso_for_window(self.now, q["window_seconds"])
                used = self.usage_lookup(source, provider, since_iso)
                per_provider_remaining.append(max(0, q["calls_per_window"] - used))
            out[source] = min(per_provider_remaining) if per_provider_remaining else 0
        return out
```

- [ ] **Step 5: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/orchestrator/test_quota_window.py -v
```

Expected: ~13 PASS.

### Task 3.2: `queue_priority.py` (priority scoring)

- [ ] **Step 6: Write the failing test**

Create `tests/common/orchestrator/test_queue_priority.py`:

```python
"""Tests for src.common.orchestrator.queue_priority — pure priority math."""
from __future__ import annotations

from src.common.orchestrator.queue_priority import (
    score_entry,
    prioritize_queue,
)


class TestScoreEntry:
    """Composite score = staleness_hours * per_(source,field)_weight + priority."""

    def test_basic_score(self):
        weights = {"yahoo": {"fundamentals": 25}}
        s = score_entry(
            source="yahoo", field="fundamentals",
            staleness_hours=10.0, priority=0,
            weights_table=weights,
        )
        assert s == 250.0

    def test_priority_overrides_when_explicit(self):
        weights = {"yahoo": {"fundamentals": 25}}
        s = score_entry(
            source="yahoo", field="fundamentals",
            staleness_hours=10.0, priority=1000,
            weights_table=weights,
        )
        # 10 * 25 + 1000 = 1250
        assert s == 1250.0

    def test_zero_weight_when_missing(self):
        """If (source, field) is not in the weights table, fall back to
        weight=1. (No silent zero-weight: that would deprioritize unknown
        fields below stale FRED macro updates, which isn't the intent.)"""
        s = score_entry(
            source="unknownsrc", field="unknownfield",
            staleness_hours=10.0, priority=0,
            weights_table={},
        )
        assert s == 10.0

    def test_insider_outweighs_macro_at_equal_staleness(self):
        weights = {
            "edgar": {"insider": 100},
            "fred":  {"macro": 5},
        }
        insider_score = score_entry(
            source="edgar", field="insider",
            staleness_hours=10.0, priority=0,
            weights_table=weights,
        )
        macro_score = score_entry(
            source="fred", field="macro",
            staleness_hours=10.0, priority=0,
            weights_table=weights,
        )
        assert insider_score > macro_score
        assert insider_score == 1000.0
        assert macro_score == 50.0


class TestPrioritizeQueue:
    """`prioritize_queue` sorts a list of (FetchQueueEntry, staleness_hours)
    tuples by descending composite score."""

    def _entry(self, source, field, ticker, priority=0, queue_id=None):
        # Minimal-shape FetchQueueEntry stand-in: a dict is fine for the
        # pure-function priority logic. We use a SimpleNamespace so the
        # production code can attribute-access source/field/ticker/priority.
        from types import SimpleNamespace
        return SimpleNamespace(
            queue_id=queue_id, source=source, ticker=ticker, field=field,
            priority=priority, created_at="2026-05-21T08:00:00Z",
            dispatched_at=None, completed_at=None, status="pending",
        )

    def test_orders_by_score_descending(self):
        weights = {
            "edgar": {"insider": 100},
            "fred":  {"macro": 5},
        }
        items = [
            (self._entry("fred", "macro", None), 24.0),
            (self._entry("edgar", "insider", "AAPL"), 24.0),
        ]
        ordered = prioritize_queue(items, weights_table=weights)
        # edgar/insider should come first.
        assert ordered[0][0].source == "edgar"
        assert ordered[1][0].source == "fred"

    def test_staleness_breaks_ties_within_same_weight(self):
        weights = {"yahoo": {"fundamentals": 25}}
        items = [
            (self._entry("yahoo", "fundamentals", "AAPL"), 5.0),
            (self._entry("yahoo", "fundamentals", "MSFT"), 50.0),
        ]
        ordered = prioritize_queue(items, weights_table=weights)
        # More-stale MSFT first.
        assert ordered[0][0].ticker == "MSFT"

    def test_priority_boost_pulls_to_front(self):
        weights = {"yahoo": {"fundamentals": 25}, "fred": {"macro": 5}}
        items = [
            (self._entry("yahoo", "fundamentals", "AAPL", priority=0), 50.0),
            # FRED macro with very stale + huge manual priority = pulls ahead
            (self._entry("fred", "macro", None, priority=10_000), 1.0),
        ]
        ordered = prioritize_queue(items, weights_table=weights)
        assert ordered[0][0].source == "fred"  # manual priority wins.

    def test_empty_input(self):
        assert prioritize_queue([], weights_table={}) == []

    def test_stable_sort_preserves_order_at_equal_score(self):
        weights = {"yahoo": {"fundamentals": 25}}
        a = self._entry("yahoo", "fundamentals", "AAPL", queue_id=1)
        b = self._entry("yahoo", "fundamentals", "MSFT", queue_id=2)
        items = [(a, 10.0), (b, 10.0)]
        ordered = prioritize_queue(items, weights_table=weights)
        # Stable: original order preserved on tie.
        assert ordered[0][0].queue_id == 1
        assert ordered[1][0].queue_id == 2

    def test_negative_priority_demotes(self):
        weights = {"yahoo": {"fundamentals": 25}}
        a = self._entry("yahoo", "fundamentals", "AAPL", priority=-200)
        b = self._entry("yahoo", "fundamentals", "MSFT", priority=0)
        items = [(a, 10.0), (b, 10.0)]
        ordered = prioritize_queue(items, weights_table=weights)
        # MSFT (default priority) comes first.
        assert ordered[0][0].ticker == "MSFT"
```

- [ ] **Step 7: Run tests to verify they fail**

```
./venv/Scripts/python.exe -m pytest tests/common/orchestrator/test_queue_priority.py -v
```

Expected: `ImportError`.

- [ ] **Step 8: Create `src/common/orchestrator/queue_priority.py`**

```python
"""Pure-function priority scoring for the orchestrator's dispatch queue.

Composite score for a queue entry:
    score = staleness_hours * weight(source, field) + priority

where `weight` is loaded from `config/orchestrator.yaml`'s
`priority_weights` block. Higher score => dispatched earlier.

The high-bit `priority` term is caller-controlled (default 0). An
operator who wants to force "refresh AAPL right now" enqueues with
priority=1000 and the entry leapfrogs the staleness-driven ordering.
"""
from __future__ import annotations

from typing import Iterable


def score_entry(
    source: str,
    field: str,
    staleness_hours: float,
    priority: int,
    weights_table: dict,
) -> float:
    """Composite score. Missing (source, field) in weights -> weight=1
    (NOT 0 — see test_zero_weight_when_missing for rationale).
    """
    src_weights = weights_table.get(source, {})
    weight = src_weights.get(field, 1)
    return staleness_hours * weight + priority


def prioritize_queue(items: Iterable, weights_table: dict) -> list:
    """Sort `items` (list of (queue_entry, staleness_hours) tuples) by
    descending composite score. Stable: original ordering preserved on tie.
    """
    items_list = list(items)
    # Python's sort is stable; sort by negative score so descending becomes
    # natural ascending under the stable-sort semantics.
    return sorted(
        items_list,
        key=lambda pair: -score_entry(
            source=pair[0].source,
            field=pair[0].field,
            staleness_hours=pair[1],
            priority=pair[0].priority,
            weights_table=weights_table,
        ),
    )
```

- [ ] **Step 9: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/orchestrator/test_queue_priority.py -v
```

Expected: ~8 PASS.

### Task 3.3: `fetch_orchestrator.py` (the `RateLimitedFetchOrchestrator` class)

- [ ] **Step 10: Write the failing test**

Create `tests/common/orchestrator/test_orchestrator_tick.py`:

```python
"""Tests for src.common.orchestrator.fetch_orchestrator.RateLimitedFetchOrchestrator.tick().

Mocks the source registry's fetch methods so no live HTTP. Asserts:
  - dispatch count matches available quota + queue
  - quota usage written to provider_quota_state
  - per-call rows written to provider_call_log
  - budget respected
  - no-op tick returns clean summary
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.common.database import DatabaseManager
from src.common.orchestrator import RateLimitedFetchOrchestrator


# ---------- Fixtures ----------------------------------------------------

def _fresh_db(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(db_path=str(tmp_path / "test.db"))
    db.migrate_to_v10()
    return db


def _stub_registry(mocker, source_method_returns: dict):
    """Build a mock DataSourceRegistry whose enabled_sources() returns
    SimpleNamespace stubs with patched fetch methods.

    source_method_returns: dict like
       {"yahoo": {"fetch_fundamentals_for_ticker": lambda t, run_id: ...}}
    """
    sources = []
    for src_name, method_map in source_method_returns.items():
        ns = SimpleNamespace(name=src_name, cadence="daily", provides=set())
        for mname, fn in method_map.items():
            setattr(ns, mname, fn)
        sources.append(ns)
    registry = SimpleNamespace(enabled_sources=lambda: sources)
    return registry


def _seed_stale_watermark(db, source, ticker, field, hours_ago):
    """Insert a fetch_watermarks row so the staleness sweep finds it."""
    stale_at = (
        _dt.datetime.utcnow() - _dt.timedelta(hours=hours_ago)
    ).isoformat()
    with sqlite3.connect(db.db_path) as c:
        c.execute(
            """
            INSERT INTO fetch_watermarks
              (source, ticker, field, last_fetched_at, last_observation_date,
               fetch_count, error_count, last_error_message)
            VALUES (?, ?, ?, ?, NULL, 1, 0, NULL)
            """,
            (source, ticker, field, stale_at),
        )
        c.commit()


# ---------- Tests --------------------------------------------------------

def test_tick_no_work_returns_clean_summary(tmp_path: Path, mocker):
    """Empty queue + no stale watermarks -> 0 dispatched, no errors."""
    db = _fresh_db(tmp_path)
    registry = _stub_registry(mocker, {})
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=registry,
        config_path=Path("config/orchestrator.yaml"),
    )
    summary = orch.tick(budget_seconds=60)
    assert summary["dispatched"] == 0
    assert summary["deferred"] == 0
    assert summary["failed"] == 0
    assert "quota_remaining" in summary


def test_tick_dispatches_when_quota_available(tmp_path: Path, mocker):
    """Single Yahoo fundamentals fetch dispatched and recorded."""
    db = _fresh_db(tmp_path)
    db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=0)
    registry = _stub_registry(mocker, {
        "yahoo": {
            "fetch_fundamentals_for_ticker": lambda ticker, run_id: {"pe_ratio": 28.5},
        },
    })
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=registry,
        config_path=Path("config/orchestrator.yaml"),
    )
    summary = orch.tick(budget_seconds=60)
    assert summary["dispatched"] == 1
    # provider_call_log gets one row.
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT source, ticker, field, status FROM provider_call_log"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("yahoo", "AAPL", "fundamentals", "ok")
    # provider_quota_state gets one increment.
    used = db.get_quota_calls_in_window(
        source="yahoo", provider="yahoo",
        since_iso="2026-01-01T00:00:00",
    )
    assert used == 1


def test_tick_defers_when_quota_saturated(tmp_path: Path, mocker):
    """Polygon's 5/min limit; pre-saturate with 5 calls; tick should defer
    any openbb work that depends on Polygon."""
    db = _fresh_db(tmp_path)
    db.enqueue_fetch(source="openbb", ticker="AAPL", field="multi_provider", priority=0)
    # Saturate polygon's window.
    now_floor = _dt.datetime.utcnow().replace(microsecond=0).isoformat()
    db.record_quota_use(source="openbb", provider="polygon",
                        window_start=now_floor, n=5)
    registry = _stub_registry(mocker, {
        "openbb": {
            "fetch_fundamentals_for_ticker": lambda ticker, run_id: {"market_cap": 3e12},
        },
    })
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=registry,
        config_path=Path("config/orchestrator.yaml"),
    )
    summary = orch.tick(budget_seconds=60)
    assert summary["dispatched"] == 0
    assert summary["deferred"] == 1


def test_tick_records_failure_on_source_exception(tmp_path: Path, mocker):
    """If the source's fetch method raises, the call_log row records
    status='failed' with the error message and the queue row goes to
    'failed'."""
    db = _fresh_db(tmp_path)
    db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=0)

    def raising_fetch(ticker, run_id):
        raise RuntimeError("simulated provider 500")

    registry = _stub_registry(mocker, {
        "yahoo": {"fetch_fundamentals_for_ticker": raising_fetch},
    })
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=registry,
        config_path=Path("config/orchestrator.yaml"),
    )
    summary = orch.tick(budget_seconds=60)
    assert summary["dispatched"] == 0
    assert summary["failed"] == 1
    with sqlite3.connect(db.db_path) as c:
        row = c.execute(
            "SELECT status, error_message FROM provider_call_log"
        ).fetchone()
    assert row[0] == "failed"
    assert "simulated provider 500" in row[1]


def test_tick_budget_respected(tmp_path: Path, mocker):
    """If the budget elapses mid-loop, remaining items stay pending."""
    import time
    db = _fresh_db(tmp_path)
    # Enqueue 5 items.
    for i, t in enumerate(["AAPL", "MSFT", "GOOG", "AMZN", "META"]):
        db.enqueue_fetch(source="yahoo", ticker=t, field="fundamentals",
                         priority=10 - i)
    # Each fetch sleeps 0.4s.
    def slow_fetch(ticker, run_id):
        time.sleep(0.4)
        return {"pe_ratio": 28.0}
    registry = _stub_registry(mocker, {
        "yahoo": {"fetch_fundamentals_for_ticker": slow_fetch},
    })
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=registry,
        config_path=Path("config/orchestrator.yaml"),
    )
    summary = orch.tick(budget_seconds=1)  # ~2 fetches fit in 1s
    assert summary["dispatched"] <= 3
    with sqlite3.connect(db.db_path) as c:
        n_pending = c.execute(
            "SELECT COUNT(*) FROM fetch_queue WHERE status='pending'"
        ).fetchone()[0]
    assert n_pending >= 2  # remainder stays pending for next tick


def test_tick_enqueues_stale_watermarks(tmp_path: Path, mocker):
    """A stale watermark with age > cadence_hours triggers an enqueue."""
    db = _fresh_db(tmp_path)
    # Yahoo fundamentals cadence is 24h; seed a 48h-old watermark.
    _seed_stale_watermark(db, "yahoo", "AAPL", "fundamentals", hours_ago=48)
    registry = _stub_registry(mocker, {
        "yahoo": {
            "fetch_fundamentals_for_ticker": lambda ticker, run_id: {"pe_ratio": 28.5},
        },
    })
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=registry,
        config_path=Path("config/orchestrator.yaml"),
    )
    summary = orch.tick(budget_seconds=60)
    # Either dispatched in this tick (if quota allowed) or enqueued for next.
    with sqlite3.connect(db.db_path) as c:
        n_queue = c.execute("SELECT COUNT(*) FROM fetch_queue").fetchone()[0]
    assert n_queue >= 1


def test_tick_skips_fresh_watermarks(tmp_path: Path, mocker):
    """Watermark with age < cadence_hours must NOT be enqueued."""
    db = _fresh_db(tmp_path)
    _seed_stale_watermark(db, "yahoo", "AAPL", "fundamentals", hours_ago=1)
    registry = _stub_registry(mocker, {
        "yahoo": {
            "fetch_fundamentals_for_ticker": lambda ticker, run_id: {"pe_ratio": 28.5},
        },
    })
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=registry,
        config_path=Path("config/orchestrator.yaml"),
    )
    orch.tick(budget_seconds=60)
    with sqlite3.connect(db.db_path) as c:
        n_queue = c.execute("SELECT COUNT(*) FROM fetch_queue").fetchone()[0]
    assert n_queue == 0


def test_tick_marks_completed_queue_row(tmp_path: Path, mocker):
    db = _fresh_db(tmp_path)
    db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=0)
    registry = _stub_registry(mocker, {
        "yahoo": {
            "fetch_fundamentals_for_ticker": lambda ticker, run_id: {"pe_ratio": 28.5},
        },
    })
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=registry,
        config_path=Path("config/orchestrator.yaml"),
    )
    orch.tick(budget_seconds=60)
    with sqlite3.connect(db.db_path) as c:
        status = c.execute("SELECT status FROM fetch_queue").fetchone()[0]
    assert status == "completed"


def test_tick_reclaims_orphaned_dispatched(tmp_path: Path, mocker):
    """A pre-existing 'dispatched' row stale >5min is reclaimed to 'pending'
    at the start of tick(), then dispatched normally."""
    db = _fresh_db(tmp_path)
    old_iso = (_dt.datetime.utcnow() - _dt.timedelta(minutes=10)).isoformat()
    with sqlite3.connect(db.db_path) as c:
        c.execute(
            "INSERT INTO fetch_queue (source, ticker, field, priority, created_at, "
            "dispatched_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("yahoo", "AAPL", "fundamentals", 0, old_iso, old_iso, "dispatched"),
        )
        c.commit()
    registry = _stub_registry(mocker, {
        "yahoo": {
            "fetch_fundamentals_for_ticker": lambda ticker, run_id: {"pe_ratio": 28.5},
        },
    })
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=registry,
        config_path=Path("config/orchestrator.yaml"),
    )
    summary = orch.tick(budget_seconds=60)
    assert summary["dispatched"] == 1


def test_tick_idempotent_within_budget_after_interruption(tmp_path: Path, mocker):
    """Two consecutive ticks with the same single pending row -> first tick
    dispatches, second tick is a clean no-op."""
    db = _fresh_db(tmp_path)
    db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=0)
    registry = _stub_registry(mocker, {
        "yahoo": {
            "fetch_fundamentals_for_ticker": lambda ticker, run_id: {"pe_ratio": 28.5},
        },
    })
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=registry,
        config_path=Path("config/orchestrator.yaml"),
    )
    s1 = orch.tick(budget_seconds=60)
    s2 = orch.tick(budget_seconds=60)
    assert s1["dispatched"] == 1
    assert s2["dispatched"] == 0


def test_tick_writes_quota_remaining_summary(tmp_path: Path, mocker):
    db = _fresh_db(tmp_path)
    registry = _stub_registry(mocker, {})
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=registry,
        config_path=Path("config/orchestrator.yaml"),
    )
    summary = orch.tick(budget_seconds=60)
    # quota_remaining must include polygon (one of the configured providers).
    assert "polygon" in summary["quota_remaining"]
    assert summary["quota_remaining"]["polygon"] == 5  # full window fresh.
```

- [ ] **Step 11: Run tests to verify they fail**

```
./venv/Scripts/python.exe -m pytest tests/common/orchestrator/test_orchestrator_tick.py -v
```

Expected: `ImportError` on `RateLimitedFetchOrchestrator`.

- [ ] **Step 12: Create `src/common/orchestrator/fetch_orchestrator.py`**

```python
"""RateLimitedFetchOrchestrator — the per-minute cron-friendly scheduler.

Composes:
  - `QuotaWindow` (sliding-window math)
  - `prioritize_queue` (per-(source, field) priority scoring)
  - DataSourceRegistry (the seven A.3 source adapters)
  - DatabaseManager (provider_call_log + provider_quota_state + fetch_queue)

Public surface:
  - `__init__(db, registry, config_path)`: load config; build QuotaWindow.
  - `tick(budget_seconds: int = 60) -> dict`: main entry. Idempotent across
    interruptions; no-op when there's no work.
  - `enqueue_staleness_sweep()`: internal — scans fetch_watermarks for
    items past their cadence and enqueues them.

CLI:
  python -m src.common.orchestrator.fetch_orchestrator tick --budget=60
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sqlite3
import sys
import time as _time
import traceback
from pathlib import Path
from typing import Any

import yaml

from src.common.database import DatabaseManager
from src.common.datasources.registry import DataSourceRegistry
from src.common.orchestrator.quota_window import (
    QuotaWindow,
    floor_to_second,
)
from src.common.orchestrator.queue_priority import prioritize_queue
from src.common.schemas import ProviderCallLog

_log = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "orchestrator.yaml"
)


class RateLimitedFetchOrchestrator:
    """Per-minute cron-friendly scheduler over the seven A.3 source adapters.

    Usage (cron invocation):
        orch = RateLimitedFetchOrchestrator(db, registry)
        summary = orch.tick(budget_seconds=60)
        print(json.dumps(summary))

    On each tick():
      1. Reclaim any orphaned 'dispatched' queue rows (previous tick crash).
      2. Enqueue work from stale fetch_watermarks.
      3. Loop: while budget remains and queue has pending items,
         dispatch the highest-priority entry whose source has quota.
      4. Per dispatch: invoke registry source method, log to
         provider_call_log, increment provider_quota_state, update
         fetch_queue status.
      5. Cleanup old quota rows once per tick.
      6. Return summary dict.

    The tick is structured as a `while` loop with a wall-clock deadline
    so an interrupted tick (Ctrl-C, OOM, machine reboot) leaves the DB
    in a recoverable state — the next tick's `reclaim_orphaned_dispatched`
    sweep cleans up any stragglers.
    """

    def __init__(
        self,
        db: DatabaseManager,
        registry: Any,
        config_path: Path | str | None = None,
    ):
        self.db = db
        self.registry = registry
        self.config_path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        with open(self.config_path, "r", encoding="utf-8") as f:
            self._cfg = yaml.safe_load(f)
        self._provider_quotas = self._cfg["provider_quotas"]
        self._priority_weights = self._cfg["priority_weights"]
        self._source_provider_map = self._cfg["source_provider_map"]
        self._refresh_cadence = self._cfg["refresh_cadence_hours"]
        self._orch_cfg = self._cfg["orchestrator"]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def tick(self, budget_seconds: int = 60) -> dict:
        """Main entry. Run for at most budget_seconds wall-clock.

        Returns a JSON-serializable summary:
            {
              "dispatched": <int>,
              "deferred":   <int>,
              "failed":     <int>,
              "quota_remaining": {source: int, ...},
              "elapsed_seconds": <float>,
            }
        """
        t0 = _time.monotonic()
        deadline = t0 + budget_seconds
        n_dispatched = 0
        n_deferred = 0
        n_failed = 0

        # 1. Reclaim orphans
        n_reclaimed = self.db.reclaim_orphaned_dispatched(
            stale_after_seconds=self._orch_cfg["stale_dispatch_seconds"]
        )
        if n_reclaimed:
            _log.info("reclaimed %s orphaned dispatched rows", n_reclaimed)

        # 2. Enqueue stale watermarks
        n_enqueued = self.enqueue_staleness_sweep(
            max_enqueue=self._orch_cfg["max_enqueue_per_tick"]
        )
        if n_enqueued:
            _log.info("enqueued %s stale watermark fetches", n_enqueued)

        # 3+4. Dispatch loop
        while _time.monotonic() < deadline:
            entry = self.db.dequeue_next()
            if entry is None:
                break  # queue exhausted
            # Quota check
            qw = self._build_quota_window()
            if not qw.available(entry.source, n=1):
                self.db.update_queue_status(entry.queue_id, "deferred")
                n_deferred += 1
                continue
            # Dispatch
            ok = self._dispatch_one(entry)
            if ok:
                n_dispatched += 1
            else:
                n_failed += 1

        # 5. Cleanup old quota rows once per tick.
        try:
            self.db.cleanup_stale_quota_rows(
                keep_days=self._orch_cfg["quota_retention_days"]
            )
        except sqlite3.Error as exc:
            _log.warning("cleanup_stale_quota_rows failed: %s", exc)

        # 6. Summary
        elapsed = _time.monotonic() - t0
        qw_final = self._build_quota_window()
        return {
            "dispatched":      n_dispatched,
            "deferred":        n_deferred,
            "failed":          n_failed,
            "quota_remaining": qw_final.remaining(),
            "elapsed_seconds": round(elapsed, 3),
        }

    def enqueue_staleness_sweep(self, max_enqueue: int = 200) -> int:
        """Scan fetch_watermarks; for any (source, ticker, field) older than
        the source's cadence, enqueue it. Returns count enqueued.

        Dedup is handled by `enqueue_fetch` — re-enqueueing an identical
        pending row is a no-op.
        """
        now = _dt.datetime.utcnow()
        n_added = 0
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT source, ticker, field, last_fetched_at
                FROM fetch_watermarks
                ORDER BY last_fetched_at ASC
                """,
            ).fetchall()
        for source, ticker, field, last_fetched_at in rows:
            if n_added >= max_enqueue:
                break
            cadence = self._cadence_hours(source, field)
            if cadence is None:
                continue  # No cadence config -> never auto-enqueue.
            try:
                last = _dt.datetime.fromisoformat(last_fetched_at)
            except (ValueError, TypeError):
                continue
            staleness_hours = (now - last).total_seconds() / 3600.0
            if staleness_hours < cadence:
                continue
            new_id = self.db.enqueue_fetch(
                source=source, ticker=ticker, field=field, priority=0,
            )
            if new_id is not None:
                n_added += 1
        return n_added

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_quota_window(self) -> QuotaWindow:
        return QuotaWindow(
            provider_quotas=self._provider_quotas,
            source_provider_map=self._source_provider_map,
            usage_lookup=self.db.get_quota_calls_in_window,
            now=_dt.datetime.utcnow(),
        )

    def _cadence_hours(self, source: str, field: str) -> int | None:
        src_cfg = self._refresh_cadence.get(source)
        if not src_cfg:
            return None
        return src_cfg.get(field)

    def _dispatch_one(self, entry) -> bool:
        """Invoke the registry source method for one queue entry. Logs to
        provider_call_log and increments provider_quota_state.

        Returns True on success, False on failure.
        """
        source_obj = self._find_source(entry.source)
        started_at = _dt.datetime.utcnow().isoformat()
        if source_obj is None:
            self._log_call(
                entry, started_at=started_at,
                finished_at=started_at, status="failed",
                error_message=f"source {entry.source!r} not in registry",
                http_status=None, bytes_returned=0,
            )
            self.db.update_queue_status(entry.queue_id, "failed")
            return False

        # Record quota use BEFORE the fetch (conservative — if the fetch
        # crashes the call still counted against the provider's window).
        self._record_quota_use(entry.source)

        run_id = f"orch-{started_at}"
        try:
            method = self._source_method_for_field(source_obj, entry.field)
            if method is None:
                raise RuntimeError(
                    f"source {entry.source} has no fetch method for "
                    f"field {entry.field!r}"
                )
            # The method signature differs per category; we pass ticker
            # + run_id for the ticker-scoped methods and just run_id for
            # universe / macro. The dispatcher figures this out.
            if entry.field in ("universe",):
                method(run_id)
            elif entry.field.startswith("macro"):
                # FRED accepts (series_ids: list[str], run_id)
                series_id = entry.field.split(":", 1)[1] if ":" in entry.field else entry.field
                method([series_id], run_id)
            else:
                # ticker-scoped
                method(entry.ticker, run_id)
            finished_at = _dt.datetime.utcnow().isoformat()
            self._log_call(
                entry, started_at=started_at,
                finished_at=finished_at, status="ok",
                error_message=None,
                http_status=200, bytes_returned=0,
            )
            self.db.update_queue_status(entry.queue_id, "completed")
            return True
        except Exception as exc:  # noqa: BLE001 — must isolate per dispatch
            finished_at = _dt.datetime.utcnow().isoformat()
            msg = f"{type(exc).__name__}: {exc}"
            self._log_call(
                entry, started_at=started_at,
                finished_at=finished_at, status="failed",
                error_message=msg, http_status=None, bytes_returned=0,
            )
            self.db.update_queue_status(entry.queue_id, "failed")
            _log.warning(
                "dispatch failed source=%s ticker=%s field=%s: %s\n%s",
                entry.source, entry.ticker, entry.field, msg,
                traceback.format_exc(),
            )
            return False

    def _find_source(self, name: str):
        for s in self.registry.enabled_sources():
            if s.name == name:
                return s
        return None

    def _source_method_for_field(self, source_obj, field: str):
        """Pick which fetch_* method on the source handles this field.

        v1 mapping (rule of thumb):
          - field == "universe" -> fetch_universe
          - field.startswith("macro") -> fetch_macro_series
          - field in {"filings", "insider"} -> fetch_filings_for_ticker
          - everything else -> fetch_fundamentals_for_ticker

        This is a simplification; A.3.10 may refine to a per-field
        registry once the materialization layer is wired.
        """
        if field == "universe":
            return getattr(source_obj, "fetch_universe", None)
        if field.startswith("macro"):
            return getattr(source_obj, "fetch_macro_series", None)
        if field in ("filings", "insider"):
            return getattr(source_obj, "fetch_filings_for_ticker", None)
        return getattr(source_obj, "fetch_fundamentals_for_ticker", None)

    def _record_quota_use(self, source: str) -> None:
        providers = self._source_provider_map.get(source, [])
        window_start = floor_to_second(_dt.datetime.utcnow()).isoformat()
        for provider in providers:
            self.db.record_quota_use(
                source=source, provider=provider,
                window_start=window_start, n=1,
            )

    def _log_call(
        self, entry, started_at, finished_at, status,
        error_message, http_status, bytes_returned,
    ) -> None:
        try:
            row = ProviderCallLog(
                source=entry.source,
                ticker=entry.ticker,
                field=entry.field,
                started_at=started_at,
                finished_at=finished_at,
                status=status,
                bytes_returned=bytes_returned,
                error_message=error_message,
                http_status=http_status,
            )
            self.db.insert_provider_call_log([row])
        except Exception as exc:  # noqa: BLE001 — logging must not crash dispatch
            _log.error("failed to write provider_call_log: %s", exc)


# ----------------------------------------------------------------------
# CLI entrypoint:  python -m src.common.orchestrator.fetch_orchestrator tick --budget=60
# ----------------------------------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.common.orchestrator.fetch_orchestrator",
        description="Rate-Limited Fetch Orchestrator (A.3.7.5).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    p_tick = sub.add_parser("tick", help="Run a single orchestrator tick.")
    p_tick.add_argument("--budget", type=int, default=60,
                        help="Wall-clock budget in seconds (default 60).")
    p_tick.add_argument("--db", type=str, default=None,
                        help="Path to SQLite DB (default: project default).")
    p_tick.add_argument("--config", type=str, default=None,
                        help="Path to orchestrator.yaml (default: project default).")
    return p


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_argparser().parse_args(argv)
    if args.cmd == "tick":
        db = DatabaseManager(db_path=args.db) if args.db else DatabaseManager()
        # Ensure migrations run -> v10. Idempotent.
        db.migrate_to_v10()
        registry = DataSourceRegistry()
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=registry,
            config_path=args.config,
        )
        summary = orch.tick(budget_seconds=args.budget)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(_main())
```

- [ ] **Step 13: Run all orchestrator tests**

```
./venv/Scripts/python.exe -m pytest tests/common/orchestrator/ -v
```

Expected: ~31 PASS (13 quota_window + 8 queue_priority + 10 tick — ±2 acceptable).

- [ ] **Step 14: Commit**

```
git add src/common/orchestrator/__init__.py src/common/orchestrator/quota_window.py src/common/orchestrator/queue_priority.py src/common/orchestrator/fetch_orchestrator.py tests/common/orchestrator/__init__.py tests/common/orchestrator/test_quota_window.py tests/common/orchestrator/test_queue_priority.py tests/common/orchestrator/test_orchestrator_tick.py
git commit -m "feat(orchestrator): A.3.7.5 - RateLimitedFetchOrchestrator with sliding-window quotas + staleness queue + cron tick()"
```

---

## Task 4: CLI hook + integration acceptance test

**Files:**
- Create: `tests/common/orchestrator/test_integration_a3_7_5.py`

(The CLI hook itself was added inline with Task 3 Step 12 — `_main()` and the `__main__` guard at the bottom of `fetch_orchestrator.py`. Task 4 is the end-to-end integration test that exercises CLI + tick + DB + mocked registry as one composition.)

- [ ] **Step 1: Verify CLI argparse wiring**

Smoke-test the CLI's argparse before testing the full flow. From the repository root:

```
./venv/Scripts/python.exe -m src.common.orchestrator.fetch_orchestrator tick --help
```

Expected: argparse prints the help block describing `--budget`, `--db`, `--config`. No exceptions.

- [ ] **Step 2: Write the integration acceptance test**

Create `tests/common/orchestrator/test_integration_a3_7_5.py`:

```python
"""Phase A.3.7.5 end-to-end acceptance test.

Mocks `DataSourceRegistry.enabled_sources()` at the import boundary so
no actual sources are instantiated (which would attempt real HTTP on
init for some adapters). Exercises tick() with:
  - mix of single-provider and multi-provider sources
  - mix of quota-saturated and quota-available providers
  - mix of successful and failing source methods
  - budget enforcement
  - call_log + quota_state + queue rows correctly written

This file is the canonical "does the whole orchestrator work end-to-end
with the existing 7-source registry shape" test.

Note: marked with @pytest.mark.integration so it is excluded from the
default `pytest -m "not integration"` run. Run explicitly with
`pytest tests/common/orchestrator/test_integration_a3_7_5.py`.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.common.database import DatabaseManager
from src.common.orchestrator import RateLimitedFetchOrchestrator


pytestmark = pytest.mark.integration


def _make_source(name, **method_overrides):
    """Build a stub source with the given fetch methods overridden."""
    ns = SimpleNamespace(name=name, cadence="daily", provides=set())
    # Default fetch methods raise NotImplementedError (matches BaseDataSource).
    def _raise(*args, **kwargs):
        raise NotImplementedError(f"{name} stub: not implemented in test")
    ns.fetch_universe = _raise
    ns.fetch_fundamentals_for_ticker = _raise
    ns.fetch_filings_for_ticker = _raise
    ns.fetch_macro_series = _raise
    for mname, fn in method_overrides.items():
        setattr(ns, mname, fn)
    return ns


def _stub_registry(sources):
    return SimpleNamespace(enabled_sources=lambda: list(sources))


@pytest.fixture
def fresh_db(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(db_path=str(tmp_path / "integration.db"))
    db.migrate_to_v10()
    return db


# ---------- Tests --------------------------------------------------------

def test_a3_7_5_end_to_end_mixed_sources(fresh_db: DatabaseManager):
    """One Yahoo + one EDGAR + one OpenBB enqueued. Yahoo + EDGAR have
    quota; OpenBB does too. All three should dispatch in one tick."""
    db = fresh_db
    db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=0)
    db.enqueue_fetch(source="edgar", ticker="AAPL", field="insider", priority=0)
    db.enqueue_fetch(source="openbb", ticker="AAPL", field="multi_provider", priority=0)

    sources = [
        _make_source("yahoo",
            fetch_fundamentals_for_ticker=lambda t, run_id: {"pe_ratio": 28.5}),
        _make_source("edgar",
            fetch_filings_for_ticker=lambda t, run_id: [{"form": "4"}]),
        _make_source("openbb",
            fetch_fundamentals_for_ticker=lambda t, run_id: {"market_cap": 3e12}),
    ]
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=_stub_registry(sources),
        config_path=Path("config/orchestrator.yaml"),
    )
    summary = orch.tick(budget_seconds=60)
    assert summary["dispatched"] == 3
    assert summary["failed"] == 0
    assert summary["deferred"] == 0

    # call_log has three OK rows.
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT source, status FROM provider_call_log ORDER BY source"
        ).fetchall()
    assert rows == [("edgar", "ok"), ("openbb", "ok"), ("yahoo", "ok")]

    # quota_state has rows for yahoo, sec_edgar (edgar -> sec_edgar provider),
    # and all three openbb providers.
    with sqlite3.connect(db.db_path) as c:
        providers = sorted({r[0] for r in c.execute(
            "SELECT provider FROM provider_quota_state"
        )})
    assert "yahoo" in providers
    assert "sec_edgar" in providers
    assert "fmp_stable" in providers
    assert "polygon" in providers
    assert "tiingo" in providers


def test_a3_7_5_polygon_saturation_defers_openbb(fresh_db: DatabaseManager):
    """Pre-saturate the polygon quota (5/min) so any openbb dispatch must
    defer until next tick."""
    db = fresh_db
    now_floor = _dt.datetime.utcnow().replace(microsecond=0).isoformat()
    db.record_quota_use(source="openbb", provider="polygon",
                        window_start=now_floor, n=5)
    db.enqueue_fetch(source="openbb", ticker="AAPL", field="multi_provider", priority=0)

    sources = [_make_source("openbb",
        fetch_fundamentals_for_ticker=lambda t, run_id: {"market_cap": 3e12})]
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=_stub_registry(sources),
        config_path=Path("config/orchestrator.yaml"),
    )
    summary = orch.tick(budget_seconds=60)
    assert summary["dispatched"] == 0
    assert summary["deferred"] == 1

    # Queue row went to 'deferred'.
    with sqlite3.connect(db.db_path) as c:
        status = c.execute(
            "SELECT status FROM fetch_queue WHERE source='openbb'"
        ).fetchone()[0]
    assert status == "deferred"


def test_a3_7_5_partial_failure_handling(fresh_db: DatabaseManager):
    """Two sources enqueued; one's fetch method raises. The orchestrator
    must keep going (no exception leaks to caller) and surface the failure
    in the summary."""
    db = fresh_db
    db.enqueue_fetch(source="yahoo", ticker="AAPL", field="fundamentals", priority=10)
    db.enqueue_fetch(source="edgar", ticker="AAPL", field="insider", priority=0)

    def yahoo_ok(t, run_id):
        return {"pe_ratio": 28.5}

    def edgar_boom(t, run_id):
        raise RuntimeError("simulated 500 from EDGAR")

    sources = [
        _make_source("yahoo", fetch_fundamentals_for_ticker=yahoo_ok),
        _make_source("edgar", fetch_filings_for_ticker=edgar_boom),
    ]
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=_stub_registry(sources),
        config_path=Path("config/orchestrator.yaml"),
    )
    summary = orch.tick(budget_seconds=60)
    assert summary["dispatched"] == 1
    assert summary["failed"] == 1
    assert summary["deferred"] == 0
    # call_log records the failure with the error message.
    with sqlite3.connect(db.db_path) as c:
        row = c.execute(
            "SELECT status, error_message FROM provider_call_log "
            "WHERE source='edgar'"
        ).fetchone()
    assert row[0] == "failed"
    assert "simulated 500 from EDGAR" in row[1]


def test_a3_7_5_budget_enforcement_carries_remainder(fresh_db: DatabaseManager):
    """Enqueue 5 slow fetches. With 1s budget and 0.4s per fetch, ~2 should
    dispatch; the remaining 3 stay pending for the next tick."""
    import time
    db = fresh_db
    for i, t in enumerate(["AAPL", "MSFT", "GOOG", "AMZN", "META"]):
        db.enqueue_fetch(source="yahoo", ticker=t, field="fundamentals",
                         priority=10 - i)

    def slow_fetch(ticker, run_id):
        time.sleep(0.4)
        return {"pe_ratio": 28.0}

    sources = [_make_source("yahoo",
        fetch_fundamentals_for_ticker=slow_fetch)]
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=_stub_registry(sources),
        config_path=Path("config/orchestrator.yaml"),
    )
    s1 = orch.tick(budget_seconds=1)
    assert 1 <= s1["dispatched"] <= 3

    with sqlite3.connect(db.db_path) as c:
        n_pending = c.execute(
            "SELECT COUNT(*) FROM fetch_queue WHERE status='pending'"
        ).fetchone()[0]
    assert n_pending == 5 - s1["dispatched"]


def test_a3_7_5_consecutive_ticks_accrete_data(fresh_db: DatabaseManager):
    """Five Yahoo enqueues, no quota pressure, fast fetches; two consecutive
    ticks should drain the queue completely."""
    db = fresh_db
    for t in ["AAPL", "MSFT", "GOOG", "AMZN", "META"]:
        db.enqueue_fetch(source="yahoo", ticker=t, field="fundamentals", priority=0)

    sources = [_make_source("yahoo",
        fetch_fundamentals_for_ticker=lambda t, run_id: {"pe_ratio": 28.0})]
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=_stub_registry(sources),
        config_path=Path("config/orchestrator.yaml"),
    )
    s1 = orch.tick(budget_seconds=60)
    s2 = orch.tick(budget_seconds=60)
    total_dispatched = s1["dispatched"] + s2["dispatched"]
    assert total_dispatched == 5
    with sqlite3.connect(db.db_path) as c:
        n_completed = c.execute(
            "SELECT COUNT(*) FROM fetch_queue WHERE status='completed'"
        ).fetchone()[0]
    assert n_completed == 5


def test_a3_7_5_cron_no_op_tick(fresh_db: DatabaseManager):
    """Empty DB + empty queue + no stale watermarks: tick must complete
    cleanly with zero dispatched, zero failures, no exceptions. This is
    the cron-friendly invariant."""
    db = fresh_db
    sources = [_make_source("yahoo"),
               _make_source("edgar"),
               _make_source("openbb")]
    orch = RateLimitedFetchOrchestrator(
        db=db, registry=_stub_registry(sources),
        config_path=Path("config/orchestrator.yaml"),
    )
    summary = orch.tick(budget_seconds=60)
    assert summary["dispatched"] == 0
    assert summary["deferred"] == 0
    assert summary["failed"] == 0
    assert summary["elapsed_seconds"] < 60
```

- [ ] **Step 3: Run the integration test**

```
./venv/Scripts/python.exe -m pytest tests/common/orchestrator/test_integration_a3_7_5.py -v -m integration
```

Expected: 6 PASS.

- [ ] **Step 4: Run the full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" -v 2>&1 | tail -5
```

Expected: ~554 passed (475 baseline + ~20 schema + ~26 database + ~13 quota_window + ~8 queue_priority + ~11 tick — totals approximate; ±2 tolerance).

- [ ] **Step 5: Smoke-test the CLI hook end-to-end**

```
./venv/Scripts/python.exe -m src.common.orchestrator.fetch_orchestrator tick --budget=5 --db=data/_cli_smoke.db
```

Expected: Prints a JSON summary like:

```json
{
  "dispatched": 0,
  "deferred": 0,
  "failed": 0,
  "quota_remaining": {
    "polygon": 5,
    ...
  },
  "elapsed_seconds": 0.X
}
```

Cleanup:

```
./venv/Scripts/python.exe -c "import pathlib; [pathlib.Path(p).unlink(missing_ok=True) for p in ['data/_cli_smoke.db','data/_cli_smoke.db-wal','data/_cli_smoke.db-shm']]"
```

- [ ] **Step 6: Commit**

```
git add tests/common/orchestrator/test_integration_a3_7_5.py
git commit -m "test: A.3.7.5 integration acceptance - end-to-end orchestrator tick with mixed sources + quota saturation + partial failure"
```

---

## Task 5: Update the build plan

**Files:**
- Modify: the build plan

- [ ] **Step 1: Locate the A.3 row in §5.1.0**

Grep the build plan for `**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 + A.3.6 + A.3.7 shipped 2026-05-21**` to find the existing marker line (added by the A.3.7 sub-phase Task 5).

- [ ] **Step 2: Update the A.3 row's shipped marker**

Use `Edit` to replace the substring `**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 + A.3.6 + A.3.7 shipped 2026-05-21**` with `**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 + A.3.6 + A.3.7 + A.3.7.5 shipped 2026-05-21**`, add a link to the A.3.7.5 plan file in the plan-link parenthetical, and bump the schema marker from v9 to v10.

Concretely, locate this fragment:

```
**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 + A.3.6 + A.3.7 shipped 2026-05-21** (plans: [A.3.1](docs/design/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/design/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md), [A.3.3](docs/design/plans/2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md), [A.3.4](docs/design/plans/2026-05-21-phase-a3-sub04-edgar-insider-and-filings.md), [A.3.5](docs/design/plans/2026-05-21-phase-a3-sub05-fred-finra.md), [A.3.6](docs/design/plans/2026-05-21-phase-a3-sub06-stockanalysis-ratios.md), [A.3.7](docs/design/plans/2026-05-21-phase-a3-sub07-openbb-router.md)): schema v9
```

Replace with:

```
**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 + A.3.6 + A.3.7 + A.3.7.5 shipped 2026-05-21** (plans: [A.3.1](docs/design/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/design/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md), [A.3.3](docs/design/plans/2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md), [A.3.4](docs/design/plans/2026-05-21-phase-a3-sub04-edgar-insider-and-filings.md), [A.3.5](docs/design/plans/2026-05-21-phase-a3-sub05-fred-finra.md), [A.3.6](docs/design/plans/2026-05-21-phase-a3-sub06-stockanalysis-ratios.md), [A.3.7](docs/design/plans/2026-05-21-phase-a3-sub07-openbb-router.md), [A.3.7.5](docs/design/plans/2026-05-21-phase-a3-sub07_5-rate-limited-orchestrator.md)): schema v10
```

In the same A.3 row description, append the A.3.7.5 fragment after the existing A.3.7 fragment:

```
 + RateLimitedFetchOrchestrator with per-provider sliding-window quotas (Polygon 5/min, FMP-stable 250/day, Tiingo 1000/day, FRED 120/min, Yahoo 2000/hr, FINRA 100/day, SEC EDGAR 10/s, stockanalysis 60/min) + staleness-prioritized queue (CMP insider weight=100, Yahoo revisions=60, FRED macro=5) + provider_call_log audit + cron-friendly tick(budget_seconds) entry point per 2026-05-21 directive.
```

- [ ] **Step 3: Commit**

```
git add <build-plan>
git commit -m "docs: mark A.3.7.5 shipped - Rate-Limited Fetch Orchestrator (schema v10)"
```

---

## Phase A.3.7.5 — Definition of Done

- [ ] `./venv/Scripts/python.exe -m pytest -m "not integration"` shows ~554 tests passing (±2 tolerance).
- [ ] `migrate_to_v10()` produces `schema_version == 10`.
- [ ] `provider_call_log` table exists with PK `call_id INTEGER PRIMARY KEY AUTOINCREMENT` and columns matching the Pydantic model.
- [ ] `provider_quota_state` table exists with PK `(source, provider, window_start)`; PK conflict raises `IntegrityError`.
- [ ] `fetch_queue` table exists with PK `queue_id INTEGER PRIMARY KEY AUTOINCREMENT` and status column constrained to {pending, dispatched, completed, failed, deferred}.
- [ ] `insert_provider_call_log()` writes rows (no INSERT OR IGNORE — append-only audit).
- [ ] `enqueue_fetch()` deduplicates identical pending rows.
- [ ] `dequeue_next()` atomically marks the highest-priority pending row 'dispatched' under a `BEGIN IMMEDIATE` lock.
- [ ] `update_queue_status()` transitions terminal states with `completed_at` set.
- [ ] `reclaim_orphaned_dispatched()` resets stale 'dispatched' rows to 'pending'.
- [ ] `record_quota_use()` increments via `ON CONFLICT ... DO UPDATE`.
- [ ] `get_quota_calls_in_window()` correctly aggregates with strict `>` predicate.
- [ ] `cleanup_stale_quota_rows()` deletes rows older than 7 days.
- [ ] `ProviderCallLog.status` is constrained to `{ok, failed, deferred}` Literal.
- [ ] `ProviderCallLog.ticker` accepts None and uppercases non-None values.
- [ ] `FetchQueueEntry.status` is constrained to the 5-state Literal.
- [ ] `FetchQueueEntry.priority` accepts negative values.
- [ ] `QuotaWindow.available(source, n)` returns False for unknown source (defensive).
- [ ] `QuotaWindow.available(source, n)` requires ALL providers in `source_provider_map[source]` to have headroom (multi-provider AND-semantics).
- [ ] `QuotaWindow.remaining()` returns min-across-providers for multi-provider sources.
- [ ] `since_iso_for_window` uses strict `>` boundary (boundary rows excluded).
- [ ] `score_entry(source, field, staleness_hours, priority, weights_table)` formula is `staleness_hours * weight + priority`.
- [ ] `score_entry` falls back to weight=1 (not 0) when (source, field) is missing from weights_table.
- [ ] `prioritize_queue()` is stable on tie.
- [ ] `RateLimitedFetchOrchestrator.tick()` reclaims orphans BEFORE dispatch loop.
- [ ] `RateLimitedFetchOrchestrator.tick()` returns `{dispatched, deferred, failed, quota_remaining, elapsed_seconds}` summary.
- [ ] `RateLimitedFetchOrchestrator.tick()` is a clean no-op when there's no work (cron-friendly).
- [ ] `RateLimitedFetchOrchestrator.tick()` records quota use BEFORE invoking the source method (conservative — crashes still count).
- [ ] `RateLimitedFetchOrchestrator.tick()` isolates per-dispatch exceptions; one failing source doesn't abort the tick.
- [ ] `RateLimitedFetchOrchestrator.tick()` respects `budget_seconds`; remainder stays pending for next tick.
- [ ] `RateLimitedFetchOrchestrator.enqueue_staleness_sweep()` reads cadence from `config/orchestrator.yaml` and skips fresh watermarks.
- [ ] CLI hook `python -m src.common.orchestrator.fetch_orchestrator tick --budget=N` runs, prints JSON summary, returns 0.
- [ ] No new runtime dependencies added to `requirements.txt` (only `pyyaml` which was already pinned).
- [ ] WAL mode enabled by `migrate_to_v10()`; sidecar `*-wal` / `*-shm` files do NOT leak into git (verified by existing `.gitignore` exclusion of `data/`).
- [ ] `config/orchestrator.yaml` exists with the 8-provider quota table + 7-source priority weights + source→provider map + refresh cadences + orchestrator runtime defaults.
- [ ] No A.1 / A.2 / A.3.1 / A.3.2 / A.3.3 / A.3.4 / A.3.5 / A.3.6 / A.3.7 regressions (`health_check`, all prior `raw_*` tables, watermarks, raw_openbb).
- [ ] The build plan marks A.3.7.5 shipped (schema v10).
- [ ] Git log shows ~5 task commits.

---

## Self-review

**Spec coverage:**

| A.3 spec section | A.3.7.5 task |
|---|---|
| §15 row A.3.7.5: per-provider quotas | Task 2 (`config/orchestrator.yaml` provider_quotas block) + Task 3.1 (`QuotaWindow.available`) |
| §15 row A.3.7.5: priority queue of (ticker, field) pairs sorted by staleness | Task 2 (`fetch_queue` table) + Task 3.2 (`prioritize_queue` + `score_entry`) + Task 3.3 (`enqueue_staleness_sweep`) |
| §15 row A.3.7.5: per-minute call history in `provider_call_log` | Task 2 (`provider_call_log` DDL + `insert_provider_call_log`) + Task 3.3 (`_log_call` per dispatch) |
| §15 row A.3.7.5: operator can run "do whatever you can in N seconds with M call budget" | Task 3.3 (`tick(budget_seconds)`) + Task 4 (CLI `--budget` flag) |
| §15 row A.3.7.5: cron / Task Scheduler dispatch every minute | Task 3.3 (no-op tick path; `reclaim_orphaned_dispatched` for crash safety; CLI hook) + Task 4 (integration test `test_a3_7_5_cron_no_op_tick`) |
| §15 row A.3.7.5: incremental data accretion enabling continuous in-house agents | Task 4 integration test `test_a3_7_5_consecutive_ticks_accrete_data` |
| 2026-05-21 directive: maintain an actionable last-call log | `provider_call_log` (Task 2) — every call, append-only, queryable |
| 2026-05-21 directive: run every minute, accrete data over time | `tick(budget_seconds=60)` + cron compatibility (Task 3.3 + Task 4) |
| 2026-05-21 directive: updatable, expansive data aggregator | The staleness sweep + queue + budget loop is the substrate (Task 3.3) |
| 2026-05-21 directive: in-house agents continuously managing data | The orchestrator IS the agent's hands; a future agent layer (A.3.10+) consumes `tick()` as its action verb |

**Out of scope** (explicitly deferred):

- Multi-process / distributed orchestrator. v1 assumes one process; horizontal scale requires per-row locking on `dequeue_next()` and is a clean follow-up sub-phase.
- Heavy task-queue libraries (Celery / RQ / APScheduler). The SQLite-table queue is good enough for v1's <10k-pending-item scale.
- Smart per-field provider selection inside OpenBB. v1 treats openbb as one atomic 3-provider operation; A.3.10 may refine.
- Automatic 429-backoff. v1 lets quota saturation naturally throttle; smarter backoff is a follow-up.
- Web dashboard / TUI. All state is in three SQL tables — the operator can `SELECT` against them today.
- Cross-tick budget carryover. Each tick is independent.
- UI for per-ticker priority overrides.
- Live network in any test. Integration tests mock the registry; A.5 acceptance does live runs.

**Placeholder scan:** No "TBD", "TODO", or "implement later" in any code block. Every step contains full source. The one judgment call is `_source_method_for_field` — its "rule of thumb" mapping covers the seven existing source.fetch_* method shapes; if A.3.10 refines per-field method dispatch, that's a one-method change in `fetch_orchestrator.py`.

**Pydantic v2 gotchas avoided:**

- `ProviderCallLog.ticker` is `Optional[str] = None` with a `@field_validator(mode="before")` that returns None on None and uppercases otherwise. Same pattern as A.3.5 (`RawFredObservation` has no ticker; A.3.5's row models all use this Optional ticker shape).
- `ProviderCallLog.status` and `FetchQueueEntry.status` use Pydantic `Literal[...]` rather than `str` + custom validator. Catches typos at validation time.
- `FetchQueueEntry.priority` is `int = 0` with no constraint — negative values are intentional (deprioritization).
- The `_CallStatus` and `_QueueStatus` Literal aliases are module-private (underscore prefix) so they aren't part of the public `schemas` API surface; only the three row models are exported.
- `call_id` / `queue_id` are `Optional[int] = None` — autoincremented by SQLite at INSERT time; the model accepts absence on construction.

**Concurrency model — explicit re-statement (load-bearing):**

The orchestrator's correctness model relies on three concurrency invariants:

1. **Single-process invariant.** The operator runs `tick()` from one cron / Task Scheduler job; running two simultaneously can race on `dequeue_next` because the `BEGIN IMMEDIATE` lock is released between the SELECT and the next iteration's SELECT — within a single dequeue call the lock holds, but two parallel orchestrators could both pass the lock-acquire phase one after the other and each claim a separate row, which is *fine* (no row double-claim), but they would *interfere on quota accounting* (both think they have full quota headroom). For v1 we accept this and document the constraint.
2. **Crash-safe resumability via the `dispatched` orphan-reclaim sweep.** If `tick()` crashes mid-dispatch (machine reboot, OOM, Ctrl-C), the row sits in `status='dispatched'` indefinitely. The next tick's `reclaim_orphaned_dispatched(stale_after_seconds=300)` resets it to `pending`. The 5-minute window is generous — the longest legitimate dispatch is a 12-second Polygon call inside an OpenBB multi-route, so a row that's been `dispatched` for >5min IS a crash.
3. **Quota under-count vs over-count trade-off.** `_record_quota_use` is called BEFORE the source method invokes its HTTP call. Rationale: if the HTTP call succeeds, the quota IS consumed regardless of whether we update the table after. If the HTTP call fails (crash, timeout), recording quota use before means we *over-count* by 1 (we count a call that may not have hit the provider). The alternative — record after — would *under-count* on crash (we don't record a call we already made). Over-count is the conservative direction: it makes us back off sooner than necessary, never harder. Documented; if A.3.10 surfaces a need to tighten the accounting, a "best-effort post-record" can be added without changing the API.

**Quota window math — explicit re-statement (load-bearing):**

The sliding-window aggregate `SUM(calls_used) WHERE window_start > since_iso` has one subtle property worth highlighting: it counts EVERY bucket whose window_start is strictly greater than the cutoff, regardless of how many seconds those buckets are apart. If at second 0 we made 5 Polygon calls all in the same second (window_start='...T08:00:00'), the row has calls_used=5 and the next call at second 1 will see 5 used — full saturation. If those 5 calls were spread across seconds 0–4, they would write 5 rows (calls_used=1 each); the aggregate at second 30 sees `SUM(1,1,1,1,1) = 5` — still full saturation. The semantics are correct under both spreading patterns because the bucket *granularity* doesn't affect the *sum within the window*. The bucket granularity only affects how cleanly buckets age out of the window — finer granularity (per-second) ages out one call at a time; coarser granularity (per-minute) would age out all of a minute's calls at once. We use per-second because it matches both the highest-resolution quota (SEC EDGAR's 10/s) and the natural floor of `datetime.utcnow().replace(microsecond=0)`.

**Priority weight calibration — explicit re-statement (load-bearing):**

The weight table reflects the spec §7 alpha-contribution ranking but is **expected to be re-tuned during A.3.10's acceptance phase**. The v1 values:

- `edgar.insider = 100` (highest — CMP insider trades contribute ~82 bps/mo per spec §7, the alpha leader).
- `yahoo.revisions = 60` (second tier — analyst revision deltas).
- `finra.short_interest = 50` (mid tier — biweekly publishes, lower-velocity signal).
- `edgar.fundamentals = 30`, `yahoo.fundamentals = 25` (steady fundamentals, not time-sensitive).
- `openbb.multi_provider = 25` (cross-vendor validation, similar to fundamentals).
- `stockanalysis.ratios = 20`, `edgar.filings = 20` (lower velocity).
- `yahoo.historical_price = 15` (high data volume, low per-row alpha).
- `finviz.universe = 10` (universe definition only).
- `fred.macro = 5` (slowest-moving — daily/weekly macro series).

The relative ordering is what matters for v1; absolute values can shift by 10x without changing dispatch order. Once A.3.10's equivalence harness produces per-field information-coefficient data, the weights can be re-tuned from empirical alpha rather than spec estimates.

**Cron compatibility — explicit re-statement (load-bearing):**

A.3.7.5's success criterion as a *cron-friendly* scheduler is:

1. **Idempotent across invocations.** Two ticks in a row with no new work both return `{dispatched: 0, ...}` without error. Tested in `test_a3_7_5_cron_no_op_tick`.
2. **Bounded wall-clock.** `tick(budget_seconds=60)` returns within 60 seconds (modulo small overrun while a fetch-in-flight finishes — we don't kill mid-fetch). Tested in `test_a3_7_5_budget_enforcement_carries_remainder`.
3. **Graceful failure isolation.** A crashing source doesn't take down the tick. Tested in `test_a3_7_5_partial_failure_handling`.
4. **No external state required.** WAL mode is auto-enabled by `migrate_to_v10()`; the DB is the only persistent state. A cron job that runs `python -m src.common.orchestrator.fetch_orchestrator tick --budget=60` and pipes the JSON output to a log file is a complete operational story.

**Architecture risk: SQLite write contention**

WAL mode keeps readers unblocked, but per-call writes to `provider_call_log` + `provider_quota_state` + `fetch_queue` are all serialized through a single SQLite writer lock. At full-throttle dispatch (Yahoo 2000/hr = ~0.55 calls/sec), the write rate is ~3 short transactions per dispatched call (call_log + quota_state + queue_status), or ~1.7 writes/sec sustained. SQLite handles this comfortably (>1000 writes/sec on commodity hardware). The risk surfaces only if a future sub-phase parallelizes dispatch across threads, which would multiply the contention. **Mitigation if/when this happens**: batch the writes into a single transaction at the end of each dispatch, or use a background-flush pattern. v1 doesn't need this; flagged for revisit at A.3.10.

**Architecture risk: clock skew between cron schedule and quota windows**

If the machine clock drifts, the orchestrator's `datetime.utcnow()` calls (used for both quota window_start and queue dispatched_at) become inconsistent with provider rate-limit windows. Practical impact:

- Forward drift (machine clock ahead of real time): we'd think the quota window slid out sooner than it did and dispatch into a still-saturated provider window, getting 429'd. The 429 would surface as a failed call_log row, the queue row would go to 'failed', the next staleness sweep would re-enqueue, and the next tick (with the still-saturated quota state) would defer. **Self-healing.**
- Backward drift (machine clock behind): we'd think the window hasn't slid yet and over-conservatively defer when we could dispatch. **No data loss, only delayed throughput.**

Both modes recover automatically. The orchestrator is robust to ±60s of clock skew without intervention.

**Architecture risk: queue table growth**

`fetch_queue` rows in `completed` / `failed` / `deferred` states accumulate forever in v1. At full-throttle (~3 dispatched/sec × 86400 sec = 260k rows/day), the table grows ~95M rows/year. SQLite handles this but query performance on the `idx_queue_status` index degrades as the index spans more pages. **Mitigation (deferred to A.3.10):** add a `cleanup_terminal_queue_rows(keep_days=30)` helper analogous to `cleanup_stale_quota_rows`. v1 ships without it because the queue's read pattern is "WHERE status='pending'" which the index serves efficiently regardless of terminal-row count — only writes get expensive, and not measurably so under v1's expected load.

**Architecture risk: `_source_method_for_field` mapping fragility**

The current rule-of-thumb mapping (`universe` → `fetch_universe`, `macro:*` → `fetch_macro_series`, `filings|insider` → `fetch_filings_for_ticker`, else → `fetch_fundamentals_for_ticker`) covers the seven existing source method shapes. If A.3.8+ adds a new method shape (e.g., `fetch_news_for_ticker`), the dispatcher needs an entry. **Mitigation:** when a method-shape changes, update `_source_method_for_field` and add a test case. The fragility is bounded — only seven sources to track, and each is exercised in the integration test.

**Architecture risk: budget under-spend vs over-spend**

If the budget elapses mid-fetch, the in-flight HTTP call completes (we don't abort mid-call), so the tick may overrun `budget_seconds` by up to the longest single-fetch latency (~12s for Polygon). **Mitigation considered, deliberately deferred:** wrapping the fetch in `concurrent.futures.ThreadPoolExecutor.submit(...).result(timeout=...)` would let us cancel mid-call, but the cancellation semantics for `requests`-based fetches are not clean (the thread would still hold the connection). v1 accepts the ±12s overrun. The cron job's 60s tick budget + 12s max-overrun is well under the 60s cron cadence.

**Test coverage shape:**

| Layer | Unit tests | Integration tests |
|---|---|---|
| Schemas (3 Pydantic models) | ~20 | — |
| DB migration + 8 helpers | ~26 | — |
| `QuotaWindow` (pure sliding-window math) | ~13 | — |
| `prioritize_queue` + `score_entry` (pure priority scoring) | ~8 | — |
| `RateLimitedFetchOrchestrator.tick()` (mocked registry) | ~11 | — |
| End-to-end orchestrator (mocked registry, multiple sources, quota saturation, partial failure, budget enforcement, cron no-op) | — | 6 |
| **Total new tests** | **~78** | **6** |

(Totals approximate — codebase has historically run ±2 from planned figures due to parametrize-collection idiosyncrasies; that variance is acceptable per the project's known-pattern note.)

**Architectural notes:**

- `QuotaWindow` is a pure dataclass with no I/O; `usage_lookup` is the injection seam. This makes the sliding-window math testable with synthetic inputs (no DB fixture needed) and lets future sub-phases swap in a Redis-backed or in-memory `usage_lookup` without touching the math.
- `prioritize_queue` returns a new sorted list rather than mutating in place. Stable sort is critical — two equal-score rows must preserve insertion order so an operator can rely on FIFO semantics within a weight class.
- `RateLimitedFetchOrchestrator._dispatch_one` records quota use BEFORE the source method call (conservative — over-counts on crash; under-count would let a crashing source escape rate-limit accounting).
- `_source_method_for_field` is the only place in the orchestrator that knows about specific field names. If A.3.10 adds new field categories, that method is the single point of update.
- The `__init__.py` re-export lets callers `from src.common.orchestrator import RateLimitedFetchOrchestrator` without needing to know the internal module layout.
- The CLI hook (`_main`) is intentionally minimal — it parses args, instantiates db + registry + orchestrator, calls `tick`, prints JSON, returns 0. Anything more (loops, retries, sleeping) belongs in the cron / Task Scheduler invocation, not in Python.
- WAL mode is enabled in `migrate_to_v10` rather than per-orchestrator-init, because it's a persistent DB-file property and migration is the natural one-time setup site.

**Architecture risk: cron drift on Windows Task Scheduler**

Windows Task Scheduler's "Run every 1 minute" trigger drifts slightly under load (typical drift ~100ms). The orchestrator's `datetime.utcnow()`-based bucketing is robust to this — buckets are 1-second wide and the drift is sub-bucket. A higher-cadence trigger (every 10s, say) could allow drift to equal or exceed the bucket width and create occasional under-buckets, but no correctness impact (it's still a sliding-window aggregate).

---

## Execution Handoff

**Recommended:** Subagent-driven, mirroring the A.3.7 wave pattern.

- **Wave 1 (Pre-flight + Schema + DB):** One sub-agent does Tasks 0–2 (verify environment + schema v9; add three Pydantic models; add `migrate_to_v10` + helpers; create `config/orchestrator.yaml`). All in `src/common/`; sequential. **~15 min**.
- **Wave 2 (Orchestrator package):** One sub-agent does Task 3 in three sub-task waves (3.1 quota_window, 3.2 queue_priority, 3.3 fetch_orchestrator). Each sub-task is test-first, so the subagent should follow the pattern: write tests → run (fail) → write source → run (pass) → commit at end of task. **~30 min**.
- **Wave 3 (Integration + build plan):** One sub-agent does Task 4 (integration test + CLI smoke) inline with Task 5 (build plan update). **~10 min**.

Total: 3 waves, ~55 min wall-clock.

**Critical pre-execution checks for the executing agent:**

1. After Task 0, confirm schema is at v9 and the 475-test baseline passes:

   ```
   ./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; print(DatabaseManager().get_schema_version())"
   ./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
   ```

   Expect `9` and `475 passed`.

2. After Task 2, confirm `schema_version == 10` and the three new tables exist:

   ```
   ./venv/Scripts/python.exe -c "import sqlite3; from src.common.database import DatabaseManager; m=DatabaseManager('data/_v10_check.db'); m.migrate_to_v10(); c=sqlite3.connect('data/_v10_check.db'); print('version:', m.get_schema_version()); print('tables:', sorted(r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name IN ('provider_call_log','provider_quota_state','fetch_queue')\")))"
   ```

   Expect `version: 10` and `tables: ['fetch_queue', 'provider_call_log', 'provider_quota_state']`.

3. After Task 3.1, the `QuotaWindow` tests should all pass before moving to 3.2. Do NOT chain — if 3.1 has a sliding-window bug, 3.3's `_build_quota_window` will exhibit the same bug as integration noise. Fail fast on the pure-function tests.

4. After Task 3.3, smoke-test the CLI BEFORE writing the integration test (Task 4):

   ```
   ./venv/Scripts/python.exe -m src.common.orchestrator.fetch_orchestrator tick --help
   ```

   Expect argparse help output with `--budget`, `--db`, `--config`. If `ModuleNotFoundError`, the orchestrator package's `__init__.py` is missing or the file paths don't match.

5. The `_record_quota_use` is called BEFORE the source method invocation (conservative). If you accidentally swap the order, the test `test_tick_records_failure_on_source_exception` will still pass (the source raises, the call_log row is written), but the quota_state will be under-counted by 1 — visible as `test_tick_dispatches_when_quota_available` returning `used == 0` instead of `used == 1`. Watch for this.

6. **Watermark dict key gotcha (A.3.6 lesson, still applies):** `db.get_watermark()` returns a dict with key `last_error_message`, NOT `error_message`. The orchestrator's `enqueue_staleness_sweep` uses `last_fetched_at` (not `last_error_message`) so it's unaffected, but any debug logging that references the watermark error message must use `last_error_message`.

7. **`tenacity.__version__` does NOT exist** in many tenacity builds. A.3.7.5 doesn't use tenacity at all (no per-call retries — quota saturation IS the retry policy: deferred rows re-enqueue on next staleness sweep), so this is moot — but if you reach for tenacity for a "smarter backoff" mid-task, remember the no-`__version__` constraint.

8. **URL-dispatch single side_effect pattern (A.3.5/A.3.6/A.3.7 lesson)** does NOT apply directly to A.3.7.5 because the orchestrator's tests mock the source method (the Python function) rather than `requests.get`. But if you find yourself reaching for `mocker.patch("requests.get", ...)` inside an orchestrator test, you're testing the wrong layer — the source itself should be mocked at the registry boundary.

9. **Use the `Write` tool, not bash heredoc, when creating the orchestrator source files.** Heredoc strips backticks inside docstrings on Windows PowerShell shells (A.3.6 lesson). The same applies to the test files — `Write` preserves the exact content. (This plan was authored using `Write` + `Edit` for the same reason.)

10. **Do NOT modify other sources / the registry.** Only `src/common/database.py` (append) + `src/common/schemas.py` (append) + new files under `src/common/orchestrator/` and `tests/common/orchestrator/`. The registry is consumed as an opaque dependency; if it doesn't expose what the orchestrator needs (e.g., `enabled_sources()`), that's a different sub-phase.

11. **`time.sleep` IS used in `test_tick_budget_respected` and `test_a3_7_5_budget_enforcement_carries_remainder` — DO NOT mock it.** The budget-enforcement test deliberately uses real wall-clock to verify the `_time.monotonic() < deadline` loop terminates correctly. Total test wall-clock for those two tests is ~2-3 seconds, which is within the project's per-test timeout.

12. **Plan defect watch (A.3.5+ lesson): test counts may drift ±2.** The Self-review test counts are approximate; if the actual count differs by 1–2, that's parametrize-collection variance, not a bug.

13. **`BEGIN IMMEDIATE` inside `dequeue_next` is required for atomic claim.** If you accidentally use `BEGIN DEFERRED` (the default), two concurrent `dequeue_next` calls can both pass the SELECT and both UPDATE the same row, which is a silent double-dispatch bug. The test `test_dequeue_marks_dispatched` verifies the single-call case but does NOT cover concurrent calls (single-process invariant — see Concurrency notes). If you add a multi-process test in a future sub-phase, this is where to start.

**Methodology callouts before execution:**

**1. Single-process invariant — flag for review.** This plan assumes a single `tick()` cron / Task Scheduler job at a time. Parallelizing (e.g., "run two orchestrators side-by-side for higher throughput") would require row-level `UPDATE ... WHERE queue_id = ? AND status = 'pending'` semantics inside the same BEGIN IMMEDIATE block, and the quota recording needs to be transactional with the dispatch (so two orchestrators can't both think they have full quota and over-dispatch). Decision: single-process is acceptable for v1. Multi-process safety adds ~half a day and tightens the test suite.

**2. Quota record-before-vs-record-after.** This plan records quota use BEFORE invoking the source method (conservative — over-counts on crash, never under-counts). The alternative — record after success — would under-count on crashes and risk consecutive 429s as we keep dispatching against a saturated-but-not-yet-recorded provider. Decision: conservative semantics for v1. Best-effort post-record that resets the bucket if the call crashed mid-flight requires a `record_quota_use_with_rollback` helper and complicates the call/dispatch path.

**3. Priority weights table — calibration deferral to A.3.10.** The v1 weights reflect spec §7 alpha contributions as estimated, NOT measured. After A.3.10's equivalence harness produces per-field IC data, the weights can be re-tuned.

**4. Cron compatibility — Windows Task Scheduler vs cron.** The CLI hook is OS-agnostic. On Windows the recommended invocation is: Task Scheduler → "Run a program" → `<project>\venv\Scripts\python.exe -m src.common.orchestrator.fetch_orchestrator tick --budget=60` with trigger "Every minute" and the project directory as start-in. Stdout pipes to a log file via the action's arguments + redirect (Task Scheduler doesn't directly redirect; wrap in a `cmd.exe /c "...python... > orch.log 2>&1"` for a file log).

**5. `provider_call_log` retention — unbounded growth in v1.** Same as the queue table, `provider_call_log` rows accumulate forever. At full throttle (~3 dispatched/sec × 86400 sec) the table grows ~260k rows/day. This is fine for SQLite but eventually a `cleanup_old_call_logs(keep_days=N)` helper is needed. Adding is ~5 lines + 1 test.

**6. CLI subcommands — `tick` is the only one in v1.** Future subcommands (`enqueue --source=... --ticker=... --field=... --priority=...`, `status` to dump the queue + quota state, `drain` to flush completed/failed queue rows) are natural extensions.

