"""Tests for src.common.orchestrator.config - Pydantic loader for
config/orchestrator.yaml.

Covers:
  * load_orchestrator_config (happy path, file-not-found, malformed)
  * ProviderQuotaConfig validation
  * OrchestratorConfig structure: providers + source_providers + weights
  * Cross-references: every provider in source_providers must exist in providers
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.orchestrator.config import (
    OrchestratorConfig,
    ProviderQuotaConfig,
    load_orchestrator_config,
)


PROJECT_CONFIG = Path("config/orchestrator.yaml")


class TestProviderQuotaConfig:
    def test_minimal_valid(self) -> None:
        q = ProviderQuotaConfig(quota_calls=5, quota_window_seconds=60)
        assert q.quota_calls == 5
        assert q.quota_window_seconds == 60

    def test_quota_calls_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            ProviderQuotaConfig(quota_calls=0, quota_window_seconds=60)

    def test_window_seconds_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            ProviderQuotaConfig(quota_calls=5, quota_window_seconds=0)


class TestLoadOrchestratorConfig:
    def test_loads_project_default_file(self) -> None:
        cfg = load_orchestrator_config(PROJECT_CONFIG)
        assert isinstance(cfg, OrchestratorConfig)
        # All 9 expected providers from the prompt's spec.
        for p in [
            "fmp_stable", "polygon", "tiingo", "sec_edgar",
            "fred", "finra", "yahoo", "finviz", "stockanalysis",
        ]:
            assert p in cfg.providers, f"missing provider {p}"

    def test_provider_quotas_match_spec(self) -> None:
        cfg = load_orchestrator_config(PROJECT_CONFIG)
        # Spot-check known values from prompt.
        assert cfg.providers["polygon"].quota_calls == 5
        assert cfg.providers["polygon"].quota_window_seconds == 60
        assert cfg.providers["fmp_stable"].quota_calls == 250
        assert cfg.providers["fmp_stable"].quota_window_seconds == 86400
        assert cfg.providers["sec_edgar"].quota_calls == 10
        assert cfg.providers["sec_edgar"].quota_window_seconds == 1

    def test_source_providers_mapping(self) -> None:
        cfg = load_orchestrator_config(PROJECT_CONFIG)
        assert cfg.source_providers["openbb"] == ["fmp_stable", "polygon", "tiingo"]
        assert cfg.source_providers["edgar"] == ["sec_edgar"]
        assert cfg.source_providers["yahoo"] == ["yahoo"]

    def test_weights_match_spec(self) -> None:
        cfg = load_orchestrator_config(PROJECT_CONFIG)
        # edgar.form4 = 100 (highest per spec)
        assert cfg.weights["edgar"]["form4"] == 100
        assert cfg.weights["edgar"]["form_8k"] == 80
        assert cfg.weights["fred"]["macro_series"] == 5

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_orchestrator_config(tmp_path / "does_not_exist.yaml")

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("providers: : :")  # syntactically broken
        with pytest.raises(Exception):
            load_orchestrator_config(bad)

    def test_source_provider_must_exist_in_providers(self, tmp_path: Path) -> None:
        """A source listing an unknown provider is a config bug."""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "providers:\n"
            "  polygon: {quota_calls: 5, quota_window_seconds: 60}\n"
            "source_providers:\n"
            "  openbb: [polygon, fmp_stable]\n"  # fmp_stable missing
            "weights: {}\n"
        )
        with pytest.raises(Exception):
            load_orchestrator_config(bad)

    def test_empty_weights_allowed(self, tmp_path: Path) -> None:
        """Unknown (source, field) fall back to weight=1; an empty weights
        block is valid (just means everything has weight=1)."""
        cfg_path = tmp_path / "minimal.yaml"
        cfg_path.write_text(
            "providers:\n"
            "  yahoo: {quota_calls: 100, quota_window_seconds: 60}\n"
            "source_providers:\n"
            "  yahoo: [yahoo]\n"
            "weights: {}\n"
        )
        cfg = load_orchestrator_config(cfg_path)
        assert cfg.weights == {}
