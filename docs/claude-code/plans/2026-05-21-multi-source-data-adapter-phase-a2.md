# Multi-Source Data Adapter — Phase A.2 Implementation Plan (Storage Migration)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `data/fundamentals.db` from schema version 1 to version 2 by **additively** adding the storage shape required by the v2 architecture (canonical universe wide table, thesis objects placeholder, source provenance, source health, time-series accumulators, Bayesian/agent caches). Pre-existing v1 tables (`runs`, `universe_members`, `candidate_results`, `filter_waterfall`, `data_quality_metrics`, plus the legacy `finviz_universe_history`) are preserved unchanged and remain functional.

**Architecture:** All new schema lives in the same SQLite database. The migration is invoked via a new `DatabaseManager.migrate_to_v2()` method that calls `migrate()` first (idempotent v1 baseline), then applies v2 DDL with `CREATE TABLE IF NOT EXISTS` and `CREATE VIEW IF NOT EXISTS`, and finally records a row in `schema_version`. Pydantic models for every new table go in `src/common/schemas.py`. Insert helpers go in `DatabaseManager`. No A.1 source-adapter code is touched.

**Tech Stack:** Python 3.11+, `sqlite3` (stdlib), `pydantic` v2, `pytest`. No new runtime dependencies.

**Spec references:**
- The layer architecture — §6 (storage shape), §13 (data integrity invariants) are the primary sources
- [docs/claude-code/specs/2026-05-21-multi-source-data-adapter-design.md](../specs/2026-05-21-multi-source-data-adapter-design.md) — Phase A.1 sub-spec; A.2 builds on its `field_provenance` and `source_run_log` table designs

