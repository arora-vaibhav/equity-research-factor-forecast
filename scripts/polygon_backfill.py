"""Polygon free-tier options backfill scheduler.

I built this to grow a local options DB one ticker at a time without
tripping Polygon's free-tier rate limit (5 req/min). The script
schedules per-ticker chain-and-greeks downloads and skips (ticker,
today) rows that already exist, so re-runs are idempotent.

Order of operations:
  1. Optional watchlist (config/watchlist.json) first, if present.
  2. Top market-cap from finviz_universe_history second.
  3. Daily incremental: skip (ticker, today) rows already in DB.
  4. 5 req/min token bucket; ~6-10 min/ticker on the free tier.

Run via Windows Task Scheduler (daily 17:30 ET):
    setx POLYGON_API_KEY "<your-key>"
    python scripts/polygon_backfill.py --watchlist-only
    python scripts/polygon_backfill.py --top-n 20 --min-mcap-b 50
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.common.datasources.polygon_options import (  # noqa: E402
    PolygonClient,
    compute_options_daily_summary,
    fetch_options_chain_with_greeks,
    init_options_db,
    log_request,
    persist_chain,
    persist_daily_summary,
)


def _load_watchlist() -> list[str]:
    p = REPO_ROOT / "config" / "watchlist.json"
    if not p.exists():
        return []
    try:
        wl = json.loads(p.read_text(encoding="utf-8"))
        return [t["ticker"] for t in wl.get("tickers", [])]
    except Exception:
        return []


def _load_universe_topn(top_n: int, min_mcap_b: float) -> list[str]:
    db = REPO_ROOT / "data" / "fundamentals.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT Ticker FROM finviz_universe_history "
            "WHERE [Market Cap] >= ? "
            "ORDER BY scrape_timestamp DESC, [Market Cap] DESC",
            (min_mcap_b * 1e9,),
        ).fetchall()
    finally:
        conn.close()
    seen: set[str] = set()
    out: list[str] = []
    for (t,) in rows:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
            if len(out) >= top_n:
                break
    return out


def _already_have_today(
    ticker: str, today: _dt.date, conn: sqlite3.Connection,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM options_daily_summary WHERE ticker = ? AND snapshot_date = ?",
        (ticker.upper(), today.isoformat()),
    ).fetchone()
    return row is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist-only", action="store_true")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--min-mcap-b", type=float, default=50.0)
    parser.add_argument("--rpm", type=int, default=5)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        print(
            "[ERROR] POLYGON_API_KEY environment variable not set.\n"
            "        On Windows:  setx POLYGON_API_KEY \"your-key-here\"\n"
            "        Then restart this shell and re-run.",
            file=sys.stderr,
        )
        return 1

    client = PolygonClient(api_key=api_key, rpm=args.rpm)
    conn = init_options_db()
    today = _dt.date.today()

    tickers: list[str] = _load_watchlist()
    if not args.watchlist_only:
        seen = set(tickers)
        for t in _load_universe_topn(args.top_n, args.min_mcap_b):
            if t not in seen:
                tickers.append(t)
                seen.add(t)

    print(f"Polygon backfill {today}: {len(tickers)} tickers @ {args.rpm}/min")
    print(f"Order: {tickers[:10]}{'...' if len(tickers) > 10 else ''}\n")

    n_done = n_skip = n_err = 0
    for i, t in enumerate(tickers, 1):
        if not args.force and _already_have_today(t, today, conn):
            print(f"  [{i:3d}/{len(tickers)}] {t:6s}  skip (have today's row)")
            n_skip += 1
            continue
        try:
            print(f"  [{i:3d}/{len(tickers)}] {t:6s}  fetching...", flush=True)
            spot = client.underlying_close(t)
            if not spot or spot <= 0:
                print(f"      no underlying close")
                log_request(t, "underlying_close", 0, 0)
                n_err += 1
                continue
            chain = fetch_options_chain_with_greeks(
                client, t, spot=spot, today=today,
            )
            n_rows = persist_chain(chain, conn) if not chain.empty else 0
            summary = compute_options_daily_summary(chain, spot=spot, today=today)
            persist_daily_summary(summary, t, conn)
            log_request(t, "chain+summary", 200, n_rows)
            n_done += 1
            iv30 = summary.get("atm_iv_30d")
            skew = summary.get("iv_skew_25d")
            iv30_s = f"{iv30:.3f}" if iv30 is not None else "n/a"
            skew_s = f"{skew:+.4f}" if skew is not None else "n/a"
            print(f"      spot=${spot:.2f}  n={n_rows}  "
                  f"atm_iv_30d={iv30_s}  skew={skew_s}")
        except Exception as exc:
            print(f"      ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            log_request(t, "ERROR", 0, 0)
            n_err += 1

    print(f"\nDone. {n_done} fetched, {n_skip} skipped, {n_err} errors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
