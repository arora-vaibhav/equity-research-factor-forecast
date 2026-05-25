# Phase A.3.1 — Watermarks Foundation + .env Loader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]` checkbox syntax for tracking.

**Goal:** Lay the persistent-accumulation foundation for all subsequent A.3 sub-phases. Migrate the database from schema v2 to v3 by adding the `fetch_watermarks` table; switch `historical_price` insert semantics from `INSERT OR REPLACE` to `INSERT OR IGNORE`; add watermark management methods on `DatabaseManager`; extend `BaseDataSource` with `get_fetch_gap()` and `update_watermark()` helpers; build the `.env` loader for API keys and the `api_keys.yaml` provider mapping. This sub-phase unblocks every later A.3 sub-phase.

**Architecture:** Additive only. The migration adds one new table and switches one INSERT variant inside an existing helper — no destructive changes. All v2 schema and data preserved. New code in `src/common/database.py` (extended), `src/common/schemas.py` (extended), `src/common/datasources/base.py` (extended), `src/common/env_loader.py` (new), `config/api_keys.yaml` (new). No source-adapter logic changes in this sub-phase.

**Tech Stack:** Python 3.11+, `sqlite3` (stdlib), `pydantic` v2, `pyyaml`, `pytest`. No new runtime dependencies.

**Spec reference:** [docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md](../specs/2026-05-21-phase-a3-layer1-hardening-design.md) §5 (Persistent data accumulation) and §6.8 (.env loader).

**Out of scope for A.3.1:**
- Any source's actual `fetch_*` real implementation → A.3.2 onward
- News activity score sub-signals → A.3.4, A.3.8, A.3.9
- Yang-Zhang vol → A.3.10
- factors.py refactor → A.3.10
- Dual-write logic → A.3.10
- INSERT OR IGNORE on `historical_iv` / `historical_earnings_reactions` → A.3.2 / A.3.4 when those data flow

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| `src/common/schemas.py` | Add `FetchWatermark` Pydantic model | Modify |
| `src/common/database.py` | Add `migrate_to_v3()`, watermark helpers, `force_refetch()`, switch `insert_historical_price` to INSERT OR IGNORE | Modify |
| `src/common/datasources/base.py` | Add `get_fetch_gap()` + `update_watermark()` to `BaseDataSource` | Modify |
| `src/common/env_loader.py` | NEW — `.env` reader + `get_api_key()` | Create |
| `config/api_keys.yaml` | NEW — provider → env-var mapping | Create |
| `tests/common/test_schemas_a3.py` | Tests for FetchWatermark | Create |
| `tests/common/test_database_a3.py` | Tests for migrate_to_v3 + watermark CRUD + force_refetch + INSERT OR IGNORE | Create |
| `tests/datasources/test_base_watermarks.py` | Tests for BaseDataSource fetch-gap/update-watermark | Create |
| `tests/common/test_env_loader.py` | Tests for env loader + get_api_key | Create |
| `tests/common/test_integration_a3_1.py` | End-to-end v2→v3 + watermark flow + env loader | Create |
| the build plan | Mark A.3.1 shipped | Modify |

---

## Task 0: Pre-flight verification

**Files:** none modified

- [ ] **Step 1: Verify schema is at v2 from A.2**

Run from the repository root:

```
./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager(); print('current schema version:', m.get_schema_version())"
```

Expected: prints `current schema version: 2`.

- [ ] **Step 2: Verify A.2 tests still pass (no regressions inherited)**

Run: `./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -3`
Expected: `109 passed, 2 deselected` (the prior A.2-shipped baseline).

- [ ] **Step 3: Verify working tree is clean before starting**

Run: `git status -s`
Expected: empty output. If anything is uncommitted, stop and report.

No commit at this task — verification only.

---

## Task 1: `FetchWatermark` Pydantic model

**Files:**
- Modify: `src/common/schemas.py` (append)
- Create: `tests/common/test_schemas_a3.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_schemas_a3.py`:

