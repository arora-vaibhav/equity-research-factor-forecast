# Methodology — Volatility Analysis

**Status:** Draft v0.1
**Layer:** Foundation (Layer 0); consumed by Layer 3 (options chain analysis) and Layer 4 (ranking)
**Last updated:** 2026-05-15
**Purpose:** Defines methods for measuring realized volatility, characterizing the implied vol surface (IV rank, skew, term structure), forecasting future realized vol, and applying the variance risk premium to trade selection. This is the most important methodology doc for the strategy — if vol is not handled correctly, every other layer suffers.

---

## 1. Why vol matters most for this strategy

For an option buyer, the return depends on three things:
1. **Direction** (delta) — does the underlying move the right way?
2. **Magnitude** (gamma/vega exposure) — does it move ENOUGH?
3. **Time** (theta) — does it move in time?

The variance risk premium says: on average, the market sells implied vol higher than what realizes. Translation: long-premium strategies fight a structural headwind. To win, one must either:
- Buy when IV is unusually LOW relative to its history (the VRP is compressed)
- Buy when the directional view is strong enough to overcome the average vol bleed
- Sell premium instead (avoided here except in spreads, because of the unlimited-risk profile of naked shorts)

This document defines how I measure and exploit these.

---

## 2. Realized volatility estimators

Multiple estimators exist. They differ in efficiency (variance of the estimate) and bias (under various microstructure assumptions). I use close-to-close as the baseline and Yang-Zhang as the high-quality alternative.

### 2.1 Close-to-close (CC) — the baseline

The simplest. Uses daily log returns. Standard for academic work.

```python
def realized_vol_cc(prices: pd.Series, lookback_days: int = 21) -> float:
    """
    Annualized close-to-close realized volatility.
    prices: daily close prices.
    """
    log_returns = np.log(prices / prices.shift(1)).iloc[-lookback_days:]
    return log_returns.std(ddof=1) * np.sqrt(252)
```

Pros: simple, universal. Cons: ignores intraday range information; less efficient than range-based estimators.

### 2.2 Parkinson (P) — high-low range

Uses high and low prices; much more efficient than CC (about 5× the same precision with same data).

```python
def realized_vol_parkinson(df: pd.DataFrame, lookback_days: int = 21) -> float:
    """
    df must contain 'high' and 'low' columns.
    Assumes no drift; not robust to drift.
    """
    hl = np.log(df['high'] / df['low']).iloc[-lookback_days:]
    return np.sqrt((1 / (4 * np.log(2))) * (hl ** 2).mean()) * np.sqrt(252)
```

Pros: efficient. Cons: assumes no drift (slight bias for trending stocks); ignores opening jumps.

### 2.3 Garman-Klass (GK) — OHLC

Uses open, high, low, close. Even more efficient than Parkinson.

```python
def realized_vol_garman_klass(df: pd.DataFrame, lookback_days: int = 21) -> float:
    """df must contain 'open', 'high', 'low', 'close'."""
    log_hl = np.log(df['high'] / df['low']).iloc[-lookback_days:]
    log_co = np.log(df['close'] / df['open']).iloc[-lookback_days:]
    daily_var = 0.5 * log_hl**2 - (2 * np.log(2) - 1) * log_co**2
    return np.sqrt(daily_var.mean()) * np.sqrt(252)
```

Cons: still assumes no overnight gaps in its derivation.

### 2.4 Yang-Zhang (YZ) — drift-robust, gap-aware

The most accurate. Accounts for opening jumps, intraday range, and intraday drift. Recommended for our system.

```python
def realized_vol_yang_zhang(df: pd.DataFrame, lookback_days: int = 21) -> float:
    """
    Yang-Zhang volatility estimator.
    df: DataFrame with 'open', 'high', 'low', 'close' columns, indexed by date.
    Returns annualized vol.
    """
    df = df.iloc[-lookback_days-1:].copy()   # need previous close for overnight return
    df['prev_close'] = df['close'].shift(1)
    df = df.dropna()

    n = len(df)
    if n < 5:
        return np.nan

    # Overnight component
    overnight = np.log(df['open'] / df['prev_close'])
    overnight_var = overnight.var(ddof=1)

    # Open-to-close component
    open_close = np.log(df['close'] / df['open'])
    oc_var = open_close.var(ddof=1)

    # Rogers-Satchell component (drift-robust intraday)
    rs = (np.log(df['high'] / df['close']) * np.log(df['high'] / df['open'])
          + np.log(df['low'] / df['close']) * np.log(df['low'] / df['open']))
    rs_var = rs.mean()

    # Yang-Zhang composite
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    yz_var = overnight_var + k * oc_var + (1 - k) * rs_var

    return np.sqrt(yz_var * 252)
```

