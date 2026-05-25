# Methodology

This is a research project. Every signal, feature, and label decision is grounded in published methodology, and the deep documents under `docs/methodology/` cite the papers I leaned on. I treat this index as a router: it points at the long-form docs, the modules that implement each method, and the original sources, so a reader can verify any choice rather than take my word for it.

## Reading guide

I suggest reading the deep docs in this order:

1. `docs/methodology/methodology-handbook.md` — the pedagogical walkthrough. Bridges the code to the finance concepts and explains why each decision matters before getting into formulas.
2. `docs/methodology/factor_models.md` — Fama-French, momentum, quality, value, low-vol, and profitability factors as I implement them. Most Layer 1 screening and Layer 3 feature derivation comes from this.
3. `docs/methodology/volatility_analysis.md` — close-to-close, Parkinson, Garman-Klass, Yang-Zhang, and HAR-RV. The Yang-Zhang estimator and IV-vs-RV gap features are the most load-bearing pieces of the forecast layer.
4. `docs/methodology/options_pricing.md` — Black-Scholes, Bjerksund-Stensland, Greeks, IV rank, IV skew, term structure, and the volatility surface. Read if you care about the options panel.
5. `docs/methodology/research_bibliography.md` — the master reference: 200+ papers organized by topic (factor models, options pricing, vol analysis, sentiment, ML-finance, conformal prediction, microstructure). Consult as needed; not meant to be read straight through.
6. `docs/methodology/financial-methodology-reference.md` — the research-pass synthesis. Read this if you want the decision rationale: which alternatives I considered, what I rejected, and why.

## Key methodology highlights

Each bullet lists the method, the citation I used, the implementation module, and the deep doc for further detail.

- **Yang-Zhang volatility estimator** (Yang & Zhang 2000, *Journal of Business* 73(3)) — drift-free, opening-jump-aware realized volatility. I cite it because it dominates close-to-close and Garman-Klass when overnight gaps are non-trivial, which is the case for single-name US equities. Implementation: `src/methodology/yang_zhang_vol.py`. Deep doc: `docs/methodology/volatility_analysis.md`.
- **Loughran-McDonald domain dictionary** (Loughran & McDonald 2011, *Journal of Finance* 66(1)) — finance-specific tone dictionary. I cite it because generic-language dictionaries (Harvard-IV, LIWC) misclassify roughly 60% of negative financial terms on 10-K filings; LM cuts the false-positive rate substantially. Implementation: `src/methodology/lm_dictionary.py` and `src/methodology/lm_tone_signal.py`.
- **Opportunistic insider classifier** (Cohen, Malloy, Pomorski 2012, *Journal of Finance* 67(3)) — routine vs. opportunistic insider-trade segmentation. I cite it because routine clusters (recurring monthly sales, 10b5-1 plans) carry roughly 80% less signal than discretionary trades, and treating all insider activity uniformly washes out the predictive content. Implementation: `src/methodology/opportunistic_insider.py`.
- **Purged k-fold walk-forward with embargo** (Lopez de Prado, *Advances in Financial Machine Learning*, Ch. 7) — the only honest cross-validation scheme for serially correlated daily labels. I cite it because standard k-fold leaks future information through overlapping label windows. Implementation: `src/layer3_forecast/ml_forecaster.py`.
- **Triple-barrier labels** (Lopez de Prado, *Advances in Financial Machine Learning*, Ch. 3) — sigma-scaled upper, lower, and time barriers with a deadzone. I cite it because raw next-day return regression conflates direction with magnitude and is dominated by noise; the triple-barrier scheme produces a cleaner directional target. Implementation: `src/layer3_forecast/ml_features.py`.
- **HMM regime classifier as a feature** — three-state Gaussian HMM on realized vol and returns. I use the regime as a feature for downstream models, not as a hard gating threshold, because the threshold variant overfits the in-sample regime breakpoints. Implementation: `src/layer3_forecast/hmm_regime.py`.
- **HAR-RV volatility forecasting** (Corsi 2009, *Journal of Financial Econometrics* 7(2)) — heterogeneous autoregressive realized-volatility model with daily, weekly, and monthly RV regressors. I cite it because the additive horizon decomposition captures vol persistence at the time scales that actually matter for equity options. Implementation in the volatility-analysis pipeline, referenced from `docs/methodology/volatility_analysis.md`.
- **TA signal aggregation** — TradingView-style verdict computed from 17 indicators: RSI, MACD histogram and signal-line cross, Bollinger %b and squeeze, ADX, Stochastic, Williams %R, OBV slope, MFI, CMF, ATR%, SMA50/200 cross, EMA9/21 cross, Donchian-20, and price-vs-SMA200. Each indicator votes BUY / SELL / NEUTRAL; the aggregate is a calibrated verdict, not a raw vote count. Implementation: `src/methodology/ta_signal_rules.py`.
- **Cross-sectional rank target** (cross-sectional momentum tradition, e.g., Asness 1997, "The interaction of value and momentum strategies," *Financial Analysts Journal* 53(2)) — rank-based label instead of raw-return regression. I cite the cross-sectional momentum literature because rank labels are robust to regime-level return-distribution shifts, where raw-return models drift badly. Implementation: `src/layer3_forecast/cross_sectional.py`.
- **Sector rotation features** — sector-relative momentum and sector breadth. The model sees how a name is doing relative to its sector, not just its absolute return. Implementation: `src/methodology/sector_rotation.py`.
- **Peer comparison** (Fama & French 1992, *Journal of Finance* 47(2); Asness, Frazzini & Pedersen 2013, "Quality minus junk," working paper / *Review of Accounting Studies* 24(1) 2019) — same-sector peer percentile ranks on P/E and 20-day momentum, as a single-asset analog to the cross-sectional value-quality factor structure. Implementation: `src/methodology/peer_comparison.py`.

