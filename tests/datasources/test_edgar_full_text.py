"""Tests for `src.common.datasources.edgar_full_text`.

Coverage:
  * extract_risk_factors_section:
    - Clean 10-K HTML with both Item 1A start and Item 1B end markers.
    - 10-Q HTML where Item 1A is the FIRST marker but no Item 1B/2
      terminator -> section runs to end of document.
    - Pre-2005 10-K body without Item 1A markers -> full-document
      fallback path with status 'full_document_fallback'.
    - Empty body -> 'empty_document'.
    - HTML with <script>/<style> noise -> stripped from output.
    - Multi-occurrence Item 1A (TOC + body) -> picks LAST occurrence
      (R1 reconciliation 2026-05-23: was second, now last).

  * fetch_filing_text:
    - 2xx returns body unchanged.
    - 404 returns empty string.
    - 503 triggers tenacity retry -- mock raises until attempt succeeds.

No live network. All HTTP via mocker.MagicMock + side_effect.
"""
from __future__ import annotations

import pytest

from src.common.datasources.edgar_full_text import (
    _ITEM_1A_START_RE,
    _ITEM_END_RE,
    _SecHttpError,
    extract_risk_factors_section,
    fetch_filing_text,
)


# ---------------------------------------------------------------------------
# extract_risk_factors_section
# ---------------------------------------------------------------------------


class TestExtractRiskFactorsSection:
    def test_empty_input_status(self):
        section, status = extract_risk_factors_section("")
        assert section == ""
        assert status == "empty_document"

    def test_whitespace_only_input(self):
        section, status = extract_risk_factors_section("   \n\n  \t  ")
        assert section == ""
        assert status == "empty_document"

    def test_clean_10k_with_item_1a_and_1b_markers(self):
        html = """
        <html><body>
        <p>Table of Contents</p>
        <p>Item 1A. Risk Factors ... 12</p>
        <p>Item 1B. Unresolved Staff Comments ... 30</p>
        <hr>
        <p>Item 1A. Risk Factors</p>
        <p>The company faces significant risks of loss, litigation,
        and bankruptcy in volatile markets. Maybe these risks will
        materialize. Strong execution remains beneficial.</p>
        <p>Item 1B. Unresolved Staff Comments</p>
        <p>None.</p>
        </body></html>
        """
        section, status = extract_risk_factors_section(html)
        assert status == "item_1a_extracted"
        assert "litigation" in section.lower()
        assert "bankruptcy" in section.lower()
        # The "None." after the body's Item 1B should be cut.
        assert "None." not in section

    def test_item_1a_without_terminator_runs_to_end(self):
        text = (
            "Some preamble. Item 1A. Risk Factors. The firm may incur "
            "losses and litigation in coming quarters. End of doc."
        )
        section, status = extract_risk_factors_section(text)
        assert status == "item_1a_extracted"
        assert section.lower().startswith("item 1a")
        assert section.lower().endswith("end of doc.")

    def test_no_item_1a_markers_returns_fallback(self):
        # Pre-2005 filings predate the Item 1A requirement entirely.
        text = (
            "ANNUAL REPORT 2003. This pre-2005 filing predates the "
            "SEC's risk-factor disclosure requirement. Net loss for "
            "the year was material. The business outlook remains "
            "uncertain in light of the litigation."
        )
        section, status = extract_risk_factors_section(text)
        assert status == "full_document_fallback"
        assert "2003" in section
        assert "Net loss for the year" in section

    def test_html_script_and_style_stripped(self):
        html = """
        <html><head><style>p { color: red; }</style></head>
        <body>
          <script>alert('xss');</script>
          <p>Item 1A. Risk Factors. The company has litigation risk.</p>
          <p>Item 2. Properties</p>
        </body></html>
        """
        section, status = extract_risk_factors_section(html)
        assert status == "item_1a_extracted"
        assert "alert" not in section
        assert "color: red" not in section
        assert "litigation" in section.lower()

    def test_only_toc_occurrence_uses_first_match(self):
        text = (
            "Item 1. Business. Description of the business. "
            "Item 1A. Risk Factors -- see page 12. "
            "Item 1B. Unresolved staff comments -- see page 30. "
            "Item 2. Properties."
        )
        section, status = extract_risk_factors_section(text)
        assert status == "item_1a_extracted"
        assert "Item 1A" in section
        assert "Item 1B" not in section

    def test_multi_occurrence_prefers_last(self):
        """TOC occurrence(s) then body occurrence -> body wins.

        Reconciliation R1 (2026-05-23) switched the preference from
        the second match to the LAST match. This still picks body
        in the two-occurrence case (TOC then body), but also handles
        filings that mention Item 1A in both a TOC and an exhibits
        index before the actual body section.
        """
        text = (
            "TABLE OF CONTENTS. Item 1A. Risk Factors 12. Item 1B. "
            "Unresolved 30. Item 2. Properties 40. "
            "PART I. Item 1. Business overview here. "
            "Item 1A. Risk Factors. BODY: the firm faces litigation "
            "and bankruptcy risks across markets. "
            "Item 2. Properties. Properties text."
        )
        section, status = extract_risk_factors_section(text)
        assert status == "item_1a_extracted"
        assert "BODY" in section
        assert "litigation" in section.lower()

    def test_three_occurrences_picks_last(self):
        """When Item 1A appears in TOC + exhibits index + body, last wins."""
        text = (
            "TOC. Item 1A. Risk Factors page 12. Item 2. Properties page 40. "
            "EXHIBITS INDEX. Item 1A. references exhibit list. Item 2. text. "
            "PART I. Item 1A. Risk Factors. ACTUAL BODY: firm faces "
            "litigation and bankruptcy risks. "
            "Item 2. Properties. Properties text."
        )
        section, status = extract_risk_factors_section(text)
        assert status == "item_1a_extracted"
        assert "ACTUAL BODY" in section
        assert "litigation" in section.lower()


