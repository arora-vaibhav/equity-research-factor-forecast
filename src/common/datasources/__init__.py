"""Multi-source data adapter module.

Public API:
- BaseDataSource: abstract base class for all source adapters
- SourceHealthStatus, SourceStatus: health-check return types
- DataSourceRegistry: orchestrator
"""
from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)
from src.common.datasources.registry import DataSourceRegistry

__all__ = [
    "BaseDataSource",
    "SourceHealthStatus",
    "SourceStatus",
    "DataSourceRegistry",
]
