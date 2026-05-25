"""Tests for src.layer1_universe.notebook_panels (Phase A.4).

Coverage:
  * source_status: empty DB -> empty frame with right columns; seeded
    watermarks reflected; enabled flag respects an optional registry.
  * provenance_summary: lists every known raw_* table even when some
    are empty; covered_tickers populated for ticker-bearing tables and
    None for FRED/pytrends.
  * per_ticker_drilldown: keys are stable; seeded ticker yields
    non-empty slices for the seeded sources; unknown ticker returns
    all-empty slices without raising.
  * apply_notebook_overrides: applies both lists; leaves other keys
    untouched; input dict not mutated.
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


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(db_path=str(tmp_path / "test.db"))
    d.migrate_to_v13()
    return d


# ---------------------------------------------------------------------------
# source_status
# ---------------------------------------------------------------------------


def test_source_status_empty_db_returns_empty_frame_with_columns(db):
    out = source_status(db)
    assert list(out.columns) == [
        "source", "field", "enabled", "last_fetched_at",
        "last_observation_date", "fetch_count", "error_count",
        "last_error_message",
    ]
    assert len(out) == 0


def test_source_status_reflects_seeded_watermark(db):
    from src.common.datasources.base import BaseDataSource

    class _Stub(BaseDataSource):
        name = "edgar"
        cadence = "weekly"
        provides = set()
        def health_check(self):
            return None

    _Stub().update_watermark(
        ticker="AAPL", field="fundamentals",
        last_observation_date=None, db=db, success=True,
    )

    out = source_status(db)
    assert len(out) == 1
    assert out.iloc[0]["source"] == "edgar"
    assert out.iloc[0]["field"] == "fundamentals"
    assert bool(out.iloc[0]["enabled"]) is True


def test_source_status_enabled_flag_uses_registry(db):
    from src.common.datasources.base import BaseDataSource

    class _Stub(BaseDataSource):
        name = "edgar"
        cadence = "weekly"
        provides = set()
        def health_check(self):
            return None

    _Stub().update_watermark(
        ticker="AAPL", field="fundamentals",
        last_observation_date=None, db=db, success=True,
    )

    class _Reg:
        def enabled_sources(self):
            return []  # nothing enabled

    out = source_status(db, registry=_Reg())
    assert len(out) == 1
    assert bool(out.iloc[0]["enabled"]) is False


# ---------------------------------------------------------------------------
# provenance_summary
# ---------------------------------------------------------------------------


def test_provenance_summary_lists_all_known_raw_tables(db):
    out = provenance_summary(db)
    expected = {
        "raw_yahoo", "raw_edgar_fundamentals", "raw_edgar_insider",
        "raw_edgar_filings", "raw_finra", "raw_stockanalysis_ratios",
        "raw_openbb", "raw_gdelt", "raw_edgar_filing_tone",
        "raw_fred", "raw_pytrends",
    }
    assert set(out["raw_table"]) == expected


def test_provenance_summary_zero_count_on_empty_tables(db):
    out = provenance_summary(db)
    counts = out.set_index("raw_table")["row_count"]
    # All raw_* tables are empty on a fresh DB.
    for table, n in counts.items():
        if n is None:
            continue
        assert int(n) == 0, table


def test_provenance_summary_after_seeding(db):
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
    out = provenance_summary(db)
    filings_row = out[out["raw_table"] == "raw_edgar_filings"].iloc[0]
    assert filings_row["row_count"] == 2
    assert filings_row["covered_tickers"] == 2
    fred_row = out[out["raw_table"] == "raw_fred"].iloc[0]
    # pandas coerces None -> NaN when other rows in the column are int.
    assert pd.isna(fred_row["covered_tickers"])


# ---------------------------------------------------------------------------
# per_ticker_drilldown
# ---------------------------------------------------------------------------


def test_per_ticker_drilldown_unknown_ticker_returns_empty_frames(db):
    out = per_ticker_drilldown(db, "ZZZZ")
    expected_keys = {
        "canonical", "fundamentals_yahoo", "fundamentals_xbrl",
        "insider", "filings", "short_interest", "ratios", "openbb",
        "news_mentions", "filing_tone", "watermarks",
    }
    assert set(out.keys()) == expected_keys
    for k, df in out.items():
        assert isinstance(df, pd.DataFrame), k
        assert len(df) == 0, k


def test_per_ticker_drilldown_seeded_ticker(db):
    db.insert_raw_edgar_filings([
        RawEdgarFilingRow(
            run_id="r1", ticker="AAPL", cik="0000320193",
            form_type="10-K", filing_date="2025-11-01",
            source_filing_accn="0000320193-26-000010",
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
    out = per_ticker_drilldown(db, "aapl")
    assert len(out["filings"]) == 1
    assert len(out["insider"]) == 1
    assert len(out["fundamentals_yahoo"]) == 0
    assert len(out["filing_tone"]) == 0


def test_per_ticker_drilldown_lowercase_input_normalized(db):
    db.insert_raw_edgar_filings([
        RawEdgarFilingRow(
            run_id="r1", ticker="AAPL", cik="0000320193",
            form_type="10-K", filing_date="2025-11-01",
            source_filing_accn="0000320193-26-000010",
            primary_doc_url="https://x", scrape_timestamp="2026-05-22T00:00:00Z",
        ),
    ])
    out_upper = per_ticker_drilldown(db, "AAPL")
    out_lower = per_ticker_drilldown(db, "aapl")
    out_mixed = per_ticker_drilldown(db, " Aapl ")
    assert len(out_upper["filings"]) == 1
    assert len(out_lower["filings"]) == 1
    assert len(out_mixed["filings"]) == 1


# ---------------------------------------------------------------------------
# apply_notebook_overrides
# ---------------------------------------------------------------------------


def test_apply_notebook_overrides_basic():
    base = {"enabled_sources": ["finviz", "yahoo"], "other_key": 42}
    out = apply_notebook_overrides(
        base, enabled_sources=["edgar"], force_refresh_sources=["yahoo"],
    )
    assert out["enabled_sources"] == ["edgar"]
    assert out["force_refresh_sources"] == ["yahoo"]
    assert out["other_key"] == 42


def test_apply_notebook_overrides_preserves_input():
    base = {"enabled_sources": ["finviz"], "other": "x"}
    apply_notebook_overrides(base, enabled_sources=["edgar"])
    assert base["enabled_sources"] == ["finviz"]
    assert "force_refresh_sources" not in base


def test_apply_notebook_overrides_no_overrides_returns_copy():
    base = {"enabled_sources": ["finviz"]}
    out = apply_notebook_overrides(base)
    assert out == base
    assert out is not base
