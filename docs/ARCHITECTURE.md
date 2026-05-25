# ARCHITECTURE

This document describes the architecture of the equity-research factor-forecast pipeline that lives in this repository. I built it as a 3-layer Python research stack: a universe screener, a catalyst + technical-setup layer, and a factor-forecast / report layer. Two foundation packages (data adapters and a rate-limited fetch orchestrator) sit underneath. A methodology library implements the academic factor and signal formulas the layers call into.

The repository is the sanitized, public-CV portion of a larger private project. The proprietary execution, broker-integration, and trade-journal components have been removed so that what remains is a self-contained research pipeline that runs locally against free or low-tier paid data sources, produces per-ticker HTML forecast reports, and is fully unit-tested.

---

## 1. Design principles

I picked these seven principles up front and let every architectural decision fall out of them.

### 1.1 Data integrity above all -- no silent failures

Every data fetch records a `source_run_log` row with status, rows fetched, and any HTTP error. Every persisted observation carries provenance (which source, which run, which timestamp). If a source returns garbage, a downstream consumer must be able to tell. The data-quality module (`src/layer1_universe/data_quality.py`) applies per-field sanity bounds and flags violations into `field_provenance` rather than silently dropping rows. A negative market cap, an RSI > 100, or an EPS that disagrees with three other sources is flagged and dampened, not hidden.

### 1.2 Modular layers -- each independently testable

Layer 1 does not call Layer 2 functions in memory. Layer 2 does not call Layer 3. Each layer reads its inputs from disk (SQLite or parquet) and writes its outputs to disk. I can run Layer 2 against last week's Layer 1 output, swap in a different Layer 3 forecaster, or re-render reports without re-running anything upstream. Each layer has its own test directory (`tests/layer1_universe/`, `tests/layer2_catalyst/`, `tests/layer3_forecast/`) and each module is unit-testable as a pure function or with a stub `DatabaseManager`.

### 1.3 Reproducibility -- every output traceable to inputs

Every layer stamps `run_id`, `as_of_date`, and a `*_VERSION` constant (e.g. `FORECAST_VERSION`, `SETUPS_VERSION`, `WALK_FORWARD_VERSION`) into its output rows. Given a report HTML, I can trace it back to the Layer 2 run, then to the Layer 1 run, then to the exact `source_run_log` entries that fed it. The `config_snapshot_json` column on the `runs` table captures the YAML configs in force at run time, so changing `scoring.yaml` does not retroactively change what a previous run was scored under.

### 1.4 Local-first

The pipeline runs on a laptop. SQLite is the persistent store. Parquet is the inter-layer file format. No cloud SQL, no S3, no message queue. The orchestrator's rate-limit accounting is a single `quota_window` table inside the same SQLite file. The only external dependencies are the data-vendor HTTP APIs themselves, all of which have free or hobbyist tiers. Local-first is what makes the project demoable from a clone-and-`pytest` cold start.

### 1.5 Plain-language outputs with explanations

The Layer 3 report is the user-facing artifact. Every panel -- forecast cone, factor contributions, IC backtest, catalysts in window -- has a prose paragraph explaining what is shown and why it matters. The factor contributions panel surfaces additive (`coef * x`) per-factor impact, which is equivalent to Shapley values in the linear case. The LLM-narrative module (`src/layer3_forecast/llm_narrative.py`) optionally enriches the prose behind an `ANTHROPIC_API_KEY` guard but the report renders fine without it. A report is meant to be readable by an investment-committee audience that has never opened the codebase.

### 1.6 Version everything

Module-level `*_VERSION` constants are baked into every row of output the module writes. The SQLite schema has a `schema_version` table that gates idempotent migrations. The methodology modules each carry `{SIGNAL}_VERSION` so that a recalibration that changes the formula is detectable in the audit trail. The factor-scoring weights, screening thresholds, and orchestrator quotas all live in versioned YAML under `config/` and never get hard-coded into Python.

### 1.7 Layers communicate via files on disk, not in-memory

