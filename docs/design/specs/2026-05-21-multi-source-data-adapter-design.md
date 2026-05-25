# Multi-Source Data Adapter — Design Spec

**Owner:** Vaibhav Arora
**Status:** Draft v1.0
**Date:** 2026-05-21
**Scope:** Layer 1 (Universe & Fundamental Screen)
**Slots into:** the build plan §5 (Phase A), reorganized into sub-phases A.1–A.5
**Related:**
- [methodology/factor_models.md](../../methodology/factor_models.md)

---

## 1. Purpose

Replace the current single-source (Finviz) Layer 1 pipeline with a multi-source data adapter framework that:

1. Pulls fundamentals from multiple free sources in parallel and cross-validates them.
2. Resolves source conflicts using a configurable **priority-weighted average** policy (no penalties; no ticker-dropping based on disagreement).
3. Stores full provenance for every field so disagreements are auditable but invisible to downstream code unless asked for.
4. Surfaces source health, agreement, and per-ticker provenance directly in [notebooks/layer1_control.ipynb](../../../notebooks/layer1_control.ipynb) — the operator notebook.
5. Scaffolds Layer 2 data sources (news, sentiment, calendar) so adding them later requires zero refactoring.

This design adheres strictly to the existing layer architecture. No new layers are introduced. The work is folded into existing Phase A as ordered sub-phases A.1–A.5.

## 2. Non-goals

- No Layer 2 implementation (dossier system, news, sentiment, insider, calendar) — only interface scaffolding.
- No paid/premium data sources (Polygon, FMP, OptionMetrics) — adapter pattern accepts them, but not in this phase.
- No real-time / streaming — Layer 5 concern.
- No caching beyond append-only SQLite history that already exists.
- No multi-machine distributed pulls — single-process, async/parallel within one machine.
- No change to `run_layer1()` public signature — the notebook keeps working with no edits to existing cells.

## 3. Design principles applied

From the build plan §3 and the layer architecture §1:

| Principle | How this design honors it |
|---|---|
| Data integrity above all | Bad data fails loudly per source; the run continues with remaining sources, recording the failure |
| Modular layers | New `datasources/` module isolated; no Layer 2/3/4 code touched |
| Reproducibility | Every canonical value traces back to N source observations stamped with `run_id` |
| Plain-language outputs | Notebook drilldown shows raw values per source in a readable table |
| Local-first | All sources are free; all storage is local SQLite |
| Version everything | `config/datasources.yaml` versioned in git; resolution rules captured per run |
| File-passed contracts | Layer 1 output files (parquet + CSV + manifest.json) unchanged in shape |

## 4. Source set (Phase A.1)

### Active (wired into Layer 1)

| Source | Module | Provides | Cadence |
|---|---|---|---|
| Finviz | `finviz_source.py` | Universe snapshot, current fundamentals, technicals | Weekly |
| Yahoo Finance | `yahoo_source.py` | Full financial statements, 5y+ history, identifiers | Weekly |
| SEC EDGAR | `edgar_source.py` | Raw filings (10-K/10-Q), Form 4 insider trades, CIK | Weekly |
| FRED (St. Louis Fed) | `fred_source.py` | Treasury curve, macro indicators (risk-free rate) | Weekly |
| stockanalysis.com | `stockanalysis_source.py` | 10y ratio history (powers valuation percentile scoring) | Weekly |
| FINRA | `finra_source.py` | Short interest (bi-monthly publication cadence) | Bi-monthly |

### Scaffolded but not registered (Layer 2 preparation)

These classes are written to validate the `BaseDataSource` interface against non-fundamental data shapes, but are NOT registered in `config/datasources.yaml`'s active source list. They become active in a future Layer 2 design.

- `_layer2_scaffold/yahoo_news_source.py` — Yahoo Finance news RSS
- `_layer2_scaffold/gdelt_source.py` — GDELT global events database

Each scaffold class implements only its method signature with a `NotImplementedError("Activated in Layer 2 — see <future doc>")` body. The point is to prove the interface accommodates news/event data shapes before Layer 1 ships, so Layer 2 doesn't force a redesign.

## 5. Module layout

```
src/
├── common/
│   ├── database.py            ← extended: new tables, no breaking changes
│   ├── schemas.py             ← extended: new Pydantic models
│   ├── parsing.py             ← unchanged
│   └── datasources/           ← NEW MODULE
│       ├── __init__.py
│       ├── base.py            ← BaseDataSource (abstract)
│       ├── registry.py        ← DataSourceRegistry + orchestration
│       ├── resolution.py      ← weighted-average / freshest / highest-priority logic
│       ├── finviz_source.py
│       ├── yahoo_source.py
│       ├── edgar_source.py
│       ├── fred_source.py
│       ├── stockanalysis_source.py
│       ├── finra_source.py
│       └── _layer2_scaffold/
│           ├── __init__.py
│           ├── yahoo_news_source.py
│           └── gdelt_source.py
└── layer1_universe/
    ├── screen.py              ← refactored: reads canonical_universe
    └── factors.py             ← refactored: reads canonical field names
```

