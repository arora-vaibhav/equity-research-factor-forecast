"""End-to-end integration test for A.3.8 (Task 11 of the A.3.8 plan).

Wires schema v11 + all five A.3.8 methodology modules + the source-shaped
row contracts together against a mocked HTTP / pytrends boundary.  This
is the canary that confirms the methodology modules can consume the
rows that GdeltSource / PytrendsSource / OpenBBSource emit.

Strategy
--------
1.  Migrate a fresh tmp_path SQLite database to schema v11.
2.  Seed each of the five upstream tables with realistic synthetic rows
    (60-365 days of history per signal's window requirement).
3.  Pull the rows back via DatabaseManager + dict-conversion to mimic
    what the orchestrator does at A.3.10 composite time.
4.  Run all five methodology modules in turn.
5.  Assert each returns a non-NaN result for the seeded ticker (AAPL)
    or, for fears_signal, a non-NaN universe scalar.

No live network calls.  No real GDELT or pytrends requests — the
upstream sources are not exercised here; we test the *methodology
contracts* against the *schema shapes*.

Caller: pytest.
"""
from __future__ import annotations

import datetime as _dt
import math
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.schemas import (
    RawEdgarFilingRow,
    RawFinraShortInterest,
    RawGdeltMention,
    RawOpenBBRow,
    RawPytrendsObservation,
)
from src.methodology.fears_signal import compute_fears_signal
from src.methodology.filing_density import compute_filing_density
from src.methodology.news_volume_anomaly import compute_news_volume_anomaly
from src.methodology.revisions_velocity import compute_revisions_velocity
from src.methodology.short_interest_delta import compute_short_interest_delta


AS_OF = _dt.date(2026, 5, 20)
TICKER = "AAPL"
CIK = "0000320193"
RUN_ID = "integration_a3_8_r1"


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(db_path=str(tmp_path / "test.db"))
    mgr.migrate_to_v11()
    return mgr


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

def _seed_finra(db: DatabaseManager) -> None:
    """Seed raw_finra with 60 days of daily short-interest snapshots.

    short_interest_shares rises ~2% / day so the delta is clearly positive.
    """
    rows = []
    for i in range(60):
        d = AS_OF - _dt.timedelta(days=i)
        si_shares = 1_000_000.0 * (1.0 + 0.02 * (60 - i))
        rows.append(RawFinraShortInterest(
            run_id=RUN_ID,
            ticker=TICKER,
            settlement_date=d.isoformat(),
            exchange="NSDQ",
            short_interest_shares=si_shares,
            avg_daily_volume=50_000_000.0,
            days_to_cover=si_shares / 50_000_000.0,
            source_filename="integration_test",
            scrape_timestamp="2026-05-20T12:00:00Z",
        ))
    db.insert_raw_finra(rows)


def _seed_gdelt(db: DatabaseManager) -> None:
    """Seed raw_gdelt with 365+ days of AAPL mentions.

    Baseline (-365..-30): 1-3 mentions/day with variance (sigma > 0).
    Recent 30-day window: 6 mentions/day (clearly elevated).
    """
    rows = []
    record_counter = 0
    for i in range(365, 30, -1):
        d = AS_OF - _dt.timedelta(days=i)
        n = 1 + (i % 3)
        for j in range(n):
            record_counter += 1
            ts = d.strftime("%Y-%m-%d") + f"T12:{j:02d}:00Z"
            rows.append(RawGdeltMention(
                run_id=RUN_ID,
                gkg_record_id=f"int-{record_counter:08d}",
                ticker=TICKER,
                mention_timestamp=ts,
                match_method="cashtag",
                source_url=f"https://example.com/article-{record_counter}",
                gkg_tone_json=None,
                scrape_timestamp="2026-05-20T12:00:00Z",
            ))
    for i in range(30):
        d = AS_OF - _dt.timedelta(days=i)
        for j in range(6):
            record_counter += 1
            ts = d.strftime("%Y-%m-%d") + f"T12:{j:02d}:00Z"
            rows.append(RawGdeltMention(
                run_id=RUN_ID,
                gkg_record_id=f"int-{record_counter:08d}",
                ticker=TICKER,
                mention_timestamp=ts,
                match_method="cashtag",
                source_url=f"https://example.com/article-{record_counter}",
                gkg_tone_json=None,
                scrape_timestamp="2026-05-20T12:00:00Z",
            ))
    db.insert_raw_gdelt(rows)


