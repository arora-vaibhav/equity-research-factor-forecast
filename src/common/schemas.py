"""Pydantic models and Finviz column normalization for the Trade Identifier
pipeline. These models are the schema contract between scraping/enrichment
code and the database/analytics layers.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Optional

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.common.parsing import (
    convert_percentage,
    parse_currency,
    parse_float,
    parse_int,
    parse_market_cap,
)


_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


# --- Finviz -> canonical column mapping ---------------------------------
# Keys are Finviz column names (as they appear in the scraped DataFrame);
# values are canonical field names matching UniverseRow.

FINVIZ_COLUMN_MAP: dict[str, str] = {
    "Ticker": "ticker",
    "Company": "company_name",
    "Sector": "sector",
    "Industry": "industry",
    "Market Cap": "market_cap_usd",
    "P/E": "pe_ratio",
    "Forward P/E": "forward_pe",
    "Price": "price",
    "Volume": "avg_daily_volume",
    "Avg Volume": "avg_daily_volume",
    "Operating Margin": "operating_margin",
    "Profit Margin": "net_profit_margin",
    "Performance (Year)": "perf_1y",
    "Perf Year": "perf_1y",
    "52W High": "dist_52w_high",
    "52W Low": "dist_52w_low",
    "RSI (14)": "rsi_14",
    "RSI": "rsi_14",
}

_PARSERS = {
    "market_cap_usd": parse_market_cap,
    "pe_ratio": parse_float,
    "forward_pe": parse_float,
    "price": parse_currency,
    "avg_daily_volume": parse_int,
    "operating_margin": convert_percentage,
    "net_profit_margin": convert_percentage,
    "perf_1y": convert_percentage,
    "dist_52w_high": convert_percentage,
    "dist_52w_low": convert_percentage,
    "rsi_14": parse_float,
}


def normalize_finviz_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename Finviz columns to canonical names and parse all numeric strings.

    Unknown columns are preserved as-is. Duplicate canonical names (e.g. both
    'Volume' and 'Avg Volume') keep the first non-null mapping.
    """
    if df.empty:
        return df.copy()

    out = df.copy()
    rename_map = {col: FINVIZ_COLUMN_MAP[col] for col in out.columns if col in FINVIZ_COLUMN_MAP}
    out = out.rename(columns=rename_map)
    out = out.loc[:, ~out.columns.duplicated(keep="first")]

    for canonical, parser in _PARSERS.items():
        if canonical in out.columns:
            out[canonical] = out[canonical].map(parser)

    if "ticker" in out.columns:
        out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()

    return out


# --- Models -------------------------------------------------------------


class UniverseRow(BaseModel):
    """One row of the Layer 1 universe after column normalization and before
    factor scoring. Most fields are Optional because coverage is imperfect.
    """

    run_id: str
    ticker: str
    company_name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    market_cap_usd: Optional[float] = Field(default=None, gt=0)
    price: Optional[float] = Field(default=None, gt=0)
    avg_daily_volume: Optional[int] = Field(default=None, ge=0)
    pe_ratio: Optional[float] = None
    forward_pe: Optional[float] = None
    operating_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    net_profit_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    perf_1y: Optional[float] = Field(default=None, ge=-1.0, le=20.0)
    dist_52w_high: Optional[float] = Field(default=None, le=0.0)
    dist_52w_low: Optional[float] = Field(default=None, ge=0.0)
    rsi_14: Optional[float] = Field(default=None, ge=0.0, le=100.0)

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s


class FactorScores(BaseModel):
    """Per-symbol factor outputs. NaN is a valid value (stored as None)."""

    run_id: str
    ticker: str
    value_score: Optional[float] = None
    quality_score: Optional[float] = None
    momentum_score: Optional[float] = None
    lowvol_score: Optional[float] = None
    revisions_score: Optional[float] = None
    valuation_history_pct: Optional[float] = None
    earnings_momentum_revisions_score: Optional[float] = None
    sentiment_score: Optional[float] = None
    data_approximated_flags: list[str] = Field(default_factory=list)


class CandidateRow(BaseModel):
    """Final ranked output row for one ticker x playbook combination."""

    run_id: str
    ticker: str
    playbook: Literal["A", "B", "C"]
    value_score: Optional[float] = None
    quality_score: Optional[float] = None
    momentum_score: Optional[float] = None
    lowvol_score: Optional[float] = None
    revisions_score: Optional[float] = None
    composite_score: Optional[float] = None
    eligible: bool = False
    reasoning: str = ""


class RunManifest(BaseModel):
    """Metadata describing a single pipeline run."""

    run_id: str
    run_timestamp: datetime
    run_type: str
    source_label: str
    status: Literal["ok", "partial", "failed"] = "ok"
    universe_row_count: int = 0
    rejected_row_count: int = 0
    playbook_a_count: int = 0
    playbook_b_count: int = 0
    critical_field_coverage: dict[str, float] = Field(default_factory=dict)
    low_coverage_fields: list[str] = Field(default_factory=list)
    config_snapshot: dict = Field(default_factory=dict)
    notes: Optional[str] = None


