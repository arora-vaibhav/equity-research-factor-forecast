"""Unit tests for src.layer3_forecast.free_sentiment.

All HTTP and yfinance calls are mocked. No real network use.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.layer3_forecast.free_sentiment import (
    FREE_SENTIMENT_VERSION,
    SentimentSnapshot,
    fetch_apewisdom_mentions,
    fetch_iv_skew_25d,
    fetch_sentiment_snapshot,
    fetch_stocktwits_sentiment,
)


def _mock_resp(payload: dict, ok: bool = True) -> MagicMock:
    r = MagicMock()
    r.ok = ok
    r.json.return_value = payload
    return r


class TestFetchStocktwitsSentiment:
    @patch("src.layer3_forecast.free_sentiment.requests.get")
    def test_counts_bull_and_bear_tags(self, mock_get) -> None:
        mock_get.return_value = _mock_resp({
            "messages": [
                {"entities": {"sentiment": {"basic": "Bullish"}}},
                {"entities": {"sentiment": {"basic": "Bullish"}}},
                {"entities": {"sentiment": {"basic": "Bearish"}}},
                {"entities": {"sentiment": None}},  # untagged
                {"entities": {}},                     # missing
            ],
        })
        out = fetch_stocktwits_sentiment("X")
        assert out["bull_count"] == 2
        assert out["bear_count"] == 1
        assert out["message_volume"] == 5
        assert out["net_sentiment"] == pytest.approx((2 - 1) / 3)
        assert out["available"] is True

    @patch("src.layer3_forecast.free_sentiment.requests.get")
    def test_empty_messages_returns_zero_net(self, mock_get) -> None:
        mock_get.return_value = _mock_resp({"messages": []})
        out = fetch_stocktwits_sentiment("X")
        assert out["bull_count"] == 0
        assert out["bear_count"] == 0
        assert out["net_sentiment"] == 0.0
        assert out["available"] is True

    @patch("src.layer3_forecast.free_sentiment.requests.get")
    def test_http_error_returns_unavailable(self, mock_get) -> None:
        mock_get.return_value = _mock_resp({}, ok=False)
        out = fetch_stocktwits_sentiment("X")
        assert out["available"] is False

    @patch("src.layer3_forecast.free_sentiment.requests.get")
    def test_exception_returns_unavailable(self, mock_get) -> None:
        mock_get.side_effect = RuntimeError("network down")
        out = fetch_stocktwits_sentiment("X")
        assert out["available"] is False


class TestFetchApewisdomMentions:
    @patch("src.layer3_forecast.free_sentiment.requests.get")
    def test_finds_ticker_on_page_1(self, mock_get) -> None:
        mock_get.return_value = _mock_resp({
            "results": [
                {"ticker": "X", "mentions": "120", "mentions_24h_ago": "100", "rank": "5"},
                {"ticker": "Y", "mentions": "30", "mentions_24h_ago": "10", "rank": "20"},
            ],
        })
        out = fetch_apewisdom_mentions("X")
        assert out["mentions"] == 120
        assert out["mentions_24h_change"] == 20
        assert out["rank"] == 5
        assert out["available"] is True

    @patch("src.layer3_forecast.free_sentiment.requests.get")
    def test_missing_ticker_returns_zero_mentions(self, mock_get) -> None:
        mock_get.return_value = _mock_resp({"results": [{"ticker": "OTHER", "mentions": 10}]})
        out = fetch_apewisdom_mentions("MISSING")
        # ticker isn't on any page -> empty results but `available` still True
        assert out["mentions"] == 0
        assert out["available"] is True


class TestFetchIvSkew25d:
    def test_no_options_returns_unavailable(self) -> None:
        tk = MagicMock()
        tk.options = []
        factory = MagicMock(return_value=tk)
        out = fetch_iv_skew_25d("X", yf_ticker_factory=factory)
        assert out["available"] is False

    def test_typical_skew_bearish(self) -> None:
        import datetime as dt
        future_date = (dt.date.today() + dt.timedelta(days=45)).isoformat()
        # Wide strike grid so the BS-delta interpolation has +/-0.25 covered
        calls = pd.DataFrame({
            "strike": [60, 80, 100, 120, 140, 160],
            "impliedVolatility": [0.45, 0.36, 0.30, 0.28, 0.27, 0.26],
        })
        puts = pd.DataFrame({
            "strike": [60, 80, 100, 120, 140, 160],
            "impliedVolatility": [0.55, 0.45, 0.38, 0.34, 0.32, 0.31],
        })
        tk = MagicMock()
        tk.options = [future_date]
        tk.history.return_value = pd.DataFrame(
            {"Close": [100.0, 100.0, 100.0, 100.0, 100.0]}
        )
        tk.option_chain.return_value = MagicMock(calls=calls, puts=puts)
        factory = MagicMock(return_value=tk)
        out = fetch_iv_skew_25d("X", yf_ticker_factory=factory)
        assert out["available"] is True
        assert out["iv_skew"] is not None
        assert out["iv_skew"] > 0
        assert out["interpretation"] == "bearish"

    def test_zero_iv_returns_unavailable(self) -> None:
        import datetime as dt
        future_date = (dt.date.today() + dt.timedelta(days=45)).isoformat()
        calls = pd.DataFrame({"strike": [100, 110], "impliedVolatility": [0.0, 0.0]})
        puts = pd.DataFrame({"strike": [70, 80], "impliedVolatility": [0.0, 0.0]})
        tk = MagicMock()
        tk.options = [future_date]
        tk.option_chain.return_value = MagicMock(calls=calls, puts=puts)
        factory = MagicMock(return_value=tk)
        out = fetch_iv_skew_25d("X", yf_ticker_factory=factory)
        assert out["available"] is False


class TestFetchSentimentSnapshot:
    @patch("src.layer3_forecast.free_sentiment.requests.get")
    def test_aggregates_all_three_sources_and_clips(self, mock_get) -> None:
        st_payload = {
            "messages": [
                {"entities": {"sentiment": {"basic": "Bullish"}}}
                for _ in range(8)
            ] + [
                {"entities": {"sentiment": {"basic": "Bearish"}}}
                for _ in range(2)
            ],
        }
        aw_payload = {
            "results": [{"ticker": "X", "mentions": "500",
                         "mentions_24h_ago": "100", "rank": "10"}],
        }
        # stocktwits call, then ApeWisdom page 1
        mock_get.side_effect = [_mock_resp(st_payload), _mock_resp(aw_payload)]

        import datetime as dt
        future_date = (dt.date.today() + dt.timedelta(days=45)).isoformat()
        calls = pd.DataFrame({
            "strike": [60, 80, 100, 120, 140, 160],
            "impliedVolatility": [0.45, 0.36, 0.30, 0.28, 0.27, 0.26],
        })
        puts = pd.DataFrame({
            "strike": [60, 80, 100, 120, 140, 160],
            "impliedVolatility": [0.55, 0.45, 0.38, 0.34, 0.32, 0.31],
        })
        tk = MagicMock()
        tk.options = [future_date]
        tk.history.return_value = pd.DataFrame(
            {"Close": [100.0, 100.0, 100.0, 100.0, 100.0]}
        )
        tk.option_chain.return_value = MagicMock(calls=calls, puts=puts)
        factory = MagicMock(return_value=tk)

        snap = fetch_sentiment_snapshot("X", yf_ticker_factory=factory)
        assert isinstance(snap, SentimentSnapshot)
        assert snap.ticker == "X"
        assert snap.composite_n_signals == 3
        assert set(snap.sources_available) == {"stocktwits", "apewisdom", "iv_skew"}
        assert -3.0 <= snap.composite_score <= 3.0  # clipped

    def test_empty_snapshot_when_all_disabled(self) -> None:
        snap = fetch_sentiment_snapshot(
            "X",
            include_stocktwits=False, include_apewisdom=False,
            include_iv_skew=False,
        )
        assert snap.composite_n_signals == 0
        assert snap.composite_score == 0.0
        assert snap.sources_available == []
        assert snap.free_sentiment_version == FREE_SENTIMENT_VERSION
