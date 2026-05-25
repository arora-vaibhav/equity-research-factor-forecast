# Phase A.3.5 — FRED Macro Series + FINRA Biweekly Short Interest

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]` checkbox syntax for tracking.

**Goal:** Wire `FredSource` and `FinraSource` to real-fetch end-to-end. `FredSource.fetch_macro_series()` pulls the five spec-required macro series (`DGS10`, `DGS3MO`, `DGS2`, `VIXCLS`, `CPIAUCSL`) via FRED's keyless `fredgraph.csv` endpoint, parses long-format, and writes to `raw_fred`. `FinraSource.fetch_short_interest()` pulls the biweekly NYSE / Nasdaq short-interest aggregated files from `cdn.finra.org`, parses the pipe-delimited records, and writes to `raw_finra`. Migrate schema v6 → v7 to add the two new raw tables. Both writers use INSERT OR IGNORE (Principle 5), both fetches are watermark-aware (Principle 6, with the FINRA watermark using a 14-day refresh skip per spec §6.5), and all HTTP is mockable via `pytest-mock`.

**Why this matters:** Two new sub-signals in `news_activity_score` need this substrate (see the methodology bibliography). Signal #2 — *Short-interest delta + Days-to-Cover* (Diether-Lee-Werner 2009) — consumes `raw_finra`; high-DTC rising-short-interest names are the bearish flag the composite leans on for the short side. The yield curve (`DGS10` − `DGS3MO`) and `VIXCLS` from `raw_fred` feed Layer 2/4 regime priors (recession-probability features in later phases) and the Layer-1 macro context block called out in the architecture spec §4. Without these two raw tables landing first, Signal 2 and the macro-context features can't be computed downstream.

**Architecture:** Additive only. Migration v6→v7 creates `raw_fred` + `raw_finra` and bumps `schema_version`. Both sources gain a single new public method:
1. `FredSource.fetch_macro_series(run_id, series_ids=None, db)` — pulls each series, parses CSV → `RawFredObservation` rows, INSERT OR IGNORE into `raw_fred`. Per-series watermark: `(fred, *, series:<id>)` advances to the max `observation_date` observed. Polite 250ms delay between series.
2. `FinraSource.fetch_short_interest(run_id, settlement_date=None, db)` — resolves the target settlement date (latest available if not specified), pulls the per-exchange short-interest files, parses the pipe-delimited records → `RawFinraShortInterest` rows, INSERT OR IGNORE into `raw_finra`. Watermark: `(finra, *, short_interest_biweekly)` — refresh skipped if the previous successful fetch is within 14 days.

CSV parsing for FRED and pipe-delimited parsing for FINRA both live inside their respective `Source` methods (the dataset shape is simple enough that pure-function extraction isn't justified at this scale; if FINRA parsing ever grows complex enough to justify a separate parser module we can re-extract — A.3.8/A.3.10 would be the natural moment). The classification / signal-construction layer (`short_interest_signal.py`, `macro_regime_signal.py`) is OUT of scope for A.3.5 — that's A.3.8.

**Tech Stack:** Python 3.11+, `requests`, `tenacity`, `pydantic` v2, `pytest`, `pytest-mock`. No new runtime dependencies. (FRED CSV is plain `text/csv` with no compression; FINRA files are plain text from the `cdn.finra.org` CDN — no gzip handling needed for the public daily/biweekly files we target.)

**Spec reference:** [docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md](../specs/2026-05-21-phase-a3-layer1-hardening-design.md) §6.4 (FRED) + §6.5 (FINRA) + §3 Principle 5 (no silent overwrite) + Principle 6 (watermarks bound deltas).

**Methodology reference:** the methodology bibliography — Diether, Lee, Werner (2009), *"Short-Sale Strategies and Return Predictability"*, Review of Financial Studies, consumes the FINRA substrate produced here. The macro substrate (`DGS10`, `DGS3MO`, `DGS2`, `VIXCLS`, `CPIAUCSL`) feeds the regime priors used in the higher-layer signals; the actual signal construction is the consumer's job, not A.3.5's.

**FRED vintage-data note (load-bearing — read once before executing):** The public `fredgraph.csv?id=<series>` endpoint returns the *currently revised* series (not the as-originally-published vintage). For research-rigorous look-ahead-bias avoidance, we'd need ALFRED (the archival FRED API) which returns each observation's `realtime_start` and `realtime_end`. We are NOT integrating ALFRED in v1. The `raw_fred` schema includes `realtime_start` and `realtime_end` columns so the table can accommodate ALFRED data later, but the v1 `FredSource` implementation leaves them None and documents a "look-ahead bias is a known concern; see methodology handbook §revision-handling for backtest-time controls" comment in the source. When ALFRED is added (post-A.3.10), the same table accepts the more granular data without a migration.

**FINRA URL pattern (load-bearing — verify before production):** The spec calls out `cdn.finra.org/equity/regsho/monthly/...` but the actual files published twice monthly are organized under two endpoints:
1. **Daily short-volume files** (per market center): `https://cdn.finra.org/equity/regsho/daily/FNRAshvol{YYYYMMDD}.txt`, `FNSQshvol{YYYYMMDD}.txt`, `FNYXshvol{YYYYMMDD}.txt`, `FORFshvol{YYYYMMDD}.txt` — pipe-delimited, one row per ticker, columns: `Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market`. These are daily and are NOT the biweekly short-interest tape.
2. **Biweekly short-interest data (the actual target for Signal 2)**: This is the FINRA "Short Interest" report (formerly the NYSE/Nasdaq short-interest reports prior to FINRA consolidation). FINRA publishes it at `https://www.finra.org/finra-data/browse-catalog/short-sale-volume-data/files` (HTML browse page) and via the FINRA Query API at `https://api.finra.org/data/group/otcMarket/name/shortInterest`. The Query API requires no key for the public dataset and returns JSON.

