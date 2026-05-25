# Phase A.3.7 — OpenBBSource Multi-Provider Router (FMP-stable + Polygon + Tiingo)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]` checkbox syntax for tracking.

**Goal:** Wire `OpenBBSource.fetch_fundamentals_for_ticker(ticker, run_id)` to a real, multi-provider implementation that routes a single logical call across three external providers — **FMP (stable API)**, **Polygon**, and **Tiingo** — and writes each provider's response into a new long-format `raw_openbb` table keyed by `(run_id, ticker, field_name, provider_used)`. Schema migrates v8 → v9 to add the table and helper. The source is a **lightweight direct-HTTP wrapper**, NOT the OpenBB CLI / Python SDK — we hit each provider's REST endpoint directly so we own the failure surface, retry policy, key-rotation hooks, and the canonical-field mapping per provider.

**Why this matters:** The A.3 design's spec §6.7 calls for OpenBB as a *multi-provider router* that supplements the single-source adapters (Finviz/Yahoo/EDGAR/FRED/FINRA/Stockanalysis) with provider-redundant fundamentals + price-anchor fields. Every other source in Layer 1 covers one path; OpenBB's job is to expose three independent routes for the *same* field so the materialization layer's adapter chain (A.3.8+) has cross-vendor cross-validation built in. If FMP's `pe_ratio` for `AAPL` disagrees with Polygon's `market_cap`-derived implied valuation, that disagreement IS the signal — the provenance store records both observations and the equivalence harness (A.3.10) flags the divergence. The long-format `(field_name, provider_used)` composite key is the substrate that makes that comparison possible: two providers reporting the same field for the same ticker is two rows in `raw_openbb`, not a silent overwrite.

**Architecture:** Additive only. Migration v8 → v9 creates `raw_openbb` and bumps `schema_version`. The source gains a single new public method:

1. `OpenBBSource.fetch_fundamentals_for_ticker(ticker, run_id, db, *, refresh_after_hours=24)` — resolves whether a refresh is needed by reading the `(openbb, ticker, multi_provider)` watermark; if the previous successful pull is within 24h, returns 0 immediately. Otherwise loops through three provider routes (FMP-stable → Polygon → Tiingo). Each route is independent: a missing API key (`get_api_key(provider) is None`) silently skips that route, an HTTP error logs and continues to the next route, and a successful pull's response is normalized to a small set of canonical fields and INSERTed into `raw_openbb` with `provider_used` set. Polite per-provider delay (Polygon free tier = 12 s; FMP + Tiingo = 0.5 s; test patches `time.sleep` to no-op). Watermark records aggregate success/error: success if at least one route wrote at least one row.

The provider-specific HTTP + field-mapping code is colocated in `openbb_source.py` rather than extracted into a separate parser module. Rationale: unlike `stockanalysis_parser.py` (A.3.6) where the HTML DOM logic was genuinely complex and reusable, each provider's JSON-response shape is small, flat, and provider-private — there's no reusable "JSON parser" abstraction worth extracting. The mapping helpers `_map_fmp`, `_map_polygon`, `_map_tiingo` are private module-level pure functions inside the source file, which keeps the dispatcher (`fetch_fundamentals_for_ticker`) easy to read top-to-bottom.

