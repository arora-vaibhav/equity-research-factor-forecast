# Phase A.3.2 — Finviz Full Universe Fetch + Yahoo Full Fetch (with Watermarks)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]` checkbox syntax for tracking.

**Goal:** Make the first two sources actually fetch real data end-to-end with watermark-bounded delta-only pulls. FinvizSource gets a real `fetch_universe()` that wraps the existing scraper. YahooSource gets full implementations of `fetch_universe(ticker_list)`, `fetch_fundamentals_for_ticker(ticker)`, and `fetch_historical_price(ticker)` — all watermark-aware, all writing to the v2 storage. Migrate schema v3 → v4 to add the `raw_yahoo` source-native table.

**Architecture:** Additive only. Migration v3→v4 creates `raw_yahoo` table + `insert_raw_yahoo` helper. Source-adapter code in `finviz_source.py` and `yahoo_source.py` is extended (the `health_check()` already there stays untouched). Tests use `pytest-mock` to stub `yfinance` and `finvizfinance` so no live network in unit tests.

**Tech Stack:** Python 3.11+, `yfinance==1.3.0`, `finvizfinance==1.3.0`, `tenacity`, `pytest-mock`. No new runtime dependencies.

**Spec reference:** [docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md](../specs/2026-05-21-phase-a3-layer1-hardening-design.md) §6.1 (Finviz), §6.2 (Yahoo).

**Out of scope for A.3.2:**
- EDGAR XBRL parsing → A.3.3
- EDGAR Form 4 / 8-K → A.3.4
- FRED / FINRA / stockanalysis / OpenBB → A.3.5–A.3.7
- News activity sub-signals → A.3.8 onward
- Yang-Zhang vol → A.3.10
- factors.py refactor / dual-write → A.3.10
- Notebook extension → A.4
- Live integration test against real Yahoo/Finviz network → A.5 acceptance phase

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| `src/common/schemas.py` | Add `RawYahooRow` Pydantic model | Modify |
| `src/common/database.py` | Add `migrate_to_v4()` + `insert_raw_yahoo()` | Modify |
| `src/common/datasources/finviz_source.py` | Add real `fetch_universe()` body | Modify |
| `src/common/datasources/yahoo_source.py` | Add `fetch_historical_price`, `fetch_fundamentals_for_ticker`, `fetch_universe` | Modify |
| `tests/common/test_schemas_a3_2.py` | Tests for `RawYahooRow` | Create |
| `tests/common/test_database_a3_2.py` | Tests for `migrate_to_v4` + `insert_raw_yahoo` | Create |
| `tests/datasources/test_finviz_fetch.py` | Tests for `FinvizSource.fetch_universe` | Create |
| `tests/datasources/test_yahoo_fetch.py` | Tests for the 3 Yahoo fetch methods | Create |
| `tests/datasources/test_integration_a3_2.py` | Mock-level end-to-end Finviz→Yahoo handoff | Create |
| the build plan | Mark A.3.2 shipped | Modify |

---

## Task 0: Pre-flight verification

- [ ] **Step 1: Verify schema is at v3**

```
./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager(); print('schema:', m.get_schema_version())"
```

Expected: `schema: 3`.

- [ ] **Step 2: Verify A.3.1 tests still pass**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: `145 passed, 2 deselected`.

- [ ] **Step 3: Verify yfinance + finvizfinance importable from venv**

```
./venv/Scripts/python.exe -c "import yfinance, finvizfinance; print('yfinance', yfinance.__version__); print('finvizfinance', finvizfinance.__version__)"
```

Expected: prints both version strings.

No commit at this task — verification only.

---

