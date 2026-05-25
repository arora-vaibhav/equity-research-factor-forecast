# Phase A.3.9 — Loughran-McDonald Tone Signal

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`. Steps use `- [ ]` checkbox syntax for tracking.

**Goal:** Land sub-signal 6 of the seven-signal `news_activity_score` composite (spec §7.1 row 6, §7.2 signal 6, §18 entry 12) — Loughran-McDonald textual tone scored on the *Risk Factors* (Item 1A) section of each 10-K / 10-Q filing, then z-scored against the prior 4 filings of the same ticker (tone-shift signal). Pure-function module mirrors the A.3.8 template.

A.3.9 ships:

1. **LM master dictionary asset** — `config/lm_dictionary/LoughranMcDonald_MasterDictionary.csv` (committed; 86,553 master words; 3,876 with one or more finance-sentiment tags). Sourced from sraf.nd.edu (Loughran-McDonald 2024 release). One-line `README.md` documenting source URL, citation requirement, and the academic-research license clause.
2. **Dictionary loader** — `src/methodology/lm_dictionary.py`:
   - `load_lm_dictionary(path) -> dict[str, frozenset[str]]` where the value is the subset of `{positive, negative, uncertainty, litigious, strong_modal, weak_modal, constraining}` for which the dictionary marks the word non-zero. Words with no tag are *excluded* (~82k of 86k rows).
   - Module-level `@lru_cache` so repeated `load_lm_dictionary(path)` calls are free.
   - `LM_DICTIONARY_VERSION = "2024"`.
3. **Schema v11 → v12 migration** — adds `raw_edgar_filing_tone` (long-format: one row per scored filing) and `RawEdgarFilingToneRow` Pydantic. Idempotent. Bumps `schema_version` to 12. No existing tables touched.
4. **Tone scorer** — `src/methodology/lm_tone_signal.py` (pure):
   - `tokenize(text) -> list[str]` — lowercase, strip non-`[A-Z']` characters, simple whitespace tokenizer. Matches the dictionary's word forms (the LM CSV is uppercase, plain English words).
   - `score_tone(text, dictionary) -> dict[str, int | float]` returning `{n_positive, n_negative, n_uncertainty, n_litigious, total_words, net_tone}` where `net_tone = (n_positive − n_negative) / total_words` per spec §7.2 signal 6.
   - `compute_lm_tone_shift(filing_rows, *, as_of_date, baseline_n=4) -> dict[ticker, float]` — for each ticker, z-score the most-recent filing's `net_tone` against the prior `baseline_n` filings. NaN if fewer than 4 prior baselines (literature default; Loughran-McDonald 2014 use 4–5 prior filings).
   - `LM_TONE_VERSION = "1.0"`.
5. **EDGAR full-text fetcher** — `src/common/datasources/edgar_full_text.py`:
   - `fetch_filing_text(primary_doc_url, *, headers, session=None) -> str` — tenacity-retried SEC GET. Same `_sec_get_with_retry` pattern as `edgar_source.py` (4-attempt exponential backoff, 0.5–8s). Plain `requests`; no BeautifulSoup unless extraction needs it.
   - `extract_risk_factors_section(html_or_text) -> tuple[str, str]` — returns `(section_text, extraction_status)` where status ∈ `{'item_1a_found', 'full_document_fallback', 'empty_document'}`. Permissive policy (decision 2026-05-22): if Item 1A markers aren't found, score the full document body and flag `'full_document_fallback'`. Implementation:
     - Normalize HTML → text via `BeautifulSoup(..., 'html.parser').get_text(' ')`.
     - Regex for `Item 1A` start (case-insensitive, optional period, optional whitespace, optional "Risk Factors" heading).
     - Regex for the section end — first of Item 1B, Item 2, or document end.
     - If start marker missing → return whole text with `'full_document_fallback'`.
     - If start marker present but end marker missing → return text from start to document end with `'item_1a_found'`.
6. **EdgarSource integration** — extend `src/common/datasources/edgar_source.py`:
   - `fetch_filing_text_for_ticker(ticker, run_id, db, *, form_types={'10-K','10-Q'}, max_filings=None) -> int` — selects rows from `raw_edgar_filings` for this ticker where `form_type ∈ form_types` AND `(cik, source_filing_accn)` not already in `raw_edgar_filing_tone`, oldest first, fetches each `primary_doc_url`, extracts Item 1A (permissive), scores, persists, advances the `(edgar, ticker, filing_tone)` watermark to the latest filing_date scored. Returns count of new rows inserted.
   - Adds `'lm_tone'` to `EdgarSource.provides` set.
7. **Orchestrator routing** — `src/common/orchestrator/fetch_orchestrator.py` `_route_field_to_call`: add `field == 'lm_tone' → fetch_filing_text_for_ticker`. No new provider quota (SEC EDGAR quota already governs this source).
8. **Tests:**
   - `tests/methodology/test_lm_dictionary.py` — loader returns expected tag counts on real CSV; idempotent caching; raises `FileNotFoundError` cleanly.
   - `tests/methodology/test_lm_tone_signal.py` — tokenizer edge cases (punctuation, contractions, numerics); `score_tone` math against hand-built mini-dictionary; `compute_lm_tone_shift` z-score correctness; NaN when baseline < 4; sign convention (positive z == net_tone rising == bullish per spec §7.2 row 6).
   - `tests/datasources/test_edgar_full_text.py` — Item 1A extraction on three fixtures: clean 10-K (start + end markers), 10-Q with Item 1A but no Item 1B end, document without Item 1A (fallback path). Retry path via `responses`.
   - `tests/datasources/test_edgar_filing_tone.py` — end-to-end mock: pre-seed `raw_edgar_filings`, run `fetch_filing_text_for_ticker`, assert tone rows persisted + watermark advanced + INSERT OR IGNORE on re-run.
   - `tests/common/test_migrate_to_v12.py` — idempotency + v11 data preservation.
   - `tests/common/test_registry_a3_9.py` — `edgar.provides` contains `lm_tone`; resolver routes `lm_tone` → EdgarSource.
   - `tests/common/test_orchestrator_routing_a3_9.py` — orchestrator dispatches `field='lm_tone'` to `fetch_filing_text_for_ticker`.

**Why this matters:** LM tone is the second-highest weighted news-activity sub-signal (0.15 per spec §7.3, tied with short-interest delta and revisions; only the opportunistic-insider signal weighs higher at 0.30). Empirical literature (Loughran-McDonald 2011 JF; Garcia-Norli 2018) finds tone-shift z-scores in 10-K/10-Q Risk Factors carry ~3–5 bps/mo information after controlling for fundamentals, with the value concentrated in *changes* relative to a firm's own baseline (hence the 4-filing rolling z-score rather than absolute level).

**Architecture:** Additive only. Migration v11 → v12 creates `raw_edgar_filing_tone` and bumps `schema_version`. No existing tables touched. New runtime dep: **`beautifulsoup4>=4.12`** (already in `requirements.txt` from A.3.6 stockanalysis parser — re-used, not re-added).

**Critical scope boundary — no factor-scoring integration in A.3.9.** Like A.3.8, this is "ingredients not the meal." Composition into `news_activity_score` and integration with `factors.py` ships in A.3.10. If you find yourself editing `src/layer1_universe/factors.py` you are out of scope.

**Sign convention:** Positive z-score == net tone *rising* relative to the firm's prior 4 filings == bullish, consistent with the rest of the composite. The negative loading from "lots of new negative words" is captured naturally: when a filing introduces more negative words than prior filings, `net_tone` *falls* (more negative count → smaller `(pos − neg)` numerator), so the z-score is negative, so the signal flags bearish.

**Tech Stack:** Python 3.11+, `requests`, `pydantic` v2, `pytest`, `pytest-mock`, `beautifulsoup4` (re-use), `tenacity` (re-use). No other new deps.

**Spec reference:** [docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md](../specs/2026-05-21-phase-a3-layer1-hardening-design.md) §7.1 row 6, §7.2 signal 6, §12.3 (LM dictionary asset path), §14 error matrix row "LM dictionary missing", §15 row A.3.9, §18 entry 12.

**Methodology references:**

| Component | Citation | Mechanism |
|---|---|---|
| Finance-specific sentiment dictionary | Loughran, T. & McDonald, B. (2011). "When Is a Liability Not a Liability? Textual Analysis, Dictionaries, and 10-Ks." *Journal of Finance* 66(1). | General sentiment dictionaries (Harvard IV-4, Diction) misclassify ~75% of negative words in 10-Ks (e.g., "tax", "liability", "cost" → neutral in finance). LM built a hand-curated finance-specific replacement. |
| Dictionary update + Constraining category | Loughran, T. & McDonald, B. (2014). "Measuring Readability in Financial Disclosures." *Journal of Finance* 69(4); + 2024 release. | Adds Constraining category for words signalling restrictive covenants. |
| Tone-shift z-score | Garcia, D. & Norli, Ø. (2018). "Local Information in Stock Markets." *RFS* 31(7); Loughran-McDonald 2014. | Levels of net tone are dominated by firm/industry fixed effects; *changes* from a firm's own baseline carry the cross-sectional information. 4-filing baseline is the literature-standard short window (also used in MD&A tone-shift papers). |
| Item 1A Risk Factors as text source | Brown, S. V. & Tucker, J. W. (2011). "Large-Sample Evidence on Firms' Year-over-Year MD&A Modifications." *JAR* 49(2); SEC FRR §503. | Item 1A introduced 2005 (SEC FRR §503); contains forward-looking risk disclosures with the highest density of negative LM words across the 10-K. Empirically gives the cleanest tone signal versus other 10-K sections. |

**Reference data shape — LoughranMcDonald_MasterDictionary CSV:**

| Column | Type | Description |
|---|---|---|
| Word | str (uppercase) | The lexeme |
| Seq_num | int | Position in sorted word list |
| Word Count, Word Proportion, Average Proportion, Std Dev, Doc Count | float | Corpus-frequency stats (not used by us) |
| Negative, Positive, Uncertainty, Litigious, Strong_Modal, Weak_Modal, Constraining | int (year-added or 0) | Non-zero = word is in that category. We treat the non-zero binary only. |
| Complexity, Syllables, Source | misc | Not used by us |

The 7 tag columns map to our 7 internal tag names (lowercased): `negative`, `positive`, `uncertainty`, `litigious`, `strong_modal`, `weak_modal`, `constraining`. Spec §7.2 only requires positive + negative for `net_tone`; we still store the other counts in `raw_edgar_filing_tone` for future use (Layer 2 thesis can read litigious / uncertainty counts as auxiliary inputs).

**`raw_edgar_filing_tone` schema:**

| Column | Type | Notes |
|---|---|---|
| run_id | TEXT NOT NULL | Run that scored this filing |
| ticker | TEXT NOT NULL | Uppercase |
| cik | TEXT NOT NULL | 10-digit zero-padded |
| source_filing_accn | TEXT NOT NULL | EDGAR accession number, no dashes |
| form_type | TEXT NOT NULL | '10-K' / '10-Q' (or '10-K/A' etc.) |
| filing_date | TEXT NOT NULL | YYYY-MM-DD |
| n_positive | INTEGER NOT NULL | |
| n_negative | INTEGER NOT NULL | |
| n_uncertainty | INTEGER NOT NULL | |
| n_litigious | INTEGER NOT NULL | |
| total_words | INTEGER NOT NULL | Tokenized count |
| net_tone | REAL NOT NULL | `(n_positive − n_negative) / total_words` |
| extraction_status | TEXT NOT NULL | 'item_1a_found' / 'full_document_fallback' / 'empty_document' |
| lm_dictionary_version | TEXT NOT NULL | LM_DICTIONARY_VERSION ("2024") |
| scrape_timestamp | TEXT NOT NULL | ISO-Z |

**PRIMARY KEY:** `(cik, source_filing_accn)` — one tone row per filing per dictionary version. Re-runs with the same dictionary version are no-ops via INSERT OR IGNORE; future dictionary upgrades insert new rows under the new version.

---

## Wave 1 — plan + LM dictionary + loader

- [x] Write this plan doc (you are here)
- [x] Download `LoughranMcDonald_MasterDictionary.csv` → `config/lm_dictionary/` (DONE 2026-05-22; 9.1 MB, 86,553 rows, 3,876 tagged)
- [ ] Write `config/lm_dictionary/README.md` (source URL, citation, license clause)
- [ ] Implement `src/methodology/lm_dictionary.py` (loader + LM_DICTIONARY_VERSION)
- [ ] Write `tests/methodology/test_lm_dictionary.py` (loader, tag counts, idempotent cache, FileNotFoundError)
- [ ] Run `pytest tests/methodology/test_lm_dictionary.py -q` → all green
- [ ] Commit Wave 1

## Wave 2 — schema v12 + tone scorer + full-text fetcher

- [x] Add `RawEdgarFilingToneRow` to `src/common/schemas.py`
- [x] Add `migrate_to_v12` to `src/common/database.py` (PK includes lm_dictionary_version; preserves v11 data)
- [x] Add `insert_raw_edgar_filing_tone` helper to `DatabaseManager`
- [x] Write `tests/common/test_database_a3_9.py` (13 tests: migration + insert helper)
- [x] Implement `src/methodology/lm_tone_signal.py` (tokenize, score_tone, compute_lm_tone_shift, LM_TONE_VERSION)
- [x] Write `tests/methodology/test_lm_tone_signal.py` (25 tests: tokenizer / score math / z-score / sign convention)
- [x] Implement `src/common/datasources/edgar_full_text.py` (fetch_filing_text + extract_risk_factors_section)
- [x] Write `tests/datasources/test_edgar_full_text.py` (22 tests: extraction + HTTP retry path)
- [x] Commit Wave 2

## Wave 3 — EdgarSource integration + orchestrator wiring

- [x] Add `fetch_filing_text_for_ticker` to `EdgarSource`
- [x] Add `'lm_tone'` to `EdgarSource.provides`
- [x] Update `src/common/orchestrator/fetch_orchestrator.py` `_route_field_to_call` for `lm_tone`
- [x] Write `tests/datasources/test_edgar_filing_tone.py` (end-to-end mock, 6 tests)
- [x] Write `tests/common/test_registry_a3_9.py` (4 tests)
- [x] Write `tests/common/test_orchestrator_routing_a3_9.py` (3 tests)
- [x] Run full pytest (non-integration) → 838 / 838 green
- [x] Commit Wave 3

## Finalize

- [x] Update the build plan §5.1.0 A.3 row + plan-link list to mark A.3.9 shipped
- [x] Commit build-plan update

---

## Decisions captured (approvals 2026-05-22)

| Decision | Choice | Why |
|---|---|---|
| LM dictionary source | Committed in repo at `config/lm_dictionary/LoughranMcDonald_MasterDictionary.csv` | Local-first per build-plan §3.5; one-time 9 MB; refreshed yearly only. |
| Item 1A extraction mode | Permissive (fallback to full document when markers missing) | Maximize signal coverage; older filings + layout variation make strict matching brittle. Status flag preserves auditability. |

## Out of scope (A.3.10 territory)

- `news_activity.py` composite that combines all 7 sub-signals.
- `factors.py` integration of news_activity_score.
- Renormalization of sub-signal weights when LM tone is NaN (lives in `news_activity.py` per spec §7.4).
- Dual-write equivalence harness.
