"""Claude API narrative integration -- bull/bear/synthesis.

Calls the Anthropic API before the report renders so the LLM narrative
is part of the reporting pack.

Inspired by TauricResearch/TradingAgents bull/bear analyst debate
pattern, compressed into a single structured Claude call (cost +
determinism).

Gated by `ANTHROPIC_API_KEY` env var; falls back to deterministic
template on any error.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional


LLM_NARRATIVE_VERSION = "1.0"


@dataclass
class LLMNarrative:
    bull_case: str
    bear_case: str
    synthesis: str
    risk_flags: list[str] = field(default_factory=list)
    source: str = "template"  # 'template' | 'claude'
    llm_narrative_version: str = LLM_NARRATIVE_VERSION


_SYSTEM_PROMPT = """You are a financial-engineering analyst writing a forecast brief.

You receive a JSON packet with quantitative forecast data and must respond with EXACTLY a JSON object:
{
  "bull_case": "<3 sentences, max>",
  "bear_case": "<3 sentences, max>",
  "synthesis": "<3 sentences, max>",
  "risk_flags": ["<flag 1>", "<flag 2>", ...]
}

ABSOLUTE RULES:
- DO NOT introduce facts not present in the packet.
- DO NOT predict prices outside the supplied 95% confidence interval.
- DO NOT recommend trade actions; the analyst makes that call.
- REUSE numbers verbatim where possible.
- Risk flags are short concrete observations.
"""


def _packet_from_inputs(
    *,
    ticker: str,
    sector: Optional[str],
    as_of_date: str,
    horizon_days: int,
    ensemble_point: float,
    ensemble_lower_80: float,
    ensemble_upper_80: float,
    ensemble_lower_95: float,
    ensemble_upper_95: float,
    method_points: dict[str, float],
    method_dispersion: float,
    beats_random_walk: bool,
    top_factor_contributions: list[dict[str, Any]],
    macro_context: dict[str, Any],
    catalysts_in_window: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "sector": sector,
        "as_of_date": as_of_date,
        "horizon_days": horizon_days,
        "ensemble": {
            "point_return": ensemble_point,
            "ci_80": [ensemble_lower_80, ensemble_upper_80],
            "ci_95": [ensemble_lower_95, ensemble_upper_95],
            "method_dispersion": method_dispersion,
            "beats_random_walk": beats_random_walk,
        },
        "per_method_points": method_points,
        "top_factor_contributions": top_factor_contributions,
        "macro_context": macro_context,
        "catalysts_in_window": catalysts_in_window,
    }


def _template_narrative(packet: dict[str, Any]) -> LLMNarrative:
    ticker = packet.get("ticker", "?")
    ens = packet.get("ensemble", {})
    point = ens.get("point_return", 0.0)
    band = ens.get("ci_80", [0.0, 0.0])
    disp = ens.get("method_dispersion", 0.0)
    beats_rw = ens.get("beats_random_walk", False)
    macro = packet.get("macro_context", {})
    regime = macro.get("macro_regime", "neutral")
    factors = packet.get("top_factor_contributions", [])
    n_cat = len(packet.get("catalysts_in_window", []))
    horizon = packet.get("horizon_days", 30)

    pos_drivers = [f["factor"] for f in factors if f.get("contribution", 0) > 0][:2]
    neg_drivers = [f["factor"] for f in factors if f.get("contribution", 0) < 0][:1]

    bull = (
        f"{ticker} ensemble forecast is {point:+.2%} with 80% band "
        f"[{band[0]:+.2%}, {band[1]:+.2%}] over {horizon} days. "
        f"Positive drivers: {', '.join(pos_drivers) if pos_drivers else 'no clear drivers'}. "
        f"{n_cat} catalyst(s) in the window provide event-driven upside potential."
    )
    bear = (
        f"Inter-method dispersion ({disp:+.4f}) and "
        f"{'beats random-walk' if beats_rw else 'no clear edge vs. random-walk'} "
        f"argue caution. "
        f"Negative drivers: {', '.join(neg_drivers) if neg_drivers else 'few headwinds in the factor stack'}. "
        f"Macro regime {regime} historically caps single-name conviction."
    )
    synth = (
        f"Weight the ensemble's {point:+.2%} center against {disp:+.4f} method dispersion. "
        f"{regime.replace('_', ' ').title()} regime + {n_cat} catalyst(s) define the setup. "
        f"Treat the 80% band as the working trade-sizing envelope."
    )
    flags: list[str] = []
    if disp > 0.05:
        flags.append("wide method dispersion -- model uncertainty elevated")
    if not beats_rw:
        flags.append("ensemble does not beat random-walk baseline -- no clear edge")
    if regime == "risk_off":
        flags.append("macro regime is risk-off -- consider hedging")
    if n_cat == 0:
        flags.append("no catalysts in window -- pure factor bet")

    return LLMNarrative(
        bull_case=bull, bear_case=bear, synthesis=synth, risk_flags=flags,
        source="template",
    )


def _claude_narrative(packet: dict[str, Any]) -> Optional[LLMNarrative]:
    try:
        import anthropic
    except Exception:
        return None
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-3-5-sonnet-latest",
            max_tokens=800,
            system=_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    "Packet:\n```json\n"
                    + json.dumps(packet, indent=2, default=str)
                    + "\n```\n\nReturn the JSON object now."
                ),
            }],
        )
        text_parts = []
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
        raw = "".join(text_parts).strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < 0 or end <= start:
            return None
        parsed = json.loads(raw[start:end + 1])
        return LLMNarrative(
            bull_case=str(parsed.get("bull_case", "")).strip(),
            bear_case=str(parsed.get("bear_case", "")).strip(),
            synthesis=str(parsed.get("synthesis", "")).strip(),
            risk_flags=list(parsed.get("risk_flags", []) or []),
            source="claude",
        )
    except Exception:
        return None


_CRITIQUE_SYSTEM_PROMPT = """You are a buy-side junior analyst reviewing a quant model's output. Your job is NOT to rewrite the model -- it is to flag where you would push back in front of a PM.