```python
"""Tests for Phase A.3 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import FetchWatermark


class TestFetchWatermark:
    def test_minimal_valid(self):
        w = FetchWatermark(
            source="yahoo",
            ticker="AAPL",
            field="historical_price",
            last_fetched_at="2026-05-21T08:00:00Z",
        )
        assert w.source == "yahoo"
        assert w.ticker == "AAPL"
        assert w.field == "historical_price"
        assert w.fetch_count == 0
        assert w.error_count == 0
        assert w.last_observation_date is None
        assert w.last_error_message is None

    def test_full_valid(self):
        w = FetchWatermark(
            source="edgar",
            ticker="MSFT",
            field="fundamentals_xbrl",
            last_fetched_at="2026-05-21T08:00:00Z",
            last_observation_date="2026-05-20",
            fetch_count=42,
            error_count=2,
            last_error_message="connection timeout",
        )
        assert w.fetch_count == 42
        assert w.error_count == 2

    def test_wildcard_ticker_allowed(self):
        # Some watermarks aren't ticker-scoped (e.g., FRED macro series).
        # Convention: ticker='*'. Must not raise.
        w = FetchWatermark(
            source="fred",
            ticker="*",
            field="series:DGS10",
            last_fetched_at="2026-05-21T08:00:00Z",
        )
        assert w.ticker == "*"

    def test_negative_fetch_count_rejected(self):
        with pytest.raises(Exception):
            FetchWatermark(
                source="yahoo",
                ticker="AAPL",
                field="price",
                last_fetched_at="2026-05-21T08:00:00Z",
                fetch_count=-1,
            )

    def test_negative_error_count_rejected(self):
        with pytest.raises(Exception):
            FetchWatermark(
                source="yahoo",
                ticker="AAPL",
                field="price",
                last_fetched_at="2026-05-21T08:00:00Z",
                error_count=-1,
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3.py -v`
Expected: ImportError on `FetchWatermark`.

- [ ] **Step 3: Append `FetchWatermark` to `src/common/schemas.py`**

At the **end** of `src/common/schemas.py`, append:

```python


# === Phase A.3 - persistent accumulation model =========================


class FetchWatermark(BaseModel):
    """Per (source, ticker, field) tracking of what we've already fetched.

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md section 5.1.
    PRIMARY KEY of the underlying table is (source, ticker, field).
    `ticker='*'` is the convention for non-ticker-scoped data such as
    FRED macro series.
    """

    source: str
    ticker: str
    field: str
    last_fetched_at: str
    last_observation_date: Optional[str] = None
    fetch_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    last_error_message: Optional[str] = None
```

(Uses existing imports at the top of schemas.py: `BaseModel`, `Field`, `Optional`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/schemas.py tests/common/test_schemas_a3.py
git commit -m "feat(schemas): add FetchWatermark model for A.3 persistent accumulation"
```

---

## Task 2: `migrate_to_v3()` + `historical_price` INSERT OR IGNORE switch

**Files:**
- Modify: `src/common/database.py`
- Create: `tests/common/test_database_a3.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_database_a3.py`:

```python
"""Tests for Phase A.3.1 DatabaseManager extensions."""
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


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as c:
        rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def test_migrate_to_v3_creates_fetch_watermarks_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    assert "fetch_watermarks" in _table_names(db_path)


def test_fetch_watermarks_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    cols = _columns(db_path, "fetch_watermarks")
    expected = {
        "source", "ticker", "field",
        "last_fetched_at", "last_observation_date",
        "fetch_count", "error_count", "last_error_message",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v3_bumps_schema_version_to_3(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3]


def test_migrate_to_v3_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.migrate_to_v3()
    mgr.migrate_to_v3()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3]


def test_get_schema_version_returns_3_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    assert mgr.get_schema_version() == 3


def test_insert_historical_price_now_uses_insert_or_ignore(tmp_path: Path):
    """Per Principle 6: re-inserting same (ticker, date) does NOT overwrite.
    Previously INSERT OR REPLACE; A.3.1 switches to INSERT OR IGNORE."""
    from src.common.schemas import HistoricalPriceRow
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()

    first = HistoricalPriceRow(
        ticker="AAPL", observation_date="2026-05-20",
        open=100.0, high=110.0, low=99.0, close=105.0,
        volume=1_000_000, adj_close=105.0,
        source="yahoo", scrape_timestamp="2026-05-21T08:00:00Z",
    )
    mgr.insert_historical_price([first])

    second = HistoricalPriceRow(
        ticker="AAPL", observation_date="2026-05-20",
        open=200.0, high=220.0, low=199.0, close=210.0,
        volume=2_000_000, adj_close=210.0,
        source="yahoo", scrape_timestamp="2026-05-21T09:00:00Z",
    )
    mgr.insert_historical_price([second])

    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT close FROM historical_price WHERE ticker='AAPL' AND observation_date='2026-05-20'"
        ).fetchall()
    assert rows == [(105.0,)], f"first write wins; got {rows}"


