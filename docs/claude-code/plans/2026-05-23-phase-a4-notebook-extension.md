# Phase A.4 — Notebook Extension (Source Status / Provenance Summary / Per-Ticker Drilldown)

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`. Step-by-step deliverables.

**Goal:** Extend the notebook surface with three new panels backed by the v2 canonical universe + raw_* tables landed in A.3.1-A.3.10:

1. **Source Status panel** — at-a-glance health of every registered source: name, enabled, last_fetched_at, last_observation_date, fetch_count, error_count, last_error_message. Joins `fetch_watermarks` and rolls up the latest watermark per `(source, field)` pair.
2. **Provenance Summary panel** — per-(field, source) coverage: how many tickers have a value, when last refreshed, drift between vendors when multiple sources cover the same field. Backed by `field_provenance` when populated, falling back to per-source raw_* counts otherwise.
3. **Per-Ticker Drilldown panel** — accepts a ticker, returns a dict of frames showing every raw_* row + the canonical_universe slice + the factor scores + the news_activity sub-signal breakdown. The "show me everything we know about AAPL right now" view.

Plus two notebook control variables:
- `ENABLED_SOURCES` — list overriding `config/datasources.yaml > enabled_sources` for the current run.
- `FORCE_REFRESH_SOURCES` — list bypassing the watermark short-circuit for the current run.

**Architecture:** Helper functions live in `src/layer1_universe/notebook_panels.py` so they're unit-testable. The notebook itself is a thin caller. New notebook: `notebooks/layer1_v2_drilldown.ipynb` (additive; the existing `layer1_control.ipynb` stays untouched).

**Acceptance:** notebook smoke-test runs end-to-end against a fixture dataset (seeded raw_* tables in tmp_path) without errors AND each panel returns a non-empty pandas object.

**Spec reference:** build plan §5.1.0 row A.4; no design spec section (A.4 was "to be written after A.3 lands" -- this plan IS the design).

---

## Wave 1 — `notebook_panels.py` + unit tests

- [ ] Implement `src/layer1_universe/notebook_panels.py`:
  - `source_status(db, *, registry=None) -> pd.DataFrame` -- one row per (source, field) from `fetch_watermarks`, joined with `enabled_sources()` for the enabled flag.
  - `provenance_summary(db, *, run_id=None) -> pd.DataFrame` -- per-raw_* row counts as v1; later versions can join `field_provenance` when populated.
  - `per_ticker_drilldown(db, ticker, *, run_id=None) -> dict[str, pd.DataFrame]` -- returns a dict of per-source slices for the ticker.
  - `apply_notebook_overrides(config, *, enabled_sources=None, force_refresh_sources=None) -> dict` -- shallow-clones a datasources.yaml-shaped dict and overrides the two lists.
- [ ] `tests/layer1_universe/test_notebook_panels.py` covering each function.
- [ ] Commit Wave 1.

## Wave 2 — notebook + fixture smoke test

- [ ] Author `notebooks/layer1_v2_drilldown.ipynb` (JSON-as-code).
- [ ] `tests/integration/test_a4_notebook_smoke.py` (`@pytest.mark.integration`): seeds fixture data, calls each panel, asserts non-empty output.
- [ ] Update the build plan §5.1.0 row A.4 to mark shipped.
- [ ] Commit Wave 2.

## Out of scope

- Live network calls -- notebook + panels operate on whatever is in the SQLite DB at call time.
- `force_refresh_sources` propagating to actual fetchers -- A.4 ships the control variable; the wiring into source-level watermark bypass already exists behind A.3.1's `force_refetch` plumbing.
- A live Jupyter run via nbconvert/papermill -- the smoke test exercises the underlying calls without spinning up a kernel.
