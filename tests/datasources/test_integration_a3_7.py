"""End-to-end orchestration test for A.3.7:
- Full happy path: all 3 providers route, all endpoints OK, raw_openbb
  populated, watermark advanced clean.
- Idempotent re-pull within 24h: zero new rows.
- Partial failure: one provider 404s, the surviving two still write; the
  watermark stays in success state because at least one provider wrote.
- Key-missing matrix: a provider missing -> the other two still write.

All HTTP is mocked via a SINGLE side_effect on
`src.common.datasources.openbb_source.requests.get` (URL-dispatch
pattern, NOT one patch per URL). This pattern is the explicit lesson
from A.3.5 Wave 3 + A.3.6 Wave 2 — two `mocker.patch` calls competing
for the same target broke tests there.
"""
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


def _ok(mocker, text: str):
    resp = mocker.MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.text = text
    resp.content = text.encode()
    import json
    resp.json = lambda: json.loads(text)
    return resp


def _404(mocker):
    resp = mocker.MagicMock()
    resp.status_code = 404
    resp.ok = False
    resp.text = ""
    resp.content = b""
    resp.json = lambda: {}
    return resp


def _make_url_dispatch(
    mocker,
    *,
    tiingo_404: bool = False,
    polygon_404: bool = False,
    fmp_404: bool = False,
):
    """URL-dispatch mock — single side_effect routes by URL substring.

    Lesson from A.3.5/A.3.6: do NOT patch the same target twice with
    different side effects; the second patch wins and the first becomes
    dead code. One mock, one side_effect, branch inside.
    """
    def side_effect(url, **kwargs):
        # FMP
        if "financialmodelingprep.com" in url:
            if fmp_404:
                return _404(mocker)
            if "/stable/quote" in url:
                return _ok(mocker, FMP_QUOTE)
            if "/stable/profile" in url:
                return _ok(mocker, FMP_PROFILE)
            if "/stable/ratios-ttm" in url:
                return _ok(mocker, FMP_RATIOS_TTM)
            if "/stable/key-metrics-ttm" in url:
                return _ok(mocker, FMP_KEY_METRICS_TTM)
            return _404(mocker)
        # Polygon
        if "api.polygon.io" in url:
            if polygon_404:
                return _404(mocker)
            if "/v3/reference/tickers/" in url:
                return _ok(mocker, POLYGON_REF)
            if "/v2/aggs/ticker/" in url and "/prev" in url:
                return _ok(mocker, POLYGON_PREV)
            return _404(mocker)
        # Tiingo
        if "api.tiingo.com" in url:
            if tiingo_404:
                return _404(mocker)
            if "/tiingo/daily/" in url:
                return _ok(mocker, TIINGO_DAILY)
            if "/iex/" in url:
                return _ok(mocker, TIINGO_IEX)
            return _404(mocker)
        return _404(mocker)

    mocker.patch(
        "src.common.datasources.openbb_source.requests.get",
        side_effect=side_effect,
    )
    mocker.patch("src.common.datasources.openbb_source.time.sleep")


def _mock_all_keys_present(mocker):
    def fake_get_key(provider, *args, **kwargs):
        return {
            "fmp": "fmp-test-k",
            "polygon": "poly-test-k",
            "tiingo": "tiingo-test-k",
        }.get(provider)
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=fake_get_key,
    )


def test_a3_7_full_flow_writes_all_three_providers(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v9()

    _mock_all_keys_present(mocker)
    _make_url_dispatch(mocker)

    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="run-1", db=db,
    )
    assert n > 0

    with sqlite3.connect(db_path) as c:
        by_p = {
            r[0]: r[1] for r in c.execute(
                "SELECT provider_used, COUNT(*) FROM raw_openbb GROUP BY provider_used"
            )
        }
    assert set(by_p.keys()) == {"fmp", "polygon", "tiingo"}
    # Each provider should emit at least 3 fields.
    for p in ("fmp", "polygon", "tiingo"):
        assert by_p[p] >= 3, f"{p} produced only {by_p[p]} rows"

    w = db.get_watermark("openbb", "AAPL", "multi_provider")
    assert w is not None
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_a3_7_idempotent_rerun_within_24h_skips(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v9()

    _mock_all_keys_present(mocker)
    _make_url_dispatch(mocker)
    src = OpenBBSource()

    n1 = src.fetch_fundamentals_for_ticker(ticker="AAPL", run_id="run-1", db=db)
    assert n1 > 0

    # Re-pull immediately: the 24h skip fires, returning 0.
    n2 = src.fetch_fundamentals_for_ticker(ticker="AAPL", run_id="run-1", db=db)
    assert n2 == 0


def test_a3_7_partial_failure_tiingo_404(tmp_path: Path, mocker):
    """Tiingo 404; FMP + Polygon survive. The watermark stays in success
    state because at least one provider wrote; total rows reflect only
    the surviving providers."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v9()

    _mock_all_keys_present(mocker)
    _make_url_dispatch(mocker, tiingo_404=True)

    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="run-1", db=db,
    )
    assert n > 0

    with sqlite3.connect(db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert providers == ["fmp", "polygon"]

    w = db.get_watermark("openbb", "AAPL", "multi_provider")
    assert w is not None
    # At least one provider succeeded so success path fires; error_count == 0.
    assert w["error_count"] == 0


def test_a3_7_key_missing_polygon_falls_back(tmp_path: Path, mocker):
    """Polygon key absent; FMP + Tiingo still write. Source returns success;
    watermark records no error."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v9()

    def fake_get_key(provider, *args, **kwargs):
        return {"fmp": "f", "tiingo": "t"}.get(provider)
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=fake_get_key,
    )
    _make_url_dispatch(mocker)

    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="run-1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db_path) as c:
        providers = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT provider_used FROM raw_openbb"
            )
        )
    assert providers == ["fmp", "tiingo"]


def test_a3_7_long_format_admits_market_cap_from_fmp_and_polygon(
    tmp_path: Path, mocker,
):
    """`market_cap` is emitted by BOTH FMP (key-metrics-ttm.marketCapTTM
    OR quote.marketCap) and Polygon (reference.results.market_cap). The
    long-format PK admits both — this is the cross-vendor cross-validation
    substrate the spec calls for."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v9()

    _mock_all_keys_present(mocker)
    _make_url_dispatch(mocker)

    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="run-1", db=db,
    )

    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT provider_used, value FROM raw_openbb "
            "WHERE ticker='AAPL' AND field_name='market_cap' "
            "ORDER BY provider_used"
        ).fetchall()
    providers = {r[0] for r in rows}
    assert "fmp" in providers
    assert "polygon" in providers
    # Two distinct values from two providers — not collapsed.
    assert len(rows) >= 2


def test_a3_7_fresh_run_id_appends_new_snapshot(tmp_path: Path, mocker):
    """A fresh run_id is the supported way to refresh — every row written
    under the new run_id is a distinct observation in raw_openbb."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v9()

    _mock_all_keys_present(mocker)
    _make_url_dispatch(mocker)
    src = OpenBBSource()

    src.fetch_fundamentals_for_ticker(ticker="AAPL", run_id="run-1", db=db)

    # Backdate the watermark so the 24h skip doesn't fire on second call.
    # We rely on refresh_after_hours=0 to bypass the recency check.
    src.fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="run-2", db=db, refresh_after_hours=0,
    )

    with sqlite3.connect(db_path) as c:
        run_ids = sorted(
            r[0] for r in c.execute("SELECT DISTINCT run_id FROM raw_openbb")
        )
    assert run_ids == ["run-1", "run-2"]
