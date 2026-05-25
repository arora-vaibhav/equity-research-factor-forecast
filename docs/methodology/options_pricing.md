# Methodology — Options Pricing & Greeks

**Status:** Draft v0.1
**Layer:** Foundation (Layer 0); consumed by Layer 3 (options chain analysis) and Layer 4 (ranking)
**Last updated:** 2026-05-15
**Purpose:** Defines exact mathematical models for option pricing, Greeks calculation, implied volatility extraction, and theoretical-vs-market edge measurement. Every formula here is implementable from this document alone.

---

## 1. Notation and conventions

Throughout this document:
- `S` = underlying spot price
- `K` = strike price
- `T` = time to expiry in years (calendar days / 365)
- `r` = risk-free rate (continuously compounded, annualized)
- `q` = dividend yield (continuously compounded, annualized)
- `σ` (sigma) = volatility (annualized, in decimal: 0.30 = 30%)
- `N(x)` = standard normal cumulative distribution function
- `n(x)` = standard normal probability density function

All US single-stock options on listed equities are **American-style**. Index options (SPX) are European. The universe is single-stock → American pricing is required for accuracy.

---

## 2. Black-Scholes (European baseline)

### 2.1 Concept

The starting point. Closed-form, fast, well-understood. Underestimates American-option early-exercise value (especially for puts on dividend-paying stocks) but is the universal reference. I compute B-S values and Greeks as the baseline, then layer on American adjustments.

### 2.2 Formulas

**d1 and d2 helper terms:**
```
d1 = [ln(S/K) + (r - q + σ²/2) · T] / (σ · √T)
d2 = d1 - σ · √T
```

**European call price (with dividend yield q):**
```
C = S · e^(-q·T) · N(d1) - K · e^(-r·T) · N(d2)
```

**European put price:**
```
P = K · e^(-r·T) · N(-d2) - S · e^(-q·T) · N(-d1)
```

### 2.3 Python reference implementation

```python
from scipy.stats import norm
import numpy as np

def black_scholes(S, K, T, r, q, sigma, option_type='call'):
    """
    Black-Scholes price for European options (with continuous dividend yield).
    All inputs are scalars or NumPy arrays of equal shape.
    """
    if T <= 0:
        # At expiry, value is intrinsic
        if option_type == 'call':
            return max(S - K, 0)
        else:
            return max(K - S, 0)

    d1 = (np.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if option_type == 'call':
        return S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    elif option_type == 'put':
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * np.exp(-q * T) * norm.cdf(-d1)
    else:
        raise ValueError(f"Unknown option_type: {option_type}")
```

### 2.4 Validation tests

- **Put-call parity:** `C - P = S·e^(-q·T) - K·e^(-r·T)` for European options. Must hold within numerical precision (≤ 1e-6).
- **Boundary tests:** At T=0, price = intrinsic. At very high σ, call price approaches S. At σ→0, call price = max(S·e^(-q·T) - K·e^(-r·T), 0).
- **Comparison with QuantLib or py_vollib:** any new implementation should reconcile to existing library within 1e-4 across a grid of (S, K, T, σ, r, q).

---

## 3. American option pricing — Bjerksund-Stensland 2002

### 3.1 Why it is required

American options can be exercised at any time before expiry. The optimal-exercise boundary affects pricing.
- For **calls on non-dividend-paying stocks:** early exercise is never optimal → American call price = European call price. B-S is exact.
- For **calls on dividend-paying stocks:** early exercise may be optimal just before ex-dividend.
- For **puts:** early exercise can be optimal when the stock falls deep ITM (avoiding further discounting of strike value).

Single-stock options on dividend payers (most large caps) need American adjustments.

### 3.2 Bjerksund-Stensland approximation

Closed-form approximation to American option price; typically within 0.1% of binomial tree price, but ~1000× faster. Reference: Bjerksund, P. & Stensland, G. (2002).

The approximation uses an early-exercise trigger price `X` and a barrier-option-like formula. Full math is dense; here's the structure:

