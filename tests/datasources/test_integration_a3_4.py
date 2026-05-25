"""End-to-end orchestration test for A.3.4: ticker -> CIK -> submissions ->
parse -> Form 4 XML fetch -> parse -> CMP classify -> persist -> watermark advance ->
idempotent re-run.

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


FIXTURE_SUB = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "submissions_CIK0000320193.json"
FIXTURE_F4 = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "form4_sample.xml"
FIXTURE_CT = Path(__file__).resolve().parents[1] / "fixtures" / "edgar" / "company_tickers.json"


def _wire_mocks(mocker, ct_blob, sub_blob, form4_bytes):
    def side_effect(url, **kwargs):
        resp = mocker.MagicMock()
        resp.status_code = 200
        resp.ok = True
        if "company_tickers" in url:
            resp.json.return_value = ct_blob
            resp.content = json.dumps(ct_blob).encode()
        elif "submissions/CIK" in url:
            resp.json.return_value = sub_blob
            resp.content = json.dumps(sub_blob).encode()
        elif url.endswith(".xml"):
            resp.content = form4_bytes
        else:
            resp.ok = False
            resp.status_code = 404
            resp.content = b""
        return resp
    mocker.patch(
        "src.common.datasources.sec_cik_lookup.requests.get",
        side_effect=side_effect,
    )
    mocker.patch(
        "src.common.datasources.edgar_source.requests.get",
        side_effect=side_effect,
    )


def test_a3_4_full_flow_with_watermark_idempotency(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v6()

    ct_blob = json.loads(FIXTURE_CT.read_text())
    sub_blob = json.loads(FIXTURE_SUB.read_text())
    form4_bytes = FIXTURE_F4.read_bytes()
    _wire_mocks(mocker, ct_blob, sub_blob, form4_bytes)

    edgar = EdgarSource()

    n_filings_1, n_insider_1 = edgar.fetch_filings_for_ticker(
        "AAPL", run_id="run-1", db=db,
    )
    assert n_filings_1 > 0
    assert n_insider_1 > 0

    w = db.get_watermark("edgar", "AAPL", "filings")
    assert w is not None
    assert w["last_observation_date"] == "2026-03-17"
    assert w["fetch_count"] == 1
    assert w["error_count"] == 0

    with sqlite3.connect(db_path) as c:
        n_unclassified = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider WHERE is_opportunistic IS NULL"
        ).fetchone()[0]
    assert n_unclassified == 0

    with sqlite3.connect(db_path) as c:
        forms = sorted(
            r[0] for r in c.execute(
                "SELECT DISTINCT form_type FROM raw_edgar_filings WHERE ticker='AAPL'"
            )
        )
    assert "4" in forms
    assert "8-K" in forms
    assert "10-K" in forms

    with sqlite3.connect(db_path) as c:
        item_codes = c.execute(
            "SELECT item_codes FROM raw_edgar_filings WHERE ticker='AAPL' AND form_type='8-K'"
        ).fetchone()[0]
    assert item_codes == "2.02,9.01"

    # Second pass: short-circuit on watermark
    with sqlite3.connect(db_path) as c:
        n_filings_before = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_filings"
        ).fetchone()[0]
        n_insider_before = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider"
        ).fetchone()[0]

    n_filings_2, n_insider_2 = edgar.fetch_filings_for_ticker(
        "AAPL", run_id="run-1", db=db,
    )
    assert n_filings_2 == 0
    assert n_insider_2 == 0

    with sqlite3.connect(db_path) as c:
        n_filings_after = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_filings"
        ).fetchone()[0]
        n_insider_after = c.execute(
            "SELECT COUNT(*) FROM raw_edgar_insider"
        ).fetchone()[0]
    assert n_filings_after == n_filings_before
    assert n_insider_after == n_insider_before


def test_a3_4_unknown_ticker_records_failure_does_not_break(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v6()

    ct_blob = json.loads(FIXTURE_CT.read_text())
    sub_blob = json.loads(FIXTURE_SUB.read_text())
    form4_bytes = FIXTURE_F4.read_bytes()
    _wire_mocks(mocker, ct_blob, sub_blob, form4_bytes)

    n_filings, n_insider = EdgarSource().fetch_filings_for_ticker(
        "NOTREAL", run_id="r1", db=db,
    )
    assert (n_filings, n_insider) == (0, 0)
    w = db.get_watermark("edgar", "NOTREAL", "filings")
    assert w is not None
    assert w["error_count"] >= 1


def test_a3_4_classifier_version_stamped_on_every_row(tmp_path: Path, mocker):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path=str(db_path))
    db.migrate_to_v6()

    ct_blob = json.loads(FIXTURE_CT.read_text())
    sub_blob = json.loads(FIXTURE_SUB.read_text())
    form4_bytes = FIXTURE_F4.read_bytes()
    _wire_mocks(mocker, ct_blob, sub_blob, form4_bytes)

    EdgarSource().fetch_filings_for_ticker("AAPL", run_id="r1", db=db)
    with sqlite3.connect(db_path) as c:
        rows = c.execute(
            "SELECT DISTINCT opportunistic_classifier_version FROM raw_edgar_insider"
        ).fetchall()
    versions = {r[0] for r in rows}
    assert versions == {"1.0"}
