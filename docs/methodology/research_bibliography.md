# Research Bibliography — Annotated

**Status:** Draft v0.1 — Layer 0 foundation document
**Last updated:** 2026-05-15
**Purpose:** Annotated reading list organized by topic. Each entry includes what it contributes to the system. This is the academic and practitioner foundation that the ranking framework, screening logic, and execution rules in the master strategy doc must reflect. Where appropriate, the entry notes implementation status — whether the paper's insight is already encoded in the strategy, or pending.

**Access categories used below:**
- 🟢 = Open access (SSRN, arXiv, NBER, Fed working papers, author preprints) — full text available
- 🟡 = Partial access (abstracts + some preprints — access depends on institutional subscriptions)
- 🔴 = Paywalled or proprietary — method described from public summaries; primary source not directly accessed
- 📘 = Practitioner book — content known in public domain

Items flagged "pending-fetch" mean I have not yet retrieved the actual paper; the citation reflects the standard reference. As Layer 1 work proceeds, each is pulled and read in full.

---

## 1. Factor investing & cross-sectional return predictability

The academic foundation for the fundamental screening layer.

### Value factor

**Fama, E. F., & French, K. R. (1992). "The Cross-Section of Expected Stock Returns." Journal of Finance.** 🟢 (NBER w/p version)
The original empirical demonstration that book-to-market and size predict cross-sectional returns. Foundation for the value tilt in the long-bias setup. *Implementation:* informs the valuation-rank-vs-history component of the ranking framework.

**Asness, C. S., Moskowitz, T. J., & Pedersen, L. H. (2013). "Value and Momentum Everywhere." Journal of Finance.** 🟢 (AQR working paper version)
Shows value and momentum effects are global, persistent, and combinable. Critically: value alone has long drawdowns; combined with momentum, drawdowns shrink dramatically. *Implementation:* justifies why the system uses BOTH value AND technical/momentum signals — not value alone.

**Asness, C., Frazzini, A., & Pedersen, L. H. (2019). "Quality Minus Junk." Review of Accounting Studies.** 🟢 (AQR w/p)
Quality (profitability, growth, safety) is itself a return factor and pairs especially well with value. Filters out the "value trap" problem. *Implementation:* the Quality dimension in the Directional Thesis score should include profitability and safety subcomponents, not just valuation.

### Momentum and trend

**Jegadeesh, N., & Titman, S. (1993). "Returns to Buying Winners and Selling Losers." Journal of Finance.** 🟢 (preprint available)
Classic momentum paper. 3–12 month winners continue to outperform; 3–12 month losers continue to underperform. *Implementation:* the technical setup subcomponent includes 6-month return percentile.

**Moskowitz, T. J., Ooi, Y. H., & Pedersen, L. H. (2012). "Time Series Momentum." Journal of Financial Economics.** 🟢
Time-series momentum (trend-following on the same instrument) works as well or better than cross-sectional momentum. *Implementation:* the technical setup detection logic includes individual-stock trend signals, not just relative-strength rankings.

### Low volatility / quality

**Frazzini, A., & Pedersen, L. H. (2014). "Betting Against Beta." Journal of Financial Economics.** 🟢 (AQR w/p)
Low-beta stocks have higher risk-adjusted returns than high-beta stocks — opposite of what CAPM predicts. Relevant because option premiums on high-beta names are systematically expensive. *Implementation:* explains why the long-bias setup should tilt toward moderate-beta names; explains why the short-bias setup (shorting overvalued momentum names) faces persistent headwinds even when valuation is right.

### Analyst revisions

**Chan, L. K. C., Jegadeesh, N., & Lakonishok, J. (1996). "Momentum Strategies." Journal of Finance.** 🔴 (abstract + preprint available)
Earnings estimate revisions are themselves predictive of returns over 6–12 months. *Implementation:* the "Earnings momentum & revisions" subcomponent uses 30-day forward EPS revision direction.

---

## 2. Options pricing theory

### Foundational

**Black, F., & Scholes, M. (1973). "The Pricing of Options and Corporate Liabilities." Journal of Political Economy.** 📘 (foundational, covered exhaustively in textbooks)
B-S is the baseline. I compute B-S theoretical value and use deviations as one input to the option pricing edge score. *Implementation note:* B-S assumes constant vol, lognormal returns, no dividends, European exercise. American option pricing requires binomial/trinomial trees or Bjerksund-Stensland approximation.

**Bjerksund, P., & Stensland, G. (2002). "Closed-Form Valuation of American Options."** 🟢 (NHH working paper)
Fast, accurate closed-form approximation for American option pricing including dividends. *Implementation:* use this for the theoretical-value calculation in Layer 3, not raw B-S.

### Hull's textbook treatment

