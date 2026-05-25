"""news_sentiment_v2 -- free real-time headline tone for a single ticker.

Pulls yfinance ticker.news (Yahoo aggregates ~Reuters/Benzinga/MT
Newswires/Bloomberg ticker mentions etc), scores each headline with the
Loughran-McDonald finance lexicon already shipped at
config/lm_dictionary/LoughranMcDonald_MasterDictionary.csv, and returns
24h / 7d aggregates plus a headline-count anomaly z.

Output schema is a superset of the per-ticker row consumed by
src.layer1_universe.news_activity.compute_news_activity_score (keys:
ticker, filing_date, net_tone) so the result can be fed directly into
the existing lm_tone sub-signal as well as a new news_volume_anomaly
contribution. Falls back to a tiny positive/negative wordlist when the
LM CSV is unavailable (e.g. notebook context with no config dir).

Swap-to-bigdata.com path: when V activates the bigdata-com MCP plugin,
replace `_fetch_headlines` with a call to the bigdata-com:company-brief
or :catalyst-monitor skill via mcp__plugin_bigdata-com_bigdata_com__*
auth tokens (env: BIGDATA_API_KEY / BIGDATA_REFRESH). The downstream
schema below stays unchanged so the rest of the pipeline is unaware.
"""
from __future__ import annotations

import datetime as _dt
import math
from pathlib import Path
from typing import Any, Optional

NEWS_SENTIMENT_V2_VERSION = "1.0"

_FALLBACK_POS = frozenset({
    "beat", "beats", "surge", "surges", "upgrade", "upgraded", "strong",
    "record", "rally", "outperform", "boost", "boosted", "raise", "raised",
})
_FALLBACK_NEG = frozenset({
    "miss", "misses", "plunge", "plunges", "downgrade", "downgraded",
    "weak", "loss", "losses", "lawsuit", "probe", "cut", "fraud", "warn",
})


def _load_lm_or_fallback(lm_csv_path: Optional[str | Path]) -> tuple[dict, str]:
    if lm_csv_path is not None:
        try:
            from src.methodology.lm_dictionary import load_lm_dictionary
            return load_lm_dictionary(lm_csv_path), "loughran_mcdonald"
        except Exception:
            pass
    return {}, "fallback_wordlist"


def _score_headline(text: str, dictionary: dict, mode: str) -> tuple[int, int, int]:
    """Return (n_pos, n_neg, n_words) for one headline."""
    if not text:
        return 0, 0, 0
    tokens = [w.strip(".,!?:;()[]\"'").upper() for w in text.split() if w]
    tokens = [t for t in tokens if t]
    if mode == "loughran_mcdonald":
        n_pos = sum(1 for t in tokens if "positive" in dictionary.get(t, ()))
        n_neg = sum(1 for t in tokens if "negative" in dictionary.get(t, ()))
    else:
        lc = [t.lower() for t in tokens]
        n_pos = sum(1 for t in lc if t in _FALLBACK_POS)
        n_neg = sum(1 for t in lc if t in _FALLBACK_NEG)
    return n_pos, n_neg, len(tokens)


def _fetch_headlines(ticker: str, yf_ticker_factory=None) -> list[dict[str, Any]]:
    try:
        if yf_ticker_factory is None:
            import yfinance as yf
            yf_ticker_factory = yf.Ticker
        items = yf_ticker_factory(ticker).news or []
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        title = (it.get("title") or it.get("content", {}).get("title") or "")
        ts = it.get("providerPublishTime") or it.get("pubDate")
        try:
            if isinstance(ts, (int, float)):
                d = _dt.datetime.utcfromtimestamp(int(ts)).date()
            else:
                d = _dt.date.fromisoformat(str(ts)[:10])
        except Exception:
            continue
        out.append({"date": d, "title": title})
    return out


def compute_news_sentiment_v2(
    ticker: str,
    *,
    as_of_date: Optional[str] = None,
    lm_csv_path: Optional[str | Path] = "config/lm_dictionary/LoughranMcDonald_MasterDictionary.csv",
    baseline_days: int = 30,
    yf_ticker_factory=None,
) -> dict[str, Any]:
    """Score yfinance headlines and aggregate to 24h / 7d windows.

    Returns a dict shaped to feed lm_tone_signal.compute_lm_tone_shift
    (key 'net_tone' + 'filing_date' + 'ticker') and news_volume_anomaly
    (key 'mention_timestamp'). Bigdata-com swap-in: see module docstring.
    """
    dictionary, mode = _load_lm_or_fallback(lm_csv_path)
    as_of = _dt.date.fromisoformat(as_of_date) if as_of_date else _dt.date.today()
    headlines = _fetch_headlines(ticker, yf_ticker_factory=yf_ticker_factory)

    n_pos = n_neg = n_words = n_24h = n_7d = n_baseline = 0
    for h in headlines:
        p, n, w = _score_headline(h["title"], dictionary, mode)
        n_pos += p; n_neg += n; n_words += w
        delta = (as_of - h["date"]).days
        if 0 <= delta <= 1:
            n_24h += 1
        if 0 <= delta <= 7:
            n_7d += 1
        if 1 < delta <= baseline_days:
            n_baseline += 1

    net_tone = (n_pos - n_neg) / max(n_words, 1) if n_words else 0.0
    daily_baseline = n_baseline / max(baseline_days - 1, 1)
    vol_z = (n_24h - daily_baseline) / max(math.sqrt(daily_baseline), 1.0)

    return {
        "ticker": ticker.upper(),
        "filing_date": as_of.isoformat(),
        "mention_timestamp": as_of.isoformat(),
        "net_tone": net_tone,
        "n_positive": n_pos,
        "n_negative": n_neg,
        "total_words": n_words,
        "n_headlines_24h": n_24h,
        "n_headlines_7d": n_7d,
        "headline_volume_z": vol_z,
        "dictionary_mode": mode,
        "news_sentiment_v2_version": NEWS_SENTIMENT_V2_VERSION,
    }