You must respond with EXACTLY a JSON object of the form:
{
  "agreement": "agree" | "agree_with_caveats" | "disagree",
  "confidence": 0.0-1.0,
  "flags": [
    {
      "type": "factor_conflict" | "stale_data" | "regime_mismatch" | "peer_outlier" | "news_contradiction" | "crowding" | "model_uncertainty",
      "severity": "low" | "medium" | "high",
      "evidence": "<one sentence pointing at specific number>",
      "suggested_check": "<one sentence on what to verify>"
    }
  ],
  "what_the_model_missed": ["<1-3 short items, qualitative>"],
  "sensitivity_concern": "<which input, if wrong by 1 stdev, flips the call? one sentence>",
  "one_line_pm_summary": "<= 25 words>"
}

ABSOLUTE RULES:
- Cite specific numbers from the packet; do not invent facts.
- If no real flag, return flags: [].
- 'what_the_model_missed' references qualitative/macro context OUTSIDE the numbers.
"""


@dataclass
class LLMCritique:
    """Structured analyst critique."""
    agreement: str  # 'agree' | 'agree_with_caveats' | 'disagree'
    confidence: float
    flags: list[dict] = field(default_factory=list)
    what_the_model_missed: list[str] = field(default_factory=list)
    sensitivity_concern: str = ""
    one_line_pm_summary: str = ""
    source: str = "template"


def _template_critique(packet: dict[str, Any]) -> LLMCritique:
    """Deterministic fallback critique (no LLM).

    All items are derived from the packet -- no static boilerplate.
    Source tagged `offline_template` so report can visibly badge
    offline-vs-Claude.
    """
    ticker = packet.get("ticker", "?")
    sector = packet.get("sector")
    horizon = packet.get("horizon_days", 30)
    ens = packet.get("ensemble", {})
    point = ens.get("point_return", 0.0)
    ci80 = ens.get("ci_80", [0.0, 0.0])
    disp = ens.get("method_dispersion", 0.0)
    beats_rw = ens.get("beats_random_walk", False)
    catalysts = packet.get("catalysts_in_window", []) or []
    n_cat = len(catalysts)
    macro = packet.get("macro_context", {}) or {}
    regime = macro.get("macro_regime", "neutral")
    factors = packet.get("top_factor_contributions", []) or []
    sens_top = packet.get("sensitivity_top_factor")

    # Compute factor-concentration: share of |contribution| in top factor
    top_factor_name = None
    top_factor_contrib = 0.0
    concentration_share = 0.0
    if factors:
        sorted_f = sorted(
            factors,
            key=lambda f: abs(float(f.get("contribution", 0.0) or 0.0)),
            reverse=True,
        )
        top_factor_name = str(sorted_f[0].get("factor", "?"))
        top_factor_contrib = float(sorted_f[0].get("contribution", 0.0) or 0.0)
        total_abs = sum(
            abs(float(f.get("contribution", 0.0) or 0.0)) for f in factors
        )
        if total_abs > 0:
            concentration_share = abs(top_factor_contrib) / total_abs

    # Width of the 80% band -- proxy for forecast uncertainty
    band_width = float(ci80[1]) - float(ci80[0]) if len(ci80) == 2 else 0.0

    # ---- Flags (only emitted when conditions actually fire) ----
    flags: list[dict] = []

    if disp > 0.05:
        flags.append({
            "type": "model_uncertainty",
            "severity": "high" if disp > 0.10 else "medium",
            "evidence": (
                f"method dispersion {disp:+.4f} exceeds 5% "
                f"(80% band width {band_width:+.2%})"
            ),
            "suggested_check": (
                "inspect per-method points -- if outlier method drags ensemble, "
                "consider trimmed mean or median"
            ),
        })
    if not beats_rw:
        flags.append({
            "type": "model_uncertainty",
            "severity": "high",
            "evidence": (
                f"ensemble point {point:+.2%} does not exceed random-walk "
                f"baseline; no edge over naive carry"
            ),
            "suggested_check": (
                "abstain / treat as no-trade until next factor refresh "
                "or independent signal corroborates"
            ),
        })
    if regime == "risk_off":
        flags.append({
            "type": "regime_mismatch",
            "severity": "medium",
            "evidence": f"macro regime is {regime}",
            "suggested_check": (
                "size positions smaller; "
                "consider beta-neutral overlay regardless of single-name bias"
            ),
        })
    if concentration_share > 0.50 and top_factor_name:
        flags.append({
            "type": "factor_conflict",
            "severity": "medium",
            "evidence": (
                f"{top_factor_name} drives "
                f"{concentration_share:.0%} of total contribution -- single-factor bet"
            ),
            "suggested_check": (
                f"stress-test what {ticker} looks like if {top_factor_name} "
                f"z-score reverts to 0"
            ),
        })
    # Note: deliberately NOT emitting a generic 'stale_data' flag when n_cat==0
    # (V called that out as boilerplate). A truly stale-data flag requires
    # genuine evidence (e.g. price data > 5 trading days old).

    # ---- Context-derived 'what_the_model_missed' ----
    missed: list[str] = []
    # Always-applicable but specific: peer comparison gap referencing sector
    if sector:
        missed.append(
            f"no peer-relative cross-check vs other {sector} names "
            f"(forecast is single-ticker, not pair / sector-rel)"
        )
    else:
        missed.append(
            "sector unknown -- forecast cannot be cross-checked against peers"
        )
    # Catalyst integration depth
    if n_cat == 0:
        missed.append(
            f"no scheduled catalysts within {horizon}d "
            f"-- model is a pure factor bet, no event-pricing component"
        )
    else:
        cat_types = [str(c.get("type", "?")) for c in catalysts if isinstance(c, dict)]
        if cat_types:
            missed.append(
                f"{n_cat} catalyst(s) ({', '.join(cat_types[:3])}) "
                "scheduled -- model integrates calendar dates but not "
                "expected-move size from options skew"
            )
        else:
            missed.append(
                f"{n_cat} catalyst(s) in window -- "
                "no options-implied move sizing folded in"
            )
    # IV/realized vol gap (always relevant for options-strategy decisions)
    missed.append(
        "no options-implied vol vs realized vol gap "
        "(IV-RV would size delta-1 vs vol-trade choice)"
    )

    # ---- Sensitivity concern, referencing the actual top factor ----
    if sens_top:
        sens_text = (
            f"If {sens_top} (the highest-loading factor) is mis-measured by "
            f"+/-1 stdev, the bias likely flips."
        )
    elif top_factor_name:
        sens_text = (
            f"If {top_factor_name} (top loading; {concentration_share:.0%} of "
            f"|contribution|) z is wrong by +/-1, the {point:+.2%} call flips."
        )
    else:
        sens_text = (
            "Top-factor sensitivity unavailable -- factor contributions "
            "not surfaced for this run."
        )

    # ---- One-line PM summary, with sector hook if available ----
    sector_tag = f" {sector}" if sector else ""
    one_liner = (
        f"{ticker}{sector_tag}: quant {point:+.2%}/{horizon}d, "
        f"disp {disp:+.4f}, "
        f"{'beats' if beats_rw else 'no edge vs'} RW, "
        f"macro {regime}, {n_cat} catalyst(s)."
    )

    # Confidence: lower when uncertainty signals stack
    confidence = 0.55
    if not beats_rw:
        confidence -= 0.15
    if disp > 0.05:
        confidence -= 0.05
    if regime == "risk_off":
        confidence -= 0.05
    confidence = max(0.20, min(0.80, confidence))

    return LLMCritique(
        agreement="agree_with_caveats",
        confidence=confidence,
        flags=flags,
        what_the_model_missed=missed,
        sensitivity_concern=sens_text,
        one_line_pm_summary=one_liner,
        source="offline_template",
    )


def _claude_critique(packet: dict[str, Any]) -> Optional[LLMCritique]:
    try:
        import anthropic
    except Exception:
        return None
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-3-5-sonnet-latest",
            max_tokens=1200,
            system=_CRITIQUE_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    "Packet:\n```json\n"
                    + json.dumps(packet, indent=2, default=str)
                    + "\n```\n\nReturn the JSON now."
                ),
            }],
        )
        text_parts = []
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
        raw = "".join(text_parts).strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < 0 or end <= start:
            return None
        parsed = json.loads(raw[start:end + 1])
        return LLMCritique(
            agreement=str(parsed.get("agreement", "agree_with_caveats")),
            confidence=float(parsed.get("confidence", 0.5) or 0.5),
            flags=list(parsed.get("flags", []) or []),
            what_the_model_missed=list(parsed.get("what_the_model_missed", []) or []),
            sensitivity_concern=str(parsed.get("sensitivity_concern", "") or ""),
            one_line_pm_summary=str(parsed.get("one_line_pm_summary", "") or ""),
            source="claude",
        )
    except Exception:
        return None


def build_critique(
    *,
    ticker: str,
    sector: Optional[str],
    as_of_date: str,
    horizon_days: int,
    ensemble_point: float,
    ensemble_lower_80: float,
    ensemble_upper_80: float,
    ensemble_lower_95: float,
    ensemble_upper_95: float,
    method_points: dict[str, float],
    method_dispersion: float,
    beats_random_walk: bool,
    top_factor_contributions: list[dict[str, Any]],
    macro_context: dict[str, Any],
    catalysts_in_window: list[dict[str, Any]],
    sensitivity_top_factor: Optional[str] = None,
    allow_llm: Optional[bool] = None,
) -> LLMCritique:
    """Run the analyst-critique pass over the forecast packet."""
    packet = _packet_from_inputs(
        ticker=ticker, sector=sector,
        as_of_date=as_of_date, horizon_days=horizon_days,
        ensemble_point=ensemble_point,
        ensemble_lower_80=ensemble_lower_80,
        ensemble_upper_80=ensemble_upper_80,
        ensemble_lower_95=ensemble_lower_95,
        ensemble_upper_95=ensemble_upper_95,
        method_points=method_points,
        method_dispersion=method_dispersion,
        beats_random_walk=beats_random_walk,
        top_factor_contributions=top_factor_contributions,
        macro_context=macro_context,
        catalysts_in_window=catalysts_in_window,
    )
    if sensitivity_top_factor is not None:
        packet["sensitivity_top_factor"] = sensitivity_top_factor

    if allow_llm is None:
        allow_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if allow_llm:
        c = _claude_critique(packet)
        if c is not None:
            return c
    return _template_critique(packet)


def build_narrative(
    *,
    ticker: str,
    sector: Optional[str],
    as_of_date: str,
    horizon_days: int,
    ensemble_point: float,
    ensemble_lower_80: float,
    ensemble_upper_80: float,
    ensemble_lower_95: float,
    ensemble_upper_95: float,
    method_points: dict[str, float],
    method_dispersion: float,
    beats_random_walk: bool,
    top_factor_contributions: list[dict[str, Any]],
    macro_context: dict[str, Any],
    catalysts_in_window: list[dict[str, Any]],
    allow_llm: Optional[bool] = None,
) -> LLMNarrative:
    """Top-level entry: packet -> Claude (if allowed) -> template fallback."""
    packet = _packet_from_inputs(
        ticker=ticker, sector=sector,
        as_of_date=as_of_date, horizon_days=horizon_days,
        ensemble_point=ensemble_point,
        ensemble_lower_80=ensemble_lower_80,
        ensemble_upper_80=ensemble_upper_80,
        ensemble_lower_95=ensemble_lower_95,
        ensemble_upper_95=ensemble_upper_95,
        method_points=method_points,
        method_dispersion=method_dispersion,
        beats_random_walk=beats_random_walk,
        top_factor_contributions=top_factor_contributions,
        macro_context=macro_context,
        catalysts_in_window=catalysts_in_window,
    )
    if allow_llm is None:
        allow_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if allow_llm:
        claude = _claude_narrative(packet)
        if claude is not None:
            return claude
    return _template_narrative(packet)
