"""Free real-time sentiment sources for individual US equities.

Pulls StockTwits (user-tagged bull/bear), ApeWisdom (Reddit WSB mention
counts), and yfinance options chain (25-delta IV skew = "smart-money"
proxy).

All free, no auth. Each wrapped in try/except so one flaky source
never blocks the report.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import requests


FREE_SENTIMENT_VERSION = "1.0"

_STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
_APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}"


@dataclass
class SentimentSnapshot:
    ticker: str
    stocktwits_bull_count: int = 0
    stocktwits_bear_count: int = 0
    stocktwits_net_sentiment: float = 0.0
    stocktwits_message_volume: int = 0
    apewisdom_mentions: int = 0
    apewisdom_mentions_24h_change: int = 0
    apewisdom_rank: Optional[int] = None
    iv_skew_25d: Optional[float] = None
    iv_skew_interpretation: str = "unknown"
    composite_score: float = 0.0
    composite_n_signals: int = 0
    sources_available: list[str] = field(default_factory=list)
    free_sentiment_version: str = FREE_SENTIMENT_VERSION


def _safe_get(url: str, *, timeout: float = 10.0) -> Optional[dict]:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Trade-Identifier/1.0 (research)"},
            timeout=timeout,
        )
        if not resp.ok:
            return None
        return resp.json()
    except Exception:
        return None


def fetch_stocktwits_sentiment(ticker: str) -> dict:
    """StockTwits public stream -- counts Bullish/Bearish-tagged messages."""
    payload = _safe_get(_STOCKTWITS_URL.format(ticker=ticker.upper()))
    out = {
        "bull_count": 0, "bear_count": 0,
        "message_volume": 0, "net_sentiment": 0.0,
        "available": False,
    }
    if payload is None:
        return out
    messages = payload.get("messages") or []
    out["message_volume"] = len(messages)
    for m in messages:
        ent = m.get("entities") or {}
        sent = (ent.get("sentiment") or {}).get("basic", "")
        if sent == "Bullish":
            out["bull_count"] += 1
        elif sent == "Bearish":
            out["bear_count"] += 1
    total_tagged = out["bull_count"] + out["bear_count"]
    if total_tagged > 0:
        out["net_sentiment"] = (out["bull_count"] - out["bear_count"]) / total_tagged
    out["available"] = True
    return out


def fetch_apewisdom_mentions(ticker: str, *, pages: int = 3) -> dict:
    """ApeWisdom Reddit-mention scan over top N pages."""
    out = {
        "mentions": 0, "mentions_24h_change": 0,
        "rank": None, "available": False,
    }
    upper = ticker.upper()
    for page in range(1, pages + 1):
        payload = _safe_get(_APEWISDOM_URL.format(page=page))
        if payload is None:
            continue
        results = payload.get("results") or []
        for item in results:
            if str(item.get("ticker", "")).upper() == upper:
                try:
                    out["mentions"] = int(item.get("mentions", 0))
                except (TypeError, ValueError):
                    out["mentions"] = 0
                try:
                    out["mentions_24h_change"] = int(
                        item.get("mentions_24h_ago", out["mentions"])
                    )
                    out["mentions_24h_change"] = (
                        out["mentions"] - out["mentions_24h_change"]
                    )
                except (TypeError, ValueError):
                    out["mentions_24h_change"] = 0
                try:
                    out["rank"] = int(item.get("rank", 0))
                except (TypeError, ValueError):
                    out["rank"] = None
                out["available"] = True
                return out
    out["available"] = True
    return out


def _bs_call_delta(
    s: float, k: float, t: float, r: float, sigma: float,
) -> float:
    """Black-Scholes call delta = N(d1)."""
    import math
    from statistics import NormalDist
    if t <= 0 or sigma <= 0 or s <= 0 or k <= 0:
        return float("nan")
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return NormalDist().cdf(d1)


def _bs_put_delta(
    s: float, k: float, t: float, r: float, sigma: float,
) -> float:
    """Black-Scholes put delta = N(d1) - 1 (always negative)."""
    return _bs_call_delta(s, k, t, r, sigma) - 1.0


def _interpolate_iv_at_delta(
    chain_df, *, spot: float, t_years: float, r: float,
    target_delta: float, side: str,
) -> Optional[float]:
    """Find the IV at the strike whose Black-Scholes delta matches target.

    side: 'call' or 'put'. target_delta should be positive for call,
    negative for put. Returns interpolated IV or None.
    """
    if chain_df is None or len(chain_df) == 0:
        return None
    if "strike" not in chain_df.columns or "impliedVolatility" not in chain_df.columns:
        return None
    rows = chain_df.dropna(subset=["strike", "impliedVolatility"]).copy()
    rows = rows[(rows["impliedVolatility"] > 0.01) & (rows["impliedVolatility"] < 5.0)]
    if len(rows) < 3:
        return None
    rows = rows.sort_values("strike")
    strikes = rows["strike"].to_numpy(dtype=float)
    ivs = rows["impliedVolatility"].to_numpy(dtype=float)

    deltas = []
    for k, iv in zip(strikes, ivs):
        if side == "call":
            d = _bs_call_delta(spot, k, t_years, r, iv)
        else:
            d = _bs_put_delta(spot, k, t_years, r, iv)
        deltas.append(d)
    deltas = np.asarray(deltas, dtype=float)
    finite = np.isfinite(deltas)
    if finite.sum() < 3:
        return None
    deltas = deltas[finite]
    ivs = ivs[finite]
    if side == "call":
        order = np.argsort(-deltas)
    else:
        order = np.argsort(deltas)
    d_sorted = deltas[order]
    iv_sorted = ivs[order]
    if target_delta < d_sorted.min() or target_delta > d_sorted.max():
        if target_delta < d_sorted.min():
            return float(iv_sorted[np.argmin(d_sorted)])
        return float(iv_sorted[np.argmax(d_sorted)])
    return float(np.interp(target_delta, d_sorted, iv_sorted))


def fetch_iv_skew_25d(
    ticker: str,
    *,
    yf_ticker_factory=None,
    risk_free_rate: float = 0.045,
) -> dict:
    """True 25-delta IV skew via Black-Scholes delta interpolation.

    Replaces the strike-quantile proxy with a real Black-Scholes 25-
    delta computation.

    Method:
      1. First expiry >= 21 days out.
      2. For each strike, compute its BS delta given that strike's IV.
      3. Linearly interpolate to find IV at delta = +0.25 (call) and
         delta = -0.25 (put).
      4. Skew = put_iv_25d - call_iv_25d (Hull "Options, Futures, &
         Other Derivatives" ch.20 vol-smile convention).

    Positive skew (put IV > call IV) = bearish; negative = bullish.
    """
    out = {"iv_skew": None, "interpretation": "unknown", "available": False}
    try:
        if yf_ticker_factory is None:
            import yfinance as yf
            yf_ticker_factory = yf.Ticker
        tk = yf_ticker_factory(ticker)
        expirations = tk.options or []
        try:
            hist = tk.history(period="5d")
            if hist is None or hist.empty:
                return out
            spot = float(hist["Close"].iloc[-1])
        except Exception:
            return out
    except Exception:
        return out
    if not expirations:
        return out
    if spot <= 0:
        return out

    import datetime as _dt
    today = _dt.date.today()
    expiry = None
    expiry_date = None
    for e in expirations:
        try:
            d = _dt.date.fromisoformat(e)
        except ValueError:
            continue
        if (d - today).days >= 21:
            expiry = e
            expiry_date = d
            break
    if expiry is None and expirations:
        try:
            expiry = expirations[-1]
            expiry_date = _dt.date.fromisoformat(expiry)
        except ValueError:
            return out
    if expiry is None or expiry_date is None:
        return out
    t_years = max((expiry_date - today).days, 1) / 365.0

    try:
        chain = yf_ticker_factory(ticker).option_chain(expiry)
    except Exception:
        return out

    calls = chain.calls
    puts = chain.puts
    if calls is None or puts is None or len(calls) == 0 or len(puts) == 0:
        return out

    call_iv_25d = _interpolate_iv_at_delta(
        calls, spot=spot, t_years=t_years, r=risk_free_rate,
        target_delta=0.25, side="call",
    )
    put_iv_25d = _interpolate_iv_at_delta(
        puts, spot=spot, t_years=t_years, r=risk_free_rate,
        target_delta=-0.25, side="put",
    )
    if call_iv_25d is None or put_iv_25d is None:
        return out
    if call_iv_25d <= 0 or put_iv_25d <= 0:
        return out

    skew = put_iv_25d - call_iv_25d
    out["iv_skew"] = skew
    out["call_iv_25d"] = call_iv_25d
    out["put_iv_25d"] = put_iv_25d
    out["t_years"] = t_years
    if skew > 0.02:
        out["interpretation"] = "bearish"
    elif skew < -0.02:
        out["interpretation"] = "bullish"
    else:
        out["interpretation"] = "neutral"
    out["available"] = True
    return out


def fetch_sentiment_snapshot(
    ticker: str,
    *,
    include_stocktwits: bool = True,
    include_apewisdom: bool = True,
    include_iv_skew: bool = True,
    yf_ticker_factory=None,
) -> SentimentSnapshot:
    """Aggregate all available free sentiment sources for `ticker`."""
    snap = SentimentSnapshot(ticker=ticker.upper())
    z_components: list[float] = []
    sources: list[str] = []

    if include_stocktwits:
        st = fetch_stocktwits_sentiment(ticker)
        if st.get("available"):
            snap.stocktwits_bull_count = st["bull_count"]
            snap.stocktwits_bear_count = st["bear_count"]
            snap.stocktwits_message_volume = st["message_volume"]
            snap.stocktwits_net_sentiment = st["net_sentiment"]
            if (st["bull_count"] + st["bear_count"]) >= 5:
                z_components.append(float(st["net_sentiment"]))
                sources.append("stocktwits")

    if include_apewisdom:
        aw = fetch_apewisdom_mentions(ticker)
        if aw.get("available"):
            snap.apewisdom_mentions = aw["mentions"]
            snap.apewisdom_mentions_24h_change = aw["mentions_24h_change"]
            snap.apewisdom_rank = aw["rank"]
            prev = max(1, aw["mentions"] - aw["mentions_24h_change"])
            if aw["mentions"] > 0:
                ratio = math.log((aw["mentions"] + 1) / max(prev, 1))
                z = max(-2.0, min(2.0, ratio))
                z_components.append(z)
                sources.append("apewisdom")

    if include_iv_skew:
        iv = fetch_iv_skew_25d(ticker, yf_ticker_factory=yf_ticker_factory)
        if iv.get("available") and iv.get("iv_skew") is not None:
            snap.iv_skew_25d = iv["iv_skew"]
            snap.iv_skew_interpretation = iv["interpretation"]
            # Scale skew to a z-score with a ~3-sigma cap so a single
            # blown-out option chain can't dominate the composite
            # (Grinold-Kahn 2000 ch.3 winsorize-at-+/-3 convention).
            iv_z = -float(iv["iv_skew"]) / 0.05
            iv_z = max(-3.0, min(3.0, iv_z))
            z_components.append(iv_z)
            sources.append("iv_skew")

    if z_components:
        # Per-source clip to +/-3 (matches the factor-z winsorisation
        # used in _heuristic_ticker_factors).
        clipped = [max(-3.0, min(3.0, z)) for z in z_components]
        snap.composite_score = float(np.mean(clipped))
        snap.composite_n_signals = len(clipped)
    snap.sources_available = sources
    return snap
