# Phase A.3.8 — News-Activity Sub-Signal Methodology Modules + GDELT + pytrends

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]` checkbox syntax for tracking.

**Goal:** Land the *data substrate* and the *pure-function methodology modules* for five of the seven `news_activity_score` sub-signals (spec §7.1 entries 2–5 and 7, named per §18 entries 7–11). The lead signal (Cohen-Malloy-Pomorski opportunistic insider — entry 6 / §7.1 row 1) shipped in A.3.4 and is the highest-confidence sub-signal in the composite (82 bps/mo); A.3.8 is the *supplementary* batch. A.3.9 will add Loughran-McDonald tone; A.3.10 will wire all seven modules into `news_activity.py` and `factors.py`.

Specifically A.3.8 ships:

1. **Schema v10 → v11 migration** — adds `raw_gdelt` (news-mention volume, long-format) and `raw_pytrends` (Google Trends search-volume index, long-format). Plus `RawGdeltMention` and `RawPytrendsObservation` Pydantic models (extra="forbid", same conventions as A.3.5–A.3.7) and two insert helpers (INSERT OR IGNORE).
2. **Five pure-function methodology modules** under `src/methodology/`:
   - `short_interest_delta.py` — Diether-Lee-Werner (2009).
   - `revisions_velocity.py` — Chan-Jegadeesh-Lakonishok (1996).
   - `news_volume_anomaly.py` — Tetlock (2007).
   - `filing_density.py` — Lee-So (2017).
   - `fears_signal.py` — Da-Engelberg-Gao (2015).
   Each exports a `*_VERSION` constant and a single public function. No I/O, no DB access. Same authoring convention as `src/methodology/opportunistic_insider.py`.
3. **Two new source classes** in `src/common/datasources/`:
   - `GdeltSource` — implements `fetch_news_volume(ticker, run_id, db)` against the GDELT GKG 15-minute file index (https://data.gdeltproject.org/gdeltv2/). No API key. Polite 250ms inter-file delay.
   - `PytrendsSource` — implements `fetch_search_interest(run_id, db, terms=None)` against Google Trends via the `pytrends` library (1 new runtime dep). FEARS term basket (default: `["recession", "bankruptcy", "unemployment"]` per Da-Engelberg-Gao 2015 §III; US-only `geo='US'`). Aggressive throttling — 1 call/12s = 5/min sliding window.
4. **One field added to `OpenBBSource`** — `analyst_estimates` route (FMP `/stable/analyst-estimates?symbol=...`). Adds three new entries to `_OpenBBFieldName` Literal (`eps_estimate_current`, `eps_estimate_30d_ago`, `eps_revision_direction_count`) — the three numerics `revisions_velocity.py` consumes. v1 route is FMP-only (Polygon and Tiingo's free tiers do not expose analyst estimates with sufficient history); Polygon/Tiingo silently skip this field.
5. **Orchestrator wiring** — `config/orchestrator.yaml` gains entries for `gdelt` + `pytrends` providers and `news_volume` + `fears` weights. `fetch_orchestrator.py`'s `_route_field_to_call()` adds one new branch (`field == 'news_volume' → fetch_news_volume`; `field == 'fears' → fetch_search_interest`). Existing routing for `fundamentals`, `filings`, `macro_series` is unchanged.

**Why this matters:** The seven-signal composite (spec §7) needs *all* seven inputs in DB tables and methodology code before A.3.10 can wire it into `factors.py`. A.3.4 shipped the lead signal (opportunistic insider — the alpha workhorse at 82 bps/mo). The five A.3.8 signals are *supplementary* — each contributes ~5–15 % of the composite weight per spec §7.3 — but the composite cannot ship without them. Of the five, three are pure compute against tables we already have (`short_interest_delta` reads `raw_finra` from A.3.5; `filing_density` reads `raw_edgar_filings` from A.3.4; `revisions_velocity` reads a new field on `raw_openbb` from A.3.7) and two require new ingestion (`news_volume_anomaly` ← GDELT; `fears_signal` ← pytrends). A.3.8 is therefore "land the cheap modules + the two new sources + the schema substrate" so A.3.10 can plug-and-play.

**Architecture:** Additive only. Migration v10 → v11 creates `raw_gdelt` + `raw_pytrends` and bumps `schema_version` to 11. No existing tables touched.

All five methodology modules follow the `opportunistic_insider.py` template exactly: module docstring with literature citation; module-level `*_VERSION = "1.0"` constant; one public top-level function taking primitives or dict-of-primitives (NOT a `DatabaseManager` — the caller pulls rows first and hands them in); deterministic; returns either a single float or `dict[ticker, float]`. The sign convention is signed-scalar where positive == bullish (consistent with the rest of the composite per spec §7.2). The contrarian FEARS signal (Da-Engelberg-Gao 2015 — high fear-search → bullish) is sign-flipped *inside the module* so the caller never has to know about the inversion.

Source classes follow the A.1 scaffold-then-extend pattern. Each `*Source` exposes a single new method (`fetch_news_volume` for GDELT, `fetch_search_interest` for pytrends), persists long-format rows via INSERT OR IGNORE, and updates a watermark — `(gdelt, ticker, news_volume)` for GDELT (per-ticker), `(pytrends, *, fears_basket)` for the FEARS basket pull (universe-wide).

**Critical scope boundary — no factor-scoring integration in A.3.8.** The five modules return raw signal values. Z-scoring, sector-relative normalization, weight composition, and the `news_activity_score` rollup all happen in `news_activity.py` and `factors.py` at A.3.10. A.3.8 produces *ingredients*, not the *meal*. If you find yourself editing `src/layer1_universe/factors.py` you are out of scope.

**Tech Stack:** Python 3.11+, `requests`, `pydantic` v2, `pytest`, `pytest-mock`, **`pytrends>=4.9.2`** (NEW runtime dep — added to `requirements.txt`). The `pytrends` package wraps Google Trends and is the standard FEARS-construction tool in the literature replication code (Da-Engelberg-Gao 2015 used a private Google data-feed; pytrends is the post-2014 public replica). No other new deps. GDELT 15-min files are plain CSV-gz over HTTPS — `requests` + stdlib `gzip` + `csv` are sufficient.

**Spec reference:** [docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md](../specs/2026-05-21-phase-a3-layer1-hardening-design.md) §7.1 rows 2–5 + 7, §7.2 per-signal formulas, §15 row A.3.8, §18 entries 7–11.

**Methodology references** (one-line mechanism each):

| Signal | Citation | Mechanism |
|---|---|---|
| Short-interest delta | Diether-Lee-Werner (2009), *"Short-Sale Strategies and Return Predictability,"* Review of Financial Studies 22(2), 575–607 | Rising short interest with high days-to-cover predicts negative future returns (short sellers are informed). |
| Analyst revision velocity | Chan-Jegadeesh-Lakonishok (1996), *"Momentum Strategies,"* Journal of Finance 51(5), 1681–1713 | Net upward EPS revisions over 90d predict positive future returns (analysts under-react to news). |
| News volume anomaly | Tetlock (2007), *"Giving Content to Investor Sentiment,"* Journal of Finance 62(3), 1139–1168 | Abnormal news mention volume (z-score vs trailing 30d baseline) predicts return persistence then mean-reversion. |
| 8-K filing density | Lee-So (2017), *"Uncovering Expected Returns: Information in Analyst Coverage Proxies,"* Journal of Financial Economics 124(2), 331–348 | High 8-K filing density vs trailing-365d baseline signals elevated information arrival. |
| FEARS (Google Trends) | Da-Engelberg-Gao (2015), *"The Sum of All FEARS,"* Review of Financial Studies 28(1), 1–32 | High Google search volume on fear-related terms → near-term bearish retail sentiment → contrarian bullish for next 1–2 weeks. |

**A.3.4's CMP classifier is the lead, not these five.** Cohen-Malloy-Pomorski (2012) opportunistic-insider classifier (already shipped at `src/methodology/opportunistic_insider.py`, `OPPORTUNISTIC_CLASSIFIER_VERSION = "1.0"`) delivers ~82 bps/mo abnormal return in the published literature — the highest-alpha sub-signal in the composite. The A.3.8 five are supplementary signals contributing 5–15 % weight each per spec §7.3. Future readers chasing performance regressions should look at the CMP classifier output (`raw_edgar_insider.is_opportunistic`) FIRST and the A.3.8 modules second.

**Out of scope for A.3.8:**

- Wiring the five modules into `news_activity.py` / `factors.py` → A.3.10. A.3.8 produces ingredients only.
- Loughran-McDonald risk-section tone (signal 6) → A.3.9.
- Yang-Zhang realized volatility → A.3.10.
- Z-scoring / sector-relative normalization → A.3.10 (`factors.py` does it once for all signals).
- Composite weighting → A.3.10 (`news_activity.py` per `config/scoring.yaml > news_activity_subweights`).
- GDELT GKG sentiment tone (V2.5 GKG has a tone column) → post-A.3.10. We pull *volume only* in v1; the GKG record's `Tone` / `TonePos` / `ToneNeg` columns are stored as opaque JSON in `raw_gdelt.gkg_tone_json` (Optional) so a future enhancement can parse them without schema change.
- pytrends per-ticker pulls — only the universe-wide FEARS basket. Per-ticker Google Trends queries (Da-Engelberg-Gao §IV's robustness checks) are deferred. The `raw_pytrends.term` column is the seam — future per-ticker pulls add rows with `term='ticker:AAPL'` and the same schema works unchanged.
- Backfilling historical GDELT (GDELT GKG goes back to 2015). v1 starts forward from the first run; backfill is a separate operation outside the orchestrator.
- ALFRED-style vintage data for any of the five tables. Same convention as A.3.5: `realtime_start` / `realtime_end` are not modelled in `raw_gdelt` / `raw_pytrends` because neither GDELT nor pytrends publishes revisions; first-observed wins.
- Live integration test against real GDELT + Google Trends endpoints → A.5 acceptance phase.
- Polygon / Tiingo analyst-estimates routes in `OpenBBSource` (free tiers do not expose this data with sufficient history; only FMP-stable is implemented).

---

## Open questions — resolved upfront

These were identified before plan drafting against the established principles ("AI is additive; quant floor is sovereign"; "Maximum coverage, accuracy, data integrity"; "Persistent data accumulation, once stored, immutable"). The executing agent should NOT re-debate these.

1. **Does a GDELT scaffold already exist?** No. `src/common/datasources/` contains 13 modules (`base.py`, `registry.py`, `resolution.py`, `sec_cik_lookup.py`, `edgar_*`, `fred_source.py`, `finra_source.py`, `finviz_source.py`, `yahoo_source.py`, `stockanalysis_*`, `openbb_source.py`) but no `gdelt_source.py`. The plan creates `GdeltSource` from scratch following the A.1 scaffold-then-extend pattern: ship with `health_check()` + the one fetch method together (no separate scaffold phase).
2. **Does a pytrends scaffold exist?** No. Same as #1 — created from scratch in this plan as `PytrendsSource`. Adds `pytrends>=4.9.2` to `requirements.txt`. No `__version__` introspection (matches A.3.3–A.3.7.5 pattern of avoiding `__version__` checks because not every dep exposes it consistently).
3. **What ticker-mapping does GDELT use?** GDELT GKG records use a `V2Tone`-keyed column and several entity columns (`V2Persons`, `V2Organizations`, `V2Locations`). Public companies appear in `V2Organizations` (free-text name). GDELT does NOT canonicalize to tickers natively. **Decision:** v1 uses cashtag-first (the `SocialEmbed` column captures `$AAPL`-style mentions, which avoids the name-disambiguation problem entirely) with company-name fallback via a small static alias table `config/gdelt_alias.yaml` (~50 rows, one per top-50 ticker by market cap). Aliases for the long tail are deferred until the news_volume_anomaly signal proves itself in A.3.10 backtesting. The alias YAML is created in Task 4 and is read once at `GdeltSource.__init__` so updates are hot via re-instantiation.
4. **pytrends scope — global vs US-only?** US-only (`geo='US'`). The trading universe is US-only (FINRA-regulated tickers via the A.3.2 Finviz / Yahoo filters), so US search volume is the relevant fear-of-recession signal. Global Trends data would dilute the signal with non-US noise. Da-Engelberg-Gao 2015 used US-only data; we match.
5. **Should the five methodology modules return raw or z-scored values?** Raw signed scalars per ticker (or per-(ticker, date) when applicable). Z-scoring is done once in `factors.py` at composite time (A.3.10), not five times across five modules. This keeps each module testable in isolation against deterministic numeric expectations and avoids the "what's the z-score sample" coupling that would otherwise leak DB shape into pure functions.

Two additional callouts the executing agent should know but does NOT need to re-decide:

- **Sign convention for the contrarian FEARS signal:** Da-Engelberg-Gao 2015 found that *high* fear-search predicts *bullish* near-term returns (retail panic → mean-reversion). To keep all five A.3.8 modules sign-aligned with the rest of `news_activity_score` (where positive == bullish), `fears_signal.py` returns `-log(SVI_7d) + log(SVI_365d_avg)` (inverted from the natural reading direction). The docstring states this explicitly.
- **Field routing — add one new key per signal, NOT a single bucket.** `news_volume` and `fears` are *distinct field names* in the orchestrator (different sources, different watermarks). The plan adds *two* new routing branches (one per field) rather than collapsing them into a single `news` bucket. Reason: the orchestrator's `_route_field_to_call` is already a long if/elif chain; one more branch each is clearer than introducing a new bucket abstraction that needs a separate routing table.

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| `src/common/schemas.py` | Add `RawGdeltMention` + `RawPytrendsObservation` Pydantic models; extend `_OpenBBFieldName` Literal with 3 analyst-estimate fields | Modify |
| `src/common/database.py` | Add `migrate_to_v11()` + `insert_raw_gdelt()` + `insert_raw_pytrends()` | Modify |
| `src/methodology/short_interest_delta.py` | NEW — Diether-Lee-Werner short-interest delta (reads from caller-supplied list of `raw_finra` rows) | Create |
| `src/methodology/revisions_velocity.py` | NEW — Chan-Jegadeesh-Lakonishok revision velocity (reads caller-supplied analyst-estimate rows from `raw_openbb`) | Create |
| `src/methodology/news_volume_anomaly.py` | NEW — Tetlock 2007 news-mention z-score (reads caller-supplied `raw_gdelt` rows) | Create |
| `src/methodology/filing_density.py` | NEW — Lee-So 2017 8-K density (reads caller-supplied `raw_edgar_filings` rows) | Create |
| `src/methodology/fears_signal.py` | NEW — Da-Engelberg-Gao FEARS contrarian (reads caller-supplied `raw_pytrends` rows; sign-flipped) | Create |
| `src/common/datasources/gdelt_source.py` | NEW — `GdeltSource` with `health_check()` + `fetch_news_volume()` | Create |
| `src/common/datasources/pytrends_source.py` | NEW — `PytrendsSource` with `health_check()` + `fetch_search_interest()` | Create |
| `src/common/datasources/openbb_source.py` | Add FMP `/stable/analyst-estimates` route + 3 new fields in `_map_fmp` | Modify |
| `src/common/datasources/registry.py` | Register GdeltSource + PytrendsSource in `DataSourceRegistry` | Modify |
| `src/common/orchestrator/fetch_orchestrator.py` | Add 2 routing branches in `_route_field_to_call` | Modify |
| `config/orchestrator.yaml` | Add `gdelt` + `pytrends` provider quotas, source-provider map entries, and weights | Modify |
| `config/gdelt_alias.yaml` | NEW — top-50 ticker → company-name alias table | Create |
| `requirements.txt` | Add `pytrends>=4.9.2` | Modify |
| `tests/fixtures/gdelt/sample_gkg.csv.gz` | NEW — synthetic GDELT GKG 15-min file (gzipped, ~30 rows) | Create |
| `tests/fixtures/gdelt/sample_masterfilelist.txt` | NEW — synthetic GDELT master-list pointing at the sample GKG | Create |
| `tests/fixtures/pytrends/sample_response.json` | NEW — synthetic pytrends `interest_over_time` JSON | Create |
| `tests/common/test_schemas_a3_8.py` | Tests for `RawGdeltMention` + `RawPytrendsObservation` + extended `_OpenBBFieldName` | Create |
| `tests/common/test_database_a3_8.py` | Tests for `migrate_to_v11` + the 2 insert helpers | Create |
| `tests/methodology/test_short_interest_delta.py` | Diether-Lee-Werner unit tests | Create |
| `tests/methodology/test_revisions_velocity.py` | Chan-Jegadeesh-Lakonishok unit tests | Create |
| `tests/methodology/test_news_volume_anomaly.py` | Tetlock unit tests | Create |
| `tests/methodology/test_filing_density.py` | Lee-So unit tests | Create |
| `tests/methodology/test_fears_signal.py` | Da-Engelberg-Gao unit tests (incl. sign-flip verification) | Create |
| `tests/datasources/test_gdelt_fetch.py` | `GdeltSource.fetch_news_volume` mocked HTTP | Create |
| `tests/datasources/test_pytrends_fetch.py` | `PytrendsSource.fetch_search_interest` mocked pytrends | Create |
| `tests/datasources/test_openbb_revisions.py` | OpenBBSource analyst-estimates route (FMP only) | Create |
| `tests/datasources/test_integration_a3_8.py` | End-to-end orchestration: schema v11 + 2 fetches + 5 module runs | Create |
| `tests/common/test_orchestrator_routing_a3_8.py` | Orchestrator `_route_field_to_call` for `news_volume` + `fears` | Create |
| the build plan | Mark A.3.8 shipped (schema v11) | Modify |

---

## Task 0: Pre-flight verification

**Files:** none modified.

- [ ] **Step 1: Verify schema is at v10 from A.3.7.5**

```
./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager(); print('current schema version:', m.get_schema_version())"
```

Expected: `current schema version: 10`.

- [ ] **Step 2: Verify A.3.7.5 baseline tests still pass**

```
./venv/Scripts/python.exe -m pytest -m "not integration" 2>&1 | tail -2
```

Expected: ~540 passed (will drift ±5 — A.3.7.5 added ~70 tests). Capture the exact baseline; later tasks compare against it.

- [ ] **Step 3: Verify A.3.4 CMP classifier is importable (it's the lead signal; we must not collide with its naming pattern)**

```
./venv/Scripts/python.exe -c "from src.methodology.opportunistic_insider import classify_transactions, OPPORTUNISTIC_CLASSIFIER_VERSION; print('CMP v', OPPORTUNISTIC_CLASSIFIER_VERSION)"
```

Expected: `CMP v 1.0`.

- [ ] **Step 4: Verify `pytrends` is NOT yet installed in the venv**

```
./venv/Scripts/python.exe -c "import pytrends; print(pytrends.__file__)"
```

Expected: `ModuleNotFoundError`. After Task 7 this becomes a successful import.

- [ ] **Step 5: Verify the GDELT and Pytrends modules are absent**

```
./venv/Scripts/python.exe -c "import importlib.util; print('gdelt:', importlib.util.find_spec('src.common.datasources.gdelt_source')); print('pytrends:', importlib.util.find_spec('src.common.datasources.pytrends_source'))"
```

Expected: both print `None`.

- [ ] **Step 6: Verify `_OpenBBFieldName` Literal currently has exactly 14 entries**

```
./venv/Scripts/python.exe -c "from typing import get_args; from src.common.schemas import _OpenBBFieldName; print(len(get_args(_OpenBBFieldName)))"
```

Expected: `14` (per A.3.7 schema). After Task 1 this becomes 17.

- [ ] **Step 7: Verify working tree is clean**

```
git status -s
```

Expected: empty (or only unrelated `.env` / notebooks).

No commit at this task.

---

## Wave 1 — Schema substrate + the five pure-function methodology modules

> One sub-agent. Tasks 1–6. Estimated ~25 minutes wall-clock.
>
> **Why grouped:** Wave 1 is pure-compute and schema work. Zero network, zero new dependencies, no fragile mocking. Sized to fit comfortably under the 40-tool / 1000-token budget cap that A.3.7.5 Wave 2 over-ran.

---

## Task 1: `RawGdeltMention` + `RawPytrendsObservation` Pydantic models + 3 new `_OpenBBFieldName` entries

**Files:**
- Modify: `src/common/schemas.py`
- Create: `tests/common/test_schemas_a3_8.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/common/test_schemas_a3_8.py` with tests covering:
- `RawGdeltMention` — minimal-valid, ticker uppercased, `gkg_tone_json` defaults None, blank ticker rejected, blank `gkg_record_id` rejected, `mention_timestamp` required, `match_method` Literal enforced.
- `RawPytrendsObservation` — minimal-valid, term lowercased, geo defaults `"US"`, blank term rejected, `svi >= 0` enforced, `svi > 100` accepted (pytrends can return scaled-up values when `gprop` is set).
- Extended `_OpenBBFieldName` includes `"eps_estimate_current"`, `"eps_estimate_30d_ago"`, `"eps_revision_direction_count"` (and the existing 14 are unchanged).

- [ ] **Step 2: Run test to verify it fails**

```
./venv/Scripts/python.exe -m pytest tests/common/test_schemas_a3_8.py -v
```

Expected: `ImportError` on the two new model names.

- [ ] **Step 3: Append models to `src/common/schemas.py`**

At the end of `schemas.py`, append:

```python
# === Phase A.3.8 — GDELT news mentions + pytrends FEARS substrate =====


