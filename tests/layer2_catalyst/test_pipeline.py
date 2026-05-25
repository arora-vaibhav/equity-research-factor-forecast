"""Tests for src.layer2_catalyst.pipeline."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.common.database import DatabaseManager
from src.common.schemas import CanonicalUniverseRow, CatalystRow
from src.layer2_catalyst.pipeline import (
    CATALYST_WINDOW_DAYS,
    PIPELINE_VERSION,
    run_layer2,
)


def test_version_constant():
    assert PIPELINE_VERSION == "1.0"


def test_catalyst_window_locked():
    assert CATALYST_WINDOW_DAYS == 90


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    d = DatabaseManager(db_path=str(tmp_path / "t.db"))
    d.migrate_to_v14()
    return d


def _seed_layer1_candidates(db, run_id, tickers_playbooks):
    # First insert a row in `runs` so the FK from candidate_results
    # is satisfied.
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO runs
            (run_id, run_timestamp, run_type, status)
            VALUES (?, ?, 'layer1', 'ok')
            """,
            (run_id, "2026-05-23T00:00:00Z"),
        )
        conn.commit()

    canonical_rows = [
        CanonicalUniverseRow(
            run_id=run_id, ticker=t, sector="Technology",
        )
        for t, _ in tickers_playbooks
    ]
    db.insert_canonical_universe(canonical_rows)
    cand_df = pd.DataFrame([
        {
            "run_id": run_id, "ticker": t, "playbook": pb,
            "composite_score": 75.0, "eligible": 1,
        }
        for t, pb in tickers_playbooks
    ])
    for (t, pb) in tickers_playbooks:
        sub_df = cand_df[(cand_df["ticker"] == t) & (cand_df["playbook"] == pb)]
        db.save_candidates(run_id, pb, sub_df)


def _seed_historical_price(db, ticker, n_days=260, base_price=100.0, trend=0.0):
    end = datetime.now(timezone.utc).date()
    idx = pd.bdate_range(end=end, periods=n_days)
    rng = np.random.default_rng(42)
    closes = base_price + np.arange(n_days) * trend + rng.normal(0.0, 1.0, n_days).cumsum() * 0.1
    rows = []
    for i, d in enumerate(idx):
        c = float(closes[i])
        rows.append({
            "ticker": ticker,
            "observation_date": d.date().isoformat(),
            "open": c * 0.998,
            "high": c * 1.005,
            "low": c * 0.995,
            "close": c,
            "volume": 1_000_000,
            "adj_close": c,
            "source": "yahoo",
            "scrape_timestamp": "2026-05-23T00:00:00Z",
        })
    with db.get_connection() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO historical_price
                (ticker, observation_date, open, high, low, close, volume,
                 adj_close, source, scrape_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(r[k] for k in [
                    "ticker", "observation_date", "open", "high", "low",
                    "close", "volume", "adj_close", "source", "scrape_timestamp",
                ]),
            )
        conn.commit()


def _seed_catalyst(db, run_id, ticker, catalyst_date, catalyst_type="earnings"):
    db.insert_catalyst_calendar([
        CatalystRow(
            run_id=run_id, ticker=ticker,
            catalyst_type=catalyst_type, catalyst_date=catalyst_date,
            source="yahoo", confidence="high",
            scrape_timestamp="2026-05-23T00:00:00Z",
        ),
    ])


def test_run_layer2_no_layer1_candidates(db, tmp_path):
    result = run_layer2(
        layer1_run_id="missing-run", as_of_date="2026-05-23", db=db,
        output_dir=tmp_path / "out",
    )
    assert result.n_input_candidates == 0
    assert result.n_active_candidates == 0
    assert result.output_parquet_path is None


def test_run_layer2_skips_candidate_without_ohlcv(db, tmp_path):
    _seed_layer1_candidates(db, "L1-run", [("AAPL", "playbook_a")])
    result = run_layer2(
        layer1_run_id="L1-run", as_of_date="2026-05-23", db=db,
        output_dir=tmp_path / "out",
    )
    assert result.n_input_candidates == 1
    assert result.n_active_candidates == 0
    assert result.n_skipped_no_prices == 1


def test_run_layer2_no_catalyst_no_active(db, tmp_path):
    """OHLCV seeded but no catalyst -> not active even if setups pass."""
    _seed_layer1_candidates(db, "L1-run", [("AAPL", "playbook_a")])
    _seed_historical_price(db, "AAPL")
    result = run_layer2(
        layer1_run_id="L1-run", as_of_date="2026-05-23", db=db,
        output_dir=tmp_path / "out",
    )
    assert result.n_input_candidates == 1
    assert result.n_active_candidates == 0


def test_run_layer2_catalyst_outside_window_excluded(db, tmp_path):
    _seed_layer1_candidates(db, "L1-run", [("AAPL", "playbook_a")])
    _seed_historical_price(db, "AAPL")
    _seed_catalyst(db, "L1-run", "AAPL", catalyst_date="2027-12-01")
    result = run_layer2(
        layer1_run_id="L1-run", as_of_date="2026-05-23", db=db,
        output_dir=tmp_path / "out",
    )
    assert result.n_active_candidates == 0


def test_run_layer2_returns_clean_result_with_catalyst(db, tmp_path):
    """When OHLCV + in-window catalyst present, the pipeline runs
    without exception. is_active depends on the random setup
    evaluation -- the contract here is "no crash"."""
    _seed_layer1_candidates(db, "L1-run", [("AAPL", "playbook_a")])
    _seed_historical_price(db, "AAPL", n_days=260, base_price=100.0, trend=0.3)
    _seed_catalyst(db, "L1-run", "AAPL", catalyst_date="2026-06-15")
    result = run_layer2(
        layer1_run_id="L1-run", as_of_date="2026-05-23", db=db,
        output_dir=tmp_path / "out",
    )
    assert result.n_input_candidates == 1
    assert result.n_skipped_no_prices == 0
    if result.n_active_candidates > 0:
        assert result.output_parquet_path is not None
        assert result.output_parquet_path.exists()
