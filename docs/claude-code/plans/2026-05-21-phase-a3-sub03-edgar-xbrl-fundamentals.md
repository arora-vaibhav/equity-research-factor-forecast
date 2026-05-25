# Phase A.3.3 — EDGAR XBRL Fundamentals (10-K / 10-Q Parser + Watermarked Persistence)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]` checkbox syntax for tracking.

**Goal:** Make `EdgarSource` actually pull structured fundamentals end-to-end. Build the ticker→CIK resolver, the XBRL companyfacts parser, the per-ticker fetcher, and the batch universe wrapper. Migrate schema v4 → v5 to add `raw_edgar_fundamentals` (structured per-(run_id, ticker, fiscal_year, fiscal_period) output) and `sec_ticker_cik_map` (additive lookup snapshot). All HTTP is mockable, watermark-aware (Principle 6), SEC-fair-access compliant (User-Agent + ≤10 req/s with tenacity retry on 429/503), and idempotent (`INSERT OR IGNORE` semantics on the structured rows — once a (fiscal_period, fiscal_year) is recorded, restated values from later filings do NOT overwrite the original; Principle 5/6).

**Architecture:** Additive only. Migration v4→v5 creates `raw_edgar_fundamentals` + `sec_ticker_cik_map` and bumps schema_version. `EdgarSource` gets three new methods: `_resolve_cik(ticker)` (private + delegated to `sec_cik_lookup.py` for testability), `fetch_fundamentals_for_ticker(ticker, run_id, db)`, and `fetch_universe(run_id, ticker_list, db)`. The XBRL parser lives in a **pure-function** module (`src/common/datasources/edgar_xbrl_parser.py`) so it can be unit-tested against a fixture JSON without any networking. All `requests.get` calls are wrapped with a `tenacity` retry decorator (exponential backoff on 429/503/connection errors) sharing one shared `_sec_get(url, headers)` helper inside `edgar_source.py`.

**Tech Stack:** Python 3.11+, `requests`, `tenacity` (already a dep — used in A.1 plans), `pydantic` v2, `pytest`, `pytest-mock`. No new runtime dependencies.

**Spec reference:** [docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md](../specs/2026-05-21-phase-a3-layer1-hardening-design.md) §6.3.1 (10-K/10-Q XBRL fundamentals) + §3 Principle 6 (watermarks bound deltas) + §3 Principle 5 (no overwrite without `force_refetch`).

**Out of scope for A.3.3:**
- EDGAR Form 4 insider transactions → A.3.4
- EDGAR 8-K filing index → A.3.4
- Cohen-Malloy-Pomorski opportunistic-insider classifier → A.3.4
- FRED / FINRA / stockanalysis / OpenBB → A.3.5 onward
- News activity score sub-signals → A.3.8
- LM tone / Yang-Zhang vol / factor refactor / dual-write → A.3.9 / A.3.10
- Live integration test against the real SEC endpoint → A.5 acceptance phase
- Restated-financials reconciliation (we store first-observed value per fiscal_period; restatements are visible via `accn` audit trail in the source JSON but not re-persisted)

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| `src/common/schemas.py` | Add `RawEdgarFundamentalsRow` Pydantic model | Modify |
| `src/common/database.py` | Add `migrate_to_v5()` + `insert_raw_edgar_fundamentals()` + `upsert_sec_ticker_cik_map()` + `get_cik_for_ticker()` | Modify |
| `src/common/datasources/sec_cik_lookup.py` | NEW — ticker→CIK resolver (loads `company_tickers.json`, caches in-memory + DB) | Create |
| `src/common/datasources/edgar_xbrl_parser.py` | NEW — pure-function `parse_companyfacts(blob) -> list[RawEdgarFundamentalsRow]` | Create |
| `src/common/datasources/edgar_source.py` | Add `_sec_get` (tenacity-wrapped), `_resolve_cik`, `fetch_fundamentals_for_ticker`, `fetch_universe` | Modify |
| `tests/fixtures/edgar/CIK0000320193.json` | NEW — synthetic but realistic Apple companyfacts excerpt | Create |
| `tests/fixtures/edgar/company_tickers.json` | NEW — synthetic ticker-CIK map (3 tickers) | Create |
| `tests/common/test_schemas_a3_3.py` | Tests for `RawEdgarFundamentalsRow` | Create |
| `tests/common/test_database_a3_3.py` | Tests for `migrate_to_v5` + `insert_raw_edgar_fundamentals` + CIK map helpers | Create |
| `tests/datasources/test_sec_cik_lookup.py` | Tests for ticker→CIK resolver | Create |
| `tests/datasources/test_edgar_xbrl_parser.py` | Tests for the pure-function parser against the fixture | Create |
| `tests/datasources/test_edgar_fetch.py` | Tests for `fetch_fundamentals_for_ticker` + `fetch_universe` (mocked HTTP) | Create |
| `tests/datasources/test_integration_a3_3.py` | Mock-level end-to-end orchestration with DB persistence + watermark respect | Create |
| the build plan | Mark A.3.3 shipped | Modify |

---

## Task 0: Pre-flight verification

**Files:** none modified

- [ ] **Step 1: Verify schema is at v4 from A.3.2**

Run from the repository root:

```
./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager(); print('current schema version:', m.get_schema_version())"
```

Expected: prints `current schema version: 4`.

- [ ] **Step 2: Verify A.3.2 baseline tests still pass**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: `177 passed, 2 deselected`.

- [ ] **Step 3: Verify tenacity + requests importable from venv**

```
./venv/Scripts/python.exe -c "import tenacity, requests; print('tenacity', tenacity.__version__); print('requests', requests.__version__)"
```

Expected: both version strings print.

- [ ] **Step 4: Verify working tree is clean before starting**

Run: `git status -s`
Expected: empty output (or only an unrelated `.env`). If anything else is uncommitted, stop and report.

No commit at this task — verification only.

---

## Task 1: `RawEdgarFundamentalsRow` Pydantic model

**Files:**
- Modify: `src/common/schemas.py` (append)
- Create: `tests/common/test_schemas_a3_3.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_schemas_a3_3.py`:

```python
"""Tests for Phase A.3.3 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import RawEdgarFundamentalsRow


class TestRawEdgarFundamentalsRow:
    def test_minimal_valid(self):
        r = RawEdgarFundamentalsRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            fiscal_year=2023,
            fiscal_period="annual",
            filing_date="2023-11-03",
            form_type="10-K",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"
        assert r.cik == "0000320193"
        assert r.fiscal_year == 2023
        assert r.fiscal_period == "annual"
        assert r.revenue_ttm is None
        assert r.operating_margin is None

    def test_full_valid(self):
        r = RawEdgarFundamentalsRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            fiscal_year=2023,
            fiscal_period="annual",
            filing_date="2023-11-03",
            accepted_at="2023-11-03T18:01:14.000Z",
            form_type="10-K",
            revenue_ttm=383285000000.0,
            ebit_ttm=114301000000.0,
            net_income_ttm=96995000000.0,
            total_assets=352755000000.0,
            total_liabilities=290437000000.0,
            total_equity=62146000000.0,
            cash_and_equivalents=29965000000.0,
            total_debt=111088000000.0,
            operating_cashflow=110543000000.0,
            capex=-10959000000.0,
            fcf_ttm=99584000000.0,
            operating_margin=0.2982,
            net_profit_margin=0.2531,
            total_debt_to_equity=1.7876,
            interest_coverage=29.84,
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.revenue_ttm == 383285000000.0
        assert -1.0 <= r.operating_margin <= 1.0
        assert r.total_debt_to_equity > 0

    def test_fiscal_period_must_be_valid_enum(self):
        with pytest.raises(Exception):
            RawEdgarFundamentalsRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                fiscal_year=2023,
                fiscal_period="not-a-period",
                filing_date="2023-11-03",
                form_type="10-K",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_form_type_must_be_valid_enum(self):
        with pytest.raises(Exception):
            RawEdgarFundamentalsRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                fiscal_year=2023,
                fiscal_period="annual",
                filing_date="2023-11-03",
                form_type="S-1",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_cik_normalized_to_10_digit_string(self):
        r = RawEdgarFundamentalsRow(
            run_id="r1",
            ticker="AAPL",
            cik="320193",
            fiscal_year=2023,
            fiscal_period="annual",
            filing_date="2023-11-03",
            form_type="10-K",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.cik == "0000320193"

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            RawEdgarFundamentalsRow(
                run_id="r1",
                ticker="not-a-ticker",
                cik="0000320193",
                fiscal_year=2023,
                fiscal_period="annual",
                filing_date="2023-11-03",
                form_type="10-K",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_operating_margin_out_of_range_rejected(self):
        with pytest.raises(Exception):
            RawEdgarFundamentalsRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                fiscal_year=2023,
                fiscal_period="annual",
                filing_date="2023-11-03",
                form_type="10-K",
                operating_margin=1.5,
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_3.py -v
```

Expected: `ImportError` on `RawEdgarFundamentalsRow`.

- [ ] **Step 3: Append `RawEdgarFundamentalsRow` to `src/common/schemas.py`**

At the **end** of `src/common/schemas.py`, append:

