"""CatalystSource -- adapter for upcoming-catalyst feeds.

Phase B.1 ships earnings dates + ex-dividend dates via yfinance. FDA
PDUFA, FOMC, lockup expiry, and index inclusion are deferred to a
later wave (B.5 post-Forecast Layer) so V can validate the substrate
on the test-set tickers first.

Reads / writes:
  * Pulls from yfinance.Ticker(ticker).get_earnings_dates() and
    .calendar / .dividends.
  * Persists to raw_catalysts (audit) and catalyst_calendar (curated
    per-run snapshot).
  * Watermark field: (catalysts, ticker, catalysts).
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Optional

import pandas as pd

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


class CatalystSource(BaseDataSource):
    name = "catalysts"
    cadence = "daily"
    provides = {"earnings_dates", "ex_dividend_dates"}

    def health_check(self) -> SourceHealthStatus:
        """Import-only health check; we don't hit network in health."""
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            import yfinance  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"yfinance import failed: {type(exc).__name__}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK,
            checked_at=now,
            message="yfinance import ok",
            response_time_ms=(time.monotonic() - started) * 1000,
        )

    @staticmethod
    def _iso_date(value) -> Optional[str]:
        """Coerce a date-like value to 'YYYY-MM-DD' or return None."""
        if value is None:
            return None
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            if "T" in s:
                s = s.split("T", 1)[0]
            if " " in s:
                s = s.split(" ", 1)[0]
            return s
        try:
            if hasattr(value, "date"):
                return value.date().isoformat()
            return str(value)[:10]
        except Exception:
            return None

    def fetch_earnings_dates(
        self,
        ticker: str,
        run_id: str,
        db=None,
        *,
        limit: int = 12,
        ticker_factory=None,
    ) -> int:
        """Fetch upcoming + recent earnings dates via yfinance.

        Returns the count of CatalystRow records persisted to BOTH
        raw_catalysts and catalyst_calendar.
        """
        if db is None:
            raise ValueError("db is required")

        if ticker_factory is None:
            import yfinance
            ticker_factory = yfinance.Ticker

        from src.common.schemas import CatalystRow

        try:
            tkr = ticker_factory(ticker)
            earnings_df = tkr.get_earnings_dates(limit=limit) if hasattr(
                tkr, "get_earnings_dates"
            ) else tkr.earnings_dates
        except Exception as exc:  # noqa: BLE001
            self.update_watermark(
                ticker=ticker, field="catalysts",
                last_observation_date=None, db=db, success=False,
                error_message=f"yf earnings: {type(exc).__name__}: {exc}",
            )
            return 0

        if earnings_df is None or len(earnings_df) == 0:
            self.update_watermark(
                ticker=ticker, field="catalysts",
                last_observation_date=None, db=db, success=True,
                error_message="empty",
            )
            return 0

        rows: list[CatalystRow] = []
        scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        latest_date: Optional[str] = None
        for idx_val in earnings_df.index:
            iso = self._iso_date(idx_val)
            if iso is None:
                continue
            rows.append(
                CatalystRow(
                    run_id=run_id,
                    ticker=ticker,
                    catalyst_type="earnings",
                    catalyst_date=iso,
                    catalyst_description=f"Earnings announcement ({ticker})",
                    source="yahoo",
                    source_url=f"https://finance.yahoo.com/quote/{ticker}/earnings",
                    confidence="high",
                    scrape_timestamp=scrape_ts,
                )
            )
            if latest_date is None or iso > latest_date:
                latest_date = iso

        db.insert_raw_catalysts(rows)
        db.insert_catalyst_calendar(rows)

        if latest_date is not None:
            import datetime as _dt
            try:
                last_obs = _dt.date.fromisoformat(latest_date)
            except ValueError:
                last_obs = None
            self.update_watermark(
                ticker=ticker, field="catalysts",
                last_observation_date=last_obs, db=db, success=True,
            )

        return len(rows)

    def fetch_ex_dividend_dates(
        self,
        ticker: str,
        run_id: str,
        db=None,
        *,
        ticker_factory=None,
    ) -> int:
        """Fetch upcoming ex-dividend date via yfinance.

        Yahoo's `Ticker.calendar` returns a dict-like with a
        'Ex-Dividend Date' key. Past ex-div dates from .dividends are
        not actionable as forward catalysts and are skipped.
        """
        if db is None:
            raise ValueError("db is required")

        if ticker_factory is None:
            import yfinance
            ticker_factory = yfinance.Ticker

        from src.common.schemas import CatalystRow

        try:
            tkr = ticker_factory(ticker)
            cal = tkr.calendar
        except Exception:  # noqa: BLE001
            return 0

        # `cal` may be a dict, a pandas DataFrame, or None. Don't use
        # truthiness directly (DataFrame truthiness raises ValueError).
        if cal is None:
            return 0
        if isinstance(cal, dict) and not cal:
            return 0
        if isinstance(cal, pd.DataFrame) and cal.empty:
            return 0

        ex_div_iso = None
        if isinstance(cal, dict):
            raw = cal.get("Ex-Dividend Date") or cal.get("Ex Dividend Date")
            ex_div_iso = self._iso_date(raw)
        elif isinstance(cal, pd.DataFrame):
            for col in cal.columns:
                if "Ex" in str(col) and "Dividend" in str(col):
                    val = cal[col].iloc[0] if len(cal) > 0 else None
                    ex_div_iso = self._iso_date(val)
                    break
        if ex_div_iso is None:
            return 0

        scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        row = CatalystRow(
            run_id=run_id,
            ticker=ticker,
            catalyst_type="ex_dividend",
            catalyst_date=ex_div_iso,
            catalyst_description=f"Ex-dividend date ({ticker})",
            source="yahoo",
            source_url=f"https://finance.yahoo.com/quote/{ticker}/history",
            confidence="high",
            scrape_timestamp=scrape_ts,
        )
        db.insert_raw_catalysts([row])
        db.insert_catalyst_calendar([row])
        return 1

    def fetch_catalysts_for_ticker(
        self,
        ticker: str,
        run_id: str,
        db=None,
        *,
        include_earnings: bool = True,
        include_ex_dividend: bool = True,
        ticker_factory=None,
    ) -> tuple[int, int]:
        """Convenience: pulls both available catalyst types in one call."""
        n_earnings = 0
        n_ex_div = 0
        if include_earnings:
            n_earnings = self.fetch_earnings_dates(
                ticker, run_id, db, ticker_factory=ticker_factory,
            )
        if include_ex_dividend:
            n_ex_div = self.fetch_ex_dividend_dates(
                ticker, run_id, db, ticker_factory=ticker_factory,
            )
        return (n_earnings, n_ex_div)
