"""EdgarSource — adapter for SEC EDGAR.

SEC EDGAR requires identifying contact info in every request per
https://www.sec.gov/os/accessing-edgar-data. The User-Agent format is:
"Application Name AdminEmail@example.com".

A.1 deliverable: health_check via the EDGAR submissions endpoint.
A.3.3 deliverable: fetch_fundamentals_for_ticker + fetch_universe pulling
the XBRL companyfacts JSON, parsing to RawEdgarFundamentalsRow, and
persisting to raw_edgar_fundamentals via INSERT OR IGNORE. Watermark per
(edgar, ticker, xbrl_fundamentals) tracks the latest filing date observed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import time

import pandas as pd
import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.common.datasources.base import (
    BaseDataSource,
    SourceHealthStatus,
    SourceStatus,
)


_DEFAULT_USER_AGENT = "Trade Identifier research@example.com"
_PROBE_URL = "https://data.sec.gov/submissions/CIK0000320193.json"  # Apple
_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


class _SecHttpError(RuntimeError):
    """Wrapper for retry-eligible HTTP errors from SEC endpoints."""


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
    retry=retry_if_exception_type(_SecHttpError),
)
def _sec_get_with_retry(url: str, headers: dict) -> requests.Response:
    """GET against an SEC endpoint with tenacity-backed retry on 429/503.

    SEC fair-access policy: <=10 req/sec. Retries with exponential backoff
    (0.5s -> 8s capped). Other HTTP errors (4xx) are not retried — they
    indicate a hard mis-config (e.g., missing User-Agent) and should fail
    fast so we don't waste budget.
    """
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code in (429, 503):
        raise _SecHttpError(f"transient HTTP {resp.status_code} for {url}")
    return resp


class EdgarSource(BaseDataSource):
    name = "edgar"
    cadence = "weekly"
    provides = {
        "cik", "company_name", "exchange",
        "revenue_ttm", "ebit_ttm", "net_income_ttm",
        "total_assets", "total_debt_to_equity",
        "operating_margin", "net_profit_margin",
        "fcf_ttm", "cash_and_equivalents", "total_debt",
        "interest_coverage",
        # A.3.4 additions:
        "insider_transactions", "insider_opportunistic_buys_30d",
        "insider_opportunistic_sells_30d", "filing_count_8k_30d",
        # A.3.9 addition: Loughran-McDonald tone signal sourced from
        # 10-K / 10-Q Item 1A text.
        "lm_tone",
    }

    def _headers(self) -> dict[str, str]:
        ua = os.environ.get("SEC_EDGAR_USER_AGENT", _DEFAULT_USER_AGENT)
        return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}

    def health_check(self) -> SourceHealthStatus:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            resp = requests.get(_PROBE_URL, headers=self._headers(), timeout=10)
        except Exception as exc:  # noqa: BLE001
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"{type(exc).__name__}: {exc}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        if not resp.ok:
            return SourceHealthStatus(
                source=self.name,
                status=SourceStatus.FAILED,
                checked_at=now,
                message=f"HTTP {resp.status_code}",
                response_time_ms=(time.monotonic() - started) * 1000,
            )
        return SourceHealthStatus(
            source=self.name,
            status=SourceStatus.OK,
            checked_at=now,
            message=f"HTTP 200 ({len(resp.content)} bytes)",
            response_time_ms=(time.monotonic() - started) * 1000,
        )

    # ------------------------------------------------------------------
    # Phase A.3.3: XBRL fundamentals
    # ------------------------------------------------------------------

    def _resolve_cik(self, ticker: str, db) -> str:
        """Resolve ticker to 10-digit padded CIK using SecCikLookup."""
        from src.common.datasources.sec_cik_lookup import SecCikLookup
        return SecCikLookup(db=db).resolve(ticker)

    def _latest_filing_date(self, blob: dict) -> str | None:
        """Return the most recent `filed` date across all us-gaap observations.

        Used to drive the (edgar, ticker, xbrl_fundamentals) watermark — once
        we've seen filing X, we don't need to re-pull until filing X+1 lands.
        """
        latest = None
        facts = (blob.get("facts") or {}).get("us-gaap") or {}
        for _concept, body in facts.items():
            for _unit, obs_list in (body.get("units") or {}).items():
                for o in obs_list:
                    filed = o.get("filed")
                    if filed and (latest is None or filed > latest):
                        latest = filed
        return latest

    def fetch_fundamentals_for_ticker(
        self,
        ticker: str,
        run_id: str,
        db=None,
    ) -> int:
        """Pull XBRL companyfacts for ticker, parse, and persist.

        Returns the number of (period) rows actually inserted into
        raw_edgar_fundamentals (post-INSERT-OR-IGNORE).

        Watermark behavior:
          - (edgar, ticker, xbrl_fundamentals) tracks last_observation_date =
            most recent SEC `filed` date we've parsed for this ticker.
          - Same-day short-circuit: if a successful fetch already ran today
            (last_fetched_at starts with today's date), return 0 without HTTP.
            This protects against duplicate same-day calls from the registry
            without losing the ability to discover newly-filed 10-Q/10-K once
            the day rolls over.
        """
        if db is None:
            raise ValueError("db is required")

        # Short-circuit: if we've fetched today and a watermark is set, skip.
        w = db.get_watermark(self.name, ticker, "xbrl_fundamentals")
        if w is not None and w.get("last_observation_date"):
            try:
                last_fetched = (w.get("last_fetched_at") or "")[:10]  # YYYY-MM-DD
                today_iso = datetime.now(timezone.utc).date().isoformat()
                if last_fetched == today_iso:
                    return 0
            except Exception:
                pass

        # Resolve CIK (this can fail for tickers not in SEC map)
        try:
            cik = self._resolve_cik(ticker, db)
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="xbrl_fundamentals",
                last_observation_date=None, db=db, success=False,
                error_message=f"cik_resolve: {type(exc).__name__}: {exc}",
            )
            return 0

        # Pull companyfacts JSON
        url = _COMPANYFACTS_URL.format(cik=cik)
        try:
            resp = _sec_get_with_retry(url, headers=self._headers())
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="xbrl_fundamentals",
                last_observation_date=None, db=db, success=False,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            return 0

        if not resp.ok:
            self.update_watermark(
                ticker=ticker, field="xbrl_fundamentals",
                last_observation_date=None, db=db, success=False,
                error_message=f"HTTP {resp.status_code}",
            )
            return 0

        try:
            blob = resp.json()
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="xbrl_fundamentals",
                last_observation_date=None, db=db, success=False,
                error_message=f"json_decode: {type(exc).__name__}: {exc}",
            )
            return 0

        # Parse to structured rows
        from src.common.datasources.edgar_xbrl_parser import parse_companyfacts
        scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rows = parse_companyfacts(
            blob, run_id=run_id, ticker=ticker, scrape_timestamp=scrape_ts,
        )
        if not rows:
            self.update_watermark(
                ticker=ticker, field="xbrl_fundamentals",
                last_observation_date=None, db=db, success=False,
                error_message="parser returned 0 rows",
            )
            return 0

        # Persist with INSERT OR IGNORE (Principle 5: no overwrite)
        import sqlite3
        with sqlite3.connect(db.db_path) as c:
            n_before = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_fundamentals WHERE run_id=? AND ticker=?",
                (run_id, ticker),
            ).fetchone()[0]
        db.insert_raw_edgar_fundamentals(rows)
        with sqlite3.connect(db.db_path) as c:
            n_after = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_fundamentals WHERE run_id=? AND ticker=?",
                (run_id, ticker),
            ).fetchone()[0]
        n_inserted = n_after - n_before

        # Update watermark with the latest filing_date seen
        latest_filed = self._latest_filing_date(blob)
        import datetime as _dt
        last_obs = _dt.date.fromisoformat(latest_filed) if latest_filed else None
        self.update_watermark(
            ticker=ticker, field="xbrl_fundamentals",
            last_observation_date=last_obs, db=db, success=True,
        )
        return n_inserted

    def fetch_universe(
        self,
        run_id: str,
        ticker_list: list[str] | None = None,
        db=None,
    ) -> pd.DataFrame:
        """Batch-pull XBRL fundamentals for every ticker in ticker_list.

        EDGAR has no "scan all stocks" endpoint — companyfacts is per-CIK,
        so a ticker list (typically from FinvizSource.fetch_universe) is
        required. Each ticker is fetched independently via
        fetch_fundamentals_for_ticker; per-ticker failures are logged via
        the watermark and do NOT abort the batch.

        Returns a DataFrame with one row per (ticker, fiscal_year,
        fiscal_period) row visible in raw_edgar_fundamentals for this
        run_id at completion.
        """
        if db is None:
            raise ValueError("db is required")
        if not ticker_list:
            return pd.DataFrame()

        for ticker in ticker_list:
            try:
                self.fetch_fundamentals_for_ticker(ticker, run_id=run_id, db=db)
            except Exception:
                # update_watermark already called inside fetch_fundamentals_for_ticker
                # on the failure paths; broad except here is a final safety net.
                continue

        import sqlite3
        with sqlite3.connect(db.db_path) as c:
            cur = c.execute(
                """
                SELECT run_id, ticker, cik, fiscal_year, fiscal_period,
                       filing_date, form_type,
                       revenue_ttm, ebit_ttm, net_income_ttm,
                       total_assets, total_liabilities, total_equity,
                       cash_and_equivalents, total_debt,
                       operating_cashflow, capex, fcf_ttm,
                       operating_margin, net_profit_margin,
                       total_debt_to_equity, interest_coverage,
                       scrape_timestamp
                FROM raw_edgar_fundamentals
                WHERE run_id = ? AND ticker IN ({})
                """.format(",".join("?" * len(ticker_list))),
                (run_id, *[t.upper() for t in ticker_list]),
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, columns=cols)

    # ------------------------------------------------------------------
    # Phase A.3.4: Form 4 insider transactions + filings index (8-K etc.)
    # ------------------------------------------------------------------

    def fetch_filings_for_ticker(
        self,
        ticker: str,
        run_id: str,
        db=None,
        *,
        form_types: set[str] | None = None,
        last_n_days: int | None = 90,
    ) -> tuple[int, int]:
        """Pull the submissions index + every Form 4 XML for ticker.

        End-to-end pipeline:
          1. Resolve ticker -> CIK via SecCikLookup.
          2. GET /submissions/CIK{cik}.json (the per-issuer filings index).
          3. parse_submissions() -> (filing_rows, form4_accns).
          4. Short-circuit: if latest filing in the parsed rows <= the
             (edgar, ticker, filings) watermark's last_observation_date,
             skip Form 4 XML fetches and return (0, 0).
          5. For each form4_accn, GET the primary doc XML and
             parse_form4_xml() into RawEdgarInsiderRow records.
          6. classify_transactions() across ALL parsed insider rows from
             this batch to populate is_opportunistic +
             opportunistic_classifier_version per row.
          7. Persist filings + insider rows via INSERT OR IGNORE.
          8. Update (edgar, ticker, filings) watermark with the most-
             recent filing_date observed.

        SEC URL construction
        --------------------
        Submissions JSON:
          https://data.sec.gov/submissions/CIK{padded10}.json
        Form 4 primary XML (built by parse_submissions):
          https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{accn_no_dashes}/{primary_doc}
        Source: https://www.sec.gov/edgar/sec-api-documentation +
                https://www.sec.gov/os/accessing-edgar-data

        Returns
        -------
        (n_filings_inserted, n_insider_rows_inserted)
            Counts AFTER INSERT OR IGNORE (i.e., the net new rows).
        """
        if db is None:
            raise ValueError("db is required")

        # 1. CIK resolution
        try:
            cik = self._resolve_cik(ticker, db)
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="filings",
                last_observation_date=None, db=db, success=False,
                error_message=f"cik_resolve: {type(exc).__name__}: {exc}",
            )
            return (0, 0)

        # 2. Pull submissions JSON
        sub_url = _SUBMISSIONS_URL.format(cik=cik)
        try:
            sub_resp = _sec_get_with_retry(sub_url, headers=self._headers())
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="filings",
                last_observation_date=None, db=db, success=False,
                error_message=f"submissions: {type(exc).__name__}: {exc}",
            )
            return (0, 0)
        if not sub_resp.ok:
            self.update_watermark(
                ticker=ticker, field="filings",
                last_observation_date=None, db=db, success=False,
                error_message=f"submissions HTTP {sub_resp.status_code}",
            )
            return (0, 0)
        try:
            sub_blob = sub_resp.json()
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="filings",
                last_observation_date=None, db=db, success=False,
                error_message=f"submissions json: {type(exc).__name__}: {exc}",
            )
            return (0, 0)

        # 3. Parse submissions
        from src.common.datasources.edgar_submissions_parser import parse_submissions
        scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        filing_rows, form4_accns = parse_submissions(
            sub_blob, ticker=ticker, run_id=run_id,
            scrape_timestamp=scrape_ts,
            form_types=form_types,
            last_n_days=last_n_days,
        )

        if not filing_rows:
            self.update_watermark(
                ticker=ticker, field="filings",
                last_observation_date=None, db=db, success=True,
            )
            return (0, 0)

        # 4. Watermark short-circuit on latest filing date
        latest_filing = max(r.filing_date for r in filing_rows)
        w = db.get_watermark(self.name, ticker, "filings")
        prev_obs = (w or {}).get("last_observation_date") if w else None
        if prev_obs and latest_filing <= prev_obs:
            import datetime as _dt
            self.update_watermark(
                ticker=ticker, field="filings",
                last_observation_date=_dt.date.fromisoformat(prev_obs),
                db=db, success=True,
            )
            return (0, 0)

        # 5. Fetch Form 4 XMLs and parse to insider rows
        from src.common.datasources.edgar_form4_parser import (
            parse_form4_xml, Form4ParseError,
        )
        accn_to_url = {
            r.source_filing_accn: r.primary_doc_url
            for r in filing_rows
            if r.form_type in ("4", "4/A") and r.primary_doc_url
        }

        all_insider_rows: list = []
        for accn in form4_accns:
            url = accn_to_url.get(accn)
            if not url:
                continue
            try:
                f4_resp = _sec_get_with_retry(url, headers=self._headers())
            except Exception:
                continue
            if not f4_resp.ok:
                continue
            try:
                rows = parse_form4_xml(
                    f4_resp.content,
                    cik=cik, accn=accn, ticker=ticker,
                    scrape_timestamp=scrape_ts,
                    run_id=run_id,
                )
            except Form4ParseError:
                continue
            all_insider_rows.extend(rows)

        # 6. Run Cohen-Malloy-Pomorski classifier across the batch.
        if all_insider_rows:
            from src.methodology.opportunistic_insider import (
                classify_transactions, OPPORTUNISTIC_CLASSIFIER_VERSION,
            )
            txn_inputs = [
                {
                    "filer_id": f"{r.filer_name}:{r.cik}",
                    "transaction_date": r.transaction_date,
                    "transaction_code": r.transaction_code,
                }
                for r in all_insider_rows
            ]
            labels = classify_transactions(txn_inputs)
            classified = []
            for r, is_opp in zip(all_insider_rows, labels):
                classified.append(r.model_copy(update={
                    "is_opportunistic": bool(is_opp),
                    "opportunistic_classifier_version": OPPORTUNISTIC_CLASSIFIER_VERSION,
                }))
            all_insider_rows = classified

        # 7. Persist (INSERT OR IGNORE on both tables)
        import sqlite3
        with sqlite3.connect(db.db_path) as c:
            n_f_before = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_filings WHERE ticker=?", (ticker,)
            ).fetchone()[0]
            n_i_before = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_insider WHERE ticker=?", (ticker,)
            ).fetchone()[0]

        db.insert_raw_edgar_filings(filing_rows)
        db.insert_raw_edgar_insider(all_insider_rows)

        with sqlite3.connect(db.db_path) as c:
            n_f_after = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_filings WHERE ticker=?", (ticker,)
            ).fetchone()[0]
            n_i_after = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_insider WHERE ticker=?", (ticker,)
            ).fetchone()[0]

        n_filings_inserted = n_f_after - n_f_before
        n_insider_inserted = n_i_after - n_i_before

        # 8. Advance watermark to the latest filing_date observed
        import datetime as _dt
        try:
            last_obs = _dt.date.fromisoformat(latest_filing)
        except ValueError:
            last_obs = None
        self.update_watermark(
            ticker=ticker, field="filings",
            last_observation_date=last_obs, db=db, success=True,
        )

        return (n_filings_inserted, n_insider_inserted)

    # ------------------------------------------------------------------
    # Phase A.3.9: Loughran-McDonald tone for 10-K / 10-Q Item 1A
    # ------------------------------------------------------------------

    def fetch_filing_text_for_ticker(
        self,
        ticker: str,
        run_id: str,
        db=None,
        *,
        form_types: set[str] | None = None,
        max_filings: int | None = None,
        lm_dictionary_path: str | None = None,
    ) -> tuple[int, int]:
        """Score the LM tone of every unscored 10-K / 10-Q for ticker.

        Pipeline:
          1. Resolve ticker -> CIK.
          2. Select pending filings (NOT EXISTS in raw_edgar_filing_tone
             keyed on (cik, accn, lm_dictionary_version)).
          3. Load LM dictionary. On FileNotFoundError, persist one
             'dictionary_missing' audit row per pending filing so the
             failure is auditable (R2 reconciliation 2026-05-23).
          4. Per pending filing: fetch + extract + score. On HTTP
             failure (Exception OR empty body), persist a 'fetch_failed'
             audit row instead of silently skipping (R2 reconciliation).
          5. Persist via INSERT OR IGNORE. Advance the
             (edgar, ticker, filing_tone) watermark to the most-recent
             filing_date PROCESSED.

        Returns
        -------
        tuple[int, int]
            (n_processed, n_inserted). n_processed is the count of
            filings the call examined (including failure modes that
            still inserted an audit row). n_inserted is the net new
            rows in raw_edgar_filing_tone after INSERT OR IGNORE.
            (Was a single int before R2 reconciliation 2026-05-23.)
        """
        if db is None:
            raise ValueError("db is required")

        if form_types is None:
            form_types = {"10-K", "10-Q", "10-K/A", "10-Q/A"}

        try:
            _cik = self._resolve_cik(ticker, db)
        except Exception as exc:
            self.update_watermark(
                ticker=ticker, field="filing_tone",
                last_observation_date=None, db=db, success=False,
                error_message=f"cik_resolve: {type(exc).__name__}: {exc}",
            )
            return (0, 0)

        from src.methodology.lm_dictionary import (
            LM_DICTIONARY_VERSION,
            load_lm_dictionary,
        )
        from src.methodology.lm_tone_signal import score_tone

        if lm_dictionary_path is None:
            from pathlib import Path
            here = Path(__file__).resolve()
            repo_root = here.parents[3]
            lm_dictionary_path = str(
                repo_root / "config" / "lm_dictionary"
                / "LoughranMcDonald_MasterDictionary.csv"
            )

        form_placeholders = ", ".join("?" * len(form_types))
        select_sql = (
            f"""
            SELECT f.cik, f.source_filing_accn, f.form_type, f.filing_date,
                   f.primary_doc_url
              FROM raw_edgar_filings f
             WHERE f.ticker = ?
               AND f.form_type IN ({form_placeholders})
               AND f.primary_doc_url IS NOT NULL
               AND NOT EXISTS (
                 SELECT 1 FROM raw_edgar_filing_tone t
                  WHERE t.cik = f.cik
                    AND t.source_filing_accn = f.source_filing_accn
                    AND t.lm_dictionary_version = ?
               )
             ORDER BY f.filing_date ASC
            """
        )
        params: list = [ticker, *form_types, LM_DICTIONARY_VERSION]
        if max_filings is not None:
            select_sql += " LIMIT ?"
            params.append(int(max_filings))

        with db.get_connection() as conn:
            pending = conn.execute(select_sql, params).fetchall()

        if not pending:
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT MAX(filing_date) FROM raw_edgar_filing_tone "
                    "WHERE ticker = ? AND lm_dictionary_version = ?",
                    (ticker, LM_DICTIONARY_VERSION),
                ).fetchone()
            last_obs = None
            if row and row[0]:
                import datetime as _dt
                try:
                    last_obs = _dt.date.fromisoformat(row[0])
                except ValueError:
                    last_obs = None
            self.update_watermark(
                ticker=ticker, field="filing_tone",
                last_observation_date=last_obs, db=db, success=True,
            )
            return (0, 0)

        try:
            lm_dict = load_lm_dictionary(lm_dictionary_path)
            dictionary_available = True
            dict_missing_error: str | None = None
        except FileNotFoundError as exc:
            lm_dict = None
            dictionary_available = False
            dict_missing_error = str(exc)

        from src.common.datasources.edgar_full_text import (
            extract_risk_factors_section,
            fetch_filing_text,
        )
        from src.common.schemas import RawEdgarFilingToneRow

        tone_rows: list[RawEdgarFilingToneRow] = []
        latest_processed_date: str | None = None
        n_processed = 0

        for cik, accn, form_type, filing_date, primary_doc_url in pending:
            n_processed += 1
            scrape_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            if latest_processed_date is None or filing_date > latest_processed_date:
                latest_processed_date = filing_date

            if not dictionary_available:
                tone_rows.append(
                    RawEdgarFilingToneRow(
                        run_id=run_id, ticker=ticker, cik=cik,
                        source_filing_accn=accn, form_type=form_type,
                        filing_date=filing_date,
                        extraction_status="dictionary_missing",
                        lm_dictionary_version=LM_DICTIONARY_VERSION,
                        scrape_timestamp=scrape_ts,
                    )
                )
                continue

            try:
                raw_text = fetch_filing_text(
                    primary_doc_url, headers=self._headers()
                )
            except Exception:
                tone_rows.append(
                    RawEdgarFilingToneRow(
                        run_id=run_id, ticker=ticker, cik=cik,
                        source_filing_accn=accn, form_type=form_type,
                        filing_date=filing_date,
                        extraction_status="fetch_failed",
                        lm_dictionary_version=LM_DICTIONARY_VERSION,
                        scrape_timestamp=scrape_ts,
                    )
                )
                continue

            if not raw_text:
                tone_rows.append(
                    RawEdgarFilingToneRow(
                        run_id=run_id, ticker=ticker, cik=cik,
                        source_filing_accn=accn, form_type=form_type,
                        filing_date=filing_date,
                        extraction_status="fetch_failed",
                        lm_dictionary_version=LM_DICTIONARY_VERSION,
                        scrape_timestamp=scrape_ts,
                    )
                )
                continue

            section, status = extract_risk_factors_section(raw_text)
            scores = score_tone(section, lm_dict)
            tone_rows.append(
                RawEdgarFilingToneRow(
                    run_id=run_id, ticker=ticker, cik=cik,
                    source_filing_accn=accn, form_type=form_type,
                    filing_date=filing_date,
                    n_positive=scores["n_positive"],
                    n_negative=scores["n_negative"],
                    n_uncertainty=scores["n_uncertainty"],
                    n_litigious=scores["n_litigious"],
                    total_words=scores["total_words"],
                    net_tone=scores["net_tone"],
                    extraction_status=status,
                    lm_dictionary_version=LM_DICTIONARY_VERSION,
                    scrape_timestamp=scrape_ts,
                )
            )

        import sqlite3
        with sqlite3.connect(db.db_path) as c:
            n_before = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_filing_tone "
                "WHERE ticker = ? AND lm_dictionary_version = ?",
                (ticker, LM_DICTIONARY_VERSION),
            ).fetchone()[0]

        db.insert_raw_edgar_filing_tone(tone_rows)

        with sqlite3.connect(db.db_path) as c:
            n_after = c.execute(
                "SELECT COUNT(*) FROM raw_edgar_filing_tone "
                "WHERE ticker = ? AND lm_dictionary_version = ?",
                (ticker, LM_DICTIONARY_VERSION),
            ).fetchone()[0]

        n_inserted = n_after - n_before

        if not dictionary_available:
            self.update_watermark(
                ticker=ticker, field="filing_tone",
                last_observation_date=None, db=db, success=False,
                error_message=f"lm_dictionary_missing: {dict_missing_error}",
            )
        elif latest_processed_date is not None:
            import datetime as _dt
            try:
                last_obs = _dt.date.fromisoformat(latest_processed_date)
            except ValueError:
                last_obs = None
            self.update_watermark(
                ticker=ticker, field="filing_tone",
                last_observation_date=last_obs, db=db, success=True,
            )

        return (n_processed, n_inserted)
