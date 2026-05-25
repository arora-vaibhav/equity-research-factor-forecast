# Phase A.3.6 — StockanalysisSource 10-Year Ratio History Scraper

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]` checkbox syntax for tracking.

**Goal:** Wire `StockanalysisSource.fetch_ratio_history(ticker, run_id)` to a real, end-to-end implementation. The method pulls the three public ratio pages from `stockanalysis.com` (annual + quarterly + TTM), parses each HTML table into long-format `RawStockanalysisRatioRow` records, and writes to a new `raw_stockanalysis_ratios` table via INSERT OR IGNORE. The parser lives in a pure module (`src/common/datasources/stockanalysis_parser.py`) so its failure surface is isolated and easy to test against synthetic HTML fixtures. Schema migrates v7 → v8 to add the new raw table. Watermark per (`stockanalysis`, ticker, `ratio_history`) gates refreshes — skip within 90 days because ratios change quarterly, not daily.

**Why this matters:** A.3.10's valuation factor scoring needs each ticker's *own* trailing-5-year distribution of P/E and EV/EBITDA so the materialization layer can compute `pe_5y_percentile` and `ev_ebitda_5y_percentile` in `canonical_universe` (see the methodology bibliography). The alpha signal is "is today's P/E in this stock's own 10th, 50th, or 90th percentile of its trailing-5-year window" — i.e. valuation *in context*, not against a sector or market median. That requires a long-format ratio-history substrate; stockanalysis.com is the cheapest keyless source with 10 years of annual + quarterly history per ticker. EDGAR XBRL gives us point-in-time income-statement and balance-sheet facts (A.3.3), but it does NOT publish a ready-made ratio history with TTM rollups — computing 10 years of TTM P/E from EDGAR alone would require us to join in historical share-price closes per quarter-end, which is more brittle than scraping a vendor who already did the work. We can later cross-check stockanalysis values against an EDGAR-derived ratio in A.3.8+; for now the scraped numbers are the substrate that makes Layer-1 valuation context computable at all.

**Architecture:** Additive only. Migration v7 → v8 creates `raw_stockanalysis_ratios` and bumps `schema_version`. The source gains a single new public method:

1. `StockanalysisSource.fetch_ratio_history(ticker, run_id, db, *, refresh_after_days=90)` — resolves whether a refresh is needed by reading the `(stockanalysis, ticker, ratio_history)` watermark; if the previous successful pull is within 90 days, returns 0 immediately. Otherwise pulls three URLs (annual, quarterly, TTM), parses each via `parse_ratios_page()`, INSERTs OR IGNOREs into `raw_stockanalysis_ratios`, and updates the watermark on success. Polite 1.0-second delay between the three URL pulls per ticker.

HTML parsing lives in a **pure-function module** (`src/common/datasources/stockanalysis_parser.py`) rather than as an instance method on the source, because the parse logic is genuinely complex (table-header date discovery, per-row metric-name normalization, per-cell value-format dispatch for percentages vs floats vs missing-value sentinels) and we want a sharp unit-test boundary. This matches the A.3.3 + A.3.4 pattern where `edgar_xbrl_parser.py`, `edgar_form4_parser.py`, and `edgar_submissions_parser.py` were extracted from `EdgarSource` for the same reason.

**Tech Stack:** Python 3.11+, `requests`, `tenacity`, `pydantic` v2, `pytest`, `pytest-mock`, **+ new dependency `beautifulsoup4`** for HTML parsing. BeautifulSoup is the standard tool for "scrape a vendor's HTML table" in Python; it gives us a robust DOM with CSS-selector / `find_all('table')` semantics. No new runtime dependencies beyond `beautifulsoup4`. (No `lxml` — `beautifulsoup4` ships with the stdlib `html.parser` backend and that is sufficient for stockanalysis.com's small ratio-page payloads.)

**Spec reference:** [docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md](../specs/2026-05-21-phase-a3-layer1-hardening-design.md) §6.6 (StockanalysisSource) + §3 Principle 5 (no silent overwrite) + Principle 6 (watermarks bound deltas).

**Methodology reference:** the methodology bibliography — the 10-year ratio history powers `pe_5y_percentile` and `ev_ebitda_5y_percentile` in `canonical_universe`. These trailing-window percentile features feed A.3.10 valuation factor scoring: a stock trading at the 10th percentile of its own 5-year P/E distribution scores high on "cheap-vs-history"; one at the 90th percentile scores low. Sector-relative valuation is the other axis (A.3.8+); the self-history axis lives here.

**HTML scraping fragility — read once before executing:** stockanalysis.com may rearrange their DOM at any time without notice (they're a content site, not a stable API contract). The parser must NOT crash on unexpected HTML; it must log the failure mode (which table couldn't be located, which header row lacked period dates, etc.) and return an empty list. The `StockanalysisSource.fetch_ratio_history` caller treats an empty parse as a soft failure: the watermark records error_count += 1 with a "parse: no rows" message, and the loop proceeds with the remaining URLs. Re-runs are idempotent: a future page-layout fix lets the next pull succeed and INSERT OR IGNORE silently de-dupes any previously-captured data. This is identical to the A.3.4 EDGAR Form 4 / submissions parser policy.

**URL format (verified from public stockanalysis.com pages as of the plan date):**
- Annual ratios:    `https://stockanalysis.com/stocks/{ticker}/financials/ratios/`
- Quarterly ratios: `https://stockanalysis.com/stocks/{ticker}/financials/ratios/?p=quarterly`
- Trailing (TTM):   `https://stockanalysis.com/stocks/{ticker}/financials/ratios/?p=trailing`

If the executing agent finds a URL has been renamed (e.g., `?p=ttm` instead of `?p=trailing`), only the URL-format constants in `stockanalysis_source.py` need to change — the parser is URL-pattern-independent.