**Tech Stack:** Python 3.11+, `requests`, `tenacity`, `pydantic` v2, `pytest`, `pytest-mock`. **No new runtime dependencies** — `requests` + `tenacity` are already pinned. We deliberately do NOT pull in the `openbb` PyPI package (its install footprint is ~500 MB and pulls in dozens of provider SDKs as transitive deps for a single feature surface; we'd be paying full price for ~5% utilization). The trade-off is documented under Out of Scope below.

**Spec reference:** [docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md](../specs/2026-05-21-phase-a3-layer1-hardening-design.md) §6.7 (OpenBBSource) + §6.8 (.env loader) + §3 Principle 5 (no silent overwrite) + Principle 6 (watermarks bound deltas) + §13 Failure modes ("OpenBB key missing → Provider silently skipped; other sources cover").

**API key configuration:** All three providers' keys live in `.env` and are looked up via `src.common.env_loader.get_api_key("fmp" | "polygon" | "tiingo")`. The `config/api_keys.yaml` mapping is already in place from A.3.1:

```
fmp:      FMP_API_KEY
polygon:  POLYGON_API_KEY
tiingo:   TIINGO_API_KEY
```

**Key-missing graceful degradation (load-bearing):** If `get_api_key("polygon")` returns `None`, the Polygon route is skipped without raising; the source emits a log line at `INFO` level (not `WARNING` — a missing key is a configuration choice, not an error) and proceeds to the next route. The fetch returns "success" if at least one route wrote at least one row. Only when ALL three routes are skipped (e.g., zero keys provisioned) does the watermark record an error.

**FMP stable-API contract (verified 2026-05-21):** FMP issued post-2024 trial / paid keys work ONLY against the `https://financialmodelingprep.com/stable/...` endpoints. The legacy `https://financialmodelingprep.com/api/v3/...` endpoints return HTTP 403 with body `"Legacy Endpoint"`. This plan ONLY targets stable URLs:

- `https://financialmodelingprep.com/stable/profile?symbol={ticker}&apikey={key}` → company profile (sector, industry, market cap, exchange, last_price, beta, dividend_yield, currency, ipo_date)
- `https://financialmodelingprep.com/stable/quote?symbol={ticker}&apikey={key}` → live quote (price, change %, volume, market cap, pe_ratio)
- `https://financialmodelingprep.com/stable/ratios-ttm?symbol={ticker}&apikey={key}` → TTM ratios (pe_ratio_ttm, ps_ratio_ttm, pb_ratio_ttm, dividend_yield_ttm)
- `https://financialmodelingprep.com/stable/key-metrics-ttm?symbol={ticker}&apikey={key}` → TTM key metrics (market_cap, ev, ev_to_ebitda_ttm, fcf_yield)

`income-statement` and other deeper financial-statement endpoints exist on stable too but are **skipped for v1** — EdgarSource (A.3.3) already covers fundamentals from authoritative XBRL filings; FMP would be redundant for those, and the alpha here is cross-vendor *price-anchor* + *ratio* validation, not balance-sheet redundancy.

**Polygon free-tier contract (verified 2026-05-21):** Polygon's free tier rate-limits at 5 requests/minute. To stay under the ceiling we sleep 12 seconds between Polygon calls. Two endpoints are pulled per ticker:

- `https://api.polygon.io/v3/reference/tickers/{ticker}?apiKey={key}` → identifier metadata (ticker name, market, locale, primary_exchange, type, currency_name, cik, market_cap, share_class_shares_outstanding)
- `https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?apiKey={key}` → previous-day aggregate (open, high, low, close, volume, vwap)

The 12-second budget per ticker is acceptable for our refresh cadence (24h skip, 500-ticker universe = 1.7 hours worst-case if we run Polygon serially; in practice we'd run the universe in parallel batches in A.3.10's orchestrator).

**Tiingo free-tier contract (verified 2026-05-21):** Tiingo allows 1000 requests/day on the free tier. Per-call latency is fast (median < 500ms) so we only enforce a token 0.5-second inter-call delay as politeness. Two endpoints are pulled per ticker:

- `https://api.tiingo.com/tiingo/daily/{ticker}?token={key}` → ticker metadata (name, exchange_code, description, start_date, end_date)
- `https://api.tiingo.com/iex/{ticker}?token={key}` → real-time-ish quote (last, open, high, low, mid, bidPrice, askPrice, volume, tngoLast, prevClose)

**Canonical field mapping** (per provider, normalized to a common vocabulary for `raw_openbb.field_name`):

| canonical `field_name` | FMP source | Polygon source | Tiingo source |
|---|---|---|---|
| `pe_ratio` | `ratios-ttm.peRatioTTM` | — (not exposed) | — (not exposed) |
| `ps_ratio` | `ratios-ttm.priceToSalesRatioTTM` | — | — |
| `pb_ratio` | `ratios-ttm.priceToBookRatioTTM` | — | — |
| `dividend_yield` | `ratios-ttm.dividendYieldTTM` | — | — |
| `ev_to_ebitda` | `key-metrics-ttm.enterpriseValueOverEBITDATTM` | — | — |
| `market_cap` | `key-metrics-ttm.marketCapTTM` or `quote.marketCap` | `reference.results.market_cap` | — |
| `last_price` | `quote.price` | `aggs.results[0].c` | `iex[0].last` or `iex[0].tngoLast` |
| `prev_close` | `quote.previousClose` | `aggs.results[0].c` (prev-day close) | `iex[0].prevClose` |
| `volume` | `quote.volume` | `aggs.results[0].v` | `iex[0].volume` |
| `shares_outstanding` | — | `reference.results.share_class_shares_outstanding` | — |
| `primary_exchange` | `profile.exchange` | `reference.results.primary_exchange` | `daily.exchangeCode` |
| `sector` | `profile.sector` | — | — |
| `industry` | `profile.industry` | — | — |
| `beta` | `profile.beta` | — | — |
| `currency` | `profile.currency` | `reference.results.currency_name` | — |

This table is the **canonical contract** the v1 parser implements. Any field a provider doesn't expose simply isn't emitted by that provider's mapper — we never invent values, and we never overwrite a present value with `None`. The `raw_openbb` PK `(run_id, ticker, field_name, provider_used)` means the SAME field from two providers writes TWO rows; cross-validation happens at the materialization layer (A.3.10).

**Out of scope for A.3.7:**

- The materialization layer that consumes `raw_openbb` for cross-vendor reconciliation and `data_approximated_flags` propagation → A.3.10.
- The OpenBB Python SDK (`openbb` PyPI package). Direct HTTP keeps install lean and avoids cross-cutting SDK config (user settings, credentials store) that would need a separate teardown story. If we later need provider features beyond plain-REST (e.g., GraphQL subscriptions for live quotes), revisit.
- Provider-specific deep endpoints: `income-statement`, `balance-sheet-statement`, `cash-flow-statement` from FMP (covered by EDGAR), `news` from Polygon (deferred to A.3.8 news pipeline), Tiingo `news`, Tiingo `crypto`, Tiingo `forex`.
- Yfinance as a fourth route. Yahoo already has a first-class source (`YahooSource` from A.3.2); funneling it through OpenBB would just double-count it in the materialization adapter chain.
- A live-network integration test against the actual `financialmodelingprep.com` / `api.polygon.io` / `api.tiingo.com` endpoints → A.5 acceptance phase.
- Intrinio provider route. The `api_keys.yaml` mapping reserves an `intrinio` entry, but no key is provisioned and the design doesn't require Intrinio at v1.
- Per-field tenacity retry. v1 uses a single `tenacity` wrapper per provider HTTP call (retry on 429/503 only); per-field retry is unnecessary because each field is part of a single JSON response.

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| `src/common/schemas.py` | Add `RawOpenBBRow` Pydantic model | Modify |
| `src/common/database.py` | Add `migrate_to_v9()` + `insert_raw_openbb()` | Modify |
| `src/common/datasources/openbb_source.py` | NEW — multi-provider router source | Create |
| `tests/fixtures/openbb/fmp_quote_AAPL.json` | NEW — synthetic FMP `/stable/quote` response | Create |
| `tests/fixtures/openbb/fmp_profile_AAPL.json` | NEW — synthetic FMP `/stable/profile` response | Create |
| `tests/fixtures/openbb/fmp_ratios_ttm_AAPL.json` | NEW — synthetic FMP `/stable/ratios-ttm` response | Create |
| `tests/fixtures/openbb/fmp_key_metrics_ttm_AAPL.json` | NEW — synthetic FMP `/stable/key-metrics-ttm` response | Create |
| `tests/fixtures/openbb/polygon_aapl_ref.json` | NEW — synthetic Polygon `/v3/reference/tickers/AAPL` response | Create |
| `tests/fixtures/openbb/polygon_aapl_prev.json` | NEW — synthetic Polygon `/v2/aggs/.../prev` response | Create |
| `tests/fixtures/openbb/tiingo_aapl_daily.json` | NEW — synthetic Tiingo `/tiingo/daily/aapl` response | Create |
| `tests/fixtures/openbb/tiingo_aapl_iex.json` | NEW — synthetic Tiingo `/iex/aapl` response | Create |
| `tests/common/test_schemas_a3_7.py` | Tests for `RawOpenBBRow` | Create |
| `tests/common/test_database_a3_7.py` | Tests for `migrate_to_v9` + insert helper | Create |
| `tests/datasources/test_openbb_fetch.py` | Tests for `OpenBBSource.fetch_fundamentals_for_ticker` (mocked HTTP, per-provider + key-missing) | Create |
| `tests/datasources/test_integration_a3_7.py` | End-to-end orchestration test (URL-dispatch single side_effect) | Create |
| the build plan | Mark A.3.7 shipped (schema v9) | Modify |

---

## Task 0: Pre-flight verification

**Files:** none modified.

- [ ] **Step 1: Verify schema is at v8 from A.3.6**

Run from the repository root:

```
./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager(); print('current schema version:', m.get_schema_version())"
```

Expected: prints `current schema version: 8`.

- [ ] **Step 2: Verify A.3.6 baseline tests still pass**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: `427 passed, 2 deselected` (or whatever the current A.3.6 baseline shows; counts may drift ±2).

- [ ] **Step 3: Verify `tenacity` + `requests` importable from venv**

```
./venv/Scripts/python.exe -c "import tenacity, requests; print('tenacity import ok'); print('requests', requests.__version__)"
```

Expected: both lines print without error. NOTE: `tenacity` has no module-level `__version__` attribute in many builds — we only verify `import tenacity` works (matches A.3.3/A.3.4/A.3.5/A.3.6 pattern).

- [ ] **Step 4: Verify `env_loader.get_api_key` is available for all three providers**

```
./venv/Scripts/python.exe -c "from src.common.env_loader import get_api_key; print('fmp:', 'present' if get_api_key('fmp') else 'MISSING'); print('polygon:', 'present' if get_api_key('polygon') else 'MISSING'); print('tiingo:', 'present' if get_api_key('tiingo') else 'MISSING')"
```

Expected (when the local `.env` is on disk): all three print `present`. If any print `MISSING`, the unit tests still pass because they mock the lookup; the *integration* concern only surfaces during A.5 acceptance.

NOTE: this step is **diagnostic only** — do NOT fail-fast on missing keys. The plan explicitly tests the key-missing degradation path in Task 3 Step 5.

- [ ] **Step 5: Verify A.1 OpenBBSource shell is ABSENT (we are creating it from scratch)**

```
./venv/Scripts/python.exe -c "import importlib, importlib.util; print('openbb_source present:', importlib.util.find_spec('src.common.datasources.openbb_source') is not None)"
```

Expected: `openbb_source present: False`.

If `True` (i.e., an earlier A.1 stub was created for OpenBBSource), inspect the file:

```
ls src/common/datasources/openbb_source.py
```

If a stub exists, this plan REPLACES it. The Task 3 implementation overwrites the file in full (matches the A.3.6 pattern with `stockanalysis_source.py`).

- [ ] **Step 6: Verify `config/api_keys.yaml` mapping is intact**

```
./venv/Scripts/python.exe -c "import yaml; m=yaml.safe_load(open('config/api_keys.yaml')); assert m.get('fmp')=='FMP_API_KEY' and m.get('polygon')=='POLYGON_API_KEY' and m.get('tiingo')=='TIINGO_API_KEY'; print('api_keys.yaml mapping OK')"
```

Expected: `api_keys.yaml mapping OK`.

- [ ] **Step 7: Verify working tree is clean before starting**

Run: `git status -s`

Expected: empty output (or only unrelated `.env` / notebooks).

No code commits at this task.

---

## Task 1: `RawOpenBBRow` Pydantic model

**Files:**
- Modify: `src/common/schemas.py` (append)
- Create: `tests/common/test_schemas_a3_7.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_schemas_a3_7.py`:

```python
"""Tests for Phase A.3.7 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import RawOpenBBRow


class TestRawOpenBBRow:
    def test_minimal_valid_fmp(self):
        r = RawOpenBBRow(
            run_id="r1",
            ticker="AAPL",
            field_name="pe_ratio",
            provider_used="fmp",
            value=28.45,
            unit="ratio",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"
        assert r.field_name == "pe_ratio"
        assert r.provider_used == "fmp"
        assert r.value == 28.45
        assert r.unit == "ratio"

    def test_minimal_valid_polygon(self):
        r = RawOpenBBRow(
            run_id="r1",
            ticker="AAPL",
            field_name="market_cap",
            provider_used="polygon",
            value=3_000_000_000_000.0,
            unit="usd",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.provider_used == "polygon"
        assert r.unit == "usd"

    def test_minimal_valid_tiingo(self):
        r = RawOpenBBRow(
            run_id="r1",
            ticker="AAPL",
            field_name="last_price",
            provider_used="tiingo",
            value=180.50,
            unit="usd",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.provider_used == "tiingo"

    def test_ticker_uppercased(self):
        r = RawOpenBBRow(
            run_id="r1",
            ticker="aapl",
            field_name="pe_ratio",
            provider_used="fmp",
            value=28.45,
            unit="ratio",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            RawOpenBBRow(
                run_id="r1",
                ticker="not-a-ticker",
                field_name="pe_ratio",
                provider_used="fmp",
                value=28.45,
                unit="ratio",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_unknown_field_name_rejected(self):
        """field_name is a Literal of supported canonical names; unknown
        names must raise rather than silently corrupting the raw store."""
        with pytest.raises(Exception):
            RawOpenBBRow(
                run_id="r1",
                ticker="AAPL",
                field_name="not_a_field",
                provider_used="fmp",
                value=1.0,
                unit="ratio",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_unknown_provider_rejected(self):
        """provider_used is a Literal of {fmp, polygon, tiingo}; anything
        else must raise."""
        with pytest.raises(Exception):
            RawOpenBBRow(
                run_id="r1",
                ticker="AAPL",
                field_name="pe_ratio",
                provider_used="bloomberg",
                value=28.45,
                unit="ratio",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_value_can_be_none(self):
        """Sentinel: provider returned the field but with a null value.
        The row is still emitted (provenance of the attempt) but with
        value=None."""
        r = RawOpenBBRow(
            run_id="r1",
            ticker="AAPL",
            field_name="dividend_yield",
            provider_used="fmp",
            value=None,
            unit="ratio",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.value is None

    def test_unit_required(self):
        """unit is mandatory — it disambiguates 'usd' vs 'ratio' vs
        'percent' for downstream cross-vendor comparison."""
        with pytest.raises(Exception):
            RawOpenBBRow(
                run_id="r1",
                ticker="AAPL",
                field_name="pe_ratio",
                provider_used="fmp",
                value=28.45,
                unit="",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_all_supported_field_names_accepted(self):
        """Round-trip every supported field_name to confirm the Literal."""
        supported = [
            "pe_ratio", "ps_ratio", "pb_ratio",
            "dividend_yield", "ev_to_ebitda",
            "market_cap", "last_price", "prev_close",
            "volume", "shares_outstanding",
            "primary_exchange", "sector", "industry",
            "beta", "currency",
        ]
        for fn in supported:
            r = RawOpenBBRow(
                run_id="r1",
                ticker="AAPL",
                field_name=fn,
                provider_used="fmp",
                value=1.0,
                unit="ratio",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
            assert r.field_name == fn

    def test_all_three_providers_accepted(self):
        for p in ["fmp", "polygon", "tiingo"]:
            r = RawOpenBBRow(
                run_id="r1",
                ticker="AAPL",
                field_name="pe_ratio",
                provider_used=p,
                value=1.0,
                unit="ratio",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
            assert r.provider_used == p

    def test_string_valued_fields_allowed_via_unit(self):
        """For string-valued canonical fields like sector / industry / currency,
        `value` is float-typed in the model. v1 stores those fields with
        `value=None` and the actual string passed through the `unit`
        channel with a category prefix (e.g., unit='sector:Technology').
        """
        r = RawOpenBBRow(
            run_id="r1",
            ticker="AAPL",
            field_name="sector",
            provider_used="fmp",
            value=None,
            unit="sector:Technology",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.value is None
        assert r.unit.startswith("sector:")
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_7.py -v
```

Expected: `ImportError` on `RawOpenBBRow`.

- [ ] **Step 3: Append model to `src/common/schemas.py`**

At the **end** of `src/common/schemas.py`, append:

```python


# === Phase A.3.7 - OpenBB multi-provider router ==========================


# Supported canonical field names. Constrained as a Literal so an unknown
# field name from a future provider-response schema change raises loudly
# rather than silently corrupting raw_openbb. To accommodate a new field,
# add it here AND extend the relevant provider mapper(s) in
# src/common/datasources/openbb_source.py.
_OpenBBFieldName = Literal[
    # Valuation ratios
    "pe_ratio",
    "ps_ratio",
    "pb_ratio",
    "ev_to_ebitda",
    "dividend_yield",
    # Price-anchor / market
    "market_cap",
    "last_price",
    "prev_close",
    "volume",
    "shares_outstanding",
    # Identifier metadata (string-valued; stored with value=None +
    # category-tagged unit)
    "primary_exchange",
    "sector",
    "industry",
    "currency",
    # Risk metric
    "beta",
]

_OpenBBProvider = Literal["fmp", "polygon", "tiingo"]


class RawOpenBBRow(BaseModel):
    """One (ticker, field_name, provider_used) observation pulled from the
    OpenBB multi-provider router (A.3.7).

    Spec: docs/design/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.7. Stored long-format in raw_openbb; PK
    (run_id, ticker, field_name, provider_used).

    Provenance is the load-bearing concept: the same canonical field
    pulled from two providers writes TWO rows (different `provider_used`),
    NOT one row that the second provider silently overwrites. The
    materialization layer (A.3.10) consumes the long-format rows to do
    cross-vendor reconciliation and flag divergence per
    `data_approximated_flags`.

    `value` is Optional because:
      * a provider may report the field with an explicit null,
      * the field may be string-valued (sector / industry / currency /
        primary_exchange) — for v1 the string is stored in `unit` with a
        category prefix and `value` is None.
    """

    run_id: str
    ticker: str
    field_name: _OpenBBFieldName
    provider_used: _OpenBBProvider
    value: Optional[float] = None
    unit: str
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

    @field_validator("unit", mode="before")
    @classmethod
    def _validate_unit(cls, v: object) -> str:
        if v is None:
            raise ValueError("unit is required")
        s = str(v).strip()
        if not s:
            raise ValueError("unit cannot be empty")
        return s
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_7.py -v
```

Expected: All 12 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/schemas.py tests/common/test_schemas_a3_7.py
git commit -m "feat(schemas): add RawOpenBBRow for A.3.7"
```

---

## Task 2: `migrate_to_v9` + `insert_raw_openbb`

**Files:**
- Modify: `src/common/database.py`
- Create: `tests/common/test_database_a3_7.py`

- [ ] **Step 1: Write the failing test**

Create `tests/common/test_database_a3_7.py`:

```python
"""Tests for Phase A.3.7 DatabaseManager extensions."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawOpenBBRow


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


# --- migrate_to_v9 ----------------------------------------------------


def test_migrate_to_v9_creates_raw_openbb_table(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    assert "raw_openbb" in _table_names(db_path)


def test_raw_openbb_has_expected_columns(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    cols = _columns(db_path, "raw_openbb")
    expected = {
        "run_id", "ticker", "field_name", "provider_used",
        "value", "unit", "scrape_timestamp",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_migrate_to_v9_bumps_schema_version_to_9(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_migrate_to_v9_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.migrate_to_v9()
    mgr.migrate_to_v9()
    with sqlite3.connect(db_path) as c:
        versions = sorted(r[0] for r in c.execute("SELECT version FROM schema_version"))
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_get_schema_version_returns_9_after_migration(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    assert mgr.get_schema_version() == 9


def test_migrate_to_v9_preserves_v8_data(tmp_path: Path):
    """raw_stockanalysis_ratios from v8 must survive the v9 migration."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v8()
    from src.common.schemas import RawStockanalysisRatioRow
    mgr.insert_raw_stockanalysis_ratios([
        RawStockanalysisRatioRow(
            run_id="r1", ticker="AAPL", metric="pe_ratio",
            period_end_date="2024-12-31", period_type="annual",
            value=28.45,
            source_url="https://stockanalysis.com/stocks/aapl/financials/ratios/",
            scrape_timestamp="2026-05-21T08:00:00Z",
        ),
    ])
    mgr.migrate_to_v9()
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_stockanalysis_ratios WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert v == 28.45


# --- insert_raw_openbb ----------------------------------------------------


def _row(**overrides) -> RawOpenBBRow:
    base = dict(
        run_id="r1",
        ticker="AAPL",
        field_name="pe_ratio",
        provider_used="fmp",
        value=28.45,
        unit="ratio",
        scrape_timestamp="2026-05-21T08:00:00Z",
    )
    base.update(overrides)
    return RawOpenBBRow(**base)


def test_insert_raw_openbb_persists_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([_row()])
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT ticker, field_name, provider_used, value, unit "
            "FROM raw_openbb"
        ).fetchone()
    assert row == ("AAPL", "pe_ratio", "fmp", 28.45, "ratio")


def test_insert_raw_openbb_empty_list_is_noop(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([])
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM raw_openbb").fetchone()[0]
    assert n == 0


def test_insert_raw_openbb_uses_insert_or_ignore(tmp_path: Path):
    """PK = (run_id, ticker, field_name, provider_used). First write wins on collision."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([_row(value=28.45)])
    mgr.insert_raw_openbb([_row(value=999.99)])
    with sqlite3.connect(db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_openbb "
            "WHERE run_id='r1' AND ticker='AAPL' AND field_name='pe_ratio' "
            "AND provider_used='fmp'"
        ).fetchone()[0]
    assert v == 28.45


def test_insert_raw_openbb_allows_same_field_from_two_providers(tmp_path: Path):
    """Long-format key INTENTIONALLY admits two rows for the same
    (run_id, ticker, field_name) when the providers differ — that's the
    cross-vendor cross-validation substrate the materialization layer needs."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([
        _row(provider_used="fmp", value=28.45),
        _row(provider_used="polygon", value=28.50),
        _row(provider_used="tiingo", value=28.42),
    ])
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT provider_used, value FROM raw_openbb "
            "WHERE run_id='r1' AND ticker='AAPL' AND field_name='pe_ratio' "
            "ORDER BY provider_used"
        ).fetchall()
    assert rows == [("fmp", 28.45), ("polygon", 28.50), ("tiingo", 28.42)]