```python


# === Phase A.3.3 - EDGAR XBRL fundamentals row =========================


class RawEdgarFundamentalsRow(BaseModel):
    """One per-period structured fundamentals row parsed from the SEC
    companyfacts XBRL JSON.

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.3.1. Stored in raw_edgar_fundamentals.

    Primary key conceptually: (run_id, ticker, fiscal_period, fiscal_year).
    A `fiscal_period` of "annual" represents a 10-K full-year value or
    a TTM proxy (sum of last 4 quarterly observations). Quarterly values
    (Q1/Q2/Q3/Q4) are the as-reported 10-Q numbers for that quarter.
    """

    run_id: str
    ticker: str
    cik: str
    fiscal_year: int = Field(ge=1900, le=2100)
    fiscal_period: Literal["annual", "Q1", "Q2", "Q3", "Q4"]
    filing_date: str
    accepted_at: Optional[str] = None
    form_type: Literal["10-K", "10-Q"]

    revenue_ttm: Optional[float] = None
    ebit_ttm: Optional[float] = None
    net_income_ttm: Optional[float] = None
    total_assets: Optional[float] = None
    total_liabilities: Optional[float] = None
    total_equity: Optional[float] = None
    cash_and_equivalents: Optional[float] = None
    total_debt: Optional[float] = None
    operating_cashflow: Optional[float] = None
    capex: Optional[float] = None
    fcf_ttm: Optional[float] = None
    operating_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    net_profit_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    total_debt_to_equity: Optional[float] = None
    interest_coverage: Optional[float] = None

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

    @field_validator("cik", mode="before")
    @classmethod
    def _normalize_cik(cls, v: object) -> str:
        if v is None:
            raise ValueError("cik is required")
        s = str(v).strip()
        # Strip CIK prefix if present, then zero-pad to 10 digits
        if s.upper().startswith("CIK"):
            s = s[3:]
        s = s.lstrip("0") or "0"
        if not s.isdigit():
            raise ValueError(f"invalid cik (non-digit): {v!r}")
        return s.zfill(10)
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_3.py -v
```

Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/schemas.py tests/common/test_schemas_a3_3.py
git commit -m "feat(schemas): add RawEdgarFundamentalsRow model for A.3.3 EDGAR XBRL fundamentals"
```

---

## Task 2: `migrate_to_v5()` + `insert_raw_edgar_fundamentals()` + CIK map helpers

**Files:**
- Modify: `src/common/database.py`
- Create: `tests/common/test_database_a3_3.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_database_a3_3.py`:

```python
"""Tests for Phase A.3.3 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawEdgarFundamentalsRow


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


# --- migrate_to_v5 ----------------------------------------------------


def test_migrate_to_v5_creates_raw_edgar_fundamentals_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    assert "raw_edgar_fundamentals" in _table_names(db_path)


def test_migrate_to_v5_creates_sec_ticker_cik_map_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    assert "sec_ticker_cik_map" in _table_names(db_path)


def test_raw_edgar_fundamentals_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    cols = _columns(db_path, "raw_edgar_fundamentals")
    expected = {
        "run_id", "ticker", "cik",
        "fiscal_year", "fiscal_period",
        "filing_date", "accepted_at", "form_type",
        "revenue_ttm", "ebit_ttm", "net_income_ttm",
        "total_assets", "total_liabilities", "total_equity",
        "cash_and_equivalents", "total_debt",
        "operating_cashflow", "capex", "fcf_ttm",
        "operating_margin", "net_profit_margin",
        "total_debt_to_equity", "interest_coverage",
        "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_sec_ticker_cik_map_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    cols = _columns(db_path, "sec_ticker_cik_map")
    expected = {"ticker", "cik", "company_name", "snapshot_at"}
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v5_bumps_schema_version_to_5(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5]


def test_migrate_to_v5_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.migrate_to_v5()
    mgr.migrate_to_v5()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5]


def test_get_schema_version_returns_5_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    assert mgr.get_schema_version() == 5


def test_migrate_to_v5_preserves_v4_data(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-20", success=True,
    )
    mgr.migrate_to_v5()
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w is not None
    assert w["last_observation_date"] == "2026-05-20"


# --- insert_raw_edgar_fundamentals ------------------------------------


def _row(**overrides):
    base = dict(
        run_id="r1", ticker="AAPL", cik="0000320193",
        fiscal_year=2023, fiscal_period="annual",
        filing_date="2023-11-03", form_type="10-K",
        revenue_ttm=383285000000.0,
        net_income_ttm=96995000000.0,
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawEdgarFundamentalsRow(**base)


def test_insert_raw_edgar_fundamentals_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.insert_raw_edgar_fundamentals([_row()])
    with sqlite3.connect(db_path) as c:
        result = c.execute(
            "SELECT ticker, fiscal_year, fiscal_period, revenue_ttm "
            "FROM raw_edgar_fundamentals WHERE run_id='r1'"
        ).fetchone()
    assert result == ("AAPL", 2023, "annual", 383285000000.0)


def test_insert_raw_edgar_fundamentals_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.insert_raw_edgar_fundamentals([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_edgar_fundamentals").fetchone()[0]
    assert n == 0


def test_insert_raw_edgar_fundamentals_uses_insert_or_ignore(tmp_path: Path):
    """Principle 5/6: once a (run_id, ticker, fiscal_period, fiscal_year) row
    is written, a second insert with the same PK is silently ignored (i.e.,
    original value preserved). Restated financials do not overwrite the first
    observation."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.insert_raw_edgar_fundamentals([_row(revenue_ttm=383285000000.0)])
    # Try to overwrite with a "restated" value
    mgr.insert_raw_edgar_fundamentals([_row(revenue_ttm=999999999999.0)])
    with sqlite3.connect(db_path) as c:
        rev = c.execute(
            "SELECT revenue_ttm FROM raw_edgar_fundamentals WHERE run_id='r1' AND ticker='AAPL'"
        ).fetchone()[0]
    assert rev == 383285000000.0


def test_insert_raw_edgar_fundamentals_supports_multiple_periods_same_ticker(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.insert_raw_edgar_fundamentals([
        _row(fiscal_period="annual", fiscal_year=2023),
        _row(fiscal_period="Q1", fiscal_year=2024, form_type="10-Q",
             filing_date="2024-02-02"),
        _row(fiscal_period="Q2", fiscal_year=2024, form_type="10-Q",
             filing_date="2024-05-03"),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_fundamentals WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert n == 3


# --- CIK map helpers --------------------------------------------------


def test_upsert_sec_ticker_cik_map_inserts(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."},
        {"ticker": "MSFT", "cik": "0000789019", "company_name": "Microsoft Corp"},
    ])
    assert mgr.get_cik_for_ticker("AAPL") == "0000320193"
    assert mgr.get_cik_for_ticker("MSFT") == "0000789019"


def test_upsert_sec_ticker_cik_map_updates_existing(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."}
    ])
    mgr.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc. (Updated)"}
    ])
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT company_name FROM sec_ticker_cik_map WHERE ticker='AAPL'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Apple Inc. (Updated)"


def test_get_cik_for_ticker_returns_none_when_absent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    assert mgr.get_cik_for_ticker("NOPE") is None
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_3.py -v
```

Expected: `AttributeError` on `migrate_to_v5`.

- [ ] **Step 3: Add `migrate_to_v5()` to `DatabaseManager`**

In `src/common/database.py`, **locate** `migrate_to_v4()`. Immediately AFTER it (before `get_connection`), insert:

```python
    def migrate_to_v5(self) -> None:
        """Idempotent migration v4 -> v5 per A.3 spec section 6.3.1.

        Adds:
          - raw_edgar_fundamentals: per-(run_id, ticker, fiscal_period, fiscal_year)
            structured XBRL output parsed from companyfacts JSON.
          - sec_ticker_cik_map: cached ticker->CIK lookup snapshot
            (refreshed daily via the sec_cik_lookup watermark).

        Safe to call multiple times. Existing data preserved.

        raw_edgar_fundamentals PRIMARY KEY = (run_id, ticker, fiscal_period, fiscal_year).
        Inserts use INSERT OR IGNORE: once a period is recorded, subsequent runs
        on the same run_id do not overwrite (Principle 5 — no silent overwrite).
        """
        self.migrate_to_v4()

        v5_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_edgar_fundamentals (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              cik TEXT NOT NULL,
              fiscal_year INTEGER NOT NULL,
              fiscal_period TEXT NOT NULL,
              filing_date TEXT NOT NULL,
              accepted_at TEXT,
              form_type TEXT NOT NULL,
              revenue_ttm REAL,
              ebit_ttm REAL,
              net_income_ttm REAL,
              total_assets REAL,
              total_liabilities REAL,
              total_equity REAL,
              cash_and_equivalents REAL,
              total_debt REAL,
              operating_cashflow REAL,
              capex REAL,
              fcf_ttm REAL,
              operating_margin REAL,
              net_profit_margin REAL,
              total_debt_to_equity REAL,
              interest_coverage REAL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (run_id, ticker, fiscal_period, fiscal_year)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_fund_ticker ON raw_edgar_fundamentals(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_fund_cik ON raw_edgar_fundamentals(cik)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_fund_filing_date ON raw_edgar_fundamentals(filing_date)",
            """
            CREATE TABLE IF NOT EXISTS sec_ticker_cik_map (
              ticker TEXT PRIMARY KEY,
              cik TEXT NOT NULL,
              company_name TEXT,
              snapshot_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_sec_ticker_cik_map_cik ON sec_ticker_cik_map(cik)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v5_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 5")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (5, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()
```

In `src/common/database.py`, at the **end** of the `DatabaseManager` class, append:

```python
    # ------------------------------------------------------------------
    # Phase A.3.3: raw_edgar_fundamentals + ticker->CIK map helpers
    # ------------------------------------------------------------------

    def insert_raw_edgar_fundamentals(self, rows: list) -> None:
        """Insert RawEdgarFundamentalsRow records into raw_edgar_fundamentals.

        Uses INSERT OR IGNORE — once a (run_id, ticker, fiscal_period, fiscal_year)
        is recorded, subsequent inserts with the same PK are silently dropped.
        Restated financials from a later filing therefore do NOT overwrite the
        original first-observed value. Use force_refetch() for deliberate
        operator-initiated reset (Principle 5).
        """
        if not rows:
            return
        from src.common.schemas import RawEdgarFundamentalsRow
        records = [
            r.model_dump() if isinstance(r, RawEdgarFundamentalsRow) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_edgar_fundamentals ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()

    def upsert_sec_ticker_cik_map(self, entries: list) -> None:
        """Bulk-insert ticker->CIK rows; UPDATE company_name on conflict.

        entries is a list of dicts with keys: ticker, cik, company_name.
        """
        if not entries:
            return
        now_iso = datetime.datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO sec_ticker_cik_map (ticker, cik, company_name, snapshot_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                  cik = excluded.cik,
                  company_name = excluded.company_name,
                  snapshot_at = excluded.snapshot_at
                """,
                [
                    (
                        e["ticker"].upper().strip(),
                        str(e["cik"]).zfill(10),
                        e.get("company_name"),
                        now_iso,
                    )
                    for e in entries
                ],
            )
            conn.commit()

    def get_cik_for_ticker(self, ticker: str) -> str | None:
        """Return 10-digit padded CIK for ticker, or None if not in the map."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT cik FROM sec_ticker_cik_map WHERE ticker = ?",
                (ticker.upper().strip(),),
            ).fetchone()
        return row[0] if row else None
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_3.py -v
```

Expected: All 14 tests PASS.

- [ ] **Step 5: Run full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: ~198 passed (177 baseline + 7 schema + 14 database).

- [ ] **Step 6: Commit**

```
git add src/common/database.py tests/common/test_database_a3_3.py
git commit -m "feat(database): migrate_to_v5 - raw_edgar_fundamentals + sec_ticker_cik_map (INSERT OR IGNORE)"
```

---

## Task 3: Ticker→CIK lookup module (`sec_cik_lookup.py`)

**Files:**
- Create: `src/common/datasources/sec_cik_lookup.py`
- Create: `tests/fixtures/edgar/company_tickers.json`
- Create: `tests/datasources/test_sec_cik_lookup.py`

- [ ] **Step 1: Create the fixture file**

Create `tests/fixtures/edgar/company_tickers.json` with the exact SEC schema (a dict keyed by integer string, each value has `cik_str`, `ticker`, `title`):

```json
{
  "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
  "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
  "2": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."}
}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/datasources/test_sec_cik_lookup.py`:

```python
"""Tests for ticker->CIK resolver (Phase A.3.3)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.sec_cik_lookup import (
    SecCikLookup,
    TickerNotFoundError,
)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "company_tickers.json"


@pytest.fixture
def fixture_blob():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v5()
    return mgr


def test_resolve_returns_padded_cik_for_known_ticker(db, mocker, fixture_blob):
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    lookup = SecCikLookup(db=db)
    assert lookup.resolve("AAPL") == "0000320193"
    assert lookup.resolve("MSFT") == "0000789019"


def test_resolve_is_case_insensitive(db, mocker, fixture_blob):
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    assert SecCikLookup(db=db).resolve("aapl") == "0000320193"


def test_resolve_unknown_ticker_raises(db, mocker, fixture_blob):
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    with pytest.raises(TickerNotFoundError):
        SecCikLookup(db=db).resolve("FAKE")


def test_resolve_fetches_only_once_per_process(db, mocker, fixture_blob):
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    get_mock = mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    lookup = SecCikLookup(db=db)
    lookup.resolve("AAPL")
    lookup.resolve("MSFT")
    lookup.resolve("GOOGL")
    assert get_mock.call_count == 1


def test_resolve_persists_to_sec_ticker_cik_map(db, mocker, fixture_blob):
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    SecCikLookup(db=db).resolve("AAPL")
    assert db.get_cik_for_ticker("AAPL") == "0000320193"
    assert db.get_cik_for_ticker("MSFT") == "0000789019"


def test_resolve_uses_db_cache_when_watermark_fresh(db, mocker):
    """If watermark (sec, *, ticker_cik_map) is from today, skip the HTTP call
    and use the DB snapshot."""
    db.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."}
    ])
    import datetime as _dt
    today = _dt.date.today().isoformat()
    db.upsert_watermark(
        source="sec", ticker="*", field="ticker_cik_map",
        last_observation_date=today, success=True,
    )
    get_mock = mocker.patch("src.common.datasources.sec_cik_lookup.requests.get")
    assert SecCikLookup(db=db).resolve("AAPL") == "0000320193"
    get_mock.assert_not_called()


