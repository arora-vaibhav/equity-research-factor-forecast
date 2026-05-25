# Phase A.3 — Layer 1 Full Hardening Design Spec

**Owner:** Vaibhav Arora
**Status:** Draft v1.0
**Date:** 2026-05-21
**Scope:** Make Layer 1 production-ready. Wire full multi-source data fetching, persistent accumulation, factor-scoring refactor to canonical_universe, the 7-signal news_activity_score (with Cohen-Malloy-Pomorski opportunistic-insider classifier as lead), Yang-Zhang realized volatility, OpenBB multi-provider router, and dual-write equivalence harness.
**Slots into:** the layer architecture, Phase A.3 (now expanded to absorb A.3.1 methodology upgrades).
**Related:**
- [docs/claude-code/specs/2026-05-21-multi-source-data-adapter-design.md](2026-05-21-multi-source-data-adapter-design.md) — A.1 sub-spec (shipped)
- [docs/methodology/methodology-handbook.md](../../methodology/methodology-handbook.md) — running explainer

---

## 1. Purpose

Take the Phase A.1 framework (6 source classes with `health_check()` only) and Phase A.2 storage (10 new tables, schema v2) and **make Layer 1 do real work end-to-end against live data**. After A.3, the notebook runs the multi-source pipeline against real data into `canonical_universe`, factor scores compute including the new `news_activity_score`, and the legacy v1 outputs continue to be produced in parallel (dual-write) until A.5 equivalence-verifies them.

This is the most consequential single phase in the v2 build because it's where the architecture stops being conceptual and starts producing trade-decision-grade output.

## 2. Non-goals

- **Not Layer 2 / 3 / 4 / 5 / 6 work.** Thesis-building, options pricing, Bayesian ranking, execution, backtesting — all separate phases.
- **Not the notebook extension.** A.4.
- **Not the deliberately-corrupted-value acceptance test.** A.5.
- **Not v1 table removal.** Dual-write keeps both representations alive through A.5.
- **Not the AI agent desk.** Phase B.1 onward.
- **Not Bayesian inference.** Phase D.1.
- **Not interface (Streamlit/Marimo).** Phase G, parked.

## 3. Design principles applied

Reaffirms the layer architecture §2 principles 1-6. Applied to A.3:

| Principle | How A.3 honors it |
|---|---|
| 1 — AI additive, quant sovereign | News tone via Loughran-McDonald (static dictionary, no API) is the quant fallback for Layer 2's AI news_synthesizer. Yang-Zhang vol replaces close-to-close everywhere. |
| 2 — Selectivity over coverage | Layer 1 still rejects 95%+; news_activity_score adds a peer-to-fundamentals signal but doesn't expand the universe. |
| 3 — Liquidity provision edge | Unaffected at Layer 1; A.3 prepares inputs Layer 3 will use. |
| 4 — Reproducible/explainable | Every news_activity_score has a structured sub-signal breakdown stored in `field_provenance`. |
| 5 — Data integrity above convenience | Dual-write lets us verify v1↔v2 equivalence. Bad source data logged in `source_run_log`. |
| 6 — Persistent accumulation | `fetch_watermarks` table tracks per-(source, ticker, field) last-fetched date; every fetch pulls only deltas. |

## 4. Module layout — what A.3 adds

