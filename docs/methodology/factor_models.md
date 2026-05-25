# Methodology — Factor Models

**Status:** Draft v0.1
**Layer:** Foundation (Layer 0); consumed by Layer 1 (universe screen) and Layer 4 (ranking)
**Last updated:** 2026-05-15
**Purpose:** Defines the exact factor calculations used in fundamental and statistical scoring. Every formula is implementable in Python from this document alone. Where two methods are reasonable, this document picks one and explains why.

---

## 1. Doc structure

Each factor section includes: (1) concept and academic anchor, (2) raw inputs required, (3) formula, (4) parameter choices with justification, (5) edge cases, (6) Python pseudocode, (7) integration with the ranking system.

---

## 2. Universal preprocessing rules

These apply to every factor BEFORE the factor-specific math.

### 2.1 Winsorization

Trim extreme values at the 1st and 99th percentiles within each cross-section (the universe on a given date). Reason: financial data is fat-tailed; a single outlier can dominate z-scores.

```python
def winsorize(series: pd.Series, lower_pct: float = 0.01, upper_pct: float = 0.99) -> pd.Series:
    lower = series.quantile(lower_pct)
    upper = series.quantile(upper_pct)
    return series.clip(lower=lower, upper=upper)
```

### 2.2 Cross-sectional z-score normalization

After winsorization, convert each factor to a z-score within the cross-section:

```
z_i = (x_i - mean(x)) / std(x)
```

This gives a comparable scale across factors regardless of native units (P/E ratios, growth rates, etc.).

```python
def z_score(series: pd.Series) -> pd.Series:
    return (series - series.mean()) / series.std(ddof=0)
```

### 2.3 Sector neutralization (when applicable)

For factors where sector-level effects dominate (notably value — software always looks expensive, banks always look cheap), compute the z-score WITHIN each sector instead of globally:

```python
def sector_z_score(df: pd.DataFrame, value_col: str, sector_col: str) -> pd.Series:
    return df.groupby(sector_col)[value_col].transform(
        lambda x: (x - x.mean()) / x.std(ddof=0)
    )
```

**Which factors are sector-neutralized in this system:**
- Value: YES (sector-neutralized)
- Quality: PARTIAL — neutralize ROE and margins; do not neutralize debt ratios
- Momentum: NO (cross-sectional momentum works better unneutralized)
- Low-vol: NO
- Revisions: NO

### 2.4 Composite score direction

By convention, every factor's z-score is oriented so that HIGHER is BETTER for a long position. Inverse where needed (e.g., P/E: low is good, so use negative log of P/E, or use earnings yield E/P directly).

---

## 3. Value factor

### 3.1 Concept

The value factor captures the premium earned by buying stocks cheap relative to fundamentals (Fama-French 1992; Lakonishok et al. 1994). The historical risk premium is ~3-5% annual, but with multi-year drawdowns (e.g., 2017-2019).

In this system, value drives the long-bias setup's universe entry. I use it both cross-sectionally (cheap relative to peers) and time-series (cheap relative to its own 5-year history).

### 3.2 Sub-metrics

We use four value sub-metrics. Each is z-scored within sector, then averaged into a composite value score.

**1. Earnings Yield (inverse P/E)**

```
EY = TTM_Net_Income / Market_Cap
```

Why TTM (trailing 12 months) rather than forward: forward estimates introduce analyst-bias noise. We capture revisions separately.

**2. EBIT Yield (inverse EV/EBIT)**

```
EBIT_Yield = TTM_EBIT / Enterprise_Value
where Enterprise_Value = Market_Cap + Total_Debt - Cash_and_Equivalents
```

