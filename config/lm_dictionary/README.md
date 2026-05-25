# Loughran-McDonald Master Dictionary

**File:** `LoughranMcDonald_MasterDictionary.csv` (committed 2026-05-22; 9.1 MB; 86,553 master words; 3,876 with one or more finance-sentiment tags)

## Source

Downloaded from the Software Repository for Accounting and Finance
(sraf.nd.edu) maintained by Bill McDonald, University of Notre Dame:

  https://sraf.nd.edu/loughranmcdonald-master-dictionary/

The CSV on disk is the **2024 release** (published Mar 2026), file label
"Loughran-McDonald_MasterDictionary_1993-2025.csv" on the source page.

## Citation requirement

Per sraf.nd.edu, the dictionary requires citation when used:

> Loughran, T. and B. McDonald (2011). "When Is a Liability Not a
> Liability? Textual Analysis, Dictionaries, and 10-Ks." Journal of
> Finance 66(1), 35-65.

Additional citation for the 2014 / 2024 extensions:

> Loughran, T. and B. McDonald (2014). "Measuring Readability in
> Financial Disclosures." Journal of Finance 69(4), 1643-1671.

## License

> "The dictionary/sentiment lists are free for use in academic research.
>  For commercial applications, contacting loughranmcdonald@gmail.com is
>  required to obtain a commercial license."

This project is a personal trading research system (build-plan section 1).
If Trade Identifier is ever commercialized or distributed, the commercial
license must be obtained before re-distribution of this CSV.

## Columns used by Trade Identifier

`src/methodology/lm_dictionary.py` reads only:

- `Word` — the lexeme (uppercase ASCII)
- `Negative`, `Positive`, `Uncertainty`, `Litigious`,
  `Strong_Modal`, `Weak_Modal`, `Constraining` — non-zero means the word
  is in that category (the integer is the year the word was added; we
  treat it as a binary flag)

Other columns (frequency stats, complexity, syllables, source code) are
ignored. The loader filters out rows where all seven tag columns are
zero (~82k of 86k master rows).

## Refresh cadence

Yearly. Loughran-McDonald publishes an updated master dictionary roughly
once per academic year. To refresh: re-download from sraf.nd.edu and
overwrite this CSV; bump `LM_DICTIONARY_VERSION` in
`src/methodology/lm_dictionary.py`; re-score historical filings via the
orchestrator `force_refetch` path (downstream `raw_edgar_filing_tone`
rows are keyed by `(cik, source_filing_accn, lm_dictionary_version)`, so
new-version rows insert alongside old ones for audit comparison).
