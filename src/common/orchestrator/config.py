"""Pydantic loader for config/orchestrator.yaml.

Phase A.3.7.5. The YAML file holds three blocks:

    providers:           map[provider_name] -> {quota_calls, quota_window_seconds}
    source_providers:    map[source_name]   -> list[provider_name]
    weights:             map[source_name]   -> map[field_name -> weight]

Validation guarantees:
  * quota_calls > 0 and quota_window_seconds > 0 for every provider.
  * every provider listed in source_providers exists in providers.
  * weights is a free dict (unknown (source, field) falls back to 1.0 at
    query time per queue_priority.weight_for).
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderQuotaConfig(BaseModel):
    """Per-provider sliding-window quota: ``quota_calls`` in any rolling
    ``quota_window_seconds`` interval."""

    model_config = ConfigDict(extra="forbid")

    quota_calls: int = Field(..., gt=0, description="max calls per window")
    quota_window_seconds: int = Field(..., gt=0, description="rolling window length")


class OrchestratorConfig(BaseModel):
    """Top-level orchestrator config parsed from config/orchestrator.yaml."""

    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderQuotaConfig]
    source_providers: dict[str, list[str]]
    # weights[source][field] -> numeric weight. Cast to float at parse
    # time so callers don't have to.
    weights: dict[str, dict[str, float]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_source_providers_resolvable(self) -> "OrchestratorConfig":
        for source, providers in self.source_providers.items():
            for prov in providers:
                if prov not in self.providers:
                    raise ValueError(
                        f"source_providers[{source!r}] references unknown "
                        f"provider {prov!r}; known providers: "
                        f"{sorted(self.providers)}"
                    )
        return self


def load_orchestrator_config(path: Path | str) -> OrchestratorConfig:
    """Load and validate ``config/orchestrator.yaml`` (or any path-compatible
    YAML file with the same structure).

    Raises:
      * FileNotFoundError if the file is missing.
      * yaml.YAMLError if the YAML is malformed.
      * pydantic.ValidationError if the structure violates schema or a
        source_providers entry references an unknown provider.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"orchestrator config not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return OrchestratorConfig.model_validate(raw)
