"""Tests for the Cohen-Malloy-Pomorski 2012 opportunistic-insider classifier.

Operational rule (per the spec §6.3.2 and the methodology reference):
  A transaction is ROUTINE if its filer has placed >= 2 transactions in
  the same calendar month within the prior 3 years (strictly earlier
  than the transaction in question, with the lookback bounded to 3 years).
  Otherwise OPPORTUNISTIC.

`filer_id` (e.g., "COOK TIMOTHY D:0000320193") is the unit of identity:
the same physical insider trading the same issuer.

The classifier is a pure function:
  classify_transactions(txns: list[dict]) -> list[bool]
where:
  - txns is a list with keys: filer_id, transaction_date, transaction_code
  - returns a parallel list of bools where True == opportunistic
"""
from __future__ import annotations

import pytest

from src.methodology.opportunistic_insider import (
    classify_transactions,
    OPPORTUNISTIC_CLASSIFIER_VERSION,
)


def test_version_constant_exists():
    assert OPPORTUNISTIC_CLASSIFIER_VERSION == "1.0"


def test_single_transaction_is_opportunistic():
    txns = [
        {"filer_id": "ALICE:CIKA", "transaction_date": "2026-03-15", "transaction_code": "P"}
    ]
    assert classify_transactions(txns) == [True]


def test_empty_input_returns_empty_list():
    assert classify_transactions([]) == []


def test_filer_with_2_same_month_prior_3y_is_routine():
    """Threshold inclusive at 2: a filer with exactly 2 same-month-prior-3y
    transactions has the third March trade classified as routine."""
    txns = [
        {"filer_id": "BOB:CIKB", "transaction_date": "2023-03-10", "transaction_code": "S"},
        {"filer_id": "BOB:CIKB", "transaction_date": "2024-03-12", "transaction_code": "S"},
        {"filer_id": "BOB:CIKB", "transaction_date": "2026-03-15", "transaction_code": "S"},
    ]
    out = classify_transactions(txns)
    assert out[0] is True
    assert out[1] is True
    assert out[2] is False  # routine


def test_filer_with_3_same_month_prior_3y_is_routine():
    txns = [
        {"filer_id": "CARA:CIKC", "transaction_date": "2023-06-05", "transaction_code": "S"},
        {"filer_id": "CARA:CIKC", "transaction_date": "2024-06-10", "transaction_code": "S"},
        {"filer_id": "CARA:CIKC", "transaction_date": "2025-06-15", "transaction_code": "S"},
        {"filer_id": "CARA:CIKC", "transaction_date": "2026-06-20", "transaction_code": "S"},
    ]
    out = classify_transactions(txns)
    assert out[3] is False  # routine


def test_same_month_but_beyond_3y_lookback_is_opportunistic():
    """Edge: filer has same-month transactions only in years > 3 prior."""
    txns = [
        {"filer_id": "DAN:CIKD", "transaction_date": "2020-04-05", "transaction_code": "P"},
        {"filer_id": "DAN:CIKD", "transaction_date": "2021-04-05", "transaction_code": "P"},
        {"filer_id": "DAN:CIKD", "transaction_date": "2026-04-05", "transaction_code": "P"},
    ]
    out = classify_transactions(txns)
    assert out[2] is True  # opportunistic — priors are too far back


def test_same_month_within_3y_boundary_is_routine():
    """Boundary: a same-month trade exactly inside the prior-3-year window."""
    txns = [
        {"filer_id": "EVE:CIKE", "transaction_date": "2023-04-06", "transaction_code": "S"},
        {"filer_id": "EVE:CIKE", "transaction_date": "2024-04-10", "transaction_code": "S"},
        {"filer_id": "EVE:CIKE", "transaction_date": "2026-04-05", "transaction_code": "S"},
    ]
    out = classify_transactions(txns)
    assert out[2] is False  # routine


def test_different_filers_classified_independently():
    txns = [
        {"filer_id": "ROUTINE:X", "transaction_date": "2023-03-10", "transaction_code": "S"},
        {"filer_id": "ROUTINE:X", "transaction_date": "2024-03-10", "transaction_code": "S"},
        {"filer_id": "ROUTINE:X", "transaction_date": "2026-03-10", "transaction_code": "S"},
        {"filer_id": "ONEOFF:Y", "transaction_date": "2026-03-15", "transaction_code": "P"},
    ]
    out = classify_transactions(txns)
    assert out == [True, True, False, True]


