# Phase A.3.4 — EDGAR Form 4 Insider Transactions + 8-K Filing Index + Cohen-Malloy-Pomorski Opportunistic Classifier

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]` checkbox syntax for tracking.

**Goal:** Wire `EdgarSource` to pull Form 4 insider transactions and the per-ticker filings index (8-K + 10-K/10-Q + Form 4 envelope) end-to-end. Build the EDGAR submissions parser, the Form 4 XML parser, and the Cohen-Malloy-Pomorski opportunistic-insider classifier as a pure function. Migrate schema v5 → v6 to add `raw_edgar_insider` (one row per Form 4 line item, classified opportunistic vs routine) and `raw_edgar_filings` (one row per filing — 8-K / 10-K / 10-Q / Form 4 envelope). All HTTP is mockable, watermark-aware (Principle 6), SEC-fair-access compliant (User-Agent + ≤10 req/s with tenacity retry on 429/503), and idempotent (`INSERT OR IGNORE` on the two new tables; Principle 5/6).

**Why this matters:** The Cohen-Malloy-Pomorski 2012 opportunistic-insider classifier is the **highest-confidence sub-signal** in `news_activity_score` (82 bps/month value-weighted abnormal return on opportunistic-only portfolios; routine traders ≈ 0 alpha — see the methodology reference §1 row 1). This sub-phase produces the raw substrate the classifier consumes plus the classifier itself.

**Architecture:** Additive only. Migration v5→v6 creates `raw_edgar_insider` + `raw_edgar_filings` and bumps schema_version. `EdgarSource` gets a single new public method `fetch_filings_for_ticker(ticker, run_id, db)` which:
1. Resolves CIK via `SecCikLookup` (built in A.3.3).
2. Pulls `data.sec.gov/submissions/CIK{cik}.json` (the filings index).
3. Calls `parse_submissions()` (pure function) to produce filing rows + a list of Form 4 accession numbers to fetch.
4. For each Form 4 accn, pulls the primary XML doc, calls `parse_form4_xml()` (pure function) to extract per-transaction rows.
5. Runs `classify_transactions()` over **all parsed insider rows aggregated across this batch** (the classifier needs per-filer cross-filing history to determine routine vs opportunistic).
6. Writes filings to `raw_edgar_filings` and insider rows to `raw_edgar_insider`, both via INSERT OR IGNORE.
7. Updates the `(edgar, ticker, filings)` watermark with the most-recent `filing_date` observed.

The parsers live in **pure-function** modules so they can be unit-tested against synthetic XML/JSON fixtures without any networking. The Cohen-Malloy-Pomorski classifier also lives as a pure function so it can be re-applied during materialization (the persisted `is_opportunistic` is a snapshot stamped with `opportunistic_classifier_version` for audit and future recalibration).

**Tech Stack:** Python 3.11+, `requests`, `tenacity`, `pydantic` v2, stdlib `xml.etree.ElementTree`, `pytest`, `pytest-mock`. No new runtime dependencies.

**Spec reference:** [docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md](../specs/2026-05-21-phase-a3-layer1-hardening-design.md) §6.3.2 (Form 4) + §6.3.3 (8-K) + §3 Principle 5/6 (no overwrite + watermarks bound deltas) + §7.1 row 1 (CMP as `news_activity_score` lead).

**Methodology reference:** the methodology bibliography §1 row 1 — Cohen, Malloy, Pomorski (2012), *"Decoding Inside Information"*, Journal of Finance. Routine-trader rule: "an insider who placed a trade in the same calendar month for at least 3 consecutive years prior to the trade in question." We implement the more permissive **≥2 transactions in the same calendar month in the prior 3 years** variant, which is the operational version used in subsequent literature (Jagolinzer et al. 2020 follow the same threshold) and which the spec calls out explicitly in §6.3.2. The classifier writes `opportunistic_classifier_version = "1.0"` on every row for future-recalibration audit.

**Out of scope for A.3.4:**
- Derivative Form 4 transactions (option grants, exercises beyond `M`-code reporting): we parse `<nonDerivativeTransaction>` blocks. `<derivativeTransaction>` blocks are flagged with `is_derivative=True` if extracted, otherwise skipped cleanly. Full derivative parsing → A.3.10 or beyond.
- Schedule 13D / 13G (ownership-threshold filings) — not in the spec for A.3 at all.
- FRED / FINRA / stockanalysis / OpenBB → A.3.5 onward.
- The downstream `news_activity_score` composite that consumes `is_opportunistic` → A.3.8.
- Live integration test against the real SEC endpoint → A.5 acceptance phase.
- Form 4 amendments (`4/A`) — we parse the original Form 4 (form_type == "4"); amendments are recorded in `raw_edgar_filings` but their XML payload is not parsed in this sub-phase. The INSERT OR IGNORE PK on `raw_edgar_insider` would silently drop an amendment that reports the same (cik, accn, filer, txn_date, txn_code) tuple, which is the desired conservative default — original disclosure wins.
- Restated insider counts. The CMP classifier runs once per ticker batch on whatever insider history we have in-memory from that batch. It does NOT cross-reference prior runs' `raw_edgar_insider` rows — those are first-observed snapshots. Cross-run re-classification (e.g., once enough history accrues to reclassify an originally-opportunistic trader as routine) is a future enhancement; for A.3.4 the classifier's input is the union of all Form 4 line items parsed in this run's batch fetch.

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| `src/common/schemas.py` | Add `RawEdgarInsiderRow` + `RawEdgarFilingRow` Pydantic models | Modify |
| `src/common/database.py` | Add `migrate_to_v6()` + `insert_raw_edgar_insider()` + `insert_raw_edgar_filings()` | Modify |
| `src/methodology/__init__.py` | NEW — package init for methodology module | Create |
| `src/methodology/opportunistic_insider.py` | NEW — Cohen-Malloy-Pomorski pure-function classifier | Create |
| `src/common/datasources/edgar_form4_parser.py` | NEW — pure-function Form 4 XML parser | Create |
| `src/common/datasources/edgar_submissions_parser.py` | NEW — pure-function submissions JSON parser | Create |
| `src/common/datasources/edgar_source.py` | Add `fetch_filings_for_ticker()` real implementation | Modify |
| `tests/methodology/__init__.py` | NEW — package init for methodology tests | Create |
| `tests/methodology/test_opportunistic_insider.py` | CMP classifier unit tests (13 cases) | Create |
| `tests/fixtures/edgar/form4_sample.xml` | NEW — synthetic but realistic Form 4 XML | Create |
| `tests/fixtures/edgar/submissions_CIK0000320193.json` | NEW — synthetic Apple submissions excerpt | Create |
| `tests/common/test_schemas_a3_4.py` | Tests for `RawEdgarInsiderRow` + `RawEdgarFilingRow` | Create |
| `tests/common/test_database_a3_4.py` | Tests for `migrate_to_v6` + insert helpers | Create |
| `tests/datasources/test_edgar_form4_parser.py` | Tests for Form 4 XML parser against fixture | Create |
| `tests/datasources/test_edgar_submissions_parser.py` | Tests for submissions parser against fixture | Create |
| `tests/datasources/test_edgar_filings_fetch.py` | Tests for `EdgarSource.fetch_filings_for_ticker` (mocked HTTP) | Create |
| `tests/datasources/test_integration_a3_4.py` | Mock-level end-to-end orchestration with DB persistence + watermark + CMP | Create |
| the build plan | Mark A.3.4 shipped | Modify |

---

## Task 0: Pre-flight verification

**Files:** none modified

- [ ] **Step 1: Verify schema is at v5 from A.3.3**

Run from the repository root:

```
./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager(); print('current schema version:', m.get_schema_version())"
```

Expected: prints `current schema version: 5`.

- [ ] **Step 2: Verify A.3.3 baseline tests still pass**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: `235 passed, 2 deselected` (or whatever the current A.3.3 baseline is).

- [ ] **Step 3: Verify tenacity + requests + xml.etree importable from venv**

```
./venv/Scripts/python.exe -c "import tenacity, requests; import xml.etree.ElementTree as ET; print('tenacity', tenacity.__version__); print('requests', requests.__version__); print('ET ok')"
```

Expected: all three lines print without error.

- [ ] **Step 4: Verify A.3.3 SecCikLookup is wired and importable**

```
./venv/Scripts/python.exe -c "from src.common.datasources.sec_cik_lookup import SecCikLookup; print('SecCikLookup ok')"
```

Expected: `SecCikLookup ok`.

- [ ] **Step 5: Verify working tree is clean before starting**

Run: `git status -s`
Expected: empty output (or only an unrelated `.env` / notebooks). If anything else under `src/` or `tests/` is uncommitted, stop and report.

No commit at this task — verification only.

---

## Task 1: `RawEdgarInsiderRow` + `RawEdgarFilingRow` Pydantic models

**Files:**
- Modify: `src/common/schemas.py` (append)
- Create: `tests/common/test_schemas_a3_4.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_schemas_a3_4.py`:

```python
"""Tests for Phase A.3.4 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import RawEdgarInsiderRow, RawEdgarFilingRow


class TestRawEdgarInsiderRow:
    def test_minimal_valid_purchase(self):
        r = RawEdgarInsiderRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            filer_name="COOK TIMOTHY D",
            filer_title="Chief Executive Officer",
            filer_is_officer=True,
            filer_is_director=True,
            filer_is_10pct_owner=False,
            transaction_date="2026-03-15",
            transaction_code="P",
            transaction_code_description="Open-market or private purchase",
            shares=1000.0,
            price_per_share=175.25,
            total_value=175250.0,
            shares_after_transaction=3279000.0,
            source_filing_accn="0000320193-26-000045",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.transaction_code == "P"
        assert r.cik == "0000320193"
        assert r.is_opportunistic is None
        assert r.is_derivative is False

    def test_full_valid_with_classifier_output(self):
        r = RawEdgarInsiderRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            filer_name="COOK TIMOTHY D",
            filer_title="Chief Executive Officer",
            filer_is_officer=True,
            filer_is_director=True,
            filer_is_10pct_owner=False,
            transaction_date="2026-03-15",
            transaction_code="S",
            transaction_code_description="Open-market or private sale",
            shares=500000.0,
            price_per_share=175.25,
            total_value=87625000.0,
            shares_after_transaction=2779000.0,
            is_opportunistic=True,
            opportunistic_classifier_version="1.0",
            is_derivative=False,
            source_filing_accn="0000320193-26-000045",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.is_opportunistic is True
        assert r.opportunistic_classifier_version == "1.0"

    def test_all_standard_transaction_codes_accepted(self):
        codes = ["P", "S", "A", "M", "D", "F", "G", "J", "C",
                 "E", "H", "I", "O", "X", "V", "W", "Z", "K", "L", "U"]
        for code in codes:
            r = RawEdgarInsiderRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                filer_name="X",
                transaction_date="2026-03-15",
                transaction_code=code,
                shares=1.0,
                source_filing_accn="0000320193-26-000001",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
            assert r.transaction_code == code

    def test_unknown_transaction_code_rejected(self):
        with pytest.raises(Exception):
            RawEdgarInsiderRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                filer_name="X",
                transaction_date="2026-03-15",
                transaction_code="ZZ",
                shares=1.0,
                source_filing_accn="0000320193-26-000001",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_cik_normalized_to_10_digit_string(self):
        r = RawEdgarInsiderRow(
            run_id="r1",
            ticker="AAPL",
            cik="320193",
            filer_name="X",
            transaction_date="2026-03-15",
            transaction_code="P",
            shares=1.0,
            source_filing_accn="0000320193-26-000001",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.cik == "0000320193"

    def test_filer_name_uppercased(self):
        r = RawEdgarInsiderRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            filer_name="cook timothy d",
            transaction_date="2026-03-15",
            transaction_code="P",
            shares=1.0,
            source_filing_accn="0000320193-26-000001",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.filer_name == "COOK TIMOTHY D"

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            RawEdgarInsiderRow(
                run_id="r1",
                ticker="not-a-ticker",
                cik="0000320193",
                filer_name="X",
                transaction_date="2026-03-15",
                transaction_code="P",
                shares=1.0,
                source_filing_accn="0000320193-26-000001",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_shares_stored_as_positive(self):
        r = RawEdgarInsiderRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            filer_name="X",
            transaction_date="2026-03-15",
            transaction_code="D",
            shares=500.0,
            source_filing_accn="0000320193-26-000001",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.shares == 500.0


class TestRawEdgarFilingRow:
    def test_minimal_valid_8k(self):
        r = RawEdgarFilingRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            form_type="8-K",
            filing_date="2026-02-01",
            accepted_at="2026-02-01T16:30:01Z",
            item_codes="2.02,9.01",
            source_filing_accn="0000320193-26-000010",
            primary_doc_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000010/aapl-20260201.htm",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.form_type == "8-K"
        assert r.item_codes == "2.02,9.01"

    def test_form_4_envelope_record(self):
        r = RawEdgarFilingRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            form_type="4",
            filing_date="2026-03-15",
            accepted_at="2026-03-15T18:00:00Z",
            item_codes=None,
            source_filing_accn="0000320193-26-000045",
            primary_doc_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/wk-form4_1742068800.xml",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.form_type == "4"
        assert r.item_codes is None

    def test_10k_record(self):
        r = RawEdgarFilingRow(
            run_id="r1",
            ticker="AAPL",
            cik="0000320193",
            form_type="10-K",
            filing_date="2024-11-01",
            accepted_at="2024-10-31T18:09:50Z",
            item_codes=None,
            source_filing_accn="0000320193-24-000158",
            primary_doc_url=None,
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.form_type == "10-K"

    def test_allowed_form_types(self):
        for ft in ["8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A", "4", "4/A"]:
            r = RawEdgarFilingRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                form_type=ft,
                filing_date="2024-11-01",
                source_filing_accn="0000320193-24-000158",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
            assert r.form_type == ft

    def test_unknown_form_type_rejected(self):
        with pytest.raises(Exception):
            RawEdgarFilingRow(
                run_id="r1",
                ticker="AAPL",
                cik="0000320193",
                form_type="S-1",
                filing_date="2024-11-01",
                source_filing_accn="0000320193-24-000158",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_cik_padded(self):
        r = RawEdgarFilingRow(
            run_id="r1",
            ticker="AAPL",
            cik="320193",
            form_type="8-K",
            filing_date="2026-02-01",
            source_filing_accn="0000320193-26-000010",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.cik == "0000320193"
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_4.py -v
```

Expected: `ImportError` on `RawEdgarInsiderRow` and `RawEdgarFilingRow`.

- [ ] **Step 3: Append models to `src/common/schemas.py`**

At the **end** of `src/common/schemas.py`, append:

```python


# === Phase A.3.4 - EDGAR Form 4 insider transactions + 8-K filing index ====


# SEC Form 4 transaction codes per the General Instructions, Table I/II.
# Source: https://www.sec.gov/about/forms/form4.pdf
#   P  Open-market or private purchase of non-derivative or derivative security
#   S  Open-market or private sale of non-derivative or derivative security
#   A  Grant, award, or other acquisition pursuant to Rule 16b-3
#   M  Exercise or conversion of derivative security exempted under Rule 16b-3
#   D  Disposition to the issuer of issuer equity securities pursuant to Rule 16b-3
#   F  Payment of exercise price or tax liability by delivering or withholding
#   G  Bona fide gift
#   J  Other acquisition or disposition (describe transaction)
#   C  Conversion of derivative security
#   E  Expiration of short derivative position
#   H  Expiration (or cancellation) of long derivative position with value received
#   I  Discretionary transaction in accordance with Rule 16b-3(f)
#   O  Exercise of out-of-the-money derivative security
#   X  Exercise of in-the-money or at-the-money derivative security
#   V  Transaction voluntarily reported earlier than required
#   W  Acquisition or disposition by will or laws of descent and distribution
#   Z  Deposit into or withdrawal from voting trust
#   K  Transaction in equity swap or instrument with similar characteristics
#   L  Small acquisition under Rule 16a-6
#   U  Disposition pursuant to a tender of shares in a change of control transaction
_FORM4_TRANSACTION_CODES = Literal[
    "P", "S", "A", "M", "D", "F", "G", "J",
    "C", "E", "H", "I", "O", "X", "V", "W",
    "Z", "K", "L", "U",
]


_EDGAR_FORM_TYPES = Literal[
    "8-K", "8-K/A",
    "10-K", "10-K/A",
    "10-Q", "10-Q/A",
    "4", "4/A",
]


class RawEdgarInsiderRow(BaseModel):
    """One row per Form 4 non-derivative line item.

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.3.2. Stored in raw_edgar_insider.

    Primary key conceptually:
      (cik, source_filing_accn, filer_name, transaction_date, transaction_code).

    A single Form 4 filing can disclose multiple <nonDerivativeTransaction>
    blocks for the same filer (e.g., a planned 10b5-1 sale executed across
    multiple price points on the same day, or distinct codes M+S on the
    same day for an exercise+sell). The PK above is the operational
    granularity at which we deduplicate.

    `is_opportunistic` is None at parse time. It is populated by
    `methodology.opportunistic_insider.classify_transactions` AFTER all
    Form 4 rows for a ticker batch have been parsed (the classifier needs
    cross-filing history per filer to determine routine vs opportunistic).

    `opportunistic_classifier_version` is the version string of the
    classifier that produced `is_opportunistic` (for future-recalibration
    audit). It is None when `is_opportunistic` is None.
    """

    run_id: str
    ticker: str
    cik: str
    filer_name: str
    filer_title: Optional[str] = None
    filer_is_officer: Optional[bool] = None
    filer_is_director: Optional[bool] = None
    filer_is_10pct_owner: Optional[bool] = None
    transaction_date: str
    transaction_code: _FORM4_TRANSACTION_CODES
    transaction_code_description: Optional[str] = None
    shares: float = Field(ge=0)
    price_per_share: Optional[float] = Field(default=None, ge=0)
    total_value: Optional[float] = None
    shares_after_transaction: Optional[float] = Field(default=None, ge=0)

    is_opportunistic: Optional[bool] = None
    opportunistic_classifier_version: Optional[str] = None
    is_derivative: bool = False

    source_filing_accn: str
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
        if s.upper().startswith("CIK"):
            s = s[3:]
        s = s.lstrip("0") or "0"
        if not s.isdigit():
            raise ValueError(f"invalid cik (non-digit): {v!r}")
        return s.zfill(10)

    @field_validator("filer_name", mode="before")
    @classmethod
    def _normalize_filer_name(cls, v: object) -> str:
        if v is None:
            raise ValueError("filer_name is required")
        return str(v).strip().upper()


class RawEdgarFilingRow(BaseModel):
    """One row per SEC filing (envelope record).

    Covers 8-K, 10-K, 10-Q, Form 4, and amendments. For Form 4, the
    *line items* live in raw_edgar_insider; the envelope row in
    raw_edgar_filings is the audit trail.

    Primary key conceptually: (cik, source_filing_accn).
    """

    run_id: str
    ticker: str
    cik: str
    form_type: _EDGAR_FORM_TYPES
    filing_date: str
    accepted_at: Optional[str] = None
    item_codes: Optional[str] = None  # CSV of 8-K item numbers, e.g. "2.02,9.01"
    source_filing_accn: str
    primary_doc_url: Optional[str] = None
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
        if s.upper().startswith("CIK"):
            s = s[3:]
        s = s.lstrip("0") or "0"
        if not s.isdigit():
            raise ValueError(f"invalid cik (non-digit): {v!r}")
        return s.zfill(10)
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_4.py -v
```

Expected: All 14 tests PASS (8 insider + 6 filing).

- [ ] **Step 5: Commit**

```
git add src/common/schemas.py tests/common/test_schemas_a3_4.py
git commit -m "feat(schemas): add RawEdgarInsiderRow + RawEdgarFilingRow for A.3.4 Form 4 + filings"
```

---

## Task 2: `migrate_to_v6` + `insert_raw_edgar_insider` + `insert_raw_edgar_filings`

**Files:**
- Modify: `src/common/database.py`
- Create: `tests/common/test_database_a3_4.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_database_a3_4.py`:

```python
"""Tests for Phase A.3.4 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawEdgarInsiderRow, RawEdgarFilingRow


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