def test_resolve_refetches_when_watermark_stale(db, mocker, fixture_blob):
    """If watermark is older than 1 day, refresh."""
    db.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000000000", "company_name": "Stale"}
    ])
    db.upsert_watermark(
        source="sec", ticker="*", field="ticker_cik_map",
        last_observation_date="2020-01-01", success=True,
    )
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    assert SecCikLookup(db=db).resolve("AAPL") == "0000320193"


def test_resolve_uses_sec_user_agent_header(db, mocker, fixture_blob):
    mock_resp = mocker.MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fixture_blob
    get_mock = mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        return_value=mock_resp,
    )
    SecCikLookup(db=db).resolve("AAPL")
    kwargs = get_mock.call_args.kwargs
    headers = kwargs.get("headers") or {}
    assert "User-Agent" in headers
    assert "@" in headers["User-Agent"]
```

- [ ] **Step 3: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_sec_cik_lookup.py -v
```

Expected: `ImportError` on `sec_cik_lookup`.

- [ ] **Step 4: Create the resolver module**

Create `src/common/datasources/sec_cik_lookup.py`:

```python
"""Ticker -> CIK lookup, backed by SEC's free `company_tickers.json` endpoint.

The SEC publishes a daily-refreshed JSON file at:
  https://www.sec.gov/files/company_tickers.json

Format (per SEC EDGAR docs):
  {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    ...
  }

This module loads it once per process (in-memory cache) and also persists a
snapshot to the `sec_ticker_cik_map` table. The (sec, *, ticker_cik_map)
watermark gates a daily refresh — if the watermark is from today, we serve
from the DB cache without HTTP.

SEC fair access: User-Agent header with contact email is REQUIRED on every
request to *.sec.gov per https://www.sec.gov/os/accessing-edgar-data.
"""
from __future__ import annotations

import datetime
import os
from typing import Optional

import requests

_CT_URL = "https://www.sec.gov/files/company_tickers.json"
_DEFAULT_UA = "Trade Identifier research@example.com"


class TickerNotFoundError(KeyError):
    """Raised when a ticker is not in the SEC company_tickers.json map."""


class SecCikLookup:
    """Resolve a ticker symbol to its 10-digit zero-padded CIK string."""

    def __init__(self, db):
        self.db = db
        self._cache: Optional[dict[str, str]] = None  # ticker -> padded CIK
        self._loaded_this_process = False

    def _headers(self) -> dict[str, str]:
        ua = os.environ.get("SEC_EDGAR_USER_AGENT", _DEFAULT_UA)
        return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}

    def _is_watermark_fresh(self) -> bool:
        w = self.db.get_watermark("sec", "*", "ticker_cik_map")
        if w is None or w.get("last_observation_date") is None:
            return False
        try:
            last = datetime.date.fromisoformat(w["last_observation_date"])
        except (TypeError, ValueError):
            return False
        return last >= datetime.date.today()

    def _load_from_db(self) -> dict[str, str]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT ticker, cik FROM sec_ticker_cik_map"
            ).fetchall()
        return {t.upper(): c for (t, c) in rows}

    def _fetch_from_sec(self) -> dict[str, dict]:
        resp = requests.get(_CT_URL, headers=self._headers(), timeout=15)
        if not resp.ok:
            raise RuntimeError(f"company_tickers.json HTTP {resp.status_code}")
        return resp.json()

    def _persist_snapshot(self, raw: dict) -> dict[str, str]:
        entries = []
        for _idx, rec in raw.items():
            ticker = str(rec.get("ticker", "")).strip().upper()
            cik = str(rec.get("cik_str", "")).strip()
            if not ticker or not cik:
                continue
            entries.append({
                "ticker": ticker,
                "cik": cik.zfill(10),
                "company_name": rec.get("title"),
            })
        self.db.upsert_sec_ticker_cik_map(entries)
        today_iso = datetime.date.today().isoformat()
        self.db.upsert_watermark(
            source="sec", ticker="*", field="ticker_cik_map",
            last_observation_date=today_iso, success=True,
        )
        return {e["ticker"]: e["cik"] for e in entries}

    def _ensure_loaded(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if self._is_watermark_fresh():
            self._cache = self._load_from_db()
            self._loaded_this_process = True
            return self._cache
        raw = self._fetch_from_sec()
        self._cache = self._persist_snapshot(raw)
        self._loaded_this_process = True
        return self._cache

    def resolve(self, ticker: str) -> str:
        """Return the 10-digit padded CIK for ticker. Raises TickerNotFoundError
        if the ticker is not in the SEC map."""
        t = ticker.strip().upper()
        cache = self._ensure_loaded()
        if t not in cache:
            raise TickerNotFoundError(f"ticker not in SEC company_tickers.json: {t!r}")
        return cache[t]
```

