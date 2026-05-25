"""Tests for StockanalysisSource.fetch_ratio_history (Phase A.3.6)."""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.stockanalysis_source import StockanalysisSource


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "stockanalysis"
ANNUAL_HTML = (FIXTURE_DIR / "aapl_ratios_annual.html").read_text(encoding="utf-8")
QUARTERLY_HTML = (FIXTURE_DIR / "aapl_ratios_quarterly.html").read_text(encoding="utf-8")
TTM_HTML = (FIXTURE_DIR / "aapl_ratios_ttm.html").read_text(encoding="utf-8")


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v8()
    return mgr


def _wire_url_dispatch_mock(mocker, sleep_zero: bool = True):
    """Dispatch mock by URL fragment — the three ratio URLs route to
    the three fixtures. Unknown URLs 404."""
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        if "?p=quarterly" in url:
            resp.status_code = 200
            resp.ok = True
            resp.text = QUARTERLY_HTML
            resp.content = QUARTERLY_HTML.encode()
        elif "?p=trailing" in url:
            resp.status_code = 200
            resp.ok = True
            resp.text = TTM_HTML
            resp.content = TTM_HTML.encode()
        elif "/financials/ratios/" in url:
            # Annual is the bare URL (no ?p=...).
            resp.status_code = 200
            resp.ok = True
            resp.text = ANNUAL_HTML
            resp.content = ANNUAL_HTML.encode()
        else:
            resp.status_code = 404
            resp.ok = False
            resp.text = ""
            resp.content = b""
        return resp
    mocker.patch(
        "src.common.datasources.stockanalysis_source.requests.get",
        side_effect=side_effect,
    )
    if sleep_zero:
        mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")


def test_fetch_ratio_history_persists_annual_quarterly_ttm(db, mocker):
    _wire_url_dispatch_mock(mocker)
    n = StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        period_types = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT period_type FROM raw_stockanalysis_ratios"
            )
        )
    assert period_types == ["annual", "quarterly", "ttm"]


def test_fetch_ratio_history_returns_expected_row_count(db, mocker):
    """Annual: 12 metrics * 5 years = 60.
       Quarterly: 5 metrics * 5 quarters = 25, but 5 of those quarters
       (Q4 2023 -> 2023-12-31) collide with annual 2023 -> 2023-12-31
       on PK (ticker, metric, period_end_date) and are INSERT OR IGNORE'd.
       Net quarterly: 25 - 5 = 20.
       TTM: 10 metrics * 1 column = 10.
       Total: 60 + 20 + 10 = 90.

       The PK collision is by design (see schemas.py docstring): annual
       FY-end and quarterly Q4 share the same period_end_date and yield
       semantically identical ratios; first write wins.
    """
    _wire_url_dispatch_mock(mocker)
    n = StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n == 90


def test_fetch_ratio_history_records_source_url_per_period_type(db, mocker):
    _wire_url_dispatch_mock(mocker)
    StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    with sqlite3.connect(db.db_path) as c:
        annual_url = c.execute(
            "SELECT DISTINCT source_url FROM raw_stockanalysis_ratios "
            "WHERE period_type='annual'"
        ).fetchone()[0]
        quarterly_url = c.execute(
            "SELECT DISTINCT source_url FROM raw_stockanalysis_ratios "
            "WHERE period_type='quarterly'"
        ).fetchone()[0]
        ttm_url = c.execute(
            "SELECT DISTINCT source_url FROM raw_stockanalysis_ratios "
            "WHERE period_type='ttm'"
        ).fetchone()[0]
    assert "?p=quarterly" in quarterly_url
    assert "?p=trailing" in ttm_url
    # Annual is the bare ratios URL with no query parameter.
    assert "?p=" not in annual_url


def test_fetch_ratio_history_updates_watermark_on_success(db, mocker):
    _wire_url_dispatch_mock(mocker)
    StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    w = db.get_watermark("stockanalysis", "AAPL", "ratio_history")
    assert w is not None
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_fetch_ratio_history_90d_skip_within_window(db, mocker):
    """Watermark within 90 days -> skip with return 0."""
    recent = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
    db.upsert_watermark(
        source="stockanalysis", ticker="AAPL", field="ratio_history",
        last_observation_date=recent, success=True,
    )
    _wire_url_dispatch_mock(mocker)
    n = StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n == 0
    with sqlite3.connect(db.db_path) as c:
        total = c.execute(
            "SELECT COUNT(*) FROM raw_stockanalysis_ratios"
        ).fetchone()[0]
    assert total == 0