def test_insert_raw_openbb_supports_multiple_fields_same_provider(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([
        _row(field_name="pe_ratio", value=28.45, unit="ratio"),
        _row(field_name="market_cap", value=3.0e12, unit="usd"),
        _row(field_name="last_price", value=180.50, unit="usd"),
    ])
    with sqlite3.connect(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM raw_openbb WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert n == 3


def test_insert_raw_openbb_handles_null_value(tmp_path: Path):
    """String-valued canonical fields (sector / industry / etc.) land in
    raw_openbb with value=NULL and the category in unit."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([
        _row(field_name="sector", value=None, unit="sector:Technology"),
    ])
    with sqlite3.connect(db_path) as c:
        v, u = c.execute(
            "SELECT value, unit FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='sector'"
        ).fetchone()
    assert v is None
    assert u == "sector:Technology"


def test_insert_raw_openbb_supports_multiple_runs_same_ticker_same_field_same_provider(tmp_path: Path):
    """The run_id discriminator in the PK means re-pulls under a fresh
    run_id append, not collide. This is intentional — it lets us reconstruct
    a per-run snapshot of provider state."""
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v9()
    mgr.insert_raw_openbb([_row(run_id="r1", value=28.45)])
    mgr.insert_raw_openbb([_row(run_id="r2", value=29.00)])
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT run_id, value FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='pe_ratio' AND provider_used='fmp' "
            "ORDER BY run_id"
        ).fetchall()
    assert rows == [("r1", 28.45), ("r2", 29.00)]
```

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_7.py -v
```

Expected: `AttributeError` on `migrate_to_v9`.

- [ ] **Step 3: Add `migrate_to_v9()` to `DatabaseManager`**

In `src/common/database.py`, **locate** `migrate_to_v8()` (around line ~647). Immediately AFTER the `migrate_to_v8` method body (find the trailing `conn.commit()` of the `migrate_to_v8` `with self.get_connection()` block, and add the new method after that), insert:

```python
    def migrate_to_v9(self) -> None:
        """Idempotent migration v8 -> v9 per A.3 spec section 6.7.

        Adds:
          - raw_openbb: long-format multi-provider observations pulled by
            OpenBBSource (FMP-stable + Polygon + Tiingo). PK is
            (run_id, ticker, field_name, provider_used) so the same
            canonical field pulled from two providers writes TWO rows —
            that's the cross-vendor cross-validation substrate the
            materialization layer (A.3.10) joins on.

        Safe to call multiple times. Existing data preserved.

        Uses INSERT OR IGNORE on insert (Principle 5: no silent overwrite).
        The first-observed value for any
        (run_id, ticker, field_name, provider_used) wins; a re-pull within
        the same run_id is silently dropped. A fresh run_id is the
        supported way to refresh — the run_id in the PK is what makes
        per-run snapshots distinguishable.
        """
        self.migrate_to_v8()

        v9_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_openbb (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              field_name TEXT NOT NULL,
              provider_used TEXT NOT NULL,
              value REAL,
              unit TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (run_id, ticker, field_name, provider_used)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_openbb_ticker ON raw_openbb(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_openbb_field ON raw_openbb(field_name)",
            "CREATE INDEX IF NOT EXISTS idx_openbb_provider ON raw_openbb(provider_used)",
            "CREATE INDEX IF NOT EXISTS idx_openbb_run ON raw_openbb(run_id)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v9_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 9")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (9, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()
```

In `src/common/database.py`, at the **end** of the `DatabaseManager` class (after `insert_raw_stockanalysis_ratios`), append:

```python
    # ------------------------------------------------------------------
    # Phase A.3.7: raw_openbb helpers
    # ------------------------------------------------------------------

    def insert_raw_openbb(self, rows: list) -> None:
        """Insert RawOpenBBRow records into raw_openbb.

        Uses INSERT OR IGNORE on PK
        (run_id, ticker, field_name, provider_used). First write under a
        given run_id wins; subsequent re-pulls within the same run_id are
        silently dropped (per Principle 5: no silent overwrite). To
        refresh, start a fresh run_id — every row written under the new
        run_id is treated as a distinct observation, which lets the
        materialization layer reconstruct a per-run snapshot of provider
        state.
        """
        if not rows:
            return
        from src.common.schemas import RawOpenBBRow
        records = [
            r.model_dump() if isinstance(r, RawOpenBBRow) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_openbb ({col_list}) "
                f"VALUES ({placeholders})",
                values,
            )
            conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/common/test_database_a3_7.py -v
```

Expected: All 13 tests PASS.

- [ ] **Step 5: Run full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: ~452 passed (427 baseline + 12 schema + 13 database).

- [ ] **Step 6: Commit**

```
git add src/common/database.py tests/common/test_database_a3_7.py
git commit -m "feat(database): migrate_to_v9 - raw_openbb (long-format multi-provider, INSERT OR IGNORE)"
```

---

## Task 3: `OpenBBSource.fetch_fundamentals_for_ticker` real implementation + per-provider tests

**Files:**
- Create: `src/common/datasources/openbb_source.py`
- Create: `tests/fixtures/openbb/fmp_quote_AAPL.json`
- Create: `tests/fixtures/openbb/fmp_profile_AAPL.json`
- Create: `tests/fixtures/openbb/fmp_ratios_ttm_AAPL.json`
- Create: `tests/fixtures/openbb/fmp_key_metrics_ttm_AAPL.json`
- Create: `tests/fixtures/openbb/polygon_aapl_ref.json`
- Create: `tests/fixtures/openbb/polygon_aapl_prev.json`
- Create: `tests/fixtures/openbb/tiingo_aapl_daily.json`
- Create: `tests/fixtures/openbb/tiingo_aapl_iex.json`
- Create: `tests/datasources/test_openbb_fetch.py`

- [ ] **Step 1: Create the synthetic FMP `/stable/quote` fixture**

Create `tests/fixtures/openbb/fmp_quote_AAPL.json`:

```json
[
  {
    "symbol": "AAPL",
    "name": "Apple Inc.",
    "price": 180.50,
    "changesPercentage": 1.23,
    "change": 2.19,
    "dayLow": 178.42,
    "dayHigh": 181.05,
    "yearHigh": 199.62,
    "yearLow": 164.08,
    "marketCap": 2810000000000,
    "priceAvg50": 175.20,
    "priceAvg200": 168.40,
    "exchange": "NASDAQ",
    "volume": 51234567,
    "avgVolume": 60000000,
    "open": 179.20,
    "previousClose": 178.31,
    "eps": 6.42,
    "pe": 28.12,
    "earningsAnnouncement": "2026-04-30T20:00:00.000+0000",
    "sharesOutstanding": 15555000000,
    "timestamp": 1716307200
  }
]
```

- [ ] **Step 2: Create the synthetic FMP `/stable/profile` fixture**

Create `tests/fixtures/openbb/fmp_profile_AAPL.json`:

```json
[
  {
    "symbol": "AAPL",
    "price": 180.50,
    "beta": 1.275,
    "volAvg": 60000000,
    "mktCap": 2810000000000,
    "lastDiv": 0.96,
    "range": "164.08-199.62",
    "changes": 2.19,
    "companyName": "Apple Inc.",
    "currency": "USD",
    "cik": "0000320193",
    "isin": "US0378331005",
    "cusip": "037833100",
    "exchange": "NASDAQ Global Select",
    "exchangeShortName": "NASDAQ",
    "industry": "Consumer Electronics",
    "website": "https://www.apple.com",
    "description": "Apple Inc. designs, manufactures, and markets...",
    "ceo": "Mr. Timothy D. Cook",
    "sector": "Technology",
    "country": "US",
    "fullTimeEmployees": "164000",
    "phone": "408-996-1010",
    "ipoDate": "1980-12-12",
    "dividendYield": 0.0053,
    "isEtf": false
  }
]
```

- [ ] **Step 3: Create the synthetic FMP `/stable/ratios-ttm` fixture**

Create `tests/fixtures/openbb/fmp_ratios_ttm_AAPL.json`:

```json
[
  {
    "symbol": "AAPL",
    "peRatioTTM": 28.45,
    "priceToSalesRatioTTM": 7.55,
    "priceToBookRatioTTM": 52.10,
    "dividendYieldTTM": 0.0044,
    "payoutRatioTTM": 0.16,
    "currentRatioTTM": 1.04,
    "quickRatioTTM": 0.92,
    "debtToEquityTTM": 3.30,
    "returnOnEquityTTM": 1.6532,
    "returnOnAssetsTTM": 0.2810,
    "grossProfitMarginTTM": 0.4612,
    "operatingProfitMarginTTM": 0.3151,
    "netProfitMarginTTM": 0.2640,
    "freeCashFlowYieldTTM": 0.038
  }
]
```

- [ ] **Step 4: Create the synthetic FMP `/stable/key-metrics-ttm` fixture**

Create `tests/fixtures/openbb/fmp_key_metrics_ttm_AAPL.json`:

```json
[
  {
    "symbol": "AAPL",
    "revenuePerShareTTM": 24.65,
    "netIncomePerShareTTM": 6.42,
    "operatingCashFlowPerShareTTM": 7.55,
    "freeCashFlowPerShareTTM": 6.88,
    "cashPerShareTTM": 4.20,
    "bookValuePerShareTTM": 3.46,
    "tangibleBookValuePerShareTTM": 3.40,
    "marketCapTTM": 2810000000000,
    "enterpriseValueTTM": 2870000000000,
    "peRatioTTM": 28.45,
    "priceToSalesRatioTTM": 7.55,
    "evToSalesTTM": 7.71,
    "enterpriseValueOverEBITDATTM": 21.30,
    "evToOperatingCashFlowTTM": 24.10,
    "evToFreeCashFlowTTM": 26.42
  }
]
```

- [ ] **Step 5: Create the synthetic Polygon `/v3/reference/tickers/AAPL` fixture**

Create `tests/fixtures/openbb/polygon_aapl_ref.json`:

```json
{
  "request_id": "abc123",
  "results": {
    "ticker": "AAPL",
    "name": "Apple Inc.",
    "market": "stocks",
    "locale": "us",
    "primary_exchange": "XNAS",
    "type": "CS",
    "active": true,
    "currency_name": "usd",
    "cik": "0000320193",
    "composite_figi": "BBG000B9XRY4",
    "share_class_figi": "BBG001S5N8V8",
    "market_cap": 2811234567890.0,
    "phone_number": "(408) 996-1010",
    "description": "Apple Inc. designs, manufactures, and markets smartphones...",
    "sic_code": "3571",
    "sic_description": "ELECTRONIC COMPUTERS",
    "ticker_root": "AAPL",
    "homepage_url": "https://www.apple.com",
    "total_employees": 164000,
    "list_date": "1980-12-12",
    "share_class_shares_outstanding": 15555000000,
    "weighted_shares_outstanding": 15500000000,
    "round_lot": 100
  },
  "status": "OK"
}
```

- [ ] **Step 6: Create the synthetic Polygon `/v2/aggs/ticker/AAPL/prev` fixture**

Create `tests/fixtures/openbb/polygon_aapl_prev.json`:

```json
{
  "ticker": "AAPL",
  "queryCount": 1,
  "resultsCount": 1,
  "adjusted": true,
  "results": [
    {
      "T": "AAPL",
      "v": 51234567,
      "vw": 180.12,
      "o": 179.20,
      "c": 180.50,
      "h": 181.05,
      "l": 178.42,
      "t": 1716249600000,
      "n": 425000
    }
  ],
  "status": "OK",
  "request_id": "def456",
  "count": 1
}
```

- [ ] **Step 7: Create the synthetic Tiingo `/tiingo/daily/aapl` fixture**

Create `tests/fixtures/openbb/tiingo_aapl_daily.json`:

```json
{
  "ticker": "AAPL",
  "name": "Apple Inc",
  "description": "Apple, Inc., incorporated on January 3, 1977, is engaged in the design...",
  "startDate": "1980-12-12",
  "endDate": "2026-05-21",
  "exchangeCode": "NASDAQ"
}
```

- [ ] **Step 8: Create the synthetic Tiingo `/iex/aapl` fixture**

Create `tests/fixtures/openbb/tiingo_aapl_iex.json`:

```json
[
  {
    "ticker": "AAPL",
    "timestamp": "2026-05-21T20:00:00.000Z",
    "quoteTimestamp": "2026-05-21T20:00:00.000Z",
    "lastSaleTimestamp": "2026-05-21T20:00:00.000Z",
    "last": 180.50,
    "lastSize": 100,
    "tngoLast": 180.48,
    "prevClose": 178.31,
    "open": 179.20,
    "high": 181.05,
    "low": 178.42,
    "mid": 180.45,
    "volume": 51234567,
    "bidSize": 200,
    "bidPrice": 180.40,
    "askPrice": 180.52,
    "askSize": 300
  }
]
```

- [ ] **Step 9: Write the failing fetch tests**

Create `tests/datasources/test_openbb_fetch.py`:

```python
"""Tests for OpenBBSource.fetch_fundamentals_for_ticker (Phase A.3.7)."""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.openbb_source import OpenBBSource


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "openbb"
FMP_QUOTE = (FIXTURE_DIR / "fmp_quote_AAPL.json").read_text(encoding="utf-8")
FMP_PROFILE = (FIXTURE_DIR / "fmp_profile_AAPL.json").read_text(encoding="utf-8")
FMP_RATIOS_TTM = (FIXTURE_DIR / "fmp_ratios_ttm_AAPL.json").read_text(encoding="utf-8")
FMP_KEY_METRICS_TTM = (FIXTURE_DIR / "fmp_key_metrics_ttm_AAPL.json").read_text(encoding="utf-8")
POLYGON_REF = (FIXTURE_DIR / "polygon_aapl_ref.json").read_text(encoding="utf-8")
POLYGON_PREV = (FIXTURE_DIR / "polygon_aapl_prev.json").read_text(encoding="utf-8")
TIINGO_DAILY = (FIXTURE_DIR / "tiingo_aapl_daily.json").read_text(encoding="utf-8")
TIINGO_IEX = (FIXTURE_DIR / "tiingo_aapl_iex.json").read_text(encoding="utf-8")


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v9()
    return mgr


def _ok_resp(mocker, text: str):
    resp = mocker.MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.text = text
    resp.content = text.encode()
    import json
    resp.json = lambda: json.loads(text)
    return resp


def _404_resp(mocker):
    resp = mocker.MagicMock()
    resp.status_code = 404
    resp.ok = False
    resp.text = ""
    resp.content = b""
    resp.json = lambda: {}
    return resp


def _wire_url_dispatch_all_ok(mocker):
    """Single side_effect that dispatches by URL fragment — covers all
    eight provider endpoints with happy responses."""
    def side_effect(url, **kwargs):
        # FMP
        if "financialmodelingprep.com/stable/quote" in url:
            return _ok_resp(mocker, FMP_QUOTE)
        if "financialmodelingprep.com/stable/profile" in url:
            return _ok_resp(mocker, FMP_PROFILE)
        if "financialmodelingprep.com/stable/ratios-ttm" in url:
            return _ok_resp(mocker, FMP_RATIOS_TTM)
        if "financialmodelingprep.com/stable/key-metrics-ttm" in url:
            return _ok_resp(mocker, FMP_KEY_METRICS_TTM)
        # Polygon
        if "api.polygon.io/v3/reference/tickers/" in url:
            return _ok_resp(mocker, POLYGON_REF)
        if "api.polygon.io/v2/aggs/ticker/" in url and "/prev" in url:
            return _ok_resp(mocker, POLYGON_PREV)
        # Tiingo
        if "api.tiingo.com/tiingo/daily/" in url:
            return _ok_resp(mocker, TIINGO_DAILY)
        if "api.tiingo.com/iex/" in url:
            return _ok_resp(mocker, TIINGO_IEX)
        return _404_resp(mocker)

    mocker.patch(
        "src.common.datasources.openbb_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.openbb_source.time.sleep")


def _mock_all_keys_present(mocker):
    """Stub get_api_key so all three providers report a key."""
    def fake_get_key(provider, *args, **kwargs):
        return {
            "fmp": "fmp-test-key",
            "polygon": "poly-test-key",
            "tiingo": "tiingo-test-key",
        }.get(provider)
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=fake_get_key,
    )


# --- happy path: all three providers route, all OK ---------------------


def test_fetch_writes_at_least_one_row_per_provider(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert providers == ["fmp", "polygon", "tiingo"]


def test_fetch_emits_pe_ratio_from_fmp(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='pe_ratio' AND provider_used='fmp'"
        ).fetchone()
    assert v is not None
    assert abs(v[0] - 28.45) < 1e-9


def test_fetch_emits_market_cap_from_polygon(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='market_cap' AND provider_used='polygon'"
        ).fetchone()
    assert v is not None
    assert v[0] == 2811234567890.0


def test_fetch_emits_last_price_from_tiingo(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='last_price' AND provider_used='tiingo'"
        ).fetchone()
    assert v is not None
    assert abs(v[0] - 180.50) < 1e-9


def test_fetch_records_same_field_from_two_providers_as_two_rows(db, mocker):
    """`market_cap` is emitted by BOTH FMP and Polygon. The long-format PK
    intentionally admits both rows — that's the cross-vendor substrate."""
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT provider_used FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='market_cap' "
            "ORDER BY provider_used"
        ).fetchall()
    providers = {r[0] for r in rows}
    # Both fmp and polygon emit market_cap.
    assert "fmp" in providers
    assert "polygon" in providers


def test_fetch_updates_watermark_on_success(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    w = db.get_watermark("openbb", "AAPL", "multi_provider")
    assert w is not None
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


# --- 24h refresh-skip ---------------------------------------------------


def test_fetch_24h_skip_within_window(db, mocker):
    """Watermark within 24h -> skip with return 0."""
    recent = _dt.date.today().isoformat()
    db.upsert_watermark(
        source="openbb", ticker="AAPL", field="multi_provider",
        last_observation_date=recent, success=True,
    )
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n == 0
    with sqlite3.connect(db.db_path) as c:
        total = c.execute("SELECT COUNT(*) FROM raw_openbb").fetchone()[0]
    assert total == 0


def test_fetch_24h_skip_does_not_fire_after_2d(db, mocker):
    old = (_dt.date.today() - _dt.timedelta(days=2)).isoformat()
    db.upsert_watermark(
        source="openbb", ticker="AAPL", field="multi_provider",
        last_observation_date=old, success=True,
    )
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0


# --- key-missing graceful degradation ----------------------------------


def test_fetch_polygon_key_missing_falls_back_to_fmp_and_tiingo(db, mocker):
    """Polygon key absent -> route skipped silently; FMP + Tiingo still write.
    Source returns success (n>0), watermark records no error."""
    def fake_get_key(provider, *args, **kwargs):
        return {"fmp": "fmp-k", "tiingo": "tiingo-k"}.get(provider)
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=fake_get_key,
    )
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert providers == ["fmp", "tiingo"]
    w = db.get_watermark("openbb", "AAPL", "multi_provider")
    assert w is not None
    assert w["error_count"] == 0


def test_fetch_fmp_key_missing_falls_back_to_polygon_and_tiingo(db, mocker):
    def fake_get_key(provider, *args, **kwargs):
        return {"polygon": "p-k", "tiingo": "t-k"}.get(provider)
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=fake_get_key,
    )
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert providers == ["polygon", "tiingo"]


