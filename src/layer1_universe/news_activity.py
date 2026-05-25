"""news_activity_score -- the 7-signal sentiment composite per spec section 7.

Combines the per-ticker scalars from seven independent academic signals
into a single weighted sum:

  | # | Signal                  | Module                              | Weight |
  |---|-------------------------|-------------------------------------|--------|
  | 1 | Opportunistic insider   | opportunistic_insider               | 0.30   |
  | 2 | Short-interest delta    | short_interest_delta                | 0.15   |
  | 3 | Revisions velocity      | revisions_velocity                  | 0.15   |
  | 4 | News volume anomaly     | news_volume_anomaly                 | 0.10   |
  | 5 | Filing density          | filing_density                      | 0.10   |
  | 6 | LM tone shift           | lm_tone_signal.compute_lm_tone_shift| 0.15   |
  | 7 | FEARS (Da-Engelberg-Gao)| fears_signal                        | 0.05   |

Sign convention: every sub-signal returns a signed scalar where
*positive == bullish*. FEARS internally sign-flips inside
`fears_signal.compute_fears_signal` per Da-Engelberg-Gao 2015. The
opportunistic-insider module returns booleans rather than a score; we
aggregate those into a per-ticker net-buying scalar inside this
module.

Missing-data renormalization: when a ticker has fewer than the full
seven signals available, the surviving weights are rescaled to sum to
1.0. `news_activity_score = NaN` only if ALL seven signals are missing.

This module is the *aggregator*: it takes already-computed per-ticker
sub-signal values and produces the composite. The thin shape-adapter
`load_news_activity_inputs(db, as_of_date)` lives below.
"""
from __future__ import annotations

import datetime as _dt
import math
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Optional


NEWS_ACTIVITY_VERSION = "1.0"

# Default sub-signal weights per spec section 7.3. Override via
# scoring.yaml's `news_activity_subweights` block.
DEFAULT_SUBWEIGHTS: dict[str, float] = {
    "opportunistic_insider": 0.30,
    "short_interest_delta": 0.15,
    "revisions_velocity": 0.15,
    "news_volume_anomaly": 0.10,
    "filing_density": 0.10,
    "lm_tone": 0.15,
    "fears": 0.05,
}


def _parse_iso_date(s: str) -> _dt.date:
    s = str(s)
    if "T" in s:
        s = s.split("T", 1)[0]
    return _dt.date.fromisoformat(s)


def opportunistic_insider_score(
    insider_rows: Iterable[Mapping[str, Any]],
    *,
    as_of_date: str,
    lookback_days: int = 30,
) -> dict[str, float]:
    """Per-ticker opportunistic-insider score.

    Among insider transactions classified as opportunistic
    (`is_opportunistic == True`) in the last `lookback_days`, compute
    `(n_buys - n_sells) / max(n_buys + n_sells, 1)`. Positive == net
    opportunistic buying (bullish), negative == net selling.

    Tickers with zero opportunistic transactions in the window get 0.0
    (neutral), NOT NaN -- absence of opportunistic activity is
    information (Cohen-Malloy-Pomorski 2012 §IV).

    Tickers entirely absent from `insider_rows` (no Form 4 data) do
    NOT appear in the output -- composite treats them as "signal
    missing" and renormalises.
    """
    cutoff = _parse_iso_date(as_of_date)
    earliest = cutoff - _dt.timedelta(days=lookback_days)

    by_ticker: dict[str, Counter] = defaultdict(Counter)
    seen_tickers: set[str] = set()

    for r in insider_rows:
        ticker = r.get("ticker")
        if not ticker:
            continue
        seen_tickers.add(ticker)
        try:
            d = _parse_iso_date(r["transaction_date"])
        except (KeyError, TypeError, ValueError):
            continue
        if d < earliest or d > cutoff:
            continue
        if not r.get("is_opportunistic"):
            continue
        code = (r.get("transaction_code") or "").strip().upper()
        if code == "P":
            by_ticker[ticker]["buy"] += 1
        elif code == "S":
            by_ticker[ticker]["sell"] += 1

    out: dict[str, float] = {}
    for ticker in seen_tickers:
        counts = by_ticker.get(ticker, Counter())
        total = counts["buy"] + counts["sell"]
        if total == 0:
            out[ticker] = 0.0
        else:
            out[ticker] = (counts["buy"] - counts["sell"]) / total
    return out