## 6. BaseDataSource contract

```python
class BaseDataSource(ABC):
    name: str                # short identifier, e.g. "finviz"
    cadence: str             # "weekly" | "biweekly" | "monthly"
    provides: set[str]       # canonical field names this source can populate

    @abstractmethod
    def health_check(self) -> SourceHealthStatus: ...

    # Optional methods - implement only what the source provides.
    def fetch_universe(self, run_id: str) -> pd.DataFrame: ...
    def fetch_fundamentals_for_ticker(self, ticker: str, run_id: str) -> dict: ...
    def fetch_filings_for_ticker(self, ticker: str, run_id: str) -> list[Filing]: ...
    def fetch_macro_series(self, series_ids: list[str], run_id: str) -> pd.DataFrame: ...
```

Each source declares what it `provides` via the canonical-field set. The registry uses this declaration to know which sources to consult for which fields.

A source that cannot satisfy a method raises `NotImplementedError` cleanly; the registry catches it and treats that source as silent for that field.

## 7. DataSourceRegistry orchestration

`registry.fetch_all(run_id)`:

1. Loads `config/datasources.yaml`.
2. Iterates the **enabled** sources (subject to per-run override from notebook `ENABLED_SOURCES`).
3. For each, calls the method appropriate to its declared `provides` set.
4. Each source runs in its own thread/async task. Failures are isolated to that source.
5. Writes results to that source's `raw_<source>` table (append-only, stamped with `run_id` and `scrape_timestamp`).
6. Writes a row to `source_run_log` recording start/end time, status, row count, error if any.
7. After all sources finish (or fail), calls `resolution.materialize_canonical(run_id)` (§9).

`registry.fetch_canonical_universe(run_id)` returns the wide DataFrame for `screen.py` and `factors.py` — they never touch raw tables directly.

## 8. Storage schema (additive to existing SQLite)

### Preserved as-is

The existing `finviz_universe_history` table is **renamed in code only** to `raw_finviz` via a SQL view alias. Physical data and existing notebook outputs are unaffected. Historical scrape rows remain queryable as `raw_finviz` going forward.

### New tables

**`raw_<source>` family** — one per source, wide, source-native columns:
- `raw_finviz` (alias of existing table)
- `raw_yahoo(run_id, ticker, market_cap, pe_ttm, pe_forward, ebit_ttm, fcf_ttm, total_debt, cash, book_value, …, scrape_timestamp)`
- `raw_edgar(run_id, ticker, cik, latest_10q_filed, revenue_ttm, op_income_ttm, net_income_ttm, total_assets, …, scrape_timestamp)`
- `raw_stockanalysis(run_id, ticker, pe_history_json, pe_5y_avg, pe_5y_min, pe_5y_max, …, scrape_timestamp)`
- `raw_fred(run_id, series_id, observation_date, value, scrape_timestamp)` — long-format (time series)
- `raw_finra(run_id, ticker, short_interest, short_interest_pct_float, settlement_date, scrape_timestamp)`

All `raw_<source>` tables are append-only and indexed on `(run_id, ticker)` (or `(run_id, series_id)` for FRED).

**`canonical_universe`** — the screener-facing wide table, one row per ticker per run:

```
run_id, ticker, company_name, sector, industry, exchange, cik,
market_cap_usd, price, avg_daily_volume,
pe_ttm, pe_forward, ebit_ttm, fcf_ttm,
operating_margin, net_profit_margin, roe, roic,
total_debt_to_equity, interest_coverage,
revenue_growth_yoy, eps_growth_yoy,
perf_1m, perf_3m, perf_6m, perf_12m,
rsi_14, dist_52w_high, dist_52w_low, dist_200dma,
short_interest_pct_float,
pe_5y_percentile, ev_ebitda_5y_percentile,
data_quality_score,
materialized_at
```

`data_quality_score` is computed as `1 − (weighted_std / weighted_mean)` for the weighted-average fields, averaged across critical fields. Range 0–1. Available for factor scoring to consume as a "soft" quality signal without dropping tickers.

**`field_provenance`** — the audit trail, one row per `(run_id, ticker, field, source)`:

```
run_id, ticker, field, source, raw_value, parsed_value,
weight, contributed_to_canonical (bool),
disagreement_pct, fetched_at
```

The notebook drilldown reads from this table.