**Hull, J. C. "Options, Futures, and Other Derivatives" (latest edition).** 📘
The standard graduate text. Particularly chapters on: implied volatility surfaces, the Greeks, volatility smiles/skew, exotic option pricing where relevant. *Implementation:* anchor reference for Layer 3 (Options Chain Analysis). When we implement Greeks, IV calc, surface fitting, this is the canonical source.

### Volatility-specific

**Natenberg, S. "Option Volatility & Pricing." (2nd ed., 2014).** 📘
Practitioner's bible on volatility trading. Specifically the chapters on: vol skew, term structure, vega exposure, dispersion. *Implementation:* the IV-rank, skew-tilt, and term-structure inputs in the Option Pricing Edge dimension come from this framework.

**Sinclair, E. "Volatility Trading." (2nd ed., 2013).** 📘
More quantitative than Natenberg. Specifically the chapters on: realized-vs-implied vol estimators, GARCH-based realized vol forecasting, the variance risk premium. *Implementation:* the realized-vs-implied vol comparison (a core edge source) is built from this framework. The variance risk premium concept explains why systematically buying premium is hard, and informs our IV-rank filter.

---

## 3. The volatility risk premium (why long premium is hard)

This is the single most important body of literature for the strategy. Without understanding the VRP, one cannot explain why long-option positions lose money on average.

**Carr, P., & Wu, L. (2009). "Variance Risk Premiums." Review of Financial Studies.** 🟢 (preprint)
Implied variance systematically exceeds realized variance — option sellers earn a premium for bearing volatility risk. *Implementation:* this is THE justification for the IV-rank filter. Buy options when IV is below historical norms (when the VRP is compressed); avoid when it's elevated. Also justifies the calendar-spread setup (selling premium against long premium reduces VRP cost).

**Bollerslev, T., Tauchen, G., & Zhou, H. (2009). "Expected Stock Returns and Variance Risk Premia." Review of Financial Studies.** 🟢 (Fed Reserve w/p)
The VRP itself is time-varying and predictive. When VRP is high, future returns are stronger. *Implementation:* this hints at a Layer 2+ enhancement — use VRP as a regime indicator. Pending; not in v1.

---

## 4. Volatility skew, term structure, and the IV surface

**Bakshi, G., Kapadia, N., & Madan, D. (2003). "Stock Return Characteristics, Skew Laws, and the Differential Pricing of Individual Equity Options." Review of Financial Studies.** 🟢 (preprint available)
Individual stock options have systematically different skew structures from index options, driven by stock-specific risk asymmetries. *Implementation:* the option-pricing-edge score includes a skew-relative-to-history component (a stock priced with unusual skew may signal an opportunity or a trap).

**Cremers, M., & Weinbaum, D. (2010). "Deviations from Put-Call Parity and Stock Return Predictability." Journal of Financial and Quantitative Analysis.** 🟢 (preprint)
When put-call parity is violated (calls trade rich vs. puts or vice versa), the stock's future return has a directional bias. Real, persistent, monetizable signal. *Implementation:* PCP-deviation signal is a candidate addition to the Directional Thesis dimension. Pending in a later layer.

**Ang, A., Hodrick, R. J., Xing, Y., & Zhang, X. (2006). "The Cross-Section of Volatility and Expected Returns." Journal of Finance.** 🟢 (NBER w/p)
High idiosyncratic-volatility stocks earn LOWER returns. Counterintuitive but well-documented. *Implementation:* an additional filter — be cautious going long on high-IV-rank, high-idio-vol names; that's where retail loses the most.

---

## 5. Post-earnings announcement drift (PEAD)

The single most well-documented "free lunch" in academic finance.

**Bernard, V. L., & Thomas, J. K. (1989). "Post-Earnings-Announcement Drift: Delayed Price Response or Risk Premium?" Journal of Accounting Research.** 🟢 (preprint)
After a positive earnings surprise, the stock continues to drift up for 60+ days. Holds across decades, across markets, robust to controls. *Implementation:* THIS is the academic justification for the long-bias setup's "post-earnings beat where stock hasn't fully responded" entry rule.

**Ke, B., & Ramalingegowda, S. (2005). "Do Institutional Investors Exploit the Post-Earnings Announcement Drift?" Journal of Accounting and Economics.** 🟡
Institutions DO trade on PEAD, but it persists anyway. Limits to arbitrage. *Implementation:* good news — the edge isn't being arbitraged away. *Caveat:* this means competitors are looking at the same signals; selectivity on entry is required.

**Livnat, J., & Mendenhall, R. R. (2006). "Comparing the Post–Earnings Announcement Drift for Surprises Calculated from Analyst and Time Series Forecasts." Journal of Accounting Research.** 🟢 (preprint)
PEAD is stronger when measured against analyst expectations than against historical-time-series expectations. *Implementation:* use analyst-surprise definition, not time-series.

