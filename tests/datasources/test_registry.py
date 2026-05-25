"""Tests for DataSourceRegistry."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.common.datasources.registry import DataSourceRegistry
from src.common.datasources.base import SourceStatus, SourceHealthStatus


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    cfg = {
        "enabled_sources": ["finviz", "yahoo"],
        "source_priority": {
            "fundamentals": ["yahoo", "finviz"],
            "prices": ["yahoo", "finviz"],
            "identifiers": ["yahoo", "finviz"],
            "short_interest": ["finviz"],
            "macro": [],
        },
        "priority_weights": {"decay": 0.5},
        "field_resolution": {
            "weighted_average": ["pe_ttm"],
            "highest_priority": ["sector"],
            "freshest": ["price"],
            "derived": [],
        },
        "disagreement_threshold_log_only": 0.10,
        "cadence_window_hours": {"weekly": 168, "biweekly": 336, "monthly": 720},
    }
    p = tmp_path / "datasources.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


class TestRegistryLoading:
    def test_loads_enabled_sources_only(self, config_path):
        reg = DataSourceRegistry(config_path=config_path)
        names = {s.name for s in reg.enabled_sources()}
        assert names == {"finviz", "yahoo"}

    def test_priority_rank_lookup(self, config_path):
        reg = DataSourceRegistry(config_path=config_path)
        assert reg.priority_rank("yahoo", "fundamentals") == 1
        assert reg.priority_rank("finviz", "fundamentals") == 2

    def test_priority_rank_unranked_source_returns_none(self, config_path):
        reg = DataSourceRegistry(config_path=config_path)
        assert reg.priority_rank("finra", "fundamentals") is None

    def test_field_category_lookup(self, config_path):
        reg = DataSourceRegistry(config_path=config_path)
        assert reg.field_category("pe_ttm") == "weighted_average"
        assert reg.field_category("sector") == "highest_priority"
        assert reg.field_category("price") == "freshest"
        assert reg.field_category("unknown_field") is None

    def test_unknown_enabled_source_raises(self, tmp_path):
        cfg = {
            "enabled_sources": ["nonexistent"],
            "source_priority": {},
            "priority_weights": {"decay": 0.5},
            "field_resolution": {
                "weighted_average": [], "highest_priority": [],
                "freshest": [], "derived": [],
            },
            "disagreement_threshold_log_only": 0.10,
            "cadence_window_hours": {"weekly": 168},
        }
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.safe_dump(cfg))
        with pytest.raises(KeyError, match="nonexistent"):
            DataSourceRegistry(config_path=p)


class TestRegistryHealthCheckAll:
    def test_health_check_all_returns_one_status_per_source(self, config_path, mocker):
        mocker.patch(
            "src.common.datasources.finviz_source.FinvizSource.health_check",
            return_value=SourceHealthStatus(
                source="finviz", status=SourceStatus.OK, checked_at="t"
            ),
        )
        mocker.patch(
            "src.common.datasources.yahoo_source.YahooSource.health_check",
            return_value=SourceHealthStatus(
                source="yahoo", status=SourceStatus.OK, checked_at="t"
            ),
        )
        reg = DataSourceRegistry(config_path=config_path)
        results = reg.health_check_all()
        assert len(results) == 2
        assert all(r.is_healthy for r in results)
        assert {r.source for r in results} == {"finviz", "yahoo"}

    def test_health_check_isolates_failures(self, config_path, mocker):
        mocker.patch(
            "src.common.datasources.finviz_source.FinvizSource.health_check",
            return_value=SourceHealthStatus(
                source="finviz", status=SourceStatus.OK, checked_at="t"
            ),
        )
        mocker.patch(
            "src.common.datasources.yahoo_source.YahooSource.health_check",
            side_effect=RuntimeError("crash"),
        )
        reg = DataSourceRegistry(config_path=config_path)
        results = reg.health_check_all()
        statuses = {r.source: r.status for r in results}
        assert statuses["finviz"] == SourceStatus.OK
        assert statuses["yahoo"] == SourceStatus.FAILED