def test_migrate_to_v3_preserves_v2_data(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v2()
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO canonical_universe (run_id, ticker) VALUES (?, ?)",
            ("sentinel-r", "TSLA"),
        )
        c.commit()
    mgr.migrate_to_v3()
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT run_id, ticker FROM canonical_universe WHERE run_id='sentinel-r'"
        ).fetchall()
    assert rows == [("sentinel-r", "TSLA")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_database_a3.py -v`
Expected: `AttributeError: 'DatabaseManager' object has no attribute 'migrate_to_v3'`.

- [ ] **Step 3: Add `migrate_to_v3()` to `DatabaseManager`**

In `src/common/database.py`, **locate** `migrate_to_v2()`. Immediately AFTER it (before `get_connection`), add:

```python
    def migrate_to_v3(self) -> None:
        """Idempotent migration v2 -> v3 per A.3 spec section 5.

        Adds fetch_watermarks table (per (source, ticker, field) accumulation tracking).
        Note: switching insert_historical_price to INSERT OR IGNORE is done as a
        code change in that helper, not via DDL.

        Safe to call multiple times. Existing data preserved.
        """
        self.migrate_to_v2()  # ensure v2 baseline

        v3_ddl = [
            """
            CREATE TABLE IF NOT EXISTS fetch_watermarks (
              source TEXT NOT NULL,
              ticker TEXT NOT NULL,
              field TEXT NOT NULL,
              last_fetched_at TEXT NOT NULL,
              last_observation_date TEXT,
              fetch_count INTEGER NOT NULL DEFAULT 0,
              error_count INTEGER NOT NULL DEFAULT 0,
              last_error_message TEXT,
              PRIMARY KEY (source, ticker, field)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_watermark_source ON fetch_watermarks(source)",
            "CREATE INDEX IF NOT EXISTS idx_watermark_ticker ON fetch_watermarks(ticker)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v3_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 3")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (3, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()
```

- [ ] **Step 4: Change `insert_historical_price` to INSERT OR IGNORE**

In `src/common/database.py`, **locate** `insert_historical_price` (the A.2 helper). Replace the entire method body with:

```python
    def insert_historical_price(self, rows: list) -> None:
        """Insert HistoricalPriceRow records into historical_price.

        Uses INSERT OR IGNORE per Principle 6 (persistent accumulation): once a
        (ticker, observation_date) row is stored it is IMMUTABLE. Re-inserts of
        the same primary key are silently dropped. To overwrite, the operator
        must explicitly call force_refetch() first.

        Changed from INSERT OR REPLACE in A.3.1.
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
                INSERT OR IGNORE INTO historical_price
                  (ticker, observation_date, open, high, low, close,
                   volume, adj_close, source, scrape_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_database_a3.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 6: Run full non-integration suite — no regressions**

Run: `./venv/Scripts/python.exe -m pytest -m "not integration" -v 2>&1 | tail -3`
Expected: 116 passed (109 A.2 baseline + 7 new).

- [ ] **Step 7: Commit**

```
git add src/common/database.py tests/common/test_database_a3.py
git commit -m "feat(database): migrate_to_v3 - fetch_watermarks table + historical_price INSERT OR IGNORE per Principle 6"
```

---

## Task 3: Watermark CRUD on `DatabaseManager`

**Files:**
- Modify: `src/common/database.py` (append methods)
- Modify: `tests/common/test_database_a3.py` (append tests)

- [ ] **Step 1: Append failing tests**

At the bottom of `tests/common/test_database_a3.py`, append:

```python


# Task 3: watermark CRUD helpers
from src.common.schemas import FetchWatermark  # noqa: E402


def test_get_watermark_returns_none_when_absent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    assert mgr.get_watermark("yahoo", "AAPL", "historical_price") is None


def test_upsert_watermark_insert_then_get_roundtrips(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-20",
        success=True, error_message=None,
    )
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w is not None
    assert w["source"] == "yahoo"
    assert w["ticker"] == "AAPL"
    assert w["field"] == "historical_price"
    assert w["last_observation_date"] == "2026-05-20"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0
    assert w["last_error_message"] is None


def test_upsert_watermark_second_call_increments_fetch_count(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-20", success=True,
    )
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-21", success=True,
    )
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w["fetch_count"] == 2
    assert w["error_count"] == 0
    assert w["last_observation_date"] == "2026-05-21"


def test_upsert_watermark_failure_increments_error_count(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date=None,
        success=False, error_message="429 too many requests",
    )
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w["fetch_count"] == 0
    assert w["error_count"] == 1
    assert w["last_error_message"] == "429 too many requests"
    assert w["last_observation_date"] is None


def test_upsert_watermark_failure_does_not_advance_observation_date(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-20", success=True,
    )
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date=None,
        success=False, error_message="timeout",
    )
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w["last_observation_date"] == "2026-05-20"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_database_a3.py -v`
Expected: `AttributeError` on `get_watermark`/`upsert_watermark`.

- [ ] **Step 3: Append CRUD methods to `DatabaseManager`**

In `src/common/database.py`, at the **end** of the class (after the A.2 insert helpers), append:

```python
    # ------------------------------------------------------------------
    # Phase A.3.1: fetch_watermarks CRUD
    # ------------------------------------------------------------------

    def get_watermark(self, source: str, ticker: str, field: str) -> dict | None:
        """Return watermark dict for (source, ticker, field) or None if absent."""
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT source, ticker, field, last_fetched_at, last_observation_date,
                       fetch_count, error_count, last_error_message
                FROM fetch_watermarks
                WHERE source = ? AND ticker = ? AND field = ?
                """,
                (source, ticker, field),
            ).fetchone()
        if row is None:
            return None
        return {
            "source": row[0],
            "ticker": row[1],
            "field": row[2],
            "last_fetched_at": row[3],
            "last_observation_date": row[4],
            "fetch_count": row[5],
            "error_count": row[6],
            "last_error_message": row[7],
        }

    def upsert_watermark(
        self,
        source: str,
        ticker: str,
        field: str,
        last_observation_date: str | None,
        success: bool,
        error_message: str | None = None,
    ) -> None:
        """Insert or update a watermark row.

        On success: fetch_count += 1; last_observation_date updates IF caller
        passes a non-null value.
        On failure: error_count += 1; last_error_message updates;
        last_observation_date is NOT advanced.
        last_fetched_at always set to current UTC time.
        """
        now_iso = datetime.datetime.utcnow().isoformat()
        existing = self.get_watermark(source, ticker, field)
        if existing is None:
            with self.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO fetch_watermarks
                      (source, ticker, field, last_fetched_at, last_observation_date,
                       fetch_count, error_count, last_error_message)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source, ticker, field, now_iso,
                        last_observation_date if success else None,
                        1 if success else 0,
                        0 if success else 1,
                        None if success else error_message,
                    ),
                )
                conn.commit()
            return

        new_fetch_count = existing["fetch_count"] + (1 if success else 0)
        new_error_count = existing["error_count"] + (0 if success else 1)
        new_obs_date = (
            last_observation_date if (success and last_observation_date is not None)
            else existing["last_observation_date"]
        )
        new_err_msg = None if success else (error_message or existing["last_error_message"])
        with self.get_connection() as conn:
            conn.execute(
                """
                UPDATE fetch_watermarks
                SET last_fetched_at = ?, last_observation_date = ?,
                    fetch_count = ?, error_count = ?, last_error_message = ?
                WHERE source = ? AND ticker = ? AND field = ?
                """,
                (now_iso, new_obs_date, new_fetch_count, new_error_count, new_err_msg,
                 source, ticker, field),
            )
            conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_database_a3.py -v`
Expected: All 12 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/database.py tests/common/test_database_a3.py
git commit -m "feat(database): add fetch_watermarks CRUD (get_watermark + upsert_watermark)"
```

---

## Task 4: `force_refetch()`

**Files:**
- Modify: `src/common/database.py` (append method)
- Modify: `tests/common/test_database_a3.py` (append tests)

- [ ] **Step 1: Append failing tests**

```python


# Task 4: force_refetch
from src.common.schemas import HistoricalPriceRow  # noqa: E402


def test_force_refetch_deletes_historical_price_rows_from_date(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    rows = [
        HistoricalPriceRow(
            ticker="AAPL", observation_date=f"2026-05-{20 + i}",
            open=100.0, high=110.0, low=99.0, close=100.0 + i,
            volume=1_000_000, adj_close=100.0 + i,
            source="yahoo", scrape_timestamp="2026-05-21T08:00:00Z",
        )
        for i in range(5)
    ]
    mgr.insert_historical_price(rows)
    mgr.upsert_watermark("yahoo", "AAPL", "historical_price",
                          last_observation_date="2026-05-24", success=True)

    deleted = mgr.force_refetch("yahoo", "AAPL", "historical_price", from_date="2026-05-22")
    assert deleted == 3

    with sqlite3.connect(db_path) as c:
        remaining = sorted(r[0] for r in c.execute(
            "SELECT observation_date FROM historical_price WHERE ticker='AAPL'"
        ))
    assert remaining == ["2026-05-20", "2026-05-21"]


def test_force_refetch_resets_watermark_last_observation_date(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.upsert_watermark("yahoo", "AAPL", "historical_price",
                          last_observation_date="2026-05-24", success=True)
    mgr.force_refetch("yahoo", "AAPL", "historical_price", from_date="2026-05-22")
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w["last_observation_date"] == "2026-05-21"


def test_force_refetch_logs_to_source_run_log(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.force_refetch("yahoo", "AAPL", "historical_price", from_date="2026-05-22")
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT source, status FROM source_run_log WHERE source='yahoo' AND status='force_refetch'"
        ).fetchall()
    assert len(rows) == 1


def test_force_refetch_with_no_existing_rows_does_not_raise(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    deleted = mgr.force_refetch("yahoo", "ZZZZ", "historical_price", from_date="2026-05-22")
    assert deleted == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_database_a3.py -v`
Expected: `AttributeError` on `force_refetch`.

- [ ] **Step 3: Append `force_refetch` to `DatabaseManager`**

```python
    def force_refetch(
        self,
        source: str,
        ticker: str,
        field: str,
        from_date: str,
    ) -> int:
        """Operator-only: delete time-series rows from from_date onward and
        reset the watermark to from_date - 1.

        Per A.3 spec section 5.4, the ONLY supported way to overwrite historical
        observations once stored. Never called automatically. Logged in
        source_run_log with status='force_refetch'.

        A.3.1 supports field='historical_price' only. Other time-series tables
        get their support added in the sub-phase that populates them.

        Returns the number of rows deleted.
        """
        import datetime as _dt
        if field != "historical_price":
            raise NotImplementedError(
                f"force_refetch for field {field!r} not yet implemented; "
                f"only historical_price is supported in A.3.1"
            )
        with self.get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM historical_price WHERE ticker = ? AND observation_date >= ?",
                (ticker, from_date),
            )
            deleted = cur.rowcount

            try:
                prior_date = (_dt.date.fromisoformat(from_date) - _dt.timedelta(days=1)).isoformat()
            except ValueError:
                prior_date = None
            existing = conn.execute(
                "SELECT 1 FROM fetch_watermarks WHERE source=? AND ticker=? AND field=?",
                (source, ticker, field),
            ).fetchone()
            now_iso = _dt.datetime.utcnow().isoformat()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO fetch_watermarks
                      (source, ticker, field, last_fetched_at, last_observation_date,
                       fetch_count, error_count, last_error_message)
                    VALUES (?, ?, ?, ?, ?, 0, 0, NULL)
                    """,
                    (source, ticker, field, now_iso, prior_date),
                )
            else:
                conn.execute(
                    """
                    UPDATE fetch_watermarks
                    SET last_observation_date = ?, last_fetched_at = ?
                    WHERE source = ? AND ticker = ? AND field = ?
                    """,
                    (prior_date, now_iso, source, ticker, field),
                )

            conn.execute(
                """
                INSERT INTO source_run_log
                  (run_id, source, started_at, finished_at, status,
                   rows_fetched, error_message, error_traceback)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"force_refetch-{now_iso}",
                    source,
                    now_iso,
                    now_iso,
                    "force_refetch",
                    deleted,
                    f"operator-forced refetch of ({ticker}, {field}) from {from_date}",
                    None,
                ),
            )
            conn.commit()
        return deleted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_database_a3.py -v`
Expected: All 16 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/database.py tests/common/test_database_a3.py
git commit -m "feat(database): add force_refetch - operator-only deletion + watermark reset + audit log"
```

---

## Task 5: `BaseDataSource.get_fetch_gap()` + `update_watermark()`

**Files:**
- Modify: `src/common/datasources/base.py`
- Create: `tests/datasources/test_base_watermarks.py`

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_base_watermarks.py`:

```python
"""Tests for BaseDataSource watermark helpers added in A.3.1."""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


class _StubSource(BaseDataSource):
    name = "stub"
    cadence = "weekly"
    provides = {"historical_price"}

    def health_check(self) -> SourceHealthStatus:
        return SourceHealthStatus(source=self.name, status=SourceStatus.OK, checked_at="t")


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v3()
    return mgr


def test_get_fetch_gap_returns_full_lookback_when_no_watermark(db):
    src = _StubSource()
    start, end = src.get_fetch_gap("AAPL", "historical_price", db, max_lookback_days=365)
    today = datetime.date.today()
    assert end == today
    assert start == today - datetime.timedelta(days=365)


def test_get_fetch_gap_returns_delta_when_watermark_exists(db):
    src = _StubSource()
    db.upsert_watermark(
        source="stub", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-15", success=True,
    )
    start, end = src.get_fetch_gap("AAPL", "historical_price", db)
    assert start == datetime.date(2026, 5, 16)
    assert end == datetime.date.today()


def test_get_fetch_gap_returns_inverted_window_when_already_current(db):
    src = _StubSource()
    today_iso = datetime.date.today().isoformat()
    db.upsert_watermark(
        source="stub", ticker="AAPL", field="historical_price",
        last_observation_date=today_iso, success=True,
    )
    start, end = src.get_fetch_gap("AAPL", "historical_price", db)
    # start > end signals "nothing to fetch"
    assert start > end


def test_update_watermark_success_writes_through(db):
    src = _StubSource()
    src.update_watermark(
        ticker="AAPL", field="historical_price",
        last_observation_date=datetime.date(2026, 5, 20),
        db=db, success=True,
    )
    w = db.get_watermark("stub", "AAPL", "historical_price")
    assert w is not None
    assert w["last_observation_date"] == "2026-05-20"
    assert w["fetch_count"] == 1


def test_update_watermark_failure_records_error(db):
    src = _StubSource()
    src.update_watermark(
        ticker="AAPL", field="historical_price",
        last_observation_date=None,
        db=db, success=False, error_message="429",
    )
    w = db.get_watermark("stub", "AAPL", "historical_price")
    assert w is not None
    assert w["error_count"] == 1
    assert w["last_error_message"] == "429"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/datasources/test_base_watermarks.py -v`
Expected: `AttributeError` on `get_fetch_gap`/`update_watermark`.

- [ ] **Step 3: Add the two methods to `BaseDataSource`**

In `src/common/datasources/base.py`, **locate** the `BaseDataSource` class. **Inside** the class, AFTER the `fetch_macro_series` method (the last method before the class ends), append:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/datasources/test_base_watermarks.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/base.py tests/datasources/test_base_watermarks.py
git commit -m "feat(datasources): add BaseDataSource get_fetch_gap + update_watermark helpers"
```

---

## Task 6: `.env` loader + `config/api_keys.yaml`

**Files:**
- Create: `src/common/env_loader.py`
- Create: `config/api_keys.yaml`
- Create: `tests/common/test_env_loader.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_env_loader.py`:

```python
"""Tests for src/common/env_loader.py (Phase A.3.1)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.common.env_loader import load_env, get_api_key


def test_load_env_reads_simple_key_value_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\nBAZ=qux\n")
    result = load_env(env_file)
    assert result == {"FOO": "bar", "BAZ": "qux"}


def test_load_env_ignores_comments_and_blank_lines(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# this is a comment\n"
        "\n"
        "REAL_KEY=value\n"
        "# another comment\n"
        "   \n"
        "OTHER=thing\n"
    )
    result = load_env(env_file)
    assert result == {"REAL_KEY": "value", "OTHER": "thing"}


def test_load_env_handles_quoted_values(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'KEY1="quoted value"\n'
        "KEY2='single quoted'\n"
        "KEY3=unquoted\n"
    )
    result = load_env(env_file)
    assert result == {
        "KEY1": "quoted value",
        "KEY2": "single quoted",
        "KEY3": "unquoted",
    }


def test_load_env_missing_file_returns_empty_dict(tmp_path: Path):
    result = load_env(tmp_path / "nonexistent.env")
    assert result == {}


def test_load_env_skips_malformed_lines(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GOOD=value\n"
        "no_equals_sign_here\n"
        "ALSO_GOOD=2\n"
    )
    result = load_env(env_file)
    assert result == {"GOOD": "value", "ALSO_GOOD": "2"}


def test_get_api_key_returns_value_when_present(tmp_path):
    (tmp_path / ".env").write_text("FMP_API_KEY=test-fmp-key\n")
    (tmp_path / "api_keys.yaml").write_text(
        "fmp: FMP_API_KEY\npolygon: POLYGON_API_KEY\n"
    )
    from src.common import env_loader as el
    key = el.get_api_key(
        "fmp",
        env_path=tmp_path / ".env",
        keys_yaml_path=tmp_path / "api_keys.yaml",
    )
    assert key == "test-fmp-key"


def test_get_api_key_returns_none_when_provider_unknown(tmp_path):
    (tmp_path / ".env").write_text("FMP_API_KEY=abc\n")
    (tmp_path / "api_keys.yaml").write_text("fmp: FMP_API_KEY\n")
    from src.common import env_loader as el
    assert el.get_api_key(
        "nonexistent_provider",
        env_path=tmp_path / ".env",
        keys_yaml_path=tmp_path / "api_keys.yaml",
    ) is None


def test_get_api_key_returns_none_when_env_var_not_set(tmp_path):
    (tmp_path / ".env").write_text("")
    (tmp_path / "api_keys.yaml").write_text("fmp: FMP_API_KEY\n")
    from src.common import env_loader as el
    assert el.get_api_key(
        "fmp",
        env_path=tmp_path / ".env",
        keys_yaml_path=tmp_path / "api_keys.yaml",
    ) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_env_loader.py -v`
Expected: ImportError on `src.common.env_loader`.

- [ ] **Step 3: Create `config/api_keys.yaml`**

```yaml
# Provider -> .env variable name mapping (Phase A.3.1).
# When a provider's env var is missing, that provider's adapter degrades
# silently. Other priority-ranked sources still cover the canonical field.

fmp:      FMP_API_KEY
polygon:  POLYGON_API_KEY
tiingo:   TIINGO_API_KEY
intrinio: INTRINIO_API_KEY
```

- [ ] **Step 4: Create `src/common/env_loader.py`**

```python
"""Minimal .env reader + provider->API-key lookup (Phase A.3.1).

No new runtime dependency: parses simple `KEY=value` directly.
Supports comments (#), blank lines, and quoted values (single or double).
Missing files return empty mappings — sources degrade gracefully.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml


_DEFAULT_ENV_PATH = Path(".env")
_DEFAULT_KEYS_YAML_PATH = Path("config") / "api_keys.yaml"


def load_env(path: Path | str = _DEFAULT_ENV_PATH) -> dict[str, str]:
    """Read a .env file and return its mapping.

    Each line is `KEY=value`. Comments (lines starting with `#`) and
    blank lines are ignored. Values may be optionally wrapped in single
    or double quotes — quotes are stripped. Lines without `=` are skipped.

    Missing file returns {}.
    """
    p = Path(path)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if not key:
            continue
        out[key] = value
    return out


def _load_provider_mapping(keys_yaml_path: Path | str = _DEFAULT_KEYS_YAML_PATH) -> dict[str, str]:
    p = Path(keys_yaml_path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {str(k): str(v) for k, v in data.items()}


def get_api_key(
    provider: str,
    env_path: Path | str = _DEFAULT_ENV_PATH,
    keys_yaml_path: Path | str = _DEFAULT_KEYS_YAML_PATH,
) -> Optional[str]:
    """Look up the API key for `provider` by resolving its env var.

    1. Read api_keys.yaml -> {provider_name: env_var_name}
    2. If provider not in mapping, return None
    3. Read .env -> {env_var: value}
    4. Return env value, or None if absent
    """
    mapping = _load_provider_mapping(keys_yaml_path)
    env_var_name = mapping.get(provider)
    if env_var_name is None:
        return None
    env = load_env(env_path)
    value = env.get(env_var_name)
    if value is None or value == "":
        return None
    return value
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_env_loader.py -v`
Expected: All 8 tests PASS.

- [ ] **Step 6: Commit**

```
git add src/common/env_loader.py config/api_keys.yaml tests/common/test_env_loader.py
git commit -m "feat(common): add env_loader + config/api_keys.yaml provider mapping"
```

---

## Task 7: Integration test — full A.3.1 acceptance

**Files:**
- Create: `tests/common/test_integration_a3_1.py`

- [ ] **Step 1: Write the integration test**

Create `tests/common/test_integration_a3_1.py`:

```python
"""End-to-end A.3.1 acceptance: v2->v3 migration + watermark flow + force_refetch + env loading."""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)
from src.common.env_loader import get_api_key
from src.common.schemas import HistoricalPriceRow


class _FakeYahooSource(BaseDataSource):
    name = "yahoo"
    cadence = "weekly"
    provides = {"historical_price"}

    def health_check(self) -> SourceHealthStatus:
        return SourceHealthStatus(source=self.name, status=SourceStatus.OK, checked_at="t")


def test_full_watermark_flow_against_stub_source(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    src = _FakeYahooSource()

    start, end = src.get_fetch_gap("AAPL", "historical_price", mgr, max_lookback_days=30)
    assert end == datetime.date.today()
    assert (end - start).days == 30

    rows = [
        HistoricalPriceRow(
            ticker="AAPL", observation_date=f"2026-05-{18 + i}",
            open=100.0, high=110.0, low=99.0, close=105.0 + i,
            volume=1_000_000, adj_close=105.0 + i,
            source="yahoo", scrape_timestamp=datetime.datetime.utcnow().isoformat(),
        )
        for i in range(3)
    ]
    mgr.insert_historical_price(rows)
    src.update_watermark(
        ticker="AAPL", field="historical_price",
        last_observation_date=datetime.date(2026, 5, 20),
        db=mgr, success=True,
    )

    start2, _ = src.get_fetch_gap("AAPL", "historical_price", mgr)
    assert start2 == datetime.date(2026, 5, 21)

    with sqlite3.connect(db_path) as c:
        count = c.execute(
            "SELECT COUNT(*) FROM historical_price WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert count == 3

    mgr.insert_historical_price(rows)  # duplicate insert should be no-op
    with sqlite3.connect(db_path) as c:
        count2 = c.execute(
            "SELECT COUNT(*) FROM historical_price WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert count2 == 3

    deleted = mgr.force_refetch("yahoo", "AAPL", "historical_price", from_date="2026-05-19")
    assert deleted == 2

    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w["last_observation_date"] == "2026-05-18"


def test_env_loader_finds_keys_when_present(tmp_path: Path):
    (tmp_path / ".env").write_text("FMP_API_KEY=fake-key-123\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "api_keys.yaml").write_text(
        "fmp: FMP_API_KEY\npolygon: POLYGON_API_KEY\n"
    )
    key = get_api_key(
        "fmp",
        env_path=tmp_path / ".env",
        keys_yaml_path=tmp_path / "config" / "api_keys.yaml",
    )
    assert key == "fake-key-123"

    assert get_api_key(
        "polygon",
        env_path=tmp_path / ".env",
        keys_yaml_path=tmp_path / "config" / "api_keys.yaml",
    ) is None
```

- [ ] **Step 2: Run the integration test**

Run: `./venv/Scripts/python.exe -m pytest tests/common/test_integration_a3_1.py -v`
Expected: Both tests PASS.

- [ ] **Step 3: Run the full suite (no-regressions check)**

Run: `./venv/Scripts/python.exe -m pytest -m "not integration" -v 2>&1 | tail -3`
Expected: ~145 passed (109 A.2 + 5 schema + 16 database + 5 base + 8 env + 2 integration).

- [ ] **Step 4: Commit**

```
git add tests/common/test_integration_a3_1.py
git commit -m "test: A.3.1 integration acceptance - watermark flow + INSERT OR IGNORE + force_refetch + env loader"
```

---

## Task 8: Update the build plan

**Files:**
- Modify: the build plan

- [ ] **Step 1: Locate the §5.1.0 Sub-phase ordering table**

Find the row for A.3 (currently `| **A.3** | (to be written after A.2 lands) | ...`).

- [ ] **Step 2: Update the A.3 row**

Replace the A.3 row with:

```markdown
| **A.3** | [docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md](docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md) | Full Layer 1 hardening — 10 sub-phases (A.3.1 watermarks, A.3.2 Finviz+Yahoo, A.3.3 EDGAR XBRL, A.3.4 EDGAR Form 4 + 8-K + opportunistic-insider, A.3.5 FRED + FINRA, A.3.6 stockanalysis 10y ratios, A.3.7 OpenBB router, A.3.8 5 sub-signals + GDELT + pytrends, A.3.9 LM tone, A.3.10 Yang-Zhang + factors refactor + dual-write + equivalence). **A.3.1 shipped 2026-05-21** ([plan](docs/claude-code/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md)): schema v3 + fetch_watermarks + watermark CRUD + force_refetch + BaseDataSource gap/update helpers + .env loader + api_keys.yaml. | A.3 DoD: all 7 sources real-fetching, news_activity_score 7-signal, Yang-Zhang vol, dual-write equivalence ≤5% drift, full integration suite green |
```

(A.4 and A.5 rows stay unchanged.)

- [ ] **Step 3: Commit**

```
git add <build-plan>
git commit -m "docs: mark A.3.1 shipped in build plan; reference A.3 spec + sub-phase decomposition"
```

---

## Phase A.3.1 — Definition of Done

- [ ] `./venv/Scripts/python.exe -m pytest -m "not integration"` shows ~145 tests passing
- [ ] `migrate_to_v3()` produces `schema_version == 3`
- [ ] `fetch_watermarks` table exists with correct columns + indexes
- [ ] `historical_price` uses INSERT OR IGNORE (first write wins; duplicates dropped)
- [ ] `get_watermark`, `upsert_watermark`, `force_refetch` work and are tested
- [ ] `BaseDataSource.get_fetch_gap` and `update_watermark` exist + tested via stub subclass
- [ ] `src/common/env_loader.py` exists; `load_env` + `get_api_key` work
- [ ] `config/api_keys.yaml` exists with FMP/Polygon/Tiingo/Intrinio mapping
- [ ] The local `.env` is recognized at runtime
- [ ] `force_refetch` logged in `source_run_log`
- [ ] The build plan marks A.3.1 shipped
- [ ] No A.1 / A.2 regressions
- [ ] Git log shows ~8 small commits

---

## Self-review

**Spec coverage:**

| A.3 spec section | A.3.1 task |
|---|---|
| §5.1 fetch_watermarks table | Task 2 |
| §5.2 BaseDataSource extensions | Task 5 |
| §5.3 INSERT OR IGNORE on time-series (historical_price only this sub-phase) | Task 2 |
| §5.4 force_refetch | Task 4 |
| §5.5 migrate_to_v3 | Task 2 |
| §6.8 .env loader | Task 6 |

**Deferred to later sub-phases (correctly out of A.3.1):**
- INSERT OR IGNORE on `historical_iv`, `historical_earnings_reactions` (when their insert helpers land in A.3.2/A.3.4)
- `force_refetch` for fields other than historical_price (extend as needed when those data flow)

**Placeholder scan:** No TBD/TODO. Every step has full source.

**Pydantic gotcha avoidance** (per A.2 lesson): no cross-field validators in this plan; simple `Field(ge=0)` constraints only — side-steps the declaration-order trap.

---

## Execution Handoff

**Recommended:** Subagent-driven, mirror of the A.2 wave pattern.

- **Wave 1**: One sub-agent does Tasks 0–4 (verify + FetchWatermark + migrate_to_v3 + CRUD + force_refetch). All in `src/common/`; sequential.
- **Wave 2**: One sub-agent does Tasks 5–6 (BaseDataSource extensions + env_loader). Independent of each other; one agent for simplicity.
- **Wave 3**: Sub-agent does Task 7 (integration test); inline does Task 8 (build plan update) in parallel.

Total: 3 waves, ~30-45 min wall-clock.