class FilterConfig(BaseModel):
    """Validated wrapper around filters.yaml. Defaults are conservative."""

    universe_min_market_cap_usd: float = 300_000_000.0
    universe_min_avg_daily_volume: int = 500_000
    playbook_a_max_valuation_percentile: float = Field(default=0.40, ge=0.0, le=1.0)
    playbook_a_min_earnings_surprise_pct: float = 0.0
    playbook_b_min_valuation_percentile: float = Field(default=0.70, ge=0.0, le=1.0)


# === Phase A.2 - v2 storage models =====================================


class CanonicalUniverseRow(BaseModel):
    """Per-(run_id, ticker) wide row in canonical_universe.

    Spec: docs/claude-code/specs/2026-05-21-platform-architecture-v2.md section 6.1.
    All non-id fields are Optional because data coverage is imperfect.
    """

    run_id: str
    ticker: str
    company_name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    exchange: Optional[str] = None
    cik: Optional[str] = None

    market_cap_usd: Optional[float] = Field(default=None, gt=0)
    price: Optional[float] = Field(default=None, gt=0)
    avg_daily_volume: Optional[int] = Field(default=None, ge=0)

    pe_ttm: Optional[float] = None
    pe_forward: Optional[float] = None
    ebit_ttm: Optional[float] = None
    fcf_ttm: Optional[float] = None
    operating_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    net_profit_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    roe: Optional[float] = None
    roic: Optional[float] = None
    total_debt_to_equity: Optional[float] = None
    interest_coverage: Optional[float] = None
    revenue_growth_yoy: Optional[float] = None
    eps_growth_yoy: Optional[float] = None

    perf_1m: Optional[float] = None
    perf_3m: Optional[float] = None
    perf_6m: Optional[float] = None
    perf_12m: Optional[float] = None
    rsi_14: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    dist_52w_high: Optional[float] = Field(default=None, le=0.0)
    dist_52w_low: Optional[float] = Field(default=None, ge=0.0)
    dist_200dma: Optional[float] = None

    short_interest_pct_float: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    news_activity_score: Optional[float] = None
    pe_5y_percentile: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    ev_ebitda_5y_percentile: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    data_quality_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    materialized_at: Optional[str] = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s


class ThesisObjectRow(BaseModel):
    """Per-(run_id, ticker) thesis record.

    Layer 2 (Phase B.4) populates this. A.2 only creates the table and the schema.
    """

    run_id: str
    ticker: str
    direction: Literal["bullish", "bearish", "range-bound", "earnings-play"]
    target_price: float
    horizon_days: int = Field(gt=0)
    confidence_prior: float = Field(ge=0.0, le=1.0)
    invalidation_conditions: list[str] = Field(default_factory=list)
    fundamental_bias_score: Optional[float] = None
    news_bias_score: Optional[float] = None
    macro_context: dict = Field(default_factory=dict)
    technical_setup: dict = Field(default_factory=dict)
    earnings_in_window: dict = Field(default_factory=dict)
    ai_research_synthesis: dict = Field(default_factory=dict)
    built_at: Optional[str] = None


class FieldProvenanceRow(BaseModel):
    """Per-(run_id, ticker, field, source) raw observation log. Append-only."""

    run_id: str
    ticker: str
    field: str
    source: str
    raw_value: Optional[str] = None
    parsed_value: Optional[float] = None
    weight: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    contributed_to_canonical: bool = False
    disagreement_pct: Optional[float] = None
    fetched_at: str


class SourceRunLogRow(BaseModel):
    """Per-(run_id, source) health record. Append-only."""

    run_id: str
    source: str
    started_at: str
    finished_at: Optional[str] = None
    status: Literal["ok", "partial", "failed"]
    rows_fetched: int = Field(ge=0)
    error_message: Optional[str] = None
    error_traceback: Optional[str] = None


# === Phase A.2 - v2 time-series accumulator models =====================


class HistoricalPriceRow(BaseModel):
    """Daily OHLCV per (ticker, observation_date). Append-only."""

    ticker: str
    observation_date: str
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)
    adj_close: Optional[float] = Field(default=None, gt=0)
    source: str
    scrape_timestamp: str

    @field_validator("low")
    @classmethod
    def _high_at_least_low(cls, v: float, info) -> float:
        high = info.data.get("high")
        if high is not None and high < v:
            raise ValueError(f"high {high} must be >= low {v}")
        return v


class HistoricalIVRow(BaseModel):
    """Daily ATM IV per (ticker, observation_date, expiry_date). Append-only."""

    ticker: str
    observation_date: str
    expiry_date: str
    dte_days: int = Field(ge=0)
    atm_iv: float = Field(gt=0)
    atm_strike: float = Field(gt=0)
    iv_rank: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    source: str
    scrape_timestamp: str


class HistoricalEarningsReactionRow(BaseModel):
    """Per (ticker, earnings_date) post-earnings move + IV crush record."""

    ticker: str
    earnings_date: str
    pre_earnings_iv: Optional[float] = Field(default=None, gt=0)
    post_earnings_iv: Optional[float] = Field(default=None, gt=0)
    iv_crush_pct: Optional[float] = None
    absolute_move_pct: Optional[float] = None
    beat_or_miss: Literal["beat", "miss", "inline", "unknown"]
    source: str


