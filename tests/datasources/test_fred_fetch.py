"""Tests for FredSource.fetch_macro_series (Phase A.3.5)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.fred_source import FredSource


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fred"
DGS10_CSV = (FIXTURE_DIR / "DGS10_sample.csv").read_text()
VIXCLS_CSV = (FIXTURE_DIR / "VIXCLS_sample.csv").read_text()


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v7()
    return mgr


def _make_csv_mock(mocker, csv_by_series: dict[str, str]):
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        # URL form: https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>
        for series_id, csv_text in csv_by_series.items():
            if f"id={series_id}" in url:
                resp.text = csv_text
                resp.content = csv_text.encode()
                return resp
        resp.ok = False
        resp.status_code = 404
        resp.text = ""
        resp.content = b""
        return resp
    mocker.patch(
        "src.common.datasources.fred_source.requests.get",
        side_effect=side_effect,
    )


def test_fetch_macro_series_default_series_list(db, mocker):
    csvs = {
        "DGS10": DGS10_CSV,
        "DGS3MO": "DATE,DGS3MO\n2026-05-12,5.10\n",
        "DGS2": "DATE,DGS2\n2026-05-12,4.50\n",
        "VIXCLS": VIXCLS_CSV,
        "CPIAUCSL": "DATE,CPIAUCSL\n2026-04-01,312.45\n",
    }
    _make_csv_mock(mocker, csvs)
    n = FredSource().fetch_macro_series(run_id="r1", db=db)
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        series = sorted(
            r[0] for r in c.execute("SELECT DISTINCT series_id FROM raw_fred")
        )
    assert series == ["CPIAUCSL", "DGS10", "DGS2", "DGS3MO", "VIXCLS"]


def test_fetch_macro_series_custom_series_list(db, mocker):
    _make_csv_mock(mocker, {"DGS10": DGS10_CSV})
    n = FredSource().fetch_macro_series(
        run_id="r1", series_ids=["DGS10"], db=db,
    )
    assert n == 30  # rows in DGS10_sample.csv
    with sqlite3.connect(db.db_path) as c:
        s = c.execute("SELECT DISTINCT series_id FROM raw_fred").fetchall()
    assert [r[0] for r in s] == ["DGS10"]


def test_fetch_macro_series_parses_values_correctly(db, mocker):
    _make_csv_mock(mocker, {"DGS10": DGS10_CSV})
    FredSource().fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    with sqlite3.connect(db.db_path) as c:
        row = c.execute(
            "SELECT observation_date, value FROM raw_fred "
            "WHERE series_id='DGS10' AND observation_date='2026-04-01'"
        ).fetchone()
    assert row == ("2026-04-01", 4.18)


def test_fetch_macro_series_handles_dot_missing_value(db, mocker):
    """FRED '.' sentinel must map to NULL, not raise."""
    _make_csv_mock(mocker, {"VIXCLS": VIXCLS_CSV})
    n = FredSource().fetch_macro_series(run_id="r1", series_ids=["VIXCLS"], db=db)
    assert n == 5
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT observation_date, value FROM raw_fred "
            "WHERE series_id='VIXCLS' ORDER BY observation_date"
        ).fetchall()
    assert rows[0] == ("2026-01-01", None)
    assert rows[1] == ("2026-01-02", 14.21)
    assert rows[3] == ("2026-01-06", None)


def test_fetch_macro_series_updates_watermark_per_series(db, mocker):
    _make_csv_mock(mocker, {"DGS10": DGS10_CSV})
    FredSource().fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    w = db.get_watermark("fred", "*", "series:DGS10")
    assert w is not None
    # Latest observation date in the fixture.
    assert w["last_observation_date"] == "2026-05-12"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_fetch_macro_series_failed_series_records_error(db, mocker):
    """One series 404s; others still succeed; failed watermark has error_count >= 1."""
    _make_csv_mock(mocker, {"DGS10": DGS10_CSV})  # only DGS10 served
    n = FredSource().fetch_macro_series(
        run_id="r1", series_ids=["DGS10", "BOGUS"], db=db,
    )
    assert n == 30  # only DGS10 rows inserted
    w_ok = db.get_watermark("fred", "*", "series:DGS10")
    w_err = db.get_watermark("fred", "*", "series:BOGUS")
    assert w_ok["error_count"] == 0
    assert w_err is not None
    assert w_err["error_count"] >= 1


def test_fetch_macro_series_insert_or_ignore_protects_against_dup(db, mocker):
    """Second run with identical fixture inserts zero new rows."""
    _make_csv_mock(mocker, {"DGS10": DGS10_CSV})
    src = FredSource()
    n1 = src.fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    assert n1 == 30
    # Clear watermark so the second pass actually re-fetches the CSV.
    db.upsert_watermark(
        source="fred", ticker="*", field="series:DGS10",
        last_observation_date=None, success=True,
    )
    n2 = src.fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    assert n2 == 0
    with sqlite3.connect(db.db_path) as c:
        total = c.execute(
            "SELECT COUNT(*) FROM raw_fred WHERE series_id='DGS10'"
        ).fetchone()[0]
    assert total == 30


def test_fetch_macro_series_calls_correct_url(db, mocker):
    captured = []

    def side_effect(url, **kwargs):
        captured.append(url)
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.text = DGS10_CSV
        resp.content = DGS10_CSV.encode()
        return resp

    mocker.patch(
        "src.common.datasources.fred_source.requests.get",
        side_effect=side_effect,
    )
    FredSource().fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    assert any("fredgraph.csv?id=DGS10" in u for u in captured)


def test_fetch_macro_series_empty_db_when_no_series_provided_uses_defaults(db, mocker):
    """series_ids=None means use the default 5-series macro list."""
    _make_csv_mock(mocker, {
        "DGS10": "DATE,DGS10\n2026-05-12,4.17\n",
        "DGS3MO": "DATE,DGS3MO\n2026-05-12,5.10\n",
        "DGS2": "DATE,DGS2\n2026-05-12,4.50\n",
        "VIXCLS": "DATE,VIXCLS\n2026-05-12,14.0\n",
        "CPIAUCSL": "DATE,CPIAUCSL\n2026-04-01,312.45\n",
    })
    n = FredSource().fetch_macro_series(run_id="r1", db=db)
    assert n == 5


def test_fetch_macro_series_skips_malformed_rows(db, mocker):
    """Rows that fail integer/float parsing or have bad shape are dropped."""
    csv_with_junk = (
        "DATE,DGS10\n"
        "2026-05-12,4.17\n"
        "garbage row\n"
        ",4.18\n"  # blank date
        "2026-05-13,not-a-float\n"
        "2026-05-14,4.16\n"
    )
    _make_csv_mock(mocker, {"DGS10": csv_with_junk})
    n = FredSource().fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    # Only 2026-05-12, 2026-05-14 should parse cleanly with non-null values.
    # The 'not-a-float' row stores observation_date with value=None (treated as missing).
    assert n >= 2
    with sqlite3.connect(db.db_path) as c:
        dates = sorted(
            r[0] for r in c.execute(
                "SELECT observation_date FROM raw_fred WHERE series_id='DGS10'"
            )
        )
    assert "2026-05-12" in dates
    assert "2026-05-14" in dates


def test_fetch_macro_series_watermark_present_still_refetches(db, mocker):
    """FRED is cheap; we always re-pull the full series even when watermark
    is up-to-date. INSERT OR IGNORE handles dedup."""
    db.upsert_watermark(
        source="fred", ticker="*", field="series:DGS10",
        last_observation_date="2026-05-12", success=True,
    )
    _make_csv_mock(mocker, {"DGS10": DGS10_CSV})
    n = FredSource().fetch_macro_series(run_id="r1", series_ids=["DGS10"], db=db)
    assert n == 30
