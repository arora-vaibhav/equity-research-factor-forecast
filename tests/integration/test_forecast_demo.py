"""Phase F.4 end-to-end demo smoke test.

Runs scripts.forecast_demo.run_synthetic_demo against tmp_path and
asserts that one HTML file per ticker in TEST_TICKERS is written, plus
an index.html linking them.

@pytest.mark.integration -- not in the default suite.
"""
from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


def test_forecast_demo_produces_five_reports(tmp_path):
    from scripts.forecast_demo import TEST_TICKERS, run_synthetic_demo
    paths = run_synthetic_demo(
        output_dir=tmp_path / "reports",
        as_of_date="2026-05-23",
        horizon_days=30,
    )
    for ticker in TEST_TICKERS:
        assert ticker in paths
        assert paths[ticker].exists()
        html = paths[ticker].read_text(encoding="utf-8")
        assert ticker in html

    assert "__index__" in paths
    index = paths["__index__"]
    assert index.exists()
    index_html = index.read_text(encoding="utf-8")
    for ticker in TEST_TICKERS:
        assert f'href="{ticker}.html"' in index_html


def test_test_ticker_set_is_nonempty_list_of_strings():
    """TEST_TICKERS in the demo script must be a non-empty list of upper-case tickers."""
    from scripts.forecast_demo import TEST_TICKERS
    assert isinstance(TEST_TICKERS, list)
    assert len(TEST_TICKERS) > 0
    assert all(isinstance(t, str) and t == t.upper() for t in TEST_TICKERS)