def _renormalize_and_sum(
    per_signal: dict[str, float],
    subweights: Mapping[str, float],
) -> float:
    """Weighted sum with NaN-renormalization.

    Drop NaN entries from per_signal; rescale surviving weights to sum
    to 1; compute the weighted sum. Returns NaN if no signals survive
    or no weight is positive.
    """
    surviving: list[tuple[float, float]] = []
    for name, value in per_signal.items():
        if not math.isfinite(value):
            continue
        w = float(subweights.get(name, 0.0))
        if w <= 0.0:
            continue
        surviving.append((value, w))
    if not surviving:
        return float("nan")
    total_w = sum(w for _, w in surviving)
    if total_w <= 0.0:
        return float("nan")
    return sum(v * (w / total_w) for v, w in surviving)


def compute_news_activity_score(
    *,
    tickers: Iterable[str],
    as_of_date: str,
    insider_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    short_interest_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    revisions_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    news_volume_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    filing_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    tone_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    fears_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    subweights: Optional[Mapping[str, float]] = None,
) -> dict[str, float]:
    """Compute the 7-signal news_activity_score per ticker.

    Per spec section 7.4: when a sub-signal is unavailable for a given
    ticker, its weight is redistributed proportionally to the surviving
    signals. `news_activity_score` is NaN only when ALL seven signals
    are missing.

    FEARS is a universe-wide scalar (Da-Engelberg-Gao); the same value
    applies to every ticker.
    """
    weights = dict(subweights) if subweights is not None else dict(DEFAULT_SUBWEIGHTS)
    tickers_list = list(tickers)

    from src.methodology.short_interest_delta import compute_short_interest_delta
    from src.methodology.revisions_velocity import compute_revisions_velocity
    from src.methodology.news_volume_anomaly import compute_news_volume_anomaly
    from src.methodology.filing_density import compute_filing_density
    from src.methodology.lm_tone_signal import compute_lm_tone_shift
    from src.methodology.fears_signal import compute_fears_signal

    insider_per_ticker = (
        opportunistic_insider_score(insider_rows, as_of_date=as_of_date)
        if insider_rows is not None else {}
    )
    short_per_ticker = (
        compute_short_interest_delta(list(short_interest_rows), as_of_date=as_of_date)
        if short_interest_rows is not None else {}
    )
    revisions_per_ticker = (
        compute_revisions_velocity(list(revisions_rows), as_of_date=as_of_date)
        if revisions_rows is not None else {}
    )
    news_per_ticker = (
        compute_news_volume_anomaly(list(news_volume_rows), as_of_date=as_of_date)
        if news_volume_rows is not None else {}
    )
    filings_per_ticker = (
        compute_filing_density(list(filing_rows), as_of_date=as_of_date)
        if filing_rows is not None else {}
    )
    # LM tone-shift: takes per-filing rows with net_tone, returns
    # per-ticker latest-vs-baseline z-score.
    tone_per_ticker = (
        compute_lm_tone_shift(list(tone_rows), as_of_date=as_of_date)
        if tone_rows is not None else {}
    )
    fears_value: Optional[float]
    if fears_rows is None:
        fears_value = None
    else:
        try:
            fears_value = compute_fears_signal(list(fears_rows), as_of_date=as_of_date)
        except Exception:
            fears_value = None
        if fears_value is not None and not math.isfinite(fears_value):
            fears_value = None

    out: dict[str, float] = {}
    for ticker in tickers_list:
        per_signal: dict[str, float] = {}
        if ticker in insider_per_ticker and math.isfinite(insider_per_ticker[ticker]):
            per_signal["opportunistic_insider"] = insider_per_ticker[ticker]
        if ticker in short_per_ticker and math.isfinite(short_per_ticker[ticker]):
            per_signal["short_interest_delta"] = short_per_ticker[ticker]
        if ticker in revisions_per_ticker and math.isfinite(revisions_per_ticker[ticker]):
            per_signal["revisions_velocity"] = revisions_per_ticker[ticker]
        if ticker in news_per_ticker and math.isfinite(news_per_ticker[ticker]):
            per_signal["news_volume_anomaly"] = news_per_ticker[ticker]
        if ticker in filings_per_ticker and math.isfinite(filings_per_ticker[ticker]):
            per_signal["filing_density"] = filings_per_ticker[ticker]
        if ticker in tone_per_ticker and math.isfinite(tone_per_ticker[ticker]):
            per_signal["lm_tone"] = tone_per_ticker[ticker]
        if fears_value is not None:
            per_signal["fears"] = fears_value
        out[ticker] = _renormalize_and_sum(per_signal, weights)
    return out


