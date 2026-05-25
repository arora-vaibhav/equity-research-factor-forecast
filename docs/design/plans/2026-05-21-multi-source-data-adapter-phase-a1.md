# Multi-Source Data Adapter — Phase A.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundational `src/common/datasources/` module — abstract base class, registry, priority-weighted resolution logic, six active source-class shells with working `health_check()` methods, two Layer-2 scaffold stubs, and the `config/datasources.yaml` config file. Phase A.1's acceptance criterion (from the spec §11) is: **every active source's `health_check()` returns OK against the real upstream service, and scaffold stubs raise `NotImplementedError` cleanly.**

**Architecture:** Each data source subclasses an abstract `BaseDataSource` and implements only the methods it can provide. A `DataSourceRegistry` orchestrates them at run time. A pure-function `resolution.py` module implements the three field-resolution rules (weighted_average, highest_priority, freshest). All wiring is config-driven via `config/datasources.yaml`. No SQLite writes, no `screen.py` changes, no notebook changes — those are A.2, A.3, A.4. This phase is the framework only.

**Tech Stack:** Python 3.11+, `abc`, `pydantic`, `pyyaml`, `requests`, `yfinance`, `finvizfinance`, `pytest`. New runtime dependencies added in this plan: `requests` (FRED, EDGAR, FINRA, stockanalysis HTTP), `tenacity` (retry/backoff).

**Spec reference:** [docs/design/specs/2026-05-21-multi-source-data-adapter-design.md](../specs/2026-05-21-multi-source-data-adapter-design.md), specifically §4 (source set), §5 (module layout), §6 (BaseDataSource contract), §7 (registry), §9 (resolution policy), §11 (sub-phase acceptance).