**`source_run_log`** — per-source per-run health, one row per `(run_id, source)`:

```
run_id, source, started_at, finished_at,
status ('ok'|'partial'|'failed'),
rows_fetched, error_message, error_traceback
```

## 9. Resolution policy

Configured in `config/datasources.yaml`:

```yaml
source_priority:
  fundamentals:   [edgar, stockanalysis, yahoo, finviz]
  prices:         [yahoo, finviz]
  identifiers:    [edgar, yahoo, finviz]
  short_interest: [finra, finviz]
  macro:          [fred]

priority_weights:
  decay: 0.5
  # Geometric decay by rank: rank 1 weight = 0.5, rank 2 = 0.25, rank 3 = 0.125 …
  # Normalized across sources that actually have a value for that field on that ticker.

field_resolution:
  weighted_average:
    - pe_ttm
    - pe_forward
    - ebit_ttm
    - fcf_ttm
    - operating_margin
    - net_profit_margin
    - roe
    - roic
    - total_debt_to_equity
    - interest_coverage
    - revenue_growth_yoy
    - eps_growth_yoy
  highest_priority:
    - sector
    - industry
    - company_name
    - cik
    - exchange
    - short_interest_pct_float    # FINRA is authoritative; fallback to finviz only
  freshest:
    - price
    - market_cap_usd
    - avg_daily_volume
    - rsi_14
    - dist_52w_high
    - dist_52w_low
    - dist_200dma
    - perf_1m
    - perf_3m
    - perf_6m
    - perf_12m
  derived:
    # NOT resolved across sources; computed by canonical materialization
    # downstream of source resolution. Listed for clarity only.
    - pe_5y_percentile         # computed from raw_stockanalysis history
    - ev_ebitda_5y_percentile  # computed from raw_stockanalysis history
    - data_quality_score       # computed from inter-source agreement

disagreement_threshold_log_only: 0.10
# Disagreements above 10% are written to field_provenance with a flag,
# but do NOT drop the ticker and do NOT penalize the score.
# This is metadata for the notebook drilldown only.
```

### Resolution rules formalized

For a given ticker × field with observations from sources `s_1, s_2, …, s_n`:

- **`weighted_average`** (numeric fundamentals):
  - Get the priority rank of each contributing source for this field's category.
  - Assign raw weight `w_i = decay^(rank_i - 1)`.
  - Normalize: `w_i' = w_i / sum(w_j)` over sources that actually have a value.
  - `canonical_value = sum(w_i' × value_i)`.
- **`highest_priority`** (categorical / identifiers): take the value from the highest-priority source that has a non-null value.
- **`freshest`** (snapshot fields): take the value from the source with the most recent `fetched_at`, breaking ties by priority.

All raw observations from all sources are written to `field_provenance` regardless of whether they contributed to the canonical value (so audit is complete even for sources that lost a freshest-wins tiebreak).

## 10. Notebook integration

[notebooks/layer1_control.ipynb](../../../notebooks/layer1_control.ipynb) is extended with new sections. **Existing cells stay where they are**; existing notebook flow is preserved.

### New / extended sections

```
1. Setup                                  [unchanged]
2. Control Parameters                     [extended]
   + ENABLED_SOURCES = ['finviz', 'yahoo', 'edgar', 'fred', 'stockanalysis', 'finra']
   + FORCE_REFRESH_SOURCES = []   # which sources to re-pull this run
3. Run                                    [unchanged signature]
4. [NEW] Source Status
   Table: source, last_refresh, rows_fetched, status, error_msg
5. [NEW] Provenance Summary
   Table: field, contributors, avg_agreement_pct, weighted_quality_score
6. Filter Waterfall                       [unchanged]
7. Data Coverage                          [unchanged]
8. Long Candidates                        [unchanged]
9. Short Candidates                       [unchanged]
10. [NEW] Per-Ticker Drilldown
    DRILLDOWN_TICKER = "AAPL"
    Table: field × source matrix with raw values, canonical highlighted,
           disagreement % per row
11. Export Watchlists                     [unchanged]
```

### Per-ticker drilldown example output

| field | finviz | yahoo | edgar | stockanalysis | canonical | disagreement |
|---|---|---|---|---|---|---|
| pe_ttm | 24.5 | 25.1 | 24.8 | 24.6 | **24.78** | 2.4% |
| operating_margin | 0.298 | 0.305 | 0.301 | — | **0.301** | 2.3% |
| sector | Tech | Technology | — | Technology | **Technology** | n/a (categorical) |
| price | 154.20 | 154.18 | — | — | **154.20** | freshest @ finviz |

## 11. Phase A sub-phase amendment

Phase A in the build plan §5 will be amended to split into ordered sub-phases. The build plan file is updated as part of this work.

