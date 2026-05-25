"""OpenBBSource — multi-provider router for FMP-stable + Polygon + Tiingo.

A.3.7 deliverable per spec section 6.7. Unlike the single-vendor adapters
(YahooSource, EdgarSource, ...), this source routes a single logical
`fetch_fundamentals_for_ticker(ticker, run_id)` call across THREE
independent providers and writes each provider's response as long-format
rows into `raw_openbb`. The PK
`(run_id, ticker, field_name, provider_used)` means the same field from
two providers writes two rows — that's the cross-vendor cross-validation
substrate the materialization layer (A.3.10) joins on.

Provider contracts (verified 2026-05-21)
----------------------------------------
* FMP issued post-2024 keys are STABLE-API ONLY. Legacy `/api/v3/`
  returns 403 "Legacy Endpoint". We target `/stable/{endpoint}?symbol=...`
* Polygon free tier = 5 req/min. We sleep 12s between Polygon calls.
* Tiingo free tier = 1000 req/day. 0.5s politeness delay.

Key-missing graceful degradation
--------------------------------
If `get_api_key(provider)` returns None, that route is silently skipped
(INFO log, not WARNING — missing key is a configuration choice, not an
error). The fetch returns "success" if at least one route wrote at least
one row. Only when ALL three routes are skipped (zero keys provisioned)
does the watermark record an error.

24h refresh-skip semantics
--------------------------
Per spec section 6.7 "Watermark per (openbb, ticker, field)". v1 uses a
single synthetic field name `multi_provider` so the entire router's
state is gated by one watermark. If the previous successful pull is
within 24h, the fetch returns 0 immediately. Caller can force a refresh
by clearing the watermark or passing `refresh_after_hours=0`.

Long-format row emission
------------------------
Each provider's response is normalized to a small canonical-field set
(see `_OpenBBFieldName` Literal in schemas.py and the docstring of each
`_map_*` function). String-valued fields (sector, industry, currency,
primary_exchange) are emitted with `value=None` and the string value
encoded in `unit` (e.g., `unit='sector:Technology'`).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)
from src.common.env_loader import get_api_key
from src.common.schemas import RawOpenBBRow


_log = logging.getLogger(__name__)


# --- URL templates ----------------------------------------------------

_FMP_PROBE = (
    "https://financialmodelingprep.com/stable/profile"
    "?symbol=AAPL&apikey={key}"
)
_FMP_PROFILE = (
    "https://financialmodelingprep.com/stable/profile"
    "?symbol={ticker}&apikey={key}"
)
_FMP_QUOTE = (
    "https://financialmodelingprep.com/stable/quote"
    "?symbol={ticker}&apikey={key}"
)
_FMP_RATIOS_TTM = (
    "https://financialmodelingprep.com/stable/ratios-ttm"
    "?symbol={ticker}&apikey={key}"
)
_FMP_KEY_METRICS_TTM = (
    "https://financialmodelingprep.com/stable/key-metrics-ttm"
    "?symbol={ticker}&apikey={key}"
)

_POLYGON_REF = (
    "https://api.polygon.io/v3/reference/tickers/{ticker}?apiKey={key}"
)
_POLYGON_PREV = (
    "https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?apiKey={key}"
)

_TIINGO_DAILY = "https://api.tiingo.com/tiingo/daily/{ticker_lower}?token={key}"
_TIINGO_IEX = "https://api.tiingo.com/iex/{ticker_lower}?token={key}"

# A.3.8: FMP analyst-estimates endpoint (FMP-only; Polygon + Tiingo free
# tiers do not expose analyst estimates with sufficient history).
_FMP_ANALYST_ESTIMATES = (
    "https://financialmodelingprep.com/stable/analyst-estimates"
    "?symbol={ticker}&apikey={key}"
)


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Trade Identifier; research@example.com)"
    ),
}

# Per-provider polite delays (seconds). Polygon free tier is 5/min so we
# sleep 12s between Polygon calls to stay under the ceiling. FMP + Tiingo
# are fast and we only enforce a token politeness delay.
_POLYGON_DELAY_SEC = 12.0
_FMP_DELAY_SEC = 0.5
_TIINGO_DELAY_SEC = 0.5

# Per spec section 6.7: 24h refresh-skip window. Multi-provider data
# changes intraday (last_price), so 24h is the natural cadence — same as
# a typical Layer-1 daily refresh cycle.
_REFRESH_SKIP_HOURS = 24

# Per-provider HTTP timeout (seconds).
_HTTP_TIMEOUT_SEC = 15


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(v: Any) -> Optional[float]:
    """Coerce a JSON value to float, returning None for null / unparseable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Per-provider mappers — pure functions, no I/O.