**Out of scope for A.2** (covered in later phases):
- Refactoring `screen.py` / `factors.py` to consume `canonical_universe` → A.3
- Adding `news_activity_score` factor computation → A.3
- Notebook extension (provenance drilldown, source toggles) → A.4
- The deliberately-corrupted-value acceptance test → A.5
- Layer 2/3/4 reading and writing these tables → their respective phases (B.x, C.x, D.x)
- Live position state tables (`open_positions`, `order_log`, `closed_trades`) → Layer 5 / Phase E (their schemas are NOT spec'd here)
- `expression_evaluations`, `ranked_trades` tables → Layer 3/4 / Phase C/D

A.2 produces empty, query-able storage. Populating it is the job of subsequent phases.

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| `src/common/schemas.py` | Add 9 new Pydantic models | Modify |
| `src/common/database.py` | Add `migrate_to_v2()` method + insert helpers | Modify |
| `tests/common/__init__.py` | Marker | Create |
| `tests/common/test_schemas_v2.py` | Tests for new Pydantic models | Create |
| `tests/common/test_database_v2.py` | Tests for `migrate_to_v2()` + insert helpers | Create |
| `tests/common/test_integration_migration.py` | End-to-end migration against fresh + A.1-populated databases | Create |
| the build plan | Mark A.2 status | Modify |

---

## Task 0: Pre-flight verification

**Files:**
- Create: `tests/common/__init__.py`

- [ ] **Step 1: Verify current schema state by running existing v1 migration**

Run from the repository root:

```
./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager(); m.migrate(); print('migration OK'); import sqlite3; c=sqlite3.connect(m.db_path); print(sorted(r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'\")))"
```

Expected: prints `migration OK` followed by a sorted list including at minimum: `candidate_results, data_quality_metrics, filter_waterfall, finviz_universe_history, runs, schema_version, universe_members`. Schema version 1 exists.

- [ ] **Step 2: Confirm schema_version table contains version 1**

```
./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; import sqlite3; m=DatabaseManager(); c=sqlite3.connect(m.db_path); print(list(c.execute('SELECT version, applied_at FROM schema_version ORDER BY version')))"
```

Expected: prints `[(1, '<some iso timestamp>')]`.

- [ ] **Step 3: Create the test package marker**

Create `tests/common/__init__.py` with content: (empty file — single newline only)

- [ ] **Step 4: Confirm pytest discovers it**

Run: `./venv/Scripts/python.exe -m pytest tests/common/ --collect-only -q`
Expected: `no tests ran` (exit code 5). No errors about missing __init__.

- [ ] **Step 5: Commit**

```
git add tests/common/__init__.py
git commit -m "chore: add tests/common/ package marker for A.2"
```

---

## Task 1: Pydantic models for snapshot tables

**Files:**
- Modify: `src/common/schemas.py` (append new classes)
- Test: `tests/common/test_schemas_v2.py`

The four snapshot models: `CanonicalUniverseRow`, `ThesisObjectRow`, `FieldProvenanceRow`, `SourceRunLogRow`.

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_schemas_v2.py`:

```python
"""Tests for v2 Pydantic models added in Phase A.2."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.common.schemas import (
    CanonicalUniverseRow,
    ThesisObjectRow,
    FieldProvenanceRow,
    SourceRunLogRow,
)


class TestCanonicalUniverseRow:
    def test_minimal_valid(self):
        row = CanonicalUniverseRow(run_id="r1", ticker="AAPL")
        assert row.run_id == "r1"
        assert row.ticker == "AAPL"
        assert row.pe_ttm is None

    def test_full_valid(self):
        row = CanonicalUniverseRow(
            run_id="r1",
            ticker="AAPL",
            company_name="Apple Inc",
            sector="Technology",
            industry="Consumer Electronics",
            exchange="NASDAQ",
            cik="0000320193",
            market_cap_usd=3_000_000_000_000.0,
            price=150.0,
            avg_daily_volume=50_000_000,
            pe_ttm=24.5,
            pe_forward=22.1,
            ebit_ttm=120_000_000_000.0,
            fcf_ttm=110_000_000_000.0,
            operating_margin=0.30,
            net_profit_margin=0.25,
            roe=1.40,
            roic=0.55,
            total_debt_to_equity=2.1,
            interest_coverage=42.0,
            revenue_growth_yoy=0.08,
            eps_growth_yoy=0.10,
            perf_1m=0.03,
            perf_3m=0.08,
            perf_6m=0.15,
            perf_12m=0.22,
            rsi_14=58.0,
            dist_52w_high=-0.05,
            dist_52w_low=0.40,
            dist_200dma=0.06,
            short_interest_pct_float=0.012,
            news_activity_score=0.4,
            pe_5y_percentile=0.65,
            ev_ebitda_5y_percentile=0.60,
            data_quality_score=0.92,
        )
        assert row.market_cap_usd > 0
        assert -1.0 <= row.operating_margin <= 1.0
        assert 0.0 <= row.data_quality_score <= 1.0

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            CanonicalUniverseRow(run_id="r1", ticker="not-a-ticker")

    def test_data_quality_score_out_of_range_rejected(self):
        with pytest.raises(Exception):
            CanonicalUniverseRow(run_id="r1", ticker="AAPL", data_quality_score=1.5)


class TestThesisObjectRow:
    def test_minimal_valid(self):
        row = ThesisObjectRow(
            run_id="r1",
            ticker="AAPL",
            direction="bullish",
            target_price=200.0,
            horizon_days=90,
            confidence_prior=0.7,
        )
        assert row.direction == "bullish"
        assert row.invalidation_conditions == []
        assert row.macro_context == {}

    def test_direction_must_be_in_literal(self):
        with pytest.raises(Exception):
            ThesisObjectRow(
                run_id="r1",
                ticker="AAPL",
                direction="sideways",
                target_price=200.0,
                horizon_days=90,
                confidence_prior=0.7,
            )

    def test_horizon_days_must_be_positive(self):
        with pytest.raises(Exception):
            ThesisObjectRow(
                run_id="r1",
                ticker="AAPL",
                direction="bullish",
                target_price=200.0,
                horizon_days=0,
                confidence_prior=0.7,
            )

    def test_confidence_prior_bounded(self):
        with pytest.raises(Exception):
            ThesisObjectRow(
                run_id="r1",
                ticker="AAPL",
                direction="bullish",
                target_price=200.0,
                horizon_days=90,
                confidence_prior=1.5,
            )


class TestFieldProvenanceRow:
    def test_basic(self):
        row = FieldProvenanceRow(
            run_id="r1",
            ticker="AAPL",
            field="pe_ttm",
            source="edgar",
            raw_value="24.5",
            parsed_value=24.5,
            weight=0.5,
            contributed_to_canonical=True,
            disagreement_pct=0.02,
            fetched_at="2026-05-21T08:00:00Z",
        )
        assert row.contributed_to_canonical is True
        assert row.weight == 0.5

    def test_weight_in_unit_interval(self):
        with pytest.raises(Exception):
            FieldProvenanceRow(
                run_id="r1",
                ticker="AAPL",
                field="pe_ttm",
                source="edgar",
                raw_value="x",
                parsed_value=None,
                weight=1.5,
                contributed_to_canonical=False,
                disagreement_pct=None,
                fetched_at="2026-05-21T08:00:00Z",
            )


class TestSourceRunLogRow:
    def test_basic_ok(self):
        row = SourceRunLogRow(
            run_id="r1",
            source="finviz",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:02Z",
            status="ok",
            rows_fetched=915,
            error_message=None,
            error_traceback=None,
        )
        assert row.status == "ok"
        assert row.rows_fetched == 915

    def test_status_literal(self):
        with pytest.raises(Exception):
            SourceRunLogRow(
                run_id="r1",
                source="finviz",
                started_at="2026-05-21T08:00:00Z",
                finished_at="2026-05-21T08:00:02Z",
                status="weird",
                rows_fetched=0,
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_schemas_v2.py -v`
Expected: ImportError on the four new model classes.

- [ ] **Step 3: Append the new Pydantic models to `src/common/schemas.py`**

Add the following at the **end** of `src/common/schemas.py`:

```python


# === Phase A.2 - v2 storage models =====================================


class CanonicalUniverseRow(BaseModel):
    """Per-(run_id, ticker) wide row in canonical_universe.

    Spec: the layer architecture section 6.1.
    All non-id fields are Optional because data coverage is imperfect.
    """

    run_id: str
    ticker: str
    company_name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    exchange: Optional[str] = None
    cik: Optional[str] = None

    market_cap_usd: Optional[float] = Field(default=None, gt=0)
    price: Optional[float] = Field(default=None, gt=0)
    avg_daily_volume: Optional[int] = Field(default=None, ge=0)

    pe_ttm: Optional[float] = None
    pe_forward: Optional[float] = None
    ebit_ttm: Optional[float] = None
    fcf_ttm: Optional[float] = None
    operating_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    net_profit_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    roe: Optional[float] = None
    roic: Optional[float] = None
    total_debt_to_equity: Optional[float] = None
    interest_coverage: Optional[float] = None
    revenue_growth_yoy: Optional[float] = None
    eps_growth_yoy: Optional[float] = None

    perf_1m: Optional[float] = None
    perf_3m: Optional[float] = None
    perf_6m: Optional[float] = None
    perf_12m: Optional[float] = None
    rsi_14: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    dist_52w_high: Optional[float] = Field(default=None, le=0.0)
    dist_52w_low: Optional[float] = Field(default=None, ge=0.0)
    dist_200dma: Optional[float] = None

    short_interest_pct_float: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    news_activity_score: Optional[float] = None
    pe_5y_percentile: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    ev_ebitda_5y_percentile: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    data_quality_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    materialized_at: Optional[str] = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s


class ThesisObjectRow(BaseModel):
    """Per-(run_id, ticker) thesis record.

    Layer 2 (Phase B.4) populates this. A.2 only creates the table and the schema.
    """

    run_id: str
    ticker: str
    direction: Literal["bullish", "bearish", "range-bound", "earnings-play"]
    target_price: float
    horizon_days: int = Field(gt=0)
    confidence_prior: float = Field(ge=0.0, le=1.0)
    invalidation_conditions: list[str] = Field(default_factory=list)
    fundamental_bias_score: Optional[float] = None
    news_bias_score: Optional[float] = None
    macro_context: dict = Field(default_factory=dict)
    technical_setup: dict = Field(default_factory=dict)
    earnings_in_window: dict = Field(default_factory=dict)
    ai_research_synthesis: dict = Field(default_factory=dict)
    built_at: Optional[str] = None


class FieldProvenanceRow(BaseModel):
    """Per-(run_id, ticker, field, source) raw observation log. Append-only."""

    run_id: str
    ticker: str
    field: str
    source: str
    raw_value: Optional[str] = None
    parsed_value: Optional[float] = None
    weight: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    contributed_to_canonical: bool = False
    disagreement_pct: Optional[float] = None
    fetched_at: str


class SourceRunLogRow(BaseModel):
    """Per-(run_id, source) health record. Append-only."""

    run_id: str
    source: str
    started_at: str
    finished_at: Optional[str] = None
    status: Literal["ok", "partial", "failed"]
    rows_fetched: int = Field(ge=0)
    error_message: Optional[str] = None
    error_traceback: Optional[str] = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_schemas_v2.py -v`
Expected: All 12 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/schemas.py tests/common/test_schemas_v2.py
git commit -m "feat(schemas): add v2 snapshot models (canonical, thesis, provenance, source log)"
```

---

## Task 2: Pydantic models for time-series accumulator tables

**Files:**
- Modify: `src/common/schemas.py` (append three more models)
- Test: `tests/common/test_schemas_v2.py` (extend)

The three accumulator models: `HistoricalPriceRow`, `HistoricalIVRow`, `HistoricalEarningsReactionRow`.

- [ ] **Step 1: Append the failing tests to `tests/common/test_schemas_v2.py`**

```python


from src.common.schemas import (  # noqa: E402
    HistoricalPriceRow,
    HistoricalIVRow,
    HistoricalEarningsReactionRow,
)


class TestHistoricalPriceRow:
    def test_basic(self):
        row = HistoricalPriceRow(
            ticker="AAPL",
            observation_date="2026-05-20",
            open=148.0,
            high=152.0,
            low=147.5,
            close=151.2,
            volume=50_000_000,
            adj_close=151.2,
            source="yahoo",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert row.high >= row.low
        assert row.volume >= 0

    def test_negative_volume_rejected(self):
        with pytest.raises(Exception):
            HistoricalPriceRow(
                ticker="AAPL",
                observation_date="2026-05-20",
                open=148.0,
                high=152.0,
                low=147.5,
                close=151.2,
                volume=-100,
                source="yahoo",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_high_below_low_rejected(self):
        with pytest.raises(Exception):
            HistoricalPriceRow(
                ticker="AAPL",
                observation_date="2026-05-20",
                open=148.0,
                high=140.0,
                low=147.5,
                close=151.2,
                volume=50_000_000,
                source="yahoo",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )


class TestHistoricalIVRow:
    def test_basic(self):
        row = HistoricalIVRow(
            ticker="AAPL",
            observation_date="2026-05-20",
            expiry_date="2026-07-17",
            dte_days=58,
            atm_iv=0.27,
            atm_strike=150.0,
            iv_rank=0.42,
            source="yahoo",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert 0 < row.atm_iv < 5
        assert 0.0 <= row.iv_rank <= 1.0

    def test_iv_rank_bounded(self):
        with pytest.raises(Exception):
            HistoricalIVRow(
                ticker="AAPL",
                observation_date="2026-05-20",
                expiry_date="2026-07-17",
                dte_days=58,
                atm_iv=0.27,
                atm_strike=150.0,
                iv_rank=1.5,
                source="yahoo",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )


class TestHistoricalEarningsReactionRow:
    def test_basic(self):
        row = HistoricalEarningsReactionRow(
            ticker="AAPL",
            earnings_date="2026-04-30",
            pre_earnings_iv=0.45,
            post_earnings_iv=0.28,
            iv_crush_pct=0.378,
            absolute_move_pct=0.06,
            beat_or_miss="beat",
            source="yahoo",
        )
        assert row.beat_or_miss == "beat"

    def test_beat_or_miss_literal(self):
        with pytest.raises(Exception):
            HistoricalEarningsReactionRow(
                ticker="AAPL",
                earnings_date="2026-04-30",
                pre_earnings_iv=0.45,
                post_earnings_iv=0.28,
                iv_crush_pct=0.378,
                absolute_move_pct=0.06,
                beat_or_miss="okay",
                source="yahoo",
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_schemas_v2.py -v`
Expected: ImportError on the three new model classes.

- [ ] **Step 3: Append the new Pydantic models to `src/common/schemas.py`**

```python


# === Phase A.2 - v2 time-series accumulator models =====================


class HistoricalPriceRow(BaseModel):
    """Daily OHLCV per (ticker, observation_date). Append-only."""

    ticker: str
    observation_date: str
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)
    adj_close: Optional[float] = Field(default=None, gt=0)
    source: str
    scrape_timestamp: str

    @field_validator("low")
    @classmethod
    def _low_at_most_high(cls, v: float, info) -> float:
        # NOTE: Pydantic v2 `info.data` only contains already-validated fields,
        # and fields are validated in declaration order. `high` is declared
        # BEFORE `low`, so we attach the cross-field check to `low` and
        # inverted the comparison. Same invariant: high >= low.
        high = info.data.get("high")
        if high is not None and high < v:
            raise ValueError(f"low {v} must be <= high {high}")
        return v


class HistoricalIVRow(BaseModel):
    """Daily ATM IV per (ticker, observation_date, expiry_date). Append-only."""

    ticker: str
    observation_date: str
    expiry_date: str
    dte_days: int = Field(ge=0)
    atm_iv: float = Field(gt=0)
    atm_strike: float = Field(gt=0)
    iv_rank: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    source: str
    scrape_timestamp: str


class HistoricalEarningsReactionRow(BaseModel):
    """Per (ticker, earnings_date) post-earnings move + IV crush record."""

    ticker: str
    earnings_date: str
    pre_earnings_iv: Optional[float] = Field(default=None, gt=0)
    post_earnings_iv: Optional[float] = Field(default=None, gt=0)
    iv_crush_pct: Optional[float] = None
    absolute_move_pct: Optional[float] = None
    beat_or_miss: Literal["beat", "miss", "inline", "unknown"]
    source: str
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_schemas_v2.py -v`
Expected: All 19 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/schemas.py tests/common/test_schemas_v2.py
git commit -m "feat(schemas): add v2 time-series accumulator models (price, iv, earnings)"
```

---

## Task 3: Pydantic models for cache tables

**Files:**
- Modify: `src/common/schemas.py` (append two more models)
- Test: `tests/common/test_schemas_v2.py` (extend)

- [ ] **Step 1: Append the failing tests**

```python


from src.common.schemas import (  # noqa: E402
    PosteriorCacheEntry,
    AgentResponseCacheEntry,
)


class TestPosteriorCacheEntry:
    def test_basic(self):
        entry = PosteriorCacheEntry(
            ticker="AAPL",
            evidence_hash="a1b2c3",
            model_name="p_target",
            model_version="1.0",
            posterior_blob={"mean": 0.72, "samples": [0.7, 0.74]},
            credible_interval_blob={"lo": 0.65, "hi": 0.79},
            computed_at="2026-05-21T08:00:00Z",
            expires_at="2026-05-28T08:00:00Z",
        )
        assert entry.model_name == "p_target"
        assert "mean" in entry.posterior_blob


class TestAgentResponseCacheEntry:
    def test_basic(self):
        entry = AgentResponseCacheEntry(
            ticker="AAPL",
            agent_name="news_synthesizer",
            agent_version="v1",
            evidence_hash="d4e5f6",
            response_blob={"sentiment": 0.6, "headlines": []},
            confidence=0.8,
            computed_at="2026-05-21T08:00:00Z",
            expires_at="2026-05-22T08:00:00Z",
        )
        assert entry.confidence == 0.8

    def test_confidence_bounded(self):
        with pytest.raises(Exception):
            AgentResponseCacheEntry(
                ticker="AAPL",
                agent_name="news_synthesizer",
                agent_version="v1",
                evidence_hash="d4e5f6",
                response_blob={},
                confidence=1.5,
                computed_at="2026-05-21T08:00:00Z",
                expires_at="2026-05-22T08:00:00Z",
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_schemas_v2.py -v`
Expected: ImportError on the two new classes.

- [ ] **Step 3: Append the cache models to `src/common/schemas.py`**

```python


# === Phase A.2 - v2 cache models =======================================


class PosteriorCacheEntry(BaseModel):
    """Cached Bayesian posterior keyed by (ticker, evidence_hash, model_name)."""

    ticker: str
    evidence_hash: str
    model_name: str
    model_version: str
    posterior_blob: dict
    credible_interval_blob: dict
    computed_at: str
    expires_at: str


class AgentResponseCacheEntry(BaseModel):
    """Cached AI agent output keyed by (ticker, agent_name, evidence_hash)."""

    ticker: str
    agent_name: str
    agent_version: str
    evidence_hash: str
    response_blob: dict
    confidence: float = Field(ge=0.0, le=1.0)
    computed_at: str
    expires_at: str
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_schemas_v2.py -v`
Expected: All 22 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/schemas.py tests/common/test_schemas_v2.py
git commit -m "feat(schemas): add v2 cache models (posterior, agent response)"
```

---

## Task 4: `migrate_to_v2()` — DDL for all v2 tables + raw_finviz view

**Files:**
- Modify: `src/common/database.py`
- Test: `tests/common/test_database_v2.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_database_v2.py`:

```python
"""Tests for migrate_to_v2() in DatabaseManager."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {r[0] for r in rows}


def _view_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()
    return {r[0] for r in rows}


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as c:
        rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def test_migrate_to_v2_creates_all_expected_tables(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()

    expected_new_tables = {
        "canonical_universe",
        "thesis_objects",
        "field_provenance",
        "source_run_log",
        "historical_price",
        "historical_iv",
        "historical_earnings_reactions",
        "posterior_cache",
        "agent_response_cache",
    }
    actual = _table_names(db_path)
    missing = expected_new_tables - actual
    assert not missing, f"missing v2 tables: {missing}"


def test_migrate_to_v2_creates_raw_finviz_view(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    assert "raw_finviz" in _view_names(db_path)


def test_migrate_to_v2_bumps_schema_version_to_2(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2]


def test_migrate_to_v2_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO canonical_universe (run_id, ticker) VALUES (?, ?)",
            ("sentinel", "AAPL"),
        )
        c.commit()
    mgr.migrate_to_v2()  # second call must be safe
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT run_id, ticker FROM canonical_universe WHERE run_id='sentinel'"
        ).fetchall()
    assert rows == [("sentinel", "AAPL")]
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2]