**Period-end date normalization:** stockanalysis.com publishes column headers as fiscal-year labels (`2024`, `2023`, ...) for annual and as `Q3 2024`, `Q2 2024` for quarterly, and as a single `TTM` or recent quarter label for trailing. The parser normalizes them to `YYYY-MM-DD` ISO dates:
- Annual `2024` → `2024-12-31` (fiscal year-end approximation; the materialization layer can refine via EDGAR period-end alignment in A.3.8+).
- Quarterly `Q1 2024` → `2024-03-31`; `Q2 2024` → `2024-06-30`; `Q3 2024` → `2024-09-30`; `Q4 2024` → `2024-12-31`.
- TTM → the date of the most recent quarterly period we can parse from the header (or today's UTC date as a fallback if the header is just literal `TTM`).

These approximations are CONSISTENT: every annual 2024 row across every ticker normalizes to the same `2024-12-31` key. That consistency is what matters for cross-ticker comparisons in A.3.10. (Some companies have non-December fiscal year-ends — Apple's is in September. We accept this approximation in v1 and document it; the methodology handbook §fiscal-year-handling can refine later by joining against `raw_edgar_fundamentals` period-end dates.)

**90-day refresh skip rationale:** Ratios on stockanalysis.com update with each new quarterly earnings release — so the underlying data changes ~every 90 days per ticker. Daily refresh wastes bandwidth and politeness budget. The watermark-based 90-day skip is the analog of A.3.5's 14-day FINRA skip — same Principle-6 pattern, calibrated to ratio-history's publication cadence.

**Out of scope for A.3.6:**
- The materialization layer that converts `raw_stockanalysis_ratios` into `pe_5y_percentile` / `ev_ebitda_5y_percentile` in `canonical_universe` → A.3.10.
- Cross-validation of stockanalysis values against EDGAR-derived ratios → A.3.8+.
- The OpenBB multi-provider router (§6.7) → A.3.7.
- Live integration tests against the real stockanalysis.com endpoint → A.5 acceptance phase.
- Sector-relative valuation percentiles (different signal, different substrate) → A.3.8.

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| `requirements.txt` | Add `beautifulsoup4==4.12.3` | Modify |
| `src/common/schemas.py` | Add `RawStockanalysisRatioRow` Pydantic model | Modify |
| `src/common/database.py` | Add `migrate_to_v8()` + `insert_raw_stockanalysis_ratios()` | Modify |
| `src/common/datasources/stockanalysis_parser.py` | NEW — pure-function HTML parser | Create |
| `src/common/datasources/stockanalysis_source.py` | Add `fetch_ratio_history()` real implementation | Modify |
| `tests/fixtures/stockanalysis/aapl_ratios_annual.html` | NEW — synthetic HTML excerpt for annual ratios | Create |
| `tests/fixtures/stockanalysis/aapl_ratios_quarterly.html` | NEW — synthetic HTML excerpt for quarterly ratios | Create |
| `tests/fixtures/stockanalysis/aapl_ratios_ttm.html` | NEW — synthetic HTML excerpt for TTM row | Create |
| `tests/fixtures/stockanalysis/malformed.html` | NEW — broken HTML to exercise parser-robustness path | Create |
| `tests/common/test_schemas_a3_6.py` | Tests for `RawStockanalysisRatioRow` | Create |
| `tests/common/test_database_a3_6.py` | Tests for `migrate_to_v8` + insert helper | Create |
| `tests/datasources/test_stockanalysis_parser.py` | Tests for `parse_ratios_page` pure function | Create |
| `tests/datasources/test_stockanalysis_fetch.py` | Tests for `StockanalysisSource.fetch_ratio_history` (mocked HTTP) | Create |
| `tests/datasources/test_integration_a3_6.py` | End-to-end orchestration test | Create |
| the build plan | Mark A.3.6 shipped | Modify |

---

## Task 0: Pre-flight verification

**Files:** none modified (and possibly `requirements.txt` modified at the end if `beautifulsoup4` is absent).

- [ ] **Step 1: Verify schema is at v7 from A.3.5**

Run from the repository root:

```
./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager(); print('current schema version:', m.get_schema_version())"
```

Expected: prints `current schema version: 7`.

- [ ] **Step 2: Verify A.3.5 baseline tests still pass**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: `370 passed, 2 deselected` (or whatever the current A.3.5 baseline shows; counts may drift ±2).

- [ ] **Step 3: Verify tenacity + requests importable from venv**

```
./venv/Scripts/python.exe -c "import tenacity, requests; print('tenacity import ok'); print('requests', requests.__version__)"
```

Expected: both lines print without error. NOTE: `tenacity` has no module-level `__version__` attribute in many builds — we only verify `import tenacity` works (matches A.3.3/A.3.4/A.3.5 pattern).

- [ ] **Step 4: Verify `beautifulsoup4` import status**

```
./venv/Scripts/python.exe -c "import bs4; print('bs4 version:', bs4.__version__)"
```

Two possible outcomes:

a. **Import succeeds**: `bs4` is already on the path. Note the version; if it's older than 4.10, upgrade in Step 5. If it's already 4.12+ AND already pinned in `requirements.txt`, skip Step 5.

b. **`ModuleNotFoundError: No module named 'bs4'`**: BeautifulSoup is not installed. Proceed to Step 5 to install it AND pin in `requirements.txt`.

- [ ] **Step 5: Install/upgrade `beautifulsoup4` and pin in `requirements.txt`**

If Step 4 indicated `bs4` is missing or older than 4.12:

```
./venv/Scripts/python.exe -m pip install "beautifulsoup4==4.12.3"
```

Then append to `requirements.txt` (after the `pytest-mock==3.14.0` line) the line:

```
beautifulsoup4==4.12.3
```

Use `Edit` rather than appending blindly: locate the line `pytest-mock==3.14.0` and add a new line below it containing `beautifulsoup4==4.12.3`. If the file ends without a trailing newline, add one.

Verify:

```
./venv/Scripts/python.exe -c "import bs4; print('bs4 ok:', bs4.__version__)"
```

Expected: `bs4 ok: 4.12.3`.

- [ ] **Step 6: Verify A.1 StockanalysisSource.health_check is present (we extend it, do not replace)**

```
./venv/Scripts/python.exe -c "from src.common.datasources.stockanalysis_source import StockanalysisSource; print('stockanalysis', StockanalysisSource().name)"
```

Expected: `stockanalysis stockanalysis`.

- [ ] **Step 7: Verify working tree is clean before starting**

Run: `git status -s`

Expected: empty output (or only unrelated `.env` / notebooks). If `requirements.txt` was modified in Step 5, commit it now in a small isolated commit:

```
git add requirements.txt
git commit -m "build: add beautifulsoup4==4.12.3 dependency for A.3.6 HTML parsing"
```

If `requirements.txt` was NOT modified (i.e., bs4 was already there at a compatible version), no commit at this task — verification only.

No code commits at this task beyond the optional `requirements.txt` commit.

---

## Task 1: `RawStockanalysisRatioRow` Pydantic model

**Files:**
- Modify: `src/common/schemas.py` (append)
- Create: `tests/common/test_schemas_a3_6.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_schemas_a3_6.py`:

```python
"""Tests for Phase A.3.6 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import RawStockanalysisRatioRow


class TestRawStockanalysisRatioRow:
    def test_minimal_valid_annual(self):
        r = RawStockanalysisRatioRow(
            run_id="r1",
            ticker="AAPL",
            metric="pe_ratio",
            period_end_date="2024-12-31",
            period_type="annual",
            value=28.45,
            source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"
        assert r.metric == "pe_ratio"
        assert r.period_type == "annual"
        assert r.value == 28.45

    def test_minimal_valid_quarterly(self):
        r = RawStockanalysisRatioRow(
            run_id="r1",
            ticker="AAPL",
            metric="ev_ebitda",
            period_end_date="2024-09-30",
            period_type="quarterly",
            value=21.10,
            source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/?p=quarterly",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.period_type == "quarterly"
        assert r.period_end_date == "2024-09-30"

    def test_minimal_valid_ttm(self):
        r = RawStockanalysisRatioRow(
            run_id="r1",
            ticker="AAPL",
            metric="pe_ratio",
            period_end_date="2026-05-21",
            period_type="ttm",
            value=30.12,
            source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/?p=trailing",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.period_type == "ttm"

    def test_ticker_uppercased(self):
        r = RawStockanalysisRatioRow(
            run_id="r1",
            ticker="aapl",
            metric="pe_ratio",
            period_end_date="2024-12-31",
            period_type="annual",
            value=28.45,
            source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            RawStockanalysisRatioRow(
                run_id="r1",
                ticker="not-a-ticker",
                metric="pe_ratio",
                period_end_date="2024-12-31",
                period_type="annual",
                value=28.45,
                source_url="https://stockanalysis.com/x",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_unknown_metric_rejected(self):
        """metric is a Literal of supported names; unknown ones must raise."""
        with pytest.raises(Exception):
            RawStockanalysisRatioRow(
                run_id="r1",
                ticker="AAPL",
                metric="not_a_metric",
                period_end_date="2024-12-31",
                period_type="annual",
                value=28.45,
                source_url="https://stockanalysis.com/x",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_unknown_period_type_rejected(self):
        with pytest.raises(Exception):
            RawStockanalysisRatioRow(
                run_id="r1",
                ticker="AAPL",
                metric="pe_ratio",
                period_end_date="2024-12-31",
                period_type="monthly",
                value=28.45,
                source_url="https://stockanalysis.com/x",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_value_can_be_none(self):
        """Cells reported as 'n/a' or '-' in the HTML must map to None."""
        r = RawStockanalysisRatioRow(
            run_id="r1",
            ticker="AAPL",
            metric="pe_ratio",
            period_end_date="2015-12-31",
            period_type="annual",
            value=None,
            source_url="https://stockanalysis.com/x",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.value is None

    def test_period_end_date_required(self):
        with pytest.raises(Exception):
            RawStockanalysisRatioRow(
                run_id="r1",
                ticker="AAPL",
                metric="pe_ratio",
                period_end_date="",
                period_type="annual",
                value=28.45,
                source_url="https://stockanalysis.com/x",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_all_supported_metrics_accepted(self):
        """Round-trip every supported metric name to confirm the Literal."""
        supported = [
            "pe_ratio", "pb_ratio", "ps_ratio", "ev_ebitda",
            "dividend_yield", "roe", "roa",
            "profit_margin", "operating_margin",
            "fcf_yield", "current_ratio", "debt_to_equity",
        ]
        for m in supported:
            r = RawStockanalysisRatioRow(
                run_id="r1",
                ticker="AAPL",
                metric=m,
                period_end_date="2024-12-31",
                period_type="annual",
                value=1.0,
                source_url="https://stockanalysis.com/x",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
            assert r.metric == m

    def test_period_type_annual_quarterly_ttm_accepted(self):
        for pt in ["annual", "quarterly", "ttm"]:
            r = RawStockanalysisRatioRow(
                run_id="r1",
                ticker="AAPL",
                metric="pe_ratio",
                period_end_date="2024-12-31",
                period_type=pt,
                value=1.0,
                source_url="https://stockanalysis.com/x",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
            assert r.period_type == pt
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_6.py -v
```

Expected: `ImportError` on `RawStockanalysisRatioRow`.

- [ ] **Step 3: Append model to `src/common/schemas.py`**

At the **end** of `src/common/schemas.py`, append:

```python


# === Phase A.3.6 - stockanalysis.com 10y ratio history ===================


# Supported ratio metrics. Constrained as a Literal so unknown names from
# a future stockanalysis.com layout change raise loudly rather than silently
# corrupting the raw store. To accommodate a new metric, add it here and
# also extend the parser's whitelist in stockanalysis_parser.py.
_StockanalysisMetric = Literal[
    "pe_ratio",
    "pb_ratio",
    "ps_ratio",
    "ev_ebitda",
    "dividend_yield",
    "roe",
    "roa",
    "profit_margin",
    "operating_margin",
    "fcf_yield",
    "current_ratio",
    "debt_to_equity",
]

_StockanalysisPeriodType = Literal["annual", "quarterly", "ttm"]


class RawStockanalysisRatioRow(BaseModel):
    """One (ticker, metric, period_end_date) ratio observation scraped
    from stockanalysis.com.

    Spec: docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.6. Stored long-format in raw_stockanalysis_ratios; PK
    (ticker, metric, period_end_date). The materialization layer (A.3.10)
    pivots this table to wide form when computing the 5-year percentile
    features `pe_5y_percentile` and `ev_ebitda_5y_percentile`.

    `period_type` discriminates annual (FY) vs quarterly (Q1..Q4) vs ttm
    (trailing twelve months). The PK does NOT include period_type because
    a single (ticker, metric, period_end_date) tuple has at most one
    semantically distinct value — annual 2024-12-31 and quarterly Q4-2024
    end on the same date and would yield identical ratios.

    `value` is Optional because stockanalysis.com publishes 'n/a', '-',
    and blank cells where the underlying data is unavailable (typically
    for the oldest year in the 10y history). The parser maps all such
    sentinels to None.
    """

    run_id: str
    ticker: str
    metric: _StockanalysisMetric
    period_end_date: str
    period_type: _StockanalysisPeriodType
    value: Optional[float] = None
    source_url: str
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

    @field_validator("period_end_date", mode="before")
    @classmethod
    def _normalize_period_end_date(cls, v: object) -> str:
        if v is None:
            raise ValueError("period_end_date is required")
        s = str(v).strip()
        if not s:
            raise ValueError("period_end_date cannot be empty")
        return s
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_6.py -v
```

Expected: All 11 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/schemas.py tests/common/test_schemas_a3_6.py
git commit -m "feat(schemas): add RawStockanalysisRatioRow for A.3.6"
```

---

## Task 2: `migrate_to_v8` + `insert_raw_stockanalysis_ratios`

**Files:**
- Modify: `src/common/database.py`
- Create: `tests/common/test_database_a3_6.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_database_a3_6.py`:

```python
"""Tests for Phase A.3.6 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawStockanalysisRatioRow


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


# --- migrate_to_v8 ----------------------------------------------------


def test_migrate_to_v8_creates_raw_stockanalysis_ratios_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    assert "raw_stockanalysis_ratios" in _table_names(db_path)


def test_raw_stockanalysis_ratios_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    cols = _columns(db_path, "raw_stockanalysis_ratios")
    expected = {
        "run_id", "ticker", "metric", "period_end_date", "period_type",
        "value", "source_url", "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v8_bumps_schema_version_to_8(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8]


def test_migrate_to_v8_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.migrate_to_v8()
    mgr.migrate_to_v8()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8]


def test_get_schema_version_returns_8_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    assert mgr.get_schema_version() == 8


def test_migrate_to_v8_preserves_v7_data(tmp_path: Path):
    """raw_fred and raw_finra from v7 must survive the v8 migration."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v7()
    # Seed a raw_fred row.
    from src.common.schemas import RawFredObservation
    mgr.insert_raw_fred([
        RawFredObservation(
            run_id="r1", series_id="DGS10",
            observation_date="2026-05-12", value=4.17,
            source_filename="fredgraph.csv?id=DGS10",
            scrape_timestamp="2026-05-21T08:00:00Z",
        ),
    ])
    mgr.migrate_to_v8()
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_fred WHERE series_id='DGS10'"
        ).fetchone()[0]
    assert v == 4.17


# --- insert_raw_stockanalysis_ratios -------------------------------------


def _row(**overrides) -> RawStockanalysisRatioRow:
    base = dict(
        run_id="r1",
        ticker="AAPL",
        metric="pe_ratio",
        period_end_date="2024-12-31",
        period_type="annual",
        value=28.45,
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawStockanalysisRatioRow(**base)


def test_insert_raw_stockanalysis_ratios_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.insert_raw_stockanalysis_ratios([_row()])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT ticker, metric, period_end_date, value "
            "FROM raw_stockanalysis_ratios"
        ).fetchone()
    assert row == ("AAPL", "pe_ratio", "2024-12-31", 28.45)


def test_insert_raw_stockanalysis_ratios_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.insert_raw_stockanalysis_ratios([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_stockanalysis_ratios").fetchone()[0]
    assert n == 0


def test_insert_raw_stockanalysis_ratios_uses_insert_or_ignore(tmp_path: Path):
    """PK = (ticker, metric, period_end_date). First write wins on collision."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.insert_raw_stockanalysis_ratios([_row(value=28.45)])
    mgr.insert_raw_stockanalysis_ratios([_row(value=999.99)])
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_stockanalysis_ratios "
            "WHERE ticker='AAPL' AND metric='pe_ratio' "
            "AND period_end_date='2024-12-31'"
        ).fetchone()[0]
    assert v == 28.45


def test_insert_raw_stockanalysis_ratios_supports_multiple_metrics_same_period(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.insert_raw_stockanalysis_ratios([
        _row(metric="pe_ratio", value=28.45),
        _row(metric="ev_ebitda", value=21.10),
        _row(metric="ps_ratio", value=7.5),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM raw_stockanalysis_ratios WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert n == 3


def test_insert_raw_stockanalysis_ratios_supports_multiple_periods_same_metric(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.insert_raw_stockanalysis_ratios([
        _row(period_end_date="2024-12-31", value=28.45),
        _row(period_end_date="2023-12-31", value=25.10),
        _row(period_end_date="2022-12-31", value=22.00),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM raw_stockanalysis_ratios "
            "WHERE ticker='AAPL' AND metric='pe_ratio'"
        ).fetchone()[0]
    assert n == 3


def test_insert_raw_stockanalysis_ratios_handles_null_value(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    mgr.insert_raw_stockanalysis_ratios([_row(value=None, period_end_date="2015-12-31")])
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_stockanalysis_ratios "
            "WHERE period_end_date='2015-12-31'"
        ).fetchone()[0]
    assert v is None
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_6.py -v
```

Expected: `AttributeError` on `migrate_to_v8`.

- [ ] **Step 3: Add `migrate_to_v8()` to `DatabaseManager`**

In `src/common/database.py`, **locate** `migrate_to_v7()` (around line ~577). Immediately AFTER the `migrate_to_v7` method body (find the trailing `conn.commit()` of the `migrate_to_v7` `with self.get_connection()` block, and add the new method after that), insert:

```python
    def migrate_to_v8(self) -> None:
        """Idempotent migration v7 -> v8 per A.3 spec section 6.6.

        Adds:
          - raw_stockanalysis_ratios: long-format scraped ratio history
            from stockanalysis.com (annual + quarterly + ttm). PK
            (ticker, metric, period_end_date). Powers the 5-year
            percentile features `pe_5y_percentile` and
            `ev_ebitda_5y_percentile` in the A.3.10 materialization.

        Safe to call multiple times. Existing data preserved.

        Uses INSERT OR IGNORE on insert (Principle 5: no silent
        overwrite). The first-observed value for any
        (ticker, metric, period_end_date) wins; subsequent scrapes of
        the same historical bucket are silently dropped — ratios that
        far back are settled and not subject to legitimate revision.
        Recent buckets (current TTM, last-quarter) can be force-refreshed
        by deleting their watermark, which makes the next pull bypass the
        90-day skip. Updating *value* in place is intentionally not
        supported in v1.
        """
        self.migrate_to_v7()

        v8_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_stockanalysis_ratios (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              metric TEXT NOT NULL,
              period_end_date TEXT NOT NULL,
              period_type TEXT NOT NULL,
              value REAL,
              source_url TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (ticker, metric, period_end_date)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_sar_ticker ON raw_stockanalysis_ratios(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_sar_metric ON raw_stockanalysis_ratios(metric)",
            "CREATE INDEX IF NOT EXISTS idx_sar_period ON raw_stockanalysis_ratios(period_end_date)",
            "CREATE INDEX IF NOT EXISTS idx_sar_period_type ON raw_stockanalysis_ratios(period_type)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v8_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 8")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (8, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()
```

In `src/common/database.py`, at the **end** of the `DatabaseManager` class (after `insert_raw_finra`), append:

```python
    # ------------------------------------------------------------------
    # Phase A.3.6: raw_stockanalysis_ratios helpers
    # ------------------------------------------------------------------

    def insert_raw_stockanalysis_ratios(self, rows: list) -> None:
        """Insert RawStockanalysisRatioRow records into raw_stockanalysis_ratios.

        Uses INSERT OR IGNORE on PK (ticker, metric, period_end_date).
        First write wins — historical ratio buckets are immutable for
        v1 purposes; revisions are silently dropped.
        """
        if not rows:
            return
        from src.common.schemas import RawStockanalysisRatioRow
        records = [
            r.model_dump() if isinstance(r, RawStockanalysisRatioRow) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_stockanalysis_ratios ({col_list}) "
                f"VALUES ({placeholders})",
                values,
            )
            conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_6.py -v
```

Expected: All 12 tests PASS.

- [ ] **Step 5: Run full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: ~393 passed (370 baseline + 11 schema + 12 database).

- [ ] **Step 6: Commit**

```
git add src/common/database.py tests/common/test_database_a3_6.py
git commit -m "feat(database): migrate_to_v8 - raw_stockanalysis_ratios (INSERT OR IGNORE)"
```

---

## Task 3: `stockanalysis_parser.py` pure function + synthetic HTML fixtures

**Files:**
- Create: `src/common/datasources/stockanalysis_parser.py`
- Create: `tests/fixtures/stockanalysis/aapl_ratios_annual.html`
- Create: `tests/fixtures/stockanalysis/aapl_ratios_quarterly.html`
- Create: `tests/fixtures/stockanalysis/aapl_ratios_ttm.html`
- Create: `tests/fixtures/stockanalysis/malformed.html`
- Create: `tests/datasources/test_stockanalysis_parser.py`

- [ ] **Step 1: Create the synthetic ANNUAL HTML fixture**

Create `tests/fixtures/stockanalysis/aapl_ratios_annual.html`. This mimics stockanalysis.com's annual ratio page structure: a single `<table class="ratios-table">` with header dates and metric rows. Five period columns covering 2020-2024, twelve supported ratio rows plus one unsupported row to exercise the whitelist filter.

```html
<!DOCTYPE html>
<html lang="en">
<head><title>AAPL Ratios — stockanalysis.com</title></head>
<body>
<div class="financials">
  <table class="ratios-table">
    <thead>
      <tr>
        <th>Fiscal Year</th>
        <th>2020</th>
        <th>2021</th>
        <th>2022</th>
        <th>2023</th>
        <th>2024</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>PE Ratio</td>
        <td>33.21</td>
        <td>28.50</td>
        <td>24.10</td>
        <td>26.80</td>
        <td>28.45</td>
      </tr>
      <tr>
        <td>PS Ratio</td>
        <td>7.10</td>
        <td>7.60</td>
        <td>6.80</td>
        <td>7.20</td>
        <td>7.50</td>
      </tr>
      <tr>
        <td>PB Ratio</td>
        <td>30.10</td>
        <td>40.20</td>
        <td>45.50</td>
        <td>48.80</td>
        <td>52.10</td>
      </tr>
      <tr>
        <td>EV/EBITDA</td>
        <td>24.50</td>
        <td>22.10</td>
        <td>19.80</td>
        <td>20.50</td>
        <td>21.30</td>
      </tr>
      <tr>
        <td>Dividend Yield</td>
        <td>0.65%</td>
        <td>0.50%</td>
        <td>0.55%</td>
        <td>0.50%</td>
        <td>0.45%</td>
      </tr>
      <tr>
        <td>Profit Margin</td>
        <td>20.91%</td>
        <td>25.88%</td>
        <td>25.31%</td>
        <td>25.31%</td>
        <td>26.40%</td>
      </tr>
      <tr>
        <td>Operating Margin</td>
        <td>24.15%</td>
        <td>29.78%</td>
        <td>30.29%</td>
        <td>29.82%</td>
        <td>31.51%</td>
      </tr>
      <tr>
        <td>ROE</td>
        <td>87.87%</td>
        <td>147.44%</td>
        <td>196.96%</td>
        <td>171.95%</td>
        <td>165.32%</td>
      </tr>
      <tr>
        <td>ROA</td>
        <td>17.73%</td>
        <td>26.97%</td>
        <td>28.36%</td>
        <td>27.51%</td>
        <td>28.10%</td>
      </tr>
      <tr>
        <td>Current Ratio</td>
        <td>1.36</td>
        <td>1.07</td>
        <td>0.88</td>
        <td>0.99</td>
        <td>1.04</td>
      </tr>
      <tr>
        <td>Debt / Equity</td>
        <td>3.96</td>
        <td>4.56</td>
        <td>5.96</td>
        <td>4.99</td>
        <td>3.30</td>
      </tr>
      <tr>
        <td>FCF Yield</td>
        <td>4.50%</td>
        <td>3.80%</td>
        <td>4.10%</td>
        <td>3.90%</td>
        <td>n/a</td>
      </tr>
      <tr>
        <td>Some Unsupported Metric</td>
        <td>1.0</td>
        <td>1.1</td>
        <td>1.2</td>
        <td>1.3</td>
        <td>1.4</td>
      </tr>
    </tbody>
  </table>
</div>
</body>
</html>
```

- [ ] **Step 2: Create the synthetic QUARTERLY HTML fixture**

Create `tests/fixtures/stockanalysis/aapl_ratios_quarterly.html`. Five period columns for Q3 2023 through Q3 2024, five ratio rows. Uses `Q1 2024`-style header labels.

```html
<!DOCTYPE html>
<html lang="en">
<head><title>AAPL Quarterly Ratios — stockanalysis.com</title></head>
<body>
<div class="financials">
  <table class="ratios-table">
    <thead>
      <tr>
        <th>Quarter</th>
        <th>Q3 2023</th>
        <th>Q4 2023</th>
        <th>Q1 2024</th>
        <th>Q2 2024</th>
        <th>Q3 2024</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>PE Ratio</td>
        <td>27.80</td>
        <td>28.10</td>
        <td>27.90</td>
        <td>28.20</td>
        <td>30.10</td>
      </tr>
      <tr>
        <td>EV/EBITDA</td>
        <td>20.50</td>
        <td>21.00</td>
        <td>20.80</td>
        <td>21.10</td>
        <td>21.10</td>
      </tr>
      <tr>
        <td>PB Ratio</td>
        <td>48.00</td>
        <td>49.00</td>
        <td>50.00</td>
        <td>51.00</td>
        <td>52.50</td>
      </tr>
      <tr>
        <td>PS Ratio</td>
        <td>7.10</td>
        <td>7.20</td>
        <td>7.30</td>
        <td>7.40</td>
        <td>7.55</td>
      </tr>
      <tr>
        <td>Dividend Yield</td>
        <td>0.50%</td>
        <td>0.48%</td>
        <td>0.46%</td>
        <td>0.45%</td>
        <td>0.44%</td>
      </tr>
    </tbody>
  </table>
</div>
</body>
</html>
```

- [ ] **Step 3: Create the synthetic TTM HTML fixture**

Create `tests/fixtures/stockanalysis/aapl_ratios_ttm.html`. Single-column TTM page. The header is literal `TTM` so the parser must fall back to the scrape date.

```html
<!DOCTYPE html>
<html lang="en">
<head><title>AAPL TTM Ratios — stockanalysis.com</title></head>
<body>
<div class="financials">
  <table class="ratios-table">
    <thead>
      <tr>
        <th>Metric</th>
        <th>TTM</th>
      </tr>
    </thead>
    <tbody>
      <tr><td>PE Ratio</td><td>30.12</td></tr>
      <tr><td>EV/EBITDA</td><td>21.40</td></tr>
      <tr><td>PB Ratio</td><td>52.80</td></tr>
      <tr><td>PS Ratio</td><td>7.60</td></tr>
      <tr><td>Dividend Yield</td><td>0.44%</td></tr>
      <tr><td>Profit Margin</td><td>26.50%</td></tr>
      <tr><td>Operating Margin</td><td>31.80%</td></tr>
      <tr><td>ROE</td><td>168.00%</td></tr>
      <tr><td>ROA</td><td>28.50%</td></tr>
      <tr><td>FCF Yield</td><td>4.20%</td></tr>
    </tbody>
  </table>
</div>
</body>
</html>
```

- [ ] **Step 4: Create the malformed HTML fixture**

Create `tests/fixtures/stockanalysis/malformed.html`. No `<table>` at all; should make the parser return an empty list and log cleanly.

```html
<!DOCTYPE html>
<html lang="en">
<head><title>Not the page you were looking for</title></head>
<body>
<div class="error">
  <h1>404 - Stock not found</h1>
  <p>stockanalysis.com could not locate that ticker.</p>
</div>
</body>
</html>
```

- [ ] **Step 5: Write the failing parser tests**

Create `tests/datasources/test_stockanalysis_parser.py`:

```python
"""Tests for stockanalysis_parser.parse_ratios_page (Phase A.3.6)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.datasources.stockanalysis_parser import parse_ratios_page


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "stockanalysis"
ANNUAL_HTML = (FIXTURE_DIR / "aapl_ratios_annual.html").read_text(encoding="utf-8")
QUARTERLY_HTML = (FIXTURE_DIR / "aapl_ratios_quarterly.html").read_text(encoding="utf-8")
TTM_HTML = (FIXTURE_DIR / "aapl_ratios_ttm.html").read_text(encoding="utf-8")
MALFORMED_HTML = (FIXTURE_DIR / "malformed.html").read_text(encoding="utf-8")


# --- annual -----------------------------------------------------------


def test_parse_annual_extracts_pe_ratio_per_year():
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    pe_rows = [r for r in rows if r.metric == "pe_ratio"]
    by_date = {r.period_end_date: r.value for r in pe_rows}
    assert by_date["2020-12-31"] == 33.21
    assert by_date["2021-12-31"] == 28.50
    assert by_date["2022-12-31"] == 24.10
    assert by_date["2023-12-31"] == 26.80
    assert by_date["2024-12-31"] == 28.45


def test_parse_annual_marks_period_type():
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    assert rows
    assert all(r.period_type == "annual" for r in rows)


def test_parse_annual_percentage_normalized_to_decimal():
    """Dividend Yield '0.65%' -> 0.0065 (decimal)."""
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    dy = next(r for r in rows
              if r.metric == "dividend_yield" and r.period_end_date == "2020-12-31")
    assert abs(dy.value - 0.0065) < 1e-9


def test_parse_annual_skips_unsupported_metrics():
    """The 'Some Unsupported Metric' row must NOT appear in output."""
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    metric_names = {r.metric for r in rows}
    for m in metric_names:
        assert m in {
            "pe_ratio", "pb_ratio", "ps_ratio", "ev_ebitda",
            "dividend_yield", "roe", "roa",
            "profit_margin", "operating_margin",
            "fcf_yield", "current_ratio", "debt_to_equity",
        }


def test_parse_annual_handles_na_as_none():
    """FCF Yield 2024 is 'n/a' in the fixture -> value None."""
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    fcf = next(r for r in rows
               if r.metric == "fcf_yield" and r.period_end_date == "2024-12-31")
    assert fcf.value is None


def test_parse_annual_ticker_uppercased():
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="aapl", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    assert rows
    assert all(r.ticker == "AAPL" for r in rows)


def test_parse_annual_returns_full_metric_x_year_cardinality():
    """12 supported metrics x 5 years = 60 rows from the annual fixture."""
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
    )
    assert len(rows) == 60


def test_parse_annual_records_source_url_on_every_row():
    src = "https://stockanalysis.com/stocks/aapl/financials/ratios/"
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url=src,
    )
    assert rows
    assert all(r.source_url == src for r in rows)


def test_parse_annual_run_id_threaded_through():
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/x", run_id="my-run-42",
    )
    assert rows
    assert all(r.run_id == "my-run-42" for r in rows)


