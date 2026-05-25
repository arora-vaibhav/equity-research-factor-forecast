"""Tests for Phase A.3.7 Pydantic models."""
from __future__ import annotations

import pytest

from src.common.schemas import RawOpenBBRow


class TestRawOpenBBRow:
    def test_minimal_valid_fmp(self):
        r = RawOpenBBRow(
            run_id="r1",
            ticker="AAPL",
            field_name="pe_ratio",
            provider_used="fmp",
            value=28.45,
            unit="ratio",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"
        assert r.field_name == "pe_ratio"
        assert r.provider_used == "fmp"
        assert r.value == 28.45
        assert r.unit == "ratio"

    def test_minimal_valid_polygon(self):
        r = RawOpenBBRow(
            run_id="r1",
            ticker="AAPL",
            field_name="market_cap",
            provider_used="polygon",
            value=3_000_000_000_000.0,
            unit="usd",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.provider_used == "polygon"
        assert r.unit == "usd"

    def test_minimal_valid_tiingo(self):
        r = RawOpenBBRow(
            run_id="r1",
            ticker="AAPL",
            field_name="last_price",
            provider_used="tiingo",
            value=180.50,
            unit="usd",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.provider_used == "tiingo"

    def test_ticker_uppercased(self):
        r = RawOpenBBRow(
            run_id="r1",
            ticker="aapl",
            field_name="pe_ratio",
            provider_used="fmp",
            value=28.45,
            unit="ratio",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.ticker == "AAPL"

    def test_invalid_ticker_rejected(self):
        with pytest.raises(Exception):
            RawOpenBBRow(
                run_id="r1",
                ticker="not-a-ticker",
                field_name="pe_ratio",
                provider_used="fmp",
                value=28.45,
                unit="ratio",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_unknown_field_name_rejected(self):
        """field_name is a Literal of supported canonical names; unknown
        names must raise rather than silently corrupting the raw store."""
        with pytest.raises(Exception):
            RawOpenBBRow(
                run_id="r1",
                ticker="AAPL",
                field_name="not_a_field",
                provider_used="fmp",
                value=1.0,
                unit="ratio",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_unknown_provider_rejected(self):
        """provider_used is a Literal of {fmp, polygon, tiingo}; anything
        else must raise."""
        with pytest.raises(Exception):
            RawOpenBBRow(
                run_id="r1",
                ticker="AAPL",
                field_name="pe_ratio",
                provider_used="bloomberg",
                value=28.45,
                unit="ratio",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_value_can_be_none(self):
        """Sentinel: provider returned the field but with a null value.
        The row is still emitted (provenance of the attempt) but with
        value=None."""
        r = RawOpenBBRow(
            run_id="r1",
            ticker="AAPL",
            field_name="dividend_yield",
            provider_used="fmp",
            value=None,
            unit="ratio",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.value is None

    def test_unit_required(self):
        """unit is mandatory — it disambiguates 'usd' vs 'ratio' vs
        'percent' for downstream cross-vendor comparison."""
        with pytest.raises(Exception):
            RawOpenBBRow(
                run_id="r1",
                ticker="AAPL",
                field_name="pe_ratio",
                provider_used="fmp",
                value=28.45,
                unit="",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )

    def test_all_supported_field_names_accepted(self):
        """Round-trip every supported field_name to confirm the Literal."""
        supported = [
            "pe_ratio", "ps_ratio", "pb_ratio",
            "dividend_yield", "ev_to_ebitda",
            "market_cap", "last_price", "prev_close",
            "volume", "shares_outstanding",
            "primary_exchange", "sector", "industry",
            "beta", "currency",
        ]
        for fn in supported:
            r = RawOpenBBRow(
                run_id="r1",
                ticker="AAPL",
                field_name=fn,
                provider_used="fmp",
                value=1.0,
                unit="ratio",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
            assert r.field_name == fn

    def test_all_three_providers_accepted(self):
        for p in ["fmp", "polygon", "tiingo"]:
            r = RawOpenBBRow(
                run_id="r1",
                ticker="AAPL",
                field_name="pe_ratio",
                provider_used=p,
                value=1.0,
                unit="ratio",
                scrape_timestamp="2026-05-21T08:00:00Z",
            )
            assert r.provider_used == p

    def test_string_valued_fields_allowed_via_unit(self):
        """For string-valued canonical fields like sector / industry / currency,
        `value` is float-typed in the model. v1 stores those fields with
        `value=None` and the actual string passed through the `unit`
        channel with a category prefix (e.g., unit='sector:Technology').
        """
        r = RawOpenBBRow(
            run_id="r1",
            ticker="AAPL",
            field_name="sector",
            provider_used="fmp",
            value=None,
            unit="sector:Technology",
            scrape_timestamp="2026-05-21T08:00:00Z",
        )
        assert r.value is None
        assert r.unit.startswith("sector:")
