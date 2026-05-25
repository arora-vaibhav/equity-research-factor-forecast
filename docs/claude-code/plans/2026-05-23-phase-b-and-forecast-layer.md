# Phase B + Forecast Layer — Execution Plan

> Supersedes the earlier `2026-05-23-phase-b-decomposition.md` draft. Decisions approved 2026-05-23 (resumed session).

**Hard dependency:** Phase A complete (head `dbb7525`, 935/935 tests).

**Standing instruction:** autonomous execution through Phase B + Forecast Layer.

---

## Methodology decisions (locked 2026-05-23)

| # | Decision | Choice | Source |
|---|---|---|---|
| 1 | Catalyst window | 90 days | Build plan §6.1 |
| 2 | Setup confluence threshold for "active" | **2** confirming signals (of 4) | Murphy 1999 "Technical Analysis of the Financial Markets" ch.17 -- two-or-more independent confirmation rule. Lo, Mamaysky & Wang 2000 (JF) used 3+ for stronger patterns but balanced with selectivity-vs-coverage. 2 is the standard practitioner threshold. |
| 3 | OHLCV source | Reuse `raw_yahoo` from A.3.2 | No parallel fetches; route through existing data-pull layer. |
| 4 | Reasoning string | **Hybrid**: Jinja2 template skeleton + optional LLM enrichment | Per advanced-AI-engineering lit (LangChain `PromptTemplate`, Anthropic Constitutional AI, OpenAI structured outputs): templates as deterministic scaffold + LLM only for synthesis. LLM guarded by `ANTHROPIC_API_KEY` env var so tests stay deterministic + free. |
| 5 | Technicals spot-check reference | Hand-computed textbook values | Wilder 1978 "New Concepts in Technical Trading" (RSI/ATR); Appel 1979 (MACD). More reliable than any third-party lib. |

---

## Phase B execution waves

### B.1 — Catalyst calendar substrate
- `migrate_to_v14` adds `raw_catalysts` (audit) + `catalyst_calendar` (curated).
- `CatalystRow` Pydantic with `ticker, catalyst_type, catalyst_date, catalyst_description, source_url, confidence`.
- `CatalystSource(BaseDataSource)` with `fetch_earnings_dates` + `fetch_ex_dividend_dates` via yfinance.

### B.2 — Technical indicators (pure functions)
- `src/methodology/technical_indicators.py` with `rsi_14`, `macd`, `sma`, `ema`, `atr_14`, `volume_profile`, `pivot_points`.
- Yang-Zhang vol reused from A.3.10.

### B.3 — Setup evaluators
- `src/layer2_catalyst/setups.py` -- 4 criteria each per long screen and short screen; `confluence_score` returns 0-4; threshold = 2.

### B.4 — Layer 2 pipeline
- `src/layer2_catalyst/pipeline.py` -- `run_layer2(layer1_run_id, as_of_date, db) -> Layer2RunResult`. Reads `raw_yahoo` (no parallel fetch). Emits `active_candidates.parquet`.

### B.5 — Catalyst source expansion (DEFERRED post-Forecast)
- yfinance earnings + ex-div in B.1 are enough for the Forecast Layer demo on SNDK/LSCC/TCOM/GIS/AECOM.

### B.6 — Hybrid reasoning generator
- `src/layer2_catalyst/reasoning.py` -- Jinja2 template + optional LLM synthesis behind `ANTHROPIC_API_KEY` env-var guard.

### B.7 — Integration test
- `tests/integration/test_b_end_to_end.py`.

---

## Forecast Layer execution waves (NEW 2026-05-23)

Sits BETWEEN Phase B and Phase C. Consumes all aggregated factors; produces per-stock HTML report with forecast plot + accuracy tracking.

**Methodology:** Linear multi-factor expected-return regression + bootstrapped 80% / 95% intervals.

**Why not LSTMs/Prophet?** Gu, Kelly, Xiu 2020 *RFS* "Empirical Asset Pricing via Machine Learning" + Krauss, Do, Huck 2017 *EJOR* find that linear factor models match or exceed deep-learning models for monthly-to-quarterly horizons when factors are well-engineered -- exactly what Phase A.3 + Phase B deliver. LSTMs win on tick-level intraday with order-flow data; we don't have that.

**Forecast horizon:** 30 / 60 / 90 calendar days (matches short / medium / long DTE windows).

### F.1 — Forecast core
- `src/layer3_forecast/factor_forecast.py`:
  - `compute_factor_loadings(history, horizon_days) -> pd.Series`
  - `forecast_return(ticker_factors, loadings, *, horizon_days, n_bootstrap=1000) -> ForecastResult`
  - `ForecastResult` dataclass with point + 80/95 intervals + per-factor Shapley-style attribution.

### F.2 — HTML report generator
- `src/layer3_forecast/report.py` -- Jinja2 template + matplotlib base64-embedded forecast plot.
- Output: `data/reports/<as_of_date>/<ticker>.html` (single self-contained file).

### F.3 — Prediction tracking + accuracy backtest
- Schema v15: `forecast_log` + `forecast_realized` tables.
- `compute_forecast_accuracy(db, *, horizon_days, lookback_days=365)` returns MAE + hit-rate-80 + hit-rate-95.

### F.4 — End-to-end demo on the test set
- `scripts/forecast_demo.py` -- runs the pipeline for SNDK, LSCC, TCOM, GIS, AECOM and emits 5 HTML reports + `index.html`.

### F.5 — Methodology handbook entry
- `docs/methodology/forecast_layer_methodology.md` with citations.

---

## Out of scope (Phase C+)

- Options chain / Greeks / IV (Phase C)
- Ranking (Phase D)
- Broker integration / journal (Phase E / F)

The Forecast Layer is an INTERMEDIATE deliverable available for review before committing to Phase C's options-data work.