- [ ] **Step 5: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_sec_cik_lookup.py -v
```

Expected: All 8 tests PASS.

- [ ] **Step 6: Commit**

```
git add src/common/datasources/sec_cik_lookup.py tests/datasources/test_sec_cik_lookup.py tests/fixtures/edgar/company_tickers.json
git commit -m "feat(datasources): SecCikLookup - ticker->CIK resolver with in-memory + DB cache + daily watermark"
```

---

## Task 4: XBRL companyfacts parser (`edgar_xbrl_parser.py`)

**Files:**
- Create: `tests/fixtures/edgar/CIK0000320193.json`
- Create: `src/common/datasources/edgar_xbrl_parser.py`
- Create: `tests/datasources/test_edgar_xbrl_parser.py`

- [ ] **Step 1: Create the fixture (synthetic Apple companyfacts JSON)**

Create `tests/fixtures/edgar/CIK0000320193.json` with the literal content below. This is a SYNTHETIC fixture — numbers are realistic-shaped and align with public Apple 10-K figures, but the goal is unit-testability of the parser. Real production fetches use the live SEC endpoint at run time.

```json
{
  "cik": 320193,
  "entityName": "Apple Inc.",
  "facts": {
    "us-gaap": {
      "Revenues": {
        "label": "Revenues",
        "description": "Aggregate revenue recognized.",
        "units": {
          "USD": [
            {"end": "2022-09-24", "val": 394328000000, "fy": 2022, "fp": "FY", "form": "10-K", "filed": "2022-10-28", "accepted": "2022-10-27T18:01:14.000Z", "accn": "0000320193-22-000108"},
            {"end": "2023-09-30", "val": 383285000000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "accepted": "2023-11-02T18:08:43.000Z", "accn": "0000320193-23-000106"},
            {"end": "2023-12-30", "val": 119575000000, "fy": 2024, "fp": "Q1", "form": "10-Q", "filed": "2024-02-02", "accepted": "2024-02-01T18:04:25.000Z", "accn": "0000320193-24-000005"},
            {"end": "2024-03-30", "val": 90753000000, "fy": 2024, "fp": "Q2", "form": "10-Q", "filed": "2024-05-03", "accepted": "2024-05-02T18:04:30.000Z", "accn": "0000320193-24-000069"},
            {"end": "2024-06-29", "val": 85777000000, "fy": 2024, "fp": "Q3", "form": "10-Q", "filed": "2024-08-02", "accepted": "2024-08-01T18:05:00.000Z", "accn": "0000320193-24-000123"},
            {"end": "2024-09-28", "val": 94930000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01", "accepted": "2024-10-31T18:09:50.000Z", "accn": "0000320193-24-000158"}
          ]
        }
      },
      "NetIncomeLoss": {
        "label": "Net Income (Loss)",
        "units": {
          "USD": [
            {"end": "2023-09-30", "val": 96995000000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "accn": "0000320193-23-000106"},
            {"end": "2023-12-30", "val": 33916000000, "fy": 2024, "fp": "Q1", "form": "10-Q", "filed": "2024-02-02", "accn": "0000320193-24-000005"},
            {"end": "2024-03-30", "val": 23636000000, "fy": 2024, "fp": "Q2", "form": "10-Q", "filed": "2024-05-03", "accn": "0000320193-24-000069"},
            {"end": "2024-06-29", "val": 21448000000, "fy": 2024, "fp": "Q3", "form": "10-Q", "filed": "2024-08-02", "accn": "0000320193-24-000123"},
            {"end": "2024-09-28", "val": 14736000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01", "accn": "0000320193-24-000158"}
          ]
        }
      },
      "OperatingIncomeLoss": {
        "label": "Operating Income (Loss)",
        "units": {
          "USD": [
            {"end": "2023-09-30", "val": 114301000000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "accn": "0000320193-23-000106"},
            {"end": "2024-09-28", "val": 123216000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01", "accn": "0000320193-24-000158"}
          ]
        }
      },
      "Assets": {
        "label": "Assets",
        "units": {
          "USD": [
            {"end": "2023-09-30", "val": 352755000000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "accn": "0000320193-23-000106"},
            {"end": "2024-09-28", "val": 364980000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01", "accn": "0000320193-24-000158"}
          ]
        }
      },
      "Liabilities": {
        "label": "Liabilities",
        "units": {
          "USD": [
            {"end": "2023-09-30", "val": 290437000000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "accn": "0000320193-23-000106"},
            {"end": "2024-09-28", "val": 308030000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01", "accn": "0000320193-24-000158"}
          ]
        }
      },
      "StockholdersEquity": {
        "label": "Stockholders Equity",
        "units": {
          "USD": [
            {"end": "2023-09-30", "val": 62146000000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "accn": "0000320193-23-000106"},
            {"end": "2024-09-28", "val": 56950000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01", "accn": "0000320193-24-000158"}
          ]
        }
      },
      "CashAndCashEquivalentsAtCarryingValue": {
        "label": "Cash and Cash Equivalents",
        "units": {
          "USD": [
            {"end": "2023-09-30", "val": 29965000000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "accn": "0000320193-23-000106"},
            {"end": "2024-09-28", "val": 29943000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01", "accn": "0000320193-24-000158"}
          ]
        }
      },
      "LongTermDebtNoncurrent": {
        "label": "Long-Term Debt, Noncurrent",
        "units": {
          "USD": [
            {"end": "2023-09-30", "val": 95281000000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "accn": "0000320193-23-000106"},
            {"end": "2024-09-28", "val": 85750000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01", "accn": "0000320193-24-000158"}
          ]
        }
      },
      "LongTermDebt": {
        "label": "Long-Term Debt",
        "units": {
          "USD": [
            {"end": "2023-09-30", "val": 15807000000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "accn": "0000320193-23-000106"},
            {"end": "2024-09-28", "val": 25338000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01", "accn": "0000320193-24-000158"}
          ]
        }
      },
      "NetCashProvidedByUsedInOperatingActivities": {
        "label": "Net Cash Provided by Operating Activities",
        "units": {
          "USD": [
            {"end": "2023-09-30", "val": 110543000000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "accn": "0000320193-23-000106"},
            {"end": "2024-09-28", "val": 118254000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01", "accn": "0000320193-24-000158"}
          ]
        }
      },
      "PaymentsToAcquirePropertyPlantAndEquipment": {
        "label": "Payments to Acquire PP&E (Capex)",
        "units": {
          "USD": [
            {"end": "2023-09-30", "val": 10959000000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "accn": "0000320193-23-000106"},
            {"end": "2024-09-28", "val": 9447000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01", "accn": "0000320193-24-000158"}
          ]
        }
      },
      "InterestExpense": {
        "label": "Interest Expense",
        "units": {
          "USD": [
            {"end": "2023-09-30", "val": 3933000000, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "accn": "0000320193-23-000106"},
            {"end": "2024-09-28", "val": 0, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01", "accn": "0000320193-24-000158"}
          ]
        }
      }
    }
  }
}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/datasources/test_edgar_xbrl_parser.py`:

```python
"""Tests for the pure-function XBRL companyfacts parser (Phase A.3.3)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.common.datasources.edgar_xbrl_parser import (
    parse_companyfacts,
    extract_concept_observations,
    compute_ttm_from_quarters,
)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "CIK0000320193.json"


@pytest.fixture
def blob():
    return json.loads(FIXTURE.read_text())


def test_parse_companyfacts_returns_rows(blob):
    rows = parse_companyfacts(
        blob,
        run_id="r1",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    assert len(rows) > 0


def test_parse_companyfacts_extracts_2023_annual(blob):
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    fy23 = next(r for r in rows if r.fiscal_year == 2023 and r.fiscal_period == "annual")
    assert fy23.form_type == "10-K"
    assert fy23.filing_date == "2023-11-03"
    assert fy23.revenue_ttm == 383285000000.0
    assert fy23.net_income_ttm == 96995000000.0
    assert fy23.ebit_ttm == 114301000000.0
    assert fy23.total_assets == 352755000000.0
    assert fy23.total_liabilities == 290437000000.0
    assert fy23.total_equity == 62146000000.0
    assert fy23.cash_and_equivalents == 29965000000.0
    # total_debt = LongTermDebt + LongTermDebtNoncurrent
    assert fy23.total_debt == 95281000000.0 + 15807000000.0
    assert fy23.operating_cashflow == 110543000000.0
    assert fy23.capex == -10959000000.0  # stored as negative per spec
    # fcf = ocf - abs(capex)
    assert fy23.fcf_ttm == 110543000000.0 - 10959000000.0


def test_parse_companyfacts_extracts_quarterly_rows(blob):
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    quarters = [r for r in rows if r.fiscal_period in {"Q1", "Q2", "Q3"} and r.fiscal_year == 2024]
    assert len(quarters) == 3
    q1 = next(r for r in quarters if r.fiscal_period == "Q1")
    assert q1.form_type == "10-Q"
    assert q1.revenue_ttm == 119575000000.0


def test_parse_companyfacts_computes_margins(blob):
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    fy23 = next(r for r in rows if r.fiscal_year == 2023 and r.fiscal_period == "annual")
    assert fy23.operating_margin is not None
    assert abs(fy23.operating_margin - (114301000000.0 / 383285000000.0)) < 1e-6
    assert fy23.net_profit_margin is not None
    assert abs(fy23.net_profit_margin - (96995000000.0 / 383285000000.0)) < 1e-6


def test_parse_companyfacts_computes_debt_to_equity(blob):
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    fy23 = next(r for r in rows if r.fiscal_year == 2023 and r.fiscal_period == "annual")
    total_debt = 95281000000.0 + 15807000000.0
    assert abs(fy23.total_debt_to_equity - (total_debt / 62146000000.0)) < 1e-6


def test_parse_companyfacts_computes_interest_coverage(blob):
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    fy23 = next(r for r in rows if r.fiscal_year == 2023 and r.fiscal_period == "annual")
    assert fy23.interest_coverage is not None
    assert abs(fy23.interest_coverage - (114301000000.0 / 3933000000.0)) < 1e-3


def test_parse_companyfacts_handles_zero_interest_expense(blob):
    """When InterestExpense == 0, interest_coverage must be None (not inf/NaN)."""
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    fy24 = next(r for r in rows if r.fiscal_year == 2024 and r.fiscal_period == "annual")
    assert fy24.interest_coverage is None


def test_parse_companyfacts_carries_cik(blob):
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    for r in rows:
        assert r.cik == "0000320193"


def test_extract_concept_observations_returns_empty_for_missing(blob):
    obs = extract_concept_observations(blob, "us-gaap", "NotARealConcept")
    assert obs == []


def test_extract_concept_observations_returns_usd_list(blob):
    obs = extract_concept_observations(blob, "us-gaap", "Revenues")
    assert len(obs) == 6
    assert any(o["fy"] == 2023 and o["fp"] == "FY" for o in obs)


def test_compute_ttm_from_quarters_sums_latest_4():
    quarters = [
        {"end": "2023-12-30", "val": 100},
        {"end": "2024-03-30", "val": 90},
        {"end": "2024-06-29", "val": 80},
        {"end": "2024-09-28", "val": 95},
    ]
    assert compute_ttm_from_quarters(quarters) == 365


def test_compute_ttm_from_quarters_returns_none_when_fewer_than_4():
    assert compute_ttm_from_quarters([{"end": "2024-01-01", "val": 100}]) is None
    assert compute_ttm_from_quarters([]) is None


def test_parse_companyfacts_handles_empty_facts():
    blob = {"cik": 320193, "entityName": "Empty Co", "facts": {"us-gaap": {}}}
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="EMP",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    assert rows == []


def test_parse_companyfacts_skips_periods_with_no_revenue(blob):
    """If a fiscal_period has revenue but lacks the other concepts, the row
    should still be emitted with revenue populated and the other fields
    gracefully None. This guards against over-strict filtering."""
    rows = parse_companyfacts(
        blob, run_id="r1", ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    fy22 = [r for r in rows if r.fiscal_year == 2022 and r.fiscal_period == "annual"]
    assert len(fy22) == 1
    assert fy22[0].revenue_ttm == 394328000000.0
    assert fy22[0].net_income_ttm is None  # gracefully None
```

- [ ] **Step 3: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_xbrl_parser.py -v
```

Expected: `ImportError` on `edgar_xbrl_parser`.

- [ ] **Step 4: Create the parser module**

Create `src/common/datasources/edgar_xbrl_parser.py`:

```python
"""Pure-function XBRL companyfacts parser.

Input: the JSON blob returned by:
  https://data.sec.gov/api/xbrl/companyfacts/CIK{padded}.json

Output: a list of `RawEdgarFundamentalsRow` covering every distinct
(fiscal_year, fiscal_period) combination that has at least one of the
canonical concepts populated. No HTTP, no DB — fully unit-testable.

Mapping (us-gaap concept -> our canonical field):
  Revenues OR RevenueFromContractWithCustomerExcludingAssessedTax -> revenue_ttm
  NetIncomeLoss                                                   -> net_income_ttm
  OperatingIncomeLoss                                             -> ebit_ttm proxy
  Assets                                                          -> total_assets
  Liabilities                                                     -> total_liabilities
  StockholdersEquity                                              -> total_equity
  CashAndCashEquivalentsAtCarryingValue                           -> cash_and_equivalents
  LongTermDebt + LongTermDebtNoncurrent                           -> total_debt (sum if both present)
  NetCashProvidedByUsedInOperatingActivities                      -> operating_cashflow
  PaymentsToAcquirePropertyPlantAndEquipment                      -> capex (stored as -abs)
  InterestExpense                                                 -> used for interest_coverage

Derived:
  fcf_ttm              = operating_cashflow - abs(capex)
  operating_margin     = ebit_ttm / revenue_ttm        (when both present and revenue != 0)
  net_profit_margin    = net_income_ttm / revenue_ttm  (when both present and revenue != 0)
  total_debt_to_equity = total_debt / total_equity     (when total_equity not in {None, 0})
  interest_coverage    = ebit_ttm / interest_expense   (when interest_expense not in {None, 0})

Fiscal-period normalization:
  source `fp` field is one of {"FY", "Q1", "Q2", "Q3", "Q4"}.
  We map "FY" -> "annual" to match RawEdgarFundamentalsRow's Literal.
"""
from __future__ import annotations

from typing import Iterable, Optional

from src.common.schemas import RawEdgarFundamentalsRow


# Concept tag preferences (first non-empty wins)
_REVENUE_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")


def extract_concept_observations(
    blob: dict, taxonomy: str, concept: str, unit: str = "USD",
) -> list[dict]:
    """Return the list of observations under facts[taxonomy][concept][units][unit].

    Returns [] if any node in the path is missing.
    """
    try:
        return list(
            blob["facts"][taxonomy][concept]["units"][unit]
        )
    except (KeyError, TypeError):
        return []


def _first_concept_observations(
    blob: dict, taxonomy: str, concepts: Iterable[str],
) -> list[dict]:
    """Return observations for the first concept in `concepts` that has any data."""
    for c in concepts:
        obs = extract_concept_observations(blob, taxonomy, c)
        if obs:
            return obs
    return []


def _normalize_period(fp: str) -> Optional[str]:
    if fp == "FY":
        return "annual"
    if fp in {"Q1", "Q2", "Q3", "Q4"}:
        return fp
    return None


def _normalize_form(form: str) -> Optional[str]:
    if form == "10-K":
        return "10-K"
    if form == "10-Q":
        return "10-Q"
    # 10-K/A and 10-Q/A (amendments) map to their base form
    if form == "10-K/A":
        return "10-K"
    if form == "10-Q/A":
        return "10-Q"
    return None


def _index_by_period(observations: list[dict]) -> dict[tuple[int, str], dict]:
    """Index a list of observations by (fy, normalized_period).

    When duplicate observations exist for the same (fy, fp), the LATEST `filed`
    date wins (we want the most recently filed value as our first-observed
    canonical, since the parser is called fresh from live SEC data each run).
    Note this is parser-internal; the DB-level INSERT OR IGNORE then preserves
    the first run's value across runs.
    """
    out: dict[tuple[int, str], dict] = {}
    for o in observations:
        fy = o.get("fy")
        fp_norm = _normalize_period(str(o.get("fp", "")))
        if fy is None or fp_norm is None:
            continue
        key = (int(fy), fp_norm)
        prev = out.get(key)
        if prev is None or str(o.get("filed", "")) > str(prev.get("filed", "")):
            out[key] = o
    return out


def compute_ttm_from_quarters(quarters: list[dict]) -> Optional[float]:
    """Sum the 4 most recent quarterly observations (by `end` date).

    Returns None if fewer than 4 quarters are available.
    """
    if not quarters or len(quarters) < 4:
        return None
    sorted_q = sorted(quarters, key=lambda o: str(o.get("end", "")))
    latest_4 = sorted_q[-4:]
    try:
        return float(sum(float(o["val"]) for o in latest_4))
    except (KeyError, TypeError, ValueError):
        return None


def _safe_div(num: Optional[float], denom: Optional[float]) -> Optional[float]:
    if num is None or denom is None:
        return None
    if denom == 0:
        return None
    return num / denom


def _clamp_margin(x: Optional[float]) -> Optional[float]:
    """Margins are stored with Pydantic range [-1.0, 1.0]; clip extremes that
    can occur for distressed firms where net_income > revenue (e.g. one-time
    gains) so we don't fail validation. Out-of-range becomes None — the row
    still persists with the rest of its fields."""
    if x is None:
        return None
    if x < -1.0 or x > 1.0:
        return None
    return x


def _as_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sum_if_any(*vals: Optional[float]) -> Optional[float]:
    present = [v for v in vals if v is not None]
    if not present:
        return None
    return float(sum(present))


def parse_companyfacts(
    blob: dict,
    run_id: str,
    ticker: str,
    scrape_timestamp: str,
) -> list[RawEdgarFundamentalsRow]:
    """Parse a companyfacts JSON blob into a list of RawEdgarFundamentalsRow.

    Emits one row per (fiscal_year, fiscal_period) that has at least a
    revenue observation. All other fields are best-effort and become None
    if the underlying us-gaap concept is missing for that period.
    """
    cik_int = blob.get("cik")
    if cik_int is None:
        return []
    cik = str(cik_int).zfill(10)

    revenue_idx = _index_by_period(_first_concept_observations(blob, "us-gaap", _REVENUE_CONCEPTS))
    ni_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "NetIncomeLoss"))
    oi_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "OperatingIncomeLoss"))
    assets_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "Assets"))
    liab_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "Liabilities"))
    equity_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "StockholdersEquity"))
    cash_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "CashAndCashEquivalentsAtCarryingValue"))
    ltd_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "LongTermDebt"))
    ltd_nc_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "LongTermDebtNoncurrent"))
    ocf_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "NetCashProvidedByUsedInOperatingActivities"))
    capex_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"))
    int_exp_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "InterestExpense"))

    rows: list[RawEdgarFundamentalsRow] = []
    for (fy, fp_norm), rev_obs in revenue_idx.items():
        form_norm = _normalize_form(str(rev_obs.get("form", "")))
        if form_norm is None:
            continue
        filing_date = str(rev_obs.get("filed", ""))
        if not filing_date:
            continue
        accepted_at = rev_obs.get("accepted")

        revenue = _as_float(rev_obs.get("val"))
        ni = _as_float((ni_idx.get((fy, fp_norm)) or {}).get("val"))
        oi = _as_float((oi_idx.get((fy, fp_norm)) or {}).get("val"))

        total_assets = _as_float((assets_idx.get((fy, fp_norm)) or {}).get("val"))
        total_liab = _as_float((liab_idx.get((fy, fp_norm)) or {}).get("val"))
        total_equity = _as_float((equity_idx.get((fy, fp_norm)) or {}).get("val"))
        cash = _as_float((cash_idx.get((fy, fp_norm)) or {}).get("val"))

        ltd = _as_float((ltd_idx.get((fy, fp_norm)) or {}).get("val"))
        ltd_nc = _as_float((ltd_nc_idx.get((fy, fp_norm)) or {}).get("val"))
        total_debt = _sum_if_any(ltd, ltd_nc)

        ocf = _as_float((ocf_idx.get((fy, fp_norm)) or {}).get("val"))
        capex_raw = _as_float((capex_idx.get((fy, fp_norm)) or {}).get("val"))
        # Spec convention: capex stored as negative; XBRL value is typically positive (cash outflow)
        capex = -abs(capex_raw) if capex_raw is not None else None
        fcf = (ocf - abs(capex)) if (ocf is not None and capex is not None) else None

        int_exp = _as_float((int_exp_idx.get((fy, fp_norm)) or {}).get("val"))

        op_margin = _clamp_margin(_safe_div(oi, revenue))
        np_margin = _clamp_margin(_safe_div(ni, revenue))
        d2e = _safe_div(total_debt, total_equity)
        int_cov = _safe_div(oi, int_exp)  # ebit / interest_expense; None if int_exp in (None, 0)

        try:
            row = RawEdgarFundamentalsRow(
                run_id=run_id,
                ticker=ticker,
                cik=cik,
                fiscal_year=fy,
                fiscal_period=fp_norm,
                filing_date=filing_date,
                accepted_at=accepted_at,
                form_type=form_norm,
                revenue_ttm=revenue,
                ebit_ttm=oi,
                net_income_ttm=ni,
                total_assets=total_assets,
                total_liabilities=total_liab,
                total_equity=total_equity,
                cash_and_equivalents=cash,
                total_debt=total_debt,
                operating_cashflow=ocf,
                capex=capex,
                fcf_ttm=fcf,
                operating_margin=op_margin,
                net_profit_margin=np_margin,
                total_debt_to_equity=d2e,
                interest_coverage=int_cov,
                scrape_timestamp=scrape_timestamp,
            )
        except Exception:
            continue
        rows.append(row)
    return rows
```

- [ ] **Step 5: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_xbrl_parser.py -v
```

Expected: All 13 tests PASS.

- [ ] **Step 6: Commit**

```
git add src/common/datasources/edgar_xbrl_parser.py tests/datasources/test_edgar_xbrl_parser.py tests/fixtures/edgar/CIK0000320193.json
git commit -m "feat(datasources): edgar_xbrl_parser - pure-function companyfacts -> RawEdgarFundamentalsRow"
```

---

## Task 5: `EdgarSource.fetch_fundamentals_for_ticker` real implementation

**Files:**
- Modify: `src/common/datasources/edgar_source.py`
- Create: `tests/datasources/test_edgar_fetch.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/datasources/test_edgar_fetch.py`:

```python
"""Tests for EdgarSource fetch methods (Phase A.3.3)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.edgar_source import EdgarSource


FIXTURE_CF = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "CIK0000320193.json"
FIXTURE_CT = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "company_tickers.json"


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v5()
    return mgr


@pytest.fixture
def cf_blob():
    return json.loads(FIXTURE_CF.read_text())


@pytest.fixture
def ct_blob():
    return json.loads(FIXTURE_CT.read_text())


def _mock_sec_get(mocker, ct_blob, cf_blob):
    """Wire requests.get to return company_tickers.json or the companyfacts
    blob based on URL substring."""
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.ok = True
        resp.status_code = 200
        if "company_tickers" in url:
            resp.json.return_value = ct_blob
        elif "companyfacts" in url:
            resp.json.return_value = cf_blob
        else:
            resp.ok = False
            resp.status_code = 404
        return resp
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        side_effect=side_effect,
    )
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )


def test_fetch_fundamentals_writes_rows_to_db(db, mocker, ct_blob, cf_blob):
    _mock_sec_get(mocker, ct_blob, cf_blob)
    src = EdgarSource()
    n = src.fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT fiscal_year, fiscal_period FROM raw_edgar_fundamentals "
            "WHERE ticker='AAPL' ORDER BY fiscal_year, fiscal_period"
        ).fetchall()
    assert (2023, "annual") in rows
    assert (2024, "annual") in rows
    assert (2024, "Q1") in rows


def test_fetch_fundamentals_updates_watermark_on_success(db, mocker, ct_blob, cf_blob):
    _mock_sec_get(mocker, ct_blob, cf_blob)
    EdgarSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    w = db.get_watermark("edgar", "AAPL", "xbrl_fundamentals")
    assert w is not None
    # last_observation_date is the most recent filing_date in the blob
    assert w["last_observation_date"] == "2024-11-01"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_fetch_fundamentals_skips_when_already_fetched_today(db, mocker, ct_blob, cf_blob):
    """Re-running on a ticker whose watermark was last_fetched_at today should
    be a no-op (returns 0, does not call companyfacts)."""
    # Seed CIK map + watermark as if a previous fetch already happened today
    db.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."}
    ])
    import datetime as _dt
    today = _dt.date.today().isoformat()
    db.upsert_watermark(
        source="sec", ticker="*", field="ticker_cik_map",
        last_observation_date=today, success=True,
    )
    db.upsert_watermark(
        source="edgar", ticker="AAPL", field="xbrl_fundamentals",
        last_observation_date="2024-11-01", success=True,
    )
    # Do NOT mock companyfacts — assert it is never called
    get_mock = mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
    )
    n = EdgarSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    assert n == 0
    get_mock.assert_not_called()


def test_fetch_fundamentals_records_failure_on_exception(db, mocker, ct_blob):
    # CIK lookup succeeds
    db.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."}
    ])
    import datetime as _dt
    today = _dt.date.today().isoformat()
    db.upsert_watermark(
        source="sec", ticker="*", field="ticker_cik_map",
        last_observation_date=today, success=True,
    )
    # companyfacts blows up
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=RuntimeError("sec 503"),
    )
    n = EdgarSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    assert n == 0
    w = db.get_watermark("edgar", "AAPL", "xbrl_fundamentals")
    assert w is not None
    assert w["error_count"] >= 1
    assert "sec 503" in (w["last_error_message"] or "")


def test_fetch_fundamentals_unknown_ticker_records_failure(db, mocker, ct_blob, cf_blob):
    _mock_sec_get(mocker, ct_blob, cf_blob)
    src = EdgarSource()
    n = src.fetch_fundamentals_for_ticker("NOTREAL", run_id="r1", db=db)
    assert n == 0
    w = db.get_watermark("edgar", "NOTREAL", "xbrl_fundamentals")
    assert w is not None
    assert w["error_count"] >= 1


def test_fetch_fundamentals_uses_sec_user_agent(db, mocker, ct_blob, cf_blob):
    captured_calls = []
    def side_effect(url, **kwargs):
        captured_calls.append((url, kwargs.get("headers", {})))
        resp = mocker.MagicMock()
        resp.ok = True
        resp.status_code = 200
        if "company_tickers" in url:
            resp.json.return_value = ct_blob
        elif "companyfacts" in url:
            resp.json.return_value = cf_blob
        return resp
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        side_effect=side_effect,
    )
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )
    EdgarSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    cf_call = next(c for c in captured_calls if "companyfacts" in c[0])
    headers = cf_call[1]
    assert "User-Agent" in headers
    assert "@" in headers["User-Agent"]


def test_fetch_fundamentals_calls_correct_companyfacts_url(db, mocker, ct_blob, cf_blob):
    captured_urls = []
    def side_effect(url, **kwargs):
        captured_urls.append(url)
        resp = mocker.MagicMock()
        resp.ok = True
        resp.status_code = 200
        if "company_tickers" in url:
            resp.json.return_value = ct_blob
        elif "companyfacts" in url:
            resp.json.return_value = cf_blob
        return resp
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        side_effect=side_effect,
    )
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )
    EdgarSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    cf_urls = [u for u in captured_urls if "companyfacts" in u]
    assert len(cf_urls) == 1
    assert "CIK0000320193.json" in cf_urls[0]


