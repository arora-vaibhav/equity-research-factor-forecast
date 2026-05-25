"""End-to-end A.3.1 acceptance: v2->v3 migration + watermark flow + force_refetch + env loading."""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

from src.common.database import DatabaseManager
from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)
from src.common.env_loader import get_api_key
from src.common.schemas import HistoricalPriceRow


class _FakeYahooSource(BaseDataSource):
    name = "yahoo"
    cadence = "weekly"
    provides = {"historical_price"}

    def health_check(self) -> SourceHealthStatus:
        return SourceHealthStatus(source=self.name, status=SourceStatus.OK, checked_at="t")


def test_full_watermark_flow_against_stub_source(tmp_path: Path):
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.migrate_to_v3()
    src = _FakeYahooSource()

    start, end = src.get_fetch_gap("AAPL", "historical_price", mgr, max_lookback_days=30)
    assert end == datetime.date.today()
    assert (end - start).days == 30

    rows = [
        HistoricalPriceRow(
            ticker="AAPL", observation_date=f"2026-05-{18 + i}",
            open=100.0, high=110.0, low=99.0, close=105.0 + i,
            volume=1_000_000, adj_close=105.0 + i,
            source="yahoo", scrape_timestamp=datetime.datetime.utcnow().isoformat(),
        )
        for i in range(3)
    ]
    mgr.insert_historical_price(rows)
    src.update_watermark(
        ticker="AAPL", field="historical_price",
        last_observation_date=datetime.date(2026, 5, 20),
        db=mgr, success=True,
    )

    start2, _ = src.get_fetch_gap("AAPL", "historical_price", mgr)
    assert start2 == datetime.date(2026, 5, 21)

    with sqlite3.connect(db_path) as c:
        count = c.execute(
            "SELECT COUNT(*) FROM historical_price WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert count == 3

    mgr.insert_historical_price(rows)  # duplicate insert should be no-op
    with sqlite3.connect(db_path) as c:
        count2 = c.execute(
            "SELECT COUNT(*) FROM historical_price WHERE ticker='AAPL'"
        ).fetchone()[0]
    assert count2 == 3

    deleted = mgr.force_refetch("yahoo", "AAPL", "historical_price", from_date="2026-05-19")
    assert deleted == 2

    w = mgr.get_watermark("yahoo", "AAPL", "historical_price")
    assert w["last_observation_date"] == "2026-05-18"


def test_env_loader_finds_keys_when_present(tmp_path: Path):
    (tmp_path / ".env").write_text("FMP_API_KEY=fake-key-123\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "api_keys.yaml").write_text(
        "fmp: FMP_API_KEY\npolygon: POLYGON_API_KEY\n"
    )
    key = get_api_key(
        "fmp",
        env_path=tmp_path / ".env",
        keys_yaml_path=tmp_path / "config" / "api_keys.yaml",
    )
    assert key == "fake-key-123"

    assert get_api_key(
        "polygon",
        env_path=tmp_path / ".env",
        keys_yaml_path=tmp_path / "config" / "api_keys.yaml",
    ) is None
