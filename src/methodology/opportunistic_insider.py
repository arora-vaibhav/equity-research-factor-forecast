"""Cohen-Malloy-Pomorski (2012) opportunistic-insider classifier.

Reference: Cohen, L., Malloy, C., Pomorski, L. (2012). "Decoding Inside
Information." Journal of Finance, 67(3), 1009-1043.

Operational rule (per spec §6.3.2):
  A transaction T by filer F is classified as ROUTINE if, in the
  3 calendar years strictly prior to T's transaction_date, filer F
  has at least 2 transactions in the same calendar month as T's
  transaction_date. Otherwise OPPORTUNISTIC.

The CMP paper's original definition required same-month trades for
3 consecutive prior years. The >=2-in-prior-3y operational variant
implemented here is the version used by subsequent literature
(Jagolinzer et al. 2020 and the spec § 6.3.2 call this version out).

Pure function — no I/O, no DB, deterministic. Versioned via
OPPORTUNISTIC_CLASSIFIER_VERSION so persisted classifier outputs are
auditable and recalibratable.

`filer_id` is the unit of identity. The caller is responsible for
constructing it consistently (typically "{filer_name}:{cik}" so the
same physical insider trading the same issuer is one filer).
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict


OPPORTUNISTIC_CLASSIFIER_VERSION = "1.0"

# CMP rule threshold: filer must have >= this many same-month transactions
# in the prior-3-year window for the focal trade to be classified routine.
_ROUTINE_THRESHOLD = 2

# Lookback window (years). Strict: a prior trade T' counts only if
# T'.date >= focal_date - 3 years AND T'.date < focal_date.
_LOOKBACK_YEARS = 3


def _parse_date(s: str) -> _dt.date:
    """Parse 'YYYY-MM-DD'. Raises ValueError on malformed input."""
    return _dt.date.fromisoformat(s)


def _years_before(d: _dt.date, years: int) -> _dt.date:
    """Return the FIRST DAY of the calendar month that's `years` years before d.

    CMP literature operates on calendar-month patterns over a 3-year window:
    a transaction in March 2023 is "in the prior 3 years" of a March 2026
    focal date. We anchor the cutoff at month-start so any day in the
    target month counts as inside the window.

    Example: focal = 2026-03-15, years = 3 -> cutoff = 2023-03-01
             So 2023-03-10 (same calendar month, ~3y prior) IS in window.

    For Feb-29 focal in non-leap result years, the month rolls back normally
    because day=1 is always valid.
    """
    return d.replace(year=d.year - years, day=1)


def classify_transactions(txns: list[dict]) -> list[bool]:
    """Classify each transaction as opportunistic (True) or routine (False).

    Parameters
    ----------
    txns
        A list of dicts, each with at minimum:
          - 'filer_id': str
          - 'transaction_date': str ('YYYY-MM-DD')
          - 'transaction_code': str (accepted but unused by the classifier)

    Returns
    -------
    A list of booleans in the SAME ORDER as the input. True == opportunistic.

    Notes
    -----
    Order-invariant on input. A transaction does NOT count itself as a
    prior; the focal trade is strictly excluded from its own check.
    """
    if not txns:
        return []

    per_filer_dates: dict[str, list[_dt.date]] = defaultdict(list)
    parsed_dates: list[_dt.date] = []
    for t in txns:
        d = _parse_date(t["transaction_date"])
        parsed_dates.append(d)
        per_filer_dates[t["filer_id"]].append(d)

    for fid in per_filer_dates:
        per_filer_dates[fid].sort()

    out: list[bool] = []
    for i, t in enumerate(txns):
        fid = t["filer_id"]
        focal_date = parsed_dates[i]
        focal_month = focal_date.month
        cutoff = _years_before(focal_date, _LOOKBACK_YEARS)

        same_month_count = 0
        for d in per_filer_dates[fid]:
            if d >= focal_date:
                break
            if d < cutoff:
                continue
            if d.month == focal_month:
                same_month_count += 1
                if same_month_count >= _ROUTINE_THRESHOLD:
                    break

        is_routine = same_month_count >= _ROUTINE_THRESHOLD
        out.append(not is_routine)

    return out
