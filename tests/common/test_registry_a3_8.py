"""Tests for DataSourceRegistry A.3.8 additions (GdeltSource + PytrendsSource).

Caller: pytest. No network calls — just verifies registry wiring and
health_check callability.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.common.datasources.registry import DataSourceRegistry


@pytest.fixture
def registry() -> DataSourceRegistry:
    """Default registry backed by the real config/datasources.yaml."""
    return DataSourceRegistry()


@pytest.fixture
def disabled_registry(tmp_path: Path) -> DataSourceRegistry:
    """Registry with gdelt + pytrends disabled via a custom config."""
    cfg = {
        "finviz_universe_filter": "+Mid (over $2bln)",
        "enabled_sources": ["fred"],
        "source_priority": {},
        "priority_weights": {"decay": 0.5},
        "field_resolution": {},
        "disagreement_threshold_log_only": 0.10,
        "cadence_window_hours": {"weekly": 168},
    }
    config_file = tmp_path / "datasources.yaml"
    config_file.write_text(yaml.dump(cfg), encoding="utf-8")
    return DataSourceRegistry(config_path=config_file)


# ---------------------------------------------------------------------------
# Enabled-sources inclusion
# ---------------------------------------------------------------------------

def test_gdelt_in_enabled_sources(registry):
    names = [s.name for s in registry.enabled_sources()]
    assert "gdelt" in names


def test_pytrends_in_enabled_sources(registry):
    names = [s.name for s in registry.enabled_sources()]
    assert "pytrends" in names


# ---------------------------------------------------------------------------
# health_check callability (no network)
# ---------------------------------------------------------------------------

def test_gdelt_health_check_callable(registry):
    gdelt = next(s for s in registry.enabled_sources() if s.name == "gdelt")
    assert callable(gdelt.health_check)


def test_pytrends_health_check_callable(registry):
    pytrends = next(s for s in registry.enabled_sources() if s.name == "pytrends")
    assert callable(pytrends.health_check)


def test_pytrends_health_check_ok_no_network(registry):
    """health_check is import-only for pytrends; returns a valid status."""
    pytrends = next(s for s in registry.enabled_sources() if s.name == "pytrends")
    status = pytrends.health_check()
    assert status.status.value in ("ok", "partial", "failed")


# ---------------------------------------------------------------------------
# Disabling via config
# ---------------------------------------------------------------------------

def test_disabling_via_config_excludes_gdelt_pytrends(disabled_registry):
    names = [s.name for s in disabled_registry.enabled_sources()]
    assert "gdelt" not in names
    assert "pytrends" not in names