This is the load-bearing principle. Layer 1 writes `canonical_universe` and `candidate_results` rows to SQLite. Layer 2 reads those tables, computes its own derived state, and writes `data/layer2/<as_of_date>/<run_id>/active_candidates.parquet`. Layer 3 reads the Layer 2 parquet file plus the underlying `historical_price` table and writes `data/reports/<as_of_date>/<ticker>.html`. No layer holds another layer's intermediate state in memory. Section 6 covers the contract in detail.

---

## 2. Layer overview

```
   +-------------------+      +-------------------+      +-------------------+
   |     LAYER 1       |      |     LAYER 2       |      |     LAYER 3       |
   |                   |      |                   |      |                   |
   | Universe +        |      | Catalyst calendar |      | Factor forecast + |
   | multi-factor      | ---> | + technical setup | ---> | ensemble + report |
   | screen            |      | confluence        |      |                   |
   |                   |      |                   |      |                   |
   | weekly cadence    |      | daily cadence     |      | on-demand / daily |
   +-------------------+      +-------------------+      +-------------------+
            |                          |                          |
            v                          v                          v
   canonical_universe          active_candidates           per-ticker
   candidate_results           .parquet                    HTML report
   (SQLite)                                                + index.html

           ^                          ^                          ^
           |                          |                          |
   +-----------------------------------------------------------------------+
   |               Foundation: data adapters + orchestrator                |
   |                                                                       |
   | 13 BaseDataSource adapters (Finviz / Yahoo / EDGAR / FRED / FINRA /   |
   | StockAnalysis / OpenBB / Polygon / FMP / Tiingo / GDELT / pytrends)   |
   | Rate-limited fetch orchestrator (sliding-window quotas, priorities)   |
   +-----------------------------------------------------------------------+
                                      ^
                                      |
   +-----------------------------------------------------------------------+
   |                   Methodology: pure-function library                  |
   |                                                                       |
   | Factor formulas, Yang-Zhang vol, Loughran-McDonald tone,              |
   | opportunistic-insider classifier, FEARS, technical indicators,        |
   | peer-comparison, sector-rotation, multi-timeframe, TA signal rules    |
   +-----------------------------------------------------------------------+
```

The methodology library is called by all three layers. The data adapters and orchestrator are called by Layer 1 and (to a lesser extent) Layer 3's macro module. Layers 2 and 3 do not fetch from the network directly except where Layer 3 pulls macro proxies (^TNX, ^IRX, ^VIX, sector ETFs) via yfinance for its macro panel.

---

## 3. Per-layer specification

### 3.1 Layer 1 -- Universe + multi-factor screen

**Purpose.** Produce a weekly point-in-time mid-cap-and-above US equity universe with multi-source-blended fundamentals and a composite factor score, then filter it to a screening shortlist.

**Inputs.**
- Finviz screener (mid-cap-and-above optionable US tickers; configurable band)
- Yahoo fundamentals snapshots (`.info` + historical OHLCV)
- SEC EDGAR XBRL companyfacts (canonical GAAP fundamentals + Form 4 + 8-K)
- FRED (yield curve, VIX, CPI)
- FINRA (short interest)
- StockAnalysis.com (10-year ratio history)
- OpenBB router (FMP / Polygon / Tiingo cross-validation)
- GDELT (news-volume anomaly signal)
- pytrends (Da-Engelberg-Gao FEARS signal)
- `config/filters.yaml` (screening thresholds)
- `config/scoring.yaml` (factor composite weights)

**Processing.**
1. Fetch the universe via Finviz, persist to `finviz_universe_history`.
2. For each ticker, fan out per-source fetches respecting fetch-watermarks (never re-fetch a date already on disk).
3. Materialize `canonical_universe`: priority-weighted blending across sources, disagreement detection, data-quality flagging.
4. Compute six factor scores per ticker (`value_score`, `quality_score`, `momentum_score`, `lowvol_score`, `revisions_score`, `news_activity_score`). `news_activity_score` is itself a 7-signal sub-composite (opportunistic insider, short-interest delta, revisions velocity, news volume anomaly, filing density, LM tone shift, FEARS).
5. Compute `composite_score` as the weighted sum (weights from `scoring.yaml`).
6. Apply screening cutoffs (market cap, liquidity, ratio bands) to produce per-screen-criteria candidate sets (long-bias and short-bias filters).