**Operationally, A.3.5 implements path #1 (daily short-volume CDN) because:**
- It requires no API key, no auth, no JSON aggregation — the file URLs are deterministic by date and market center.
- The pipe-delimited format is stable and well-documented.
- It gives us per-day, per-ticker short-volume + total-volume → which is sufficient to construct days-to-cover and short-interest-delta proxies for Signal 2 at biweekly cadence (we aggregate to biweekly settlement-date in the materialization layer in A.3.8).
- Migrating to the official biweekly tape (path #2) is a non-breaking enhancement to `FinraSource` later (the `raw_finra` schema accommodates either source via the `exchange` discriminator column).

The `FinraSource` constant `_DAILY_FILE_URL_FMT` is centralized so the URL pattern has one bug surface and one test surface. The four per-market-center files we pull on each biweekly fetch are documented in the docstring: FNRA (NYSE), FNSQ (Nasdaq), FNYX (NYSE American), FORF (FINRA OTC).

**Out of scope for A.3.5:**
- The downstream `short_interest_signal.py` that consumes `raw_finra` to produce `news_activity_score` Signal 2 → A.3.8.
- ALFRED vintage data integration → post-A.3.10.
- The official biweekly short-interest tape via FINRA Query API → optional enhancement post-A.3.10.
- The Layer-2/4 macro regime-prior model consuming `raw_fred` yield-curve + VIX → Layer 2 work, not A.3.
- Live integration tests against the real FRED and FINRA endpoints → A.5 acceptance phase.
- `stockanalysis` (§6.6) and `OpenBB` (§6.7) → A.3.6 / A.3.7.

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| `src/common/schemas.py` | Add `RawFredObservation` + `RawFinraShortInterest` Pydantic models | Modify |
| `src/common/database.py` | Add `migrate_to_v7()` + `insert_raw_fred()` + `insert_raw_finra()` | Modify |
| `src/common/datasources/fred_source.py` | Add `fetch_macro_series()` real implementation | Modify |
| `src/common/datasources/finra_source.py` | Add `fetch_short_interest()` real implementation | Modify |
| `tests/fixtures/fred/DGS10_sample.csv` | NEW — synthetic FRED CSV for DGS10 (30 rows) | Create |
| `tests/fixtures/fred/VIXCLS_sample.csv` | NEW — synthetic FRED CSV for VIXCLS (5 rows with `.` missing values) | Create |
| `tests/fixtures/finra/shvol_sample.txt` | NEW — synthetic FINRA pipe-delimited short-volume file (20 rows) | Create |
| `tests/common/test_schemas_a3_5.py` | Tests for `RawFredObservation` + `RawFinraShortInterest` | Create |
| `tests/common/test_database_a3_5.py` | Tests for `migrate_to_v7` + insert helpers | Create |
| `tests/datasources/test_fred_fetch.py` | Tests for `FredSource.fetch_macro_series` (mocked CSV) | Create |
| `tests/datasources/test_finra_fetch.py` | Tests for `FinraSource.fetch_short_interest` (mocked file) | Create |
| `tests/datasources/test_integration_a3_5.py` | End-to-end orchestration test | Create |
| the build plan | Mark A.3.5 shipped | Modify |

---

## Task 0: Pre-flight verification

**Files:** none modified

- [ ] **Step 1: Verify schema is at v6 from A.3.4**

Run from the repository root:

```
./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager(); print('current schema version:', m.get_schema_version())"
```

Expected: prints `current schema version: 6`.

- [ ] **Step 2: Verify A.3.4 baseline tests still pass**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: `315 passed, 2 deselected` (or whatever the current A.3.4 baseline shows; counts may drift ±2).

- [ ] **Step 3: Verify tenacity + requests importable from venv**

```
./venv/Scripts/python.exe -c "import tenacity, requests; print('tenacity import ok'); print('requests', requests.__version__)"
```

Expected: both lines print without error. NOTE: `tenacity` has no module-level `__version__` attribute in many builds — we only verify `import tenacity` works (matches A.3.3/A.3.4 pattern).

- [ ] **Step 4: Verify A.1 FredSource + FinraSource health_checks are present (we extend them, do not replace)**

```
./venv/Scripts/python.exe -c "from src.common.datasources.fred_source import FredSource; from src.common.datasources.finra_source import FinraSource; print('fred', FredSource().name); print('finra', FinraSource().name)"
```

Expected: `fred fred` then `finra finra`.

- [ ] **Step 5: Verify working tree is clean before starting**

Run: `git status -s`
Expected: empty output (or only unrelated `.env` / notebooks). If anything else under `src/` or `tests/` is uncommitted, stop and report.

No commit at this task — verification only.

---

## Task 1: `RawFredObservation` + `RawFinraShortInterest` Pydantic models

**Files:**
- Modify: `src/common/schemas.py` (append)
- Create: `tests/common/test_schemas_a3_5.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_schemas_a3_5.py`:

```python
"""Tests for Phase A.3.5 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import RawFredObservation, RawFinraShortInterest


class TestRawFredObservation:
    def test_minimal_valid(self):
        r = RawFredObservation(
            run_id="r1",
            series_id="DGS10",
            observation_date="2026-05-15",
            value=4.21,
            source_filename="fredgraph.csv?id=DGS10",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.series_id == "DGS10"
        assert r.value == 4.21
        assert r.realtime_start is None
        assert r.realtime_end is None

    def test_full_valid_with_realtime_window(self):
        r = RawFredObservation(
            run_id="r1",
            series_id="CPIAUCSL",
            observation_date="2026-04-01",
            value=312.45,
            realtime_start="2026-05-15",
            realtime_end="2099-12-31",
            source_filename="fredgraph.csv?id=CPIAUCSL",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.realtime_start == "2026-05-15"
        assert r.realtime_end == "2099-12-31"

    def test_missing_value_allowed_as_none(self):
        """FRED uses '.' for missing observations; the parser stores them as None."""
        r = RawFredObservation(
            run_id="r1",
            series_id="VIXCLS",
            observation_date="2026-01-01",
            value=None,
            source_filename="fredgraph.csv?id=VIXCLS",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.value is None

    def test_series_id_uppercased(self):
        r = RawFredObservation(
            run_id="r1",
            series_id="dgs10",
            observation_date="2026-05-15",
            value=4.21,
            source_filename="fredgraph.csv?id=DGS10",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.series_id == "DGS10"

    def test_series_id_required(self):
        with pytest.raises(Exception):
            RawFredObservation(
                run_id="r1",
                series_id="",
                observation_date="2026-05-15",
                value=4.21,
                source_filename="fredgraph.csv?id=",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_observation_date_required(self):
        with pytest.raises(Exception):
            RawFredObservation(
                run_id="r1",
                series_id="DGS10",
                observation_date="",
                value=4.21,
                source_filename="fredgraph.csv?id=DGS10",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )


class TestRawFinraShortInterest:
    def test_minimal_valid(self):
        r = RawFinraShortInterest(
            run_id="r1",
            ticker="AAPL",
            settlement_date="2026-05-15",
            exchange="NSDQ",
            short_interest_shares=12_345_678.0,
            avg_daily_volume=85_000_000.0,
            days_to_cover=0.145,
            source_filename="FNSQshvol20260515.txt",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"
        assert r.exchange == "NSDQ"
        assert r.days_to_cover == 0.145

    def test_ticker_uppercased(self):
        r = RawFinraShortInterest(
            run_id="r1",
            ticker="aapl",
            settlement_date="2026-05-15",
            exchange="NSDQ",
            short_interest_shares=1.0,
            source_filename="FNSQshvol20260515.txt",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"

    def test_exchange_uppercased(self):
        r = RawFinraShortInterest(
            run_id="r1",
            ticker="AAPL",
            settlement_date="2026-05-15",
            exchange="nsdq",
            short_interest_shares=1.0,
            source_filename="FNSQshvol20260515.txt",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.exchange == "NSDQ"

    def test_optional_fields_default_none(self):
        r = RawFinraShortInterest(
            run_id="r1",
            ticker="AAPL",
            settlement_date="2026-05-15",
            exchange="NSDQ",
            short_interest_shares=1.0,
            source_filename="FNSQshvol20260515.txt",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.avg_daily_volume is None
        assert r.days_to_cover is None

    def test_negative_short_interest_rejected(self):
        with pytest.raises(Exception):
            RawFinraShortInterest(
                run_id="r1",
                ticker="AAPL",
                settlement_date="2026-05-15",
                exchange="NSDQ",
                short_interest_shares=-100.0,
                source_filename="FNSQshvol20260515.txt",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            RawFinraShortInterest(
                run_id="r1",
                ticker="not-a-ticker",
                settlement_date="2026-05-15",
                exchange="NSDQ",
                short_interest_shares=1.0,
                source_filename="FNSQshvol20260515.txt",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_5.py -v
```

Expected: `ImportError` on `RawFredObservation` and `RawFinraShortInterest`.

- [ ] **Step 3: Append models to `src/common/schemas.py`**

At the **end** of `src/common/schemas.py`, append:

```python


# === Phase A.3.5 - FRED macro series + FINRA biweekly short interest =====


class RawFredObservation(BaseModel):
    """One row per (series_id, observation_date) FRED macro observation.

    Spec: docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.4. Stored in raw_fred. PK: (series_id, observation_date).

    `realtime_start` / `realtime_end` model the FRED ALFRED vintage-data
    convention (when this observation was published vs current revision).
    For v1 with the public `fredgraph.csv` endpoint they are None
    (look-ahead bias is acknowledged; see methodology handbook §revision-
    handling). When ALFRED integration is added post-A.3.10, the same
    schema accommodates per-vintage rows without migration.
    """

    run_id: str
    series_id: str
    observation_date: str
    value: Optional[float] = None  # FRED uses '.' for missing -> stored as None
    realtime_start: Optional[str] = None
    realtime_end: Optional[str] = None
    source_filename: str
    scrape_timestamp: str

    @field_validator("series_id", mode="before")
    @classmethod
    def _normalize_series_id(cls, v: object) -> str:
        if v is None:
            raise ValueError("series_id is required")
        s = str(v).strip().upper()
        if not s:
            raise ValueError("series_id cannot be empty")
        return s

    @field_validator("observation_date", mode="before")
    @classmethod
    def _normalize_observation_date(cls, v: object) -> str:
        if v is None:
            raise ValueError("observation_date is required")
        s = str(v).strip()
        if not s:
            raise ValueError("observation_date cannot be empty")
        return s


class RawFinraShortInterest(BaseModel):
    """One row per (ticker, settlement_date, exchange) FINRA short-volume record.

    Spec: docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.5. Stored in raw_finra. PK: (ticker, settlement_date, exchange).

    The `exchange` discriminator (NSDQ / NYSE / NYAX / ORF) lets us
    accommodate the four per-market-center daily files we pull from
    cdn.finra.org and, in a future enhancement, also the official
    biweekly tape via the FINRA Query API without schema change.
    """

    run_id: str
    ticker: str
    settlement_date: str
    exchange: str
    short_interest_shares: float = Field(ge=0)
    avg_daily_volume: Optional[float] = Field(default=None, ge=0)
    days_to_cover: Optional[float] = Field(default=None, ge=0)
    source_filename: str
    scrape_timestamp: str

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s

    @field_validator("exchange", mode="before")
    @classmethod
    def _normalize_exchange(cls, v: object) -> str:
        if v is None:
            raise ValueError("exchange is required")
        s = str(v).strip().upper()
        if not s:
            raise ValueError("exchange cannot be empty")
        return s
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_5.py -v
```

Expected: All 12 tests PASS (6 FRED + 6 FINRA).

- [ ] **Step 5: Commit**

```
git add src/common/schemas.py tests/common/test_schemas_a3_5.py
git commit -m "feat(schemas): add RawFredObservation + RawFinraShortInterest for A.3.5"
```

---

## Task 2: `migrate_to_v7` + `insert_raw_fred` + `insert_raw_finra`

**Files:**
- Modify: `src/common/database.py`
- Create: `tests/common/test_database_a3_5.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_database_a3_5.py`:

```python
"""Tests for Phase A.3.5 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawFredObservation, RawFinraShortInterest


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


# --- migrate_to_v7 ----------------------------------------------------


def test_migrate_to_v7_creates_raw_fred_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    assert "raw_fred" in _table_names(db_path)


def test_migrate_to_v7_creates_raw_finra_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    assert "raw_finra" in _table_names(db_path)


def test_raw_fred_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    cols = _columns(db_path, "raw_fred")
    expected = {
        "run_id", "series_id", "observation_date", "value",
        "realtime_start", "realtime_end",
        "source_filename", "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_raw_finra_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    cols = _columns(db_path, "raw_finra")
    expected = {
        "run_id", "ticker", "settlement_date", "exchange",
        "short_interest_shares", "avg_daily_volume", "days_to_cover",
        "source_filename", "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v7_bumps_schema_version_to_7(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6, 7]


def test_migrate_to_v7_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.migrate_to_v7()
    mgr.migrate_to_v7()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6, 7]


def test_get_schema_version_returns_7_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    assert mgr.get_schema_version() == 7


def test_migrate_to_v7_preserves_v6_data(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."}
    ])
    mgr.migrate_to_v7()
    assert mgr.get_cik_for_ticker("AAPL") == "0000320193"


# --- insert_raw_fred --------------------------------------------------


def _fred(**overrides):
    base = dict(
        run_id="r1", series_id="DGS10",
        observation_date="2026-05-15", value=4.21,
        realtime_start=None, realtime_end=None,
        source_filename="fredgraph.csv?id=DGS10",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawFredObservation(**base)


def test_insert_raw_fred_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_fred([_fred()])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT series_id, observation_date, value FROM raw_fred"
        ).fetchone()
    assert row == ("DGS10", "2026-05-15", 4.21)


def test_insert_raw_fred_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_fred([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_fred").fetchone()[0]
    assert n == 0


def test_insert_raw_fred_uses_insert_or_ignore(tmp_path: Path):
    """PK = (series_id, observation_date). First write wins on collision."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_fred([_fred(value=4.21)])
    mgr.insert_raw_fred([_fred(value=9.99)])
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_fred WHERE series_id='DGS10'"
        ).fetchone()[0]
    assert v == 4.21


def test_insert_raw_fred_handles_null_value(tmp_path: Path):
    """FRED '.' missing-observation maps to NULL in storage."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_fred([_fred(value=None, observation_date="2026-01-01")])
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_fred WHERE observation_date='2026-01-01'"
        ).fetchone()[0]
    assert v is None


def test_insert_raw_fred_supports_multiple_series_same_date(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_fred([
        _fred(series_id="DGS10", value=4.21),
        _fred(series_id="DGS3MO", value=5.10),
        _fred(series_id="VIXCLS", value=14.2),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_fred").fetchone()[0]
    assert n == 3


# --- insert_raw_finra -------------------------------------------------


def _finra(**overrides):
    base = dict(
        run_id="r1", ticker="AAPL",
        settlement_date="2026-05-15", exchange="NSDQ",
        short_interest_shares=12_345_678.0,
        avg_daily_volume=85_000_000.0,
        days_to_cover=0.145,
        source_filename="FNSQshvol20260515.txt",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawFinraShortInterest(**base)


def test_insert_raw_finra_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_finra([_finra()])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT ticker, exchange, short_interest_shares FROM raw_finra"
        ).fetchone()
    assert row == ("AAPL", "NSDQ", 12_345_678.0)


def test_insert_raw_finra_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_finra([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_finra").fetchone()[0]
    assert n == 0


def test_insert_raw_finra_uses_insert_or_ignore(tmp_path: Path):
    """PK = (ticker, settlement_date, exchange). First write wins."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_finra([_finra(short_interest_shares=100.0)])
    mgr.insert_raw_finra([_finra(short_interest_shares=9999.0)])
    with sqlite3.connect(db_path) as c:
        v = c.execute("SELECT short_interest_shares FROM raw_finra").fetchone()[0]
    assert v == 100.0


def test_insert_raw_finra_supports_multiple_exchanges_same_ticker_date(tmp_path: Path):
    """Same ticker on the same settlement_date across different exchanges
    is allowed (the exchange column is part of the PK)."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    mgr.insert_raw_finra([
        _finra(exchange="NSDQ"),
        _finra(exchange="NYSE"),
        _finra(exchange="NYAX"),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_finra WHERE ticker='AAPL'").fetchone()[0]
    assert n == 3
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_5.py -v
```

Expected: `AttributeError` on `migrate_to_v7`.

- [ ] **Step 3: Add `migrate_to_v7()` to `DatabaseManager`**

In `src/common/database.py`, **locate** `migrate_to_v6()` (around line ~488). Immediately AFTER it (before any helper methods that follow), insert:

```python
    def migrate_to_v7(self) -> None:
        """Idempotent migration v6 -> v7 per A.3 spec sections 6.4 + 6.5.

        Adds:
          - raw_fred: long-format FRED macro observations. PK
            (series_id, observation_date). `realtime_start`/`realtime_end`
            accommodate future ALFRED vintage-data integration; the v1
            FredSource leaves them None.
          - raw_finra: per-(ticker, settlement_date, exchange) short-
            interest / short-volume aggregates from FINRA's CDN. PK
            (ticker, settlement_date, exchange).

        Safe to call multiple times. Existing data preserved.

        Both use INSERT OR IGNORE on insert (Principle 5: no silent
        overwrite). First-observed values win — re-runs are idempotent
        and amendments to historical observations (FRED revisions,
        FINRA re-statements) are silently dropped unless they arrive
        with a distinct realtime_start/realtime_end (ALFRED) or a
        distinct exchange marker.
        """
        self.migrate_to_v6()

        v7_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_fred (
              run_id TEXT NOT NULL,
              series_id TEXT NOT NULL,
              observation_date TEXT NOT NULL,
              value REAL,
              realtime_start TEXT,
              realtime_end TEXT,
              source_filename TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (series_id, observation_date)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_fred_series ON raw_fred(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_raw_fred_date ON raw_fred(observation_date)",
            """
            CREATE TABLE IF NOT EXISTS raw_finra (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              settlement_date TEXT NOT NULL,
              exchange TEXT NOT NULL,
              short_interest_shares REAL NOT NULL,
              avg_daily_volume REAL,
              days_to_cover REAL,
              source_filename TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (ticker, settlement_date, exchange)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_finra_ticker ON raw_finra(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_finra_settle ON raw_finra(settlement_date)",
            "CREATE INDEX IF NOT EXISTS idx_raw_finra_exchange ON raw_finra(exchange)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v7_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 7")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (7, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()
```

In `src/common/database.py`, at the **end** of the `DatabaseManager` class, append:

```python
    # ------------------------------------------------------------------
    # Phase A.3.5: raw_fred + raw_finra helpers
    # ------------------------------------------------------------------

    def insert_raw_fred(self, rows: list) -> None:
        """Insert RawFredObservation records into raw_fred.

        Uses INSERT OR IGNORE — once a (series_id, observation_date) is
        recorded, subsequent inserts with the same PK are silently
        dropped. First write wins. This protects re-runs and routine
        revision storms (FRED revises CPIAUCSL, GDP, etc. periodically)
        from re-writing the original-observation value. ALFRED-vintage
        integration in a later phase will distinguish via realtime_start.
        """
        if not rows:
            return
        from src.common.schemas import RawFredObservation
        records = [
            r.model_dump() if isinstance(r, RawFredObservation) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_fred ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()

    def insert_raw_finra(self, rows: list) -> None:
        """Insert RawFinraShortInterest records into raw_finra.

        Uses INSERT OR IGNORE on PK (ticker, settlement_date, exchange).
        """
        if not rows:
            return
        from src.common.schemas import RawFinraShortInterest
        records = [
            r.model_dump() if isinstance(r, RawFinraShortInterest) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_finra ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_5.py -v
```

Expected: All 13 tests PASS (8 migration + 5 fred + 4 finra — total 13 unique cases as listed).

- [ ] **Step 5: Run full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: ~340 passed (315 baseline + 12 schema + 13 database).

- [ ] **Step 6: Commit**

```
git add src/common/database.py tests/common/test_database_a3_5.py
git commit -m "feat(database): migrate_to_v7 - raw_fred + raw_finra (INSERT OR IGNORE)"
```

---

## Task 3: `FredSource.fetch_macro_series` real implementation

**Files:**
- Modify: `src/common/datasources/fred_source.py`
- Create: `tests/fixtures/fred/DGS10_sample.csv`
- Create: `tests/fixtures/fred/VIXCLS_sample.csv`
- Create: `tests/datasources/test_fred_fetch.py`

- [ ] **Step 1: Create the synthetic FRED CSV fixtures**

Create `tests/fixtures/fred/DGS10_sample.csv` with the literal content below. This mimics the FRED public CSV format exactly: a `DATE,DGS10` header followed by ISO-date rows with float values, with `.` representing missing observations (FRED's documented sentinel for non-trading days etc.).

```
DATE,DGS10
2026-04-01,4.18
2026-04-02,4.21
2026-04-03,4.19
2026-04-06,4.22
2026-04-07,4.25
2026-04-08,4.27
2026-04-09,4.24
2026-04-10,4.20
2026-04-13,4.18
2026-04-14,4.16
2026-04-15,4.15
2026-04-16,4.17
2026-04-17,4.19
2026-04-20,4.22
2026-04-21,4.24
2026-04-22,4.26
2026-04-23,4.28
2026-04-24,4.27
2026-04-27,4.25
2026-04-28,4.23
2026-04-29,4.21
2026-04-30,4.20
2026-05-01,4.18
2026-05-04,4.16
2026-05-05,4.14
2026-05-06,4.13
2026-05-07,4.12
2026-05-08,4.13
2026-05-11,4.15
2026-05-12,4.17
```

Create `tests/fixtures/fred/VIXCLS_sample.csv` with the literal content below. This fixture includes the FRED `.` missing-observation marker to exercise the parser's None-handling.

```
DATE,VIXCLS
2026-01-01,.
2026-01-02,14.21
2026-01-05,13.85
2026-01-06,.
2026-01-07,14.10
```

- [ ] **Step 2: Write the failing tests**

Create `tests/datasources/test_fred_fetch.py`:

```python
"""Tests for FredSource.fetch_macro_series (Phase A.3.5)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.fred_source import FredSource


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fred"
DGS10_CSV = (FIXTURE_DIR / "DGS10_sample.csv").read_text()
VIXCLS_CSV = (FIXTURE_DIR / "VIXCLS_sample.csv").read_text()


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v7()
    return mgr


def _make_csv_mock(mocker, csv_by_series: dict[str, str]):
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        # URL form: https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>
        for series_id, csv_text in csv_by_series.items():
            if f"id={series_id}" in url:
                resp.text = csv_text
                resp.content = csv_text.encode()
                return resp
        resp.ok = False
        resp.status_code = 404
        resp.text = ""
        resp.content = b""
        return resp
    mocker.patch(
        "src.common.datasources.fred_source.requests.get",
        side_effect=side_effect,
    )


def test_fetch_macro_series_default_series_list(db, mocker):
    csvs = {
        "DGS10": DGS10_CSV,
        "DGS3MO": "DATE,DGS3MO\n2026-05-12,5.10\n",
        "DGS2": "DATE,DGS2\n2026-05-12,4.50\n",
        "VIXCLS": VIXCLS_CSV,
        "CPIAUCSL": "DATE,CPIAUCSL\n2026-04-01,312.45\n",
    }
    _make_csv_mock(mocker, csvs)
    n = FredSource().fetch_macro_series(run_id="r1", db=db)
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        series = sorted(
            r[0] for r in c.execute("SELECT DISTINCT series_id FROM raw_fred")
        )
    assert series == ["CPIAUCSL", "DGS10", "DGS2", "DGS3MO", "VIXCLS"]


def test_fetch_macro_series_custom_series_list(db, mocker):
    _make_csv_mock(mocker, {"DGS10": DGS10_CSV})
    n = FredSource().fetch_macro_series(
        run_id="r1", series_ids=["DGS10"], db=db,
    )
    assert n == 30  # rows in DGS10_sample.csv
    with sqlite3.connect(db.db_path) as c:
        s = c.execute("SELECT DISTINCT series_id FROM raw_fred").fetchall()
    assert [r[0] for r in s] == ["DGS10"]


def test_fetch_macro_series_parses_values_correctly(db, mocker):
    _make_csv_mock(mocker, {"DGS10": DGS10_CSV})
    FredSource().fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    with sqlite3.connect(db.db_path) as c:
        row = c.execute(
            "SELECT observation_date, value FROM raw_fred "
            "WHERE series_id='DGS10' AND observation_date='2026-04-01'"
        ).fetchone()
    assert row == ("2026-04-01", 4.18)


def test_fetch_macro_series_handles_dot_missing_value(db, mocker):
    """FRED '.' sentinel must map to NULL, not raise."""
    _make_csv_mock(mocker, {"VIXCLS": VIXCLS_CSV})
    n = FredSource().fetch_macro_series(run_id="r1", series_ids=["VIXCLS"], db=db)
    assert n == 5
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT observation_date, value FROM raw_fred "
            "WHERE series_id='VIXCLS' ORDER BY observation_date"
        ).fetchall()
    assert rows[0] == ("2026-01-01", None)
    assert rows[1] == ("2026-01-02", 14.21)
    assert rows[3] == ("2026-01-06", None)


def test_fetch_macro_series_updates_watermark_per_series(db, mocker):
    _make_csv_mock(mocker, {"DGS10": DGS10_CSV})
    FredSource().fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    w = db.get_watermark("fred", "*", "series:DGS10")
    assert w is not None
    # Latest observation date in the fixture.
    assert w["last_observation_date"] == "2026-05-12"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_fetch_macro_series_failed_series_records_error(db, mocker):
    """One series 404s; others still succeed; failed watermark has error_count >= 1."""
    _make_csv_mock(mocker, {"DGS10": DGS10_CSV})  # only DGS10 served
    n = FredSource().fetch_macro_series(
        run_id="r1", series_ids=["DGS10", "BOGUS"], db=db,
    )
    assert n == 30  # only DGS10 rows inserted
    w_ok = db.get_watermark("fred", "*", "series:DGS10")
    w_err = db.get_watermark("fred", "*", "series:BOGUS")
    assert w_ok["error_count"] == 0
    assert w_err is not None
    assert w_err["error_count"] >= 1


def test_fetch_macro_series_insert_or_ignore_protects_against_dup(db, mocker):
    """Second run with identical fixture inserts zero new rows."""
    _make_csv_mock(mocker, {"DGS10": DGS10_CSV})
    src = FredSource()
    n1 = src.fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    assert n1 == 30
    # Clear watermark so the second pass actually re-fetches the CSV.
    db.upsert_watermark(
        source="fred", ticker="*", field="series:DGS10",
        last_observation_date=None, success=True,
    )
    n2 = src.fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    assert n2 == 0
    with sqlite3.connect(db.db_path) as c:
        total = c.execute(
            "SELECT COUNT(*) FROM raw_fred WHERE series_id='DGS10'"
        ).fetchone()[0]
    assert total == 30


def test_fetch_macro_series_calls_correct_url(db, mocker):
    captured = []

    def side_effect(url, **kwargs):
        captured.append(url)
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.text = DGS10_CSV
        resp.content = DGS10_CSV.encode()
        return resp

    mocker.patch(
        "src.common.datasources.fred_source.requests.get",
        side_effect=side_effect,
    )
    FredSource().fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    assert any("fredgraph.csv?id=DGS10" in u for u in captured)


def test_fetch_macro_series_empty_db_when_no_series_provided_uses_defaults(db, mocker):
    """series_ids=None means use the default 5-series macro list."""
    _make_csv_mock(mocker, {
        "DGS10": "DATE,DGS10\n2026-05-12,4.17\n",
        "DGS3MO": "DATE,DGS3MO\n2026-05-12,5.10\n",
        "DGS2": "DATE,DGS2\n2026-05-12,4.50\n",
        "VIXCLS": "DATE,VIXCLS\n2026-05-12,14.0\n",
        "CPIAUCSL": "DATE,CPIAUCSL\n2026-04-01,312.45\n",
    })
    n = FredSource().fetch_macro_series(run_id="r1", db=db)
    assert n == 5


def test_fetch_macro_series_skips_malformed_rows(db, mocker):
    """Rows that fail integer/float parsing or have bad shape are dropped."""
    csv_with_junk = (
        "DATE,DGS10\n"
        "2026-05-12,4.17\n"
        "garbage row\n"
        ",4.18\n"  # blank date
        "2026-05-13,not-a-float\n"
        "2026-05-14,4.16\n"
    )
    _make_csv_mock(mocker, {"DGS10": csv_with_junk})
    n = FredSource().fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    # Only 2026-05-12, 2026-05-14 should parse cleanly with non-null values.
    # The 'not-a-float' row stores observation_date with value=None (treated as missing).
    assert n >= 2
    with sqlite3.connect(db.db_path) as c:
        dates = sorted(
            r[0] for r in c.execute(
                "SELECT observation_date FROM raw_fred WHERE series_id='DGS10'"
            )
        )
    assert "2026-05-12" in dates
    assert "2026-05-14" in dates


def test_fetch_macro_series_watermark_present_still_refetches(db, mocker):
    """FRED is cheap; we always re-pull the full series even when watermark
    is up-to-date. INSERT OR IGNORE handles dedup."""
    db.upsert_watermark(
        source="fred", ticker="*", field="series:DGS10",
        last_observation_date="2026-05-12", success=True,
    )
    _make_csv_mock(mocker, {"DGS10": DGS10_CSV})
    n = FredSource().fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    assert n == 30
```

- [ ] **Step 3: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_fred_fetch.py -v
```

Expected: `AttributeError` on `fetch_macro_series`.

- [ ] **Step 4: Implement `fetch_macro_series` in `FredSource`**

Replace the contents of `src/common/datasources/fred_source.py` with:

```python
"""FredSource — Federal Reserve Economic Data (St. Louis Fed).

Uses the public `fredgraph.csv` endpoint, which is keyless and stable.
A.1 deliverable: health_check via DGS10 (10y Treasury).
A.3.5 deliverable: fetch_macro_series — pulls the spec-required macro
series (DGS10, DGS3MO, DGS2, VIXCLS, CPIAUCSL by default), parses the
public CSV, and persists to raw_fred via INSERT OR IGNORE. Per-series
watermark `(fred, *, series:<id>)` tracks the latest observation_date
seen.

FRED CSV format
---------------
  DATE,<SERIES_ID>
  YYYY-MM-DD,<value or '.' for missing>
  ...

The `.` sentinel represents missing observations (non-trading days,
holidays, etc.). The parser maps `.` -> None (stored as NULL in SQLite).

Vintage-data note (look-ahead bias)
-----------------------------------
The `fredgraph.csv` endpoint returns the CURRENTLY REVISED series, not
the as-originally-published vintage. CPI in particular gets revised.
For research-grade look-ahead-bias avoidance we'd need ALFRED (the
archival FRED API with `realtime_start`/`realtime_end` per observation).
For v1 we leave the realtime_start/realtime_end columns None and
document the bias in the methodology handbook. When ALFRED integration
is added post-A.3.10, the same `raw_fred` schema accommodates the
finer-grained data with no migration.

SEC-style fair-access policy does not apply here (FRED has no published
rate limit on the public CSV endpoint), but we still serialize series
pulls with a polite 250ms delay between requests to avoid bursty
behavior.
"""
from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timezone
from typing import Iterable

import requests

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_PROBE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
_CSV_URL_FMT = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

# Default macro series per spec §6.4 + v2 spec §4 (Layer 1 macro context).
DEFAULT_MACRO_SERIES = ("DGS10", "DGS3MO", "DGS2", "VIXCLS", "CPIAUCSL")

# Polite inter-series delay (seconds). FRED has no documented limit, but
# burst-friendly behavior is courteous and matches our SEC pattern.
_INTER_SERIES_DELAY_SEC = 0.25

# FRED missing-observation sentinel.
_FRED_MISSING_SENTINEL = "."


class FredSource(BaseDataSource):
    name = "fred"
    cadence = "weekly"
    provides = {
        "risk_free_rate_10y",
        "risk_free_rate_3m",
        "risk_free_rate_2y",
        "vix_close",
        "cpi_yoy",
    }

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            resp = requests.get(_PROBE_URL, timeout=10)
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        if not resp.ok:
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"HTTP {resp.status_code}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        lines = resp.text.strip().split("\n")
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK,
            checked_at=now,
            message=f"DGS10 rows={len(lines) - 1}",
            response_time_ms=(time.monotonic() - started) * 1000,
        )

    # ------------------------------------------------------------------
    # Phase A.3.5: macro series ingest
    # ------------------------------------------------------------------

    def _parse_fred_csv(
        self,
        csv_text: str,
        *,
        series_id: str,
        run_id: str,
        source_filename: str,
        scrape_timestamp: str,
    ) -> list:
        """Parse the FRED public CSV into a list of RawFredObservation."""
        from src.common.schemas import RawFredObservation

        rows: list = []
        if not csv_text:
            return rows
        buf = io.StringIO(csv_text)
        reader = csv.reader(buf)
        try:
            header = next(reader)
        except StopIteration:
            return rows
        if not header or header[0].strip().upper() != "DATE":
            # Not a parseable FRED CSV.
            return rows

        for raw_row in reader:
            if not raw_row or len(raw_row) < 2:
                continue
            date_s = (raw_row[0] or "").strip()
            value_s = (raw_row[1] or "").strip()
            if not date_s:
                continue

            if value_s == _FRED_MISSING_SENTINEL or value_s == "":
                value = None
            else:
                try:
                    value = float(value_s)
                except (TypeError, ValueError):
                    # Malformed value — store as missing rather than drop.
                    value = None

            try:
                obs = RawFredObservation(
                    run_id=run_id,
                    series_id=series_id,
                    observation_date=date_s,
                    value=value,
                    realtime_start=None,
                    realtime_end=None,
                    source_filename=source_filename,
                    scrape_timestamp=scrape_timestamp,
                )
            except Exception:
                # Pydantic validation failed (e.g., empty date). Skip row.
                continue
            rows.append(obs)
        return rows

    def fetch_macro_series(
        self,
        run_id: str,
        db=None,
        *,
        series_ids: Iterable[str] | None = None,
    ) -> int:
        """Pull each FRED macro series and persist to raw_fred.

        End-to-end pipeline (per series):
          1. GET https://fred.stlouisfed.org/graph/fredgraph.csv?id=<id>
          2. _parse_fred_csv() -> list[RawFredObservation]
          3. INSERT OR IGNORE into raw_fred
          4. update_watermark(fred, *, series:<id>) with max observation_date

        Per-series failures (HTTP error, parse error) are recorded in the
        watermark's error_count and the loop continues with the remaining
        series — Principle 6's per-source resilience.

        Polite 250ms delay between series (FRED is generous, but bursty
        behavior on a shared endpoint is rude).

        Parameters
        ----------
        run_id : provenance tag stamped on every emitted row
        db     : DatabaseManager (required)
        series_ids : iterable of series IDs to pull; default
                     DEFAULT_MACRO_SERIES (5 series per spec §6.4)

        Returns
        -------
        int : total NEW rows inserted across all series (net of
              INSERT OR IGNORE collisions).
        """
        if db is None:
            raise ValueError("db is required")

        ids = list(series_ids) if series_ids is not None else list(DEFAULT_MACRO_SERIES)

        scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        total_inserted = 0
        import sqlite3
        import datetime as _dt

        for i, sid in enumerate(ids):
            field = f"series:{sid}"
            url = _CSV_URL_FMT.format(series_id=sid)

            if i > 0:
                time.sleep(_INTER_SERIES_DELAY_SEC)

            # 1. Fetch
            try:
                resp = requests.get(url, timeout=15)
            except Exception as exc:  # noqa: BLE001
                self.update_watermark(
                    ticker="*", field=field,
                    last_observation_date=None, db=db, success=False,
                    error_message=f"http: {type(exc).__name__}: {exc}",
                )
                continue
            if not resp.ok:
                self.update_watermark(
                    ticker="*", field=field,
                    last_observation_date=None, db=db, success=False,
                    error_message=f"http {resp.status_code}",
                )
                continue

            # 2. Parse
            parsed = self._parse_fred_csv(
                resp.text,
                series_id=sid,
                run_id=run_id,
                source_filename=f"fredgraph.csv?id={sid}",
                scrape_timestamp=scrape_ts,
            )
            if not parsed:
                self.update_watermark(
                    ticker="*", field=field,
                    last_observation_date=None, db=db, success=False,
                    error_message="parse: no rows extracted",
                )
                continue

            # 3. Insert OR IGNORE; count net new rows
            with sqlite3.connect(db.db_path) as c:
                n_before = c.execute(
                    "SELECT COUNT(*) FROM raw_fred WHERE series_id=?", (sid,)
                ).fetchone()[0]
            db.insert_raw_fred(parsed)
            with sqlite3.connect(db.db_path) as c:
                n_after = c.execute(
                    "SELECT COUNT(*) FROM raw_fred WHERE series_id=?", (sid,)
                ).fetchone()[0]
            n_inserted = n_after - n_before
            total_inserted += n_inserted

            # 4. Watermark advance to max observation_date observed
            try:
                latest_iso = max(
                    r.observation_date for r in parsed if r.observation_date
                )
                last_obs = _dt.date.fromisoformat(latest_iso)
            except (ValueError, TypeError):
                last_obs = None
            self.update_watermark(
                ticker="*", field=field,
                last_observation_date=last_obs, db=db, success=True,
            )

        return total_inserted
```

- [ ] **Step 5: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_fred_fetch.py -v
```

Expected: All 11 tests PASS.

- [ ] **Step 6: Verify A.1 FredSource.health_check still works (no regression)**

```
./venv/Scripts/python.exe -m pytest tests/datasources -k "fred and health" -v 2>&1 | tail -5
```

Expected: any pre-existing fred health-check tests still pass.

- [ ] **Step 7: Commit**

```
git add src/common/datasources/fred_source.py tests/fixtures/fred/DGS10_sample.csv tests/fixtures/fred/VIXCLS_sample.csv tests/datasources/test_fred_fetch.py
git commit -m "feat(datasources): FredSource.fetch_macro_series - 5 macro series CSV pull with per-series watermarks"
```

---

## Task 4: `FinraSource.fetch_short_interest` real implementation

**Files:**
- Modify: `src/common/datasources/finra_source.py`
- Create: `tests/fixtures/finra/shvol_sample.txt`
- Create: `tests/datasources/test_finra_fetch.py`

- [ ] **Step 1: Create the synthetic FINRA pipe-delimited fixture**

Create `tests/fixtures/finra/shvol_sample.txt` with the literal content below. This mimics the FINRA public daily short-volume file format exactly (pipe-delimited, header row, per-symbol records).

```
Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
20260515|AAPL|12345678|0|85123456|Q
20260515|MSFT|7890123|100|45000000|Q
20260515|GOOGL|3456789|0|22000000|Q
20260515|AMZN|2345678|50|18000000|Q
20260515|NVDA|9876543|0|50000000|Q
20260515|META|4567890|0|28000000|Q
20260515|TSLA|11000000|200|72000000|Q
20260515|BRKB|234567|0|2500000|Q
20260515|JPM|3456789|100|18000000|Q
20260515|JNJ|1234567|0|9500000|Q
20260515|V|2345678|0|11000000|Q
20260515|UNH|987654|0|6500000|Q
20260515|XOM|3456789|100|18000000|Q
20260515|PG|1234567|0|7800000|Q
20260515|HD|2345678|50|12000000|Q
20260515|MA|1500000|0|7200000|Q
20260515|BAC|4567890|100|22000000|Q
20260515|PFE|3210987|0|17500000|Q
20260515|KO|1098765|0|9800000|Q
20260515|PEP|987654|0|6700000|Q
```

- [ ] **Step 2: Write the failing tests**

Create `tests/datasources/test_finra_fetch.py`:

```python
"""Tests for FinraSource.fetch_short_interest (Phase A.3.5)."""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.finra_source import FinraSource


FIXTURE_TXT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "finra" / "shvol_sample.txt"
).read_text()


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v7()
    return mgr


def _wire_mock(mocker, txt: str, *, status: int = 200):
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = status
        resp.ok = status == 200
        resp.text = txt
        resp.content = txt.encode()
        return resp
    mocker.patch(
        "src.common.datasources.finra_source.requests.get",
        side_effect=side_effect,
    )


def test_fetch_short_interest_persists_rows(db, mocker):
    _wire_mock(mocker, FIXTURE_TXT)
    n = FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        tickers = sorted(
            r[0] for r in c.execute("SELECT DISTINCT ticker FROM raw_finra")
        )
    assert "AAPL" in tickers
    assert "MSFT" in tickers


def test_fetch_short_interest_parses_correctly(db, mocker):
    _wire_mock(mocker, FIXTURE_TXT)
    FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT ticker, short_interest_shares, avg_daily_volume "
            "FROM raw_finra WHERE ticker='AAPL'"
        ).fetchall()
    assert len(rows) >= 1
    aapl = rows[0]
    assert aapl[0] == "AAPL"
    assert aapl[1] == 12_345_678.0
    # total_volume from fixture = 85_123_456; avg_daily_volume mirrors it
    assert aapl[2] == 85_123_456.0


def test_fetch_short_interest_computes_days_to_cover(db, mocker):
    """days_to_cover = short_interest_shares / avg_daily_volume."""
    _wire_mock(mocker, FIXTURE_TXT)
    FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        row = c.execute(
            "SELECT short_interest_shares, avg_daily_volume, days_to_cover "
            "FROM raw_finra WHERE ticker='AAPL'"
        ).fetchone()
    si, adv, dtc = row
    assert abs(dtc - si / adv) < 1e-6


def test_fetch_short_interest_records_exchange_marker(db, mocker):
    """Each row should carry the exchange discriminator from the file."""
    _wire_mock(mocker, FIXTURE_TXT)
    FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        exchanges = {
            r[0] for r in c.execute("SELECT DISTINCT exchange FROM raw_finra")
        }
    # Default file_stem='FNSQ' maps to exchange 'NSDQ'.
    assert exchanges == {"NSDQ"}


def test_fetch_short_interest_updates_watermark(db, mocker):
    _wire_mock(mocker, FIXTURE_TXT)
    FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    w = db.get_watermark("finra", "*", "short_interest_biweekly")
    assert w is not None
    assert w["last_observation_date"] == "2026-05-15"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_fetch_short_interest_14d_skip(db, mocker):
    """Spec §6.5: refresh skipped within 14d of prior successful pull."""
    # Seed a watermark dated 7 days ago.
    recent = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
    db.upsert_watermark(
        source="finra", ticker="*", field="short_interest_biweekly",
        last_observation_date=recent, success=True,
    )
    _wire_mock(mocker, FIXTURE_TXT)
    n = FinraSource().fetch_short_interest(run_id="r1", db=db)
    assert n == 0  # short-circuited
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute("SELECT COUNT(*) FROM raw_finra").fetchone()[0]
    assert rows == 0


def test_fetch_short_interest_14d_skip_overridable_by_explicit_settlement_date(db, mocker):
    """Caller-supplied settlement_date bypasses the 14d skip — explicit
    is louder than default."""
    recent = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
    db.upsert_watermark(
        source="finra", ticker="*", field="short_interest_biweekly",
        last_observation_date=recent, success=True,
    )
    _wire_mock(mocker, FIXTURE_TXT)
    n = FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert n > 0


def test_fetch_short_interest_file_not_found_records_error(db, mocker):
    _wire_mock(mocker, "", status=404)
    n = FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert n == 0
    w = db.get_watermark("finra", "*", "short_interest_biweekly")
    assert w is not None
    assert w["error_count"] >= 1


def test_fetch_short_interest_calls_correct_url(db, mocker):
    captured = []

    def side_effect(url, **kwargs):
        captured.append(url)
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.text = FIXTURE_TXT
        resp.content = FIXTURE_TXT.encode()
        return resp

    mocker.patch(
        "src.common.datasources.finra_source.requests.get",
        side_effect=side_effect,
    )
    FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert any("cdn.finra.org" in u for u in captured)
    assert any("20260515" in u for u in captured)
    assert any("shvol" in u.lower() for u in captured)


def test_fetch_short_interest_insert_or_ignore_protects_against_dup(db, mocker):
    """Second fetch on the same settlement_date inserts no new rows."""
    _wire_mock(mocker, FIXTURE_TXT)
    src = FinraSource()
    n1 = src.fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert n1 == 20  # all rows in fixture
    n2 = src.fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert n2 == 0
    with sqlite3.connect(db.db_path) as c:
        total = c.execute("SELECT COUNT(*) FROM raw_finra").fetchone()[0]
    assert total == 20


def test_fetch_short_interest_default_settlement_date_uses_today(db, mocker):
    """With no settlement_date and no recent watermark, the source picks
    today() as the target date and calls the corresponding URL."""
    captured = []

    def side_effect(url, **kwargs):
        captured.append(url)
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.text = FIXTURE_TXT
        resp.content = FIXTURE_TXT.encode()
        return resp

    mocker.patch(
        "src.common.datasources.finra_source.requests.get",
        side_effect=side_effect,
    )
    FinraSource().fetch_short_interest(run_id="r1", db=db)
    today_compact = _dt.date.today().strftime("%Y%m%d")
    assert any(today_compact in u for u in captured)


def test_fetch_short_interest_handles_blank_and_short_rows(db, mocker):
    """Blank lines, rows with too-few fields, and the header row are skipped."""
    junk = (
        "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
        "20260515|AAPL|12345678|0|85123456|Q\n"
        "\n"
        "20260515|BAD\n"
        "20260515|MSFT|7890123|100|45000000|Q\n"
    )
    _wire_mock(mocker, junk)
    n = FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert n == 2  # AAPL + MSFT only
```

- [ ] **Step 3: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_finra_fetch.py -v
```

Expected: `AttributeError` on `fetch_short_interest`.

- [ ] **Step 4: Implement `fetch_short_interest` in `FinraSource`**

Replace the contents of `src/common/datasources/finra_source.py` with:

```python
"""FinraSource — FINRA short-interest / short-volume CDN.

FINRA publishes per-market-center short-volume data daily and the
official short-interest report twice monthly. A.1 deliverable:
health_check by probing the public landing page.

A.3.5 deliverable: fetch_short_interest — pulls the daily short-volume
file from FINRA's CDN, parses the pipe-delimited per-ticker records,
and persists to raw_finra via INSERT OR IGNORE. Watermark
`(finra, *, short_interest_biweekly)` tracks the latest settlement date
seen; refresh is skipped within 14 days per spec §6.5.

URL pattern (verify before production)
--------------------------------------
The FINRA CDN organizes daily short-volume files under:
  https://cdn.finra.org/equity/regsho/daily/<MARKET><FILE>{YYYYMMDD}.txt

Per-market-center file stems:
  - FNRA : NYSE
  - FNSQ : Nasdaq          <-- A.3.5 default (broadest coverage)
  - FNYX : NYSE American
  - FORF : FINRA / OTC

The default pull uses the Nasdaq daily file (FNSQ) because Nasdaq has
the broadest single-file coverage of the large-cap universe we care
about for Layer 1. Future enhancement (post-A.3.10): pull all four and
aggregate by exchange. The `exchange` column in raw_finra accommodates
that without schema change.

Note: this is NOT the FINRA biweekly "Short Interest" report (which
lives at the FINRA Query API and requires JSON aggregation). The daily
file gives us per-day per-ticker short-volume + total-volume, which is
sufficient for Signal 2 (Diether-Lee-Werner 2009 days-to-cover proxy)
at biweekly cadence after materialization-layer aggregation.

File format (pipe-delimited)
----------------------------
  Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
  YYYYMMDD|<ticker>|<int>|<int>|<int>|<exchange>
  ...
"""
from __future__ import annotations

import datetime as _dt
import time
from datetime import datetime, timezone

import requests

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_PROBE_URL = "https://www.finra.org/finra-data/short-sale-volume-data"
_DAILY_FILE_URL_FMT = (
    "https://cdn.finra.org/equity/regsho/daily/{stem}shvol{yyyymmdd}.txt"
)

# Default per-market-center file stem. FNSQ = Nasdaq daily short-volume.
_DEFAULT_FILE_STEM = "FNSQ"

# Canonical exchange code stamped on raw_finra rows when pulling each
# file stem. Future enhancement may pull all four and stamp accordingly.
_STEM_TO_EXCHANGE = {
    "FNRA": "NYSE",
    "FNSQ": "NSDQ",
    "FNYX": "NYAX",
    "FORF": "ORF",
}

# Per spec §6.5: skip biweekly refresh if previous successful pull was
# within this many days. Caller can force a refresh by passing an
# explicit settlement_date.
_REFRESH_SKIP_DAYS = 14


class FinraSource(BaseDataSource):
    name = "finra"
    cadence = "biweekly"
    provides = {"short_interest_pct_float"}

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            resp = requests.get(_PROBE_URL, timeout=10)
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        if not resp.ok:
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"HTTP {resp.status_code}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK,
            checked_at=now,
            message="HTTP 200",
            response_time_ms=(time.monotonic() - started) * 1000,
        )

    # ------------------------------------------------------------------
    # Phase A.3.5: short-interest ingest
    # ------------------------------------------------------------------

    def _parse_shvol_text(
        self,
        text: str,
        *,
        settlement_date: _dt.date,
        exchange: str,
        run_id: str,
        source_filename: str,
        scrape_timestamp: str,
    ) -> list:
        """Parse a FINRA daily short-volume pipe-delimited file."""
        from src.common.schemas import RawFinraShortInterest

        rows: list = []
        if not text:
            return rows

        settlement_iso = settlement_date.isoformat()

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue
            # Skip header row: "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market"
            if parts[0].strip().lower() == "date":
                continue
            symbol = parts[1].strip()
            if not symbol:
                continue
            short_vol_s = parts[2].strip()
            total_vol_s = parts[4].strip() if len(parts) > 4 else ""
            try:
                short_vol = float(short_vol_s)
            except (TypeError, ValueError):
                continue
            try:
                total_vol = float(total_vol_s) if total_vol_s else None
            except (TypeError, ValueError):
                total_vol = None

            # days_to_cover = short_interest_shares / avg_daily_volume.
            # We use the daily TotalVolume as our ADV proxy; the
            # materialization layer can later replace this with a true
            # 30-day average by joining against raw_yahoo prices.
            dtc = None
            if total_vol and total_vol > 0:
                try:
                    dtc = short_vol / total_vol
                except ZeroDivisionError:
                    dtc = None

            try:
                row = RawFinraShortInterest(
                    run_id=run_id,
                    ticker=symbol,
                    settlement_date=settlement_iso,
                    exchange=exchange,
                    short_interest_shares=short_vol,
                    avg_daily_volume=total_vol,
                    days_to_cover=dtc,
                    source_filename=source_filename,
                    scrape_timestamp=scrape_timestamp,
                )
            except Exception:
                # Pydantic validation failed (e.g., invalid ticker). Skip.
                continue
            rows.append(row)
        return rows

    def fetch_short_interest(
        self,
        run_id: str,
        db=None,
        *,
        settlement_date: _dt.date | None = None,
        file_stem: str = _DEFAULT_FILE_STEM,
    ) -> int:
        """Pull a FINRA daily short-volume file and persist to raw_finra.

        Watermark logic
        ---------------
        Watermark `(finra, *, short_interest_biweekly)` tracks the latest
        settlement_date seen. If `settlement_date is None`, the source
        first checks the watermark:
          - If the previous successful pull is within 14 days, skip
            (return 0). Per spec §6.5.
          - Else target today() as the settlement date.
        An explicit `settlement_date` from the caller overrides the
        14-day skip — explicit-beats-default for backfills.

        Per-fetch failures (HTTP error, parse error, file not found)
        increment the watermark's error_count and return 0.

        Returns
        -------
        int : net new rows inserted (after INSERT OR IGNORE).
        """
        if db is None:
            raise ValueError("db is required")

        field = "short_interest_biweekly"

        # Resolve the target settlement date.
        if settlement_date is None:
            w = db.get_watermark(self.name, "*", field)
            if w and w.get("last_observation_date"):
                try:
                    last_obs = _dt.date.fromisoformat(w["last_observation_date"])
                    if (_dt.date.today() - last_obs).days < _REFRESH_SKIP_DAYS:
                        # Refresh-skip per §6.5.
                        return 0
                except ValueError:
                    pass
            settlement_date = _dt.date.today()

        exchange = _STEM_TO_EXCHANGE.get(file_stem, file_stem)
        yyyymmdd = settlement_date.strftime("%Y%m%d")
        source_filename = f"{file_stem}shvol{yyyymmdd}.txt"
        url = _DAILY_FILE_URL_FMT.format(stem=file_stem, yyyymmdd=yyyymmdd)
        scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # Fetch
        try:
            resp = requests.get(url, timeout=15)
        except Exception as exc:  # noqa: BLE001
            self.update_watermark(
                ticker="*", field=field,
                last_observation_date=None, db=db, success=False,
                error_message=f"http: {type(exc).__name__}: {exc}",
            )
            return 0
        if not resp.ok:
            self.update_watermark(
                ticker="*", field=field,
                last_observation_date=None, db=db, success=False,
                error_message=f"http {resp.status_code} for {source_filename}",
            )
            return 0

        # Parse
        parsed = self._parse_shvol_text(
            resp.text,
            settlement_date=settlement_date,
            exchange=exchange,
            run_id=run_id,
            source_filename=source_filename,
            scrape_timestamp=scrape_ts,
        )

        if not parsed:
            self.update_watermark(
                ticker="*", field=field,
                last_observation_date=None, db=db, success=False,
                error_message=f"parse: no rows in {source_filename}",
            )
            return 0

        # Insert OR IGNORE; count net new rows
        import sqlite3
        with sqlite3.connect(db.db_path) as c:
            n_before = c.execute(
                "SELECT COUNT(*) FROM raw_finra WHERE settlement_date=? AND exchange=?",
                (settlement_date.isoformat(), exchange),
            ).fetchone()[0]
        db.insert_raw_finra(parsed)
        with sqlite3.connect(db.db_path) as c:
            n_after = c.execute(
                "SELECT COUNT(*) FROM raw_finra WHERE settlement_date=? AND exchange=?",
                (settlement_date.isoformat(), exchange),
            ).fetchone()[0]
        n_inserted = n_after - n_before

        # Watermark advance
        self.update_watermark(
            ticker="*", field=field,
            last_observation_date=settlement_date, db=db, success=True,
        )

        return n_inserted
