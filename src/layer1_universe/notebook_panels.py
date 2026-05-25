"""Notebook panels for Phase A.4 -- helpers that read the v2 schema
and produce DataFrames the new `layer1_v2_drilldown.ipynb` displays.

All functions are *read-only* over the SQLite DB. None of them fetch
or mutate -- they pull whatever has accumulated in the raw_* /
canonical_universe / fetch_watermarks tables.

Three panels per spec / build plan A.4:

  * source_status(db)            -- per-(source, field) watermark roll-up.
  * provenance_summary(db)       -- per-raw_table row counts.
  * per_ticker_drilldown(db, t)  -- dict of per-source slices for one ticker.

Plus one helper:

  * apply_notebook_overrides(config, enabled_sources=None, force_refresh_sources=None)
      Shallow-clones a datasources.yaml-shaped dict and overrides the
      two lists. Pure-function so the notebook can wire control
      variables without leaking globals.

Defensive: queries that hit a non-existent table return an empty
DataFrame rather than raising, so the panels work on partially-migrated
DBs (e.g., a notebook running on an older schema during a transition).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

import pandas as pd


# Inventory of raw_* tables introduced across A.3.2 -> A.3.10. Order
# matches the chronological-ish display order in the drilldown panel.
_RAW_TABLES_FOR_TICKER: tuple[tuple[str, str], ...] = (
    # (drilldown key, table name with ticker column)
    ("fundamentals_yahoo", "raw_yahoo"),
    ("fundamentals_xbrl", "raw_edgar_fundamentals"),
    ("insider", "raw_edgar_insider"),
    ("filings", "raw_edgar_filings"),
    ("short_interest", "raw_finra"),
    ("ratios", "raw_stockanalysis_ratios"),
    ("openbb", "raw_openbb"),
    ("news_mentions", "raw_gdelt"),
    ("filing_tone", "raw_edgar_filing_tone"),
)

# Tables WITHOUT a per-ticker column (universe-level data).
_RAW_TABLES_UNIVERSE: tuple[str, ...] = (
    "raw_fred",
    "raw_pytrends",
)

# All raw_* tables for the provenance summary.
_ALL_RAW_TABLES: tuple[str, ...] = tuple(
    name for _, name in _RAW_TABLES_FOR_TICKER
) + _RAW_TABLES_UNIVERSE


def _safe_query(db, sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run sql, return DataFrame; on any error return empty frame."""
    try:
        with db.get_connection() as conn:
            cur = conn.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame(rows, columns=cols)
    except Exception:
        return pd.DataFrame()


def _safe_count(db, table: str) -> Optional[int]:
    """Return COUNT(*) of `table`, or None if the table doesn't exist."""
    try:
        with db.get_connection() as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return None