def test_fetch_fundamentals_insert_or_ignore_protects_against_dup(db, mocker, ct_blob, cf_blob):
    _mock_sec_get(mocker, ct_blob, cf_blob)
    src = EdgarSource()
    n1 = src.fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    # Force a re-fetch by clearing the same-day short-circuit (set last_fetched_at to yesterday)
    import datetime as _dt
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat() + "T00:00:00"
    with sqlite3.connect(db.db_path) as c:
        c.execute(
            "UPDATE fetch_watermarks SET last_fetched_at = ? WHERE source='edgar' AND ticker='AAPL'",
            (yesterday,),
        )
        c.commit()
    n2 = src.fetch_fundamentals_for_ticker("AAPL", run_id="r1", db=db)
    with sqlite3.connect(db.db_path) as c:
        total = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_fundamentals WHERE run_id='r1' AND ticker='AAPL'"
        ).fetchone()[0]
    assert total == n1, "INSERT OR IGNORE should prevent duplicates on the same PK"
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_fetch.py -v
```

Expected: `NotImplementedError` from BaseDataSource default (since `fetch_fundamentals_for_ticker` is not overridden on EdgarSource yet).

- [ ] **Step 3: Implement `_sec_get_with_retry` + `_resolve_cik` + `fetch_fundamentals_for_ticker`**

Open `src/common/datasources/edgar_source.py`. **Replace its entire contents** with:

```python
"""EdgarSource — adapter for SEC EDGAR.

SEC EDGAR requires identifying contact info in every request per
https://www.sec.gov/os/accessing-edgar-data. The User-Agent format is:
"Application Name AdminEmail@example.com".

A.1 deliverable: health_check via the EDGAR submissions endpoint.
A.3.3 deliverable: fetch_fundamentals_for_ticker + fetch_universe pulling
the XBRL companyfacts JSON, parsing to RawEdgarFundamentalsRow, and
persisting to raw_edgar_fundamentals via INSERT OR IGNORE. Watermark per
(edgar, ticker, xbrl_fundamentals) tracks the latest filing date observed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import time

import pandas as pd
import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_DEFAULT_USER_AGENT = "Trade Identifier research@example.com"
_PROBE_URL = "https://data.sec.gov/submissions/CIK0000320193.json"  # Apple
_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


class _SecHttpError(RuntimeError):
    """Wrapper for retry-eligible HTTP errors from SEC endpoints."""


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
    retry=retry_if_exception_type(_SecHttpError),
)
def _sec_get_with_retry(url: str, headers: dict) -> requests.Response:
    """GET against an SEC endpoint with tenacity-backed retry on 429/503.

    SEC fair-access policy: <=10 req/sec. Retries with exponential backoff
    (0.5s -> 8s capped). Other HTTP errors (4xx) are not retried — they
    indicate a hard mis-config (e.g., missing User-Agent) and should fail
    fast so we don't waste budget.
    """
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code in (429, 503):
        raise _SecHttpError(f"transient HTTP {resp.status_code} for {url}")
    return resp


class EdgarSource(BaseDataSource):
    name = "edgar"
    cadence = "weekly"
    provides = {
        "cik", "company_name", "exchange",
        "revenue_ttm", "ebit_ttm", "net_income_ttm",
        "total_assets", "total_debt_to_equity",
        "operating_margin", "net_profit_margin",
        "fcf_ttm", "cash_and_equivalents", "total_debt",
        "interest_coverage",
    }

    def _headers(self) -> dict[str, str]:
        ua = os.environ.get("SEC_EDGAR_USER_AGENT", _DEFAULT_USER_AGENT)
        return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            resp = requests.get(_PROBE_URL, headers=self._headers(), timeout=10)
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
            message=f"HTTP 200 ({len(resp.content)} bytes)",
            response_time_ms=(time.monotonic() - started) * 1000,
        )

    # ------------------------------------------------------------------
    # Phase A.3.3: XBRL fundamentals
    # ------------------------------------------------------------------

    def _resolve_cik(self, ticker: str, db) -> str:
        """Resolve ticker to 10-digit padded CIK using SecCikLookup."""
        from src.common.datasources.sec_cik_lookup import SecCikLookup
        return SecCikLookup(db=db).resolve(ticker)

    def _latest_filing_date(self, blob: dict) -> str | None:
        """Return the most recent `filed` date across all us-gaap observations.

        Used to drive the (edgar, ticker, xbrl_fundamentals) watermark — once
        we've seen filing X, we don't need to re-pull until filing X+1 lands.
        """
        latest = None
        facts = (blob.get("facts") or {}).get("us-gaap") or {}
        for _concept, body in facts.items():
            for _unit, obs_list in (body.get("units") or {}).items():
                for o in obs_list:
                    filed = o.get("filed")
                    if filed and (latest is None or filed > latest):
                        latest = filed
        return latest

    def fetch_fundamentals_for_ticker(
        self,
        ticker: str,
        run_id: str,
        db=None,
    ) -> int:
        """Pull XBRL companyfacts for ticker, parse, and persist.

        Returns the number of (period) rows actually inserted into
        raw_edgar_fundamentals (post-INSERT-OR-IGNORE).

        Watermark behavior:
          - (edgar, ticker, xbrl_fundamentals) tracks last_observation_date =
            most recent SEC `filed` date we've parsed for this ticker.
          - Same-day short-circuit: if a successful fetch already ran today
            (last_fetched_at starts with today's date), return 0 without HTTP.
            This protects against duplicate same-day calls from the registry
            without losing the ability to discover newly-filed 10-Q/10-K once
            the day rolls over.
        """
        if db is None:
            raise ValueError("db is required")

        # Short-circuit: if we've fetched today and a watermark is set, skip.
        w = db.get_watermark(self.name, ticker, "xbrl_fundamentals")
        if w is not None and w.get("last_observation_date"):
            try:
                last_fetched = (w.get("last_fetched_at") or "")[:10]  # YYYY-MM-DD
                today_iso = datetime.now(timezone.utc).date().isoformat()
                if last_fetched == today_iso:
                    return 0
            except Exception:
                pass

        # Resolve CIK (this can fail for tickers not in SEC map)
        try:
            cik = self._resolve_cik(ticker, db)
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="xbrl_fundamentals",
                last_observation_date=None, db=db, success=False,
                error_message=f"cik_resolve: {type(exc).__name__}: {exc}",
            )
            return 0

        # Pull companyfacts JSON
        url = _COMPANYFACTS_URL.format(cik=cik)
        try:
            resp = _sec_get_with_retry(url, headers=self._headers())
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="xbrl_fundamentals",
                last_observation_date=None, db=db, success=False,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            return 0

        if not resp.ok:
            self.update_watermark(
                ticker=ticker, field="xbrl_fundamentals",
                last_observation_date=None, db=db, success=False,
                error_message=f"HTTP {resp.status_code}",
            )
            return 0

        try:
            blob = resp.json()
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="xbrl_fundamentals",
                last_observation_date=None, db=db, success=False,
                error_message=f"json_decode: {type(exc).__name__}: {exc}",
            )
            return 0

        # Parse to structured rows
        from src.common.datasources.edgar_xbrl_parser import parse_companyfacts
        scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rows = parse_companyfacts(
            blob, run_id=run_id, ticker=ticker, scrape_timestamp=scrape_ts,
        )
        if not rows:
            self.update_watermark(
                ticker=ticker, field="xbrl_fundamentals",
                last_observation_date=None, db=db, success=False,
                error_message="parser returned 0 rows",
            )
            return 0

        # Persist with INSERT OR IGNORE (Principle 5: no overwrite)
        import sqlite3
        with sqlite3.connect(db.db_path) as c:
            n_before = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_fundamentals WHERE run_id=? AND ticker=?",
                (run_id, ticker),
            ).fetchone()[0]
        db.insert_raw_edgar_fundamentals(rows)
        with sqlite3.connect(db.db_path) as c:
            n_after = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_fundamentals WHERE run_id=? AND ticker=?",
                (run_id, ticker),
            ).fetchone()[0]
        n_inserted = n_after - n_before

        # Update watermark with the latest filing_date seen
        latest_filed = self._latest_filing_date(blob)
        import datetime as _dt
        last_obs = _dt.date.fromisoformat(latest_filed) if latest_filed else None
        self.update_watermark(
            ticker=ticker, field="xbrl_fundamentals",
            last_observation_date=last_obs, db=db, success=True,
        )
        return n_inserted
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_fetch.py -v
```

Expected: All 8 tests PASS.

- [ ] **Step 5: Verify health_check still passes (no regression on A.1)**

```
./venv/Scripts/python.exe -m pytest tests/datasources -v 2>&1 | tail -5
```

Expected: All A.1 + A.3.2 + A.3.3 datasource tests pass.

- [ ] **Step 6: Commit**

```
git add src/common/datasources/edgar_source.py tests/datasources/test_edgar_fetch.py
git commit -m "feat(datasources): EdgarSource.fetch_fundamentals_for_ticker - XBRL companyfacts -> raw_edgar_fundamentals (watermarked, INSERT OR IGNORE, tenacity retry)"
```

---

## Task 6: `EdgarSource.fetch_universe` batch over ticker list

**Files:**
- Modify: `src/common/datasources/edgar_source.py`
- Modify: `tests/datasources/test_edgar_fetch.py` (append)

- [ ] **Step 1: Append failing tests to `tests/datasources/test_edgar_fetch.py`**

```python


# Task 6: fetch_universe batch
def test_fetch_universe_returns_dataframe_aggregating_all_tickers(db, mocker, ct_blob, cf_blob):
    _mock_sec_get(mocker, ct_blob, cf_blob)
    df = EdgarSource().fetch_universe(
        run_id="r1", ticker_list=["AAPL", "MSFT"], db=db,
    )
    import pandas as pd
    assert isinstance(df, pd.DataFrame)
    # 2 tickers; both resolve to the same companyfacts mock so we get rows
    # for each ticker.
    assert "ticker" in df.columns
    assert sorted(df["ticker"].unique().tolist()) == ["AAPL", "MSFT"]


def test_fetch_universe_skips_bad_tickers(db, mocker, ct_blob, cf_blob):
    _mock_sec_get(mocker, ct_blob, cf_blob)
    df = EdgarSource().fetch_universe(
        run_id="r1", ticker_list=["AAPL", "FAKE", "MSFT"], db=db,
    )
    assert "FAKE" not in df["ticker"].unique().tolist()
    assert "AAPL" in df["ticker"].unique().tolist()
    assert "MSFT" in df["ticker"].unique().tolist()


def test_fetch_universe_empty_list_returns_empty_dataframe(db, mocker):
    mocker.patch("src.common.datasources.edgar_source.requests.get")
    df = EdgarSource().fetch_universe(run_id="r1", ticker_list=[], db=db)
    assert df.empty


def test_fetch_universe_continues_on_per_ticker_exception(db, mocker, cf_blob):
    """If one ticker's companyfacts call raises, the batch continues for others."""
    db.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."},
        {"ticker": "MSFT", "cik": "0000789019", "company_name": "Microsoft Corp"},
    ])
    import datetime as _dt
    today = _dt.date.today().isoformat()
    db.upsert_watermark(
        source="sec", ticker="*", field="ticker_cik_map",
        last_observation_date=today, success=True,
    )

    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        if "CIK0000320193" in url:
            resp.ok = True
            resp.status_code = 200
            resp.json.return_value = cf_blob
        elif "CIK0000789019" in url:
            raise RuntimeError("MSFT blew up")
        else:
            resp.ok = False
            resp.status_code = 404
        return resp

    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )
    df = EdgarSource().fetch_universe(
        run_id="r1", ticker_list=["AAPL", "MSFT"], db=db,
    )
    tickers = df["ticker"].unique().tolist()
    assert "AAPL" in tickers
    assert "MSFT" not in tickers
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_fetch.py -v
```

Expected: `NotImplementedError` (BaseDataSource `fetch_universe` default) or `TypeError` on the `ticker_list` kwarg.

- [ ] **Step 3: Implement `fetch_universe` on `EdgarSource`**

In `src/common/datasources/edgar_source.py`, **inside** the `EdgarSource` class, AFTER `fetch_fundamentals_for_ticker`, append:

```python

    def fetch_universe(
        self,
        run_id: str,
        ticker_list: list[str] | None = None,
        db=None,
    ) -> pd.DataFrame:
        """Batch-pull XBRL fundamentals for every ticker in ticker_list.

        EDGAR has no "scan all stocks" endpoint — companyfacts is per-CIK,
        so a ticker list (typically from FinvizSource.fetch_universe) is
        required. Each ticker is fetched independently via
        fetch_fundamentals_for_ticker; per-ticker failures are logged via
        the watermark and do NOT abort the batch.

        Returns a DataFrame with one row per (ticker, fiscal_year,
        fiscal_period) row visible in raw_edgar_fundamentals for this
        run_id at completion.
        """
        if db is None:
            raise ValueError("db is required")
        if not ticker_list:
            return pd.DataFrame()

        for ticker in ticker_list:
            try:
                self.fetch_fundamentals_for_ticker(ticker, run_id=run_id, db=db)
            except Exception:
                # update_watermark already called inside fetch_fundamentals_for_ticker
                # on the failure paths; broad except here is a final safety net.
                continue

        import sqlite3
        with sqlite3.connect(db.db_path) as c:
            cur = c.execute(
                """
                SELECT run_id, ticker, cik, fiscal_year, fiscal_period,
                       filing_date, form_type,
                       revenue_ttm, ebit_ttm, net_income_ttm,
                       total_assets, total_liabilities, total_equity,
                       cash_and_equivalents, total_debt,
                       operating_cashflow, capex, fcf_ttm,
                       operating_margin, net_profit_margin,
                       total_debt_to_equity, interest_coverage,
                       scrape_timestamp
                FROM raw_edgar_fundamentals
                WHERE run_id = ? AND ticker IN ({})
                """.format(",".join("?" * len(ticker_list))),
                (run_id, *[t.upper() for t in ticker_list]),
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, columns=cols)
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_fetch.py -v
```

Expected: All 12 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/edgar_source.py tests/datasources/test_edgar_fetch.py
git commit -m "feat(datasources): EdgarSource.fetch_universe - batch XBRL pulls with per-ticker skip-on-failure"
```