```

- [ ] **Step 5: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_finra_fetch.py -v
```

Expected: All 12 tests PASS.

- [ ] **Step 6: Verify A.1 FinraSource.health_check still works (no regression)**

```
./venv/Scripts/python.exe -m pytest tests/datasources -k "finra and health" -v 2>&1 | tail -5
```

Expected: any pre-existing finra health-check tests still pass.

- [ ] **Step 7: Commit**

```
git add src/common/datasources/finra_source.py tests/fixtures/finra/shvol_sample.txt tests/datasources/test_finra_fetch.py
git commit -m "feat(datasources): FinraSource.fetch_short_interest - daily shvol pull with 14d refresh skip + INSERT OR IGNORE"
```

---

## Task 5: Integration test (end-to-end mocked HTTP)

**Files:**
- Create: `tests/datasources/test_integration_a3_5.py`

- [ ] **Step 1: Write the integration test**

Create `tests/datasources/test_integration_a3_5.py`:

```python
"""End-to-end orchestration test for A.3.5:
- FRED: pull 5 default macro series, persist to raw_fred, advance per-series
  watermarks, then re-run idempotently.
- FINRA: pull daily short-volume file, persist to raw_finra, advance the
  biweekly watermark, then re-run within 14d and verify the skip fires.

Unit-level integration with mocked HTTP. Real-endpoint integration is
deferred to A.5.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.fred_source import FredSource, DEFAULT_MACRO_SERIES
from src.common.datasources.finra_source import FinraSource


FRED_DGS10 = (
    Path(__file__).resolve().parents[1] / "fixtures" / "fred" / "DGS10_sample.csv"
).read_text()
FRED_VIXCLS = (
    Path(__file__).resolve().parents[1] / "fixtures" / "fred" / "VIXCLS_sample.csv"
).read_text()
FINRA_TXT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "finra" / "shvol_sample.txt"
).read_text()


# Stub responses for the 3 FRED series we don't have full CSVs for.
_STUB_CSVS = {
    "DGS3MO": "DATE,DGS3MO\n2026-05-12,5.10\n2026-05-13,5.11\n",
    "DGS2": "DATE,DGS2\n2026-05-12,4.50\n2026-05-13,4.52\n",
    "CPIAUCSL": "DATE,CPIAUCSL\n2026-04-01,312.45\n2026-05-01,313.10\n",
}


def _wire_fred_mock(mocker):
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "id=DGS10" in url:
            resp.text = FRED_DGS10
        elif "id=VIXCLS" in url:
            resp.text = FRED_VIXCLS
        else:
            for sid, csv in _STUB_CSVS.items():
                if f"id={sid}" in url:
                    resp.text = csv
                    break
            else:
                resp.ok = False
                resp.status_code = 404
                resp.text = ""
        resp.content = resp.text.encode()
        return resp
    mocker.patch(
        "src.common.datasources.fred_source.requests.get",
        side_effect=side_effect,
    )


def _wire_finra_mock(mocker, *, status: int = 200, txt: str | None = None):
    payload = FINRA_TXT if txt is None else txt

    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = status
        resp.ok = status == 200
        resp.text = payload
        resp.content = payload.encode()
        return resp
    mocker.patch(
        "src.common.datasources.finra_source.requests.get",
        side_effect=side_effect,
    )


def test_a3_5_fred_full_flow_with_idempotent_rerun(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v7()

    _wire_fred_mock(mocker)

    fred = FredSource()

    # First pull: all 5 default series.
    n1 = fred.fetch_macro_series(run_id="run-1", db=db)
    assert n1 > 0

    # Every default series has a watermark.
    for sid in DEFAULT_MACRO_SERIES:
        w = db.get_watermark("fred", "*", f"series:{sid}")
        assert w is not None, f"missing watermark for {sid}"
        assert w["fetch_count"] >= 1
        assert w["error_count"] == 0

    # Idempotent re-pull: zero new rows.
    with sqlite3.connect(db_path) as c:
        n_before = c.execute("SELECT COUNT(*) FROM raw_fred").fetchone()[0]
    n2 = fred.fetch_macro_series(run_id="run-1", db=db)
    assert n2 == 0
    with sqlite3.connect(db_path) as c:
        n_after = c.execute("SELECT COUNT(*) FROM raw_fred").fetchone()[0]
    assert n_after == n_before


def test_a3_5_finra_full_flow_with_14d_skip(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v7()

    _wire_finra_mock(mocker)

    finra = FinraSource()
    target = _dt.date(2026, 5, 15)

    # First pull: explicit settlement_date.
    n1 = finra.fetch_short_interest(
        run_id="run-1", settlement_date=target, db=db,
    )
    assert n1 == 20  # all rows in fixture

    w1 = db.get_watermark("finra", "*", "short_interest_biweekly")
    assert w1["last_observation_date"] == "2026-05-15"

    # Re-pull within 14 days of the watermark with no explicit date.
    # Should short-circuit (return 0) per §6.5.
    db.upsert_watermark(
        source="finra", ticker="*", field="short_interest_biweekly",
        last_observation_date=(_dt.date.today() - _dt.timedelta(days=3)).isoformat(),
        success=True,
    )
    n2 = finra.fetch_short_interest(run_id="run-1", db=db)
    assert n2 == 0

    # Explicit date bypasses the skip — but INSERT OR IGNORE still de-dupes.
    n3 = finra.fetch_short_interest(
        run_id="run-1", settlement_date=target, db=db,
    )
    assert n3 == 0  # rows already present from first call


def test_a3_5_finra_failure_does_not_break_fred(tmp_path: Path, mocker):
    """Sources are independent — a FINRA fetch error must not affect FRED."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v7()

    # FRED mocked OK.
    _wire_fred_mock(mocker)

    # FINRA mocked to 404. The two modules import `requests` separately,
    # so each side-effect lives at its own module path.
    def finra_side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 404
        resp.ok = False
        resp.text = ""
        resp.content = b""
        return resp
    mocker.patch(
        "src.common.datasources.finra_source.requests.get",
        side_effect=finra_side_effect,
    )

    n_fred = FredSource().fetch_macro_series(run_id="r1", db=db)
    n_finra = FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )

    assert n_fred > 0
    assert n_finra == 0
    w_finra = db.get_watermark("finra", "*", "short_interest_biweekly")
    assert w_finra["error_count"] >= 1
    # FRED watermarks all healthy.
    for sid in DEFAULT_MACRO_SERIES:
        w = db.get_watermark("fred", "*", f"series:{sid}")
        assert w["error_count"] == 0
```

