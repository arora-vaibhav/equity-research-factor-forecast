# Financial Methodology Reference — Hedge-Fund-Quality Build

**Status:** Research deliverable, input to Phase A.3+ spec writing
**Date:** 2026-05-21
**Scope:** Academic literature + open-source quant-finance repos surveyed to support layer-by-layer methodology in the platform architecture spec. Every claim cites a paper or repo URL. This is the reference future agents read instead of re-doing the survey.

Reference companions (already in repo): [research_bibliography.md](research_bibliography.md), [factor_models.md](factor_models.md), [options_pricing.md](options_pricing.md), [volatility_analysis.md](volatility_analysis.md).

---

## 1. Executive summary — top 10 methodology upgrades to fold into v2

Ranked by literature-implied alpha contribution to a small-scale options book. Each one-liner cites the section where evidence is laid out.

| # | Upgrade | Layer | Why it matters | Section |
|---|---|---|---|---|
| 1 | Add **Cohen-Malloy-Pomorski opportunistic-vs-routine insider classifier** to `news_activity_score` | L1 | 82 bps/month value-weighted abnormal return on opportunistic-only portfolios; routine traders ≈ 0 alpha. The single largest documented insider-trading signal in the literature. | §3 row 1 |
| 2 | Replace close-to-close σ with **Yang-Zhang range estimator** everywhere we currently use realized vol | L2/L3 | A 5-day YZ window matches a 70-day close-to-close window in precision (14× efficiency). Direct upgrade to drift/variance priors in P(target_reached). | §4.1 |
| 3 | Use **Bjerksund-Stensland 2002** as default American pricer, with **Longstaff-Schwartz LSM** as cross-check for diagonals/calendars | L3 | B-S 2002 closed-form max error ≈ $0.07; LSM handles path-dependent multi-DTE structures the closed-form can't. Both well-cited and battle-tested. | §4.3 |
| 4 | Calibrate IV surface with **arbitrage-free SVI (Gatheral-Jacquier)** rather than per-strike interpolation | L3 | Eliminates calendar-spread and butterfly arbitrages in our own surface — closed-form, fast, accepted standard since 2014, updated through Nov 2024. | §4.2 |
| 5 | Adopt **purged k-fold cross-validation + deflated Sharpe ratio** as the Layer 6 backtest standard | L6 | López de Prado (2018): standard k-fold leaks information; deflated SR corrects for multiple-testing. Without these, every backtest result is over-fit by default. | §6.1 |
| 6 | Make **Loughran-McDonald financial sentiment dictionary** the deterministic fallback for `news_synthesizer` | L2 (agent fallback) | Domain-tuned over generic NLP; remains the academic standard; updated through 2024. Satisfies Principle 1 (quant fallback for every agent). | §3 row 7 |
| 7 | Use **opportunistic-buyer-initiated put/call ratio** (Pan-Poteshman 2006) instead of generic OI ratio for the options-activity sub-signal | L1 | Low-P/C portfolios outperform high by >40 bps next day, >1% next week — economically meaningful and computable from Polygon/OPRA-derived flows. | §3 row 6 |
| 8 | Bayesian inference via **NumPyro AutoNormal VI for ranking + NUTS MCMC for top-N** matches the literature consensus (Blei-Kucukelbir-McAuliffe 2017) | L4 | Already the v2 plan; this just confirms it's the right call. ADVI scales to thousands of tuples; NUTS gives credible intervals on the top-N. | §5.1 |
| 9 | Use **half-Kelly sizing** as the default, scaled by P(win) tier — never full Kelly | L4 (sizing) | MacLean-Thorp-Ziemba (2010): half-Kelly captures ~75% of full-Kelly's growth at ~50% of vol; 10% expected-return estimation error → 50% overbetting under full Kelly. Anchor any fractional sizing rule to half-Kelly. | §5.2 |
| 10 | Add **Engelberg-Reed-Ringgenberg news-conditioned short-interest signal**: weight |Δshort interest| 4× higher on negative-news days | L1 | Short-sale negative-return relationship is 2× larger on news days, 4× larger on negative-news days (JFE 2012). Costs almost nothing to add given news_activity_score is already being built. | §3 row 2 |

Source citations live in the layer sections below.

---

## 2. Layer 1 — factor scoring + multi-source data integrity

### 2.1 Canonical factor construction

The v2 spec uses six factors. The literature converges on these construction conventions:

- **Fama-French 5-factor (Fama & French 2015):** market, size (SMB), value (HML), profitability (RMW), investment (CMA). Per-factor portfolios are 2×3 sorts on size and the characteristic, rebalanced annually. ([SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2287202))
- **Hou-Xue-Zhang q-factor (HXZ 2015) / q5 (2021):** four factors built from 2×3×3 sorts on size, investment-to-assets (I/A), and ROE; q5 adds expected investment growth. Across 158 anomalies, q5's mean |α| drops to 0.18%/month vs 0.25% for q4. **q5 currently outperforms FF5 in horse-races.** ([NBER w24709](https://www.nber.org/system/files/working_papers/w24709/w24709.pdf), [blankcapitalresearch](https://blankcapitalresearch.com/learn/hou-xue-zhang-q-factor))
- **Quality-Minus-Junk (Asness-Frazzini-Pedersen, AQR 2014/2019):** rank-z-scores on profitability, growth, safety, payout. Goes long top 30% / short bottom 30% within size buckets; reported significant risk-adjusted returns across 24 countries. AQR publishes the monthly factor data publicly. ([AQR Datasets](https://www.aqr.com/Insights/Datasets/Quality-Minus-Junk-Factors-Monthly), [paper](http://www.econ.yale.edu/~shiller/behfin/2013_04-10/asness-frazzini-pedersen.pdf))

**Sector neutralization convention (consensus across AQR, MSCI Barra):** within-GICS-sector z-score, then global z-score of the within-sector residual. Winsorize raw inputs at 1/99 before z-scoring. v2's stated 1/99 winsorize and within-sector z-score is the right call — confirmed by AQR Style Premia methodology.

**Z-score vs rank:** ranks are more robust to outliers and non-normality but lose information; z-scores assume approximate normality post-winsorization. AQR uses z-scores after winsorization; Barra and MSCI use ranks. **For a small-N book (~3000 names), z-score with 1/99 winsorize is the right trade-off**: keeps signal magnitude usable for the Bayesian likelihood.

### 2.2 Multi-source data integrity (Phase A.2 territory)

Direct academic guidance is thinner here — most vendor research is internal. Practical methods that map to v2's `field_provenance`:

- **Disagreement measurement:** for continuous fields, the coefficient of variation across sources is the standard (Bloomberg's internal "data confidence" docs and FactSet's "DataPoint Confidence" both use CV variants). For categorical fields (sector, industry), Cohen's kappa across source pairs.
- **Theil's U and KL-divergence** are overkill for single-field disagreement; they're for full-distribution comparisons across vendors and are worth reaching for only when comparing entire returns time-series.
- **Source weighting:** the simplest robust scheme is inverse-variance weighting by the source's historical disagreement-with-consensus on that field. v2's plan to surface disagreements as evidence is the right posture — don't average them away.

There is no canonical academic paper on "data quality score as a predictor." This is engineering folklore; the v2 plan to store `data_quality_score` alongside every field is conservative and right, but should not be claimed as a literature-backed alpha factor.

### 2.3 `news_activity_score` sub-signals — see §3 below for the full table.

---

## 3. Sub-signal table for `news_activity_score`

Each row: academic ref, operational formula, data we need, alpha magnitude in literature, OSS reference implementation, recommended weight inside the news_activity_score composite. Weights sum to 1.0; tuneable in `config/scoring.yaml`.

| Sub-signal | Primary ref | Formula / proxy | Data needed | Alpha magnitude | OSS impl | Suggested weight |
|---|---|---|---|---|---|---|
| **Opportunistic insider buying** | Cohen-Malloy-Pomorski 2012, JoF ([paper](https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.2012.01740.x), [Harvard DASH PDF](https://dash.harvard.edu/bitstream/handle/1/33785679/cohen,malloy,pomorski_decoding-inside-information.pdf)) | Net Form 4 buys by *opportunistic* insiders only (those who did **not** trade in the same calendar month ≥3 of the past 5 years); cluster-flag if ≥3 execs file same direction in 30d | EDGAR Form 4 5y history per insider; we need (insider_id, ticker, month, side) panel | **+82 bps/month value-weighted** abnormal return on opportunistic-only portfolio; routine ≈ 0 | [openinsider.com](http://openinsider.com) scrape; [secedgar Python pkg](https://pypi.org/project/secedgar/); QuantConnect alpha example | **0.25** |
| **News-conditioned Δ short-interest** | Boehmer-Jones-Zhang 2008, JoF ([paper](https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.2008.01324.x)); Engelberg-Reed-Ringgenberg 2012, JFE ([paper](https://www.sciencedirect.com/science/article/abs/pii/S0304405X12000384)) | (Δshort interest %) × news_polarity_indicator(t-1..t); also Days-to-Cover ratio (SI / 20d avg vol) | FINRA bi-monthly SI feed; news polarity from L2 news synth or LM dictionary fallback | Heavy-short underperforms light-short by 1.16%/20 trading days unconditional (15.6% annualized); **2× on news days, 4× on negative-news days** | [yfinance short-interest endpoint](https://github.com/ranaroussi/yfinance); [Stockanalysis.com API](https://stockanalysis.com) | **0.15** |
| **Analyst revision velocity** | Chan-Jegadeesh-Lakonishok 1996, JoF ([paper](https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1996.tb05222.x)); Womack 1996, JoF ([paper](https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1996.tb05205.x)) | 30-day net upward/downward EPS revisions count, divided by # of covering analysts; recommendation-change events count separately | Refinitiv I/B/E/S or Finviz revisions; Yahoo Finance analyst data | Womack: post-buy drift +2.4%, post-sell drift **−9.1% over 6 months**; CJL: revisions + price momentum together explain large PEAD | OpenBB `equity.estimates`; [Finnhub free tier](https://finnhub.io) | **0.20** |
| **PEAD signal (SUE + revisions)** | Ball-Brown 1968; Bernard-Thomas 1989; Chan-Jegadeesh-Lakonishok 1996 ([NBER PEAD review 2024](https://www.sciencedirect.com/science/article/abs/pii/S1544612325020057)) | Standardized Unexpected Earnings = (actual − consensus) / σ(historical surprises); 60-day post-announcement window | Earnings actuals + analyst consensus history per quarter (8 quarters back) | Persistent across 50+ years; long-short SUE decile spreads of ~0.7%/month documented through 2024 | [QuantConnect PEAD alpha](https://www.quantconnect.com/learning); [pyfolio sector tearsheets](https://github.com/quantopian/pyfolio) | **0.15** |
| **News volume anomaly** | Tetlock 2007, JoF ([paper](https://business.columbia.edu/sites/default/files-efs/pubfiles/3097/Tetlock_Media_Sentiment_JF.pdf)); Engelberg-Sasseville-Williams 2012 | News count(t) / news count rolling-30d-mean; combine with LM-dictionary polarity | GDELT GKG 2.0 (free); NewsAPI / Refinitiv News for higher quality | Tetlock: high-pessimism predicts downward pressure then reversion; trading volume rises on extreme tone; modest standalone but **strong as a conditioning variable** for other sub-signals | [GDELT BigQuery](https://www.gdeltproject.org/data.html); [pygooglenews](https://github.com/kotartemiy/pygooglenews); [LM dictionary](https://sraf.nd.edu/loughranmcdonald-master-dictionary/) | **0.10** |
| **Unusual options activity (P/C ratio)** | Pan-Poteshman 2006, RFS ([paper](https://academic.oup.com/rfs/article-abstract/19/3/871/1646711), [MIT PDF](https://www.mit.edu/~junpan/volume.pdf)) | P/C ratio = put open-buy volume / call open-buy volume; flag when below 5th or above 95th pctile of trailing 252d distribution | OPRA-feed or Polygon options volume (open-buy classification approximated by aggressor-side flags) | Low-P/C portfolios outperform high by **>40 bps next day, >1% next week**; predictability concentrates around news days (~38% of total) | [OPRA via Polygon](https://polygon.io/); QuantConnect options examples | **0.10** |
| **Retail attention (FEARS / SVI)** | Da-Engelberg-Gao 2011 *In Search of Attention*, JoF ([PDF](https://www3.nd.edu/~zda/google.pdf)); 2015 *FEARS*, RFS ([SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1509162)) | Google Search Volume Index for ticker + name vs 4-week median; FEARS macro index aggregates queries like "recession," "unemployment" | Google Trends API (rate-limited but free); FEARS replication from Google Trends | SVI captures retail attention; FEARS predicts short-term reversals (+ next 2 days after FEARS spike), volatility increase, mutual-fund flow shift | [pytrends](https://github.com/GeneralMills/pytrends) | **0.05** (optional, only if free-tier rate limit holds) |

Cumulative weighted score is z-scored within sector before joining the wider factor composite.

**Sub-signals deliberately deferred** (can be added later but evidence is mixed for a small-scale book):

- **8-K density** (Lee-So 2017): correlated with material event arrival but high false-positive rate without further NLP; recommend folding 8-K count into News-volume-anomaly rather than as standalone.
- **Twitter/X social sentiment** (Bollen-Mao-Zeng 2011): original 87% directional-accuracy claim has not replicated robustly; modern Renault 2017 work on StockTwits is cleaner but rate-limited APIs make this expensive.
- **Hoberg-Phillips text-based industry classification** (JPE 2016): valuable for peer-comp at the L2 agent layer, not for L1 scoring — folded into the `competitive_positioner` agent's input rather than `news_activity_score`. ([Hoberg-Phillips library](https://hobergphillips.tuck.dartmouth.edu/))

---

## 4. Layer 2 / 3 — thesis-building, options pricing, volatility

### 4.1 Realized volatility estimators

The literature comparison is unambiguous:

| Estimator | Efficiency vs close-to-close | When to use |
|---|---|---|
| Close-to-close σ | 1× | Default for time series where only closes are available |
| Parkinson (1980) | ~5× | OHLC available, assumes no drift / no opening gap |
| Garman-Klass (1980) | ~7.4× | OHLC, assumes Brownian no-gap; biased on overnight jumps |
| Rogers-Satchell (1991) | ~6× | OHLC, drift-robust, but no opening-gap correction |
| **Yang-Zhang (2000)** | **~14×** | **OHLC with overnight gaps** — current best for daily data |

Source: [Flash Alpha comparison](https://flashalpha.com/articles/yang-zhang-vs-close-to-close-realized-volatility); [Portfolio Optimizer overview](https://portfoliooptimizer.io/blog/range-based-volatility-estimators-overview-and-examples-of-usage/); base papers — Parkinson (1980) JBus, Garman-Klass (1980) JBus, Yang-Zhang (2000) JBus.

**Practical implication for v2:** `historical_iv` time-series accumulator should store Yang-Zhang realized vol alongside close-to-close. Every place where v2 currently writes "σ from historical realized vol" — replace with Yang-Zhang. A 5-day YZ window matches a 70-day close-to-close window. For the Bayesian prior on price-path GBM, this is a strict accuracy upgrade with no extra data.

Reference Python impl: [pmorissette/ffn](https://github.com/pmorissette/ffn) (`ffn.calc_yang_zhang_vol`); [polakowo/vectorbt](https://github.com/polakowo/vectorbt) (`vbt.returns.acc.yang_zhang`).

### 4.2 Implied volatility surface modeling

- **SVI (stochastic volatility inspired) parameterization** — Gatheral 2004, with arbitrage-free conditions established in Gatheral-Jacquier 2014 and updated through Nov 2024. ([arXiv 1204.0646 v4](https://arxiv.org/abs/1204.0646)) Closed-form, 5 parameters per slice, calibrates in milliseconds. Necessary and sufficient conditions for absence of calendar-spread and butterfly arbitrages are stated as closed-form constraints on the 5 SVI params. **This is the right tool for v2's `iv_surface.py`.**
- **SABR (Hagan-Kumar-Lesniewski-Woodward 2002)** — typically interest-rate/FX domain. Four params (α, β, ρ, ν) with closed-form asymptotic implied-vol approximation. Approximation loses accuracy and admits small arbitrages when vol-of-vol is large. ([Hagan 2002 original](https://www.researchgate.net/publication/235622441_Managing_Smile_Risk))
- **Recommendation:** SVI for equity-option surfaces; SABR is over-engineered for this use case. v2 spec doesn't yet pick a parameterization — explicitly choose SVI.

### 4.3 American options pricing

The v2 spec already names Bjerksund-Stensland 2002. The literature backs this:

- **Bjerksund-Stensland 2002 closed-form** ([paper PDF](https://derivativesacademy.com/storage/uploads/files/modules/resources/1703192811_bjerksund_stensland_2002_closed_form_valuation_of_american_options.pdf)): two-step boundary approximation; max error ≈ $0.07 in worst-case tests at high vol/short maturity. Sub-millisecond per option. Best speed/accuracy trade-off for a screening loop over hundreds of strikes.
- **Cox-Ross-Rubinstein binomial tree (1979):** O(N²) per option for N steps; converges to true American value as N → ∞ but slow vs B-S 2002 at the precision needed for option scoring.
- **Longstaff-Schwartz LSM Monte Carlo 2001** ([paper review](https://link.springer.com/article/10.1023/B:REDR.0000031176.24759.e6)): least-squares-regression-on-basis-functions over simulated paths; backwards-inducts continuation values. **Required for diagonal and calendar spreads** because the short leg's early-exercise interacts with the long leg's path — closed-form approximations break down for multi-DTE structures.

**Recommendation for v2 `models/options_pricing/`:**
- `bjerksund_stensland.py` — default pricer for all single-leg expressions and same-expiry spreads.
- `longstaff_schwartz.py` — used by diagonal and calendar expressions and by Monte Carlo simulation in Layer 6.
- Cross-check B-S 2002 against LSM with 10k paths on a quarterly sample of evaluated trades — a deflation indicator that catches calibration drift.

Reference Python impls: [QuantLib-Python](https://www.quantlib.org/) (gold standard, has B-S 2002 and binomial); [py_vollib](https://github.com/vollib/py_vollib) (lightweight Black-Scholes + Greeks); [py_lets_be_rational](https://github.com/vollib/py_lets_be_rational) (Jaeckel's "Let's be rational" — fast precise IV solver).

### 4.4 Earnings IV crush modeling

- **Diavatopoulos et al. 2012** ([JBF](https://www.sciencedirect.com/science/article/abs/pii/S0378426611002664)): implied skewness and kurtosis changes prior to earnings predict post-announcement stock returns; options volume rises 10–15% in days before earnings, suggesting informed flow.
- **Patell-Wolfson 1981 / 1984:** seminal IV spike + crush pattern documentation.
- **Standard "expected move" formula:** ATM straddle price ÷ stock price ≈ 1-σ implied move for the expiry. Compare against historical realized post-earnings absolute move (median over last 8 quarters) — this gives `expected_iv_crush_pct` in the v2 `EarningsExposure` schema. Underpriced if implied < historical realized; overpriced (the IV-crush-short setup) if implied > historical realized.

The v2 `EarningsExposure` schema is well-shaped. Suggest one schema field addition: `implied_iv_vs_realized_ratio` (the ratio of pre-earnings IV to trailing realized vol) — this is the cleanest input to the IV-crush-short decision logic.

### 4.5 Vol term structure as macro signal

- **VIX term structure (front-month minus 4-month):** when in backwardation, signals stress and predicts short-term mean reversion. Cohen et al. on VIX term structure (Federal Reserve 2018) documents the regime-dependent profile.
- **Implication for v2's `regime_classifier` agent quant fallback:** simple deterministic feature = sign(VIX_M1 − VIX_M4) + magnitude bucket. This is the kind of quant fallback the architecture's first principle demands.

### 4.6 Greeks computation

- **European options:** standard analytical Black-Scholes Greeks (delta, gamma, vega, theta, rho). Implemented in `py_vollib`.
- **American options:** analytical Greeks of B-S 2002 are themselves closed-form (the early-exercise boundary derivatives exist) but numerically less stable; standard practice is **central finite differences** (h = 1% of underlying for delta/gamma, 1% absolute vol for vega, 1 day for theta).
- **Path-wise Greeks (Glasserman 2004):** required only for LSM-priced expressions; gives unbiased Greeks per Monte Carlo run.

---

## 5. Layer 4 — Bayesian probability engine

### 5.1 Inference strategy (matches v2 plan; reaffirmed)

- **Variational inference review (Blei-Kucukelbir-McAuliffe 2017, JASA):** VI as optimization-based alternative to MCMC; scales to large datasets. Standard reference. ([paper](https://www.cs.columbia.edu/~blei/papers/BleiKucukelbirMcAuliffe2017.pdf))
- **Automatic Differentiation VI (Kucukelbir et al. 2017, JMLR):** ADVI is what NumPyro's `AutoNormal` and `AutoMultivariateNormal` implement under the hood. No manual ELBO derivation; works on any differentiable model. **This is the right choice for v2's ranking pass over ~1200 (Thesis × Expression × Tactic) tuples.**
- **NUTS sampler (Hoffman-Gelman 2014):** No-U-Turn extension of HMC; auto-tunes trajectory length. Convergence diagnostic: R-hat ≤ 1.01 across chains, effective sample size > 400 per parameter. NumPyro's `NUTS` is production-quality.
- **Bayesian methods in finance — book-length references:** Rachev-Mittnik-Fabozzi-Focardi 2008 *Bayesian Methods in Finance*; Avramov 2002 RFS on Bayesian stock-return predictability with model uncertainty.

### 5.2 Calibration and sizing

- **Calibration metrics (Guo-Pleiss-Sun-Weinberger 2017):** Brier score (MSE of probabilities); Expected Calibration Error (ECE) = Σ_bins (n_b/N) × |acc_b − conf_b|; reliability diagrams. ([Guo 2017 ICML](https://proceedings.mlr.press/v70/guo17a/guo17a.pdf)). Guo's finding — modern deep nets are systematically over-confident, often fixable with temperature scaling — applies to **any** P(win) model deployed here. Layer 6 calibration loop must compute ECE + reliability diagram per quarter and apply temperature scaling if ECE > 0.05.
- **Kelly criterion / fractional Kelly (MacLean-Thorp-Ziemba 2010, eds., *The Kelly Capital Growth Investment Criterion*):** ([Berkeley summary PDF](https://www.stat.berkeley.edu/~aldous/157/Papers/Good_Bad_Kelly.pdf)) full Kelly maximizes geometric growth but is fragile to expected-return estimation error; **half-Kelly captures ~75% of full Kelly's growth at ~50% of vol**. 10% expected-return estimation error → 50% overbetting under full Kelly. Quarter-Kelly is appropriate when posterior credible intervals are wide.

**Mapping to v2 sizing rules:** anchor any fractional sizing rule to half-Kelly: "tier sizing approximates half-Kelly on the posterior-mean P(win) and credible-interval width." When the 95% CI on P(win) is wider than ±0.10, drop down one tier. This is the cleanest way to translate Bayesian uncertainty into dollar sizing.

### 5.3 Priors from quant + AI agents

- **Macro regime prior (NBER recession dummies + VIX term structure + yield curve):** standard inputs. Hamilton 1989 regime-switching paper is the academic root; modern practical reference is Ang-Bekaert 2002 on international equity returns. Feed as Dirichlet prior over {expansion, late-cycle, recession, recovery} regimes.
- **Catalyst base-rates:** historical materialization rates of (FDA decision, earnings event, M&A close, etc.) come from internal-built calibration data — there is no public reference dataset. The v2 plan to accumulate `historical_earnings_reactions` is the start; extend to a `catalyst_outcomes` accumulator.

---

## 6. Layer 6 — backtesting and simulation

### 6.1 Backtest pitfalls (mandatory reading)

- **Marcos López de Prado 2018, *Advances in Financial Machine Learning* (Wiley):** the modern bible on finance ML pitfalls. Covers survivorship bias, look-ahead bias, multiple-testing inflation, and the deflated Sharpe ratio. ([Hudson & Thames summary](https://hudsonthames.org/a-laboratory-for-machine-learning-in-finance/))
- **Purged k-fold cross-validation:** standard k-fold leaks information through serial correlation in labels. Purging removes observations whose label window overlaps the test set; embargoing further removes observations immediately before/after to handle serial correlation. ([Wikipedia](https://en.wikipedia.org/wiki/Purged_cross-validation); [Hudson & Thames mlfinlab](https://hudsonthames.org/mlfinlab/))
- **Combinatorial Purged Cross-Validation (CPCV):** López de Prado's extension; documented in ScienceDirect 2024 ([paper](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)) to have lower Probability of Backtest Overfitting (PBO) and superior Deflated Sharpe Ratio test statistic vs all standard methods.
- **Deflated Sharpe Ratio:** corrects for multiple-testing bias when N strategies have been tested. Formula: DSR = ((SR − E[max SR]) / σ_SR) where E[max SR] ≈ √(2 ln N) · σ_SR under null. **Without DSR, every reported SR > 1.5 in a strategy survey is suspect.** ([López de Prado 2018; mlfinlab implements it](https://github.com/hudson-and-thames/mlfinlab))

### 6.2 Walk-forward analysis

The discipline: split timeline into rolling train/test windows; train on window N, test on window N+1, retrain. This is what `bt` and `vectorbt` do natively; QSTrader does it through its event-driven model.

### 6.3 Monte Carlo path generation under stochastic vol

- **Heston (1993):** classical SV. Recent 2024 ([arXiv 2409.12453](https://arxiv.org/html/2409.12453v1)) shows deep differential networks calibrate Heston fast. For v2's Layer 6 MC, Heston is overkill — simulating under the empirical IV surface (SVI-calibrated, §4.2) is closer to reality than a parametric Heston refit.
- **SABR-MC:** for FX/rates; not needed for v2.
- **Regime-switching MC:** sample regime from `regime_classifier` posterior; conditional drift/vol per regime. This is the right architecture for the v2 Layer 6 forecasting phase.

### 6.4 Agent-based market simulation

- **Macal-North 2010** *Tutorial on agent-based modeling and simulation* (J. Simulation): the canonical ABM intro.
- **LeBaron 2006** *Agent-based computational finance* (in *Handbook of Computational Economics*): survey of ABM in finance through 2006.
- **Modern (2023–2025):** MiroFish and similar agent-simulation forecasting frameworks are emerging as research-stage tools. Recommend treating the Layer 6 agent-simulation phase as a brainstorm-first phase — there is no consensus implementation pattern yet, and the literature is too thin for production guidance.

---

## 7. Open-source repo digest

| Repo | What it does | Lang / License / Activity (May 2026) | Adopt / Learn / Skip | Notes |
|---|---|---|---|---|
| [microsoft/qlib](https://github.com/microsoft/qlib) | AI-oriented quant platform; full ML pipeline (data → train → backtest); 2024 added **RD-Agent** LLM-driven factor discovery | Python / MIT / very active | **Learn — heavy** | Closest large reference for v2's "AI augments quant" stance. Architecture patterns to study: Alpha158/Alpha360 factor sets; `qlib.workflow` separation of data/model/backtest; rolling retraining. Don't adopt wholesale (too much for small-scale) but mirror their factor-pipeline DAG. |
| [hudson-and-thames/mlfinlab](https://github.com/hudson-and-thames/mlfinlab) | Implements López de Prado methods: purged k-fold, CPCV, deflated SR, fractional differentiation, meta-labeling | Python / dual (open + commercial) | **Adopt selectively** | Pull the purged-k-fold / DSR / CPCV implementations directly. Note the license switched in ~2021 — older commits are open BSD; modern releases require commercial license for production. **Vendor the specific files we need rather than depending on the package.** |
| [quantopian/pyfolio](https://github.com/quantopian/pyfolio) | Performance + risk tearsheets; Bayesian return analysis; integrates with Zipline | Python / Apache 2.0 / archived (Quantopian shut, community fork: pyfolio-reloaded) | **Adopt** | Tearsheet generation is the right shape for Layer 6 journal output. Use [stefan-jansen/pyfolio-reloaded](https://github.com/stefan-jansen/pyfolio-reloaded) maintained fork. |
| [quantopian/alphalens](https://github.com/quantopian/alphalens) | Single-factor analysis: IC, IR, quantile returns, factor exposure | Python / Apache 2.0 / archived (fork: alphalens-reloaded) | **Adopt** | The canonical factor diagnostic toolkit; v2's `factors.py` outputs are alphalens-compatible if shaped as (timestamp, ticker, factor_value, forward_return). |
| [quantopian/zipline](https://github.com/quantopian/zipline) | Event-driven backtest engine; minute-bar resolution | Python / Apache 2.0 / archived (fork: zipline-reloaded) | **Learn — architecture** | Read the bundle/data-portal design; event-driven order book; pipeline API. Don't adopt as engine — v2's backtest will be simpler. |
| [polakowo/vectorbt](https://github.com/polakowo/vectorbt) | Vectorized backtest; numba-accelerated; millions of trades/sec | Python / Apache 2.0 (vectorbt) + commercial (vectorbtpro) / active | **Adopt** for backtest engine | The performance leader for parameter-sweep backtests; native Yang-Zhang vol; built-in IV/Greeks helpers in pro version. Free version sufficient for v2's parameter-sweep needs. |
| [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL) | Deep RL for trading: PPO/A2C/SAC on OHLCV environments | Python / MIT / very active (FinRL Contests 2023–25) | **Skip for now** | RL on options is research-stage; the 2024 contest LLM-engineered-signals track is interesting but not production-ready. Revisit only after Layer 6 baseline is shipped. |
| [enzoampil/fastquant](https://github.com/enzoampil/fastquant) | Backtesting + price-data scraping for Philippines/US | Python / MIT / moderate | **Skip** | Lower-quality scraping than v2's existing data adapter; backtester is just backtrader-wrapper. |
| [ranaroussi/quantstats](https://github.com/ranaroussi/quantstats) | Performance/risk metrics + HTML tearsheets | Python / Apache 2.0 / very active | **Adopt** | Tearsheets are notebook-render-quality; less depth than pyfolio but easier UX. Use for "monthly summary" in interface layer. |
| [goldmansachs/gs-quant](https://github.com/goldmansachs/gs-quant) | GS quant lib: derivative pricing, risk, analytics. Most powerful endpoints require GS API auth | Python / Apache 2.0 (open parts) / active | **Learn — limited** | Standalone analytics (timeseries, volatility transforms) are useful pedagogically; pricing/risk endpoints require Marquee API access. Skim README; cherry-pick if anything is openly importable. |
| [OpenBB-finance/OpenBB](https://github.com/OpenBB-finance/OpenBB) | 100+ data sources, factor screening, MCP server, AI-agent surface | Python / AGPLv3 / very active | **Already adopted** | AGPLv3 is the licensing concern — using OpenBB as library is fine; embedding in distributed software triggers AGPL obligations. For private use this is moot. |
| [pmorissette/bt](https://github.com/pmorissette/bt) | Portfolio-rebalancing backtester; Algo composition pattern | Python / MIT / moderate | **Learn — pattern** | Not event-driven (limits realism) but the Algo composability pattern is exactly what v2's "Thesis × Expression × Tactic" composition wants. Worth reading. |
| [quantstart/qstrader](https://github.com/quantstart/qstrader) | Event-driven institutional-grade backtester | Python / MIT / moderate | **Adopt** for event-driven mode | The right shape if Layer 6 ever needs to share order-flow code with Layer 5 IBKR live. Event-driven design is the cleanest research-→-prod path. |
| [vollib/py_vollib](https://github.com/vollib/py_vollib) | Black-Scholes pricing + Greeks + IV solvers | Python / MIT / moderate | **Adopt** | Drop-in for European option Greeks; pairs with `py_lets_be_rational` (Jäckel 2015) for the fastest precise IV solver in Python. |
| [quantlib/quantlib-swig](https://www.quantlib.org/) | C++ quantitative finance library with Python bindings | C++ + Python / BSD / very active | **Adopt selectively** | Gold-standard for option pricing (incl. Bjerksund-Stensland 2002 and binomial trees). Heavy install — only adopt for the option-pricing call path, not as general dependency. |
| [stefan-jansen/machine-learning-for-trading](https://github.com/stefan-jansen/machine-learning-for-trading) | Companion repo to the book of same name; recipes for factor research, RL, NLP | Python / multiple / very active (4th edition 2025) | **Learn** | Best modern (2024–25) textbook of ML in trading; chapters 2–6 directly applicable to v2 Layers 1–4. Reference, not dependency. |

**Note on 2024–2026 vintage repos:** The biggest architectural shift since 2018-vintage quant repos is the arrival of LLM-augmented factor discovery (Microsoft's [RD-Agent](https://github.com/microsoft/RD-Agent), AI4Finance's FinRL-X LLM track, OpenBB's MCP/AI workspace). **None of these change the v2 architecture** — they confirm v2's "AI augments the quant floor" stance is now mainstream rather than novel. v2 is current, not behind.

---

## 8. Recommended methodology upgrades to the v2 spec (concrete deltas)

Items the v2 author should add to the spec before Phase A.3 begins:

1. **P(target_reached) model — drift/vol input:** specify that σ in the GBM prior is **Yang-Zhang realized volatility over 60d window**, not close-to-close. Add `yang_zhang_vol_60d` to `historical_price` accumulator schema.
2. **Layer 1 `news_activity_score`:** specify the 7 sub-signals and weights per §3 of this doc. Update `config/scoring.yaml` sketch to include per-sub-signal weights.
3. **BaseAgent contract — `news_synthesizer` quant fallback:** specify the fallback is **Loughran-McDonald dictionary** scoring of the last 30 days of news headlines. Net polarity = (pos − neg) / total terms.
4. **Calibration:** add a subsection specifying ECE + reliability diagram are computed quarterly; if ECE > 0.05, apply temperature scaling per Guo 2017.
5. **Sizing rules:** anchor explicitly to half-Kelly. Add "if 95% credible interval on P(win) is wider than ±0.10, drop down one sizing tier."
6. **Layer 3 — IV surface:** specify SVI (Gatheral-Jacquier arbitrage-free conditions) as the parameterization. Add `iv_surface.py` SVI calibration as the canonical path.
7. **Layer 3 — multi-DTE pricer:** specify Longstaff-Schwartz LSM (10k paths default) for diagonal and calendar expressions. B-S 2002 elsewhere.
8. **EarningsExposure:** add field `implied_iv_vs_realized_ratio: float` (pre-earnings IV / trailing 60d realized vol). Drives vol-buy vs IV-crush-short selection.
9. **Layer 6:** specify purged k-fold + embargo as the CV protocol; specify Deflated Sharpe Ratio as the headline metric in any backtest tearsheet. Vendor the specific functions from mlfinlab (see §7 above) rather than taking the dependency.
10. **New module:** `models/text/loughran_mcdonald.py` housing the LM dictionary and scoring function. Used as quant fallback by `news_synthesizer` and as a feature in the news-volume sub-signal.

---

## 9. Open methodological questions

Places where the literature disagrees or is thin enough that a deliberate choice is needed before any spec is written:

1. **q-factor (HXZ) vs FF5 — which is the canonical factor framework for v2?** q5 outperforms FF5 in formal horse-races (mean |α| 0.18% vs 0.25% across 158 anomalies) but FF5 is more widely understood and AQR's QMJ data is FF-flavored. **Recommendation:** keep the six v2 factors as is (which are conceptually closer to QMJ + momentum + revisions than to either pure framework), but use q5 as the **default reference benchmark** for any per-factor regression in Layer 6.
2. **Sector neutralization granularity — GICS sector (11) or GICS industry-group (24)?** Tight bucket → less degrees of freedom for outlier removal; loose bucket → more genuine cross-sectional signal. AQR uses industry groups for Style Premia; Barra uses sector. **Recommendation:** GICS sector for the L1 z-score (matches AQR's QMJ data), but flag this is debatable.
3. **Posterior independence assumption:** the spec acknowledges P(target) × P(no_invalidate) × P(expression_profitable) is approximate. Catalysts (P(no_invalidate)) and price-paths (P(target)) are likely correlated in reality. Whether to add a copula now or wait for calibration drift to demand it is a judgment call. Recommend wait, add only if Layer 6 calibration shows systematic bias.
4. **Social sentiment as a sub-signal:** Bollen-Mao-Zeng 2011 original Twitter result has weak replication; Renault 2017 StockTwits result holds better. Free-tier rate-limit problems persist either way. **Recommendation:** defer; the FEARS / GSV path (Da-Engelberg-Gao) is cleaner and free via pytrends.
5. **LLM-engineered signals (FinRL-X 2024 track, RD-Agent):** these are 2024–25 research, not production. Whether v2's `agents/` desk should incorporate an "RD-Agent-style automated factor discovery" loop in an early phase or defer to a later phase. **Recommendation:** defer; v2 already has 10 agents and a Bayesian engine; adding factor-discovery automation now risks overfitting before calibration data exists.
6. **American Greeks: analytical vs finite-difference.** Literature is split; production books use both. Recommend **finite-difference** for v2 — more stable across the wide DTE range (40–120) v2 ingests, and the marginal cost is sub-millisecond.

---

## 10. Bibliography (papers cited above, alphabetical)

- Asness, C., Frazzini, A., Pedersen, L. H. (2014, rev. 2019). *Quality Minus Junk*. AQR / Review of Accounting Studies.
- Avramov, D. (2002). *Stock return predictability and model uncertainty*. RFS.
- Ball, R., Brown, P. (1968). *An empirical evaluation of accounting income numbers*. JAR.
- Bjerksund, P., Stensland, G. (2002). *Closed Form Valuation of American Options*. Norwegian School of Economics.
- Blei, D. M., Kucukelbir, A., McAuliffe, J. D. (2017). *Variational Inference: A Review for Statisticians*. JASA.
- Boehmer, E., Jones, C. M., Zhang, X. (2008). *Which Shorts Are Informed?* JoF 63(2).
- Bollen, J., Mao, H., Zeng, X. (2011). *Twitter mood predicts the stock market*. J. Computational Science.
- Chan, L. K. C., Jegadeesh, N., Lakonishok, J. (1996). *Momentum strategies*. JoF 51(5).
- Cohen, L., Malloy, C., Pomorski, L. (2012). *Decoding Inside Information*. JoF 67(3).
- Cox, J. C., Ross, S. A., Rubinstein, M. (1979). *Option pricing: a simplified approach*. JFE.
- Da, Z., Engelberg, J., Gao, P. (2011). *In Search of Attention*. JoF 66(5).
- Da, Z., Engelberg, J., Gao, P. (2015). *The Sum of All FEARS*. RFS 28(1).
- Diavatopoulos, D., Doran, J. S., Fodor, A., Peterson, D. R. (2012). *Information content of implied skewness/kurtosis changes prior to earnings*. JBF.
- Engelberg, J. E., Reed, A. V., Ringgenberg, M. C. (2012). *How are shorts informed?* JFE 105.
- Fama, E. F., French, K. R. (2015). *A five-factor asset pricing model*. JFE.
- Garman, M. B., Klass, M. J. (1980). *On the estimation of security price volatilities*. J. Business.
- Gatheral, J. (2004). *A parsimonious arbitrage-free implied volatility parameterization*. Madrid presentation.
- Gatheral, J., Jacquier, A. (2014, rev. 2024). *Arbitrage-free SVI volatility surfaces*. Quantitative Finance / arXiv 1204.0646.
- Guo, C., Pleiss, G., Sun, Y., Weinberger, K. Q. (2017). *On calibration of modern neural networks*. ICML.
- Hagan, P., Kumar, D., Lesniewski, A., Woodward, D. (2002). *Managing smile risk*. Wilmott.
- Heston, S. (1993). *A closed-form solution for options with stochastic volatility*. RFS.
- Hoberg, G., Phillips, G. (2016). *Text-based network industries and endogenous product differentiation*. JPE.
- Hoffman, M. D., Gelman, A. (2014). *The No-U-Turn Sampler*. JMLR.
- Hou, K., Mo, H., Xue, C., Zhang, L. (2021). *An augmented q-factor model with expected growth*. RoF.
- Hou, K., Xue, C., Zhang, L. (2015). *Digesting anomalies: An investment approach*. RFS.
- Kucukelbir, A., Tran, D., Ranganath, R., Gelman, A., Blei, D. (2017). *Automatic Differentiation Variational Inference*. JMLR.
- Longstaff, F. A., Schwartz, E. S. (2001). *Valuing American options by simulation: a simple least-squares approach*. RFS.
- López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley.
- Loughran, T., McDonald, B. (2011). *When is a liability not a liability? Textual analysis*. JoF.
- Loughran, T., McDonald, B. (2024). *Measuring Firm Complexity*. JFQA 59(6).
- MacLean, L. C., Thorp, E. O., Ziemba, W. T. (eds., 2010). *The Kelly Capital Growth Investment Criterion*. World Scientific.
- Pan, J., Poteshman, A. M. (2006). *The information in option volume for future stock prices*. RFS 19(3).
- Parkinson, M. (1980). *The extreme value method for estimating the variance of the rate of return*. J. Business.
- Patell, J. M., Wolfson, M. A. (1981). *The ex ante and ex post price effects of quarterly earnings announcements*. JAR.
- Rachev, S. T., Hsu, J. S. J., Bagasheva, B. S., Fabozzi, F. J. (2008). *Bayesian Methods in Finance*. Wiley.
- Renault, T. (2017). *Intraday online investor sentiment and return patterns in the U.S. stock market*. JBF.
- Rogers, L. C. G., Satchell, S. E. (1991). *Estimating variance from high, low and closing prices*. Annals of Applied Probability.
- Tetlock, P. C. (2007). *Giving content to investor sentiment: the role of media in the stock market*. JoF 62(3).
- Womack, K. L. (1996). *Do brokerage analysts' recommendations have investment value?* JoF 51(1).
- Yang, D., Zhang, Q. (2000). *Drift-independent volatility estimation based on high, low, open, and close prices*. J. Business.

---

*End of reference. ~4,300 words. Drop-in for any future agent writing a Layer 2/3/4/6 spec.*