# === Phase A.2 - v2 cache models =======================================


class PosteriorCacheEntry(BaseModel):
    """Cached Bayesian posterior keyed by (ticker, evidence_hash, model_name)."""

    ticker: str
    evidence_hash: str
    model_name: str
    model_version: str
    posterior_blob: dict
    credible_interval_blob: dict
    computed_at: str
    expires_at: str


class AgentResponseCacheEntry(BaseModel):
    """Cached AI agent output keyed by (ticker, agent_name, evidence_hash)."""

    ticker: str
    agent_name: str
    agent_version: str
    evidence_hash: str
    response_blob: dict
    confidence: float = Field(ge=0.0, le=1.0)
    computed_at: str
    expires_at: str


# === Phase A.3 - persistent accumulation model =========================


class FetchWatermark(BaseModel):
    """Per (source, ticker, field) tracking of what we've already fetched.

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md section 5.1.
    PRIMARY KEY of the underlying table is (source, ticker, field).
    `ticker='*'` is the convention for non-ticker-scoped data such as
    FRED macro series.
    """

    source: str
    ticker: str
    field: str
    last_fetched_at: str
    last_observation_date: Optional[str] = None
    fetch_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    last_error_message: Optional[str] = None


# === Phase A.3.2 - Yahoo source-native row =============================


class RawYahooRow(BaseModel):
    """One Yahoo Finance fundamentals snapshot per (run_id, ticker).

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.2. Stored in raw_yahoo table. Source-native (no canonicalization).
    """

    run_id: str
    ticker: str

    company_name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    exchange: Optional[str] = None

    market_cap: Optional[float] = Field(default=None, gt=0)
    price: Optional[float] = Field(default=None, gt=0)
    avg_daily_volume: Optional[int] = Field(default=None, ge=0)

    pe_ttm: Optional[float] = None
    pe_forward: Optional[float] = None
    ebit_ttm: Optional[float] = None
    fcf_ttm: Optional[float] = None
    total_debt: Optional[float] = None
    cash: Optional[float] = None
    book_value: Optional[float] = None
    operating_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    net_profit_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    revenue_growth_yoy: Optional[float] = None
    eps_growth_yoy: Optional[float] = None

    scrape_timestamp: str

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s


# === Phase A.3.3 - EDGAR XBRL fundamentals row =========================


class RawEdgarFundamentalsRow(BaseModel):
    """One per-period structured fundamentals row parsed from the SEC
    companyfacts XBRL JSON.

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.3.1. Stored in raw_edgar_fundamentals.

    Primary key conceptually: (run_id, ticker, fiscal_period, fiscal_year).
    A `fiscal_period` of "annual" represents a 10-K full-year value or
    a TTM proxy (sum of last 4 quarterly observations). Quarterly values
    (Q1/Q2/Q3/Q4) are the as-reported 10-Q numbers for that quarter.
    """

    run_id: str
    ticker: str
    cik: str
    fiscal_year: int = Field(ge=1900, le=2100)
    fiscal_period: Literal["annual", "Q1", "Q2", "Q3", "Q4"]
    filing_date: str
    accepted_at: Optional[str] = None
    form_type: Literal["10-K", "10-Q"]

    revenue_ttm: Optional[float] = None
    ebit_ttm: Optional[float] = None
    net_income_ttm: Optional[float] = None
    total_assets: Optional[float] = None
    total_liabilities: Optional[float] = None
    total_equity: Optional[float] = None
    cash_and_equivalents: Optional[float] = None
    total_debt: Optional[float] = None
    operating_cashflow: Optional[float] = None
    capex: Optional[float] = None
    fcf_ttm: Optional[float] = None
    operating_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    net_profit_margin: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    total_debt_to_equity: Optional[float] = None
    interest_coverage: Optional[float] = None

    scrape_timestamp: str

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s

    @field_validator("cik", mode="before")
    @classmethod
    def _normalize_cik(cls, v: object) -> str:
        if v is None:
            raise ValueError("cik is required")
        s = str(v).strip()
        # Strip CIK prefix if present, then zero-pad to 10 digits
        if s.upper().startswith("CIK"):
            s = s[3:]
        s = s.lstrip("0") or "0"
        if not s.isdigit():
            raise ValueError(f"invalid cik (non-digit): {v!r}")
        return s.zfill(10)


# === Phase A.3.4 - EDGAR Form 4 insider transactions + 8-K filing index ====


