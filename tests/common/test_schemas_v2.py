"""Tests for v2 Pydantic models added in Phase A.2."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.common.schemas import (
    CanonicalUniverseRow,
    ThesisObjectRow,
    FieldProvenanceRow,
    SourceRunLogRow,
)


class TestCanonicalUniverseRow:
    def test_minimal_valid(self):
        row = CanonicalUniverseRow(run_id="r1", ticker="AAPL")
        assert row.run_id == "r1"
        assert row.ticker == "AAPL"
        assert row.pe_ttm is None

    def test_full_valid(self):
        row = CanonicalUniverseRow(
            run_id="r1",
            ticker="AAPL",
            company_name="Apple Inc",
            sector="Technology",
            industry="Consumer Electronics",
            exchange="NASDAQ",
            cik="0000320193",
            market_cap_usd=3_000_000_000_000.0,
            price=150.0,
            avg_daily_volume=50_000_000,
            pe_ttm=24.5,
            pe_forward=22.1,
            ebit_ttm=120_000_000_000.0,
            fcf_ttm=110_000_000_000.0,
            operating_margin=0.30,
            net_profit_margin=0.25,
            roe=1.40,
            roic=0.55,
            total_debt_to_equity=2.1,
            interest_coverage=42.0,
            revenue_growth_yoy=0.08,
            eps_growth_yoy=0.10,
            perf_1m=0.03,
            perf_3m=0.08,
            perf_6m=0.15,
            perf_12m=0.22,
            rsi_14=58.0,
            dist_52w_high=-0.05,
            dist_52w_low=0.40,
            dist_200dma=0.06,
            short_interest_pct_float=0.012,
            news_activity_score=0.4,
            pe_5y_percentile=0.65,
            ev_ebitda_5y_percentile=0.60,
            data_quality_score=0.92,
        )
        assert row.market_cap_usd > 0
        assert -1.0 <= row.operating_margin <= 1.0
        assert 0.0 <= row.data_quality_score <= 1.0

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            CanonicalUniverseRow(run_id="r1", ticker="not-a-ticker")

    def test_data_quality_score_out_of_range_rejected(self):
        with pytest.raises(Exception):
            CanonicalUniverseRow(run_id="r1", ticker="AAPL", data_quality_score=1.5)


class TestThesisObjectRow:
    def test_minimal_valid(self):
        row = ThesisObjectRow(
            run_id="r1",
            ticker="AAPL",
            direction="bullish",
            target_price=200.0,
            horizon_days=90,
            confidence_prior=0.7,
        )
        assert row.direction == "bullish"
        assert row.invalidation_conditions == []
        assert row.macro_context == {}

    def test_direction_must_be_in_literal(self):
        with pytest.raises(Exception):
            ThesisObjectRow(
                run_id="r1",
                ticker="AAPL",
                direction="sideways",
                target_price=200.0,
                horizon_days=90,
                confidence_prior=0.7,
            )

    def test_horizon_days_must_be_positive(self):
        with pytest.raises(Exception):
            ThesisObjectRow(
                run_id="r1",
                ticker="AAPL",
                direction="bullish",
                target_price=200.0,
                horizon_days=0,
                confidence_prior=0.7,
            )

    def test_confidence_prior_bounded(self):
        with pytest.raises(Exception):
            ThesisObjectRow(
                run_id="r1",
                ticker="AAPL",
                direction="bullish",
                target_price=200.0,
                horizon_days=90,
                confidence_prior=1.5,
            )


class TestFieldProvenanceRow:
    def test_basic(self):
        row = FieldProvenanceRow(
            run_id="r1",
            ticker="AAPL",
            field="pe_ttm",
            source="edgar",
            raw_value="24.5",
            parsed_value=24.5,
            weight=0.5,
            contributed_to_canonical=True,
            disagreement_pct=0.02,
            fetched_at="2026-05-21T08:00:00Z",
        )
        assert row.contributed_to_canonical is True
        assert row.weight == 0.5

    def test_weight_in_unit_interval(self):
        with pytest.raises(Exception):
            FieldProvenanceRow(
                run_id="r1",
                ticker="AAPL",
                field="pe_ttm",
                source="edgar",
                raw_value="x",
                parsed_value=None,
                weight=1.5,
                contributed_to_canonical=False,
                disagreement_pct=None,
                fetched_at="2026-05-21T08:00:00Z",
            )


class TestSourceRunLogRow:
    def test_basic_ok(self):
        row = SourceRunLogRow(
            run_id="r1",
            source="finviz",
            started_at="2026-05-21T08:00:00Z",
            finished_at="2026-05-21T08:00:02Z",
            status="ok",
            rows_fetched=915,
            error_message=None,
            error_traceback=None,
        )
        assert row.status == "ok"
        assert row.rows_fetched == 915

    def test_status_literal(self):
        with pytest.raises(Exception):
            SourceRunLogRow(
                run_id="r1",
                source="finviz",
                started_at="2026-05-21T08:00:00Z",
                finished_at="2026-05-21T08:00:02Z",
                status="weird",
                rows_fetched=0,
            )


from src.common.schemas import (  # noqa: E402
    HistoricalPriceRow,
    HistoricalIVRow,
    HistoricalEarningsReactionRow,
)


class TestHistoricalPriceRow:
    def test_basic(self):
        row = HistoricalPriceRow(
            ticker="AAPL",
            observation_date="2026-05-20",
            open=148.0,
            high=152.0,
            low=147.5,
            close=151.2,
            volume=50_000_000,
            adj_close=151.2,
            source="yahoo",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert row.high >= row.low
        assert row.volume >= 0

    def test_negative_volume_rejected(self):
        with pytest.raises(Exception):
            HistoricalPriceRow(
                ticker="AAPL",
                observation_date="2026-05-20",
                open=148.0,
                high=152.0,
                low=147.5,
                close=151.2,
                volume=-100,
                source="yahoo",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_high_below_low_rejected(self):
        with pytest.raises(Exception):
            HistoricalPriceRow(
                ticker="AAPL",
                observation_date="2026-05-20",
                open=148.0,
                high=140.0,
                low=147.5,
                close=151.2,
                volume=50_000_000,
                source="yahoo",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )


class TestHistoricalIVRow:
    def test_basic(self):
        row = HistoricalIVRow(
            ticker="AAPL",
            observation_date="2026-05-20",
            expiry_date="2026-07-17",
            dte_days=58,
            atm_iv=0.27,
            atm_strike=150.0,
            iv_rank=0.42,
            source="yahoo",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert 0 < row.atm_iv < 5
        assert 0.0 <= row.iv_rank <= 1.0

    def test_iv_rank_bounded(self):
        with pytest.raises(Exception):
            HistoricalIVRow(
                ticker="AAPL",
                observation_date="2026-05-20",
                expiry_date="2026-07-17",
                dte_days=58,
                atm_iv=0.27,
                atm_strike=150.0,
                iv_rank=1.5,
                source="yahoo",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )


class TestHistoricalEarningsReactionRow:
    def test_basic(self):
        row = HistoricalEarningsReactionRow(
            ticker="AAPL",
            earnings_date="2026-04-30",
            pre_earnings_iv=0.45,
            post_earnings_iv=0.28,
            iv_crush_pct=0.378,
            absolute_move_pct=0.06,
            beat_or_miss="beat",
            source="yahoo",
        )
        assert row.beat_or_miss == "beat"

    def test_beat_or_miss_literal(self):
        with pytest.raises(Exception):
            HistoricalEarningsReactionRow(
                ticker="AAPL",
                earnings_date="2026-04-30",
                pre_earnings_iv=0.45,
                post_earnings_iv=0.28,
                iv_crush_pct=0.378,
                absolute_move_pct=0.06,
                beat_or_miss="okay",
                source="yahoo",
            )


from src.common.schemas import (  # noqa: E402
    PosteriorCacheEntry,
    AgentResponseCacheEntry,
)


class TestPosteriorCacheEntry:
    def test_basic(self):
        entry = PosteriorCacheEntry(
            ticker="AAPL",
            evidence_hash="a1b2c3",
            model_name="p_target",
            model_version="1.0",
            posterior_blob={"mean": 0.72, "samples": [0.7, 0.74]},
            credible_interval_blob={"lo": 0.65, "hi": 0.79},
            computed_at="2026-05-21T08:00:00Z",
            expires_at="2026-05-28T08:00:00Z",
        )
        assert entry.model_name == "p_target"
        assert "mean" in entry.posterior_blob


class TestAgentResponseCacheEntry:
    def test_basic(self):
        entry = AgentResponseCacheEntry(
            ticker="AAPL",
            agent_name="news_synthesizer",
            agent_version="v1",
            evidence_hash="d4e5f6",
            response_blob={"sentiment": 0.6, "headlines": []},
            confidence=0.8,
            computed_at="2026-05-21T08:00:00Z",
            expires_at="2026-05-22T08:00:00Z",
        )
        assert entry.confidence == 0.8

    def test_confidence_bounded(self):
        with pytest.raises(Exception):
            AgentResponseCacheEntry(
                ticker="AAPL",
                agent_name="news_synthesizer",
                agent_version="v1",
                evidence_hash="d4e5f6",
                response_blob={},
                confidence=1.5,
                computed_at="2026-05-21T08:00:00Z",
                expires_at="2026-05-22T08:00:00Z",
            )