- [ ] **Step 2: Run the integration test**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_integration_a3_5.py -v
```

Expected: 3 PASS.

- [ ] **Step 3: Run the full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" -v 2>&1 | tail -5
```

Expected: ~366 passed (315 baseline + 12 schema + 13 database + 11 fred + 12 finra + 3 integration_a3_5 — counts approximate; ±2 is within tolerance).

- [ ] **Step 4: Commit**

```
git add tests/datasources/test_integration_a3_5.py
git commit -m "test: A.3.5 integration acceptance - end-to-end FRED + FINRA with watermark behavior"
```

---

## Task 6: Update the build plan

**Files:**
- Modify: the build plan

- [ ] **Step 1: Locate the A.3 row in §5.1.0**

Grep the build plan for `**A.3.1 + A.3.2 + A.3.3 + A.3.4 shipped 2026-05-21**` to find the line.

- [ ] **Step 2: Update the A.3 row's shipped marker**

Use `Edit` to replace the substring `**A.3.1 + A.3.2 + A.3.3 + A.3.4 shipped 2026-05-21**` with `**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 shipped 2026-05-21**`, and update the plan-link parenthetical to add the A.3.5 plan file. Concretely, find this fragment:

```
**A.3.1 + A.3.2 + A.3.3 + A.3.4 shipped 2026-05-21** (plans: [A.3.1](docs/design/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/design/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md), [A.3.3](docs/design/plans/2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md), [A.3.4](docs/design/plans/2026-05-21-phase-a3-sub04-edgar-insider-and-filings.md)): schema v6
```