# --- migrate_to_v6 ----------------------------------------------------


def test_migrate_to_v6_creates_raw_edgar_insider_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    assert "raw_edgar_insider" in _table_names(db_path)


def test_migrate_to_v6_creates_raw_edgar_filings_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    assert "raw_edgar_filings" in _table_names(db_path)


def test_raw_edgar_insider_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    cols = _columns(db_path, "raw_edgar_insider")
    expected = {
        "run_id", "ticker", "cik",
        "filer_name", "filer_title",
        "filer_is_officer", "filer_is_director", "filer_is_10pct_owner",
        "transaction_date", "transaction_code", "transaction_code_description",
        "shares", "price_per_share", "total_value", "shares_after_transaction",
        "is_opportunistic", "opportunistic_classifier_version", "is_derivative",
        "source_filing_accn", "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_raw_edgar_filings_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    cols = _columns(db_path, "raw_edgar_filings")
    expected = {
        "run_id", "ticker", "cik", "form_type",
        "filing_date", "accepted_at", "item_codes",
        "source_filing_accn", "primary_doc_url", "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v6_bumps_schema_version_to_6(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6]


def test_migrate_to_v6_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.migrate_to_v6()
    mgr.migrate_to_v6()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6]


def test_get_schema_version_returns_6_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    assert mgr.get_schema_version() == 6


def test_migrate_to_v6_preserves_v5_data(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v5()
    mgr.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple Inc."}
    ])
    mgr.migrate_to_v6()
    assert mgr.get_cik_for_ticker("AAPL") == "0000320193"


# --- insert_raw_edgar_insider -----------------------------------------


def _insider(**overrides):
    base = dict(
        run_id="r1", ticker="AAPL", cik="0000320193",
        filer_name="COOK TIMOTHY D", filer_title="CEO",
        filer_is_officer=True, filer_is_director=True, filer_is_10pct_owner=False,
        transaction_date="2026-03-15", transaction_code="P",
        transaction_code_description="Open-market purchase",
        shares=1000.0, price_per_share=175.0, total_value=175000.0,
        shares_after_transaction=3279000.0,
        is_opportunistic=True, opportunistic_classifier_version="1.0",
        is_derivative=False,
        source_filing_accn="0000320193-26-000045",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawEdgarInsiderRow(**base)


def test_insert_raw_edgar_insider_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_insider([_insider()])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT filer_name, transaction_code, shares, is_opportunistic "
            "FROM raw_edgar_insider WHERE run_id='r1' AND ticker='AAPL'"
        ).fetchone()
    assert row == ("COOK TIMOTHY D", "P", 1000.0, 1)