Better than P/E for cross-capital-structure comparison (Greenblatt's Magic Formula draws on this).

**3. Free Cash Flow Yield**

```
FCF_Yield = TTM_Free_Cash_Flow / Market_Cap
where TTM_FCF = Operating_Cash_Flow - CapEx
```

Cash flow is harder to manipulate than earnings — more robust signal.

**4. Book-to-Market (B/M)**

```
B/M = Common_Equity / Market_Cap
```

The classic Fama-French value metric. We include it for continuity with the academic literature, but weight it less than the yield-based metrics (book value is increasingly noisy in an intangibles-heavy economy).

### 3.3 Composite value score

```python
def compute_value_score(df: pd.DataFrame) -> pd.Series:
    df = df.copy()
    df['ey'] = df['ttm_net_income'] / df['market_cap']
    df['ebit_y'] = df['ttm_ebit'] / df['enterprise_value']
    df['fcf_y'] = (df['ttm_ocf'] - df['ttm_capex']) / df['market_cap']
    df['bm'] = df['common_equity'] / df['market_cap']

    # Winsorize then sector-neutralize z-score
    for col in ['ey', 'ebit_y', 'fcf_y', 'bm']:
        df[col] = winsorize(df[col])
        df[f'{col}_z'] = sector_z_score(df, col, 'sector')

    # Weighted average
    df['value_score'] = (
        0.30 * df['ey_z']
        + 0.30 * df['ebit_y_z']
        + 0.25 * df['fcf_y_z']
        + 0.15 * df['bm_z']
    )
    return df['value_score']
```

### 3.4 Time-series value (own-history percentile)

In addition to cross-sectional, compute each name's current valuation vs. its own 5-year history. This is the "Valuation rank vs. 5y history" sub-component of the ranking framework.

```python
def historical_value_percentile(df: pd.DataFrame, lookback_years: int = 5) -> float:
    """
    df: time series of P/E (or other valuation metric) for ONE stock
    Returns: percentile of CURRENT value within the lookback window.
    """
    current = df['pe'].iloc[-1]
    history = df['pe'].iloc[-(lookback_years * 252):]
    return (history < current).sum() / len(history)  # 0 = cheapest, 1 = most expensive
```

For the long-bias setup, this percentile should be < 0.30 (in the cheapest 30% of own history).

### 3.5 Edge cases & pitfalls

- **Negative earnings:** E/P is meaningless. Use median for the sector or set z-score to 0 with a quality penalty flag.
- **Negative book value:** rare for large caps but happens (e.g., heavily levered companies, recent buybacks). Drop B/M from composite for that name; reweight other components.
- **Dilution events:** if shares outstanding jumped >10% in the quarter, re-check market cap and equity figures. Stale share count is a common data error.
- **Special items in earnings:** ideally use adjusted earnings excluding one-time items; in practice, use GAAP and accept the noise. Quality factor catches some of this.
- **Spin-offs and M&A:** TTM figures are corrupted for ~12 months post-event. Flag and exclude.

### 3.6 Calibration

Historical premium for the value factor (US large-cap, 1963-present): ~3-4% annualized, Sharpe ~0.25-0.35. This is NOT expected to be the sole driver of returns — value alone has periods of 5+ year underperformance.

### 3.7 Integration

The composite `value_score` is one input to the **Directional thesis quality** dimension. For the long-bias setup, higher value_score → higher dimension score. For the short-bias setup, INVERTED — lower value_score (richly valued) → higher dimension score.

---

## 4. Quality factor

### 4.1 Concept

Quality captures profitability, growth stability, and balance sheet strength. Combined with value, it filters out "value traps" — cheap stocks that deserve to be cheap because the business is deteriorating (Asness, Frazzini, Pedersen 2019 "Quality Minus Junk").

### 4.2 Sub-metrics

Four pillars, each with multiple sub-components.

**Profitability (40% of composite):**
- Gross Profit / Assets (Novy-Marx 2013 — strongest single quality metric in academic studies)
- Return on Equity (ROE) = Net Income / Average Common Equity
- Return on Invested Capital (ROIC) = NOPAT / (Total Debt + Equity - Cash)

**Growth stability (25% of composite):**
- 5-year revenue CAGR
- 5-year EPS CAGR
- Standard deviation of YoY EPS growth (lower is better — invert)

**Safety (20% of composite):**
- Debt-to-Equity ratio (invert: lower D/E better)
- Interest Coverage = EBIT / Interest Expense (higher better)
- Altman Z-Score (composite distress predictor; higher better)

**Capital allocation (15% of composite):**
- Cash conversion = OCF / Net Income (closer to or above 1.0 is good)
- Share count change (decreasing is positive)
- Reinvestment rate × ROIC (proxies for compounding ability)

### 4.3 Formula

```python
def compute_quality_score(df: pd.DataFrame) -> pd.Series:
    # Profitability
    df['gp_to_assets'] = df['gross_profit'] / df['total_assets']
    df['roe'] = df['ttm_net_income'] / df['avg_common_equity']
    df['roic'] = df['nopat'] / (df['total_debt'] + df['common_equity'] - df['cash'])

    # Growth stability
    df['rev_cagr_5y'] = compute_cagr(df['revenue_5y_history'])
    df['eps_cagr_5y'] = compute_cagr(df['eps_5y_history'])
    df['eps_growth_std_5y'] = df['eps_yoy_growth_5y'].std()

    # Safety
    df['debt_to_equity'] = df['total_debt'] / df['common_equity']
    df['interest_coverage'] = df['ebit'] / df['interest_expense']
    df['altman_z'] = compute_altman_z(df)

    # Capital allocation
    df['cash_conversion'] = df['ttm_ocf'] / df['ttm_net_income']
    df['share_count_chg_5y'] = df['shares_now'] / df['shares_5y_ago'] - 1
    df['reinvestment_roic'] = df['retention_ratio'] * df['roic']

    # Convert each to sector z-score; invert where lower is better
    metrics = {
        'gp_to_assets':       (+1, 'profitability'),
        'roe':                (+1, 'profitability'),
        'roic':               (+1, 'profitability'),
        'rev_cagr_5y':        (+1, 'growth'),
        'eps_cagr_5y':        (+1, 'growth'),
        'eps_growth_std_5y':  (-1, 'growth'),  # invert: lower variance = higher quality
        'debt_to_equity':     (-1, 'safety'),
        'interest_coverage':  (+1, 'safety'),
        'altman_z':           (+1, 'safety'),
        'cash_conversion':    (+1, 'capital'),
        'share_count_chg_5y': (-1, 'capital'),
        'reinvestment_roic':  (+1, 'capital'),
    }

    pillars = {'profitability': 0.40, 'growth': 0.25, 'safety': 0.20, 'capital': 0.15}
    pillar_scores = {}
    for pillar in pillars:
        cols = [c for c, (sign, p) in metrics.items() if p == pillar]
        signs = [metrics[c][0] for c in cols]
        z = pd.DataFrame({c: sign * sector_z_score(df, c, 'sector') for c, sign in zip(cols, signs)})
        pillar_scores[pillar] = z.mean(axis=1)

    return sum(pillars[p] * pillar_scores[p] for p in pillars)
```

### 4.4 Altman Z-Score (auxiliary calc)

```python
def compute_altman_z(df: pd.DataFrame) -> pd.Series:
    """Z-Score for public manufacturers; adjusted variant for non-manufacturers."""
    A = df['working_capital'] / df['total_assets']
    B = df['retained_earnings'] / df['total_assets']
    C = df['ebit'] / df['total_assets']
    D = df['market_cap'] / df['total_liabilities']
    E = df['revenue'] / df['total_assets']
    return 1.2*A + 1.4*B + 3.3*C + 0.6*D + 1.0*E
```

Interpretation: Z > 2.99 safe; 1.81-2.99 grey zone; < 1.81 distress probable.

### 4.5 Edge cases

- **Negative equity:** ROE undefined. Drop that metric for the name; reweight.
- **Financial sector:** standard quality metrics (D/E, interest coverage) don't apply meaningfully to banks. For Phase 1, exclude financials entirely from the universe; revisit in Phase 2 with a banks-specific quality framework (NIM, Tier 1 capital, NPL ratios).
- **Heavy intangibles:** book-value-based metrics understate quality of intangible-heavy businesses. Use ROIC excluding goodwill where possible.

### 4.6 Calibration

QMJ (Quality Minus Junk) factor historical premium: ~4-5% annualized, Sharpe ~0.40-0.55. Combined with value, "Quality Value" strategies have Sharpe ~0.6-0.8 in academic studies.

---

## 5. Momentum factor

### 5.1 Concept

Stocks that have outperformed over 3-12 months tend to continue outperforming for another 3-6 months (Jegadeesh & Titman 1993). The standard formulation skips the most recent month to avoid short-term reversal effects.

### 5.2 Formula — Cross-sectional momentum

```python
def cross_sectional_momentum(prices: pd.Series, lookback_months: int = 12, skip_recent_months: int = 1) -> float:
    """
    Returns the price return from t-lookback to t-skip.
    Standard 'JT 12-1' momentum: 12-month return excluding most recent month.
    """
    p_now = prices.iloc[-skip_recent_months * 21]   # ~21 trading days per month
    p_then = prices.iloc[-lookback_months * 21]
    return (p_now / p_then) - 1
```

### 5.3 Multi-horizon momentum

We compute three momentum horizons and aggregate:

| Horizon | Lookback | Skip | Weight |
|---|---|---|---|
| Short | 3 months | 1 day | 0.25 |
| Medium | 6 months | 1 month | 0.35 |
| Long | 12 months | 1 month | 0.40 |

```python
def composite_momentum(prices: pd.Series) -> float:
    mom_3m = cross_sectional_momentum_days(prices, 63, 1)
    mom_6m = cross_sectional_momentum_days(prices, 126, 21)
    mom_12m = cross_sectional_momentum_days(prices, 252, 21)
    return 0.25 * mom_3m + 0.35 * mom_6m + 0.40 * mom_12m
```

### 5.4 Risk-adjusted momentum (residual momentum)

Standard momentum is correlated with market beta — high-beta stocks dominate momentum rankings in up markets and crash in turns. Residual momentum (Blitz, Huij, Martens 2011) removes the beta component:

```python
def residual_momentum(stock_returns: pd.Series, market_returns: pd.Series, lookback_months: int = 12) -> float:
    # Regress stock returns on market returns over the lookback
    # Use the residuals' cumulative return as the "residual momentum" signal
    from statsmodels.api import OLS, add_constant
    X = add_constant(market_returns.iloc[-lookback_months*21:-21])
    y = stock_returns.iloc[-lookback_months*21:-21]
    model = OLS(y, X).fit()
    residuals = y - model.predict(X)
    return (1 + residuals).prod() - 1
```

For our system: use BOTH raw and residual momentum, weight 60/40 toward raw (raw is what the market actually trades on; residual is what's robust).

### 5.5 Edge cases

- **Recent IPOs:** insufficient history. Require at least 18 months of trading before including.
- **Heavy news names:** large idiosyncratic moves (lawsuits, M&A rumors) corrupt momentum signal. Apply a daily-return cap (e.g., winsorize at ±15% daily moves before computing cumulative momentum).
- **Earnings dates within window:** earnings-day returns are mostly noise from the perspective of price momentum. Optional: exclude earnings-day returns from the cumulative.

### 5.6 Calibration

Historical 12-1 momentum premium: ~6-8% annualized, Sharpe ~0.4-0.5, BUT has occasional severe drawdowns (2009, 2016 momentum crashes). I use momentum as a confirming signal, not a primary driver.

---

## 6. Low volatility factor

### 6.1 Concept

Low-volatility stocks historically deliver higher risk-adjusted returns than high-volatility stocks (Ang, Hodrick, Xing, Zhang 2006; Frazzini & Pedersen 2014 BAB). Anomalous from a CAPM perspective but persistent.

For an options-trading system, this factor matters two ways:
1. Long options on low-vol stocks have less "premium tax" since IV is lower
2. Avoid high-IV-rank/high-realized-vol names where retail loses the most

### 6.2 Formula

Multiple measures; combine three:

**1. Realized volatility (annualized stdev of daily log returns over 252 days)**

```python
def realized_vol(prices: pd.Series, lookback_days: int = 252) -> float:
    log_returns = np.log(prices / prices.shift(1)).iloc[-lookback_days:]
    return log_returns.std() * np.sqrt(252)
```

**2. Beta to S&P 500**

```python
def beta(stock_returns: pd.Series, market_returns: pd.Series, lookback_days: int = 252) -> float:
    cov = stock_returns.iloc[-lookback_days:].cov(market_returns.iloc[-lookback_days:])
    var = market_returns.iloc[-lookback_days:].var()
    return cov / var
```

**3. Idiosyncratic volatility (residual vol from market regression)**

```python
def idiosyncratic_vol(stock_returns, market_returns, lookback_days: int = 252) -> float:
    from statsmodels.api import OLS, add_constant
    X = add_constant(market_returns.iloc[-lookback_days:])
    y = stock_returns.iloc[-lookback_days:]
    model = OLS(y, X).fit()
    return model.resid.std() * np.sqrt(252)
```

### 6.3 Composite (signs inverted: lower is better, so we use NEGATIVE of each)

```python
def low_vol_score(df: pd.DataFrame) -> pd.Series:
    df['neg_realized_vol'] = -df['realized_vol_252d']
    df['neg_beta'] = -df['beta_252d']
    df['neg_idio_vol'] = -df['idio_vol_252d']

    z_rv = z_score(winsorize(df['neg_realized_vol']))
    z_beta = z_score(winsorize(df['neg_beta']))
    z_idio = z_score(winsorize(df['neg_idio_vol']))

    return 0.40 * z_rv + 0.25 * z_beta + 0.35 * z_idio
```

### 6.4 Edge cases & integration

- **Recent IPOs:** require 252 days of price history.
- **High-event names:** stocks with recent splits, spinoffs, M&A: vol estimates are corrupted for 60+ days post-event.
- For the long-bias setup: HIGHER low-vol score is BETTER (stable names with cheaper option premium).
- For the short-bias setup: NEUTRAL or slightly negative (shorting; low-vol shorts are not specifically desirable, but extreme high-vol is also undesirable).

---

## 7. Analyst revisions factor

### 7.1 Concept

Forward EPS estimate revisions are themselves predictive of future returns (Chan, Jegadeesh, Lakonishok 1996). Direction matters more than magnitude. The 30-90 day window is most predictive.

### 7.2 Formula

```python
def revision_score(df: pd.DataFrame) -> pd.Series:
    """
    df must contain:
    - eps_estimate_current: current consensus EPS estimate for next fiscal year
    - eps_estimate_30d_ago: same, 30 days ago
    - n_analysts: number of analysts covering
    - n_upward_revisions_30d: count of analysts revising up in last 30 days
    - n_downward_revisions_30d: count revising down
    """
    df['revision_magnitude'] = (df['eps_estimate_current'] - df['eps_estimate_30d_ago']) / df['eps_estimate_30d_ago'].abs()
    df['revision_breadth'] = (df['n_upward_revisions_30d'] - df['n_downward_revisions_30d']) / df['n_analysts']

    # Cross-sectional z-scores
    z_mag = z_score(winsorize(df['revision_magnitude']))
    z_breadth = z_score(df['revision_breadth'])  # already bounded [-1, 1], no winsorize

    return 0.50 * z_mag + 0.50 * z_breadth
```

### 7.3 Earnings surprise sub-factor

In addition to forward revisions, look at the most recent earnings surprise:

```python
def earnings_surprise(actual: float, estimate: float) -> float:
    return (actual - estimate) / abs(estimate) if estimate != 0 else 0
```

Combine with the revision score as a single "Earnings momentum & revisions" component of the Directional Thesis dimension:

```python
def earnings_momentum_revisions(df: pd.DataFrame) -> pd.Series:
    rev = revision_score(df)
    surprise = z_score(winsorize(df['latest_earnings_surprise']))
    return 0.60 * rev + 0.40 * surprise
```

### 7.4 Edge cases

- **Companies with no analyst coverage:** rare at $10B+ market cap, but if it happens, set z-score to 0 (neutral) rather than NaN.
- **Stale estimates:** if estimates haven't been updated in > 60 days, treat as low-information; downgrade score weight to half.
- **Loss companies:** revisions on negative EPS are confusing (a less-negative estimate is an upward revision). Use absolute-value denominators carefully.

---

## 8. Combining factor scores into the directional thesis dimension

The 5-dimension ranking lists four sub-components of Directional Thesis (5 pts each). Mapping factor scores to those sub-components:

| Sub-component | Max | Maps to |
|---|---|---|
| Valuation rank vs. 5y history | 5 | Time-series value percentile (Section 3.4) |
| Earnings momentum & revisions | 5 | `earnings_momentum_revisions` (Section 7.3) |
| Technical setup confluence | 5 | Computed in Layer 2 (technical-setups methodology, forthcoming) |
| Sentiment & positioning | 5 | Short interest, IV skew, analyst dispersion (see Section 9 below) |

**Conversion from z-score to 0-5 sub-component score:**

```python
def z_to_score(z: float, max_pts: float = 5.0) -> float:
    """
    Map z-score to a 0-max_pts scale.
    z = +2 or higher -> max_pts
    z = 0 -> max_pts / 2
    z = -2 or lower -> 0
    Linear interpolation between.
    """
    clipped = max(min(z, 2.0), -2.0)
    return max_pts * (clipped + 2.0) / 4.0
```

For the long-bias setup: positive z → higher score.
For the short-bias setup: INVERT the z-score sign before applying `z_to_score`.

---

## 9. Sentiment & positioning sub-component

This is the fourth sub-component of Directional Thesis. Combines:

**1. Short interest (% of float):**
- For the long-bias setup: low SI is mildly positive (no crowded short)
- For the short-bias setup: low SI is positive (market isn't already short)

**2. Options skew (25-delta put IV vs. 25-delta call IV):**

```python
def options_skew_25d(option_chain: pd.DataFrame, expiry_days: int) -> float:
    """
    Returns: put_iv_25d - call_iv_25d (positive = puts trade rich, market expects downside)
    """
    near_expiry = option_chain[option_chain['dte'].between(expiry_days - 7, expiry_days + 7)]
    put_25d = near_expiry[(near_expiry['type'] == 'put') & near_expiry['delta'].between(-0.30, -0.20)]
    call_25d = near_expiry[(near_expiry['type'] == 'call') & near_expiry['delta'].between(0.20, 0.30)]
    return put_25d['iv'].mean() - call_25d['iv'].mean()
```

Interpretation: elevated put skew → market positioned for downside → mildly bullish contrarian signal (when extreme).

**3. Analyst dispersion:**

```python
def analyst_dispersion(eps_estimates: list) -> float:
    """Std of EPS estimates divided by absolute mean (coefficient of variation)."""
    arr = np.array(eps_estimates)
    return arr.std() / abs(arr.mean()) if arr.mean() != 0 else np.inf
```

High dispersion = analysts disagree = informational opportunity for differentiated view.

### Composite sentiment score

```python
def sentiment_score(df: pd.DataFrame, side: str) -> pd.Series:
    si_z = z_score(winsorize(-df['short_interest_pct']))   # invert: lower SI = better for long
    skew_z = z_score(winsorize(df['options_skew_25d']))
    disp_z = z_score(winsorize(df['analyst_dispersion']))

    if side == 'long':
        return 0.35 * si_z + 0.30 * skew_z + 0.35 * disp_z
    elif side == 'short':
        # For shorts: high SI is bad (crowded), high skew is bad (puts already priced in)
        si_short = z_score(winsorize(df['short_interest_pct']))      # NOT inverted
        skew_short = z_score(winsorize(-df['options_skew_25d']))     # invert
        return 0.40 * si_short + 0.30 * skew_short + 0.30 * disp_z
```

---

## 10. Sector & industry handling

### 10.1 Sector taxonomy

Use GICS sectors (11 top-level: Energy, Materials, Industrials, Consumer Disc, Consumer Staples, Health Care, Financials, IT, Comm Services, Utilities, Real Estate). Always available via IBKR / yfinance / FMP.

### 10.2 Within-sector ranking vs. global ranking

For the universe-screen phase (Layer 1):
- Value factor: within-sector ranking (sectors have persistent valuation differentials)
- Quality, Momentum, Low-Vol, Revisions: global ranking (these factors are less sector-dependent)

For final candidate filtering, also enforce diversification: no more than 2 names from the same sector in the final top-N shortlist.

### 10.3 Sector exclusions (Phase 1)

For Phase 1, exclude:
- Financials (need bank-specific framework)
- Utilities (low-vol, low-return; not worth the option premium)
- Real Estate (REIT fundamentals are different — REITs valued on FFO not EPS — requiring a sector-specific scoring framework)

Revisit in Phase 2 with sector-specific scoring frameworks.

---

## 11. Putting it all together — universe-screen pipeline

```python
def run_layer1_universe_screen(date: str) -> pd.DataFrame:
    """
    Top-level pipeline for Layer 1.
    Returns: DataFrame with universe + all factor scores + setup eligibility.
    """
    # 1. Pull eligible universe (market cap, volume, optionability filters)
    universe = load_eligible_universe(date)

    # 2. Pull fundamentals
    fundamentals = load_fundamentals(universe['ticker'], date)

    # 3. Pull price history
    prices = load_price_history(universe['ticker'], date, lookback_days=400)

    # 4. Pull analyst data
    analyst = load_analyst_data(universe['ticker'], date)

    # 5. Compute each factor score
    df = universe.merge(fundamentals, on='ticker').merge(analyst, on='ticker')

    df['value_score'] = compute_value_score(df)
    df['quality_score'] = compute_quality_score(df)
    df['momentum_score'] = compute_momentum_for_universe(prices)
    df['lowvol_score'] = compute_lowvol_for_universe(prices)
    df['revisions_score'] = earnings_momentum_revisions(df)

    # 6. Time-series value percentile (per stock, own 5y history)
    df['valuation_history_pct'] = df['ticker'].apply(
        lambda t: historical_value_percentile(load_long_history(t, years=5))
    )

    # 7. Setup eligibility flags
    df['eligible_long_setup'] = (
        (df['valuation_history_pct'] < 0.30) &
        (df['quality_score'] > -0.5) &  # not deteriorating
        (df['revisions_score'] > -1.0)
    )
    df['eligible_short_setup'] = (
        (df['valuation_history_pct'] > 0.75) &
        (df['momentum_score'] > 0.5) &  # still has momentum to fade
        (df['short_interest_pct'] < 0.04)  # not crowded short
    )

    # 8. Snapshot
    df.to_parquet(f'data/universe/{date}/universe.parquet')
    return df
```

---

## 12. Validation tests

Every layer-output must pass these checks before being consumed downstream.

1. **Factor distribution stability:** mean of each z-scored factor should be near 0 (|mean| < 0.05); std near 1 (within [0.95, 1.05]). Sudden shifts flag data issues.
2. **Coverage check:** ≥ 95% of universe names should have non-null scores for every factor. Below threshold → upstream data issue.
3. **Hand-check sample:** randomly select 10 names per run; manually verify computed factors against external source (Bloomberg, Yahoo, broker) within a tolerance of 5%.
4. **Backtest sanity:** historical realized factor premia should be in the academic range (e.g., value: 2-5% annualized; momentum: 5-10%; quality: 3-6%). Wildly different premia in backtest indicates a calculation error.
5. **No look-ahead:** for ANY computation using a "current" value, that value must be timestamped to match the simulation date. Especially analyst revisions and fundamentals (which have reporting lags).

---

## 13. Open questions for further research

These are flagged for Phase 2 / 3 deepening:

1. **Earnings quality adjustments:** accrual-based earnings quality metrics (Sloan 1996) — does adding them meaningfully improve the quality factor?
2. **Intangibles-adjusted value:** book value in 2026 understates the "true" capital of intangible-heavy businesses. Worth using Peters-Taylor intangibles-adjusted book value?
3. **Machine-learning factor combiner:** instead of linear weights, use a gradient-boosted tree to combine factor inputs. Tradeoff: gains in fit, losses in interpretability.
4. **Macro-conditional factor weights:** factor premia vary with macro regime (interest rate, vol regime). Dynamic weighting based on regime detection.
5. **Sentiment from text:** earnings call sentiment, 10-K MD&A sentiment, news flow. NLP-driven additional signal.

---

## 14. Version control

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-05-15 | Initial draft |
