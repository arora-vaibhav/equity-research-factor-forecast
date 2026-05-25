"""Tests for `src.methodology.lm_dictionary`.

Two test bodies:
  * Loader-on-real-CSV: validates the committed
    `config/lm_dictionary/LoughranMcDonald_MasterDictionary.csv` parses
    and yields the expected Loughran-McDonald tag counts (matches the
    paper's reported figures within rounding).
  * Loader-on-synthetic-CSV (tmp_path): tokenization edge cases,
    FileNotFoundError, malformed-header ValueError, and idempotent
    cache behaviour.

Reads no production data. The real-CSV check is included because the
file is committed in repo and is the authoritative interface contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.methodology.lm_dictionary import (
    LM_DICTIONARY_VERSION,
    LM_TAGS,
    load_lm_dictionary,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_LM_CSV = REPO_ROOT / "config" / "lm_dictionary" / "LoughranMcDonald_MasterDictionary.csv"


def test_version_constant():
    assert LM_DICTIONARY_VERSION == "2024"


def test_tags_set_is_seven_categories():
    assert LM_TAGS == frozenset(
        {
            "negative",
            "positive",
            "uncertainty",
            "litigious",
            "strong_modal",
            "weak_modal",
            "constraining",
        }
    )


@pytest.mark.skipif(
    not REAL_LM_CSV.exists(),
    reason="real LM dictionary CSV not present in repo",
)
def test_load_real_dictionary_yields_expected_tag_counts():
    """Loading the committed CSV produces ~3,876 tagged words with the
    per-category counts reported in Loughran-McDonald 2011/2024."""
    load_lm_dictionary.cache_clear()
    d = load_lm_dictionary(REAL_LM_CSV)

    # Total tagged words. Allow a small drift in case sraf.nd.edu
    # re-publishes between yearly updates.
    assert 3500 <= len(d) <= 4500, len(d)

    counts = {tag: 0 for tag in LM_TAGS}
    for tags in d.values():
        for t in tags:
            counts[t] += 1

    # Loughran-McDonald 2024 release: 2,355 negative, 354 positive, 297
    # uncertainty, 905 litigious, 19 strong_modal, 27 weak_modal, 184
    # constraining. Ranges allow yearly LM patches.
    assert 2200 <= counts["negative"] <= 2500, counts
    assert 300 <= counts["positive"] <= 400, counts
    assert 250 <= counts["uncertainty"] <= 350, counts
    assert 800 <= counts["litigious"] <= 1000, counts
    assert 10 <= counts["strong_modal"] <= 30, counts
    assert 20 <= counts["weak_modal"] <= 40, counts
    assert 150 <= counts["constraining"] <= 250, counts


@pytest.mark.skipif(
    not REAL_LM_CSV.exists(),
    reason="real LM dictionary CSV not present in repo",
)
def test_load_real_dictionary_specific_words():
    """A handful of words known to be in the LM dictionary at well-known
    polarities. Stable across LM releases 2011-2024."""
    load_lm_dictionary.cache_clear()
    d = load_lm_dictionary(REAL_LM_CSV)
    # Negative anchors -- core LM 2011 set
    assert "negative" in d["LOSS"]
    assert "negative" in d["LITIGATION"]
    assert "negative" in d["BANKRUPTCY"]
    # Positive anchors
    assert "positive" in d["STRONG"]
    assert "positive" in d["BENEFICIAL"]


def test_missing_file_raises_filenotfounderror(tmp_path):
    load_lm_dictionary.cache_clear()
    with pytest.raises(FileNotFoundError, match="LM dictionary CSV not found"):
        load_lm_dictionary(tmp_path / "does_not_exist.csv")


def test_missing_word_column_raises_valueerror(tmp_path):
    load_lm_dictionary.cache_clear()
    csv = tmp_path / "bad.csv"
    csv.write_text("Foo,Bar\nx,y\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing the 'Word' column"):
        load_lm_dictionary(csv)


def test_missing_tag_column_raises_valueerror(tmp_path):
    load_lm_dictionary.cache_clear()
    csv = tmp_path / "bad.csv"
    # Has Word + Negative but is missing the other 6 tag columns.
    csv.write_text("Word,Negative\nAARDVARK,0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing tag column"):
        load_lm_dictionary(csv)


def test_loader_excludes_zero_tag_rows(tmp_path):
    """Words with all-zero tag columns are dropped from the output."""
    load_lm_dictionary.cache_clear()
    csv = tmp_path / "tiny.csv"
    csv.write_text(
        "Word,Negative,Positive,Uncertainty,Litigious,Strong_Modal,Weak_Modal,Constraining\n"
        "AARDVARK,0,0,0,0,0,0,0\n"
        "LOSS,2009,0,0,0,0,0,0\n"
        "STRONG,0,2009,0,0,0,0,0\n",
        encoding="utf-8",
    )
    d = load_lm_dictionary(csv)
    assert "AARDVARK" not in d
    assert d["LOSS"] == frozenset({"negative"})
    assert d["STRONG"] == frozenset({"positive"})


def test_loader_handles_multi_tag_row(tmp_path):
    """A word in multiple categories returns a frozenset of all tags."""
    load_lm_dictionary.cache_clear()
    csv = tmp_path / "tiny.csv"
    csv.write_text(
        "Word,Negative,Positive,Uncertainty,Litigious,Strong_Modal,Weak_Modal,Constraining\n"
        "RESTRICTED,0,0,0,2009,0,0,2011\n",
        encoding="utf-8",
    )
    d = load_lm_dictionary(csv)
    assert d["RESTRICTED"] == frozenset({"litigious", "constraining"})


def test_loader_normalizes_word_to_uppercase(tmp_path):
    load_lm_dictionary.cache_clear()
    csv = tmp_path / "tiny.csv"
    csv.write_text(
        "Word,Negative,Positive,Uncertainty,Litigious,Strong_Modal,Weak_Modal,Constraining\n"
        "  loss  ,2009,0,0,0,0,0,0\n",
        encoding="utf-8",
    )
    d = load_lm_dictionary(csv)
    assert "LOSS" in d
    assert "loss" not in d


def test_loader_handles_float_string_tag_values(tmp_path):
    """Some third-party redistributions write '2009.0' instead of '2009'.
    The loader treats them as non-zero."""
    load_lm_dictionary.cache_clear()
    csv = tmp_path / "tiny.csv"
    csv.write_text(
        "Word,Negative,Positive,Uncertainty,Litigious,Strong_Modal,Weak_Modal,Constraining\n"
        'LOSS,"2009.0",0,0,0,0,0,0\n',
        encoding="utf-8",
    )
    d = load_lm_dictionary(csv)
    assert d["LOSS"] == frozenset({"negative"})


def test_loader_is_cached_returns_same_object(tmp_path):
    """Same path returns the same dict object (lru_cache identity)."""
    load_lm_dictionary.cache_clear()
    csv = tmp_path / "tiny.csv"
    csv.write_text(
        "Word,Negative,Positive,Uncertainty,Litigious,Strong_Modal,Weak_Modal,Constraining\n"
        "LOSS,2009,0,0,0,0,0,0\n",
        encoding="utf-8",
    )
    d1 = load_lm_dictionary(csv)
    d2 = load_lm_dictionary(csv)
    assert d1 is d2

    # str and Path produce different cache keys but identical content.
    d3 = load_lm_dictionary(str(csv))
    assert d3 == d1