**Outputs.**
- `canonical_universe` (one row per ticker per run, with all blended fields + factor scores)
- `candidate_results` (one row per ticker per screen, with `eligible` flag and rank)
- `filter_waterfall` (per-step survivor counts -- the audit trail of which filter knocked out which ticker)
- `field_provenance` (per-field, per-source observations -- the raw audit trail)
- `data_quality_metrics` (per-run aggregate quality numbers)

**Cadence.** Weekly. Watermarks ensure the second weekly run is incremental, not a full refetch.

**Tech stack.** finvizfinance, yfinance, requests, pandas, pydantic (schemas), SQLite.

**Validation rules.**
- Per-field sanity bounds in `data_quality.py` (e.g. `0 <= rsi_14 <= 100`, `market_cap_usd > 0`, `-1 <= net_profit_margin <= 1`).
- Cross-source disagreement detection in `equivalence_harness.py`.
- Source priority resolution in `src/common/datasources/resolution.py` (EDGAR > OpenBB > Yahoo > Finviz for fundamentals).
- Factor scoring uses sector-relative z-scores so a cyclical at the top of its sector beats a defensive at the top of its.
- Winsorization at 1% / 99% before z-scoring to dampen outliers.

### 3.2 Layer 2 -- Catalyst calendar + technical setups

**Purpose.** Filter the Layer 1 shortlist to the subset that has a near-term catalyst and a confirming technical setup right now.

**Inputs.**
- `canonical_universe` and `candidate_results` from Layer 1 (read by `_fetch_layer1_candidates`).
- `historical_price` OHLCV (read by `_fetch_ohlcv`). Layer 2 does not fan out a parallel yfinance fetch; if `historical_price` has fewer than 30 rows for a ticker, it is skipped and logged.
- `catalyst_calendar` (read by `_fetch_catalysts_in_window` over a 90-day forward window).

**Processing.**
For each candidate:
1. Compute technical indicators (`rsi_14`, `macd`, `atr_14`, `sma_200`, `sma_20`, volume profile, pivot points) via `src/methodology/technical_indicators.py`.
2. Evaluate a 4-feature bullish or bearish setup block depending on the screen the candidate came through. Bullish features: within 5% of nearest support, 30-day base breakout, RSI not overbought, volume confirmation. Bearish features: >= 15% extended above 200-DMA, lower-high pattern, RSI overbought, distribution volume.
3. Sum the firing features to a `confluence_score` (Murphy two-or-more confirmation rule -- threshold = 2).
4. Check `catalyst_in_window` over the next 90 days.
5. Mark `is_active = (confluence_score >= 2) AND catalyst_in_window`.

**Outputs.**
- `data/layer2/<as_of_date>/<run_id>/active_candidates.parquet` with one row per active candidate, containing ticker, screen, `confluence_score`, `catalyst_in_window`, `n_catalysts`, `nearest_catalyst_date`, `nearest_catalyst_type`, the firing feature names, and `is_active`.
- A `Layer2RunResult` dataclass returned to the caller summarising input count, active count, and skipped count.

**Cadence.** Daily. Technical setups expire fast; catalyst windows shift daily.

**Tech stack.** pandas, pyarrow (parquet), SQLite reader.

**Validation rules.**
- Fewer than 30 OHLCV rows -> skip with a note in `Layer2RunResult.notes`, do not raise.
- Setup-feature functions return four booleans per ticker regardless of input quirks; NaN handling lives inside `bullish_setup_features` / `bearish_setup_features` so the caller cannot get a partial dict.
- Parquet output is only written when at least one candidate is active; an empty active set produces no file (callers check `n_active_candidates`).

### 3.3 Layer 3 -- Factor forecast

**Purpose.** Produce a forward-return point forecast + 80% / 95% confidence intervals per ticker, with per-factor attribution, technical / macro / sentiment overlays, an IC-backtest panel, and a walk-forward accuracy panel. Render the whole thing as a self-contained HTML report.