## Task 1: `RawYahooRow` Pydantic model

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_schemas_a3_2.py`:

```python
"""Tests for Phase A.3.2 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import RawYahooRow


class TestRawYahooRow:
    def test_minimal_valid(self):
        r = RawYahooRow(
            run_id="r1",
            ticker="AAPL",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.run_id == "r1"
        assert r.ticker == "AAPL"
        assert r.market_cap is None
        assert r.pe_ttm is None

    def test_full_valid(self):
        r = RawYahooRow(
            run_id="r1",
            ticker="AAPL",
            market_cap=3_000_000_000_000.0,
            pe_ttm=24.5,
            pe_forward=22.1,
            ebit_ttm=120_000_000_000.0,
            fcf_ttm=110_000_000_000.0,
            total_debt=100_000_000_000.0,
            cash=50_000_000_000.0,
            book_value=70.0,
            operating_margin=0.30,
            net_profit_margin=0.25,
            revenue_growth_yoy=0.08,
            eps_growth_yoy=0.10,
            company_name="Apple Inc",
            sector="Technology",
            industry="Consumer Electronics",
            exchange="NASDAQ",
            price=150.0,
            avg_daily_volume=50_000_000,
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.market_cap > 0
        assert -1.0 <= r.operating_margin <= 1.0

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            RawYahooRow(
                run_id="r1",
                ticker="not-a-ticker",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_negative_market_cap_rejected(self):
        with pytest.raises(Exception):
            RawYahooRow(
                run_id="r1",
                ticker="AAPL",
                market_cap=-100.0,
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_operating_margin_out_of_range_rejected(self):
        with pytest.raises(Exception):
            RawYahooRow(
                run_id="r1",
                ticker="AAPL",
                operating_margin=1.5,
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_2.py -v
```

Expected: ImportError on `RawYahooRow`.

- [ ] **Step 3: Append `RawYahooRow` to `src/common/schemas.py`**

At the **end** of `src/common/schemas.py`, append:

```python


# === Phase A.3.2 - Yahoo source-native row =============================


class RawYahooRow(BaseModel):
    """One Yahoo Finance fundamentals snapshot per (run_id, ticker).

    Spec: docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.2. Stored in raw_yahoo table. Source-native (no canonicalization).
    """

    run_id: str
    ticker: str

    company_name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    exchange: Optional[str] = None

    market_cap: Optional[float] = Field(default=None, gt=0)
    price: Optional[float] = Field(default=None, gt=0)
    avg_daily_volume: Optional[int] = Field(default=None, ge=0)

    pe_ttm: Optional[float] = None
    pe_forward: Optional[float] = None
    ebit_ttm: Optional[float] = None
    fcf_ttm: Optional[float] = None
    total_debt: Optional[float] = None
    cash: Optional[float] = None
    book_value: Optional[float] = None
    operating_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    net_profit_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    revenue_growth_yoy: Optional[float] = None
    eps_growth_yoy: Optional[float] = None

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
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_2.py -v
```

Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/schemas.py tests/common/test_schemas_a3_2.py
git commit -m "feat(schemas): add RawYahooRow model for A.3.2 Yahoo source-native data"
```

---

## Task 2: `migrate_to_v4()` + `insert_raw_yahoo()`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_database_a3_2.py`:

```python
"""Tests for Phase A.3.2 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawYahooRow


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


def test_migrate_to_v4_creates_raw_yahoo_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    assert "raw_yahoo" in _table_names(db_path)


def test_raw_yahoo_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    cols = _columns(db_path, "raw_yahoo")
    expected = {
        "run_id", "ticker",
        "company_name", "sector", "industry", "exchange",
        "market_cap", "price", "avg_daily_volume",
        "pe_ttm", "pe_forward", "ebit_ttm", "fcf_ttm",
        "total_debt", "cash", "book_value",
        "operating_margin", "net_profit_margin",
        "revenue_growth_yoy", "eps_growth_yoy",
        "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v4_bumps_schema_version_to_4(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4]


def test_migrate_to_v4_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    mgr.migrate_to_v4()
    mgr.migrate_to_v4()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4]


def test_get_schema_version_returns_4_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    assert mgr.get_schema_version() == 4


def test_migrate_to_v4_preserves_v3_data(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    mgr.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-20", success=True,
    )
    mgr.migrate_to_v4()
    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w is not None
    assert w["last_observation_date"] == "2026-05-20"


