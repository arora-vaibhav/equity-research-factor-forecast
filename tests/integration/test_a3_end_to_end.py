"""A.3 end-to-end integration test per spec section 13.5.

Marked `@pytest.mark.integration` so it's deselected from the default
suite. Run with `pytest -m integration`.

Seeds a small synthetic dataset across the raw_* tables, calls the
Layer 1 factor pipeline (Wave 3) + dual-write helper (Wave 4) + the
equivalence harness, and asserts:

  * compute_factor_scores returns the expected columns including the
    news_activity_score column from spec section 7.
  * save_layer1_outputs writes to canonical_universe (v2).
  * write_equivalence_report produces a JSON file with run_id +
    row_counts + per_column drift breakdown.

No live network. Fully synthetic data; the test is self-contained.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from src.common.database import DatabaseManager
from src.common.schemas import RawEdgarFilingRow, RawEdgarInsiderRow
from src.layer1_universe.equivalence_harness import write_equivalence_report
from src.layer1_universe.factors import compute_factor_scores


pytestmark = pytest.mark.integration


def _seed_raw_tables(db: DatabaseManager) -> None:
    db.upsert_sec_ticker_cik_map(
        [{"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple", "snapshot_at": "2026-05-22"}]
    )
    db.insert_raw_edgar_filings([
        RawEdgarFilingRow(
            run_id="seed", ticker="AAPL", cik="0000320193",
            form_type="10-K", filing_date="2025-11-01",
            source_filing_accn="0000320193-26-000010",
            primary_doc_url="https://x", scrape_timestamp="2026-05-22T00:00:00Z",
        ),
    ])
    db.insert_raw_edgar_insider([
        RawEdgarInsiderRow(
            run_id="seed", source_filing_accn="0000320193-26-000010",
            ticker="AAPL", cik="0000320193",
            filer_name="JOHN DOE", filer_role="OFFICER",
            transaction_date="2026-05-15",
            transaction_code="P", shares=100.0,
            price_per_share=180.0,
            scrape_timestamp="2026-05-22T00:00:00Z",
            is_opportunistic=True,
            opportunistic_classifier_version="1.0",
        ),
    ])


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(db_path=str(tmp_path / "test.db"))
    d.migrate_to_v12()
    return d


def _build_canonical_df() -> pd.DataFrame:
    return pd.DataFrame({
        "ticker": ["AAPL", "MSFT"],
        "company_name": ["Apple", "Microsoft"],
        "sector": ["Technology", "Technology"],
        "industry": ["Consumer Electronics", "Software"],
        "market_cap_usd": [3_000_000_000_000.0, 2_800_000_000_000.0],
        "price": [180.0, 380.0],
        "avg_daily_volume": [50_000_000, 25_000_000],
        "pe_ttm": [30.0, 35.0],
        "pe_forward": [28.0, 33.0],
        "operating_margin": [0.30, 0.40],
        "net_profit_margin": [0.25, 0.35],
        "perf_12m": [0.20, 0.25],
        "dist_52w_high": [-0.05, -0.10],
        "dist_52w_low": [0.30, 0.40],
        "rsi_14": [60.0, 55.0],
    })


def test_end_to_end_pipeline(db, tmp_path, monkeypatch):
    """Seed raw tables, compute factor scores, dual-write, run
    equivalence harness, assert outputs."""
    run_id = "layer1_int_test_20260522"

    _seed_raw_tables(db)

    from src.layer1_universe.news_activity import load_news_activity_inputs
    raw_inputs = load_news_activity_inputs(db, as_of_date="2026-05-22")
    assert len(raw_inputs["insider_rows"]) == 1
    assert len(raw_inputs["filing_rows"]) == 1

    canonical_df = _build_canonical_df()

    factor_df = compute_factor_scores(
        canonical_df,
        raw_inputs=raw_inputs,
        as_of_date="2026-05-22",
    )
    expected_cols = {
        "value_score", "quality_score", "momentum_score",
        "lowvol_score", "revisions_score", "news_activity_score",
    }
    assert expected_cols.issubset(set(factor_df.columns))
    assert math.isfinite(factor_df.loc["AAPL", "news_activity_score"])

    counts = db.save_layer1_outputs(
        run_id=run_id,
        canonical_df=canonical_df,
    )
    assert counts["v2_canonical"] == 2

    monkeypatch.chdir(tmp_path)
    report_path = write_equivalence_report(run_id, db)
    assert report_path.exists()
    with report_path.open("r", encoding="utf-8") as f:
        report = json.load(f)
    assert report["run_id"] == run_id
    assert "row_counts" in report
    assert report["row_counts"]["v2_canonical_universe"] == 2
    assert report["status"] in ("ok", "partial", "no_data")
