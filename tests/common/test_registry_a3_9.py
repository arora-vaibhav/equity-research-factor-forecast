"""Tests for DataSourceRegistry A.3.9 additions (EdgarSource lm_tone provides).

A.3.9 doesn't add a new source class -- it extends the existing
EdgarSource's `provides` set with `'lm_tone'` and adds the
`fetch_filing_text_for_ticker` method. This test verifies:

  * `edgar.provides` contains `'lm_tone'`
  * `fetch_filing_text_for_ticker` is a callable method on EdgarSource
  * EDGAR is the unique provider of lm_tone

No network. Pure registry / introspection.
"""
from __future__ import annotations

import pytest

from src.common.datasources.registry import DataSourceRegistry


@pytest.fixture
def registry() -> DataSourceRegistry:
    return DataSourceRegistry()


def test_edgar_in_enabled_sources(registry):
    names = [s.name for s in registry.enabled_sources()]
    assert "edgar" in names


def test_edgar_provides_lm_tone(registry):
    edgar = next(s for s in registry.enabled_sources() if s.name == "edgar")
    assert "lm_tone" in edgar.provides


def test_edgar_exposes_fetch_filing_text_for_ticker(registry):
    edgar = next(s for s in registry.enabled_sources() if s.name == "edgar")
    assert callable(getattr(edgar, "fetch_filing_text_for_ticker", None))


def test_only_edgar_provides_lm_tone(registry):
    """LM tone is sourced exclusively from EDGAR filing text. No other
    source should claim it -- if one does, field-resolution would have
    ambiguity to break, which we never want for this field."""
    providers = [
        s.name for s in registry.enabled_sources() if "lm_tone" in s.provides
    ]
    assert providers == ["edgar"]
