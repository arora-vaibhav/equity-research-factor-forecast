"""Polygon.io options data source -- free Basic tier compatible.

Integrates Polygon free-tier options. Per the research-agent verdict:
5 req/min, EOD only, snapshot endpoint PAYWALLED. Discover contracts
via /v3/reference/options/contracts, pull EOD bars via
/v2/aggs/ticker/{O:...}/prev, compute IV + Greeks ourselves via Brent
root-solve on Black-Scholes.

Schema (auto-init at data/polygon_options.db):
  options_contracts        (ticker, snapshot_date, expiry, strike, side, ...)
  options_daily_summary    (ticker, snapshot_date, atm_iv_*, skew, pcr, ...)

Log: data/polygon_log.csv.
"""
from __future__ import annotations

import csv
import datetime as _dt
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

POLYGON_OPTIONS_VERSION = "1.0"
BASE = "https://api.polygon.io"
DEFAULT_DB = Path("data") / "polygon_options.db"
DEFAULT_LOG = Path("data") / "polygon_log.csv"


@dataclass
class TokenBucket:
    rate_per_min: int = 5
    capacity: int = 5
    tokens: float = 5.0
    last: float = 0.0

    def __post_init__(self):
        self.last = time.monotonic()

    def take(self) -> None:
        now = time.monotonic()
        self.tokens = min(
            self.capacity,
            self.tokens + (now - self.last) * self.rate_per_min / 60.0,
        )
        self.last = now
        if self.tokens < 1.0:
            wait = (1.0 - self.tokens) * 60.0 / self.rate_per_min
            time.sleep(wait + 0.05)
            self.tokens = 0.0
        else:
            self.tokens -= 1.0


class PolygonClient:
    def __init__(self, api_key: Optional[str] = None, rpm: int = 5):
        self.key = api_key or os.environ.get("POLYGON_API_KEY")
        if not self.key:
            raise ValueError(
                "POLYGON_API_KEY not provided. Set env var or pass api_key=..."
            )
        self.bucket = TokenBucket(rate_per_min=rpm, capacity=rpm,
                                  tokens=float(rpm))
        self.sess = requests.Session()

    def _get(self, path: str, **params) -> dict:
        self.bucket.take()
        params["apiKey"] = self.key
        url = f"{BASE}{path}"
        for attempt in range(3):
            r = self.sess.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def list_contracts(
        self, ticker: str, exp_gte: str, exp_lte: str,
    ) -> list[dict]:
        out: list[dict] = []
        cursor: Optional[str] = None
        while True:
            params = {
                "underlying_ticker": ticker.upper(),
                "expiration_date.gte": exp_gte,
                "expiration_date.lte": exp_lte,
                "limit": 1000, "expired": "false",
            }
            if cursor:
                params["cursor"] = cursor
            j = self._get("/v3/reference/options/contracts", **params)
            out.extend(j.get("results", []))
            nxt = j.get("next_url")
            if not nxt:
                break
            if "cursor=" in nxt:
                cursor = nxt.split("cursor=")[-1].split("&")[0]
            else:
                break
        return out

    def prev_bar(self, option_symbol: str) -> Optional[dict]:
        try:
            j = self._get(
                f"/v2/aggs/ticker/{option_symbol}/prev",
                adjusted="true",
            )
        except Exception:
            return None
        res = j.get("results") or []
        return res[0] if res else None

    def underlying_close(self, ticker: str) -> Optional[float]:
        try:
            j = self._get(
                f"/v2/aggs/ticker/{ticker.upper()}/prev",
                adjusted="true",
            )
        except Exception:
            return None
        res = j.get("results") or []
        return float(res[0]["c"]) if res else None


# ----- Black-Scholes helpers -----

def _norm_cdf(x: float) -> float:
    from statistics import NormalDist
    return NormalDist().cdf(x)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _bs_price(S: float, K: float, T: float, r: float,
              sigma: float, side: str) -> float:
    if sigma <= 0 or T <= 0:
        intrinsic = (S - K) if side == "C" else (K - S)
        return max(0.0, intrinsic)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if side == "C":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_iv(S: float, K: float, T: float, r: float,
           mkt: float, side: str) -> float:
    if mkt is None or not (mkt > 0) or S <= 0:
        return float("nan")
    intrinsic = max(0.0, (S - K) if side == "C" else (K - S))
    if mkt < intrinsic:
        return float("nan")
    try:
        from scipy.optimize import brentq
        return float(brentq(
            lambda v: _bs_price(S, K, T, r, v, side) - mkt,
            1e-4, 5.0, maxiter=64,
        ))
    except Exception:
        return float("nan")