**Out of scope for A.1** (covered in later phases — listed here so reviewers don't expect them):
- SQLite schema migration → A.2
- `screen.py` / `factors.py` refactor → A.3
- Notebook extension → A.4
- Full `fetch_universe()` / `fetch_fundamentals_for_ticker()` / `fetch_filings_for_ticker()` / `fetch_macro_series()` implementations beyond what's needed for `health_check()` → A.2 and A.3
- The deliberately-corrupted-value acceptance test → A.5

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| `requirements.txt` | Add `requests`, `tenacity`, `pytest`, `pytest-mock` | Modify |
| `config/datasources.yaml` | Source priority + field-resolution rules | Create |
| `src/common/datasources/__init__.py` | Module init, re-exports | Create |
| `src/common/datasources/base.py` | `BaseDataSource` ABC, `SourceHealthStatus`, `SourceStatus` | Create |
| `src/common/datasources/resolution.py` | `priority_weight`, `weighted_average_value`, `highest_priority_value`, `freshest_value` | Create |
| `src/common/datasources/registry.py` | `DataSourceRegistry` — load config, instantiate sources, expose `health_check_all()` | Create |
| `src/common/datasources/finviz_source.py` | `FinvizSource` | Create |
| `src/common/datasources/yahoo_source.py` | `YahooSource` | Create |
| `src/common/datasources/edgar_source.py` | `EdgarSource` | Create |
| `src/common/datasources/fred_source.py` | `FredSource` | Create |
| `src/common/datasources/stockanalysis_source.py` | `StockanalysisSource` | Create |
| `src/common/datasources/finra_source.py` | `FinraSource` | Create |
| `src/common/datasources/_layer2_scaffold/__init__.py` | Module init | Create |
| `src/common/datasources/_layer2_scaffold/yahoo_news_source.py` | `YahooNewsSource` stub | Create |
| `src/common/datasources/_layer2_scaffold/gdelt_source.py` | `GdeltSource` stub | Create |
| `tests/__init__.py` | Marker | Create |
| `tests/datasources/__init__.py` | Marker | Create |
| `tests/datasources/test_base.py` | Tests for `BaseDataSource` contract | Create |
| `tests/datasources/test_resolution.py` | Tests for resolution functions | Create |
| `tests/datasources/test_registry.py` | Tests for registry | Create |
| `tests/datasources/test_finviz_source.py` | FinvizSource unit tests | Create |
| `tests/datasources/test_yahoo_source.py` | YahooSource unit tests | Create |
| `tests/datasources/test_edgar_source.py` | EdgarSource unit tests | Create |
| `tests/datasources/test_fred_source.py` | FredSource unit tests | Create |
| `tests/datasources/test_stockanalysis_source.py` | StockanalysisSource unit tests | Create |
| `tests/datasources/test_finra_source.py` | FinraSource unit tests | Create |
| `tests/datasources/test_layer2_scaffold.py` | Scaffold-stub tests | Create |
| `tests/datasources/test_integration_health.py` | All-sources health check (network-enabled) | Create |
| `pytest.ini` | Configure markers, paths | Create |
| the build plan | Amend Phase A with sub-phase ordering | Modify |

---

## Task 0: Prerequisites

**Files:**
- Modify: `requirements.txt`
- Create: `pytest.ini`
- Create: `tests/__init__.py`
- Create: `tests/datasources/__init__.py`

- [ ] **Step 1: Add new dependencies to requirements.txt**

Modify `requirements.txt` to:

```
pandas==3.0.3
numpy==2.4.5
pyarrow==24.0.0
yfinance==1.3.0
pydantic==2.13.4
pyyaml==6.0.3
finvizfinance==1.3.0
requests==2.32.3
tenacity==9.0.0
pytest==8.3.3
pytest-mock==3.14.0
```

- [ ] **Step 2: Install updated dependencies**

Run: `pip install -r requirements.txt`
Expected: All packages install without conflict.

- [ ] **Step 3: Create pytest.ini**

Create `pytest.ini`:

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
markers =
    integration: requires network access to real upstream services (deselect with '-m "not integration"')
addopts = -ra --strict-markers
```

- [ ] **Step 4: Create test package markers**

Create `tests/__init__.py` (empty file — single newline).

Create `tests/datasources/__init__.py` (empty file — single newline).

- [ ] **Step 5: Verify pytest finds nothing yet (sanity check)**

Run: `pytest -m "not integration"`
Expected: `no tests ran` — exit code 5 is OK at this stage.

- [ ] **Step 6: Commit**

```
git init   # if not already a repo
git add requirements.txt pytest.ini tests/__init__.py tests/datasources/__init__.py
git commit -m "chore: add pytest + requests + tenacity for datasources module"
```

If `git init` errors because already a repo, skip it.

---

## Task 1: BaseDataSource ABC and types

**Files:**
- Create: `src/common/datasources/__init__.py`
- Create: `src/common/datasources/base.py`
- Test: `tests/datasources/test_base.py`

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_base.py`:

```python
"""Tests for BaseDataSource contract and helper types."""
from __future__ import annotations

import pytest

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


class TestSourceStatus:
    def test_has_required_values(self):
        assert SourceStatus.OK.value == "ok"
        assert SourceStatus.PARTIAL.value == "partial"
        assert SourceStatus.FAILED.value == "failed"


class TestSourceHealthStatus:
    def test_basic_fields(self):
        status = SourceHealthStatus(
            source="finviz",
            status=SourceStatus.OK,
            checked_at="2026-05-21T08:00:00Z",
            message="200 OK",
            response_time_ms=143.0,
        )
        assert status.source == "finviz"
        assert status.status == SourceStatus.OK
        assert status.message == "200 OK"
        assert status.response_time_ms == 143.0

    def test_is_healthy_property(self):
        ok = SourceHealthStatus(source="x", status=SourceStatus.OK, checked_at="t")
        partial = SourceHealthStatus(source="x", status=SourceStatus.PARTIAL, checked_at="t")
        failed = SourceHealthStatus(source="x", status=SourceStatus.FAILED, checked_at="t")
        assert ok.is_healthy is True
        assert partial.is_healthy is True
        assert failed.is_healthy is False


class TestBaseDataSource:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            BaseDataSource()  # type: ignore[abstract]

    def test_subclass_must_implement_health_check(self):
        class IncompleteSource(BaseDataSource):
            name = "incomplete"
            cadence = "weekly"
            provides = set()
        with pytest.raises(TypeError):
            IncompleteSource()  # type: ignore[abstract]

    def test_subclass_with_health_check_works(self):
        class GoodSource(BaseDataSource):
            name = "good"
            cadence = "weekly"
            provides = {"pe_ttm"}
            def health_check(self) -> SourceHealthStatus:
                return SourceHealthStatus(
                    source=self.name,
                    status=SourceStatus.OK,
                    checked_at="2026-05-21T08:00:00Z",
                )
        src = GoodSource()
        assert src.name == "good"
        assert src.health_check().is_healthy

    def test_optional_methods_raise_not_implemented_by_default(self):
        class GoodSource(BaseDataSource):
            name = "good"
            cadence = "weekly"
            provides = set()
            def health_check(self) -> SourceHealthStatus:
                return SourceHealthStatus(source="good", status=SourceStatus.OK, checked_at="t")
        src = GoodSource()
        with pytest.raises(NotImplementedError):
            src.fetch_universe(run_id="r")
        with pytest.raises(NotImplementedError):
            src.fetch_fundamentals_for_ticker("AAPL", run_id="r")
        with pytest.raises(NotImplementedError):
            src.fetch_filings_for_ticker("AAPL", run_id="r")
        with pytest.raises(NotImplementedError):
            src.fetch_macro_series(["DGS10"], run_id="r")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/datasources/test_base.py -v`
Expected: All tests fail with `ImportError` or `ModuleNotFoundError`.

- [ ] **Step 3: Create the module package marker**

Create `src/common/datasources/__init__.py`:

```python
"""Multi-source data adapter module.

Public API:
- BaseDataSource: abstract base class for all source adapters
- SourceHealthStatus, SourceStatus: health-check return types
- DataSourceRegistry: orchestrator (added in Task 11)
"""
from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)

__all__ = ["BaseDataSource", "SourceHealthStatus", "SourceStatus"]
```

- [ ] **Step 4: Create base.py with the minimal implementation**

Create `src/common/datasources/base.py`:

```python
"""BaseDataSource ABC and supporting types.

Per spec §6 (BaseDataSource contract): each data source declares its name,
cadence, and the canonical fields it provides. It implements health_check()
mandatorily and the four fetch methods optionally — the ones it can't
satisfy raise NotImplementedError, which the registry treats as the source
being silent for that data category.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pandas as pd


class SourceStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class SourceHealthStatus:
    """Result of a source's health_check() call."""
    source: str
    status: SourceStatus
    checked_at: str  # ISO 8601 UTC
    message: str = ""
    response_time_ms: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        """True if status is OK or PARTIAL (i.e., usable). False if FAILED."""
        return self.status in (SourceStatus.OK, SourceStatus.PARTIAL)


class BaseDataSource(ABC):
    """Abstract base for every data source in src/common/datasources/.

    Subclasses MUST set:
      - name: str (short identifier, e.g. "finviz")
      - cadence: str ("weekly" | "biweekly" | "monthly")
      - provides: set[str] (canonical field names this source can populate)

    Subclasses MUST implement health_check().
    Subclasses MAY override any of the fetch_* methods; the defaults
    raise NotImplementedError to signal the source doesn't provide that
    data category.
    """

    name: str = ""
    cadence: str = ""
    provides: set[str] = set()

    @abstractmethod
    def health_check(self) -> SourceHealthStatus:
        """Probe the upstream service and report status.

        Implementations should make a cheap, idempotent call (e.g., HEAD,
        or fetch a known-tiny endpoint). Must not raise — convert exceptions
        into SourceStatus.FAILED.
        """
        raise NotImplementedError

    def fetch_universe(self, run_id: str) -> pd.DataFrame:
        """Return a wide DataFrame: one row per ticker, source-native columns."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide fetch_universe()"
        )

    def fetch_fundamentals_for_ticker(
        self, ticker: str, run_id: str
    ) -> dict[str, Any]:
        """Return a dict of canonical-field-name -> value for one ticker."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide fetch_fundamentals_for_ticker()"
        )

    def fetch_filings_for_ticker(
        self, ticker: str, run_id: str
    ) -> list[dict[str, Any]]:
        """Return a list of filing records (10-K/10-Q, Form 4, etc.)."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide fetch_filings_for_ticker()"
        )

    def fetch_macro_series(
        self, series_ids: list[str], run_id: str
    ) -> pd.DataFrame:
        """Return a long-format DataFrame: series_id, observation_date, value."""
        raise NotImplementedError(
            f"{type(self).__name__} does not provide fetch_macro_series()"
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/datasources/test_base.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 6: Commit**

```
git add src/common/datasources/__init__.py src/common/datasources/base.py tests/datasources/test_base.py
git commit -m "feat(datasources): add BaseDataSource ABC, SourceHealthStatus, SourceStatus"
```

---

## Task 2: resolution.py — priority-weighted resolution functions

**Files:**
- Create: `src/common/datasources/resolution.py`
- Test: `tests/datasources/test_resolution.py`

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_resolution.py`:

```python
"""Tests for priority-weighted resolution functions (spec §9)."""
from __future__ import annotations

import math
import pytest

from src.common.datasources.resolution import (
    priority_weight,
    weighted_average_value,
    highest_priority_value,
    freshest_value,
    Observation,
)


class TestPriorityWeight:
    def test_rank_1_weight(self):
        assert priority_weight(rank=1, decay=0.5) == 0.5

    def test_rank_2_weight(self):
        assert priority_weight(rank=2, decay=0.5) == 0.25

    def test_rank_3_weight(self):
        assert priority_weight(rank=3, decay=0.5) == 0.125

    def test_invalid_rank_raises(self):
        with pytest.raises(ValueError):
            priority_weight(rank=0, decay=0.5)


class TestWeightedAverageValue:
    def test_three_sources_all_present(self):
        obs = [
            Observation(source="edgar", rank=1, value=24.8),
            Observation(source="yahoo", rank=2, value=25.1),
            Observation(source="finviz", rank=3, value=24.5),
        ]
        result = weighted_average_value(obs, decay=0.5)
        # raw weights: 0.5, 0.25, 0.125 -> normalized: 4/7, 2/7, 1/7
        expected = (4/7)*24.8 + (2/7)*25.1 + (1/7)*24.5
        assert math.isclose(result.value, expected, rel_tol=1e-9)
        assert result.contributors == ["edgar", "yahoo", "finviz"]

    def test_missing_source_renormalizes(self):
        obs = [
            Observation(source="edgar", rank=1, value=24.8),
            Observation(source="finviz", rank=3, value=24.5),
        ]
        result = weighted_average_value(obs, decay=0.5)
        # raw weights: 0.5, 0.125 -> normalized: 4/5, 1/5
        expected = (4/5)*24.8 + (1/5)*24.5
        assert math.isclose(result.value, expected, rel_tol=1e-9)

    def test_outlier_dampened_by_majority(self):
        obs = [
            Observation(source="edgar",         rank=1, value=25.0),
            Observation(source="stockanalysis", rank=2, value=25.1),
            Observation(source="yahoo",         rank=3, value=24.9),
            Observation(source="finviz",        rank=4, value=150.0),
        ]
        result = weighted_average_value(obs, decay=0.5)
        # outlier rank=4 contributes ~1/15 of weight
        assert 30.0 < result.value < 40.0

    def test_empty_observations_returns_none(self):
        result = weighted_average_value([], decay=0.5)
        assert result.value is None
        assert result.contributors == []

    def test_all_null_values_returns_none(self):
        obs = [
            Observation(source="edgar", rank=1, value=None),
            Observation(source="yahoo", rank=2, value=None),
        ]
        result = weighted_average_value(obs, decay=0.5)
        assert result.value is None

    def test_single_source(self):
        obs = [Observation(source="edgar", rank=1, value=42.0)]
        result = weighted_average_value(obs, decay=0.5)
        assert result.value == 42.0
        assert result.contributors == ["edgar"]


class TestHighestPriorityValue:
    def test_picks_lowest_rank(self):
        obs = [
            Observation(source="yahoo",  rank=2, value="Technology"),
            Observation(source="edgar",  rank=1, value="Information Technology"),
            Observation(source="finviz", rank=3, value="Tech"),
        ]
        result = highest_priority_value(obs)
        assert result.value == "Information Technology"
        assert result.contributors == ["edgar"]

    def test_skips_null_high_priority(self):
        obs = [
            Observation(source="edgar",  rank=1, value=None),
            Observation(source="yahoo",  rank=2, value="Technology"),
        ]
        result = highest_priority_value(obs)
        assert result.value == "Technology"
        assert result.contributors == ["yahoo"]

    def test_empty_returns_none(self):
        result = highest_priority_value([])
        assert result.value is None


class TestFreshestValue:
    def test_picks_latest_fetched_at(self):
        obs = [
            Observation(source="finviz", rank=2, value=154.20,
                        fetched_at="2026-05-21T08:00:00Z"),
            Observation(source="yahoo",  rank=1, value=154.18,
                        fetched_at="2026-05-21T07:00:00Z"),
        ]
        result = freshest_value(obs)
        assert result.value == 154.20
        assert result.contributors == ["finviz"]

    def test_tie_broken_by_priority(self):
        obs = [
            Observation(source="finviz", rank=2, value=154.20,
                        fetched_at="2026-05-21T08:00:00Z"),
            Observation(source="yahoo",  rank=1, value=154.18,
                        fetched_at="2026-05-21T08:00:00Z"),
        ]
        result = freshest_value(obs)
        assert result.value == 154.18
        assert result.contributors == ["yahoo"]

    def test_missing_fetched_at_skipped(self):
        obs = [
            Observation(source="finviz", rank=2, value=154.20, fetched_at=None),
            Observation(source="yahoo",  rank=1, value=154.18,
                        fetched_at="2026-05-21T07:00:00Z"),
        ]
        result = freshest_value(obs)
        assert result.value == 154.18
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/datasources/test_resolution.py -v`
Expected: All tests fail with `ImportError` on `src.common.datasources.resolution`.

- [ ] **Step 3: Create resolution.py**

Create `src/common/datasources/resolution.py`:

```python
"""Priority-weighted resolution functions (spec §9).

Three resolution rules:
  - weighted_average_value: numeric fundamentals
  - highest_priority_value: categorical / identifiers
  - freshest_value:         snapshot fields (price, volume, etc.)

All three accept a list of Observation records (one per source for one
ticker x field) and return a ResolutionResult containing the canonical
value plus the list of contributing source names.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Observation:
    """One source's observation of one (ticker, field) pair."""
    source: str
    rank: int               # priority rank for this field's category (1 = highest)
    value: Any
    fetched_at: str | None = None  # ISO 8601 UTC; required for freshest_value


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of applying a resolution rule to a list of observations."""
    value: Any
    contributors: list[str]


def priority_weight(rank: int, decay: float = 0.5) -> float:
    """Raw priority weight for rank N: decay^N.

    With default decay=0.5: rank 1 -> 0.5, rank 2 -> 0.25, rank 3 -> 0.125.
    Caller normalizes across contributing observations.
    """
    if rank < 1:
        raise ValueError(f"rank must be >= 1, got {rank}")
    return decay ** rank


def weighted_average_value(
    observations: list[Observation],
    decay: float = 0.5,
) -> ResolutionResult:
    """Priority-weighted average of numeric observations.

    Steps:
      1. Drop observations whose value is None.
      2. Compute raw weight per surviving observation: decay^rank.
      3. Normalize weights to sum to 1.
      4. canonical_value = sum(w_i * value_i).
    """
    surviving = [o for o in observations if o.value is not None]
    if not surviving:
        return ResolutionResult(value=None, contributors=[])
    raw_weights = [priority_weight(o.rank, decay) for o in surviving]
    total = sum(raw_weights)
    if total == 0:
        return ResolutionResult(value=None, contributors=[])
    normalized = [w / total for w in raw_weights]
    canonical = sum(w * float(o.value) for w, o in zip(normalized, surviving))
    return ResolutionResult(
        value=canonical,
        contributors=[o.source for o in surviving],
    )


def highest_priority_value(observations: list[Observation]) -> ResolutionResult:
    """Take the value from the highest-priority (lowest rank) non-null source."""
    surviving = [o for o in observations if o.value is not None]
    if not surviving:
        return ResolutionResult(value=None, contributors=[])
    chosen = min(surviving, key=lambda o: o.rank)
    return ResolutionResult(value=chosen.value, contributors=[chosen.source])


def freshest_value(observations: list[Observation]) -> ResolutionResult:
    """Take the value from the source with the most recent fetched_at.

    Ties broken by priority rank (lower rank wins).
    Observations with fetched_at=None or value=None are skipped.
    """
    surviving = [
        o for o in observations if o.value is not None and o.fetched_at is not None
    ]
    if not surviving:
        return ResolutionResult(value=None, contributors=[])
    chosen = min(
        surviving,
        key=lambda o: (-_iso_to_sortable(o.fetched_at), o.rank),
    )
    return ResolutionResult(value=chosen.value, contributors=[chosen.source])


def _iso_to_sortable(iso: str) -> float:
    """Convert ISO 8601 UTC string to a numeric sortable POSIX timestamp."""
    from datetime import datetime
    s = iso.replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/datasources/test_resolution.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/resolution.py tests/datasources/test_resolution.py
git commit -m "feat(datasources): add priority-weighted resolution functions"
```

---

## Task 3: config/datasources.yaml

**Files:**
- Create: `config/datasources.yaml`

- [ ] **Step 1: Create the config file**

Create `config/datasources.yaml`:

```yaml
# Multi-source data adapter configuration.
# Spec: docs/design/specs/2026-05-21-multi-source-data-adapter-design.md §9.

enabled_sources:
  - finviz
  - yahoo
  - edgar
  - fred
  - stockanalysis
  - finra

source_priority:
  fundamentals:   [edgar, stockanalysis, yahoo, finviz]
  prices:         [yahoo, finviz]
  identifiers:    [edgar, yahoo, finviz]
  short_interest: [finra, finviz]
  macro:          [fred]

priority_weights:
  decay: 0.5

field_resolution:
  weighted_average:
    - pe_ttm
    - pe_forward
    - ebit_ttm
    - fcf_ttm
    - operating_margin
    - net_profit_margin
    - roe
    - roic
    - total_debt_to_equity
    - interest_coverage
    - revenue_growth_yoy
    - eps_growth_yoy
  highest_priority:
    - sector
    - industry
    - company_name
    - cik
    - exchange
    - short_interest_pct_float
  freshest:
    - price
    - market_cap_usd
    - avg_daily_volume
    - rsi_14
    - dist_52w_high
    - dist_52w_low
    - dist_200dma
    - perf_1m
    - perf_3m
    - perf_6m
    - perf_12m
  derived:
    - pe_5y_percentile
    - ev_ebitda_5y_percentile
    - data_quality_score

disagreement_threshold_log_only: 0.10

cadence_window_hours:
  weekly:    168
  biweekly:  336
  monthly:   720
```

- [ ] **Step 2: Sanity-check YAML parses**

Run:
```
python -c "import yaml; cfg = yaml.safe_load(open('config/datasources.yaml')); print(sorted(cfg.keys()))"
```

Expected output:
```
['cadence_window_hours', 'disagreement_threshold_log_only', 'enabled_sources', 'field_resolution', 'priority_weights', 'source_priority']
```

- [ ] **Step 3: Commit**

```
git add config/datasources.yaml
git commit -m "feat(config): add datasources.yaml with priority + resolution rules"
```

---

## Task 4: FinvizSource

**Files:**
- Create: `src/common/datasources/finviz_source.py`
- Test: `tests/datasources/test_finviz_source.py`

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_finviz_source.py`:

```python
"""Unit tests for FinvizSource."""
from __future__ import annotations

import pytest

from src.common.datasources.base import SourceStatus
from src.common.datasources.finviz_source import FinvizSource


class TestFinvizSourceMeta:
    def test_name(self):
        assert FinvizSource().name == "finviz"

    def test_cadence(self):
        assert FinvizSource().cadence == "weekly"

    def test_provides_includes_expected_fields(self):
        src = FinvizSource()
        for f in ("pe_ttm", "operating_margin", "net_profit_margin",
                  "perf_12m", "rsi_14", "dist_52w_high", "dist_52w_low",
                  "sector", "industry", "company_name",
                  "market_cap_usd", "price", "avg_daily_volume"):
            assert f in src.provides, f"missing {f}"


class TestFinvizSourceHealthCheck:
    def test_health_check_returns_ok(self, mocker):
        mock_df = mocker.MagicMock(empty=False)
        mock_df.__len__ = lambda self: 10
        mock_ov = mocker.MagicMock()
        mock_ov.screener_view.return_value = mock_df
        mocker.patch(
            "src.common.datasources.finviz_source.Overview",
            return_value=mock_ov,
        )
        result = FinvizSource().health_check()
        assert result.source == "finviz"
        assert result.status == SourceStatus.OK
        assert result.is_healthy

    def test_health_check_handles_exception(self, mocker):
        mocker.patch(
            "src.common.datasources.finviz_source.Overview",
            side_effect=RuntimeError("upstream broke"),
        )
        result = FinvizSource().health_check()
        assert result.status == SourceStatus.FAILED
        assert "upstream broke" in result.message
        assert not result.is_healthy
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/datasources/test_finviz_source.py -v`
Expected: ImportError on `src.common.datasources.finviz_source`.

- [ ] **Step 3: Create FinvizSource**

Create `src/common/datasources/finviz_source.py`:

```python
"""FinvizSource — adapter around finvizfinance.

For A.1 only the health_check is wired. Full fetch_universe() wrapping of
the existing src/layer1_universe/screen.py code happens in A.3 when the
screener is refactored to consume canonical_universe.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time

from finvizfinance.screener.overview import Overview

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


class FinvizSource(BaseDataSource):
    name = "finviz"
    cadence = "weekly"
    provides = {
        "pe_ttm", "pe_forward", "operating_margin", "net_profit_margin",
        "perf_1m", "perf_3m", "perf_6m", "perf_12m",
        "rsi_14", "dist_52w_high", "dist_52w_low",
        "sector", "industry", "company_name",
        "market_cap_usd", "price", "avg_daily_volume",
        "short_interest_pct_float",
    }

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            ov = Overview()
            ov.set_filter(filters_dict={"Market Cap.": "+Mega (over $200bln)"})
            df = ov.screener_view()
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        ok = df is not None and not df.empty
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK if ok else SourceStatus.PARTIAL,
            checked_at=now,
            message=f"mega-cap rows={0 if df is None else len(df)}",
            response_time_ms=(time.monotonic() - started) * 1000,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/datasources/test_finviz_source.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/finviz_source.py tests/datasources/test_finviz_source.py
git commit -m "feat(datasources): add FinvizSource with health_check"
```

---

## Task 5: YahooSource

**Files:**
- Create: `src/common/datasources/yahoo_source.py`
- Test: `tests/datasources/test_yahoo_source.py`

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_yahoo_source.py`:

```python
"""Unit tests for YahooSource."""
from __future__ import annotations

import pytest

from src.common.datasources.base import SourceStatus
from src.common.datasources.yahoo_source import YahooSource


class TestYahooSourceMeta:
    def test_name(self):
        assert YahooSource().name == "yahoo"

    def test_cadence(self):
        assert YahooSource().cadence == "weekly"

    def test_provides_includes_expected_fields(self):
        src = YahooSource()
        for f in ("pe_ttm", "pe_forward", "ebit_ttm", "fcf_ttm",
                  "operating_margin", "net_profit_margin",
                  "total_debt_to_equity", "roe", "roic",
                  "company_name", "sector", "industry",
                  "market_cap_usd", "price", "avg_daily_volume"):
            assert f in src.provides, f"missing {f}"


class TestYahooSourceHealthCheck:
    def test_health_check_ok(self, mocker):
        mock_ticker = mocker.MagicMock()
        mock_ticker.fast_info = {"last_price": 150.0}
        mocker.patch(
            "src.common.datasources.yahoo_source.yf.Ticker",
            return_value=mock_ticker,
        )
        result = YahooSource().health_check()
        assert result.source == "yahoo"
        assert result.status == SourceStatus.OK

    def test_health_check_failed(self, mocker):
        mocker.patch(
            "src.common.datasources.yahoo_source.yf.Ticker",
            side_effect=RuntimeError("network down"),
        )
        result = YahooSource().health_check()
        assert result.status == SourceStatus.FAILED
        assert "network down" in result.message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/datasources/test_yahoo_source.py -v`
Expected: ImportError.

- [ ] **Step 3: Create YahooSource**

Create `src/common/datasources/yahoo_source.py`:

```python
"""YahooSource — adapter around yfinance.

A.1 deliverable: health_check via a probe ticker (SPY) fast_info call.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time

import yfinance as yf

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


class YahooSource(BaseDataSource):
    name = "yahoo"
    cadence = "weekly"
    provides = {
        "pe_ttm", "pe_forward", "ebit_ttm", "fcf_ttm",
        "operating_margin", "net_profit_margin",
        "total_debt_to_equity", "interest_coverage",
        "revenue_growth_yoy", "eps_growth_yoy",
        "roe", "roic",
        "company_name", "sector", "industry", "exchange",
        "market_cap_usd", "price", "avg_daily_volume",
    }

    _PROBE_TICKER = "SPY"

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            t = yf.Ticker(self._PROBE_TICKER)
            info = t.fast_info
            price = info.get("last_price") if hasattr(info, "get") else info["last_price"]
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        ok = price is not None
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK if ok else SourceStatus.PARTIAL,
            checked_at=now,
            message=f"{self._PROBE_TICKER} last_price={price}",
            response_time_ms=(time.monotonic() - started) * 1000,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/datasources/test_yahoo_source.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/yahoo_source.py tests/datasources/test_yahoo_source.py
git commit -m "feat(datasources): add YahooSource with fast_info health_check"
```

---

## Task 6: EdgarSource

**Files:**
- Create: `src/common/datasources/edgar_source.py`
- Test: `tests/datasources/test_edgar_source.py`

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_edgar_source.py`:

```python
"""Unit tests for EdgarSource."""
from __future__ import annotations

import pytest

from src.common.datasources.base import SourceStatus
from src.common.datasources.edgar_source import EdgarSource


class TestEdgarSourceMeta:
    def test_name(self):
        assert EdgarSource().name == "edgar"

    def test_cadence(self):
        assert EdgarSource().cadence == "weekly"

    def test_provides_includes_filings_and_fundamentals(self):
        src = EdgarSource()
        for f in ("cik", "company_name", "revenue_ttm", "net_income_ttm",
                  "total_assets", "operating_margin", "net_profit_margin"):
            assert f in src.provides

    def test_user_agent_required(self):
        # SEC requires a real User-Agent per their fair-access policy.
        src = EdgarSource()
        assert "User-Agent" in src._headers()
        assert "@" in src._headers()["User-Agent"]  # must contain a contact email


class TestEdgarSourceHealthCheck:
    def test_health_check_ok(self, mocker):
        mock_resp = mocker.MagicMock(status_code=200, ok=True)
        mock_resp.json.return_value = {"name": "SEC"}
        mock_resp.content = b'{"name":"SEC"}'
        mocker.patch(
            "src.common.datasources.edgar_source.requests.get",
            return_value=mock_resp,
        )
        result = EdgarSource().health_check()
        assert result.source == "edgar"
        assert result.status == SourceStatus.OK

    def test_health_check_failed_on_non_200(self, mocker):
        mock_resp = mocker.MagicMock(status_code=503, ok=False)
        mocker.patch(
            "src.common.datasources.edgar_source.requests.get",
            return_value=mock_resp,
        )
        result = EdgarSource().health_check()
        assert result.status == SourceStatus.FAILED
        assert "503" in result.message

    def test_health_check_failed_on_exception(self, mocker):
        mocker.patch(
            "src.common.datasources.edgar_source.requests.get",
            side_effect=ConnectionError("dns failed"),
        )
        result = EdgarSource().health_check()
        assert result.status == SourceStatus.FAILED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/datasources/test_edgar_source.py -v`
Expected: ImportError.

- [ ] **Step 3: Create EdgarSource**

Create `src/common/datasources/edgar_source.py`:

```python
"""EdgarSource — adapter for SEC EDGAR.

SEC EDGAR requires identifying contact info in every request per
https://www.sec.gov/os/accessing-edgar-data. The User-Agent format is:
"Application Name AdminEmail@example.com".

A.1 deliverable: health_check via the EDGAR submissions endpoint.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import time

import requests

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_DEFAULT_USER_AGENT = "Trade Identifier research@example.com"
_PROBE_URL = "https://data.sec.gov/submissions/CIK0000320193.json"  # Apple


class EdgarSource(BaseDataSource):
    name = "edgar"
    cadence = "weekly"
    provides = {
        "cik", "company_name", "exchange",
        "revenue_ttm", "ebit_ttm", "net_income_ttm",
        "total_assets", "total_debt_to_equity",
        "operating_margin", "net_profit_margin",
    }

    def _headers(self) -> dict[str, str]:
        ua = os.environ.get("SEC_EDGAR_USER_AGENT", _DEFAULT_USER_AGENT)
        return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            resp = requests.get(_PROBE_URL, headers=self._headers(), timeout=10)
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        if not resp.ok:
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"HTTP {resp.status_code}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK,
            checked_at=now,
            message=f"HTTP 200 ({len(resp.content)} bytes)",
            response_time_ms=(time.monotonic() - started) * 1000,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/datasources/test_edgar_source.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/edgar_source.py tests/datasources/test_edgar_source.py
git commit -m "feat(datasources): add EdgarSource with submissions health_check"
```

---

## Task 7: FredSource

**Files:**
- Create: `src/common/datasources/fred_source.py`
- Test: `tests/datasources/test_fred_source.py`

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_fred_source.py`:

```python
"""Unit tests for FredSource."""
from __future__ import annotations

import pytest

from src.common.datasources.base import SourceStatus
from src.common.datasources.fred_source import FredSource


class TestFredSourceMeta:
    def test_name(self):
        assert FredSource().name == "fred"

    def test_cadence(self):
        assert FredSource().cadence == "weekly"

    def test_provides_macro_series(self):
        assert "risk_free_rate_10y" in FredSource().provides
        assert "vix_close" in FredSource().provides


class TestFredSourceHealthCheck:
    def test_health_check_ok(self, mocker):
        mock_resp = mocker.MagicMock(status_code=200, ok=True)
        mock_resp.text = "DATE,DGS10\n2026-05-20,4.30\n"
        mocker.patch(
            "src.common.datasources.fred_source.requests.get",
            return_value=mock_resp,
        )
        result = FredSource().health_check()
        assert result.source == "fred"
        assert result.status == SourceStatus.OK

    def test_health_check_failed_on_non_200(self, mocker):
        mock_resp = mocker.MagicMock(status_code=500, ok=False)
        mocker.patch(
            "src.common.datasources.fred_source.requests.get",
            return_value=mock_resp,
        )
        result = FredSource().health_check()
        assert result.status == SourceStatus.FAILED

    def test_health_check_failed_on_exception(self, mocker):
        mocker.patch(
            "src.common.datasources.fred_source.requests.get",
            side_effect=TimeoutError("read timeout"),
        )
        result = FredSource().health_check()
        assert result.status == SourceStatus.FAILED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/datasources/test_fred_source.py -v`
Expected: ImportError.

- [ ] **Step 3: Create FredSource**

Create `src/common/datasources/fred_source.py`:

```python
"""FredSource — Federal Reserve Economic Data (St. Louis Fed).

Uses the public fredgraph.csv endpoint which doesn't require an API key.
A.1 deliverable: health_check via DGS10 (10y Treasury).
"""
from __future__ import annotations

from datetime import datetime, timezone
import time

import requests

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_PROBE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"


class FredSource(BaseDataSource):
    name = "fred"
    cadence = "weekly"
    provides = {
        "risk_free_rate_10y",
        "risk_free_rate_3m",
        "risk_free_rate_2y",
        "vix_close",
        "cpi_yoy",
    }

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            resp = requests.get(_PROBE_URL, timeout=10)
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        if not resp.ok:
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"HTTP {resp.status_code}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        lines = resp.text.strip().split("\n")
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK,
            checked_at=now,
            message=f"DGS10 rows={len(lines) - 1}",
            response_time_ms=(time.monotonic() - started) * 1000,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/datasources/test_fred_source.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/fred_source.py tests/datasources/test_fred_source.py
git commit -m "feat(datasources): add FredSource with DGS10 health_check"
```

---

## Task 8: StockanalysisSource

**Files:**
- Create: `src/common/datasources/stockanalysis_source.py`
- Test: `tests/datasources/test_stockanalysis_source.py`

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_stockanalysis_source.py`:

```python
"""Unit tests for StockanalysisSource."""
from __future__ import annotations

import pytest

from src.common.datasources.base import SourceStatus
from src.common.datasources.stockanalysis_source import StockanalysisSource


class TestStockanalysisSourceMeta:
    def test_name(self):
        assert StockanalysisSource().name == "stockanalysis"

    def test_cadence(self):
        assert StockanalysisSource().cadence == "weekly"

    def test_provides_history_fields(self):
        src = StockanalysisSource()
        for f in ("pe_ttm", "pe_5y_history_raw", "ev_ebitda_5y_history_raw"):
            assert f in src.provides


class TestStockanalysisSourceHealthCheck:
    def test_health_check_ok(self, mocker):
        mock_resp = mocker.MagicMock(status_code=200, ok=True)
        mock_resp.content = b"<html><body>AAPL ratios</body></html>"
        mocker.patch(
            "src.common.datasources.stockanalysis_source.requests.get",
            return_value=mock_resp,
        )
        result = StockanalysisSource().health_check()
        assert result.status == SourceStatus.OK

    def test_health_check_fails_on_404(self, mocker):
        mock_resp = mocker.MagicMock(status_code=404, ok=False)
        mocker.patch(
            "src.common.datasources.stockanalysis_source.requests.get",
            return_value=mock_resp,
        )
        result = StockanalysisSource().health_check()
        assert result.status == SourceStatus.FAILED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/datasources/test_stockanalysis_source.py -v`
Expected: ImportError.

- [ ] **Step 3: Create StockanalysisSource**

Create `src/common/datasources/stockanalysis_source.py`:

```python
"""StockanalysisSource — adapter for stockanalysis.com.

Used primarily for multi-year ratio history (P/E, EV/EBITDA) needed
to compute pe_5y_percentile and ev_ebitda_5y_percentile per spec §9
"derived" fields.

A.1 deliverable: health_check via a known stable URL.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time

import requests

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_PROBE_URL = "https://stockanalysis.com/stocks/aapl/financials/ratios/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Trade Identifier; research@example.com)"
    ),
}


class StockanalysisSource(BaseDataSource):
    name = "stockanalysis"
    cadence = "weekly"
    provides = {
        "pe_ttm", "pe_forward",
        "ebit_ttm", "fcf_ttm",
        "operating_margin", "net_profit_margin",
        "roe", "roic",
        "pe_5y_history_raw",
        "ev_ebitda_5y_history_raw",
    }

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            resp = requests.get(_PROBE_URL, headers=_HEADERS, timeout=10)
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        if not resp.ok:
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"HTTP {resp.status_code}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK,
            checked_at=now,
            message=f"HTTP 200 ({len(resp.content)} bytes)",
            response_time_ms=(time.monotonic() - started) * 1000,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/datasources/test_stockanalysis_source.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/stockanalysis_source.py tests/datasources/test_stockanalysis_source.py
git commit -m "feat(datasources): add StockanalysisSource for 10y ratio history"
```

---

## Task 9: FinraSource

**Files:**
- Create: `src/common/datasources/finra_source.py`
- Test: `tests/datasources/test_finra_source.py`

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_finra_source.py`:

```python
"""Unit tests for FinraSource."""
from __future__ import annotations

import pytest

from src.common.datasources.base import SourceStatus
from src.common.datasources.finra_source import FinraSource


class TestFinraSourceMeta:
    def test_name(self):
        assert FinraSource().name == "finra"

    def test_cadence_is_biweekly(self):
        assert FinraSource().cadence == "biweekly"

    def test_provides_short_interest(self):
        assert "short_interest_pct_float" in FinraSource().provides


class TestFinraSourceHealthCheck:
    def test_health_check_ok(self, mocker):
        mock_resp = mocker.MagicMock(status_code=200, ok=True)
        mock_resp.text = "OK"
        mocker.patch(
            "src.common.datasources.finra_source.requests.get",
            return_value=mock_resp,
        )
        result = FinraSource().health_check()
        assert result.status == SourceStatus.OK

    def test_health_check_failed_on_5xx(self, mocker):
        mock_resp = mocker.MagicMock(status_code=502, ok=False)
        mocker.patch(
            "src.common.datasources.finra_source.requests.get",
            return_value=mock_resp,
        )
        result = FinraSource().health_check()
        assert result.status == SourceStatus.FAILED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/datasources/test_finra_source.py -v`
Expected: ImportError.

- [ ] **Step 3: Create FinraSource**

Create `src/common/datasources/finra_source.py`:

```python
"""FinraSource — FINRA short interest reports.

FINRA publishes short interest twice monthly. A.1 deliverable: health_check
by probing the public download landing page.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time

import requests

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_PROBE_URL = "https://www.finra.org/finra-data/browse-catalog/short-sale-volume-daily/files"


class FinraSource(BaseDataSource):
    name = "finra"
    cadence = "biweekly"
    provides = {"short_interest_pct_float"}

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            resp = requests.get(_PROBE_URL, timeout=10)
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        if not resp.ok:
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"HTTP {resp.status_code}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK,
            checked_at=now,
            message="HTTP 200",
            response_time_ms=(time.monotonic() - started) * 1000,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/datasources/test_finra_source.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```
git add src/common/datasources/finra_source.py tests/datasources/test_finra_source.py
git commit -m "feat(datasources): add FinraSource with short_interest health_check"
```

---

## Task 10: Layer-2 scaffold stubs

**Files:**
- Create: `src/common/datasources/_layer2_scaffold/__init__.py`
- Create: `src/common/datasources/_layer2_scaffold/yahoo_news_source.py`
- Create: `src/common/datasources/_layer2_scaffold/gdelt_source.py`
- Test: `tests/datasources/test_layer2_scaffold.py`

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_layer2_scaffold.py`:

```python
"""Layer-2 scaffold stubs: registered as classes but raise NotImplementedError
when fetch methods are called. Acceptance per spec §11 A.1."""
from __future__ import annotations

import pytest

from src.common.datasources._layer2_scaffold.yahoo_news_source import YahooNewsSource
from src.common.datasources._layer2_scaffold.gdelt_source import GdeltSource
from src.common.datasources.base import SourceStatus


class TestYahooNewsScaffold:
    def test_can_instantiate(self):
        assert YahooNewsSource().name == "yahoo_news"

    def test_health_check_returns_partial_not_failed(self):
        result = YahooNewsSource().health_check()
        assert result.status == SourceStatus.PARTIAL
        assert "Layer 2" in result.message

    def test_fetch_methods_raise_not_implemented_with_clear_message(self):
        src = YahooNewsSource()
        with pytest.raises(NotImplementedError, match="Layer 2"):
            src.fetch_universe(run_id="r")


class TestGdeltScaffold:
    def test_can_instantiate(self):
        assert GdeltSource().name == "gdelt"

    def test_health_check_returns_partial(self):
        result = GdeltSource().health_check()
        assert result.status == SourceStatus.PARTIAL

    def test_fetch_methods_raise_not_implemented(self):
        src = GdeltSource()
        with pytest.raises(NotImplementedError, match="Layer 2"):
            src.fetch_universe(run_id="r")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/datasources/test_layer2_scaffold.py -v`
Expected: ImportError.

- [ ] **Step 3: Create the scaffold package marker**

Create `src/common/datasources/_layer2_scaffold/__init__.py`:

```python
"""Layer-2 source scaffolds.

These classes exist to validate that the BaseDataSource interface
accommodates news / events / sentiment data shapes BEFORE Layer 2 is
brainstormed and built. They MUST NOT be registered in
config/datasources.yaml's enabled_sources.

All fetch_* methods raise NotImplementedError. Implementations land in
the Layer 2 design phase.
"""
```

- [ ] **Step 4: Create YahooNewsSource scaffold**

Create `src/common/datasources/_layer2_scaffold/yahoo_news_source.py`:

```python
"""Scaffold for Yahoo Finance news RSS — Layer 2."""
from __future__ import annotations

from datetime import datetime, timezone

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)

_LAYER2_MSG = "Activated in Layer 2 — see future Layer 2 design doc"


class YahooNewsSource(BaseDataSource):
    name = "yahoo_news"
    cadence = "daily"
    provides = {"news_headlines", "news_sentiment_raw"}

    def health_check(self) -> SourceHealthStatus:
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.PARTIAL,
            checked_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            message=f"scaffold stub — {_LAYER2_MSG}",
        )

    def fetch_universe(self, run_id: str):
        raise NotImplementedError(f"{type(self).__name__}: {_LAYER2_MSG}")

    def fetch_fundamentals_for_ticker(self, ticker: str, run_id: str):
        raise NotImplementedError(f"{type(self).__name__}: {_LAYER2_MSG}")

    def fetch_filings_for_ticker(self, ticker: str, run_id: str):
        raise NotImplementedError(f"{type(self).__name__}: {_LAYER2_MSG}")

    def fetch_macro_series(self, series_ids, run_id: str):
        raise NotImplementedError(f"{type(self).__name__}: {_LAYER2_MSG}")
```

- [ ] **Step 5: Create GdeltSource scaffold**

Create `src/common/datasources/_layer2_scaffold/gdelt_source.py`:

```python
"""Scaffold for GDELT global events database — Layer 2."""
from __future__ import annotations

from datetime import datetime, timezone

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)

_LAYER2_MSG = "Activated in Layer 2 — see future Layer 2 design doc"


class GdeltSource(BaseDataSource):
    name = "gdelt"
    cadence = "daily"
    provides = {"global_events_raw", "event_sentiment_raw"}

    def health_check(self) -> SourceHealthStatus:
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.PARTIAL,
            checked_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            message=f"scaffold stub — {_LAYER2_MSG}",
        )

    def fetch_universe(self, run_id: str):
        raise NotImplementedError(f"{type(self).__name__}: {_LAYER2_MSG}")

    def fetch_fundamentals_for_ticker(self, ticker: str, run_id: str):
        raise NotImplementedError(f"{type(self).__name__}: {_LAYER2_MSG}")

    def fetch_filings_for_ticker(self, ticker: str, run_id: str):
        raise NotImplementedError(f"{type(self).__name__}: {_LAYER2_MSG}")

    def fetch_macro_series(self, series_ids, run_id: str):
        raise NotImplementedError(f"{type(self).__name__}: {_LAYER2_MSG}")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/datasources/test_layer2_scaffold.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 7: Commit**

```
git add src/common/datasources/_layer2_scaffold/ tests/datasources/test_layer2_scaffold.py
git commit -m "feat(datasources): add Layer-2 scaffold stubs (yahoo_news, gdelt)"
```

---

## Task 11: DataSourceRegistry

**Files:**
- Create: `src/common/datasources/registry.py`
- Modify: `src/common/datasources/__init__.py`
- Test: `tests/datasources/test_registry.py`

- [ ] **Step 1: Write the failing test**

Create `tests/datasources/test_registry.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/datasources/test_registry.py -v`
Expected: ImportError.

- [ ] **Step 3: Create registry.py**

Create `src/common/datasources/registry.py`:

```python
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
```

- [ ] **Step 4: Update __init__.py to export the registry**

Replace `src/common/datasources/__init__.py` with:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/datasources/test_registry.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 6: Run the full non-integration suite to confirm no regressions**

Run: `pytest -m "not integration" -v`
Expected: All unit tests PASS (approximately 45 tests across 9 files).

- [ ] **Step 7: Commit**

```
git add src/common/datasources/registry.py src/common/datasources/__init__.py tests/datasources/test_registry.py
git commit -m "feat(datasources): add DataSourceRegistry with config loading + health_check_all"
```

---

## Task 12: Integration test — Phase A.1 acceptance

**Files:**
- Create: `tests/datasources/test_integration_health.py`

- [ ] **Step 1: Write the integration test**

Create `tests/datasources/test_integration_health.py`:

```python
"""Integration test — runs the real health_check against every enabled source.

Marked @pytest.mark.integration so it's excluded from the default run
(pytest -m "not integration"). Run explicitly with:
    pytest -m integration -v

This test validates Phase A.1's acceptance criterion (spec §11):
"Each source's health_check() passes; scaffold stubs raise
NotImplementedError cleanly."

A source may return PARTIAL (upstream slow, etc.). PARTIAL is still
considered healthy. Only FAILED counts as failing the acceptance.
"""
from __future__ import annotations

import pytest

from src.common.datasources import DataSourceRegistry, SourceStatus


@pytest.mark.integration
class TestPhaseA1Acceptance:
    """Spec §11 A.1 acceptance: every active source's health_check passes."""

    def test_all_enabled_sources_health_check_ok(self):
        reg = DataSourceRegistry()
        results = reg.health_check_all()
        assert len(results) == 6  # six active sources from default config

        failed = [r for r in results if r.status == SourceStatus.FAILED]
        if failed:
            msg = "\n".join(f"{r.source}: {r.message}" for r in failed)
            pytest.fail(f"sources failed health_check:\n{msg}")

    def test_scaffold_stubs_raise_not_implemented(self):
        from src.common.datasources._layer2_scaffold.yahoo_news_source import (
            YahooNewsSource,
        )
        from src.common.datasources._layer2_scaffold.gdelt_source import (
            GdeltSource,
        )
        for cls in (YahooNewsSource, GdeltSource):
            src = cls()
            with pytest.raises(NotImplementedError, match="Layer 2"):
                src.fetch_universe(run_id="acceptance")
```

- [ ] **Step 2: Run the integration test**

Run: `pytest -m integration -v`

Expected: 2 tests PASS. May take 30–60 seconds due to real network calls.

If any source fails — investigate that source's `health_check()` implementation, the upstream URL, or any required headers/credentials (e.g., SEC EDGAR User-Agent). Fix the source and re-run. Do not skip the failure: Phase A.1's acceptance criterion is gated on this passing.

- [ ] **Step 3: Run the full test suite (unit + integration)**

Run: `pytest -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```
git add tests/datasources/test_integration_health.py
git commit -m "test(datasources): add Phase A.1 acceptance integration test"
```

---

## Task 13: Update the build plan with Phase A sub-phases

**Files:**
- Modify: the build plan (Phase A section, §5)

- [ ] **Step 1: Locate the Phase A section**

Find §5 ("Phase A — Harden Layer 1") and its §5.1 ("Deliverables") in the build plan.

- [ ] **Step 2: Insert sub-phase ordering**

After the existing §5.1 introduction and before its existing bullet list, insert a new sub-section §5.1.0. The exact text to insert:

````markdown
### 5.1.0 Sub-phase ordering

Phase A is decomposed into ordered sub-phases per the multi-source data adapter
design spec (`docs/design/specs/2026-05-21-multi-source-data-adapter-design.md`).
Each sub-phase is acceptance-tested before the next begins, per §13 handoff protocol.

| Sub-phase | Plan file | Deliverable | Acceptance |
|---|---|---|---|
| **A.1** | `docs/design/plans/2026-05-21-multi-source-data-adapter-phase-a1.md` | `src/common/datasources/` framework + six source classes (Finviz/Yahoo/EDGAR/FRED/stockanalysis/FINRA) with `health_check()`; two Layer-2 scaffold stubs; `config/datasources.yaml` | Every active source's `health_check()` passes (`pytest -m integration`); scaffold stubs raise `NotImplementedError` |
| **A.2** | (to be written after A.1 lands) | SQLite migration: `raw_<source>`, `canonical_universe`, `field_provenance`, `source_run_log` tables; Pydantic models in `schemas.py` | Migration idempotent; existing `finviz_universe_history` preserved as `raw_finviz` view |
| **A.3** | (to be written after A.2 lands) | Refactor `screen.py` + `factors.py` to consume `canonical_universe`; implement remaining `fetch_*` methods on each source | `run_layer1()` public signature unchanged; fixture-data outputs equivalent |
| **A.4** | (to be written after A.3 lands) | Notebook extension: Source Status, Provenance Summary, Per-Ticker Drilldown; `ENABLED_SOURCES` + `FORCE_REFRESH_SOURCES` controls | Notebook runs end-to-end against fixture data |
| **A.5** | (to be written after A.4 lands) | Deliberately-corrupted-value acceptance test; coverage targets per §11 | Corrupted source value flagged in provenance, dampened in canonical |
````

- [ ] **Step 3: Commit**

```
git add <build-plan>
git commit -m "docs: amend Phase A with sub-phase ordering A.1-A.5"
```

---

## Phase A.1 — Definition of Done

A.1 is complete when **all** of these are true:

- [ ] `pytest -m "not integration" -v` shows all unit tests passing (~50 tests across 9 test files)
- [ ] `pytest -m integration -v` shows 2 acceptance tests passing
- [ ] `python -c "from src.common.datasources import DataSourceRegistry; print([s.name for s in DataSourceRegistry().enabled_sources()])"` prints `['finviz', 'yahoo', 'edgar', 'fred', 'stockanalysis', 'finra']`
- [ ] `python -c "from src.common.datasources import DataSourceRegistry; [print(r.source, r.status.value, r.message) for r in DataSourceRegistry().health_check_all()]"` prints six lines, none with `failed` status
- [ ] The build plan §5.1.0 documents the sub-phase ordering
- [ ] Layer 1 notebook (`notebooks/layer1_control.ipynb`) still runs without modification
- [ ] No file added under `src/layer1_universe/` (A.1 is framework only)
- [ ] No SQLite schema change made (that's A.2)
- [ ] Git log shows ~13 small commits, one per task

---

## Self-review

**Spec coverage** — every section of the spec mapped to a task:

| Spec section | Covered in |
|---|---|
| §4 active source set (6 sources) | Tasks 4–9 |
| §4 Layer-2 scaffold | Task 10 |
| §5 module layout | Tasks 1, 10, 11 |
| §6 BaseDataSource contract | Task 1 |
| §7 DataSourceRegistry orchestration (health_check_all part) | Task 11 |
| §9 resolution policy functions | Task 2 |
| §9 datasources.yaml config | Task 3 |
| §11 A.1 acceptance | Task 12 |
| §13 error handling (per-source isolation in registry) | Task 11 (test_health_check_isolates_failures) |
| §14 separate config file | Task 3 |
| §15 cadence window enforcement (config keys) | Task 3 (`cadence_window_hours` in YAML — enforcement logic deferred to A.3 where `fetch_all` lives) |

**Items deferred** (correctly out of A.1 scope, called out at top of plan):
- `registry.fetch_all()` / `fetch_canonical_universe()` → A.3 when canonical table exists
- SQLite tables → A.2
- Full fetch_* implementations → A.2/A.3

**Placeholder scan:** No "TBD", "TODO", or "implement later" in the plan. Every code step contains the full source to write.

**Type consistency:** `SourceHealthStatus`, `SourceStatus`, `Observation`, `ResolutionResult` are defined exactly once and referenced consistently across tasks. `health_check()` signature identical across all six source classes. Field names in `provides` sets match canonical field categories in `config/datasources.yaml`.

---

## Execution Handoff

Plan complete and saved to `docs/design/plans/2026-05-21-multi-source-data-adapter-phase-a1.md`. Two execution options:

**1. Subagent-Driven (recommended)** — Each task dispatched to a fresh subagent, with review between tasks. Best for pacing consumption and catching design slips early.

**2. Inline Execution** — Tasks run in this session using `superpowers:executing-plans`, with batch checkpoints for review.

Once Phase A.1 lands and its acceptance tests pass, the same brainstorm → spec → plan → execute cycle runs for Phase A.2 (storage migration).