# --- quarterly --------------------------------------------------------


def test_parse_quarterly_extracts_per_quarter_dates():
    rows = parse_ratios_page(
        html=QUARTERLY_HTML, ticker="AAPL", period_type="quarterly",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/?p=quarterly",
    )
    pe_rows = [r for r in rows if r.metric == "pe_ratio"]
    by_date = {r.period_end_date: r.value for r in pe_rows}
    assert by_date["2023-09-30"] == 27.80
    assert by_date["2023-12-31"] == 28.10
    assert by_date["2024-03-31"] == 27.90
    assert by_date["2024-06-30"] == 28.20
    assert by_date["2024-09-30"] == 30.10


def test_parse_quarterly_marks_period_type():
    rows = parse_ratios_page(
        html=QUARTERLY_HTML, ticker="AAPL", period_type="quarterly",
        source_url="https://stockanalysis.com/x?p=quarterly",
    )
    assert rows
    assert all(r.period_type == "quarterly" for r in rows)


# --- ttm --------------------------------------------------------------


def test_parse_ttm_returns_single_period_per_metric():
    rows = parse_ratios_page(
        html=TTM_HTML, ticker="AAPL", period_type="ttm",
        source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/?p=trailing",
    )
    # 10 supported-metric rows in TTM fixture x 1 column = 10 rows.
    assert len(rows) == 10
    assert {r.period_type for r in rows} == {"ttm"}


