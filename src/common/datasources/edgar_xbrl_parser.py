"""Pure-function XBRL companyfacts parser.

Input: the JSON blob returned by:
  https://data.sec.gov/api/xbrl/companyfacts/CIK{padded}.json

Output: a list of `RawEdgarFundamentalsRow` covering every distinct
(fiscal_year, fiscal_period) combination that has at least one of the
canonical concepts populated. No HTTP, no DB — fully unit-testable.

Mapping (us-gaap concept -> our canonical field):
  Revenues OR RevenueFromContractWithCustomerExcludingAssessedTax -> revenue_ttm
  NetIncomeLoss                                                   -> net_income_ttm
  OperatingIncomeLoss                                             -> ebit_ttm proxy
  Assets                                                          -> total_assets
  Liabilities                                                     -> total_liabilities
  StockholdersEquity                                              -> total_equity
  CashAndCashEquivalentsAtCarryingValue                           -> cash_and_equivalents
  LongTermDebt + LongTermDebtNoncurrent                           -> total_debt (sum if both present)
  NetCashProvidedByUsedInOperatingActivities                      -> operating_cashflow
  PaymentsToAcquirePropertyPlantAndEquipment                      -> capex (stored as -abs)
  InterestExpense                                                 -> used for interest_coverage

Derived:
  fcf_ttm              = operating_cashflow - abs(capex)
  operating_margin     = ebit_ttm / revenue_ttm        (when both present and revenue != 0)
  net_profit_margin    = net_income_ttm / revenue_ttm  (when both present and revenue != 0)
  total_debt_to_equity = total_debt / total_equity     (when total_equity not in {None, 0})
  interest_coverage    = ebit_ttm / interest_expense   (when interest_expense not in {None, 0})

Fiscal-period normalization:
  source `fp` field is one of {"FY", "Q1", "Q2", "Q3", "Q4"}.
  We map "FY" -> "annual" to match RawEdgarFundamentalsRow's Literal.
"""
from __future__ import annotations

from typing import Iterable, Optional

from src.common.schemas import RawEdgarFundamentalsRow


# Concept tag preferences (first non-empty wins)
_REVENUE_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")


def extract_concept_observations(
    blob: dict, taxonomy: str, concept: str, unit: str = "USD",
) -> list[dict]:
    """Return the list of observations under facts[taxonomy][concept][units][unit].

    Returns [] if any node in the path is missing.
    """
    try:
        return list(
            blob["facts"][taxonomy][concept]["units"][unit]
        )
    except (KeyError, TypeError):
        return []


def _first_concept_observations(
    blob: dict, taxonomy: str, concepts: Iterable[str],
) -> list[dict]:
    """Return observations for the first concept in `concepts` that has any data."""
    for c in concepts:
        obs = extract_concept_observations(blob, taxonomy, c)
        if obs:
            return obs
    return []


def _normalize_period(fp: str) -> Optional[str]:
    if fp == "FY":
        return "annual"
    if fp in {"Q1", "Q2", "Q3", "Q4"}:
        return fp
    return None


def _normalize_form(form: str) -> Optional[str]:
    if form == "10-K":
        return "10-K"
    if form == "10-Q":
        return "10-Q"
    # 10-K/A and 10-Q/A (amendments) map to their base form
    if form == "10-K/A":
        return "10-K"
    if form == "10-Q/A":
        return "10-Q"
    return None


def _index_by_period(observations: list[dict]) -> dict[tuple[int, str], dict]:
    """Index a list of observations by (fy, normalized_period).

    When duplicate observations exist for the same (fy, fp), the LATEST `filed`
    date wins (we want the most recently filed value as our first-observed
    canonical, since the parser is called fresh from live SEC data each run).
    Note this is parser-internal; the DB-level INSERT OR IGNORE then preserves
    the first run's value across runs.
    """
    out: dict[tuple[int, str], dict] = {}
    for o in observations:
        fy = o.get("fy")
        fp_norm = _normalize_period(str(o.get("fp", "")))
        if fy is None or fp_norm is None:
            continue
        key = (int(fy), fp_norm)
        prev = out.get(key)
        if prev is None or str(o.get("filed", "")) > str(prev.get("filed", "")):
            out[key] = o
    return out


def compute_ttm_from_quarters(quarters: list[dict]) -> Optional[float]:
    """Sum the 4 most recent quarterly observations (by `end` date).

    Returns None if fewer than 4 quarters are available.
    """
    if not quarters or len(quarters) < 4:
        return None
    sorted_q = sorted(quarters, key=lambda o: str(o.get("end", "")))
    latest_4 = sorted_q[-4:]
    try:
        return float(sum(float(o["val"]) for o in latest_4))
    except (KeyError, TypeError, ValueError):
        return None


def _safe_div(num: Optional[float], denom: Optional[float]) -> Optional[float]:
    if num is None or denom is None:
        return None
    if denom == 0:
        return None
    return num / denom


