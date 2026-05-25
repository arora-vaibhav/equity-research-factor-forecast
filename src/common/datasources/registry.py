"""DataSourceRegistry — loads config, instantiates sources, orchestrates.

For A.1 the registry exposes:
  - enabled_sources(): iter sources currently enabled in config
  - priority_rank(source, category): rank lookup for resolution
  - field_category(field): which resolution rule applies to a field
  - health_check_all(): run health_check() on every enabled source,
    isolating exceptions per source.

Future phases extend this with fetch_all() and fetch_canonical_universe()
(spec §7).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)
from src.common.datasources.finviz_source import FinvizSource
from src.common.datasources.yahoo_source import YahooSource
from src.common.datasources.edgar_source import EdgarSource
from src.common.datasources.fred_source import FredSource
from src.common.datasources.stockanalysis_source import StockanalysisSource
from src.common.datasources.finra_source import FinraSource
# A.3.8 news-activity substrate sources
from src.common.datasources.gdelt_source import GdeltSource
from src.common.datasources.pytrends_source import PytrendsSource


# Single registry of known source classes keyed by source.name.
# Layer-2 scaffold sources are intentionally NOT in this table — they are
# imported only by tests that exercise the scaffold contract.
_SOURCE_CLASSES: dict[str, type[BaseDataSource]] = {
    "finviz":        FinvizSource,
    "yahoo":         YahooSource,
    "edgar":         EdgarSource,
    "fred":          FredSource,
    "stockanalysis": StockanalysisSource,
    "finra":         FinraSource,
    # A.3.8 news-activity substrate sources
    "gdelt":         GdeltSource,
    "pytrends":      PytrendsSource,
}

_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "datasources.yaml"
)


class DataSourceRegistry:
    """Loads config/datasources.yaml and exposes source orchestration."""

    def __init__(self, config_path: Path | None = None):
        self._config_path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        with open(self._config_path, "r", encoding="utf-8") as f:
            self._cfg = yaml.safe_load(f)
        self._sources: dict[str, BaseDataSource] = {}
        for name in self._cfg.get("enabled_sources", []):
            cls = _SOURCE_CLASSES.get(name)
            if cls is None:
                raise KeyError(
                    f"enabled_sources references unknown source '{name}' "
                    f"(known: {sorted(_SOURCE_CLASSES)})"
                )
            self._sources[name] = cls()

    def enabled_sources(self) -> Iterable[BaseDataSource]:
        return list(self._sources.values())

    def priority_rank(self, source: str, category: str) -> int | None:
        order = self._cfg.get("source_priority", {}).get(category, [])
        if source not in order:
            return None
        return order.index(source) + 1

    def field_category(self, field: str) -> str | None:
        for category, fields in self._cfg.get("field_resolution", {}).items():
            if field in fields:
                return category
        return None

    def health_check_all(self) -> list[SourceHealthStatus]:
        results: list[SourceHealthStatus] = []
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for src in self.enabled_sources():
            try:
                results.append(src.health_check())
            except Exception as exc:  # noqa: BLE001 — must isolate per source
                results.append(
                    SourceHealthStatus(
                        source=src.name,
                        status=SourceStatus.FAILED,
                        checked_at=now_iso,
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
        return results