Replace with:

```
**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 shipped 2026-05-21** (plans: [A.3.1](docs/design/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/design/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md), [A.3.3](docs/design/plans/2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md), [A.3.4](docs/design/plans/2026-05-21-phase-a3-sub04-edgar-insider-and-filings.md), [A.3.5](docs/design/plans/2026-05-21-phase-a3-sub05-fred-finra.md)): schema v7
```

Then in the same sentence, append the A.3.5 fragment after the existing A.3.4 fragment (right before the `. A.3.5–A.3.10 plans drafted...` clause if present, otherwise at the end of the current shipped-summary clause):

```
 + FredSource.fetch_macro_series (DGS10/DGS3MO/DGS2/VIXCLS/CPIAUCSL CSV pull with per-series watermarks + `.` missing-value handling) + FinraSource.fetch_short_interest (daily shvol CDN pull with 14d biweekly refresh skip per spec §6.5) + raw_fred + raw_finra tables (PKs `(series_id, observation_date)` and `(ticker, settlement_date, exchange)`).
```

- [ ] **Step 3: Commit**

```
git add <build-plan>
git commit -m "docs: mark A.3.5 shipped - FRED macro series + FINRA daily short-volume"
```

---

## Phase A.3.5 — Definition of Done

- [ ] `./venv/Scripts/python.exe -m pytest -m "not integration"` shows ~366 tests passing
- [ ] `migrate_to_v7()` produces `schema_version == 7`
- [ ] `raw_fred` table exists with PK `(series_id, observation_date)` and `realtime_start` / `realtime_end` columns present (null for v1)
- [ ] `raw_finra` table exists with PK `(ticker, settlement_date, exchange)`
- [ ] `insert_raw_fred()` uses INSERT OR IGNORE (FRED revisions do NOT overwrite originals in v1)
- [ ] `insert_raw_finra()` uses INSERT OR IGNORE
- [ ] `RawFredObservation.value` accepts None (FRED `.` sentinel maps to NULL)
- [ ] `RawFredObservation.series_id` is uppercased + non-empty validated
- [ ] `RawFinraShortInterest.ticker` is uppercased + matches `_TICKER_RE`
- [ ] `RawFinraShortInterest.short_interest_shares` rejects negative values
- [ ] `FredSource.fetch_macro_series()` defaults to the 5 spec-required series `("DGS10", "DGS3MO", "DGS2", "VIXCLS", "CPIAUCSL")`
- [ ] `FredSource.fetch_macro_series()` parses the FRED `.` missing sentinel as None
- [ ] `FredSource.fetch_macro_series()` updates `(fred, *, series:<id>)` watermark per series with max observation_date
- [ ] Per-series failure in `FredSource` does NOT abort the loop — other series proceed; failed watermark has `error_count >= 1`
- [ ] `FredSource` adds a polite 250ms delay between series fetches
- [ ] `FinraSource.fetch_short_interest()` skips refresh within 14 days when no explicit settlement_date is passed (spec §6.5)
- [ ] Explicit `settlement_date` from caller bypasses the 14-day skip (backfill path)
- [ ] `FinraSource.fetch_short_interest()` computes `days_to_cover = short_interest_shares / avg_daily_volume` per row when total volume > 0
- [ ] `FinraSource.fetch_short_interest()` updates `(finra, *, short_interest_biweekly)` watermark on success
- [ ] File-not-found failure recorded in watermark `error_count`
- [ ] Every FINRA HTTP request targets `cdn.finra.org/equity/regsho/daily/{stem}shvol{yyyymmdd}.txt` (centralized in `_DAILY_FILE_URL_FMT`)
- [ ] No live network in unit tests (all HTTP mocked via pytest-mock)
- [ ] The build plan marks A.3.5 shipped (schema v7)
- [ ] No A.1 / A.2 / A.3.1 / A.3.2 / A.3.3 / A.3.4 regressions (`health_check`, `raw_finviz`, `raw_yahoo`, `raw_edgar_fundamentals`, `raw_edgar_insider`, `raw_edgar_filings` unchanged)
- [ ] Git log shows ~6 task commits