```
For an American call on a stock with continuous dividend yield:

if q ≤ 0:
    return BS_european_call(S, K, T, r, q, σ)   # early exercise never optimal

Otherwise compute:
    β = (1/2 - r/σ²) + √[(r/σ² - 1/2)² + 2r/σ²]
    B_inf = β / (β - 1) · K
    B_0 = max(K, (r / (r - q)) · K)
    h_T = -[(q - r) · T + 2σ·√T] · (K² / [(B_inf - B_0) · B_0])
    I = B_0 + (B_inf - B_0) · (1 - exp(h_T))

if S >= I:
    return S - K   # immediate exercise

α = (I - K) · I^(-β)

American_call_price ≈ α·S^β - α·φ(S, T, β, I, I) + φ(S, T, 1, I, I) - φ(S, T, 1, K, I) - K·φ(S, T, 0, I, I) + K·φ(S, T, 0, K, I)

where φ is a complex barrier-option helper function.
```

For put options, use the put-call symmetry property: an American put on stock with dividend yield q is equivalent to an American call on stock with adjusted parameters.

### 3.3 Python reference

A clean implementation lives in the `py_vollib_vectorized` package (`american_pricing`) or in QuantLib. Recommended: don't reimplement from scratch — use one of:

```python
# Option 1: py_vollib + extensions
from py_vollib.black_scholes import black_scholes  # European
# American: use Bjerksund-Stensland via py_lets_be_rational or similar

# Option 2: QuantLib (more setup, more accurate)
import QuantLib as ql
# Build a process, build an option, set engine = AnalyticEuropeanEngine or
# BjerksundStenslandApproximationEngine for American

# Option 3: For pure-Python implementation reference, see:
# https://github.com/vollib/py_vollib (extensible)
```

System standard: **use `py_vollib_vectorized` for batch Greek calculations across the chain** (vectorized = fast). Fall back to QuantLib for any name where the approximation looks off.

### 3.4 Validation tests

- Compare BS-2002 prices against a 100-step binomial tree (CRR or Leisen-Reimer) — should agree within 0.5% for typical input ranges.
- For zero-dividend calls: BS-2002 should exactly equal Black-Scholes (verify within 1e-6).
- Boundary: deep ITM American put ≥ intrinsic value, with equality as the early-exercise boundary is hit.

---

## 4. The Greeks

### 4.1 Definitions

Greeks measure sensitivity of option price to inputs. I use first- and second-order:

| Greek | Symbol | Definition | Units (typical) |
|---|---|---|---|
| Delta | Δ | ∂Price/∂S | Per $ move in underlying |
| Gamma | Γ | ∂²Price/∂S² = ∂Delta/∂S | Per $ move (rate of delta change) |
| Theta | Θ | -∂Price/∂T | Per day of time decay |
| Vega | ν | ∂Price/∂σ | Per 1% (0.01) change in IV |
| Rho | ρ | ∂Price/∂r | Per 1% change in rates (mostly ignored) |
| Vanna | | ∂²Price/(∂S·∂σ) | Cross-greek; useful for risk |
| Volga | | ∂²Price/∂σ² | Vol-of-vol sensitivity |

### 4.2 Closed-form Greeks (European, Black-Scholes)

**Call delta:** `Δ_call = e^(-q·T) · N(d1)`
**Put delta:** `Δ_put = -e^(-q·T) · N(-d1) = Δ_call - e^(-q·T)`

**Gamma (same for call & put):**
```
Γ = [e^(-q·T) · n(d1)] / (S · σ · √T)
```

**Theta (call):**
```
Θ_call = -[S · σ · e^(-q·T) · n(d1)] / (2 · √T) - r·K·e^(-r·T)·N(d2) + q·S·e^(-q·T)·N(d1)
```

For practical use, divide by 365 to get "theta per calendar day" or by 252 for "theta per trading day."