def test_fetch_tiingo_key_missing_falls_back_to_fmp_and_polygon(db, mocker):
    def fake_get_key(provider, *args, **kwargs):
        return {"fmp": "f-k", "polygon": "p-k"}.get(provider)
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=fake_get_key,
    )
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert providers == ["fmp", "polygon"]


def test_fetch_all_keys_missing_records_error_and_returns_zero(db, mocker):
    """No keys provisioned -> all three routes skipped; n=0 and watermark
    records error_count >= 1."""
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        return_value=None,
    )
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n == 0
    w = db.get_watermark("openbb", "AAPL", "multi_provider")
    assert w is not None
    assert w["error_count"] >= 1


# --- per-provider HTTP failures ----------------------------------------


def test_fetch_fmp_http_error_skips_route_and_continues(db, mocker):
    """FMP /stable/* all 404 -> FMP route emits zero rows; Polygon + Tiingo
    continue normally."""
    _mock_all_keys_present(mocker)

    def side_effect(url, **kwargs):
        if "financialmodelingprep.com" in url:
            return _404_resp(mocker)
        if "api.polygon.io/v3/reference" in url:
            return _ok_resp(mocker, POLYGON_REF)
        if "api.polygon.io/v2/aggs" in url:
            return _ok_resp(mocker, POLYGON_PREV)
        if "api.tiingo.com/tiingo/daily" in url:
            return _ok_resp(mocker, TIINGO_DAILY)
        if "api.tiingo.com/iex" in url:
            return _ok_resp(mocker, TIINGO_IEX)
        return _404_resp(mocker)

    mocker.patch(
        "src.common.datasources.openbb_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.openbb_source.time.sleep")

    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert "fmp" not in providers
    assert "polygon" in providers
    assert "tiingo" in providers


def test_fetch_all_providers_http_error_records_error(db, mocker):
    _mock_all_keys_present(mocker)
    mocker.patch(
        "src.common.datasources.openbb_source.requests.get",
        return_value=_404_resp(mocker),
    )
    mocker.patch("src.common.datasources.openbb_source.time.sleep")
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n == 0
    w = db.get_watermark("openbb", "AAPL", "multi_provider")
    assert w is not None
    assert w["error_count"] >= 1


# --- polite-delay verification -----------------------------------------


def test_fetch_polygon_polite_delay_applied(db, mocker):
    """Polygon free tier = 5/min -> 12s between Polygon calls.
    `time.sleep` invoked with >= 12 between Polygon calls."""
    _mock_all_keys_present(mocker)

    def side_effect(url, **kwargs):
        if "api.polygon.io/v3/reference" in url:
            return _ok_resp(mocker, POLYGON_REF)
        if "api.polygon.io/v2/aggs" in url:
            return _ok_resp(mocker, POLYGON_PREV)
        # All other URLs return empty so only Polygon route fires.
        return _404_resp(mocker)

    mocker.patch(
        "src.common.datasources.openbb_source.requests.get",
        side_effect=side_effect,
    )
    sleep_spy = mocker.patch(
        "src.common.datasources.openbb_source.time.sleep",
    )
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    # At least one sleep call should be >= 12s (the Polygon inter-call delay).
    polygon_delays = [c.args[0] for c in sleep_spy.call_args_list if c.args and c.args[0] >= 12]
    assert polygon_delays, (
        "expected at least one polygon inter-call sleep >= 12s; "
        f"observed sleep delays: {[c.args for c in sleep_spy.call_args_list]}"
    )


# --- run_id propagation -----------------------------------------------


