"""Tests for `src.layer1_universe.news_activity`.

Coverage:
  * opportunistic_insider_score: buy/sell/neutral cases; window
    boundary; opportunistic-only filter; multi-ticker independence.
  * _renormalize_and_sum: full surviving signals; partial; all-missing
    -> NaN; zero-weight surviving -> NaN.
  * compute_news_activity_score:
    - All signals missing -> NaN per ticker.
    - Single signal survives -> raw value verbatim.
    - Ticker absent from all sources -> NaN.
    - Empty signal iterables treated as missing.
  * load_news_activity_inputs: pulls from a seeded DB; ticker filter
    works; every expected key present.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.layer1_universe.news_activity import (
    DEFAULT_SUBWEIGHTS,
    NEWS_ACTIVITY_VERSION,
    _renormalize_and_sum,
    compute_news_activity_score,
    load_news_activity_inputs,
    opportunistic_insider_score,
)


def test_version_constant():
    assert NEWS_ACTIVITY_VERSION == "1.0"


def _txn(ticker, date, code, is_opp):
    return {
        "ticker": ticker,
        "transaction_date": date,
        "transaction_code": code,
        "is_opportunistic": is_opp,
    }


# ---------------------------------------------------------------------------
# opportunistic_insider_score
# ---------------------------------------------------------------------------


class TestOpportunisticInsiderScore:
    def test_no_data_yields_empty_dict(self):
        assert opportunistic_insider_score([], as_of_date="2026-05-22") == {}

    def test_ticker_with_no_opportunistic_txns_gets_zero(self):
        rows = [_txn("AAPL", "2026-05-01", "P", False)]
        out = opportunistic_insider_score(rows, as_of_date="2026-05-22")
        assert out["AAPL"] == 0.0

    def test_net_buying_positive(self):
        rows = [
            _txn("AAPL", "2026-05-01", "P", True),
            _txn("AAPL", "2026-05-05", "P", True),
            _txn("AAPL", "2026-05-10", "S", True),
        ]
        out = opportunistic_insider_score(rows, as_of_date="2026-05-22")
        assert math.isclose(out["AAPL"], 1.0 / 3.0)

    def test_net_selling_negative(self):
        rows = [
            _txn("AAPL", "2026-05-01", "S", True),
            _txn("AAPL", "2026-05-05", "S", True),
            _txn("AAPL", "2026-05-10", "P", True),
        ]
        out = opportunistic_insider_score(rows, as_of_date="2026-05-22")
        assert math.isclose(out["AAPL"], -1.0 / 3.0)

    def test_window_boundary_excludes_old_txns(self):
        rows = [
            _txn("AAPL", "2026-04-12", "P", True),  # ~40d back, out of window
            _txn("AAPL", "2026-05-15", "P", True),
        ]
        out = opportunistic_insider_score(rows, as_of_date="2026-05-22")
        assert out["AAPL"] == 1.0

    def test_routine_codes_excluded(self):
        rows = [
            _txn("AAPL", "2026-05-01", "M", True),  # option exercise
            _txn("AAPL", "2026-05-02", "G", True),  # gift
        ]
        out = opportunistic_insider_score(rows, as_of_date="2026-05-22")
        assert out["AAPL"] == 0.0

    def test_multiple_tickers_independent(self):
        rows = [
            _txn("AAPL", "2026-05-01", "P", True),
            _txn("MSFT", "2026-05-01", "S", True),
        ]
        out = opportunistic_insider_score(rows, as_of_date="2026-05-22")
        assert out["AAPL"] == 1.0
        assert out["MSFT"] == -1.0


# ---------------------------------------------------------------------------
# _renormalize_and_sum
# ---------------------------------------------------------------------------


class TestRenormalizeAndSum:
    def test_full_survivors(self):
        per = {"a": 1.0, "b": 2.0}
        w = {"a": 0.4, "b": 0.6}
        assert _renormalize_and_sum(per, w) == pytest.approx(1.6)

    def test_partial_survivors_renormalize(self):
        per = {"a": 1.0, "b": 2.0}
        w = {"a": 0.2, "b": 0.3, "c": 0.5}
        # Surviving sum 0.5 -> a=0.4, b=0.6 -> 1.0*0.4 + 2.0*0.6 = 1.6
        assert _renormalize_and_sum(per, w) == pytest.approx(1.6)

    def test_nan_inputs_excluded(self):
        per = {"a": float("nan"), "b": 2.0}
        w = {"a": 0.5, "b": 0.5}
        assert _renormalize_and_sum(per, w) == pytest.approx(2.0)

    def test_no_survivors_returns_nan(self):
        per: dict[str, float] = {}
        w = {"a": 1.0}
        assert math.isnan(_renormalize_and_sum(per, w))

    def test_all_zero_weight_returns_nan(self):
        per = {"a": 1.0, "b": 2.0}
        w = {"a": 0.0, "b": 0.0}
        assert math.isnan(_renormalize_and_sum(per, w))


# ---------------------------------------------------------------------------
# compute_news_activity_score
# ---------------------------------------------------------------------------


class TestComputeNewsActivityScore:
    def test_no_signals_returns_nan_per_ticker(self):
        out = compute_news_activity_score(
            tickers=["AAPL", "MSFT"],
            as_of_date="2026-05-22",
        )
        assert math.isnan(out["AAPL"])
        assert math.isnan(out["MSFT"])

    def test_single_signal_survives_renormalization(self):
        rows = [_txn("AAPL", "2026-05-15", "P", True)]
        out = compute_news_activity_score(
            tickers=["AAPL"],
            as_of_date="2026-05-22",
            insider_rows=rows,
        )
        # 1 opportunistic buy / 1 total = +1.0. Single surviving signal
        # -> renormalised weight is 1.0 -> score = +1.0.
        assert out["AAPL"] == pytest.approx(1.0)

    def test_ticker_not_in_any_source_returns_nan(self):
        rows = [_txn("AAPL", "2026-05-15", "P", True)]
        out = compute_news_activity_score(
            tickers=["AAPL", "ZZZ"],
            as_of_date="2026-05-22",
            insider_rows=rows,
        )
        assert math.isfinite(out["AAPL"])
        assert math.isnan(out["ZZZ"])

    def test_default_subweights_used_when_not_passed(self):
        rows = [_txn("AAPL", "2026-05-15", "P", True)]
        out1 = compute_news_activity_score(
            tickers=["AAPL"], as_of_date="2026-05-22", insider_rows=rows,
        )
        out2 = compute_news_activity_score(
            tickers=["AAPL"], as_of_date="2026-05-22", insider_rows=rows,
            subweights=DEFAULT_SUBWEIGHTS,
        )
        assert out1["AAPL"] == out2["AAPL"]

    def test_empty_signal_iterables_treated_as_missing(self):
        out = compute_news_activity_score(
            tickers=["AAPL"],
            as_of_date="2026-05-22",
            insider_rows=[],
            short_interest_rows=[],
        )
        assert math.isnan(out["AAPL"])


# ---------------------------------------------------------------------------
# load_news_activity_inputs (DB shape-adapter)
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(db_path=str(tmp_path / "test.db"))
    d.migrate_to_v12()
    return d


def test_load_news_activity_inputs_returns_all_keys(db):
    out = load_news_activity_inputs(db, as_of_date="2026-05-22")
    expected = {
        "insider_rows", "short_interest_rows", "revisions_rows",
        "news_volume_rows", "filing_rows", "tone_rows", "fears_rows",
    }
    assert set(out.keys()) == expected
    for v in out.values():
        assert isinstance(v, list)


def test_load_news_activity_inputs_ticker_filter(db):
    """When tickers is provided, only matching rows for per-ticker
    sources are returned. FEARS is universe-wide and unaffected."""
    from src.common.schemas import RawEdgarFilingRow
    db.insert_raw_edgar_filings([
        RawEdgarFilingRow(
            run_id="r", ticker="AAPL", cik="0000320193", form_type="8-K",
            filing_date="2026-04-01", source_filing_accn="0000320193-26-000001",
            primary_doc_url="https://x", scrape_timestamp="2026-05-22T00:00:00Z",
        ),
        RawEdgarFilingRow(
            run_id="r", ticker="MSFT", cik="0000789019", form_type="8-K",
            filing_date="2026-04-15", source_filing_accn="0000789019-26-000002",
            primary_doc_url="https://x", scrape_timestamp="2026-05-22T00:00:00Z",
        ),
    ])

    out = load_news_activity_inputs(
        db, as_of_date="2026-05-22", tickers=["AAPL"],
    )
    filings = out["filing_rows"]
    assert len(filings) == 1
    assert filings[0]["ticker"] == "AAPL"