**Inputs.**
- Layer 2 `active_candidates.parquet` (the ticker list).
- `canonical_universe` (the factor scores).
- `historical_price` (for the price chart, walk-forward backtest, and Yang-Zhang vol).
- A cross-sectional factor + realized-return panel (real if A.3 universe history exists, synthetic via `ic_backtest_demo.build_synthetic_panel` otherwise -- provenance surfaced in the report).
- Macro proxies via yfinance (^TNX, ^IRX, ^VIX, sector ETF).
- `config/scoring.yaml` for ensemble weights.

**Processing.**
1. Linear multi-factor forecast (`factor_forecast.py`): cross-sectional OLS over the panel produces per-factor loadings; the ticker's current factor vector is dotted with the loadings to produce a point return; residual bootstrap (Efron 1979) produces 80% / 95% intervals.
2. GBM Monte Carlo (`monte_carlo.py`): drift from the linear forecast, volatility from Yang-Zhang (`src/methodology/yang_zhang_vol.py`) over trailing 60d, optional EWMA dynamic vol.
3. AR(1) momentum baseline.
4. Random-walk baseline ("humility line").
5. Ensemble (`ensemble.py`): inverse-MAE weights if a `weight_history` is available, equal weights otherwise.
6. Walk-forward backtest (`walk_forward_backtest.py`): per-ticker, fit on a rolling 60-day window, predict +1 / +30 / +60, compute MAE / RMSE / directional accuracy / 80% CI hit-rate against realized history. The benchmark target is PMC9680880's LASSO-LSTM directional accuracy.
7. Factor IC backtest (`factor_backtest.py`): Spearman rank correlation between each factor and realized horizon returns across the cross-section; ICIR over time; cumulative attribution P&L per factor.
8. Sensitivity tornado (`sensitivity.py`): per-factor +/-1 std shock impact on the point forecast.
9. Macro panel (`macro_factors.py`): yield-curve slope, VIX z-score, sector relative-return, macro regime tag.
10. Optional LLM narrative (`llm_narrative.py`): bull / bear / synthesis paragraphs behind an `ANTHROPIC_API_KEY` guard.
11. Render via Jinja2: `templates/stock_report.html.j2` (v1, matplotlib + base64 PNG) or `templates/stock_report_v2.html.j2` (v2, Plotly dark mode, interactive).

**Outputs.**
- `data/reports/<as_of_date>/<ticker>.html` -- one self-contained file per ticker (no external assets in v1; Plotly CDN in v2).
- `data/reports/<as_of_date>/index.html` -- ticker index.

**Cadence.** Daily for active candidates; on-demand for ad-hoc tickers via `scripts/forecast_demo.py` or `scripts/forecast_live_demo.py`.

**Tech stack.** numpy, pandas, scikit-learn (linear regression utilities), lightgbm (`ml_forecaster.py` directional classifier), hmmlearn (`hmm_regime.py` regime conditioning), matplotlib (v1 plots), plotly (v2 plots), jinja2 (templates).

**Validation rules.**
- The IC backtest module flags synthetic-panel provenance into the rendered report -- a reader sees explicitly whether the IC numbers came from real cross-sectional history or from a calibrated synthetic panel.
- Walk-forward backtest requires at least `lookback + horizon` bars; otherwise emits a `HorizonAccuracy` row with `n_predictions = 0` rather than raising.
- Bootstrap intervals fall back to the +/-1 standard error of the residuals when the bootstrap sample is empty.
- The LLM-narrative call is wrapped in a try/except; an absent API key, a timeout, or a malformed response degrades the report to the template-only narrative without aborting the render.

---

## 4. File and folder structure