def test_insert_raw_edgar_insider_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_insider([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_edgar_insider").fetchone()[0]
    assert n == 0


def test_insert_raw_edgar_insider_uses_insert_or_ignore(tmp_path: Path):
    """PK = (cik, source_filing_accn, filer_name, transaction_date, transaction_code).
    A re-insert with the same PK is silently dropped. First write wins."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_insider([_insider(shares=1000.0)])
    mgr.insert_raw_edgar_insider([_insider(shares=9999.0)])
    with sqlite3.connect(db_path) as c:
        shares = c.execute(
            "SELECT shares FROM raw_edgar_insider WHERE run_id='r1'"
        ).fetchone()[0]
    assert shares == 1000.0


def test_insert_raw_edgar_insider_supports_multiple_line_items_per_filing(tmp_path: Path):
    """One Form 4 can disclose multiple non-derivative transactions for the
    same filer, distinguished by transaction_code."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_insider([
        _insider(transaction_code="M", transaction_date="2026-03-15"),
        _insider(transaction_code="S", transaction_date="2026-03-15"),
        _insider(transaction_code="F", transaction_date="2026-03-15"),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider "
            "WHERE source_filing_accn='0000320193-26-000045'"
        ).fetchone()[0]
    assert n == 3


def test_insert_raw_edgar_insider_handles_none_classifier_fields(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_insider([
        _insider(is_opportunistic=None, opportunistic_classifier_version=None)
    ])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT is_opportunistic, opportunistic_classifier_version "
            "FROM raw_edgar_insider WHERE run_id='r1'"
        ).fetchone()
    assert row == (None, None)


# --- insert_raw_edgar_filings -----------------------------------------


def _filing(**overrides):
    base = dict(
        run_id="r1", ticker="AAPL", cik="0000320193",
        form_type="8-K", filing_date="2026-02-01",
        accepted_at="2026-02-01T16:30:01Z",
        item_codes="2.02,9.01",
        source_filing_accn="0000320193-26-000010",
        primary_doc_url="https://www.sec.gov/x.htm",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawEdgarFilingRow(**base)


def test_insert_raw_edgar_filings_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_filings([_filing()])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT form_type, item_codes FROM raw_edgar_filings WHERE run_id='r1'"
        ).fetchone()
    assert row == ("8-K", "2.02,9.01")


def test_insert_raw_edgar_filings_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_filings([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_edgar_filings").fetchone()[0]
    assert n == 0


def test_insert_raw_edgar_filings_uses_insert_or_ignore(tmp_path: Path):
    """PK = (cik, source_filing_accn). Duplicates silently dropped."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_filings([_filing(item_codes="2.02")])
    mgr.insert_raw_edgar_filings([_filing(item_codes="2.02,9.01")])
    with sqlite3.connect(db_path) as c:
        items = c.execute(
            "SELECT item_codes FROM raw_edgar_filings WHERE source_filing_accn='0000320193-26-000010'"
        ).fetchone()[0]
    assert items == "2.02"


def test_insert_raw_edgar_filings_supports_mixed_form_types(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v6()
    mgr.insert_raw_edgar_filings([
        _filing(form_type="8-K", source_filing_accn="0000320193-26-000010"),
        _filing(form_type="10-K", source_filing_accn="0000320193-26-000050", item_codes=None),
        _filing(form_type="4", source_filing_accn="0000320193-26-000045", item_codes=None),
    ])
    with sqlite3.connect(db_path) as c:
        forms = sorted(r[0] for r in c.execute(
            "SELECT form_type FROM raw_edgar_filings WHERE ticker='AAPL'"
        ))
    assert forms == ["10-K", "4", "8-K"]
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_4.py -v
```

Expected: `AttributeError` on `migrate_to_v6`.

- [ ] **Step 3: Add `migrate_to_v6()` to `DatabaseManager`**

In `src/common/database.py`, **locate** `migrate_to_v5()` (it ends near line ~440). Immediately AFTER it (before any helper methods that follow), insert:

```python
    def migrate_to_v6(self) -> None:
        """Idempotent migration v5 -> v6 per A.3 spec sections 6.3.2 + 6.3.3.

        Adds:
          - raw_edgar_insider: per-line-item Form 4 non-derivative
            transactions, classified opportunistic vs routine via the
            Cohen-Malloy-Pomorski 2012 rule.
          - raw_edgar_filings: per-filing envelope record covering 8-K,
            10-K, 10-Q, Form 4 (and amendments). The Form 4 line items
            live in raw_edgar_insider; this table is the audit trail.

        Safe to call multiple times. Existing data preserved.

        raw_edgar_insider PRIMARY KEY =
          (cik, source_filing_accn, filer_name, transaction_date, transaction_code).
        raw_edgar_filings PRIMARY KEY = (cik, source_filing_accn).

        Both use INSERT OR IGNORE on insert (Principle 5: no silent
        overwrite). First-observed values win; amendments are recorded as
        new filing rows but their re-stated line items are silently
        dropped if they collide with the original (cik, accn, filer,
        date, code) tuple.
        """
        self.migrate_to_v5()

        v6_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_edgar_insider (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              cik TEXT NOT NULL,
              filer_name TEXT NOT NULL,
              filer_title TEXT,
              filer_is_officer INTEGER,
              filer_is_director INTEGER,
              filer_is_10pct_owner INTEGER,
              transaction_date TEXT NOT NULL,
              transaction_code TEXT NOT NULL,
              transaction_code_description TEXT,
              shares REAL NOT NULL,
              price_per_share REAL,
              total_value REAL,
              shares_after_transaction REAL,
              is_opportunistic INTEGER,
              opportunistic_classifier_version TEXT,
              is_derivative INTEGER NOT NULL DEFAULT 0,
              source_filing_accn TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (cik, source_filing_accn, filer_name, transaction_date, transaction_code)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_insider_ticker ON raw_edgar_insider(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_insider_cik ON raw_edgar_insider(cik)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_insider_txn_date ON raw_edgar_insider(transaction_date)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_insider_filer ON raw_edgar_insider(filer_name)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_insider_opportunistic ON raw_edgar_insider(is_opportunistic)",
            """
            CREATE TABLE IF NOT EXISTS raw_edgar_filings (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              cik TEXT NOT NULL,
              form_type TEXT NOT NULL,
              filing_date TEXT NOT NULL,
              accepted_at TEXT,
              item_codes TEXT,
              source_filing_accn TEXT NOT NULL,
              primary_doc_url TEXT,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (cik, source_filing_accn)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_filings_ticker ON raw_edgar_filings(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_filings_cik ON raw_edgar_filings(cik)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_filings_filing_date ON raw_edgar_filings(filing_date)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_filings_form_type ON raw_edgar_filings(form_type)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v6_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 6")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (6, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()
```

In `src/common/database.py`, at the **end** of the `DatabaseManager` class, append:

```python
    # ------------------------------------------------------------------
    # Phase A.3.4: raw_edgar_insider + raw_edgar_filings helpers
    # ------------------------------------------------------------------

    def insert_raw_edgar_insider(self, rows: list) -> None:
        """Insert RawEdgarInsiderRow records into raw_edgar_insider.

        Uses INSERT OR IGNORE — once a
        (cik, source_filing_accn, filer_name, transaction_date, transaction_code)
        is recorded, subsequent inserts with the same PK are silently
        dropped. First write wins. This protects against amendment storms
        and re-runs duplicating line items (Principle 5).
        """
        if not rows:
            return
        from src.common.schemas import RawEdgarInsiderRow
        records = [
            r.model_dump() if isinstance(r, RawEdgarInsiderRow) else dict(r)
            for r in rows
        ]
        # Boolean -> int for SQLite
        for rec in records:
            for k in ("filer_is_officer", "filer_is_director",
                      "filer_is_10pct_owner", "is_opportunistic", "is_derivative"):
                if rec.get(k) is not None:
                    rec[k] = int(bool(rec[k]))
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_edgar_insider ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()

    def insert_raw_edgar_filings(self, rows: list) -> None:
        """Insert RawEdgarFilingRow records into raw_edgar_filings.

        Uses INSERT OR IGNORE on PK (cik, source_filing_accn).
        """
        if not rows:
            return
        from src.common.schemas import RawEdgarFilingRow
        records = [
            r.model_dump() if isinstance(r, RawEdgarFilingRow) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_edgar_filings ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_4.py -v
```

Expected: All 16 tests PASS (8 migration + 5 insider insert + 3 filings insert).

- [ ] **Step 5: Run full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: ~265 passed (235 baseline + 14 schema + 16 database).

- [ ] **Step 6: Commit**

```
git add src/common/database.py tests/common/test_database_a3_4.py
git commit -m "feat(database): migrate_to_v6 - raw_edgar_insider + raw_edgar_filings (INSERT OR IGNORE)"
```

---

## Task 3: Cohen-Malloy-Pomorski opportunistic-insider classifier

**Files:**
- Create: `src/methodology/__init__.py`
- Create: `src/methodology/opportunistic_insider.py`
- Create: `tests/methodology/__init__.py`
- Create: `tests/methodology/test_opportunistic_insider.py`

- [ ] **Step 1: Create the package init files**

Create `src/methodology/__init__.py` with content:

```python
"""Methodology layer — pure-function implementations of academic signals.

Each module here implements a single paper / signal as a pure function,
unit-testable in isolation. Versioned via a module-level
`{SIGNAL}_VERSION` constant stamped onto every produced output row, so
the persisted classifier output is auditable and recalibratable.
"""
```

Create `tests/methodology/__init__.py` with empty content (single newline).

- [ ] **Step 2: Write the failing tests**

Create `tests/methodology/test_opportunistic_insider.py`:

```python
"""Tests for the Cohen-Malloy-Pomorski 2012 opportunistic-insider classifier.

Operational rule (per the spec §6.3.2 and the methodology reference):
  A transaction is ROUTINE if its filer has placed >= 2 transactions in
  the same calendar month within the prior 3 years (strictly earlier
  than the transaction in question, with the lookback bounded to 3 years).
  Otherwise OPPORTUNISTIC.

`filer_id` (e.g., "COOK TIMOTHY D:0000320193") is the unit of identity:
the same physical insider trading the same issuer.

The classifier is a pure function:
  classify_transactions(txns: list[dict]) -> list[bool]
where:
  - txns is a list with keys: filer_id, transaction_date, transaction_code
  - returns a parallel list of bools where True == opportunistic
"""
from __future__ import annotations

import pytest

from src.methodology.opportunistic_insider import (
    classify_transactions,
    OPPORTUNISTIC_CLASSIFIER_VERSION,
)


def test_version_constant_exists():
    assert OPPORTUNISTIC_CLASSIFIER_VERSION == "1.0"


def test_single_transaction_is_opportunistic():
    txns = [
        {"filer_id": "ALICE:CIKA", "transaction_date": "2026-03-15", "transaction_code": "P"}
    ]
    assert classify_transactions(txns) == [True]


def test_empty_input_returns_empty_list():
    assert classify_transactions([]) == []


def test_filer_with_2_same_month_prior_3y_is_routine():
    """Threshold inclusive at 2: a filer with exactly 2 same-month-prior-3y
    transactions has the third March trade classified as routine."""
    txns = [
        {"filer_id": "BOB:CIKB", "transaction_date": "2023-03-10", "transaction_code": "S"},
        {"filer_id": "BOB:CIKB", "transaction_date": "2024-03-12", "transaction_code": "S"},
        {"filer_id": "BOB:CIKB", "transaction_date": "2026-03-15", "transaction_code": "S"},
    ]
    out = classify_transactions(txns)
    assert out[0] is True
    assert out[1] is True
    assert out[2] is False  # routine


def test_filer_with_3_same_month_prior_3y_is_routine():
    txns = [
        {"filer_id": "CARA:CIKC", "transaction_date": "2023-06-05", "transaction_code": "S"},
        {"filer_id": "CARA:CIKC", "transaction_date": "2024-06-10", "transaction_code": "S"},
        {"filer_id": "CARA:CIKC", "transaction_date": "2025-06-15", "transaction_code": "S"},
        {"filer_id": "CARA:CIKC", "transaction_date": "2026-06-20", "transaction_code": "S"},
    ]
    out = classify_transactions(txns)
    assert out[3] is False  # routine


def test_same_month_but_beyond_3y_lookback_is_opportunistic():
    """Edge: filer has same-month transactions only in years > 3 prior."""
    txns = [
        {"filer_id": "DAN:CIKD", "transaction_date": "2020-04-05", "transaction_code": "P"},
        {"filer_id": "DAN:CIKD", "transaction_date": "2021-04-05", "transaction_code": "P"},
        {"filer_id": "DAN:CIKD", "transaction_date": "2026-04-05", "transaction_code": "P"},
    ]
    out = classify_transactions(txns)
    assert out[2] is True  # opportunistic — priors are too far back


def test_same_month_within_3y_boundary_is_routine():
    """Boundary: a same-month trade exactly inside the prior-3-year window."""
    txns = [
        {"filer_id": "EVE:CIKE", "transaction_date": "2023-04-06", "transaction_code": "S"},
        {"filer_id": "EVE:CIKE", "transaction_date": "2024-04-10", "transaction_code": "S"},
        {"filer_id": "EVE:CIKE", "transaction_date": "2026-04-05", "transaction_code": "S"},
    ]
    out = classify_transactions(txns)
    assert out[2] is False  # routine


def test_different_filers_classified_independently():
    txns = [
        {"filer_id": "ROUTINE:X", "transaction_date": "2023-03-10", "transaction_code": "S"},
        {"filer_id": "ROUTINE:X", "transaction_date": "2024-03-10", "transaction_code": "S"},
        {"filer_id": "ROUTINE:X", "transaction_date": "2026-03-10", "transaction_code": "S"},
        {"filer_id": "ONEOFF:Y", "transaction_date": "2026-03-15", "transaction_code": "P"},
    ]
    out = classify_transactions(txns)
    assert out == [True, True, False, True]


def test_mixed_codes_count_toward_routine_threshold():
    """CMP counts transactions, not directions. P and S both count."""
    txns = [
        {"filer_id": "MIXY:Z", "transaction_date": "2023-09-05", "transaction_code": "S"},
        {"filer_id": "MIXY:Z", "transaction_date": "2024-09-05", "transaction_code": "P"},
        {"filer_id": "MIXY:Z", "transaction_date": "2026-09-05", "transaction_code": "S"},
    ]
    out = classify_transactions(txns)
    assert out[2] is False  # routine


def test_different_month_priors_do_not_count():
    """A filer with many priors but in DIFFERENT months stays opportunistic."""
    txns = [
        {"filer_id": "FREQ:W", "transaction_date": "2023-01-10", "transaction_code": "S"},
        {"filer_id": "FREQ:W", "transaction_date": "2023-07-15", "transaction_code": "S"},
        {"filer_id": "FREQ:W", "transaction_date": "2024-02-10", "transaction_code": "S"},
        {"filer_id": "FREQ:W", "transaction_date": "2025-11-20", "transaction_code": "S"},
        {"filer_id": "FREQ:W", "transaction_date": "2026-04-05", "transaction_code": "S"},
    ]
    out = classify_transactions(txns)
    assert out[4] is True


def test_focal_transaction_itself_not_counted():
    """A transaction does not count itself as a prior."""
    txns = [
        {"filer_id": "SELF:V", "transaction_date": "2024-05-10", "transaction_code": "S"},
        {"filer_id": "SELF:V", "transaction_date": "2026-05-10", "transaction_code": "S"},
    ]
    out = classify_transactions(txns)
    assert out[1] is True  # opportunistic — only 1 prior in May


def test_same_day_multiple_transactions_each_classified():
    """Multiple line items on the same day for the same filer."""
    txns = [
        {"filer_id": "EXSELL:U", "transaction_date": "2023-03-20", "transaction_code": "S"},
        {"filer_id": "EXSELL:U", "transaction_date": "2024-03-20", "transaction_code": "S"},
        {"filer_id": "EXSELL:U", "transaction_date": "2025-03-20", "transaction_code": "S"},
        {"filer_id": "EXSELL:U", "transaction_date": "2026-03-20", "transaction_code": "M"},
        {"filer_id": "EXSELL:U", "transaction_date": "2026-03-20", "transaction_code": "S"},
        {"filer_id": "EXSELL:U", "transaction_date": "2026-03-20", "transaction_code": "F"},
    ]
    out = classify_transactions(txns)
    assert out[3] is False
    assert out[4] is False
    assert out[5] is False


def test_classify_preserves_input_order():
    txns = [
        {"filer_id": "B:1", "transaction_date": "2026-01-10", "transaction_code": "P"},
        {"filer_id": "A:1", "transaction_date": "2026-01-15", "transaction_code": "S"},
        {"filer_id": "B:1", "transaction_date": "2026-02-10", "transaction_code": "P"},
    ]
    out = classify_transactions(txns)
    assert len(out) == 3
    assert out == [True, True, True]


def test_unsorted_input_ordered_correctly_internally():
    """Order-invariant on input: prior-history is by date."""
    txns_sorted = [
        {"filer_id": "ORD:1", "transaction_date": "2023-07-01", "transaction_code": "S"},
        {"filer_id": "ORD:1", "transaction_date": "2024-07-01", "transaction_code": "S"},
        {"filer_id": "ORD:1", "transaction_date": "2026-07-01", "transaction_code": "S"},
    ]
    txns_unsorted = [
        {"filer_id": "ORD:1", "transaction_date": "2026-07-01", "transaction_code": "S"},
        {"filer_id": "ORD:1", "transaction_date": "2023-07-01", "transaction_code": "S"},
        {"filer_id": "ORD:1", "transaction_date": "2024-07-01", "transaction_code": "S"},
    ]
    out_sorted = classify_transactions(txns_sorted)
    out_unsorted = classify_transactions(txns_unsorted)
    assert out_sorted == [True, True, False]
    assert out_unsorted[0] is False
    assert out_unsorted[1] is True
    assert out_unsorted[2] is True
```

- [ ] **Step 3: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/methodology/test_opportunistic_insider.py -v
```

Expected: `ImportError` on `opportunistic_insider`.

- [ ] **Step 4: Create the classifier module**

Create `src/methodology/opportunistic_insider.py`:

```python
"""Cohen-Malloy-Pomorski (2012) opportunistic-insider classifier.

Reference: Cohen, L., Malloy, C., Pomorski, L. (2012). "Decoding Inside
Information." Journal of Finance, 67(3), 1009-1043.

Operational rule (per spec §6.3.2):
  A transaction T by filer F is classified as ROUTINE if, in the
  3 calendar years strictly prior to T's transaction_date, filer F
  has at least 2 transactions in the same calendar month as T's
  transaction_date. Otherwise OPPORTUNISTIC.

The CMP paper's original definition required same-month trades for
3 consecutive prior years. The >=2-in-prior-3y operational variant
implemented here is the version used by subsequent literature
(Jagolinzer et al. 2020 and the spec § 6.3.2 call this version out).

Pure function — no I/O, no DB, deterministic. Versioned via
OPPORTUNISTIC_CLASSIFIER_VERSION so persisted classifier outputs are
auditable and recalibratable.

`filer_id` is the unit of identity. The caller is responsible for
constructing it consistently (typically "{filer_name}:{cik}" so the
same physical insider trading the same issuer is one filer).
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict


OPPORTUNISTIC_CLASSIFIER_VERSION = "1.0"

# CMP rule threshold: filer must have >= this many same-month transactions
# in the prior-3-year window for the focal trade to be classified routine.
_ROUTINE_THRESHOLD = 2

# Lookback window (years). Strict: a prior trade T' counts only if
# T'.date >= focal_date - 3 years AND T'.date < focal_date.
_LOOKBACK_YEARS = 3


def _parse_date(s: str) -> _dt.date:
    """Parse 'YYYY-MM-DD'. Raises ValueError on malformed input."""
    return _dt.date.fromisoformat(s)


def _years_before(d: _dt.date, years: int) -> _dt.date:
    """Return date `years` years before d, anchored on the same month/day.

    For Feb-29 anchors in non-leap result years, falls back to Feb-28.
    """
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


def classify_transactions(txns: list[dict]) -> list[bool]:
    """Classify each transaction as opportunistic (True) or routine (False).

    Parameters
    ----------
    txns
        A list of dicts, each with at minimum:
          - 'filer_id': str
          - 'transaction_date': str ('YYYY-MM-DD')
          - 'transaction_code': str (accepted but unused by the classifier)

    Returns
    -------
    A list of booleans in the SAME ORDER as the input. True == opportunistic.

    Notes
    -----
    Order-invariant on input. A transaction does NOT count itself as a
    prior; the focal trade is strictly excluded from its own check.
    """
    if not txns:
        return []

    per_filer_dates: dict[str, list[_dt.date]] = defaultdict(list)
    parsed_dates: list[_dt.date] = []
    for t in txns:
        d = _parse_date(t["transaction_date"])
        parsed_dates.append(d)
        per_filer_dates[t["filer_id"]].append(d)

    for fid in per_filer_dates:
        per_filer_dates[fid].sort()

    out: list[bool] = []
    for i, t in enumerate(txns):
        fid = t["filer_id"]
        focal_date = parsed_dates[i]
        focal_month = focal_date.month
        cutoff = _years_before(focal_date, _LOOKBACK_YEARS)

        same_month_count = 0
        for d in per_filer_dates[fid]:
            if d >= focal_date:
                break
            if d < cutoff:
                continue
            if d.month == focal_month:
                same_month_count += 1
                if same_month_count >= _ROUTINE_THRESHOLD:
                    break

        is_routine = same_month_count >= _ROUTINE_THRESHOLD
        out.append(not is_routine)

    return out
```

- [ ] **Step 5: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/methodology/test_opportunistic_insider.py -v
```

Expected: All 13 tests PASS.

- [ ] **Step 6: Run full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: ~278 passed.

- [ ] **Step 7: Commit**

```
git add src/methodology/__init__.py src/methodology/opportunistic_insider.py tests/methodology/__init__.py tests/methodology/test_opportunistic_insider.py
git commit -m "feat(methodology): Cohen-Malloy-Pomorski opportunistic-insider classifier (pure function, v1.0)"
```

---

## Task 4: Form 4 XML parser (`edgar_form4_parser.py`)

**Files:**
- Create: `tests/fixtures/edgar/form4_sample.xml`
- Create: `src/common/datasources/edgar_form4_parser.py`
- Create: `tests/datasources/test_edgar_form4_parser.py`

- [ ] **Step 1: Create the synthetic Form 4 XML fixture**

Create `tests/fixtures/edgar/form4_sample.xml` with the literal content below. This is a SYNTHETIC fixture modeled on the SEC's Form 4 XML schema (Form 345 specification).

```xml
<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
  <schemaVersion>X0306</schemaVersion>
  <documentType>4</documentType>
  <periodOfReport>2026-03-15</periodOfReport>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>Apple Inc.</issuerName>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001214156</rptOwnerCik>
      <rptOwnerName>COOK TIMOTHY D</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerAddress>
      <rptOwnerStreet1>ONE APPLE PARK WAY</rptOwnerStreet1>
      <rptOwnerCity>CUPERTINO</rptOwnerCity>
      <rptOwnerState>CA</rptOwnerState>
      <rptOwnerZipCode>95014</rptOwnerZipCode>
    </reportingOwnerAddress>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <isOther>0</isOther>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle>
        <value>Common Stock</value>
      </securityTitle>
      <transactionDate>
        <value>2026-03-15</value>
      </transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>P</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares>
          <value>1000</value>
        </transactionShares>
        <transactionPricePerShare>
          <value>175.25</value>
        </transactionPricePerShare>
        <transactionAcquiredDisposedCode>
          <value>A</value>
        </transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction>
          <value>3279000</value>
        </sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership>
          <value>D</value>
        </directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle>
        <value>Common Stock</value>
      </securityTitle>
      <transactionDate>
        <value>2026-03-15</value>
      </transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>S</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares>
          <value>500</value>
        </transactionShares>
        <transactionPricePerShare>
          <value>176.00</value>
        </transactionPricePerShare>
        <transactionAcquiredDisposedCode>
          <value>D</value>
        </transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction>
          <value>3278500</value>
        </sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership>
          <value>D</value>
        </directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <derivativeTable>
    <derivativeTransaction>
      <securityTitle>
        <value>Restricted Stock Unit</value>
      </securityTitle>
      <conversionOrExercisePrice>
        <footnoteId id="F1"/>
      </conversionOrExercisePrice>
      <transactionDate>
        <value>2026-03-15</value>
      </transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>M</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares>
          <value>2500</value>
        </transactionShares>
        <transactionPricePerShare>
          <value>0</value>
        </transactionPricePerShare>
        <transactionAcquiredDisposedCode>
          <value>D</value>
        </transactionAcquiredDisposedCode>
      </transactionAmounts>
    </derivativeTransaction>
  </derivativeTable>
  <footnotes>
    <footnote id="F1">Each restricted stock unit represents a contingent right to receive one share of Apple common stock.</footnote>
  </footnotes>
  <ownerSignature>
    <signatureName>/s/ Sam Whittington, Attorney-in-Fact</signatureName>
    <signatureDate>2026-03-17</signatureDate>
  </ownerSignature>
</ownershipDocument>
```

- [ ] **Step 2: Write the failing tests**

Create `tests/datasources/test_edgar_form4_parser.py`:

```python
"""Tests for the pure-function Form 4 XML parser (Phase A.3.4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.datasources.edgar_form4_parser import (
    parse_form4_xml,
    Form4ParseError,
)
from src.common.schemas import RawEdgarInsiderRow


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "form4_sample.xml"


@pytest.fixture
def xml_bytes() -> bytes:
    return FIXTURE.read_bytes()


def test_parse_form4_returns_non_derivative_rows(xml_bytes):
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    non_deriv = [r for r in rows if not r.is_derivative]
    assert len(non_deriv) == 2


def test_parse_form4_extracts_filer_info(xml_bytes):
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    assert rows[0].filer_name == "COOK TIMOTHY D"
    assert rows[0].filer_title == "Chief Executive Officer"
    assert rows[0].filer_is_officer is True
    assert rows[0].filer_is_director is True
    assert rows[0].filer_is_10pct_owner is False


def test_parse_form4_extracts_purchase_correctly(xml_bytes):
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    purchase = next(r for r in rows if r.transaction_code == "P")
    assert purchase.transaction_date == "2026-03-15"
    assert purchase.shares == 1000.0
    assert purchase.price_per_share == 175.25
    assert abs(purchase.total_value - 1000.0 * 175.25) < 1e-3
    assert purchase.shares_after_transaction == 3279000.0


def test_parse_form4_extracts_sale_correctly(xml_bytes):
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    sale = next(r for r in rows if r.transaction_code == "S")
    assert sale.shares == 500.0
    assert sale.price_per_share == 176.0
    assert abs(sale.total_value - 500.0 * 176.0) < 1e-3
    assert sale.shares_after_transaction == 3278500.0


def test_parse_form4_optionally_extracts_derivative_with_flag(xml_bytes):
    """Derivative transactions are tagged with is_derivative=True."""
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
        include_derivative=True,
    )
    deriv = [r for r in rows if r.is_derivative]
    assert len(deriv) == 1
    assert deriv[0].transaction_code == "M"
    assert deriv[0].shares == 2500.0


def test_parse_form4_skips_derivative_by_default(xml_bytes):
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    assert all(r.is_derivative is False for r in rows)


def test_parse_form4_stamps_source_accn_and_cik(xml_bytes):
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    for r in rows:
        assert r.cik == "0000320193"
        assert r.source_filing_accn == "0000320193-26-000045"
        assert r.ticker == "AAPL"


def test_parse_form4_initial_is_opportunistic_is_none(xml_bytes):
    """The parser does NOT classify. is_opportunistic is None at parse time."""
    rows = parse_form4_xml(
        xml_bytes,
        cik="0000320193",
        accn="0000320193-26-000045",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    for r in rows:
        assert r.is_opportunistic is None
        assert r.opportunistic_classifier_version is None


def test_parse_form4_empty_xml_raises():
    with pytest.raises(Form4ParseError):
        parse_form4_xml(
            b"<?xml version='1.0'?><root/>",
            cik="0000320193",
            accn="0000320193-26-000045",
            ticker="AAPL",
            scrape_timestamp="2026-05-21T08:00:00Z",
            run_id="r1",
        )


def test_parse_form4_malformed_xml_raises():
    with pytest.raises(Form4ParseError):
        parse_form4_xml(
            b"not xml at all",
            cik="0000320193",
            accn="0000320193-26-000045",
            ticker="AAPL",
            scrape_timestamp="2026-05-21T08:00:00Z",
            run_id="r1",
        )


def test_parse_form4_returns_empty_when_no_non_derivative_table():
    """A Form 4 with only derivative transactions returns empty (default)."""
    only_deriv_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-03-15</periodOfReport>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>Apple Inc.</issuerName>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001214156</rptOwnerCik>
      <rptOwnerName>SOMEONE ELSE</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
    </reportingOwnerRelationship>
  </reportingOwner>
  <derivativeTable>
    <derivativeTransaction>
      <transactionDate><value>2026-03-15</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
      </transactionAmounts>
    </derivativeTransaction>
  </derivativeTable>
</ownershipDocument>"""
    rows = parse_form4_xml(
        only_deriv_xml,
        cik="0000320193",
        accn="0000320193-26-999999",
        ticker="AAPL",
        scrape_timestamp="2026-05-21T08:00:00Z",
        run_id="r1",
    )
    assert rows == []
```

- [ ] **Step 3: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_form4_parser.py -v
```

Expected: `ImportError` on `edgar_form4_parser`.

- [ ] **Step 4: Create the parser module**

Create `src/common/datasources/edgar_form4_parser.py`:

```python
"""Pure-function SEC Form 4 XML parser.

Form 4 is the SEC's disclosure for insider transactions in the
issuer's equity. The XML schema is the EDGAR Ownership XML
specification (Form 345 / version X0306+). Reference:
  https://www.sec.gov/info/edgar/specifications/form-345.html

Input: the raw XML bytes of the primary Form 4 document.
Output: a list of RawEdgarInsiderRow, one per parsed transaction.

Default behavior parses only `<nonDerivativeTransaction>` blocks
(equity buys/sells). With include_derivative=True, also emits a row
per `<derivativeTransaction>` block tagged with is_derivative=True.

The parser is intentionally tolerant of missing optional fields
(e.g., transactionPricePerShare may be absent for code A grants).
Required fields (transactionCode, transactionShares, transactionDate)
that are missing cause the affected block to be silently skipped.

`is_opportunistic` and `opportunistic_classifier_version` are NOT set
by this parser; they remain None on the produced rows. Those fields
are populated post-parse by
`src.methodology.opportunistic_insider.classify_transactions` running
across the aggregated batch of Form 4 line items.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional

from src.common.schemas import RawEdgarInsiderRow


class Form4ParseError(RuntimeError):
    """Raised when the XML is not parseable or required Form 4 nodes are missing."""


def _text(node: Optional[ET.Element]) -> Optional[str]:
    if node is None or node.text is None:
        return None
    s = node.text.strip()
    return s or None


def _value_text(parent: Optional[ET.Element], tag: str) -> Optional[str]:
    """Find <tag><value>...</value></tag> and return the value text."""
    if parent is None:
        return None
    child = parent.find(tag)
    if child is None:
        return None
    v = child.find("value")
    if v is not None:
        return _text(v)
    return _text(child)


def _direct_text(parent: Optional[ET.Element], tag: str) -> Optional[str]:
    if parent is None:
        return None
    child = parent.find(tag)
    return _text(child)


def _bool_from_01(s: Optional[str]) -> Optional[bool]:
    if s is None:
        return None
    s = s.strip().lower()
    if s in ("1", "true", "y", "yes"):
        return True
    if s in ("0", "false", "n", "no"):
        return False
    return None


def _float_or_none(s: Optional[str]) -> Optional[float]:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _extract_reporting_owner(root: ET.Element) -> dict:
    owner = root.find("reportingOwner")
    if owner is None:
        raise Form4ParseError("missing <reportingOwner>")
    rid = owner.find("reportingOwnerId")
    rel = owner.find("reportingOwnerRelationship")

    name = _direct_text(rid, "rptOwnerName") or "UNKNOWN"
    title = _direct_text(rel, "officerTitle")
    is_officer = _bool_from_01(_direct_text(rel, "isOfficer"))
    is_director = _bool_from_01(_direct_text(rel, "isDirector"))
    is_10pct = _bool_from_01(_direct_text(rel, "isTenPercentOwner"))
    return {
        "filer_name": name,
        "filer_title": title,
        "filer_is_officer": is_officer,
        "filer_is_director": is_director,
        "filer_is_10pct_owner": is_10pct,
    }


_TXN_CODE_DESCRIPTIONS = {
    "P": "Open-market or private purchase",
    "S": "Open-market or private sale",
    "A": "Grant or award (Rule 16b-3)",
    "M": "Exercise or conversion of derivative security",
    "D": "Disposition to issuer (Rule 16b-3)",
    "F": "Payment of exercise price or tax via share withholding",
    "G": "Bona fide gift",
    "J": "Other acquisition or disposition",
    "C": "Conversion of derivative security",
    "E": "Expiration of short derivative position",
    "H": "Expiration of long derivative position",
    "I": "Discretionary transaction (Rule 16b-3(f))",
    "O": "Exercise of out-of-money derivative",
    "X": "Exercise of in/at-money derivative",
    "V": "Transaction voluntarily reported early",
    "W": "Acquisition by will or laws of descent",
    "Z": "Voting trust deposit or withdrawal",
    "K": "Equity swap or similar instrument",
    "L": "Small acquisition (Rule 16a-6)",
    "U": "Disposition via tender in change-of-control",
}


def _parse_transaction(
    txn_node: ET.Element,
    owner_info: dict,
    is_derivative: bool,
    *,
    run_id: str,
    ticker: str,
    cik: str,
    accn: str,
    scrape_timestamp: str,
) -> Optional[RawEdgarInsiderRow]:
    txn_date = _value_text(txn_node, "transactionDate")
    coding = txn_node.find("transactionCoding")
    txn_code = _direct_text(coding, "transactionCode") if coding is not None else None
    amounts = txn_node.find("transactionAmounts")
    shares_s = _value_text(amounts, "transactionShares")
    price_s = _value_text(amounts, "transactionPricePerShare")
    post = txn_node.find("postTransactionAmounts")
    post_shares_s = _value_text(post, "sharesOwnedFollowingTransaction") if post is not None else None

    if not txn_date or not txn_code or shares_s is None:
        return None

    shares = _float_or_none(shares_s)
    if shares is None:
        return None
    price = _float_or_none(price_s)
    post_shares = _float_or_none(post_shares_s)

    total_value = None
    if price is not None and shares is not None:
        total_value = round(price * shares, 4)

    try:
        return RawEdgarInsiderRow(
            run_id=run_id,
            ticker=ticker,
            cik=cik,
            filer_name=owner_info["filer_name"],
            filer_title=owner_info.get("filer_title"),
            filer_is_officer=owner_info.get("filer_is_officer"),
            filer_is_director=owner_info.get("filer_is_director"),
            filer_is_10pct_owner=owner_info.get("filer_is_10pct_owner"),
            transaction_date=txn_date,
            transaction_code=txn_code,
            transaction_code_description=_TXN_CODE_DESCRIPTIONS.get(txn_code),
            shares=abs(shares),
            price_per_share=price,
            total_value=total_value,
            shares_after_transaction=post_shares,
            is_opportunistic=None,
            opportunistic_classifier_version=None,
            is_derivative=is_derivative,
            source_filing_accn=accn,
            scrape_timestamp=scrape_timestamp,
        )
    except Exception:
        return None


def parse_form4_xml(
    xml_bytes: bytes,
    *,
    cik: str,
    accn: str,
    ticker: str | None,
    scrape_timestamp: str,
    run_id: str,
    include_derivative: bool = False,
) -> list[RawEdgarInsiderRow]:
    """Parse a Form 4 XML document into a list of RawEdgarInsiderRow.

    Parameters
    ----------
    xml_bytes
        The raw XML bytes.
    cik
        Issuer CIK (10-digit padded).
    accn
        SEC accession number, used as source_filing_accn.
    ticker
        Issuer ticker symbol (required).
    scrape_timestamp
        ISO-8601 UTC scrape time.
    run_id
        Run identifier.
    include_derivative
        If True, also emit rows for <derivativeTransaction> blocks
        tagged with is_derivative=True. Default False.

    Raises
    ------
    Form4ParseError
        If the XML is malformed, missing <reportingOwner>, or otherwise
        not a Form 4 ownership document.
    """
    if ticker is None:
        raise Form4ParseError("ticker is required for Form 4 parse (PK constraint)")

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise Form4ParseError(f"malformed XML: {exc}") from exc

    if root.tag != "ownershipDocument":
        raise Form4ParseError(f"not a Form 4 ownershipDocument; root tag = {root.tag!r}")

    owner_info = _extract_reporting_owner(root)

    out: list[RawEdgarInsiderRow] = []

    nd_table = root.find("nonDerivativeTable")
    if nd_table is not None:
        for txn in nd_table.findall("nonDerivativeTransaction"):
            row = _parse_transaction(
                txn, owner_info, is_derivative=False,
                run_id=run_id, ticker=ticker, cik=cik,
                accn=accn, scrape_timestamp=scrape_timestamp,
            )
            if row is not None:
                out.append(row)

    if include_derivative:
        d_table = root.find("derivativeTable")
        if d_table is not None:
            for txn in d_table.findall("derivativeTransaction"):
                row = _parse_transaction(
                    txn, owner_info, is_derivative=True,
                    run_id=run_id, ticker=ticker, cik=cik,
                    accn=accn, scrape_timestamp=scrape_timestamp,
                )
                if row is not None:
                    out.append(row)

    return out
```

- [ ] **Step 5: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_form4_parser.py -v
```

Expected: All 11 tests PASS.

- [ ] **Step 6: Commit**

```
git add src/common/datasources/edgar_form4_parser.py tests/datasources/test_edgar_form4_parser.py tests/fixtures/edgar/form4_sample.xml
git commit -m "feat(datasources): edgar_form4_parser - pure-function Form 4 XML -> RawEdgarInsiderRow"
```

---

## Task 5: EDGAR submissions parser (`edgar_submissions_parser.py`)

**Files:**
- Create: `tests/fixtures/edgar/submissions_CIK0000320193.json`
- Create: `src/common/datasources/edgar_submissions_parser.py`
- Create: `tests/datasources/test_edgar_submissions_parser.py`

- [ ] **Step 1: Create the synthetic submissions JSON fixture**

Create `tests/fixtures/edgar/submissions_CIK0000320193.json`. SYNTHETIC excerpt structured like the SEC's submissions endpoint response:

```json
{
  "cik": "320193",
  "entityType": "operating",
  "name": "Apple Inc.",
  "tickers": ["AAPL"],
  "exchanges": ["Nasdaq"],
  "filings": {
    "recent": {
      "accessionNumber": [
        "0000320193-26-000045",
        "0000320193-26-000030",
        "0000320193-26-000010",
        "0000320193-25-000158",
        "0000320193-25-000123",
        "0000320193-24-000158",
        "0000320193-26-000200"
      ],
      "filingDate": [
        "2026-03-17",
        "2026-02-15",
        "2026-02-01",
        "2025-11-01",
        "2025-08-02",
        "2024-11-01",
        "2026-04-10"
      ],
      "acceptanceDateTime": [
        "2026-03-17T18:00:00.000Z",
        "2026-02-15T17:00:00.000Z",
        "2026-02-01T16:30:01.000Z",
        "2025-10-31T18:09:50.000Z",
        "2025-08-01T18:05:00.000Z",
        "2024-10-31T18:09:50.000Z",
        "2026-04-10T16:00:00.000Z"
      ],
      "form": [
        "4",
        "4",
        "8-K",
        "10-K",
        "10-Q",
        "10-K",
        "DEF 14A"
      ],
      "items": [
        "",
        "",
        "2.02,9.01",
        "",
        "",
        "",
        ""
      ],
      "primaryDocument": [
        "wk-form4_1742068800.xml",
        "wk-form4_1739635200.xml",
        "aapl-20260201.htm",
        "aapl-20251101.htm",
        "aapl-20250802.htm",
        "aapl-20241101.htm",
        "aapl-def14a_20260410.htm"
      ]
    }
  }
}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/datasources/test_edgar_submissions_parser.py`:

```python
"""Tests for the pure-function EDGAR submissions parser (Phase A.3.4)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.common.datasources.edgar_submissions_parser import (
    parse_submissions,
    DEFAULT_FORM_TYPES,
)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "submissions_CIK0000320193.json"


@pytest.fixture
def blob():
    return json.loads(FIXTURE.read_text())


def test_parse_submissions_returns_rows_and_form4_accns(blob):
    rows, form4_accns = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    assert len(rows) > 0
    assert len(form4_accns) > 0


def test_parse_submissions_filters_by_default_form_types(blob):
    """Default form filter excludes DEF 14A."""
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    forms = sorted({r.form_type for r in rows})
    assert "DEF 14A" not in forms
    assert "4" in forms
    assert "8-K" in forms
    assert "10-K" in forms
    assert "10-Q" in forms


def test_parse_submissions_extracts_form4_accns(blob):
    _, form4_accns = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    assert sorted(form4_accns) == [
        "0000320193-26-000030", "0000320193-26-000045"
    ]


def test_parse_submissions_extracts_item_codes_for_8k(blob):
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    eight_k = next(r for r in rows if r.form_type == "8-K")
    assert eight_k.item_codes == "2.02,9.01"


def test_parse_submissions_form_4_item_codes_normalize_to_none(blob):
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    form4_rows = [r for r in rows if r.form_type == "4"]
    assert all(r.item_codes is None for r in form4_rows)


def test_parse_submissions_constructs_primary_doc_url(blob):
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    eight_k = next(r for r in rows if r.form_type == "8-K")
    assert eight_k.primary_doc_url is not None
    assert "Archives/edgar/data/320193" in eight_k.primary_doc_url
    assert "000032019326000010" in eight_k.primary_doc_url
    assert "aapl-20260201.htm" in eight_k.primary_doc_url


def test_parse_submissions_respects_last_n_days(blob):
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
        last_n_days=90,
    )
    dates = sorted({r.filing_date for r in rows})
    # Floor relative to max filingDate 2026-04-10 minus 90 days = 2026-01-10
    for d in dates:
        assert d >= "2026-01-10"


def test_parse_submissions_custom_form_types(blob):
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
        form_types={"8-K"},
    )
    forms = {r.form_type for r in rows}
    assert forms == {"8-K"}


def test_parse_submissions_extracts_cik_from_top_level(blob):
    rows, _ = parse_submissions(
        blob, ticker="AAPL",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    for r in rows:
        assert r.cik == "0000320193"


def test_default_form_types_constant_includes_all_required():
    required = {"8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A", "4", "4/A"}
    assert required.issubset(DEFAULT_FORM_TYPES)


def test_parse_submissions_empty_filings_block_returns_empty():
    blob = {
        "cik": "320193",
        "name": "Empty Co",
        "tickers": ["EMP"],
        "filings": {"recent": {
            "accessionNumber": [],
            "filingDate": [],
            "acceptanceDateTime": [],
            "form": [],
            "items": [],
            "primaryDocument": [],
        }},
    }
    rows, form4_accns = parse_submissions(
        blob, ticker="EMP",
        run_id="r1",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    assert rows == []
    assert form4_accns == []
```

- [ ] **Step 3: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_submissions_parser.py -v
```

Expected: `ImportError` on `edgar_submissions_parser`.

- [ ] **Step 4: Create the parser module**

Create `src/common/datasources/edgar_submissions_parser.py`:

```python
"""Pure-function parser for the SEC EDGAR submissions index JSON.

Input: the JSON returned by
  https://data.sec.gov/submissions/CIK{padded}.json

This file contains a per-issuer list of recent SEC filings. The
`filings.recent` block is a column-oriented structure: each filing
field (accessionNumber, filingDate, form, etc.) is a list, and the
i-th element across all lists describes the i-th filing.

Output: a tuple of
  - list[RawEdgarFilingRow]: one envelope row per filing whose form
    matches the filter
  - list[str]: the subset of accession numbers whose form_type == "4"
    (or "4/A"), so the orchestrator knows which Form 4 XML docs to
    fetch next.

The primary_doc_url for each filing is constructed by EDGAR's
deterministic Archive path scheme:
  https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accn_no_dashes}/{primary_doc}

Pure-function — no HTTP, no DB, no globals.
"""
from __future__ import annotations

import datetime as _dt
from typing import Iterable, Optional

from src.common.schemas import RawEdgarFilingRow


# Default filter for which forms to record in raw_edgar_filings.
DEFAULT_FORM_TYPES: frozenset[str] = frozenset({
    "8-K", "8-K/A",
    "10-K", "10-K/A",
    "10-Q", "10-Q/A",
    "4", "4/A",
})


_ARCHIVE_URL_FMT = (
    "https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accn_clean}/{primary_doc}"
)


def _build_primary_doc_url(
    cik_unpadded: str, accn: str, primary_doc: str
) -> Optional[str]:
    """Construct the canonical Archives URL for a filing's primary document.

    accn format from SEC: "NNNNNNNNNN-YY-NNNNNN" (e.g., "0000320193-26-000045").
    The Archives path uses the accn with dashes stripped:
      .../Archives/edgar/data/{cik_unpadded}/000032019326000045/aapl-...xml
    """
    if not accn or not primary_doc:
        return None
    accn_clean = accn.replace("-", "")
    return _ARCHIVE_URL_FMT.format(
        cik_unpadded=cik_unpadded.lstrip("0") or "0",
        accn_clean=accn_clean,
        primary_doc=primary_doc,
    )


def _date_floor(blob_dates: list[str], last_n_days: Optional[int]) -> Optional[str]:
    """Lower-bound date (ISO) for last_n_days filtering, anchored on max date."""
    if not last_n_days:
        return None
    if not blob_dates:
        return None
    try:
        latest = max(_dt.date.fromisoformat(d) for d in blob_dates if d)
    except ValueError:
        return None
    return (latest - _dt.timedelta(days=last_n_days)).isoformat()


def parse_submissions(
    blob: dict,
    *,
    ticker: str,
    run_id: str,
    scrape_timestamp: str,
    form_types: Optional[Iterable[str]] = None,
    last_n_days: Optional[int] = None,
) -> tuple[list[RawEdgarFilingRow], list[str]]:
    """Parse the submissions JSON.

    Parameters
    ----------
    blob
        The full JSON dict from /submissions/CIK{padded}.json.
    ticker
        Issuer ticker, stamped onto every emitted row.
    run_id, scrape_timestamp
        Provenance fields.
    form_types
        Iterable of form-type strings to include. Default
        DEFAULT_FORM_TYPES.
    last_n_days
        If set, drop filings older than `last_n_days` from the latest
        filing in the blob. None = no time filter.

    Returns
    -------
    (filing_rows, form4_accns)
    """
    types_filter = frozenset(form_types) if form_types is not None else DEFAULT_FORM_TYPES

    cik_raw = str(blob.get("cik", "")).strip()
    if not cik_raw:
        return [], []
    cik_padded = cik_raw.lstrip("0").zfill(10) if cik_raw.isdigit() else cik_raw
    cik_unpadded = cik_padded.lstrip("0") or "0"

    recent = (blob.get("filings") or {}).get("recent") or {}
    accns: list[str] = recent.get("accessionNumber") or []
    filing_dates: list[str] = recent.get("filingDate") or []
    accepted_ats: list[str] = recent.get("acceptanceDateTime") or []
    forms: list[str] = recent.get("form") or []
    items_list: list[str] = recent.get("items") or []
    primary_docs: list[str] = recent.get("primaryDocument") or []

    floor = _date_floor(filing_dates, last_n_days)

    rows: list[RawEdgarFilingRow] = []
    form4_accns: list[str] = []

    n = min(len(accns), len(filing_dates), len(forms))
    for i in range(n):
        form_type = (forms[i] or "").strip()
        if form_type not in types_filter:
            continue
        filing_date = (filing_dates[i] or "").strip()
        if floor and filing_date and filing_date < floor:
            continue
        accn = (accns[i] or "").strip()
        if not accn:
            continue

        accepted_at = accepted_ats[i] if i < len(accepted_ats) else None
        items_raw = items_list[i] if i < len(items_list) else None
        item_codes = items_raw.strip() if items_raw else None
        if item_codes == "":
            item_codes = None
        primary_doc = (primary_docs[i] or "").strip() if i < len(primary_docs) else ""
        primary_doc_url = _build_primary_doc_url(cik_unpadded, accn, primary_doc)

        try:
            row = RawEdgarFilingRow(
                run_id=run_id,
                ticker=ticker,
                cik=cik_padded,
                form_type=form_type,
                filing_date=filing_date,
                accepted_at=accepted_at,
                item_codes=item_codes,
                source_filing_accn=accn,
                primary_doc_url=primary_doc_url,
                scrape_timestamp=scrape_timestamp,
            )
        except Exception:
            continue
        rows.append(row)

        if form_type in ("4", "4/A"):
            form4_accns.append(accn)

    return rows, form4_accns
```

- [ ] **Step 5: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_submissions_parser.py -v
```

Expected: All 11 tests PASS.

- [ ] **Step 6: Commit**

```
git add src/common/datasources/edgar_submissions_parser.py tests/datasources/test_edgar_submissions_parser.py tests/fixtures/edgar/submissions_CIK0000320193.json
git commit -m "feat(datasources): edgar_submissions_parser - pure-function submissions JSON -> filings + Form 4 accn list"
```

---

## Task 6: `EdgarSource.fetch_filings_for_ticker` real implementation

**Files:**
- Modify: `src/common/datasources/edgar_source.py`
- Create: `tests/datasources/test_edgar_filings_fetch.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/datasources/test_edgar_filings_fetch.py`:

```python
"""Tests for EdgarSource.fetch_filings_for_ticker (Phase A.3.4)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.edgar_source import EdgarSource


FIXTURE_SUB = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "submissions_CIK0000320193.json"
FIXTURE_F4 = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "form4_sample.xml"
FIXTURE_CT = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "company_tickers.json"


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v6()
    return mgr


@pytest.fixture
def sub_blob():
    return json.loads(FIXTURE_SUB.read_text())


@pytest.fixture
def form4_bytes() -> bytes:
    return FIXTURE_F4.read_bytes()


@pytest.fixture
def ct_blob():
    return json.loads(FIXTURE_CT.read_text())


def _seed_cik_map(db: DatabaseManager) -> None:
    """Pre-seed the CIK map + fresh watermark so SecCikLookup doesn't HTTP."""
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


def _wire_filings_mocks(mocker, sub_blob, form4_bytes):
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "submissions/CIK" in url:
            resp.json.return_value = sub_blob
            resp.content = json.dumps(sub_blob).encode()
        elif url.endswith(".xml"):
            resp.content = form4_bytes
        else:
            resp.ok = False
            resp.status_code = 404
            resp.content = b""
        return resp
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )


def test_fetch_filings_writes_filings_rows(db, mocker, sub_blob, form4_bytes):
    _seed_cik_map(db)
    _wire_filings_mocks(mocker, sub_blob, form4_bytes)
    src = EdgarSource()
    n_filings, n_insider = src.fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    assert n_filings > 0
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT form_type, COUNT(*) FROM raw_edgar_filings "
            "WHERE ticker='AAPL' GROUP BY form_type ORDER BY form_type"
        ).fetchall()
    forms = dict(rows)
    assert forms.get("4", 0) == 2
    assert forms.get("8-K", 0) == 1
    assert forms.get("10-K", 0) >= 1


def test_fetch_filings_writes_insider_rows_from_form4(db, mocker, sub_blob, form4_bytes):
    _seed_cik_map(db)
    _wire_filings_mocks(mocker, sub_blob, form4_bytes)
    n_filings, n_insider = EdgarSource().fetch_filings_for_ticker(
        "AAPL", run_id="r1", db=db,
    )
    assert n_insider > 0
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT transaction_code, COUNT(*) FROM raw_edgar_insider "
            "WHERE ticker='AAPL' GROUP BY transaction_code"
        ).fetchall()
    codes = dict(rows)
    # Two Form 4 envelopes resolve to the same XML mock; INSERT OR IGNORE
    # collapses identical (cik, accn, filer, date, code) tuples per accn,
    # so 2 distinct accns x (1 P + 1 S) = 4 rows.
    assert codes.get("P", 0) >= 1
    assert codes.get("S", 0) >= 1


def test_fetch_filings_populates_is_opportunistic(db, mocker, sub_blob, form4_bytes):
    """All parsed insider rows must have is_opportunistic set + version stamped."""
    _seed_cik_map(db)
    _wire_filings_mocks(mocker, sub_blob, form4_bytes)
    EdgarSource().fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    with sqlite3.connect(db.db_path) as c:
        n_null = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider WHERE is_opportunistic IS NULL"
        ).fetchone()[0]
        versions = {
            r[0] for r in c.execute(
                "SELECT opportunistic_classifier_version FROM raw_edgar_insider"
            )
        }
    assert n_null == 0
    assert versions == {"1.0"}


def test_fetch_filings_updates_watermark(db, mocker, sub_blob, form4_bytes):
    _seed_cik_map(db)
    _wire_filings_mocks(mocker, sub_blob, form4_bytes)
    EdgarSource().fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    w = db.get_watermark("edgar", "AAPL", "filings")
    assert w is not None
    # Latest included filing date in the fixture (DEF 14A is filtered out).
    assert w["last_observation_date"] == "2026-03-17"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_fetch_filings_skips_when_watermark_matches_latest(db, mocker, sub_blob, form4_bytes):
    """If last_observation_date >= latest filing, don't re-fetch Form 4 XMLs."""
    _seed_cik_map(db)
    db.upsert_watermark(
        source="edgar", ticker="AAPL", field="filings",
        last_observation_date="2026-03-17", success=True,
    )

    submissions_calls = []
    form4_calls = []

    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "submissions/CIK" in url:
            submissions_calls.append(url)
            resp.json.return_value = sub_blob
            resp.content = json.dumps(sub_blob).encode()
        elif url.endswith(".xml"):
            form4_calls.append(url)
            resp.content = form4_bytes
        return resp
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )

    n_filings, n_insider = EdgarSource().fetch_filings_for_ticker(
        "AAPL", run_id="r1", db=db,
    )
    assert n_filings == 0
    assert n_insider == 0
    assert len(submissions_calls) == 1
    assert form4_calls == []