---

## 6. Market microstructure & options liquidity

Foundation for limit-order execution and microstructure-aware trade timing.

**Glosten, L. R., & Milgrom, P. R. (1985). "Bid, Ask and Transaction Prices in a Specialist Market with Heterogeneously Informed Traders." Journal of Financial Economics.** 🔴 (well-summarized in textbooks)
The original adverse selection model. Market makers widen spreads to protect against informed traders. *Implementation:* a sober reminder why one cannot simply "be the market maker" — informed flow eats retail.

**Easley, D., Kiefer, N. M., & O'Hara, M. (1996). "Cream-Skimming or Profit-Sharing? The Curious Role of Purchased Order Flow." Journal of Finance.** 🔴 (abstract + working papers)
Order flow is sorted by informational content; retail flow is the "uninformed" tier MMs prefer to interact with. *Implementation:* MMs WILL fill the limits, but only because the flow looks uninformed. The adverse-selection risk is real but priced into wide spreads. I accept this trade-off.

**Mayhew, S. (2002). "Competition, Market Structure, and Bid-Ask Spreads in Stock Option Markets." Journal of Finance.** 🟢 (preprint available)
Quantifies how option spreads vary with competition, volume, and underlying volatility. *Implementation:* informs the Liquidity & Exit Feasibility dimension — specifically, how to score spreads relative to what's "normal" for that name and IV level.

**Christoffersen, P., Goyenko, R., Jacobs, K., & Karoui, M. (2018). "Illiquidity Premia in the Equity Options Market." Review of Financial Studies.** 🟢 (preprint)
There IS a return premium for holding illiquid options to expiry — but it comes with substantial mark-to-market volatility. *Implementation:* validates the thesis that less-traded options can have edge, BUT only if you can hold them. Strengthens the case for the patient approach.

---

## 7. Behavioral finance & mispricing

**Lakonishok, J., Shleifer, A., & Vishny, R. W. (1994). "Contrarian Investment, Extrapolation, and Risk." Journal of Finance.** 🟢 (preprint)
Value strategies work in part because investors extrapolate recent results too far. *Implementation:* this is the academic underpinning of the contrarian-value bias in the long-bias setup.

**Daniel, K., Hirshleifer, D., & Subrahmanyam, A. (1998). "Investor Psychology and Security Market Under- and Overreactions." Journal of Finance.** 🟢 (preprint)
Overconfidence and biased self-attribution drive both PEAD and longer-term reversal. *Implementation:* unified theory for why both the long-bias setup (PEAD-leveraging) and the short-bias setup (overvaluation-reversal) can work — they exploit complementary errors.

**Stein, J. C. (1989). "Overreactions in the Options Market." Journal of Finance.** 🔴 (abstract + summaries)
Long-dated options react too strongly to short-term news. *Implementation:* this is a candidate signal — when long-dated IV moves more than short-dated IV on news, the long-dated is mispriced. Pending Layer 3.

---

## 8. Catalyst-driven trading

Less academic, more practitioner. The literature is thin because catalysts are idiosyncratic.

**Hartzmark, M. L., & Solomon, D. H. (2018). "Reconsidering Returns." Review of Financial Studies.** 🟢 (preprint)
Returns around earnings announcements account for a disproportionate share of total annual return. *Implementation:* validates concentrating around catalysts.

**Patell, J. M., & Wolfson, M. A. (1979). "Anticipated Information Releases Reflected in Call Option Prices." Journal of Accounting and Economics.** 🔴 (covered in textbooks)
IV rises into expected announcements, then collapses post-announcement (IV crush). *Implementation:* fundamental to short-bias catalyst-window timing. The catalyst-window scoring component should penalize trades that depend on IV crush they're not positioned for.

**Gao, C., Xing, Y., & Zhang, X. (2018). "Anticipating Uncertainty: Straddles around Earnings Announcements." Journal of Financial and Quantitative Analysis.** 🟡
The implied move from straddle pricing is a remarkably good (but imperfect) predictor of actual realized earnings-move. *Implementation:* the implied-vs-realized-move comparison should be a core input to catalyst trade ranking.

---

## 9. Position sizing & risk

**Kelly, J. L. Jr. (1956). "A New Interpretation of Information Rate." Bell System Technical Journal.** 📘 (foundational)
The Kelly criterion: optimal bet size as a function of edge and odds. The standard reference for conviction-based sizing.

**Thorp, E. O. "The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market." (2007, paper).** 🟢
Practitioner extension of Kelly to financial markets. Includes the famous "fractional Kelly" rule (typically bet ¼ to ½ of full Kelly to control drawdown). *Implementation:* fractional Kelly with conservative scaling anchors any explicit position-sizing rule.

