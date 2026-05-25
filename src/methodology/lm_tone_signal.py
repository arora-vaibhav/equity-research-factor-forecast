"""Loughran-McDonald textual tone signal -- spec section 7.2 signal 6.

Reference:
  Loughran, T. & McDonald, B. (2011). "When Is a Liability Not a
  Liability? Textual Analysis, Dictionaries, and 10-Ks." Journal of
  Finance 66(1).
  Garcia, D. & Norli, O. (2018). "Local Information in Stock Markets."
  Review of Financial Studies 31(7) -- tone-shift z-score against own-
  firm baseline.

Operational rule per spec section 7.2 signal 6:

  net_tone = (n_positive - n_negative) / total_words
  score    = (net_tone_current - mu_prior4) / sigma_prior4

  where the prior-4 baseline is the four chronologically previous
  filings of the *same ticker*. NaN if fewer than 4 prior baselines.

Sign convention: POSITIVE z == net tone rising versus the firm's own
recent baseline == bullish, consistent with the rest of the
news_activity_score composite (spec section 7.3). When a filing
introduces more LM-negative words than prior filings, net_tone falls
(smaller (pos-neg) numerator) and the z-score goes negative -- bearish.

Pure function. No I/O, no DB, deterministic. The caller does HTML
extraction + dictionary load + DB hand-off; this module just runs the
math.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any, Iterable, Mapping


LM_TONE_SIGNAL_VERSION = "1.0"

# Backwards-compat alias for the original A.3.9 ship-name. A.3.9
# Reconciliation R1 (2026-05-23) renamed the canonical export to match
# the strict-prompt spec; this alias prevents any older callers from
# breaking until they migrate. Remove after Phase A closes.
LM_TONE_VERSION = LM_TONE_SIGNAL_VERSION

# Word tokenizer. The LM dictionary uses uppercase plain-ASCII words
# (LOSS, BANKRUPT, LITIGATION, ...). Lowercase the text first, split on
# any non-letter/apostrophe boundary, then re-uppercase before
# dictionary lookup. Apostrophes are kept inside tokens (don't, didn't)
# so contractions survive.
_TOKEN_RE = re.compile(r"[a-z][a-z']*[a-z]|[a-z]")


def tokenize(text: str) -> list[str]:
    """Split `text` into uppercase ASCII tokens for LM lookup.

    Lowercases, splits on non-letter boundaries (keeping intra-word
    apostrophes), drops empty tokens, then uppercases the result so it
    matches the dictionary's keying convention.

    Parameters
    ----------
    text
        Arbitrary string. Non-ASCII characters are silently dropped by
        the regex.

    Returns
    -------
    list[str]
        Token list in document order. Includes duplicates -- counts are
        what `score_tone` operates on.
    """
    if not text:
        return []
    lowered = text.lower()
    return [t.upper() for t in _TOKEN_RE.findall(lowered)]


def score_tone(
    text: str,
    dictionary: Mapping[str, frozenset[str]],
) -> dict[str, Any]:
    """Count LM tags in `text` and return tone counters + net_tone.

    Parameters
    ----------
    text
        Document body. Pass already-extracted Item 1A text, not raw HTML.
    dictionary
        Output of `load_lm_dictionary` -- mapping from uppercase word to
        the frozenset of tag names. Words not in the dictionary
        contribute to `total_words` but to no tag count.

    Returns
    -------
    dict
        With keys n_positive, n_negative, n_uncertainty, n_litigious,
        total_words (all int), and net_tone (float).
        net_tone = (n_positive - n_negative) / max(total_words, 1).
        When total_words == 0, net_tone is 0.0 (matches the
        "empty_document" downstream extraction_status).
    """
    tokens = tokenize(text)
    total = len(tokens)
    n_pos = n_neg = n_unc = n_lit = 0
    for tok in tokens:
        tags = dictionary.get(tok)
        if tags is None:
            continue
        if "positive" in tags:
            n_pos += 1
        if "negative" in tags:
            n_neg += 1
        if "uncertainty" in tags:
            n_unc += 1
        if "litigious" in tags:
            n_lit += 1
    if total == 0:
        net = 0.0
    else:
        net = (n_pos - n_neg) / total
    return {
        "n_positive": n_pos,
        "n_negative": n_neg,
        "n_uncertainty": n_unc,
        "n_litigious": n_lit,
        "total_words": total,
        "net_tone": net,
    }


def _parse_iso_date(s: str):
    import datetime as _dt
    s = str(s)
    if "T" in s:
        s = s.split("T", 1)[0]
    return _dt.date.fromisoformat(s)


def compute_lm_tone_shift(
    filing_rows: Iterable[Mapping[str, Any]],
    *,
    as_of_date: str,
    baseline_n: int = 4,
) -> dict[str, float]:
    """Per-ticker z-score of latest net_tone against prior `baseline_n` filings.

    Parameters
    ----------
    filing_rows
        Iterable of per-filing rows. Each row is a mapping with at
        minimum:
          - 'ticker' : str
          - 'filing_date' : str ('YYYY-MM-DD')
          - 'net_tone' : float
        Rows with missing fields, unparseable dates, or non-finite
        net_tone values are skipped.
    as_of_date
        Focal date 'YYYY-MM-DD'. Only filings dated <= as_of_date are
        considered. The most recent qualifying filing per ticker is the
        "current" observation; prior `baseline_n` filings form the
        baseline.
    baseline_n
        Number of prior filings required for the z-score baseline.
        Default 4 (Loughran-McDonald 2014 / Garcia-Norli 2018 convention).
        Tickers with fewer than `baseline_n` prior filings receive NaN.

    Returns
    -------
    dict[str, float]
        Per-ticker z-score. NaN when the baseline is too thin or
        sigma == 0. Sign convention: positive z == net tone rising
        relative to the firm's own baseline == bullish.
    """
    if baseline_n < 1:
        raise ValueError("baseline_n must be >= 1")

    as_of = _parse_iso_date(as_of_date)

    # ticker -> list of (filing_date, net_tone) tuples
    per_ticker: dict[str, list[tuple[Any, float]]] = defaultdict(list)
    for r in filing_rows:
        try:
            ticker = r["ticker"]
            d = _parse_iso_date(r["filing_date"])
            nt = float(r["net_tone"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(nt):
            continue
        if d > as_of:
            continue
        per_ticker[ticker].append((d, nt))

    out: dict[str, float] = {}
    for ticker, items in per_ticker.items():
        items.sort(key=lambda t: t[0])
        if len(items) < baseline_n + 1:
            out[ticker] = float("nan")
            continue
        # The `baseline_n` filings immediately before the latest.
        baseline = items[-(baseline_n + 1):-1]
        latest_nt = items[-1][1]
        prior_vals = [nt for _, nt in baseline]
        mu = sum(prior_vals) / len(prior_vals)
        var = sum((x - mu) ** 2 for x in prior_vals) / len(prior_vals)
        sigma = math.sqrt(var)
        if sigma == 0.0:
            out[ticker] = 0.0 if latest_nt == mu else float("nan")
            continue
        out[ticker] = (latest_nt - mu) / sigma

    return out