# SEC Form 4 transaction codes per the General Instructions, Table I/II.
# Source: https://www.sec.gov/about/forms/form4.pdf
#   P  Open-market or private purchase of non-derivative or derivative security
#   S  Open-market or private sale of non-derivative or derivative security
#   A  Grant, award, or other acquisition pursuant to Rule 16b-3
#   M  Exercise or conversion of derivative security exempted under Rule 16b-3
#   D  Disposition to the issuer of issuer equity securities pursuant to Rule 16b-3
#   F  Payment of exercise price or tax liability by delivering or withholding
#   G  Bona fide gift
#   J  Other acquisition or disposition (describe transaction)
#   C  Conversion of derivative security
#   E  Expiration of short derivative position
#   H  Expiration (or cancellation) of long derivative position with value received
#   I  Discretionary transaction in accordance with Rule 16b-3(f)
#   O  Exercise of out-of-the-money derivative security
#   X  Exercise of in-the-money or at-the-money derivative security
#   V  Transaction voluntarily reported earlier than required
#   W  Acquisition or disposition by will or laws of descent and distribution
#   Z  Deposit into or withdrawal from voting trust
#   K  Transaction in equity swap or instrument with similar characteristics
#   L  Small acquisition under Rule 16a-6
#   U  Disposition pursuant to a tender of shares in a change of control transaction
_FORM4_TRANSACTION_CODES = Literal[
    "P", "S", "A", "M", "D", "F", "G", "J",
    "C", "E", "H", "I", "O", "X", "V", "W",
    "Z", "K", "L", "U",
]


# === Phase A.3.5 - FRED macro series + FINRA biweekly short interest =====


class RawFredObservation(BaseModel):
    """One row per (series_id, observation_date) FRED macro observation.

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.4. Stored in raw_fred. PK: (series_id, observation_date).

    `realtime_start` / `realtime_end` model the FRED ALFRED vintage-data
    convention (when this observation was published vs current revision).
    For v1 with the public `fredgraph.csv` endpoint they are None
    (look-ahead bias is acknowledged; see methodology handbook §revision-
    handling). When ALFRED integration is added post-A.3.10, the same
    schema accommodates per-vintage rows without migration.
    """

    run_id: str
    series_id: str
    observation_date: str
    value: Optional[float] = None  # FRED uses '.' for missing -> stored as None
    realtime_start: Optional[str] = None
    realtime_end: Optional[str] = None
    source_filename: str
    scrape_timestamp: str

    @field_validator("series_id", mode="before")
    @classmethod
    def _normalize_series_id(cls, v: object) -> str:
        if v is None:
            raise ValueError("series_id is required")
        s = str(v).strip().upper()
        if not s:
            raise ValueError("series_id cannot be empty")
        return s

    @field_validator("observation_date", mode="before")
    @classmethod
    def _normalize_observation_date(cls, v: object) -> str:
        if v is None:
            raise ValueError("observation_date is required")
        s = str(v).strip()
        if not s:
            raise ValueError("observation_date cannot be empty")
        return s


class RawFinraShortInterest(BaseModel):
    """One row per (ticker, settlement_date, exchange) FINRA short-volume record.

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.5. Stored in raw_finra. PK: (ticker, settlement_date, exchange).

    The `exchange` discriminator (NSDQ / NYSE / NYAX / ORF) lets us
    accommodate the four per-market-center daily files we pull from
    cdn.finra.org and, in a future enhancement, also the official
    biweekly tape via the FINRA Query API without schema change.
    """

    run_id: str
    ticker: str
    settlement_date: str
    exchange: str
    short_interest_shares: float = Field(ge=0)
    avg_daily_volume: Optional[float] = Field(default=None, ge=0)
    days_to_cover: Optional[float] = Field(default=None, ge=0)
    source_filename: str
    scrape_timestamp: str

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s

    @field_validator("exchange", mode="before")
    @classmethod
    def _normalize_exchange(cls, v: object) -> str:
        if v is None:
            raise ValueError("exchange is required")
        s = str(v).strip().upper()
        if not s:
            raise ValueError("exchange cannot be empty")
        return s

_EDGAR_FORM_TYPES = Literal[
    "8-K", "8-K/A",
    "10-K", "10-K/A",
    "10-Q", "10-Q/A",
    "4", "4/A",
]


class RawEdgarInsiderRow(BaseModel):
    """One row per Form 4 non-derivative line item.

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.3.2. Stored in raw_edgar_insider.

    Primary key conceptually:
      (cik, source_filing_accn, filer_name, transaction_date, transaction_code).

    A single Form 4 filing can disclose multiple <nonDerivativeTransaction>
    blocks for the same filer (e.g., a planned 10b5-1 sale executed across
    multiple price points on the same day, or distinct codes M+S on the
    same day for an exercise+sell). The PK above is the operational
    granularity at which we deduplicate.

    `is_opportunistic` is None at parse time. It is populated by
    `methodology.opportunistic_insider.classify_transactions` AFTER all
    Form 4 rows for a ticker batch have been parsed (the classifier needs
    cross-filing history per filer to determine routine vs opportunistic).

    `opportunistic_classifier_version` is the version string of the
    classifier that produced `is_opportunistic` (for future-recalibration
    audit). It is None when `is_opportunistic` is None.
    """

    run_id: str
    ticker: str
    cik: str
    filer_name: str
    filer_title: Optional[str] = None
    filer_is_officer: Optional[bool] = None
    filer_is_director: Optional[bool] = None
    filer_is_10pct_owner: Optional[bool] = None
    transaction_date: str
    transaction_code: _FORM4_TRANSACTION_CODES
    transaction_code_description: Optional[str] = None
    shares: float = Field(ge=0)
    price_per_share: Optional[float] = Field(default=None, ge=0)
    total_value: Optional[float] = None
    shares_after_transaction: Optional[float] = Field(default=None, ge=0)

    is_opportunistic: Optional[bool] = None
    opportunistic_classifier_version: Optional[str] = None
    is_derivative: bool = False

    source_filing_accn: str
    scrape_timestamp: str

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s

    @field_validator("cik", mode="before")
    @classmethod
    def _normalize_cik(cls, v: object) -> str:
        if v is None:
            raise ValueError("cik is required")
        s = str(v).strip()
        if s.upper().startswith("CIK"):
            s = s[3:]
        s = s.lstrip("0") or "0"
        if not s.isdigit():
            raise ValueError(f"invalid cik (non-digit): {v!r}")
        return s.zfill(10)

    @field_validator("filer_name", mode="before")
    @classmethod
    def _normalize_filer_name(cls, v: object) -> str:
        if v is None:
            raise ValueError("filer_name is required")
        return str(v).strip().upper()