def test_fetch_threads_run_id_through_to_rows(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="my-run-42", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        run_ids = {
            r[0] for r in c.execute("SELECT DISTINCT run_id FROM raw_openbb")
        }
    assert run_ids == {"my-run-42"}


def test_fetch_uppercases_ticker(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="aapl", run_id="r1", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        tickers = {
            r[0] for r in c.execute("SELECT DISTINCT ticker FROM raw_openbb")
        }
    assert tickers == {"AAPL"}
```

- [ ] **Step 10: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_openbb_fetch.py -v
```

Expected: `ImportError: cannot import name 'OpenBBSource'` (or `ModuleNotFoundError` on the source file).

- [ ] **Step 11: Implement `openbb_source.py`**

Create `src/common/datasources/openbb_source.py` using the **Write tool** (not bash heredoc — heredoc strips backticks in docstrings, A.3.6 lesson):

```python
"""OpenBBSource — multi-provider router for FMP-stable + Polygon + Tiingo.

A.3.7 deliverable per spec section 6.7. Unlike the single-vendor adapters
(YahooSource, EdgarSource, ...), this source routes a single logical
`fetch_fundamentals_for_ticker(ticker, run_id)` call across THREE
independent providers and writes each provider's response as long-format
rows into `raw_openbb`. The PK
`(run_id, ticker, field_name, provider_used)` means the same field from
two providers writes two rows — that's the cross-vendor cross-validation
substrate the materialization layer (A.3.10) joins on.

Provider contracts (verified 2026-05-21)
----------------------------------------
* FMP issued post-2024 keys are STABLE-API ONLY. Legacy `/api/v3/`
  returns 403 "Legacy Endpoint". We target `/stable/{endpoint}?symbol=...`
* Polygon free tier = 5 req/min. We sleep 12s between Polygon calls.
* Tiingo free tier = 1000 req/day. 0.5s politeness delay.

Key-missing graceful degradation
--------------------------------
If `get_api_key(provider)` returns None, that route is silently skipped
(INFO log, not WARNING — missing key is a configuration choice, not an
error). The fetch returns "success" if at least one route wrote at least
one row. Only when ALL three routes are skipped (zero keys provisioned)
does the watermark record an error.

24h refresh-skip semantics
--------------------------
Per spec section 6.7 "Watermark per (openbb, ticker, field)". v1 uses a
single synthetic field name `multi_provider` so the entire router's
state is gated by one watermark. If the previous successful pull is
within 24h, the fetch returns 0 immediately. Caller can force a refresh
by clearing the watermark or passing `refresh_after_hours=0`.

Long-format row emission
------------------------
Each provider's response is normalized to a small canonical-field set
(see `_OpenBBFieldName` Literal in schemas.py and the docstring of each
`_map_*` function). String-valued fields (sector, industry, currency,
primary_exchange) are emitted with `value=None` and the string value
encoded in `unit` (e.g., `unit='sector:Technology'`).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)
from src.common.env_loader import get_api_key
from src.common.schemas import RawOpenBBRow


_log = logging.getLogger(__name__)


# --- URL templates ----------------------------------------------------

_FMP_PROBE = (
    "https://financialmodelingprep.com/stable/profile"
    "?symbol=AAPL&apikey={key}"
)
_FMP_PROFILE = (
    "https://financialmodelingprep.com/stable/profile"
    "?symbol={ticker}&apikey={key}"
)
_FMP_QUOTE = (
    "https://financialmodelingprep.com/stable/quote"
    "?symbol={ticker}&apikey={key}"
)
_FMP_RATIOS_TTM = (
    "https://financialmodelingprep.com/stable/ratios-ttm"
    "?symbol={ticker}&apikey={key}"
)
_FMP_KEY_METRICS_TTM = (
    "https://financialmodelingprep.com/stable/key-metrics-ttm"
    "?symbol={ticker}&apikey={key}"
)

_POLYGON_REF = (
    "https://api.polygon.io/v3/reference/tickers/{ticker}?apiKey={key}"
)
_POLYGON_PREV = (
    "https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?apiKey={key}"
)

_TIINGO_DAILY = "https://api.tiingo.com/tiingo/daily/{ticker_lower}?token={key}"
_TIINGO_IEX = "https://api.tiingo.com/iex/{ticker_lower}?token={key}"


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Trade Identifier; research@example.com)"
    ),
}

# Per-provider polite delays (seconds). Polygon free tier is 5/min so we
# sleep 12s between Polygon calls to stay under the ceiling. FMP + Tiingo
# are fast and we only enforce a token politeness delay.
_POLYGON_DELAY_SEC = 12.0
_FMP_DELAY_SEC = 0.5
_TIINGO_DELAY_SEC = 0.5

# Per spec section 6.7: 24h refresh-skip window. Multi-provider data
# changes intraday (last_price), so 24h is the natural cadence — same as
# a typical Layer-1 daily refresh cycle.
_REFRESH_SKIP_HOURS = 24

# Per-provider HTTP timeout (seconds).
_HTTP_TIMEOUT_SEC = 15


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(v: Any) -> Optional[float]:
    """Coerce a JSON value to float, returning None for null / unparseable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Per-provider mappers — pure functions, no I/O.
# Each takes the parsed JSON for ONE provider's bundle of endpoints
# and returns a list of (field_name, value, unit) tuples. The caller
# wraps each tuple into a RawOpenBBRow with the standard run_id /
# ticker / provider_used / scrape_timestamp.
# ----------------------------------------------------------------------


def _map_fmp(
    *,
    profile: Optional[list],
    quote: Optional[list],
    ratios_ttm: Optional[list],
    key_metrics_ttm: Optional[list],
) -> list[tuple[str, Optional[float], str]]:
    """Map FMP-stable JSON responses to canonical (field, value, unit) tuples."""
    out: list[tuple[str, Optional[float], str]] = []

    if profile and isinstance(profile, list) and profile:
        p = profile[0]
        out.append(("beta", _safe_float(p.get("beta")), "ratio"))
        # String fields encoded via unit prefix.
        if p.get("sector"):
            out.append(("sector", None, f"sector:{p['sector']}"))
        if p.get("industry"):
            out.append(("industry", None, f"industry:{p['industry']}"))
        if p.get("currency"):
            out.append(("currency", None, f"currency:{p['currency']}"))
        if p.get("exchangeShortName"):
            out.append((
                "primary_exchange", None,
                f"primary_exchange:{p['exchangeShortName']}",
            ))

    if quote and isinstance(quote, list) and quote:
        q = quote[0]
        out.append(("last_price", _safe_float(q.get("price")), "usd"))
        out.append(("prev_close", _safe_float(q.get("previousClose")), "usd"))
        out.append(("volume", _safe_float(q.get("volume")), "shares"))
        # Prefer key-metrics-ttm.marketCapTTM below; fall back to quote.marketCap.
        if q.get("marketCap") is not None:
            out.append(("market_cap", _safe_float(q.get("marketCap")), "usd"))

    if ratios_ttm and isinstance(ratios_ttm, list) and ratios_ttm:
        r = ratios_ttm[0]
        out.append(("pe_ratio", _safe_float(r.get("peRatioTTM")), "ratio"))
        out.append(("ps_ratio", _safe_float(r.get("priceToSalesRatioTTM")), "ratio"))
        out.append(("pb_ratio", _safe_float(r.get("priceToBookRatioTTM")), "ratio"))
        out.append((
            "dividend_yield",
            _safe_float(r.get("dividendYieldTTM")),
            "ratio",
        ))

    if key_metrics_ttm and isinstance(key_metrics_ttm, list) and key_metrics_ttm:
        k = key_metrics_ttm[0]
        out.append((
            "ev_to_ebitda",
            _safe_float(k.get("enterpriseValueOverEBITDATTM")),
            "ratio",
        ))
        # Prefer key-metrics-ttm.marketCapTTM if quote didn't have it.
        if k.get("marketCapTTM") is not None and not any(
            t[0] == "market_cap" for t in out
        ):
            out.append(("market_cap", _safe_float(k.get("marketCapTTM")), "usd"))

    return out


def _map_polygon(
    *,
    ref: Optional[dict],
    prev: Optional[dict],
) -> list[tuple[str, Optional[float], str]]:
    """Map Polygon JSON responses to canonical (field, value, unit) tuples."""
    out: list[tuple[str, Optional[float], str]] = []

    if ref and isinstance(ref, dict):
        results = ref.get("results")
        if isinstance(results, dict):
            if results.get("market_cap") is not None:
                out.append((
                    "market_cap",
                    _safe_float(results.get("market_cap")),
                    "usd",
                ))
            if results.get("share_class_shares_outstanding") is not None:
                out.append((
                    "shares_outstanding",
                    _safe_float(results.get("share_class_shares_outstanding")),
                    "shares",
                ))
            if results.get("primary_exchange"):
                out.append((
                    "primary_exchange", None,
                    f"primary_exchange:{results['primary_exchange']}",
                ))
            if results.get("currency_name"):
                out.append((
                    "currency", None,
                    f"currency:{results['currency_name']}",
                ))

    if prev and isinstance(prev, dict):
        results = prev.get("results")
        if isinstance(results, list) and results:
            r0 = results[0]
            out.append(("last_price", _safe_float(r0.get("c")), "usd"))
            # Polygon's `/prev` is by definition the previous day's close,
            # so `c` doubles as prev_close in this context.
            out.append(("prev_close", _safe_float(r0.get("c")), "usd"))
            out.append(("volume", _safe_float(r0.get("v")), "shares"))

    return out


def _map_tiingo(
    *,
    daily: Optional[dict],
    iex: Optional[list],
) -> list[tuple[str, Optional[float], str]]:
    """Map Tiingo JSON responses to canonical (field, value, unit) tuples."""
    out: list[tuple[str, Optional[float], str]] = []

    if daily and isinstance(daily, dict):
        if daily.get("exchangeCode"):
            out.append((
                "primary_exchange", None,
                f"primary_exchange:{daily['exchangeCode']}",
            ))

    if iex and isinstance(iex, list) and iex:
        i = iex[0]
        # Prefer `last` (real-time) over `tngoLast` (Tiingo's delayed feed).
        last = i.get("last") if i.get("last") is not None else i.get("tngoLast")
        out.append(("last_price", _safe_float(last), "usd"))
        out.append(("prev_close", _safe_float(i.get("prevClose")), "usd"))
        out.append(("volume", _safe_float(i.get("volume")), "shares"))

    return out


# ----------------------------------------------------------------------
# The source.
# ----------------------------------------------------------------------


class OpenBBSource(BaseDataSource):
    name = "openbb"
    cadence = "daily"
    provides = {
        "pe_ratio", "ps_ratio", "pb_ratio",
        "dividend_yield", "ev_to_ebitda",
        "market_cap", "last_price", "prev_close",
        "volume", "shares_outstanding",
        "primary_exchange", "sector", "industry",
        "beta", "currency",
    }

    # ------------------------------------------------------------------
    # health_check - cheap probe of FMP /stable/profile for AAPL (the
    # one route that requires no live universe context).
    # ------------------------------------------------------------------

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = _now_iso()
        fmp_key = get_api_key("fmp")
        if not fmp_key:
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.PARTIAL,
                checked_at=now,
                message="FMP key absent; cannot probe",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        url = _FMP_PROBE.format(key=fmp_key)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=10)
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
    # A.3.7: multi-provider fundamentals fetch
    # ------------------------------------------------------------------

    def _http_get_json(self, url: str) -> Optional[Any]:
        """GET one URL and parse JSON. Returns parsed JSON on 200, or
        None on any failure (HTTP error, network error, JSON decode
        error). Caller treats None as a soft per-call failure."""
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001
            _log.warning("openbb http error url=%s: %s", url, exc)
            return None
        if not resp.ok:
            _log.warning(
                "openbb http non-200 url=%s status=%s",
                url, resp.status_code,
            )
            return None
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            _log.warning("openbb json decode error url=%s: %s", url, exc)
            return None

    def _route_fmp(
        self, ticker: str,
    ) -> list[tuple[str, Optional[float], str]]:
        """Pull all four FMP endpoints and map -> canonical tuples.
        Returns [] if FMP key is missing."""
        key = get_api_key("fmp")
        if not key:
            _log.info("openbb route fmp skipped (no key)")
            return []

        # Politeness inter-call delay before each FMP request.
        time.sleep(_FMP_DELAY_SEC)
        profile = self._http_get_json(_FMP_PROFILE.format(ticker=ticker, key=key))
        time.sleep(_FMP_DELAY_SEC)
        quote = self._http_get_json(_FMP_QUOTE.format(ticker=ticker, key=key))
        time.sleep(_FMP_DELAY_SEC)
        ratios_ttm = self._http_get_json(_FMP_RATIOS_TTM.format(ticker=ticker, key=key))
        time.sleep(_FMP_DELAY_SEC)
        key_metrics_ttm = self._http_get_json(
            _FMP_KEY_METRICS_TTM.format(ticker=ticker, key=key)
        )
        return _map_fmp(
            profile=profile, quote=quote,
            ratios_ttm=ratios_ttm, key_metrics_ttm=key_metrics_ttm,
        )

    def _route_polygon(
        self, ticker: str,
    ) -> list[tuple[str, Optional[float], str]]:
        """Pull Polygon reference + prev-day aggregate. Returns [] if key
        is missing. Sleeps `_POLYGON_DELAY_SEC` between the two Polygon
        calls to respect free-tier 5/min ceiling."""
        key = get_api_key("polygon")
        if not key:
            _log.info("openbb route polygon skipped (no key)")
            return []

        ref = self._http_get_json(_POLYGON_REF.format(ticker=ticker, key=key))
        time.sleep(_POLYGON_DELAY_SEC)
        prev = self._http_get_json(_POLYGON_PREV.format(ticker=ticker, key=key))
        return _map_polygon(ref=ref, prev=prev)

    def _route_tiingo(
        self, ticker: str,
    ) -> list[tuple[str, Optional[float], str]]:
        """Pull Tiingo daily + iex. Returns [] if key is missing."""
        key = get_api_key("tiingo")
        if not key:
            _log.info("openbb route tiingo skipped (no key)")
            return []

        t = ticker.lower()
        time.sleep(_TIINGO_DELAY_SEC)
        daily = self._http_get_json(_TIINGO_DAILY.format(ticker_lower=t, key=key))
        time.sleep(_TIINGO_DELAY_SEC)
        iex = self._http_get_json(_TIINGO_IEX.format(ticker_lower=t, key=key))
        return _map_tiingo(daily=daily, iex=iex)

    def fetch_fundamentals_for_ticker(
        self,
        ticker: str,
        run_id: str,
        db=None,
        *,
        refresh_after_hours: int = _REFRESH_SKIP_HOURS,
    ) -> int:
        """Pull fundamentals across FMP + Polygon + Tiingo for one ticker.

        Pipeline:
          0. Check watermark (openbb, <ticker>, multi_provider). If the
             previous successful pull is within `refresh_after_hours`,
             return 0 immediately.
          1. For each provider in (fmp, polygon, tiingo):
               a. If key missing -> skip silently (INFO log).
               b. Else: pull provider's endpoints, map to canonical
                  (field, value, unit) tuples, build RawOpenBBRow per
                  tuple, INSERT OR IGNORE.
               c. Track per-provider success/error.
          2. Update watermark - success if at least one provider wrote
             at least one row; error otherwise (with message naming the
             failed providers).

        Returns
        -------
        int : net new rows inserted across all three providers.
        """
        if db is None:
            raise ValueError("db is required")

        field = "multi_provider"
        ticker = ticker.strip().upper()

        # 0. 24h refresh-skip
        w = db.get_watermark(self.name, ticker, field)
        if w and w.get("last_fetched_at") and w.get("error_count", 0) == 0:
            try:
                last = datetime.fromisoformat(w["last_fetched_at"])
                age_hours = (datetime.utcnow() - last).total_seconds() / 3600.0
                if age_hours < refresh_after_hours:
                    return 0
            except (TypeError, ValueError):
                pass

        scrape_ts = _now_iso()
        total_inserted = 0
        provider_errors: list[str] = []
        any_provider_succeeded = False
        import sqlite3

        provider_routes = [
            ("fmp", self._route_fmp),
            ("polygon", self._route_polygon),
            ("tiingo", self._route_tiingo),
        ]

        for provider_name, route_fn in provider_routes:
            # Probe key presence FIRST so we can distinguish "skipped
            # because no key" (no error) from "attempted but failed"
            # (recorded as error).
            key_present = bool(get_api_key(provider_name))
            if not key_present:
                provider_errors.append(f"no-key:{provider_name}")
                continue

            try:
                tuples = route_fn(ticker)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "openbb provider %s raised: %s", provider_name, exc,
                )
                provider_errors.append(f"exception:{provider_name}")
                continue

            if not tuples:
                provider_errors.append(f"empty:{provider_name}")
                continue

            rows: list[RawOpenBBRow] = []
            for field_name, value, unit in tuples:
                try:
                    rows.append(
                        RawOpenBBRow(
                            run_id=run_id,
                            ticker=ticker,
                            field_name=field_name,
                            provider_used=provider_name,
                            value=value,
                            unit=unit,
                            scrape_timestamp=scrape_ts,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "openbb row build failed provider=%s field=%s: %s",
                        provider_name, field_name, exc,
                    )
                    continue

            if not rows:
                provider_errors.append(f"no-rows:{provider_name}")
                continue

            # Count net new rows by before/after on (run_id, ticker, provider).
            with sqlite3.connect(db.db_path) as c:
                n_before = c.execute(
                    "SELECT COUNT(*) FROM raw_openbb "
                    "WHERE run_id=? AND ticker=? AND provider_used=?",
                    (run_id, ticker, provider_name),
                ).fetchone()[0]
            db.insert_raw_openbb(rows)
            with sqlite3.connect(db.db_path) as c:
                n_after = c.execute(
                    "SELECT COUNT(*) FROM raw_openbb "
                    "WHERE run_id=? AND ticker=? AND provider_used=?",
                    (run_id, ticker, provider_name),
                ).fetchone()[0]
            inserted = n_after - n_before
            total_inserted += inserted
            if inserted > 0:
                any_provider_succeeded = True

        # Watermark advance. Success requires AT LEAST ONE provider to
        # have written at least one row. If zero providers succeeded
        # (all keys missing or all routes failed) we record error.
        if any_provider_succeeded:
            self.update_watermark(
                ticker=ticker, field=field,
                last_observation_date=_dt.date.today(),
                db=db, success=True,
            )
        else:
            self.update_watermark(
                ticker=ticker, field=field,
                last_observation_date=_dt.date.today(),
                db=db, success=False,
                error_message="; ".join(provider_errors) or "no-providers",
            )

        return total_inserted
```

- [ ] **Step 12: Run tests to verify they pass**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_openbb_fetch.py -v
```

Expected: All 16 tests PASS.

- [ ] **Step 13: Verify no A.1 regressions for other sources (smoke)**

```
./venv/Scripts/python.exe -m pytest tests/datasources -k "health" -v 2>&1 | tail -10
```

Expected: any pre-existing source health-check tests still pass.

- [ ] **Step 14: Commit**

```
git add src/common/datasources/openbb_source.py tests/fixtures/openbb tests/datasources/test_openbb_fetch.py
git commit -m "feat(datasources): OpenBBSource.fetch_fundamentals_for_ticker - 3-provider router (FMP-stable + Polygon + Tiingo) with key-missing graceful degradation"
```

---

## Task 4: Integration test (end-to-end mocked HTTP, URL-dispatch single side_effect)

**Files:**
- Create: `tests/datasources/test_integration_a3_7.py`

- [ ] **Step 1: Write the integration test**

Create `tests/datasources/test_integration_a3_7.py`:

```python
"""End-to-end orchestration test for A.3.7:
- Full happy path: all 3 providers route, all endpoints OK, raw_openbb
  populated, watermark advanced clean.
- Idempotent re-pull within 24h: zero new rows.
- Partial failure: one provider 404s, the surviving two still write; the
  watermark stays in success state because at least one provider wrote.
- Key-missing matrix: a provider missing -> the other two still write.

All HTTP is mocked via a SINGLE side_effect on
`src.common.datasources.openbb_source.requests.get` (URL-dispatch
pattern, NOT one patch per URL). This pattern is the explicit lesson
from A.3.5 Wave 3 + A.3.6 Wave 2 — two `mocker.patch` calls competing
for the same target broke tests there.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.openbb_source import OpenBBSource


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "openbb"
FMP_QUOTE = (FIXTURE_DIR / "fmp_quote_AAPL.json").read_text(encoding="utf-8")
FMP_PROFILE = (FIXTURE_DIR / "fmp_profile_AAPL.json").read_text(encoding="utf-8")
FMP_RATIOS_TTM = (FIXTURE_DIR / "fmp_ratios_ttm_AAPL.json").read_text(encoding="utf-8")
FMP_KEY_METRICS_TTM = (FIXTURE_DIR / "fmp_key_metrics_ttm_AAPL.json").read_text(encoding="utf-8")
POLYGON_REF = (FIXTURE_DIR / "polygon_aapl_ref.json").read_text(encoding="utf-8")
POLYGON_PREV = (FIXTURE_DIR / "polygon_aapl_prev.json").read_text(encoding="utf-8")
TIINGO_DAILY = (FIXTURE_DIR / "tiingo_aapl_daily.json").read_text(encoding="utf-8")
TIINGO_IEX = (FIXTURE_DIR / "tiingo_aapl_iex.json").read_text(encoding="utf-8")


def _ok(mocker, text: str):
    resp = mocker.MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.text = text
    resp.content = text.encode()
    import json
    resp.json = lambda: json.loads(text)
    return resp


def _404(mocker):
    resp = mocker.MagicMock()
    resp.status_code = 404
    resp.ok = False
    resp.text = ""
    resp.content = b""
    resp.json = lambda: {}
    return resp


def _make_url_dispatch(
    mocker,
    *,
    tiingo_404: bool = False,
    polygon_404: bool = False,
    fmp_404: bool = False,
):
    """URL-dispatch mock — single side_effect routes by URL substring.

    Lesson from A.3.5/A.3.6: do NOT patch the same target twice with
    different side effects; the second patch wins and the first becomes
    dead code. One mock, one side_effect, branch inside.
    """
    def side_effect(url, **kwargs):
        # FMP
        if "financialmodelingprep.com" in url:
            if fmp_404:
                return _404(mocker)
            if "/stable/quote" in url:
                return _ok(mocker, FMP_QUOTE)
            if "/stable/profile" in url:
                return _ok(mocker, FMP_PROFILE)
            if "/stable/ratios-ttm" in url:
                return _ok(mocker, FMP_RATIOS_TTM)
            if "/stable/key-metrics-ttm" in url:
                return _ok(mocker, FMP_KEY_METRICS_TTM)
            return _404(mocker)
        # Polygon
        if "api.polygon.io" in url:
            if polygon_404:
                return _404(mocker)
            if "/v3/reference/tickers/" in url:
                return _ok(mocker, POLYGON_REF)
            if "/v2/aggs/ticker/" in url and "/prev" in url:
                return _ok(mocker, POLYGON_PREV)
            return _404(mocker)
        # Tiingo
        if "api.tiingo.com" in url:
            if tiingo_404:
                return _404(mocker)
            if "/tiingo/daily/" in url:
                return _ok(mocker, TIINGO_DAILY)
            if "/iex/" in url:
                return _ok(mocker, TIINGO_IEX)
            return _404(mocker)
        return _404(mocker)

    mocker.patch(
        "src.common.datasources.openbb_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.openbb_source.time.sleep")


def _mock_all_keys_present(mocker):
    def fake_get_key(provider, *args, **kwargs):
        return {
            "fmp": "fmp-test-k",
            "polygon": "poly-test-k",
            "tiingo": "tiingo-test-k",
        }.get(provider)
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=fake_get_key,
    )


def test_a3_7_full_flow_writes_all_three_providers(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v9()

    _mock_all_keys_present(mocker)
    _make_url_dispatch(mocker)

    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="run-1", db=db,
    )
    assert n > 0

    with sqlite3.connect(db_path) as c:
        by_p = {
            r[0]: r[1] for r in c.execute(
                "SELECT provider_used, COUNT(*) FROM raw_openbb GROUP BY provider_used"
            )
        }
    assert set(by_p.keys()) == {"fmp", "polygon", "tiingo"}
    # Each provider should emit at least 3 fields.
    for p in ("fmp", "polygon", "tiingo"):
        assert by_p[p] >= 3, f"{p} produced only {by_p[p]} rows"

    w = db.get_watermark("openbb", "AAPL", "multi_provider")
    assert w is not None
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_a3_7_idempotent_rerun_within_24h_skips(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v9()

    _mock_all_keys_present(mocker)
    _make_url_dispatch(mocker)
    src = OpenBBSource()

    n1 = src.fetch_fundamentals_for_ticker(ticker="AAPL", run_id="run-1", db=db)
    assert n1 > 0

    # Re-pull immediately: the 24h skip fires, returning 0.
    n2 = src.fetch_fundamentals_for_ticker(ticker="AAPL", run_id="run-1", db=db)
    assert n2 == 0


def test_a3_7_partial_failure_tiingo_404(tmp_path: Path, mocker):
    """Tiingo 404; FMP + Polygon survive. The watermark stays in success
    state because at least one provider wrote; total rows reflect only
    the surviving providers."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v9()

    _mock_all_keys_present(mocker)
    _make_url_dispatch(mocker, tiingo_404=True)

    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="run-1", db=db,
    )
    assert n > 0

    with sqlite3.connect(db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert providers == ["fmp", "polygon"]

    w = db.get_watermark("openbb", "AAPL", "multi_provider")
    assert w is not None
    # At least one provider succeeded so success path fires; error_count == 0.
    assert w["error_count"] == 0


def test_a3_7_key_missing_polygon_falls_back(tmp_path: Path, mocker):
    """Polygon key absent; FMP + Tiingo still write. Source returns success;
    watermark records no error."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v9()

    def fake_get_key(provider, *args, **kwargs):
        return {"fmp": "f", "tiingo": "t"}.get(provider)
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=fake_get_key,
    )
    _make_url_dispatch(mocker)

    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="run-1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert providers == ["fmp", "tiingo"]


def test_a3_7_long_format_admits_market_cap_from_fmp_and_polygon(
    tmp_path: Path, mocker,
):
    """`market_cap` is emitted by BOTH FMP (key-metrics-ttm.marketCapTTM
    OR quote.marketCap) and Polygon (reference.results.market_cap). The
    long-format PK admits both — this is the cross-vendor cross-validation
    substrate the spec calls for."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v9()

    _mock_all_keys_present(mocker)
    _make_url_dispatch(mocker)

    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="run-1", db=db,
    )

    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT provider_used, value FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='market_cap' "
            "ORDER BY provider_used"
        ).fetchall()
    providers = {r[0] for r in rows}
    assert "fmp" in providers
    assert "polygon" in providers
    # Two distinct values from two providers — not collapsed.
    assert len(rows) >= 2


