"""YahooSource — adapter around yfinance.

A.1 deliverable: health_check via a probe ticker (SPY) fast_info call.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time

import yfinance as yf

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


class YahooSource(BaseDataSource):
    name = "yahoo"
    cadence = "weekly"
    provides = {
        "pe_ttm", "pe_forward", "ebit_ttm", "fcf_ttm",
        "operating_margin", "net_profit_margin",
        "total_debt_to_equity", "interest_coverage",
        "revenue_growth_yoy", "eps_growth_yoy",
        "roe", "roic",
        "company_name", "sector", "industry", "exchange",
        "market_cap_usd", "price", "avg_daily_volume",
    }

    _PROBE_TICKER = "SPY"

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            t = yf.Ticker(self._PROBE_TICKER)
            info = t.fast_info
            price = info.get("last_price") if hasattr(info, "get") else info["last_price"]
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        ok = price is not None
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK if ok else SourceStatus.PARTIAL,
            checked_at=now,
            message=f"{self._PROBE_TICKER} last_price={price}",
            response_time_ms=(time.monotonic() - started) * 1000,
        )

    def fetch_historical_price(self, ticker: str, db) -> int:
        """Pull historical OHLCV from Yahoo for the gap window only.

        Reads (source=yahoo, ticker, field=historical_price) watermark to find
        the start date; pulls only deltas. Writes to historical_price with
        INSERT OR IGNORE. Updates the watermark on success/failure.

        Returns the number of rows successfully inserted (0 if up-to-date
        or on failure, with watermark error_count incremented).
        """
        import datetime
        import pandas as pd
        from src.common.schemas import HistoricalPriceRow

        start_date, end_date = self.get_fetch_gap(ticker, "historical_price", db)
        if start_date > end_date:
            return 0
        try:
            t = yf.Ticker(ticker)
            df = t.history(start=str(start_date), end=str(end_date + datetime.timedelta(days=1)))
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="historical_price",
                last_observation_date=None, db=db, success=False,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            return 0

        if df is None or df.empty:
            self.update_watermark(
                ticker=ticker, field="historical_price",
                last_observation_date=None, db=db, success=False,
                error_message="empty history response",
            )
            return 0

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        rows = []
        for ts, r in df.iterrows():
            obs_date = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            rows.append(
                HistoricalPriceRow(
                    ticker=ticker,
                    observation_date=obs_date,
                    open=float(r["Open"]),
                    high=float(r["High"]),
                    low=float(r["Low"]),
                    close=float(r["Close"]),
                    volume=int(r["Volume"]),
                    adj_close=float(r["Adj Close"]) if "Adj Close" in r else None,
                    source="yahoo",
                    scrape_timestamp=now_iso,
                )
            )

        db.insert_historical_price(rows)
        last_obs = max(r.observation_date for r in rows)
        self.update_watermark(
            ticker=ticker, field="historical_price",
            last_observation_date=datetime.date.fromisoformat(last_obs),
            db=db, success=True,
        )
        return len(rows)

    def fetch_fundamentals_for_ticker(self, ticker: str, run_id: str):
        """Pull one-ticker fundamentals snapshot from Yahoo.

        Returns a RawYahooRow with as much detail as yfinance .info exposes.
        Returns None on exception or empty info dict.
        Missing yfinance fields become None on the row.
        """
        import datetime
        from src.common.schemas import RawYahooRow

        try:
            t = yf.Ticker(ticker)
            info = t.info
        except Exception:
            return None

        if not info:
            return None

        def _get_float(key):
            v = info.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _get_int(key):
            v = info.get(key)
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        ocf = _get_float("operatingCashflow")
        capex = _get_float("capitalExpenditures")
        fcf_ttm = None
        if ocf is not None and capex is not None:
            fcf_ttm = ocf - abs(capex)

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

        try:
            return RawYahooRow(
                run_id=run_id,
                ticker=ticker,
                company_name=info.get("longName") or info.get("shortName"),
                sector=info.get("sector"),
                industry=info.get("industry"),
                exchange=info.get("exchange"),
                market_cap=_get_float("marketCap"),
                price=_get_float("regularMarketPrice"),
                avg_daily_volume=_get_int("averageDailyVolume10Day"),
                pe_ttm=_get_float("trailingPE"),
                pe_forward=_get_float("forwardPE"),
                ebit_ttm=_get_float("ebitda"),
                fcf_ttm=fcf_ttm,
                total_debt=_get_float("totalDebt"),
                cash=_get_float("totalCash"),
                book_value=_get_float("bookValue"),
                operating_margin=_get_float("operatingMargins"),
                net_profit_margin=_get_float("profitMargins"),
                revenue_growth_yoy=_get_float("revenueGrowth"),
                eps_growth_yoy=_get_float("earningsGrowth"),
                scrape_timestamp=now_iso,
            )
        except Exception:
            return None

    def fetch_universe(self, run_id: str, ticker_list: list[str] | None = None):
        """Pull one-ticker fundamentals for each ticker in ticker_list.

        Yahoo has no "scan all stocks" endpoint, so this requires an externally
        supplied list (typically from FinvizSource.fetch_universe).

        Returns a pd.DataFrame with one row per successful ticker. Bad tickers
        are silently skipped.
        """
        import pandas as pd
        if not ticker_list:
            return pd.DataFrame()

        rows = []
        for ticker in ticker_list:
            try:
                row = self.fetch_fundamentals_for_ticker(ticker, run_id=run_id)
            except Exception:
                continue
            if row is None:
                continue
            if row.company_name is None and row.market_cap is None and row.pe_ttm is None:
                continue
            rows.append(row.model_dump())

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)