**Theta (put):**
```
Θ_put = -[S · σ · e^(-q·T) · n(d1)] / (2 · √T) + r·K·e^(-r·T)·N(-d2) - q·S·e^(-q·T)·N(-d1)
```

**Vega (same for call & put):**
```
ν = S · e^(-q·T) · n(d1) · √T
```

Vega is typically reported as price change per 1% IV move, so divide raw vega by 100:
```
vega_displayed = ν / 100
```

**Rho:**
```
ρ_call = K · T · e^(-r·T) · N(d2)
ρ_put = -K · T · e^(-r·T) · N(-d2)
```

### 4.3 Python reference

```python
def bs_greeks(S, K, T, r, q, sigma, option_type='call'):
    """Returns dict of Greeks for a European option."""
    if T <= 0 or sigma <= 0:
        return {'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0, 'rho': 0}

    d1 = (np.log(S/K) + (r - q + 0.5*sigma**2)*T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    exp_qT = np.exp(-q*T)
    exp_rT = np.exp(-r*T)
    n_d1 = norm.pdf(d1)

    gamma = exp_qT * n_d1 / (S * sigma * np.sqrt(T))
    vega = S * exp_qT * n_d1 * np.sqrt(T) / 100   # per 1% IV move

    if option_type == 'call':
        delta = exp_qT * norm.cdf(d1)
        theta = (-S * sigma * exp_qT * n_d1 / (2 * np.sqrt(T))
                 - r * K * exp_rT * norm.cdf(d2)
                 + q * S * exp_qT * norm.cdf(d1)) / 365  # per calendar day
        rho = K * T * exp_rT * norm.cdf(d2) / 100
    else:
        delta = -exp_qT * norm.cdf(-d1)
        theta = (-S * sigma * exp_qT * n_d1 / (2 * np.sqrt(T))
                 + r * K * exp_rT * norm.cdf(-d2)
                 - q * S * exp_qT * norm.cdf(-d1)) / 365
        rho = -K * T * exp_rT * norm.cdf(-d2) / 100

    return {'delta': delta, 'gamma': gamma, 'theta': theta, 'vega': vega, 'rho': rho}
```

### 4.4 American-option Greeks

For American options, Greeks can be computed via:
1. **Finite differences** on the Bjerksund-Stensland price (cleanest):
```python
def fd_delta(price_func, S, K, T, r, q, sigma, h=0.01, option_type='call'):
    return (price_func(S*(1+h), K, T, r, q, sigma, option_type)
            - price_func(S*(1-h), K, T, r, q, sigma, option_type)) / (2 * S * h)
```

2. **Closed-form American Greeks** (more complex; available in QuantLib).

**Recommendation:** use finite-difference on BS-2002 prices for American Greeks. Step sizes:
- Delta/Gamma: ΔS = 1% of S
- Vega: Δσ = 0.01 (1 vol point)
- Theta: ΔT = 1 day (1/365)

### 4.5 Aggregate position Greeks

For multi-leg positions (spreads), Greeks are additive. For a long-leg-short-leg spread:
```
Spread_Delta = (qty_long × leg_long.delta) - (qty_short × leg_short.delta)
```
Same for gamma, theta, vega.

---

## 5. Implied volatility extraction

### 5.1 Concept

IV is the σ that makes the model price match the market price. Cannot be computed in closed form for B-S; must be solved iteratively.

### 5.2 Algorithm — Brent's method

Brent's method (combining bisection, secant, and inverse-quadratic interpolation) is the standard. Bracketed root-finding; guaranteed convergence; no derivative required.

```python
from scipy.optimize import brentq

def implied_volatility_bs(market_price, S, K, T, r, q, option_type='call'):
    """
    Solve for sigma such that black_scholes(...) == market_price.
    Returns IV in decimal (0.30 = 30%).
    """
    def objective(sigma):
        return black_scholes(S, K, T, r, q, sigma, option_type) - market_price

    # Sanity bounds
    iv_low, iv_high = 0.001, 5.0   # 0.1% to 500%

    # Check intrinsic value
    intrinsic = max(S - K, 0) if option_type == 'call' else max(K - S, 0)
    if market_price < intrinsic:
        return np.nan  # arbitrage/data error

    try:
        return brentq(objective, iv_low, iv_high, xtol=1e-6, maxiter=100)
    except ValueError:
        return np.nan
```