| Sub-phase | Deliverable | Acceptance |
|---|---|---|
| **A.1** | `src/common/datasources/` module with `BaseDataSource`, `DataSourceRegistry`, `resolution.py`, six active source classes, two scaffold stubs, `config/datasources.yaml` | Each source's `health_check()` passes; scaffold stubs raise `NotImplementedError` cleanly |
| **A.2** | SQLite migration (additive): `raw_<source>` family, `canonical_universe`, `field_provenance`, `source_run_log`; Pydantic models in `schemas.py` | Existing data preserved; migration is idempotent; all new tables documented in the build plan §4.2 |
| **A.3** | Refactor `screen.py` + `factors.py` to consume `canonical_universe` | `run_layer1()` signature unchanged; existing notebook produces equivalent outputs against fixture data |
| **A.4** | Notebook extension: new sections, `ENABLED_SOURCES`, `FORCE_REFRESH_SOURCES`, per-ticker drilldown | Notebook runs end-to-end; new sections render correctly with fixture data |
| **A.5** | Tests: per-source unit tests, integration test, resolution-rule edge-case tests | Pytest passes; coverage targets per the build plan §11; **acceptance: a deliberately corrupted source value (e.g., 5× the true P/E) shows up as high disagreement in provenance and is dampened by the weighted average when 3+ sources agree** |

Each sub-phase is acceptance-tested before the next begins, per the existing build plan handoff protocol (§13).

## 12. Testing strategy

### Unit tests

- `tests/datasources/test_base.py` — interface contract tests
- `tests/datasources/test_<source>.py` — per source, against fixture JSON/HTML/CSV. **No network calls in test suite.**
- `tests/datasources/test_resolution.py`:
  - Weighted average with all sources present
  - Weighted average with some sources missing (renormalization)
  - Highest-priority with top-priority source null
  - Freshest with tied timestamps
  - All sources null for a field
  - Single-source-only case
  - Outlier dampening: 4 sources at ~25 P/E, 1 source at 150 P/E → canonical near 25

### Integration test

- `tests/integration/test_layer1_run.py`:
  - Loads fixture data for all six sources
  - Runs `registry.fetch_all(run_id)` end-to-end
  - Verifies: `canonical_universe` populated, `field_provenance` complete, `source_run_log` records all sources
  - Verifies: `run_layer1()` produces parquet + CSV outputs of the expected shape

### Fixture data

Stored in `tests/fixtures/datasources/<source>/`. Each source has a small snapshot of representative response data (HTML pages from Finviz, JSON from yfinance, etc.) checked into git.

## 13. Error handling & failure modes

| Failure mode | Behavior |
|---|---|
| One source fails entirely (network, API change) | Run continues with remaining sources; `source_run_log` records the failure; canonical resolution re-normalizes weights over surviving sources |
| One source returns a single bad row (corrupted value) | Value is logged in provenance with high disagreement %; weighted average dampens it if 2+ other sources agree |
| All sources fail for a critical field on a ticker | Field is null in canonical; `data_quality_score` for that ticker drops; ticker is NOT auto-dropped (filter rules in `filters.yaml` decide that downstream) |
| Schema drift in a source (new columns, removed columns) | Source adapter's parser raises a clear error; the source is marked `partial` or `failed` in `source_run_log`; an explicit assertion test catches this in CI before it hits production |
| Rate-limit on a source | Adapter implements per-source rate-limit-aware retry with backoff; ultimate failure is recorded, not silently retried forever |

## 14. Configuration files

After this work:

| File | Owns |
|---|---|
| `config/filters.yaml` | Screener thresholds (existing) |
| `config/scoring.yaml` | Factor composite weights (existing) |
| `config/datasources.yaml` | **NEW** — source priority, weight decay, field-type resolution rules |

Three separate concerns, three separate files. Tuning data resolution doesn't touch screening thresholds and vice versa.

## 15. Decisions confirmed in brainstorm

- **Refresh cadence enforcement**: registry auto-skips sources pulled within their cadence window; notebook `FORCE_REFRESH_SOURCES` overrides per run.
- **Canonical field set**: based on [methodology/factor_models.md](../../methodology/factor_models.md) and §5 of the original strategy doc. If `factors.py` requires fields not listed in §8, sub-phase A.3 adds them with a documented amendment in the commit message and a build-plan footnote.
- **Layer 2 source scaffold scope**: only two stubs (`yahoo_news_source.py`, `gdelt_source.py`) in this phase, purely to validate the interface against news/event data shapes. Other Layer 2 sources are added when the Layer 2 brainstorm happens.

## 16. Version control

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-05-21 | Initial draft from brainstorm |