Pros: most accurate, handles gaps and drift. Cons: requires full OHLC data.

### 2.5 Which estimator to use when

- **For IV rank computation** (1-year window): use Close-to-Close. Standard, comparable to broker IV calcs.
- **For realized-vs-implied edge measurement:** use Yang-Zhang on multiple horizons (10d, 21d, 63d).
- **For forecasting input:** use Yang-Zhang; it's the most data-efficient.

### 2.6 Sampling horizons

I compute realized vol on multiple windows:
- 5-day (1 week) — short-horizon, noisy
- 10-day (2 weeks) — short-term, useful for catalyst-window expectations
- 21-day (1 month) — the "standard" comparison to monthly IV
- 63-day (3 months) — medium-term, smoother
- 252-day (1 year) — long-term, for IV rank denominator

### 2.7 Validation

- Cross-check at least one estimator against the broker-reported HV (historical vol) figure. Should agree within ~5%.
- For known-quiet stocks (e.g., JNJ, PG), realized vol should be in the 15-25% range. For known-volatile (e.g., TSLA, COIN), 50-80%+. Sanity-check estimator output.

---

## 3. IV rank and IV percentile

### 3.1 Definitions

**IV rank** = (current IV - 1y_low_IV) / (1y_high_IV - 1y_low_IV) × 100

Range: 0-100. Where current IV sits between its 1-year low and 1-year high.