def _safe_max_timestamp(
    db, table: str, ts_col: str = "scrape_timestamp"
) -> Optional[str]:
    """Return MAX(ts_col) from `table`, or None if missing."""
    try:
        with db.get_connection() as conn:
            row = conn.execute(
                f"SELECT MAX({ts_col}) FROM {table}"
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Source Status panel
# ---------------------------------------------------------------------------


_SOURCE_STATUS_COLUMNS = [
    "source",
    "field",
    "enabled",
    "last_fetched_at",
    "last_observation_date",
    "fetch_count",
    "error_count",
    "last_error_message",
]


def source_status(
    db,
    *,
    registry=None,
) -> pd.DataFrame:
    """Per-(source, field) watermark roll-up with the enabled flag joined in.

    Parameters
    ----------
    db
        DatabaseManager. The function reads from `fetch_watermarks`.
    registry
        Optional `DataSourceRegistry` instance. When provided, the
        `enabled` column reflects `registry.enabled_sources()`. When
        omitted, `enabled` defaults to True for every source that
        appears in fetch_watermarks.

    Returns
    -------
    pd.DataFrame
        Columns: source, field, enabled, last_fetched_at,
        last_observation_date, fetch_count, error_count,
        last_error_message. Empty DataFrame (with these columns) when
        fetch_watermarks is empty.
    """
    df = _safe_query(
        db,
        """
        SELECT source, field, last_fetched_at, last_observation_date,
               fetch_count, error_count, last_error_message
          FROM fetch_watermarks
         ORDER BY source, field
        """,
    )
    if df.empty:
        return pd.DataFrame(columns=_SOURCE_STATUS_COLUMNS)

    if registry is not None:
        try:
            enabled_names = {s.name for s in registry.enabled_sources()}
        except Exception:
            enabled_names = set(df["source"].unique())
    else:
        enabled_names = set(df["source"].unique())

    df["enabled"] = df["source"].isin(enabled_names)
    return df[_SOURCE_STATUS_COLUMNS]


# ---------------------------------------------------------------------------
# Provenance Summary panel
# ---------------------------------------------------------------------------


_PROVENANCE_COLUMNS = [
    "raw_table",
    "row_count",
    "last_scrape_timestamp",
    "covered_tickers",
]


def provenance_summary(
    db,
    *,
    run_id: Optional[str] = None,
) -> pd.DataFrame:
    """Per-raw_table coverage summary.

    Parameters
    ----------
    db
        DatabaseManager.
    run_id
        Optional. When provided, row counts are filtered to that run
        (where the table has a run_id column). When absent, counts
        are over the entire table.

    Returns
    -------
    pd.DataFrame
        Columns: raw_table, row_count, last_scrape_timestamp,
        covered_tickers. `covered_tickers` is None for tables without
        a ticker column (FRED / pytrends).
    """
    rows: list[dict] = []
    for table in _ALL_RAW_TABLES:
        params: tuple = ()
        where_clause = ""
        if run_id is not None:
            where_clause = "WHERE run_id = ?"
            params = (run_id,)
        cnt_df = _safe_query(
            db, f"SELECT COUNT(*) FROM {table} {where_clause}", params,
        )
        if cnt_df.empty:
            rows.append({
                "raw_table": table,
                "row_count": None,
                "last_scrape_timestamp": None,
                "covered_tickers": None,
            })
            continue
        n = int(cnt_df.iloc[0, 0])

        last_ts = _safe_max_timestamp(db, table, "scrape_timestamp")

        covered = None
        if table in {name for _, name in _RAW_TABLES_FOR_TICKER}:
            t_df = _safe_query(
                db,
                f"SELECT DISTINCT ticker FROM {table} {where_clause}",
                params,
            )
            if not t_df.empty:
                covered = int(len(t_df))
            else:
                covered = 0

        rows.append({
            "raw_table": table,
            "row_count": n,
            "last_scrape_timestamp": last_ts,
            "covered_tickers": covered,
        })

    df = pd.DataFrame(rows, columns=_PROVENANCE_COLUMNS)
    return df


# ---------------------------------------------------------------------------
# Per-Ticker Drilldown panel
# ---------------------------------------------------------------------------


def per_ticker_drilldown(
    db,
    ticker: str,
    *,
    run_id: Optional[str] = None,
) -> dict[str, pd.DataFrame]:
    """All data for one ticker, sliced by raw_* table.

    Parameters
    ----------
    db
        DatabaseManager.
    ticker
        Symbol (case-insensitive; upper-cased before the query).
    run_id
        Optional run filter for tables that carry it.

    Returns
    -------
    dict[str, pd.DataFrame]
        Keys: 'canonical', 'fundamentals_yahoo', 'fundamentals_xbrl',
        'insider', 'filings', 'short_interest', 'ratios', 'openbb',
        'news_mentions', 'filing_tone', 'watermarks'. Each value is a
        DataFrame slice for the ticker (possibly empty).
    """
    t = (ticker or "").strip().upper()
    out: dict[str, pd.DataFrame] = {}

    run_clause = ""
    base_params: tuple = (t,)
    if run_id is not None:
        run_clause = " AND run_id = ?"
        base_params = (t, run_id)

    # canonical_universe has run_id but no materialized_at sort that we
    # can rely on portably; ORDER BY run_id DESC gives the freshest
    # snapshot last-inserted.
    out["canonical"] = _safe_query(
        db,
        f"SELECT * FROM canonical_universe WHERE ticker = ?{run_clause} "
        f"ORDER BY run_id DESC",
        base_params,
    )

    for key, table in _RAW_TABLES_FOR_TICKER:
        out[key] = _safe_query(
            db,
            f"SELECT * FROM {table} WHERE ticker = ?{run_clause}",
            base_params,
        )

    out["watermarks"] = _safe_query(
        db,
        "SELECT * FROM fetch_watermarks WHERE ticker = ? ORDER BY source, field",
        (t,),
    )

    return out


# ---------------------------------------------------------------------------
# Notebook control overrides
# ---------------------------------------------------------------------------


def apply_notebook_overrides(
    config: dict[str, Any],
    *,
    enabled_sources: Optional[Iterable[str]] = None,
    force_refresh_sources: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Shallow-clone `config` and override two notebook-controlled lists.

    Parameters
    ----------
    config
        A datasources.yaml-shaped dict. Untouched.
    enabled_sources
        When non-None, replaces config['enabled_sources'] with a list
        copy of the given iterable.
    force_refresh_sources
        When non-None, sets config['force_refresh_sources'] to a list
        copy of the given iterable. (Not in the base schema by default;
        downstream code consults it via config.get('force_refresh_sources',
        []) and bypasses watermark short-circuits for the named sources.)

    Returns
    -------
    dict
        A new dict with the overrides applied. The input is not
        mutated -- callers can safely re-apply different overrides
        from the same base config.
    """
    new_config = dict(config)
    if enabled_sources is not None:
        new_config["enabled_sources"] = list(enabled_sources)
    if force_refresh_sources is not None:
        new_config["force_refresh_sources"] = list(force_refresh_sources)
    return new_config
