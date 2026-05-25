# Design specs and implementation plans

This directory contains the design specs and implementation plans I wrote while building this project. They are preserved as engineering receipts. Anyone who wants to understand why a particular module looks the way it does can read the spec that was written before the code.

The directory name is a legacy of how the project was scaffolded; the content is design documentation.

## Why these documents exist

Every non-trivial component went through the same loop:

1. State the purpose, constraints, and success criteria for the component in plain English.
2. Write a spec under `specs/` that names the design alternatives, the trade-offs, and the chosen approach.
3. Write a plan under `plans/` that decomposes the spec into file-by-file steps with acceptance criteria and a test-first ordering.
4. Implement against the plan, with tests written before the code they exercise.
5. Review the diff, run the full test suite, then ship.

The cost of writing a spec before touching code is small. The cost of not having a spec when you come back to a module six months later is large. The discipline is not glamorous, and the documents are sometimes verbose; they exist because they make the rest of the work easier to defend and easier to extend.

## What's in here

- `specs/` — design specifications. Each one answers: what is this component, what are the design alternatives, what was chosen, and why.
- `plans/` — implementation plans. Each one decomposes a spec into atomic steps, with explicit acceptance criteria and a test-first ordering.

## Suggested reading order

If you want to skim the engineering process rather than the code, this is the order I would read in.

1. [`specs/2026-05-21-multi-source-data-adapter-design.md`](specs/2026-05-21-multi-source-data-adapter-design.md) — the foundational spec for the data layer. Shows what a spec looks like when it actually constrains the design.
2. [`specs/2026-05-21-phase-a3-layer1-hardening-design.md`](specs/2026-05-21-phase-a3-layer1-hardening-design.md) — the Layer 1 hardening design. Watermarks for incremental ingest, schemas for every fetcher, validation gates between layers. This is where the test infrastructure requirements came from.
3. [`plans/2026-05-21-phase-a3-sub07_5-rate-limited-orchestrator.md`](plans/2026-05-21-phase-a3-sub07_5-rate-limited-orchestrator.md) — a representative implementation plan. Explicit acceptance criteria, file-by-file steps, test-first ordering. This plan emerged mid-flight when it became clear an orchestrator layer was needed; that itself is worth seeing.
4. [`plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md`](plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md) — a tightly-scoped data adapter sub-plan. Read this to see what an atomic plan looks like.
5. [`plans/2026-05-23-phase-b-and-forecast-layer.md`](plans/2026-05-23-phase-b-and-forecast-layer.md) — the largest plan. Layer 2 plus the forecast layer end-to-end. Read this to see how a multi-component build is sequenced.
6. [`plans/2026-05-23-forecast-layer-v2-overhaul.md`](plans/2026-05-23-forecast-layer-v2-overhaul.md) — the iteration plan after v1 shipped and was evaluated. Shows what changes when you build, ship, measure, and then rebuild with the measurement in hand.

If you read those six, you have seen the workflow at its smallest atomic unit, at its largest, and across an iteration cycle.

## What I would change next time

The workflow worked. It is not perfect. Improvements I would make on the next project:

- **Smaller sub-plans.** A few of the Phase A3 sub-plans grew past two thousand lines. When a plan stops being a single atomic unit of work and starts being a small project, the dispatch-and-review pattern weakens. Next time anything that hits about a thousand lines gets split.

- **Earlier cross-layer integration tests.** The adapter contract tests were good. The cross-layer integration tests — does Layer 1 feeding Layer 2 produce the shapes Layer 2 expects — came later than they should have. I had to backfill some of them after I found schema drift, which is exactly the failure mode the contract tests were supposed to catch.

- **Explicit "non-goals" sections in every spec.** Sometimes a reasonable-but-wrong path was taken that the spec did not explicitly rule out. A short non-goals or rejected-alternatives section in every spec prevents that.

- **Cleaner separation of design spec and implementation plan.** Sometimes they bled together. A spec that contains file paths is not really a spec anymore. A plan that re-litigates design decisions is not really a plan anymore. Tightening that boundary makes both documents more useful in isolation.

- **Versioning of specs.** When a spec was amended mid-implementation it was hard to trace what changed. Next time spec amendments get treated like code changes: a dated diff or an explicit `v2` file, rather than an in-place edit.
