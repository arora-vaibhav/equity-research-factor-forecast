"""Tests for OpenBBSource.fetch_fundamentals_for_ticker (Phase A.3.7)."""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.openbb_source import OpenBBSource


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "openbb"
FMP_QUOTE = (FIXTURE_DIR / "fmp_quote_AAPL.json").read_text(encoding="utf-8")
FMP_PROFILE = (FIXTURE_DIR / "fmp_profile_AAPL.json").read_text(encoding="utf-8")
FMP_RATIOS_TTM = (FIXTURE_DIR / "fmp_ratios_ttm_AAPL.json").read_text(encoding="utf-8")
FMP_KEY_METRICS_TTM = (FIXTURE_DIR / "fmp_key_metrics_ttm_AAPL.json").read_text(encoding="utf-8")
POLYGON_REF = (FIXTURE_DIR / "polygon_aapl_ref.json").read_text(encoding="utf-8")
POLYGON_PREV = (FIXTURE_DIR / "polygon_aapl_prev.json").read_text(encoding="utf-8")
TIINGO_DAILY = (FIXTURE_DIR / "tiingo_aapl_daily.json").read_text(encoding="utf-8")
TIINGO_IEX = (FIXTURE_DIR / "tiingo_aapl_iex.json").read_text(encoding="utf-8")


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v9()
    return mgr


def _ok_resp(mocker, text: str):
    resp = mocker.MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.text = text
    resp.content = text.encode()
    import json
    resp.json = lambda: json.loads(text)
    return resp


def _404_resp(mocker):
    resp = mocker.MagicMock()
    resp.status_code = 404
    resp.ok = False
    resp.text = ""
    resp.content = b""
    resp.json = lambda: {}
    return resp


def _wire_url_dispatch_all_ok(mocker):
    """Single side_effect that dispatches by URL fragment - covers all
    eight provider endpoints with happy responses."""
    def side_effect(url, **kwargs):
        # FMP
        if "financialmodelingprep.com/stable/quote" in url:
            return _ok_resp(mocker, FMP_QUOTE)
        if "financialmodelingprep.com/stable/profile" in url:
            return _ok_resp(mocker, FMP_PROFILE)
        if "financialmodelingprep.com/stable/ratios-ttm" in url:
            return _ok_resp(mocker, FMP_RATIOS_TTM)
        if "financialmodelingprep.com/stable/key-metrics-ttm" in url:
            return _ok_resp(mocker, FMP_KEY_METRICS_TTM)
        # Polygon
        if "api.polygon.io/v3/reference/tickers/" in url:
            return _ok_resp(mocker, POLYGON_REF)
        if "api.polygon.io/v2/aggs/ticker/" in url and "/prev" in url:
            return _ok_resp(mocker, POLYGON_PREV)
        # Tiingo
        if "api.tiingo.com/tiingo/daily/" in url:
            return _ok_resp(mocker, TIINGO_DAILY)
        if "api.tiingo.com/iex/" in url:
            return _ok_resp(mocker, TIINGO_IEX)
        return _404_resp(mocker)

    mocker.patch(
        "src.common.datasources.openbb_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.openbb_source.time.sleep")


def _mock_all_keys_present(mocker):
    """Stub get_api_key so all three providers report a key."""
    def fake_get_key(provider, *args, **kwargs):
        return {
            "fmp": "fmp-test-key",
            "polygon": "poly-test-key",
            "tiingo": "tiingo-test-key",
        }.get(provider)
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=fake_get_key,
    )


# --- happy path: all three providers route, all OK ---------------------


def test_fetch_writes_at_least_one_row_per_provider(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert providers == ["fmp", "polygon", "tiingo"]


def test_fetch_emits_pe_ratio_from_fmp(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='pe_ratio' AND provider_used='fmp'"
        ).fetchone()
    assert v is not None
    assert abs(v[0] - 28.45) < 1e-9


def test_fetch_emits_market_cap_from_polygon(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='market_cap' AND provider_used='polygon'"
        ).fetchone()
    assert v is not None
    assert v[0] == 2811234567890.0


def test_fetch_emits_last_price_from_tiingo(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        v = c.execute(
            "SELECT value FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='last_price' AND provider_used='tiingo'"
        ).fetchone()
    assert v is not None
    assert abs(v[0] - 180.50) < 1e-9


def test_fetch_records_same_field_from_two_providers_as_two_rows(db, mocker):
    """`market_cap` is emitted by BOTH FMP and Polygon. The long-format PK
    intentionally admits both rows - that's the cross-vendor substrate."""
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT provider_used FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='market_cap' "
            "ORDER BY provider_used"
        ).fetchall()
    providers = {r[0] for r in rows}
    # Both fmp and polygon emit market_cap.
    assert "fmp" in providers
    assert "polygon" in providers


def test_fetch_updates_watermark_on_success(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    w = db.get_watermark("openbb", "AAPL", "multi_provider")
    assert w is not None
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


# --- 24h refresh-skip ---------------------------------------------------


def test_fetch_24h_skip_within_window(db, mocker):
    """Watermark within 24h -> skip with return 0."""
    recent = _dt.date.today().isoformat()
    db.upsert_watermark(
        source="openbb", ticker="AAPL", field="multi_provider",
        last_observation_date=recent, success=True,
    )
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n == 0
    with sqlite3.connect(db.db_path) as c:
        total = c.execute("SELECT COUNT(*) FROM raw_openbb").fetchone()[0]
    assert total == 0


def test_fetch_24h_skip_does_not_fire_after_2d(db, mocker):
    old = (_dt.date.today() - _dt.timedelta(days=2)).isoformat()
    db.upsert_watermark(
        source="openbb", ticker="AAPL", field="multi_provider",
        last_observation_date=old, success=True,
    )
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0


# --- key-missing graceful degradation ----------------------------------


def test_fetch_polygon_key_missing_falls_back_to_fmp_and_tiingo(db, mocker):
    """Polygon key absent -> route skipped silently; FMP + Tiingo still write.
    Source returns success (n>0), watermark records no error."""
    def fake_get_key(provider, *args, **kwargs):
        return {"fmp": "fmp-k", "tiingo": "tiingo-k"}.get(provider)
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=fake_get_key,
    )
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert providers == ["fmp", "tiingo"]
    w = db.get_watermark("openbb", "AAPL", "multi_provider")
    assert w is not None
    assert w["error_count"] == 0


def test_fetch_fmp_key_missing_falls_back_to_polygon_and_tiingo(db, mocker):
    def fake_get_key(provider, *args, **kwargs):
        return {"polygon": "p-k", "tiingo": "t-k"}.get(provider)
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=fake_get_key,
    )
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert providers == ["polygon", "tiingo"]


