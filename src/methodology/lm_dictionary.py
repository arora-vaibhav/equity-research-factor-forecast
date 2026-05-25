"""Loughran-McDonald finance-specific sentiment dictionary loader.

Reference:
  Loughran, T. and McDonald, B. (2011). "When Is a Liability Not a
  Liability? Textual Analysis, Dictionaries, and 10-Ks." Journal of
  Finance 66(1), 35-65.
  Loughran, T. and McDonald, B. (2014). "Measuring Readability in
  Financial Disclosures." Journal of Finance 69(4), 1643-1671.

This module loads the LM Master Dictionary CSV from
`config/lm_dictionary/LoughranMcDonald_MasterDictionary.csv` and exposes
a single function returning `dict[word, frozenset[tag]]`. Only words
with at least one non-zero category tag are kept (~3,876 of 86,553).

Pure I/O wrapper -- the CSV is the source of truth; the loader is cached
so repeated calls are free. No DB, no network.
"""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path


LM_DICTIONARY_VERSION = "2024"

# CSV column header -> internal tag name. The dictionary stores year-added
# (or 0) in each tag column; we treat any non-zero value as "word is in
# this category".
_TAG_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Negative", "negative"),
    ("Positive", "positive"),
    ("Uncertainty", "uncertainty"),
    ("Litigious", "litigious"),
    ("Strong_Modal", "strong_modal"),
    ("Weak_Modal", "weak_modal"),
    ("Constraining", "constraining"),
)

LM_TAGS: frozenset[str] = frozenset(t for _, t in _TAG_COLUMNS)


def _parse_int_safe(value: str) -> int:
    """Parse `value` as int; treat empty / blank / non-numeric as 0.

    The LM CSV uses bare integers; defensive against third-party
    redistributions that might carry NaN-ish cells.
    """
    if value is None:
        return 0
    s = str(value).strip()
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return 0


@lru_cache(maxsize=8)
def load_lm_dictionary(
    path: str | Path,
) -> dict[str, frozenset[str]]:
    """Load the Loughran-McDonald Master Dictionary from `path`.

    Parameters
    ----------
    path
        Filesystem path to the CSV. Stringified for the lru_cache key
        so both `str` and `Path` are accepted.

    Returns
    -------
    dict[str, frozenset[str]]
        Mapping from uppercase word to the frozenset of tag names for
        which the row is non-zero. Words with no non-zero tag are
        excluded.

    Raises
    ------
    FileNotFoundError
        If `path` does not exist.
    ValueError
        If the CSV header is missing the `Word` column or any tag
        column.

    Notes
    -----
    The result is memoised; pass the same `path` to share the dict.
    Cached return is the same object across calls -- do NOT mutate it.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"LM dictionary CSV not found at {p}. See "
            f"config/lm_dictionary/README.md for the source URL."
        )

    out: dict[str, frozenset[str]] = {}
    with p.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "Word" not in reader.fieldnames:
            raise ValueError(
                f"LM dictionary at {p} is missing the 'Word' column "
                f"(header: {reader.fieldnames!r})"
            )
        for col, _ in _TAG_COLUMNS:
            if col not in reader.fieldnames:
                raise ValueError(
                    f"LM dictionary at {p} is missing tag column {col!r} "
                    f"(header: {reader.fieldnames!r})"
                )
        for row in reader:
            word = (row.get("Word") or "").strip().upper()
            if not word:
                continue
            tags: list[str] = []
            for col, tag in _TAG_COLUMNS:
                if _parse_int_safe(row.get(col, "")) != 0:
                    tags.append(tag)
            if tags:
                out[word] = frozenset(tags)
    return out