### 5.3 Newton-Raphson alternative (faster but less robust)

Uses the derivative (vega) to converge in fewer iterations:
```
σ_(n+1) = σ_n - (Price(σ_n) - market_price) / vega(σ_n)
```

Faster but can fail to converge for very ITM/OTM options or near expiry. Recommendation: use Brent's for production; Newton for speed-critical batch jobs with fallback to Brent on failure.

### 5.4 IV calc for American options

Same iterative idea, but the price function is BS-2002 instead of B-S. The shape is similar enough that Brent's still converges quickly. Implementation in `py_vollib_vectorized` or QuantLib supports this directly.

### 5.5 Edge cases

- **Deep ITM/OTM:** vega approaches 0 → IV becomes ill-defined. Set NaN; downstream code must handle.
- **Bid = 0:** real for very OTM options. Use mid only if bid > 0 AND ask > 0 AND spread reasonable.
- **Market price < intrinsic:** data error or stale quote. Reject.
- **Pre-earnings or pre-event:** IV will be elevated relative to historical norms. This is information, not an error.

### 5.6 Validation tests

- Round-trip: IV(BS(S,K,T,r,q,σ)) ≈ σ within 1e-4 across a grid.
- Compare to broker-reported IV: should agree within ~5% (brokers may use slightly different rate conventions, dividend handling).

---

## 6. Forward price and dividend handling

### 6.1 Forward price

```
F = S · e^[(r - q) · T]
```

The forward price is what the market expects the underlying to be at expiry under risk-neutral measure. For ATM strikes (where K = F), call and put have equal value (modulo American premium).

### 6.2 Dividend yield

Use the **trailing 12-month dividend yield** as `q`, computed as:
```
q_continuous = ln(1 + DPS_TTM / S)
```

Where DPS_TTM is total dividends paid per share in trailing 12 months. The continuous form is theoretically correct for B-S.

For stocks with irregular or special dividends, this approach can misstate `q`. Better for discrete-dividend stocks: use the **discrete dividend forward** formulation:
```
F = (S - PV(known_dividends_until_expiry)) · e^(r·T)
```

Where PV(known_dividends) = sum of e^(-r·t_i) × D_i for each known ex-dividend date t_i and dividend amount D_i.

**Implementation recommendation:**
- For most names with stable dividends: use continuous `q`
- For names with known upcoming ex-dividend dates within option window: use discrete adjustment

### 6.3 Risk-free rate

Use the US Treasury yield matching the option's days to expiry:
- < 30 days: 1-month T-bill yield
- 30-90 days: 3-month T-bill yield
- 90+ days: interpolate from the Treasury curve

For our 40-60 DTE range: 2-month interpolated yield is appropriate. In practice, the difference between using 1m vs. 3m vs. 6m rates is tiny for option pricing on this horizon — order of 0.1-0.5% on the option price. Don't sweat the precision; just use a reasonable rate from FRED daily.

```python
def get_risk_free_rate(date: str, dte: int) -> float:
    """Returns continuously-compounded annualized rate for the given DTE."""
    rates = load_fred_treasury_curve(date)
    # Interpolate based on dte
    return np.interp(dte, [30, 90, 180, 365], [rates['1mo'], rates['3mo'], rates['6mo'], rates['1yr']])
```

---

## 7. Theoretical-vs-market edge

### 7.1 Concept

The system's "option pricing edge" doesn't come from beating Black-Scholes — the market doesn't mis-price relative to plain BS. It comes from:
- IV relative to history (IV rank)
- IV surface anomalies (skew, term structure)
- Realized-vs-implied vol divergence