```
src/
├── common/
│   ├── database.py                  ← extended: fetch_watermarks helpers, force_refetch, dual-write hooks
│   ├── schemas.py                   ← extended: FetchWatermark + news sub-signal models
│   ├── env_loader.py                ← NEW: .env file reader for API keys
│   └── datasources/
│       ├── base.py                  ← extended: get_fetch_gap, update_watermark
│       ├── registry.py              ← extended: full fetch orchestration, raw_<source> writers
│       ├── finviz_source.py         ← extended: real fetch_universe
│       ├── yahoo_source.py          ← extended: real fetch_universe + fetch_fundamentals + fetch_historical_price
│       ├── edgar_source.py          ← extended: 10-K/10-Q XBRL parser + Form 4 + 8-K
│       ├── fred_source.py           ← extended: full fetch_macro_series
│       ├── finra_source.py          ← extended: biweekly short-interest pull
│       ├── stockanalysis_source.py  ← extended: 10y ratio history scraper
│       └── openbb_source.py         ← NEW: OpenBB multi-provider router wrapper
├── layer1_universe/
│   ├── screen.py                    ← refactored: dual-write v1+v2
│   ├── factors.py                   ← refactored: consumes canonical_universe + adds news_activity_score
│   ├── news_activity.py             ← NEW: 7-signal composite
│   └── equivalence_harness.py       ← NEW: v1↔v2 comparison report
└── methodology/
    ├── __init__.py                  ← NEW
    ├── opportunistic_insider.py     ← Cohen-Malloy-Pomorski classifier
    ├── short_interest_signal.py     ← Diether-Lee-Werner
    ├── revision_signal.py           ← Chan-Jegadeesh-Lakonishok
    ├── news_volume_signal.py        ← Tetlock 2007 via GDELT
    ├── filing_density_signal.py     ← Lee-So 8-K count
    ├── lm_tone_signal.py            ← Loughran-McDonald dictionary
    ├── fears_signal.py              ← Da-Engelberg-Gao via pytrends
    └── yang_zhang_vol.py            ← Yang-Zhang realized volatility

config/
├── datasources.yaml                 ← extended: OpenBB router, news sub-weights
├── api_keys.yaml                    ← NEW: env-var mapping
└── lm_dictionary/                   ← NEW: Loughran-McDonald 2024 CSV
```

## 5. Persistent data accumulation — `fetch_watermarks`

The Principle 6 implementation.

### 5.1 Table schema (migrate_to_v3)

```sql
CREATE TABLE IF NOT EXISTS fetch_watermarks (
  source TEXT NOT NULL,
  ticker TEXT NOT NULL,           -- '*' for non-ticker-scoped data
  field TEXT NOT NULL,            -- canonical field name OR 'series:<id>' for FRED
  last_fetched_at TEXT NOT NULL,  -- ISO 8601 UTC
  last_observation_date TEXT,     -- latest date of actual data we hold
  fetch_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  last_error_message TEXT,
  PRIMARY KEY (source, ticker, field)
);
CREATE INDEX idx_watermark_source ON fetch_watermarks(source);
CREATE INDEX idx_watermark_ticker ON fetch_watermarks(ticker);
```

### 5.2 BaseDataSource extensions

```python
class BaseDataSource(ABC):
    def get_fetch_gap(self, ticker, field, db, max_lookback_days=365*5) -> tuple[date, date]:
        """Return (start_date, end_date) of data we still need."""

    def update_watermark(self, ticker, field, last_observation_date, db, success=True, error_message=None) -> None:
        """Upsert into fetch_watermarks. Increments fetch_count or error_count."""
```

### 5.3 INSERT OR IGNORE on time-series tables

`historical_price`, `historical_iv`, `historical_earnings_reactions` switch from `INSERT OR REPLACE` to `INSERT OR IGNORE`. Duplicate (ticker, observation_date) rows silently dropped — never overwritten. Integrity invariant: stored observations are immutable.

### 5.4 force_refetch

`DatabaseManager.force_refetch(source, ticker, field, from_date)`:
- Deletes time-series rows where observation_date >= from_date
- Resets watermark.last_observation_date to from_date - 1
- Logs to source_run_log with status='force_refetch'
- Never called from automated code

### 5.5 Schema migration

`DatabaseManager.migrate_to_v3()` adds the table, switches INSERT semantics, stamps schema_version=3. Idempotent per A.1/A.2 pattern.

## 6. Per-source full fetch implementations

Each source's `fetch_*` methods get real bodies. All respect watermarks. All write raw responses to their `raw_<source>` table.

### 6.1 FinvizSource — `fetch_universe()`

Wraps existing `src/layer1_universe/screen.py` Finviz logic into the adapter contract.
- Output: pd.DataFrame ~3000 rows, source-native columns
- Side effect: writes to `raw_finviz` view → `finviz_universe_history` physical
- Watermark: (`finviz`, `*`, `universe_snapshot`), observation_date=run_date — weekly full refresh
- Idempotency: skip if today's snapshot already exists

### 6.2 YahooSource — universe + fundamentals + history

Uses `yfinance`.
- `fetch_universe(run_id)`: iterates Finviz's ticker list, `yf.Ticker(t).fast_info + .info` per ticker; batch where possible
- `fetch_fundamentals_for_ticker(ticker, run_id)`: `.info`, `.financials`, `.balance_sheet`, `.cashflow`, `.earnings` → extracts 18 canonical fields
- `fetch_historical_price(ticker, gap_dates)`: `yf.Ticker(t).history(start, end)` → writes `historical_price`
- Rate handling: tenacity with exponential backoff (Yahoo throttles unauthenticated calls aggressively)