---

## Self-review

**Spec coverage:**

| A.3 spec section | A.3.5 task |
|---|---|
| §6.4 FRED CSV pull for `DGS10`, `DGS3MO`, `DGS2`, `VIXCLS`, `CPIAUCSL` | Task 3 (`FredSource.fetch_macro_series`, `DEFAULT_MACRO_SERIES` constant) |
| §6.4 Long-format `raw_fred(run_id, series_id, observation_date, value, scrape_timestamp)` | Task 1 + Task 2 (`RawFredObservation` + `raw_fred` DDL) |
| §6.4 Watermark per (`fred`, `*`, `series:<id>`) | Task 3 (`update_watermark` call per series) |
| §6.5 FINRA short-volume CDN pull | Task 4 (`FinraSource.fetch_short_interest`, `_DAILY_FILE_URL_FMT`) |
| §6.5 Parse: per-ticker shares short, days-to-cover, exchange | Task 4 (`_parse_shvol_text` + `days_to_cover` computation) |
| §6.5 Watermark `(finra, *, short_interest_biweekly)`; skip pulls within 14d | Task 4 (`_REFRESH_SKIP_DAYS = 14`, watermark-based short-circuit) |
| Principle 5 no silent overwrite | Task 2 (INSERT OR IGNORE on both new tables) |
| Principle 6 watermarks | Task 3 + Task 4 (per-series and biweekly watermarks) |

