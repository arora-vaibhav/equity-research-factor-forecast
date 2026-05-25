"""Data-quality validators -- bounds checks + corruption flagging.

Per build plan A.5 acceptance criterion: "Corrupted source value
flagged in provenance, dampened in canonical."

This module is the *flagging* half. The dampening half is in
`src.layer1_universe.factors.winsorize` (already shipped pre-A.5) +
the source-priority resolution rules in
`src.common.datasources.resolution`.

Pure functions. No DB I/O. The caller hands in (ticker, field, value)
tuples; this module classifies each and returns audit-shaped rows.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional

from src.common.schemas import FieldProvenanceRow


DATA_QUALITY_VERSION = "1.0"


# Per-field sanity bounds. Tight enough to catch obvious corruption
# (negative market caps, RSI > 100) but loose enough to admit
# legitimate edge cases (negative earnings yield, very high P/E for
# speculative growth names). Bounds are *inclusive*.
#
# Notation: (low, high). `None` on either side disables that side.
FIELD_BOUNDS: dict[str, tuple[Optional[float], Optional[float]]] = {
    # Equity identifiers + market-cap range
    "market_cap_usd": (1e6, 1e14),       # 1M to 100T USD
    "price": (0.01, 1e6),                # 1 cent to $1M per share
    "avg_daily_volume": (0, 1e11),       # 0 to 100B shares/day
    # Valuation multiples -- generous bounds for cyclical / loss-makers
    "pe_ttm": (-1000.0, 5000.0),
    "pe_forward": (-1000.0, 5000.0),
    # Margins / ratios -- decimals
    "operating_margin": (-2.0, 1.0),     # allow large negative
    "net_profit_margin": (-2.0, 1.0),
    # Performance -- decimals
    "perf_1m": (-1.0, 10.0),
    "perf_3m": (-1.0, 10.0),
    "perf_6m": (-1.0, 20.0),
    "perf_12m": (-1.0, 50.0),
    # Distance from 52w high/low
    "dist_52w_high": (-1.0, 0.0),
    "dist_52w_low": (0.0, 50.0),
    # Technicals
    "rsi_14": (0.0, 100.0),
    # Capital-structure ratios
    "total_debt_to_equity": (-100.0, 1000.0),  # negative when equity < 0
    "interest_coverage": (-1000.0, 10000.0),
    # Short interest -- fraction of float
    "short_interest_pct_float": (0.0, 1.0),
    # News-activity composite + sub-signals
    "news_activity_score": (-10.0, 10.0),
}


def detect_field_corruption(
    ticker: str,
    field: str,
    value: Any,
    bounds: Optional[Mapping[str, tuple[Optional[float], Optional[float]]]] = None,
) -> tuple[bool, Optional[str]]:
    """Classify a single (ticker, field, value) observation.

    Parameters
    ----------
    ticker
        Symbol. Only used in the reason string.
    field
        Canonical field name. If not in `bounds`, the value is
        accepted as-is (we never fail on unknown fields -- new fields
        added without bounds entries shouldn't break the pipeline).
    value
        Observation. None / NaN / Inf are treated as 'missing, not
        corrupt' -- they return (False, None). The actual rejection
        path is bounds-violation.
    bounds
        Optional override map. Defaults to `FIELD_BOUNDS`.

    Returns
    -------
    (bool, Optional[str])
        (is_corrupt, reason). reason is None when the value passes.
    """
    bounds_map = bounds if bounds is not None else FIELD_BOUNDS

    if field not in bounds_map:
        return (False, None)

    if value is None:
        return (False, None)
    try:
        v = float(value)
    except (TypeError, ValueError):
        # Non-numeric where numeric expected -- treat as corrupt.
        return (True, f"non_numeric:{type(value).__name__}")
    if not math.isfinite(v):
        # NaN / Inf -- treat as missing (not corrupt) so downstream
        # NaN propagation works as designed.
        return (False, None)

    low, high = bounds_map[field]
    if low is not None and v < low:
        return (True, f"below_low_bound:{field}<{low}")
    if high is not None and v > high:
        return (True, f"above_high_bound:{field}>{high}")
    return (False, None)


def flag_corrupt_observations(
    observations: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    fetched_at: str,
    bounds: Optional[Mapping[str, tuple[Optional[float], Optional[float]]]] = None,
) -> list[FieldProvenanceRow]:
    """Classify each observation; emit one FieldProvenanceRow per corrupt.

    Parameters
    ----------
    observations
        Iterable of mappings with keys: 'ticker', 'field', 'source',
        'raw_value', 'parsed_value' (optional). Sane observations are
        SILENTLY SKIPPED -- the caller's normal resolution path will
        produce its own provenance entries with weight > 0. This
        function only emits weight=0 audit rows for the corrupt
        subset.
    run_id
        Stamped on every output row.
    fetched_at
        Stamped on every output row (ISO-Z).
    bounds
        Optional override map.

    Returns
    -------
    list[FieldProvenanceRow]
        One row per corrupt observation, with:
          - parsed_value = the corrupt value (audit-preserved)
          - raw_value = str(raw_value) when provided, else the reason
          - weight = 0.0 (does not contribute to canonical)
          - contributed_to_canonical = False
          - disagreement_pct = None (no comparison yet)
    """
    out: list[FieldProvenanceRow] = []
    for obs in observations:
        ticker = obs.get("ticker")
        field = obs.get("field")
        source = obs.get("source")
        parsed = obs.get("parsed_value", obs.get("raw_value"))
        raw = obs.get("raw_value")
        if ticker is None or field is None or source is None:
            continue
        is_corrupt, reason = detect_field_corruption(
            ticker=ticker, field=field, value=parsed, bounds=bounds,
        )
        if not is_corrupt:
            continue
        try:
            parsed_f = float(parsed) if parsed is not None else None
            if parsed_f is not None and not math.isfinite(parsed_f):
                parsed_f = None
        except (TypeError, ValueError):
            parsed_f = None
        out.append(
            FieldProvenanceRow(
                run_id=run_id,
                ticker=ticker,
                field=field,
                source=source,
                raw_value=str(raw) if raw is not None else (
                    reason or "corruption_detected"
                ),
                parsed_value=parsed_f,
                weight=0.0,
                contributed_to_canonical=False,
                disagreement_pct=None,
                fetched_at=fetched_at,
            )
        )
    return out