def test_parse_ttm_period_end_date_is_iso_format():
    rows = parse_ratios_page(
        html=TTM_HTML, ticker="AAPL", period_type="ttm",
        source_url="https://stockanalysis.com/x?p=trailing",
    )
    # YYYY-MM-DD shape; can't assert exact date because parser falls back
    # to today() for literal 'TTM' headers.
    for r in rows:
        assert len(r.period_end_date) == 10
        assert r.period_end_date[4] == "-"
        assert r.period_end_date[7] == "-"


# --- malformed --------------------------------------------------------


def test_parse_malformed_returns_empty_list():
    """The parser must NOT crash on unexpected HTML; it returns []."""
    rows = parse_ratios_page(
        html=MALFORMED_HTML, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/x",
    )
    assert rows == []


def test_parse_empty_html_returns_empty_list():
    assert parse_ratios_page(
        html="", ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/x",
    ) == []


def test_parse_html_with_no_table_returns_empty_list():
    """HTML present but no <table>: empty result, no exception."""
    rows = parse_ratios_page(
        html="<html><body><p>no table here</p></body></html>",
        ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/x",
    )
    assert rows == []


def test_parse_html_with_table_but_no_data_rows_returns_empty_list():
    """Header-only table: parser sees the columns but no data rows."""
    html = """
    <html><body>
      <table>
        <thead><tr><th>Metric</th><th>2024</th></tr></thead>
        <tbody></tbody>
      </table>
    </body></html>
    """
    rows = parse_ratios_page(
        html=html, ticker="AAPL", period_type="annual",
        source_url="https://stockanalysis.com/x",
    )
    assert rows == []


def test_parse_invalid_ticker_returns_empty_list():
    """Malformed ticker means downstream validation fails per row;
    parser swallows and returns empty rather than crashing."""
    rows = parse_ratios_page(
        html=ANNUAL_HTML, ticker="not-a-ticker", period_type="annual",
        source_url="https://stockanalysis.com/x",
    )
    assert rows == []
```

- [ ] **Step 6: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_stockanalysis_parser.py -v
```

Expected: `ImportError: cannot import name 'parse_ratios_page'`.

- [ ] **Step 7: Implement `stockanalysis_parser.py`**

Create `src/common/datasources/stockanalysis_parser.py`:

```python
"""Pure HTML parser for stockanalysis.com ratio history pages.

The function `parse_ratios_page` is intentionally I/O-free: it takes a
ready HTML string and returns a list of `RawStockanalysisRatioRow`. All
HTTP, retries, watermarks, and database writes live in
`StockanalysisSource.fetch_ratio_history`.

DOM contract (load-bearing)
---------------------------
stockanalysis.com publishes ratio data in a single `<table>` per page.
Two patterns are observed in the wild:

  1. The table carries a class hint such as `ratios-table` or similar.
  2. The page may render multiple tables; the ratio table is the LARGEST
     by `<tr>` count and always contains the metric-name labels in the
     first column.

This parser is robust to both: it iterates through every `<table>` in the
document, scores each by row-count, picks the largest, and falls back to
empty-list-with-log if no candidate has at least one header row + one
data row.

Header date normalization
-------------------------
Period header labels are normalized to `YYYY-MM-DD` ISO dates:

  * Annual: a 4-digit year `YYYY` -> `YYYY-12-31`. Aligned to the
    calendar fiscal-year end. Companies with off-calendar FYs (Apple,
    Walmart) accept this approximation in v1; materialization layer can
    refine via EDGAR period-end alignment in A.3.8+.
  * Quarterly: `Q1 YYYY` -> `YYYY-03-31`, `Q2 YYYY` -> `YYYY-06-30`,
    `Q3 YYYY` -> `YYYY-09-30`, `Q4 YYYY` -> `YYYY-12-31`.
  * TTM: literal `TTM` -> today's UTC date (scrape moment); if a
    specific period like `Q3 2024` is in the TTM header, that wins.

Metric-name normalization
-------------------------
Metric-name labels are matched against a whitelist that maps the
human-readable HTML label to the canonical Literal in
`RawStockanalysisRatioRow.metric`. Anything not on the whitelist is
skipped silently (so a new metric showing up in a future layout doesn't
trip Pydantic Literal validation). To support a new metric: add an entry
to `_METRIC_LABEL_MAP` AND extend the Literal in schemas.py.

Value parsing
-------------
Per-cell values support three formats:

  * Decimal float like `28.45` -> `28.45`
  * Percentage like `25.31%` -> `0.2531` (decimal)
  * Currency like `$1.50` -> `1.50` (rare in ratio pages but tolerated)
  * Missing sentinels `n/a`, `-`, empty string -> None

Unparseable values become None (the row is still emitted because the
period+metric pair is useful provenance even with a missing value).
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from src.common.schemas import RawStockanalysisRatioRow


_log = logging.getLogger(__name__)


# Human-readable label -> canonical metric name. Case-insensitive lookup
# happens in _normalize_metric_label.
_METRIC_LABEL_MAP = {
    "pe ratio": "pe_ratio",
    "p/e ratio": "pe_ratio",
    "p/e": "pe_ratio",
    "pb ratio": "pb_ratio",
    "p/b ratio": "pb_ratio",
    "p/b": "pb_ratio",
    "ps ratio": "ps_ratio",
    "p/s ratio": "ps_ratio",
    "p/s": "ps_ratio",
    "ev/ebitda": "ev_ebitda",
    "ev / ebitda": "ev_ebitda",
    "dividend yield": "dividend_yield",
    "roe": "roe",
    "return on equity": "roe",
    "roa": "roa",
    "return on assets": "roa",
    "profit margin": "profit_margin",
    "net margin": "profit_margin",
    "operating margin": "operating_margin",
    "fcf yield": "fcf_yield",
    "free cash flow yield": "fcf_yield",
    "current ratio": "current_ratio",
    "debt / equity": "debt_to_equity",
    "debt/equity": "debt_to_equity",
    "debt to equity": "debt_to_equity",
}

_PERCENT_RE = re.compile(r"^\s*(-?[\d,]*\.?\d+)\s*%\s*$")
_CURRENCY_RE = re.compile(r"^\s*\$\s*(-?[\d,]*\.?\d+)\s*$")
_PLAIN_NUMBER_RE = re.compile(r"^\s*(-?[\d,]*\.?\d+)\s*$")

_MISSING_TOKENS = {"", "-", "—", "n/a", "na", "nan", "null"}

_QUARTER_RE = re.compile(r"^\s*Q\s*([1-4])\s+(\d{4})\s*$", re.IGNORECASE)
_YEAR_ONLY_RE = re.compile(r"^\s*(\d{4})\s*$")

_QUARTER_END = {
    1: "03-31",
    2: "06-30",
    3: "09-30",
    4: "12-31",
}


def _normalize_metric_label(label: str) -> Optional[str]:
    """Map a HTML row-label to the canonical metric name, or None if
    the label is not in our whitelist."""
    if not label:
        return None
    key = label.strip().lower()
    # Drop trailing punctuation like a trailing colon.
    key = key.rstrip(":").strip()
    return _METRIC_LABEL_MAP.get(key)


def _normalize_period_header(label: str, *, period_type: str) -> Optional[str]:
    """Map an HTML column-header label to a `YYYY-MM-DD` period-end date.

    Returns None if the header is not parseable (e.g., 'Metric',
    'Fiscal Year', 'Quarter').
    """
    if not label:
        return None
    text = label.strip()
    if not text:
        return None

    # Quarterly: "Q3 2024" -> "2024-09-30"
    m = _QUARTER_RE.match(text)
    if m:
        q = int(m.group(1))
        year = m.group(2)
        return f"{year}-{_QUARTER_END[q]}"

    # Annual: "2024" -> "2024-12-31"
    m = _YEAR_ONLY_RE.match(text)
    if m:
        return f"{m.group(1)}-12-31"

    # TTM: literal "TTM" with no embedded date -> today (UTC).
    if text.upper() == "TTM":
        if period_type == "ttm":
            return _dt.date.today().isoformat()
        return None

    # Anything else (e.g., "Metric", "Fiscal Year", "Quarter") is a
    # non-period header and should not yield a date.
    return None


def _parse_value(cell: str) -> Optional[float]:
    """Parse one HTML cell value to float or None.

    Handles percent ('25.31%' -> 0.2531), currency ('$1.50' -> 1.5),
    plain decimal ('28.45' -> 28.45), and missing-value sentinels
    ('n/a', '-', '', etc.) -> None.
    """
    if cell is None:
        return None
    s = cell.strip()
    if s.lower() in _MISSING_TOKENS:
        return None

    m = _PERCENT_RE.match(s)
    if m:
        try:
            return float(m.group(1).replace(",", "")) / 100.0
        except ValueError:
            return None

    m = _CURRENCY_RE.match(s)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None

    m = _PLAIN_NUMBER_RE.match(s)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None

    return None


def _select_ratio_table(soup: BeautifulSoup):
    """Pick the largest <table> with at least one header row and one
    data row, or return None.

    A table is a candidate if:
      - It has a <thead> OR a first <tr> we can treat as header.
      - It has at least one data row.
    The "ratio table" is whichever candidate has the most rows.
    """
    candidates = []
    for table in soup.find_all("table"):
        all_rows = table.find_all("tr")
        if len(all_rows) < 2:
            continue
        candidates.append((len(all_rows), table))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _extract_header_cells(table) -> list[str]:
    """Pull the column-header labels from the first <tr> (preferring
    one inside <thead> if present)."""
    thead = table.find("thead")
    if thead is not None:
        first_row = thead.find("tr")
    else:
        first_row = table.find("tr")
    if first_row is None:
        return []
    cells = first_row.find_all(["th", "td"])
    return [c.get_text(strip=True) for c in cells]


def _extract_data_rows(table) -> list[list[str]]:
    """Pull all data rows (skipping the header row if it's at the top
    of <tbody> rather than in <thead>)."""
    tbody = table.find("tbody")
    if tbody is not None:
        trs = tbody.find_all("tr")
    else:
        # Fallback: every <tr> except the first.
        all_trs = table.find_all("tr")
        trs = all_trs[1:] if len(all_trs) > 1 else []
    out: list[list[str]] = []
    for tr in trs:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        out.append([c.get_text(strip=True) for c in cells])
    return out


def parse_ratios_page(
    html: str,
    ticker: str,
    period_type: str,
    source_url: str,
    *,
    run_id: str = "",
    scrape_timestamp: Optional[str] = None,
) -> list[RawStockanalysisRatioRow]:
    """Parse a stockanalysis.com ratio-history page into long-format rows.

    Returns an empty list if the HTML structure is unparseable. Logs the
    failure mode at WARNING level. Never raises on layout surprises —
    the caller treats the empty list as a soft failure and records an
    error_count increment on the watermark.

    Parameters
    ----------
    html : the page HTML as a string. Caller supplies; this function does no I/O.
    ticker : ticker symbol; will be uppercased and validated by Pydantic.
    period_type : one of {'annual', 'quarterly', 'ttm'}.
    source_url : the URL the HTML came from; stamped on every emitted row.
    run_id : optional provenance tag.
    scrape_timestamp : ISO-8601 UTC; defaults to now() if not provided.
    """
    if scrape_timestamp is None:
        scrape_timestamp = (
            _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
        )

    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "stockanalysis_parser: BeautifulSoup raised on ticker=%s url=%s: %s",
            ticker, source_url, exc,
        )
        return []

    table = _select_ratio_table(soup)
    if table is None:
        _log.warning(
            "stockanalysis_parser: no <table> found for ticker=%s url=%s",
            ticker, source_url,
        )
        return []

    header_cells = _extract_header_cells(table)
    if len(header_cells) < 2:
        _log.warning(
            "stockanalysis_parser: header row too short (%d cells) for ticker=%s",
            len(header_cells), ticker,
        )
        return []

    # The first header cell is the metric-name column label (e.g.,
    # "Fiscal Year", "Quarter", "Metric"). The remaining cells are the
    # period columns to normalize.
    period_dates: list[Optional[str]] = [None]  # placeholder for col 0
    for h in header_cells[1:]:
        period_dates.append(_normalize_period_header(h, period_type=period_type))

    # If NONE of the period headers parsed, the table isn't a ratio table.
    if not any(p for p in period_dates[1:]):
        _log.warning(
            "stockanalysis_parser: no parseable period headers in %s for ticker=%s",
            header_cells, ticker,
        )
        return []

    data_rows = _extract_data_rows(table)
    if not data_rows:
        _log.warning(
            "stockanalysis_parser: header present but no data rows for ticker=%s",
            ticker,
        )
        return []

    emitted: list[RawStockanalysisRatioRow] = []
    for row_cells in data_rows:
        if len(row_cells) < 2:
            continue
        metric_label = row_cells[0]
        canonical_metric = _normalize_metric_label(metric_label)
        if canonical_metric is None:
            # Not on our whitelist; skip silently.
            continue
        # Each data column corresponds 1:1 with the matching position
        # in period_dates. row_cells[0] is the label; row_cells[i] for
        # i>=1 is the value at period_dates[i].
        for i, val_text in enumerate(row_cells[1:], start=1):
            if i >= len(period_dates):
                break
            period_end_date = period_dates[i]
            if period_end_date is None:
                continue
            value = _parse_value(val_text)
            try:
                emitted.append(
                    RawStockanalysisRatioRow(
                        run_id=run_id,
                        ticker=ticker,
                        metric=canonical_metric,
                        period_end_date=period_end_date,
                        period_type=period_type,
                        value=value,
                        source_url=source_url,
                        scrape_timestamp=scrape_timestamp,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "stockanalysis_parser: row rejected (%s) "
                    "ticker=%s metric=%s period=%s",
                    exc, ticker, canonical_metric, period_end_date,
                )
                # If the ticker itself is invalid, the FIRST row failure
                # signals a fundamental problem — bail out rather than
                # spamming the log with one rejection per row.
                if "ticker" in str(exc).lower():
                    return []
                continue

    return emitted
```