### 6.3 EdgarSource — XBRL + Form 4 + 8-K

Most engineering-intensive source.

**6.3.1 — 10-K/10-Q XBRL fundamentals**
- Pull `data.sec.gov/api/xbrl/companyfacts/CIK{padded}.json` per ticker
- Parse standardized XBRL concept tags: `us-gaap:Revenues`, `NetIncomeLoss`, `Assets`, `Liabilities`, `OperatingIncomeLoss`, etc.
- Map to canonical: `revenue_ttm`, `net_income_ttm`, `total_assets`, `ebit_ttm`, `operating_margin`, `net_profit_margin`, `total_debt_to_equity`, `interest_coverage`
- TTM = sum of latest 4 quarterly observations
- Writes structured rows to `raw_edgar`

**6.3.2 — Form 4 insider transactions**
- Pull EDGAR Form 4 daily index per ticker's CIK
- Parse XML: filer name, title, transaction code (P/S/A/M), shares, price, post-transaction holdings
- **Cohen-Malloy-Pomorski classifier** (`methodology/opportunistic_insider.py`):
  - Routine: filer has ≥2 transactions in the same calendar month over prior 3 years
  - Opportunistic: everything else
- Writes to `raw_edgar_insider` (new sub-table; created in migrate_to_v3)

**6.3.3 — 8-K filing index**
- Pull EDGAR filing-index for form-type=8-K per ticker, last 90 days
- Records: filing_date, item_codes (2.02 earnings, 5.02 officer change, 7.01 Reg FD, …)
- Writes to `raw_edgar_filings` (new sub-table)

All EDGAR requests use spec-required User-Agent. SEC rate limit ≤10 req/sec — token bucket via tenacity.

### 6.4 FredSource — `fetch_macro_series()`

- Pull fredgraph CSV for: `DGS10`, `DGS3MO`, `DGS2`, `VIXCLS`, `CPIAUCSL`
- Long-format → writes `raw_fred(run_id, series_id, observation_date, value, scrape_timestamp)`
- Watermark per (`fred`, `*`, `series:<id>`)
- Yield curve feeds Layer 2/4 regime priors

### 6.5 FinraSource — biweekly short-interest

- Pull gzipped CSV from `cdn.finra.org/equity/regsho/monthly/...`
- Parse: per-ticker shares short, days-to-cover, exchange
- Compute short-interest delta vs prior period in `short_interest_signal.py`
- Watermark: (`finra`, `*`, `short_interest_biweekly`); skip pulls within 14d window

### 6.6 StockanalysisSource — 10y ratio history

- Target: `stockanalysis.com/stocks/<ticker>/financials/ratios/` + `?p=quarterly`
- HTML parser extracts 10y P/E, EV/EBITDA, P/B, P/S history
- Writes to `raw_stockanalysis(ticker, metric, period, value, scrape_timestamp)` long-format
- Used by materialization to compute `pe_5y_percentile`, `ev_ebitda_5y_percentile` (current vs trailing 5y distribution)
- Parser failures → log `source_run_log` for offline debug; ticker-row marked partial
- Watermark per (ticker, `ratio_history`); refresh quarterly

### 6.7 OpenBBSource — multi-provider router

New `src/common/datasources/openbb_source.py`.

- On init: reads `.env` for `FMP_API_KEY`, `POLYGON_API_KEY`, `TIINGO_API_KEY` → sets in OpenBB user settings
- `fetch_fundamentals_for_ticker(ticker)`: tries `openbb.equity.fundamental.metrics(ticker, provider='fmp')` → fallback `polygon` → `yfinance`. First non-empty wins
- `fetch_historical_price(ticker, gap_dates)`: routes `fmp → polygon → tiingo → yfinance`
- Each successful pull writes one row to `raw_openbb(ticker, field, value, provider_used, scrape_timestamp)`
- Watermark per (`openbb`, ticker, field)
- Missing keys → that route degrades; registry's other sources still cover the field

### 6.8 .env loader

`src/common/env_loader.py`:
```python
def load_env(path: Path = Path('.env')) -> dict[str, str]:
    """Read .env (simple KEY=value), return mapping. Missing file → {}."""

def get_api_key(provider: str) -> str | None:
    """Returns key from env or None. Provider mapping in api_keys.yaml."""
```