def test_fetch_filings_records_failure_on_submissions_error(db, mocker):
    _seed_cik_map(db)
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=RuntimeError("submissions 503"),
    )
    n_filings, n_insider = EdgarSource().fetch_filings_for_ticker(
        "AAPL", run_id="r1", db=db,
    )
    assert (n_filings, n_insider) == (0, 0)
    w = db.get_watermark("edgar", "AAPL", "filings")
    assert w is not None
    assert w["error_count"] >= 1


def test_fetch_filings_continues_when_one_form4_xml_fails(db, mocker, sub_blob, form4_bytes):
    """If one Form 4 XML fetch fails, the batch continues for other Form 4s."""
    _seed_cik_map(db)
    call_counter = {"f4": 0}

    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "submissions/CIK" in url:
            resp.json.return_value = sub_blob
            resp.content = json.dumps(sub_blob).encode()
        elif url.endswith(".xml"):
            call_counter["f4"] += 1
            if call_counter["f4"] == 1:
                resp.content = b"not xml"
            else:
                resp.content = form4_bytes
        return resp
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )

    n_filings, n_insider = EdgarSource().fetch_filings_for_ticker(
        "AAPL", run_id="r1", db=db,
    )
    assert n_insider >= 2


def test_fetch_filings_uses_sec_user_agent(db, mocker, sub_blob, form4_bytes):
    _seed_cik_map(db)
    captured = []

    def side_effect(url, **kwargs):
        captured.append((url, kwargs.get("headers", {})))
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "submissions/CIK" in url:
            resp.json.return_value = sub_blob
            resp.content = json.dumps(sub_blob).encode()
        elif url.endswith(".xml"):
            resp.content = form4_bytes
        return resp
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )

    EdgarSource().fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    sub_call = next(c for c in captured if "submissions/CIK" in c[0])
    assert "User-Agent" in sub_call[1]
    assert "@" in sub_call[1]["User-Agent"]