**IV percentile** = (% of days in past 1y where IV was BELOW today's IV) × 100

Range: 0-100. What fraction of the year was IV cheaper than now.

### 3.2 Which to use

IV rank is range-based; IV percentile is distribution-based. They give different answers for skewed IV distributions.

**Recommended:** use both, prefer IV percentile for filtering, IV rank for reporting (more intuitive).

```python
def iv_rank(current_iv: float, iv_history: pd.Series) -> float:
    """Returns IV rank 0-100."""
    iv_min, iv_max = iv_history.min(), iv_history.max()
    if iv_max == iv_min:
        return 50.0
    return 100 * (current_iv - iv_min) / (iv_max - iv_min)

def iv_percentile(current_iv: float, iv_history: pd.Series) -> float:
    """Returns IV percentile 0-100."""
    return 100 * (iv_history < current_iv).sum() / len(iv_history)
```

### 3.3 What "IV" to use

The IV used for rank/percentile must be a CONSISTENT measure across the time series. Common choices:
- **30-day ATM IV** — interpolated from the surface. Most common. Most stable.
- **VIX-style weighted IV** — variance-weighted across all strikes for a target maturity. More information; harder to compute.
- **Front-month ATM IV** — simple, but expiry-cycle artifacts cause noise.

**Recommended:** interpolate 30-day ATM IV from the surface daily, store the time series.

```python
def thirty_day_atm_iv(chain: pd.DataFrame, S: float) -> float:
    """
    Interpolate 30d ATM IV from the chain.
    """
    # Use chain's surface fit (from options_pricing.md Section 8.3)
    surface_fn = fit_iv_surface(chain)
    return surface_fn(strike=S, dte=30)
```

### 3.4 IV rank thresholds

For long-premium trades (the long-bias setup and the long leg of the calendar-spread setup):
- IV rank ≤ 30: **prime conditions**, premium cheap relative to history
- IV rank 30-50: acceptable
- IV rank 50-70: cautious — option already pricing some expectation
- IV rank > 70: avoid — premium expensive; almost certain to lose vega even if directionally right

For the short-bias setup (put spread): IV rank ≤ 60 acceptable (spread structure partially neutralizes vega).

### 3.5 Calibration & validation

- Require ≥ 200 trading days of IV history before computing rank/percentile. Below that, mark "insufficient data."
- Cross-check rank against Tastytrade/ThinkOrSwim displayed IVR for at least 5 names.

---

## 4. Implied vol skew

### 4.1 Definitions

Skew measures the asymmetry of IV across strikes for a given expiry. I use multiple representations:

**25-delta risk reversal:**
```
RR_25d = IV(call, |delta| = 0.25) - IV(put, |delta| = 0.25)
```
Negative RR = put skew (puts more expensive); typical for equities.

**10-delta wing skew:**
```
Skew_10d = IV(put, |delta| = 0.10) - IV(call, |delta| = 0.10)
```
Reflects tail-event pricing.

**Put-call skew ratio:**
```
PC_Skew = IV(25d put) / IV(25d call)
```

### 4.2 Skew rank

As with IV rank, compute the percentile of CURRENT skew within its 1-year history. Extreme skew readings are mean-reverting; trades can be structured around skew normalization.

```python
def skew_rank(current_skew: float, skew_history: pd.Series) -> float:
    return 100 * (skew_history < current_skew).sum() / len(skew_history)
```

### 4.3 Signal interpretation

- **Unusually elevated put skew** on a name (rank > 80): market pricing in significant downside risk. Two readings:
  - For the long-bias setup (long call): contrarian signal — puts expensive, calls cheap by comparison
  - For the short-bias setup (put spread): market already positioned; harder to gain edge
- **Unusually flat skew** (rank < 20): market complacent on downside. Favorable for the short-bias setup if the thesis is bearish.

### 4.4 Sub-component for the ranking framework

The **Option Pricing Edge** dimension can include a "skew opportunity" sub-score:

```python
def skew_edge_score(setup: str, current_skew: float, skew_history: pd.Series) -> float:
    """Returns 0-5 sub-score."""
    sr = skew_rank(current_skew, skew_history)
    if setup == 'long_bias':   # long call, want puts expensive (so calls cheap by parity)
        if sr > 75: return 5
        if sr > 60: return 3
        if sr > 40: return 2
        return 1
    elif setup == 'short_bias':   # bearish put spread, want skew NOT already extended
        if sr < 25: return 5
        if sr < 40: return 3
        if sr < 60: return 2
        return 1
    return 0
```

This is an optional v2 addition; v1 ranking framework treats option pricing edge with IV rank as the dominant signal.

---

## 5. Term structure of IV

### 5.1 Definitions

The IV "term structure" plots ATM IV against expiry. Shapes:

- **Contango** (normal): longer-dated IV > shorter-dated. Typical absent catalysts.
- **Backwardation**: shorter-dated IV > longer-dated. Often indicates upcoming binary event (earnings).
- **Flat**: no expectation differential across horizons.

### 5.2 Measurement

```python
def term_structure_ratio(chain: pd.DataFrame, S: float) -> dict:
    """
    Returns:
        front_30d_iv: ATM IV for front expiry (or 30d interpolated)
        back_60d_iv: ATM IV for 60-day expiry
        ts_ratio: front_30d / back_60d   (>1 = backwardation)
        ts_spread: front_30d - back_60d
    """
    surface = fit_iv_surface(chain)
    front_iv = surface(strike=S, dte=30)
    back_iv = surface(strike=S, dte=60)
    return {
        'front_30d_iv': front_iv,
        'back_60d_iv': back_iv,
        'ts_ratio': front_iv / back_iv,
        'ts_spread': front_iv - back_iv,
    }
```

### 5.3 Use in strategy

- **Calendar-spread setup:** ts_ratio ≥ 1.0 is desirable — backwardation means selling expensive front and buying cheap back.
- **Long-bias setup (long call):** prefer ts_ratio close to 1.0 or below (paying for the back, want it cheap).
- **Around earnings:** ts_ratio spikes to 1.2-1.5 pre-earnings, collapses to ~1.0 post-earnings (IV crush). This is part of the catalyst dynamic, covered in the catalyst-analytics methodology doc.

### 5.4 Forward variance / forward vol

To extract the "implied vol between dates" from two listed expiries:

```
σ_forward² · (T2 - T1) = σ_T2² · T2 - σ_T1² · T1

σ_forward = √[(σ_T2² · T2 - σ_T1² · T1) / (T2 - T1)]
```

This is useful for catalyst trades: the forward vol between an earnings date and a later expiry gives the "non-earnings vol" the market is pricing.

```python
def forward_vol(iv_T1: float, T1: float, iv_T2: float, T2: float) -> float:
    """T1, T2 in years; T2 > T1."""
    var_T1 = iv_T1**2 * T1
    var_T2 = iv_T2**2 * T2
    forward_var = (var_T2 - var_T1) / (T2 - T1)
    if forward_var <= 0:
        return np.nan
    return np.sqrt(forward_var)
```

---

## 6. Forecasting future realized volatility

This is the single most important capability for assessing IV edge: knowing what realized vol is LIKELY to do over the option's life lets one price IV honestly.

### 6.1 The hierarchy of models

From simplest to most sophisticated:

1. **Historical mean** — average of past realized vol over some window. Surprisingly hard to beat.
2. **EWMA (exponentially weighted moving average)** — gives more weight to recent observations.
3. **GARCH(1,1)** — explicit vol clustering model. Classic.
4. **HAR-RV (Heterogeneous Autoregressive Realized Variance)** — captures realized vol persistence across multiple horizons. Best-in-class for daily-to-monthly forecasts.
5. **Implied vol itself** — IV is a forecast of realized vol (with a risk premium overlay). On average, IV is an overestimate.
6. **Combination** — weighted average of HAR-RV + IV - constant VRP adjustment. Often the most accurate.

### 6.2 EWMA — the practical baseline

```python
def ewma_vol_forecast(returns: pd.Series, lambda_: float = 0.94, horizon_days: int = 21) -> float:
    """
    RiskMetrics EWMA volatility forecast.
    lambda_=0.94 is the classic RiskMetrics value for daily data.
    """
    returns = returns.dropna()
    weights = np.array([(1 - lambda_) * lambda_**i for i in range(len(returns))])
    weights = weights[::-1]  # most recent first
    weights /= weights.sum()
    daily_var = np.sum(weights * returns**2)
    return np.sqrt(daily_var * 252)
```

EWMA tends to be slightly more reactive than rolling averages without overfitting. Decent default.

### 6.3 GARCH(1,1)

Models conditional variance as:
```
σ_t² = ω + α · ε_(t-1)² + β · σ_(t-1)²
```

Where ε is the previous residual return. Parameters typically: α ≈ 0.06-0.10, β ≈ 0.88-0.92, with α+β just below 1 (high persistence).

```python
def garch_vol_forecast(returns: pd.Series, horizon_days: int = 21) -> float:
    from arch import arch_model
    returns_pct = returns.dropna() * 100  # arch library prefers pct
    model = arch_model(returns_pct, vol='Garch', p=1, q=1, mean='Zero', rescale=False)
    res = model.fit(disp='off')
    forecast = res.forecast(horizon=horizon_days)
    avg_var = forecast.variance.iloc[-1].mean()
    return np.sqrt(avg_var * 252) / 100   # convert back
```

Use the `arch` library. Rolling a custom GARCH from scratch is not worth the effort.

### 6.4 HAR-RV — recommended

Heterogeneous Autoregressive Realized Variance (Corsi 2009). Uses daily, weekly, and monthly realized vol as predictors:
```
RV_(t+1) = β_0 + β_d · RV_d_t + β_w · RV_w_t + β_m · RV_m_t + ε
```

Where:
- RV_d_t = today's realized variance (annualized vol squared / 252)
- RV_w_t = mean of last 5 days' RV
- RV_m_t = mean of last 22 days' RV

```python
def har_rv_fit(price_series: pd.Series) -> dict:
    """
    Fit a HAR-RV model. Returns coefficients.
    """
    log_returns = np.log(price_series / price_series.shift(1)).dropna()
    rv_d = log_returns**2  # daily realized variance proxy (could also use intraday RV)
    rv_w = rv_d.rolling(5).mean()
    rv_m = rv_d.rolling(22).mean()

    rv_next = rv_d.shift(-1)

    df = pd.concat([rv_next, rv_d, rv_w, rv_m], axis=1).dropna()
    df.columns = ['rv_next', 'rv_d', 'rv_w', 'rv_m']

    from statsmodels.api import OLS, add_constant
    X = add_constant(df[['rv_d', 'rv_w', 'rv_m']])
    y = df['rv_next']
    model = OLS(y, X).fit()
    return {
        'intercept': model.params['const'],
        'beta_d': model.params['rv_d'],
        'beta_w': model.params['rv_w'],
        'beta_m': model.params['rv_m'],
        'r_squared': model.rsquared,
        'model': model,
    }

def har_rv_forecast(price_series: pd.Series, horizon_days: int = 21) -> float:
    """
    Forecast annualized vol over next horizon_days using HAR-RV.
    """
    fit = har_rv_fit(price_series)
    log_returns = np.log(price_series / price_series.shift(1)).dropna()
    rv_d = (log_returns**2).iloc[-1]
    rv_w = (log_returns**2).iloc[-5:].mean()
    rv_m = (log_returns**2).iloc[-22:].mean()

    forecast_rv = (fit['intercept'] + fit['beta_d']*rv_d + fit['beta_w']*rv_w + fit['beta_m']*rv_m)

    # Annualize and convert to vol
    return np.sqrt(forecast_rv * 252)
```

HAR-RV typically achieves out-of-sample R² of 0.4-0.6 on monthly horizons, which is excellent for vol forecasting.

### 6.5 Combination forecast

The best forecast is usually a weighted combination:

```python
def combined_vol_forecast(price_series, current_iv_30d, vrp_adjustment=-0.02):
    """
    Combine HAR-RV forecast with implied vol (de-biased for VRP).

    Default VRP adjustment: subtract 2 vol points from IV to account for the
    average vol risk premium. Calibrate empirically.
    """
    har_forecast = har_rv_forecast(price_series, horizon_days=30)
    iv_adjusted = current_iv_30d + vrp_adjustment
    return 0.6 * har_forecast + 0.4 * iv_adjusted
```

Weights are empirical; calibrate via backtesting (Layer 6).

### 6.6 Evaluation of forecasts

For each name, I periodically check:
- **MAE (mean absolute error):** |forecast - actual realized| averaged across windows
- **Bias:** average (forecast - actual). Persistent positive bias = over-forecasting.
- **R²:** how much variance the forecast explains
- **Encompassing test:** can the model beat IV? Can IV beat the model?

---

## 7. The variance risk premium

### 7.1 Definition

VRP = Implied Variance - Realized Variance (forward-looking expectation)

In vol units: VRP_vol ≈ Implied_Vol - Realized_Vol_forecast

Historically positive on average (~2-4 vol points on indices; varies by name on single stocks).

### 7.2 Why it exists

The VRP exists because option SELLERS demand compensation for taking on vega risk (Carr & Wu 2009). It's structurally persistent because:
- Most retail and institutional investors are net option BUYERS (puts for insurance, calls for speculation)
- Option sellers (market makers, vol-selling funds) require a premium to absorb this flow
- Crash risk: vol spikes during crises hurt option sellers asymmetrically

### 7.3 Measuring single-name VRP

For each stock, build a historical time series of:
```
VRP_t = IV_30d_t - RV_30d_(t+30 days, actual)
```

Note: the RHS is only known after the fact. Build the time series with a 30-day lag.

```python
def historical_vrp(iv_series: pd.Series, price_series: pd.Series, window_days: int = 30) -> pd.Series:
    """
    Returns time series of realized VRP per day (with lag).
    """
    rv_series = pd.Series(index=iv_series.index, dtype=float)
    for t in iv_series.index:
        future = price_series.loc[t:t + pd.Timedelta(days=window_days)]
        if len(future) < window_days * 0.7:  # need most of the window
            continue
        rv = np.log(future / future.shift(1)).std() * np.sqrt(252)
        rv_series.loc[t] = rv
    vrp = iv_series - rv_series
    return vrp.dropna()
```

### 7.4 Using VRP in trade selection

Long-premium setups (long-bias and calendar-spread setups) become more attractive when:
- Current IV is LOW relative to history (IV rank < 30) — VRP compressed → less headwind
- The name has historically low VRP (i.e., IV doesn't systematically overshoot RV) — VRP is not eating returns

Short-premium (mostly avoided here, except in spread short legs) becomes more attractive when:
- IV is HIGH (rank > 70) — VRP elevated → more premium to capture
- The name has historically high VRP — pattern persists

### 7.5 VRP-adjusted theoretical pricing

In the options-pricing methodology doc, the theoretical-vs-market edge uses a "fair IV" benchmark. The VRP-aware version:

```python
def fair_iv_with_vrp(realized_vol_forecast: float, historical_vrp: float) -> float:
    """
    The 'fair' IV the market should price, accounting for VRP.
    """
    return realized_vol_forecast + max(historical_vrp, 0)
```

If market IV >> fair IV: options expensive (VRP elevated) — short bias.
If market IV << fair IV: options cheap (VRP compressed) — long bias.

---

## 8. Vol regimes and regime detection

### 8.1 Why regimes matter

Vol has persistent regimes. In low-vol regimes (extended bull markets, low VIX), realized vol is low, IV is low, and IV tends to underestimate the next vol spike. In high-vol regimes (post-crash, crisis), IV stays elevated even as realized vol mean-reverts down.

Strategy weights should shift by regime — but for v1, I use static weights and accept regime risk. Phase 2 enhancement: dynamic weights.

### 8.2 Simple regime detection

```python
def vol_regime(vix_series: pd.Series, vix_window: int = 60) -> str:
    """
    Classify current vol regime.
    """
    current = vix_series.iloc[-1]
    rolling_mean = vix_series.iloc[-vix_window:].mean()
    rolling_std = vix_series.iloc[-vix_window:].std()

    if current < 15:
        return 'low_calm'
    elif current < 20 and current < rolling_mean:
        return 'low_normal'
    elif current < 25:
        return 'medium'
    elif current < 35:
        return 'elevated'
    else:
        return 'crisis'
```

### 8.3 Regime-based setup gating

Crude rules for v1:
- **low_calm / low_normal**: all three setups enabled
- **medium**: prefer the long-bias and calendar-spread setups; reduce short-bias sizing
- **elevated**: pause new long-bias entries (IV too elevated for long calls); short-bias only if catalyst-specific
- **crisis**: pause all new entries; manage existing positions only

These rules feed into the master strategy doc as additional filters.

---

## 9. Per-name IV history database

To compute IV rank, percentile, skew rank, VRP — historical IV is required. Sourcing options:

### 9.1 Build it in-house

Daily snapshot the option chain, compute ATM 30-day IV, store. 1 year of history takes 1 year of data collection. Start now.

```python
def daily_iv_snapshot(ticker: str, date: str) -> dict:
    chain = pull_full_chain(ticker, date)
    S = get_spot(ticker, date)
    surface = fit_iv_surface(chain)
    return {
        'date': date,
        'ticker': ticker,
        'spot': S,
        'iv_atm_30d': surface(strike=S, dte=30),
        'iv_25d_call': surface(strike=strike_for_delta(0.25, S, 30, ...), dte=30),
        'iv_25d_put': surface(strike=strike_for_delta(-0.25, S, 30, ...), dte=30),
        'skew_25d': ...,
        'iv_atm_60d': surface(strike=S, dte=60),
        'ts_ratio': ...,
    }
```

Store in `data/iv_history/{ticker}.parquet`, appending daily.

### 9.2 Buy data

- **Polygon.io** options history: ~$50-200/month; gives clean IV time series.
- **OptionMetrics IvyDB**: gold standard, ~$2-5k/year academic license.

For Layer 0-2 this is unnecessary. For real backtests of the long-bias / short-bias / calendar-spread setup performance (Layer 6), at least 2-3 years of IV history is required; in-house collection takes 2-3 years of waiting. **Recommendation:** budget for at least Polygon.io once Layer 4 is producing real signals.

### 9.3 Approximation while building history

Until ≥ 200 trading days of IV history per name is available, approximate IV rank using:
1. Realized vol rank (use realized vol percentile as a proxy for what IV rank might be)
2. VIX-relative measure (current IV relative to VIX)

Mark these as "approximated" in the output so consumers know they're lower confidence.

---

## 10. Vol-driven position structuring rules

### 10.1 Naked long premium (long-bias setup) requires LOW IV rank

Hard rule: no long-bias setup entry if IV percentile > 60. Strongly preferred: < 40.

### 10.2 Spread structures (short-bias setup) tolerate higher IV rank

The short leg of a vertical spread captures part of the elevated IV. Hard rule: no short-bias setup entry if IV percentile > 80. Preferred: < 60.

### 10.3 Diagonals (calendar-spread setup) want IV term structure premium

Hard rule: ts_ratio (front/back IV) ≥ 0.95. Preferred ≥ 1.05.

### 10.4 Vega-budget per position

Limit total portfolio vega exposure:
- Net long vega (across all positions): ≤ $1,000 per 1 vol point move
- For a $50k portfolio, that's 2% of capital per 1 vol point — reasonable.

```python
def portfolio_vega_check(open_positions: list, max_vega: float = 1000) -> bool:
    total_vega = sum(p['vega'] for p in open_positions)
    return abs(total_vega) <= max_vega
```

---

## 11. Empirical sanity checks

Each month, automatically generate a "vol diagnostics" report per active name:

1. Time series of realized vs. implied vol (visual)
2. IV rank/percentile history
3. Skew history with current marker
4. Term structure history
5. Recent VRP (if 30+ days have passed since IV reading)
6. Forecast vs. actual realized vol for the last 3 forecasts

This catches systematic problems (e.g., the vol forecast model has drifted out of calibration) before they corrupt many trades.

---

## 12. Reference Python stack

For all vol work:

- **pandas-ta** or **TA-Lib** — for OHLC range calculations
- **arch** — for GARCH/EGARCH/ARCH models
- **statsmodels** — for OLS in HAR-RV
- **py_vollib_vectorized** — for IV extraction (per the options-pricing methodology doc)
- **numpy/scipy** — for everything else

Recommended pattern: build a `VolAnalyzer` class per ticker that loads OHLC + chain data, exposes methods for `realized_vol(estimator, window)`, `iv_rank()`, `skew()`, `term_structure()`, `forecast_rv(horizon)`. Cache results aggressively (vol diagnostics are slow to compute fresh; daily/weekly cadence is fine for everything except live spot/IV).

---

## 13. Integration with the ranking system

This document provides:

### To **Option Pricing Edge** dimension of the ranking framework:

| Sub-component | Source |
|---|---|
| IV rank | Section 3 |
| Spread quality at target entry | (option-pricing doc; uses IV surface fit here) |
| Theta-to-delta ratio | (computed in option-pricing doc) |
| **Optional v2:** Skew opportunity | Section 4.4 |
| **Optional v2:** Term structure favorability | Section 5.3 |

### To **Catalyst Quality** dimension:

| Sub-component | Source |
|---|---|
| Mispricing differential | Compare event-IV implied move (Section 7) vs. the forecast (Section 6.5) |

### To **Risk/Reward Math** dimension:

| Sub-component | Source |
|---|---|
| Breakeven distance vs. expected move | Realized vol forecast (Section 6) provides expected move |

### As hard filters (master strategy):

- IV rank ≤ 50 for the long-bias setup (gating filter)
- Implied move > 2× realized move → reject (premium too rich)

---

## 14. Open questions and Phase 2 research

1. **Better forecast combiners:** can an ML model (XGBoost, LSTM) outperform HAR-RV + IV blending? Probably small improvement; not worth complexity for v1.
2. **Cross-sectional vol relationships:** does HAR-RV improve with sector or peer-group cross-sectional regressors?
3. **Intraday realized vol:** with 5-min bar data, realized vol estimates are far more accurate. Worth the data infrastructure cost?
4. **Volatility surface arbitrage:** identifying genuine local mispricings in the surface (vs. just bid-ask noise). Requires very clean intraday data.
5. **Conditional VRP:** does VRP differ in low-vol vs. high-vol regimes? Yes, in academic studies. How to use?
6. **Earnings vol "term structure":** the implied earnings-day vol can be decomposed via the forward-vol formula (Section 5.4). Compare to historical realized earnings-day moves. Catalyst doc covers this.

---

## 15. Version control

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-05-15 | Initial draft |