- [ ] **Step 8: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_stockanalysis_parser.py -v
```

Expected: All 18 tests PASS.

- [ ] **Step 9: Commit**

```
git add src/common/datasources/stockanalysis_parser.py tests/fixtures/stockanalysis/aapl_ratios_annual.html tests/fixtures/stockanalysis/aapl_ratios_quarterly.html tests/fixtures/stockanalysis/aapl_ratios_ttm.html tests/fixtures/stockanalysis/malformed.html tests/datasources/test_stockanalysis_parser.py
git commit -m "feat(datasources): stockanalysis_parser - pure HTML -> RawStockanalysisRatioRow with whitelist + sentinel handling"
```

---

## Task 4: `StockanalysisSource.fetch_ratio_history` real implementation

**Files:**
- Modify: `src/common/datasources/stockanalysis_source.py`
- Create: `tests/datasources/test_stockanalysis_fetch.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/datasources/test_stockanalysis_fetch.py`:

```python
"""Tests for StockanalysisSource.fetch_ratio_history (Phase A.3.6)."""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.stockanalysis_source import StockanalysisSource


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "stockanalysis"
ANNUAL_HTML = (FIXTURE_DIR / "aapl_ratios_annual.html").read_text(encoding="utf-8")
QUARTERLY_HTML = (FIXTURE_DIR / "aapl_ratios_quarterly.html").read_text(encoding="utf-8")
TTM_HTML = (FIXTURE_DIR / "aapl_ratios_ttm.html").read_text(encoding="utf-8")


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v8()
    return mgr


def _wire_url_dispatch_mock(mocker, sleep_zero: bool = True):
    """Dispatch mock by URL fragment — the three ratio URLs route to
    the three fixtures. Unknown URLs 404."""
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        if "?p=quarterly" in url:
            resp.status_code = 200
            resp.ok = True
            resp.text = QUARTERLY_HTML
            resp.content = QUARTERLY_HTML.encode()
        elif "?p=trailing" in url:
            resp.status_code = 200
            resp.ok = True
            resp.text = TTM_HTML
            resp.content = TTM_HTML.encode()
        elif "/financials/ratios/" in url:
            # Annual is the bare URL (no ?p=...).
            resp.status_code = 200
            resp.ok = True
            resp.text = ANNUAL_HTML
            resp.content = ANNUAL_HTML.encode()
        else:
            resp.status_code = 404
            resp.ok = False
            resp.text = ""
            resp.content = b""
        return resp
    mocker.patch(
        "src.common.datasources.stockanalysis_source.requests.get",
        side_effect=side_effect,
    )
    if sleep_zero:
        mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")


def test_fetch_ratio_history_persists_annual_quarterly_ttm(db, mocker):
    _wire_url_dispatch_mock(mocker)
    n = StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        period_types = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT period_type FROM raw_stockanalysis_ratios"
            )
        )
    assert period_types == ["annual", "quarterly", "ttm"]


def test_fetch_ratio_history_returns_expected_row_count(db, mocker):
    """Annual: 12 metrics * 5 years = 60.
       Quarterly: 5 metrics * 5 quarters = 25.
       TTM: 10 metrics * 1 column = 10.
       Total: 95.
    """
    _wire_url_dispatch_mock(mocker)
    n = StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n == 95


def test_fetch_ratio_history_records_source_url_per_period_type(db, mocker):
    _wire_url_dispatch_mock(mocker)
    StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    with sqlite3.connect(db.db_path) as c:
        annual_url = c.execute(
            "SELECT DISTINCT source_url FROM raw_stockanalysis_ratios "
            "WHERE period_type='annual'"
        ).fetchone()[0]
        quarterly_url = c.execute(
            "SELECT DISTINCT source_url FROM raw_stockanalysis_ratios "
            "WHERE period_type='quarterly'"
        ).fetchone()[0]
        ttm_url = c.execute(
            "SELECT DISTINCT source_url FROM raw_stockanalysis_ratios "
            "WHERE period_type='ttm'"
        ).fetchone()[0]
    assert "?p=quarterly" in quarterly_url
    assert "?p=trailing" in ttm_url
    # Annual is the bare ratios URL with no query parameter.
    assert "?p=" not in annual_url


def test_fetch_ratio_history_updates_watermark_on_success(db, mocker):
    _wire_url_dispatch_mock(mocker)
    StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    w = db.get_watermark("stockanalysis", "AAPL", "ratio_history")
    assert w is not None
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_fetch_ratio_history_90d_skip_within_window(db, mocker):
    """Watermark within 90 days -> skip with return 0."""
    recent = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
    db.upsert_watermark(
        source="stockanalysis", ticker="AAPL", field="ratio_history",
        last_observation_date=recent, success=True,
    )
    _wire_url_dispatch_mock(mocker)
    n = StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n == 0
    with sqlite3.connect(db.db_path) as c:
        total = c.execute(
            "SELECT COUNT(*) FROM raw_stockanalysis_ratios"
        ).fetchone()[0]
    assert total == 0


def test_fetch_ratio_history_90d_skip_does_not_fire_outside_window(db, mocker):
    """Watermark older than 90 days -> fetch proceeds."""
    old = (_dt.date.today() - _dt.timedelta(days=120)).isoformat()
    db.upsert_watermark(
        source="stockanalysis", ticker="AAPL", field="ratio_history",
        last_observation_date=old, success=True,
    )
    _wire_url_dispatch_mock(mocker)
    n = StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n > 0


def test_fetch_ratio_history_404_records_error(db, mocker):
    """All three URLs 404 -> 0 rows, error_count >= 1 on watermark."""
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 404
        resp.ok = False
        resp.text = ""
        resp.content = b""
        return resp
    mocker.patch(
        "src.common.datasources.stockanalysis_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")
    n = StockanalysisSource().fetch_ratio_history(ticker="BOGUS", run_id="r1", db=db)
    assert n == 0
    w = db.get_watermark("stockanalysis", "BOGUS", "ratio_history")
    assert w is not None
    assert w["error_count"] >= 1


def test_fetch_ratio_history_partial_success(db, mocker):
    """Annual 200, quarterly 404, ttm 200 -> rows for annual + ttm only;
    watermark records error_count >= 1 because at least one URL failed."""
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        if "?p=quarterly" in url:
            resp.status_code = 404
            resp.ok = False
            resp.text = ""
            resp.content = b""
        elif "?p=trailing" in url:
            resp.status_code = 200
            resp.ok = True
            resp.text = TTM_HTML
            resp.content = TTM_HTML.encode()
        else:
            resp.status_code = 200
            resp.ok = True
            resp.text = ANNUAL_HTML
            resp.content = ANNUAL_HTML.encode()
        return resp
    mocker.patch(
        "src.common.datasources.stockanalysis_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")
    n = StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    # 60 annual + 10 ttm = 70
    assert n == 70
    w = db.get_watermark("stockanalysis", "AAPL", "ratio_history")
    assert w is not None
    assert w["error_count"] >= 1


def test_fetch_ratio_history_calls_three_distinct_urls(db, mocker):
    captured: list[str] = []

    def side_effect(url, **kwargs):
        captured.append(url)
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.text = ANNUAL_HTML
        resp.content = ANNUAL_HTML.encode()
        return resp

    mocker.patch(
        "src.common.datasources.stockanalysis_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")
    StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert len(captured) == 3
    assert any(u.endswith("/financials/ratios/") for u in captured)
    assert any("?p=quarterly" in u for u in captured)
    assert any("?p=trailing" in u for u in captured)


def test_fetch_ratio_history_polite_delay_between_calls(db, mocker):
    """time.sleep is invoked twice (between the 3 URL pulls)."""
    _wire_url_dispatch_mock(mocker, sleep_zero=False)
    sleep_spy = mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")
    StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    # 3 URLs -> 2 inter-URL sleeps.
    assert sleep_spy.call_count >= 2
    for call in sleep_spy.call_args_list:
        # Each call uses the configured polite delay (1.0s in source).
        assert call.args[0] >= 1.0


def test_fetch_ratio_history_idempotent_within_same_window(db, mocker):
    """First call fetches & writes; second call within 90d skips."""
    _wire_url_dispatch_mock(mocker)
    src = StockanalysisSource()
    n1 = src.fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n1 == 95
    n2 = src.fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n2 == 0


def test_fetch_ratio_history_insert_or_ignore_protects_against_dup(db, mocker):
    """After clearing watermark, the second pull writes 0 net new rows."""
    _wire_url_dispatch_mock(mocker)
    src = StockanalysisSource()
    n1 = src.fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n1 == 95
    # Backdate the watermark to force a re-pull.
    old = (_dt.date.today() - _dt.timedelta(days=200)).isoformat()
    db.upsert_watermark(
        source="stockanalysis", ticker="AAPL", field="ratio_history",
        last_observation_date=old, success=True,
    )
    n2 = src.fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n2 == 0
    with sqlite3.connect(db.db_path) as c:
        total = c.execute(
            "SELECT COUNT(*) FROM raw_stockanalysis_ratios"
        ).fetchone()[0]
    assert total == 95


