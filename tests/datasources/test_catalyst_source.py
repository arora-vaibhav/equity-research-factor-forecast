"""Tests for src.common.datasources.catalyst_source (Phase B.1).

No live network -- yfinance.Ticker is replaced with a stub factory.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.common.database import DatabaseManager
from src.common.datasources.catalyst_source import CatalystSource


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(db_path=str(tmp_path / "t.db"))
    d.migrate_to_v14()
    return d


def _stub_ticker_factory(earnings_index=None, calendar=None):
    def factory(symbol):
        tkr = MagicMock()
        if earnings_index is not None:
            df = pd.DataFrame(
                {"EPS Estimate": [0.0] * len(earnings_index)},
                index=earnings_index,
            )
            tkr.get_earnings_dates = MagicMock(return_value=df)
        else:
            tkr.get_earnings_dates = MagicMock(return_value=None)
        tkr.calendar = calendar
        return tkr
    return factory


def test_health_check_passes():
    status = CatalystSource().health_check()
    assert status.status.value == "ok"


def test_fetch_earnings_dates_persists_and_watermarks(db):
    earnings_idx = pd.DatetimeIndex(["2026-08-01", "2026-11-01", "2027-02-01"])
    n = CatalystSource().fetch_earnings_dates(
        ticker="AAPL", run_id="r1", db=db,
        ticker_factory=_stub_ticker_factory(earnings_index=earnings_idx),
    )
    assert n == 3
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT catalyst_type, catalyst_date FROM catalyst_calendar "
            "ORDER BY catalyst_date"
        ).fetchall()
    assert [r[1] for r in rows] == ["2026-08-01", "2026-11-01", "2027-02-01"]
    assert all(r[0] == "earnings" for r in rows)
    with db.get_connection() as conn:
        wm = conn.execute(
            "SELECT last_observation_date FROM fetch_watermarks "
            "WHERE source='catalysts' AND ticker='AAPL' AND field='catalysts'"
        ).fetchone()
    assert wm[0] == "2027-02-01"


def test_fetch_earnings_dates_empty_handled(db):
    n = CatalystSource().fetch_earnings_dates(
        ticker="AAPL", run_id="r1", db=db,
        ticker_factory=_stub_ticker_factory(earnings_index=None),
    )
    assert n == 0


def test_fetch_earnings_dates_yfinance_exception_handled(db):
    def factory(symbol):
        tkr = MagicMock()
        tkr.get_earnings_dates = MagicMock(side_effect=RuntimeError("yf down"))
        return tkr
    n = CatalystSource().fetch_earnings_dates(
        ticker="AAPL", run_id="r1", db=db,
        ticker_factory=factory,
    )
    assert n == 0
    with db.get_connection() as conn:
        wm = conn.execute(
            "SELECT last_error_message FROM fetch_watermarks "
            "WHERE source='catalysts' AND ticker='AAPL' AND field='catalysts'"
        ).fetchone()
    assert wm is not None
    assert "yf" in (wm[0] or "").lower()


def test_fetch_ex_dividend_dict_calendar(db):
    cal = {"Ex-Dividend Date": "2026-09-15"}
    n = CatalystSource().fetch_ex_dividend_dates(
        ticker="AAPL", run_id="r1", db=db,
        ticker_factory=_stub_ticker_factory(calendar=cal),
    )
    assert n == 1
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT catalyst_type, catalyst_date FROM catalyst_calendar "
            "WHERE ticker='AAPL'"
        ).fetchone()
    assert row == ("ex_dividend", "2026-09-15")


def test_fetch_ex_dividend_dataframe_calendar(db):
    cal_df = pd.DataFrame({"Ex Dividend Date": [pd.Timestamp("2026-09-15")]})
    n = CatalystSource().fetch_ex_dividend_dates(
        ticker="AAPL", run_id="r1", db=db,
        ticker_factory=_stub_ticker_factory(calendar=cal_df),
    )
    assert n == 1


def test_fetch_ex_dividend_missing_calendar_no_op(db):
    n = CatalystSource().fetch_ex_dividend_dates(
        ticker="AAPL", run_id="r1", db=db,
        ticker_factory=_stub_ticker_factory(calendar=None),
    )
    assert n == 0


def test_fetch_catalysts_combines_earnings_and_ex_div(db):
    earnings_idx = pd.DatetimeIndex(["2026-08-01"])
    cal = {"Ex-Dividend Date": "2026-09-15"}
    n_e, n_d = CatalystSource().fetch_catalysts_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
        ticker_factory=_stub_ticker_factory(
            earnings_index=earnings_idx, calendar=cal,
        ),
    )
    assert (n_e, n_d) == (1, 1)


def test_fetch_catalysts_can_exclude_each_kind(db):
    earnings_idx = pd.DatetimeIndex(["2026-08-01"])
    n_e, n_d = CatalystSource().fetch_catalysts_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
        include_ex_dividend=False,
        ticker_factory=_stub_ticker_factory(earnings_index=earnings_idx),
    )
    assert (n_e, n_d) == (1, 0)
    n_e, n_d = CatalystSource().fetch_catalysts_for_ticker(
        ticker="AAPL", run_id="r2", db=db,
        include_earnings=False,
        ticker_factory=_stub_ticker_factory(
            calendar={"Ex-Dividend Date": "2026-09-15"},
        ),
    )
    assert (n_e, n_d) == (0, 1)
