"""Tests for GdeltSource.fetch_news_volume (Phase A.3.8).

Mocks requests.get and time.sleep so no live GDELT requests are made.
Uses tests/fixtures/gdelt/ for the synthetic GKG file + masterfilelist.

Caller: pytest.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.gdelt_source import GdeltSource


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "gdelt"
MASTERLIST_TEXT = (FIXTURE_DIR / "sample_masterfilelist.txt").read_text(encoding="utf-8")
GKG_BYTES = (FIXTURE_DIR / "sample_gkg.csv.gz").read_bytes()


@pytest.fixture
def alias_path(tmp_path: Path) -> Path:
    """Write a small alias YAML to a temp dir for test isolation."""
    p = tmp_path / "gdelt_alias.yaml"
    p.write_text(
        "aliases:\n"
        "  AAPL:\n"
        "    - 'Apple Inc'\n"
        "    - 'Apple Computer'\n"
        "  MSFT:\n"
        "    - 'Microsoft Corporation'\n"
        "    - 'Microsoft Corp'\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v11()
    return mgr


def _ok(mocker, content, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = status < 400
    if isinstance(content, str):
        resp.text = content
        resp.content = content.encode()
    else:
        resp.text = ""
        resp.content = content
    return resp


def _wire_requests(mocker, *, masterlist_text=MASTERLIST_TEXT, gkg_bytes=GKG_BYTES,
                   masterlist_status=200, gkg_status=200):
    """Patch requests.get and .head to serve the fixtures."""
    def get_side_effect(url, **kwargs):
        if "masterfilelist" in url:
            return _ok(mocker, masterlist_text, status=masterlist_status)
        if masterlist_status == 200 and gkg_status == 200:
            return _ok(mocker, gkg_bytes, status=gkg_status)
        return _ok(mocker, b"", status=gkg_status)

    def head_side_effect(url, **kwargs):
        return _ok(mocker, b"", status=200)

    mocker.patch("src.common.datasources.gdelt_source.requests.get",
                 side_effect=get_side_effect)
    mocker.patch("src.common.datasources.gdelt_source.requests.head",
                 side_effect=head_side_effect)
    mocker.patch("src.common.datasources.gdelt_source.time.sleep")


# ---------------------------------------------------------------------------
# _load_alias_table
# ---------------------------------------------------------------------------

def test_load_alias_table_returns_dict(alias_path):
    src = GdeltSource(alias_path=alias_path)
    assert "AAPL" in src._alias_table
    assert "Apple Inc" in src._alias_table["AAPL"]


def test_load_alias_table_missing_file_returns_empty(tmp_path):
    src = GdeltSource(alias_path=tmp_path / "nonexistent.yaml")
    assert src._alias_table == {}


# ---------------------------------------------------------------------------
# _parse_masterfilelist
# ---------------------------------------------------------------------------

def test_parse_masterfilelist_returns_gkg_urls():
    urls = GdeltSource._parse_masterfilelist(MASTERLIST_TEXT, cutoff=0)
    assert len(urls) == 1
    assert ".gkg." in urls[0]


def test_parse_masterfilelist_respects_cutoff():
    # cutoff = very far future -> nothing qualifies
    urls = GdeltSource._parse_masterfilelist(
        MASTERLIST_TEXT, cutoff=float("inf"),
    )
    assert urls == []


def test_parse_masterfilelist_empty_text():
    assert GdeltSource._parse_masterfilelist("", cutoff=0) == []


# ---------------------------------------------------------------------------
# Cashtag and alias matching
# ---------------------------------------------------------------------------

def test_cashtag_matched_row_writes_cashtag_method(db, mocker, alias_path):
    src = GdeltSource(alias_path=alias_path)
    _wire_requests(mocker)

    n = src.fetch_news_volume("AAPL", "r1", db, lookback_hours=999999)
    assert n > 0

    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT match_method FROM raw_gdelt "
            "WHERE ticker='AAPL' AND match_method='cashtag'"
        ).fetchall()
    assert len(rows) > 0


def test_alias_matched_row_writes_alias_method(db, mocker, alias_path):
    src = GdeltSource(alias_path=alias_path)
    _wire_requests(mocker)

    n = src.fetch_news_volume("AAPL", "r1", db, lookback_hours=999999)
    assert n > 0

    with sqlite3.connect(db.db_path) as c:
        rows = c.execute(
            "SELECT match_method FROM raw_gdelt "
            "WHERE ticker='AAPL' AND match_method='alias'"
        ).fetchall()
    assert len(rows) > 0


def test_unknown_ticker_emits_zero_rows(db, mocker, alias_path):
    """A ticker not in the alias table and no cashtag match -> 0 rows."""
    src = GdeltSource(alias_path=alias_path)
    _wire_requests(mocker)

    n = src.fetch_news_volume("ZZZZ", "r1", db, lookback_hours=999999)
    assert n == 0

    with sqlite3.connect(db.db_path) as c:
        cnt = c.execute(
            "SELECT COUNT(*) FROM raw_gdelt WHERE ticker='ZZZZ'"
        ).fetchone()[0]
    assert cnt == 0


# ---------------------------------------------------------------------------
# INSERT OR IGNORE idempotence
# ---------------------------------------------------------------------------

def test_insert_or_ignore_second_pull_returns_zero_new_rows(db, mocker, alias_path):
    """Second fetch for same ticker returns 0 net-new rows (PK collision)."""
    src = GdeltSource(alias_path=alias_path)
    _wire_requests(mocker)

    n1 = src.fetch_news_volume("AAPL", "r1", db, lookback_hours=999999)
    assert n1 > 0

    # Re-wire for second call
    _wire_requests(mocker)
    n2 = src.fetch_news_volume("AAPL", "r1", db, lookback_hours=999999)
    # INSERT OR IGNORE: same (gkg_record_id, ticker) -> 0 new rows
    assert n2 == 0


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

def test_watermark_advances_after_successful_pull(db, mocker, alias_path):
    src = GdeltSource(alias_path=alias_path)
    _wire_requests(mocker)

    src.fetch_news_volume("AAPL", "r1", db, lookback_hours=999999)
    w = db.get_watermark(src.name, "AAPL", "news_volume")
    assert w is not None
    assert w["last_observation_date"] is not None


def test_watermark_error_count_increments_on_masterlist_failure(db, mocker, alias_path):
    """HTTP error on masterfilelist -> error_count updated."""
    src = GdeltSource(alias_path=alias_path)
    _wire_requests(mocker, masterlist_status=500)

    n = src.fetch_news_volume("AAPL", "r1", db, lookback_hours=999999)
    assert n == 0

    w = db.get_watermark(src.name, "AAPL", "news_volume")
    assert w is not None
    assert w["error_count"] >= 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_masterlist_returns_zero(db, mocker, alias_path):
    src = GdeltSource(alias_path=alias_path)
    _wire_requests(mocker, masterlist_text="")

    n = src.fetch_news_volume("AAPL", "r1", db, lookback_hours=999999)
    assert n == 0


def test_health_check_ok_on_200_head(mocker, alias_path):
    src = GdeltSource(alias_path=alias_path)
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    mocker.patch(
        "src.common.datasources.gdelt_source.requests.head", return_value=resp,
    )
    status = src.health_check()
    assert status.status.value == "ok"