**MacLean, L. C., Thorp, E. O., & Ziemba, W. T. (2010). "Long-Term Capital Growth: The Good and Bad Properties of the Kelly and Fractional Kelly Capital Growth Criteria." Quantitative Finance.** 🔴 (well-summarized; preprint partial)
Full Kelly maximizes growth but has terrible drawdowns. Quarter-Kelly is the practitioner sweet spot. *Implementation:* sizing rules at the position level should target roughly quarter-Kelly equivalents.

---

## 10. Practitioner books (full reading list)

These are the "must read in full" list. Each covers material referenced piecewise above, but the books are where the practical intuition lives.

- **Hull, J. C. "Options, Futures, and Other Derivatives."** 📘 — Canonical textbook.
- **Natenberg, S. "Option Volatility & Pricing."** 📘 — Practitioner bible.
- **Sinclair, E. "Volatility Trading."** 📘 — Quant-flavored.
- **Sinclair, E. "Positional Option Trading."** 📘 — Directional option strategies. Highly relevant.
- **Taleb, N. N. "Dynamic Hedging."** 📘 — Risk-management view from a former MM.
- **Connors, L., & Alvarez, C. "Short Term Trading Strategies That Work."** 📘 — Mean-reversion technical setups. Mixed academic quality but worth reading for the screening intuitions.
- **Pedersen, L. H. "Efficiently Inefficient."** 📘 — How real hedge funds make money. Chapter 6 (Discretionary Equity Investing) and Chapter 11 (Volatility Trading) are directly relevant.
- **Lo, A. W., & MacKinlay, A. C. "A Non-Random Walk Down Wall Street."** 📘 — Empirical evidence on serial correlation and predictability.

---

## 11. Hedge fund methods — what we know from public sources

We cannot access proprietary methods. What we CAN learn from public talks, books, court filings, and post-fund memoirs:

**Renaissance / Medallion (from Zuckerman's "The Man Who Solved the Market"):**
- Statistical arbitrage across thousands of instruments
- Heavy reliance on hidden Markov models, kernel regression
- Edge in microstructure and execution as much as in signal
- *Lesson:* their edge requires scale, infrastructure, and PhD density a personal book cannot replicate. Different terrain.

**Citadel (from public-facing materials, S-1s of related entities):**
- Multi-strategy with significant market-making
- Quant equities, fixed income, commodities
- *Lesson:* Citadel makes money from BEING the market maker who collects the retail spread tax. They are the counterparty. Recognize this.

**Two Sigma, DE Shaw (from public talks):**
- Heavy use of alternative data
- Significant infrastructure investment in low-latency execution
- *Lesson:* same as Renaissance — different terrain.

**AQR (from extensive published research):**
- The most useful hedge fund to study because they publish nearly everything
- Factor-based, lower-frequency, persistent strategies
- *Lesson:* their published methodology is much closer to what is replicable at retail scale. The AQR working paper series should be a continuous reading source.

---

## 12. Working paper series to monitor continuously

These are sources I re-check periodically as Layers 1–6 are built out:

- **SSRN** — Especially the Financial Economics Network and the Derivatives & Hedging eJournals
- **NBER Working Papers — Asset Pricing program**
- **AQR Capital Working Papers** (free, published, high quality)
- **Federal Reserve FEDS series** — for macro/regime work
- **arXiv q-fin** — for newer methods, ML in finance

---

## 13. Topics flagged for follow-up research

Items I know exist as literature but haven't yet researched in detail. These become research tasks in Layer 1:

1. **Earnings surprise + IV behavior:** how does the IV term structure react to earnings surprise vs. miss? Implications for diagonal spread structuring.
2. **Skew as a predictor:** the relationship between equity option skew and short-horizon stock returns (some evidence it predicts crashes).
3. **Limits to arbitrage in single-name options:** why does the PCP-deviation signal persist?
4. **Optimal order-splitting in options:** if we want to enter a $10k position, do we use one limit or three smaller laddered limits?
5. **Volatility forecasting models:** GARCH, HAR-RV, ML-based — which provides the best 30-day realized vol forecast for individual large-caps?
6. **Sector rotation indicators:** academic literature on identifying sector regime changes.

---

## 14. What I have NOT and CANNOT access

Honest limitations:

- Sell-side analyst research (Goldman, MS, JPM proprietary reports)
- Bloomberg / Refinitiv proprietary data
- OptionMetrics / IvyDB IvyOPT history — would dramatically help backtesting; costs money
- Live or full-history NBBO data from CBOE — only summary/EOD historicals are publicly cheap
- Pre-1996 academic work that's not been digitized

Pushing to "institutional-grade" backtesting in Layer 6 would require budgeting for OptionMetrics or ORATS (~$1-5k/year for academic-tier license). For Layer 0-4, this is not yet required.

---

## 15. Version control

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-05-15 | Initial draft |
