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
