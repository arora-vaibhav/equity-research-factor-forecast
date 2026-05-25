import sqlite3
import pandas as pd
from pathlib import Path
import datetime
import json
from src.common.schemas import RunManifest


class DatabaseManager:
    def __init__(self, db_path: str = "data/fundamentals.db"):
        self.db_path = Path(db_path)
        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def migrate(self) -> None:
        """Idempotent Phase A schema migration. Safe to call multiple times."""
        ddl_statements = [
            """
            CREATE TABLE IF NOT EXISTS schema_version (
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT PRIMARY KEY,
              run_timestamp TEXT NOT NULL,
              run_type TEXT NOT NULL,
              source_label TEXT,
              status TEXT NOT NULL DEFAULT 'ok',
              config_snapshot_json TEXT,
              notes TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS universe_members (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              company_name TEXT,
              sector TEXT,
              industry TEXT,
              market_cap_usd REAL,
              price REAL,
              avg_daily_volume INTEGER,
              pe_ratio REAL,
              forward_pe REAL,
              operating_margin REAL,
              net_profit_margin REAL,
              perf_1y REAL,
              dist_52w_high REAL,
              dist_52w_low REAL,
              rsi_14 REAL,
              PRIMARY KEY (run_id, ticker),
              FOREIGN KEY (run_id) REFERENCES runs(run_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS candidate_results (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              playbook TEXT NOT NULL,
              value_score REAL,
              quality_score REAL,
              momentum_score REAL,
              lowvol_score REAL,
              revisions_score REAL,
              composite_score REAL,
              eligible INTEGER NOT NULL DEFAULT 0,
              reasoning TEXT,
              PRIMARY KEY (run_id, ticker, playbook),
              FOREIGN KEY (run_id) REFERENCES runs(run_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS filter_waterfall (
              run_id TEXT NOT NULL,
              playbook TEXT NOT NULL,
              step_order INTEGER NOT NULL,
              step_name TEXT NOT NULL,
              rows_before INTEGER NOT NULL,
              rows_after INTEGER NOT NULL,
              rows_dropped INTEGER NOT NULL,
              PRIMARY KEY (run_id, playbook, step_order),
              FOREIGN KEY (run_id) REFERENCES runs(run_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS data_quality_metrics (
              run_id TEXT NOT NULL,
              metric_name TEXT NOT NULL,
              metric_value REAL,
              PRIMARY KEY (run_id, metric_name),
              FOREIGN KEY (run_id) REFERENCES runs(run_id)
            )
            """,
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in ddl_statements:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 1")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (1, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def get_schema_version(self) -> int:
        """Return the highest version recorded in schema_version, or 0 if none."""
        with self.get_connection() as conn:
            try:
                cur = conn.execute("SELECT MAX(version) FROM schema_version")
                row = cur.fetchone()
            except sqlite3.OperationalError:
                return 0
        return int(row[0]) if row and row[0] is not None else 0

    def migrate_to_v2(self) -> None:
        """Idempotent migration v1 -> v2 per the v2 architecture spec section 6.

        Calls migrate() first to guarantee v1 baseline, then additively creates:
          - raw_finviz VIEW (alias over finviz_universe_history)
          - canonical_universe (wide snapshot)
          - thesis_objects (placeholder, Layer 2 populates)
          - field_provenance (per-source observations)
          - source_run_log (per-source health)
          - historical_price, historical_iv, historical_earnings_reactions
          - posterior_cache, agent_response_cache

        Safe to call multiple times. Existing data is preserved.
        """
        self.migrate()  # ensure v1 baseline

        v2_ddl = [
            # finviz_universe_history is created defensively for fresh-DB case.
            # Existing databases already have it with more columns; IF NOT EXISTS
            # leaves any pre-existing definition untouched.
            """
            CREATE TABLE IF NOT EXISTS finviz_universe_history (
              Ticker TEXT,
              Company TEXT,
              scrape_timestamp TEXT
            )
            """,
            """
            CREATE VIEW IF NOT EXISTS raw_finviz AS
              SELECT * FROM finviz_universe_history
            """,
            """
            CREATE TABLE IF NOT EXISTS canonical_universe (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              company_name TEXT,
              sector TEXT,
              industry TEXT,
              exchange TEXT,
              cik TEXT,
              market_cap_usd REAL,
              price REAL,
              avg_daily_volume INTEGER,
              pe_ttm REAL,
              pe_forward REAL,
              ebit_ttm REAL,
              fcf_ttm REAL,
              operating_margin REAL,
              net_profit_margin REAL,
              roe REAL,
              roic REAL,
              total_debt_to_equity REAL,
              interest_coverage REAL,
              revenue_growth_yoy REAL,
              eps_growth_yoy REAL,
              perf_1m REAL,
              perf_3m REAL,
              perf_6m REAL,
              perf_12m REAL,
              rsi_14 REAL,
              dist_52w_high REAL,
              dist_52w_low REAL,
              dist_200dma REAL,
              short_interest_pct_float REAL,
              news_activity_score REAL,
              pe_5y_percentile REAL,
              ev_ebitda_5y_percentile REAL,
              data_quality_score REAL,
              materialized_at TEXT,
              PRIMARY KEY (run_id, ticker)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS thesis_objects (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              direction TEXT NOT NULL,
              target_price REAL NOT NULL,
              horizon_days INTEGER NOT NULL,
              confidence_prior REAL NOT NULL,
              invalidation_conditions TEXT,
              fundamental_bias_score REAL,
              news_bias_score REAL,
              macro_context TEXT,
              technical_setup TEXT,
              earnings_in_window TEXT,
              ai_research_synthesis TEXT,
              built_at TEXT,
              PRIMARY KEY (run_id, ticker)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS field_provenance (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              field TEXT NOT NULL,
              source TEXT NOT NULL,
              raw_value TEXT,
              parsed_value REAL,
              weight REAL,
              contributed_to_canonical INTEGER NOT NULL DEFAULT 0,
              disagreement_pct REAL,
              fetched_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_provenance_run_ticker ON field_provenance(run_id, ticker)",
            "CREATE INDEX IF NOT EXISTS idx_provenance_run_field ON field_provenance(run_id, field)",
            """
            CREATE TABLE IF NOT EXISTS source_run_log (
              run_id TEXT NOT NULL,
              source TEXT NOT NULL,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              status TEXT NOT NULL,
              rows_fetched INTEGER NOT NULL DEFAULT 0,
              error_message TEXT,
              error_traceback TEXT,
              PRIMARY KEY (run_id, source)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS historical_price (
              ticker TEXT NOT NULL,
              observation_date TEXT NOT NULL,
              open REAL,
              high REAL,
              low REAL,
              close REAL,
              volume INTEGER,
              adj_close REAL,
              source TEXT,
              scrape_timestamp TEXT,
              PRIMARY KEY (ticker, observation_date)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_hist_price_ticker_date ON historical_price(ticker, observation_date DESC)",
            """
            CREATE TABLE IF NOT EXISTS historical_iv (
              ticker TEXT NOT NULL,
              observation_date TEXT NOT NULL,
              expiry_date TEXT NOT NULL,
              dte_days INTEGER,
              atm_iv REAL,
              atm_strike REAL,
              iv_rank REAL,
              source TEXT,
              scrape_timestamp TEXT,
              PRIMARY KEY (ticker, observation_date, expiry_date)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_hist_iv_ticker_date ON historical_iv(ticker, observation_date DESC)",
            """
            CREATE TABLE IF NOT EXISTS historical_earnings_reactions (
              ticker TEXT NOT NULL,
              earnings_date TEXT NOT NULL,
              pre_earnings_iv REAL,
              post_earnings_iv REAL,
              iv_crush_pct REAL,
              absolute_move_pct REAL,
              beat_or_miss TEXT NOT NULL,
              source TEXT,
              PRIMARY KEY (ticker, earnings_date)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS posterior_cache (
              ticker TEXT NOT NULL,
              evidence_hash TEXT NOT NULL,
              model_name TEXT NOT NULL,
              model_version TEXT NOT NULL,
              posterior_blob TEXT NOT NULL,
              credible_interval_blob TEXT NOT NULL,
              computed_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              PRIMARY KEY (ticker, evidence_hash, model_name)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_response_cache (
              ticker TEXT NOT NULL,
              agent_name TEXT NOT NULL,
              agent_version TEXT NOT NULL,
              evidence_hash TEXT NOT NULL,
              response_blob TEXT NOT NULL,
              confidence REAL NOT NULL,
              computed_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              PRIMARY KEY (ticker, agent_name, evidence_hash)
            )
            """,
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v2_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 2")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (2, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def migrate_to_v3(self) -> None:
        """Idempotent migration v2 -> v3 per A.3 spec section 5.

        Adds fetch_watermarks table (per (source, ticker, field) accumulation tracking).
        Note: switching insert_historical_price to INSERT OR IGNORE is done as a
        code change in that helper, not via DDL.

        Safe to call multiple times. Existing data preserved.
        """
        self.migrate_to_v2()  # ensure v2 baseline

        v3_ddl = [
            """
            CREATE TABLE IF NOT EXISTS fetch_watermarks (
              source TEXT NOT NULL,
              ticker TEXT NOT NULL,
              field TEXT NOT NULL,
              last_fetched_at TEXT NOT NULL,
              last_observation_date TEXT,
              fetch_count INTEGER NOT NULL DEFAULT 0,
              error_count INTEGER NOT NULL DEFAULT 0,
              last_error_message TEXT,
              PRIMARY KEY (source, ticker, field)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_watermark_source ON fetch_watermarks(source)",
            "CREATE INDEX IF NOT EXISTS idx_watermark_ticker ON fetch_watermarks(ticker)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v3_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 3")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (3, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def migrate_to_v4(self) -> None:
        """Idempotent migration v3 -> v4 per A.3 spec section 6.2.

        Adds raw_yahoo table (per (run_id, ticker) Yahoo source-native snapshot).
        Safe to call multiple times. Existing data preserved.
        """
        self.migrate_to_v3()

        v4_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_yahoo (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              company_name TEXT,
              sector TEXT,
              industry TEXT,
              exchange TEXT,
              market_cap REAL,
              price REAL,
              avg_daily_volume INTEGER,
              pe_ttm REAL,
              pe_forward REAL,
              ebit_ttm REAL,
              fcf_ttm REAL,
              total_debt REAL,
              cash REAL,
              book_value REAL,
              operating_margin REAL,
              net_profit_margin REAL,
              revenue_growth_yoy REAL,
              eps_growth_yoy REAL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (run_id, ticker)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_yahoo_ticker ON raw_yahoo(ticker)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v4_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 4")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (4, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def migrate_to_v5(self) -> None:
        """Idempotent migration v4 -> v5 per A.3 spec section 6.3.1.

        Adds:
          - raw_edgar_fundamentals: per-(run_id, ticker, fiscal_period, fiscal_year)
            structured XBRL output parsed from companyfacts JSON.
          - sec_ticker_cik_map: cached ticker->CIK lookup snapshot
            (refreshed daily via the sec_cik_lookup watermark).

        Safe to call multiple times. Existing data preserved.

        raw_edgar_fundamentals PRIMARY KEY = (run_id, ticker, fiscal_period, fiscal_year).
        Inserts use INSERT OR IGNORE: once a period is recorded, subsequent runs
        on the same run_id do not overwrite (Principle 5 — no silent overwrite).
        """
        self.migrate_to_v4()

        v5_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_edgar_fundamentals (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              cik TEXT NOT NULL,
              fiscal_year INTEGER NOT NULL,
              fiscal_period TEXT NOT NULL,
              filing_date TEXT NOT NULL,
              accepted_at TEXT,
              form_type TEXT NOT NULL,
              revenue_ttm REAL,
              ebit_ttm REAL,
              net_income_ttm REAL,
              total_assets REAL,
              total_liabilities REAL,
              total_equity REAL,
              cash_and_equivalents REAL,
              total_debt REAL,
              operating_cashflow REAL,
              capex REAL,
              fcf_ttm REAL,
              operating_margin REAL,
              net_profit_margin REAL,
              total_debt_to_equity REAL,
              interest_coverage REAL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (run_id, ticker, fiscal_period, fiscal_year)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_fund_ticker ON raw_edgar_fundamentals(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_fund_cik ON raw_edgar_fundamentals(cik)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_fund_filing_date ON raw_edgar_fundamentals(filing_date)",
            """
            CREATE TABLE IF NOT EXISTS sec_ticker_cik_map (
              ticker TEXT PRIMARY KEY,
              cik TEXT NOT NULL,
              company_name TEXT,
              snapshot_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_sec_ticker_cik_map_cik ON sec_ticker_cik_map(cik)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v5_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 5")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (5, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def migrate_to_v6(self) -> None:
        """Idempotent migration v5 -> v6 per A.3 spec sections 6.3.2 + 6.3.3.

        Adds:
          - raw_edgar_insider: per-line-item Form 4 non-derivative
            transactions, classified opportunistic vs routine via the
            Cohen-Malloy-Pomorski 2012 rule.
          - raw_edgar_filings: per-filing envelope record covering 8-K,
            10-K, 10-Q, Form 4 (and amendments). The Form 4 line items
            live in raw_edgar_insider; this table is the audit trail.

        Safe to call multiple times. Existing data preserved.

        raw_edgar_insider PRIMARY KEY =
          (cik, source_filing_accn, filer_name, transaction_date, transaction_code).
        raw_edgar_filings PRIMARY KEY = (cik, source_filing_accn).

        Both use INSERT OR IGNORE on insert (Principle 5: no silent
        overwrite). First-observed values win; amendments are recorded as
        new filing rows but their re-stated line items are silently
        dropped if they collide with the original (cik, accn, filer,
        date, code) tuple.
        """
        self.migrate_to_v5()

        v6_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_edgar_insider (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              cik TEXT NOT NULL,
              filer_name TEXT NOT NULL,
              filer_title TEXT,
              filer_is_officer INTEGER,
              filer_is_director INTEGER,
              filer_is_10pct_owner INTEGER,
              transaction_date TEXT NOT NULL,
              transaction_code TEXT NOT NULL,
              transaction_code_description TEXT,
              shares REAL NOT NULL,
              price_per_share REAL,
              total_value REAL,
              shares_after_transaction REAL,
              is_opportunistic INTEGER,
              opportunistic_classifier_version TEXT,
              is_derivative INTEGER NOT NULL DEFAULT 0,
              source_filing_accn TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (cik, source_filing_accn, filer_name, transaction_date, transaction_code)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_insider_ticker ON raw_edgar_insider(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_insider_cik ON raw_edgar_insider(cik)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_insider_txn_date ON raw_edgar_insider(transaction_date)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_insider_filer ON raw_edgar_insider(filer_name)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_insider_opportunistic ON raw_edgar_insider(is_opportunistic)",
            """
            CREATE TABLE IF NOT EXISTS raw_edgar_filings (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              cik TEXT NOT NULL,
              form_type TEXT NOT NULL,
              filing_date TEXT NOT NULL,
              accepted_at TEXT,
              item_codes TEXT,
              source_filing_accn TEXT NOT NULL,
              primary_doc_url TEXT,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (cik, source_filing_accn)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_filings_ticker ON raw_edgar_filings(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_filings_cik ON raw_edgar_filings(cik)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_filings_filing_date ON raw_edgar_filings(filing_date)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_filings_form_type ON raw_edgar_filings(form_type)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v6_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 6")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (6, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def migrate_to_v7(self) -> None:
        """Idempotent migration v6 -> v7 per A.3 spec sections 6.4 + 6.5.

        Adds:
          - raw_fred: long-format FRED macro observations. PK
            (series_id, observation_date). `realtime_start`/`realtime_end`
            accommodate future ALFRED vintage-data integration; the v1
            FredSource leaves them None.
          - raw_finra: per-(ticker, settlement_date, exchange) short-
            interest / short-volume aggregates from FINRA's CDN. PK
            (ticker, settlement_date, exchange).

        Safe to call multiple times. Existing data preserved.

        Both use INSERT OR IGNORE on insert (Principle 5: no silent
        overwrite). First-observed values win — re-runs are idempotent
        and amendments to historical observations (FRED revisions,
        FINRA re-statements) are silently dropped unless they arrive
        with a distinct realtime_start/realtime_end (ALFRED) or a
        distinct exchange marker.
        """
        self.migrate_to_v6()

        v7_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_fred (
              run_id TEXT NOT NULL,
              series_id TEXT NOT NULL,
              observation_date TEXT NOT NULL,
              value REAL,
              realtime_start TEXT,
              realtime_end TEXT,
              source_filename TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (series_id, observation_date)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_fred_series ON raw_fred(series_id)",
            "CREATE INDEX IF NOT EXISTS idx_raw_fred_date ON raw_fred(observation_date)",
            """
            CREATE TABLE IF NOT EXISTS raw_finra (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              settlement_date TEXT NOT NULL,
              exchange TEXT NOT NULL,
              short_interest_shares REAL NOT NULL,
              avg_daily_volume REAL,
              days_to_cover REAL,
              source_filename TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (ticker, settlement_date, exchange)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_finra_ticker ON raw_finra(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_finra_settle ON raw_finra(settlement_date)",
            "CREATE INDEX IF NOT EXISTS idx_raw_finra_exchange ON raw_finra(exchange)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v7_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 7")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (7, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def migrate_to_v8(self) -> None:
        """Idempotent migration v7 -> v8 per A.3 spec section 6.6.

        Adds:
          - raw_stockanalysis_ratios: long-format scraped ratio history
            from stockanalysis.com (annual + quarterly + ttm). PK
            (ticker, metric, period_end_date). Powers the 5-year
            percentile features `pe_5y_percentile` and
            `ev_ebitda_5y_percentile` in the A.3.10 materialization.

        Safe to call multiple times. Existing data preserved.

        Uses INSERT OR IGNORE on insert (Principle 5: no silent
        overwrite). The first-observed value for any
        (ticker, metric, period_end_date) wins; subsequent scrapes of
        the same historical bucket are silently dropped — ratios that
        far back are settled and not subject to legitimate revision.
        Recent buckets (current TTM, last-quarter) can be force-refreshed
        by deleting their watermark, which makes the next pull bypass the
        90-day skip. Updating *value* in place is intentionally not
        supported in v1.
        """
        self.migrate_to_v7()

        v8_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_stockanalysis_ratios (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              metric TEXT NOT NULL,
              period_end_date TEXT NOT NULL,
              period_type TEXT NOT NULL,
              value REAL,
              source_url TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (ticker, metric, period_end_date)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_sar_ticker ON raw_stockanalysis_ratios(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_sar_metric ON raw_stockanalysis_ratios(metric)",
            "CREATE INDEX IF NOT EXISTS idx_sar_period ON raw_stockanalysis_ratios(period_end_date)",
            "CREATE INDEX IF NOT EXISTS idx_sar_period_type ON raw_stockanalysis_ratios(period_type)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v8_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 8")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (8, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def migrate_to_v9(self) -> None:
        """Idempotent migration v8 -> v9 per A.3 spec section 6.7.

        Adds:
          - raw_openbb: long-format multi-provider observations pulled by
            OpenBBSource (FMP-stable + Polygon + Tiingo). PK is
            (run_id, ticker, field_name, provider_used) so the same
            canonical field pulled from two providers writes TWO rows -
            that's the cross-vendor cross-validation substrate the
            materialization layer (A.3.10) joins on.

        Safe to call multiple times. Existing data preserved.

        Uses INSERT OR IGNORE on insert (Principle 5: no silent overwrite).
        The first-observed value for any
        (run_id, ticker, field_name, provider_used) wins; a re-pull within
        the same run_id is silently dropped. A fresh run_id is the
        supported way to refresh - the run_id in the PK is what makes
        per-run snapshots distinguishable.
        """
        self.migrate_to_v8()

        v9_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_openbb (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              field_name TEXT NOT NULL,
              provider_used TEXT NOT NULL,
              value REAL,
              unit TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (run_id, ticker, field_name, provider_used)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_openbb_ticker ON raw_openbb(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_openbb_field ON raw_openbb(field_name)",
            "CREATE INDEX IF NOT EXISTS idx_openbb_provider ON raw_openbb(provider_used)",
            "CREATE INDEX IF NOT EXISTS idx_openbb_run ON raw_openbb(run_id)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v9_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 9")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (9, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def migrate_to_v10(self) -> None:
        """Idempotent migration v9 -> v10 per A.3.7.5 plan.

        Adds three orchestrator-substrate tables:

          * provider_call_log: append-only per-HTTP-call audit. PK is
            ``call_id INTEGER PRIMARY KEY AUTOINCREMENT``. Indexed on
            ``(source, started_at)`` for quota-window queries and on
            ``status`` for failure-rate diagnostics.

          * provider_quota_state: sliding-window quota tracker. PK is
            ``(source, provider, window_start)``. Window-aging is enforced
            by query predicate, NOT row deletion; a future
            ``cleanup_stale_quota_rows`` (Wave 2) prunes >7d-old rows.

          * fetch_queue: pending-work queue. PK is
            ``queue_id INTEGER PRIMARY KEY AUTOINCREMENT``. Status
            transitions: ``pending -> dispatched -> done | error``.

        Also enables WAL mode so the orchestrator's per-call short
        transactions don't block concurrent readers (notebooks, dashboards).

        Safe to call multiple times. Existing data preserved.
        """
        self.migrate_to_v9()

        v10_ddl = [
            """
            CREATE TABLE IF NOT EXISTS provider_call_log (
              call_id INTEGER PRIMARY KEY AUTOINCREMENT,
              source TEXT NOT NULL,
              ticker TEXT,
              field TEXT NOT NULL,
              started_at TEXT NOT NULL,
              finished_at TEXT NOT NULL,
              status TEXT NOT NULL,
              bytes_returned INTEGER NOT NULL DEFAULT 0,
              error_message TEXT,
              http_status INTEGER
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_call_source_started "
            "ON provider_call_log(source, started_at)",
            "CREATE INDEX IF NOT EXISTS idx_call_status "
            "ON provider_call_log(status)",
            """
            CREATE TABLE IF NOT EXISTS provider_quota_state (
              source TEXT NOT NULL,
              provider TEXT NOT NULL,
              window_start TEXT NOT NULL,
              calls_used INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (source, provider, window_start)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_quota_provider_window "
            "ON provider_quota_state(provider, window_start)",
            """
            CREATE TABLE IF NOT EXISTS fetch_queue (
              queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
              source TEXT NOT NULL,
              ticker TEXT,
              field TEXT NOT NULL,
              priority_score REAL NOT NULL DEFAULT 0.0,
              created_at TEXT NOT NULL,
              dispatched_at TEXT,
              completed_at TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              last_error TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_queue_status_priority "
            "ON fetch_queue(status, priority_score DESC)",
            "CREATE INDEX IF NOT EXISTS idx_queue_dedup "
            "ON fetch_queue(source, field, ticker, status)",
        ]

        with self.get_connection() as conn:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                # In-memory or other backends where WAL is unsupported;
                # migration still succeeds, just without WAL.
                pass
            cur = conn.cursor()
            for ddl in v10_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 10")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (10, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def migrate_to_v11(self) -> None:
        """Idempotent migration v10 -> v11 per A.3.8.

        Adds two source-native tables for the news-activity sub-signals:

          * raw_gdelt: one row per (gkg_record_id, ticker) GDELT news
            mention. Indexed on ticker + mention_timestamp for the
            trailing-30d / trailing-365d window queries that
            news_volume_anomaly.py performs at composite time.

          * raw_pytrends: one row per (term, observation_date, geo) FEARS
            search-volume observation. Indexed on term + observation_date.

        Both use INSERT OR IGNORE on insert. First write wins on
        re-fetches; revisions are silently dropped (matches A.3.5 FRED
        convention — neither GDELT nor pytrends publishes revisions).

        Safe to call multiple times. Existing v10 data preserved.
        """
        self.migrate_to_v10()

        v11_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_gdelt (
              run_id TEXT NOT NULL,
              gkg_record_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              mention_timestamp TEXT NOT NULL,
              match_method TEXT NOT NULL CHECK (match_method IN ('cashtag','alias')),
              source_url TEXT,
              gkg_tone_json TEXT,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (gkg_record_id, ticker)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_gdelt_ticker "
            "ON raw_gdelt(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_gdelt_mention_ts "
            "ON raw_gdelt(mention_timestamp)",
            """
            CREATE TABLE IF NOT EXISTS raw_pytrends (
              run_id TEXT NOT NULL,
              term TEXT NOT NULL,
              observation_date TEXT NOT NULL,
              svi REAL NOT NULL CHECK (svi >= 0),
              geo TEXT NOT NULL DEFAULT 'US',
              source_filename TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (term, observation_date, geo)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_pytrends_term "
            "ON raw_pytrends(term)",
            "CREATE INDEX IF NOT EXISTS idx_raw_pytrends_date "
            "ON raw_pytrends(observation_date)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v11_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 11")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (11, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def migrate_to_v12(self) -> None:
        """Idempotent migration v11 -> v12 per A.3.9.

        Adds raw_edgar_filing_tone -- one row per (cik, source_filing_accn,
        lm_dictionary_version) Loughran-McDonald tone score. PK includes
        the dictionary version so future LM releases insert alongside
        rather than overwrite, preserving audit history.

        Sign convention: positive net_tone == more LM-positive words than
        LM-negative words per total tokens. The compute_lm_tone_shift
        z-score over a ticker's prior 4 filings turns levels into a
        cross-sectional signal.

        Safe to call multiple times. Existing v11 data preserved.
        """
        self.migrate_to_v11()

        v12_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_edgar_filing_tone (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              cik TEXT NOT NULL,
              source_filing_accn TEXT NOT NULL,
              form_type TEXT NOT NULL,
              filing_date TEXT NOT NULL,
              n_positive INTEGER NOT NULL CHECK (n_positive >= 0),
              n_negative INTEGER NOT NULL CHECK (n_negative >= 0),
              n_uncertainty INTEGER NOT NULL CHECK (n_uncertainty >= 0),
              n_litigious INTEGER NOT NULL CHECK (n_litigious >= 0),
              total_words INTEGER NOT NULL CHECK (total_words >= 0),
              net_tone REAL NOT NULL,
              extraction_status TEXT NOT NULL CHECK (
                extraction_status IN (
                  'item_1a_found',
                  'full_document_fallback',
                  'empty_document'
                )
              ),
              lm_dictionary_version TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (cik, source_filing_accn, lm_dictionary_version)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_filing_tone_ticker "
            "ON raw_edgar_filing_tone(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_filing_tone_filing_date "
            "ON raw_edgar_filing_tone(filing_date)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v12_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 12")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (12, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def migrate_to_v13(self) -> None:
        """Idempotent migration v12 -> v13 per A.3.9 Reconciliation R2.

        Rebuilds raw_edgar_filing_tone to:
          1. Rename extraction_status value 'item_1a_found' to
             'item_1a_extracted' (matches the strict A.3.9 spec).
          2. Allow new extraction_status values 'dictionary_missing'
             and 'fetch_failed' so failures persist as audit rows.
          3. Relax NOT NULL on count + net_tone columns so the new
             failure-status rows can carry NULL for fields that
             weren't computed.

        SQLite cannot ALTER a CHECK constraint in place, so the
        migration rebuilds via the standard create-new + INSERT-SELECT
        + DROP-old + RENAME dance. Existing rows under
        extraction_status='item_1a_found' are remapped to
        'item_1a_extracted'; indexes are recreated.

        Safe to call multiple times. Existing v12 data preserved.
        """
        self.migrate_to_v12()

        # Idempotent guard: if v13 already applied, skip the rebuild.
        with self.get_connection() as conn:
            already = conn.execute(
                "SELECT 1 FROM schema_version WHERE version = 13"
            ).fetchone()
        if already:
            return

        rebuild_sql = [
            """
            CREATE TABLE IF NOT EXISTS raw_edgar_filing_tone_new (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              cik TEXT NOT NULL,
              source_filing_accn TEXT NOT NULL,
              form_type TEXT NOT NULL,
              filing_date TEXT NOT NULL,
              n_positive INTEGER CHECK (n_positive IS NULL OR n_positive >= 0),
              n_negative INTEGER CHECK (n_negative IS NULL OR n_negative >= 0),
              n_uncertainty INTEGER CHECK (n_uncertainty IS NULL OR n_uncertainty >= 0),
              n_litigious INTEGER CHECK (n_litigious IS NULL OR n_litigious >= 0),
              total_words INTEGER CHECK (total_words IS NULL OR total_words >= 0),
              net_tone REAL,
              extraction_status TEXT NOT NULL CHECK (
                extraction_status IN (
                  'item_1a_extracted',
                  'full_document_fallback',
                  'empty_document',
                  'dictionary_missing',
                  'fetch_failed'
                )
              ),
              lm_dictionary_version TEXT NOT NULL,
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (cik, source_filing_accn, lm_dictionary_version)
            )
            """,
            # Copy + remap legacy 'item_1a_found' -> 'item_1a_extracted'.
            """
            INSERT INTO raw_edgar_filing_tone_new
            SELECT
              run_id, ticker, cik, source_filing_accn, form_type,
              filing_date,
              n_positive, n_negative, n_uncertainty, n_litigious,
              total_words, net_tone,
              CASE
                WHEN extraction_status = 'item_1a_found'
                  THEN 'item_1a_extracted'
                ELSE extraction_status
              END,
              lm_dictionary_version, scrape_timestamp
            FROM raw_edgar_filing_tone
            """,
            "DROP TABLE raw_edgar_filing_tone",
            "ALTER TABLE raw_edgar_filing_tone_new RENAME TO raw_edgar_filing_tone",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_filing_tone_ticker "
            "ON raw_edgar_filing_tone(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_edgar_filing_tone_filing_date "
            "ON raw_edgar_filing_tone(filing_date)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for sql in rebuild_sql:
                cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (13, datetime.datetime.utcnow().isoformat()),
            )
            conn.commit()

    def migrate_to_v14(self) -> None:
        """Idempotent migration v13 -> v14 per Phase B.1.

        Adds two catalyst tables:
          * raw_catalysts -- per-fetch audit trail. PK
            (source, ticker, catalyst_type, catalyst_date,
            scrape_timestamp) so multiple fetches over time are
            preserved. INSERT OR IGNORE on the natural key.
          * catalyst_calendar -- per-run curated view. PK
            (run_id, ticker, catalyst_type, catalyst_date) so each
            run snapshots the catalysts current at that point in time.

        catalyst_type constrained to the six known values from
        CatalystRow._CATALYST_TYPE. confidence in {'high','med','low'}.

        Safe to call multiple times. v13 data preserved.
        """
        self.migrate_to_v13()

        v14_ddl = [
            """
            CREATE TABLE IF NOT EXISTS raw_catalysts (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              catalyst_type TEXT NOT NULL CHECK (catalyst_type IN (
                'earnings','ex_dividend','fda_pdufa','fomc',
                'lockup_expiry','index_inclusion'
              )),
              catalyst_date TEXT NOT NULL,
              catalyst_description TEXT,
              source TEXT NOT NULL,
              source_url TEXT,
              confidence TEXT NOT NULL CHECK (
                confidence IN ('high','med','low')
              ),
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (source, ticker, catalyst_type, catalyst_date, scrape_timestamp)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_raw_catalysts_ticker "
            "ON raw_catalysts(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_raw_catalysts_date "
            "ON raw_catalysts(catalyst_date)",
            """
            CREATE TABLE IF NOT EXISTS catalyst_calendar (
              run_id TEXT NOT NULL,
              ticker TEXT NOT NULL,
              catalyst_type TEXT NOT NULL CHECK (catalyst_type IN (
                'earnings','ex_dividend','fda_pdufa','fomc',
                'lockup_expiry','index_inclusion'
              )),
              catalyst_date TEXT NOT NULL,
              catalyst_description TEXT,
              source TEXT NOT NULL,
              source_url TEXT,
              confidence TEXT NOT NULL CHECK (
                confidence IN ('high','med','low')
              ),
              scrape_timestamp TEXT NOT NULL,
              PRIMARY KEY (run_id, ticker, catalyst_type, catalyst_date)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_catalyst_calendar_ticker "
            "ON catalyst_calendar(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_catalyst_calendar_date "
            "ON catalyst_calendar(catalyst_date)",
        ]

        with self.get_connection() as conn:
            cur = conn.cursor()
            for ddl in v14_ddl:
                cur.execute(ddl)
            cur.execute("SELECT version FROM schema_version WHERE version = 14")
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (14, datetime.datetime.utcnow().isoformat()),
                )
            conn.commit()

    def insert_raw_catalysts(self, rows: list) -> None:
        """Insert CatalystRow records into raw_catalysts (audit table).

        INSERT OR IGNORE on the natural PK. Empty input is a no-op.
        """
        if not rows:
            return
        from src.common.schemas import CatalystRow
        records = [
            r.model_dump() if isinstance(r, CatalystRow) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_catalysts ({col_list}) "
                f"VALUES ({placeholders})",
                values,
            )
            conn.commit()

    def insert_catalyst_calendar(self, rows: list) -> None:
        """Insert CatalystRow records into catalyst_calendar (curated view).

        INSERT OR IGNORE on (run_id, ticker, catalyst_type, catalyst_date)
        so duplicate fetches within the same run dedupe naturally.
        """
        if not rows:
            return
        from src.common.schemas import CatalystRow
        records = [
            r.model_dump() if isinstance(r, CatalystRow) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO catalyst_calendar ({col_list}) "
                f"VALUES ({placeholders})",
                values,
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Phase A.3.7.5: Rate-Limited Fetch Orchestrator helpers
    # ------------------------------------------------------------------

    def insert_provider_call_log(self, row) -> int:
        """Insert one ProviderCallLog row. Returns the autoincremented call_id.

        Append-only audit; never updates existing rows. Caller is expected
        to pass a fully-populated `ProviderCallLog` (started_at, finished_at,
        status, bytes_returned mandatory; ticker/error_message/http_status
        optional).
        """
        with self.get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO provider_call_log
                  (source, ticker, field, started_at, finished_at,
                   status, bytes_returned, error_message, http_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.source, row.ticker, row.field,
                    row.started_at, row.finished_at,
                    row.status, row.bytes_returned,
                    row.error_message, row.http_status,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def upsert_provider_quota_state(self, state) -> None:
        """UPSERT one ProviderQuotaState row.

        Conflict on PK (source, provider, window_start) REPLACES calls_used
        with the new value (NOT increment). The orchestrator's tick is the
        only writer; it computes the new total in memory and writes
        authoritatively. Increment semantics would race under multi-tick
        retries if a previous write succeeded but the response was dropped.
        """
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO provider_quota_state
                  (source, provider, window_start, calls_used)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, provider, window_start)
                DO UPDATE SET calls_used = excluded.calls_used
                """,
                (state.source, state.provider, state.window_start, state.calls_used),
            )
            conn.commit()

    def enqueue_fetch(self, entry) -> int:
        """INSERT OR IGNORE-style enqueue on (source, field, ticker) WHERE
        status='pending'. Returns the new queue_id, or the existing
        queue_id if an identical pending row is already present.

        Dedup is on (source, field, ticker) so re-enqueueing a still-pending
        job is a no-op (preserves the original priority_score + created_at).
        Once the row reaches a terminal status (done/error) a fresh enqueue
        with the same key creates a NEW row -- the queue is the per-attempt
        log, not a per-key snapshot.
        """
        with self.get_connection() as conn:
            existing = conn.execute(
                "SELECT queue_id FROM fetch_queue "
                "WHERE source=? AND field=? AND ticker IS ? AND status='pending'",
                (entry.source, entry.field, entry.ticker),
            ).fetchone()
            if existing is not None:
                return int(existing[0])
            cur = conn.execute(
                """
                INSERT INTO fetch_queue
                  (source, ticker, field, priority_score, created_at,
                   dispatched_at, completed_at, status, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.source, entry.ticker, entry.field,
                    entry.priority_score, entry.created_at,
                    entry.dispatched_at, entry.completed_at,
                    entry.status, entry.last_error,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def dequeue_next(self, limit: int) -> list[dict]:
        """Atomically claim up to `limit` highest-priority pending entries.

        Uses ``BEGIN IMMEDIATE`` so concurrent dequeue calls don't double-
        claim the same row. Selects pending rows ordered by
        ``priority_score DESC, queue_id ASC`` (tiebreaker: oldest first),
        then UPDATEs all selected rows to status='dispatched' with
        dispatched_at=now in a single statement. Returns the claimed rows
        as a list of dicts with keys matching FetchQueueEntry fields.

        Returns an empty list if no pending rows.
        """
        now_iso = datetime.datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """
                    SELECT queue_id, source, ticker, field, priority_score,
                           created_at, dispatched_at, completed_at,
                           status, last_error
                    FROM fetch_queue
                    WHERE status='pending'
                    ORDER BY priority_score DESC, queue_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = [dict(r) for r in cur.fetchall()]
                if not rows:
                    conn.commit()
                    return []
                ids = [r["queue_id"] for r in rows]
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE fetch_queue SET status='dispatched', dispatched_at=? "
                    f"WHERE queue_id IN ({placeholders})",
                    (now_iso, *ids),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        # Reflect the claim in the returned dicts so callers don't see stale
        # pending status on rows they've already claimed.
        for r in rows:
            r["status"] = "dispatched"
            r["dispatched_at"] = now_iso
        return rows

    def mark_fetch_done(
        self, queue_id: int, success: bool, error: str | None
    ) -> None:
        """Mark a dispatched fetch as terminal.

        ``success=True`` -> status='done', last_error=NULL.
        ``success=False`` -> status='error', last_error=<error>.
        Sets ``completed_at`` to now in either case.
        """
        now_iso = datetime.datetime.utcnow().isoformat()
        new_status = "done" if success else "error"
        new_error = None if success else error
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE fetch_queue SET status=?, completed_at=?, last_error=? "
                "WHERE queue_id=?",
                (new_status, now_iso, new_error, queue_id),
            )
            conn.commit()

    def reclaim_orphaned_dispatched(self, stale_seconds: int = 300) -> int:
        """Reset dispatched rows whose dispatched_at is older than
        ``stale_seconds`` back to pending (dispatched_at cleared). Returns
        the count of rows reclaimed.

        This is the crash-safety mechanism: if a previous tick crashed
        mid-dispatch, the row sat in 'dispatched' indefinitely. Five
        minutes is the v1 default; runs longer than 5 minutes per call
        should explicitly raise the threshold.
        """
        cutoff = (
            datetime.datetime.utcnow() - datetime.timedelta(seconds=stale_seconds)
        ).isoformat()
        with self.get_connection() as conn:
            cur = conn.execute(
                "UPDATE fetch_queue SET status='pending', dispatched_at=NULL "
                "WHERE status='dispatched' AND dispatched_at < ?",
                (cutoff,),
            )
            conn.commit()
            return int(cur.rowcount)

    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def save_universe_data(self, df: pd.DataFrame, table_name: str = "finviz_universe_history"):
        """
        Saves a DataFrame to the SQLite database.
        Appends the current timestamp to create a historical log.
        """
        if df.empty:
            return

        # Create a copy so we don't mutate the original dataframe
        df_save = df.copy()

        # Add a scrape timestamp for historical tracking
        scrape_time = datetime.datetime.now().isoformat()
        df_save['scrape_timestamp'] = scrape_time

        # We append to keep historical records (additive, not repetitive replacement)
        with self.get_connection() as conn:
            df_save.to_sql(table_name, conn, if_exists="append", index=False)

        print(f"Saved {len(df_save)} records to {table_name} table in {self.db_path} at {scrape_time}")

    def load_latest_universe(self, table_name: str = "finviz_universe_history") -> pd.DataFrame:
        """
        Loads the most recent scrape for all tickers.
        """
        query = f"""
            SELECT * FROM {table_name}
            WHERE scrape_timestamp = (SELECT MAX(scrape_timestamp) FROM {table_name})
        """
        with self.get_connection() as conn:
            return pd.read_sql(query, conn)

    # ------------------------------------------------------------------
    # Phase A: run-scoped CRUD
    # ------------------------------------------------------------------

    _UNIVERSE_COLUMNS = [
        "run_id", "ticker", "company_name", "sector", "industry",
        "market_cap_usd", "price", "avg_daily_volume", "pe_ratio", "forward_pe",
        "operating_margin", "net_profit_margin", "perf_1y", "dist_52w_high",
        "dist_52w_low", "rsi_14",
    ]

    _CANDIDATE_COLUMNS = [
        "run_id", "ticker", "playbook", "value_score", "quality_score",
        "momentum_score", "lowvol_score", "revisions_score", "composite_score",
        "eligible", "reasoning",
    ]

    def create_run(self, manifest: RunManifest) -> str:
        """Insert a row into runs and return run_id."""
        ts = manifest.run_timestamp
        if isinstance(ts, datetime.datetime):
            ts_str = ts.isoformat()
        else:
            ts_str = str(ts)

        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO runs (run_id, run_timestamp, run_type, source_label, status, config_snapshot_json, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.run_id,
                    ts_str,
                    manifest.run_type,
                    manifest.source_label,
                    manifest.status,
                    json.dumps(manifest.config_snapshot),
                    manifest.notes,
                ),
            )
            conn.commit()
        print(f"Created run {manifest.run_id} ({manifest.run_type}) in {self.db_path}")
        return manifest.run_id

    def save_universe(self, run_id: str, df: pd.DataFrame) -> None:
        """Append universe rows for a run into universe_members."""
        if df is None or df.empty:
            return
        df_save = df.copy()
        if "run_id" not in df_save.columns:
            df_save["run_id"] = run_id
        keep = [c for c in self._UNIVERSE_COLUMNS if c in df_save.columns]
        df_save = df_save[keep]
        with self.get_connection() as conn:
            df_save.to_sql("universe_members", conn, if_exists="append", index=False)
        print(f"Saved {len(df_save)} universe rows for run {run_id}")

    def save_layer1_outputs(
        self,
        run_id: str,
        canonical_df: pd.DataFrame | None = None,
        v1_universe_df: pd.DataFrame | None = None,
        v1_candidates_by_playbook: dict[str, pd.DataFrame] | None = None,
    ) -> dict[str, int]:
        """A.3.10 dual-write helper per spec section 10.

        Writes a single Layer 1 run's outputs to BOTH the v1
        (universe_members + candidate_results) and v2
        (canonical_universe) tables. v1 stays as the single source of
        truth in the notebook driver until A.5 equivalence-verifies;
        v2 accumulates alongside for the drift report.

        Parameters
        ----------
        run_id
            Run identifier; stamped on every row.
        canonical_df
            DataFrame whose rows can be coerced into CanonicalUniverseRow.
            Use the v2 column naming (pe_ttm, perf_12m, dist_52w_high,
            etc.). Pass None to skip the v2 write.
        v1_universe_df
            Legacy-shaped DataFrame for universe_members. Pass None to
            skip the v1 universe write.
        v1_candidates_by_playbook
            {'playbook_a': df, 'playbook_b': df}. Pass None or {} to
            skip the v1 candidates write.

        Returns
        -------
        dict[str, int]
            Counts inserted: {'v1_universe', 'v2_canonical',
            'v1_candidates_a', 'v1_candidates_b'}.
        """
        counts = {
            "v1_universe": 0,
            "v2_canonical": 0,
            "v1_candidates_a": 0,
            "v1_candidates_b": 0,
        }

        if v1_universe_df is not None and len(v1_universe_df) > 0:
            self.save_universe(run_id, v1_universe_df)
            counts["v1_universe"] = len(v1_universe_df)

        if canonical_df is not None and len(canonical_df) > 0:
            from src.common.schemas import CanonicalUniverseRow
            rows: list = []
            for _, row in canonical_df.iterrows():
                payload = {k: (None if pd.isna(v) else v) for k, v in row.items()}
                payload["run_id"] = run_id
                # Pydantic ignores unknown fields by default? No -- model_validate
                # strict-rejects extras. Pull only the canonical model's fields.
                allowed = set(CanonicalUniverseRow.model_fields.keys())
                payload = {k: v for k, v in payload.items() if k in allowed}
                try:
                    rows.append(CanonicalUniverseRow(**payload))
                except Exception:
                    continue
            if rows:
                self.insert_canonical_universe(rows)
                counts["v2_canonical"] = len(rows)

        if v1_candidates_by_playbook:
            for playbook, df in v1_candidates_by_playbook.items():
                if df is None or len(df) == 0:
                    continue
                self.save_candidates(run_id, playbook, df)
                key = (
                    "v1_candidates_a" if "a" in playbook.lower()
                    else "v1_candidates_b"
                )
                counts[key] = len(df)

        return counts

    def save_candidates(self, run_id: str, playbook: str, df: pd.DataFrame) -> None:
        """Append candidate scoring rows for a (run_id, playbook)."""
        if df is None or df.empty:
            return
        df_save = df.copy()
        if "run_id" not in df_save.columns:
            df_save["run_id"] = run_id
        if "playbook" not in df_save.columns:
            df_save["playbook"] = playbook
        if "eligible" in df_save.columns:
            df_save["eligible"] = df_save["eligible"].astype(int)
        keep = [c for c in self._CANDIDATE_COLUMNS if c in df_save.columns]
        df_save = df_save[keep]
        with self.get_connection() as conn:
            df_save.to_sql("candidate_results", conn, if_exists="append", index=False)
        print(f"Saved {len(df_save)} candidate rows for run {run_id} playbook {playbook}")

    def save_filter_waterfall(self, run_id: str, steps: list[dict]) -> None:
        """Insert filter waterfall steps for a run."""
        if not steps:
            return
        rows = []
        for step in steps:
            rows.append(
                (
                    run_id,
                    step["playbook"],
                    int(step["step_order"]),
                    step["step_name"],
                    int(step["rows_before"]),
                    int(step["rows_after"]),
                    int(step["rows_dropped"]),
                )
            )
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.executemany(
                """
                INSERT INTO filter_waterfall
                  (run_id, playbook, step_order, step_name, rows_before, rows_after, rows_dropped)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        print(f"Saved {len(rows)} waterfall steps for run {run_id}")

    def save_data_quality(self, run_id: str, metrics: dict) -> None:
        """Insert data quality metrics dict for a run."""
        if not metrics:
            return
        rows = []
        for name, value in metrics.items():
            try:
                val = float(value) if value is not None else None
            except (TypeError, ValueError):
                val = None
            rows.append((run_id, str(name), val))
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.executemany(
                """
                INSERT INTO data_quality_metrics (run_id, metric_name, metric_value)
                VALUES (?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        print(f"Saved {len(rows)} data quality metrics for run {run_id}")

    def load_run(self, run_id: str) -> dict:
        """Load a complete run bundle by run_id."""
        with self.get_connection() as conn:
            manifest_df = pd.read_sql(
                "SELECT * FROM runs WHERE run_id = ?", conn, params=(run_id,)
            )
            manifest_row = manifest_df.iloc[0].to_dict() if not manifest_df.empty else None
            universe = pd.read_sql(
                "SELECT * FROM universe_members WHERE run_id = ?", conn, params=(run_id,)
            )
            candidates = pd.read_sql(
                "SELECT * FROM candidate_results WHERE run_id = ?", conn, params=(run_id,)
            )
            waterfall = pd.read_sql(
                "SELECT * FROM filter_waterfall WHERE run_id = ? ORDER BY playbook, step_order",
                conn,
                params=(run_id,),
            )
            quality = pd.read_sql(
                "SELECT * FROM data_quality_metrics WHERE run_id = ?", conn, params=(run_id,)
            )
        return {
            "manifest": manifest_row,
            "universe": universe,
            "candidates": candidates,
            "waterfall": waterfall,
            "quality": quality,
        }

    def load_latest_run(self, run_type: str = "layer1") -> dict:
        """Load the most recent run whose run_type starts with the given prefix."""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT run_id FROM runs
                WHERE run_type LIKE ?
                ORDER BY run_timestamp DESC
                LIMIT 1
                """,
                (run_type + "%",),
            )
            row = cur.fetchone()
        if row is None:
            return {
                "manifest": None,
                "universe": pd.DataFrame(),
                "candidates": pd.DataFrame(),
                "waterfall": pd.DataFrame(),
                "quality": pd.DataFrame(),
            }
        return self.load_run(row[0])

    # ------------------------------------------------------------------
    # Phase A.2: v2 insert helpers (typed)
    # ------------------------------------------------------------------

    def insert_canonical_universe(self, rows: list) -> None:
        """Insert validated CanonicalUniverseRow records into canonical_universe."""
        if not rows:
            return
        from src.common.schemas import CanonicalUniverseRow  # local to avoid cycle
        records = [r.model_dump() if isinstance(r, CanonicalUniverseRow) else dict(r) for r in rows]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT INTO canonical_universe ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()

    def insert_field_provenance(self, rows: list) -> None:
        """Insert FieldProvenanceRow records into field_provenance (append-only)."""
        if not rows:
            return
        values = [
            (
                r.run_id, r.ticker, r.field, r.source,
                r.raw_value, r.parsed_value, r.weight,
                1 if r.contributed_to_canonical else 0,
                r.disagreement_pct, r.fetched_at,
            )
            for r in rows
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO field_provenance
                  (run_id, ticker, field, source, raw_value, parsed_value,
                   weight, contributed_to_canonical, disagreement_pct, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()

    def insert_source_run_log(self, rows: list) -> None:
        """Insert SourceRunLogRow records into source_run_log."""
        if not rows:
            return
        values = [
            (
                r.run_id, r.source, r.started_at, r.finished_at,
                r.status, r.rows_fetched, r.error_message, r.error_traceback,
            )
            for r in rows
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO source_run_log
                  (run_id, source, started_at, finished_at, status,
                   rows_fetched, error_message, error_traceback)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()

    def insert_historical_price(self, rows: list) -> None:
        """Insert HistoricalPriceRow records into historical_price.

        Uses INSERT OR IGNORE per Principle 6 (persistent accumulation): once a
        (ticker, observation_date) row is stored it is IMMUTABLE. Re-inserts of
        the same primary key are silently dropped. To overwrite, the operator
        must explicitly call force_refetch() first.

        Changed from INSERT OR REPLACE in A.3.1.
        """
        if not rows:
            return
        values = [
            (
                r.ticker, r.observation_date,
                r.open, r.high, r.low, r.close,
                r.volume, r.adj_close,
                r.source, r.scrape_timestamp,
            )
            for r in rows
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO historical_price
                  (ticker, observation_date, open, high, low, close,
                   volume, adj_close, source, scrape_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Phase A.3.1: fetch_watermarks CRUD
    # ------------------------------------------------------------------

    def get_watermark(self, source: str, ticker: str, field: str) -> dict | None:
        """Return watermark dict for (source, ticker, field) or None if absent."""
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT source, ticker, field, last_fetched_at, last_observation_date,
                       fetch_count, error_count, last_error_message
                FROM fetch_watermarks
                WHERE source = ? AND ticker = ? AND field = ?
                """,
                (source, ticker, field),
            ).fetchone()
        if row is None:
            return None
        return {
            "source": row[0],
            "ticker": row[1],
            "field": row[2],
            "last_fetched_at": row[3],
            "last_observation_date": row[4],
            "fetch_count": row[5],
            "error_count": row[6],
            "last_error_message": row[7],
        }

    def upsert_watermark(
        self,
        source: str,
        ticker: str,
        field: str,
        last_observation_date: str | None,
        success: bool,
        error_message: str | None = None,
    ) -> None:
        """Insert or update a watermark row.

        On success: fetch_count += 1; last_observation_date updates IF caller
        passes a non-null value.
        On failure: error_count += 1; last_error_message updates;
        last_observation_date is NOT advanced.
        last_fetched_at always set to current UTC time.
        """
        now_iso = datetime.datetime.utcnow().isoformat()
        existing = self.get_watermark(source, ticker, field)
        if existing is None:
            with self.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO fetch_watermarks
                      (source, ticker, field, last_fetched_at, last_observation_date,
                       fetch_count, error_count, last_error_message)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source, ticker, field, now_iso,
                        last_observation_date if success else None,
                        1 if success else 0,
                        0 if success else 1,
                        None if success else error_message,
                    ),
                )
                conn.commit()
            return

        new_fetch_count = existing["fetch_count"] + (1 if success else 0)
        new_error_count = existing["error_count"] + (0 if success else 1)
        new_obs_date = (
            last_observation_date if (success and last_observation_date is not None)
            else existing["last_observation_date"]
        )
        new_err_msg = None if success else (error_message or existing["last_error_message"])
        with self.get_connection() as conn:
            conn.execute(
                """
                UPDATE fetch_watermarks
                SET last_fetched_at = ?, last_observation_date = ?,
                    fetch_count = ?, error_count = ?, last_error_message = ?
                WHERE source = ? AND ticker = ? AND field = ?
                """,
                (now_iso, new_obs_date, new_fetch_count, new_error_count, new_err_msg,
                 source, ticker, field),
            )
            conn.commit()

    def force_refetch(
        self,
        source: str,
        ticker: str,
        field: str,
        from_date: str,
    ) -> int:
        """Operator-only: delete time-series rows from from_date onward and
        reset the watermark to from_date - 1.

        Per A.3 spec section 5.4, the ONLY supported way to overwrite historical
        observations once stored. Never called automatically. Logged in
        source_run_log with status='force_refetch'.

        A.3.1 supports field='historical_price' only. Other time-series tables
        get their support added in the sub-phase that populates them.

        Returns the number of rows deleted.
        """
        import datetime as _dt
        if field != "historical_price":
            raise NotImplementedError(
                f"force_refetch for field {field!r} not yet implemented; "
                f"only historical_price is supported in A.3.1"
            )
        with self.get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM historical_price WHERE ticker = ? AND observation_date >= ?",
                (ticker, from_date),
            )
            deleted = cur.rowcount

            try:
                prior_date = (_dt.date.fromisoformat(from_date) - _dt.timedelta(days=1)).isoformat()
            except ValueError:
                prior_date = None
            existing = conn.execute(
                "SELECT 1 FROM fetch_watermarks WHERE source=? AND ticker=? AND field=?",
                (source, ticker, field),
            ).fetchone()
            now_iso = _dt.datetime.utcnow().isoformat()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO fetch_watermarks
                      (source, ticker, field, last_fetched_at, last_observation_date,
                       fetch_count, error_count, last_error_message)
                    VALUES (?, ?, ?, ?, ?, 0, 0, NULL)
                    """,
                    (source, ticker, field, now_iso, prior_date),
                )
            else:
                conn.execute(
                    """
                    UPDATE fetch_watermarks
                    SET last_observation_date = ?, last_fetched_at = ?
                    WHERE source = ? AND ticker = ? AND field = ?
                    """,
                    (prior_date, now_iso, source, ticker, field),
                )

            conn.execute(
                """
                INSERT INTO source_run_log
                  (run_id, source, started_at, finished_at, status,
                   rows_fetched, error_message, error_traceback)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"force_refetch-{now_iso}",
                    source,
                    now_iso,
                    now_iso,
                    "force_refetch",
                    deleted,
                    f"operator-forced refetch of ({ticker}, {field}) from {from_date}",
                    None,
                ),
            )
            conn.commit()
        return deleted

    # ------------------------------------------------------------------
    # Phase A.3.2: raw_yahoo insert helper
    # ------------------------------------------------------------------

    def insert_raw_yahoo(self, rows: list) -> None:
        """Insert RawYahooRow records into raw_yahoo.

        Uses plain INSERT (no IGNORE/REPLACE) because raw_yahoo is per-run and
        (run_id, ticker) is unique by construction. A duplicate would be a bug —
        let the integrity error surface.
        """
        if not rows:
            return
        from src.common.schemas import RawYahooRow
        records = [r.model_dump() if isinstance(r, RawYahooRow) else dict(r) for r in rows]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT INTO raw_yahoo ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Phase A.3.3: raw_edgar_fundamentals + ticker->CIK map helpers
    # ------------------------------------------------------------------

    def insert_raw_edgar_fundamentals(self, rows: list) -> None:
        """Insert RawEdgarFundamentalsRow records into raw_edgar_fundamentals.

        Uses INSERT OR IGNORE — once a (run_id, ticker, fiscal_period, fiscal_year)
        is recorded, subsequent inserts with the same PK are silently dropped.
        Restated financials from a later filing therefore do NOT overwrite the
        original first-observed value. Use force_refetch() for deliberate
        operator-initiated reset (Principle 5).
        """
        if not rows:
            return
        from src.common.schemas import RawEdgarFundamentalsRow
        records = [
            r.model_dump() if isinstance(r, RawEdgarFundamentalsRow) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_edgar_fundamentals ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()

    def upsert_sec_ticker_cik_map(self, entries: list) -> None:
        """Bulk-insert ticker->CIK rows; UPDATE company_name on conflict.

        entries is a list of dicts with keys: ticker, cik, company_name.
        """
        if not entries:
            return
        now_iso = datetime.datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO sec_ticker_cik_map (ticker, cik, company_name, snapshot_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                  cik = excluded.cik,
                  company_name = excluded.company_name,
                  snapshot_at = excluded.snapshot_at
                """,
                [
                    (
                        e["ticker"].upper().strip(),
                        str(e["cik"]).zfill(10),
                        e.get("company_name"),
                        now_iso,
                    )
                    for e in entries
                ],
            )
            conn.commit()

    def get_cik_for_ticker(self, ticker: str) -> str | None:
        """Return 10-digit padded CIK for ticker, or None if not in the map."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT cik FROM sec_ticker_cik_map WHERE ticker = ?",
                (ticker.upper().strip(),),
            ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Phase A.3.4: raw_edgar_insider + raw_edgar_filings helpers
    # ------------------------------------------------------------------

    def insert_raw_edgar_insider(self, rows: list) -> None:
        """Insert RawEdgarInsiderRow records into raw_edgar_insider.

        Uses INSERT OR IGNORE — once a
        (cik, source_filing_accn, filer_name, transaction_date, transaction_code)
        is recorded, subsequent inserts with the same PK are silently
        dropped. First write wins. This protects against amendment storms
        and re-runs duplicating line items (Principle 5).
        """
        if not rows:
            return
        from src.common.schemas import RawEdgarInsiderRow
        records = [
            r.model_dump() if isinstance(r, RawEdgarInsiderRow) else dict(r)
            for r in rows
        ]
        # Boolean -> int for SQLite
        for rec in records:
            for k in ("filer_is_officer", "filer_is_director",
                      "filer_is_10pct_owner", "is_opportunistic", "is_derivative"):
                if rec.get(k) is not None:
                    rec[k] = int(bool(rec[k]))
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_edgar_insider ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()

    def insert_raw_edgar_filings(self, rows: list) -> None:
        """Insert RawEdgarFilingRow records into raw_edgar_filings.

        Uses INSERT OR IGNORE on PK (cik, source_filing_accn).
        """
        if not rows:
            return
        from src.common.schemas import RawEdgarFilingRow
        records = [
            r.model_dump() if isinstance(r, RawEdgarFilingRow) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_edgar_filings ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Phase A.3.5: raw_fred + raw_finra helpers
    # ------------------------------------------------------------------

    def insert_raw_fred(self, rows: list) -> None:
        """Insert RawFredObservation records into raw_fred.

        Uses INSERT OR IGNORE — once a (series_id, observation_date) is
        recorded, subsequent inserts with the same PK are silently
        dropped. First write wins. This protects re-runs and routine
        revision storms (FRED revises CPIAUCSL, GDP, etc. periodically)
        from re-writing the original-observation value. ALFRED-vintage
        integration in a later phase will distinguish via realtime_start.
        """
        if not rows:
            return
        from src.common.schemas import RawFredObservation
        records = [
            r.model_dump() if isinstance(r, RawFredObservation) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_fred ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()

    def insert_raw_finra(self, rows: list) -> None:
        """Insert RawFinraShortInterest records into raw_finra.

        Uses INSERT OR IGNORE on PK (ticker, settlement_date, exchange).
        """
        if not rows:
            return
        from src.common.schemas import RawFinraShortInterest
        records = [
            r.model_dump() if isinstance(r, RawFinraShortInterest) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_finra ({col_list}) VALUES ({placeholders})",
                values,
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Phase A.3.6: raw_stockanalysis_ratios helpers
    # ------------------------------------------------------------------

    def insert_raw_stockanalysis_ratios(self, rows: list) -> None:
        """Insert RawStockanalysisRatioRow records into raw_stockanalysis_ratios.

        Uses INSERT OR IGNORE on PK (ticker, metric, period_end_date).
        First write wins — historical ratio buckets are immutable for
        v1 purposes; revisions are silently dropped.
        """
        if not rows:
            return
        from src.common.schemas import RawStockanalysisRatioRow
        records = [
            r.model_dump() if isinstance(r, RawStockanalysisRatioRow) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_stockanalysis_ratios ({col_list}) "
                f"VALUES ({placeholders})",
                values,
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Phase A.3.7: raw_openbb helpers
    # ------------------------------------------------------------------

    def insert_raw_openbb(self, rows: list) -> None:
        """Insert RawOpenBBRow records into raw_openbb.

        Uses INSERT OR IGNORE on PK
        (run_id, ticker, field_name, provider_used). First write under a
        given run_id wins; subsequent re-pulls within the same run_id are
        silently dropped (per Principle 5: no silent overwrite). To
        refresh, start a fresh run_id - every row written under the new
        run_id is treated as a distinct observation, which lets the
        materialization layer reconstruct a per-run snapshot of provider
        state.
        """
        if not rows:
            return
        from src.common.schemas import RawOpenBBRow
        records = [
            r.model_dump() if isinstance(r, RawOpenBBRow) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_openbb ({col_list}) "
                f"VALUES ({placeholders})",
                values,
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Phase A.3.8: raw_gdelt + raw_pytrends helpers
    # ------------------------------------------------------------------

    def insert_raw_gdelt(self, rows: list) -> None:
        """Insert RawGdeltMention records into raw_gdelt.

        Uses INSERT OR IGNORE on PK (gkg_record_id, ticker). First write
        wins on collision — re-pulling the same 15-minute GKG file does
        not duplicate rows. Empty input is a no-op.
        """
        if not rows:
            return
        from src.common.schemas import RawGdeltMention
        records = [
            r.model_dump() if isinstance(r, RawGdeltMention) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_gdelt ({col_list}) "
                f"VALUES ({placeholders})",
                values,
            )
            conn.commit()

    def insert_raw_pytrends(self, rows: list) -> None:
        """Insert RawPytrendsObservation records into raw_pytrends.

        Uses INSERT OR IGNORE on PK (term, observation_date, geo). First
        write wins on collision. Empty input is a no-op.
        """
        if not rows:
            return
        from src.common.schemas import RawPytrendsObservation
        records = [
            r.model_dump() if isinstance(r, RawPytrendsObservation) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_pytrends ({col_list}) "
                f"VALUES ({placeholders})",
                values,
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Phase A.3.9: raw_edgar_filing_tone helper
    # ------------------------------------------------------------------

    def insert_raw_edgar_filing_tone(self, rows: list) -> None:
        """Insert RawEdgarFilingToneRow records into raw_edgar_filing_tone.

        Uses INSERT OR IGNORE on PK (cik, source_filing_accn,
        lm_dictionary_version). First write wins on collision -- re-runs
        with the same dictionary version are no-ops. Future LM dictionary
        releases insert alongside (different lm_dictionary_version) for
        audit comparison. Empty input is a no-op.
        """
        if not rows:
            return
        from src.common.schemas import RawEdgarFilingToneRow
        records = [
            r.model_dump() if isinstance(r, RawEdgarFilingToneRow) else dict(r)
            for r in rows
        ]
        cols = list(records[0].keys())
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        values = [tuple(rec[c] for c in cols) for rec in records]
        with self.get_connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO raw_edgar_filing_tone ({col_list}) "
                f"VALUES ({placeholders})",
                values,
            )
            conn.commit()
