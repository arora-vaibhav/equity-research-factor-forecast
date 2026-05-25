"""Equivalence harness comparing v1 universe_members vs v2 canonical_universe.

Per spec section 11. A.3 PRODUCES the report; A.5 SETS the assertion
threshold (currently a soft <=5% target).

Two outputs:

  * `equivalence_report(run_id, db) -> dict` -- in-memory drift report
    summarising row-count match, per-column max-abs-drift, and per-
    column n_disagreements for the subset of columns that exist in
    both tables under v1<->v2 name aliasing.

  * `write_equivalence_report(run_id, db, out_path)` -- writes the
    report to `data/runs/<run_id>/equivalence_report.json` (or any
    user-supplied path). Idempotent overwrite.

The harness is deliberately conservative: missing v1 or v2 data is
NOT a failure -- the report records the absence so callers can
distinguish 'we haven't dual-written this run yet' from 'real drift'.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
from pathlib import Path
from typing import Any, Optional


# v1 column name -> v2 canonical column name. Columns absent from this
# map are silently skipped (e.g., v1's 'finviz_long_score' has no v2
# analogue).
V1_TO_V2_COLUMN_ALIAS: dict[str, str] = {
    "ticker": "ticker",
    "company_name": "company_name",
    "sector": "sector",
    "industry": "industry",
    "market_cap_usd": "market_cap_usd",
    "price": "price",
    "avg_daily_volume": "avg_daily_volume",
    "pe_ratio": "pe_ttm",
    "forward_pe": "pe_forward",
    "operating_margin": "operating_margin",
    "net_profit_margin": "net_profit_margin",
    "perf_1y": "perf_12m",
    "dist_52w_high": "dist_52w_high",
    "dist_52w_low": "dist_52w_low",
    "rsi_14": "rsi_14",
}


def _safe_table_count(db, table: str, run_id: str) -> int:
    try:
        with db.get_connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _fetch_rows(db, table: str, run_id: str) -> list[dict]:
    try:
        with db.get_connection() as conn:
            cur = conn.execute(
                f"SELECT * FROM {table} WHERE run_id = ?", (run_id,)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def _max_abs_drift(v1: Any, v2: Any) -> float:
    """Return max-abs drift for one (v1, v2) cell.

    Conventions:
      * One side None or NaN -> 0.0 (treated as 'untracked', not drift).
      * Float cells -> absolute diff.
      * Categorical cells -> 0.0 if equal, math.inf otherwise.
    """
    if v1 is None and v2 is None:
        return 0.0
    if v1 is None or v2 is None:
        return 0.0
    if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
        if math.isnan(v1) or math.isnan(v2):
            return 0.0
        return abs(float(v1) - float(v2))
    return 0.0 if v1 == v2 else math.inf


def equivalence_report(run_id: str, db) -> dict:
    """Build the v1-vs-v2 drift report for `run_id`.

    Returns a JSON-serialisable dict:

      {
        'run_id': str,
        'generated_at': ISO-Z timestamp,
        'row_counts': {'v1_universe_members', 'v2_canonical_universe',
                       'shared_tickers'},
        'per_column': {col: {'max_abs_drift', 'n_disagreements'}},
        'status': 'ok' | 'partial' | 'no_data',
      }
    """
    v1_count = _safe_table_count(db, "universe_members", run_id)
    v2_count = _safe_table_count(db, "canonical_universe", run_id)

    if v1_count == 0 and v2_count == 0:
        status = "no_data"
    elif v1_count == 0 or v2_count == 0:
        status = "partial"
    else:
        status = "ok"

    v1_rows = _fetch_rows(db, "universe_members", run_id)
    v2_rows = _fetch_rows(db, "canonical_universe", run_id)

    v1_by_ticker = {r.get("ticker"): r for r in v1_rows if r.get("ticker")}
    v2_by_ticker = {r.get("ticker"): r for r in v2_rows if r.get("ticker")}
    shared = sorted(set(v1_by_ticker) & set(v2_by_ticker))

    per_column: dict[str, dict[str, Any]] = {}
    for v1_col, v2_col in V1_TO_V2_COLUMN_ALIAS.items():
        if v1_col == "ticker":
            continue
        max_drift = 0.0
        n_disagree = 0
        for ticker in shared:
            v1_val = v1_by_ticker[ticker].get(v1_col)
            v2_val = v2_by_ticker[ticker].get(v2_col)
            d = _max_abs_drift(v1_val, v2_val)
            if d > 0.0:
                n_disagree += 1
                if math.isinf(d):
                    max_drift = math.inf
                else:
                    max_drift = max(max_drift, d)
        per_column[v2_col] = {
            "max_abs_drift": (
                None if max_drift == math.inf else float(max_drift)
            ),
            "n_disagreements": n_disagree,
        }

    return {
        "run_id": run_id,
        "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
        "row_counts": {
            "v1_universe_members": v1_count,
            "v2_canonical_universe": v2_count,
            "shared_tickers": len(shared),
        },
        "per_column": per_column,
        "status": status,
    }


def assert_equivalence_within_threshold(
    report: dict,
    *,
    max_drift_pct: float = 0.05,
    columns: Optional[list[str]] = None,
) -> None:
    """Raise AssertionError if any per-column drift exceeds the threshold.

    Per A.5: A.3 produces the equivalence report; A.5 sets the
    assertion threshold. Default is 5 % (build plan §10 wording).
    A follow-on pass after a month of dual-write runs may tighten
    to 1 %.

    Parameters
    ----------
    report
        Output of `equivalence_report(run_id, db)`.
    max_drift_pct
        Maximum absolute drift PER COLUMN, as a fraction of 1.0
        (0.05 == 5 %). Columns whose `max_abs_drift` is None
        (categorical disagreement, captured as math.inf upstream)
        are always treated as failures when there is at least one
        disagreement.
    columns
        Optional whitelist of v2 column names to evaluate. When
        omitted, every entry in `report['per_column']` is checked.

    Raises
    ------
    AssertionError
        With a per-column breakdown of which columns exceeded the
        threshold and by how much.
    """
    per_column = report.get("per_column", {}) or {}
    if columns is not None:
        per_column = {c: per_column[c] for c in columns if c in per_column}

    failures: list[str] = []
    for col, stats in per_column.items():
        drift = stats.get("max_abs_drift")
        n_dis = stats.get("n_disagreements", 0)
        if drift is None:
            if n_dis > 0:
                failures.append(
                    f"{col}: categorical disagreement (n_disagreements={n_dis})"
                )
            continue
        if drift > max_drift_pct:
            failures.append(
                f"{col}: max_abs_drift={drift:.4g} > {max_drift_pct:.4g} "
                f"(n_disagreements={n_dis})"
            )

    if failures:
        raise AssertionError(
            "equivalence_report exceeded the drift threshold "
            f"(max_drift_pct={max_drift_pct}):\n  "
            + "\n  ".join(failures)
        )


def write_equivalence_report(
    run_id: str,
    db,
    out_path: str | Path | None = None,
) -> Path:
    """Compute the report and write JSON to disk.

    Default path: `data/runs/<run_id>/equivalence_report.json`. Creates
    parent directories as needed. Returns the path written.
    """
    report = equivalence_report(run_id, db)
    if out_path is None:
        out_path = Path("data") / "runs" / run_id / "equivalence_report.json"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    return out_path