No new deps. Parses `KEY=value` lines directly.

## 7. `news_activity_score` — 7-signal composite

Defined in `src/layer1_universe/news_activity.py`.

### 7.1 The 7 sub-signals

| # | Signal | Data | Module | Academic ref | Confidence |
|---|---|---|---|---|---|
| 1 | Opportunistic insider buy/sell | raw_edgar_insider | opportunistic_insider.py | Cohen-Malloy-Pomorski 2012 | High (82 bps/mo) |
| 2 | Short-interest delta + DTC | raw_finra | short_interest_signal.py | Diether-Lee-Werner 2009 | High |
| 3 | Analyst revision velocity | raw_yahoo, raw_finviz | revision_signal.py | Chan-Jegadeesh-Lakonishok 1996 | High |
| 4 | News volume anomaly | raw_gdelt (new) | news_volume_signal.py | Tetlock 2007 | Medium |
| 5 | 8-K filing density | raw_edgar_filings | filing_density_signal.py | Lee-So 2017 | Medium |
| 6 | 10-K/10-Q risk-section tone | EDGAR full-text + LM dict | lm_tone_signal.py | Loughran-McDonald 2011 (+2024 ext) | High |
| 7 | FEARS Google search-volume | raw_gtrends (new) | fears_signal.py | Da-Engelberg-Gao 2011 | Medium |

### 7.2 Per-signal formulas

**Signal 1 — Opportunistic Insider Score**
- Form 4 transactions, last 90 days, classified opportunistic vs routine per CMP rule
- `score = Z_sec( (Buy_opp - Sell_opp) / (Buy_opp + Sell_opp + ε) )` — sector-relative z-score
- Higher = more net opportunistic buying

**Signal 2 — Short Interest Delta + Days-to-Cover**
- `score = -Z_sec( (SI_pct_t - SI_pct_{t-14d}) × (SI_shares / ADV) )` — negative loading
- Rising short interest with high DTC = bearish for longs

**Signal 3 — Analyst Revision Velocity**
- `score = Z_sec( (Up - Down) / N - λ × σ(TP) / μ(TP) )` with λ=0.5
- Net positive revisions minus target-price dispersion penalty

**Signal 4 — News Volume Anomaly**
- GDELT count of mentions in last 30d
- `score = (n_30d/30 - μ_365d) / σ_365d` — z-score of recent activity vs base

**Signal 5 — 8-K Filing Density**
- Count of non-routine 8-Ks (exclude items 2.02, 7.01) in last 30d
- `score = anomaly_z` like Signal 4, per-ticker base rate

**Signal 6 — Loughran-McDonald Tone (Risk Section)**
- Extract 10-K/10-Q Item 1A (Risk Factors) text on each filing
- Apply LM dictionary: NetTone = (positive − negative) / total_words
- `score = (NetTone_current - μ_prior_4_filings) / σ_prior_4_filings` — tone-shift z-score

**Signal 7 — FEARS Search-Volume**
- pytrends pull for company name (not ticker — avoids financial-news noise)
- `score = -Z_sec( log(SVI_7d) - log(SVI_365d_avg) )` — negative loading (high retail attention is contrarian)

### 7.3 Composite

```
news_activity_score = Σ w_i × signal_i
```

Initial weights (`config/scoring.yaml > news_activity_subweights`):
- Opportunistic Insider: 0.30 (highest-alpha)
- Short-interest Delta: 0.15
- Revisions: 0.15
- News Volume: 0.10
- Filing Density: 0.10
- LM Tone: 0.15
- FEARS: 0.05

Phase H recalibrates against trade history.

### 7.4 Missing-data handling

- Sub-signal unavailable → its weight redistributes proportionally to surviving signals (renormalization)
- `news_activity_score = NaN` only if ALL 7 signals missing
- Per-signal availability stored in `field_provenance` for audit

## 8. Yang-Zhang realized volatility

### 8.1 Formula

Yang-Zhang 2000 — minimum-variance unbiased estimator under GBM. Uses OHLC, ~14× more info than close-to-close.

```
σ²_YZ = σ²_overnight + k × σ²_open_to_close + (1−k) × σ²_Rogers-Satchell

σ²_overnight  = mean((log(O_t / C_{t-1}))²)
σ²_oc         = mean((log(C_t / O_t))²)
σ²_RS         = mean( log(H_t/C_t)·log(H_t/O_t) + log(L_t/C_t)·log(L_t/O_t) )
k             = 0.34 / (1.34 + (n+1)/(n−1))
```

