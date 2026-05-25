"""End-to-end orchestration test for A.3.5:
- FRED: pull 5 default macro series, persist to raw_fred, advance per-series
  watermarks, then re-run idempotently.
- FINRA: pull daily short-volume file, persist to raw_finra, advance the
  biweekly watermark, then re-run within 14d and verify the skip fires.

Unit-level integration with mocked HTTP. Real-endpoint integration is
deferred to A.5.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.fred_source import FredSource, DEFAULT_MACRO_SERIES
from src.common.datasources.finra_source import FinraSource


FRED_DGS10 = (
    Path(__file__).resolve().parents[1] / "fixtures" / "fred" / "DGS10_sample.csv"
).read_text()
FRED_VIXCLS = (
    Path(__file__).resolve().parents[1] / "fixtures" / "fred" / "VIXCLS_sample.csv"
).read_text()
FINRA_TXT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "finra" / "shvol_sample.txt"
).read_text()


# Stub responses for the 3 FRED series we don't have full CSVs for.
_STUB_CSVS = {
    "DGS3MO": "DATE,DGS3MO\n2026-05-12,5.10\n2026-05-13,5.11\n",
    "DGS2": "DATE,DGS2\n2026-05-12,4.50\n2026-05-13,4.52\n",
    "CPIAUCSL": "DATE,CPIAUCSL\n2026-04-01,312.45\n2026-05-01,313.10\n",
}


def _wire_fred_mock(mocker):
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "id=DGS10" in url:
            resp.text = FRED_DGS10
        elif "id=VIXCLS" in url:
            resp.text = FRED_VIXCLS
        else:
            for sid, csv in _STUB_CSVS.items():
                if f"id={sid}" in url:
                    resp.text = csv
                    break
            else:
                resp.ok = False
                resp.status_code = 404
                resp.text = ""
        resp.content = resp.text.encode()
        return resp
    mocker.patch(
        "src.common.datasources.fred_source.requests.get",
        side_effect=side_effect,
    )


def _wire_finra_mock(mocker, *, status: int = 200, txt: str | None = None):
    payload = FINRA_TXT if txt is None else txt

    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = status
        resp.ok = status == 200
        resp.text = payload
        resp.content = payload.encode()
        return resp
    mocker.patch(
        "src.common.datasources.finra_source.requests.get",
        side_effect=side_effect,
    )


def test_a3_5_fred_full_flow_with_idempotent_rerun(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v7()

    _wire_fred_mock(mocker)

    fred = FredSource()

    # First pull: all 5 default series.
    n1 = fred.fetch_macro_series(run_id="run-1", db=db)
    assert n1 > 0

    # Every default series has a watermark.
    for sid in DEFAULT_MACRO_SERIES:
        w = db.get_watermark("fred", "*", f"series:{sid}")
        assert w is not None, f"missing watermark for {sid}"
        assert w["fetch_count"] >= 1
        assert w["error_count"] == 0

    # Idempotent re-pull: zero new rows.
    with sqlite3.connect(db_path) as c:
        n_before = c.execute("SELECT COUNT(*) FROM raw_fred").fetchone()[0]
    n2 = fred.fetch_macro_series(run_id="run-1", db=db)
    assert n2 == 0
    with sqlite3.connect(db_path) as c:
        n_after = c.execute("SELECT COUNT(*) FROM raw_fred").fetchone()[0]
    assert n_after == n_before


def test_a3_5_finra_full_flow_with_14d_skip(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v7()

    _wire_finra_mock(mocker)

    finra = FinraSource()
    target = _dt.date(2026, 5, 15)

    # First pull: explicit settlement_date.
    n1 = finra.fetch_short_interest(
        run_id="run-1", settlement_date=target, db=db,
    )
    assert n1 == 20  # all rows in fixture

    w1 = db.get_watermark("finra", "*", "short_interest_biweekly")
    assert w1["last_observation_date"] == "2026-05-15"

    # Re-pull within 14 days of the watermark with no explicit date.
    # Should short-circuit (return 0) per §6.5.
    db.upsert_watermark(
        source="finra", ticker="*", field="short_interest_biweekly",
        last_observation_date=(_dt.date.today() - _dt.timedelta(days=3)).isoformat(),
        success=True,
    )
    n2 = finra.fetch_short_interest(run_id="run-1", db=db)
    assert n2 == 0

    # Explicit date bypasses the skip — but INSERT OR IGNORE still de-dupes.
    n3 = finra.fetch_short_interest(
        run_id="run-1", settlement_date=target, db=db,
    )
    assert n3 == 0  # rows already present from first call


def test_a3_5_finra_failure_does_not_break_fred(tmp_path: Path, mocker):
    """Sources are independent — a FINRA fetch error must not affect FRED."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v7()

    # Both fred_source and finra_source share the same `requests` module
    # object (Python module identity), so a single dispatch on URL substring
    # serves both sources correctly. Patching one module path is sufficient
    # because `requests.get` is the same callable everywhere.
    def combined_side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        if "fredgraph.csv" in url:
            resp.status_code = 200
            resp.ok = True
            if "id=DGS10" in url:
                resp.text = FRED_DGS10
            elif "id=VIXCLS" in url:
                resp.text = FRED_VIXCLS
            else:
                for sid, csv in _STUB_CSVS.items():
                    if f"id={sid}" in url:
                        resp.text = csv
                        break
                else:
                    resp.ok = False
                    resp.status_code = 404
                    resp.text = ""
            resp.content = resp.text.encode()
            return resp
        # Any non-FRED URL (e.g. cdn.finra.org) -> 404.
        resp.status_code = 404
        resp.ok = False
        resp.text = ""
        resp.content = b""
        return resp

    mocker.patch(
        "src.common.datasources.fred_source.requests.get",
        side_effect=combined_side_effect,
    )

    n_fred = FredSource().fetch_macro_series(run_id="r1", db=db)
    n_finra = FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )

    assert n_fred > 0
    assert n_finra == 0
    w_finra = db.get_watermark("finra", "*", "short_interest_biweekly")
    assert w_finra["error_count"] >= 1
    # FRED watermarks all healthy.
    for sid in DEFAULT_MACRO_SERIES:
        w = db.get_watermark("fred", "*", f"series:{sid}")
        assert w["error_count"] == 0