## The honest benchmark

Out-of-sample directional accuracy on AAPL, MSFT, and BAC with 5-fold purged walk-forward cross-validation comes in at **51-58%**. That is in line with what serious public benchmarks report:

- Microsoft Qlib's reference models report rank IC around 0.04 on US equities, which translates to roughly the same directional band.
- Yoo et al., arXiv:2504.02249, report similar numbers under leakage-free CV.
- The hklchung S&P-direction repository, with carefully constructed splits, lands in the same range.

Published figures of 70% or higher on single-name equity direction typically reflect label leakage, look-ahead in feature construction, or seed-selection bias. I do not chase those numbers. The honest number is the right number. Full breakdown, fold-by-fold results, and ablations are in `docs/benchmarks/directional_accuracy.md`.

## Open methodological questions

If the project continued, the next things I would build are:

- An LSTM ensemble member alongside the current tree-based learner, to capture longer feature interactions that gradient boosting does not see cleanly. Tested as a peer model, not a replacement.
- Extension of the forecast horizon down to intraday (1-hour and 4-hour bars), which requires re-deriving the Yang-Zhang and HAR-RV estimators on the finer grid and re-validating the label scheme.
- Alternative-data integration as features rather than standalone signals: news-tone time series, options-flow imbalance, short-interest deltas. Integrated as additional columns in the existing feature matrix, subject to the same purged walk-forward discipline.
- Conformal-prediction intervals on the directional probability output, so the forecast layer reports calibrated confidence rather than a point estimate. The standard reference is Angelopoulos & Bates (2021), *A Gentle Introduction to Conformal Prediction and Distribution-Free Uncertainty Quantification* (arXiv:2107.07511); a follow-up project would benchmark LAC, APS, and locally-weighted scorers on the same purged walk-forward grid.
- A formal feature-attribution pass (SHAP on the production model, plus permutation importance under the purged CV scheme) so the deep docs can list the features that actually pull weight in production, not just the features I designed.