Annualized via × √252.

### 8.2 Where used

- A.3: `factors.py` lowvol_score component (replaces close-to-close 60d)
- Layer 2 (future): realized-vol prior for P(target_reached)
- Layer 3 (future): IV-vs-realized comparison for option pricing

### 8.3 Code

`src/methodology/yang_zhang_vol.py`:
```python
def yang_zhang_vol(ohlc_df: pd.DataFrame, window_days: int = 60) -> pd.Series:
    """Yang-Zhang annualized realized vol from OHLCV."""
```

## 9. factors.py refactor

### 9.1 Signature

- Old: `compute_factor_scores(finviz_df) -> pd.DataFrame`
- New: `compute_factor_scores(canonical_df, edgar_form4_df, finra_df, edgar_filings_df, gdelt_df, gtrends_df, lm_tone_df, db) -> pd.DataFrame`
- Returns existing factors + `news_activity_score` + per-sub-signal breakdown columns

### 9.2 Field-name updates

| v0.1 | v2 canonical |
|---|---|
| P/E | pe_ttm |
| Forward P/E | pe_forward |
| Oper M | operating_margin |
| Profit M | net_profit_margin |
| Perf Year | perf_12m |
| 52W High | dist_52w_high |
| 52W Low | dist_52w_low |
| RSI | rsi_14 |

### 9.3 Updated `config/scoring.yaml` composite weights

```yaml
long_screen:
  value_score: 0.25
  quality_score: 0.20
  momentum_score: 0.20
  lowvol_score: 0.15
  revisions_score: 0.10
  news_activity_score: 0.10   # NEW

short_screen:
  value_score: 0.25
  quality_score: 0.10
  momentum_score: 0.25
  lowvol_score: 0.10
  revisions_score: 0.15
  news_activity_score: 0.15   # NEW
```

## 10. Dual-write strategy

screen.py and factors.py write to BOTH v1 + v2.

`DatabaseManager.save_layer1_outputs(run_id, canonical_df, candidates_df)`:
- Calls existing `save_universe(run_id, v1_shaped)` — v1 universe_members
- Calls new `insert_canonical_universe(records)` — v2 canonical_universe
- Calls existing `save_candidates(run_id, screen, df)` — v1 candidate_results
- Future: when expression_evaluations / ranked_trades exist (Phase D), wires those too

Why both:
- Data integrity: can compare v1 vs v2 outputs
- No risk of breaking the existing notebook flow
- v1 stays queryable for historical comparability
- A.5 equivalence-verifies; only then v1 stops being written

## 11. Equivalence harness

`src/layer1_universe/equivalence_harness.py`. Per run:
- v1 universe_members row count vs v2 canonical_universe row count per ticker
- v1 candidate_results.composite_score vs v2 factor outputs
- Reports drift: `equivalence_report(run_id) → {field: max_abs_drift, n_disagreements, …}`
- Saves to `data/runs/<run_id>/equivalence_report.json`
- A.3 produces the report; A.5 sets the assertion threshold

## 12. Configuration

### 12.1 `config/datasources.yaml` extensions

```yaml
enabled_sources: [finviz, yahoo, edgar, fred, stockanalysis, finra, openbb]

source_priority:
  fundamentals: [edgar, stockanalysis, yahoo, openbb, finviz]
  prices:       [yahoo, openbb, finviz]
  identifiers:  [edgar, openbb, yahoo, finviz]
  # short_interest, macro unchanged

news_activity_subweights:
  opportunistic_insider: 0.30
  short_interest_delta:  0.15
  revision_velocity:     0.15
  news_volume:           0.10
  filing_density:        0.10
  lm_tone:               0.15
  fears:                 0.05

realized_vol:
  estimator: yang_zhang   # was: close_to_close
  window_days: 60
```

### 12.2 New `config/api_keys.yaml`

```yaml
fmp:       FMP_API_KEY
polygon:   POLYGON_API_KEY
tiingo:    TIINGO_API_KEY
intrinio:  INTRINIO_API_KEY   # optional
```

### 12.3 Loughran-McDonald dictionary

Static CSV from sraf.nd.edu. Committed to `config/lm_dictionary/LoughranMcDonald_MasterDictionary_2024.csv`. ~12k rows. Refreshed yearly only (academic release).