---

## Task 7: Integration test

**Files:**
- Create: `tests/datasources/test_integration_a3_3.py`

- [ ] **Step 1: Write the integration test**

Create `tests/datasources/test_integration_a3_3.py`:

```python
"""End-to-end orchestration test for A.3.3: ticker list -> CIK lookup -> XBRL fetch
-> parser -> raw_edgar_fundamentals persistence -> watermark advance -> idempotent re-run.

Unit-level integration with mocked HTTP. A separate @pytest.mark.integration
test against the real SEC endpoint is deferred to A.5.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.edgar_source import EdgarSource


FIXTURE_CF = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "CIK0000320193.json"
FIXTURE_CT = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "company_tickers.json"


def _wire_mocks(mocker, ct_blob, cf_blob):
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.ok = True
        resp.status_code = 200
        if "company_tickers" in url:
            resp.json.return_value = ct_blob
        elif "companyfacts" in url:
            resp.json.return_value = cf_blob
        return resp
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        side_effect=side_effect,
    )
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )


def test_edgar_xbrl_full_flow_with_watermark_idempotency(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v5()

    ct_blob = json.loads(FIXTURE_CT.read_text())
    cf_blob = json.loads(FIXTURE_CF.read_text())
    _wire_mocks(mocker, ct_blob, cf_blob)

    edgar = EdgarSource()

    # First pass: fetch_universe over 2 tickers
    df1 = edgar.fetch_universe(
        run_id="run-1", ticker_list=["AAPL", "MSFT"], db=db,
    )
    assert not df1.empty
    assert sorted(df1["ticker"].unique().tolist()) == ["AAPL", "MSFT"]

    # Both tickers have a watermark
    for t in ["AAPL", "MSFT"]:
        w = db.get_watermark("edgar", t, "xbrl_fundamentals")
        assert w is not None
        assert w["last_observation_date"] == "2024-11-01"
        assert w["fetch_count"] == 1
        assert w["error_count"] == 0

    # Count rows in raw_edgar_fundamentals before re-run
    with sqlite3.connect(db_path) as c:
        n_before = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_fundamentals"
        ).fetchone()[0]
    assert n_before > 0

    # Second pass: same run_id, same tickers. The fetch is skipped (watermark
    # last_fetched_at == today), no new HTTP calls to companyfacts, and the
    # row count is unchanged (also guarded by INSERT OR IGNORE).
    df2 = edgar.fetch_universe(
        run_id="run-1", ticker_list=["AAPL", "MSFT"], db=db,
    )
    with sqlite3.connect(db_path) as c:
        n_after = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_fundamentals"
        ).fetchone()[0]
    assert n_after == n_before

    # Verify the parsed fy2023 annual matches expected values
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            """
            SELECT revenue_ttm, net_income_ttm, ebit_ttm, total_assets,
                   total_equity, operating_margin, net_profit_margin,
                   total_debt_to_equity, interest_coverage
            FROM raw_edgar_fundamentals
            WHERE ticker='AAPL' AND fiscal_year=2023 AND fiscal_period='annual'
            """
        ).fetchone()
    assert row is not None
    revenue, ni, oi, assets, equity, op_m, np_m, d2e, ic = row
    assert revenue == 383285000000.0
    assert ni == 96995000000.0
    assert oi == 114301000000.0
    assert assets == 352755000000.0
    assert equity == 62146000000.0
    assert op_m is not None
    assert abs(op_m - (oi / revenue)) < 1e-6
    assert np_m is not None
    assert abs(np_m - (ni / revenue)) < 1e-6
    assert d2e is not None
    assert d2e > 0
    assert ic is not None
    assert ic > 0


def test_edgar_xbrl_unknown_ticker_logged_as_error_does_not_break_batch(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v5()

    ct_blob = json.loads(FIXTURE_CT.read_text())
    cf_blob = json.loads(FIXTURE_CF.read_text())
    _wire_mocks(mocker, ct_blob, cf_blob)

    df = EdgarSource().fetch_universe(
        run_id="run-1", ticker_list=["AAPL", "NOTREAL", "GOOGL"], db=db,
    )
    assert "AAPL" in df["ticker"].unique().tolist()
    assert "GOOGL" in df["ticker"].unique().tolist()

    # NOTREAL has a failure watermark
    w = db.get_watermark("edgar", "NOTREAL", "xbrl_fundamentals")
    assert w is not None
    assert w["error_count"] >= 1
```