def test_insert_raw_yahoo_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    row = RawYahooRow(
        run_id="r1", ticker="AAPL",
        company_name="Apple Inc", sector="Technology",
        market_cap=3.0e12, pe_ttm=24.5,
        operating_margin=0.30,
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    mgr.insert_raw_yahoo([row])
    with sqlite3.connect(db_path) as c:
        result = c.execute(
            "SELECT ticker, company_name, pe_ttm FROM raw_yahoo WHERE run_id='r1'"
        ).fetchone()
    assert result == ("AAPL", "Apple Inc", 24.5)


def test_insert_raw_yahoo_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v4()
    mgr.insert_raw_yahoo([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_yahoo").fetchone()[0]
    assert n == 0
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_2.py -v
```

Expected: `AttributeError` on `migrate_to_v4`.

- [ ] **Step 3: Add `migrate_to_v4()` to `DatabaseManager`**

In `src/common/database.py`, **locate** `migrate_to_v3()`. Immediately AFTER it (before `get_connection`), add:

```python
    def migrate_to_v4(self) -> None:
        """Idempotent migration v3 -> v4 per A.3 spec section 6.2.

        Adds raw_yahoo table (per (run_id, ticker) Yahoo source-native snapshot).
        Safe to call multiple times. Existing data preserved.
        """
        self.migrate_to_v3()

        v4_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_yahoo (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              company_name TEXT,
              sector TEXT,
              industry TEXT,
              exchange TEXT,
              market_cap REAL,
              price REAL,
              avg_daily_volume INTEGER,
              pe_ttm REAL,
              pe_forward REAL,
              ebit_ttm REAL,
              fcf_ttm REAL,
              total_debt REAL,
              cash REAL,
              book_value REAL,
              operating_margin REAL,
              net_profit_margin REAL,
              revenue_growth_yoy REAL,
              eps_growth_yoy REAL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (run_id, ticker)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_yahoo_ticker ON raw_yahoo(ticker)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v4_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 4")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (4, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()
```

In `src/common/database.py`, at the **end** of the `DatabaseManager` class, append:

```python
    # ------------------------------------------------------------------
    # Phase A.3.2: raw_yahoo insert helper
    # ------------------------------------------------------------------

    def insert_raw_yahoo(self, rows: list) -> None:
        """Insert RawYahooRow records into raw_yahoo.

        Uses plain INSERT (no IGNORE/REPLACE) because raw_yahoo is per-run and
        (run_id, ticker) is unique by construction. A duplicate would be a bug —
        let the integrity error surface.
        """
        if not rows:
            return
        from src.common.schemas import RawYahooRow
        records = [r.model_dump() if isinstance(r, RawYahooRow) else dict(r) for r in rows]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT INTO raw_yahoo ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_2.py -v
```

Expected: All 8 tests PASS.

- [ ] **Step 5: Run full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: ~158 passed (145 baseline + 5 schema + 8 database).

- [ ] **Step 6: Commit**

```
git add src/common/database.py tests/common/test_database_a3_2.py
git commit -m "feat(database): migrate_to_v4 - raw_yahoo table + insert_raw_yahoo helper"
```

---

## Task 3: `FinvizSource.fetch_universe()` real implementation

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_finviz_fetch.py`:

```python
"""Tests for FinvizSource.fetch_universe (Phase A.3.2)."""
from __future__ import annotations

import pandas as pd
import pytest

from src.common.datasources.finviz_source import FinvizSource


def test_fetch_universe_returns_dataframe(mocker):
    fake_df = pd.DataFrame([
        {"Ticker": "AAPL", "Company": "Apple Inc", "Sector": "Technology",
         "Market Cap": "3000B", "P/E": "24.5"},
        {"Ticker": "MSFT", "Company": "Microsoft Corp", "Sector": "Technology",
         "Market Cap": "2800B", "P/E": "30.5"},
    ])
    mock_ov = mocker.MagicMock()
    mock_ov.screener_view.return_value = fake_df
    mocker.patch(
        "src.common.datasources.finviz_source.Overview",
        return_value=mock_ov,
    )

    src = FinvizSource()
    df = src.fetch_universe(run_id="r1")
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
    assert "Ticker" in df.columns
    assert "run_id" in df.columns
    assert (df["run_id"] == "r1").all()


def test_fetch_universe_stamps_scrape_timestamp(mocker):
    fake_df = pd.DataFrame([{"Ticker": "AAPL", "Company": "Apple Inc"}])
    mock_ov = mocker.MagicMock()
    mock_ov.screener_view.return_value = fake_df
    mocker.patch(
        "src.common.datasources.finviz_source.Overview",
        return_value=mock_ov,
    )

    df = FinvizSource().fetch_universe(run_id="r1")
    assert "scrape_timestamp" in df.columns
    ts = df["scrape_timestamp"].iloc[0]
    assert isinstance(ts, str)
    assert "T" in ts


def test_fetch_universe_applies_market_cap_filter(mocker):
    fake_df = pd.DataFrame([{"Ticker": "AAPL"}])
    mock_ov = mocker.MagicMock()
    mock_ov.screener_view.return_value = fake_df
    mocker.patch(
        "src.common.datasources.finviz_source.Overview",
        return_value=mock_ov,
    )
    FinvizSource().fetch_universe(run_id="r1")
    assert mock_ov.set_filter.called
    call_args_list = mock_ov.set_filter.call_args_list
    saw_market_cap = any(
        "Market Cap." in (call.kwargs.get("filters_dict") or {})
        for call in call_args_list
    )
    assert saw_market_cap


def test_fetch_universe_empty_result_returns_empty_df(mocker):
    mock_ov = mocker.MagicMock()
    mock_ov.screener_view.return_value = pd.DataFrame()
    mocker.patch(
        "src.common.datasources.finviz_source.Overview",
        return_value=mock_ov,
    )
    df = FinvizSource().fetch_universe(run_id="r1")
    assert df.empty


def test_fetch_universe_propagates_upstream_exception(mocker):
    mocker.patch(
        "src.common.datasources.finviz_source.Overview",
        side_effect=RuntimeError("upstream blocked"),
    )
    with pytest.raises(RuntimeError, match="upstream blocked"):
        FinvizSource().fetch_universe(run_id="r1")
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_finviz_fetch.py -v
```

Expected: `NotImplementedError` from BaseDataSource default.

- [ ] **Step 3: Implement `fetch_universe()` on `FinvizSource`**

Open `src/common/datasources/finviz_source.py`. **Inside** the `FinvizSource` class, AFTER the `health_check` method, append:

```python

    def fetch_universe(self, run_id: str) -> "pd.DataFrame":
        """Pull the Finviz mega-cap universe and return a DataFrame.

        Spec: docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md
        section 6.1. Source-native columns are preserved (no normalization here —
        that happens at the canonical-resolution layer).

        Every returned row carries run_id and scrape_timestamp.
        Filters: Market Cap >= mega ($200bln+). Errors propagate.
        """
        import pandas as pd
        ov = Overview()
        ov.set_filter(filters_dict={"Market Cap.": "Mega ($200bln and more)"})
        df = ov.screener_view()
        if df is None:
            df = pd.DataFrame()
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        df = df.copy()
        df["run_id"] = run_id
        df["scrape_timestamp"] = now_iso
        return df
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_finviz_fetch.py -v
```

Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/finviz_source.py tests/datasources/test_finviz_fetch.py
git commit -m "feat(datasources): FinvizSource.fetch_universe - real mega-cap pull via finvizfinance"
```

---

## Task 4: `YahooSource.fetch_historical_price()` with watermark

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_yahoo_fetch.py`:

```python
"""Tests for YahooSource fetch methods (Phase A.3.2)."""
from __future__ import annotations

import datetime
from pathlib import Path

import pandas as pd
import pytest

from src.common.database import DatabaseManager
from src.common.datasources.yahoo_source import YahooSource


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v4()
    return mgr


def _fake_history_df():
    idx = pd.DatetimeIndex(["2026-05-18", "2026-05-19", "2026-05-20"])
    return pd.DataFrame(
        {
            "Open":  [148.0, 149.0, 150.0],
            "High":  [152.0, 153.0, 154.0],
            "Low":   [147.5, 148.5, 149.5],
            "Close": [151.2, 152.5, 153.8],
            "Volume": [50_000_000, 48_000_000, 52_000_000],
            "Adj Close": [151.2, 152.5, 153.8],
        },
        index=idx,
    )


def test_fetch_historical_price_writes_rows_to_db(db, mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = _fake_history_df()
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )

    src = YahooSource()
    n = src.fetch_historical_price("AAPL", db=db)
    assert n == 3

    import sqlite3
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT observation_date, close FROM historical_price WHERE ticker='AAPL' ORDER BY observation_date"
        ).fetchall()
    assert rows == [
        ("2026-05-18", 151.2),
        ("2026-05-19", 152.5),
        ("2026-05-20", 153.8),
    ]


def test_fetch_historical_price_updates_watermark_on_success(db, mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = _fake_history_df()
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )

    YahooSource().fetch_historical_price("AAPL", db=db)
    w = db.get_watermark("yahoo", "AAPL", "historical_price")
    assert w is not None
    assert w["last_observation_date"] == "2026-05-20"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_fetch_historical_price_uses_watermark_gap_when_present(db, mocker):
    db.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date="2026-05-19", success=True,
    )
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = _fake_history_df()
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )

    YahooSource().fetch_historical_price("AAPL", db=db)
    call_kwargs = mock_ticker.history.call_args.kwargs
    assert "start" in call_kwargs
    assert str(call_kwargs["start"]) == "2026-05-20"


def test_fetch_historical_price_no_gap_returns_zero_and_skips_call(db, mocker):
    today_iso = datetime.date.today().isoformat()
    db.upsert_watermark(
        source="yahoo", ticker="AAPL", field="historical_price",
        last_observation_date=today_iso, success=True,
    )
    history_mock = mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
    )
    n = YahooSource().fetch_historical_price("AAPL", db=db)
    assert n == 0
    history_mock.assert_not_called()


def test_fetch_historical_price_records_failure_on_exception(db, mocker):
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        side_effect=RuntimeError("yahoo down"),
    )
    src = YahooSource()
    n = src.fetch_historical_price("AAPL", db=db)
    assert n == 0
    w = db.get_watermark("yahoo", "AAPL", "historical_price")
    assert w is not None
    assert w["error_count"] == 1
    assert "yahoo down" in (w["last_error_message"] or "")
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_yahoo_fetch.py -v
```

Expected: `AttributeError` on `fetch_historical_price`.

- [ ] **Step 3: Add `fetch_historical_price` to `YahooSource`**

In `src/common/datasources/yahoo_source.py`, **inside** the class, AFTER `health_check`, append:

```python

    def fetch_historical_price(self, ticker: str, db) -> int:
        """Pull historical OHLCV from Yahoo for the gap window only.

        Reads (source=yahoo, ticker, field=historical_price) watermark to find
        the start date; pulls only deltas. Writes to historical_price with
        INSERT OR IGNORE. Updates the watermark on success/failure.

        Returns the number of rows successfully inserted (0 if up-to-date
        or on failure, with watermark error_count incremented).
        """
        import datetime
        import pandas as pd
        from src.common.schemas import HistoricalPriceRow

        start_date, end_date = self.get_fetch_gap(ticker, "historical_price", db)
        if start_date > end_date:
            return 0
        try:
            t = yf.Ticker(ticker)
            df = t.history(start=str(start_date), end=str(end_date + datetime.timedelta(days=1)))
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="historical_price",
                last_observation_date=None, db=db, success=False,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            return 0

        if df is None or df.empty:
            self.update_watermark(
                ticker=ticker, field="historical_price",
                last_observation_date=None, db=db, success=False,
                error_message="empty history response",
            )
            return 0

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        rows = []
        for ts, r in df.iterrows():
            obs_date = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            rows.append(
                HistoricalPriceRow(
                    ticker=ticker,
                    observation_date=obs_date,
                    open=float(r["Open"]),
                    high=float(r["High"]),
                    low=float(r["Low"]),
                    close=float(r["Close"]),
                    volume=int(r["Volume"]),
                    adj_close=float(r["Adj Close"]) if "Adj Close" in r else None,
                    source="yahoo",
                    scrape_timestamp=now_iso,
                )
            )

        db.insert_historical_price(rows)
        last_obs = max(r.observation_date for r in rows)
        self.update_watermark(
            ticker=ticker, field="historical_price",
            last_observation_date=datetime.date.fromisoformat(last_obs),
            db=db, success=True,
        )
        return len(rows)
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_yahoo_fetch.py -v
```

Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/yahoo_source.py tests/datasources/test_yahoo_fetch.py
git commit -m "feat(datasources): YahooSource.fetch_historical_price - watermark-bounded delta-only OHLCV pull"
```

---

## Task 5: `YahooSource.fetch_fundamentals_for_ticker()`

- [ ] **Step 1: Append failing tests to `tests/datasources/test_yahoo_fetch.py`**

```python


# Task 5: fetch_fundamentals_for_ticker
from src.common.schemas import RawYahooRow  # noqa: E402


def _fake_info(overrides=None):
    base = {
        "longName": "Apple Inc",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "exchange": "NMS",
        "marketCap": 3_000_000_000_000,
        "regularMarketPrice": 150.0,
        "averageDailyVolume10Day": 50_000_000,
        "trailingPE": 24.5,
        "forwardPE": 22.1,
        "ebitda": 130_000_000_000,
        "operatingCashflow": 110_000_000_000,
        "capitalExpenditures": -10_000_000_000,
        "totalDebt": 100_000_000_000,
        "totalCash": 50_000_000_000,
        "bookValue": 70.0,
        "operatingMargins": 0.30,
        "profitMargins": 0.25,
        "revenueGrowth": 0.08,
        "earningsGrowth": 0.10,
    }
    if overrides:
        base.update(overrides)
    return base


def test_fetch_fundamentals_returns_raw_yahoo_row(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.info = _fake_info()
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )
    row = YahooSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1")
    assert isinstance(row, RawYahooRow)
    assert row.ticker == "AAPL"
    assert row.company_name == "Apple Inc"
    assert row.sector == "Technology"
    assert row.pe_ttm == 24.5
    assert row.pe_forward == 22.1
    assert row.market_cap == 3_000_000_000_000


def test_fetch_fundamentals_handles_missing_fields(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.info = {"longName": "Acme Corp"}
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )
    row = YahooSource().fetch_fundamentals_for_ticker("ACME", run_id="r1")
    assert row.company_name == "Acme Corp"
    assert row.pe_ttm is None
    assert row.market_cap is None


def test_fetch_fundamentals_returns_none_on_exception(mocker):
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        side_effect=RuntimeError("yahoo unauthorized"),
    )
    row = YahooSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1")
    assert row is None


def test_fetch_fundamentals_computes_fcf_from_ocf_minus_capex(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.info = _fake_info({
        "operatingCashflow": 110_000_000_000,
        "capitalExpenditures": -10_000_000_000,
    })
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )
    row = YahooSource().fetch_fundamentals_for_ticker("AAPL", run_id="r1")
    assert row.fcf_ttm == 100_000_000_000
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_yahoo_fetch.py -v
```

Expected: `NotImplementedError` (BaseDataSource default).

- [ ] **Step 3: Implement `fetch_fundamentals_for_ticker` on `YahooSource`**

In `src/common/datasources/yahoo_source.py`, **inside** the class, after `fetch_historical_price`, append:

```python

    def fetch_fundamentals_for_ticker(self, ticker: str, run_id: str):
        """Pull one-ticker fundamentals snapshot from Yahoo.

        Returns a RawYahooRow with as much detail as yfinance .info exposes.
        Returns None on exception or empty info dict.
        Missing yfinance fields become None on the row.
        """
        import datetime
        from src.common.schemas import RawYahooRow

        try:
            t = yf.Ticker(ticker)
            info = t.info
        except Exception:
            return None

        if not info:
            return None

        def _get_float(key):
            v = info.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _get_int(key):
            v = info.get(key)
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        ocf = _get_float("operatingCashflow")
        capex = _get_float("capitalExpenditures")
        fcf_ttm = None
        if ocf is not None and capex is not None:
            fcf_ttm = ocf - abs(capex)

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

        try:
            return RawYahooRow(
                run_id=run_id,
                ticker=ticker,
                company_name=info.get("longName") or info.get("shortName"),
                sector=info.get("sector"),
                industry=info.get("industry"),
                exchange=info.get("exchange"),
                market_cap=_get_float("marketCap"),
                price=_get_float("regularMarketPrice"),
                avg_daily_volume=_get_int("averageDailyVolume10Day"),
                pe_ttm=_get_float("trailingPE"),
                pe_forward=_get_float("forwardPE"),
                ebit_ttm=_get_float("ebitda"),
                fcf_ttm=fcf_ttm,
                total_debt=_get_float("totalDebt"),
                cash=_get_float("totalCash"),
                book_value=_get_float("bookValue"),
                operating_margin=_get_float("operatingMargins"),
                net_profit_margin=_get_float("profitMargins"),
                revenue_growth_yoy=_get_float("revenueGrowth"),
                eps_growth_yoy=_get_float("earningsGrowth"),
                scrape_timestamp=now_iso,
            )
        except Exception:
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_yahoo_fetch.py -v
```

Expected: All 9 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/yahoo_source.py tests/datasources/test_yahoo_fetch.py
git commit -m "feat(datasources): YahooSource.fetch_fundamentals_for_ticker - .info -> RawYahooRow"
```

---

## Task 6: `YahooSource.fetch_universe()` batch over ticker list

- [ ] **Step 1: Append failing tests**

```python


# Task 6: fetch_universe (batch)
def test_fetch_universe_returns_dataframe_with_one_row_per_ticker(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.info = _fake_info()
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )
    df = YahooSource().fetch_universe(
        run_id="r1",
        ticker_list=["AAPL", "MSFT", "GOOG"],
    )
    assert len(df) == 3
    assert sorted(df["ticker"].tolist()) == ["AAPL", "GOOG", "MSFT"]
    assert (df["run_id"] == "r1").all()


def test_fetch_universe_skips_tickers_with_no_info(mocker):
    def side_effect(ticker):
        t = mocker.MagicMock()
        t.info = _fake_info() if ticker == "GOOD" else {}
        return t
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        side_effect=side_effect,
    )
    df = YahooSource().fetch_universe(
        run_id="r1",
        ticker_list=["GOOD", "BAD"],
    )
    assert len(df) == 1
    assert df["ticker"].iloc[0] == "GOOD"


def test_fetch_universe_empty_list_returns_empty_dataframe(mocker):
    mocker.patch("src.common.datasources.yahoo_source.yf.Ticker")
    df = YahooSource().fetch_universe(run_id="r1", ticker_list=[])
    assert df.empty


def test_fetch_universe_continues_on_per_ticker_exception(mocker):
    def side_effect(ticker):
        if ticker == "BAD":
            raise RuntimeError("yahoo blew up on BAD")
        t = mocker.MagicMock()
        t.info = _fake_info()
        return t
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        side_effect=side_effect,
    )
    df = YahooSource().fetch_universe(
        run_id="r1",
        ticker_list=["AAPL", "BAD", "MSFT"],
    )
    assert len(df) == 2
    assert "BAD" not in df["ticker"].tolist()
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_yahoo_fetch.py -v
```

Expected: TypeError/AttributeError on `ticker_list` keyword.

- [ ] **Step 3: Implement `fetch_universe` on `YahooSource`**

In `src/common/datasources/yahoo_source.py`, **inside** the class, after `fetch_fundamentals_for_ticker`, append:

```python

    def fetch_universe(self, run_id: str, ticker_list: list[str] | None = None):
        """Pull one-ticker fundamentals for each ticker in ticker_list.

        Yahoo has no "scan all stocks" endpoint, so this requires an externally
        supplied list (typically from FinvizSource.fetch_universe).

        Returns a pd.DataFrame with one row per successful ticker. Bad tickers
        are silently skipped.
        """
        import pandas as pd
        if not ticker_list:
            return pd.DataFrame()

        rows = []
        for ticker in ticker_list:
            try:
                row = self.fetch_fundamentals_for_ticker(ticker, run_id=run_id)
            except Exception:
                continue
            if row is None:
                continue
            if row.company_name is None and row.market_cap is None and row.pe_ttm is None:
                continue
            rows.append(row.model_dump())

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_yahoo_fetch.py -v
```

Expected: All 13 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/yahoo_source.py tests/datasources/test_yahoo_fetch.py
git commit -m "feat(datasources): YahooSource.fetch_universe - batch fundamentals over ticker_list"
```

---

## Task 7: Integration test

- [ ] **Step 1: Write the integration test**

Create `tests/datasources/test_integration_a3_2.py`:

```python
"""End-to-end orchestration test for A.3.2: Finviz -> Yahoo handoff with watermarks.

Unit-level integration test using mocks. A separate @pytest.mark.integration
test against real network is deferred to A.5.
"""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src.common.database import DatabaseManager
from src.common.datasources.finviz_source import FinvizSource
from src.common.datasources.yahoo_source import YahooSource


def _fake_yahoo_info():
    return {
        "longName": "Apple Inc",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "exchange": "NMS",
        "marketCap": 3_000_000_000_000,
        "regularMarketPrice": 150.0,
        "trailingPE": 24.5,
        "forwardPE": 22.1,
        "operatingMargins": 0.30,
        "profitMargins": 0.25,
    }


def _fake_yahoo_history():
    idx = pd.DatetimeIndex(["2026-05-18", "2026-05-19", "2026-05-20"])
    return pd.DataFrame(
        {
            "Open":  [148.0, 149.0, 150.0],
            "High":  [152.0, 153.0, 154.0],
            "Low":   [147.5, 148.5, 149.5],
            "Close": [151.2, 152.5, 153.8],
            "Volume": [50_000_000, 48_000_000, 52_000_000],
            "Adj Close": [151.2, 152.5, 153.8],
        },
        index=idx,
    )


def test_finviz_to_yahoo_handoff_with_watermarks(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v4()

    fake_finviz = pd.DataFrame([
        {"Ticker": "AAPL", "Company": "Apple Inc"},
        {"Ticker": "MSFT", "Company": "Microsoft Corp"},
    ])
    mock_ov = mocker.MagicMock()
    mock_ov.screener_view.return_value = fake_finviz
    mocker.patch(
        "src.common.datasources.finviz_source.Overview",
        return_value=mock_ov,
    )

    mock_ticker = mocker.MagicMock()
    mock_ticker.info = _fake_yahoo_info()
    mock_ticker.history.return_value = _fake_yahoo_history()
    mocker.patch(
        "src.common.datasources.yahoo_source.yf.Ticker",
        return_value=mock_ticker,
    )

    finviz = FinvizSource()
    universe_df = finviz.fetch_universe(run_id="run-1")
    assert len(universe_df) == 2

    yahoo = YahooSource()
    fundamentals_df = yahoo.fetch_universe(
        run_id="run-1",
        ticker_list=universe_df["Ticker"].tolist(),
    )
    assert len(fundamentals_df) == 2

    from src.common.schemas import RawYahooRow
    rows = [RawYahooRow(**rec) for rec in fundamentals_df.to_dict(orient="records")]
    db.insert_raw_yahoo(rows)

    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_yahoo WHERE run_id='run-1'").fetchone()[0]
    assert n == 2

    for ticker in ["AAPL", "MSFT"]:
        inserted = yahoo.fetch_historical_price(ticker, db=db)
        assert inserted == 3

    with sqlite3.connect(db_path) as c:
        n_prices = c.execute("SELECT COUNT(*) FROM historical_price").fetchone()[0]
    assert n_prices == 6

    for ticker in ["AAPL", "MSFT"]:
        w = db.get_watermark("yahoo", ticker, "historical_price")
        assert w is not None
        assert w["last_observation_date"] == "2026-05-20"
        assert w["fetch_count"] == 1

    # Re-running INSERTs the same days; INSERT OR IGNORE protects against duplication.
    yahoo.fetch_historical_price("AAPL", db=db)
    with sqlite3.connect(db_path) as c:
        n_prices_after = c.execute("SELECT COUNT(*) FROM historical_price").fetchone()[0]
    assert n_prices_after == 6
```

- [ ] **Step 2: Run the integration test**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_integration_a3_2.py -v
```

Expected: PASS.

- [ ] **Step 3: Run the full non-integration suite**

```
./venv/Scripts/python.exe -m pytest -m "not integration" -v 2>&1 | tail -3
```

Expected: ~180 passed.

- [ ] **Step 4: Commit**

```
git add tests/datasources/test_integration_a3_2.py
git commit -m "test: A.3.2 integration acceptance - Finviz->Yahoo handoff + watermarks + INSERT OR IGNORE"
```

---

## Task 8: Update the build plan

- [ ] **Step 1: Locate the A.3 row in §5.1.0**

Grep the build plan for `**A.3.1 shipped 2026-05-21**` to find the line.

- [ ] **Step 2: Update the A.3 row's shipped marker**

Use `Edit` to replace the substring `**A.3.1 shipped 2026-05-21**` with `**A.3.1 + A.3.2 shipped 2026-05-21**`, and update the plan-link parenthetical to mention both plan files. Concretely, find this substring in the A.3 row:

```
**A.3.1 shipped 2026-05-21** ([plan](docs/design/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md)): schema v3 + `fetch_watermarks` + watermark CRUD + `force_refetch` + BaseDataSource gap/update helpers + `.env` loader + `api_keys.yaml`.
```

Replace with:

```
**A.3.1 + A.3.2 shipped 2026-05-21** (plans: [A.3.1](docs/design/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/design/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md)): schema v4 + `fetch_watermarks` + watermark CRUD + `force_refetch` + BaseDataSource gap/update helpers + `.env` loader + `api_keys.yaml` + FinvizSource real `fetch_universe` + YahooSource `fetch_universe` batch + `fetch_fundamentals_for_ticker` + `fetch_historical_price` (watermark-bounded) + `raw_yahoo` source-native table.
```

- [ ] **Step 3: Commit**

```
git add <build-plan>
git commit -m "docs: mark A.3.2 shipped - Finviz + Yahoo full fetch with watermarks"
```

---

## Phase A.3.2 — Definition of Done

- [ ] `./venv/Scripts/python.exe -m pytest -m "not integration"` shows ~180 tests passing
- [ ] `migrate_to_v4()` produces `schema_version == 4`
- [ ] `raw_yahoo` table exists; `insert_raw_yahoo()` works
- [ ] `FinvizSource.fetch_universe()` returns DataFrame with run_id + scrape_timestamp stamped
- [ ] `YahooSource.fetch_historical_price()` writes to `historical_price` + updates watermarks correctly
- [ ] `YahooSource.fetch_fundamentals_for_ticker()` returns `RawYahooRow` (or `None` on failure)
- [ ] `YahooSource.fetch_universe(ticker_list)` returns DataFrame, skipping bad tickers
- [ ] Watermark-bounded delta-only fetch verified
- [ ] Re-running on fully-current ticker does NOT call `yf.Ticker`
- [ ] The build plan marks A.3.2 shipped
- [ ] No A.1 / A.2 / A.3.1 regressions
- [ ] Git log shows ~7 task commits + 1 build-plan commit

---

## Self-review

**Spec coverage:**

| A.3 spec section | A.3.2 task |
|---|---|
| §6.1 FinvizSource fetch_universe | Task 3 |
| §6.2 YahooSource fetch_universe + fetch_fundamentals + fetch_historical_price | Tasks 4, 5, 6 |
| §6.2 raw_yahoo table | Task 2 |
| Principle 6 watermarks bound the delta | Tasks 4, 7 |

**Out of scope** (deferred):
- raw_yahoo vs canonical_universe cross-validation → A.3.10
- Live integration against real Yahoo/Finviz → A.5
- yfinance rate-limit retry with tenacity → add when we see real-world failures

**Placeholder scan:** No "TBD", "TODO", "implement later". Every code step has full source.

**Pydantic v2 gotcha avoidance:** No cross-field validators in this plan; `Field(ge=…, le=…, gt=…)` constraints only.

---

## Execution Handoff

**Recommended:** Subagent-driven, mirroring the A.3.1 wave pattern.

- **Wave 1**: One sub-agent does Tasks 0–2 (pre-flight + `RawYahooRow` + `migrate_to_v4` + `insert_raw_yahoo`). All in `src/common/`; sequential.
- **Wave 2**: One sub-agent does Tasks 3–6 (Finviz fetch_universe + Yahoo 3 fetch methods). All in `src/common/datasources/`; sequential (tests build on each other in `test_yahoo_fetch.py`).
- **Wave 3**: Sub-agent does Task 7 (integration); inline does Task 8 (build plan update) in parallel.

Total: 3 waves, ~45-60 min wall-clock.
