"""End-to-end orchestration test for A.3.3: ticker list -> CIK lookup -> XBRL fetch
-> parser -> raw_edgar_fundamentals persistence -> watermark advance -> idempotent re-run.

Unit-level integration with mocked HTTP. A separate @pytest.mark.integration
test against the real SEC endpoint is deferred to A.5.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.edgar_source import EdgarSource


FIXTURE_CF = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "CIK0000320193.json"
FIXTURE_CT = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "company_tickers.json"


def _wire_mocks(mocker, ct_blob, cf_blob):
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.ok = True
        resp.status_code = 200
        if "company_tickers" in url:
            resp.json.return_value = ct_blob
        elif "companyfacts" in url:
            resp.json.return_value = cf_blob
        return resp
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        side_effect=side_effect,
    )
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )


def test_edgar_xbrl_full_flow_with_watermark_idempotency(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v5()

    ct_blob = json.loads(FIXTURE_CT.read_text())
    cf_blob = json.loads(FIXTURE_CF.read_text())
    _wire_mocks(mocker, ct_blob, cf_blob)

    edgar = EdgarSource()

    # First pass: fetch_universe over 2 tickers
    df1 = edgar.fetch_universe(
        run_id="run-1", ticker_list=["AAPL", "MSFT"], db=db,
    )
    assert not df1.empty
    assert sorted(df1["ticker"].unique().tolist()) == ["AAPL", "MSFT"]

    # Both tickers have a watermark
    for t in ["AAPL", "MSFT"]:
        w = db.get_watermark("edgar", t, "xbrl_fundamentals")
        assert w is not None
        assert w["last_observation_date"] == "2024-11-01"
        assert w["fetch_count"] == 1
        assert w["error_count"] == 0

    # Count rows in raw_edgar_fundamentals before re-run
    with sqlite3.connect(db_path) as c:
        n_before = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_fundamentals"
        ).fetchone()[0]
    assert n_before > 0

    # Second pass: same run_id, same tickers. The fetch is skipped (watermark
    # last_fetched_at == today), no new HTTP calls to companyfacts, and the
    # row count is unchanged (also guarded by INSERT OR IGNORE).
    df2 = edgar.fetch_universe(
        run_id="run-1", ticker_list=["AAPL", "MSFT"], db=db,
    )
    with sqlite3.connect(db_path) as c:
        n_after = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_fundamentals"
        ).fetchone()[0]
    assert n_after == n_before

    # Verify the parsed fy2023 annual matches expected values
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            """
            SELECT revenue_ttm, net_income_ttm, ebit_ttm, total_assets,
                   total_equity, operating_margin, net_profit_margin,
                   total_debt_to_equity, interest_coverage
            FROM raw_edgar_fundamentals
            WHERE ticker='AAPL' AND fiscal_year=2023 AND fiscal_period='annual'
            """
        ).fetchone()
    assert row is not None
    revenue, ni, oi, assets, equity, op_m, np_m, d2e, ic = row
    assert revenue == 383285000000.0
    assert ni == 96995000000.0
    assert oi == 114301000000.0
    assert assets == 352755000000.0
    assert equity == 62146000000.0
    assert op_m is not None
    assert abs(op_m - (oi / revenue)) < 1e-6
    assert np_m is not None
    assert abs(np_m - (ni / revenue)) < 1e-6
    assert d2e is not None
    assert d2e > 0
    assert ic is not None
    assert ic > 0


def test_edgar_xbrl_unknown_ticker_logged_as_error_does_not_break_batch(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v5()

    ct_blob = json.loads(FIXTURE_CT.read_text())
    cf_blob = json.loads(FIXTURE_CF.read_text())
    _wire_mocks(mocker, ct_blob, cf_blob)

    df = EdgarSource().fetch_universe(
        run_id="run-1", ticker_list=["AAPL", "NOTREAL", "GOOGL"], db=db,
    )
    assert "AAPL" in df["ticker"].unique().tolist()
    assert "GOOGL" in df["ticker"].unique().tolist()

    # NOTREAL has a failure watermark
    w = db.get_watermark("edgar", "NOTREAL", "xbrl_fundamentals")
    assert w is not None
    assert w["error_count"] >= 1
