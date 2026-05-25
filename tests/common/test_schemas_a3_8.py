"""Tests for Phase A.3.8 Pydantic models + extended _OpenBBFieldName.

Covers:
  * RawGdeltMention   — GDELT news mention row
  * RawPytrendsObservation — pytrends SVI observation row
  * _OpenBBFieldName  — three new analyst-revision entries
"""
from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from src.common.schemas import (
    RawGdeltMention,
    RawPytrendsObservation,
    _OpenBBFieldName,
)


# ---------------------------------------------------------------------------
# RawGdeltMention
# ---------------------------------------------------------------------------


class TestRawGdeltMention:
    def _kwargs(self, **overrides):
        base = dict(
            run_id="run-1",
            gkg_record_id="20260522001500-1234",
            ticker="AAPL",
            mention_timestamp="2026-05-22T00:15:00Z",
            match_method="cashtag",
            scrape_timestamp="2026-05-22T00:20:00Z",
        )
        base.update(overrides)
        return base

    def test_minimal_valid(self):
        r = RawGdeltMention(**self._kwargs())
        assert r.ticker == "AAPL"
        assert r.match_method == "cashtag"
        assert r.source_url is None
        assert r.gkg_tone_json is None

    def test_ticker_uppercased(self):
        r = RawGdeltMention(**self._kwargs(ticker="aapl"))
        assert r.ticker == "AAPL"

    def test_blank_ticker_rejected(self):
        with pytest.raises(ValidationError):
            RawGdeltMention(**self._kwargs(ticker=""))

    def test_blank_gkg_record_id_rejected(self):
        with pytest.raises(ValidationError):
            RawGdeltMention(**self._kwargs(gkg_record_id="   "))

    def test_mention_timestamp_required(self):
        with pytest.raises(ValidationError):
            RawGdeltMention(**self._kwargs(mention_timestamp=""))

    def test_match_method_literal_enforced(self):
        with pytest.raises(ValidationError):
            RawGdeltMention(**self._kwargs(match_method="other"))

    def test_alias_match_method_accepted(self):
        r = RawGdeltMention(**self._kwargs(match_method="alias"))
        assert r.match_method == "alias"

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            RawGdeltMention(**self._kwargs(extra_field="boom"))


# ---------------------------------------------------------------------------
# RawPytrendsObservation
# ---------------------------------------------------------------------------


class TestRawPytrendsObservation:
    def _kwargs(self, **overrides):
        base = dict(
            run_id="run-1",
            term="recession",
            observation_date="2026-05-17",
            svi=42.0,
            source_filename="pytrends_today_12-m.json",
            scrape_timestamp="2026-05-22T00:20:00Z",
        )
        base.update(overrides)
        return base

    def test_minimal_valid(self):
        r = RawPytrendsObservation(**self._kwargs())
        assert r.term == "recession"
        assert r.geo == "US"  # default
        assert r.svi == 42.0

    def test_term_lowercased(self):
        r = RawPytrendsObservation(**self._kwargs(term="RECESSION"))
        assert r.term == "recession"

    def test_blank_term_rejected(self):
        with pytest.raises(ValidationError):
            RawPytrendsObservation(**self._kwargs(term=""))

    def test_negative_svi_rejected(self):
        with pytest.raises(ValidationError):
            RawPytrendsObservation(**self._kwargs(svi=-1.0))

    def test_svi_above_100_accepted(self):
        # pytrends can return scaled values > 100 in some configurations.
        r = RawPytrendsObservation(**self._kwargs(svi=150.0))
        assert r.svi == 150.0

    def test_custom_geo_accepted(self):
        r = RawPytrendsObservation(**self._kwargs(geo="GB"))
        assert r.geo == "GB"

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            RawPytrendsObservation(**self._kwargs(extra_field="boom"))


# ---------------------------------------------------------------------------
# _OpenBBFieldName Literal extension
# ---------------------------------------------------------------------------


class TestOpenBBFieldNameExtension:
    def test_three_new_analyst_estimate_fields_present(self):
        args = set(get_args(_OpenBBFieldName))
        assert "eps_estimate_current" in args
        assert "eps_estimate_30d_ago" in args
        assert "eps_revision_direction_count" in args

    def test_existing_fields_unchanged(self):
        args = set(get_args(_OpenBBFieldName))
        expected_existing = {
            "pe_ratio",
            "ps_ratio",
            "pb_ratio",
            "ev_to_ebitda",
            "dividend_yield",
            "market_cap",
            "last_price",
            "prev_close",
            "volume",
            "shares_outstanding",
            "primary_exchange",
            "sector",
            "industry",
            "currency",
            "beta",
        }
        assert expected_existing.issubset(args)

    def test_total_count_is_18(self):
        # 15 existing (A.3.7) + 3 new (A.3.8) = 18.
        # (Plan preflight noted 14 historically; the current count is 15
        # — likely a prior intermediate edit. The invariant is "exactly 3
        # new entries added by A.3.8", asserted positively above.)
        assert len(get_args(_OpenBBFieldName)) == 18