def test_fetch_tiingo_key_missing_falls_back_to_fmp_and_polygon(db, mocker):
    def fake_get_key(provider, *args, **kwargs):
        return {"fmp": "f-k", "polygon": "p-k"}.get(provider)
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=fake_get_key,
    )
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert providers == ["fmp", "polygon"]


def test_fetch_all_keys_missing_records_error_and_returns_zero(db, mocker):
    """No keys provisioned -> all three routes skipped; n=0 and watermark
    records error_count >= 1."""
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        return_value=None,
    )
    _wire_url_dispatch_all_ok(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n == 0
    w = db.get_watermark("openbb", "AAPL", "multi_provider")
    assert w is not None
    assert w["error_count"] >= 1


# --- per-provider HTTP failures ----------------------------------------


def test_fetch_fmp_http_error_skips_route_and_continues(db, mocker):
    """FMP /stable/* all 404 -> FMP route emits zero rows; Polygon + Tiingo
    continue normally."""
    _mock_all_keys_present(mocker)

    def side_effect(url, **kwargs):
        if "financialmodelingprep.com" in url:
            return _404_resp(mocker)
        if "api.polygon.io/v3/reference" in url:
            return _ok_resp(mocker, POLYGON_REF)
        if "api.polygon.io/v2/aggs" in url:
            return _ok_resp(mocker, POLYGON_PREV)
        if "api.tiingo.com/tiingo/daily" in url:
            return _ok_resp(mocker, TIINGO_DAILY)
        if "api.tiingo.com/iex" in url:
            return _ok_resp(mocker, TIINGO_IEX)
        return _404_resp(mocker)

    mocker.patch(
        "src.common.datasources.openbb_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.openbb_source.time.sleep")

    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert "fmp" not in providers
    assert "polygon" in providers
    assert "tiingo" in providers


def test_fetch_all_providers_http_error_records_error(db, mocker):
    _mock_all_keys_present(mocker)
    mocker.patch(
        "src.common.datasources.openbb_source.requests.get",
        return_value=_404_resp(mocker),
    )
    mocker.patch("src.common.datasources.openbb_source.time.sleep")
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n == 0
    w = db.get_watermark("openbb", "AAPL", "multi_provider")
    assert w is not None
    assert w["error_count"] >= 1


# --- polite-delay verification -----------------------------------------


def test_fetch_polygon_polite_delay_applied(db, mocker):
    """Polygon free tier = 5/min -> 12s between Polygon calls.
    `time.sleep` invoked with >= 12 between Polygon calls."""
    _mock_all_keys_present(mocker)

    def side_effect(url, **kwargs):
        if "api.polygon.io/v3/reference" in url:
            return _ok_resp(mocker, POLYGON_REF)
        if "api.polygon.io/v2/aggs" in url:
            return _ok_resp(mocker, POLYGON_PREV)
        # All other URLs return empty so only Polygon route fires.
        return _404_resp(mocker)

    mocker.patch(
        "src.common.datasources.openbb_source.requests.get",
        side_effect=side_effect,
    )
    sleep_spy = mocker.patch(
        "src.common.datasources.openbb_source.time.sleep",
    )
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    # At least one sleep call should be >= 12s (the Polygon inter-call delay).
    polygon_delays = [c.args[0] for c in sleep_spy.call_args_list if c.args and c.args[0] >= 12]
    assert polygon_delays, (
        "expected at least one polygon inter-call sleep >= 12s; "
        f"observed sleep delays: {[c.args for c in sleep_spy.call_args_list]}"
    )


# --- run_id propagation -----------------------------------------------


def test_fetch_threads_run_id_through_to_rows(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="my-run-42", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        run_ids = {
            r[0] for r in c.execute("SELECT DISTINCT run_id FROM raw_openbb")
        }
    assert run_ids == {"my-run-42"}


def test_fetch_uppercases_ticker(db, mocker):
    _mock_all_keys_present(mocker)
    _wire_url_dispatch_all_ok(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="aapl", run_id="r1", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        tickers = {
            r[0] for r in c.execute("SELECT DISTINCT ticker FROM raw_openbb")
        }
    assert tickers == {"AAPL"}