class RawGdeltMention(BaseModel):
    """One GDELT GKG 15-minute record mapped to a ticker.

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 7.1 row 4. Stored in raw_gdelt. PK:
    (gkg_record_id, ticker).

    `gkg_record_id` is GDELT's stable record identifier (the first
    column of the GKG CSV, format e.g. '20260522001500-1234'). One
    GKG record may mention multiple tickers (each writes one row);
    one ticker may appear in many records over a 15-minute window.

    `mention_timestamp` is the GKG record's publication timestamp
    (ISO 8601 UTC). `match_method` records whether the ticker was
    discovered via cashtag ('cashtag') or organization-name alias
    ('alias'), so later analysis can weight the two channels.

    `gkg_tone_json` is reserved for the V2.5 GKG `V2Tone` columns
    (Tone / TonePos / TonePos1 / TonePos2 / ...). v1 leaves None;
    a post-A.3.10 enhancement parses tone without schema change.
    """

    run_id: str
    gkg_record_id: str
    ticker: str
    mention_timestamp: str  # ISO 8601 UTC
    match_method: Literal["cashtag", "alias"]
    source_url: Optional[str] = None
    gkg_tone_json: Optional[str] = None
    scrape_timestamp: str

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        # uppercases + _TICKER_RE validates — matches A.3.5 pattern
        ...

    @field_validator("gkg_record_id", mode="before")
    @classmethod
    def _require_gkg_id(cls, v: object) -> str:
        # rejects empty / whitespace-only
        ...


