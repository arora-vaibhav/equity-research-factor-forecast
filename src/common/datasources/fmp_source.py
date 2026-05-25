"""Financial Modeling Prep (FMP) data source -- free tier (v7).

Per the v7 research-agent verdict: FMP `ratios-ttm` + `price-target-
consensus` is the highest-ROI fundamentals add. Replaces yfinance .info
(which lacks ROIC, FCF yield, debt/EBITDA) and gives real analyst
estimates with revision history.

Free tier: 250 req/day, ~3 req/s soft cap, 5-yr history, US-only.
Cache: SQLite data/cache.sqlite table api_cache. Per-endpoint TTLs.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests


FMP_SOURCE_VERSION = "1.0"
_BASE = "https://financialmodelingprep.com"
_DB = Path(os.environ.get("TI_CACHE_DB", "data/cache.sqlite"))


class _Bucket:
    def __init__(self, n: int, window_s: float):
        self.n = n
        self.window = window_s
        self.q: deque[float] = deque()

    def acquire(self) -> None:
        now = time.monotonic()
        while self.q and now - self.q[0] > self.window:
            self.q.popleft()
        if len(self.q) >= self.n:
            sleep = self.window - (now - self.q[0]) + 0.05
            time.sleep(max(sleep, 0.0))
            return self.acquire()
        self.q.append(time.monotonic())


def _init_cache() -> None:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(_DB)) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS api_cache ("
            " provider TEXT, endpoint TEXT, ticker TEXT, params_hash TEXT,"
            " fetched_at TEXT, payload TEXT,"
            " PRIMARY KEY (provider, endpoint, ticker, params_hash))"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS ix_api_cache_fetched "
            "ON api_cache(fetched_at)"
        )


def _cache_get(provider: str, endpoint: str, ticker: Optional[str],
               params: dict, ttl_s: float):
    h = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()
    with sqlite3.connect(str(_DB)) as c:
        row = c.execute(
            "SELECT fetched_at, payload FROM api_cache "
            "WHERE provider = ? AND endpoint = ? AND ticker = ? "
            "AND params_hash = ?",
            (provider, endpoint, ticker or "", h),
        ).fetchone()
    if not row:
        return None
    try:
        ts = datetime.fromisoformat(row[0])
    except ValueError:
        return None
    if (datetime.now(timezone.utc) - ts).total_seconds() > ttl_s:
        return None
    try:
        return json.loads(row[1])
    except json.JSONDecodeError:
        return None


def _cache_put(provider: str, endpoint: str, ticker: Optional[str],
               params: dict, payload: object) -> None:
    h = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()
    with sqlite3.connect(str(_DB)) as c:
        c.execute(
            "INSERT OR REPLACE INTO api_cache "
            "(provider, endpoint, ticker, params_hash, fetched_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (provider, endpoint, ticker or "", h,
             datetime.now(timezone.utc).isoformat(),
             json.dumps(payload, default=str)),
        )


def _yfinance_ratios_fallback(ticker: str) -> dict:
    """Best-effort ratios when FMP free tier doesn't cover this symbol.

    yfinance .info has different field names + slightly different definitions
    (ROE = TTM not balance-sheet point-in-time, etc.) but it covers the
    long-tail ADRs and small-caps FMP charges for. Output mirrors the
    same key set as `fetch_financial_ratios` so the renderer doesn't
    need to know which source it came from.
    """
    try:
        import yfinance as _yf  # noqa: WPS433  -- local import; yfinance is heavy
    except Exception:
        return {}
    try:
        info = _yf.Ticker(ticker).info or {}
    except Exception:
        return {}
    if not info:
        return {}

    def _g(*keys):
        for k in keys:
            v = info.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None

    pe = _g("trailingPE", "forwardPE")
    p_fcf = _g("priceToFreeCashFlow", "priceToFreeCashflow")
    # yfinance unit quirks (verified 2026-05): debtToEquity and dividendYield
    # are PERCENT (e.g. 217.6 / 7.24); the rest are ratios.
    yf_d2e = _g("debtToEquity")
    yf_dy = _g("dividendYield")
    return {
        "roe":            _g("returnOnEquity"),
        "roic":           None,
        "fcf_yield":      (1.0 / p_fcf) if p_fcf else None,
        "debt_to_ebitda": None,
        "debt_to_equity": (yf_d2e / 100.0) if yf_d2e is not None else None,
        "debt_to_assets": None,
        "current_ratio":  _g("currentRatio"),
        "quick_ratio":    _g("quickRatio"),
        "interest_coverage": None,
        "gross_margin":   _g("grossMargins"),
        "op_margin":      _g("operatingMargins"),
        "net_margin":     _g("profitMargins"),
        "ebitda_margin":  _g("ebitdaMargins"),
        "pe":             pe,
        "pb":             _g("priceToBook"),
        "ps":             _g("priceToSalesTrailing12Months"),
        "ev_ebitda":      _g("enterpriseToEbitda"),
        "earnings_yield": (1.0 / pe) if (pe and pe != 0) else None,
        "dividend_yield": (yf_dy / 100.0) if yf_dy is not None else None,
        "_provenance":    "yfinance.info (FMP free-tier subscription gate)",
    }


class FMPClient:
    def __init__(self, api_key: Optional[str] = None):
        self.token = api_key or os.environ.get("FMP_API_KEY")
        if not self.token:
            raise ValueError(
                "FMP_API_KEY not set. Add it to .env or pass api_key=..."
            )
        self.day = _Bucket(250, 86400)
        self.sec = _Bucket(3, 1)
        self.sess = requests.Session()
        _init_cache()

    def _get(self, path: str, params: dict, *,
             endpoint: str, ticker: Optional[str], ttl_s: float):
        cached = _cache_get("fmp", endpoint, ticker, params, ttl_s)
        if cached is not None:
            # Subscription-gate sentinel: treat as "no data" without re-call.
            if isinstance(cached, dict) and cached.get("_subscription_gate"):
                return []
            return cached
        self.sec.acquire()
        self.day.acquire()
        p = {**params, "apikey": self.token}
        url = f"{_BASE}{path}"
        for attempt in range(3):
            try:
                r = self.sess.get(url, params=p, timeout=20)
                if r.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                # 402 = "symbol not in your subscription tier",
                # 403 = "legacy endpoint retired". These are deterministic,
                # not transient -- no retry. Cache an explicit sentinel so
                # the call does not re-burn the rate-limit budget.
                # `_cache_get` normalizes the sentinel back to `[]` on
                # subsequent reads.
                if r.status_code in (402, 403):
                    sentinel = {"_subscription_gate": True,
                                "_status": r.status_code,
                                "_endpoint": endpoint}
                    _cache_put("fmp", endpoint, ticker, params, sentinel)
                    return []
                r.raise_for_status()
                data = r.json()
                # Do NOT cache empty payloads -- they would trap callers
                # in a 24h stale window when an upstream hiccup returns
                # [] for a real ticker. Only cache non-empty results.
                if data not in (None, [], {}, "", 0):
                    _cache_put("fmp", endpoint, ticker, params, data)
                return data
            except requests.RequestException:
                if attempt == 2:
                    return [] if endpoint != "single" else {}
                time.sleep(2 ** attempt)
        return []

    def fetch_financial_ratios(self, ticker: str) -> dict:
        """TTM ratios -- ROE, FCF yield, EV/EBITDA, margins, P/E, P/B + derived.

        FMP's `/stable/ratios-ttm` endpoint (the only one returning data
        on free tier as of 2026-05) no longer surfaces ROE / ROIC /
        FCF-yield / debt-to-EBITDA as direct fields. Derivations:

            returnOnEquityTTM  -> DuPont: netMargin * assetTurnover * leverage
            freeCashFlowYieldTTM -> 1 / priceToFreeCashFlowRatioTTM
            earningsYield      -> 1 / priceToEarningsRatioTTM
            ROIC / debt-to-EBITDA: not derivable from free-tier set
                                   (would need EBIT(1-t)/invested-capital
                                   or debt/marketcap * marketcap/EV)
        """
        data = self._get(
            "/stable/ratios-ttm", {"symbol": ticker.upper()},
            endpoint="ratios-ttm", ticker=ticker, ttl_s=24 * 3600,
        )
        if not data:
            data = self._get(
                f"/api/v3/ratios-ttm/{ticker.upper()}", {},
                endpoint="ratios-ttm-v3", ticker=ticker, ttl_s=24 * 3600,
            )
        if not data:
            # When FMP's free tier does not cover this symbol (402
            # Special Endpoint), fall back to yfinance .info -- it has
            # trailingPE, priceToBook, returnOnEquity, debtToEquity for
            # most US tickers and ADRs.
            return _yfinance_ratios_fallback(ticker)
        r = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        if not isinstance(r, dict):
            return {}

        def _f(*keys):
            """First non-None numeric value from candidate keys."""
            for k in keys:
                v = r.get(k)
                if v is None:
                    continue
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
            return None

        # Direct fields (new endpoint primary; legacy v3 names as fallback)
        net_margin = _f("netProfitMarginTTM", "netProfitMargin")
        asset_turnover = _f("assetTurnoverTTM", "assetTurnover")
        leverage = _f("financialLeverageRatioTTM", "companyEquityMultiplier")
        p_fcf = _f("priceToFreeCashFlowRatioTTM", "priceToFreeCashFlowsRatio",
                   "pfcfRatio")
        pe = _f("priceToEarningsRatioTTM", "priceEarningsRatio", "peRatioTTM")

        # Derived
        if net_margin is not None and asset_turnover is not None and leverage is not None:
            roe = net_margin * asset_turnover * leverage
        else:
            roe = _f("returnOnEquityTTM", "returnOnEquity")
        fcf_yield = (1.0 / p_fcf) if (p_fcf is not None and p_fcf != 0) else \
                    _f("freeCashFlowYieldTTM", "freeCashFlowYield")
        earnings_yield = (1.0 / pe) if (pe is not None and pe != 0) else None

        return {
            "roe":            roe,
            "roic":           _f("returnOnInvestedCapitalTTM", "returnOnInvestedCapital"),
            "fcf_yield":      fcf_yield,
            "debt_to_ebitda": _f("debtToEBITDATTM", "netDebtToEBITDA"),
            "debt_to_equity": _f("debtToEquityRatioTTM", "debtEquityRatio"),
            "debt_to_assets": _f("debtToAssetsRatioTTM", "debtRatio"),
            "current_ratio":  _f("currentRatioTTM", "currentRatio"),
            "quick_ratio":    _f("quickRatioTTM", "quickRatio"),
            "interest_coverage": _f("interestCoverageRatioTTM", "interestCoverage"),
            "gross_margin":   _f("grossProfitMarginTTM", "grossProfitMargin"),
            "op_margin":      _f("operatingProfitMarginTTM", "operatingProfitMargin"),
            "net_margin":     net_margin,
            "ebitda_margin":  _f("ebitdaMarginTTM"),
            "pe":             pe,
            "pb":             _f("priceToBookRatioTTM", "priceBookValueRatio"),
            "ps":             _f("priceToSalesRatioTTM", "priceToSalesRatio"),
            "ev_ebitda":      _f("enterpriseValueMultipleTTM", "enterpriseValueMultiple"),
            "earnings_yield": earnings_yield,
            "dividend_yield": _f("dividendYieldTTM"),
            "_provenance": "stable/ratios-ttm; roe=dupont(NM*AT*EM); fcf_yld=1/PFCF",
        }

    def fetch_price_target_consensus(self, ticker: str) -> dict:
        data = self._get(
            "/stable/price-target-consensus", {"symbol": ticker.upper()},
            endpoint="ptc", ticker=ticker, ttl_s=24 * 3600,
        )
        if not data:
            return {}
        t = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        if not isinstance(t, dict):
            return {}
        return {
            "target_high":   t.get("targetHigh"),
            "target_low":    t.get("targetLow"),
            "target_mean":   t.get("targetConsensus") or t.get("targetMean"),
            "target_median": t.get("targetMedian"),
        }

    def fetch_analyst_estimates(self, ticker: str, period: str = "annual") -> pd.DataFrame:
        data = self._get(
            "/stable/analyst-estimates",
            {"symbol": ticker.upper(), "period": period, "limit": 5},
            endpoint="estimates", ticker=ticker, ttl_s=24 * 3600,
        )
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
        return df

    def fetch_insider_trading(self, ticker: str, page: int = 0) -> pd.DataFrame:
        data = self._get(
            "/stable/insider-trading",
            {"symbol": ticker.upper(), "page": page},
            endpoint="insider", ticker=ticker, ttl_s=4 * 3600,
        )
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        for c in ("filingDate", "transactionDate"):
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], errors="coerce")
        return df

    def fetch_earnings_calendar(self, start: str, end: str) -> pd.DataFrame:
        data = self._get(
            "/stable/earnings-calendar", {"from": start, "to": end},
            endpoint="earnings-cal", ticker=None, ttl_s=4 * 3600,
        )
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
        return df

    def fetch_news(self, ticker: str, limit: int = 50) -> pd.DataFrame:
        data = self._get(
            "/api/v3/stock_news",
            {"tickers": ticker.upper(), "limit": limit},
            endpoint="news", ticker=ticker, ttl_s=900,
        )
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        if "publishedDate" in df.columns:
            df["date"] = pd.to_datetime(df["publishedDate"], errors="coerce")
        return df


def compute_revisions_score(
    fmp: FMPClient, ticker: str, *, last_price: float,
) -> tuple[float, dict]:
    """Real upside-to-target + EPS revision composite.

    Returns (score in [-1, +1], info dict).
    """
    consensus = fmp.fetch_price_target_consensus(ticker)
    tgt_mean = consensus.get("target_mean")
    info: dict = {"target_mean": tgt_mean,
                  "target_high": consensus.get("target_high"),
                  "target_low": consensus.get("target_low")}

    upside_pct = 0.0
    if tgt_mean and last_price > 0:
        upside_pct = float(tgt_mean) / float(last_price) - 1.0
        info["upside_pct"] = upside_pct

    eps_rev_z = 0.0
    try:
        est = fmp.fetch_analyst_estimates(ticker)
        if not est.empty and "estimatedEpsAvg" in est.columns:
            est = est.dropna(subset=["estimatedEpsAvg"]).sort_values("date")
            if len(est) >= 2:
                eps_recent = float(est["estimatedEpsAvg"].iloc[-1])
                eps_prior = float(est["estimatedEpsAvg"].iloc[-2])
                if eps_prior != 0:
                    eps_rev_z = (eps_recent - eps_prior) / abs(eps_prior)
                info["eps_revision_pct"] = eps_rev_z
    except Exception:
        pass

    raw = 0.7 * upside_pct + 0.3 * eps_rev_z
    score = max(-1.0, min(1.0, raw * 3.0))
    info["revisions_score"] = score
    return score, info