Pure BS deviation is NOT a tradable signal at scale. But I still compute it as a sanity-check: if the computed BS price differs from the market mid by >5% with no good explanation, something is wrong (stale data, wrong dividend, wrong rate, mis-identified option).

### 7.2 Edge calculation

```python
def theoretical_vs_market_edge(option: dict) -> dict:
    """
    For a single option contract, compute the theoretical price using the
    market-implied vol from a "fair" benchmark (e.g., realized vol forecast or
    smoothed IV surface), and compare to actual market mid.
    """
    realized_vol_forecast = get_realized_vol_forecast(option['underlying'])  # see vol doc
    smoothed_surface_iv = get_smoothed_surface_iv(option['underlying'], option['strike'], option['dte'])

    fair_iv = 0.5 * realized_vol_forecast + 0.5 * smoothed_surface_iv
    theoretical = bs_or_bs2002(option['S'], option['K'], option['T'], option['r'], option['q'], fair_iv, option['type'])
    market_mid = (option['bid'] + option['ask']) / 2

    edge_dollars = theoretical - market_mid
    edge_pct = edge_dollars / market_mid if market_mid > 0 else 0
    return {'theoretical': theoretical, 'market_mid': market_mid, 'edge_dollars': edge_dollars, 'edge_pct': edge_pct}
```

Positive edge_pct → option appears cheap → mild bias toward long. Negative → expensive → bias toward short side / spreads.

### 7.3 What this score is and is not

This is **not** an independent edge source. It is a sanity check + a tiebreaker. If two trades have identical Directional Thesis scores but one has 5% theoretical edge and the other has -2%, prefer the first.

---

## 8. IV surface concepts

### 8.1 Smile and skew

For a given expiry, IV plotted against strike (or moneyness) is not flat. Common shapes:
- **Smile:** higher IV for both OTM puts and OTM calls than for ATM (typical for forex, indices, some single stocks)
- **Skew (smirk):** higher IV for OTM puts, lower for OTM calls (most US single stocks — reflects crash insurance demand)

Skew is characterized by the 25-delta or 10-delta IV differential:
```
skew_25d = IV(put, delta=-0.25) - IV(call, delta=0.25)
```

### 8.2 Term structure

For ATM options, IV plotted against time to expiry. Common shapes:
- **Contango:** longer-dated IV > shorter-dated (normal regime)
- **Backwardation:** shorter-dated > longer-dated (often around catalyst events — front-month IV spikes)

Term structure is characterized by the front-vs-back ratio:
```
ts_ratio = IV(front_month) / IV(60-90_dte)
```
ts_ratio > 1 indicates backwardation (event premium in the front month). The calendar-spread setup prefers ts_ratio ≥ 1, since the front leg is being sold.

### 8.3 Surface fitting

For systematic work, fit a smooth function to the (strike, expiry) → IV surface for each underlying. Common parametric forms:
- SVI (Stochastic Volatility Inspired) — 5-parameter form per expiry
- SABR — 4-parameter; better for inter-expiry consistency

For this use case (single-name options, ~10-30 strikes per expiry), a simpler approach works:
1. Per-expiry: fit a cubic spline through observed (delta, IV) points
2. Across expiries: linear interpolation in variance space

```python
def fit_iv_surface(option_chain: pd.DataFrame) -> callable:
    """
    Returns a function f(strike, dte) -> IV for the given chain.
    """
    from scipy.interpolate import UnivariateSpline, interp1d

    expiries = sorted(option_chain['dte'].unique())
    expiry_splines = {}
    for dte in expiries:
        chunk = option_chain[option_chain['dte'] == dte].sort_values('strike')
        if len(chunk) >= 4:
            spline = UnivariateSpline(chunk['strike'], chunk['iv'], k=3, s=0.0001)
            expiry_splines[dte] = spline

    def surface(strike, dte):
        # Find bracketing expiries
        if dte in expiry_splines:
            return float(expiry_splines[dte](strike))
        # Interpolate in variance
        below = max([e for e in expiries if e < dte], default=None)
        above = min([e for e in expiries if e > dte], default=None)
        if below is None: return float(expiry_splines[above](strike))
        if above is None: return float(expiry_splines[below](strike))
        iv_below = float(expiry_splines[below](strike))
        iv_above = float(expiry_splines[above](strike))
        var_below = iv_below**2 * below
        var_above = iv_above**2 * above
        var_interp = np.interp(dte, [below, above], [var_below, var_above])
        return np.sqrt(var_interp / dte)

    return surface
```

