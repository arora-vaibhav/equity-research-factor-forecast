# Phase G Interface Framework Comparison

**Status:** Research deliverable for Phase G design (interface layer)
**Date:** 2026-05-21
**Scope:** I compared 13 Python UI/dashboard frameworks against the v2 architecture's interface needs (trigger runs, live monitoring, ranked-candidate review, P&L view, ad-hoc research, health checks, trade approval). Local-only desktop install, single user, no auth.

Reference: the architecture spec — §4 (layer responsibilities) and §15 (phase plan).

---

## 1. Executive summary matrix

Stars/release dates are May 2026 snapshots. Legend: ✅ strong, ⚠️ workable, ❌ poor / missing.

| Framework | Stars | Latest rel. | Local-only | Real-time updates | Plotting | Inputs/forms | State mgmt | Multi-page | Pandas/SQLite | Notebook reuse | Long tasks | LLM ecosystem |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Streamlit** | 44.7k | 1.57.0 (Apr 26) | ✅ | ⚠️ fragments+rerun | ✅ Plotly/Altair/Matplotlib/Bokeh native | ✅ | ⚠️ st.session_state | ✅ | ✅ | ⚠️ (rewrite, but streamlit-jupyter helps) | ⚠️ fragments + status containers | ✅ huge |
| **Marimo** | 21.1k | 0.23.6 (May 26) | ✅ | ✅ reactive DAG | ✅ Altair/Plotly/Matplotlib native | ✅ | ✅ reactive variables | ✅ | ✅ + built-in SQL | ✅ **stored as .py, IS the notebook** | ✅ async cells, marimo pair AI | ✅ "AI-native" stance |
| **Panel (HoloViz)** | 5.7k | 1.9.0 (May 26) | ✅ | ✅ async + bidi widgets | ✅ all PyViz (Bokeh/Plotly/Matplotlib/Altair) | ✅ | ✅ proper reactive `pn.rx` | ✅ | ✅ | ✅ great Jupyter story | ✅ first-class async | ⚠️ smaller |
| **Dash (Plotly)** | ~22k | 3.x active | ✅ | ⚠️ callback model | ⚠️ Plotly-centric | ✅ | ⚠️ verbose | ✅ | ✅ | ❌ rewrite | ⚠️ background_callback w/ Celery/Redis | ⚠️ |
| **Gradio** | ~33k | 5.x active | ✅ | ✅ for stream/chat | ⚠️ Plot component, ML-oriented | ✅ | ⚠️ ML-shaped | ⚠️ Tabs/Blocks | ⚠️ DataFrame component | ❌ | ✅ stream/yield generators | ✅ HF-native |
| **Reflex** | 28.4k | 0.9.2 (May 26) | ⚠️ compiles to Next.js, heavier local install | ✅ websockets native | ⚠️ via Recharts/Plotly | ✅ | ✅ class-based | ✅ | ⚠️ ORM not pandas-shaped | ❌ | ✅ async event handlers | ✅ |
| **Solara** | 2.2k | active | ✅ | ✅ reactive ipywidgets | ✅ Plotly/Altair/Matplotlib via widgets | ✅ | ✅ React-style hooks | ✅ | ✅ | ✅ runs in Jupyter+standalone | ✅ via threading | ⚠️ |
| **Mesop** (Google) | 6.5k | 1.3.0 (May 26) | ✅ | ✅ for AI streaming | ⚠️ matplotlib, no native chart lib | ✅ | ✅ typed state | ✅ | ⚠️ basic | ❌ | ✅ streaming generators | ✅ purpose-built for LLM demos |
| **NiceGUI** | 15.8k | 3.12.1 (May 26) | ✅ | ✅ websockets/socket.io native | ✅ Plotly/Matplotlib/ECharts | ✅ Quasar component library | ✅ | ✅ | ✅ | ❌ | ✅ async, timers (10ms), bg tasks | ⚠️ |
| **Voilà** | 5.9k | 0.5.12 (Apr 26) | ✅ | ⚠️ ipywidgets only | ⚠️ ipywidget-compatible only | ⚠️ ipywidgets only | ❌ | ❌ single notebook | ✅ | ✅✅ **renders the .ipynb directly** | ❌ blocks the kernel | ❌ |
| **Textual (TUI)** | ~28k | active | ✅ (no browser needed) | ✅ asyncio | ❌ ASCII charts via plotext | ✅ rich widget set | ✅ | ✅ screens | ✅ | ❌ | ✅ asyncio-native | ⚠️ |
| **FastAPI + HTMX** | n/a | n/a | ✅ | ✅ SSE/websockets | ⚠️ inject Plotly HTML | ⚠️ hand-rolled forms | ❌ DIY | ✅ | ✅ | ❌ rewrite | ✅ async/await | ⚠️ |
| **Jupyter + ipywidgets** | baseline | — | ✅ | ⚠️ widget callbacks | ✅ via widget bridges | ✅ | ⚠️ kernel-bound | ❌ | ✅ | ✅✅ **already in use** | ❌ blocks the kernel | ⚠️ |