```
<project-root>\
|
+-- .env                          (API keys; gitignored)
+-- .env.example                  (template showing every key slot)
+-- .gitignore
+-- requirements.txt              (Python dependencies)
+-- requirements-notebook.txt     (Jupyter extras)
+-- pytest.ini                    (test runner config)
|
+-- config/
|   +-- api_keys.yaml             (source name -> env var mapping)
|   +-- datasources.yaml          (source priorities, resolution rules)
|   +-- filters.yaml              (Layer 1 screening cutoffs)
|   +-- scoring.yaml              (factor composite weights)
|   +-- orchestrator.yaml         (per-provider rate-limit quotas)
|   +-- gdelt_alias.yaml          (GDELT ticker -> company-name aliases)
|   +-- lm_dictionary/            (Loughran-McDonald sentiment dictionary)
|
+-- data/                         (gitignored; created at runtime)
|   +-- fundamentals.db           (the SQLite store; single source of truth)
|   +-- layer2/<as_of>/<run>/     (Layer 2 parquet outputs)
|   +-- reports/<as_of>/          (Layer 3 HTML reports)
|
+-- docs/
|   +-- ARCHITECTURE.md           <- this file
|   +-- DATAFLOW_OVERVIEW.md      (historical dataflow + table reference)
|   +-- HOW_TO_RUN_TESTS.md       (operator guide for the smoke notebook)
|   +-- interface-framework-comparison.md
|   +-- benchmarks/
|   |   +-- directional_accuracy.md
|   +-- methodology/
|       +-- methodology-handbook.md
|       +-- factor_models.md
|       +-- research_bibliography.md
|       +-- options_pricing.md
|       +-- volatility_analysis.md
|       +-- financial-methodology-reference.md
|
+-- notebooks/
|   +-- smoke_tests.ipynb         (the primary operator UI)
|   +-- layer1_control.ipynb      (Layer-1 control panel)
|   +-- layer1_v2_drilldown.ipynb (per-ticker drilldown panel)
|
+-- reports/
|   +-- sample/                   (committed sample report output)
|
+-- scripts/
|   +-- daily_assessment.py       (the daily run driver)
|   +-- forecast_demo.py          (synthetic end-to-end forecast demo)
|   +-- forecast_live_demo.py     (live-data variant)
|   +-- orchestrator_tick.py      (single-tick orchestrator runner)
|   +-- benchmark_directional.py  (PMC9680880 benchmark replication)
|   +-- polygon_backfill.py       (Polygon historical backfill helper)
|
+-- src/
|   +-- common/
|   |   +-- database.py           (DatabaseManager: SQLite ops + migrations)
|   |   +-- schemas.py            (Pydantic models for every table)
|   |   +-- env_loader.py         (reads .env + api_keys.yaml)
|   |   +-- parsing.py            (Finviz number-parsing helpers)
|   |   +-- datasources/          (13 BaseDataSource adapters)
|   |   |   +-- base.py
|   |   |   +-- registry.py
|   |   |   +-- resolution.py
|   |   |   +-- catalyst_source.py
|   |   |   +-- finviz_source.py
|   |   |   +-- yahoo_source.py
|   |   |   +-- edgar_source.py (+ edgar_xbrl_parser, edgar_submissions_parser, edgar_form4_parser, edgar_full_text)
|   |   |   +-- fmp_source.py
|   |   |   +-- finra_source.py
|   |   |   +-- fred_source.py
|   |   |   +-- gdelt_source.py
|   |   |   +-- openbb_source.py
|   |   |   +-- polygon_options.py
|   |   |   +-- pytrends_source.py
|   |   |   +-- stockanalysis_source.py (+ stockanalysis_parser)
|   |   |   +-- sec_cik_lookup.py
|   |   +-- orchestrator/
|   |       +-- fetch_orchestrator.py (RateLimitedFetchOrchestrator)
|   |       +-- queue_priority.py
|   |       +-- quota_window.py
|   |       +-- config.py
|   |
|   +-- methodology/              (academic-signal pure-function library)
|   |   +-- technical_indicators.py
|   |   +-- yang_zhang_vol.py
|   |   +-- volume_features.py
|   |   +-- multi_timeframe.py
|   |   +-- ta_signal_rules.py
|   |   +-- peer_comparison.py
|   |   +-- sector_rotation.py
|   |   +-- opportunistic_insider.py (Cohen-Malloy-Pomorski)
|   |   +-- short_interest_delta.py
|   |   +-- revisions_velocity.py
|   |   +-- news_volume_anomaly.py
|   |   +-- filing_density.py
|   |   +-- lm_dictionary.py + lm_tone_signal.py
|   |   +-- fears_signal.py (Da-Engelberg-Gao)
|   |
|   +-- layer1_universe/
|   |   +-- screen.py             (universe fetch + screen)
|   |   +-- factors.py            (cross-sectional factor scoring)
|   |   +-- news_activity.py      (7-signal sentiment composite)
|   |   +-- data_quality.py       (per-field sanity-bound validators)
|   |   +-- equivalence_harness.py (cross-source disagreement detection)
|   |   +-- notebook_panels.py    (notebook display helpers)
|   |
|   +-- layer2_catalyst/
|   |   +-- pipeline.py           (run_layer2 entry point)
|   |   +-- setups.py             (bullish + bearish feature blocks)
|   |
|   +-- layer3_forecast/
|       +-- factor_forecast.py    (linear multi-factor regression)
|       +-- ensemble.py           (4-method ensemble)
|       +-- monte_carlo.py        (GBM Monte Carlo)
|       +-- ml_forecaster.py      (LightGBM directional classifier)
|       +-- ml_features.py        (~89 engineered features)
|       +-- hmm_regime.py         (3-state Gaussian HMM regime conditioning)
|       +-- intraday_forecaster.py
|       +-- cross_sectional.py
|       +-- factor_backtest.py    (per-factor IC + attribution P&L)
|       +-- factor_decay.py
|       +-- walk_forward_backtest.py (per-ticker accuracy backtest)
|       +-- ic_backtest_demo.py   (synthetic-panel fallback)
|       +-- placebo_audit.py
|       +-- sensitivity.py        (one-factor-at-a-time tornado)
|       +-- macro_factors.py      (yield curve, VIX, sector relative)
|       +-- news_sentiment_v2.py
|       +-- free_sentiment.py
|       +-- llm_narrative.py      (optional ANTHROPIC_API_KEY narrative)
|       +-- report.py             (v1 matplotlib HTML render)
|       +-- report_v2.py          (v2 Plotly dark-mode HTML render)
|       +-- templates/
|           +-- stock_report.html.j2
|           +-- stock_report_v2.html.j2
|
+-- tests/
    +-- common/                   (database + schemas + env_loader)
    +-- datasources/              (per-adapter + integration)
    +-- orchestrator/             (queue + quota window + fetch tick)
    +-- methodology/              (per-signal pure-function tests)
    +-- layer1_universe/
    +-- layer2_catalyst/
    +-- layer3_forecast/
    +-- integration/              (cross-layer + smoke + corruption tests)
    +-- fixtures/                 (committed sample upstream responses)
```

