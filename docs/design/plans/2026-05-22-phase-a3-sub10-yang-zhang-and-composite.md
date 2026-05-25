# Phase A.3.10 — Yang-Zhang vol + factors refactor + news_activity composite + dual-write

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` (recommended). The closer for Phase A.3.

**Goal:** Wire the seven sub-signal substrates (A.3.4 opportunistic-insider, A.3.5 FRED+FINRA, A.3.7 OpenBB revisions, A.3.8 GDELT+pytrends+filing-density+short-interest-delta+revisions, A.3.9 LM tone) into a single `news_activity_score` composite per spec section 7. Swap close-to-close volatility for Yang-Zhang per section 8. Refactor `factors.py` to consume the v2 canonical universe + raw sub-signal tables. Dual-write v1 + v2 outputs so the existing notebook flow keeps working. Stand up the equivalence harness (the assertion threshold lands in A.5).

**Architecture:** Additive on outputs (no v1 columns removed); refactored on inputs (factors.py now takes canonical_df + raw_* dataframes instead of a single Finviz df).

A.3.10 ships:

1. **Yang-Zhang realized vol** — `src/methodology/yang_zhang_vol.py` (pure function, spec section 8).
2. **`news_activity.py` composite** — `src/layer1_universe/news_activity.py`. Reads `raw_edgar_insider`, `raw_finra`, `raw_openbb`, `raw_gdelt`, `raw_edgar_filings`, `raw_edgar_filing_tone`, `raw_pytrends`; calls each methodology module; renormalizes weights over surviving sub-signals.
3. **factors.py refactor** — new signature `compute_factor_scores(canonical_df, raw_inputs, weights)`. Adds `news_activity_score` column. Swaps `compute_lowvol_score`'s realized-vol estimator from close-to-close to Yang-Zhang when OHLCV data is available; falls back to close-to-close otherwise (no crash on missing data).
4. **`config/scoring.yaml`** — adds `news_activity_score: 0.10` (long screen) and `0.15` (short screen) to the composite weights per spec §9.3. Adds `news_activity_subweights` block per §12.1 (initial weights from §7.3). Adds `realized_vol: {estimator: yang_zhang, window_days: 60}` block.
5. **Dual-write** — `DatabaseManager.save_layer1_outputs(run_id, canonical_df, candidates_df)`. Calls existing `save_universe` (v1 universe_members) and existing `save_candidates` (v1 candidate_results); calls `insert_canonical_universe` (v2). v1 stays the single source of truth until A.5 cuts over.
6. **Equivalence harness** — `src/layer1_universe/equivalence_harness.py`. Produces `equivalence_report(run_id) -> dict` and writes `data/runs/<run_id>/equivalence_report.json`. Reports max_abs_drift + n_disagreements per shared column. A.3 produces; A.5 sets the assertion threshold.
7. **Integration test** — `tests/integration/test_a3_end_to_end.py` with `@pytest.mark.integration`. Seeds a small synthetic dataset across all raw_* tables, runs the full Layer 1 pipeline end-to-end (no live network), asserts canonical_universe gets populated AND news_activity_score is computed AND dual-write produces equivalent v1 outputs.

**Spec references:** §7 (composite), §8 (Yang-Zhang), §9 (factors refactor), §10 (dual-write), §11 (equivalence), §12.1 (scoring.yaml), §13.4 + §13.5 (tests), §15 row A.3.10, §16 Definition-of-Done, §18 entry 13.

**Methodology references:**

| Component | Citation | Mechanism |
|---|---|---|
| Yang-Zhang volatility | Yang, D. & Zhang, Q. (2000). "Drift-Independent Volatility Estimation Based on High, Low, Open, and Close Prices." J. Business 73(3). | Minimum-variance unbiased GBM estimator. Uses OHLC; ~14x more efficient than close-to-close. Robust to overnight gaps. |
| 7-signal composite weighting | Initial weights from spec §7.3, derived from literature alpha estimates. Recalibration deferred to Phase H. | Linear weighted sum with NaN-renormalization: drop missing sub-signals, rescale surviving weights to sum to 1. |
| Sign convention | Spec §7.2. | Positive score == bullish for every sub-signal (FEARS internally sign-flips inside fears_signal.py per Da-Engelberg-Gao). |

---

## Wave 1 — Yang-Zhang vol module

- [x] Implement `src/methodology/yang_zhang_vol.py` (pure function `yang_zhang_vol(ohlc_df, window_days=60) -> pd.Series`)
- [x] Write `tests/methodology/test_yang_zhang_vol.py` (10 tests: GBM recovery, zero-variance, insufficient history, statistical efficiency vs close-to-close, column normalization, ValueError paths)
- [x] Commit Wave 1

## Wave 2 — news_activity composite + scoring.yaml

- [x] Implement `src/layer1_universe/news_activity.py` with `compute_news_activity_score`, `opportunistic_insider_score` (the per-ticker aggregator over Cohen-Malloy-Pomorski classifier output), `_renormalize_and_sum` (NaN-aware), and `load_news_activity_inputs(db, ...)` DB shape-adapter. `NEWS_ACTIVITY_VERSION = "1.0"`. Default subweights from spec §7.3.
- [x] Extend `config/scoring.yaml` with rebalanced long / short screens (news_activity_score 0.10/0.15 added) + `realized_vol` block (Yang-Zhang, 60d) + `news_activity_subweights` block.
- [x] Write `tests/layer1_universe/test_news_activity.py` (20 tests: opportunistic-insider aggregator, renormalisation, composite single-signal, ticker-absent NaN, empty iterables, DB shape-adapter keys + ticker filter)
- [x] Commit Wave 2

## Wave 3 — factors.py refactor + lowvol Yang-Zhang swap

- [x] Add NEW top-level `compute_factor_scores(canonical_df, *, raw_inputs, weights, prices_df, market_returns, ohlcv_by_ticker, as_of_date) -> pd.DataFrame`. Returns the six factor sub-scores + `news_activity_score` column.
- [x] Extend `compute_lowvol_score` with optional `ohlcv_by_ticker` + `vol_window_days` kwargs -- Yang-Zhang vol when OHLCV is provided per ticker, close-to-close fallback otherwise. Backwards-compatible signature.
- [x] Write `tests/layer1_universe/test_factors_v2.py` (5 tests: minimal canonical, raw_inputs->news_activity, prices->momentum, OHLCV path triggers YZ, custom subweights override). Full suite: 873/873 green.
- [x] Commit Wave 3

## Wave 4 — dual-write + equivalence harness + integration test

- [x] Add `DatabaseManager.save_layer1_outputs(run_id, canonical_df, v1_universe_df, v1_candidates_by_playbook) -> dict[str, int]` per spec §10.
- [x] Implement `src/layer1_universe/equivalence_harness.py` with `equivalence_report(run_id, db)` + `write_equivalence_report(run_id, db, out_path)`. v1<->v2 column alias map for the comparable subset; max_abs_drift + n_disagreements per column; status ok/partial/no_data; JSON output under `data/runs/<run_id>/`.
- [x] Write `tests/integration/test_a3_end_to_end.py` (`@pytest.mark.integration`). Seeds synthetic insider + filings, computes factor pipeline, asserts news_activity_score finite for the seeded ticker, dual-writes to canonical_universe, generates equivalence report. Full suite incl. integration: **876/876 green**.
- [x] Loosened pre-existing stale assertion in `tests/datasources/test_integration_health.py` (`== 6` -> `>= 6` since registry now has 8 sources post-A.3.8).
- [x] Update the build plan §5.1.0 row to mark A.3.10 + Phase A.3 COMPLETE.
- [x] Update spec §16 DoD checkboxes.
- [x] Commit Wave 4 + finalize

---

## Scope guardrails

- **No A.4 work.** Notebook drilldown is A.4.
- **No A.5 work.** The equivalence harness PRODUCES a report; A.5 sets the assertion threshold and gates the v1 retirement.
- **No new sources.** A.3.10 wires existing A.3.1-A.3.9 substrates into a composite.
- **No factor re-calibration.** Literature weights from spec §7.3 + §9.3 are v1; Phase H empirically recalibrates.

## Out of scope (Phase B+)

- Layer 2 thesis aggregation (consumes news_activity_score as a prior).
- Layer 3 options Greeks / IV (Yang-Zhang vol is the realized prior, but Layer 3 logic itself is later).
- Phase H factor-weight recalibration against trade history.