class RawEdgarFilingRow(BaseModel):
    """One row per SEC filing (envelope record).

    Covers 8-K, 10-K, 10-Q, Form 4, and amendments. For Form 4, the
    *line items* live in raw_edgar_insider; the envelope row in
    raw_edgar_filings is the audit trail.

    Primary key conceptually: (cik, source_filing_accn).
    """

    run_id: str
    ticker: str
    cik: str
    form_type: _EDGAR_FORM_TYPES
    filing_date: str
    accepted_at: Optional[str] = None
    item_codes: Optional[str] = None  # CSV of 8-K item numbers, e.g. "2.02,9.01"
    source_filing_accn: str
    primary_doc_url: Optional[str] = None
    scrape_timestamp: str

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s

    @field_validator("cik", mode="before")
    @classmethod
    def _normalize_cik(cls, v: object) -> str:
        if v is None:
            raise ValueError("cik is required")
        s = str(v).strip()
        if s.upper().startswith("CIK"):
            s = s[3:]
        s = s.lstrip("0") or "0"
        if not s.isdigit():
            raise ValueError(f"invalid cik (non-digit): {v!r}")
        return s.zfill(10)


# === Phase A.3.6 - stockanalysis.com 10y ratio history ===================


# Supported ratio metrics. Constrained as a Literal so unknown names from
# a future stockanalysis.com layout change raise loudly rather than silently
# corrupting the raw store. To accommodate a new metric, add it here and
# also extend the parser's whitelist in stockanalysis_parser.py.
_StockanalysisMetric = Literal[
    "pe_ratio",
    "pb_ratio",
    "ps_ratio",
    "ev_ebitda",
    "dividend_yield",
    "roe",
    "roa",
    "profit_margin",
    "operating_margin",
    "fcf_yield",
    "current_ratio",
    "debt_to_equity",
]

_StockanalysisPeriodType = Literal["annual", "quarterly", "ttm"]


class RawStockanalysisRatioRow(BaseModel):
    """One (ticker, metric, period_end_date) ratio observation scraped
    from stockanalysis.com.

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.6. Stored long-format in raw_stockanalysis_ratios; PK
    (ticker, metric, period_end_date). The materialization layer (A.3.10)
    pivots this table to wide form when computing the 5-year percentile
    features `pe_5y_percentile` and `ev_ebitda_5y_percentile`.

    `period_type` discriminates annual (FY) vs quarterly (Q1..Q4) vs ttm
    (trailing twelve months). The PK does NOT include period_type because
    a single (ticker, metric, period_end_date) tuple has at most one
    semantically distinct value — annual 2024-12-31 and quarterly Q4-2024
    end on the same date and would yield identical ratios.

    `value` is Optional because stockanalysis.com publishes 'n/a', '-',
    and blank cells where the underlying data is unavailable (typically
    for the oldest year in the 10y history). The parser maps all such
    sentinels to None.
    """

    run_id: str
    ticker: str
    metric: _StockanalysisMetric
    period_end_date: str
    period_type: _StockanalysisPeriodType
    value: Optional[float] = None
    source_url: str
    scrape_timestamp: str

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s

    @field_validator("period_end_date", mode="before")
    @classmethod
    def _normalize_period_end_date(cls, v: object) -> str:
        if v is None:
            raise ValueError("period_end_date is required")
        s = str(v).strip()
        if not s:
            raise ValueError("period_end_date cannot be empty")
        return s


# === Phase A.3.7 - OpenBB multi-provider router ==========================


# Supported canonical field names. Constrained as a Literal so an unknown
# field name from a future provider-response schema change raises loudly
# rather than silently corrupting raw_openbb. To accommodate a new field,
# add it here AND extend the relevant provider mapper(s) in
# src/common/datasources/openbb_source.py.
_OpenBBFieldName = Literal[
    # Valuation ratios
    "pe_ratio",
    "ps_ratio",
    "pb_ratio",
    "ev_to_ebitda",
    "dividend_yield",
    # Price-anchor / market
    "market_cap",
    "last_price",
    "prev_close",
    "volume",
    "shares_outstanding",
    # Identifier metadata (string-valued; stored with value=None +
    # category-tagged unit)
    "primary_exchange",
    "sector",
    "industry",
    "currency",
    # Risk metric
    "beta",
    # Analyst revisions (A.3.8 — Chan-Jegadeesh-Lakonishok input)
    "eps_estimate_current",
    "eps_estimate_30d_ago",
    "eps_revision_direction_count",
]

