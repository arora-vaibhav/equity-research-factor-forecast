"""Tests for `src.methodology.lm_tone_signal`.

Pure-function exercises with synthetic inputs. No DB, no HTTP.

Coverage:
  * tokenize: punctuation stripping, contractions, numerics, casing,
    empty input.
  * score_tone: hand-counted tag totals against a tiny LM-style dict;
    sign convention; empty text yields net_tone == 0.0.
  * compute_lm_tone_shift: z-score against own-firm baseline of 4;
    NaN when baseline < 4; NaN-on-zero-variance; only filings <=
    as_of_date counted; positive z == bullish.
"""
from __future__ import annotations

import math

import pytest

from src.methodology.lm_tone_signal import (
    LM_TONE_SIGNAL_VERSION,
    LM_TONE_VERSION,
    compute_lm_tone_shift,
    score_tone,
    tokenize,
)


def test_version_constant():
    assert LM_TONE_SIGNAL_VERSION == "1.0"
    # Backwards-compat alias resolves to the same value.
    assert LM_TONE_VERSION == LM_TONE_SIGNAL_VERSION


# ---------------------------------------------------------------------------
# tokenize
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_empty_string_returns_empty_list(self):
        assert tokenize("") == []

    def test_none_input_returns_empty_list(self):
        # `tokenize` defensively returns [] on falsy text.
        assert tokenize(None) == []  # type: ignore[arg-type]

    def test_uppercase_output(self):
        assert tokenize("hello world") == ["HELLO", "WORLD"]

    def test_strips_punctuation(self):
        assert tokenize("Hello, world! Yes.") == ["HELLO", "WORLD", "YES"]

    def test_keeps_intraword_apostrophe(self):
        toks = tokenize("don't didn't won't")
        assert toks == ["DON'T", "DIDN'T", "WON'T"]

    def test_drops_numbers(self):
        assert tokenize("revenue 2024 grew 15%") == ["REVENUE", "GREW"]

    def test_drops_non_ascii(self):
        # Plain-ASCII regex; accented characters drop out.
        toks = tokenize("café résumé")
        assert toks == ["CAF", "R", "SUM"]

    def test_dedup_not_performed(self):
        """Token list preserves duplicates -- score_tone counts on them."""
        assert tokenize("loss loss loss") == ["LOSS", "LOSS", "LOSS"]


# ---------------------------------------------------------------------------
# score_tone
# ---------------------------------------------------------------------------


MINI_DICT = {
    "LOSS": frozenset({"negative"}),
    "BANKRUPTCY": frozenset({"negative"}),
    "STRONG": frozenset({"positive"}),
    "BENEFICIAL": frozenset({"positive"}),
    "MAYBE": frozenset({"uncertainty"}),
    "LITIGATION": frozenset({"negative", "litigious"}),
}


class TestScoreTone:
    def test_empty_text_yields_zero_net_tone(self):
        out = score_tone("", MINI_DICT)
        assert out["n_positive"] == 0
        assert out["n_negative"] == 0
        assert out["total_words"] == 0
        assert out["net_tone"] == 0.0

    def test_counts_all_tags(self):
        text = "strong beneficial loss bankruptcy litigation maybe filler"
        out = score_tone(text, MINI_DICT)
        assert out["n_positive"] == 2  # strong, beneficial
        assert out["n_negative"] == 3  # loss, bankruptcy, litigation
        assert out["n_uncertainty"] == 1  # maybe
        assert out["n_litigious"] == 1  # litigation
        assert out["total_words"] == 7
        assert out["net_tone"] == (2 - 3) / 7

    def test_unknown_words_count_to_total_only(self):
        out = score_tone("alpha beta gamma delta", MINI_DICT)
        assert out["n_positive"] == 0
        assert out["n_negative"] == 0
        assert out["total_words"] == 4
        assert out["net_tone"] == 0.0

    def test_positive_skew_yields_positive_net_tone(self):
        out = score_tone("strong strong strong loss filler", MINI_DICT)
        assert out["n_positive"] == 3
        assert out["n_negative"] == 1
        assert out["total_words"] == 5
        assert out["net_tone"] == (3 - 1) / 5

    def test_negative_skew_yields_negative_net_tone(self):
        out = score_tone("loss loss loss strong filler", MINI_DICT)
        assert out["n_positive"] == 1
        assert out["n_negative"] == 3
        assert out["net_tone"] == (1 - 3) / 5

    def test_case_insensitive_via_tokenizer(self):
        out_lower = score_tone("loss strong", MINI_DICT)
        out_upper = score_tone("LOSS STRONG", MINI_DICT)
        out_mixed = score_tone("Loss Strong", MINI_DICT)
        assert out_lower == out_upper == out_mixed


# ---------------------------------------------------------------------------
# compute_lm_tone_shift
# ---------------------------------------------------------------------------


def _f(ticker: str, date: str, nt: float) -> dict:
    return {"ticker": ticker, "filing_date": date, "net_tone": nt}