## 13. Testing strategy

### 13.1 Per-source full-fetch tests

- `tests/datasources/test_<source>_fetch.py` per source
- `responses` or `vcrpy` cassettes for HTTP replay; no live network in CI
- Cases: empty, single-ticker, batch, error, watermark-skip, force-refetch

### 13.2 Methodology sub-signal tests

- `tests/methodology/test_<signal>.py` per signal
- Hand-constructed inputs with known expected outputs
- Edge cases: zero transactions, all-routine, all-opportunistic, ε-handling

### 13.3 fetch_watermarks tests

- `tests/common/test_fetch_watermarks.py`
- v3 migration creates table; get_fetch_gap correct under various watermark states; upsert; force_refetch deletes + resets

### 13.4 Factor refactor + dual-write tests

- `tests/layer1_universe/test_factors_v2.py` — factor scores from canonical_universe match expected
- `tests/layer1_universe/test_dual_write.py` — end-to-end against fixture data populates v1 + v2; equivalence report ≤5% drift

### 13.5 Integration test

- `tests/integration/test_a3_end_to_end.py` (`@pytest.mark.integration`)
- Real `run_layer1()` against production DB; verifies all raw_* tables get rows, canonical_universe populated, news_activity_score computed

## 14. Error-handling matrix

| Failure | Behavior |
|---|---|
| Source fails entirely | Skip; source_run_log status='failed'; resolution renormalizes; sub-signal weight redistributes |
| EDGAR rate-limited | Tenacity backoff; retry from watermark next run |
| OpenBB key missing | Provider silently skipped; other sources cover |
| LM dictionary missing | Signal=NaN with `data_approximated_flags=['lm_dictionary_missing']`; renormalize |
| pytrends 429 | Backoff; FEARS signal optional; renormalize |
| stockanalysis HTML change | Parser raises; source partial; 5y ratio fields sparse for that ticker |
| Schema drift any raw_* | Migration test catches |

## 15. Sub-phase decomposition (A.3.1 – A.3.10)

| Phase | Deliverable | Est. effort |
|---|---|---|
| **A.3.1** | migrate_to_v3 (fetch_watermarks + INSERT OR IGNORE) + BaseDataSource watermark helpers + force_refetch + .env loader | 0.5 wk |
| **A.3.2** | Finviz full fetch_universe; Yahoo full fetch_universe + fetch_fundamentals + fetch_historical_price | 1.0 wk |
| **A.3.3** | EdgarSource 10-K/10-Q XBRL parser → fundamentals | 1.5 wk |
| **A.3.4** | EdgarSource Form 4 + Cohen-Malloy-Pomorski classifier + 8-K index | 1.0 wk |
| **A.3.5** | FredSource full fetch_macro_series; FinraSource biweekly short-interest | 0.5 wk |
| **A.3.6** | StockanalysisSource 10y ratio history scraper | 1.0 wk |
| **A.3.7** | OpenBBSource multi-provider router (FMP-stable/Polygon/Tiingo). Note: FMP API keys issued post-2024 work only on FMP's `stable/` endpoints (legacy `/api/v3/` returns HTTP 403 "Legacy Endpoint"). Confirmed 2026-05-21. OpenBB integration must target `stable/` URLs. | 0.5 wk |
| **A.3.7.5** | **Rate-Limited Fetch Orchestrator** (added 2026-05-21): centralized scheduler wrapping the registry. Tracks per-provider quotas (e.g., Polygon = 5/min, FMP-stable = 250/day, Tiingo = 1000/day). Maintains a priority queue of (ticker, field) pairs to refresh, sorted by staleness. Persists per-minute call history to a new `provider_call_log` table for audit + replay. Designed so the operator can run "do whatever you can in N seconds with M call budget" — supports cron / Windows Task Scheduler dispatch every minute, accreting data incrementally. Enables future "in-house agents continuously managing data accumulation". | 1.0 wk |
| **A.3.8** | 5 sub-signal methodology modules (short-interest, revisions, news_volume, filing_density, fears) + GDELT scaffold activation + pytrends | 1.5 wk |
| **A.3.9** | Loughran-McDonald tone signal (full-text fetcher + dictionary scorer) | 1.0 wk |
| **A.3.10** | Yang-Zhang vol; factors.py refactor; news_activity_score composite; dual-write + equivalence harness; integration test | 1.5 wk |