_OpenBBProvider = Literal["fmp", "polygon", "tiingo"]


class RawOpenBBRow(BaseModel):
    """One (ticker, field_name, provider_used) observation pulled from the
    OpenBB multi-provider router (A.3.7).

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 6.7. Stored long-format in raw_openbb; PK
    (run_id, ticker, field_name, provider_used).

    Provenance is the load-bearing concept: the same canonical field
    pulled from two providers writes TWO rows (different `provider_used`),
    NOT one row that the second provider silently overwrites. The
    materialization layer (A.3.10) consumes the long-format rows to do
    cross-vendor reconciliation and flag divergence per
    `data_approximated_flags`.

    `value` is Optional because:
      * a provider may report the field with an explicit null,
      * the field may be string-valued (sector / industry / currency /
        primary_exchange) — for v1 the string is stored in `unit` with a
        category prefix and `value` is None.
    """

    run_id: str
    ticker: str
    field_name: _OpenBBFieldName
    provider_used: _OpenBBProvider
    value: Optional[float] = None
    unit: str
    scrape_timestamp: str

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s

    @field_validator("unit", mode="before")
    @classmethod
    def _validate_unit(cls, v: object) -> str:
        if v is None:
            raise ValueError("unit is required")
        s = str(v).strip()
        if not s:
            raise ValueError("unit cannot be empty")
        return s


# === Phase A.3.7.5 - Rate-Limited Fetch Orchestrator ====================
#
# Three additive models backing the orchestrator's three new tables:
#   - ProviderCallLog     -> provider_call_log     (append-only audit)
#   - ProviderQuotaState  -> provider_quota_state  (sliding-window quota)
#   - FetchQueueEntry     -> fetch_queue           (pending-work queue)
#
# Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
# section 15 row A.3.7.5.

_CallStatus = Literal["ok", "failed", "deferred"]
_QueueStatus = Literal["pending", "dispatched", "done", "error"]


class ProviderCallLog(BaseModel):
    """One row per provider HTTP call. Append-only audit trail.

    Distinct from `source_run_log` (per-run aggregate); this is per-call
    granularity for replay + debugging. `ticker` is Optional because FRED
    macro pulls have no ticker. `field` carries the canonical field name
    (or a category-tagged label like 'macro:UNRATE' for macro series).

    `call_id` is autoincremented by SQLite's INTEGER PRIMARY KEY mechanism;
    the model accepts it as None on construction (assigned on INSERT).
    """

    model_config = ConfigDict(extra="forbid")

    call_id: Optional[int] = Field(default=None, description="autoincrement PK from SQLite; None on insert")
    source: str = Field(..., description="source-registry name, e.g. 'yahoo', 'fred', 'openbb'")
    ticker: Optional[str] = Field(default=None, description="uppercase ticker; None for non-ticker macro calls")
    field: str = Field(..., description="canonical field name or category-tagged label")
    started_at: str = Field(..., description="ISO 8601 UTC timestamp of HTTP call start")
    finished_at: str = Field(..., description="ISO 8601 UTC timestamp of HTTP call completion")
    status: _CallStatus = Field(..., description="ok | failed | deferred")
    bytes_returned: int = Field(..., ge=0, description="response body size in bytes; 0 for failed calls")
    error_message: Optional[str] = Field(default=None, description="exception/error string when status != 'ok'")
    http_status: Optional[int] = Field(default=None, description="HTTP status code; None for non-HTTP failures")

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s


