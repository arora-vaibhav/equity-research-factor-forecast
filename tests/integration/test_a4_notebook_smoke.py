"""A.4 notebook smoke test.

Mirrors what `notebooks/layer1_v2_drilldown.ipynb` does without booting
a Jupyter kernel: seeds fixture data across raw_* tables, calls each
panel function, asserts non-empty output for the seeded sources +
empty-but-no-exception output for the rest.

Marked `@pytest.mark.integration` because it's the end-to-end
acceptance test per build plan A.4 acceptance criterion ("Notebook
runs end-to-end against fixture data").
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.common.database import DatabaseManager
from src.common.schemas import (
    RawEdgarFilingRow,
    RawEdgarInsiderRow,
)
from src.layer1_universe.notebook_panels import (
    apply_notebook_overrides,
    per_ticker_drilldown,
    provenance_summary,
    source_status,
)


pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_db(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(db_path=str(tmp_path / "test.db"))
    db.migrate_to_v13()

    db.upsert_sec_ticker_cik_map([
        {"ticker": "AAPL", "cik": "0000320193", "company_name": "Apple", "snapshot_at": "2026-05-22"},
        {"ticker": "MSFT", "cik": "0000789019", "company_name": "Microsoft", "snapshot_at": "2026-05-22"},
    ])

    db.insert_raw_edgar_filings([
        RawEdgarFilingRow(
            run_id="r1", ticker="AAPL", cik="0000320193",
            form_type="10-K", filing_date="2025-11-01",
            source_filing_accn="0000320193-26-000010",
            primary_doc_url="https://x", scrape_timestamp="2026-05-22T00:00:00Z",
        ),
        RawEdgarFilingRow(
            run_id="r1", ticker="MSFT", cik="0000789019",
            form_type="10-K", filing_date="2025-10-01",
            source_filing_accn="0000789019-26-000001",
            primary_doc_url="https://x", scrape_timestamp="2026-05-22T00:00:00Z",
        ),
    ])
    db.insert_raw_edgar_insider([
        RawEdgarInsiderRow(
            run_id="r1", source_filing_accn="0000320193-26-000010",
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

    from src.common.datasources.base import BaseDataSource

    class _Stub(BaseDataSource):
        name = "edgar"
        cadence = "weekly"
        provides = set()
        def health_check(self):
            return None

    _Stub().update_watermark(
        ticker="AAPL", field="filings",
        last_observation_date=None, db=db, success=True,
    )
    return db


def test_notebook_panels_end_to_end(seeded_db, tmp_path):
    """Mirrors the four notebook cells that exercise the panels."""
    # Cell 1: control overrides.
    base_config = {"enabled_sources": ["edgar"]}
    eff = apply_notebook_overrides(
        base_config,
        enabled_sources=["edgar", "yahoo"],
        force_refresh_sources=["yahoo"],
    )
    assert eff["enabled_sources"] == ["edgar", "yahoo"]
    assert eff["force_refresh_sources"] == ["yahoo"]

    # Cell 2: Source Status.
    src_status_df = source_status(seeded_db)
    assert isinstance(src_status_df, pd.DataFrame)
    assert len(src_status_df) >= 1
    assert "edgar" in src_status_df["source"].values

    # Cell 3: Provenance Summary.
    prov_df = provenance_summary(seeded_db)
    assert isinstance(prov_df, pd.DataFrame)
    assert len(prov_df) == 11  # 9 ticker tables + 2 universe tables
    filings_row = prov_df[prov_df["raw_table"] == "raw_edgar_filings"].iloc[0]
    assert filings_row["row_count"] == 2
    assert filings_row["covered_tickers"] == 2

    # Cell 4: Per-Ticker Drilldown for AAPL.
    drilldown = per_ticker_drilldown(seeded_db, "AAPL")
    assert isinstance(drilldown, dict)
    assert len(drilldown["filings"]) == 1
    assert len(drilldown["insider"]) == 1
    assert len(drilldown["fundamentals_yahoo"]) == 0
    assert "canonical" in drilldown
    assert "watermarks" in drilldown


def test_drilldown_for_unseeded_ticker_no_exceptions(seeded_db):
    out = per_ticker_drilldown(seeded_db, "ZZZZ")
    for key, df in out.items():
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0