def _clamp_margin(x: Optional[float]) -> Optional[float]:
    """Margins are stored with Pydantic range [-1.0, 1.0]; clip extremes that
    can occur for distressed firms where net_income > revenue (e.g. one-time
    gains) so we don't fail validation. Out-of-range becomes None — the row
    still persists with the rest of its fields."""
    if x is None:
        return None
    if x < -1.0 or x > 1.0:
        return None
    return x


def _as_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sum_if_any(*vals: Optional[float]) -> Optional[float]:
    present = [v for v in vals if v is not None]
    if not present:
        return None
    return float(sum(present))


def parse_companyfacts(
    blob: dict,
    run_id: str,
    ticker: str,
    scrape_timestamp: str,
) -> list[RawEdgarFundamentalsRow]:
    """Parse a companyfacts JSON blob into a list of RawEdgarFundamentalsRow.

    Emits one row per (fiscal_year, fiscal_period) that has at least a
    revenue observation. All other fields are best-effort and become None
    if the underlying us-gaap concept is missing for that period.
    """
    cik_int = blob.get("cik")
    if cik_int is None:
        return []
    cik = str(cik_int).zfill(10)

    revenue_idx = _index_by_period(_first_concept_observations(blob, "us-gaap", _REVENUE_CONCEPTS))
    ni_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "NetIncomeLoss"))
    oi_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "OperatingIncomeLoss"))
    assets_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "Assets"))
    liab_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "Liabilities"))
    equity_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "StockholdersEquity"))
    cash_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "CashAndCashEquivalentsAtCarryingValue"))
    ltd_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "LongTermDebt"))
    ltd_nc_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "LongTermDebtNoncurrent"))
    ocf_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "NetCashProvidedByUsedInOperatingActivities"))
    capex_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"))
    int_exp_idx = _index_by_period(extract_concept_observations(blob, "us-gaap", "InterestExpense"))

    rows: list[RawEdgarFundamentalsRow] = []
    for (fy, fp_norm), rev_obs in revenue_idx.items():
        form_norm = _normalize_form(str(rev_obs.get("form", "")))
        if form_norm is None:
            continue
        filing_date = str(rev_obs.get("filed", ""))
        if not filing_date:
            continue
        accepted_at = rev_obs.get("accepted")

        revenue = _as_float(rev_obs.get("val"))
        ni = _as_float((ni_idx.get((fy, fp_norm)) or {}).get("val"))
        oi = _as_float((oi_idx.get((fy, fp_norm)) or {}).get("val"))

        total_assets = _as_float((assets_idx.get((fy, fp_norm)) or {}).get("val"))
        total_liab = _as_float((liab_idx.get((fy, fp_norm)) or {}).get("val"))
        total_equity = _as_float((equity_idx.get((fy, fp_norm)) or {}).get("val"))
        cash = _as_float((cash_idx.get((fy, fp_norm)) or {}).get("val"))

        ltd = _as_float((ltd_idx.get((fy, fp_norm)) or {}).get("val"))
        ltd_nc = _as_float((ltd_nc_idx.get((fy, fp_norm)) or {}).get("val"))
        total_debt = _sum_if_any(ltd, ltd_nc)

        ocf = _as_float((ocf_idx.get((fy, fp_norm)) or {}).get("val"))
        capex_raw = _as_float((capex_idx.get((fy, fp_norm)) or {}).get("val"))
        # Spec convention: capex stored as negative; XBRL value is typically positive (cash outflow)
        capex = -abs(capex_raw) if capex_raw is not None else None
        fcf = (ocf - abs(capex)) if (ocf is not None and capex is not None) else None

        int_exp = _as_float((int_exp_idx.get((fy, fp_norm)) or {}).get("val"))

        op_margin = _clamp_margin(_safe_div(oi, revenue))
        np_margin = _clamp_margin(_safe_div(ni, revenue))
        d2e = _safe_div(total_debt, total_equity)
        int_cov = _safe_div(oi, int_exp)  # ebit / interest_expense; None if int_exp in (None, 0)

        try:
            row = RawEdgarFundamentalsRow(
                run_id=run_id,
                ticker=ticker,
                cik=cik,
                fiscal_year=fy,
                fiscal_period=fp_norm,
                filing_date=filing_date,
                accepted_at=accepted_at,
                form_type=form_norm,
                revenue_ttm=revenue,
                ebit_ttm=oi,
                net_income_ttm=ni,
                total_assets=total_assets,
                total_liabilities=total_liab,
                total_equity=total_equity,
                cash_and_equivalents=cash,
                total_debt=total_debt,
                operating_cashflow=ocf,
                capex=capex,
                fcf_ttm=fcf,
                operating_margin=op_margin,
                net_profit_margin=np_margin,
                total_debt_to_equity=d2e,
                interest_coverage=int_cov,
                scrape_timestamp=scrape_timestamp,
            )
        except Exception:
            continue
        rows.append(row)
    return rows