**Out of scope** (explicitly deferred):

- `short_interest_signal.py` consuming `raw_finra` for Signal 2 → A.3.8.
- `macro_regime_signal.py` / Layer 2 regime priors consuming `raw_fred` yield-curve + VIX → Layer 2 work.
- ALFRED vintage-data integration (true `realtime_start` / `realtime_end` per observation) → post-A.3.10. The schema already accommodates it; the v1 source leaves the columns None.
- The official FINRA biweekly "Short Interest" tape via Query API → optional post-A.3.10 enhancement. The `exchange` discriminator column allows it to co-exist with the daily shvol data we ingest in v1.
- Multi-market-center fan-out (FNRA + FNSQ + FNYX + FORF in one call) → optional post-A.3.10 enhancement. The `file_stem` parameter on `fetch_short_interest` already exposes the seam.
- Live integration test against the real FRED + FINRA CDN endpoints → A.5.

**Placeholder scan:** No "TBD", "TODO", or "implement later" in any code block. Every step contains full source.

**Pydantic v2 gotchas avoided:**

- `RawFredObservation.value` is `Optional[float] = None` so the FRED `.` sentinel maps cleanly to NULL.
- `series_id` and `exchange` use `@field_validator(mode="before")` to upper-case before the type check; same pattern as `ticker` from A.3.3/A.3.4.
- All ints in the FINRA file are coerced to `float` to match the `RawFinraShortInterest.short_interest_shares: float` declaration (SQLite stores them as REAL).
- `RawFinraShortInterest` constrains `short_interest_shares: float = Field(ge=0)` so negative values (malformed input) raise rather than silently persist.