def test_fetch_filings_calls_correct_submissions_url(db, mocker, sub_blob, form4_bytes):
    _seed_cik_map(db)
    captured = []

    def side_effect(url, **kwargs):
        captured.append(url)
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "submissions/CIK" in url:
            resp.json.return_value = sub_blob
            resp.content = json.dumps(sub_blob).encode()
        elif url.endswith(".xml"):
            resp.content = form4_bytes
        return resp
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )

    EdgarSource().fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    sub_urls = [u for u in captured if "submissions/CIK" in u]
    assert len(sub_urls) == 1
    assert "CIK0000320193.json" in sub_urls[0]


def test_fetch_filings_insert_or_ignore_protects_against_dup(db, mocker, sub_blob, form4_bytes):
    """Second fetch with cleared watermark re-parses but inserts no new rows."""
    _seed_cik_map(db)
    _wire_filings_mocks(mocker, sub_blob, form4_bytes)
    src = EdgarSource()
    src.fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    with sqlite3.connect(db.db_path) as c:
        n_filings_before = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_filings WHERE ticker='AAPL'"
        ).fetchone()[0]
        n_insider_before = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider WHERE ticker='AAPL'"
        ).fetchone()[0]
    db.upsert_watermark(
        source="edgar", ticker="AAPL", field="filings",
        last_observation_date=None, success=True,
    )
    src.fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    with sqlite3.connect(db.db_path) as c:
        n_filings_after = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_filings WHERE ticker='AAPL'"
        ).fetchone()[0]
        n_insider_after = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert n_filings_after == n_filings_before
    assert n_insider_after == n_insider_before
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_filings_fetch.py -v
```

Expected: `AttributeError` on `fetch_filings_for_ticker`.

- [ ] **Step 3: Append `fetch_filings_for_ticker` to `EdgarSource`**

In `src/common/datasources/edgar_source.py`, **locate** the existing module-level `_COMPANYFACTS_URL` constant near the top. Immediately AFTER it, add:

```python
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
```

Then at the **end** of the `EdgarSource` class (after `fetch_universe`), append:

```python

    # ------------------------------------------------------------------
    # Phase A.3.4: Form 4 insider transactions + filings index (8-K etc.)
    # ------------------------------------------------------------------

    def fetch_filings_for_ticker(
        self,
        ticker: str,
        run_id: str,
        db=None,
        *,
        form_types: set[str] | None = None,
        last_n_days: int | None = 90,
    ) -> tuple[int, int]:
        """Pull the submissions index + every Form 4 XML for ticker.

        End-to-end pipeline:
          1. Resolve ticker -> CIK via SecCikLookup.
          2. GET /submissions/CIK{cik}.json (the per-issuer filings index).
          3. parse_submissions() -> (filing_rows, form4_accns).
          4. Short-circuit: if latest filing in the parsed rows <= the
             (edgar, ticker, filings) watermark's last_observation_date,
             skip Form 4 XML fetches and return (0, 0).
          5. For each form4_accn, GET the primary doc XML and
             parse_form4_xml() into RawEdgarInsiderRow records.
          6. classify_transactions() across ALL parsed insider rows from
             this batch to populate is_opportunistic +
             opportunistic_classifier_version per row.
          7. Persist filings + insider rows via INSERT OR IGNORE.
          8. Update (edgar, ticker, filings) watermark with the most-
             recent filing_date observed.

        SEC URL construction
        --------------------
        Submissions JSON:
          https://data.sec.gov/submissions/CIK{padded10}.json
        Form 4 primary XML (built by parse_submissions):
          https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accn_no_dashes}/{primary_doc}
        Source: https://www.sec.gov/edgar/sec-api-documentation +
                https://www.sec.gov/os/accessing-edgar-data

        Returns
        -------
        (n_filings_inserted, n_insider_rows_inserted)
            Counts AFTER INSERT OR IGNORE (i.e., the net new rows).
        """
        if db is None:
            raise ValueError("db is required")

        # 1. CIK resolution
        try:
            cik = self._resolve_cik(ticker, db)
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="filings",
                last_observation_date=None, db=db, success=False,
                error_message=f"cik_resolve: {type(exc).__name__}: {exc}",
            )
            return (0, 0)

        # 2. Pull submissions JSON
        sub_url = _SUBMISSIONS_URL.format(cik=cik)
        try:
            sub_resp = _sec_get_with_retry(sub_url, headers=self._headers())
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="filings",
                last_observation_date=None, db=db, success=False,
                error_message=f"submissions: {type(exc).__name__}: {exc}",
            )
            return (0, 0)
        if not sub_resp.ok:
            self.update_watermark(
                ticker=ticker, field="filings",
                last_observation_date=None, db=db, success=False,
                error_message=f"submissions HTTP {sub_resp.status_code}",
            )
            return (0, 0)
        try:
            sub_blob = sub_resp.json()
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="filings",
                last_observation_date=None, db=db, success=False,
                error_message=f"submissions json: {type(exc).__name__}: {exc}",
            )
            return (0, 0)

        # 3. Parse submissions
        from src.common.datasources.edgar_submissions_parser import parse_submissions
        scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        filing_rows, form4_accns = parse_submissions(
            sub_blob, ticker=ticker, run_id=run_id,
            scrape_timestamp=scrape_ts,
            form_types=form_types,
            last_n_days=last_n_days,
        )

        if not filing_rows:
            self.update_watermark(
                ticker=ticker, field="filings",
                last_observation_date=None, db=db, success=True,
            )
            return (0, 0)

        # 4. Watermark short-circuit on latest filing date
        latest_filing = max(r.filing_date for r in filing_rows)
        w = db.get_watermark(self.name, ticker, "filings")
        prev_obs = (w or {}).get("last_observation_date") if w else None
        if prev_obs and latest_filing <= prev_obs:
            import datetime as _dt
            self.update_watermark(
                ticker=ticker, field="filings",
                last_observation_date=_dt.date.fromisoformat(prev_obs),
                db=db, success=True,
            )
            return (0, 0)

        # 5. Fetch Form 4 XMLs and parse to insider rows
        from src.common.datasources.edgar_form4_parser import (
            parse_form4_xml, Form4ParseError,
        )
        accn_to_url = {
            r.source_filing_accn: r.primary_doc_url
            for r in filing_rows
            if r.form_type in ("4", "4/A") and r.primary_doc_url
        }

        all_insider_rows: list = []
        for accn in form4_accns:
            url = accn_to_url.get(accn)
            if not url:
                continue
            try:
                f4_resp = _sec_get_with_retry(url, headers=self._headers())
            except Exception:
                continue
            if not f4_resp.ok:
                continue
            try:
                rows = parse_form4_xml(
                    f4_resp.content,
                    cik=cik, accn=accn, ticker=ticker,
                    scrape_timestamp=scrape_ts,
                    run_id=run_id,
                )
            except Form4ParseError:
                continue
            all_insider_rows.extend(rows)

        # 6. Run Cohen-Malloy-Pomorski classifier across the batch.
        if all_insider_rows:
            from src.methodology.opportunistic_insider import (
                classify_transactions, OPPORTUNISTIC_CLASSIFIER_VERSION,
            )
            txn_inputs = [
                {
                    "filer_id": f"{r.filer_name}:{r.cik}",
                    "transaction_date": r.transaction_date,
                    "transaction_code": r.transaction_code,
                }
                for r in all_insider_rows
            ]
            labels = classify_transactions(txn_inputs)
            classified = []
            for r, is_opp in zip(all_insider_rows, labels):
                classified.append(r.model_copy(update={
                    "is_opportunistic": bool(is_opp),
                    "opportunistic_classifier_version": OPPORTUNISTIC_CLASSIFIER_VERSION,
                }))
            all_insider_rows = classified

        # 7. Persist (INSERT OR IGNORE on both tables)
        import sqlite3
        with sqlite3.connect(db.db_path) as c:
            n_f_before = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_filings WHERE ticker=?", (ticker,)
            ).fetchone()[0]
            n_i_before = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_insider WHERE ticker=?", (ticker,)
            ).fetchone()[0]

        db.insert_raw_edgar_filings(filing_rows)
        db.insert_raw_edgar_insider(all_insider_rows)

        with sqlite3.connect(db.db_path) as c:
            n_f_after = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_filings WHERE ticker=?", (ticker,)
            ).fetchone()[0]
            n_i_after = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_insider WHERE ticker=?", (ticker,)
            ).fetchone()[0]

        n_filings_inserted = n_f_after - n_f_before
        n_insider_inserted = n_i_after - n_i_before

        # 8. Advance watermark to the latest filing_date observed
        import datetime as _dt
        try:
            last_obs = _dt.date.fromisoformat(latest_filing)
        except ValueError:
            last_obs = None
        self.update_watermark(
            ticker=ticker, field="filings",
            last_observation_date=last_obs, db=db, success=True,
        )

        return (n_filings_inserted, n_insider_inserted)