The smoothed surface is used in `theoretical_vs_market_edge` as the "fair IV" benchmark.

---

## 9. Spread (multi-leg) pricing

### 9.1 Definitions

**Vertical spread (same expiry, different strikes):**
- Bull call spread: long lower-strike call + short higher-strike call (debit, bullish, capped upside)
- Bear put spread: long higher-strike put + short lower-strike put (debit, bearish, capped downside)
- Credit spreads: reverse (sell premium, capped risk)

**Calendar/diagonal (different expiries):**
- Calendar: same strike, long longer-dated + short shorter-dated
- Diagonal: different strike AND different expiry

### 9.2 Spread pricing

Multi-leg = sum of individual leg theoretical prices, with appropriate signs:

```python
def spread_theoretical(legs: list[dict]) -> float:
    total = 0
    for leg in legs:
        price = bs_or_bs2002(leg['S'], leg['K'], leg['T'], leg['r'], leg['q'], leg['iv'], leg['type'])
        total += leg['qty'] * (price if leg['side'] == 'long' else -price)
    return total
```

**Important pitfall:** market spread prices on multi-leg orders are NOT the sum of individual leg bid/ask. The broker's complex-order routing may achieve better prices than naive sums. For pricing edge analysis, use legs-summed theoretical vs. legs-summed market mid as a starting point, then check the broker's actual complex-order routed price as the entry reference.

### 9.3 Spread Greeks

Sum of leg Greeks with correct signs. Pay particular attention to:
- **Vega of vertical spread:** lower-strike long vs. higher-strike short have OFFSETTING vega — vertical spreads have much smaller vega than naked options. Important: vertical spreads are more direction-focused, less vol-focused.
- **Theta of calendar:** the short shorter-dated leg has more theta than the long longer-dated — calendar spreads collect theta. This is the structural benefit.
- **Net delta of diagonal:** depends on strike differential and expiry differential.

---

## 10. Probability of profit (POP)

### 10.1 Concept

POP = probability that the option/spread will be profitable at expiry. For an European option in BS:

**For a long call:** P(S_T > K + premium_paid) = N(d2_adjusted)
where d2_adjusted uses `K + premium_paid` instead of `K`.

**For a long put:** P(S_T < K - premium_paid) = N(-d2_adjusted)

```python
def pop_long_call(S, K_breakeven, T, r, q, sigma):
    """K_breakeven = strike + premium paid"""
    d2 = (np.log(S/K_breakeven) + (r - q - 0.5*sigma**2)*T) / (sigma * np.sqrt(T))
    return norm.cdf(d2)

def pop_long_put(S, K_breakeven, T, r, q, sigma):
    """K_breakeven = strike - premium paid"""
    d2 = (np.log(S/K_breakeven) + (r - q - 0.5*sigma**2)*T) / (sigma * np.sqrt(T))
    return norm.cdf(-d2)
```

### 10.2 POP for spreads

For a debit spread, POP at expiry = P(S_T > breakeven) for bullish, P(S_T < breakeven) for bearish:
```
debit_breakeven = K_long + net_debit   (for call spread)
                = K_long - net_debit   (for put spread)
```

### 10.3 POP vs. expected value

POP doesn't capture magnitude. A trade with 50% POP and asymmetric payoff (win = +$300, lose = -$100) is great. A trade with 80% POP and lose-big-when-you-lose payoff (win = +$50, lose = -$500) is terrible.