---

## 5. Technology stack summary

| Component | Library | Why |
|-----------|---------|-----|
| Runtime | Python 3.11+ | Walrus, structural pattern matching, `from __future__ import annotations`, modern typing |
| Tabular | pandas, numpy | Cross-sectional and time-series operations everywhere |
| Columnar I/O | pyarrow | Inter-layer parquet for Layer 2 outputs |
| Persistent store | SQLite (stdlib `sqlite3`) | Local-first, single file, idempotent migrations, BEGIN IMMEDIATE for atomic dequeues |
| HTTP | requests | All upstream data-vendor calls |
| Retry | tenacity | Exponential backoff on transient HTTP failures inside adapters |
| Schema validation | pydantic 2.x | Row-level validation between scraping and persistence |
| Templating | jinja2 | HTML report rendering |
| Plots | matplotlib (v1), plotly (v2) | Static base64 PNG; interactive dark-mode |
| ML forecasting | lightgbm, scikit-learn | LightGBM classifier for directional accuracy; sklearn for linear regression + cross-validation utilities |
| HMM regime | hmmlearn | 3-state Gaussian HMM regime conditioning for v6 |
| Sentiment | Loughran-McDonald dictionary (vendored); pytrends; GDELT REST | 10-K/10-Q tone; FEARS retail-attention signal; news volume |
| Screener | finvizfinance | Mid-cap-and-above universe + initial Finviz factor pull |
| Price data | yfinance | OHLCV + macro proxies |
| Config | PyYAML | All thresholds, weights, quotas, source priorities |
| Tests | pytest, pytest-mock | Per-module + integration; smoke notebook also exercised under `pytest` |
| Notebook | jupyterlab, ipykernel | Operator UI (smoke_tests.ipynb, layer1_control.ipynb) |
| HTML parsing | beautifulsoup4 | StockAnalysis.com ratio scraping; SEC filing index parsing |

