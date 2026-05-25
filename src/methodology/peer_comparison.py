"""Peer comparison research module.

I built this as a value-momentum peer-ranking layer that mirrors the
single-asset analog of Asness-Frazzini-Pedersen's cross-asset
value/momentum result. Given a target ticker and its sector, the
module pulls 4-9 sector peers in a similar market-cap band, computes
percentile ranks on P/E (valuation) and 20-day price change
(momentum), and emits a categorical ``peer_signal`` plus a numeric
``peer_score`` in [-1, +1].

The signal vocabulary is intentionally small for unambiguous
interpretation:

  * ``cheap_leader``      -- valuation-cheap AND momentum-leader
                             (>=60th pct momentum AND <=40th pct P/E).
  * ``expensive_laggard`` -- valuation-rich AND momentum-laggard
                             (<=40th pct momentum AND >=60th pct P/E).
  * ``cheap_laggard``     -- value-trap pattern; cheap on multiples
                             but weak momentum.
  * ``expensive_leader``  -- growth-momentum pattern; rich on
                             multiples but strong momentum.
  * ``mixed``             -- no consistent rank across metrics.
  * ``no_peers``          -- fewer than 3 peers identified.

The score blends the two centered percentiles (cheap-side P/E,
leader-side momentum) and clips to [-1, +1]. Positive values indicate
a cheap-and-strong profile relative to peers; negative values
indicate an expensive-and-weak profile.

References:
  Fama-French 1992 (cross-section of stock returns): book/market and
    size drive most cross-sectional dispersion.
  Asness-Frazzini-Pedersen 2013 (value/momentum everywhere): combining
    cheap + momentum across asset classes produces a robust positive
    Sharpe; this module is the single-asset analog.
  Lakonishok-Shleifer-Vishny 1994 (contrarian investment): isolated
    "cheap" without momentum confirmation is a value-trap signature.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


PEER_COMPARISON_VERSION = "1.0"

_DEFAULT_DB = Path("data/fundamentals.db")
_FINVIZ_TABLE = "finviz_universe_history"

# Universe of candidate peer columns we'd like (in priority order). We
# fall back gracefully on whatever the FINVIZ table provides.
_FINVIZ_NUMERIC_KEYS = (
    "market_cap",
    "pe_ratio",
    "price",
    "change_pct",
    "volume",
)


@dataclass
class PeerComparison:
    ticker: str
    sector: Optional[str]
    n_peers: int = 0
    peers: list[str] = field(default_factory=list)
    pe_percentile: Optional[float] = None      # 0=cheapest, 1=most expensive
    momentum_percentile: Optional[float] = None  # 0=worst, 1=best (20d change)
    mcap_percentile: Optional[float] = None
    peer_signal: str = "no_peers"
    peer_score: float = 0.0
    notes: str = ""
    version: str = PEER_COMPARISON_VERSION


def _connect(db_path: Optional[Path] = None) -> Optional[sqlite3.Connection]:
    p = Path(db_path) if db_path else _DEFAULT_DB
    if not p.exists():
        return None
    try:
        return sqlite3.connect(str(p))
    except sqlite3.DatabaseError:
        return None


_FINVIZ_COL_MAP = {
    # canonical key -> column name in finviz_universe_history table
    "ticker":      "Ticker",
    "sector":      "Sector",
    "market_cap":  "Market Cap",
    "pe_ratio":    "P/E",
    "price":       "Price",
    "change_pct":  "Perf Month",   # 20-trading-day momentum proxy
    "rsi":         "RSI",
    "gross_margin": "Gross M",
    "debt_to_eq":  "Debt/Eq",
}


def _latest_universe_rows(
    conn: sqlite3.Connection,
    sector: str,
) -> list[dict]:
    """Return the most recent row per ticker for ``sector`` from FINVIZ table.

    The FINVIZ history table uses raw FINVIZ headers (``Market Cap``,
    ``P/E``, ``Perf Month``, ...); ``_FINVIZ_COL_MAP`` normalises them
    to the lowercase keys this module uses internally. We pick the
    most recent ``scrape_timestamp`` per ticker.
    """
    cur = conn.cursor()
    try:
        rows = cur.execute(
            f'SELECT * FROM {_FINVIZ_TABLE} WHERE Sector = ?',
            (sector,),
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    if not rows:
        return []
    cols = [c[0] for c in cur.description]

    by_ticker: dict[str, dict] = {}
    for r in rows:
        raw = dict(zip(cols, r))
        t_raw = raw.get("Ticker")
        if not t_raw:
            continue
        t = str(t_raw).upper()
        # Map to canonical keys
        norm = {canon: raw.get(src) for canon, src in _FINVIZ_COL_MAP.items()}
        ts = str(raw.get("scrape_timestamp") or "")
        existing = by_ticker.get(t)
        if existing is None or ts > str(existing.get("_scrape_ts") or ""):
            norm["_scrape_ts"] = ts
            by_ticker[t] = norm
    return list(by_ticker.values())


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _percentile_rank(values: list[float], target: float) -> Optional[float]:
    """Fraction of peers with value STRICTLY LESS than target.

    Returns 0.0 if target is the minimum, 1.0 if the maximum.
    """
    if not values:
        return None
    below = sum(1 for v in values if v < target)
    return below / len(values)


def _select_peers(
    target: str,
    target_mcap: Optional[float],
    sector_rows: list[dict],
    *,
    band_lo: float = 0.20,
    band_hi: float = 5.00,
    max_n: int = 9,
) -> list[dict]:
    """Pick peers in the same market-cap band as target.

    Default band: 0.2x to 5x of target_mcap (a roughly 25x span,
    which is the standard sell-side peer-group convention for
    large/mid caps). If target_mcap is unknown, fall back to any
    sector peer.
    """
    candidates = [r for r in sector_rows if (r.get("ticker") or "").upper() != target]
    if target_mcap is None or target_mcap <= 0:
        return candidates[:max_n]
    in_band = []
    for r in candidates:
        m = _to_float(r.get("market_cap"))
        if m is None or m <= 0:
            continue
        if band_lo * target_mcap <= m <= band_hi * target_mcap:
            in_band.append(r)
    # Rank by closeness to target market cap.
    in_band.sort(key=lambda r: abs(math.log(_to_float(r["market_cap"]) / target_mcap)))
    return in_band[:max_n]


def _signal_from_percentiles(
    pe_pct: Optional[float],
    mom_pct: Optional[float],
) -> tuple[str, float]:
    """Pick the symbolic signal + numeric score from rank pair."""
    if pe_pct is None or mom_pct is None:
        return "mixed", 0.0
    cheap = pe_pct <= 0.40
    expensive = pe_pct >= 0.60
    leader = mom_pct >= 0.60
    laggard = mom_pct <= 0.40

    if cheap and leader:
        signal = "cheap_leader"
    elif expensive and laggard:
        signal = "expensive_laggard"
    elif cheap and laggard:
        signal = "cheap_laggard"
    elif expensive and leader:
        signal = "expensive_leader"
    else:
        signal = "mixed"

    # Score: positive when momentum-strong and pe-cheap (signs of edge),
    # negative when reversed. Each percentile contributes (pct - 0.5) * 2
    # so range is [-1, +1] before averaging.
    pe_component = -2 * (pe_pct - 0.5)        # cheap (low pct) -> positive
    mom_component = 2 * (mom_pct - 0.5)       # leader (high pct) -> positive
    score = (pe_component + mom_component) / 2.0
    return signal, max(-1.0, min(1.0, score))


def compute_peer_comparison(
    *,
    ticker: str,
    sector: Optional[str],
    target_mcap: Optional[float] = None,
    target_pe: Optional[float] = None,
    target_mom_20d: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> PeerComparison:
    """Build a peer comparison for ``ticker`` against same-sector peers.

    Caller supplies the target's own metrics (pe / 20d-momentum /
    mcap); the function looks up sector peers from the FINVIZ history
    DB and ranks the target against them. Returns a populated
    PeerComparison with at minimum ``peer_signal`` and ``peer_score``.

    Resilient to missing data: if sector is None, the FINVIZ DB is
    missing, or fewer than 3 peers can be identified, returns the
    no-peers sentinel.
    """
    result = PeerComparison(ticker=ticker.upper(), sector=sector)
    if not sector:
        result.notes = "sector unknown -- no peer set available"
        return result

    conn = _connect(db_path)
    if conn is None:
        result.notes = f"FINVIZ DB missing at {db_path or _DEFAULT_DB}"
        return result

    try:
        sector_rows = _latest_universe_rows(conn, sector)
    finally:
        conn.close()

    if len(sector_rows) < 4:
        result.notes = f"only {len(sector_rows)} rows in {sector}; need >=4"
        return result

    peers = _select_peers(ticker.upper(), target_mcap, sector_rows)
    if len(peers) < 3:
        result.notes = f"only {len(peers)} peers in target mcap band"
        return result

    # Rank target against peers.
    peer_pes = [_to_float(r.get("pe_ratio")) for r in peers]
    peer_pes = [p for p in peer_pes if p is not None and p > 0]
    peer_mom = [_to_float(r.get("change_pct")) for r in peers]
    peer_mom = [m for m in peer_mom if m is not None]
    peer_mcap = [_to_float(r.get("market_cap")) for r in peers]
    peer_mcap = [m for m in peer_mcap if m is not None and m > 0]

    if target_pe is not None and peer_pes:
        result.pe_percentile = _percentile_rank(peer_pes, target_pe)
    if target_mom_20d is not None and peer_mom:
        result.momentum_percentile = _percentile_rank(peer_mom, target_mom_20d)
    if target_mcap is not None and peer_mcap:
        result.mcap_percentile = _percentile_rank(peer_mcap, target_mcap)

    result.n_peers = len(peers)
    result.peers = [(r.get("ticker") or "").upper() for r in peers]
    result.peer_signal, result.peer_score = _signal_from_percentiles(
        result.pe_percentile, result.momentum_percentile,
    )
    result.notes = (
        f"{len(peers)} peers in {sector} (mcap band 0.2x-5x); "
        f"signal={result.peer_signal}; score={result.peer_score:+.2f}"
    )
    return result