def test_fetch_ratio_history_malformed_html_still_records_error(db, mocker):
    """Server returns 200 but with junk HTML -> parser returns []; watermark
    records error_count >= 1."""
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.text = "<html><body><p>oops</p></body></html>"
        resp.content = resp.text.encode()
        return resp
    mocker.patch(
        "src.common.datasources.stockanalysis_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")
    n = StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n == 0
    w = db.get_watermark("stockanalysis", "AAPL", "ratio_history")
    assert w is not None
    assert w["error_count"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_stockanalysis_fetch.py -v
```

Expected: `AttributeError` on `fetch_ratio_history`.

- [ ] **Step 3: Implement `fetch_ratio_history` in `StockanalysisSource`**

Replace the contents of `src/common/datasources/stockanalysis_source.py` with:

```python
"""StockanalysisSource — adapter for stockanalysis.com.

Used primarily for multi-year ratio history (P/E, EV/EBITDA, P/B, P/S,
dividend yield, ROE, ROA, profit/operating margins, FCF yield, current
ratio, debt/equity) needed to compute the `pe_5y_percentile` and
`ev_ebitda_5y_percentile` features in `canonical_universe` (A.3.10).

A.1 deliverable: health_check via a known stable URL.
A.3.6 deliverable: fetch_ratio_history — pulls three ratio pages
(annual + quarterly + trailing/TTM), parses each via the pure
`stockanalysis_parser.parse_ratios_page`, persists rows to
`raw_stockanalysis_ratios` via INSERT OR IGNORE, and updates the
per-ticker watermark `(stockanalysis, <ticker>, ratio_history)`.

URL pattern (verified from live pages)
--------------------------------------
  Annual:    https://stockanalysis.com/stocks/<ticker>/financials/ratios/
  Quarterly: https://stockanalysis.com/stocks/<ticker>/financials/ratios/?p=quarterly
  TTM:       https://stockanalysis.com/stocks/<ticker>/financials/ratios/?p=trailing

If these change in the future, only the URL-format constants below need
to be updated — the parser is URL-pattern-independent.

90-day refresh-skip semantics
-----------------------------
Ratios update with each quarterly earnings release, so refreshing more
often than ~90 days is bandwidth-and-politeness waste. Per spec §6.6,
the watermark `(stockanalysis, <ticker>, ratio_history)` short-circuits
the fetch if the previous successful pull is younger than 90 days.
Caller can force a refresh by clearing the watermark.

Polite delay
------------
1.0 second between each of the three URL pulls per ticker (annual ->
quarterly -> TTM). stockanalysis.com is a static content site without a
documented rate limit, but bursty behavior is rude and risks soft IP
limiting.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)
from src.common.datasources.stockanalysis_parser import parse_ratios_page


_log = logging.getLogger(__name__)


_PROBE_URL = "https://stockanalysis.com/stocks/aapl/financials/ratios/"
_ANNUAL_URL_FMT = "https://stockanalysis.com/stocks/{ticker_lower}/financials/ratios/"
_QUARTERLY_URL_FMT = (
    "https://stockanalysis.com/stocks/{ticker_lower}/financials/ratios/?p=quarterly"
)
_TTM_URL_FMT = (
    "https://stockanalysis.com/stocks/{ticker_lower}/financials/ratios/?p=trailing"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Trade Identifier; research@example.com)"
    ),
}

# Polite inter-URL delay (seconds) when pulling annual -> quarterly -> ttm.
_INTER_URL_DELAY_SEC = 1.0

# Per spec §6.6: refresh-skip window for the ratio_history watermark.
_REFRESH_SKIP_DAYS = 90

# HTTP timeout per fetch (seconds).
_HTTP_TIMEOUT_SEC = 15


class StockanalysisSource(BaseDataSource):
    name = "stockanalysis"
    cadence = "weekly"
    provides = {
        "pe_ttm", "pe_forward",
        "ebit_ttm", "fcf_ttm",
        "operating_margin", "net_profit_margin",
        "roe", "roic",
        "pe_5y_history_raw",
        "ev_ebitda_5y_history_raw",
    }

    # ------------------------------------------------------------------
    # A.1: health_check (unchanged)
    # ------------------------------------------------------------------

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            resp = requests.get(_PROBE_URL, headers=_HEADERS, timeout=10)
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
    # A.3.6: ratio history fetch
    # ------------------------------------------------------------------

    def _build_urls(self, ticker: str) -> list[tuple[str, str]]:
        """Return a list of (period_type, url) for the three pages we pull."""
        t = ticker.lower()
        return [
            ("annual", _ANNUAL_URL_FMT.format(ticker_lower=t)),
            ("quarterly", _QUARTERLY_URL_FMT.format(ticker_lower=t)),
            ("ttm", _TTM_URL_FMT.format(ticker_lower=t)),
        ]

    def _http_get(self, url: str) -> Optional[str]:
        """Fetch one URL with a UA header. Returns text on 200, or None
        on any failure (HTTP error, network error). Caller treats None
        as a soft per-URL failure."""
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001
            _log.warning("stockanalysis http error url=%s: %s", url, exc)
            return None
        if not resp.ok:
            _log.warning(
                "stockanalysis http non-200 url=%s status=%s",
                url, resp.status_code,
            )
            return None
        return resp.text

    def fetch_ratio_history(
        self,
        ticker: str,
        run_id: str,
        db=None,
        *,
        refresh_after_days: int = _REFRESH_SKIP_DAYS,
    ) -> int:
        """Pull annual + quarterly + TTM ratio pages for one ticker.

        Pipeline:
          0. Check watermark (stockanalysis, <ticker>, ratio_history).
             If the previous successful pull is within `refresh_after_days`,
             return 0 immediately.
          1. For each (period_type, url) in (annual, quarterly, ttm):
               a. HTTP GET with polite 1.0s inter-URL delay
               b. parse_ratios_page(html, ticker, period_type, url) ->
                  list[RawStockanalysisRatioRow]
               c. db.insert_raw_stockanalysis_ratios(rows)
               d. Track per-URL success/failure
          2. Update watermark — if ALL three URLs succeeded, success=True;
             if any URL failed (network, HTTP, parse), success=False and
             error_message records which URLs failed.

        Returns
        -------
        int : net new rows inserted across all three URLs.
        """
        if db is None:
            raise ValueError("db is required")

        field = "ratio_history"
        ticker = ticker.strip().upper()

        # 0. 90-day refresh-skip
        w = db.get_watermark(self.name, ticker, field)
        if w and w.get("last_observation_date"):
            try:
                last = _dt.date.fromisoformat(w["last_observation_date"])
                age_days = (_dt.date.today() - last).days
                if age_days < refresh_after_days and w.get("error_count", 0) == 0:
                    # Recently and cleanly refreshed; skip.
                    return 0
            except (TypeError, ValueError):
                pass

        scrape_ts = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )

        urls = self._build_urls(ticker)
        total_inserted = 0
        url_errors: list[str] = []
        import sqlite3

        for i, (period_type, url) in enumerate(urls):
            if i > 0:
                time.sleep(_INTER_URL_DELAY_SEC)

            html = self._http_get(url)
            if html is None:
                url_errors.append(f"http:{period_type}")
                continue

            rows = parse_ratios_page(
                html=html,
                ticker=ticker,
                period_type=period_type,
                source_url=url,
                run_id=run_id,
                scrape_timestamp=scrape_ts,
            )
            if not rows:
                url_errors.append(f"parse:{period_type}")
                continue

            # Count net new rows by before/after.
            with sqlite3.connect(db.db_path) as c:
                n_before = c.execute(
                    "SELECT COUNT(*) FROM raw_stockanalysis_ratios "
                    "WHERE ticker=? AND period_type=?",
                    (ticker, period_type),
                ).fetchone()[0]
            db.insert_raw_stockanalysis_ratios(rows)
            with sqlite3.connect(db.db_path) as c:
                n_after = c.execute(
                    "SELECT COUNT(*) FROM raw_stockanalysis_ratios "
                    "WHERE ticker=? AND period_type=?",
                    (ticker, period_type),
                ).fetchone()[0]
            total_inserted += (n_after - n_before)

        # Watermark advance.
        if url_errors:
            self.update_watermark(
                ticker=ticker, field=field,
                last_observation_date=_dt.date.today(),
                db=db, success=False,
                error_message="; ".join(url_errors),
            )
        else:
            self.update_watermark(
                ticker=ticker, field=field,
                last_observation_date=_dt.date.today(),
                db=db, success=True,
            )

        return total_inserted
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_stockanalysis_fetch.py -v
```

Expected: All 13 tests PASS.

- [ ] **Step 5: Verify A.1 StockanalysisSource.health_check still works (no regression)**

```
./venv/Scripts/python.exe -m pytest tests/datasources -k "stockanalysis and health" -v 2>&1 | tail -5
```

Expected: any pre-existing stockanalysis health-check tests still pass.

- [ ] **Step 6: Commit**

```
git add src/common/datasources/stockanalysis_source.py tests/datasources/test_stockanalysis_fetch.py
git commit -m "feat(datasources): StockanalysisSource.fetch_ratio_history - 3-URL pull with 90d refresh skip + parser delegation"
```

---

## Task 5: Integration test (end-to-end mocked HTTP)

**Files:**
- Create: `tests/datasources/test_integration_a3_6.py`

- [ ] **Step 1: Write the integration test**

Create `tests/datasources/test_integration_a3_6.py`:

```python
"""End-to-end orchestration test for A.3.6:
- Full happy path: 3 URLs hit, all parse, raw_stockanalysis_ratios populated,
  watermark advanced.
- Idempotent re-pull within 90 days: zero new rows.
- Mixed failure: annual 200, quarterly 404, ttm 200. The two surviving URLs
  still write; watermark records partial-failure error_count.

All HTTP is mocked via a SINGLE side_effect on
`src.common.datasources.stockanalysis_source.requests.get` (URL-dispatch
pattern, NOT one patch per URL). This pattern is the explicit lesson from
A.3.5 Wave 3 — two `mocker.patch` calls competing for the same target
broke the test there.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.stockanalysis_source import StockanalysisSource


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "stockanalysis"
ANNUAL_HTML = (FIXTURE_DIR / "aapl_ratios_annual.html").read_text(encoding="utf-8")
QUARTERLY_HTML = (FIXTURE_DIR / "aapl_ratios_quarterly.html").read_text(encoding="utf-8")
TTM_HTML = (FIXTURE_DIR / "aapl_ratios_ttm.html").read_text(encoding="utf-8")


def _make_url_dispatch(mocker, *, quarterly_404: bool = False):
    """URL-dispatch mock — single side_effect routes by URL substring.

    This is the lesson from A.3.5 Wave 3: do NOT patch the same target
    twice with different side effects; the second patch wins and the
    first becomes dead code. Instead, one mock, one side_effect, branch
    inside.
    """
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        if "?p=quarterly" in url:
            if quarterly_404:
                resp.status_code = 404
                resp.ok = False
                resp.text = ""
                resp.content = b""
            else:
                resp.status_code = 200
                resp.ok = True
                resp.text = QUARTERLY_HTML
                resp.content = QUARTERLY_HTML.encode()
        elif "?p=trailing" in url:
            resp.status_code = 200
            resp.ok = True
            resp.text = TTM_HTML
            resp.content = TTM_HTML.encode()
        elif "/financials/ratios/" in url:
            resp.status_code = 200
            resp.ok = True
            resp.text = ANNUAL_HTML
            resp.content = ANNUAL_HTML.encode()
        else:
            resp.status_code = 404
            resp.ok = False
            resp.text = ""
            resp.content = b""
        return resp

    mocker.patch(
        "src.common.datasources.stockanalysis_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")


def test_a3_6_full_flow_writes_three_period_types(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v8()

    _make_url_dispatch(mocker)

    src = StockanalysisSource()
    n = src.fetch_ratio_history(ticker="AAPL", run_id="run-1", db=db)
    # 60 annual + 25 quarterly + 10 ttm = 95
    assert n == 95

    with sqlite3.connect(db_path) as c:
        by_pt = {
            r[0]: r[1] for r in c.execute(
                "SELECT period_type, COUNT(*) FROM raw_stockanalysis_ratios GROUP BY period_type"
            )
        }
    assert by_pt == {"annual": 60, "quarterly": 25, "ttm": 10}

    w = db.get_watermark("stockanalysis", "AAPL", "ratio_history")
    assert w is not None
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_a3_6_idempotent_rerun_within_90d_skips(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v8()

    _make_url_dispatch(mocker)
    src = StockanalysisSource()

    # First pull lands 95 rows.
    n1 = src.fetch_ratio_history(ticker="AAPL", run_id="run-1", db=db)
    assert n1 == 95

    # Re-pull immediately: the 90d skip fires, returning 0.
    n2 = src.fetch_ratio_history(ticker="AAPL", run_id="run-1", db=db)
    assert n2 == 0

    # Confirm no rows added between the two calls.
    with sqlite3.connect(db_path) as c:
        total = c.execute(
            "SELECT COUNT(*) FROM raw_stockanalysis_ratios"
        ).fetchone()[0]
    assert total == 95


def test_a3_6_partial_failure_records_error_but_writes_surviving_urls(
    tmp_path: Path, mocker,
):
    """Quarterly URL 404s; annual + ttm succeed. Watermark records
    partial failure (error_count >= 1, success=False), but the surviving
    period_types still land in raw_stockanalysis_ratios."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v8()

    _make_url_dispatch(mocker, quarterly_404=True)
    src = StockanalysisSource()
    n = src.fetch_ratio_history(ticker="AAPL", run_id="run-1", db=db)

    # 60 annual + 0 quarterly + 10 ttm = 70
    assert n == 70

    with sqlite3.connect(db_path) as c:
        period_types = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT period_type FROM raw_stockanalysis_ratios"
            )
        )
    assert period_types == ["annual", "ttm"]

    w = db.get_watermark("stockanalysis", "AAPL", "ratio_history")
    assert w is not None
    assert w["error_count"] >= 1
    assert "quarterly" in (w.get("error_message") or "")
```

- [ ] **Step 2: Run the integration test**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_integration_a3_6.py -v
```

Expected: 3 PASS.

- [ ] **Step 3: Run the full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" -v 2>&1 | tail -5
```

Expected: ~427 passed (370 baseline + 11 schema + 12 database + 18 parser + 13 fetch + 3 integration_a3_6 — counts approximate; ±2 is within tolerance).

- [ ] **Step 4: Commit**

```
git add tests/datasources/test_integration_a3_6.py
git commit -m "test: A.3.6 integration acceptance - end-to-end ratio-history scrape with 90d skip + partial-failure handling"
```

---

## Task 6: Update the build plan

**Files:**
- Modify: the build plan

- [ ] **Step 1: Locate the A.3 row in §5.1.0**

Grep the build plan for `**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 shipped 2026-05-21**` to find the line.

- [ ] **Step 2: Update the A.3 row's shipped marker**

Use `Edit` to replace the substring `**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 shipped 2026-05-21**` with `**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 + A.3.6 shipped 2026-05-21**`, and update the plan-link parenthetical to add the A.3.6 plan file plus bump the schema marker from v7 to v8. Concretely, find this fragment:

```
**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 shipped 2026-05-21** (plans: [A.3.1](docs/design/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/design/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md), [A.3.3](docs/design/plans/2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md), [A.3.4](docs/design/plans/2026-05-21-phase-a3-sub04-edgar-insider-and-filings.md), [A.3.5](docs/design/plans/2026-05-21-phase-a3-sub05-fred-finra.md)): schema v7
```

Replace with:

```
**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 + A.3.6 shipped 2026-05-21** (plans: [A.3.1](docs/design/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/design/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md), [A.3.3](docs/design/plans/2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md), [A.3.4](docs/design/plans/2026-05-21-phase-a3-sub04-edgar-insider-and-filings.md), [A.3.5](docs/design/plans/2026-05-21-phase-a3-sub05-fred-finra.md), [A.3.6](docs/design/plans/2026-05-21-phase-a3-sub06-stockanalysis-ratios.md)): schema v8
```

Then in the same sentence, append the A.3.6 fragment after the existing A.3.5 fragment:

```
 + StockanalysisSource.fetch_ratio_history (3-URL HTML scrape of stockanalysis.com annual + quarterly + TTM ratio pages, BeautifulSoup parser with whitelist + sentinel handling, 90d refresh skip per spec §6.6) + raw_stockanalysis_ratios table (PK `(ticker, metric, period_end_date)`).
```

- [ ] **Step 3: Commit**

```
git add <build-plan>
git commit -m "docs: mark A.3.6 shipped - stockanalysis.com 10y ratio history scraper"
```

---

## Phase A.3.6 — Definition of Done

- [ ] `./venv/Scripts/python.exe -m pytest -m "not integration"` shows ~427 tests passing
- [ ] `migrate_to_v8()` produces `schema_version == 8`
- [ ] `raw_stockanalysis_ratios` table exists with PK `(ticker, metric, period_end_date)` and columns `run_id, ticker, metric, period_end_date, period_type, value, source_url, scrape_timestamp`
- [ ] `insert_raw_stockanalysis_ratios()` uses INSERT OR IGNORE (re-runs idempotent; historical buckets immutable in v1)
- [ ] `RawStockanalysisRatioRow.metric` is constrained to the 12-metric Literal whitelist
- [ ] `RawStockanalysisRatioRow.period_type` is constrained to `{annual, quarterly, ttm}` Literal
- [ ] `RawStockanalysisRatioRow.value` accepts None (n/a / `-` / blank from the HTML map cleanly)
- [ ] `RawStockanalysisRatioRow.ticker` is uppercased + matches `_TICKER_RE`
- [ ] `stockanalysis_parser.parse_ratios_page` is a pure function (no I/O, no DB) and returns `list[RawStockanalysisRatioRow]`
- [ ] Parser returns `[]` (not raises) on: empty input, no `<table>`, no parseable period headers, all-rejected metric labels
- [ ] Parser normalizes percentage cells `25.31%` → `0.2531`
- [ ] Parser normalizes currency cells `$1.50` → `1.50`
- [ ] Parser normalizes annual header `YYYY` → `YYYY-12-31`
- [ ] Parser normalizes quarterly header `Q3 YYYY` → `YYYY-09-30` (and equivalent for Q1/Q2/Q4)
- [ ] Parser falls back to today's UTC date for literal `TTM` headers
- [ ] Parser skips unsupported metric labels silently (does not raise on unknown rows)
- [ ] `StockanalysisSource.fetch_ratio_history()` pulls all three URLs (annual + quarterly + ttm)
- [ ] `StockanalysisSource.fetch_ratio_history()` skips re-pull within 90 days of a previous *clean* (error_count == 0) watermark
- [ ] `StockanalysisSource.fetch_ratio_history()` re-pulls after 90 days OR when the previous attempt had errors
- [ ] `StockanalysisSource.fetch_ratio_history()` records partial failure: surviving URLs still write; watermark `error_count >= 1` with `error_message` naming the failed `period_type`
- [ ] `StockanalysisSource.fetch_ratio_history()` adds a polite 1.0s delay between each of the three URL pulls
- [ ] `StockanalysisSource.fetch_ratio_history()` accepts `refresh_after_days` kwarg for callers who want a tighter window
- [ ] No live network in unit tests (all HTTP mocked via pytest-mock with a SINGLE `requests.get` patch using a URL-dispatch side_effect — the A.3.5 Wave 3 lesson)
- [ ] `beautifulsoup4==4.12.3` pinned in `requirements.txt`
- [ ] The build plan marks A.3.6 shipped (schema v8)
- [ ] No A.1 / A.2 / A.3.1 / A.3.2 / A.3.3 / A.3.4 / A.3.5 regressions (`health_check`, all prior `raw_*` tables, watermarks)
- [ ] Git log shows ~6 task commits (plus optional Task-0 `requirements.txt` commit)

---

## Self-review

**Spec coverage:**

| A.3 spec section | A.3.6 task |
|---|---|
| §6.6 Target: `stockanalysis.com/stocks/<ticker>/financials/ratios/` + `?p=quarterly` (+ `?p=trailing`) | Task 4 (`_ANNUAL_URL_FMT`, `_QUARTERLY_URL_FMT`, `_TTM_URL_FMT`) |
| §6.6 HTML parser extracts 10y P/E, EV/EBITDA, P/B, P/S history | Task 3 (`parse_ratios_page` + 12-metric whitelist) |
| §6.6 Long-format `raw_stockanalysis(ticker, metric, period, value, scrape_timestamp)` | Task 1 + Task 2 (`RawStockanalysisRatioRow` + `raw_stockanalysis_ratios` DDL) |
| §6.6 Used by materialization to compute `pe_5y_percentile`, `ev_ebitda_5y_percentile` | A.3.10 consumer (out of scope for this sub-phase; long-format substrate ready) |
| §6.6 Parser failures → log + ticker-row marked partial | Task 3 (parser returns `[]` and logs WARNING; Task 4 records partial failure on watermark with `error_count >= 1` and `error_message` naming failed period_type) |
| §6.6 Watermark per (ticker, `ratio_history`); refresh quarterly | Task 4 (`_REFRESH_SKIP_DAYS = 90`, watermark-based short-circuit) |
| Principle 5 no silent overwrite | Task 2 (INSERT OR IGNORE on `raw_stockanalysis_ratios`) |
| Principle 6 watermarks | Task 4 (per-ticker `ratio_history` watermark) |

**Out of scope** (explicitly deferred):

- A.3.10 materialization that converts `raw_stockanalysis_ratios` into `pe_5y_percentile` / `ev_ebitda_5y_percentile` in `canonical_universe`. The long-format table is ready; the wide-pivot + percentile compute lives in `materialization.py` later.
- Cross-validation of stockanalysis-derived ratios against EDGAR XBRL fundamentals → A.3.8+. The two substrates can be joined on `(ticker, period_end_date)` for sanity checks; this is a methodology refinement and not a Layer-1 ingest concern.
- Fiscal-year-end refinement for off-calendar fiscal years (Apple: September, Walmart: January, etc.) → A.3.8+. The v1 approximation maps every annual `YYYY` to `YYYY-12-31` consistently across all tickers; this consistency is sufficient for own-history percentile compute.
- Live integration test against the real stockanalysis.com pages → A.5. The unit-level integration test in Task 5 uses mocked HTTP per the project's "no live network in unit tests" rule.
- OpenBB router (§6.7) → A.3.7. Independent sub-phase.
- StockanalysisSource integration into the materialization adapter precedence chain → A.3.8+. The methodology spec lists stockanalysis as the second-choice source for fundamentals after EDGAR; the precedence-chain wiring lives in materialization, not in the source.

**Placeholder scan:** No "TBD", "TODO", or "implement later" in any code block. Every step contains full source.

**Pydantic v2 gotchas avoided:**

- `RawStockanalysisRatioRow.value` is `Optional[float] = None` so HTML sentinels (`n/a`, `-`, blank) map cleanly to NULL.
- `metric` and `period_type` use Pydantic `Literal[...]` to catch typos at validation time rather than silently corrupting the raw store.
- `ticker` uses `@field_validator(mode="before")` to upper-case before the regex check; same pattern as `ticker` from A.3.3 / A.3.4 / A.3.5.
- `period_end_date` is a string field (not a `date`) — SQLite stores ISO dates as TEXT, and the materialization layer parses them on demand. This avoids the `pydantic.types.date` + SQLite type-affinity friction we hit in earlier sub-phases.
- The Literal alias types `_StockanalysisMetric` and `_StockanalysisPeriodType` are module-private (underscore prefix) so they aren't part of the public `schemas` API surface; only `RawStockanalysisRatioRow` is exported.

**HTML parsing strategy (load-bearing):**

The parser uses **stdlib `html.parser` via BeautifulSoup**, not `lxml`. Rationale:

1. **Dependency surface.** `lxml` is a C extension with platform-specific wheels; `html.parser` ships with Python. For a project that may need to bootstrap on a fresh Windows / Linux / macOS VM with `pip install -r requirements.txt`, fewer C deps means more robust setup.
2. **Page size.** stockanalysis.com ratio pages are small (low tens-of-kilobytes); `html.parser` performance is fine.
3. **Fragility tolerance.** Both backends parse the same DOM; the choice doesn't affect parser correctness. If we ever need XPath we can swap to `lxml` without changing the public `parse_ratios_page` signature.

The parser's selector strategy is **"largest table wins"**, not a CSS-class lookup. Rationale:

1. stockanalysis.com may rename their CSS class (`ratios-table` → something else) at any time. A class-name selector would silently start returning empty.
2. Iterating all `<table>` elements and picking the one with the most rows is robust to renames AND to additional decorative tables on the page.
3. The downside is that if stockanalysis.com starts rendering a larger table on the page that ISN'T the ratios table (unlikely but possible), the parser would pick the wrong one. We accept this risk in v1 and document it; if it bites, we add a class-name preference filter as a tie-breaker.

The metric-name whitelist `_METRIC_LABEL_MAP` accepts MULTIPLE label variants per canonical name (e.g., both `PE Ratio` and `P/E Ratio` map to `pe_ratio`). This is defensive against minor copy edits on the vendor side.

**Date normalization strategy (load-bearing):**

We deliberately normalize ALL annual-period columns to the calendar fiscal-year end (`YYYY-12-31`), even for companies with non-December fiscal years (Apple: late September; Walmart: late January). Rationale:

1. **Cross-ticker comparability.** The downstream A.3.10 percentile computation operates on a window of `(ticker, period_end_date)` pairs. If Apple's 2024 row had `2024-09-30` and Microsoft's 2024 row had `2024-06-30`, the materialization layer would need fiscal-calendar awareness to align them. Storing everything to a consistent calendar-aligned date sidesteps that.
2. **Vendor approximation.** stockanalysis.com itself reports annual ratios as `2024` (no day-precision); they're already doing the same approximation in their UI. We mirror their convention.
3. **Refinement path.** If A.3.8+ methodology work wants true fiscal-period alignment, the materialization layer can join `raw_stockanalysis_ratios` against `raw_edgar_fundamentals` on `(ticker, year)` to map the calendar key to the true fiscal end-date. The raw store is just the substrate; alignment is a higher-layer concern.

Quarterly normalization uses calendar quarter-ends (`Q1 → 03-31`, `Q2 → 06-30`, etc.) for the same comparability reason.

TTM normalization falls back to the scrape date because TTM is, by definition, a moving window with no fixed period-end. The materialization layer treats TTM rows as "current value" rather than as part of a historical series.

**90-day refresh-skip semantics:**

Per spec §6.6, ratio history updates with each new quarterly earnings release. The `_REFRESH_SKIP_DAYS = 90` constant gates the auto-refresh path:

- **No watermark** → fetch (first-time pull).
- **Watermark older than 90 days** → fetch.
- **Watermark within last 90 days AND error_count == 0** → skip (return 0).
- **Watermark within last 90 days AND error_count > 0** → fetch (last attempt failed; try again).

The "previous attempt failed → bypass the skip" rule is important for resilience: a transient 502 on a single URL shouldn't lock that ticker out of refresh for 90 days.

Caller can also force a refresh in two ways:
1. Pass `refresh_after_days=0` to disable the skip entirely.
2. Clear the watermark directly via `db.upsert_watermark(..., last_observation_date=None, ...)`.

**Architecture risk: HTML structure drift**

The single largest production risk in A.3.6 is stockanalysis.com restructuring their page. Two specific failure modes:

1. **Table tagging change.** If they switch from a real `<table>` to a `<div>`-based grid, the parser's `find_all('table')` returns empty and we record parse-failure on the watermark. The 12-row-per-ticker error rate would be visible in the source health dashboard. Mitigation: the parser logs the failure mode at WARNING level, and the watermark's `error_count` tracks accumulation; an A.5+ monitoring layer can alert on rising error rates per source.
2. **Header label change.** If they rename `PE Ratio` to `Price to Earnings`, the metric-whitelist lookup returns None and the row is silently skipped. The whitelist accepts multiple label variants per canonical name to cushion against minor renames. For larger renames the executing agent extends `_METRIC_LABEL_MAP` and adds a regression fixture.

The `malformed.html` fixture in Task 3 directly exercises the no-table failure path so we know the parser doesn't crash.

**Architecture risk: vendor TOS / robots.txt**

stockanalysis.com's robots.txt is permissive at the page level we're fetching, and the polite 1.0-second inter-URL delay keeps us well below burst thresholds. Three URLs per ticker × the eventual ~500-ticker universe = 1500 requests per refresh cycle, spread over the polite-delay window. At 90-day cadence per ticker that's ~16 requests/day average against stockanalysis.com — orders of magnitude below any reasonable rate ceiling.

If stockanalysis ever tightens robots.txt or starts gating with Cloudflare bot-detection, the source degrades to error-only and the materialization layer's adapter chain falls back to EDGAR (precedence chain set in A.3.8+). The Layer-1 design's adapter-chain pattern (spec §6) is exactly the resilience we need for vendor risk.

**Test coverage shape:**

| Layer | Unit tests | Integration tests |
|---|---|---|
| Schemas (`RawStockanalysisRatioRow`) | 11 | — |
| DB migration + helpers | 12 | — |
| `stockanalysis_parser.parse_ratios_page` | 18 | — |
| `StockanalysisSource.fetch_ratio_history` | 13 | — |
| End-to-end orchestration | — | 3 |
| **Total new tests** | **54** | **3** |

(Total counts are approximate — the codebase has historically run ±2 from the planned figure due to parametrize-collection idiosyncrasies; that variance is acceptable per the project's known-pattern note.)

**Architectural notes:**

- `stockanalysis_parser.parse_ratios_page` is a **module-level pure function**, not a method on `StockanalysisSource`. This matches the A.3.3 + A.3.4 pattern where `edgar_xbrl_parser.py`, `edgar_form4_parser.py`, and `edgar_submissions_parser.py` were extracted from `EdgarSource`. The pure-function form gives us a sharp unit-test boundary (no DB / no HTTP / no time mocking required to exercise parsing) and makes parser regressions trivial to reproduce — paste failing HTML, call the function, assert.
- `StockanalysisSource` does NOT use `tenacity` retries in v1. The 90-day refresh-skip + per-URL error tracking means a single transient failure simply records on the watermark and the next refresh cycle retries the failed URL. If we observe persistent transient errors in A.5 acceptance testing, wrapping `_http_get` with the same `tenacity` policy as `EdgarSource._sec_get_with_retry` is a one-line change.
- The watermark uses `ticker=<ticker>` (per-ticker) with `field="ratio_history"` — distinct from FRED's `(fred, *, series:<id>)` global form because stockanalysis history is ticker-scoped (every ticker has its own page, refresh cadence, and failure surface).
- `StockanalysisSource._http_get` returns `None` on failure rather than raising. This lets the per-URL loop in `fetch_ratio_history` continue with the remaining URLs cleanly. It also keeps the public signature of `fetch_ratio_history` exception-free for ordinary failure modes (caller doesn't need to wrap in try/except just for an HTTP 404 on one URL).
- The `total_inserted` accounting uses `n_after - n_before` per period_type because `db.insert_raw_stockanalysis_ratios()` uses `executemany` and SQLite's `executemany` doesn't expose per-row INSERT OR IGNORE conflict counts. The before/after counts give us a clean net-new-rows number for caller bookkeeping.

**Architecture risk: TTM date fallback**

For literal `TTM` headers, the parser falls back to `_dt.date.today().isoformat()` as the `period_end_date`. This creates a SUBTLE issue: if the same ticker is scraped on two different days, the two TTM rows have different `period_end_date` keys, so INSERT OR IGNORE does NOT de-dupe them.

In practice this is benign because:
1. The 90-day refresh-skip means we don't re-scrape within a day.
2. The materialization layer, when computing TTM-derived features, uses the most recent `period_end_date` per `(ticker, metric, period_type='ttm')` — so multiple historical TTM observations are FINE (they're just one-per-quarter snapshots, which is informationally similar to quarterly data).

If we later want strict TTM idempotency, the fix is to detect when the TTM page header contains a parseable quarter date (e.g., `Q3 2024 TTM`) and use that quarter's calendar end-date instead. The current parser does this opportunistically: `_normalize_period_header` first tries the Q-pattern, then the year-only pattern, and only falls back to today for the literal `TTM` case. The synthetic fixture in Task 3 exercises the literal-`TTM` fallback explicitly.

---

## Execution Handoff

**Recommended:** Subagent-driven, mirroring the A.3.5 wave pattern.

- **Wave 1 (Pre-flight + Schema + DB):** One sub-agent does Tasks 0–2 (verify `beautifulsoup4`, optionally install + pin; add `RawStockanalysisRatioRow`; add `migrate_to_v8` + `insert_raw_stockanalysis_ratios`). All in `requirements.txt` + `src/common/`; sequential. **~12 min**.
- **Wave 2 (Parser + Source):** Run Tasks 3 and 4 either sequentially in one sub-agent (recommended — Task 4 imports `parse_ratios_page` from Task 3, so the file ordering matters) or in two sub-agents IF the executing harness can guarantee Task 3's file is written before Task 4's tests run. **~25 min sequential**.
- **Wave 3 (Integration + build plan):** One sub-agent does Task 5 (integration test) inline with Task 6 (build plan update). **~10 min**.

Total: 3 waves, ~45–55 min wall-clock.

**Critical pre-execution checks for the executing agent:**

1. After Task 0 (or after Task 0's optional `requirements.txt` commit), confirm `bs4` is importable from the venv:
   ```
   ./venv/Scripts/python.exe -c "import bs4; print(bs4.__version__)"
   ```
   Expect `4.12.3` (or compatible).

2. After Task 2, confirm `schema_version == 8` before proceeding:
   ```
   ./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager('data/_v8_check.db'); m.migrate_to_v8(); print(m.get_schema_version())"
   ```
   Expect `8`.

3. After Task 3, confirm the parser handles the four fixtures correctly:
   ```
   ./venv/Scripts/python.exe -c "from pathlib import Path; from src.common.datasources.stockanalysis_parser import parse_ratios_page; html=Path('tests/fixtures/stockanalysis/aapl_ratios_annual.html').read_text(); rows=parse_ratios_page(html, 'AAPL', 'annual', 'http://x'); print('annual rows:', len(rows))"
   ```
   Expect `annual rows: 60`.

4. Do NOT modify `StockanalysisSource.health_check()` — only add the new `fetch_ratio_history` method and supporting helpers. The A.1 regression tests are part of the safety net.

5. **A.3.5 Wave 3 lesson — single `mocker.patch` per target.** When mocking HTTP in the integration test (Task 5), use a SINGLE `mocker.patch("src.common.datasources.stockanalysis_source.requests.get", ...)` call with a URL-dispatch `side_effect` that branches by URL substring. Do NOT use two `mocker.patch` calls on the same target with different side effects — pytest-mock's second patch wins and the first becomes dead code. This bit A.3.5 Wave 3 and is the explicit reason Task 5 of this plan uses a single dispatcher.

6. The `time.sleep(_INTER_URL_DELAY_SEC)` call in `fetch_ratio_history` will add `1.0s × 2 = 2s` of inter-URL delay to test runtime per `fetch_ratio_history` call. The tests all patch `time.sleep` to a no-op via `mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")`. If the executing agent moves the import of `time` (e.g., `from time import sleep`), the patch target needs to follow — mocking patches the BINDING in the importing module, not the module being imported.

7. When updating `requirements.txt` in Task 0, use `Edit` (not `Write`) to preserve the existing pinned versions and ordering. Insert the `beautifulsoup4==4.12.3` line in alphabetical position OR at the bottom — either is acceptable.

**Methodology callouts before execution:**

**1. HTML scraping fragility vs operational acceptance.** The plan accepts stockanalysis.com page-structure drift as an operational risk: parser failures are logged + watermark-recorded but don't crash the pipeline, and the materialization layer's adapter chain (A.3.8+) falls back to EDGAR for the same features when stockanalysis-derived ratios are missing. The risk surface is bounded: at worst we lose own-history percentile context for some tickers, not the whole Layer-1 universe scoring. Decision: graceful-degradation posture for v1.

**2. 90-day refresh window vs A.3.10 freshness needs.** The 90-day skip aligns with quarterly earnings cadence but means a ticker's TTM row can be up to 90 days stale at the moment A.3.10 materialization runs. For trailing percentile compute this is fine (a 90-day-old TTM is still inside the trailing-5y window), but for the "current" TTM datapoint used in numerator vs trailing distribution, a tighter cadence may be wanted. Decision: keep 90 days for v1; tighten only if A.3.10 surfaces the requirement (would add a per-period_type skip — non-trivial refactor of the watermark scheme).

**3. Off-calendar fiscal-year approximation.** The plan stores Apple's `2024` annual row as `2024-12-31` even though Apple's actual fiscal year ends in late September. This is documented as a v1 approximation; the methodology refinement (joining against EDGAR period-end dates in A.3.8+) is deferred. For Apple specifically, the trailing-5y percentile distribution will treat each fiscal-year-end snapshot as if it were calendar-year-end, which biases the time-axis slightly but doesn't bias the value distribution. Decision: calendar-aligned approximation acceptable for v1; true fiscal-period alignment is an A.3.8+ refinement.