---

## 6. Layer interaction contracts

The central design choice in this codebase is that layers communicate via durable, inspectable artifacts on disk. Each layer's contract has three parts: what it reads, what it writes, and how the next layer locates the output.

### 6.1 Why files-on-disk rather than in-memory pipelines

I made this choice for five reasons.

1. **Independent runnability.** I can re-run Layer 2 against a Layer 1 output from three days ago without holding Layer 1 in memory. I can prototype a new Layer 3 forecaster against frozen Layer 2 output without paying the Layer 1 + Layer 2 cost on every iteration.
2. **Observability.** A `canonical_universe` row or an `active_candidates.parquet` file is a thing I can open in a notebook, diff between runs, and SQL-query.
3. **Crash safety.** A crash mid-Layer-3 does not corrupt Layer 1 or Layer 2 output. Each layer's writes are atomic at the level of a SQLite transaction or a parquet rename.
4. **Auditability.** Every output carries `run_id` + `as_of_date` + version stamp. The full dependency chain is reconstructible from the rows.
5. **Cadence decoupling.** Layer 1 runs weekly. Layer 2 runs daily. Layer 3 runs on demand. Each cadence is set independently of the others.

### 6.2 Layer 1 -> Layer 2 contract

Layer 1 writes:
- `canonical_universe(run_id, ticker, sector, market_cap_usd, value_score, quality_score, momentum_score, lowvol_score, revisions_score, news_activity_score, composite_score, ...)`
- `candidate_results(run_id, ticker, screen, composite_score, eligible, ...)`

Layer 2 reads via a single LEFT JOIN keyed on `(run_id, ticker)`. The Layer 2 caller passes `layer1_run_id` explicitly; there is no implicit "latest run" semantics. If `candidate_results` has no rows for that `run_id`, Layer 2 returns a `Layer2RunResult` with `n_input_candidates = 0` rather than raising.

### 6.3 Layer 2 -> Layer 3 contract

Layer 2 writes `data/layer2/<as_of_date>/<layer2_run_id>/active_candidates.parquet` with columns:
- identifiers: `ticker`, `screen`, `layer1_run_id`, `layer2_run_id`, `as_of_date`
- signal: `confluence_score` (0-4), `catalyst_in_window` (bool), `n_catalysts`, `nearest_catalyst_date`, `nearest_catalyst_type`, `setups` (comma-joined firing feature names)
- decision: `is_active` (bool)

Layer 3 receives a list of `(ticker, as_of_date)` pairs from this parquet plus the underlying `historical_price` table for OHLCV.

### 6.4 Layer 3 outputs

Layer 3 writes one self-contained HTML file per ticker at `data/reports/<as_of_date>/<ticker>.html` and an `index.html`. Each report embeds its forecast + macro + IC + walk-forward panels with no external file dependencies (v1) or only the Plotly CDN script tag (v2). The HTML is a leaf node; nothing downstream consumes it programmatically.

### 6.5 The watermark + provenance subcontract

Every data-adapter fetch records into `fetch_watermarks(source, ticker, field, last_fetched_through_date)`. Every persisted observation records into `field_provenance(run_id, ticker, field, source, value, observed_at, flagged_reason)`. These two tables are the inter-run contract: a second weekly run never re-fetches a date already on disk, and every canonical value in `canonical_universe` is traceable to the underlying per-source observations that produced it.

---

## 7. Data flow diagram

