# Equity Research Reporting — Factor Forecast Model (Claude Integrated)

A research-grade equity factor forecast and reporting pipeline. Multi-source data fabric, a methodology library that traces every signal back to the papers, and a three-layer pipeline that ends in a per-ticker HTML report.

![CI](https://github.com/arora-vaibhav/equity-research-factor-forecast-claude/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

---

## What this is

This is a research prototype for systematic equity analysis on US large-caps. It is not a trading system. It does not place orders. It does not size positions. It exists to (a) ingest the same fields a sell-side or factor desk would track, from free public sources, (b) compute the standard factor and microstructure signals against the academic literature, and (c) produce a per-ticker forecast and report that a fundamental analyst could use as an input to their own work.

The pipeline runs in three layers. Layer 1 screens a US large-cap universe on multi-factor scores with waterfall diagnostics — given a universe, who clears the value/quality/momentum/revisions gates, and why. Layer 2 enriches the survivors with a catalyst calendar (earnings, ex-div, filings) and technical setups. Layer 3 forecasts a 5-day return distribution per ticker using an 89-feature LightGBM model with purged walk-forward CV, conditions the forecast on a 3-state Gaussian HMM regime classifier, blends with three baselines in an ensemble, and writes an HTML report styled after a Bloomberg terminal.

The whole pipeline runs on free data sources. Everything that comes out the other end — the methodology, the benchmark numbers, the limitations — is documented under `docs/` with citations. The accuracy numbers in this README are the honest ones, not the marketing ones.

---

## Status and scope

- **Status:** research prototype, not a trading system
- **Does not** place orders, size positions, manage risk, or recommend buys/sells
- **Audience:** quantitative researchers, fundamental analysts using factor screens as inputs, and engineering reviewers evaluating the build process
- **Purpose:** published as a learning artifact that documents the methodology, data engineering, and design discipline behind a multi-source equity research pipeline
- **Calibration:** numbers reported here are point estimates with cross-validation standard deviations on three tickers (AAPL, MSFT, BAC) over five-year daily data; treat as illustrative, not as a portfolio backtest

---

## What's in here

| Layer | Directory | What it does |
|---|---|---|
| Data adapters | `src/common/datasources/` | 13 source adapters: Finviz, Yahoo Finance, SEC EDGAR (XBRL fundamentals + insider Form 4 + full-text + submissions), FRED, FINRA, StockAnalysis, OpenBB router, Polygon options, FMP with yfinance fallback, pytrends (Google search trends), GDELT |
| Orchestration | `src/common/orchestrator/` | Rate-limited orchestrator with per-source quota windows, a priority queue, and per-source budgets |
| Methodology | `src/methodology/` | Signal computations: Loughran-McDonald tone, Yang-Zhang vol, opportunistic insider classifier, news-volume anomaly z-score, filing density, revisions velocity, short-interest delta, FEARS-style sentiment, multi-timeframe technical aggregator, technical indicators, TA signal rules, sector rotation, peer comparison, volume features |
| Layer 1 — universe screen | `src/layer1_universe/` | Multi-factor scoring with waterfall diagnostics (factors, screen, equivalence harness, data quality, news activity, notebook panels) |
| Layer 2 — catalysts | `src/layer2_catalyst/` | Catalyst calendar + technical setups |
| Layer 3 — forecast | `src/layer3_forecast/` | 89-feature LightGBM + HMM regime + ensemble + IC backtest + walk-forward + sensitivity + placebo audit + cross-sectional rank + intraday forecaster + LLM narrative |
| Reports | `reports/` and `src/layer3_forecast/templates/` | Per-ticker HTML via Jinja2, Bloomberg-dark palette, tabular numerics |
| Configs | `config/` | API keys (template), data-source priorities, scoring weights, orchestrator quotas, LM dictionary, GDELT alias |
| Scripts | `scripts/` | `forecast_demo.py` (synthetic, no network), `forecast_live_demo.py` (live data), `benchmark_directional.py`, `daily_assessment.py`, `orchestrator_tick.py`, `polygon_backfill.py` |
| Notebooks | `notebooks/` | `layer1_control.ipynb` (Layer 1 driver), `layer1_v2_drilldown.ipynb`, `smoke_tests.ipynb` |
| Plans and specs | `docs/claude-code/` | ~17 implementation plans and 2 design specs preserved as engineering receipts |
| Methodology docs | `docs/methodology/` | Methodology handbook, factor-models reference, options pricing, volatility analysis, research bibliography |

---

## Architecture

```
                          equity-research-factor-forecast-claude
                          ====================================

         +---------------------- Data layer (src/common) ---------------------+
         |                                                                    |
         |   Finviz   Yahoo   EDGAR   FRED   FINRA   StockAnalysis            |
         |   OpenBB   Polygon   FMP+yf-fallback   pytrends   GDELT            |
         |     |         |       |       |       |          |                 |
         |     v         v       v       v       v          v                 |
         |   +-------------------------------------------------+              |
         |   |          BaseDataSource + Registry             |              |
         |   |   (per-field priority + weighted resolution)   |              |
         |   +-------------------------------------------------+              |
         |                          |                                         |
         |                          v                                         |
         |   +-------------------------------------------------+              |
         |   |    Rate-limited orchestrator                   |              |
         |   |    quota windows + priority queue + budgets    |              |
         |   +-------------------------------------------------+              |
         |                          |                                         |
         |                          v                                         |
         |   +-------------------------------------------------+              |
         |   |   SQLite (database.py + schemas.py)            |              |
         |   |   immutable observations, fetch watermarks     |              |
         |   +-------------------------------------------------+              |
         +---------------------------|----------------------------------------+
                                     |
                                     v
         +---------- Methodology library (src/methodology) -------------------+
         |                                                                    |
         |   Loughran-McDonald tone     Yang-Zhang vol                        |
         |   Opportunistic insider      News-volume anomaly                   |
         |   Filing density             Revisions velocity                    |
         |   Short-interest delta       FEARS sentiment                       |
         |   Technical indicators (RSI/MACD/Boll/ADX/Stoch/ATR/OBV/MFI/CMF)   |
         |   TA signal rules (17 interpreters + TV-style aggregator)          |
         |   Multi-timeframe aggregator                                       |
         |   Sector rotation            Peer comparison (FF + AFP)            |
         |   Volume features                                                   |
         +---------------------------|----------------------------------------+
                                     |
            +------------------------+------------------------+
            |                        |                        |
            v                        v                        v
   +-----------------+      +-----------------+      +-----------------+
   |   Layer 1       |      |   Layer 2       |      |   Layer 3       |
   |   universe      | ---> |   catalysts +   | ---> |   forecast      |
   |   screen        |      |   setups        |      |                 |
   |                 |      |                 |      |  LightGBM(89f)  |
   |  multi-factor   |      |  catalyst       |      |  HMM regime(3s) |
   |  scoring +      |      |  calendar +     |      |  Ensemble:      |
   |  waterfall      |      |  technical      |      |   LGBM+AR1+MC   |
   |  diagnostics    |      |  setups         |      |   +linear+macro |
   |                 |      |                 |      |  Purged WF + EM |
   |                 |      |                 |      |  Placebo audit  |
   |                 |      |                 |      |  Sensitivity    |
   |                 |      |                 |      |  IC backtest    |
   +-----------------+      +-----------------+      +-----------------+
            |                        |                        |
            +------------------------+------------------------+
                                     |
                                     v
                          +-----------------------+
                          |  HTML report (Jinja2) |
                          |  Bloomberg-dark, per  |
                          |  ticker + index page  |
                          +-----------------------+
```

The contract everywhere: pure functions where possible, pandas Series/DataFrames in and out, version stamps on every signal module, tests that mirror `src/` 1:1.

---

## Methodology highlights

Every signal in this codebase traces back to a paper. The choices below are the ones I'd defend in a review.

- **Yang-Zhang realized volatility** (Yang & Zhang, 2000, "Drift-Independent Volatility Estimation Based on High, Low, Open, and Close Prices," *Journal of Business*). I use this over close-to-close because it is drift-independent and uses overnight and intraday information separately. For daily OHLC data on liquid US large-caps it is the consensus efficient estimator. Implemented in `src/methodology/yang_zhang_vol.py`; cross-checked against Parkinson and Garman-Klass.

- **Loughran-McDonald financial-domain sentiment** (Loughran & McDonald, 2011, "When Is a Liability Not a Liability?," *Journal of Finance*). General-purpose lexicons (Harvard IV-4, LIWC) miss financial polarity — words like *liability*, *tax*, *cost* are not negative in financial text. LM built a domain-specific lexicon by counting actual 10-K tone. I use LM Negative as the primary tone signal on EDGAR full-text filings. Implemented in `src/methodology/lm_tone_signal.py` with the dictionary under `config/lm_dictionary/`.

- **Opportunistic insider trades** (Cohen, Malloy & Pomorski, 2012, "Decoding Inside Information," *Journal of Finance*). Routine insider trades (calendar-clustered, predictable size) are noise; opportunistic trades (off-pattern, larger, isolated) carry signal of roughly 80 bps/month abnormal return. I classify Form 4 filings using their routine-vs-opportunistic rule and surface only the opportunistic flow. Implemented in `src/methodology/opportunistic_insider.py`.

- **Purged walk-forward with embargo** (Lopez de Prado, 2018, *Advances in Financial Machine Learning*, Ch. 7). Standard k-fold and even time-series CV leak into the training set when features include rolling stats or labels are forward-looking. Purging removes training samples whose label window overlaps the test set; the embargo additionally drops a buffer of samples immediately after the test fold. This is the only honest way to cross-validate a forecasting model. Implemented in `src/layer3_forecast/ml_forecaster.py` and `walk_forward_backtest.py`.

- **Triple-barrier labels with sigma-scaled deadzone** (Lopez de Prado, *AFML* Ch. 3). Daily next-return as a regression target is dominated by noise. Triple-barrier labels {-1, 0, +1} treat the problem as classification — up barrier, down barrier, or vertical timeout — with the deadzone scaled by realized volatility so neutral days don't pollute the class boundaries. Implemented in `src/layer3_forecast/ml_features.py`.

- **Value + momentum combined factor frame** (Asness, Moskowitz & Pedersen, 2013, "Value and Momentum Everywhere," *Journal of Finance*; Asness, Frazzini & Pedersen, 2019, "Quality Minus Junk," *Review of Accounting Studies*). Value alone has long drawdowns; combining value with momentum (and adding a quality screen on profitability and safety) cuts drawdowns sharply. The Layer 1 scoring weights in `config/scoring.yaml` reflect this — value, momentum, and quality all carry weight rather than any one dimension dominating.

- **3-state Gaussian HMM regime conditioning**. A 3-state HMM on (return, abs return, realized vol) refit every 63 bars (one quarter) is exposed to the LightGBM model as a regime ID plus three regime-probability columns. The motivation is that the same feature set is predictive in different directions in trending vs choppy regimes; the HMM lets the model split on regime explicitly. Implemented in `src/layer3_forecast/hmm_regime.py`. The +0.8 to +1.5 pp lift quoted in that file is the published value from MDPI Electronics 15(6):1334; my own delta sits within that band on the three benchmark tickers.

The full bibliography with implementation status per paper lives at `docs/methodology/research_bibliography.md`. The plain-English bridge between code, finance concept, and academic source is at `docs/methodology/methodology-handbook.md`.

---

## Honest benchmark

Daily next-day directional accuracy on AAPL, MSFT, BAC over five years of daily data, 89 engineered features, LightGBM classifier with class-weighted loss, triple-barrier labels, 5-fold purged walk-forward with embargo:

| Ticker | Honest WF (5-fold purged) | PMC-style (best-of-10 single window) | PMC paper reported | Gap |
|---|---|---|---|---|
| AAPL | 54.3% +/- 4.2% | 53.3% | 75.6% | +21.3 pp |
| MSFT | 51.1% +/- 9.3% | 55.6% | 71.6% | +20.5 pp |
| BAC  | 56.6% +/- 4.1% | 54.4% | 77.2% | +20.6 pp |

The honest reproducible ceiling on daily directional accuracy for liquid US large-caps is roughly 51-58%. This matches independent honest baselines:

- Microsoft Qlib on Alpha158: IC ~= 0.04, which translates to roughly 53% directional accuracy
- hklchung S&P-direction LightGBM with proper time-series CV: ~58%
- Yoo, Soun, Park & Kang, 2024, arXiv:2504.02249, on transformer-based equity forecasting: similar range

Papers that report 70%+ daily directional accuracy on individual large-caps typically have one or more of: (a) selection bias on the test set ("best of 30 runs" reporting), (b) feature-computation leakage (rolling indicators computed on the full series before the train/test split), (c) timestamp leakage in the sentiment features (FinBERT articles published after the close used to predict the same close-to-close return).

The tradeable signal is the spread between the model's prediction and the 50% null, not the headline accuracy. A 55% directional model with proper position sizing makes money. A 75% number that came from leakage does not.

The full audit lives at `docs/benchmarks/directional_accuracy.md`, with the reproducer at `scripts/benchmark_directional.py`.

---

## Design discipline

Every non-trivial component in this project was specified before it was implemented. The design specs and implementation plans I wrote during the build are preserved under [`docs/claude-code/specs/`](docs/claude-code/specs) and [`docs/claude-code/plans/`](docs/claude-code/plans). Each spec names the design alternatives that were considered and the one that was chosen. Each plan decomposes the spec into file-by-file steps with acceptance criteria written before the code.

The data-adapter layer (`src/common/datasources/`) is the clearest example. It decomposed into ten independent sub-plans — watermarks foundation, Finviz/Yahoo fetch, EDGAR XBRL fundamentals, EDGAR insider and filings, FRED + FINRA, StockAnalysis ratios, OpenBB router, the rate-limited orchestrator, news-activity sub-signals, the Loughran-McDonald tone module, and the Yang-Zhang volatility + composite signal. Each sub-plan had its own acceptance test that had to fail before any implementation code was allowed to land. An end-to-end integration test ([`tests/integration/test_a3_end_to_end.py`](tests/integration/test_a3_end_to_end.py)) confirmed the ten pieces fit together once they all cleared their unit tests.

The test-first discipline is enforced by structure. `tests/` mirrors `src/` one-to-one. Every adapter has a contract test before it has an implementation. Golden values for the technical indicators are taken from Wilder's and Appel's textbooks directly rather than from another library. The Loughran-McDonald tone test compares against the published LM reference. The Yang-Zhang vol test cross-checks against Parkinson and Garman-Klass on synthetic geometric Brownian motion with a known volatility. Tests pass locally and in CI before any merge.

---

## Quickstart

PowerShell (Windows):

```powershell
git clone https://github.com/arora-vaibhav/equity-research-factor-forecast-claude.git
cd equity-research-factor-forecast-claude
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
# Generate a sample report (synthetic, no network calls)
python scripts/forecast_demo.py
# View report at reports/sample/AAPL.html
```

Bash (macOS / Linux / WSL):

```bash
git clone https://github.com/arora-vaibhav/equity-research-factor-forecast-claude.git
cd equity-research-factor-forecast-claude
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pytest
# Generate a sample report (synthetic, no network calls)
python scripts/forecast_demo.py
# View report at reports/sample/AAPL.html
```

For the live pipeline, copy `config/api_keys.yaml` to a local override and fill in the keys you have (FMP, Polygon, FRED, etc.). The system runs end-to-end even with most keys blank — anything that can be served by Yahoo, EDGAR, Finviz, FRED's free tier, or pytrends will be; missing keys downgrade specific sources gracefully and the orchestrator routes around them.

```powershell
python scripts/forecast_live_demo.py
python scripts/benchmark_directional.py --ticker AAPL --folds 5
```

The notebook driver for Layer 1 is `notebooks/layer1_control.ipynb`. Open in Jupyter or VS Code.

---

## Project layout

```
.
├── src/
│   ├── common/
│   │   ├── datasources/      # 13 source adapters + base + registry + resolution
│   │   ├── orchestrator/     # rate-limited fetcher: quota windows + priority queue + budgets
│   │   ├── database.py       # SQLite v2 schema, watermarks, INSERT OR IGNORE
│   │   ├── schemas.py        # Pydantic models for every table
│   │   ├── parsing.py
│   │   └── env_loader.py
│   ├── methodology/          # signal computations -- one module per signal
│   ├── layer1_universe/      # screen + factors + waterfall diagnostics
│   ├── layer2_catalyst/      # catalyst calendar + technical setups
│   └── layer3_forecast/      # LightGBM + HMM + ensemble + backtests + report
├── tests/                    # mirrors src/ 1:1; contract tests before implementation
│   ├── common/
│   ├── datasources/
│   ├── methodology/
│   ├── orchestrator/
│   ├── layer1_universe/
│   ├── layer2_catalyst/
│   ├── layer3_forecast/
│   ├── integration/          # cross-layer end-to-end tests
│   └── fixtures/
├── config/                   # api_keys, datasources priorities, scoring, orchestrator, LM dict
├── docs/
│   ├── claude-code/
│   │   ├── specs/            # 2 design specs
│   │   └── plans/            # ~17 implementation plans
│   ├── methodology/          # research bibliography + methodology handbook + factor models
│   ├── benchmarks/           # directional_accuracy.md
│   ├── DATAFLOW_OVERVIEW.md
│   └── HOW_TO_RUN_TESTS.md
├── notebooks/                # layer1_control, layer1_v2_drilldown, smoke_tests
├── scripts/                  # forecast_demo, forecast_live_demo, benchmark_directional, ...
├── reports/                  # generated HTML reports land here
├── requirements.txt
├── requirements-notebook.txt
├── pytest.ini
└── README.md
```

---

## Design choices worth flagging

A few decisions I'd defend if asked.

**LightGBM over an LSTM or transformer.** On daily tabular features for liquid equities, LightGBM matches transformer accuracy at roughly 1000x less compute. This is the published Qlib and ML4T finding, and it held in my own replication. The compute-vs-accuracy frontier on this problem favours gradient-boosted trees, full stop. The expressive capacity of an LSTM is not the binding constraint at the daily horizon on this feature set; the binding constraint is signal-to-noise.

**Triple-barrier labels over regression.** Regressing next-day return is regressing onto a target dominated by microstructure noise. Triple-barrier converts the problem to classification with up/down/timeout outcomes, with the deadzone scaled by realized volatility so the neutral class isn't just everything close to zero. The information content of the label is what gets predicted; the noise content goes into the neutral bucket.

**Purged walk-forward with embargo, not k-fold or random split.** Random k-fold leaks. Standard time-series CV with overlapping rolling features also leaks. Purging plus embargo is the only honest method I'm aware of for cross-validating a model whose features include rolling statistics and whose labels are forward-looking.

**Free public data only.** Every source in this pipeline is free for non-commercial use, including SEC EDGAR (truly free), FRED (free with key), Yahoo via yfinance (terms-of-service-bound), Finviz (HTML-scrape), and FINRA (download). FMP and Polygon are free at the tier I use them at. This is deliberate: the claim of the project is that you can do real factor research with free data if your methodology is honest, and the methodology gives the same answer as paid data on the questions we care about.

**HTML reports, not a webapp.** The report deliverable is a Jinja2-rendered static HTML file per ticker, plus an index page. No server, no frontend framework, no JavaScript beyond what's needed for sorting tables. The file you generate is the file you can email, archive, or check into version control. This is also how every sell-side research desk distributes its own research — PDF or HTML, not a live dashboard.

**SQLite, not Postgres.** The data is single-writer, append-only, and fits on disk. The right tool for that is SQLite. The schema versioning, the watermark tables, and the `INSERT OR IGNORE` discipline are all in `src/common/database.py`; a future move to Postgres or DuckDB is straightforward but not currently justified.

---

## What I'd build next

A few things on the explicit non-shipped list, prioritised:

- **Conformal prediction intervals on Layer 3.** The ensemble currently quotes 80% and 95% intervals from the parametric distribution. Conformal prediction would give finite-sample valid intervals without distributional assumptions. The plan is drafted in `docs/claude-code/plans/2026-05-23-forecast-layer-v2-overhaul.md` and the wiring is on the v8 backlog.
- **PCP-deviation directional signal.** Cremers & Weinbaum (2010, *JFQA*) — put-call parity violations on individual equity options have predictive content for directional return. Source data (Polygon options chains) is already in. The signal computation isn't yet implemented.
- **Variance risk premium as a regime indicator.** Bollerslev, Tauchen & Zhou (2009, *RFS*) — the VRP itself is time-varying and predictive of future returns. Currently the HMM regime is on returns/vol; layering VRP on top is straightforward.
- **Live deployment loop.** The forecast pipeline runs end-to-end as a one-shot script; a cron-driven version with state persistence and report archival is a tractable extension but currently out of scope.

---

## License

MIT. See `LICENSE`.

---

## References

The full annotated reading list — every paper that shaped a design choice, with implementation status and citation — lives at:

- `docs/methodology/research_bibliography.md` (annotated, by topic)
- `docs/methodology/methodology-handbook.md` (plain-English code-finance-why bridge, in build order)
- `docs/methodology/factor_models.md`
- `docs/methodology/options_pricing.md`
- `docs/methodology/volatility_analysis.md`
- `docs/methodology/financial-methodology-reference.md`

The benchmark methodology and its honest verdict against published baselines:

- `docs/benchmarks/directional_accuracy.md`

The engineering receipts for the build process:

- `docs/claude-code/specs/` — 2 design specs
- `docs/claude-code/plans/` — ~17 implementation plans, each with explicit acceptance criteria

---

*Author: Vaibhav Arora. Code under MIT. Not investment advice. Past performance — including the cross-validated directional accuracies reported above — is not indicative of future results.*
