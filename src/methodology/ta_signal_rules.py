"""Technical-analysis SIGNAL interpretation rules.

This module is the canonical interpretation layer that sits ON TOP of
`technical_indicators.py`. The numeric indicator module computes
RSI / MACD / ATR / etc. as floats; THIS module turns those floats into
{"signal": "buy"/"sell"/"neutral", "strength": 0-3, "note": str}
records and aggregates them into a per-timeframe verdict
(STRONG BUY / BUY / NEUTRAL / SELL / STRONG SELL) with counts and a
weighted net-strength score.

Why this exists:
    Raw indicator floats are unreadable as a verdict. I want a per-
    indicator buy/short/neutral plus aggregated strength and count so
    the dashboard says what the numbers actually mean.

Design:
    * Each indicator gets a pure `interpret_<name>(value, ...) -> dict`
      function. No state, no I/O, no side effects.
    * Thresholds come from the original literature where possible
      (Wilder, Appel, Bollinger, Lane, Granville, Donchian, Quong &
      Soudack, Chaikin) and from the StockCharts/TradingView
      practitioner convention where the literature is silent.
    * `aggregate_signals(...)` produces the dashboard summary using a
      weighted-sum convention modelled on TradingView's "Technical
      Analysis Summary" widget (Oscillators + Moving Averages buckets,
      26 indicators, equal weight by default, summed across sides).
    * `interpret_timeframe(...)` is the entry point a UI/dashboard
      calls per timeframe (1h, 1d, 1wk).

References:
    Wilder, J.W. (1978). New Concepts in Technical Trading Systems.
        Trend Research. -- RSI(14) 30/70, ADX 14, ATR.
    Connors, L. & Alvarez, C. (2008). Short Term Trading Strategies
        That Work. -- RSI(2) 5/95.
    Appel, G. (1979); histogram by Aspray, T. (1986).
        -- MACD(12,26,9), signal-line cross, histogram.
    Bollinger, J. (2001). Bollinger on Bollinger Bands. McGraw-Hill.
        -- %b and Squeeze.
    Lane, G. (1950s; interview 2007). -- Stochastic %K 20/80.
    Williams, L. (1973). -- %R -20/-80.
    Granville, J. (1963). Granville's New Key to Stock Market Profits.
        -- OBV slope/divergence.
    Quong, G. & Soudack, A. (1989). "Volume-Weighted RSI: Money Flow."
        Technical Analysis of Stocks & Commodities, March. -- MFI 20/80.
    Chaikin, M. (1980s practitioner work; StockCharts ChartSchool).
        -- CMF +/- 0.1 buffer convention.
    Donchian, R. (1960s) via Turtle Traders. -- 20-day channel breakout.
    TradingView (2024). "Technical Analysis Widget" methodology --
        26-indicator weighted summary convention.

All thresholds use closed-open convention (a < threshold) to avoid
double-counting at boundaries. NaN/None propagates to neutral.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

TA_SIGNAL_RULES_VERSION = "1.0"

# --------------------------------------------------------------------------
# Per-indicator default weights -- TradingView "Technical Analysis Summary"
# convention: each indicator contributes its raw strength once. Crosses
# (golden/death, MACD signal cross, Donchian breakout) and trend filters
# (price vs SMA200) carry higher weight because they are state-change
# events, not just oscillator levels. Sourced from the TradingView widget
# methodology page (Oscillators bucket + MA bucket, 26 indicators total).
# --------------------------------------------------------------------------
DEFAULT_WEIGHTS: dict[str, float] = {
    # momentum oscillators (light weight; many redundant signals)
    "rsi_14":          1.0,
    "rsi_2":           0.5,  # very short-term, noisy
    "stoch_k":         1.0,
    "williams_r":      1.0,
    "mfi_14":          1.0,
    # MACD family (medium-heavy; cross events are state changes)
    "macd_hist":       1.0,
    "macd_cross":      1.5,
    # volatility / range
    "bb_pctb":         1.0,
    "bb_squeeze":      0.5,  # context flag, not a direction
    "atr_pct":         0.5,  # regime context, not a direction
    # trend strength / direction
    "adx_14":          1.0,
    "ema_9_21_cross":  1.0,
    "sma_50_200_cross":1.5,  # golden/death is heavy
    "price_vs_sma200": 1.5,  # secular trend filter
    # volume
    "obv_slope":       1.0,
    "cmf_20":          1.0,
    # range/breakout
    "donchian_20":     1.5,  # breakout is a state change
}


def _is_nan(x: Any) -> bool:
    if x is None:
        return True
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True


def _neutral(note: str = "no data") -> dict:
    return {"signal": "neutral", "strength": 0, "note": note}


# ==========================================================================
# 1. RSI(14) -- Wilder 1978, ch.4. Canonical 30/70 with 40/60 mid-zone.
# ==========================================================================
def interpret_rsi_14(value: float) -> dict:
    if _is_nan(value):
        return _neutral()
    v = float(value)
    if v < 20:  return {"signal": "buy",  "strength": 3, "note": f"RSI14 extreme oversold ({v:.1f}<20)"}
    if v < 30:  return {"signal": "buy",  "strength": 2, "note": f"RSI14 oversold ({v:.1f}<30)"}
    if v < 45:  return {"signal": "buy",  "strength": 1, "note": f"RSI14 lower zone ({v:.1f}<45)"}
    if v > 80:  return {"signal": "sell", "strength": 3, "note": f"RSI14 extreme overbought ({v:.1f}>80)"}
    if v > 70:  return {"signal": "sell", "strength": 2, "note": f"RSI14 overbought ({v:.1f}>70)"}
    if v > 55:  return {"signal": "sell", "strength": 1, "note": f"RSI14 upper zone ({v:.1f}>55)"}
    return {"signal": "neutral", "strength": 0, "note": f"RSI14 mid-range ({v:.1f})"}


# ==========================================================================
# 2. RSI(2) -- Connors 5/95. Designed to flag extreme bars; use only
#    above SMA200 (caller filters).
# ==========================================================================
def interpret_rsi_2(value: float) -> dict:
    if _is_nan(value):
        return _neutral()
    v = float(value)
    if v < 5:   return {"signal": "buy",  "strength": 3, "note": f"RSI2 Connors extreme oversold ({v:.1f}<5)"}
    if v < 10:  return {"signal": "buy",  "strength": 2, "note": f"RSI2 oversold ({v:.1f}<10)"}
    if v > 95:  return {"signal": "sell", "strength": 3, "note": f"RSI2 Connors extreme overbought ({v:.1f}>95)"}
    if v > 90:  return {"signal": "sell", "strength": 2, "note": f"RSI2 overbought ({v:.1f}>90)"}
    return _neutral(f"RSI2 mid ({v:.1f})")


# ==========================================================================
# 3. MACD histogram -- Aspray 1986. Sign + slope; rising hist adds strength.
# ==========================================================================
def interpret_macd_hist(hist: float, hist_prev: Optional[float] = None) -> dict:
    if _is_nan(hist):
        return _neutral()
    h = float(hist)
    rising = (hist_prev is not None and not _is_nan(hist_prev) and h > float(hist_prev))
    falling = (hist_prev is not None and not _is_nan(hist_prev) and h < float(hist_prev))
    if h > 0 and rising:  return {"signal": "buy",  "strength": 2, "note": "MACD hist positive & rising"}
    if h > 0:             return {"signal": "buy",  "strength": 1, "note": "MACD hist positive"}
    if h < 0 and falling: return {"signal": "sell", "strength": 2, "note": "MACD hist negative & falling"}
    if h < 0:             return {"signal": "sell", "strength": 1, "note": "MACD hist negative"}
    return _neutral("MACD hist flat at zero")


# ==========================================================================
# 4. MACD signal cross -- Appel 1979. Binary state change.
#    cross_state: +1 = bullish cross today, -1 = bearish cross today, 0 = none.
# ==========================================================================
def interpret_macd_cross(cross_state: int) -> dict:
    if cross_state is None or _is_nan(cross_state):
        return _neutral()
    s = int(cross_state)
    if s > 0:  return {"signal": "buy",  "strength": 3, "note": "MACD bullish signal-line cross"}
    if s < 0:  return {"signal": "sell", "strength": 3, "note": "MACD bearish signal-line cross"}
    return _neutral("no MACD cross")


# ==========================================================================
# 5. Bollinger %b -- Bollinger 2001. <0 / >1 are band-piercings.
# ==========================================================================
def interpret_bb_pctb(value: float) -> dict:
    if _is_nan(value):
        return _neutral()
    v = float(value)
    if v < 0.0:  return {"signal": "buy",  "strength": 2, "note": f"%b<0 below lower band ({v:.2f})"}
    if v < 0.2:  return {"signal": "buy",  "strength": 1, "note": f"%b near lower band ({v:.2f})"}
    if v > 1.0:  return {"signal": "sell", "strength": 2, "note": f"%b>1 above upper band ({v:.2f})"}
    if v > 0.8:  return {"signal": "sell", "strength": 1, "note": f"%b near upper band ({v:.2f})"}
    return _neutral(f"%b mid ({v:.2f})")


# ==========================================================================
# 6. Bollinger Squeeze -- context flag, not directional. Strength 1 if on.
# ==========================================================================
def interpret_bb_squeeze(squeeze_on: int) -> dict:
    if squeeze_on is None or _is_nan(squeeze_on):
        return _neutral()
    if int(squeeze_on) == 1:
        return {"signal": "neutral", "strength": 1, "note": "BB squeeze ON -- breakout pending"}
    return _neutral("no squeeze")


# ==========================================================================
# 7. ADX(14) -- Wilder 1978. <20 weak / 20-25 grey / 25-40 trend / >40 strong.
#    ADX is non-directional; we report neutral but stamp strength so the
#    DI+/DI- info caller can amplify. Rising ADX = strengthening trend.
# ==========================================================================
def interpret_adx_14(adx: float, adx_prev: Optional[float] = None,
                    plus_di: Optional[float] = None,
                    minus_di: Optional[float] = None) -> dict:
    if _is_nan(adx):
        return _neutral()
    a = float(adx)
    rising = (adx_prev is not None and not _is_nan(adx_prev) and a > float(adx_prev))
    if a < 20:
        return {"signal": "neutral", "strength": 0, "note": f"ADX weak/no trend ({a:.1f}<20)"}
    # Directional bias only if DI lines supplied.
    direction = "neutral"
    if plus_di is not None and minus_di is not None and not _is_nan(plus_di) and not _is_nan(minus_di):
        direction = "buy" if plus_di > minus_di else "sell"
    strength = 1
    if a >= 25: strength = 2
    if a >= 40: strength = 3
    if rising:  strength = min(3, strength + 0)  # already capped; rising amplifies note
    arrow = "rising" if rising else "stable/falling"
    return {"signal": direction, "strength": strength if direction != "neutral" else 0,
            "note": f"ADX {a:.1f} ({arrow}) trend present"}


# ==========================================================================
# 8. Stochastic %K -- Lane 1950s. 20/80 thresholds.
# ==========================================================================
def interpret_stoch_k(value: float) -> dict:
    if _is_nan(value):
        return _neutral()
    v = float(value)
    if v < 10:  return {"signal": "buy",  "strength": 3, "note": f"%K extreme oversold ({v:.1f}<10)"}
    if v < 20:  return {"signal": "buy",  "strength": 2, "note": f"%K oversold ({v:.1f}<20)"}
    if v > 90:  return {"signal": "sell", "strength": 3, "note": f"%K extreme overbought ({v:.1f}>90)"}
    if v > 80:  return {"signal": "sell", "strength": 2, "note": f"%K overbought ({v:.1f}>80)"}
    return _neutral(f"%K mid ({v:.1f})")


# ==========================================================================
# 9. Williams %R -- Williams 1973. Range -100..0; -80/-20 thresholds.
# ==========================================================================
def interpret_williams_r(value: float) -> dict:
    if _is_nan(value):
        return _neutral()
    v = float(value)
    if v < -90:  return {"signal": "buy",  "strength": 3, "note": f"%R extreme oversold ({v:.1f}<-90)"}
    if v < -80:  return {"signal": "buy",  "strength": 2, "note": f"%R oversold ({v:.1f}<-80)"}
    if v > -10:  return {"signal": "sell", "strength": 3, "note": f"%R extreme overbought ({v:.1f}>-10)"}
    if v > -20:  return {"signal": "sell", "strength": 2, "note": f"%R overbought ({v:.1f}>-20)"}
    return _neutral(f"%R mid ({v:.1f})")


# ==========================================================================
# 10. OBV slope (10-bar) -- Granville 1963. Sign + magnitude relative to
#     trailing |slope| 60-day median (caller supplies z-like ratio).
# ==========================================================================
def interpret_obv_slope(slope: float, slope_z: Optional[float] = None) -> dict:
    if _is_nan(slope):
        return _neutral()
    s = float(slope)
    z = abs(float(slope_z)) if (slope_z is not None and not _is_nan(slope_z)) else 0.0
    if s > 0 and z >= 2.0:  return {"signal": "buy",  "strength": 3, "note": "OBV rising sharply"}
    if s > 0 and z >= 1.0:  return {"signal": "buy",  "strength": 2, "note": "OBV rising"}
    if s > 0:               return {"signal": "buy",  "strength": 1, "note": "OBV mildly up"}
    if s < 0 and z >= 2.0:  return {"signal": "sell", "strength": 3, "note": "OBV falling sharply"}
    if s < 0 and z >= 1.0:  return {"signal": "sell", "strength": 2, "note": "OBV falling"}
    if s < 0:               return {"signal": "sell", "strength": 1, "note": "OBV mildly down"}
    return _neutral("OBV flat")


# ==========================================================================
# 11. MFI(14) -- Quong & Soudack 1989. Volume-weighted RSI; same 20/80 rule.
# ==========================================================================
def interpret_mfi_14(value: float) -> dict:
    if _is_nan(value):
        return _neutral()
    v = float(value)
    if v < 10:  return {"signal": "buy",  "strength": 3, "note": f"MFI extreme oversold ({v:.1f}<10)"}
    if v < 20:  return {"signal": "buy",  "strength": 2, "note": f"MFI oversold ({v:.1f}<20)"}
    if v > 90:  return {"signal": "sell", "strength": 3, "note": f"MFI extreme overbought ({v:.1f}>90)"}
    if v > 80:  return {"signal": "sell", "strength": 2, "note": f"MFI overbought ({v:.1f}>80)"}
    return _neutral(f"MFI mid ({v:.1f})")


# ==========================================================================
# 12. CMF(20) -- Chaikin. +-0.1 buffer (StockCharts convention) to reduce
#     whipsaws around the zero line; >+0.25 / <-0.25 = strong.
# ==========================================================================
def interpret_cmf_20(value: float) -> dict:
    if _is_nan(value):
        return _neutral()
    v = float(value)
    if v >  0.25: return {"signal": "buy",  "strength": 3, "note": f"CMF strong accumulation ({v:+.2f})"}
    if v >  0.10: return {"signal": "buy",  "strength": 2, "note": f"CMF accumulation ({v:+.2f})"}
    if v >  0.05: return {"signal": "buy",  "strength": 1, "note": f"CMF mild accumulation ({v:+.2f})"}
    if v < -0.25: return {"signal": "sell", "strength": 3, "note": f"CMF strong distribution ({v:+.2f})"}
    if v < -0.10: return {"signal": "sell", "strength": 2, "note": f"CMF distribution ({v:+.2f})"}
    if v < -0.05: return {"signal": "sell", "strength": 1, "note": f"CMF mild distribution ({v:+.2f})"}
    return _neutral(f"CMF flat ({v:+.2f})")


# ==========================================================================
# 13. ATR% -- volatility regime context, not direction. Compare to its own
#     rolling-60d distribution (caller supplies percentile in [0,1]).
# ==========================================================================
def interpret_atr_pct(atr_pct: float, percentile_60d: Optional[float] = None) -> dict:
    if _is_nan(atr_pct):
        return _neutral()
    if percentile_60d is None or _is_nan(percentile_60d):
        return {"signal": "neutral", "strength": 0, "note": f"ATR% {float(atr_pct):.2f}"}
    p = float(percentile_60d)
    if p < 0.20: return {"signal": "neutral", "strength": 1, "note": "ATR low-vol regime (<20th %ile)"}
    if p > 0.80: return {"signal": "neutral", "strength": 1, "note": "ATR high-vol regime (>80th %ile)"}
    return _neutral("ATR normal regime")


# ==========================================================================
# 14. SMA50 vs SMA200 (Golden / Death cross). Days-since damps strength.
#     Fresh cross (<10 trading days) = 3; <30 = 2; <90 = 1; >90 = trend filter only.
# ==========================================================================
def interpret_sma_50_200(state: int, days_since: Optional[int] = None) -> dict:
    if state is None or _is_nan(state):
        return _neutral()
    s = int(state)
    d = int(days_since) if (days_since is not None and not _is_nan(days_since)) else 999
    if s > 0:
        if d < 10:  return {"signal": "buy", "strength": 3, "note": f"Golden cross (fresh, {d}d)"}
        if d < 30:  return {"signal": "buy", "strength": 2, "note": f"Golden cross ({d}d ago)"}
        if d < 90:  return {"signal": "buy", "strength": 1, "note": f"Golden cross ({d}d ago)"}
        return {"signal": "buy", "strength": 1, "note": "SMA50>SMA200 (mature uptrend)"}
    if s < 0:
        if d < 10:  return {"signal": "sell", "strength": 3, "note": f"Death cross (fresh, {d}d)"}
        if d < 30:  return {"signal": "sell", "strength": 2, "note": f"Death cross ({d}d ago)"}
        if d < 90:  return {"signal": "sell", "strength": 1, "note": f"Death cross ({d}d ago)"}
        return {"signal": "sell", "strength": 1, "note": "SMA50<SMA200 (mature downtrend)"}
    return _neutral("no SMA50/200 relation")


# ==========================================================================
# 15. EMA9 vs EMA21 -- short-term momentum. state=+1/-1.
# ==========================================================================
def interpret_ema_9_21(state: int, days_since: Optional[int] = None) -> dict:
    if state is None or _is_nan(state):
        return _neutral()
    s = int(state)
    d = int(days_since) if (days_since is not None and not _is_nan(days_since)) else 999
    if s > 0:
        return {"signal": "buy", "strength": 2 if d < 5 else 1,
                "note": f"EMA9>EMA21 short-term up ({d}d)"}
    if s < 0:
        return {"signal": "sell", "strength": 2 if d < 5 else 1,
                "note": f"EMA9<EMA21 short-term down ({d}d)"}
    return _neutral("EMA9/21 flat")


# ==========================================================================
# 16. Donchian channel (20). position_in_range in [0,1]; breakout_flag in
#     {+1, -1, 0}.
# ==========================================================================
def interpret_donchian_20(position: float, breakout_flag: int = 0) -> dict:
    if _is_nan(position):
        return _neutral()
    p = float(position)
    bf = int(breakout_flag) if (breakout_flag is not None and not _is_nan(breakout_flag)) else 0
    if bf > 0:  return {"signal": "buy",  "strength": 3, "note": "Donchian-20 upper breakout"}
    if bf < 0:  return {"signal": "sell", "strength": 3, "note": "Donchian-20 lower breakout"}
    if p > 0.85: return {"signal": "sell", "strength": 1, "note": f"near 20d high (pos={p:.2f})"}
    if p < 0.15: return {"signal": "buy",  "strength": 1, "note": f"near 20d low (pos={p:.2f})"}
    return _neutral(f"mid-range (pos={p:.2f})")


# ==========================================================================
# 17. Price vs SMA200 -- secular trend filter.
# ==========================================================================
def interpret_price_vs_sma200(ratio: float) -> dict:
    """ratio = close / sma200; >1 bullish, <1 bearish."""
    if _is_nan(ratio):
        return _neutral()
    r = float(ratio)
    if r > 1.10: return {"signal": "buy",  "strength": 2, "note": f"price >10% above SMA200 (r={r:.2f})"}
    if r > 1.00: return {"signal": "buy",  "strength": 1, "note": f"price above SMA200 (r={r:.2f})"}
    if r < 0.90: return {"signal": "sell", "strength": 2, "note": f"price >10% below SMA200 (r={r:.2f})"}
    if r < 1.00: return {"signal": "sell", "strength": 1, "note": f"price below SMA200 (r={r:.2f})"}
    return _neutral(f"price at SMA200 (r={r:.2f})")


# ==========================================================================
# Aggregator -- weighted net strength, mirrors TradingView's "Summary".
# Inputs: list of dicts each carrying keys signal, strength, note, +optional
#   indicator_key (used for weights lookup). If key missing, weight=1.0.
# ==========================================================================
def aggregate_signals(
    indicator_outputs: Iterable[dict],
    weights: Optional[dict[str, float]] = None,
) -> dict:
    weights = weights if weights is not None else DEFAULT_WEIGHTS
    outs = list(indicator_outputs)

    buy_strength = 0.0
    sell_strength = 0.0
    buy_count = 0
    sell_count = 0
    neutral_count = 0

    for o in outs:
        sig = o.get("signal", "neutral")
        strength = float(o.get("strength", 0))
        key = o.get("indicator_key")
        w = weights.get(key, 1.0) if key else 1.0
        if sig == "buy":
            buy_strength += w * strength
            buy_count += 1
        elif sig == "sell":
            sell_strength += w * strength
            sell_count += 1
        else:
            neutral_count += 1

    net = buy_strength - sell_strength
    n_total = len(outs)

    # Label thresholds tuned for ~17 indicators with default weights.
    # Max theoretical net per side is roughly sum(weight*3) ~= 50, so
    # +/-8 / +/-16 buckets are ~16% / ~32% saturation -- comparable to
    # TradingView's STRONG-vs-regular split (which uses MA/Osc subsumes).
    if   net >=  16: label = "STRONG BUY"
    elif net >=   8: label = "BUY"
    elif net <= -16: label = "STRONG SELL"
    elif net <=  -8: label = "SELL"
    else:            label = "NEUTRAL"

    return {
        "label": label,
        "net_strength": round(net, 2),
        "buy_strength": round(buy_strength, 2),
        "sell_strength": round(sell_strength, 2),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "neutral_count": neutral_count,
        "n_total": n_total,
    }


# ==========================================================================
# Per-timeframe wrapper. `features` is a dict from indicator name to the
# numeric value(s) needed for interpretation. Caller is expected to have
# already computed these via `methodology.technical_indicators`.
#
# Reliability notes per timeframe (used by callers to tune `weights`):
#   1wk: RSI/Stoch extremes are RARE and meaningful; ADX>25 = real cycle
#        trend; golden/death crosses dominate. Reduce OBV/short-term EMA.
#   1d : Balanced -- the canonical timeframe for these textbook rules.
#   1h : RSI/Stoch flip frequently; Donchian-20 + MACD-cross + BB %b
#        dominate. ADX needs >30 to mean anything. Discount RSI(2) less.
# ==========================================================================
def interpret_timeframe(
    features: dict,
    timeframe: str = "1d",
    weights: Optional[dict[str, float]] = None,
) -> dict:
    """Run all 17 interpreters and return the aggregate verdict.

    `features` keys expected (None/missing -> neutral skip):
        rsi_14, rsi_2, macd_hist, macd_hist_prev, macd_cross,
        bb_pctb, bb_squeeze, adx_14, adx_14_prev, plus_di, minus_di,
        stoch_k, williams_r, obv_slope, obv_slope_z, mfi_14, cmf_20,
        atr_pct, atr_pct_percentile, sma_50_200_state, sma_50_200_days,
        ema_9_21_state, ema_9_21_days, donchian_pos, donchian_break,
        price_vs_sma200_ratio
    """
    f = features
    def tag(key, d): d["indicator_key"] = key; return d

    results = [
        tag("rsi_14",          interpret_rsi_14(f.get("rsi_14"))),
        tag("rsi_2",           interpret_rsi_2(f.get("rsi_2"))),
        tag("macd_hist",       interpret_macd_hist(f.get("macd_hist"), f.get("macd_hist_prev"))),
        tag("macd_cross",      interpret_macd_cross(f.get("macd_cross"))),
        tag("bb_pctb",         interpret_bb_pctb(f.get("bb_pctb"))),
        tag("bb_squeeze",      interpret_bb_squeeze(f.get("bb_squeeze"))),
        tag("adx_14",          interpret_adx_14(f.get("adx_14"), f.get("adx_14_prev"),
                                                f.get("plus_di"), f.get("minus_di"))),
        tag("stoch_k",         interpret_stoch_k(f.get("stoch_k"))),
        tag("williams_r",      interpret_williams_r(f.get("williams_r"))),
        tag("obv_slope",       interpret_obv_slope(f.get("obv_slope"), f.get("obv_slope_z"))),
        tag("mfi_14",          interpret_mfi_14(f.get("mfi_14"))),
        tag("cmf_20",          interpret_cmf_20(f.get("cmf_20"))),
        tag("atr_pct",         interpret_atr_pct(f.get("atr_pct"), f.get("atr_pct_percentile"))),
        tag("sma_50_200_cross",interpret_sma_50_200(f.get("sma_50_200_state"),
                                                    f.get("sma_50_200_days"))),
        tag("ema_9_21_cross",  interpret_ema_9_21(f.get("ema_9_21_state"),
                                                  f.get("ema_9_21_days"))),
        tag("donchian_20",     interpret_donchian_20(f.get("donchian_pos", float("nan")),
                                                     f.get("donchian_break", 0))),
        tag("price_vs_sma200", interpret_price_vs_sma200(f.get("price_vs_sma200_ratio"))),
    ]

    summary = aggregate_signals(results, weights=weights)
    summary["timeframe"] = timeframe
    summary["details"] = results
    summary["version"] = TA_SIGNAL_RULES_VERSION
    return summary


__all__ = [
    "TA_SIGNAL_RULES_VERSION",
    "DEFAULT_WEIGHTS",
    "interpret_rsi_14", "interpret_rsi_2", "interpret_macd_hist",
    "interpret_macd_cross", "interpret_bb_pctb", "interpret_bb_squeeze",
    "interpret_adx_14", "interpret_stoch_k", "interpret_williams_r",
    "interpret_obv_slope", "interpret_mfi_14", "interpret_cmf_20",
    "interpret_atr_pct", "interpret_sma_50_200", "interpret_ema_9_21",
    "interpret_donchian_20", "interpret_price_vs_sma200",
    "aggregate_signals", "interpret_timeframe",
]
