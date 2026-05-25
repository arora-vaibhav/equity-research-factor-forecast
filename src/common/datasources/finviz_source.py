"""FinvizSource — adapter around finvizfinance.

A.3.2 wires the real fetch_universe() body. The universe filter (market-cap band)
is configurable via config/datasources.yaml > finviz_universe_filter. Default is
"+Mid (over $2bln)" which is the closest Finviz preset to a $1B+ universe intent —
Finviz doesn't expose an exact $1B threshold, so the source pulls $2B-and-up
(~2,500 US tickers). Exact $1B+ filtering can be applied downstream in factor
scoring if needed.

Other useful presets (set in config/datasources.yaml to switch):
  "Any"                       — every stock on the exchange (~10,000+ rows)
  "+Small (over $300mln)"     — small-cap and above (~5,000 rows)
  "+Mid (over $2bln)"         — DEFAULT — closest to a $1B+ universe intent
  "+Large (over $10bln)"      — large-cap and above (~700 rows)
  "Mega ($200bln and more)"   — original A.1 default (~70 rows)
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time

import yaml
from finvizfinance.screener.overview import Overview

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_DEFAULT_FINVIZ_FILTER = "+Mid (over $2bln)"
_DATASOURCES_YAML = Path(__file__).resolve().parents[3] / "config" / "datasources.yaml"


def _load_finviz_filter() -> str:
    """Read finviz_universe_filter from config/datasources.yaml; fallback default."""
    try:
        with open(_DATASOURCES_YAML, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        v = cfg.get("finviz_universe_filter")
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:
        pass
    return _DEFAULT_FINVIZ_FILTER


class FinvizSource(BaseDataSource):
    name = "finviz"
    cadence = "weekly"
    provides = {
        "pe_ttm", "pe_forward", "operating_margin", "net_profit_margin",
        "perf_1m", "perf_3m", "perf_6m", "perf_12m",
        "rsi_14", "dist_52w_high", "dist_52w_low",
        "sector", "industry", "company_name",
        "market_cap_usd", "price", "avg_daily_volume",
        "short_interest_pct_float",
    }

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            ov = Overview()
            ov.set_filter(filters_dict={"Market Cap.": "Mega ($200bln and more)"})
            df = ov.screener_view()
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        ok = df is not None and not df.empty
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK if ok else SourceStatus.PARTIAL,
            checked_at=now,
            message=f"mega-cap rows={0 if df is None else len(df)}",
            response_time_ms=(time.monotonic() - started) * 1000,
        )

    def fetch_universe(self, run_id: str) -> "pd.DataFrame":
        """Pull the Finviz universe and return a DataFrame.

        Universe band is read from config/datasources.yaml > finviz_universe_filter.
        Default "+Mid (over $2bln)" closely matches the intended "$1B and above"
        universe. See module docstring for other available presets.

        Every returned row carries run_id and scrape_timestamp.
        Errors propagate; caller logs to source_run_log.
        """
        import pandas as pd
        filter_value = _load_finviz_filter()
        ov = Overview()
        ov.set_filter(filters_dict={"Market Cap.": filter_value})
        df = ov.screener_view()
        if df is None:
            df = pd.DataFrame()
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        df = df.copy()
        df["run_id"] = run_id
        df["scrape_timestamp"] = now_iso
        return df