class TestComputeLmToneShift:
    def test_invalid_baseline_n_raises(self):
        with pytest.raises(ValueError):
            compute_lm_tone_shift([], as_of_date="2026-05-22", baseline_n=0)

    def test_empty_input_returns_empty_dict(self):
        assert compute_lm_tone_shift([], as_of_date="2026-05-22") == {}

    def test_ticker_with_fewer_than_baseline_filings_returns_nan(self):
        # Only 4 filings -> 3 prior + 1 latest. baseline_n=4 needs 4 prior.
        rows = [
            _f("AAPL", "2024-04-30", 0.001),
            _f("AAPL", "2024-07-31", 0.002),
            _f("AAPL", "2024-10-31", 0.003),
            _f("AAPL", "2025-04-30", 0.004),
        ]
        out = compute_lm_tone_shift(rows, as_of_date="2026-05-22")
        assert math.isnan(out["AAPL"])

    def test_positive_z_when_latest_above_baseline(self):
        rows = [
            _f("AAPL", "2023-04-30", 0.001),
            _f("AAPL", "2023-07-31", 0.002),
            _f("AAPL", "2023-10-31", 0.003),
            _f("AAPL", "2024-04-30", 0.004),
            _f("AAPL", "2025-04-30", 0.010),
        ]
        out = compute_lm_tone_shift(rows, as_of_date="2026-05-22")
        z = out["AAPL"]
        assert math.isfinite(z)
        assert z > 1.0

    def test_negative_z_when_latest_below_baseline(self):
        rows = [
            _f("AAPL", "2023-04-30", 0.010),
            _f("AAPL", "2023-07-31", 0.011),
            _f("AAPL", "2023-10-31", 0.012),
            _f("AAPL", "2024-04-30", 0.013),
            _f("AAPL", "2025-04-30", -0.005),
        ]
        out = compute_lm_tone_shift(rows, as_of_date="2026-05-22")
        z = out["AAPL"]
        assert math.isfinite(z)
        assert z < -1.0

    def test_zero_variance_baseline_returns_zero_when_latest_equal(self):
        rows = [
            _f("AAPL", "2023-04-30", 0.001),
            _f("AAPL", "2023-07-31", 0.001),
            _f("AAPL", "2023-10-31", 0.001),
            _f("AAPL", "2024-04-30", 0.001),
            _f("AAPL", "2025-04-30", 0.001),
        ]
        out = compute_lm_tone_shift(rows, as_of_date="2026-05-22")
        assert out["AAPL"] == 0.0

    def test_zero_variance_baseline_returns_nan_when_latest_differs(self):
        rows = [
            _f("AAPL", "2023-04-30", 0.001),
            _f("AAPL", "2023-07-31", 0.001),
            _f("AAPL", "2023-10-31", 0.001),
            _f("AAPL", "2024-04-30", 0.001),
            _f("AAPL", "2025-04-30", 0.999),
        ]
        out = compute_lm_tone_shift(rows, as_of_date="2026-05-22")
        assert math.isnan(out["AAPL"])

    def test_future_filings_excluded(self):
        """Filings after as_of_date are dropped."""
        rows = [
            _f("AAPL", "2023-04-30", 0.001),
            _f("AAPL", "2023-07-31", 0.002),
            _f("AAPL", "2023-10-31", 0.003),
            _f("AAPL", "2024-04-30", 0.004),
            _f("AAPL", "2025-04-30", 0.010),  # post-as_of, dropped
        ]
        out = compute_lm_tone_shift(rows, as_of_date="2024-12-31")
        # Now latest is 2024-04-30 with prior=3 -> NaN
        assert math.isnan(out["AAPL"])

    def test_per_ticker_independence(self):
        rows = [
            _f("AAPL", "2023-04-30", 0.001),
            _f("AAPL", "2023-07-31", 0.002),
            _f("AAPL", "2023-10-31", 0.003),
            _f("AAPL", "2024-04-30", 0.004),
            _f("AAPL", "2025-04-30", 0.010),
            _f("MSFT", "2024-01-31", 0.005),
            _f("MSFT", "2024-07-31", 0.005),
            _f("MSFT", "2025-01-31", 0.005),
        ]
        out = compute_lm_tone_shift(rows, as_of_date="2026-05-22")
        assert math.isfinite(out["AAPL"])
        assert math.isnan(out["MSFT"])

    def test_bad_rows_silently_skipped(self):
        """Missing fields, bad dates, non-finite net_tone are dropped."""
        rows = [
            {"ticker": "AAPL"},
            {"ticker": "AAPL", "filing_date": "garbage", "net_tone": 0.0},
            {"ticker": "AAPL", "filing_date": "2024-04-30", "net_tone": float("nan")},
            _f("AAPL", "2023-04-30", 0.001),
            _f("AAPL", "2023-07-31", 0.002),
            _f("AAPL", "2023-10-31", 0.003),
            _f("AAPL", "2024-04-30", 0.004),
            _f("AAPL", "2025-04-30", 0.010),
        ]
        out = compute_lm_tone_shift(rows, as_of_date="2026-05-22")
        assert math.isfinite(out["AAPL"])