Notes on color coding: "real-time updates" specifically asks whether the framework natively pushes server-side changes to the browser without polling or full reruns — critical for "monitor in-progress runs live". "Long tasks" asks whether a 30s Layer 1 run can stream progress while the UI remains responsive.

---

## 2. Top 3 recommendations (against the actual workload)

### #1 — Marimo, for the daily review surface and ad-hoc research

Why it fits Phase G:

- **Notebook is the app.** Marimo files are pure `.py` (git-friendly, diffable, testable with PyTest, runnable as scripts). The same file runs as an editable notebook *and* deploys as a standalone web app (`marimo run app.py`). This collapses requirements 3 (ranked review), 5 (Jupyter-style ad-hoc), and part of 6 (health checks) into one artifact.
- **Reactive DAG matches the data model.** Layer 4 outputs a ranked-candidate parquet file; a marimo cell that loads it auto-refreshes downstream charts/tables when I pick a different ticker or tweak a slider. This is the same execution model as Observable/Excel, which is exactly what a per-ticker drilldown wants.
- **Built-in SQL.** Storage is SQLite (per the architecture spec); marimo's native SQL cells query SQLite directly with reactive parameters, no SQLAlchemy boilerplate.
- **AI-native posture.** "marimo pair" drops Claude-style agents inside running notebooks — aligns with the agent-desk layer (10 agents) and existing Claude API spend.
- **Live activity.** v0.23.6 shipped May 11, 2026; 21.1k stars; 6,108 commits — solidly past "early-adopter risk."

Cons / things to verify:

- Live monitoring of a long-running Layer-1 run *while the UI stays interactive* needs `mo.background_task` or an async cell — works, but the pattern is less battle-tested than Streamlit's `st.status`.
- `notebooks/layer1_control.ipynb` is not a drop-in port — it has to be rewritten as a `.py` marimo file. (Marimo provides `marimo convert notebook.ipynb` but the reactive model differs from imperative Jupyter; expect ~half a day to port + clean.)
- Smaller ecosystem of third-party components than Streamlit (but bridges to ipywidgets exist).

### #2 — Streamlit, for ops/monitoring/approval (the "trigger and watch" surface)

Why it fits Phase G:

- **Highest velocity for the operational dashboard.** Trigger buttons → progress bars → status containers → ranked table → approve-trade button is the canonical Streamlit pattern. Fragments (1.37+) with `run_every=N` make live source-status panels trivial without rewriting the whole page on every tick.
- **Largest plotting catalog.** Plotly, Altair, Bokeh, Matplotlib, native st.line_chart — pick per-chart, no lock-in.
- **`st.session_state` + multi-page apps** cover the auth-free localhost case cleanly. No real auth needed.
- **Maturity.** 44.7k stars, v1.57.0, Snowflake-backed; least likely framework on this list to be abandoned.
- **streamlit-jupyter** lets me prototype Streamlit cells inside a notebook — eases the transition from `layer1_control.ipynb` if a Streamlit-only path is chosen.