# ---------------------------------------------------------------------------
# Regex sanity
# ---------------------------------------------------------------------------


class TestRegexSanity:
    @pytest.mark.parametrize("phrase", [
        "Item 1A.",
        "ITEM 1A:",
        "Item 1a Risk Factors",
        "item 1A   Risk Factor",
        "Item   1A",
    ])
    def test_start_re_matches_common_forms(self, phrase):
        assert _ITEM_1A_START_RE.search(phrase) is not None

    @pytest.mark.parametrize("phrase", [
        "Item 1B.",
        "ITEM 1B:",
        "Item 2.",
        "Item   2 ",
    ])
    def test_end_re_matches_terminators(self, phrase):
        assert _ITEM_END_RE.search(phrase) is not None

    def test_end_re_does_not_match_item_3(self):
        assert _ITEM_END_RE.search("Item 3. Legal Proceedings.") is None


# ---------------------------------------------------------------------------
# fetch_filing_text
# ---------------------------------------------------------------------------


class TestFetchFilingText:
    def test_2xx_returns_body(self, mocker):
        resp = mocker.MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.text = "<html><body>10-K body</body></html>"
        mocker.patch(
            "src.common.datasources.edgar_full_text.requests.get",
            return_value=resp,
        )
        out = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/x/y/z.htm",
            headers={"User-Agent": "test"},
        )
        assert out == "<html><body>10-K body</body></html>"

    def test_404_returns_empty(self, mocker):
        resp = mocker.MagicMock()
        resp.ok = False
        resp.status_code = 404
        resp.text = "Not Found"
        mocker.patch(
            "src.common.datasources.edgar_full_text.requests.get",
            return_value=resp,
        )
        out = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/x/y/missing.htm",
            headers={"User-Agent": "test"},
        )
        assert out == ""

    def test_503_triggers_retry_then_succeeds(self, mocker):
        good_resp = mocker.MagicMock()
        good_resp.ok = True
        good_resp.status_code = 200
        good_resp.text = "body"
        bad_resp = mocker.MagicMock()
        bad_resp.ok = False
        bad_resp.status_code = 503
        bad_resp.text = ""
        mocker.patch(
            "src.common.datasources.edgar_full_text.requests.get",
            side_effect=[bad_resp, good_resp],
        )
        out = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/x/y/z.htm",
            headers={"User-Agent": "test"},
        )
        assert out == "body"

    def test_retry_exhaustion_reraises(self, mocker):
        bad_resp = mocker.MagicMock()
        bad_resp.ok = False
        bad_resp.status_code = 503
        bad_resp.text = ""
        mocker.patch(
            "src.common.datasources.edgar_full_text.requests.get",
            return_value=bad_resp,
        )
        with pytest.raises(_SecHttpError):
            fetch_filing_text(
                "https://www.sec.gov/Archives/edgar/data/x/y/z.htm",
                headers={"User-Agent": "test"},
            )

    def test_session_path_used_when_provided(self, mocker):
        session = mocker.MagicMock()
        resp = mocker.MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.text = "body"
        session.get.return_value = resp
        mocker.patch(
            "src.common.datasources.edgar_full_text.requests.get",
            side_effect=AssertionError("module-level get should not be called"),
        )
        out = fetch_filing_text(
            "https://www.sec.gov/Archives/edgar/data/x/y/z.htm",
            headers={"User-Agent": "test"},
            session=session,
        )
        assert out == "body"
        assert session.get.called
