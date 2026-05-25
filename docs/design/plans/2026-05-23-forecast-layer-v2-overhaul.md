# Forecast Layer v2 — Comprehensive Overhaul

> Triggered by 2026-05-23 feedback on the v1 demo: synthetic prices ($75 anchor for SNDK while real SNDK is ~$1,478) made the demo meaningless. v2 ships real-data pipeline + interactive dark-mode reports + ensemble forecasting + factor backtest + macro context + LLM narrative.

## Root-cause of the v1 defect

`scripts/forecast_demo.py --synthetic` used a hand-coded `anchor_price` dict (`SNDK: 75.0`). The intent was a shape-check demo, but the rendered HTML read as a real forecast. The mistake was producing a deliverable that looked authoritative but wasn't.

**Fix in v2**: the demo defaults to LIVE yfinance fetches. Synthetic mode is opt-in via an explicit `--synthetic` flag with a giant warning banner in the rendered HTML. There is no anchor-price dict.

## Research synthesis (references)

| Source | Takeaway adopted |
|---|---|
| **TauricResearch/TradingAgents** | Bull-vs-bear analyst debate pattern for the LLM narrative cell. Persistent decision log for accuracy tracking. |
| **huseinzol05/Stock-Prediction-Models** | Confirms Monte Carlo flavors (drift, dynamic-vol) are the right baseline; LSTM/Transformer ensemble accuracy claims (95.86%) are *signal-direction* accuracy on small windows -- not return-magnitude -- so we calibrate expectations for the ensemble realistically. |
| **arxiv:1307.5122 (Relativistic Black-Scholes)** | Theoretical/physics; not applicable to factor forecasting. Skip. |
| **arxiv:2511.10365 (FCOC fractal+chaotic vol)** | Promising for vol forecasting; parked for v3 -- too new for production. |
| **AIScientists-Dev/WorldSeed** | Asymmetric-information multi-agent pattern; useful design idea (bull/bear agents see different slices) but expensive vs. value for v2 scope. |
| **Plotly** | `plotly_dark` template + `to_html(include_plotlyjs='cdn')` produces self-contained interactive HTML files. Matches the "interactive without a model database" constraint. |

## Methodology choices (locked v2)

| Decision | Choice | Rationale |
|---|---|---|
| Forecast horizons | 30 / 60 / 90 days (all shown) | Match short / medium / long DTE windows. |
| Methods (ensemble) | (a) Linear multi-factor regression (v1 retained); (b) GBM Monte Carlo with Yang-Zhang vol drift; (c) AR(1) momentum; (d) Random walk baseline. | Diversify across model classes. Ensemble weights via inverse out-of-sample MAE on rolling window. |
| Confidence intervals | 80% AND 95% from BOTH bootstrap (linear) AND 1,000-path MC (GBM) | Bootstrap is non-parametric; MC is parametric; their gap is itself a signal. |
| Factor decay | Exponential half-life per factor type (catalyst 7d; news 14d; fundamentals 90d) per Tetlock 2007 / Cohen-Malloy-Pomorski 2012 literature. | Catalysts fade fast; fundamentals slow. |
| Factor-backtest metric | Per-factor rolling Information Coefficient (Spearman, 252d windows) + cumulative attribution P&L per factor. | Grinold-Kahn standard quant-equity factor-skill metric. |
| Macro factors | DGS10 / DGS3MO / VIXCLS / sector ETF (XLK / XLY / XLP / XLI per ticker) / DXY. | Standard macro state proxies. FRED already wired (A.3.5); sector ETF via yfinance. |
| Macro regime tag | 4-state classifier (risk-on/risk-off x growth/value) using yield-curve slope + VIX + sector relative-strength. | Compact narrative tag. |
| Report tech | Plotly interactive (CDN JS) + Jinja2 + dark-mode CSS (Tokyo-Night palette adapted). | One HTML file; opens in any browser; hover/zoom/range-slider. |
| LLM narrative | Anthropic Claude API; structured packet -> bull case (3 sentences) + bear case (3 sentences) + synthesis (3 sentences) + risk flags (bullets). Gated by `ANTHROPIC_API_KEY`. Falls back to deterministic template. | TradingAgents-inspired but compressed; deterministic fallback keeps tests free. |

## v2 architecture

```
src/layer3_forecast/
  factor_forecast.py        ← linear regression + bootstrap (v1, retained)
  monte_carlo.py            ← NEW: GBM MC with Yang-Zhang drift + dynamic vol
  ensemble.py               ← NEW: 4-method ensemble + rolling weights
  factor_decay.py           ← NEW: time-varying factor contribution decay
  factor_backtest.py        ← NEW: per-factor IC + attribution P&L
  macro_factors.py          ← NEW: macro regime + sector context
  llm_narrative.py          ← NEW: Claude API bull/bear/synthesis cell
  report_v2.py              ← NEW: Plotly dark-mode HTML report
  templates/
    stock_report_v2.html.j2 ← NEW dark-mode Plotly template
scripts/
  forecast_live_demo.py     ← NEW: real-data demo for the 5-ticker test set
```

## Wave plan

| Wave | Deliverable | Module(s) | Tests |
|---|---|---|---|
| v2.0 | This plan doc | --- | --- |
| v2.1 | Live data fetcher | scripts/forecast_live_demo.py + helpers | 1 mocked-yfinance smoke |
| v2.2 | Monte Carlo + ensemble + factor decay | monte_carlo.py, ensemble.py, factor_decay.py | unit per module |
| v2.3 | Factor backtest | factor_backtest.py | unit on synthetic factor panels |
| v2.4 | Macro + sector context | macro_factors.py | unit (deterministic regime tagging) |
| v2.5 | Plotly dark-mode report | report_v2.py + template | unit on plot/widget assembly |
| v2.6 | Claude API narrative | llm_narrative.py | unit for prompt assembly + template fallback |
| v2.7 | E2E live run + commit + catch-up doc | --- | live run produces 5 HTML + index |

## Original-objective guardrails

Standing reminder: maximum coverage/effort/data-driven research; the goal is improving risk-reward analysis and net profitability; no point aggregating computational metrics if the model doesn't give an edge over the rest of the market; prepare for all possibilities while sticking to fundamental long-term roots.

Operational translation:

1. **Risk-reward over forecast accuracy.** Headline is *risk-adjusted* point return (Sharpe / Sortino) over the horizon, not raw point return.
2. **Honest baselines.** Random-walk forecast on every report as a "humility line" -- if linear / MC ensemble can't beat RW, report says so.
3. **Long-horizon fundamental anchor.** Separate "long-term fundamental fair-value" panel (DCF-lite via roe + revenue_growth_yoy + fcf_ttm) sets 12-month context. 30/60/90-day forecast trades around that anchor.
4. **Edge accounting.** "Model edge" panel shows out-of-sample R² of ensemble vs. RW over trailing 252 days. If edge < 0, report top banner shows a warning.
5. **All-possibilities scenarios.** Bull / base / bear from MC percentiles (90 / 50 / 10) side-by-side.

## Anti-patterns to avoid

- ❌ Synthetic anchor prices in any user-facing deliverable.
- ❌ Quietly burying the "synthetic" disclaimer in a footer.
- ❌ Forecast methods presented as gospel -- always show random-walk baseline + inter-method dispersion.
- ❌ One-factor narratives. Report must call out >=3 driver factors + >=1 macro factor + >=1 risk flag.
- ❌ Static charts. Every interactive-relevant panel is Plotly.

## Catch-up document

A catch-up doc records every wave so work can be resumed across sessions.