Cons:

- Rerun-from-top model is wrong for heavy ad-hoc analysis that wants cell-level state — that's where #1 wins.
- Long tasks aren't free: a 30s Layer-1 run on the main thread freezes the UI. Workarounds: fragments + `st.status` + writing progress to a sidecar file that a `run_every=2` fragment polls. Works but more plumbing than marimo's reactive cells.
- Multithreading "not officially supported" in app code — for true concurrent live monitoring, a fragment polling a file or Redis is the documented escape hatch.

### #3 — Panel, only as a single tool to span the whole platform

Why it might fit:

- **Truly reactive with proper concurrency.** Unlike Streamlit's rerun model, Panel re-runs only the widget→callback graph; with async functions it scales to the kind of bidirectional monitoring dashboard the v2 spec §4 implies. Anaconda explicitly markets it for "trading floor monitoring screens."
- **Native PyViz integration.** For HoloViews/Datashader on a historical IV surface or 5-year price tape, Panel is the only tool here where that's free.
- **Pipeline primitive** (`pn.pipeline.Pipeline`) maps directly to the 6-layer architecture as a UI metaphor.

Cons (why it's #3 not #1):

- Steeper learning curve than Streamlit or marimo. More boilerplate, more "which API do I use" (param, reactive, callbacks, pn.rx, pn.bind).
- 5.7k stars (~13% of Streamlit's), smaller community, fewer Stack Overflow answers.
- No notebook-as-app story — the existing `layer1_control.ipynb` would be a manual port.

---

## 3. Reference projects — what people actually ship for trading dashboards in 2026

**Streamlit + VectorBT/QuantStats is the dominant pattern** for retail-grade strategy backtest dashboards:

- [marketcalls/VectorBT-Streamlit](https://github.com/marketcalls/VectorBT-Streamlit) — VectorBT backtest UI; pattern: parameter sidebar → backtest run → QuantStats tearsheet embedded as HTML.
- [marketcalls/vectorbt-backtesting-skills](https://github.com/marketcalls/vectorbt-backtesting-skills) (updated March 2026) — 12 strategy templates with QuantStats integration; close analog to the per-ticker drilldown pattern.
- [mheloy/VectorBT](https://github.com/mheloy/VectorBT) — 8-page Streamlit sidebar nav over multi-year 5-min data. Demonstrates Streamlit can scale to non-trivial dashboards.

**IBKR-specific Streamlit projects** confirm Layer 5 integration is well-trodden ground:

- Multiple Streamlit dashboards exist for IBKR P&L visualization (updated late 2025).
- [mcf-long-short/ibkr-options-volatility-trading](https://github.com/mcf-long-short/ibkr-options-volatility-trading) — long/short straddle scanner over IBKR's TWS feed; closest in spirit to the Layer 3 work.

**Bayesian + Streamlit reference:**

- [aahammer/bayesian-streamlit-dashboards](https://github.com/aahammer/bayesian-streamlit-dashboards) — explicit PyMC + Streamlit pattern (OKR funnel, A/B test). Confirms credible-interval visualization in Streamlit works; the same pattern transfers to NumPyro posteriors.
- [pymc-examples](https://github.com/pymc-devs/pymc-examples) and the BlackJAX/NumPyro samplers PyMC now supports — the NumPyro variational + MCMC choice is mainstream, not exotic.

**Trading + Textual TUI** has a credible 2026 entrant:

- [stocksTUI](https://pypi.org/project/stocksTUI/) (Jan 2026) — Textual-based terminal with live tickers, watchlists, **and an options chain view with Greeks**. Proves Textual is viable for a narrower "always-on monitor" surface.

**OpenBB's own UI choices** are instructive but not directly reusable. OpenBB Workspace is a custom React-based widget composition system over a backend "Open Data Platform"; AI agents are first-class. Their bet: a widget-graph dashboard, not a Streamlit-style script. The architectural insight to steal — **separate data layer from UI layer, expose dashboards via a backend** — is already what the architecture spec does (Layers 1-4 don't know about UI). What I should *not* copy: OpenBB's full custom-React stack is overkill for a single user.

**Direct "Jupyter + Bayesian + Streamlit" analog:** none found in a single repo. The closest precedent is the PyMC/NumPyro tutorial notebooks paired with bespoke Streamlit wrappers (e.g., bayesian-streamlit-dashboards). This is a small gap I will fill, not a solved problem.

---

## 4. Recommended path for Phase G

**Hybrid, not single-tool.** Earned by the use cases differing on the rerun-vs-reactive axis.

| Surface | Recommendation | Why |
|---|---|---|
| **Daily review (top candidates + drilldown)** | Marimo notebook deployed via `marimo run` | Per-ticker drilldown wants reactive state — pick a ticker, all charts/tables/posterior plots update. Same `.py` file is editable for ad-hoc exploration. |
| **Ad-hoc research / what-if** | Same Marimo notebook in edit mode | Replaces `layer1_control.ipynb` permanently. Git-tracked. AI-native via marimo pair. |
| **Run triggering + live monitoring (Layer 1/2/4)** | Streamlit app | Best ergonomics for "click button → progress bar → status panel → done"; fragments + status containers; smallest code surface per feature. |
| **Trade approval surface (post-Layer-4 → Layer-5)** | Same Streamlit app, a "Pending picks" page | Form-style review of A/B/C grade trades; checkbox → "Send to IBKR" button. Auth-free localhost is fine for a single-user deployment. |
| **Open positions + live P&L (post-Layer-5)** | Streamlit page, with a fragment on `run_every=5` polling the position state file | Layer 5 writes `open_positions` to SQLite; Streamlit fragment reads and re-renders the table. |
| **Daily ranked-trades brief** | Already produced as `top_candidates.md` by Layer 4 (architecture spec §4) | This is the lowest-tech high-value surface. Render it on the Streamlit landing page via `st.markdown(open(latest_md).read())`. No new UI work. |
| **Health checks / cost meter** | Streamlit sidebar, polling `source_run_log` and Anthropic usage endpoint | Same fragment pattern. |
| **Terminal-resident "what's the system doing right now"** | OPTIONAL Textual TUI later | Only if an always-on small window is wanted. Not Phase G P1. |

**Why hybrid is right, not lazy:**

1. The operational surface (trigger, monitor, approve) wants imperative top-to-bottom code; **Streamlit's rerun model is a feature there, not a bug** — the whole page-handler reads top-to-bottom and the behavior is obvious. Marimo's DAG fights you on imperative button-driven flows.
2. The analytical surface (drilldown, what-if, posterior inspection) wants reactive cells and cell-level state; **Streamlit's rerun model is a bug there** — every slider tweak re-runs the whole script, defeating the point. Marimo's reactivity is exactly right.
3. The daily review is a **Markdown file already** (per architecture spec §4 Layer 4 output `top_candidates.md`). Treat it as a first-class artifact. The interface just renders it. This is the highest-leverage decision in this whole document.

**Shared infrastructure (build once, both UIs consume):**

- All long-running runs (`run_layer1.py`, `run_layer2.py`, `run_layer4.py`) become CLI entrypoints that write to SQLite + emit progress to a `runs/<run_id>/progress.jsonl` sidecar.
- Streamlit fragments tail that file. Marimo cells read the final SQLite state.
- No framework owns the "run" — it's just `subprocess.Popen(["python", "-m", "src.layer1_universe", "--run-id", ...])`. Both UIs poll the same artifacts.
- This decoupling is what the architecture spec's reproducibility invariant (run-id traceability) implies anyway.

**Phase G build order:**

1. **G.1** — Markdown `top_candidates.md` rendered in any minimal Streamlit page. Ship in a day.
2. **G.2** — Streamlit ops shell: trigger buttons + progress fragments + open-positions table. ~1 week.
3. **G.3** — Marimo migration of `layer1_control.ipynb` → reactive `.py` file. ~3 days.
4. **G.4** — Marimo drilldown notebook (per-ticker thesis + drilldown views). ~1 week.
5. **G.5** (optional) — Textual TUI for always-on monitor.

---

## 5. Anti-recommendations (do NOT use, and why)

- **Voilà.** Renders the existing `.ipynb` directly — tempting — but kernel-blocking on long cells, no multi-page, ipywidgets-only inputs, no state across runs. It's a viewer, not an interface layer. Use Marimo instead — same "notebook is the app" promise without the kernel deadlock.
- **Plain Jupyter + ipywidgets.** The current baseline. Fine for a control surface today; a dead end for Phase G because (a) widget callbacks block the kernel during a 30s Layer-1 run, (b) no multi-page story, (c) no good way to ship a daily-review surface that isn't "open Jupyter, then run this cell."
- **Dash.** The callback-graph model is more verbose than a single-user, low-traffic context warrants. Strongest case is enterprise multi-user analytics, which doesn't apply. Skip.
- **Reflex.** Compiles to Next.js — pulls in a Node toolchain, which a local-only desktop install doesn't need. Beautiful for SaaS web apps; wrong shape for a personal trading platform.
- **Gradio.** Optimized for ML model demos (input → predict → output), not multi-page ops dashboards. The DataFrame component exists but it's not the happy path. The HF-native ecosystem is a benefit this project doesn't capture.
- **Mesop.** Promising and Google-backed, but explicitly "not an officially supported Google product." For LLM-demo UIs only; doesn't out-feature marimo for notebook-flavored workflows. Park and revisit if marimo stagnates.
- **NiceGUI.** Strong framework, healthy (15.8k stars), but it's a *general* web-UI framework — building the same dashboard from lower-level primitives than Streamlit or marimo offer for free. Right answer for someone with a robotics or IoT app. Wrong answer for a data-app shape.
- **Solara.** Technically the most elegant ipywidget-native reactive framework, but 2.2k stars and a small contributor pool. Marimo has eaten the same niche with more momentum.
- **FastAPI + HTMX.** Maximally flexible and maximally hand-rolled. Phase G would be spent writing HTML templates instead of analyzing trades. Only justifiable as a stack-learning exercise.

---

## 6. Open questions to decide before Phase G starts

1. **Single-tool dogma vs. hybrid pragmatism.** The hybrid recommendation above adds a second framework (marimo + Streamlit). Worth deciding whether the cognitive cost of two tools is worth the use-case fit, or whether to single-tool on Streamlit and accept worse drilldown ergonomics. (My read: hybrid wins.)
2. **Does the daily review need *any* live UI?** The Layer 4 output is already a Markdown file with full reasoning. If I read it in VS Code or render via `marimo` or `gh markdown-preview`, Phase G can be 30% smaller. The interactive drilldown is for the cases where the Markdown surfaces something worth digging into — define those moments precisely before building UI for them.
3. **Real-time tick subscription scope.** Layer 5's "live P&L" can mean (a) IBKR pushes tick → UI updates in <1s, or (b) UI polls IBKR every 5s. (b) is dramatically cheaper to build and probably enough for the position sizes involved. Confirm.
4. **Trade-approval mutation path.** The architecture spec (§4 Layer 5) describes operator-approved trades — what's the approval-write mechanism? A signed row in a `pending_orders` table that Layer 5 polls? A direct Streamlit-button-→-IBKR call? The latter couples UI tightly to broker; the former preserves the layer separation. Recommend the former.
5. **Cost meter location.** Anthropic usage endpoint is polled where? In `agents/orchestrator.py` (per architecture spec §14) — good. But the UI needs read access. Confirm the meter writes to SQLite or a sidecar file, not a closure inside the orchestrator process.
6. **Theme/branding effort.** Zero, ideally. None of these frameworks justify time spent on theming for a single user. Pick the framework's default and stop.

---

## 7. Frameworks where signal was thin

- **Dash** — confirmed actively maintained but I didn't pull exact star/release for the v3 line; comparison stands on architecture, not metrics.
- **Reflex** — repo is public and active (28.4k stars, v0.9.2 May 2026) but most "Reflex 2026 trading dashboard" search results are speculative/marketing blogs. Real-world finance use is thinner than the star count suggests.
- **Mesop** — only 6.5k stars, "not officially supported" by Google despite Google-internal use; signal on third-party finance dashboards is essentially zero. Treat as experimental.
- **Solara** — only 2.2k stars; usage signal is thin outside Jupyter-heavy data-science orgs. Quality is high but ecosystem risk is real.

None of these gaps change the top-3 ranking.

---

## Appendix — sources

- [Streamlit GitHub](https://github.com/streamlit/streamlit) · [2026 release notes](https://docs.streamlit.io/develop/quick-reference/release-notes/2026) · [Fragments docs](https://docs.streamlit.io/develop/concepts/architecture/fragments)
- [Marimo GitHub](https://github.com/marimo-team/marimo) · [docs](https://docs.marimo.io/) · [marimo pair blog](https://marimo.io/blog/marimo-pair)
- [Panel GitHub](https://github.com/holoviz/panel) · [Panel monitoring dashboard tutorial](https://panel.holoviz.org/tutorials/basic/build_monitoring_dashboard.html) · [Streamlit comparison](https://panel.holoviz.org/explanation/comparisons/compare_streamlit.html)
- [Reflex GitHub](https://github.com/reflex-dev/reflex) · [Reflex blog: Streamlit vs Dash 2026](https://reflex.dev/blog/streamlit-vs-dash-python-dashboards/)
- [Gradio dashboard guide](https://www.gradio.app/guides/using-gradio-for-tabular-workflows) · [BigQuery dashboard guide](https://www.gradio.app/guides/creating-a-dashboard-from-bigquery-data)
- [Solara GitHub](https://github.com/widgetti/solara) · [solara.dev](https://solara.dev/)
- [Mesop GitHub](https://github.com/mesop-dev/mesop) · [mesop docs](https://mesop-dev.github.io/mesop/)
- [NiceGUI GitHub](https://github.com/zauberzeug/nicegui) · [NiceGUI 3.0 Talk Python podcast](https://talkpython.fm/episodes/show/525/nicegui-goes-3.0)
- [Voilà GitHub](https://github.com/voila-dashboards/voila) · [voila-gallery](https://voila-gallery.org/)
- [Textual GitHub](https://github.com/textualize/textual) · [stocksTUI on PyPI](https://pypi.org/project/stocksTUI/)
- [marketcalls/VectorBT-Streamlit](https://github.com/marketcalls/VectorBT-Streamlit) · [marketcalls/vectorbt-backtesting-skills](https://github.com/marketcalls/vectorbt-backtesting-skills)
- [aahammer/bayesian-streamlit-dashboards](https://github.com/aahammer/bayesian-streamlit-dashboards) · [pymc-examples](https://github.com/pymc-devs/pymc-examples)
- [OpenBB platform GitHub](https://github.com/OpenBB-finance/OpenBB) · [OpenBB Workspace docs](https://docs.openbb.co/workspace) · [OpenBB architecture blog](https://openbb.co/blog/exploring-the-architecture-behind-the-openbb-platform/)
- [streamlit-jupyter](https://github.com/ddobrinskiy/streamlit-jupyter) · [strimlitbook](https://github.com/BexTuychiev/strimlitbook)
- [mcf-long-short/ibkr-options-volatility-trading](https://github.com/mcf-long-short/ibkr-options-volatility-trading)
