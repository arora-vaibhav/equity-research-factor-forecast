"""Synthetic forecast pipeline demo.

I wrote this so the forecast pipeline could be exercised end-to-end
without any network calls or a populated database. Useful for unit
tests, CI smoke checks, and showing the shape of the report to a
reviewer with no API keys configured.

The companion script ``forecast_live_demo.py`` is the live-data
variant; this one stays fully self-contained.

Output: ``data/reports/<as_of_date>/<ticker>.html`` + index.html.

Run:
    python scripts/forecast_demo.py
    python scripts/forecast_demo.py --output-dir /tmp/demo
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.layer3_forecast.factor_forecast import (  # noqa: E402
    DEFAULT_FACTORS,
    compute_factor_loadings,
    forecast_return,
)
from src.layer3_forecast.report import (  # noqa: E402
    render_index_page,
    render_stock_report,
)


TEST_TICKERS = ["AAPL", "MSFT", "SPY", "JPM", "XOM"]

TICKER_META: dict[str, dict[str, str]] = {
    "AAPL": {"company": "Apple Inc.",                 "sector": "Technology"},
    "MSFT": {"company": "Microsoft Corporation",      "sector": "Technology"},
    "SPY":  {"company": "SPDR S&P 500 ETF Trust",     "sector": "Broad market ETF"},
    "JPM":  {"company": "JPMorgan Chase & Co.",       "sector": "Financial Services"},
    "XOM":  {"company": "Exxon Mobil Corporation",    "sector": "Energy"},
}


def _build_synthetic_history(n: int = 500, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n_factors = len(DEFAULT_FACTORS)
    X = rng.normal(0.0, 1.0, size=(n, n_factors))
    true_coef = np.zeros(n_factors)
    true_coef[DEFAULT_FACTORS.index("value_score")] = 0.05
    true_coef[DEFAULT_FACTORS.index("momentum_score")] = 0.03
    true_coef[DEFAULT_FACTORS.index("news_activity_score")] = 0.04
    true_coef[DEFAULT_FACTORS.index("quality_score")] = 0.015
    y = X @ true_coef + rng.normal(0.0, 0.025, size=n)
    df = pd.DataFrame(X, columns=list(DEFAULT_FACTORS))
    df["realized_horizon_return"] = y
    return df


def _build_synthetic_ticker_factors(seed: int) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    return {f: float(rng.normal(0.0, 1.0)) for f in DEFAULT_FACTORS}


def _build_synthetic_close(*, n: int, last_price: float, seed: int) -> pd.Series:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0003, 0.012, size=n)
    closes = last_price * np.exp(np.cumsum(rets))
    idx = pd.bdate_range(end=_dt.date.today(), periods=n)
    return pd.Series(closes, index=idx, name="close")


def _build_synthetic_catalysts(ticker: str, as_of_date: str) -> list[dict]:
    as_of = _dt.date.fromisoformat(as_of_date)
    return [{
        "catalyst_date": (as_of + _dt.timedelta(days=45)).isoformat(),
        "catalyst_type": "earnings",
        "source": "synthetic",
        "confidence": "med",
        "catalyst_description": f"Synthetic earnings catalyst for {ticker}",
    }]


def run_synthetic_demo(
    output_dir: Path,
    *,
    as_of_date: str,
    horizon_days: int = 30,
) -> dict[str, Path]:
    history = _build_synthetic_history()
    loadings, intercept, residuals = compute_factor_loadings(
        history, horizon_days=horizon_days,
    )

    paths: dict[str, Path] = {}
    for i, ticker in enumerate(TEST_TICKERS):
        meta = TICKER_META.get(ticker, {})
        factors = _build_synthetic_ticker_factors(seed=100 + i)
        anchor_price = {
            "AAPL": 195.0, "MSFT": 420.0, "SPY": 530.0,
            "JPM": 195.0, "XOM": 115.0,
        }.get(ticker, 100.0)
        closes = _build_synthetic_close(
            n=252, last_price=anchor_price, seed=200 + i,
        )
        forecast = forecast_return(
            ticker_factors=factors,
            loadings=loadings,
            intercept=intercept,
            residuals=residuals,
            ticker=ticker,
            as_of_date=as_of_date,
            horizon_days=horizon_days,
        )
        catalysts = _build_synthetic_catalysts(ticker, as_of_date)
        path = render_stock_report(
            ticker=ticker,
            forecast=forecast,
            loadings=loadings,
            ticker_factors=factors,
            historical_close=closes,
            catalysts=catalysts,
            company_name=meta.get("company"),
            sector=meta.get("sector"),
            output_dir=output_dir,
            allow_llm=False,
        )
        paths[ticker] = path

    index_path = render_index_page(
        tickers=TEST_TICKERS,
        output_dir=output_dir,
        as_of_date=as_of_date,
    )
    paths["__index__"] = index_path
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Forecast Layer demo")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--as-of-date", type=str, default=None)
    parser.add_argument("--horizon-days", type=int, default=30)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--from-db", type=Path, default=None)
    args = parser.parse_args()

    as_of = args.as_of_date or _dt.date.today().isoformat()
    output_dir = args.output_dir or (
        REPO_ROOT / "data" / "reports" / as_of
    )

    if args.from_db is not None:
        print(
            "from-db mode is a placeholder; the live-data variant lives in "
            "scripts/forecast_live_demo.py. Use synthetic (default) here.",
            file=sys.stderr,
        )
        return 2

    paths = run_synthetic_demo(
        output_dir=output_dir,
        as_of_date=as_of,
        horizon_days=args.horizon_days,
    )

    print(f"Forecast demo (synthetic) -- {as_of}, horizon={args.horizon_days}d")
    for k, v in paths.items():
        if k == "__index__":
            print(f"  index : {v}")
        else:
            print(f"  {k:6s}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
