# Dataflow Overview — Where Things Live and How Data Moves

**Purpose:** A single page to navigate the codebase and trace data from source to trade. Updated as new sub-phases ship.

Last updated: 2026-05-21 (post A.3.3).

---

## 1. Directory tree (only the parts that matter)

```
<project-root>\
│
├── .env                          (API keys — gitignored)
├── .env.example                  (template showing every key slot)
├── .gitignore
├── requirements.txt              (Python dependencies)
├── pytest.ini                    (test runner config)
│
├── HOW_TO_USE.md                 (original notebook usage)
│
├── config/
│   ├── api_keys.yaml             (maps "fmp"/"polygon"/... → env var names)
│   ├── datasources.yaml          (source priorities, resolution rules, Finviz filter)
│   ├── filters.yaml              (screening thresholds — Layer 1 cutoffs)
│   ├── scoring.yaml              (factor composite weights)
│   └── lm_dictionary/            (Loughran-McDonald sentiment dictionary — A.3.9)
│
├── data/
│   └── fundamentals.db           (the local SQLite database — gitignored)
│
├── docs/
│   ├── DATAFLOW_OVERVIEW.md      ← YOU ARE HERE
│   ├── HOW_TO_RUN_TESTS.md       (the operator guide for the smoke notebook)
│   ├── methodology/
│   │   ├── methodology-handbook.md           (running plain-English explainer per phase)
│   │   ├── research_bibliography.md
│   │   ├── factor_models.md
│   │   ├── options_pricing.md
│   │   └── volatility_analysis.md
│   └── claude-code/
│       ├── specs/                (canonical design specs — read FIRST per phase)
│       │   ├── 2026-05-21-multi-source-data-adapter-design.md     (A.1)
│       │   └── 2026-05-21-phase-a3-layer1-hardening-design.md     (A.3)
│       └── plans/                (TDD step-by-step implementation plans)
│           ├── 2026-05-21-multi-source-data-adapter-phase-a1.md
│           ├── 2026-05-21-multi-source-data-adapter-phase-a2.md
│           ├── 2026-05-21-phase-a3-sub01-watermarks-foundation.md
│           ├── 2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md
│           └── 2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md
│
├── notebooks/
│   ├── layer1_control.ipynb      (original Layer-1 control panel — pre-v2)
│   └── smoke_tests.ipynb         (the primary operator surface — run this!)
│
├── src/
│   ├── common/
│   │   ├── database.py           (DatabaseManager — all SQLite ops, migrations)
│   │   ├── schemas.py            (Pydantic models for every table)
│   │   ├── env_loader.py         (reads .env + api_keys.yaml)
│   │   ├── parsing.py            (Finviz number parsing helpers from A.0)
│   │   └── datasources/
│   │       ├── base.py           (BaseDataSource ABC + get_fetch_gap/update_watermark)
│   │       ├── registry.py       (DataSourceRegistry — orchestrates all sources)
│   │       ├── resolution.py     (priority-weighted source blending)
│   │       ├── finviz_source.py  (Finviz universe scraper — $2B+ band, configurable)
│   │       ├── yahoo_source.py   (Yahoo fundamentals + OHLCV)
│   │       ├── edgar_source.py   (SEC EDGAR XBRL fundamentals)
│   │       ├── edgar_xbrl_parser.py  (pure function: companyfacts JSON → row)
│   │       ├── sec_cik_lookup.py     (ticker→CIK resolver with daily cache)
│   │       ├── fred_source.py    (FRED Treasury curve — A.3.5)
│   │       ├── finra_source.py   (FINRA short interest — A.3.5)
│   │       ├── stockanalysis_source.py  (10y ratio history — A.3.6)
│   │       ├── openbb_source.py  (multi-provider router — A.3.7)
│   │       └── _layer2_scaffold/  (Yahoo News + GDELT — future Layer 2)
│   │
│   ├── layer1_universe/
│   │   ├── screen.py             (Layer 1 screener — old version still works)
│   │   ├── factors.py            (factor scoring — gets refactored in A.3.10)
│   │   └── news_activity.py      (7-signal composite — A.3.10)
│   │
│   ├── methodology/              (factor / signal computation modules — A.3.8+)
│   │   ├── opportunistic_insider.py     (Cohen-Malloy-Pomorski — A.3.4)
│   │   ├── short_interest_signal.py     (A.3.8)
│   │   ├── revision_signal.py           (A.3.8)
│   │   ├── news_volume_signal.py        (A.3.8)
│   │   ├── filing_density_signal.py     (A.3.8)
│   │   ├── lm_tone_signal.py            (A.3.9)
│   │   ├── fears_signal.py              (A.3.8)
│   │   └── yang_zhang_vol.py            (A.3.10)
│   │
│   └── (later layers: agents/, expressions/, models/, thesis/, layer2-6/)
│
└── tests/
    ├── common/                   (database + schemas + env_loader tests)
    ├── datasources/              (per-source tests + integration tests)
    ├── methodology/              (per-signal tests — A.3.4+)
    └── fixtures/
        └── edgar/
            └── CIK0000320193.json  (Apple companyfacts synthetic fixture)
```