def test_mixed_codes_count_toward_routine_threshold():
    """CMP counts transactions, not directions. P and S both count."""
    txns = [
        {"filer_id": "MIXY:Z", "transaction_date": "2023-09-05", "transaction_code": "S"},
        {"filer_id": "MIXY:Z", "transaction_date": "2024-09-05", "transaction_code": "P"},
        {"filer_id": "MIXY:Z", "transaction_date": "2026-09-05", "transaction_code": "S"},
    ]
    out = classify_transactions(txns)
    assert out[2] is False  # routine


def test_different_month_priors_do_not_count():
    """A filer with many priors but in DIFFERENT months stays opportunistic."""
    txns = [
        {"filer_id": "FREQ:W", "transaction_date": "2023-01-10", "transaction_code": "S"},
        {"filer_id": "FREQ:W", "transaction_date": "2023-07-15", "transaction_code": "S"},
        {"filer_id": "FREQ:W", "transaction_date": "2024-02-10", "transaction_code": "S"},
        {"filer_id": "FREQ:W", "transaction_date": "2025-11-20", "transaction_code": "S"},
        {"filer_id": "FREQ:W", "transaction_date": "2026-04-05", "transaction_code": "S"},
    ]
    out = classify_transactions(txns)
    assert out[4] is True


def test_focal_transaction_itself_not_counted():
    """A transaction does not count itself as a prior."""
    txns = [
        {"filer_id": "SELF:V", "transaction_date": "2024-05-10", "transaction_code": "S"},
        {"filer_id": "SELF:V", "transaction_date": "2026-05-10", "transaction_code": "S"},
    ]
    out = classify_transactions(txns)
    assert out[1] is True  # opportunistic — only 1 prior in May


def test_same_day_multiple_transactions_each_classified():
    """Multiple line items on the same day for the same filer."""
    txns = [
        {"filer_id": "EXSELL:U", "transaction_date": "2023-03-20", "transaction_code": "S"},
        {"filer_id": "EXSELL:U", "transaction_date": "2024-03-20", "transaction_code": "S"},
        {"filer_id": "EXSELL:U", "transaction_date": "2025-03-20", "transaction_code": "S"},
        {"filer_id": "EXSELL:U", "transaction_date": "2026-03-20", "transaction_code": "M"},
        {"filer_id": "EXSELL:U", "transaction_date": "2026-03-20", "transaction_code": "S"},
        {"filer_id": "EXSELL:U", "transaction_date": "2026-03-20", "transaction_code": "F"},
    ]
    out = classify_transactions(txns)
    assert out[3] is False
    assert out[4] is False
    assert out[5] is False


def test_classify_preserves_input_order():
    txns = [
        {"filer_id": "B:1", "transaction_date": "2026-01-10", "transaction_code": "P"},
        {"filer_id": "A:1", "transaction_date": "2026-01-15", "transaction_code": "S"},
        {"filer_id": "B:1", "transaction_date": "2026-02-10", "transaction_code": "P"},
    ]
    out = classify_transactions(txns)
    assert len(out) == 3
    assert out == [True, True, True]


def test_unsorted_input_ordered_correctly_internally():
    """Order-invariant on input: prior-history is by date."""
    txns_sorted = [
        {"filer_id": "ORD:1", "transaction_date": "2023-07-01", "transaction_code": "S"},
        {"filer_id": "ORD:1", "transaction_date": "2024-07-01", "transaction_code": "S"},
        {"filer_id": "ORD:1", "transaction_date": "2026-07-01", "transaction_code": "S"},
    ]
    txns_unsorted = [
        {"filer_id": "ORD:1", "transaction_date": "2026-07-01", "transaction_code": "S"},
        {"filer_id": "ORD:1", "transaction_date": "2023-07-01", "transaction_code": "S"},
        {"filer_id": "ORD:1", "transaction_date": "2024-07-01", "transaction_code": "S"},
    ]
    out_sorted = classify_transactions(txns_sorted)
    out_unsorted = classify_transactions(txns_unsorted)
    assert out_sorted == [True, True, False]
    assert out_unsorted[0] is False
    assert out_unsorted[1] is True
    assert out_unsorted[2] is True
