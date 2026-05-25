"""Tests for PytrendsSource.fetch_search_interest (Phase A.3.8).

Mocks TrendReq in src.common.datasources.pytrends_source so no live
Google Trends requests are made.

Caller: pytest.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.common.database import DatabaseManager
from src.common.datasources.pytrends_source import PytrendsSource


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v11()
    return mgr


def _make_df(term: str, rows: int = 4) -> pd.DataFrame:
    """Build a minimal interest_over_time() DataFrame for one term."""
    import datetime as _dt
    dates = pd.date_range(
        end=_dt.date(2026, 5, 13), periods=rows, freq="W",
    )
    svi_values = [45, 52, 48, 60][:rows]
    df = pd.DataFrame({term: svi_values, "isPartial": [False] * rows}, index=dates)
    return df


def _mock_trend_req(mocker, term_to_df: dict[str, pd.DataFrame]):
    """Patch TrendReq so each build_payload call returns the right DataFrame."""
    call_state: dict = {"last_term": None}

    def fake_build_payload(self_inner, kw_list, **kwargs):
        call_state["last_term"] = kw_list[0] if kw_list else None

    def fake_iot(self_inner):
        term = call_state["last_term"]
        return term_to_df.get(term, pd.DataFrame())

    mock_cls = MagicMock()
    instance = MagicMock()
    instance.build_payload.side_effect = lambda kw_list, **kwargs: fake_build_payload(
        instance, kw_list, **kwargs
    )
    instance.interest_over_time.side_effect = lambda: fake_iot(instance)
    mock_cls.return_value = instance

    mocker.patch("src.common.datasources.pytrends_source.TrendReq", mock_cls)
    mocker.patch("src.common.datasources.pytrends_source.time.sleep")
    return mock_cls, instance


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_default_basket_pulls_three_terms(db, mocker):
    """Default basket = 3 terms; each emits rows."""
    src = PytrendsSource()
    term_dfs = {t: _make_df(t) for t in src.DEFAULT_FEARS_BASKET}
    _mock_trend_req(mocker, term_dfs)

    n = src.fetch_search_interest("r1", db)
    assert n > 0

    with sqlite3.connect(db.db_path) as c:
        terms = {r[0] for r in c.execute("SELECT DISTINCT term FROM raw_pytrends")}
    assert set(src.DEFAULT_FEARS_BASKET) == terms


def test_custom_basket_pulls_custom_terms(db, mocker):
    custom = ("inflation", "crisis")
    src = PytrendsSource()
    term_dfs = {t: _make_df(t) for t in custom}
    _mock_trend_req(mocker, term_dfs)

    n = src.fetch_search_interest("r1", db, terms=custom)
    assert n > 0

    with sqlite3.connect(db.db_path) as c:
        terms = {r[0] for r in c.execute("SELECT DISTINCT term FROM raw_pytrends")}
    assert set(custom) == terms


def test_each_pull_writes_row_per_term_date_geo(db, mocker):
    """One row per (term, observation_date, geo) is written."""
    src = PytrendsSource()
    term_dfs = {t: _make_df(t, rows=4) for t in src.DEFAULT_FEARS_BASKET}
    _mock_trend_req(mocker, term_dfs)

    src.fetch_search_interest("r1", db, geo="US")
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT term, observation_date, geo FROM raw_pytrends"
        ).fetchall()
    # 3 terms x 4 dates = 12 rows
    assert len(rows) == 12
    geos = {r[2] for r in rows}
    assert geos == {"US"}


def test_sleep_called_between_terms(db, mocker):
    """_INTER_TERM_DELAY_SEC sleep is called N-1 times for N terms."""
    src = PytrendsSource()
    term_dfs = {t: _make_df(t) for t in src.DEFAULT_FEARS_BASKET}
    _mock_trend_req(mocker, term_dfs)
    sleep_mock = mocker.patch("src.common.datasources.pytrends_source.time.sleep")

    src.fetch_search_interest("r1", db)
    # 3 terms -> sleep called 2 times (between term 0->1, 1->2)
    assert sleep_mock.call_count == len(src.DEFAULT_FEARS_BASKET) - 1
    for call in sleep_mock.call_args_list:
        assert call.args[0] == src._INTER_TERM_DELAY_SEC


def test_watermark_short_circuit_within_7d_returns_zero(db, mocker):
    """Second call within 7 days uses watermark and returns 0."""
    src = PytrendsSource()
    term_dfs = {t: _make_df(t) for t in src.DEFAULT_FEARS_BASKET}
    _mock_trend_req(mocker, term_dfs)

    n1 = src.fetch_search_interest("r1", db)
    assert n1 > 0

    # Re-patch for second call
    mocker.patch("src.common.datasources.pytrends_source.time.sleep")

    n2 = src.fetch_search_interest("r2", db)
    assert n2 == 0


def test_empty_response_produces_zero_rows(db, mocker):
    """Empty DataFrame from TrendReq -> 0 rows inserted, no exception."""
    src = PytrendsSource()
    term_dfs = {t: pd.DataFrame() for t in src.DEFAULT_FEARS_BASKET}
    _mock_trend_req(mocker, term_dfs)

    n = src.fetch_search_interest("r1", db)
    assert n == 0

    with sqlite3.connect(db.db_path) as c:
        cnt = c.execute("SELECT COUNT(*) FROM raw_pytrends").fetchone()[0]
    assert cnt == 0


def test_exception_from_trend_req_records_error(db, mocker):
    """Exception from TrendReq (e.g., 429) -> error_count updated, 0 rows."""
    src = PytrendsSource()

    mock_cls = MagicMock()
    instance = MagicMock()
    instance.build_payload.side_effect = Exception("HTTP 429: Too Many Requests")
    mock_cls.return_value = instance
    mocker.patch("src.common.datasources.pytrends_source.TrendReq", mock_cls)
    mocker.patch("src.common.datasources.pytrends_source.time.sleep")

    n = src.fetch_search_interest("r1", db)
    assert n == 0

    w = db.get_watermark(src.name, src._WATERMARK_TICKER, src._WATERMARK_FIELD)
    assert w is not None
    assert w["error_count"] >= 1

    with sqlite3.connect(db.db_path) as c:
        cnt = c.execute("SELECT COUNT(*) FROM raw_pytrends").fetchone()[0]
    assert cnt == 0


def test_health_check_returns_ok_when_pytrends_importable():
    """health_check is OK because pytrends was imported at module load."""
    from src.common.datasources.pytrends_source import TrendReq
    src = PytrendsSource()
    status = src.health_check()
    if TrendReq is not None:
        assert status.status.value == "ok"
    else:
        assert status.status.value == "failed"
