"""String-to-numeric parsers for Finviz and other scraped data.

All parsers return None on unparseable input rather than raising, so they
can be applied across a DataFrame without losing the row.
"""
from __future__ import annotations

import re
from typing import Optional


_MARKET_CAP_MULTIPLIERS = {
    "K": 1_000.0,
    "M": 1_000_000.0,
    "B": 1_000_000_000.0,
    "T": 1_000_000_000_000.0,
}


def _clean(raw: object) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in {"-", "--", "N/A", "NaN", "nan", "None"}:
        return None
    return s


def parse_market_cap(raw: object) -> Optional[float]:
    """Parse strings like '1.23B', '450M', '2.1T' into USD floats."""
    s = _clean(raw)
    if s is None:
        return None
    s = s.replace(",", "").replace("$", "")
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([KMBT])?", s, flags=re.IGNORECASE)
    if not match:
        try:
            return float(s)
        except ValueError:
            return None
    number = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    multiplier = _MARKET_CAP_MULTIPLIERS.get(suffix, 1.0)
    return number * multiplier


def convert_percentage(raw: object) -> Optional[float]:
    """Parse '12.5%' -> 0.125. Returns float, None on bad input."""
    s = _clean(raw)
    if s is None:
        return None
    s = s.replace(",", "").replace("%", "").strip()
    try:
        return float(s) / 100.0
    except ValueError:
        return None


def parse_currency(raw: object) -> Optional[float]:
    """Parse '$1,234.56' -> 1234.56."""
    s = _clean(raw)
    if s is None:
        return None
    s = s.replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_int(raw: object) -> Optional[int]:
    """Parse '1,234,567' -> 1234567. Strips commas and whitespace."""
    s = _clean(raw)
    if s is None:
        return None
    s = s.replace(",", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_float(raw: object) -> Optional[float]:
    """Parse a plain float, handling commas."""
    s = _clean(raw)
    if s is None:
        return None
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None
