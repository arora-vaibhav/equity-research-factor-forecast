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
        # As of A.3.8 the default registry has 8 active sources:
        # finviz, yahoo, edgar, fred, stockanalysis, finra, gdelt,
        # pytrends. (openbb is enabled separately when keys are
        # present.) The assertion is >= 6 so newly-added sources
        # in later sub-phases don't break this acceptance test.
        assert len(results) >= 6

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