def test_fetch_ratio_history_90d_skip_does_not_fire_outside_window(db, mocker):
    """Watermark older than 90 days -> fetch proceeds."""
    old = (_dt.date.today() - _dt.timedelta(days=120)).isoformat()
    db.upsert_watermark(
        source="stockanalysis", ticker="AAPL", field="ratio_history",
        last_observation_date=old, success=True,
    )
    _wire_url_dispatch_mock(mocker)
    n = StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n > 0


def test_fetch_ratio_history_404_records_error(db, mocker):
    """All three URLs 404 -> 0 rows, error_count >= 1 on watermark."""
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 404
        resp.ok = False
        resp.text = ""
        resp.content = b""
        return resp
    mocker.patch(
        "src.common.datasources.stockanalysis_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")
    n = StockanalysisSource().fetch_ratio_history(ticker="BOGUS", run_id="r1", db=db)
    assert n == 0
    w = db.get_watermark("stockanalysis", "BOGUS", "ratio_history")
    assert w is not None
    assert w["error_count"] >= 1


def test_fetch_ratio_history_partial_success(db, mocker):
    """Annual 200, quarterly 404, ttm 200 -> rows for annual + ttm only;
    watermark records error_count >= 1 because at least one URL failed."""
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        if "?p=quarterly" in url:
            resp.status_code = 404
            resp.ok = False
            resp.text = ""
            resp.content = b""
        elif "?p=trailing" in url:
            resp.status_code = 200
            resp.ok = True
            resp.text = TTM_HTML
            resp.content = TTM_HTML.encode()
        else:
            resp.status_code = 200
            resp.ok = True
            resp.text = ANNUAL_HTML
            resp.content = ANNUAL_HTML.encode()
        return resp
    mocker.patch(
        "src.common.datasources.stockanalysis_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")
    n = StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    # 60 annual + 10 ttm = 70
    assert n == 70
    w = db.get_watermark("stockanalysis", "AAPL", "ratio_history")
    assert w is not None
    assert w["error_count"] >= 1


def test_fetch_ratio_history_calls_three_distinct_urls(db, mocker):
    captured: list[str] = []

    def side_effect(url, **kwargs):
        captured.append(url)
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.text = ANNUAL_HTML
        resp.content = ANNUAL_HTML.encode()
        return resp

    mocker.patch(
        "src.common.datasources.stockanalysis_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")
    StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert len(captured) == 3
    assert any(u.endswith("/financials/ratios/") for u in captured)
    assert any("?p=quarterly" in u for u in captured)
    assert any("?p=trailing" in u for u in captured)


def test_fetch_ratio_history_polite_delay_between_calls(db, mocker):
    """time.sleep is invoked twice (between the 3 URL pulls)."""
    _wire_url_dispatch_mock(mocker, sleep_zero=False)
    sleep_spy = mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")
    StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    # 3 URLs -> 2 inter-URL sleeps.
    assert sleep_spy.call_count >= 2
    for call in sleep_spy.call_args_list:
        # Each call uses the configured polite delay (1.0s in source).
        assert call.args[0] >= 1.0


def test_fetch_ratio_history_idempotent_within_same_window(db, mocker):
    """First call fetches & writes; second call within 90d skips."""
    _wire_url_dispatch_mock(mocker)
    src = StockanalysisSource()
    n1 = src.fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n1 == 90  # See test_fetch_ratio_history_returns_expected_row_count for math.
    n2 = src.fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n2 == 0


def test_fetch_ratio_history_insert_or_ignore_protects_against_dup(db, mocker):
    """After clearing watermark, the second pull writes 0 net new rows."""
    _wire_url_dispatch_mock(mocker)
    src = StockanalysisSource()
    n1 = src.fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n1 == 90  # See test_fetch_ratio_history_returns_expected_row_count for math.
    # Backdate the watermark to force a re-pull.
    old = (_dt.date.today() - _dt.timedelta(days=200)).isoformat()
    db.upsert_watermark(
        source="stockanalysis", ticker="AAPL", field="ratio_history",
        last_observation_date=old, success=True,
    )
    n2 = src.fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n2 == 0
    with sqlite3.connect(db.db_path) as c:
        total = c.execute(
            "SELECT COUNT(*) FROM raw_stockanalysis_ratios"
        ).fetchone()[0]
    assert total == 90


def test_fetch_ratio_history_malformed_html_still_records_error(db, mocker):
    """Server returns 200 but with junk HTML -> parser returns []; watermark
    records error_count >= 1."""
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.text = "<html><body><p>oops</p></body></html>"
        resp.content = resp.text.encode()
        return resp
    mocker.patch(
        "src.common.datasources.stockanalysis_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.stockanalysis_source.time.sleep")
    n = StockanalysisSource().fetch_ratio_history(ticker="AAPL", run_id="r1", db=db)
    assert n == 0
    w = db.get_watermark("stockanalysis", "AAPL", "ratio_history")
    assert w is not None
    assert w["error_count"] >= 1