def test_canonical_universe_has_v2_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    cols = _columns(db_path, "canonical_universe")
    expected_subset = {
        "run_id", "ticker", "company_name", "sector", "industry", "exchange", "cik",
        "market_cap_usd", "price", "avg_daily_volume",
        "pe_ttm", "pe_forward", "ebit_ttm", "fcf_ttm",
        "operating_margin", "net_profit_margin", "roe", "roic",
        "total_debt_to_equity", "interest_coverage",
        "revenue_growth_yoy", "eps_growth_yoy",
        "perf_1m", "perf_3m", "perf_6m", "perf_12m",
        "rsi_14", "dist_52w_high", "dist_52w_low", "dist_200dma",
        "short_interest_pct_float", "news_activity_score",
        "pe_5y_percentile", "ev_ebitda_5y_percentile",
        "data_quality_score", "materialized_at",
    }
    missing = expected_subset - cols
    assert not missing, f"missing columns: {missing}"


def test_field_provenance_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    cols = _columns(db_path, "field_provenance")
    expected = {
        "run_id", "ticker", "field", "source",
        "raw_value", "parsed_value", "weight",
        "contributed_to_canonical", "disagreement_pct", "fetched_at",
    }
    missing = expected - cols
    assert not missing


def test_raw_finviz_view_selects_from_finviz_universe_history(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO finviz_universe_history (Ticker, Company, scrape_timestamp) VALUES (?, ?, ?)",
            ("AAPL", "Apple Inc", "2026-05-21T08:00:00Z"),
        )
        c.commit()
        rows = c.execute("SELECT Ticker FROM raw_finviz").fetchall()
    assert ("AAPL",) in rows