---

## 2. How data flows through a Layer 1 weekly run

Once all A.3 sub-phases ship, a single `run_layer1()` call does this:

```
WEEKLY:                                                          PERSIST TO:
┌─────────────────────────────────────────────────────────┐
│ 1. FinvizSource.fetch_universe(run_id)                   │ → finviz_universe_history
│    Pulls ~2,500 $2B+ US tickers (mid-cap and above)      │   (audit / raw)
└────────────────────┬─────────────────────────────────────┘
                     │ ticker list →
                     ▼
┌─────────────────────────────────────────────────────────┐
│ 2. For each ticker, in parallel where possible:          │ →
│    - YahooSource.fetch_fundamentals + historical_price   │   raw_yahoo
│    - EdgarSource.fetch_fundamentals (XBRL companyfacts)  │   raw_edgar_fundamentals
│    - EdgarSource.fetch_filings (Form 4, 8-K)             │   raw_edgar_insider [A.3.4]
│                                                           │   raw_edgar_filings [A.3.4]
│    - StockanalysisSource.fetch_ratio_history    [A.3.6]  │   raw_stockanalysis
│    - OpenBBSource.fetch_fundamentals (FMP/Polygon/Tiingo)│   raw_openbb       [A.3.7]
│                                                           │   historical_price
│    - FREDSource.fetch_macro_series                [A.3.5]│   raw_fred         [A.3.5]
│    - FINRASource.fetch_short_interest             [A.3.5]│   raw_finra        [A.3.5]
│                                                           │
│    Each source respects its watermark (Principle 6):     │   fetch_watermarks
│    - get_fetch_gap → "what dates do I still need?"        │   (last-fetched-date per
│    - fetch only the delta                                 │    source × ticker × field)
│    - update_watermark → "I've now got through 2026-05-21"│
│                                                           │
│    Every call logged with start/end/status/row-count:    │   source_run_log
└────────────────────┬─────────────────────────────────────┘
                     │ raw data accumulated in SQLite
                     ▼
┌─────────────────────────────────────────────────────────┐
│ 3. Canonical materialization                  [A.3.10]   │ → canonical_universe
│    For each ticker × field:                              │   (one row per ticker per run,
│    - Collect observations from all sources               │    with the "agreed" values)
│    - Apply priority-weighted blending (resolution.py)    │
│    - Detect disagreements; tag data_quality_score        │   field_provenance
│    - Write canonical row to canonical_universe           │   (audit: which source said
│    - Write per-field provenance for the audit trail      │    what for each value)
└────────────────────┬─────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ 4. Factor scoring (factors.py refactor)       [A.3.10]   │ → canonical_universe.factor_*
│    For each ticker, compute:                             │   columns
│    - value_score (P/E, EV/EBITDA, FCF yield, etc.)       │
│    - quality_score (margins, ROE, leverage, growth)      │
│    - momentum_score (3/6/12-month returns)               │
│    - lowvol_score (Yang-Zhang realized vol, beta)        │
│    - revisions_score (analyst EPS revision velocity)     │
│    - news_activity_score (7 sub-signals)      [A.3.8/9]  │
│    Then composite_score = weighted sum (scoring.yaml)    │
└────────────────────┬─────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ 5. Screening filters                                     │ → candidate_results
│    Apply Layer 1 cutoffs (filters.yaml):                 │   (one row per ticker per
│    - market_cap_min, liquidity_min, P/E ranges, etc.     │    playbook, eligible flag)
│    Survivors → ~30-50 candidates per playbook            │
└────────────────────┬─────────────────────────────────────┘
                     │
                     ▼
            (Hands off to Layer 2: thesis building — future)
```

---

## 3. Read the database in plain English

The SQLite file `data/fundamentals.db` is the single source of truth for accumulated data. Tables grouped by purpose:

### Audit & bookkeeping (added by A.1 + A.2 + A.3.1)
- **`schema_version`** — what migration version the DB is at (currently 5)
- **`runs`** — one row per Layer-1 run (run_id, timestamp, status)
- **`source_run_log`** — one row per (run_id, source) showing fetched/failed status
- **`fetch_watermarks`** — one row per (source, ticker, field) — Principle 6's bookkeeping
- **`field_provenance`** — per-field, per-source observations for canonical-resolution audit