- [ ] **Step 2: Run the integration test**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_integration_a3_3.py -v
```

Expected: 2 PASS.

- [ ] **Step 3: Run the full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" -v 2>&1 | tail -5
```

Expected: ~225 passed (177 baseline + 7 schema + 14 database + 8 cik_lookup + 13 parser + 12 fetch + 2 integration_a3_3 — counts approximate).

- [ ] **Step 4: Commit**

```
git add tests/datasources/test_integration_a3_3.py
git commit -m "test: A.3.3 integration acceptance - end-to-end XBRL pull + watermark idempotency + INSERT OR IGNORE"
```

---

## Task 8: Update the build plan

**Files:**
- Modify: the build plan

- [ ] **Step 1: Locate the A.3 row in §5.1.0**

Grep the build plan for `**A.3.1 + A.3.2 shipped 2026-05-21**` to find the line.

- [ ] **Step 2: Update the A.3 row's shipped marker**

Use `Edit` to replace the substring `**A.3.1 + A.3.2 shipped 2026-05-21**` with `**A.3.1 + A.3.2 + A.3.3 shipped 2026-05-21**`, and update the plan-link parenthetical to add the A.3.3 plan file. Concretely, find this fragment:

```
**A.3.1 + A.3.2 shipped 2026-05-21** (plans: [A.3.1](docs/claude-code/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/claude-code/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md)): schema v4
```

