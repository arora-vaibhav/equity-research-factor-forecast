"""Tests for FinraSource.fetch_short_interest (Phase A.3.5)."""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.finra_source import FinraSource


FIXTURE_TXT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "finra" / "shvol_sample.txt"
).read_text()


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v7()
    return mgr


def _wire_mock(mocker, txt: str, *, status: int = 200):
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = status
        resp.ok = status == 200
        resp.text = txt
        resp.content = txt.encode()
        return resp
    mocker.patch(
        "src.common.datasources.finra_source.requests.get",
        side_effect=side_effect,
    )


def test_fetch_short_interest_persists_rows(db, mocker):
    _wire_mock(mocker, FIXTURE_TXT)
    n = FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert n > 0
    with sqlite3.connect(db.db_path) as c:
        tickers = sorted(
            r[0] for r in c.execute("SELECT DISTINCT ticker FROM raw_finra")
        )
    assert "AAPL" in tickers
    assert "MSFT" in tickers


def test_fetch_short_interest_parses_correctly(db, mocker):
    _wire_mock(mocker, FIXTURE_TXT)
    FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT ticker, short_interest_shares, avg_daily_volume "
            "FROM raw_finra WHERE ticker='AAPL'"
        ).fetchall()
    assert len(rows) >= 1
    aapl = rows[0]
    assert aapl[0] == "AAPL"
    assert aapl[1] == 12_345_678.0
    # total_volume from fixture = 85_123_456; avg_daily_volume mirrors it
    assert aapl[2] == 85_123_456.0


def test_fetch_short_interest_computes_days_to_cover(db, mocker):
    """days_to_cover = short_interest_shares / avg_daily_volume."""
    _wire_mock(mocker, FIXTURE_TXT)
    FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        row = c.execute(
            "SELECT short_interest_shares, avg_daily_volume, days_to_cover "
            "FROM raw_finra WHERE ticker='AAPL'"
        ).fetchone()
    si, adv, dtc = row
    assert abs(dtc - si / adv) < 1e-6


def test_fetch_short_interest_records_exchange_marker(db, mocker):
    """Each row should carry the exchange discriminator from the file."""
    _wire_mock(mocker, FIXTURE_TXT)
    FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    with sqlite3.connect(db.db_path) as c:
        exchanges = {
            r[0] for r in c.execute("SELECT DISTINCT exchange FROM raw_finra")
        }
    # Default file_stem='FNSQ' maps to exchange 'NSDQ'.
    assert exchanges == {"NSDQ"}


def test_fetch_short_interest_updates_watermark(db, mocker):
    _wire_mock(mocker, FIXTURE_TXT)
    FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    w = db.get_watermark("finra", "*", "short_interest_biweekly")
    assert w is not None
    assert w["last_observation_date"] == "2026-05-15"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0


def test_fetch_short_interest_14d_skip(db, mocker):
    """Spec §6.5: refresh skipped within 14d of prior successful pull."""
    # Seed a watermark dated 7 days ago.
    recent = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
    db.upsert_watermark(
        source="finra", ticker="*", field="short_interest_biweekly",
        last_observation_date=recent, success=True,
    )
    _wire_mock(mocker, FIXTURE_TXT)
    n = FinraSource().fetch_short_interest(run_id="r1", db=db)
    assert n == 0  # short-circuited
    with sqlite3.connect(db.db_path) as c:
        rows = c.execute("SELECT COUNT(*) FROM raw_finra").fetchone()[0]
    assert rows == 0


def test_fetch_short_interest_14d_skip_overridable_by_explicit_settlement_date(db, mocker):
    """Caller-supplied settlement_date bypasses the 14d skip — explicit
    is louder than default."""
    recent = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
    db.upsert_watermark(
        source="finra", ticker="*", field="short_interest_biweekly",
        last_observation_date=recent, success=True,
    )
    _wire_mock(mocker, FIXTURE_TXT)
    n = FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert n > 0


def test_fetch_short_interest_file_not_found_records_error(db, mocker):
    _wire_mock(mocker, "", status=404)
    n = FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert n == 0
    w = db.get_watermark("finra", "*", "short_interest_biweekly")
    assert w is not None
    assert w["error_count"] >= 1


def test_fetch_short_interest_calls_correct_url(db, mocker):
    captured = []

    def side_effect(url, **kwargs):
        captured.append(url)
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.text = FIXTURE_TXT
        resp.content = FIXTURE_TXT.encode()
        return resp

    mocker.patch(
        "src.common.datasources.finra_source.requests.get",
        side_effect=side_effect,
    )
    FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert any("cdn.finra.org" in u for u in captured)
    assert any("20260515" in u for u in captured)
    assert any("shvol" in u.lower() for u in captured)


def test_fetch_short_interest_insert_or_ignore_protects_against_dup(db, mocker):
    """Second fetch on the same settlement_date inserts no new rows."""
    _wire_mock(mocker, FIXTURE_TXT)
    src = FinraSource()
    n1 = src.fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert n1 == 20  # all rows in fixture
    n2 = src.fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert n2 == 0
    with sqlite3.connect(db.db_path) as c:
        total = c.execute("SELECT COUNT(*) FROM raw_finra").fetchone()[0]
    assert total == 20


def test_fetch_short_interest_default_settlement_date_uses_today(db, mocker):
    """With no settlement_date and no recent watermark, the source picks
    today() as the target date and calls the corresponding URL."""
    captured = []

    def side_effect(url, **kwargs):
        captured.append(url)
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.text = FIXTURE_TXT
        resp.content = FIXTURE_TXT.encode()
        return resp

    mocker.patch(
        "src.common.datasources.finra_source.requests.get",
        side_effect=side_effect,
    )
    FinraSource().fetch_short_interest(run_id="r1", db=db)
    today_compact = _dt.date.today().strftime("%Y%m%d")
    assert any(today_compact in u for u in captured)


def test_fetch_short_interest_handles_blank_and_short_rows(db, mocker):
    """Blank lines, rows with too-few fields, and the header row are skipped."""
    junk = (
        "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
        "20260515|AAPL|12345678|0|85123456|Q\n"
        "\n"
        "20260515|BAD\n"
        "20260515|MSFT|7890123|100|45000000|Q\n"
    )
    _wire_mock(mocker, junk)
    n = FinraSource().fetch_short_interest(
        run_id="r1", settlement_date=_dt.date(2026, 5, 15), db=db,
    )
    assert n == 2  # AAPL + MSFT only