I compute both:
```python
def expected_value(S, K, T, r, q, sigma, option_type, premium, ...):
    """
    EV under lognormal assumption: integrate payoff × probability density.
    Closed-form for calls/puts:
    EV(long_call) = BS_price - premium   (under risk-neutral measure)
    But under realistic (P) measure with drift = expected return, EV != 0.
    """
    # Use simulated paths or numerical integration with realistic drift
    # See: section 11
```

POP is the headline number; EV is the deeper number; both go into the Risk/Reward dimension of the ranking.

### 10.4 IMPORTANT: P-measure vs. Q-measure

Above formulas use the **risk-neutral (Q) measure** — the drift is `r - q`. Under risk-neutral measure, the expected return of any tradable asset equals the risk-free rate (after dividends). This gives the option's MARKET price.

For trading decisions, the **real-world (P) measure** matters — what's the actual expected return on this stock? This is where the trade thesis matters. If the analyst expects 10% upside vs. the market's implied 0%, the P-measure POP is HIGHER than market-implied POP.

```python
def pop_long_call_p_measure(S, K_breakeven, T, sigma, expected_annual_return):
    """
    POP under analyst's expected return assumption, not risk-neutral.
    """
    d2 = (np.log(S/K_breakeven) + (expected_annual_return - 0.5*sigma**2)*T) / (sigma * np.sqrt(T))
    return norm.cdf(d2)
```

This is the crucial difference: market-implied POP says what a fair coin says. Thesis-implied POP says what the analyst's weighted coin says. The DELTA between them is the edge.

---

## 11. Distribution assumptions and their limits

### 11.1 The lognormal assumption

BS assumes stock returns are lognormal — i.e., log(S_T/S_0) ~ Normal(μT, σ²T). Real-world deviations:
- **Fat tails:** real returns have more extreme outcomes than lognormal predicts (kurtosis >> 3)
- **Skewness:** real returns often have negative skew (crashes are bigger than rallies)
- **Vol clustering:** volatility itself varies over time (lognormal assumes constant σ)

### 11.2 When to use a different distribution

For event-driven trades (earnings, FDA decisions), the binary nature of the catalyst makes lognormal a poor model. Better approach: model as a mixture:

```
return ~ p · Normal(μ_good_outcome, σ_quiet)
       + (1-p) · Normal(μ_bad_outcome, σ_quiet)
       + ε_continuous  (residual lognormal)
```

This is more accurate around binary events. For Layer 3 production v1, I stay lognormal for simplicity. Mixture modeling becomes a Phase 2 enhancement, especially for short-bias catalyst plays.

### 11.3 Empirical (historical) distribution

For each name, also compute the empirical distribution of returns over the relevant horizon:
```python
def empirical_return_distribution(prices: pd.Series, horizon_days: int) -> pd.Series:
    """Returns the historical distribution of horizon_days returns."""
    return prices.pct_change(horizon_days).dropna()
```

Use this distribution as a sanity check on the lognormal assumption: if empirical 5th/95th percentiles diverge significantly from lognormal-implied, flag the trade.

---

## 12. Standard Greeks targets per setup

These are guidance, not hard rules. They calibrate trade structure.

### Long-bias setup — Long call

- Target delta: 0.40 to 0.55 (ATM to slightly OTM)
- Gamma: highest near ATM; accepted
- Theta: avoid theta-to-premium ratio > 1% per day (i.e., daily theta cost less than 1% of premium paid)
- Vega: positive vega is the structural cost; mitigated by IV rank filter

### Short-bias setup — Put spread

- Long leg delta: -0.35 to -0.45 (ATM to slightly OTM put)
- Short leg delta: -0.15 to -0.20 (further OTM put)
- Net delta of the spread: -0.20 to -0.30
- Net vega: small (vertical structure offsets)
- Net theta: small negative (some bleed; spread reduces it significantly)

### Calendar-spread setup — Calendar/diagonal