class ProviderQuotaState(BaseModel):
    """One row per (source, provider, window_start) sliding-window quota bucket.

    The orchestrator queries this table with a sliding-window aggregate
    (sum calls_used WHERE window_start > now - window_seconds) to decide
    how many calls remain. `source` is the registry source name; `provider`
    is the per-quota bucket name. For single-provider sources they are
    equal; for `openbb` they differ (e.g., source='openbb', provider='fmp_stable').

    `window_start` is the floor-to-second of the call time, ISO 8601 UTC.
    `calls_used` is the count of calls bucketed at exactly that
    `window_start`. Rows older than 7 days are pruned by the orchestrator.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., description="source-registry name, e.g. 'openbb'")
    provider: str = Field(..., description="per-quota bucket name, e.g. 'fmp_stable'")
    window_start: str = Field(..., description="ISO 8601 UTC floor-to-second of call time")
    calls_used: int = Field(..., ge=0, description="count of calls in this exact bucket")


class FetchQueueEntry(BaseModel):
    """One row per pending / in-flight / terminal fetch job.

    Status transitions: pending -> dispatched -> done | error.

    `queue_id` is autoincremented by SQLite; None on construction.

    `priority_score` is a float so the orchestrator's queue-priority scorer
    can encode `staleness_hours * weight + caller_priority` directly. Higher
    scores dispatch first. `last_error` carries the most recent failure
    string for entries that reached status='error'.
    """

    model_config = ConfigDict(extra="forbid")

    queue_id: Optional[int] = Field(default=None, description="autoincrement PK from SQLite; None on insert")
    source: str = Field(..., description="source-registry name, e.g. 'yahoo'")
    ticker: Optional[str] = Field(default=None, description="uppercase ticker; None for non-ticker macro fetches")
    field: str = Field(..., description="canonical field name or category-tagged label")
    priority_score: float = Field(..., description="dispatch ordering key; higher dispatches first")
    created_at: str = Field(..., description="ISO 8601 UTC enqueue timestamp")
    dispatched_at: Optional[str] = Field(default=None, description="ISO 8601 UTC dispatch timestamp; None while pending")
    completed_at: Optional[str] = Field(default=None, description="ISO 8601 UTC terminal-state timestamp")
    status: _QueueStatus = Field(..., description="pending | dispatched | done | error")
    last_error: Optional[str] = Field(default=None, description="error message for status='error' rows")

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s


# === Phase A.3.8 — GDELT news mentions + pytrends FEARS substrate ========


class RawGdeltMention(BaseModel):
    """One GDELT GKG 15-minute record mapped to a ticker.

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 7.1 row 4. Stored in raw_gdelt. PK: (gkg_record_id, ticker).

    `gkg_record_id` is GDELT's stable record identifier (the first
    column of the GKG CSV, format e.g. '20260522001500-1234'). One
    GKG record may mention multiple tickers (each writes one row);
    one ticker may appear in many records over a 15-minute window.

    `mention_timestamp` is the GKG record's publication timestamp
    (ISO 8601 UTC). `match_method` records whether the ticker was
    discovered via cashtag ('cashtag') or organization-name alias
    ('alias'), so later analysis can weight the two channels.

    `gkg_tone_json` is reserved for the V2.5 GKG `V2Tone` columns
    (Tone / TonePos / TonePos1 / TonePos2 / ...). v1 leaves None;
    a post-A.3.10 enhancement parses tone without schema change.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    gkg_record_id: str
    ticker: str
    mention_timestamp: str  # ISO 8601 UTC
    match_method: Literal["cashtag", "alias"]
    source_url: Optional[str] = None
    gkg_tone_json: Optional[str] = None
    scrape_timestamp: str

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s

    @field_validator("gkg_record_id", mode="before")
    @classmethod
    def _require_gkg_id(cls, v: object) -> str:
        if v is None:
            raise ValueError("gkg_record_id is required")
        s = str(v).strip()
        if not s:
            raise ValueError("gkg_record_id cannot be empty")
        return s

    @field_validator("mention_timestamp", mode="before")
    @classmethod
    def _require_mention_timestamp(cls, v: object) -> str:
        if v is None:
            raise ValueError("mention_timestamp is required")
        s = str(v).strip()
        if not s:
            raise ValueError("mention_timestamp cannot be empty")
        return s