def _seed_pytrends(db: DatabaseManager) -> None:
    """Seed raw_pytrends with daily observations of the FEARS basket.

    Recent 7-day window SVI=80 (high fear).  Prior 365 days SVI=40.
    fears_signal: HIGH recent SVI -> POSITIVE score (contrarian bullish).
    """
    rows = []
    for i in range(365, 7, -1):
        d = AS_OF - _dt.timedelta(days=i)
        for term in ("recession", "bankruptcy", "unemployment"):
            rows.append(RawPytrendsObservation(
                run_id=RUN_ID,
                term=term,
                observation_date=d.isoformat(),
                svi=40.0,
                geo="US",
                source_filename="integration_test",
                scrape_timestamp="2026-05-20T12:00:00Z",
            ))
    for i in range(7):
        d = AS_OF - _dt.timedelta(days=i)
        for term in ("recession", "bankruptcy", "unemployment"):
            rows.append(RawPytrendsObservation(
                run_id=RUN_ID,
                term=term,
                observation_date=d.isoformat(),
                svi=80.0,
                geo="US",
                source_filename="integration_test",
                scrape_timestamp="2026-05-20T12:00:00Z",
            ))
    db.insert_raw_pytrends(rows)


def _seed_edgar_filings(db: DatabaseManager) -> None:
    """Seed raw_edgar_filings with 8-K filings over the trailing year.

    Baseline (-454..-90): 1 non-routine 8-K every 30 days.
    Recent 90 days: 1 non-routine 8-K every 10 days (dense).
    All filings use item_codes='5.02' (officer departure, non-routine).
    """
    rows = []
    accn_counter = 0
    d = AS_OF - _dt.timedelta(days=454)
    while d <= AS_OF - _dt.timedelta(days=90):
        if (AS_OF - d).days % 30 == 0:
            accn_counter += 1
            rows.append(RawEdgarFilingRow(
                run_id=RUN_ID,
                ticker=TICKER,
                cik=CIK,
                form_type="8-K",
                filing_date=d.isoformat(),
                accepted_at=d.isoformat() + "T12:00:00Z",
                item_codes="5.02",
                source_filing_accn=f"0000320193-26-{accn_counter:06d}",
                primary_doc_url=f"https://example.com/filing-{accn_counter}",
                scrape_timestamp="2026-05-20T12:00:00Z",
            ))
        d += _dt.timedelta(days=1)
    d = AS_OF - _dt.timedelta(days=89)
    while d <= AS_OF:
        if (AS_OF - d).days % 10 == 0:
            accn_counter += 1
            rows.append(RawEdgarFilingRow(
                run_id=RUN_ID,
                ticker=TICKER,
                cik=CIK,
                form_type="8-K",
                filing_date=d.isoformat(),
                accepted_at=d.isoformat() + "T12:00:00Z",
                item_codes="5.02",
                source_filing_accn=f"0000320193-26-{accn_counter:06d}",
                primary_doc_url=f"https://example.com/filing-{accn_counter}",
                scrape_timestamp="2026-05-20T12:00:00Z",
            ))
        d += _dt.timedelta(days=1)
    db.insert_raw_edgar_filings(rows)


def _seed_openbb_analyst_estimates(db: DatabaseManager) -> None:
    """Seed raw_openbb with 3 analyst-estimate snapshots over 90 days.

    Net upward revisions (direction_count > 0) so revisions_velocity > 0.
    """
    rows = []
    obs_dates = [AS_OF - _dt.timedelta(days=d) for d in (5, 30, 60)]
    eps_values = [1.55, 1.50, 1.45]      # Rising estimates
    direction_counts = [5.0, 4.0, 3.0]   # Net upward
    for i, (d, eps, dir_cnt) in enumerate(zip(obs_dates, eps_values, direction_counts)):
        ts = d.isoformat() + "T12:00:00Z"
        rows.extend([
            RawOpenBBRow(
                run_id=RUN_ID + f"-{i}",
                ticker=TICKER,
                field_name="eps_estimate_current",
                provider_used="fmp",
                value=eps,
                unit="usd",
                scrape_timestamp=ts,
            ),
            RawOpenBBRow(
                run_id=RUN_ID + f"-{i}",
                ticker=TICKER,
                field_name="eps_revision_direction_count",
                provider_used="fmp",
                value=dir_cnt,
                unit="count",
                scrape_timestamp=ts,
            ),
        ])
    db.insert_raw_openbb(rows)


def _fetch_dicts(db: DatabaseManager, sql: str, params=()) -> list[dict]:
    """Run a SELECT and return rows as list[dict]."""
    with sqlite3.connect(db.db_path) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, params)]


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

def test_a3_8_schema_version_is_11_after_migration(db):
    """Sanity check: migrate_to_v11() leaves schema_version at 11."""
    assert db.get_schema_version() == 11


