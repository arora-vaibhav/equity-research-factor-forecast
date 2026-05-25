# How To Run Tests

This is the short, friendly guide. **You only need Jupyter** — no command-line Python.

---

## TL;DR

1. Open Anaconda → Jupyter Notebook (or JupyterLab)
2. Navigate to this project's `notebooks/` folder
3. Open `smoke_tests.ipynb`
4. Click **Cell → Run All** (or hit `Shift+Enter` cell-by-cell)
5. Read the output — green checkmarks mean working, red X's mean something needs attention

That's it. If everything works, you'll see "ALL SMOKE TESTS PASSED" at the bottom.

---

## What's in `notebooks/smoke_tests.ipynb`

| Section | What it does | How long |
|---|---|---|
| 1. Setup | Confirms imports work + database is at expected schema version | <1 s |
| 2. API key check | Tests FMP / Polygon / Tiingo against live providers | ~10 s |
| 3. Run automated tests | The full pytest suite (177+ unit tests) | ~15 s |
| 4. Live Finviz pull | Pulls the universe of $2B+ US stocks from Finviz right now | ~5 s |
| 5. Live Yahoo fundamentals | Pulls AAPL's current fundamentals from Yahoo right now | ~3 s |
| 6. Live Yahoo prices | Pulls AAPL's last 5 days of OHLCV — uses watermark to avoid re-pulling | ~3 s |
| 7. Database inspection | Shows what's in `fetch_watermarks`, `historical_price`, `raw_yahoo` | <1 s |
| 8. Summary | Pass/fail tally with a recommendation if anything is off | <1 s |

Total: ~40 seconds end-to-end.

---

## What to do if a section fails

### Section 1 — Setup fails
Imports are broken. Likely the venv didn't pick up new packages. From an Anaconda terminal:
```
cd <project-root>
venv\Scripts\activate
pip install -r requirements.txt
```
Restart the Jupyter kernel and re-run.

### Section 2 — API key check fails
You'll see one of these:
- **"NOT SET"** — that provider's key is missing from `.env`. Open `.env` in a text editor and paste the key.
- **HTTP 403 Forbidden on FMP with "Legacy Endpoint" message** — your FMP key works fine; it's just on the new "stable" API rather than the legacy v3. The smoke test now targets the stable API by default (as of 2026-05-21). If you see legacy errors, your test is hitting the old URL — re-run the latest notebook.
- **HTTP 401 / 403 on Polygon or Tiingo** — key is wrong or expired. Re-issue from their dashboard and update `.env`.
- **Connection timeout** — your internet is down or the provider's service is briefly degraded. Wait 30 seconds and re-run.

### Section 3 — pytest fails
A test broke. The output will tell you which test and which assertion failed. Inspect the failure and trace it back to the relevant source module.

### Section 4-6 — Live pulls fail
- **Finviz HTML structure changed** — they sometimes update the page. The scraper needs updating; capture the error text first.
- **Yahoo throttling** — Yahoo sometimes rate-limits unauthenticated calls. Wait a minute and re-run.

### Section 7 — Database inspection shows nothing
Sections 4-6 didn't run or didn't write to the database. Check section 4-6 output first.

---

## Adding a new API key

1. Open `.env.example` to see the template — it lists all the slots and where to sign up
2. Open `.env` in a text editor (make sure your editor doesn't add a `.txt` extension)
3. Find the key you want to add (or paste a new `NEW_KEY=value` line)
4. Save the file
5. In `config/api_keys.yaml`, add a row mapping a friendly name to your new env var:
   ```
   newprovider: NEW_KEY
   ```
6. Restart your Jupyter kernel so the new key is picked up
7. Re-run the smoke tests

---

## Running just one section (when iterating)

In Jupyter you can run individual cells by clicking the cell and pressing `Shift+Enter`. The numbered sections in `smoke_tests.ipynb` are designed to be runnable in isolation as long as Section 1 (setup) ran first.

---

## Running pytest from the command line (optional)

If you ever do want to drop to a terminal, the equivalent of Section 3 is:
```
venv\Scripts\python.exe -m pytest -m "not integration"
```
Run that from the project root. You should see `177 passed` (or similar) and zero failures. Same as what Section 3 of the notebook reports.

---

## A note on the database

The database file lives at `data/fundamentals.db`. It's gitignored so it stays a local-only copy. If a fresh start is needed (rare!), delete it — the next run of Section 1 will recreate it empty.

---

## Smoke tests vs the full test suite

- **Smoke tests** (sections 4-7 of the notebook): exercise the system against the real internet. Catches "did Yahoo change their API?" failures.
- **Full pytest suite** (section 3 of the notebook): all 177 tests against fixture data. Catches "did our code accidentally break?" failures.

You can — and should — run both. The notebook does both for you.