class RawPytrendsObservation(BaseModel):
    """One Google-Trends search-volume index observation for a FEARS term.

    Spec: section 7.1 row 7. Stored in raw_pytrends. PK:
    (term, observation_date, geo).

    `term` is lowercased before storage (FEARS terms in
    Da-Engelberg-Gao 2015 §III are case-insensitive). `svi` is the
    Google-normalized search-volume index 0..100 (Google scales each
    pull to a 0..100 range within the requested window; absolute
    counts are not available from the public Trends API).

    `geo` is the two-letter ISO country code. v1 default 'US' for
    the trading universe; future per-region pulls add rows with
    different `geo` values and the same schema works unchanged.
    """

    run_id: str
    term: str
    observation_date: str  # YYYY-MM-DD (weekly bucket per pytrends)
    svi: float = Field(ge=0)
    geo: str = "US"
    source_filename: str
    scrape_timestamp: str

    @field_validator("term", mode="before")
    @classmethod
    def _normalize_term(cls, v: object) -> str:
        # lowercases + rejects empty
        ...
```

Extend `_OpenBBFieldName` Literal in the existing definition near the `RawOpenBBRow` block. Add the three new entries in a new "Analyst revisions" group:

```python
_OpenBBFieldName = Literal[
    # ... existing 14 entries unchanged ...
    # Analyst revisions (A.3.8 — Chan-Jegadeesh-Lakonishok input)
    "eps_estimate_current",
    "eps_estimate_30d_ago",
    "eps_revision_direction_count",
]
```

- [ ] **Step 4: Run tests to verify pass**

Expected: all new tests PASS; previous Schema tests unchanged.

- [ ] **Step 5: Commit**

```
git add src/common/schemas.py tests/common/test_schemas_a3_8.py
git commit -m "feat(schemas): add RawGdeltMention + RawPytrendsObservation + 3 analyst-estimate field names for A.3.8"
```

---

## Task 2: `migrate_to_v11` + `insert_raw_gdelt` + `insert_raw_pytrends`

**Files:**
- Modify: `src/common/database.py`
- Create: `tests/common/test_database_a3_8.py`

- [ ] **Step 1: Write the failing tests**

Tests cover:
- `migrate_to_v11` creates both tables.
- Both tables have the expected columns.
- `schema_version` rows after migration are `[1..11]`.
- Migration is idempotent (3× call leaves `[1..11]`).
- `migrate_to_v11` preserves v10 data (insert a `provider_call_log` row before migrate, verify it's still there).
- `insert_raw_gdelt` persists a row; empty list is no-op; uses INSERT OR IGNORE on PK `(gkg_record_id, ticker)`.
- `insert_raw_pytrends` persists a row; empty list is no-op; uses INSERT OR IGNORE on PK `(term, observation_date, geo)`.

- [ ] **Step 2: Add `migrate_to_v11()` to `DatabaseManager`**

Immediately after `migrate_to_v10()`, insert:

```python
    def migrate_to_v11(self) -> None:
        """Idempotent migration v10 -> v11 per A.3.8.

        Adds:
          - raw_gdelt: one row per (gkg_record_id, ticker) GDELT
            news mention. Indexed on ticker + mention_timestamp for
            the trailing-30d / trailing-365d window queries that
            news_volume_anomaly.py performs at composite time.
          - raw_pytrends: one row per (term, observation_date, geo)
            FEARS search-volume observation. Indexed on term +
            observation_date.

        Both use INSERT OR IGNORE on insert. First write wins on
        re-fetches; revisions are silently dropped (matches A.3.5
        FRED convention).
        """
        self.migrate_to_v10()

        v11_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_gdelt (
              run_id TEXT NOT NULL,
              gkg_record_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              mention_timestamp TEXT NOT NULL,
              match_method TEXT NOT NULL CHECK (match_method IN ('cashtag','alias')),
              source_url TEXT,
              gkg_tone_json TEXT,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (gkg_record_id, ticker)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_gdelt_ticker ON raw_gdelt(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_gdelt_mention_ts ON raw_gdelt(mention_timestamp)",
            """
            CREATE TABLE IF NOT EXISTS raw_pytrends (
              run_id TEXT NOT NULL,
              term TEXT NOT NULL,
              observation_date TEXT NOT NULL,
              svi REAL NOT NULL CHECK (svi >= 0),
              geo TEXT NOT NULL DEFAULT 'US',
              source_filename TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (term, observation_date, geo)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_pytrends_term ON raw_pytrends(term)",
            "CREATE INDEX IF NOT EXISTS idx_raw_pytrends_date ON raw_pytrends(observation_date)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v11_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 11")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (11, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()
```

At the end of the class, append `insert_raw_gdelt` and `insert_raw_pytrends` following the A.3.5 `insert_raw_fred` / `insert_raw_finra` pattern exactly (`model_dump` + `executemany` + INSERT OR IGNORE).

- [ ] **Step 3: Run tests pass**

Expected: all new tests PASS; no regressions in `test_database_*` from earlier sub-phases.

- [ ] **Step 4: Commit**

```
git add src/common/database.py tests/common/test_database_a3_8.py
git commit -m "feat(database): migrate_to_v11 - raw_gdelt + raw_pytrends (INSERT OR IGNORE)"
```

---

## Task 3: `short_interest_delta.py` — Diether-Lee-Werner (2009)

**Files:**
- Create: `src/methodology/short_interest_delta.py`
- Create: `tests/methodology/test_short_interest_delta.py`

**Module contract:**

```python
SHORT_INTEREST_DELTA_VERSION = "1.0"


def compute_short_interest_delta(
    rows: list[dict],
    *,
    as_of_date: str,
    lookback_days: int = 30,
) -> dict[str, float]:
    """Compute the Diether-Lee-Werner 30-day short-interest delta per ticker.

    Parameters
    ----------
    rows : list[dict]
        Per-(ticker, settlement_date, exchange) records pulled from
        ``raw_finra`` by the caller. Each dict has at minimum:
          - 'ticker': str
          - 'settlement_date': str ('YYYY-MM-DD')
          - 'short_interest_shares': float
          - 'avg_daily_volume': Optional[float]
    as_of_date : str
        The focal date for the delta computation ('YYYY-MM-DD').
    lookback_days : int
        Window length (default 30). Must be positive.

    Returns
    -------
    dict[str, float]
        Per-ticker signed scalar. Positive = short-interest ratio
        RISING (bearish in the literature; sign-flipped at composite
        time in factors.py to align with the composite's positive
        == bullish convention. We return the raw rising-short-interest
        sign here — the methodology module is sign-neutral and the
        composite owns the inversion. See spec §7.2 signal 2 formula
        which explicitly negates the z-score in factors.py.)

    Returns NaN for any ticker that lacks both an "as_of_date" and a
    "lookback prior" observation in `rows`. Tickers with missing
    `avg_daily_volume` fall back to the short_interest_shares delta
    alone (no DTC multiplier).

    Pure function — no I/O, deterministic.
    """
```

**Acceptance tests (~10):**

- Returns 0.0 when as-of and lookback values are equal.
- Returns positive when short-interest rises by 10 %.
- Returns negative when it falls.
- Returns NaN when fewer than 2 observations per ticker.
- DTC multiplier applied when `avg_daily_volume` present.
- DTC multiplier skipped (falls back to raw delta) when ADV missing.
- Multiple tickers in one call computed independently.
- Bad date format raises `ValueError`.
- Negative `lookback_days` raises `ValueError`.
- `SHORT_INTEREST_DELTA_VERSION == "1.0"`.

---

## Task 4: `revisions_velocity.py` — Chan-Jegadeesh-Lakonishok (1996)

**Files:**
- Create: `src/methodology/revisions_velocity.py`
- Create: `tests/methodology/test_revisions_velocity.py`

**Module contract:**

```python
REVISIONS_VELOCITY_VERSION = "1.0"


def compute_revisions_velocity(
    rows: list[dict],
    *,
    as_of_date: str,
    lookback_days: int = 90,
    dispersion_penalty_lambda: float = 0.5,
) -> dict[str, float]:
    """Compute Chan-Jegadeesh-Lakonishok analyst-revision velocity per ticker.

    Spec §7.2 signal 3 formula:
        (Up - Down) / N  -  λ × σ(TP) / μ(TP)
    with λ default 0.5.

    Parameters
    ----------
    rows : list[dict]
        Long-format analyst-estimate rows from ``raw_openbb``,
        filtered to the three field_names this module consumes:
          - eps_estimate_current
          - eps_estimate_30d_ago
          - eps_revision_direction_count (caller-supplied integer
            encoded as signed net count: positive = net upward,
            negative = net downward).

        Each dict has at minimum:
          - 'ticker': str
          - 'field_name': str
          - 'value': Optional[float]
          - 'scrape_timestamp': str (ISO 8601 UTC)

    Pure function. Returns raw signed scalar (positive = net upward
    revisions; bullish). NaN if fewer than 3 analyst-estimate
    observations within the lookback window for that ticker.
    """
```

**Acceptance tests (~8):**

- Net-upward signal positive.
- Net-downward signal negative.
- Zero net → returns 0.0.
- Dispersion penalty reduces magnitude.
- Lambda=0 disables dispersion penalty.
- NaN when fewer than 3 observations.
- Multiple tickers.
- `REVISIONS_VELOCITY_VERSION == "1.0"`.

---

## Task 5: `news_volume_anomaly.py` — Tetlock (2007)

**Files:**
- Create: `src/methodology/news_volume_anomaly.py`
- Create: `tests/methodology/test_news_volume_anomaly.py`

**Module contract:**

```python
NEWS_VOLUME_ANOMALY_VERSION = "1.0"


def compute_news_volume_anomaly(
    rows: list[dict],
    *,
    as_of_date: str,
    recent_window_days: int = 30,
    baseline_window_days: int = 365,
) -> dict[str, float]:
    """Tetlock 2007 abnormal-news z-score per ticker.

    Spec §7.2 signal 4 formula:
        z = (n_30d/30 - μ_365d) / σ_365d
    where μ_365d, σ_365d are the per-ticker daily-mention count
    statistics over the trailing 365 days.

    rows: list of {'ticker', 'mention_timestamp'} from raw_gdelt.
    Mentions are bucketed by ticker × calendar-day (UTC).

    Returns NaN per ticker if baseline window has <30 days of data
    (insufficient sample for a stable mean/std).
    """
```

**Acceptance tests (~9):**

- Returns 0 when recent-window matches baseline.
- Positive when recent volume above baseline.
- Negative when below.
- NaN when fewer than 30 baseline observations.
- Empty input returns empty dict.
- Bucketing collapses multiple same-day mentions to one count.
- Multiple tickers tracked independently.
- Tickers absent from `rows` not in output.
- `NEWS_VOLUME_ANOMALY_VERSION == "1.0"`.

---

## Task 6: `filing_density.py` + `fears_signal.py` + analyst-estimates route added to `OpenBBSource`

> Two methodology modules + one source extension grouped because each is small and they all live in the same conceptual cluster (signals 5 and 7, plus the revisions-velocity input for signal 3).

**Files:**
- Create: `src/methodology/filing_density.py`
- Create: `src/methodology/fears_signal.py`
- Modify: `src/common/datasources/openbb_source.py`
- Create: `tests/methodology/test_filing_density.py`
- Create: `tests/methodology/test_fears_signal.py`
- Create: `tests/datasources/test_openbb_revisions.py`

**`filing_density.py` contract:**

```python
FILING_DENSITY_VERSION = "1.0"


def compute_filing_density(
    rows: list[dict],
    *,
    as_of_date: str,
    recent_window_days: int = 90,
    baseline_window_days: int = 365,
    excluded_8k_items: tuple[str, ...] = ("2.02", "7.01"),
) -> dict[str, float]:
    """Lee-So 2017 8-K filing density z-score per ticker.

    Per spec §7.2 signal 5: count of non-routine 8-Ks in trailing
    `recent_window_days`, z-scored against a per-ticker baseline of
    same-form counts over `baseline_window_days`.

    Items 2.02 (earnings) and 7.01 (Reg-FD pre-announcement) are
    *excluded* as routine. The caller supplies `raw_edgar_filings`
    rows with `form_type` and `item_numbers` fields; the function
    filters to form_type='8-K' AND no excluded item present.
    """
```

**`fears_signal.py` contract (sign-flipped for contrarian semantics):**

```python
FEARS_SIGNAL_VERSION = "1.0"


def compute_fears_signal(
    rows: list[dict],
    *,
    as_of_date: str,
    recent_window_days: int = 7,
    baseline_window_days: int = 365,
) -> float:
    """Da-Engelberg-Gao 2015 FEARS contrarian signal (universe-scalar).

    Spec §7.2 signal 7 formula:
        score = -( log(SVI_recent) - log(SVI_baseline) )
    Da-Engelberg-Gao 2015 found high fear-search PREDICTS POSITIVE
    near-term returns (retail panic mean-reverts). The minus sign is
    encoded HERE so the returned scalar follows the rest of the
    composite's positive == bullish convention; downstream code does
    NOT need to know about the inversion.

    rows: list of {'term', 'observation_date', 'svi'} from
    raw_pytrends. Aggregated across the default FEARS basket
    ('recession', 'bankruptcy', 'unemployment') by arithmetic mean
    SVI per date.

    Returns a SINGLE float (not a per-ticker dict). FEARS is a
    universe-level signal — A.3.10's news_activity.py applies the
    same scalar to every ticker in the universe (consistent with
    Da-Engelberg-Gao's universe-wide interpretation). Future
    enhancement: per-sector FEARS variants.

    Returns NaN if baseline window has <30 days of data.
    """
```

**`OpenBBSource` extension — add the FMP `/stable/analyst-estimates` route:**

In `openbb_source.py`, extend `_map_fmp(...)` to handle a new `endpoint='analyst-estimates'` call path. Add a new private method `_fetch_fmp_analyst_estimates(ticker)` that issues `GET /stable/analyst-estimates?symbol=<TICKER>&apikey=<KEY>` and emits three `RawOpenBBRow` rows per response:

- `field_name='eps_estimate_current'`, `value=<estimateEpsAvg>`, `provider_used='fmp'`
- `field_name='eps_estimate_30d_ago'`, `value=<estimateEpsAvg from previous month's response>`, `provider_used='fmp'`
- `field_name='eps_revision_direction_count'`, `value=<numAnalystsRevisionUp - numAnalystsRevisionDown>`, `provider_used='fmp'`

Polygon and Tiingo routes silently skip these three fields (their free tiers do not expose comparable data; the long-format table tolerates the absence per the existing OpenBB convention).

Wire the new method into `fetch_fundamentals_for_ticker` so the existing entry point also pulls revisions (cheap — same FMP rate-limit budget).

**Acceptance tests:**

`test_filing_density.py` (~7 tests):
- Items 2.02 and 7.01 excluded from counts.
- z-score positive when recent density above baseline.
- z-score negative when below.
- NaN when baseline has <30 days.
- Empty input returns empty dict.
- Multiple tickers tracked independently.
- `FILING_DENSITY_VERSION == "1.0"`.

`test_fears_signal.py` (~6 tests):
- Returns POSITIVE when recent SVI HIGHER than baseline (sign-flipped — high fear → bullish output).
- Returns negative when recent SVI lower than baseline.
- Returns NaN when baseline <30 days.
- Aggregates across the 3-term basket (mean of SVIs).
- `FEARS_SIGNAL_VERSION == "1.0"`.
- Empty `rows` returns NaN.

`test_openbb_revisions.py` (~5 tests):
- FMP analyst-estimates response parses to 3 rows per ticker.
- Missing FMP key → no rows emitted, watermark records skip.
- 429 from FMP → graceful failure, no rows, error logged.
- Polygon + Tiingo do NOT emit revision rows (verify via row-count assertion).
- Existing 14 OpenBB fields still produced (regression check on `fetch_fundamentals_for_ticker`).

- [ ] **Wave 1 final commit:**

```
git add src/methodology/*.py tests/methodology/*.py src/common/datasources/openbb_source.py tests/datasources/test_openbb_revisions.py
git commit -m "feat(methodology): A.3.8 sub-signals - short_interest_delta + revisions_velocity + news_volume_anomaly + filing_density + fears_signal (pure functions) + OpenBB analyst-estimates route"
```

---

## Wave 2 — `GdeltSource` + `PytrendsSource` + registry registration

> One sub-agent. Tasks 7–9. Estimated ~25 minutes wall-clock.
>
> **Why grouped:** Wave 2 is the two new source classes plus their registry registration. Each source is independently testable via mocked HTTP / mocked-`pytrends` and they share no state. The registry edit is a 3-line change.

---

## Task 7: `requirements.txt` + `PytrendsSource` (`fetch_search_interest`)

**Files:**
- Modify: `requirements.txt`
- Create: `src/common/datasources/pytrends_source.py`
- Create: `tests/fixtures/pytrends/sample_response.json`
- Create: `tests/datasources/test_pytrends_fetch.py`

- [ ] **Step 1: Add `pytrends>=4.9.2` to `requirements.txt`** and install:

```
./venv/Scripts/pip.exe install -r requirements.txt
./venv/Scripts/python.exe -c "import pytrends; from pytrends.request import TrendReq; print('pytrends import ok')"
```

Expected: prints `pytrends import ok`. (No `__version__` check — matches the A.3.3+ convention.)

- [ ] **Step 2: Create the fixture**

`tests/fixtures/pytrends/sample_response.json` — synthetic pytrends `interest_over_time` dataframe-as-JSON output with ~30 weekly observations of 3 terms (`recession`, `bankruptcy`, `unemployment`), `svi` 0..100.

- [ ] **Step 3: Implement `PytrendsSource`**

```python
class PytrendsSource(BaseDataSource):
    name = "pytrends"
    cadence = "weekly"
    provides = {"fears_basket"}

    # Da-Engelberg-Gao 2015 §III basket. Override at construction
    # for sensitivity testing post-A.3.10.
    DEFAULT_FEARS_BASKET = ("recession", "bankruptcy", "unemployment")

    # Throttle aggressively — Google Trends 429s readily. 1 call per
    # 12s = 5/min sliding window matches the orchestrator quota in
    # config/orchestrator.yaml.
    _INTER_TERM_DELAY_SEC = 12.0

    def health_check(self) -> SourceHealthStatus:
        ...

    def fetch_search_interest(
        self,
        run_id: str,
        db,
        *,
        terms: Optional[tuple[str, ...]] = None,
        geo: str = "US",
        timeframe: str = "today 12-m",
    ) -> int:
        """Pull Google Trends SVI for the FEARS basket and persist to raw_pytrends.

        Returns the number of rows inserted. Watermark
        `(pytrends, *, fears_basket)` advances to the max
        observation_date observed.

        Caller can pass `terms=(...)` to override the basket; the
        default is `DEFAULT_FEARS_BASKET`. `timeframe='today 12-m'`
        gives weekly granularity over the last year (matches the
        baseline_window_days=365 default in fears_signal.py).
        """
```

**Acceptance tests (~8):**

- Default basket pulls 3 terms.
- Custom basket pulls custom terms.
- Each pull writes a row per (term, observation_date, geo='US').
- Second call within 7d uses watermark short-circuit (returns 0).
- 429 from pytrends → graceful failure, error_count += 1, no rows.
- Empty pytrends response → 0 rows, no error.
- `time.sleep(_INTER_TERM_DELAY_SEC)` is called between terms (patch + assert call count).
- `health_check()` returns OK when pytrends import succeeds.

---

## Task 8: `GdeltSource` (`fetch_news_volume`)

**Files:**
- Create: `src/common/datasources/gdelt_source.py`
- Create: `config/gdelt_alias.yaml`
- Create: `tests/fixtures/gdelt/sample_gkg.csv.gz`
- Create: `tests/fixtures/gdelt/sample_masterfilelist.txt`
- Create: `tests/datasources/test_gdelt_fetch.py`

- [ ] **Step 1: Create the alias YAML** — top-50 ticker → company-name aliases. Format:

```yaml
# Static alias table for GDELT V2Organizations -> ticker mapping.
# v1 covers the top-50 by market cap. The long tail relies on cashtag matches.
aliases:
  AAPL: ["Apple Inc", "Apple Computer", "AAPL"]
  MSFT: ["Microsoft Corp", "Microsoft Corporation"]
  GOOGL: ["Alphabet Inc", "Google LLC", "Google Inc"]
  # ... 47 more ...
```

- [ ] **Step 2: Create fixtures**

`sample_masterfilelist.txt` — synthetic GDELT master-list with one entry pointing at `sample_gkg.csv.gz`.

`sample_gkg.csv.gz` — gzipped synthetic GKG file with ~30 rows. Columns per GDELT GKG V2.1 spec. Include 5 cashtag-matched rows (`$AAPL`, `$MSFT` in `SocialEmbed` column) and 5 alias-matched rows (e.g. "Apple Inc" in `V2Organizations`). The remaining 20 rows are noise (other companies, no ticker match).

- [ ] **Step 3: Implement `GdeltSource`**

```python
class GdeltSource(BaseDataSource):
    name = "gdelt"
    cadence = "hourly"
    provides = {"news_volume"}

    _MASTERFILELIST_URL = (
        "http://data.gdeltproject.org/gdeltv2/masterfilelist.txt"
    )
    # NOTE: GDELT production GKG files are *.gkg.csv.zip (zipfile, not gzip).
    # Tests use *.csv.gz for stdlib-gzip convenience. The source's _open_gkg()
    # helper accepts both extensions so the seam is visible.
    _INTER_FILE_DELAY_SEC = 0.25

    def __init__(self, alias_path: Path | str | None = None):
        self.alias_path = Path(alias_path or "config/gdelt_alias.yaml")
        self._alias_table = self._load_alias_table()

    def health_check(self) -> SourceHealthStatus:
        ...

    def fetch_news_volume(
        self,
        ticker: str,
        run_id: str,
        db,
        *,
        lookback_hours: int = 24,
    ) -> int:
        """Pull GDELT GKG 15-min files for the trailing `lookback_hours`
        and emit one `RawGdeltMention` row per (gkg_record_id, ticker).

        Ticker resolution:
          1. cashtag in SocialEmbed column ('$AAPL') -> match_method='cashtag'
          2. else company-name alias in V2Organizations -> 'alias'
        Both writes use INSERT OR IGNORE. The watermark
        (gdelt, ticker, news_volume) advances to the max
        mention_timestamp observed.
        """
```

**Note on GDELT file format:** GDELT V2 actually publishes `.csv.zip` not `.csv.gz`. The fixture uses `.csv.gz` for convenience in stdlib `gzip` mocking; the production code path also accepts `.csv.zip` via `zipfile.ZipFile`. Document the seam in the source docstring so the fixture and prod path don't drift.

**Acceptance tests (~9):**

- Cashtag-matched ticker writes row with `match_method='cashtag'`.
- Alias-matched ticker writes row with `match_method='alias'`.
- Unknown ticker (not in alias, no cashtag) emits zero rows.
- INSERT OR IGNORE on second pull (same gkg_record_id) → 0 new rows.
- Watermark advances after successful pull.
- Watermark `error_count` increments on HTTP failure.
- Empty master-list response → 0 rows, no exception.
- `_load_alias_table()` returns dict from YAML.
- `health_check()` returns OK when masterfilelist HEAD returns 200.

---

## Task 9: Register the two new sources in `DataSourceRegistry`

**Files:**
- Modify: `src/common/datasources/registry.py`
- Modify: `src/common/datasources/__init__.py` (re-exports)
- Create: `tests/common/test_registry_a3_8.py`

Add `GdeltSource()` and `PytrendsSource()` to the registry's source list following the existing pattern. Each respects the `enabled_sources` config (so they can be disabled in `config/datasources.yaml` if pytrends 429s become a nuisance in production).

**Acceptance tests (~3):**

- `DataSourceRegistry().enabled_sources()` includes `gdelt` and `pytrends` by default.
- Each source's `health_check()` is callable (no kwargs required).
- Disabling via config removes from `enabled_sources()`.

- [ ] **Wave 2 final commit:**

```
git add requirements.txt src/common/datasources/*.py tests/datasources/test_gdelt_fetch.py tests/datasources/test_pytrends_fetch.py tests/common/test_registry_a3_8.py config/gdelt_alias.yaml tests/fixtures/gdelt/ tests/fixtures/pytrends/
git commit -m "feat(datasources): GdeltSource + PytrendsSource + registry wiring for A.3.8"
```

---

## Wave 3 — Orchestrator routing + integration test + build-plan amendment

> One sub-agent. Tasks 10–12. Estimated ~15 minutes wall-clock.

---

## Task 10: Orchestrator routing (`news_volume` + `fears`) + `config/orchestrator.yaml` entries

**Files:**
- Modify: `src/common/orchestrator/fetch_orchestrator.py`
- Modify: `config/orchestrator.yaml`
- Create: `tests/common/test_orchestrator_routing_a3_8.py`

- [ ] **Step 1: Extend `_route_field_to_call`**

In `src/common/orchestrator/fetch_orchestrator.py`, add two branches BEFORE the final `return src_obj.fetch_universe(run_id)` fallback:

```python
    if field == "news_volume":
        return src_obj.fetch_news_volume(ticker, run_id)
    if field == "fears":
        return src_obj.fetch_search_interest(run_id)
```

- [ ] **Step 2: Extend `config/orchestrator.yaml`**

Add provider entries:

```yaml
providers:
  # ... existing 9 entries unchanged ...
  gdelt:
    quota_calls: 60          # polite — GDELT is unmetered but heavy
    quota_window_seconds: 60
  pytrends:
    quota_calls: 5           # Google Trends 429s aggressively
    quota_window_seconds: 60
```

Add source-provider mappings:

```yaml
source_providers:
  # ... existing 7 entries unchanged ...
  gdelt: [gdelt]
  pytrends: [pytrends]
```

Add weight entries:

```yaml
weights:
  # ... existing 7 source weight blocks unchanged ...
  gdelt:
    news_volume: 12          # Tetlock medium-confidence per spec §7.1
  pytrends:
    fears: 5                 # FEARS lowest weight (0.05) per spec §7.3
```

**Acceptance tests (~5):**

- `_route_field_to_call(src, 'news_volume', 'AAPL', 'r1')` invokes `fetch_news_volume`.
- `_route_field_to_call(src, 'fears', None, 'r1')` invokes `fetch_search_interest`.
- Other fields unchanged (regression — `fundamentals`, `filings`, `macro_series` still route correctly).
- `load_orchestrator_config()` parses the extended YAML without error.
- `RateLimitedFetchOrchestrator.tick()` dispatches a `news_volume` queue entry against a mocked GdeltSource registry entry.

---

## Task 11: End-to-end integration test

**Files:**
- Create: `tests/datasources/test_integration_a3_8.py`

Single test that wires everything together with mocked HTTP:

```python
def test_a3_8_end_to_end(tmp_path, mocker):
    """A.3.8 end-to-end: migrate to v11, populate raw_finra +
    raw_gdelt + raw_pytrends + raw_openbb (analyst estimates) +
    raw_edgar_filings, run all 5 methodology modules against the
    populated tables, verify each returns a non-NaN result for at
    least one ticker.
    """
    db = DatabaseManager(db_path=str(tmp_path / "test.db"))
    db.migrate_to_v11()

    # ... seed raw_finra with 60d of AAPL short-interest data ...
    # ... seed raw_gdelt with 90d of AAPL mentions (mocked GdeltSource fetch) ...
    # ... seed raw_pytrends with 90d of FEARS basket (mocked PytrendsSource fetch) ...
    # ... seed raw_edgar_filings with 90d of AAPL 8-K filings ...
    # ... seed raw_openbb with AAPL analyst-estimate rows ...

    # Run all 5 methodology modules
    si = compute_short_interest_delta(finra_rows, as_of_date="2026-05-22")
    rv = compute_revisions_velocity(openbb_rows, as_of_date="2026-05-22")
    nv = compute_news_volume_anomaly(gdelt_rows, as_of_date="2026-05-22")
    fd = compute_filing_density(filings_rows, as_of_date="2026-05-22")
    fe = compute_fears_signal(pytrends_rows, as_of_date="2026-05-22")

    assert not math.isnan(si["AAPL"])
    assert not math.isnan(rv["AAPL"])
    assert not math.isnan(nv["AAPL"])
    assert not math.isnan(fd["AAPL"])
    assert not math.isnan(fe)
```

This is the canary that confirms the five modules' contracts are compatible with the schema and source-emitted shapes — the only place in A.3.8 where all the pieces touch each other.

---

## Task 12: Build plan amendment

**Files:**
- Modify: the build plan

Update the A.3 row in §5.1.0 sub-phase table to mark A.3.8 shipped (schema v11). Append a short summary mirroring the A.3.7.5 entry style:

> A.3.8 shipped 2026-MM-DD: schema v11 + `RawGdeltMention` + `RawPytrendsObservation` + `migrate_to_v11` + two insert helpers; **5 pure-function methodology modules** (`short_interest_delta` / `revisions_velocity` / `news_volume_anomaly` / `filing_density` / `fears_signal` — each versioned `*_VERSION = "1.0"`, matches `opportunistic_insider.py` template); **GdeltSource** (`fetch_news_volume`, cashtag-first + alias fallback via `config/gdelt_alias.yaml`, INSERT OR IGNORE on PK `(gkg_record_id, ticker)`); **PytrendsSource** (`fetch_search_interest`, default FEARS basket per Da-Engelberg-Gao 2015 §III, US-only `geo='US'`, 12s inter-term throttle); **OpenBBSource analyst-estimates route** (FMP-stable only — Polygon + Tiingo silently skip); orchestrator routing for `news_volume` + `fears`; `pytrends>=4.9.2` added to `requirements.txt`.

- [ ] **Wave 3 final commit:**

```
git add src/common/orchestrator/fetch_orchestrator.py config/orchestrator.yaml tests/common/test_orchestrator_routing_a3_8.py tests/datasources/test_integration_a3_8.py <build-plan>
git commit -m "feat(orchestrator): A.3.8 routing + integration test + build plan amendment (schema v11 shipped)"
```

---

## Definition of Done

All of the following must be true before A.3.8 is considered shipped:

1. **Schema v11 stamped.** `DatabaseManager.get_schema_version()` returns 11 after `migrate_to_v11()`. `raw_gdelt` + `raw_pytrends` tables exist with the expected columns + PK constraints + indexes.
2. **Five methodology modules importable and versioned.** Each of `short_interest_delta`, `revisions_velocity`, `news_volume_anomaly`, `filing_density`, `fears_signal` exports its `*_VERSION = "1.0"` constant and its single public function.
3. **GdeltSource + PytrendsSource pass `health_check()`** (mocked at the HTTP / pytrends-library boundary; the live-endpoint version is an A.5 acceptance test).
4. **OpenBBSource emits 3 new `field_name` values** when FMP key is configured: `eps_estimate_current`, `eps_estimate_30d_ago`, `eps_revision_direction_count`. With no FMP key, OpenBBSource emits zero rows for these three fields and the watermark records a skip (not an error).
5. **Orchestrator routes `news_volume` and `fears` correctly.** `_route_field_to_call(...)` dispatches each new field to its source's new method.
6. **`config/orchestrator.yaml` extended** with 2 provider quotas (`gdelt: 60/60s`, `pytrends: 5/60s`), 2 source-provider mappings, and 2 weight entries (`gdelt.news_volume: 12`, `pytrends.fears: 5`).
7. **`config/gdelt_alias.yaml` exists** with the top-50 ticker alias table loaded by `GdeltSource.__init__`.
8. **`pytrends>=4.9.2` added to `requirements.txt`** and importable from venv.
9. **All new tests pass.** Approximate count: 12 schema + 11 database + 10 short_interest + 8 revisions + 9 news_volume + 7 filing_density + 6 fears + 5 openbb_revisions + 8 pytrends + 9 gdelt + 3 registry + 5 routing + 1 integration = **~94 new tests**. Baseline tests unchanged (no regressions).
10. **The build plan marks A.3.8 shipped** with the schema v11 summary.
11. **Out-of-scope NOT done:** No edits to `src/layer1_universe/factors.py`. No edits to `src/layer1_universe/news_activity.py` (file should not exist yet — it's A.3.10's deliverable). No z-scoring inside any methodology module. No composite weighting inside any methodology module. No LM tone work (A.3.9). No Yang-Zhang work (A.3.10).
12. **Working tree clean** after Wave 3 final commit. `git status -s` empty (or only unrelated notebooks).

---

## Spec coverage trace

| Spec wording | Tasks delivering it |
|---|---|
| §7.1 row 2 short-interest delta + DTC (Diether-Lee-Werner) | Task 3 |
| §7.1 row 3 analyst revision velocity (Chan-Jegadeesh-Lakonishok) | Tasks 4 + 6 (OpenBB route) |
| §7.1 row 4 news volume anomaly (Tetlock) | Task 5 + Task 8 (GDELT) |
| §7.1 row 5 8-K filing density (Lee-So) | Task 6 |
| §7.1 row 7 FEARS Google Trends (Da-Engelberg-Gao) | Task 6 + Task 7 (pytrends) |
| §15 row "A.3.8" — 5 sub-signal modules + GDELT + pytrends | Tasks 3–8 |
| §18 entries 7–11 academic-research citations | Module docstrings (Tasks 3–6) |
| Principle 5 no silent overwrite | Tasks 2, 7, 8 (INSERT OR IGNORE on both new tables) |
| Principle 6 watermarks bound deltas | Tasks 7, 8 (per-source watermarks) |

**Out of scope** (explicitly deferred — re-stated for the executing agent's spot-check):

- `news_activity.py` composite → A.3.10.
- `factors.py` integration → A.3.10.
- LM tone (signal 6) → A.3.9.
- Yang-Zhang vol → A.3.10.
- z-scoring / sector-relative normalization → A.3.10 owns it once across all 7 signals.
- GDELT V2.5 tone parsing → post-A.3.10 (schema reserves `gkg_tone_json`).
- Per-ticker pytrends pulls → post-A.3.10 (schema's `term` column allows it).
- Polygon / Tiingo analyst-estimates routes → post-A.3.10.
- Historical GDELT backfill → separate operation outside the orchestrator.
- Live integration tests against real GDELT + Google Trends → A.5.

---

## Execution handoff

**Recommended:** Subagent-driven, three waves to keep each sub-agent under the tool-call budget (the A.3.7.5 Wave 2 sub-agent over-ran the 50-tool cap; A.3.8 is intentionally split so each wave fits).

- **Wave 1 (Schema + 5 methodology modules):** Tasks 0–6. One sub-agent. Pure compute + schema. No new deps. **~25 min.**
- **Wave 2 (GdeltSource + PytrendsSource + registry):** Tasks 7–9. One sub-agent. Two source classes + alias YAML + `pytrends` install. **~25 min.**
- **Wave 3 (Orchestrator routing + integration + build plan):** Tasks 10–12. One sub-agent. Small surface; mostly stitching. **~15 min.**

**Total: 3 waves, ~65 min wall-clock if serialized; ~45 min if Wave 1 and Wave 2 are parallelized (they share no source files except a one-line touch to `tests/methodology/__init__.py`).**

**Critical pre-execution checks:**

1. After Task 2, confirm `schema_version == 11`:
   ```
   ./venv/Scripts/python.exe -c "from src.common.database import DatabaseManager; m=DatabaseManager('data/_v11_check.db'); m.migrate_to_v11(); print(m.get_schema_version())"
   ```
   Expect `11`.
2. After Task 7, confirm `pytrends` is importable from venv:
   ```
   ./venv/Scripts/python.exe -c "from pytrends.request import TrendReq; print('pytrends ok')"
   ```
3. After Task 8, confirm the GDELT alias YAML loads:
   ```
   ./venv/Scripts/python.exe -c "import yaml; print(len(yaml.safe_load(open('config/gdelt_alias.yaml'))['aliases']))"
   ```
   Expect approximately `50`.
4. Do NOT modify `src/layer1_universe/factors.py`. Do NOT create `src/layer1_universe/news_activity.py`. Both are A.3.10's deliverables; touching them in A.3.8 is out-of-scope creep.
5. Do NOT modify `src/methodology/opportunistic_insider.py` — the CMP classifier is shipped and is the lead signal; A.3.8's five modules sit alongside it, not on top of it.
6. The sign convention for `fears_signal.py` is sign-flipped INSIDE the module (high fear → bullish output). Document this explicitly in the docstring. The other four modules emit unsigned-by-mechanism scalars; `factors.py` at A.3.10 owns the sign-of-direction logic for those four (signal 2 explicitly negates per spec §7.2).

**Methodology callouts before execution:**

1. **Default pytrends throttle is 12s/term (5/min sliding).** This is conservative — pytrends users sometimes succeed at 2–3s/term but 429s are common at that rate. Decision: keep 12s default for v1.
2. **GDELT alias coverage is top-50 by market cap only.** Tickers outside the top-50 rely on cashtag matches alone. The downstream `news_volume_anomaly` signal will compute a lower-quality (more noisy) z-score for the long tail. Decision: ship A.3.8 with top-50 only.
3. **FEARS basket is the literature-canonical 3 terms (`recession`, `bankruptcy`, `unemployment`).** Da-Engelberg-Gao 2015 §III used these three plus an additional ~30 "economic-distress" terms. v1 ships the core 3; the full ~30 is a future enhancement once the signal proves itself in A.3.10 backtests.