def test_a3_7_fresh_run_id_appends_new_snapshot(tmp_path: Path, mocker):
    """A fresh run_id is the supported way to refresh — every row written
    under the new run_id is a distinct observation in raw_openbb."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v9()

    _mock_all_keys_present(mocker)
    _make_url_dispatch(mocker)
    src = OpenBBSource()

    src.fetch_fundamentals_for_ticker(ticker="AAPL", run_id="run-1", db=db)

    # Backdate the watermark so the 24h skip doesn't fire on second call.
    # We rely on refresh_after_hours=0 to bypass the recency check.
    src.fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="run-2", db=db, refresh_after_hours=0,
    )

    with sqlite3.connect(db_path) as c:
        run_ids = sorted(
            r[0] for r in c.execute("SELECT DISTINCT run_id FROM raw_openbb")
        )
    assert run_ids == ["run-1", "run-2"]
```

- [ ] **Step 2: Run the integration test**

```
./venv/Scripts/python.exe -m pytest tests/datasources/test_integration_a3_7.py -v
```

Expected: 6 PASS.

- [ ] **Step 3: Run the full non-integration suite — no regressions**

```
./venv/Scripts/python.exe -m pytest -m "not integration" -v 2>&1 | tail -5
```

Expected: ~474 passed (427 baseline + 12 schema + 13 database + 16 fetch + 6 integration_a3_7 — counts approximate; ±2 is within tolerance).

- [ ] **Step 4: Commit**

```
git add tests/datasources/test_integration_a3_7.py
git commit -m "test: A.3.7 integration acceptance - end-to-end multi-provider fetch with key-missing degradation + partial-failure handling"
```

---

## Task 5: Update the build plan

**Files:**
- Modify: the build plan

- [ ] **Step 1: Locate the A.3 row in §5.1.0**

Grep the build plan for `**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 + A.3.6 shipped 2026-05-21**` to find the line.

- [ ] **Step 2: Update the A.3 row's shipped marker**

Use `Edit` to replace the substring `**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 + A.3.6 shipped 2026-05-21**` with `**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 + A.3.6 + A.3.7 shipped 2026-05-21**`, and update the plan-link parenthetical to add the A.3.7 plan file plus bump the schema marker from v8 to v9.

Concretely, locate this fragment in the build plan:

```
**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 + A.3.6 shipped 2026-05-21** (plans: [A.3.1](docs/design/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/design/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md), [A.3.3](docs/design/plans/2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md), [A.3.4](docs/design/plans/2026-05-21-phase-a3-sub04-edgar-insider-and-filings.md), [A.3.5](docs/design/plans/2026-05-21-phase-a3-sub05-fred-finra.md), [A.3.6](docs/design/plans/2026-05-21-phase-a3-sub06-stockanalysis-ratios.md)): schema v8
```

Replace with:

```
**A.3.1 + A.3.2 + A.3.3 + A.3.4 + A.3.5 + A.3.6 + A.3.7 shipped 2026-05-21** (plans: [A.3.1](docs/design/plans/2026-05-21-phase-a3-sub01-watermarks-foundation.md), [A.3.2](docs/design/plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md), [A.3.3](docs/design/plans/2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md), [A.3.4](docs/design/plans/2026-05-21-phase-a3-sub04-edgar-insider-and-filings.md), [A.3.5](docs/design/plans/2026-05-21-phase-a3-sub05-fred-finra.md), [A.3.6](docs/design/plans/2026-05-21-phase-a3-sub06-stockanalysis-ratios.md), [A.3.7](docs/design/plans/2026-05-21-phase-a3-sub07-openbb-router.md)): schema v9
```

Then in the same sentence (continuing the A.3 row description), append the A.3.7 fragment after the existing A.3.6 fragment:

```
 + OpenBBSource.fetch_fundamentals_for_ticker (multi-provider router across FMP-stable + Polygon + Tiingo with key-missing graceful degradation, per-provider polite delays — Polygon 12s/free tier, FMP+Tiingo 0.5s — and 24h refresh skip per spec §6.7) + raw_openbb long-format table (PK `(run_id, ticker, field_name, provider_used)` — same field from two providers writes two rows by design for cross-vendor cross-validation).
```

- [ ] **Step 3: Commit**

```
git add <build-plan>
git commit -m "docs: mark A.3.7 shipped - OpenBB multi-provider router (FMP-stable + Polygon + Tiingo)"
```

---

## Phase A.3.7 — Definition of Done

- [ ] `./venv/Scripts/python.exe -m pytest -m "not integration"` shows ~474 tests passing
- [ ] `migrate_to_v9()` produces `schema_version == 9`
- [ ] `raw_openbb` table exists with PK `(run_id, ticker, field_name, provider_used)` and columns `run_id, ticker, field_name, provider_used, value, unit, scrape_timestamp`
- [ ] `insert_raw_openbb()` uses INSERT OR IGNORE (re-runs within same run_id idempotent)
- [ ] `RawOpenBBRow.field_name` is constrained to the 15-field canonical Literal whitelist
- [ ] `RawOpenBBRow.provider_used` is constrained to `{fmp, polygon, tiingo}` Literal
- [ ] `RawOpenBBRow.value` accepts None (string-valued canonical fields stored with value=None + category-tagged unit)
- [ ] `RawOpenBBRow.ticker` is uppercased + matches `_TICKER_RE`
- [ ] `RawOpenBBRow.unit` is mandatory and non-empty
- [ ] `OpenBBSource.fetch_fundamentals_for_ticker()` routes across all three providers when keys are present
- [ ] `OpenBBSource.fetch_fundamentals_for_ticker()` silently skips a provider whose `get_api_key(<provider>)` returns None (INFO log; no watermark error UNLESS all providers degrade and zero rows write)
- [ ] `OpenBBSource.fetch_fundamentals_for_ticker()` records `error_count >= 1` ONLY when zero providers wrote any rows
- [ ] `OpenBBSource.fetch_fundamentals_for_ticker()` writes `market_cap` from BOTH FMP and Polygon as two distinct rows (cross-vendor cross-validation substrate)
- [ ] `OpenBBSource.fetch_fundamentals_for_ticker()` skips re-pull within 24h of a previous *clean* (error_count == 0) watermark
- [ ] `OpenBBSource.fetch_fundamentals_for_ticker()` re-pulls after 24h OR with explicit `refresh_after_hours=0`
- [ ] `OpenBBSource.fetch_fundamentals_for_ticker()` enforces a `>=12 second` polite delay between Polygon calls
- [ ] All FMP endpoints target the **stable** API (`/stable/...`), NOT the legacy `/api/v3/` API (which returns 403 for post-2024 keys)
- [ ] Source uses lightweight direct HTTP via `requests`, NOT the `openbb` PyPI package
- [ ] No live network in unit tests (all HTTP mocked via pytest-mock with a SINGLE `requests.get` patch using a URL-dispatch side_effect — the A.3.5/A.3.6 lesson)
- [ ] No new runtime dependencies added to `requirements.txt`
- [ ] The build plan marks A.3.7 shipped (schema v9)
- [ ] No A.1 / A.2 / A.3.1 / A.3.2 / A.3.3 / A.3.4 / A.3.5 / A.3.6 regressions (`health_check`, all prior `raw_*` tables, watermarks)
- [ ] Git log shows ~5 task commits

---

## Self-review

**Spec coverage:**

| A.3 spec section | A.3.7 task |
|---|---|
| §6.7 New `src/common/datasources/openbb_source.py` | Task 3 |
| §6.7 On init: reads `.env` for FMP/Polygon/Tiingo via `get_api_key` | Task 3 (`_route_*` calls `get_api_key(provider)` lazily per-route) |
| §6.7 `fetch_fundamentals_for_ticker(ticker)`: tries FMP → fallback Polygon → fallback Tiingo | Task 3 (sequential `provider_routes` loop; each independently routed) |
| §6.7 Each successful pull writes one row to `raw_openbb(ticker, field, value, provider_used, scrape_timestamp)` | Task 1 + Task 2 + Task 3 (`RawOpenBBRow` schema + `raw_openbb` DDL + per-route `INSERT OR IGNORE` writes) |
| §6.7 Watermark per (`openbb`, ticker, field) | Task 3 (`update_watermark` with `field="multi_provider"` discriminator — v1 single-watermark gating; could be split per-field-name in a later refinement if A.3.10 surfaces the need) |
| §6.7 Missing keys → that route degrades; registry's other sources still cover the field | Task 3 (`get_api_key(...) is None` → silent skip, INFO log, no watermark error UNLESS all providers degrade) + Task 3 tests `test_fetch_*_key_missing_*` |
| §13 Failure mode "OpenBB key missing → Provider silently skipped; other sources cover" | Task 3 (silent skip + Task 3 Step 9 fixture exercises this explicitly) |
| §6.8 `.env` loader via `src/common/env_loader.py` | Task 0 (verification only; loader was shipped in A.3.1) |
| Note on FMP stable-API constraint (post-2024 keys 403 against legacy `/api/v3/`) | Task 3 (`_FMP_*` URL constants all target `/stable/...`) |
| Principle 5 no silent overwrite | Task 2 (`INSERT OR IGNORE` on `raw_openbb` keyed by `(run_id, ticker, field_name, provider_used)`) |
| Principle 6 watermarks | Task 3 (per-ticker `multi_provider` watermark with 24h refresh skip) |

**Out of scope** (explicitly deferred):

- A.3.10 materialization that consumes `raw_openbb` for cross-vendor reconciliation and `data_approximated_flags` propagation. The long-format substrate is ready; the per-field cross-vendor consensus rule lives in `materialization.py` later (with conservative-merge semantics — when FMP and Polygon disagree on `market_cap`, the merger picks the median + flags the divergence on `field_provenance`).
- The OpenBB Python SDK. Direct HTTP keeps install lean and avoids cross-cutting SDK config; if we later need provider features beyond plain-REST, the source's `_route_*` methods can be swapped to call the SDK without changing the public `fetch_fundamentals_for_ticker` signature.
- Yfinance as a fourth route. YahooSource is a first-class source already; routing it through OpenBB would just double-count.
- Intrinio. The `api_keys.yaml` reserves the slot but no key is provisioned. To add Intrinio later: add a `_route_intrinio` method + `_map_intrinio` mapper + entry in `provider_routes`, and extend the `_OpenBBProvider` Literal in schemas.py.
- Per-field tenacity retry. v1 has the per-call `_http_get_json` that handles transient errors by returning None; if a provider becomes especially flaky a tenacity wrapper can be added in a one-line change.
- Live integration test against the real `financialmodelingprep.com` / `api.polygon.io` / `api.tiingo.com` endpoints → A.5.

**Placeholder scan:** No "TBD", "TODO", or "implement later" in any code block. Every step contains full source.

**Pydantic v2 gotchas avoided:**

- `RawOpenBBRow.value` is `Optional[float] = None` so string-valued canonical fields (sector / industry / currency / primary_exchange) map cleanly to NULL while still being addressable rows for provenance.
- `field_name` and `provider_used` use Pydantic `Literal[...]` to catch typos at validation time rather than silently corrupting the raw store.
- `ticker` uses `@field_validator(mode="before")` to upper-case before the regex check; same pattern as `ticker` from A.3.3 / A.3.4 / A.3.5 / A.3.6.
- `unit` is a required string with a non-empty validator. We deliberately did NOT make `unit` a Literal because the category-tagged unit values (`unit='sector:Technology'`) require open-ended strings.
- The Literal alias types `_OpenBBFieldName` and `_OpenBBProvider` are module-private (underscore prefix) so they aren't part of the public `schemas` API surface; only `RawOpenBBRow` is exported.

**Per-provider field-mapping strategy (load-bearing):**

The mapping table at the top of this plan defines the v1 contract. Three design decisions are worth flagging:

1. **`market_cap` is emitted by both FMP and Polygon, intentionally.** This is the canonical cross-vendor cross-validation pair — the materialization layer (A.3.10) joins on `(ticker, field_name='market_cap')` and gets two rows it can compare. If FMP says `$2.81T` and Polygon says `$2.83T`, that 0.7% divergence is logged as a `data_approximated_flag` on the canonical row; if they disagree by >5% the materialization layer marks the merged value as low-confidence.

2. **`last_price` is emitted by all three providers.** Same rationale as `market_cap` — the live-quote field is the most volatile and most prone to "stale cache" failures, so having three providers' simultaneous observations enables the equivalence harness to flag stale providers.

3. **String-valued fields (`sector`, `industry`, `primary_exchange`, `currency`) go in via `unit=category:value` rather than via a separate column.** This is a deliberate v1 simplification — adding a `string_value` column would expand `raw_openbb` to seven columns and force every numeric-field row to write a NULL in it. The `unit:category:value` convention keeps the table six columns and lets the materialization layer split on `':'` when it needs the string. If/when a sub-phase introduces enough string-valued fields to make this awkward, splitting them into their own `raw_openbb_strings` table is a clean refactor.

**24h refresh-skip semantics:**

Per spec §6.7, the watermark `(openbb, <ticker>, field)` gates refreshes. v1 uses a single synthetic field name `multi_provider` to gate the entire router as one unit. Logic:

- **No watermark** → fetch (first-time pull).
- **Watermark `last_fetched_at` older than 24h** → fetch.
- **Watermark within last 24h AND error_count == 0** → skip (return 0).
- **Watermark within last 24h AND error_count > 0** → fetch (last attempt had errors; try again).

We use `last_fetched_at` for the recency check (not `last_observation_date`) because `last_observation_date` is a calendar date and would lose intraday resolution; multi-provider data is intraday-meaningful (`last_price`).

The "previous attempt had errors → bypass the skip" rule is the standard A.3 pattern from A.3.4/A.3.5/A.3.6: transient errors don't lock a ticker out of refresh for a full window.

Caller can also force a refresh in two ways:
1. Pass `refresh_after_hours=0` to disable the skip entirely.
2. Clear the watermark directly via `db.upsert_watermark(..., last_observation_date=None, ...)` — but this only resets the calendar date; the recency check looks at `last_fetched_at` which is set on every upsert. A targeted "force refresh now" helper could be added in a follow-up sub-phase but isn't blocking for A.3.7.

**Architecture risk: provider TOS / rate limits**

The free-tier limits are tight on Polygon (5 req/min) but generous on FMP (250 req/day on trial, way higher on paid) and Tiingo (1000 req/day). At 24h refresh cadence per ticker:

- FMP: 4 endpoints × 500-ticker universe = 2000 req/day. Comfortably under FMP's per-key ceilings on the configured account.
- Polygon: 2 endpoints × 500-ticker universe = 1000 req/day, OR 720 req/hour at 5/min. So 500 tickers takes ~100 minutes of wall clock if run serially — acceptable for daily refresh, prohibitive for sub-daily. The 12s polite delay is already baked in.
- Tiingo: 2 endpoints × 500-ticker universe = 1000 req/day = exactly Tiingo's free-tier ceiling. We'll bump up against the wall. **Practical note**: a Tiingo paid tier keeps the 0.5s politeness delay viable; if Tiingo's free tier is too tight, the materialization layer's adapter chain can deprioritize Tiingo for tickers that already have FMP + Polygon coverage.

If a provider tightens or rate-limits us out, the source's per-provider key-missing path is the right degradation channel: detection via 429s could be added in a follow-up by wiring `tenacity.retry(stop=stop_after_attempt(3), wait=wait_exponential(...))` around `_http_get_json`. v1 doesn't have this because the polite delays alone keep us well under any documented ceiling.



**Architecture risk: provider JSON schema drift**

Each provider's JSON-response shape is private API surface and the providers can rearrange fields at any time. Two specific failure modes:

1. **Field rename.** FMP renames `peRatioTTM` to `pe_ratio_ttm`. The mapper's `r.get("peRatioTTM")` returns None, the row is silently emitted with `value=None`, and the materialization layer sees a NULL — the source falls back to YahooSource or StockanalysisSource for that field, exactly as the adapter chain is designed to. Mitigation: when the operator notices a degraded coverage rate on a specific field, the fix is to extend the mapper with the new field name (single-line change).

2. **Endpoint deprecation.** FMP retires `/stable/ratios-ttm`. The HTTP call returns 404, `_http_get_json` returns None, `_map_fmp` produces fewer tuples, the watermark logs `empty:fmp` if zero tuples came out. Mitigation: same as above — adapter chain absorbs the loss and the executing agent surfaces the failure pattern in the source-health dashboard.

The v1 mappers are deliberately defensive: every `.get(key)` returns None for absent keys (no `KeyError`), every value goes through `_safe_float` (no `ValueError` on coercion), every list-indexing is bounded (no `IndexError`). The cost is silent NULL emission on schema drift; the benefit is the source never crashes the daily pipeline.

**Test coverage shape:**

| Layer | Unit tests | Integration tests |
|---|---|---|
| Schemas (`RawOpenBBRow`) | 12 | — |
| DB migration + helpers | 13 | — |
| `OpenBBSource.fetch_fundamentals_for_ticker` (mocked HTTP, per-provider, key-missing matrix, polite delay) | 16 | — |
| End-to-end orchestration (URL-dispatch single side_effect) | — | 6 |
| **Total new tests** | **41** | **6** |

(Total counts are approximate — the codebase has historically run ±2 from the planned figure due to parametrize-collection idiosyncrasies; that variance is acceptable per the project's known-pattern note.)

**Architectural notes:**

- `OpenBBSource` keeps per-provider mappers as private module-level pure functions (`_map_fmp`, `_map_polygon`, `_map_tiingo`) rather than instance methods. This matches the A.3.6 pattern with `_normalize_metric_label` etc. — pure functions give us a sharp unit-test boundary (we can call `_map_fmp` directly with synthetic dicts; we don't need to spin up the whole source).
- `OpenBBSource` does NOT use `tenacity` retries in v1. The 24h refresh-skip + per-provider error tracking means a single transient failure simply marks the provider with `empty:<name>` on the next pull's error list and the watermark's normal retry behavior fires when 24h pass. If we observe persistent transient errors in A.5 acceptance testing, wrapping `_http_get_json` with the same `tenacity` policy as `EdgarSource._sec_get_with_retry` is a one-line change.
- The watermark uses `ticker=<ticker>` with `field="multi_provider"` — a single watermark per-ticker gates the whole multi-provider router. This is the v1 simplification; if A.3.10 surfaces a need to refresh individual fields at different cadences (e.g., `last_price` every hour, `pe_ratio` daily), splitting into per-field watermarks is a contained refactor (replace the single `field="multi_provider"` with per-field-name `field=<field_name>`).
- `OpenBBSource._http_get_json` returns `None` on failure rather than raising. This lets the per-provider routes continue with the remaining endpoints cleanly and makes the public signature of `fetch_fundamentals_for_ticker` exception-free for ordinary failure modes.
- The `total_inserted` accounting uses `n_after - n_before` per provider because SQLite's `executemany` doesn't expose per-row INSERT OR IGNORE conflict counts. The before/after gives us a clean net-new-rows number for caller bookkeeping.
- The `provider_routes` list is the dispatch table. Adding a fourth provider (Intrinio, IEX Cloud, Alpha Vantage) is structurally one entry in this list + one `_route_*` method + one `_map_*` mapper + one entry in the `_OpenBBProvider` Literal. The integration test's URL-dispatch `side_effect` would also need a new branch — that's the only consumer-side change.

**Architecture risk: same-run idempotency vs cross-run snapshots**

The PK includes `run_id`, which means:

- Within a single `run_id`, calling `fetch_fundamentals_for_ticker` twice writes once (INSERT OR IGNORE drops the second pull's rows on PK collision) — desired idempotency.
- Across `run_id`s, every pull appends a fresh snapshot. This is intentional — it lets the analytics layer reconstruct what each provider said at each historical run.

The cost is that `raw_openbb` grows linearly in (#tickers × #fields × #providers × #runs). For a 500-ticker universe with daily runs and ~10 fields per provider × 3 providers, that's 15,000 rows/day × 365 days = ~5.5M rows/year. SQLite handles that comfortably with the indexes we've defined (`idx_openbb_ticker`, `idx_openbb_field`, `idx_openbb_provider`, `idx_openbb_run`). If long-term storage becomes a concern, a quarterly archival job that compresses `raw_openbb` rows older than 90 days into a separate `raw_openbb_archive` table is a clean follow-up — but that's an A.5+ concern, not A.3.7.

**Architecture risk: API key leakage in logs**

The URL templates embed `apikey={key}` (FMP) / `apiKey={key}` (Polygon) / `token={key}` (Tiingo) as query parameters. If `requests.get(url)` ever fails AND we log the URL, the key is in the log. Mitigation:

- `_log.warning("openbb http error url=%s: %s", url, exc)` will log the full URL including the key on error.
- `_log.warning("openbb http non-200 url=%s status=%s", url, resp.status_code)` will log the full URL including the key on non-200 responses.

This is a **known risk** in v1 — we accept it because (a) logs are local-only on the development machine, not shipped to a third-party log aggregator, and (b) the alternative (URL-rewriting to redact the key before logging) is non-trivial across three providers' URL schemes. **Mitigation if/when logs leave the local machine**: add a `_redact_url(url: str) -> str` helper that re-parses the URL, strips the `apikey` / `apiKey` / `token` query params, and re-serializes; route every `_log.warning` through it. That's a contained refactor and would be added in the same sub-phase that introduces remote log shipping.

---

## Execution Handoff

**Recommended:** Subagent-driven, mirroring the A.3.6 wave pattern.

- **Wave 1 (Pre-flight + Schema + DB):** One sub-agent does Tasks 0–2 (verify environment + API keys + schema v8; add `RawOpenBBRow`; add `migrate_to_v9` + `insert_raw_openbb`). All in `src/common/`; sequential. **~12 min**.
- **Wave 2 (Source):** One sub-agent does Task 3 (8 fixtures + `openbb_source.py` + 16 fetch tests). The fixtures are independent files that can be created in parallel, but the test file imports them transitively so creation order is: fixtures FIRST, then test file, then source file. **~25 min**.
- **Wave 3 (Integration + build plan):** One sub-agent does Task 4 (integration test) inline with Task 5 (build plan update). **~10 min**.

Total: 3 waves, ~45–55 min wall-clock.

**Critical pre-execution checks for the executing agent:**

1. After Task 0, confirm `get_api_key` can find at least one provider key (FMP is the minimum useful set since it's the primary route):

   ```
   ./venv/Scripts/python.exe -c "from src.common.env_loader import get_api_key; print('fmp:', 'present' if get_api_key('fmp') else 'MISSING')"
   ```

   Expect `fmp: present`. If MISSING, the unit tests still pass (they mock the lookup), but A.5 live-integration testing later will skip the FMP route.

2. After Task 2, confirm `schema_version == 9` before proceeding:

   ```
   ./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager('data/_v9_check.db'); m.migrate_to_v9(); print(m.get_schema_version())"
   ```

   Expect `9`.

3. After Task 3, confirm the eight fixture files load as valid JSON:

   ```
   ./venv/Scripts/python.exe -c "import json, pathlib; [print(p.name, 'OK' if json.loads(p.read_text(encoding='utf-8')) is not None else 'EMPTY') for p in sorted(pathlib.Path('tests/fixtures/openbb').glob('*.json'))]"
   ```

   Expect 8 lines, all `OK`.

4. After Task 3, confirm the source imports cleanly:

   ```
   ./venv/Scripts/python.exe -c "from src.common.datasources.openbb_source import OpenBBSource; s=OpenBBSource(); print('openbb', s.name, 'provides', len(s.provides), 'fields')"
   ```

   Expect `openbb openbb provides 15 fields`.

5. **A.3.5/A.3.6 lesson — single `mocker.patch` per target with URL-dispatch `side_effect`.** When mocking HTTP in the integration test (Task 4) and the per-provider fetch tests (Task 3), use a SINGLE `mocker.patch("src.common.datasources.openbb_source.requests.get", ...)` call with a `side_effect` that branches by URL substring. Do NOT use two `mocker.patch` calls on the same target with different side effects — pytest-mock's second patch wins and the first becomes dead code. This bit A.3.5 Wave 3 + A.3.6 Wave 2 and is the explicit reason all mocks in this plan use a single dispatcher.

6. The `time.sleep(_POLYGON_DELAY_SEC)` calls in `_route_polygon` add `12s × 1 = 12s` of inter-call delay per Polygon route per ticker. The tests all patch `time.sleep` to a no-op via `mocker.patch("src.common.datasources.openbb_source.time.sleep")`. If the executing agent moves the import of `time` (e.g., `from time import sleep`), the patch target needs to follow — mocking patches the BINDING in the importing module, not the module being imported.

7. **Use the `Write` tool, not bash heredoc, when creating `openbb_source.py`.** Heredoc strips backticks inside docstrings on Windows PowerShell shells (A.3.6 lesson). The same applies to the test files and JSON fixtures — `Write` preserves the exact content.

8. **Do NOT modify other sources.** Only `src/common/datasources/openbb_source.py` is created; `stockanalysis_source.py`, `edgar_source.py`, etc. should not be touched. A.1 / A.3.x regression coverage is the safety net.

9. **Watermark dict key gotcha.** `db.get_watermark()` returns a dict with key `last_error_message`, NOT `error_message`. The implementation in Task 3 uses `w.get("error_count", 0)` for the skip-bypass logic, which is correct; if you add any debug logging that references the error message, use `w.get("last_error_message")`. (A.3.6 lesson.)

10. **`tenacity.__version__` does NOT exist** in many tenacity builds; only verify `import tenacity` succeeds. Same as A.3.3/A.3.4/A.3.5/A.3.6.

**Methodology callouts before execution:**

**1. Per-provider field mapping is a v1 contract — flag for review before A.3.10.** The mapping table at the top of this plan picks ONE canonical name per field and assigns it from each provider's response. For some fields there are reasonable alternatives — e.g., FMP exposes `pe` on the `quote` endpoint AND `peRatioTTM` on `ratios-ttm`; this plan uses the latter. Polygon's `prev` endpoint returns `c` (close) which we map to BOTH `last_price` and `prev_close` because Polygon's free tier doesn't expose a real-time quote endpoint and `/prev` is by definition the previous day's data; that double-mapping is intentional but worth a sanity check. Open option: split the mapping into a YAML config (or drop the duplicate `last_price` mapping from Polygon).

**2. Key-missing semantics — "skipped silently" vs "logged as error".** This plan treats missing keys as a configuration choice (INFO log, no watermark error). The downside is that if a key was *meant* to be present but the `.env` file got corrupted, the source would silently degrade and the watermark wouldn't surface it. The current semantics match the §13 spec language; a future enhancement could add an "expected providers" config that promotes a missing-but-expected key to a warning-level error.

**3. Single `multi_provider` watermark vs per-field-name watermarks.** This plan uses one watermark `(openbb, ticker, "multi_provider")` to gate the whole router. The spec literally says "Watermark per (`openbb`, ticker, field)" which could be read as per-field-name. The single-watermark approach is the v1 simplification; per-field-name watermarks would let us refresh `last_price` hourly while keeping `pe_ratio` daily. Splitting expands the watermark write-volume by ~15x per ticker but unlocks per-field refresh cadence — that's a deliberate trade-off, and the right answer probably depends on whether A.3.10 actually needs differentiated cadences.

**4. Cross-vendor reconciliation rule — deferred to A.3.10.** When FMP says `market_cap = $2.81T` and Polygon says `$2.83T`, what does the canonical `market_cap` become? This plan stores both raw values but doesn't define the reconciliation rule. The methodology spec (§13 of the design) suggests median + flag-on-divergence; the actual implementation lives in `materialization.py` in A.3.10. Decision: reconciliation logic stays in A.3.10 (A.3.7 is JUST the substrate).