# Each takes the parsed JSON for ONE provider's bundle of endpoints
# and returns a list of (field_name, value, unit) tuples. The caller
# wraps each tuple into a RawOpenBBRow with the standard run_id /
# ticker / provider_used / scrape_timestamp.
# ----------------------------------------------------------------------


def _map_fmp(
    *,
    profile: Optional[list],
    quote: Optional[list],
    ratios_ttm: Optional[list],
    key_metrics_ttm: Optional[list],
) -> list[tuple[str, Optional[float], str]]:
    """Map FMP-stable JSON responses to canonical (field, value, unit) tuples."""
    out: list[tuple[str, Optional[float], str]] = []

    if profile and isinstance(profile, list) and profile:
        p = profile[0]
        out.append(("beta", _safe_float(p.get("beta")), "ratio"))
        # String fields encoded via unit prefix.
        if p.get("sector"):
            out.append(("sector", None, f"sector:{p['sector']}"))
        if p.get("industry"):
            out.append(("industry", None, f"industry:{p['industry']}"))
        if p.get("currency"):
            out.append(("currency", None, f"currency:{p['currency']}"))
        if p.get("exchangeShortName"):
            out.append((
                "primary_exchange", None,
                f"primary_exchange:{p['exchangeShortName']}",
            ))

    if quote and isinstance(quote, list) and quote:
        q = quote[0]
        out.append(("last_price", _safe_float(q.get("price")), "usd"))
        out.append(("prev_close", _safe_float(q.get("previousClose")), "usd"))
        out.append(("volume", _safe_float(q.get("volume")), "shares"))
        # Prefer key-metrics-ttm.marketCapTTM below; fall back to quote.marketCap.
        if q.get("marketCap") is not None:
            out.append(("market_cap", _safe_float(q.get("marketCap")), "usd"))

    if ratios_ttm and isinstance(ratios_ttm, list) and ratios_ttm:
        r = ratios_ttm[0]
        out.append(("pe_ratio", _safe_float(r.get("peRatioTTM")), "ratio"))
        out.append(("ps_ratio", _safe_float(r.get("priceToSalesRatioTTM")), "ratio"))
        out.append(("pb_ratio", _safe_float(r.get("priceToBookRatioTTM")), "ratio"))
        out.append((
            "dividend_yield",
            _safe_float(r.get("dividendYieldTTM")),
            "ratio",
        ))

    if key_metrics_ttm and isinstance(key_metrics_ttm, list) and key_metrics_ttm:
        k = key_metrics_ttm[0]
        out.append((
            "ev_to_ebitda",
            _safe_float(k.get("enterpriseValueOverEBITDATTM")),
            "ratio",
        ))
        # Prefer key-metrics-ttm.marketCapTTM if quote didn't have it.
        if k.get("marketCapTTM") is not None and not any(
            t[0] == "market_cap" for t in out
        ):
            out.append(("market_cap", _safe_float(k.get("marketCapTTM")), "usd"))

    return out


def _map_polygon(
    *,
    ref: Optional[dict],
    prev: Optional[dict],
) -> list[tuple[str, Optional[float], str]]:
    """Map Polygon JSON responses to canonical (field, value, unit) tuples."""
    out: list[tuple[str, Optional[float], str]] = []

    if ref and isinstance(ref, dict):
        results = ref.get("results")
        if isinstance(results, dict):
            if results.get("market_cap") is not None:
                out.append((
                    "market_cap",
                    _safe_float(results.get("market_cap")),
                    "usd",
                ))
            if results.get("share_class_shares_outstanding") is not None:
                out.append((
                    "shares_outstanding",
                    _safe_float(results.get("share_class_shares_outstanding")),
                    "shares",
                ))
            if results.get("primary_exchange"):
                out.append((
                    "primary_exchange", None,
                    f"primary_exchange:{results['primary_exchange']}",
                ))
            if results.get("currency_name"):
                out.append((
                    "currency", None,
                    f"currency:{results['currency_name']}",
                ))

    if prev and isinstance(prev, dict):
        results = prev.get("results")
        if isinstance(results, list) and results:
            r0 = results[0]
            out.append(("last_price", _safe_float(r0.get("c")), "usd"))
            # Polygon's `/prev` is by definition the previous day's close,
            # so `c` doubles as prev_close in this context.
            out.append(("prev_close", _safe_float(r0.get("c")), "usd"))
            out.append(("volume", _safe_float(r0.get("v")), "shares"))

    return out


