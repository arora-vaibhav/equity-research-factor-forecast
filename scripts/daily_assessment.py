"""Daily accuracy-assessment scheduler.

I built this so I could track forecast quality longitudinally. Each
run appends one row per ticker to ``data/accuracy_log.csv`` and the
CSV becomes a record of how directional accuracy / MAE / 80% CI hit
rate evolve over time. Running it daily as a scheduled task surfaces
regressions without me having to remember to re-benchmark.

What this script does, run once a day:
  1. Load the universe (top-N by market cap from finviz_universe_history).
  2. For each ticker, pull 3y price history via yfinance.
  3. Run walk-forward backtest (60d lookback, horizons 1/30/60).
  4. Append one row per ticker to data/accuracy_log.csv with the latest
     directional accuracy + MAE + Pearson(pred, real).
  5. Optionally re-run the full forecast_live_demo.py pipeline to refresh
     the report bundle.

Run via Windows Task Scheduler / cron:

  Windows (PowerShell, daily 18:00):
    schtasks /create /tn "daily forecast assessment" /tr ^
      "python <repo>\\scripts\\daily_assessment.py --universe" ^
      /sc daily /st 18:00

  Linux / Mac cron:
    0 18 * * * cd /path/to/repo && python scripts/daily_assessment.py --universe
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.layer3_forecast.walk_forward_backtest import (  # noqa: E402
    run_walk_forward,
)
from src.methodology.volume_features import (  # noqa: E402
    volume_confirmed_forecast,
)


def _fetch_history(ticker: str, period: str = "3y") -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period=period)
        if df is None or df.empty:
            return None
        df.columns = [c.lower() for c in df.columns]
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        return df
    except Exception as exc:
        print(f"  [warn] yfinance failed for {ticker}: {exc}", file=sys.stderr)
        return None


def _accuracy_row(
    ticker: str, as_of_date: str, hist: pd.DataFrame,
) -> Optional[dict]:
    if hist is None or hist.empty or "close" not in hist.columns:
        return None
    vol = hist["volume"] if "volume" in hist.columns else None

    def _wf_fn(c, lb, h):
        return volume_confirmed_forecast(c, lb, h, volume=vol)

    summary = run_walk_forward(
        ticker=ticker, as_of_date=as_of_date,
        close=hist["close"], lookback_days=60,
        horizons=(1, 30, 60),
        forecast_fn=_wf_fn if vol is not None else None,
    )
    if not summary.horizons:
        return None
    by_h = {h.horizon_days: h for h in summary.horizons}
    return {
        "date": as_of_date, "ticker": ticker, "n_walks": summary.n_walks,
        "dir_acc_1d":  by_h[1].directional_accuracy if 1 in by_h else None,
        "dir_acc_30d": by_h[30].directional_accuracy if 30 in by_h else None,
        "dir_acc_60d": by_h[60].directional_accuracy if 60 in by_h else None,
        "mae_1d":      by_h[1].mae_return if 1 in by_h else None,
        "mae_30d":     by_h[30].mae_return if 30 in by_h else None,
        "mae_60d":     by_h[60].mae_return if 60 in by_h else None,
        "pearson_1d":  by_h[1].pearson_pred_vs_realized if 1 in by_h else None,
        "pearson_30d": by_h[30].pearson_pred_vs_realized if 30 in by_h else None,
        "ci80_hit_30d": by_h[30].ci80_hit_rate if 30 in by_h else None,
    }


def _load_universe(top_n: int = 50, min_mcap_b: float = 10.0) -> list[str]:
    import sqlite3
    db_path = REPO_ROOT / "data" / "fundamentals.db"
    if not db_path.exists():
        return []
    c = sqlite3.connect(str(db_path))
    try:
        rows = c.execute(
            "SELECT Ticker FROM finviz_universe_history WHERE [Market Cap] >= ? "
            "ORDER BY scrape_timestamp DESC, [Market Cap] DESC",
            (min_mcap_b * 1e9,),
        ).fetchall()
    finally:
        c.close()
    seen: set[str] = set()
    out: list[str] = []
    for (t,) in rows:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
            if len(out) >= top_n:
                break
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily forecast-accuracy assessment")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Explicit ticker list; else uses --universe")
    parser.add_argument("--universe", action="store_true",
                        help="Load top-N tickers from finviz_universe_history")
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--min-mcap-b", type=float, default=10.0)
    parser.add_argument("--log-path", type=Path,
                        default=REPO_ROOT / "data" / "accuracy_log.csv")
    parser.add_argument("--refresh-reports", action="store_true",
                        help="Also run scripts/forecast_live_demo.py to refresh report bundle")
    args = parser.parse_args()

    as_of = _dt.date.today().isoformat()
    if args.tickers:
        tickers = args.tickers
    elif args.universe:
        tickers = _load_universe(top_n=args.top_n, min_mcap_b=args.min_mcap_b)
    else:
        tickers = ["AAPL", "MSFT", "SPY", "JPM", "XOM"]

    print(f"Daily assessment {as_of}: {len(tickers)} tickers")
    rows: list[dict] = []
    for i, t in enumerate(tickers, 1):
        print(f"  [{i:3d}/{len(tickers)}] {t}", end=" ... ", flush=True)
        hist = _fetch_history(t, "3y")
        row = _accuracy_row(t, as_of, hist) if hist is not None else None
        if row is None:
            print("skipped")
            continue
        rows.append(row)
        print(
            f"1d={row['dir_acc_1d']:.1%}  "
            f"30d={row['dir_acc_30d']:.1%}  "
            f"60d={row['dir_acc_60d']:.1%}  "
            f"n_walks={row['n_walks']}"
        )

    # append to CSV log
    args.log_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "date", "ticker", "n_walks",
        "dir_acc_1d", "dir_acc_30d", "dir_acc_60d",
        "mae_1d", "mae_30d", "mae_60d",
        "pearson_1d", "pearson_30d", "ci80_hit_30d",
    ]
    write_header = not args.log_path.exists()
    with args.log_path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\nWrote {len(rows)} rows to {args.log_path}")

    if args.refresh_reports:
        print("\nRefreshing report bundle via forecast_live_demo.py ...")
        import subprocess
        cmd = [sys.executable, str(REPO_ROOT / "scripts" / "forecast_live_demo.py")]
        if args.universe:
            cmd += ["--universe", "--universe-top-n", str(args.top_n),
                    "--universe-min-mcap-b", str(args.min_mcap_b)]
        elif args.tickers:
            cmd += ["--tickers"] + args.tickers
        cmd += ["--skip-sentiment"]
        subprocess.run(cmd, check=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
