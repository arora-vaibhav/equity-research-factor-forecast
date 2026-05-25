# Methodology Handbook — Code ↔ Finance ↔ Why

**Purpose:** A running, plain-English explainer that bridges what was built (code), what it does in finance terms (the concept), and why it matters (the alpha/risk argument). Updated with each phase. Written so a non-coder with a finance interest can follow.

Entries are added in build order. Each entry follows the same template.

---

## Entry template (delete this section once the handbook has 5+ real entries)

```
## [Phase or component label] — [Plain-English title]

**What it is in finance.** 2-4 sentences explaining the concept in everyday language. What real fund analysts call this. What it answers about the market.

**The formula or rule in plain English.** Math expressed verbally first, formula second.

**Why we use it.** What signal it captures. Estimated alpha (basis points/month) or risk-reduction value, with source. When it works best / when it fails.

**Where in code.** Specific file paths + a one-line summary of what those files do with this concept.

**Reference.** Academic citation(s) (year, journal where possible) and any OSS implementation we drew from.

**Confidence.** High / Medium / Low — based on replication strength + how directly we apply the literature.
```

---

## Entries (filled as build progresses)

### A.1 — Multi-source data adapter framework (shipped)

**What it is in finance.** Bloomberg-, FactSet-, and S&P-style platforms internally pull the same data field (e.g., a company's P/E ratio) from multiple underlying sources and decide which value to "publish" as canonical. They do this to catch data errors, vendor outages, and to score how confident they are in any given data point. This system does the same with free sources (Finviz, Yahoo, EDGAR, FRED, stockanalysis, FINRA).

**The rule in plain English.** For each numeric field on each ticker, every source's value is collected. They are blended using a "priority-weighted average": the most-trusted source (e.g., SEC EDGAR for fundamentals) gets the highest weight; secondary sources (e.g., Yahoo, Finviz) get smaller weights that decay geometrically. The result is a "canonical value" that's more robust than any single source. For non-numeric fields like sector classification, the highest-priority source's value wins.

**Formula sketch.**
- For a numeric field with sources s₁, s₂, …, sₙ (sorted by priority), each source's raw weight is `decay^rank` where rank=1 is highest priority and decay=0.5. So rank-1 gets 0.5, rank-2 gets 0.25, rank-3 gets 0.125, etc.
- Normalize: weight_i = raw_weight_i / sum(all raw weights for sources that have a value)
- Canonical value = sum(weight_i × value_i)
- For categorical fields: pick the value from the lowest-rank source that has data.
- For snapshot fields (like price): the most recent value wins, ties broken by priority rank.

**Why this matters.**
- **Data error detection.** If three sources say P/E is 18 and one says P/E is 180, the outlier gets dampened in the average but flagged in the audit trail. The 180 is almost certainly bad data.
- **Vendor outage resilience.** If Yahoo's API goes down on a Tuesday, the canonical value still computes from the surviving sources.
- **Cross-validation as a soft quality signal.** The `data_quality_score` field (range 0-1) measures how much sources agreed for a given ticker. Note: this is engineering folklore, not academic alpha — used as an integrity audit, not as a Bayesian prior input to ranking. (See [financial-methodology-reference.md](financial-methodology-reference.md) for the honest caveat.)

**Where in code.**
- `src/common/datasources/base.py` — `BaseDataSource` abstract class
- `src/common/datasources/registry.py` — `DataSourceRegistry` orchestrates pulls
- `src/common/datasources/resolution.py` — the `weighted_average_value`, `highest_priority_value`, `freshest_value` functions implement the blending rules
- `config/datasources.yaml` — per-field priority lists and weight decay

**Reference.** No single academic paper underpins multi-vendor data blending — it's a Bloomberg/FactSet-internal engineering practice. The closest formal treatment is the "data quality dimensions" literature (Wang & Strong 1996). The priority-weighted approach is operationally inspired by what large hedge fund data teams do, validated against the Phase A.1 acceptance integration test (all 6 sources successfully returned data; deliberate-outlier test lands in A.5).

**Confidence.** High for the engineering value (catches errors, survives outages). Low-to-medium for treating disagreement as an alpha signal — explicitly not used that way.

---

### A.2 — Persistent storage schema v2 (shipped)

**What it is in finance.** Every quantitative trading platform needs a database that grows over time as a permanent asset. The Bloomberg terminal in front of a portfolio manager isn't pulling 30 years of price history fresh every time — that data sits on Bloomberg's servers, accumulated permanently. The equivalent here: a local SQLite database that, once a price or filing or fundamental snapshot is stored, never gets re-fetched from the upstream source.

**The principle in plain English.** "Pull new data once; reconcile and store; never re-pull what you already have." This protects against four real risks:
1. Free API rate limits (Polygon at 5 requests/minute will throttle aggressively if history is re-pulled)
2. Data on third-party servers disappearing (Yahoo Finance has changed APIs three times in 5 years)
3. Cost escalation if upgrading to paid tiers (re-pulling 5 years of daily data for 500 tickers is millions of API calls)
4. Reproducibility — research today should be reproducible from the same data tomorrow, even if the upstream provider changed

**How it works (A.2 storage; A.3 wires the delta-fetch logic).**
- Every source records "I've fetched (ticker, field) up to date X" in a `fetch_watermarks` table
- Before any pull, sources query the watermark and ask only for data after that date
- Time-series tables use `INSERT OR IGNORE` (not `INSERT OR REPLACE`) — duplicate observations are silently dropped, never overwritten
- A historical observation, once stored, is immutable. Re-fetching requires explicit operator action that's logged.

**Where in code.**
- `src/common/database.py` — `DatabaseManager` class with `migrate_to_v2()`, schema version stamps
- `src/common/schemas.py` — Pydantic models for every table

**Reference.** This is a "data engineering 101 for finance" practice, not an academic concept. Closest formal source: Inmon (1992) "Building the Data Warehouse" — the idea that operational data should accumulate immutably rather than overwrite.

**Confidence.** High — this is uncontroversial engineering best practice for financial data systems.

---

### A.3.1 — Persistent data accumulation in action (shipped 2026-05-21)

**What it is in finance.** Real trading desks never re-download data they already own. Bloomberg, FactSet, and S&P keep decades of price history sitting on their own servers — they only ask their data providers for *new* data each day. This system does the same with a local SQLite database. Once a day's closing price for AAPL is stored, Yahoo is never asked for it again — only tomorrow's price gets added tomorrow.

**The principle in plain English.** Three rules:
1. **Track what's already held.** A small bookkeeping table called `fetch_watermarks` records "AAPL prices stored through 2026-05-20" — one row per (source, ticker, data type). Before any fetch, this table is queried for the remaining gap, then the upstream provider is asked only for the missing days.
2. **Never overwrite.** When the database tries to insert a price row that already exists (same ticker, same date), SQLite silently drops the new one. The first value ever stored is preserved forever. This is what "INSERT OR IGNORE" means — it's a one-word policy that protects historical data integrity automatically.
3. **Operator-only force-overwrite.** If a wipe-and-re-pull is needed (say, Yahoo had bad data for a week), a single explicit command — `force_refetch()` — deletes rows from a date forward and resets the watermark. It logs every use in an audit table. No code path can trigger it automatically; only the operator can.

**Why this matters.**
- **API quota protection.** Free-tier API keys (Polygon at 5 req/min, FMP at 250/day) would burn through their limit in minutes if history were re-pulled every run.
- **Cost protection.** On any paid data tier, every pull costs money. This makes those costs predictable: pay once per data point, ever.
- **Reproducibility.** Research today is reproducible from the same data tomorrow, even if the upstream provider changes their API or removes old history (Yahoo has rewritten their API 3 times in 5 years).
- **Trust.** Once stored, a historical observation is locked. It is impossible to accidentally regenerate the past — important for honest backtesting later.

**Where in code.**
- `src/common/database.py` — `DatabaseManager.migrate_to_v3()` creates the `fetch_watermarks` table; `get_watermark`/`upsert_watermark`/`force_refetch` are the bookkeeping methods.
- `src/common/datasources/base.py` — every data source inherits `get_fetch_gap()` (asks "what is still needed?") and `update_watermark()` (records "this was just fetched").
- `src/common/env_loader.py` — reads `.env` for API keys without hardcoding them; gracefully degrades if a provider's key is missing.
- `config/api_keys.yaml` — maps friendly provider names ("fmp", "polygon") to environment variable names.

**Reference.** This is data-engineering best practice rather than an academic concept. The closest formal source is Inmon (1992) "Building the Data Warehouse" — the idea that operational data should accumulate immutably rather than overwrite.

**Confidence.** High. This is uncontroversial; every serious quant platform does it. The novel part is having built it in-house rather than paying $10K/year for a vendor to do it.

---

### A.3.2 — Finviz + Yahoo: first real data flows (shipped 2026-05-21)

**What it is in finance.** Two of the seven free data sources now actually pull data. They're complementary: Finviz tells me *which* stocks exist and are big enough to trade options on; Yahoo provides the per-ticker depth (current fundamentals + 5 years of daily prices). Together they answer "what's the investable universe today, and what is known about each name?"

**How Finviz works here.**
- Finviz's screener is queried for "mega-cap stocks" (market cap ≥ $200B). This gives ~70-100 names — Apple, Microsoft, Berkshire, Aramco ADRs, etc. (Note: this is intentionally narrow for v1; later sub-phases will widen to large-cap and mid-cap as the system proves out.)
- Finviz returns a table with one row per stock and ~80 columns of pre-computed fundamentals + technicals (P/E, RSI, % from 52-week high, etc.).
- Every row is stamped with a `run_id` (identifying which weekly batch produced it) and a UTC timestamp, then the table is returned.
- The table is written to a SQLite table called `finviz_universe_history` (accessed through a `raw_finviz` view) — this is the audit trail of every Finviz pull.

**How Yahoo works here.**
- Yahoo has no "scan all stocks" endpoint, so the ticker list Finviz just produced is fed in.
- For each ticker, `yfinance.Ticker(ticker).info` returns a Python dict with ~150 fields (market cap, P/E ratios, sector, profit margins, etc.). 18 fields of interest are extracted into a structured `RawYahooRow`.
- Free cash flow isn't a direct yfinance field, so it is derived: `FCF = Operating Cash Flow − |Capital Expenditures|`. (yfinance reports capex as a negative number, so the absolute value is taken.)
- For prices, `yf.Ticker(ticker).history(start=..., end=...)` is called — but critically, the watermark from A.3.1 is used to figure out exactly which dates are needed. If AAPL is already stored through 2026-05-20, only 2026-05-21 onward is requested. **This is what makes the system efficient at scale.**
- All prices land in `historical_price` with INSERT OR IGNORE, so duplicate days are silently dropped.

**Why this is the foundation of everything else.**
- Layer 1 (the weekly screen) reads from these tables to compute factor scores.
- Layer 2 (thesis building) reads fundamentals + prices to compute realized volatility and identify catalysts.
- Layer 3 (options analysis) uses the same historical prices to compare implied volatility to realized.
- Layer 4 (Bayesian ranking) treats 5 years of accumulated history as the "prior" data for its probability models.
- None of those layers can work without these two sources flowing reliably. They're the floor.

**Where in code.**
- `src/common/datasources/finviz_source.py` — `fetch_universe(run_id)` wraps `finvizfinance.screener.Overview` with a mega-cap filter.
- `src/common/datasources/yahoo_source.py` — three methods now do real work:
  - `fetch_universe(run_id, ticker_list)` batches `.info` calls over a list of tickers
  - `fetch_fundamentals_for_ticker(ticker, run_id)` does one ticker
  - `fetch_historical_price(ticker, db)` is watermark-aware; pulls only the gap
- `src/common/database.py` — `migrate_to_v4()` creates the `raw_yahoo` table; `insert_raw_yahoo()` writes a batch of fundamentals.

**Reference.** These are operational integrations rather than academic concepts. The closest academic anchor for the "wide screen + per-ticker deep dive" pattern is the institutional investment-process literature (Damodaran "Investment Valuation" textbook style) — the same funnel every fund's portfolio team uses.

**Confidence.** High. Both Finviz and yfinance are battle-tested in retail-quant circles. The only real risk is upstream API changes (Yahoo has done 3 in 5 years); the test suite mocks both libraries so breaking changes get caught the moment fresh fixtures are run.

---

### A.3 entries forthcoming (added per sub-task as later A.3 sub-phases ship)

- A.3.3 — XBRL parsing for SEC fundamentals
- A.3.4 — Cohen-Malloy-Pomorski opportunistic-insider classifier ★ (highest-alpha addition: 82 bps/month value-weighted alpha)
- A.3.5 — Treasury yield curve as macro signal (FRED) + FINRA short-interest data
- A.3.6 — 10-year ratio history & percentile context
- A.3.7 — OpenBB multi-provider router architecture
- A.3.8 — Short-interest delta (Diether-Lee-Werner) / analyst revisions (Chan-Jegadeesh-Lakonishok) / news volume (Tetlock 2007) / 8-K filing density (Lee-So 2017) / FEARS contrarian signal (Da-Engelberg-Gao)
- A.3.9 — Loughran-McDonald tone (why standard sentiment dictionaries fail in finance)
- A.3.10 — Yang-Zhang volatility (why not close-to-close) + composite scoring + weight renormalization on missing signals

---

## How to read this handbook

- Start at the top; entries are in build order
- Each entry stands alone — readers can jump to just the parts that interest them
- "Formula in plain English" comes BEFORE the math notation, so the intuition lands first
- "Confidence" indicates whether to trust the signal as alpha-generating (high), useful-but-not-validated (medium), or honestly speculative (low)
- "Where in code" gives the exact files to read for tracing

The handbook is a teaching document, not a reference manual.