class RawPytrendsObservation(BaseModel):
    """One Google-Trends search-volume index observation for a FEARS term.

    Spec: section 7.1 row 7. Stored in raw_pytrends. PK:
    (term, observation_date, geo).

    `term` is lowercased before storage (FEARS terms in
    Da-Engelberg-Gao 2015 §III are case-insensitive). `svi` is the
    Google-normalized search-volume index 0..100 (Google scales each
    pull to a 0..100 range within the requested window; absolute
    counts are not available from the public Trends API). `svi > 100`
    is accepted because pytrends can return scaled-up values when
    `gprop` (Google property) is set or when a custom comparator
    pulls dial up the relative scaling.

    `geo` is the two-letter ISO country code. v1 default 'US' for
    the trading universe; future per-region pulls add rows with
    different `geo` values and the same schema works unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    term: str
    observation_date: str  # YYYY-MM-DD (weekly bucket per pytrends)
    svi: float = Field(..., ge=0)
    geo: str = "US"
    source_filename: str
    scrape_timestamp: str

    @field_validator("term", mode="before")
    @classmethod
    def _normalize_term(cls, v: object) -> str:
        if v is None:
            raise ValueError("term is required")
        s = str(v).strip().lower()
        if not s:
            raise ValueError("term cannot be empty")
        return s

    @field_validator("observation_date", mode="before")
    @classmethod
    def _require_observation_date(cls, v: object) -> str:
        if v is None:
            raise ValueError("observation_date is required")
        s = str(v).strip()
        if not s:
            raise ValueError("observation_date cannot be empty")
        return s


class RawEdgarFilingToneRow(BaseModel):
    """One Loughran-McDonald tone score per 10-K / 10-Q filing.

    Spec: docs/claude-code/specs/2026-05-21-phase-a3-layer1-hardening-design.md
    section 7.1 row 6, section 7.2 signal 6. Stored in
    raw_edgar_filing_tone. PK: (cik, source_filing_accn,
    lm_dictionary_version) -- so re-scoring under a future LM
    dictionary release adds rows alongside, never overwrites.

    `net_tone = (n_positive - n_negative) / total_words` per spec
    section 7.2. Other tag counts (uncertainty, litigious) are stored
    for forward use by Layer 2 thesis aggregation without requiring
    re-scoring.

    `extraction_status` is one of:
      * 'item_1a_extracted' -- Item 1A markers located and extracted
        successfully. Counts + net_tone populated.
      * 'full_document_fallback' -- Item 1A markers not found; scored
        full document body. Counts + net_tone populated. Permissive
        fallback added in A.3.9.
      * 'empty_document' -- response body empty after HTML normalisation.
        Counts == 0, net_tone == 0.
      * 'dictionary_missing' -- LM dictionary CSV not found at score
        time. R2 reconciliation (2026-05-23) persists these as audit
        rows with NULL counts + net_tone so the caller can retry once
        the dictionary is restored.
      * 'fetch_failed' -- primary_doc_url GET failed / 4xx / empty
        body before scoring could happen. NULL counts + net_tone;
        the next scheduler tick will retry.

    R2 reconciliation (2026-05-23) renamed 'item_1a_found' ->
    'item_1a_extracted' and added the two error statuses to match
    the strict spec; migrate_to_v13 remaps existing rows and updates
    the CHECK constraint.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    ticker: str
    cik: str
    source_filing_accn: str
    form_type: str
    filing_date: str  # YYYY-MM-DD
    n_positive: Optional[int] = Field(default=None, ge=0)
    n_negative: Optional[int] = Field(default=None, ge=0)
    n_uncertainty: Optional[int] = Field(default=None, ge=0)
    n_litigious: Optional[int] = Field(default=None, ge=0)
    total_words: Optional[int] = Field(default=None, ge=0)
    net_tone: Optional[float] = None
    extraction_status: Literal[
        "item_1a_extracted",
        "full_document_fallback",
        "empty_document",
        "dictionary_missing",
        "fetch_failed",
    ]
    lm_dictionary_version: str
    scrape_timestamp: str

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s

    @field_validator("cik", mode="before")
    @classmethod
    def _normalize_cik(cls, v: object) -> str:
        if v is None:
            raise ValueError("cik is required")
        s = str(v).strip().lstrip("0") or "0"
        return s.zfill(10)

    @field_validator("source_filing_accn", mode="before")
    @classmethod
    def _normalize_accn(cls, v: object) -> str:
        # Dashes preserved -- matches raw_edgar_filings' storage so the
        # PK / FK comparisons in fetch_filing_text_for_ticker's SELECT
        # short-circuit correctly on re-runs.
        if v is None:
            raise ValueError("source_filing_accn is required")
        s = str(v).strip()
        if not s:
            raise ValueError("source_filing_accn cannot be empty")
        return s

    @field_validator("form_type", mode="before")
    @classmethod
    def _normalize_form_type(cls, v: object) -> str:
        if v is None:
            raise ValueError("form_type is required")
        s = str(v).strip().upper()
        if not s:
            raise ValueError("form_type cannot be empty")
        return s

    @field_validator("filing_date", mode="before")
    @classmethod
    def _require_filing_date(cls, v: object) -> str:
        if v is None:
            raise ValueError("filing_date is required")
        s = str(v).strip()
        if not s:
            raise ValueError("filing_date cannot be empty")
        return s


# === Phase B.1 - catalyst calendar ===================================


_CATALYST_TYPE = Literal[
    "earnings",
    "ex_dividend",
    "fda_pdufa",
    "fomc",
    "lockup_expiry",
    "index_inclusion",
]


class CatalystRow(BaseModel):
    """One row per known catalyst event for a ticker.

    Stored in raw_catalysts (audit
    trail per fetch) and catalyst_calendar (run-scoped curated
    view). PK conceptually: (ticker, catalyst_type, catalyst_date).

    Conventions:
      * catalyst_date is YYYY-MM-DD; for catalyst types where only
        a quarter or week is known (FOMC ~week), pick the central
        day of the window.
      * confidence reflects how firm the date is. 'high' for SEC
        filings or yfinance earnings_dates with a confirmed date;
        'med' for analyst estimates; 'low' for inferred/heuristic.
      * source_url documents where the date came from for audit.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    ticker: str
    catalyst_type: _CATALYST_TYPE
    catalyst_date: str  # YYYY-MM-DD
    catalyst_description: Optional[str] = None
    source: str  # 'yahoo', 'edgar', 'fda', 'fomc', etc.
    source_url: Optional[str] = None
    confidence: Literal["high", "med", "low"] = "med"
    scrape_timestamp: str

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, v: object) -> str:
        if v is None:
            raise ValueError("ticker is required")
        s = str(v).strip().upper()
        if not _TICKER_RE.match(s):
            raise ValueError(f"invalid ticker format: {s!r}")
        return s

    @field_validator("catalyst_date", mode="before")
    @classmethod
    def _require_catalyst_date(cls, v: object) -> str:
        if v is None:
            raise ValueError("catalyst_date is required")
        s = str(v).strip()
        if not s:
            raise ValueError("catalyst_date cannot be empty")
        if "T" in s:
            s = s.split("T", 1)[0]
        if " " in s:
            s = s.split(" ", 1)[0]
        return s

    @field_validator("source", mode="before")
    @classmethod
    def _require_source(cls, v: object) -> str:
        if v is None:
            raise ValueError("source is required")
        s = str(v).strip().lower()
        if not s:
            raise ValueError("source cannot be empty")
        return s