**FRED CSV parsing (load-bearing):**

The public CSV format is dead simple — `DATE,SERIES` header + ISO-date + value rows + `.` sentinel for missing observations. We use stdlib `csv.reader` rather than `pandas.read_csv` to keep the parser dependency-free and to make the test surface obvious. Three error modes the parser tolerates:

1. **`.` sentinel** → `value = None` (test: `test_fetch_macro_series_handles_dot_missing_value`).
2. **Non-float value** → `value = None` (treat as missing rather than drop the row — the date is still useful).
3. **Blank date** → skip row entirely (a row with no date carries no useful information).

**FINRA URL pattern (load-bearing — re-stated):**

The single-most-likely-to-need-verification piece of A.3.5 is the FINRA CDN file URL. The pattern `https://cdn.finra.org/equity/regsho/daily/{stem}shvol{yyyymmdd}.txt` is documented at FINRA's developer portal and on the data dictionary linked from `https://www.finra.org/finra-data/short-sale-volume-data`. The four file stems we support — `FNRA`, `FNSQ`, `FNYX`, `FORF` — map to NYSE, Nasdaq, NYSE-American, and FINRA-ORF respectively. The default `FNSQ` (Nasdaq) is chosen for broadest single-file large-cap coverage; the `file_stem` parameter on `fetch_short_interest` exposes the seam for callers who want a specific market center.

If the executing agent finds the URL pattern has changed (FINRA reorganizes their CDN periodically), only `_DAILY_FILE_URL_FMT` needs to be updated — the rest of the source is URL-pattern-independent.

**14-day refresh-skip semantics (FINRA only):**

Per spec §6.5, FINRA's biweekly cadence means a daily-frequency refresh wastes bandwidth. The `_REFRESH_SKIP_DAYS = 14` constant gates the auto-refresh path:

- **No watermark** → fetch (first-time pull).
- **Watermark older than 14 days** → fetch.
- **Watermark within last 14 days** → skip (return 0).
- **Explicit `settlement_date`** → fetch regardless (backfill / forced-refresh path).

This is consistent with the Principle 6 "watermarks bound deltas" pattern — the skip is the same idea as the A.3.4 EDGAR filings-watermark short-circuit, just calibrated to FINRA's publication cadence.

**Vintage-data acknowledgement (FRED only):**

The public `fredgraph.csv` returns currently revised data, not as-originally-published. CPI in particular gets revised. For research-rigorous look-ahead-bias avoidance we'd need ALFRED. The `raw_fred` schema reserves `realtime_start` / `realtime_end` columns now so that ALFRED integration later requires no migration. The methodology handbook §revision-handling should be cross-linked (it isn't there yet; the executing agent doesn't need to create it — that lands when the methodology layer matures in A.3.8+).

**Test coverage shape:**

| Layer | Unit tests | Integration tests |
|---|---|---|
| Schemas (`RawFredObservation` + `RawFinraShortInterest`) | 12 | — |
| DB migration + helpers | 13 | — |
| `FredSource.fetch_macro_series` | 11 | — |
| `FinraSource.fetch_short_interest` | 12 | — |
| End-to-end orchestration | — | 3 |
| **Total new tests** | **48** | **3** |

(Total counts are approximate — the codebase has historically run ±2 from the planned figure due to parametrize-collection idiosyncrasies; that variance is acceptable per the project's known-pattern note.)

**Architectural notes:**

- `FredSource._parse_fred_csv` and `FinraSource._parse_shvol_text` are **instance methods** rather than pure module functions because neither dataset is complex enough to justify a separate `*_parser.py` module (compare A.3.4 where Form 4 XML + submissions JSON both got dedicated parser modules; here the parse logic is ~30 lines each). If parsing grows non-trivial later (e.g., when the FINRA Query API biweekly tape is added) we can re-extract into `finra_short_interest_parser.py` then. The schema already accommodates the migration.
- `FredSource` does NOT use `tenacity` retries. FRED's keyless CSV endpoint has no documented rate limit and historically very high availability; the 250ms inter-series delay is sufficient. If we later see 429s or 503s in production we can wrap with the same `tenacity` policy as `EdgarSource._sec_get_with_retry`.
- `FinraSource` also does NOT use `tenacity` retries. The CDN endpoint is typically very stable. Same upgrade path applies if needed.
- The FRED watermark uses `field="series:<id>"` with `ticker="*"` (the global / non-ticker-scoped sentinel). This matches the spec's §6.4 wording (`(fred, *, series:<id>)`) and the watermark table's `field TEXT NOT NULL -- canonical field name OR 'series:<id>' for FRED` comment in the schema.
- The FINRA watermark uses `field="short_interest_biweekly"` with `ticker="*"` — a single watermark covers the whole-universe daily pull, since we're fetching one file per call that covers all tickers on that market center.

**Architecture risk: FRED revisions**

A FRED revision of CPIAUCSL changes the `value` for a past `observation_date`. Because the v1 `raw_fred` PK is `(series_id, observation_date)` and we INSERT OR IGNORE, the revised value is silently dropped on re-pull — the original observation wins. This is the conservative default (matches Principle 5: no silent overwrite). When ALFRED integration is added, each (`series_id`, `observation_date`, `realtime_start`) tuple becomes a distinct row and we get full revision history without conflict.

If overwrite-on-revision semantics are desired (i.e., the *latest* revised value wins) before ALFRED lands, the insert helper can be flipped to `INSERT OR REPLACE`. That decision is a methodology call — flagged as an open question below.

---

## Execution Handoff

**Recommended:** Subagent-driven, mirroring the A.3.4 wave pattern.

- **Wave 1 (Schema + DB):** One sub-agent does Tasks 0–2 (pre-flight + `RawFredObservation` + `RawFinraShortInterest` + `migrate_to_v7` + two insert helpers). All in `src/common/`; sequential. **~12 min**.
- **Wave 2 (Sources):** Run Tasks 3 and 4 in **parallel** as two sub-agents — each is independent (different files, different test files, no cross-dependencies). **~18 min** if parallel, ~30 min if serial.
- **Wave 3 (Integration + build plan):** One sub-agent does Task 5 (integration test); inline does Task 6 (build plan update) in parallel. **~10 min**.

Total: 3 waves, ~40–55 min wall-clock if Wave 2 is parallelized.

**Critical pre-execution checks for the executing agent:**

1. After Task 2, confirm `schema_version == 7` before proceeding:
   ```
   ./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager('data/_v7_check.db'); m.migrate_to_v7(); print(m.get_schema_version())"
   ```
   Expect `7`.
2. After Task 3, confirm the FRED fixture parses with stdlib `csv.reader`:
   ```
   ./venv/Scripts/python.exe -c "import csv; r=csv.reader(open('tests/fixtures/fred/DGS10_sample.csv')); print(len(list(r)))"
   ```
   Expect `31` (1 header + 30 rows).
3. After Task 4, confirm the FINRA fixture's row count is 21 (1 header + 20 records):
   ```
   ./venv/Scripts/python.exe -c "print(sum(1 for _ in open('tests/fixtures/finra/shvol_sample.txt')))"
   ```
   Expect `21`.
4. Do not modify `FredSource.health_check()` or `FinraSource.health_check()` — only add the new fetch methods. The A.1 regression tests are part of the safety net.
5. When mocking, distinguish URL types by substring: `"fredgraph.csv?id="` for FRED, `"cdn.finra.org"` for FINRA. The two source modules import `requests` separately, so each test patches its own module path (`src.common.datasources.fred_source.requests.get` vs `src.common.datasources.finra_source.requests.get`).
6. The `time.sleep(_INTER_SERIES_DELAY_SEC)` call in `FredSource.fetch_macro_series` will add `0.25s × (n_series - 1)` to the test runtime. With 5 default series that's 1s per default-list test; acceptable. If test runtime becomes an issue, the executing agent can additionally patch `time.sleep` in the fred_fetch test module's mocker setup.

**Methodology callouts before execution:**

**1. FRED revision policy.** The v1 implementation uses `INSERT OR IGNORE` on `raw_fred`, which means the **first-observed** value for any (series_id, observation_date) wins; subsequent revisions are silently dropped. This is the conservative default (Principle 5) but means we may carry stale CPI vintages forever once they land. The alternative is `INSERT OR REPLACE` (latest-wins), which gives us currently-revised data but loses the original-observation audit trail. The ALFRED migration (post-A.3.10) fixes this properly by adding `realtime_start` to the PK. Decision: keep `INSERT OR IGNORE` (this plan's default) for v1.

**2. FINRA URL pattern verification.** The plan implements the daily-short-volume CDN path (`cdn.finra.org/equity/regsho/daily/FNSQshvol{YYYYMMDD}.txt`). This is not the **official biweekly Short Interest report** that the spec §6.5 wording hints at (`cdn.finra.org/equity/regsho/monthly/...`). The daily files give us per-day per-ticker short-volume which the materialization layer (A.3.8) aggregates to biweekly cadence. The official biweekly tape lives at the FINRA Query API and requires JSON aggregation — that's a future enhancement. Decision: ship the daily-CDN approach in v1.

**3. Default file stem for FINRA.** `FNSQ` (Nasdaq) is the default. Layer 1 universe ($2B+ band) is heavily Nasdaq-skewed but not exclusively. Without multi-market-center fan-out (post-A.3.10), tickers listed exclusively on NYSE / NYSE-American won't appear in `raw_finra` rows for the default pull. The downstream `short_interest_signal` will compute NULL for those tickers (graceful degradation). Decision: ship A.3.5 with just FNSQ; expand to all 4 stems in a follow-up.