```

- [ ] **Step 4: Update the `EdgarSource.provides` set to advertise the new fields**

In `src/common/datasources/edgar_source.py`, locate the `provides = { ... }` class attribute on `EdgarSource`. Replace it with:

```python
    provides = {
        "cik", "company_name", "exchange",
        "revenue_ttm", "ebit_ttm", "net_income_ttm",
        "total_assets", "total_debt_to_equity",
        "operating_margin", "net_profit_margin",
        "fcf_ttm", "cash_and_equivalents", "total_debt",
        "interest_coverage",
        # A.3.4 additions:
        "insider_transactions", "insider_opportunistic_buys_30d",
        "insider_opportunistic_sells_30d", "filing_count_8k_30d",
    }
```

- [ ] **Step 5: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_filings_fetch.py -v
```

Expected: All 10 tests PASS.

- [ ] **Step 6: Verify the A.3.3 tests still pass (no regression on `fetch_fundamentals_for_ticker`)**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_edgar_fetch.py -v 2>&1 | tail -5
```

Expected: all A.3.3 EdgarSource fetch tests still pass.

- [ ] **Step 7: Commit**

```
git add src/common/datasources/edgar_source.py tests/datasources/test_edgar_filings_fetch.py
git commit -m "feat(datasources): EdgarSource.fetch_filings_for_ticker - submissions index + Form 4 XML + CMP classifier (watermarked, INSERT OR IGNORE)"
```

---

## Task 7: Integration test (end-to-end mocked HTTP)

**Files:**
- Create: `tests/datasources/test_integration_a3_4.py`

- [ ] **Step 1: Write the integration test**

Create `tests/datasources/test_integration_a3_4.py`:

```python
"""End-to-end orchestration test for A.3.4: ticker -> CIK -> submissions ->
parse -> Form 4 XML fetch -> parse -> CMP classify -> persist -> watermark advance ->
idempotent re-run.

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


FIXTURE_SUB = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "submissions_CIK0000320193.json"
FIXTURE_F4 = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "form4_sample.xml"
FIXTURE_CT = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "company_tickers.json"


def _wire_mocks(mocker, ct_blob, sub_blob, form4_bytes):
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "company_tickers" in url:
            resp.json.return_value = ct_blob
            resp.content = json.dumps(ct_blob).encode()
        elif "submissions/CIK" in url:
            resp.json.return_value = sub_blob
            resp.content = json.dumps(sub_blob).encode()
        elif url.endswith(".xml"):
            resp.content = form4_bytes
        else:
            resp.ok = False
            resp.status_code = 404
            resp.content = b""
        return resp
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        side_effect=side_effect,
    )
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )


def test_a3_4_full_flow_with_watermark_idempotency(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v6()

    ct_blob = json.loads(FIXTURE_CT.read_text())
    sub_blob = json.loads(FIXTURE_SUB.read_text())
    form4_bytes = FIXTURE_F4.read_bytes()
    _wire_mocks(mocker, ct_blob, sub_blob, form4_bytes)

    edgar = EdgarSource()

    n_filings_1, n_insider_1 = edgar.fetch_filings_for_ticker(
        "AAPL", run_id="run-1", db=db,
    )
    assert n_filings_1 > 0
    assert n_insider_1 > 0

    w = db.get_watermark("edgar", "AAPL", "filings")
    assert w is not None
    assert w["last_observation_date"] == "2026-03-17"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0

    with sqlite3.connect(db_path) as c:
        n_unclassified = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider WHERE is_opportunistic IS NULL"
        ).fetchone()[0]
    assert n_unclassified == 0

    with sqlite3.connect(db_path) as c:
        forms = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT form_type FROM raw_edgar_filings WHERE ticker='AAPL'"
            )
        )
    assert "4" in forms
    assert "8-K" in forms
    assert "10-K" in forms

    with sqlite3.connect(db_path) as c:
        item_codes = c.execute(
            "SELECT item_codes FROM raw_edgar_filings WHERE ticker='AAPL' AND form_type='8-K'"
        ).fetchone()[0]
    assert item_codes == "2.02,9.01"

    # Second pass: short-circuit on watermark
    with sqlite3.connect(db_path) as c:
        n_filings_before = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_filings"
        ).fetchone()[0]
        n_insider_before = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider"
        ).fetchone()[0]

    n_filings_2, n_insider_2 = edgar.fetch_filings_for_ticker(
        "AAPL", run_id="run-1", db=db,
    )
    assert n_filings_2 == 0
    assert n_insider_2 == 0

    with sqlite3.connect(db_path) as c:
        n_filings_after = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_filings"
        ).fetchone()[0]
        n_insider_after = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider"
        ).fetchone()[0]
    assert n_filings_after == n_filings_before
    assert n_insider_after == n_insider_before


def test_a3_4_unknown_ticker_records_failure_does_not_break(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v6()

    ct_blob = json.loads(FIXTURE_CT.read_text())
    sub_blob = json.loads(FIXTURE_SUB.read_text())
    form4_bytes = FIXTURE_F4.read_bytes()
    _wire_mocks(mocker, ct_blob, sub_blob, form4_bytes)

    n_filings, n_insider = EdgarSource().fetch_filings_for_ticker(
        "NOTREAL", run_id="r1", db=db,
    )
    assert (n_filings, n_insider) == (0, 0)
    w = db.get_watermark("edgar", "NOTREAL", "filings")
    assert w is not None
    assert w["error_count"] >= 1


def test_a3_4_classifier_version_stamped_on_every_row(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v6()

    ct_blob = json.loads(FIXTURE_CT.read_text())
    sub_blob = json.loads(FIXTURE_SUB.read_text())
    form4_bytes = FIXTURE_F4.read_bytes()
    _wire_mocks(mocker, ct_blob, sub_blob, form4_bytes)

    EdgarSource().fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT DISTINCT opportunistic_classifier_version FROM raw_edgar_insider"
        ).fetchall()
    versions = {r[0] for r in rows}
    assert versions == {"1.0"}
```

- [ ] **Step 2: Run the integration test**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_integration_a3_4.py -v
```

Expected: 3 PASS.

- [ ] **Step 3: Run the full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" -v 2>&1 | tail -5
```

Expected: ~302 passed (235 baseline + 14 schema + 16 database + 13 CMP + 11 form4 + 11 submissions + 10 filings_fetch + 3 integration_a3_4 — counts approximate).

- [ ] **Step 4: Commit**

```
git add tests/datasources/test_integration_a3_4.py
git commit -m "test: A.3.4 integration acceptance - end-to-end submissions + Form 4 + CMP classifier + watermark idempotency"
```

---

## Task 8: Update the build plan

**Files:**
- Modify: the build plan

- [ ] **Step 1: Locate the A.3 row in §5.1.0**

Grep the build plan for `**A.3.1 + A.3.2 + A.3.3 shipped 2026-05-21**` to find the line.

- [ ] **Step 2: Update the A.3 row's shipped marker**

Use `Edit` to replace the substring `**A.3.1 + A.3.2 + A.3.3 shipped 2026-05-21**` with `**A.3.1 + A.3.2 + A.3.3 + A.3.4 shipped 2026-05-21**`, and update the plan-link parenthetical to add the A.3.4 plan file. Concretely, find this fragment:

```
**A.3.1 + A.3.2 + A.3.3 shipped 2026-05-21** (plans: [A.3.1](docs/claude-code/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/claude-code/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md), [A.3.3](docs/claude-code/plans/2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md)): schema v5
```

Replace with:

```
**A.3.1 + A.3.2 + A.3.3 + A.3.4 shipped 2026-05-21** (plans: [A.3.1](docs/claude-code/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/claude-code/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md), [A.3.3](docs/claude-code/plans/2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md), [A.3.4](docs/claude-code/plans/2026-05-21-phase-a3-sub04-edgar-insider-and-filings.md)): schema v6
```

Then in the same sentence, append after the existing A.3.3 fragment:

```
 + EdgarSource.fetch_filings_for_ticker + raw_edgar_insider Form 4 line-item table + raw_edgar_filings 8-K/10-K/10-Q/Form-4 envelope table + edgar_form4_parser + edgar_submissions_parser pure-function modules + Cohen-Malloy-Pomorski opportunistic-insider classifier (v1.0) at src/methodology/opportunistic_insider.py
```

- [ ] **Step 3: Commit**

```
git add <build-plan>
git commit -m "docs: mark A.3.4 shipped - EDGAR Form 4 + 8-K filings + Cohen-Malloy-Pomorski opportunistic classifier"
```

---

## Phase A.3.4 — Definition of Done

- [ ] `./venv/Scripts/python.exe -m pytest -m "not integration"` shows ~302 tests passing
- [ ] `migrate_to_v6()` produces `schema_version == 6`
- [ ] `raw_edgar_insider` table exists with PK `(cik, source_filing_accn, filer_name, transaction_date, transaction_code)`
- [ ] `raw_edgar_filings` table exists with PK `(cik, source_filing_accn)`
- [ ] `insert_raw_edgar_insider()` uses INSERT OR IGNORE (amendments do NOT overwrite original)
- [ ] `insert_raw_edgar_filings()` uses INSERT OR IGNORE
- [ ] `RawEdgarInsiderRow.transaction_code` is a Literal of the 20 SEC Form 4 codes (P/S/A/M/D/F/G/J/C/E/H/I/O/X/V/W/Z/K/L/U)
- [ ] `RawEdgarFilingRow.form_type` is a Literal of {8-K, 8-K/A, 10-K, 10-K/A, 10-Q, 10-Q/A, 4, 4/A}
- [ ] `parse_form4_xml()` is pure-function: bytes in, list of RawEdgarInsiderRow out, no I/O
- [ ] `parse_form4_xml()` extracts filer name, title, is_officer/is_director/is_10pct, transaction date/code/shares/price/value/shares-after for each `<nonDerivativeTransaction>`
- [ ] `parse_form4_xml(include_derivative=True)` also emits `<derivativeTransaction>` rows with `is_derivative=True`
- [ ] `parse_submissions()` is pure-function: dict in, (list[RawEdgarFilingRow], list[str]) out, no I/O
- [ ] `parse_submissions()` filters by form_type (default DEFAULT_FORM_TYPES) and by last_n_days
- [ ] `parse_submissions()` constructs the Archives URL `https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accn_no_dashes}/{primary_doc}` correctly
- [ ] `classify_transactions()` implements the CMP rule: routine iff filer has ≥2 same-calendar-month transactions in the prior 3 years (strictly before focal date)
- [ ] `OPPORTUNISTIC_CLASSIFIER_VERSION == "1.0"` is stamped on every classified row
- [ ] `classify_transactions([])` returns `[]`
- [ ] Single-transaction filer → opportunistic
- [ ] 3+ same-month transactions in prior 3y → routine
- [ ] Same-month transactions beyond the 3y lookback → opportunistic (lookback bounded)
- [ ] Different filers classified independently
- [ ] Classifier is order-invariant on input
- [ ] `EdgarSource.fetch_filings_for_ticker()` updates `(edgar, ticker, filings)` watermark with latest `filing_date`
- [ ] Watermark short-circuit: if latest filing ≤ last_observation_date, no Form 4 XML fetches
- [ ] Per-Form-4-failure does NOT abort the batch (continues with remaining)
- [ ] CIK resolution failure recorded in watermark error_count
- [ ] Every SEC HTTP request carries `User-Agent` with contact email
- [ ] Tenacity retries 429/503 with exponential backoff up to 5 attempts (reused `_sec_get_with_retry` from A.3.3)
- [ ] No live network in unit tests (all HTTP mocked via pytest-mock)
- [ ] The build plan marks A.3.4 shipped (schema v6)
- [ ] No A.1 / A.2 / A.3.1 / A.3.2 / A.3.3 regressions (`health_check`, `raw_finviz`, `raw_yahoo`, `raw_edgar_fundamentals` unchanged)
- [ ] Git log shows ~8 task commits

---

## Self-review

**Spec coverage:**

| A.3 spec section | A.3.4 task |
|---|---|
| §6.3.2 Form 4 daily index per CIK | Task 6 (`fetch_filings_for_ticker` step 2 — submissions JSON pull) |
| §6.3.2 Parse XML: filer name, title, code, shares, price, post-tx holdings | Task 4 (`edgar_form4_parser.parse_form4_xml`) |
| §6.3.2 Cohen-Malloy-Pomorski classifier — routine iff ≥2 same-month in prior 3y | Task 3 (`methodology/opportunistic_insider.classify_transactions`) |
| §6.3.2 Writes to `raw_edgar_insider` | Task 2 (`migrate_to_v6` + `insert_raw_edgar_insider`) |
| §6.3.3 Pull 8-K filing-index per ticker, last 90 days | Task 5 (`edgar_submissions_parser.parse_submissions`, last_n_days=90 default) |
| §6.3.3 Records filing_date, item_codes (2.02, 5.02, 7.01, …) | Task 5 + Task 1 (RawEdgarFilingRow.item_codes) |
| §6.3.3 Writes to `raw_edgar_filings` | Task 2 (`migrate_to_v6` + `insert_raw_edgar_filings`) |
| Principle 6 watermarks | Task 6 (watermark short-circuit on latest filing date + advance on success) |
| Principle 5 no silent overwrite | Task 2 (INSERT OR IGNORE on both new tables) |
| §6.3 fair-access (User-Agent + ≤10/s + tenacity) | Task 6 (reuses `_sec_get_with_retry` from A.3.3) |
| §7.1 row 1 — CMP as `news_activity_score` lead | Task 3 (classifier ready for A.3.8 consumption) |

**Out of scope** (explicitly deferred):

- Derivative Form 4 line items beyond `<derivativeTransaction>` flag: full options-grant accounting and post-exercise reconciliation → A.3.10 or later.
- Cross-run reclassification: the CMP classifier runs against the batch's in-memory insider rows for a given fetch. Once a filer accumulates enough history across many runs, an originally-opportunistic trade might warrant reclassification. We capture this via the `opportunistic_classifier_version` field — a future v2.0 classifier release can sweep `raw_edgar_insider` and re-stamp rows; the current snapshot is the as-of value at first observation.
- Schedule 13D / 13G: out of scope for A.3 entirely.
- Form 144 (planned sales): not in spec; could be added in A.3.10.
- News-volume / Tetlock LM-tone / Yang-Zhang vol → later sub-phases.
- Live integration test against the real SEC submissions+Archives endpoints → A.5.

**Placeholder scan:** No "TBD", "TODO", or "implement later" in any code block. Every step contains full source.

**Pydantic v2 gotchas avoided:**
- `_FORM4_TRANSACTION_CODES` and `_EDGAR_FORM_TYPES` are module-level Literals so the Pydantic models accept exactly the validated set.
- `filer_name` `@field_validator(mode="before")` upper-cases before the type check; same pattern as `ticker`/`cik` from A.3.3.
- Classifier output stamping uses `model_copy(update={...})` (Pydantic v2 idiom).
- All booleans stored as int in SQLite (0/1) per the `insert_raw_edgar_insider` helper's explicit conversion loop.

**SEC URL construction (load-bearing):**

The Archives URL format is the single most error-prone piece of A.3.4. Verified against SEC's API docs at https://www.sec.gov/edgar/sec-api-documentation and the live structure of e.g. https://www.sec.gov/Archives/edgar/data/320193/:
- Submissions JSON: `https://data.sec.gov/submissions/CIK{padded10}.json`
- Filing archive directory: `https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accn_no_dashes}/`
- Form 4 primary doc: `https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accn_no_dashes}/{primary_doc_from_submissions_blob}`

The `cik_unpadded` (leading zeros stripped) is what goes in the path; the `CIK0000320193.json` form uses the padded version. `accn_no_dashes` is the accession number with all hyphens removed (`0000320193-26-000045` → `000032019326000045`). The `primary_doc` filename comes directly from `submissions.recent.primaryDocument[i]` — for Form 4 this is typically `wk-form4_<unixtime>.xml` or `<filer>-form4_<unixtime>.xml`.

Construction is centralized in `_build_primary_doc_url()` inside `edgar_submissions_parser.py` so it has one test surface and one bug surface.

**CMP classifier edge-case handling (load-bearing):**

Two boundaries we explicitly test:

1. **Threshold inclusivity**: Exactly 2 same-month prior trades within 3 years → routine. This matches the spec's `≥ 2` wording. Test: `test_filer_with_2_same_month_prior_3y_is_routine`.
2. **Lookback exclusion**: Same-month trades older than 3 years are ignored. The bound uses month-day-anchored 3-years-before-focal (e.g., 2026-04-05 cutoff is 2023-04-05; trades on 2023-04-05 or later count, trades on 2023-04-04 or earlier don't). Tests: `test_same_month_but_beyond_3y_lookback_is_opportunistic` + `test_same_month_within_3y_boundary_is_routine`.

Self-exclusion of the focal trade is also tested. The classifier counts *transactions* (not directions); a routine filer can mix buys and sells.

**Test coverage shape:**

| Layer | Unit tests | Integration tests |
|---|---|---|
| Schemas (`RawEdgarInsiderRow` + `RawEdgarFilingRow`) | 14 | — |
| DB migration + helpers | 16 | — |
| CMP classifier | 13 | — |
| Form 4 XML parser | 11 | — |
| Submissions JSON parser | 11 | — |
| `fetch_filings_for_ticker` | 10 | — |
| End-to-end orchestration | — | 3 |
| **Total new tests** | **75** | **3** |

**Architectural notes:**

- `src/methodology/` is a NEW top-level package created in this sub-phase. It will accumulate one module per academic signal in A.3.5+ (`short_interest_signal.py`, `revision_signal.py`, etc.). Naming convention: `{signal_name}.py` with a module-level `{SIGNAL_UPPER}_VERSION` constant + a single primary public function.
- The CMP classifier is intentionally pure-function (no class, no DB). This lets A.3.8's `news_activity_score` composition re-run it against a different window or a recalibrated threshold without touching persistence.
- `parse_submissions()` and `parse_form4_xml()` are intentionally pure-function for the same reason: A.5's acceptance tests can replay archived SEC blobs without network.
- The Form 4 XML parser uses stdlib `xml.etree.ElementTree`, NOT a new dependency like `lxml`. The Form 4 schema is simple enough (no XSLT, no XInclude) that ET handles it; if perf becomes an issue at scale (universe ~1,000 tickers × ~10 Form 4s = 10K parses per refresh), revisit in A.3.10.
- We delegate retries to the already-existing `_sec_get_with_retry` (built in A.3.3). The Form 4 XML pull and the submissions JSON pull share the same tenacity policy and the same User-Agent.

**Architecture risk: amendments**

A Form 4/A (amendment) might restate a line item with the same (cik, accn, filer, date, code) PK as the original Form 4. Since the amendment has a DIFFERENT accn, it inserts as a separate row in `raw_edgar_insider` — no collision, both rows persist, and downstream consumers can choose whether to prefer the latest filing_date. The SAME Form 4 (same accn) re-fetched on a later run will collide on PK and INSERT OR IGNORE silently drops the duplicate. This matches Principle 5.

---

## Execution Handoff

**Recommended:** Subagent-driven, mirroring the A.3.3 wave pattern.

- **Wave 1 (Schema + DB):** One sub-agent does Tasks 0–2 (pre-flight + `RawEdgarInsiderRow` + `RawEdgarFilingRow` + `migrate_to_v6` + two insert helpers). All in `src/common/`; sequential. **~15 min**.
- **Wave 2 (Pure-function parsers + classifier):** Run Tasks 3, 4, 5 in **parallel** as three sub-agents — each is independent (different files, different test files, no cross-dependencies). The CMP classifier (Task 3) has the most tests but no XML parsing; the Form 4 parser (Task 4) and submissions parser (Task 5) are mechanically similar. **~25 min** if parallel, ~45 min if serial.
- **Wave 3 (EdgarSource orchestration):** One sub-agent does Task 6 (`fetch_filings_for_ticker` real impl). Depends on all of Wave 2. **~15 min**.
- **Wave 4 (Integration + build plan):** Sub-agent does Task 7 (integration test); inline does Task 8 (build plan update) in parallel. **~10 min**.

Total: 4 waves, ~65–90 min wall-clock if Wave 2 is parallelized.

**Critical pre-execution checks for the executing agent:**

1. After Task 2, confirm `schema_version == 6` before proceeding:
   ```
   ./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager('data/_v6_check.db'); m.migrate_to_v6(); print(m.get_schema_version())"
   ```
   Expect `6`.
2. After Task 4, confirm the Form 4 fixture parses with stdlib ET:
   ```
   ./venv/Scripts/python.exe -c "import xml.etree.ElementTree as ET; ET.parse('tests/fixtures/edgar/form4_sample.xml')"
   ```
   No traceback expected.
3. After Task 5, confirm the submissions fixture is valid JSON with the expected key shape:
   ```
   ./venv/Scripts/python.exe -c "import json; b=json.load(open('tests/fixtures/edgar/submissions_CIK0000320193.json')); print(len(b['filings']['recent']['accessionNumber']))"
   ```
   Expect `7`.
4. Do not modify `EdgarSource.health_check()` or `fetch_fundamentals_for_ticker()` — only add `fetch_filings_for_ticker()`. The A.1 + A.3.3 regression tests are part of the safety net.
5. If `_sec_get_with_retry` triggers in tests (it shouldn't — mocks should intercept), the test will hang for ~10s due to backoff. Confirm the `requests.get` patch is applied at the `edgar_source` module path. The form4 XML fetches AND the submissions JSON fetch both flow through the SAME `requests.get` import inside `edgar_source.py`, so a single `mocker.patch("src.common.datasources.edgar_source.requests.get", side_effect=...)` covers both.
6. When mocking, distinguish URL types by substring: `"submissions/CIK"` vs `".xml"` vs `"company_tickers"` — see the `_wire_mocks` helper used across the test files.

**Methodology callout before execution:**

The CMP classifier as implemented uses the **operational** ≥2-in-prior-3y rule (the spec's wording in §6.3.2). The 2012 paper's original definition was stricter: "same calendar month for at least 3 consecutive years prior." The looser operational variant is what subsequent literature uses and what the spec calls for. The strict-3-consecutive-years version could be released as a future classifier v2.0 — the `opportunistic_classifier_version` column on every row makes the migration auditable.

Also note: the classifier identity unit is `filer_id`, constructed by the orchestrator as `"{filer_name}:{cik}"`. This means the same physical insider trading two different issuers gets two filer_ids, which is the academically correct default (their routine patterns differ per issuer). Global filer identity across issuers is a possible future variant — the orchestrator can change the filer_id construction without touching the classifier.