def _map_tiingo(
    *,
    daily: Optional[dict],
    iex: Optional[list],
) -> list[tuple[str, Optional[float], str]]:
    """Map Tiingo JSON responses to canonical (field, value, unit) tuples."""
    out: list[tuple[str, Optional[float], str]] = []

    if daily and isinstance(daily, dict):
        if daily.get("exchangeCode"):
            out.append((
                "primary_exchange", None,
                f"primary_exchange:{daily['exchangeCode']}",
            ))

    if iex and isinstance(iex, list) and iex:
        i = iex[0]
        # Prefer `last` (real-time) over `tngoLast` (Tiingo's delayed feed).
        last = i.get("last") if i.get("last") is not None else i.get("tngoLast")
        out.append(("last_price", _safe_float(last), "usd"))
        out.append(("prev_close", _safe_float(i.get("prevClose")), "usd"))
        out.append(("volume", _safe_float(i.get("volume")), "shares"))

    return out


def _map_fmp_analyst(
    estimates: Optional[list],
) -> list[tuple[str, Optional[float], str]]:
    """Map FMP /stable/analyst-estimates response to canonical tuples.

    A.3.8: supplies the three field_names consumed by revisions_velocity.py.

    FMP returns a list of estimate objects sorted newest-first.  We take:
      - row[0] → eps_estimate_current    (most recent consensus EPS estimate)
      - row[1] → eps_estimate_30d_ago    (prior-quarter estimate; serves as
                                          a period-lagged proxy for ~30-day
                                          change in analyst sentiment)
      - row[0] → eps_revision_direction_count  (net up-minus-down revisions
                                                for the most recent period)

    Polygon and Tiingo free tiers lack analyst-estimates data; those routes
    silently skip these three fields.
    """
    out: list[tuple[str, Optional[float], str]] = []
    if not estimates or not isinstance(estimates, list):
        return out

    # First (most recent) estimate entry.
    e0 = estimates[0]
    out.append((
        "eps_estimate_current",
        _safe_float(e0.get("estimatedEpsAvg")),
        "usd",
    ))

    up = _safe_float(e0.get("numAnalystsRevisionUp"))
    dn = _safe_float(e0.get("numAnalystsRevisionDown"))
    direction = (up - dn) if (up is not None and dn is not None) else None
    out.append(("eps_revision_direction_count", direction, "count"))

    # Second entry (prior quarter) as the 30d-ago EPS proxy.
    if len(estimates) >= 2:
        out.append((
            "eps_estimate_30d_ago",
            _safe_float(estimates[1].get("estimatedEpsAvg")),
            "usd",
        ))

    return out


# ----------------------------------------------------------------------
# The source.
# ----------------------------------------------------------------------


