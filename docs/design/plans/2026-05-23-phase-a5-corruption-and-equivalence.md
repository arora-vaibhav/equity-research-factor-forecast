# Phase A.5 — Corrupted-Value Acceptance Test + Equivalence Threshold

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`. The closer for Phase A.

**Goal:** Two deliverables per build plan §5.1.0 row A.5:

1. **Deliberately-corrupted-value acceptance test** -- prove that a
   bad source value is **flagged in `field_provenance`** with
   `weight=0.0` + `contributed_to_canonical=False`, AND is **dampened
   in canonical** (winsorized OR replaced via source-priority).
2. **Tighten the equivalence harness assertion threshold** -- add
   `assert_equivalence_within_threshold(report, max_drift_pct)` to
   `equivalence_harness.py`. Start at 5% (build plan §10 wording),
   document the path to 1%.

---

## Wave 1 — bounds-checking data-quality module

- [ ] `src/layer1_universe/data_quality.py`:
  - `FIELD_BOUNDS` dict mapping canonical-field name to (low, high).
  - `detect_field_corruption(ticker, field, value, bounds=None)
    -> tuple[bool, str | None]`.
  - `flag_corrupt_observations(observations, *, run_id, fetched_at,
    bounds=None) -> list[FieldProvenanceRow]`.
  - `DATA_QUALITY_VERSION = "1.0"`.
- [ ] Extend `equivalence_harness.py`:
  - `assert_equivalence_within_threshold(report, *, max_drift_pct=0.05)`.
- [ ] Unit tests for both.
- [ ] Commit Wave 1.

## Wave 2 — corruption acceptance integration test

- [ ] `tests/integration/test_a5_corruption.py` (`@pytest.mark.integration`).
- [ ] Update build plan §5.1.0 + §16 status tracker.
- [ ] Commit Wave 2.