def _bs_greeks(S: float, K: float, T: float, r: float,
               sigma: float, side: str) -> dict:
    if sigma is None or sigma != sigma or sigma <= 0 or T <= 0:
        return {"delta": float("nan"), "gamma": float("nan"),
                "theta": float("nan"), "vega": float("nan")}
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    pdf = _norm_pdf(d1)
    delta = _norm_cdf(d1) if side == "C" else _norm_cdf(d1) - 1
    gamma = pdf / (S * sigma * math.sqrt(T))
    vega = S * pdf * math.sqrt(T) / 100.0
    if side == "C":
        theta = (-S * pdf * sigma / (2 * math.sqrt(T))
                 - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365.0
    else:
        theta = (-S * pdf * sigma / (2 * math.sqrt(T))
                 + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def _yrs_to_expiry(expiry: str, as_of: Optional[_dt.date] = None) -> float:
    as_of = as_of or _dt.date.today()
    d = _dt.date.fromisoformat(expiry)
    return max((d - as_of).days, 1) / 365.25


# ----- High-level -----

def fetch_options_chain_with_greeks(
    client: PolygonClient,
    ticker: str,
    *,
    spot: Optional[float] = None,
    risk_free: float = 0.045,
    dte_targets: tuple[int, ...] = (30, 60, 90),
    strike_pct: float = 0.30,
    today: Optional[_dt.date] = None,
) -> pd.DataFrame:
    """Pull contracts + EOD bars + compute IV/Greeks ourselves."""
    today = today or _dt.date.today()
    spot = spot if spot is not None else client.underlying_close(ticker)
    if not spot or spot <= 0:
        return pd.DataFrame()
    exp_lte = (today + _dt.timedelta(days=max(dte_targets) + 30)).isoformat()
    contracts = client.list_contracts(ticker, today.isoformat(), exp_lte)

    rows = []
    for c in contracts:
        try:
            strike = float(c["strike_price"])
        except (KeyError, ValueError, TypeError):
            continue
        if abs(strike / spot - 1) > strike_pct:
            continue
        sym = c.get("ticker")
        if not sym:
            continue
        bar = client.prev_bar(sym)
        if not bar:
            continue
        side = str(c.get("contract_type", "")).upper()[:1]
        if side not in ("C", "P"):
            continue
        last = float(bar.get("c", 0.0) or 0.0)
        vol = int(bar.get("v") or 0)
        rows.append({
            "ticker": ticker.upper(),
            "snapshot_date": today.isoformat(),
            "expiry": c.get("expiration_date"),
            "strike": strike,
            "side": side,
            "contract_symbol": sym,
            "last": last, "mid": last,
            "volume": vol, "oi": None,
            "bid": None, "ask": None,
            "source": "polygon_free",
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    def _row(r):
        T = _yrs_to_expiry(r["expiry"], today)
        iv = _bs_iv(spot, r["strike"], T, risk_free, r["last"], r["side"])
        if not np.isfinite(iv):
            return pd.Series({"iv": float("nan"), "delta": float("nan"),
                              "gamma": float("nan"), "theta": float("nan"),
                              "vega": float("nan")})
        g = _bs_greeks(spot, r["strike"], T, risk_free, iv, r["side"])
        return pd.Series({"iv": iv, **g})

    return pd.concat([df, df.apply(_row, axis=1)], axis=1)


def compute_options_daily_summary(
    chain_df: pd.DataFrame, spot: float, today: Optional[_dt.date] = None,
) -> dict:
    today = today or _dt.date.today()
    summary = {"snapshot_date": today.isoformat(), "spot": spot,
               "notes": "free_tier:no_oi"}
    if chain_df is None or chain_df.empty:
        summary["notes"] = "no chain available"
        return summary

    for dte in (30, 60, 90):
        bucket = chain_df[chain_df["expiry"].apply(
            lambda e: abs((_dt.date.fromisoformat(e) - today).days - dte) <= 10
        )]
        if bucket.empty:
            summary[f"atm_iv_{dte}d"] = None
            continue
        atm = bucket.iloc[(bucket["strike"] - spot).abs().argsort()[:4]]
        iv_vals = atm["iv"].dropna()
        summary[f"atm_iv_{dte}d"] = float(iv_vals.mean()) if not iv_vals.empty else None

    nearest = sorted(chain_df["expiry"].dropna().unique())
    if nearest:
        ne = nearest[0]
        sub = chain_df[chain_df["expiry"] == ne]
        calls = sub[(sub["side"] == "C") & sub["delta"].notna()].sort_values("delta")
        puts = sub[(sub["side"] == "P") & sub["delta"].notna()].sort_values("delta")
        if len(calls) >= 3 and len(puts) >= 3:
            iv_c25 = float(np.interp(0.25, calls["delta"].to_numpy(),
                                     calls["iv"].to_numpy()))
            iv_p25 = float(np.interp(-0.25, puts["delta"].to_numpy(),
                                     puts["iv"].to_numpy()))
            summary["iv_skew_25d"] = iv_p25 - iv_c25
        else:
            summary["iv_skew_25d"] = None
    else:
        summary["iv_skew_25d"] = None

    call_vol = float(chain_df[chain_df["side"] == "C"]["volume"].sum())
    put_vol = float(chain_df[chain_df["side"] == "P"]["volume"].sum())
    summary["pcr_volume"] = (put_vol / call_vol) if call_vol > 0 else None

    near_30 = chain_df[chain_df["expiry"].apply(
        lambda e: abs((_dt.date.fromisoformat(e) - today).days - 30) <= 10
    )]
    if not near_30.empty:
        atm_idx_pos = (near_30["strike"] - spot).abs().to_numpy().argmin()
        K_atm = float(near_30.iloc[atm_idx_pos]["strike"])
        call_row = near_30[(near_30["side"] == "C") & (near_30["strike"] == K_atm)]
        put_row = near_30[(near_30["side"] == "P") & (near_30["strike"] == K_atm)]
        if not call_row.empty and not put_row.empty:
            straddle = float(call_row["last"].iloc[0]) + float(put_row["last"].iloc[0])
            summary["implied_move_30d"] = straddle / spot
        else:
            summary["implied_move_30d"] = None
    else:
        summary["implied_move_30d"] = None

    return summary


# ----- Persistence -----

_SCHEMA = """
CREATE TABLE IF NOT EXISTS options_contracts (
  ticker TEXT, snapshot_date TEXT, expiry TEXT, strike REAL, side TEXT,
  contract_symbol TEXT, bid REAL, ask REAL, last REAL, mid REAL,
  iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
  oi INTEGER, volume INTEGER, source TEXT, fetched_at TEXT,
  PRIMARY KEY (ticker, snapshot_date, expiry, strike, side)
);
CREATE INDEX IF NOT EXISTS idx_contracts_ticker_date
  ON options_contracts(ticker, snapshot_date);

CREATE TABLE IF NOT EXISTS options_daily_summary (
  ticker TEXT, snapshot_date TEXT,
  spot REAL,
  atm_iv_30d REAL, atm_iv_60d REAL, atm_iv_90d REAL, atm_iv_180d REAL,
  iv_skew_25d REAL, term_slope_30_90 REAL,
  pcr_volume REAL, pcr_oi REAL,
  max_pain REAL, total_gamma REAL,
  implied_move_30d REAL, realized_vol_30d REAL,
  notes TEXT, fetched_at TEXT,
  PRIMARY KEY (ticker, snapshot_date)
);
"""


def init_options_db(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def persist_chain(df: pd.DataFrame, conn: sqlite3.Connection) -> int:
    if df is None or df.empty:
        return 0
    df = df.assign(fetched_at=_dt.datetime.utcnow().isoformat())
    cols = [
        "ticker", "snapshot_date", "expiry", "strike", "side",
        "contract_symbol", "bid", "ask", "last", "mid",
        "iv", "delta", "gamma", "theta", "vega",
        "oi", "volume", "source", "fetched_at",
    ]
    df.reindex(columns=cols).to_sql(
        "options_contracts", conn, if_exists="append", index=False,
        method="multi", chunksize=500,
    )
    conn.commit()
    return len(df)


def persist_daily_summary(
    summary: dict, ticker: str, conn: sqlite3.Connection,
) -> None:
    summary["ticker"] = ticker.upper()
    summary["fetched_at"] = _dt.datetime.utcnow().isoformat()
    cols = [
        "ticker", "snapshot_date", "spot",
        "atm_iv_30d", "atm_iv_60d", "atm_iv_90d", "atm_iv_180d",
        "iv_skew_25d", "term_slope_30_90",
        "pcr_volume", "pcr_oi",
        "max_pain", "total_gamma",
        "implied_move_30d", "realized_vol_30d",
        "notes", "fetched_at",
    ]
    row = {c: summary.get(c) for c in cols}
    pd.DataFrame([row], columns=cols).to_sql(
        "options_daily_summary", conn, if_exists="append", index=False,
    )
    conn.commit()


def log_request(
    ticker: str, endpoint: str, status: int, n_records: int,
    log_path: Path = DEFAULT_LOG,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    new = not log_path.exists()
    with log_path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "ticker", "endpoint",
                        "status", "n_records"])
        w.writerow([
            _dt.datetime.utcnow().isoformat(),
            ticker, endpoint, status, n_records,
        ])