class OpenBBSource(BaseDataSource):
    name = "openbb"
    cadence = "daily"
    provides = {
        "pe_ratio", "ps_ratio", "pb_ratio",
        "dividend_yield", "ev_to_ebitda",
        "market_cap", "last_price", "prev_close",
        "volume", "shares_outstanding",
        "primary_exchange", "sector", "industry",
        "beta", "currency",
        # A.3.8 analyst-estimates (FMP-only; Polygon + Tiingo skip silently)
        "eps_estimate_current", "eps_estimate_30d_ago",
        "eps_revision_direction_count",
    }

    # ------------------------------------------------------------------
    # health_check - cheap probe of FMP /stable/profile for AAPL (the
    # one route that requires no live universe context).
    # ------------------------------------------------------------------

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = _now_iso()
        fmp_key = get_api_key("fmp")
        if not fmp_key:
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.PARTIAL,
                checked_at=now,
                message="FMP key absent; cannot probe",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        url = _FMP_PROBE.format(key=fmp_key)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=10)
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        if not resp.ok:
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"HTTP {resp.status_code}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK,
            checked_at=now,
            message=f"HTTP 200 ({len(resp.content)} bytes)",
            response_time_ms=(time.monotonic() - started) * 1000,
        )

    # ------------------------------------------------------------------
    # A.3.7: multi-provider fundamentals fetch
    # ------------------------------------------------------------------

    def _http_get_json(self, url: str) -> Optional[Any]:
        """GET one URL and parse JSON. Returns parsed JSON on 200, or
        None on any failure (HTTP error, network error, JSON decode
        error). Caller treats None as a soft per-call failure."""
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001
            _log.warning("openbb http error url=%s: %s", url, exc)
            return None
        if not resp.ok:
            _log.warning(
                "openbb http non-200 url=%s status=%s",
                url, resp.status_code,
            )
            return None
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            _log.warning("openbb json decode error url=%s: %s", url, exc)
            return None

    def _route_fmp(
        self, ticker: str,
    ) -> list[tuple[str, Optional[float], str]]:
        """Pull all FMP endpoints (fundamentals + analyst estimates) and
        map -> canonical tuples.  Returns [] if FMP key is missing."""
        key = get_api_key("fmp")
        if not key:
            _log.info("openbb route fmp skipped (no key)")
            return []

        # Politeness inter-call delay before each FMP request.
        time.sleep(_FMP_DELAY_SEC)
        profile = self._http_get_json(_FMP_PROFILE.format(ticker=ticker, key=key))
        time.sleep(_FMP_DELAY_SEC)
        quote = self._http_get_json(_FMP_QUOTE.format(ticker=ticker, key=key))
        time.sleep(_FMP_DELAY_SEC)
        ratios_ttm = self._http_get_json(_FMP_RATIOS_TTM.format(ticker=ticker, key=key))
        time.sleep(_FMP_DELAY_SEC)
        key_metrics_ttm = self._http_get_json(
            _FMP_KEY_METRICS_TTM.format(ticker=ticker, key=key)
        )
        # A.3.8: analyst estimates (supplies eps_estimate_current,
        # eps_estimate_30d_ago, eps_revision_direction_count).
        time.sleep(_FMP_DELAY_SEC)
        estimates = self._http_get_json(
            _FMP_ANALYST_ESTIMATES.format(ticker=ticker, key=key)
        )
        return (
            _map_fmp(
                profile=profile, quote=quote,
                ratios_ttm=ratios_ttm, key_metrics_ttm=key_metrics_ttm,
            )
            + _map_fmp_analyst(estimates)
        )

    def _route_polygon(
        self, ticker: str,
    ) -> list[tuple[str, Optional[float], str]]:
        """Pull Polygon reference + prev-day aggregate. Returns [] if key
        is missing. Sleeps `_POLYGON_DELAY_SEC` between the two Polygon
        calls to respect free-tier 5/min ceiling."""
        key = get_api_key("polygon")
        if not key:
            _log.info("openbb route polygon skipped (no key)")
            return []

        ref = self._http_get_json(_POLYGON_REF.format(ticker=ticker, key=key))
        time.sleep(_POLYGON_DELAY_SEC)
        prev = self._http_get_json(_POLYGON_PREV.format(ticker=ticker, key=key))
        return _map_polygon(ref=ref, prev=prev)

    def _route_tiingo(
        self, ticker: str,
    ) -> list[tuple[str, Optional[float], str]]:
        """Pull Tiingo daily + iex. Returns [] if key is missing."""
        key = get_api_key("tiingo")
        if not key:
            _log.info("openbb route tiingo skipped (no key)")
            return []

        t = ticker.lower()
        time.sleep(_TIINGO_DELAY_SEC)
        daily = self._http_get_json(_TIINGO_DAILY.format(ticker_lower=t, key=key))
        time.sleep(_TIINGO_DELAY_SEC)
        iex = self._http_get_json(_TIINGO_IEX.format(ticker_lower=t, key=key))
        return _map_tiingo(daily=daily, iex=iex)

    def fetch_fundamentals_for_ticker(
        self,
        ticker: str,
        run_id: str,
        db=None,
        *,
        refresh_after_hours: int = _REFRESH_SKIP_HOURS,
    ) -> int:
        """Pull fundamentals across FMP + Polygon + Tiingo for one ticker.

        Pipeline:
          0. Check watermark (openbb, <ticker>, multi_provider). If the
             previous successful pull is within `refresh_after_hours`,
             return 0 immediately.
          1. For each provider in (fmp, polygon, tiingo):
               a. If key missing -> skip silently (INFO log).
               b. Else: pull provider's endpoints, map to canonical
                  (field, value, unit) tuples, build RawOpenBBRow per
                  tuple, INSERT OR IGNORE.
               c. Track per-provider success/error.
          2. Update watermark - success if at least one provider wrote
             at least one row; error otherwise (with message naming the
             failed providers).

        Returns
        -------
        int : net new rows inserted across all three providers.
        """
        if db is None:
            raise ValueError("db is required")

        field = "multi_provider"
        ticker = ticker.strip().upper()

        # 0. 24h refresh-skip. Gate on `last_observation_date` (the
        # business-day of the previous successful pull) rather than
        # `last_fetched_at` (wall-clock timestamp of the most recent
        # upsert): the latter is touched by every upsert including
        # error-only updates, which would prematurely block retries.
        w = db.get_watermark(self.name, ticker, field)
        if w and w.get("last_observation_date") and w.get("error_count", 0) == 0:
            try:
                last = _dt.date.fromisoformat(w["last_observation_date"])
                age_hours = (
                    _dt.datetime.utcnow() - _dt.datetime.combine(last, _dt.time.min)
                ).total_seconds() / 3600.0
                if age_hours < refresh_after_hours:
                    return 0
            except (TypeError, ValueError):
                pass

        scrape_ts = _now_iso()
        total_inserted = 0
        provider_errors: list[str] = []
        any_provider_succeeded = False
        import sqlite3

        provider_routes = [
            ("fmp", self._route_fmp),
            ("polygon", self._route_polygon),
            ("tiingo", self._route_tiingo),
        ]

        for provider_name, route_fn in provider_routes:
            # Probe key presence FIRST so we can distinguish "skipped
            # because no key" (no error) from "attempted but failed"
            # (recorded as error).
            key_present = bool(get_api_key(provider_name))
            if not key_present:
                provider_errors.append(f"no-key:{provider_name}")
                continue

            try:
                tuples = route_fn(ticker)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "openbb provider %s raised: %s", provider_name, exc,
                )
                provider_errors.append(f"exception:{provider_name}")
                continue

            if not tuples:
                provider_errors.append(f"empty:{provider_name}")
                continue

            rows: list[RawOpenBBRow] = []
            for field_name, value, unit in tuples:
                try:
                    rows.append(
                        RawOpenBBRow(
                            run_id=run_id,
                            ticker=ticker,
                            field_name=field_name,
                            provider_used=provider_name,
                            value=value,
                            unit=unit,
                            scrape_timestamp=scrape_ts,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "openbb row build failed provider=%s field=%s: %s",
                        provider_name, field_name, exc,
                    )
                    continue

            if not rows:
                provider_errors.append(f"no-rows:{provider_name}")
                continue

            # Count net new rows by before/after on (run_id, ticker, provider).
            with sqlite3.connect(db.db_path) as c:
                n_before = c.execute(
                    "SELECT COUNT(*) FROM raw_openbb "
                    "WHERE run_id=? AND ticker=? AND provider_used=?",
                    (run_id, ticker, provider_name),
                ).fetchone()[0]
            db.insert_raw_openbb(rows)
            with sqlite3.connect(db.db_path) as c:
                n_after = c.execute(
                    "SELECT COUNT(*) FROM raw_openbb "
                    "WHERE run_id=? AND ticker=? AND provider_used=?",
                    (run_id, ticker, provider_name),
                ).fetchone()[0]
            inserted = n_after - n_before
            total_inserted += inserted
            if inserted > 0:
                any_provider_succeeded = True

        # Watermark advance. Success requires AT LEAST ONE provider to
        # have written at least one row. If zero providers succeeded
        # (all keys missing or all routes failed) we record error.
        if any_provider_succeeded:
            self.update_watermark(
                ticker=ticker, field=field,
                last_observation_date=_dt.date.today(),
                db=db, success=True,
            )
        else:
            self.update_watermark(
                ticker=ticker, field=field,
                last_observation_date=_dt.date.today(),
                db=db, success=False,
                error_message="; ".join(provider_errors) or "no-providers",
            )

        return total_inserted