def test_a3_8_end_to_end(db):
    """All 5 A.3.8 methodology modules consume schema-v11 rows successfully.

    Each module returns a non-NaN result for AAPL (or universe scalar
    for fears_signal).
    """
    # --- Seed all upstream tables ----------------------------------
    _seed_finra(db)
    _seed_gdelt(db)
    _seed_pytrends(db)
    _seed_edgar_filings(db)
    _seed_openbb_analyst_estimates(db)

    # --- Pull rows back as dicts -----------------------------------
    finra_rows = _fetch_dicts(
        db,
        "SELECT ticker, settlement_date, short_interest_shares, "
        "avg_daily_volume FROM raw_finra WHERE ticker=?",
        (TICKER,),
    )
    gdelt_rows = _fetch_dicts(
        db,
        "SELECT ticker, mention_timestamp FROM raw_gdelt WHERE ticker=?",
        (TICKER,),
    )
    pytrends_rows = _fetch_dicts(
        db,
        "SELECT term, observation_date, svi FROM raw_pytrends",
    )
    filings_rows = _fetch_dicts(
        db,
        "SELECT ticker, form_type, filing_date, item_codes "
        "FROM raw_edgar_filings WHERE ticker=?",
        (TICKER,),
    )
    openbb_rows = _fetch_dicts(
        db,
        "SELECT ticker, field_name, value, scrape_timestamp "
        "FROM raw_openbb WHERE ticker=?",
        (TICKER,),
    )

    assert len(finra_rows) > 0, "raw_finra not seeded"
    assert len(gdelt_rows) > 0, "raw_gdelt not seeded"
    assert len(pytrends_rows) > 0, "raw_pytrends not seeded"
    assert len(filings_rows) > 0, "raw_edgar_filings not seeded"
    assert len(openbb_rows) > 0, "raw_openbb not seeded"

    # --- Run all 5 methodology modules -----------------------------
    si = compute_short_interest_delta(finra_rows, as_of_date=AS_OF.isoformat())
    rv = compute_revisions_velocity(
        openbb_rows, as_of_date=AS_OF.isoformat(),
        dispersion_penalty_lambda=0.0,
    )
    nv = compute_news_volume_anomaly(
        gdelt_rows, as_of_date=AS_OF.isoformat(),
        recent_window_days=30, baseline_window_days=365,
    )
    fd = compute_filing_density(
        filings_rows, as_of_date=AS_OF.isoformat(),
        recent_window_days=90, baseline_window_days=365,
    )
    fe = compute_fears_signal(
        pytrends_rows, as_of_date=AS_OF.isoformat(),
        recent_window_days=7, baseline_window_days=365,
    )

    # --- Assert each produced a non-NaN result ---------------------
    assert TICKER in si, f"short_interest_delta missing ticker; got {si}"
    assert not math.isnan(si[TICKER]), f"short_interest_delta NaN for {TICKER}"

    assert TICKER in rv, f"revisions_velocity missing ticker; got {rv}"
    assert not math.isnan(rv[TICKER]), f"revisions_velocity NaN for {TICKER}"

    assert TICKER in nv, f"news_volume_anomaly missing ticker; got {nv}"
    assert not math.isnan(nv[TICKER]), f"news_volume_anomaly NaN for {TICKER}"

    assert TICKER in fd, f"filing_density missing ticker; got {fd}"
    assert not math.isnan(fd[TICKER]), f"filing_density NaN for {TICKER}"

    assert isinstance(fe, float), f"fears_signal should be float; got {type(fe)}"
    assert not math.isnan(fe), "fears_signal is NaN"


def test_a3_8_directional_sanity(db):
    """End-to-end with deliberately-directional seeds: signals point the
    expected way given the seed data."""
    _seed_finra(db)
    _seed_gdelt(db)
    _seed_pytrends(db)
    _seed_edgar_filings(db)
    _seed_openbb_analyst_estimates(db)

    finra_rows = _fetch_dicts(
        db,
        "SELECT ticker, settlement_date, short_interest_shares, "
        "avg_daily_volume FROM raw_finra WHERE ticker=?",
        (TICKER,),
    )
    pytrends_rows = _fetch_dicts(
        db, "SELECT term, observation_date, svi FROM raw_pytrends",
    )
    openbb_rows = _fetch_dicts(
        db,
        "SELECT ticker, field_name, value, scrape_timestamp "
        "FROM raw_openbb WHERE ticker=?",
        (TICKER,),
    )

    si = compute_short_interest_delta(finra_rows, as_of_date=AS_OF.isoformat())
    rv = compute_revisions_velocity(
        openbb_rows, as_of_date=AS_OF.isoformat(),
        dispersion_penalty_lambda=0.0,
    )
    fe = compute_fears_signal(
        pytrends_rows, as_of_date=AS_OF.isoformat(),
        recent_window_days=7, baseline_window_days=365,
    )

    # Seeded SI: rises ~2%/day -> recent value HIGHER than 30d-back ->
    # delta POSITIVE (DLW raw, bearish in lit; sign-flipped to bullish
    # at composite time per spec §7.2 sig 2).
    assert si[TICKER] > 0

    # Seeded EPS direction_count = 5,4,3 (all positive) -> POSITIVE velocity.
    assert rv[TICKER] > 0

    # Seeded SVI: recent=80, baseline=40 -> HIGH recent fear ->
    # POSITIVE score (contrarian bullish per D-E-G 2015).
    assert fe > 0
