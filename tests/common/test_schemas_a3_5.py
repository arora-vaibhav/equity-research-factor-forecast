"""Tests for Phase A.3.5 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import RawFredObservation, RawFinraShortInterest


class TestRawFredObservation:
    def test_minimal_valid(self):
        r = RawFredObservation(
            run_id="r1",
            series_id="DGS10",
            observation_date="2026-05-15",
            value=4.21,
            source_filename="fredgraph.csv?id=DGS10",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.series_id == "DGS10"
        assert r.value == 4.21
        assert r.realtime_start is None
        assert r.realtime_end is None

    def test_full_valid_with_realtime_window(self):
        r = RawFredObservation(
            run_id="r1",
            series_id="CPIAUCSL",
            observation_date="2026-04-01",
            value=312.45,
            realtime_start="2026-05-15",
            realtime_end="2099-12-31",
            source_filename="fredgraph.csv?id=CPIAUCSL",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.realtime_start == "2026-05-15"
        assert r.realtime_end == "2099-12-31"

    def test_missing_value_allowed_as_none(self):
        """FRED uses '.' for missing observations; the parser stores them as None."""
        r = RawFredObservation(
            run_id="r1",
            series_id="VIXCLS",
            observation_date="2026-01-01",
            value=None,
            source_filename="fredgraph.csv?id=VIXCLS",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.value is None

    def test_series_id_uppercased(self):
        r = RawFredObservation(
            run_id="r1",
            series_id="dgs10",
            observation_date="2026-05-15",
            value=4.21,
            source_filename="fredgraph.csv?id=DGS10",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.series_id == "DGS10"

    def test_series_id_required(self):
        with pytest.raises(Exception):
            RawFredObservation(
                run_id="r1",
                series_id="",
                observation_date="2026-05-15",
                value=4.21,
                source_filename="fredgraph.csv?id=",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_observation_date_required(self):
        with pytest.raises(Exception):
            RawFredObservation(
                run_id="r1",
                series_id="DGS10",
                observation_date="",
                value=4.21,
                source_filename="fredgraph.csv?id=DGS10",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )


class TestRawFinraShortInterest:
    def test_minimal_valid(self):
        r = RawFinraShortInterest(
            run_id="r1",
            ticker="AAPL",
            settlement_date="2026-05-15",
            exchange="NSDQ",
            short_interest_shares=12_345_678.0,
            avg_daily_volume=85_000_000.0,
            days_to_cover=0.145,
            source_filename="FNSQshvol20260515.txt",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"
        assert r.exchange == "NSDQ"
        assert r.days_to_cover == 0.145

    def test_ticker_uppercased(self):
        r = RawFinraShortInterest(
            run_id="r1",
            ticker="aapl",
            settlement_date="2026-05-15",
            exchange="NSDQ",
            short_interest_shares=1.0,
            source_filename="FNSQshvol20260515.txt",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"

    def test_exchange_uppercased(self):
        r = RawFinraShortInterest(
            run_id="r1",
            ticker="AAPL",
            settlement_date="2026-05-15",
            exchange="nsdq",
            short_interest_shares=1.0,
            source_filename="FNSQshvol20260515.txt",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.exchange == "NSDQ"

    def test_optional_fields_default_none(self):
        r = RawFinraShortInterest(
            run_id="r1",
            ticker="AAPL",
            settlement_date="2026-05-15",
            exchange="NSDQ",
            short_interest_shares=1.0,
            source_filename="FNSQshvol20260515.txt",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.avg_daily_volume is None
        assert r.days_to_cover is None

    def test_negative_short_interest_rejected(self):
        with pytest.raises(Exception):
            RawFinraShortInterest(
                run_id="r1",
                ticker="AAPL",
                settlement_date="2026-05-15",
                exchange="NSDQ",
                short_interest_shares=-100.0,
                source_filename="FNSQshvol20260515.txt",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            RawFinraShortInterest(
                run_id="r1",
                ticker="not-a-ticker",
                settlement_date="2026-05-15",
                exchange="NSDQ",
                short_interest_shares=1.0,
                source_filename="FNSQshvol20260515.txt",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
