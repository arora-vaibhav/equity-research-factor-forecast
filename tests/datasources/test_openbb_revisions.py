"""Tests for OpenBBSource analyst-estimates route (Phase A.3.8).

Verifies that FMP /stable/analyst-estimates is called, mapped to the three
revisions_velocity.py field names, and stored in raw_openbb.  Polygon and
Tiingo are confirmed to NOT emit revision rows.

Caller: pytest.  Mocks requests.get + get_api_key; no live network.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.openbb_source import OpenBBSource, _map_fmp_analyst


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "openbb"
FMP_PROFILE          = (FIXTURE_DIR / "fmp_profile_AAPL.json").read_text(encoding="utf-8")
FMP_QUOTE            = (FIXTURE_DIR / "fmp_quote_AAPL.json").read_text(encoding="utf-8")
FMP_RATIOS_TTM       = (FIXTURE_DIR / "fmp_ratios_ttm_AAPL.json").read_text(encoding="utf-8")
FMP_KEY_METRICS_TTM  = (FIXTURE_DIR / "fmp_key_metrics_ttm_AAPL.json").read_text(encoding="utf-8")
FMP_ANALYST_ESTIMATES = (
    FIXTURE_DIR / "fmp_analyst_estimates_AAPL.json"
).read_text(encoding="utf-8")


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v9()
    return mgr


def _ok(mocker, text: str):
    resp = mocker.MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.text = text
    resp.content = text.encode()
    resp.json = lambda: json.loads(text)
    return resp


def _non_200(mocker, status: int = 429):
    resp = mocker.MagicMock()
    resp.status_code = status
    resp.ok = False
    resp.text = ""
    resp.content = b""
    resp.json = lambda: {}
    return resp


def _wire_fmp_only(mocker, *, analyst_estimates_text: str | None = FMP_ANALYST_ESTIMATES,
                   analyst_status: int = 200):
    """Patch requests.get to return valid FMP responses (all five endpoints)
    and 404 for Polygon + Tiingo, so only FMP rows land in the DB."""
    def side_effect(url, **kwargs):
        if "financialmodelingprep.com/stable/profile" in url:
            return _ok(mocker, FMP_PROFILE)
        if "financialmodelingprep.com/stable/quote" in url:
            return _ok(mocker, FMP_QUOTE)
        if "financialmodelingprep.com/stable/ratios-ttm" in url:
            return _ok(mocker, FMP_RATIOS_TTM)
        if "financialmodelingprep.com/stable/key-metrics-ttm" in url:
            return _ok(mocker, FMP_KEY_METRICS_TTM)
        if "financialmodelingprep.com/stable/analyst-estimates" in url:
            if analyst_status != 200:
                return _non_200(mocker, analyst_status)
            if analyst_estimates_text is None:
                return _non_200(mocker, 404)
            return _ok(mocker, analyst_estimates_text)
        # Polygon + Tiingo return 404 → empty rows.
        return _non_200(mocker, 404)

    mocker.patch("src.common.datasources.openbb_source.requests.get",
                 side_effect=side_effect)
    mocker.patch("src.common.datasources.openbb_source.time.sleep")


def _mock_fmp_key_only(mocker):
    """Only FMP key available; Polygon + Tiingo return None."""
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        side_effect=lambda p, *a, **k: "fmp-test-key" if p == "fmp" else None,
    )


# ---------------------------------------------------------------------------
# Unit test: _map_fmp_analyst pure function
# ---------------------------------------------------------------------------

def test_map_fmp_analyst_two_entries_produces_three_tuples():
    """Two estimate entries → eps_current, eps_30d_ago, revision_direction_count."""
    estimates = json.loads(FMP_ANALYST_ESTIMATES)
    tuples = _map_fmp_analyst(estimates)
    field_names = [t[0] for t in tuples]
    assert "eps_estimate_current" in field_names
    assert "eps_estimate_30d_ago" in field_names
    assert "eps_revision_direction_count" in field_names


def test_map_fmp_analyst_values_match_fixture():
    estimates = json.loads(FMP_ANALYST_ESTIMATES)
    tuples = _map_fmp_analyst(estimates)
    by_field = {t[0]: t[1] for t in tuples}
    # estimatedEpsAvg from entry[0] = 1.55
    assert abs(by_field["eps_estimate_current"] - 1.55) < 1e-9
    # estimatedEpsAvg from entry[1] = 1.42
    assert abs(by_field["eps_estimate_30d_ago"] - 1.42) < 1e-9
    # numAnalystsRevisionUp(8) - numAnalystsRevisionDown(2) = 6
    assert abs(by_field["eps_revision_direction_count"] - 6.0) < 1e-9


def test_map_fmp_analyst_none_returns_empty():
    assert _map_fmp_analyst(None) == []


def test_map_fmp_analyst_empty_list_returns_empty():
    assert _map_fmp_analyst([]) == []


def test_map_fmp_analyst_single_entry_no_30d_ago():
    """Only one estimate entry → no eps_estimate_30d_ago tuple."""
    single = json.loads(FMP_ANALYST_ESTIMATES)[:1]
    tuples = _map_fmp_analyst(single)
    field_names = [t[0] for t in tuples]
    assert "eps_estimate_current" in field_names
    assert "eps_revision_direction_count" in field_names
    assert "eps_estimate_30d_ago" not in field_names


# ---------------------------------------------------------------------------
# Integration tests: fetch_fundamentals_for_ticker writes revision rows
# ---------------------------------------------------------------------------

def test_revision_rows_emitted_from_fmp(db, mocker):
    """fetch_fundamentals_for_ticker writes the three revision fields."""
    _mock_fmp_key_only(mocker)
    _wire_fmp_only(mocker)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT field_name, value FROM raw_openbb "
            "WHERE ticker='AAPL' AND provider_used='fmp' "
            "AND field_name IN ('eps_estimate_current', 'eps_estimate_30d_ago',"
            "                   'eps_revision_direction_count')"
        ).fetchall()
    by_field = {r[0]: r[1] for r in rows}
    assert "eps_estimate_current" in by_field
    assert abs(by_field["eps_estimate_current"] - 1.55) < 1e-9
    assert "eps_estimate_30d_ago" in by_field
    assert abs(by_field["eps_estimate_30d_ago"] - 1.42) < 1e-9
    assert "eps_revision_direction_count" in by_field
    assert abs(by_field["eps_revision_direction_count"] - 6.0) < 1e-9


def test_polygon_and_tiingo_do_not_emit_revision_rows(db, mocker):
    """Revision fields must only appear under provider_used='fmp'."""
    _mock_fmp_key_only(mocker)
    _wire_fmp_only(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        non_fmp_revision = c.execute(
            "SELECT COUNT(*) FROM raw_openbb "
            "WHERE field_name IN ('eps_estimate_current', 'eps_estimate_30d_ago',"
            "                     'eps_revision_direction_count')"
            "  AND provider_used != 'fmp'"
        ).fetchone()[0]
    assert non_fmp_revision == 0


def test_missing_fmp_key_produces_no_revision_rows(db, mocker):
    """If FMP key is absent, zero revision rows are written."""
    mocker.patch(
        "src.common.datasources.openbb_source.get_api_key",
        return_value=None,
    )
    mocker.patch("src.common.datasources.openbb_source.requests.get")
    mocker.patch("src.common.datasources.openbb_source.time.sleep")
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    assert n == 0
    with sqlite3.connect(db.db_path) as c:
        cnt = c.execute(
            "SELECT COUNT(*) FROM raw_openbb "
            "WHERE field_name='eps_estimate_current'"
        ).fetchone()[0]
    assert cnt == 0


def test_fmp_analyst_estimates_429_produces_no_revision_rows(db, mocker):
    """A 429 from the analyst-estimates endpoint is silently skipped."""
    _mock_fmp_key_only(mocker)
    _wire_fmp_only(mocker, analyst_status=429)
    n = OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    # Other FMP rows (pe_ratio etc.) still land, so n > 0.
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        cnt = c.execute(
            "SELECT COUNT(*) FROM raw_openbb "
            "WHERE field_name='eps_estimate_current'"
        ).fetchone()[0]
    assert cnt == 0


def test_existing_fundamentals_still_produced_regression(db, mocker):
    """Adding the analyst-estimates route must not drop existing fields."""
    _mock_fmp_key_only(mocker)
    _wire_fmp_only(mocker)
    OpenBBSource().fetch_fundamentals_for_ticker(
        ticker="AAPL", run_id="r1", db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        existing_fields = {
            r[0] for r in c.execute(
                "SELECT DISTINCT field_name FROM raw_openbb "
                "WHERE provider_used='fmp'"
            )
        }
    # Core fundamentals from the original A.3.7 FMP route must still be present.
    for expected in ("pe_ratio", "ps_ratio", "pb_ratio", "beta"):
        assert expected in existing_fields, f"missing field: {expected}"