# ---------------------------------------------------------------------------
# DB shape-adapter
# ---------------------------------------------------------------------------


def _row_factory(cursor, row):
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


def load_news_activity_inputs(
    db,
    *,
    as_of_date: str,
    tickers: Optional[Iterable[str]] = None,
) -> dict[str, list[dict]]:
    """Pull the seven sub-signal row iterables from `db`.

    Returns a dict whose keys match `compute_news_activity_score`'s
    keyword arguments. Each value is a list of plain dicts shaped per
    the corresponding methodology module's input contract.

    Filters by `tickers` when provided. FEARS is universe-wide, so the
    filter doesn't apply to it.

    For source tables that don't exist on the current DB version
    (defensive), the corresponding key is an empty list -- callers see
    "no signal" and renormalise.
    """
    def _safe_query(sql: str, params: tuple = ()) -> list[dict]:
        try:
            with db.get_connection() as conn:
                conn.row_factory = _row_factory
                cur = conn.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
            return rows
        except Exception:
            return []

    ticker_filter = ""
    ticker_params: tuple = ()
    if tickers is not None:
        tks = tuple(t for t in tickers)
        if tks:
            placeholders = ",".join("?" * len(tks))
            ticker_filter = f" AND ticker IN ({placeholders})"
            ticker_params = tks

    insider = _safe_query(
        "SELECT ticker, transaction_date, transaction_code, is_opportunistic "
        "FROM raw_edgar_insider WHERE 1=1" + ticker_filter,
        ticker_params,
    )
    short_interest = _safe_query(
        "SELECT ticker, observation_date, short_interest_pct_float, settlement_date "
        "FROM raw_finra WHERE 1=1" + ticker_filter,
        ticker_params,
    )
    revisions = _safe_query(
        "SELECT ticker, field_name, value_numeric, observation_date "
        "FROM raw_openbb WHERE field_name IN ('eps_estimate_current','eps_estimate_30d_ago',"
        "'eps_revision_direction_count')" + ticker_filter,
        ticker_params,
    )
    news_volume = _safe_query(
        "SELECT ticker, mention_timestamp FROM raw_gdelt WHERE 1=1" + ticker_filter,
        ticker_params,
    )
    filings = _safe_query(
        "SELECT ticker, form_type, filing_date, item_codes FROM raw_edgar_filings "
        "WHERE 1=1" + ticker_filter,
        ticker_params,
    )
    tone = _safe_query(
        "SELECT ticker, filing_date, net_tone FROM raw_edgar_filing_tone "
        "WHERE 1=1" + ticker_filter,
        ticker_params,
    )
    fears = _safe_query(
        "SELECT term, observation_date, svi FROM raw_pytrends",
    )

    return {
        "insider_rows": insider,
        "short_interest_rows": short_interest,
        "revisions_rows": revisions,
        "news_volume_rows": news_volume,
        "filing_rows": filings,
        "tone_rows": tone,
        "fears_rows": fears,
    }