### Source-native raw data (one table per source — accumulates over time)
- **`finviz_universe_history`** (also exposed as view `raw_finviz`) — Finviz screener snapshots
- **`raw_yahoo`** — Yahoo .info per ticker per run (fundamentals snapshots)
- **`raw_edgar_fundamentals`** — SEC XBRL fundamentals per ticker per fiscal period
- **`raw_edgar_insider`** [A.3.4] — Form 4 insider transactions
- **`raw_edgar_filings`** [A.3.4] — 8-K filing index
- **`raw_fred`** [A.3.5] — FRED macro series (Treasury yields, VIX, CPI)
- **`raw_finra`** [A.3.5] — FINRA biweekly short interest
- **`raw_stockanalysis`** [A.3.6] — 10-year ratio history scraped from stockanalysis.com
- **`raw_openbb`** [A.3.7] — multi-provider router (FMP/Polygon/Tiingo cross-validation)

### Time-series accumulators (Principle 6 — never re-fetched)
- **`historical_price`** — daily OHLCV per ticker
- **`historical_iv`** — daily implied vol per ticker (populated by Layer 3 later)
- **`historical_earnings_reactions`** — post-earnings move + IV crush per quarter

### Lookups & caches
- **`sec_ticker_cik_map`** — daily-cached SEC ticker→CIK map (avoids 10,000+ HTTP calls)
- **`posterior_cache`** — Bayesian inference cache (Layer 4 — future)
- **`agent_response_cache`** — AI agent output cache (Phase B — future)

### Canonical layer (the screener's input — populated in A.3.10)
- **`canonical_universe`** — one row per (run_id, ticker) with the blended "canonical" values across all sources, plus factor scores

### Layer 1 outputs (legacy from v0.1, kept for backward compat)
- **`universe_members`** — universe rows pre-canonical
- **`candidate_results`** — per-playbook screening output
- **`filter_waterfall`** — per-step filtering counts
- **`data_quality_metrics`** — per-run aggregate quality metrics

### Thesis & ranking outputs (future)
- **`thesis_objects`** — Layer 2 output (populated when Layer 2 builds)

---

## 4. The phase ladder — where we are

```
A.1   Source adapter framework (BaseDataSource, registry, resolution)  ✓ shipped
A.2   Storage layer (SQLite tables, Pydantic models, migrations v1→v2) ✓ shipped
A.3.1 Watermarks + .env loader + force_refetch (schema v3)              ✓ shipped 2026-05-21
A.3.2 FinvizSource + YahooSource real fetches + raw_yahoo (v4)          ✓ shipped 2026-05-21
A.3.3 EDGAR XBRL fundamentals + CIK lookup (v5)                         ✓ shipped 2026-05-21
A.3.4 EDGAR Form 4 + 8-K + opportunistic insider classifier             ← NEXT
A.3.5 FRED + FINRA
A.3.6 stockanalysis.com 10y ratio history
A.3.7 OpenBB router (FMP-stable / Polygon / Tiingo)
A.3.7.5 Rate-Limited Fetch Orchestrator
A.3.8 5 sub-signals + GDELT news + pytrends FEARS
A.3.9 Loughran-McDonald 10-K/10-Q tone
A.3.10 Yang-Zhang vol + factors.py refactor + dual-write + equivalence
A.4   Notebook extension (operator UI improvements)
A.5   Deliberately-corrupted-value acceptance test
B.x   Layer 2 — catalyst calendar + technical setups
C.x   Layer 3 — factor forecast (ML + ensemble + regime)
G     Interface (notebook control panel)
```

---

## 5. What to read first when you sit down

If you have **5 minutes**: this doc + scroll [methodology-handbook.md](methodology/methodology-handbook.md)'s most-recent entry.

If you have **a coffee break**: run [notebooks/smoke_tests.ipynb](../notebooks/smoke_tests.ipynb) → Cell → Run All. You'll see the full data layer exercised against real upstream services in ~40 seconds, with output explaining each step.

If you want to **trace what a single ticker does**, run smoke tests and look at section 7 (Database inspection) — you'll see `fetch_watermarks` showing what's been pulled, `historical_price` row counts per ticker, and `raw_yahoo` / `raw_edgar_fundamentals` for any ticker you've pulled.

If something **breaks**, [docs/HOW_TO_RUN_TESTS.md](HOW_TO_RUN_TESTS.md) lists the common failures and what to do about each.
