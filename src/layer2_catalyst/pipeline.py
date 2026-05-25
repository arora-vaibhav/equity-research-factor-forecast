"""Layer 2 catalyst-and-setup pipeline.

Reads:
  * Layer 1 outputs (``canonical_universe`` + ``candidate_results``).
  * ``historical_price`` for OHLCV. I do not spawn a parallel
    yfinance fetch here; if a candidate has no rows in
    ``historical_price`` the candidate is skipped and logged.
  * ``catalyst_calendar`` for the catalyst-in-window check.

Computes per candidate:
  * Technical indicators (RSI/MACD/ATR/SMA-200/volume_profile/pivots).
  * Bullish or bearish setup binary feature block.
  * ``confluence_score``, ``catalyst_in_window``, ``is_active``.

Persists:
  * ``data/layer2/<as_of_date>/<run_id>/active_candidates.parquet``.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

from src.layer2_catalyst.setups import (
    ACTIVE_CONFLUENCE_THRESHOLD,
    bearish_setup_features,
    bullish_setup_features,
    confluence_score,
    is_active,
)
from src.methodology.technical_indicators import (
    atr_14,
    macd,
    pivot_points,
    rsi_14,
    sma,
    volume_profile,
)


PIPELINE_VERSION = "1.0"

# 90-day forward window for catalyst proximity.
CATALYST_WINDOW_DAYS = 90


@dataclass
class Layer2RunResult:
    """Compact return type for `run_layer2`."""
    run_id: str
    layer1_run_id: str
    as_of_date: str
    n_input_candidates: int
    n_active_candidates: int
    n_skipped_no_prices: int
    active_candidates_df: pd.DataFrame
    output_parquet_path: Optional[Path] = None
    notes: list[str] = field(default_factory=list)


def _fetch_layer1_candidates(db, layer1_run_id: str) -> pd.DataFrame:
    try:
        with db.get_connection() as conn:
            cur = conn.execute(
                """
                SELECT cr.ticker, cr.playbook, cr.composite_score,
                       cr.eligible, cu.sector, cu.market_cap_usd
                  FROM candidate_results cr
             LEFT JOIN canonical_universe cu
                    ON cu.run_id = cr.run_id AND cu.ticker = cr.ticker
                 WHERE cr.run_id = ?
                """,
                (layer1_run_id,),
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame(rows, columns=cols)
    except Exception:
        return pd.DataFrame()


def _fetch_ohlcv(db, ticker: str, lookback_days: int = 365) -> pd.DataFrame:
    try:
        with db.get_connection() as conn:
            cur = conn.execute(
                """
                SELECT observation_date, open, high, low, close, volume
                  FROM historical_price
                 WHERE ticker = ?
              ORDER BY observation_date ASC
                """,
                (ticker,),
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=cols)
        df["observation_date"] = pd.to_datetime(df["observation_date"])
        df = df.set_index("observation_date").sort_index()
        if lookback_days is not None:
            df = df.tail(lookback_days + 30)
        return df
    except Exception:
        return pd.DataFrame()


def _fetch_catalysts_in_window(
    db, ticker: str, as_of_date: str, window_days: int = CATALYST_WINDOW_DAYS,
) -> pd.DataFrame:
    try:
        as_of = _dt.date.fromisoformat(as_of_date)
    except ValueError:
        return pd.DataFrame()
    end = (as_of + _dt.timedelta(days=window_days)).isoformat()
    try:
        with db.get_connection() as conn:
            cur = conn.execute(
                """
                SELECT ticker, catalyst_type, catalyst_date,
                       catalyst_description, source, confidence
                  FROM catalyst_calendar
                 WHERE ticker = ?
                   AND catalyst_date >= ?
                   AND catalyst_date <= ?
              ORDER BY catalyst_date ASC
                """,
                (ticker, as_of_date, end),
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame(rows, columns=cols)
    except Exception:
        return pd.DataFrame()


def _compute_indicators(ohlcv: pd.DataFrame) -> dict:
    close = ohlcv["close"]
    volume = ohlcv["volume"]
    return {
        "rsi": rsi_14(close),
        "macd": macd(close),
        "atr": atr_14(ohlcv),
        "sma_200": sma(close, 200),
        "sma_20": sma(close, 20),
        "volume_profile": volume_profile(volume),
        "pivots": pivot_points(ohlcv, lookback=10),
    }


def _evaluate_candidate(
    ticker: str, playbook: str,
    ohlcv: pd.DataFrame, catalysts: pd.DataFrame,
) -> dict:
    if len(ohlcv) < 30:
        return {
            "ticker": ticker, "playbook": playbook,
            "confluence_score": 0,
            "catalyst_in_window": False, "n_catalysts": len(catalysts),
            "nearest_catalyst_date": None, "nearest_catalyst_type": None,
            "setups": {}, "is_active": False,
        }

    indicators = _compute_indicators(ohlcv)
    pb = (playbook or "").upper()
    if pb in ("PLAYBOOK_A", "A", "FINVIZ_LONG"):
        setups = bullish_setup_features(ohlcv, indicators)
    elif pb in ("PLAYBOOK_B", "B", "FINVIZ_SHORT"):
        setups = bearish_setup_features(ohlcv, indicators)
    else:
        setups = {}

    n_cat = len(catalysts)
    catalyst_in_window = n_cat > 0
    nearest_date = None
    nearest_type = None
    if n_cat > 0:
        nearest = catalysts.iloc[0]
        nearest_date = nearest["catalyst_date"]
        nearest_type = nearest["catalyst_type"]

    conf = confluence_score(setups)
    active = (
        is_active(setups, threshold=ACTIVE_CONFLUENCE_THRESHOLD)
        and catalyst_in_window
    )

    return {
        "ticker": ticker, "playbook": playbook,
        "confluence_score": conf,
        "catalyst_in_window": catalyst_in_window,
        "n_catalysts": n_cat,
        "nearest_catalyst_date": nearest_date,
        "nearest_catalyst_type": nearest_type,
        "setups": setups, "is_active": active,
    }


def run_layer2(
    layer1_run_id: str,
    as_of_date: str,
    db,
    *,
    output_dir: Optional[Path] = None,
    layer2_run_id: Optional[str] = None,
) -> Layer2RunResult:
    """Top-level Layer 2 pipeline entry."""
    if layer2_run_id is None:
        layer2_run_id = (
            "layer2_" + _dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        )

    candidates_df = _fetch_layer1_candidates(db, layer1_run_id)
    n_input = len(candidates_df)
    notes: list[str] = []

    output_rows: list[dict] = []
    n_skipped_no_prices = 0
    for _, row in candidates_df.iterrows():
        ticker = row["ticker"]
        playbook = row["playbook"]
        ohlcv = _fetch_ohlcv(db, ticker)
        if len(ohlcv) < 30:
            n_skipped_no_prices += 1
            notes.append(f"skipped {ticker}: insufficient OHLCV ({len(ohlcv)} rows)")
            continue
        catalysts = _fetch_catalysts_in_window(db, ticker, as_of_date)
        out = _evaluate_candidate(ticker, playbook, ohlcv, catalysts)
        out["layer1_run_id"] = layer1_run_id
        out["layer2_run_id"] = layer2_run_id
        out["as_of_date"] = as_of_date
        output_rows.append(out)

    active_only = [r for r in output_rows if r["is_active"]]
    active_df = pd.DataFrame(active_only)
    n_active = len(active_df)

    out_path: Optional[Path] = None
    if output_dir is None:
        output_dir = Path("data") / "layer2" / as_of_date / layer2_run_id
    output_dir = Path(output_dir)
    if n_active > 0:
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "active_candidates.parquet"
        flat = active_df.copy()
        flat["setups"] = flat["setups"].apply(
            lambda d: ",".join(k for k, v in d.items() if v) if isinstance(d, dict) else ""
        )
        flat.to_parquet(out_path, index=False)

    return Layer2RunResult(
        run_id=layer2_run_id,
        layer1_run_id=layer1_run_id,
        as_of_date=as_of_date,
        n_input_candidates=n_input,
        n_active_candidates=n_active,
        n_skipped_no_prices=n_skipped_no_prices,
        active_candidates_df=active_df,
        output_parquet_path=out_path,
        notes=notes,
    )