def test_get_schema_version_returns_2_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    assert mgr.get_schema_version() == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_database_v2.py -v`
Expected: `AttributeError: 'DatabaseManager' object has no attribute 'migrate_to_v2'`.

- [ ] **Step 3: Add `migrate_to_v2()` and `get_schema_version()` to `DatabaseManager`**

In `src/common/database.py`, **add the following two methods to the `DatabaseManager` class** (place them immediately after the existing `migrate()` method, before `get_connection`):

```python
    def get_schema_version(self) -> int:
        """Return the highest version recorded in schema_version, or 0 if none."""
        with self.get_connection() as conn:
            try:
                cur = conn.execute("SELECT MAX(version) FROM schema_version")
                row = cur.fetchone()
            except sqlite3.OperationalError:
                return 0
        return int(row[0]) if row and row[0] is not None else 0

    def migrate_to_v2(self) -> None:
        """Idempotent migration v1 -> v2 per the v2 architecture spec section 6.

        Calls migrate() first to guarantee v1 baseline, then additively creates:
          - raw_finviz VIEW (alias over finviz_universe_history)
          - canonical_universe (wide snapshot)
          - thesis_objects (placeholder, Layer 2 populates)
          - field_provenance (per-source observations)
          - source_run_log (per-source health)
          - historical_price, historical_iv, historical_earnings_reactions
          - posterior_cache, agent_response_cache

        Safe to call multiple times. Existing data is preserved.
        """
        self.migrate()  # ensure v1 baseline

        v2_ddl = [
            # finviz_universe_history is created defensively for fresh-DB case.
            # Existing databases already have it with more columns; IF NOT EXISTS
            # leaves any pre-existing definition untouched.
            """
            CREATE TABLE IF NOT EXISTS finviz_universe_history (
              Ticker TEXT,
              Company TEXT,
              scrape_timestamp TEXT
            )
            """,
            """
            CREATE VIEW IF NOT EXISTS raw_finviz AS
              SELECT * FROM finviz_universe_history
            """,
            """
            CREATE TABLE IF NOT EXISTS canonical_universe (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              company_name TEXT,
              sector TEXT,
              industry TEXT,
              exchange TEXT,
              cik TEXT,
              market_cap_usd REAL,
              price REAL,
              avg_daily_volume INTEGER,
              pe_ttm REAL,
              pe_forward REAL,
              ebit_ttm REAL,
              fcf_ttm REAL,
              operating_margin REAL,
              net_profit_margin REAL,
              roe REAL,
              roic REAL,
              total_debt_to_equity REAL,
              interest_coverage REAL,
              revenue_growth_yoy REAL,
              eps_growth_yoy REAL,
              perf_1m REAL,
              perf_3m REAL,
              perf_6m REAL,
              perf_12m REAL,
              rsi_14 REAL,
              dist_52w_high REAL,
              dist_52w_low REAL,
              dist_200dma REAL,
              short_interest_pct_float REAL,
              news_activity_score REAL,
              pe_5y_percentile REAL,
              ev_ebitda_5y_percentile REAL,
              data_quality_score REAL,
              materialized_at TEXT,
              PRIMARY KEY (run_id, ticker)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS thesis_objects (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              direction TEXT NOT NULL,
              target_price REAL NOT NULL,
              horizon_days INTEGER NOT NULL,
              confidence_prior REAL NOT NULL,
              invalidation_conditions TEXT,
              fundamental_bias_score REAL,
              news_bias_score REAL,
              macro_context TEXT,
              technical_setup TEXT,
              earnings_in_window TEXT,
              ai_research_synthesis TEXT,
              built_at TEXT,
              PRIMARY KEY (run_id, ticker)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS field_provenance (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              field TEXT NOT NULL,
              source TEXT NOT NULL,
              raw_value TEXT,
              parsed_value REAL,
              weight REAL,
              contributed_to_canonical INTEGER NOT NULL DEFAULT 0,
              disagreement_pct REAL,
              fetched_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_provenance_run_ticker ON field_provenance(run_id, ticker)",
            "CREATE INDEX IF NOT EXISTS idx_provenance_run_field ON field_provenance(run_id, field)",
            """
            CREATE TABLE IF NOT EXISTS source_run_log (
              run_id TEXT NOT NULL,
              source TEXT NOT NULL,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              status TEXT NOT NULL,
              rows_fetched INTEGER NOT NULL DEFAULT 0,
              error_message TEXT,
              error_traceback TEXT,
              PRIMARY KEY (run_id, source)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS historical_price (
              ticker TEXT NOT NULL,
              observation_date TEXT NOT NULL,
              open REAL,
              high REAL,
              low REAL,
              close REAL,
              volume INTEGER,
              adj_close REAL,
              source TEXT,
              scrape_timestamp TEXT,
              PRIMARY KEY (ticker, observation_date)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_hist_price_ticker_date ON historical_price(ticker, observation_date DESC)",
            """
            CREATE TABLE IF NOT EXISTS historical_iv (
              ticker TEXT NOT NULL,
              observation_date TEXT NOT NULL,
              expiry_date TEXT NOT NULL,
              dte_days INTEGER,
              atm_iv REAL,
              atm_strike REAL,
              iv_rank REAL,
              source TEXT,
              scrape_timestamp TEXT,
              PRIMARY KEY (ticker, observation_date, expiry_date)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_hist_iv_ticker_date ON historical_iv(ticker, observation_date DESC)",
            """
            CREATE TABLE IF NOT EXISTS historical_earnings_reactions (
              ticker TEXT NOT NULL,
              earnings_date TEXT NOT NULL,
              pre_earnings_iv REAL,
              post_earnings_iv REAL,
              iv_crush_pct REAL,
              absolute_move_pct REAL,
              beat_or_miss TEXT NOT NULL,
              source TEXT,
              PRIMARY KEY (ticker, earnings_date)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS posterior_cache (
              ticker TEXT NOT NULL,
              evidence_hash TEXT NOT NULL,
              model_name TEXT NOT NULL,
              model_version TEXT NOT NULL,
              posterior_blob TEXT NOT NULL,
              credible_interval_blob TEXT NOT NULL,
              computed_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              PRIMARY KEY (ticker, evidence_hash, model_name)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_response_cache (
              ticker TEXT NOT NULL,
              agent_name TEXT NOT NULL,
              agent_version TEXT NOT NULL,
              evidence_hash TEXT NOT NULL,
              response_blob TEXT NOT NULL,
              confidence REAL NOT NULL,
              computed_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              PRIMARY KEY (ticker, agent_name, evidence_hash)
            )
            """,
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v2_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 2")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (2, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_database_v2.py -v`
Expected: All 8 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/database.py tests/common/test_database_v2.py
git commit -m "feat(database): add migrate_to_v2 - 9 new tables, raw_finviz view, schema v2"
```

---

## Task 5: Insert helpers for v2 tables

**Files:**
- Modify: `src/common/database.py` (append insert methods)
- Test: `tests/common/test_database_v2.py` (extend)

- [ ] **Step 1: Append failing tests to `tests/common/test_database_v2.py`**

```python


from src.common.schemas import (  # noqa: E402
    CanonicalUniverseRow,
    FieldProvenanceRow,
    SourceRunLogRow,
    HistoricalPriceRow,
)


def test_insert_canonical_universe_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    row = CanonicalUniverseRow(
        run_id="r1", ticker="AAPL",
        company_name="Apple Inc", sector="Technology",
        pe_ttm=24.5, operating_margin=0.30,
    )
    mgr.insert_canonical_universe([row])
    with sqlite3.connect(db_path) as c:
        result = c.execute(
            "SELECT ticker, company_name, pe_ttm FROM canonical_universe WHERE run_id='r1'"
        ).fetchall()
    assert result == [("AAPL", "Apple Inc", 24.5)]


def test_insert_field_provenance_persists_rows(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    rows = [
        FieldProvenanceRow(
            run_id="r1", ticker="AAPL", field="pe_ttm", source="edgar",
            raw_value="24.5", parsed_value=24.5,
            weight=0.5, contributed_to_canonical=True,
            disagreement_pct=0.02, fetched_at="2026-05-21T08:00:00Z",
        ),
        FieldProvenanceRow(
            run_id="r1", ticker="AAPL", field="pe_ttm", source="yahoo",
            raw_value="25.1", parsed_value=25.1,
            weight=0.25, contributed_to_canonical=True,
            disagreement_pct=0.024, fetched_at="2026-05-21T08:00:01Z",
        ),
    ]
    mgr.insert_field_provenance(rows)
    with sqlite3.connect(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM field_provenance WHERE run_id='r1' AND ticker='AAPL'"
        ).fetchone()[0]
    assert n == 2


def test_insert_source_run_log_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    row = SourceRunLogRow(
        run_id="r1", source="finviz",
        started_at="2026-05-21T08:00:00Z",
        finished_at="2026-05-21T08:00:02Z",
        status="ok", rows_fetched=915,
    )
    mgr.insert_source_run_log([row])
    with sqlite3.connect(db_path) as c:
        result = c.execute(
            "SELECT source, status, rows_fetched FROM source_run_log WHERE run_id='r1'"
        ).fetchone()
    assert result == ("finviz", "ok", 915)


def test_insert_historical_price_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    row = HistoricalPriceRow(
        ticker="AAPL", observation_date="2026-05-20",
        open=148.0, high=152.0, low=147.5, close=151.2,
        volume=50_000_000, adj_close=151.2,
        source="yahoo", scrape_timestamp="2026-05-21T08:00:00Z",
    )
    mgr.insert_historical_price([row])
    with sqlite3.connect(db_path) as c:
        result = c.execute(
            "SELECT ticker, observation_date, close FROM historical_price"
        ).fetchone()
    assert result == ("AAPL", "2026-05-20", 151.2)


def test_insert_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    mgr.insert_canonical_universe([])
    mgr.insert_field_provenance([])
    mgr.insert_source_run_log([])
    mgr.insert_historical_price([])
    with sqlite3.connect(db_path) as c:
        for table in (
            "canonical_universe", "field_provenance",
            "source_run_log", "historical_price",
        ):
            n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n == 0, f"{table} should be empty"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_database_v2.py -v`
Expected: `AttributeError` on the new `insert_*` methods.

- [ ] **Step 3: Add insert helpers to `DatabaseManager`**

In `src/common/database.py`, at the **end** of the `DatabaseManager` class (after `load_latest_run`), add:

```python
    # ------------------------------------------------------------------
    # Phase A.2: v2 insert helpers (typed)
    # ------------------------------------------------------------------

    def insert_canonical_universe(self, rows: list) -> None:
        """Insert validated CanonicalUniverseRow records into canonical_universe."""
        if not rows:
            return
        from src.common.schemas import CanonicalUniverseRow  # local to avoid cycle
        records = [r.model_dump() if isinstance(r, CanonicalUniverseRow) else dict(r) for r in rows]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT INTO canonical_universe ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()

    def insert_field_provenance(self, rows: list) -> None:
        """Insert FieldProvenanceRow records into field_provenance (append-only)."""
        if not rows:
            return
        values = [
            (
                r.run_id, r.ticker, r.field, r.source,
                r.raw_value, r.parsed_value, r.weight,
                1 if r.contributed_to_canonical else 0,
                r.disagreement_pct, r.fetched_at,
            )
            for r in rows
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO field_provenance
                  (run_id, ticker, field, source, raw_value, parsed_value,
                   weight, contributed_to_canonical, disagreement_pct, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()

    def insert_source_run_log(self, rows: list) -> None:
        """Insert SourceRunLogRow records into source_run_log."""
        if not rows:
            return
        values = [
            (
                r.run_id, r.source, r.started_at, r.finished_at,
                r.status, r.rows_fetched, r.error_message, r.error_traceback,
            )
            for r in rows
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO source_run_log
                  (run_id, source, started_at, finished_at, status,
                   rows_fetched, error_message, error_traceback)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()

    def insert_historical_price(self, rows: list) -> None:
        """Insert HistoricalPriceRow records into historical_price.

        Uses INSERT OR REPLACE since (ticker, observation_date) is unique;
        re-scraping the same day for the same ticker overwrites.
        """
        if not rows:
            return
        values = [
            (
                r.ticker, r.observation_date,
                r.open, r.high, r.low, r.close,
                r.volume, r.adj_close,
                r.source, r.scrape_timestamp,
            )
            for r in rows
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO historical_price
                  (ticker, observation_date, open, high, low, close,
                   volume, adj_close, source, scrape_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_database_v2.py -v`
Expected: All 13 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/database.py tests/common/test_database_v2.py
git commit -m "feat(database): add v2 insert helpers for canonical, provenance, runlog, price"
```

---

## Task 6: Integration test — preserves A.1 data + idempotent end-to-end

**Files:**
- Test: `tests/common/test_integration_migration.py`

- [ ] **Step 1: Write the integration test**

Create `tests/common/test_integration_migration.py`:

```python
"""End-to-end migration test: A.1 data preservation + idempotency + concurrency."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager


def test_v2_migration_preserves_v1_data(tmp_path: Path):
    """Simulate an A.1-populated database, then migrate to v2, assert all
    pre-existing data is intact."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))

    mgr.migrate()
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO runs (run_id, run_timestamp, run_type, status) VALUES (?, ?, ?, ?)",
            ("legacy-run-1", "2026-05-17T10:00:00Z", "layer1", "ok"),
        )
        c.execute(
            """
            INSERT INTO universe_members
              (run_id, ticker, sector, market_cap_usd, pe_ratio)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("legacy-run-1", "MSFT", "Technology", 3.0e12, 30.5),
        )
        c.execute(
            """
            INSERT INTO candidate_results
              (run_id, ticker, playbook, composite_score, eligible, reasoning)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("legacy-run-1", "MSFT", "A", 78.0, 1, "fundamental: cheap; technical: bouncing"),
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS finviz_universe_history (
              Ticker TEXT, Company TEXT, scrape_timestamp TEXT
            )
            """
        )
        c.execute(
            "INSERT INTO finviz_universe_history (Ticker, Company, scrape_timestamp) VALUES (?, ?, ?)",
            ("MSFT", "Microsoft Corp", "2026-05-17T10:00:00Z"),
        )
        c.commit()

    mgr.migrate_to_v2()

    with sqlite3.connect(db_path) as c:
        run_rows = c.execute("SELECT run_id FROM runs WHERE run_id='legacy-run-1'").fetchall()
        assert run_rows == [("legacy-run-1",)]
        univ_rows = c.execute(
            "SELECT ticker, sector, pe_ratio FROM universe_members WHERE run_id='legacy-run-1'"
        ).fetchall()
        assert univ_rows == [("MSFT", "Technology", 30.5)]
        cand_rows = c.execute(
            "SELECT ticker, playbook, composite_score FROM candidate_results WHERE run_id='legacy-run-1'"
        ).fetchall()
        assert cand_rows == [("MSFT", "A", 78.0)]
        finviz_rows = c.execute(
            "SELECT Ticker, Company FROM finviz_universe_history WHERE Ticker='MSFT'"
        ).fetchall()
        assert finviz_rows == [("MSFT", "Microsoft Corp")]
        view_rows = c.execute("SELECT Ticker FROM raw_finviz WHERE Ticker='MSFT'").fetchall()
        assert view_rows == [("MSFT",)]


def test_v2_migration_double_call_is_safe(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    mgr.migrate_to_v2()
    mgr.migrate_to_v2()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2]


def test_v2_migration_on_brand_new_db_works(tmp_path: Path):
    db_path = tmp_path / "fresh.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    assert mgr.get_schema_version() == 2


def test_schema_version_is_2_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    assert mgr.get_schema_version() == 2


def test_partial_migration_recovers(tmp_path: Path):
    db_path = tmp_path / "partial.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate()
    assert mgr.get_schema_version() == 1
    mgr.migrate_to_v2()
    assert mgr.get_schema_version() == 2
    with sqlite3.connect(db_path) as c:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    for t in (
        "canonical_universe", "thesis_objects", "field_provenance",
        "source_run_log", "historical_price", "historical_iv",
        "historical_earnings_reactions", "posterior_cache", "agent_response_cache",
    ):
        assert t in tables, f"missing {t}"
```

- [ ] **Step 2: Run the integration test**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_integration_migration.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 3: Run the full suite (sanity check)**

Run: `./venv/Scripts/python.exe -m pytest -m "not integration" -v`
Expected: All tests pass — at least ~90 across A.1 + A.2 work.

- [ ] **Step 4: Commit**

```
git add tests/common/test_integration_migration.py
git commit -m "test(database): integration test - v2 migration preserves v1 data, idempotent"
```

---

## Task 7: Update the build plan

**Files:**
- Modify: the build plan (mark A.2 done in §5.1.0)

- [ ] **Step 1: Open and locate §5.1.0**

In the build plan, find the `### 5.1.0 Sub-phase ordering` section. It contains a 5-row table with A.1 through A.5.

- [ ] **Step 2: Update the A.2 row to mark it shipped**

Use the `Edit` tool to update the A.2 row. Change:
- **Before (the old A.2 row):** `| **A.2** | (to be written after A.1 lands) | SQLite migration: ...`
- **After:** `| **A.2** | [docs/claude-code/plans/2026-05-21-multi-source-data-adapter-phase-a2.md](docs/claude-code/plans/2026-05-21-multi-source-data-adapter-phase-a2.md) | SQLite migration shipped: 9 new tables (canonical_universe, thesis_objects, field_provenance, source_run_log, historical_price, historical_iv, historical_earnings_reactions, posterior_cache, agent_response_cache) + raw_finviz view. Schema v2. Pydantic models for all. Insert helpers for canonical/provenance/runlog/price. | Migration idempotent; existing finviz_universe_history preserved as raw_finviz view |`

The other rows (A.3, A.4, A.5) remain unchanged.

- [ ] **Step 3: Commit**

```
git add <build-plan>
git commit -m "docs: mark Phase A.2 shipped in build plan"
```

---

## Phase A.2 — Definition of Done

A.2 is complete when **all** of these are true:

- [ ] `./venv/Scripts/python.exe -m pytest -m "not integration" -v` shows all unit + module-integration tests passing (target ~109 tests across A.1 + A.2 work)
- [ ] `./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager(); m.migrate_to_v2(); print(m.get_schema_version())"` prints `2`
- [ ] All 9 v2 tables exist after `migrate_to_v2()` is called on a fresh DB
- [ ] `raw_finviz` view exists and reads from `finviz_universe_history`
- [ ] Existing A.1 data in the production `data/fundamentals.db` is preserved when migrated
- [ ] Pydantic models for all v2 tables exist in `src/common/schemas.py` and validate correctly
- [ ] Insert helpers exist for `canonical_universe`, `field_provenance`, `source_run_log`, `historical_price`
- [ ] The build plan §5.1.0 marks A.2 shipped
- [ ] No A.1 code (`src/common/datasources/`, source classes) was touched
- [ ] Git log shows ~7 small commits, one per task

---

## Self-review

**Spec coverage** — every relevant v2 spec section mapped to a task:

| Spec section | Covered in |
|---|---|
| §6.1 Snapshots (canonical_universe, thesis_objects, field_provenance, source_run_log) | Tasks 1, 4 |
| §6.2 Time-series accumulators (historical_*, posterior_cache, agent_response_cache) | Tasks 2, 3, 4 |
| §6.3 Position state | Out of scope (deferred to Phase E) — noted in plan header |
| §13.1 Source observation immutability | Task 4 (no UPDATE clause on field_provenance; only INSERT) |
| §13.2 Run-id traceability | All tables include run_id where applicable |
| §13.3 Schema validation at boundaries | Tasks 1-3 Pydantic models; Task 5 helpers use them |
| §13.4 No silent NaN propagation | Pydantic Optional[float] with explicit None semantics |
| §13.5 Posterior sanity checks | Pydantic `Field(ge=0.0, le=1.0)` on probability-like fields |
| Migration idempotency | Tasks 4, 6 |

**Items deferred** (correctly out of A.2 scope, called out at top of plan):
- A.3: refactor screen.py / factors.py to read canonical_universe
- A.4: notebook extension
- A.5: deliberately-corrupted-value acceptance test
- Position state tables: Phase E
- expression_evaluations, ranked_trades: Phases C, D

**Placeholder scan:** No "TBD", "TODO", "implement later". Every code step contains the full source.

**Type consistency:** Each v2 model is defined exactly once and consistently referenced in helpers + tests. SQL column names match Pydantic field names 1:1.

---

## Execution Handoff

Plan complete and saved to `docs/claude-code/plans/2026-05-21-multi-source-data-adapter-phase-a2.md`. Two execution options:

**1. Subagent-Driven (recommended)** — Each task dispatched to a fresh subagent, with review between tasks.

**2. Inline Execution** — Tasks run in this session using `superpowers:executing-plans`, with batch checkpoints for review.

A.2 is smaller than A.1 was (7 tasks vs 14, all touching the same 2 source files). Likely 2-3 waves max:
- Wave 1: Task 0 + Tasks 1-3 (Pydantic models, sequential within one agent)
- Wave 2: Tasks 4-5 (migrate_to_v2 + insert helpers, one agent)
- Wave 3: Task 6 + Task 7 (integration test + build plan update, can run in parallel)