Replace with:

```
**A.3.1 + A.3.2 + A.3.3 shipped 2026-05-21** (plans: [A.3.1](docs/claude-code/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/claude-code/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md), [A.3.3](docs/claude-code/plans/2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md)): schema v5
```

Then in the same sentence, append after the existing `+ raw_yahoo source-native table`:

```
 + EdgarSource.fetch_fundamentals_for_ticker + EdgarSource.fetch_universe + raw_edgar_fundamentals structured XBRL table + sec_ticker_cik_map ticker resolver + edgar_xbrl_parser pure-function module + tenacity retry on SEC 429/503
```

- [ ] **Step 3: Commit**

```
git add <build-plan>
git commit -m "docs: mark A.3.3 shipped - EDGAR XBRL fundamentals pipeline"
```

---

## Phase A.3.3 — Definition of Done

- [ ] `./venv/Scripts/python.exe -m pytest -m "not integration"` shows ~225 tests passing
- [ ] `migrate_to_v5()` produces `schema_version == 5`
- [ ] `raw_edgar_fundamentals` table exists with PK `(run_id, ticker, fiscal_period, fiscal_year)`
- [ ] `sec_ticker_cik_map` table exists with daily-refresh watermark
- [ ] `insert_raw_edgar_fundamentals()` uses INSERT OR IGNORE (restatements do NOT overwrite)
- [ ] `SecCikLookup.resolve()` returns 10-digit padded CIK; one HTTP call per process; persists to DB
- [ ] `parse_companyfacts()` is pure-function: blob in, list of RawEdgarFundamentalsRow out, no I/O
- [ ] All 11 us-gaap concepts in spec §6.3.1 are parsed (Revenues / Net Income / OperatingIncomeLoss / Assets / Liabilities / StockholdersEquity / Cash / LongTermDebt + LongTermDebtNoncurrent / OperatingCashflow / Capex / InterestExpense)
- [ ] Derived fields computed correctly: fcf_ttm, operating_margin, net_profit_margin, total_debt_to_equity, interest_coverage
- [ ] `EdgarSource.fetch_fundamentals_for_ticker()` updates `(edgar, ticker, xbrl_fundamentals)` watermark with most recent SEC `filed` date
- [ ] Re-running on a same-day-fetched ticker is a no-op (no companyfacts HTTP call)
- [ ] `EdgarSource.fetch_universe()` skips bad tickers and continues on per-ticker exceptions
- [ ] Every SEC HTTP request carries `User-Agent` with contact email
- [ ] Tenacity retries 429/503 with exponential backoff up to 5 attempts (0.5s -> 8s)
- [ ] No live network in unit tests (all HTTP mocked via pytest-mock)
- [ ] The build plan marks A.3.3 shipped
- [ ] No A.1 / A.2 / A.3.1 / A.3.2 regressions (`health_check` still works, `raw_yahoo` unchanged)
- [ ] Git log shows ~7 task commits + 1 build-plan commit

---

## Self-review

**Spec coverage:**

| A.3 spec section | A.3.3 task |
|---|---|
| §6.3.1 companyfacts pull | Task 5 (`_sec_get_with_retry`, `fetch_fundamentals_for_ticker`) |
| §6.3.1 us-gaap concept mapping | Task 4 (`edgar_xbrl_parser.parse_companyfacts`) |
| §6.3.1 TTM = sum of 4 latest quarters | Task 4 (`compute_ttm_from_quarters`) — present but not used by the per-period emitter; reserved for canonical-resolution layer in A.3.10 |
| §6.3.1 writes raw_edgar (here: `raw_edgar_fundamentals`) | Task 2 (`migrate_to_v5` + `insert_raw_edgar_fundamentals`) |
| Principle 6 watermarks | Task 5 (watermark short-circuit on same-day fetch + advance on success) |
| Principle 5 no silent overwrite | Task 2 (INSERT OR IGNORE) — Task 5 test exercises it |
| Spec §6.3 fair-access (User-Agent + <=10/s + tenacity) | Task 5 (`_sec_get_with_retry` with `wait_exponential(0.5..8s, attempts=5)`) |

**Out of scope** (deferred and called out explicitly above):

- TTM computation across quarterly observations into the `annual` row when only quarterly data is present (i.e., a TTM proxy for companies without a 10-K). For A.3.3 we emit `annual` rows only when the source reports a `fp=FY` observation; the `compute_ttm_from_quarters` helper is present and unit-tested but invoked by the canonical-resolution layer in A.3.10. This avoids a per-period vs. TTM ambiguity in the `revenue_ttm` column for now.
- Restated-financials reconciliation. Once a (run_id, ticker, fiscal_period, fiscal_year) is recorded, a later 10-K/A amendment does not overwrite. The `accn` audit trail in the source JSON is not persisted to our table; if amendment tracking is needed, a separate `raw_edgar_amendments` table would be added in A.3.10.
- Form 4 / 8-K → A.3.4
- Live integration against real SEC → A.5
- A separate `fiscal_year_end` column. Our `fiscal_period == "annual"` proxy assumes the SEC's `fp=FY` annotation. Companies with non-calendar fiscal years (e.g., Apple's FY ends late September) are correctly recorded because we use SEC's own `fy` value verbatim.
- Canonical-universe materialization (mapping `raw_edgar_fundamentals` -> `canonical_universe.revenue_ttm` / `ebit_ttm` / etc.). That's A.3.10's `materialization.py` work.

**Placeholder scan:** No "TBD", "TODO", or "implement later" in any code block above. Every step contains full source.

**Pydantic v2 gotchas avoided:** Only single-field `@field_validator(mode="before")` decorators on `ticker` and `cik`. Margin-clamping is done in the parser (returning None for out-of-range), not via a cross-field validator, so `Field(ge=-1.0, le=1.0)` constraints fire only on valid inputs. All optional numeric fields use `Optional[float] = None`.

**SEC fair-access gotchas avoided:**
- User-Agent header set on every request via `_headers()` (env-var override `SEC_EDGAR_USER_AGENT`).
- 429/503 -> tenacity exponential backoff 0.5s..8s, 5 attempts, then surface.
- One companyfacts call per ticker per run; same-day re-run short-circuits via watermark.
- One `company_tickers.json` call per process (in-memory cache + DB persistence + daily watermark).

**Test coverage shape:**

| Layer | Unit tests | Integration tests |
|---|---|---|
| Schema (`RawEdgarFundamentalsRow`) | 7 | — |
| DB migration + helpers | 14 | — |
| CIK lookup | 8 | — |
| XBRL parser | 13 | — |
| EdgarSource fetch methods | 12 | — |
| End-to-end orchestration | — | 2 |
| **Total new tests** | **54** | **2** |

**Architectural notes:**

- `edgar_xbrl_parser.py` is intentionally a pure-function module (no class, no I/O). This lets future canonical-resolution work in A.3.10 reuse the parser directly against a cached blob without re-hitting SEC.
- `_sec_get_with_retry` lives at module scope (not on the class) so tests can easily mock `requests.get` at the module level.
- The `(edgar, ticker, xbrl_fundamentals)` watermark stores the SEC `filed` date (not today's date) as `last_observation_date`. This is the spec-correct interpretation: the watermark says "we have all data up through filing X". `last_fetched_at` is what we compare against today to short-circuit same-day re-runs.

---

## Execution Handoff

**Recommended:** Subagent-driven, mirroring the A.3.1 / A.3.2 wave pattern.

- **Wave 1 (Schema + DB):** One sub-agent does Tasks 0–2 (pre-flight + `RawEdgarFundamentalsRow` + `migrate_to_v5` + `insert_raw_edgar_fundamentals` + CIK map helpers). All in `src/common/`; sequential. **~15 min**.
- **Wave 2 (CIK lookup + parser):** One sub-agent does Tasks 3–4 (`sec_cik_lookup.py` + `edgar_xbrl_parser.py` + fixture JSON files). Tasks 3 and 4 are independent — could split into two parallel sub-agents if available. **~20 min**.
- **Wave 3 (EdgarSource fetch):** One sub-agent does Tasks 5–6 (`fetch_fundamentals_for_ticker` + `fetch_universe`). Sequential (Task 6 depends on Task 5). **~15 min**.
- **Wave 4 (Integration + build plan):** Sub-agent does Task 7 (integration test); inline does Task 8 (build plan update) in parallel. **~10 min**.

Total: 4 waves, ~60-90 min wall-clock.

**Critical pre-execution checks for the executing agent:**
1. After Task 2, confirm `schema_version == 5` before proceeding.
2. After Task 4, confirm the fixture JSON parses correctly with `python -c "import json; print(len(json.load(open('tests/fixtures/edgar/CIK0000320193.json'))['facts']['us-gaap']))"` — expect 12.
3. Do not modify `EdgarSource.health_check()` — only add new methods. The A.1 health_check test is part of the regression net.
4. If the `_sec_get_with_retry` tenacity decorator triggers in tests (it shouldn't — mocks should intercept), the test will hang for ~10s due to backoff. Confirm the `requests.get` patch is applied at the `edgar_source` module path, not at `requests` directly.