- Long leg delta: 0.45 to 0.55 (near-ATM, 60-90 DTE)
- Short leg delta: 0.25 to 0.35 (1-2 strikes further OTM, 20-30 DTE)
- Net delta: positive (~0.20-0.30 for bullish), small (~-0.20 for bearish)
- Net theta: POSITIVE (the short leg collects more theta than long leg pays) — structural benefit
- Net vega: positive (long leg longer-dated has more vega) — be aware on vol-collapse risk

---

## 13. Pricing validation pipeline

Every option in the chain must pass:

1. **Bid > 0 AND ask > 0**: rejected otherwise
2. **Spread ≤ 25% of mid**: rejected if wider (deeper filter than the entry rule, which is 10% — but we still want to PRICE wider-spread options for context)
3. **Market price ≥ intrinsic - $0.05**: arbitrage check (5-cent tolerance for stale quotes)
4. **IV calc converges**: if Brent fails, mark NaN and exclude from ranking
5. **Greeks within sane ranges**:
   - |delta| ≤ 1.0
   - Gamma ≥ 0
   - Vega ≥ 0
6. **Put-call parity check** (for European-equivalent matched strikes): violations > 2% of underlying flag a data issue

```python
def validate_chain(chain: pd.DataFrame) -> pd.DataFrame:
    chain = chain.copy()
    chain['valid'] = True
    chain.loc[chain['bid'] <= 0, 'valid'] = False
    chain.loc[chain['ask'] <= 0, 'valid'] = False
    chain['spread_pct'] = (chain['ask'] - chain['bid']) / chain['mid']
    chain.loc[chain['spread_pct'] > 0.25, 'valid'] = False
    chain.loc[chain['mid'] < chain['intrinsic'] - 0.05, 'valid'] = False
    chain.loc[chain['iv'].isna(), 'valid'] = False
    chain.loc[chain['delta'].abs() > 1.0, 'valid'] = False
    return chain
```

---

## 14. Integration with the ranking system

This document supplies inputs for the **Option Pricing Edge** dimension of the ranking framework:

| Sub-component | Source |
|---|---|
| IV rank | Computed in the volatility-analysis methodology doc (uses IV history) |
| Spread quality at target entry | Computed here (validation pipeline + sub-spread calc) |
| Theta-to-delta ratio | Greeks from Section 4 |

And the **Risk/Reward Math** dimension:

| Sub-component | Source |
|---|---|
| Breakeven distance vs. expected move | POP calc (Section 10) + realized-vol forecast |
| Max loss vs. position size | Premium × multiplier (100 per US contract); spread max loss = max(net debit, K_long - K_short - net_credit) |
| Achievability of 30% profit target | Derived from POP at 1.30 × entry premium |

---

## 15. Library recommendations

For production:

- **py_vollib_vectorized** — vectorized BS, BS-2002, Greeks, IV. Best for batch processing across full chains.
- **QuantLib (Python bindings)** — comprehensive, slower setup, very accurate. Use for any name where py_vollib disagrees with broker by > 5%.
- **scipy.optimize** — Brent's method for IV; widely available.
- **scipy.stats.norm** — normal CDF/PDF for BS formulas.
- **pandas-market-calendars** — market holidays for DTE calculations.

Avoid: writing the math from scratch. Use libraries; reserve original code for the strategy layer.

---

## 16. Open questions for further research

1. **Stochastic vol pricing:** Heston model fits skew and term structure better than BS. Implementation cost is significant; payoff is modest for retail-scale. Phase 3 candidate.
2. **Jump-diffusion models (Merton):** capture earnings-jump structure. May improve event-driven pricing. Phase 3.
3. **Volatility risk premium pricing:** explicitly price the VRP into theoretical-vs-market edge. Would require historical VRP measurement.
4. **American early-exercise probability:** add probability of early exercise as a Greek-like measure for short-leg risk in spreads.
5. **Treasury curve interpolation:** does using SOFR or LIBOR-OIS vs. Treasury bills meaningfully change pricing? Probably not material; verify.

---

## 17. Version control

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-05-15 | Initial draft |