```
                                Network
                                   |
                                   v
+-----------------------------------------------------------------------+
|                       Foundation layer                                |
|                                                                       |
|   +-------------------+    +-------------------+                      |
|   | BaseDataSource    |    | RateLimited       |                      |
|   | adapters (13)     |--->| FetchOrchestrator |                      |
|   |                   |    | (sliding-window   |                      |
|   | finviz, yahoo,    |    |  quotas, priority |                      |
|   | edgar, fred,      |    |  queue, BEGIN     |                      |
|   | finra, polygon,   |    |  IMMEDIATE)       |                      |
|   | fmp, tiingo,      |    +-------------------+                      |
|   | gdelt, pytrends,  |              |                                |
|   | stockanalysis,    |              v                                |
|   | openbb,           |    +-------------------+                      |
|   | catalyst          |    | source_run_log    |                      |
|   +-------------------+    | fetch_watermarks  |                      |
|             |              | provider_call_log |                      |
|             v              | quota_window      |                      |
|   +-------------------+    +-------------------+                      |
|   | raw_finviz        |                                               |
|   | raw_yahoo         |                                               |
|   | raw_edgar_*       |                                               |
|   | raw_fred          |                                               |
|   | raw_finra         |                                               |
|   | raw_stockanalysis |                                               |
|   | raw_openbb        |                                               |
|   | raw_gdelt         |  <-- one table per source, accumulating       |
|   | raw_pytrends      |                                               |
|   | historical_price  |                                               |
|   | catalyst_calendar |                                               |
|   +-------------------+                                               |
|             |                                                         |
+-------------|---------------------------------------------------------+
              |
              v
+-----------------------------------------------------------------------+
| Layer 1                                                               |
|   reads : raw_*, historical_price                                     |
|   uses  : methodology.* (factor formulas, news_activity signals,      |
|           yang_zhang_vol, opportunistic_insider, lm_tone_signal,      |
|           fears_signal, revisions_velocity, short_interest_delta,     |
|           news_volume_anomaly, filing_density)                        |
|   writes: canonical_universe, candidate_results, field_provenance,    |
|           filter_waterfall, data_quality_metrics                      |
+-----------------------------------------------------------------------+
              |
              v
+-----------------------------------------------------------------------+
| Layer 2                                                               |
|   reads : canonical_universe, candidate_results, historical_price,    |
|           catalyst_calendar                                           |
|   uses  : methodology.technical_indicators (rsi, macd, atr, sma,      |
|           volume_profile, pivot_points)                               |
|   writes: data/layer2/<as_of>/<run>/active_candidates.parquet         |
+-----------------------------------------------------------------------+
              |
              v
+-----------------------------------------------------------------------+
| Layer 3                                                               |
|   reads : active_candidates.parquet, canonical_universe,              |
|           historical_price, cross-sectional panel (real or synthetic) |
|   fetches macro proxies: ^TNX, ^IRX, ^VIX, sector ETFs (yfinance)     |
|   uses  : factor_forecast, monte_carlo, ensemble, walk_forward,       |
|           factor_backtest, sensitivity, macro_factors,                |
|           ml_forecaster + ml_features + hmm_regime, llm_narrative,    |
|           methodology.yang_zhang_vol                                  |
|   writes: data/reports/<as_of>/<ticker>.html (one per ticker)         |
|           data/reports/<as_of>/index.html                             |
+-----------------------------------------------------------------------+
              |
              v
                              Human reader
```

The diagram makes the two horizontal cuts explicit. The foundation layer is the only thing that touches the network during Layers 1 and 2 (Layer 3 touches yfinance directly for its four macro proxies). The methodology library is called by all three layers but writes nothing itself. The three numbered layers are the durable artifacts: SQLite tables for Layer 1, a parquet file for Layer 2, an HTML file for Layer 3.

---

## 8. Notes on what is intentionally not in this repository

The original project had additional layers for sizing, broker execution, order management, and a closed-trade journal-based backtest. Those are proprietary and have been removed. What remains here is the research half: universe construction, multi-source data integrity, factor scoring, technical-setup confirmation, multi-method forecasting with confidence intervals, IC and walk-forward backtests, and a per-ticker HTML report. The backtest that does ship (`walk_forward_backtest.py`, `factor_backtest.py`) is research-grade: it measures per-ticker prediction accuracy and per-factor Information Coefficient against realized history. It is not a closed-trade trading journal.