**Total: 9-10 weeks of agent-dispatch work.** Each sub-phase ends with all-tests-green + commit + methodology-handbook entry.

## 16. Definition of Done

Status as of A.3.10 ship (2026-05-22):

- [x] Schema at v3+ (`fetch_watermarks` exists; INSERT OR IGNORE on time-series) -- shipped A.3.1. Schema is now at v12 (raw_edgar_filing_tone from A.3.9).
- [x] All 7 sources have full `fetch_*` matching their `provides` sets -- A.3.2 through A.3.9 shipped per-source fetchers.
- [x] All 7 news_activity_score sub-signals compute on real data -- methodology modules in `src/methodology/*.py`; aggregated by `src/layer1_universe/news_activity.py` per A.3.10.
- [x] news_activity_score appears in canonical_universe + factor composite -- A.3.10 `compute_factor_scores` adds the column; `CanonicalUniverseRow` already carries `news_activity_score`.
- [x] Yang-Zhang vol computes; opt-in via `ohlcv_by_ticker` arg to `compute_lowvol_score`; close-to-close fallback preserved for the operator notebook.
- [x] Dual-write helper available (`DatabaseManager.save_layer1_outputs`); wiring into `screen.py` is A.4 territory.
- [x] Equivalence harness produces per-run report (`equivalence_report` + `write_equivalence_report`). Assertion threshold (<=5% / <=1%) is A.5 territory.
- [x] force_refetch works; never auto-fires (A.3.1 / A.3.2).
- [ ] Methodology handbook has ~10 new entries -- DEFERRED to a separate documentation pass.
- [ ] All 7 sources pass `@pytest.mark.integration` against live upstream -- A.3 ships one synthetic integration test; live-upstream sweep run manually.
- [x] Full suite passes (876 tests, up from the ~250 projected).
- [x] The build plan marks A.3 shipped (§5.1.0 updated 2026-05-22).

## 17. Future-phase hooks A.3 enables

- **A.4** notebook drilldown reads now-populated v2 tables
- **A.5** equivalence acceptance test assertion (harness ready; A.5 sets threshold)
- **B.x** Layer 2 thesis (consumes canonical_universe + raw_edgar + macro_context)
- **C.x** Layer 3 options (uses historical_iv accumulator + Yang-Zhang priors)
- **D.x** Layer 4 Bayesian (news_activity_score as sentiment prior; opportunistic-insider as directional prior)
- **H** Factor calibration (replaces literature weights with empirical)

## 18. Methodology handbook entries to produce during A.3

One per sub-phase, plus per sub-signal:

1. A.3.1 — Persistent data accumulation (Principle 6 in action)
2. A.3.3 — XBRL parsing for SEC fundamentals
3. A.3.4 — Cohen-Malloy-Pomorski opportunistic-insider classifier ★ (highest-alpha)
4. A.3.5 — Treasury curve and FRED macro signals
5. A.3.6 — Multi-year ratio history and percentile context
6. A.3.7 — OpenBB router architecture
7. A.3.8 — Short-interest delta (Diether-Lee-Werner)
8. A.3.8 — Analyst revision velocity (Chan-Jegadeesh-Lakonishok)
9. A.3.8 — News volume anomaly (Tetlock 2007)
10. A.3.8 — 8-K filing density (Lee-So 2017)
11. A.3.8 — FEARS Google Trends contrarian signal (Da-Engelberg-Gao)
12. A.3.9 — Loughran-McDonald and why general sentiment dictionaries fail in finance
13. A.3.10 — Yang-Zhang volatility (why not close-to-close)
14. A.3.10 — Composite scoring & weight renormalization on missing signals

Each entry follows the handbook template.

## 19. Open questions deferred to Phase H

- Are literature-derived sub-signal weights right for our universe size + holding horizon?
- Does news_activity_score predict short-horizon (1-3d) or longer (10-30d) moves?
- Should `data_quality_score` enter as a Bayesian prior despite methodology-research caveat?

## 20. API key checklist

Already in `.env` (confirmed 2026-05-21):
- FMP_API_KEY ✓
- POLYGON_API_KEY ✓
- TIINGO_API_KEY ✓

Optional override:
- SEC_EDGAR_USER_AGENT (defaults to a project contact email)

Future (not A.3):
- Additional Layer 3 options-data providers
- CME — futures/options expansion in later Layer 3

## 21. Version control

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-05-21 | Initial A.3 design from architecture refresh + econometric research |
