# Building this project with Claude Code

This directory is the engineering receipts for the project. Every spec under `specs/` and every plan under `plans/` was used to build a feature that lives in `src/`. I am preserving them in the public repo for two reasons.

First, the design decisions are auditable. If you want to know why the volatility estimator is Yang-Zhang and not Garman-Klass, or why the data layer has watermarks instead of full re-pulls, you can read the spec that was written before the code was written. The choice and the alternatives that were rejected are both on the page. Second, the workflow itself is part of what I am demonstrating with this project. The code quality is a function of the process that produced it, and the process was deliberate. If you skip the workflow, you have to take the code at face value. If you read the workflow, you can see the controls that were in place while it was being written.

## The loop

```
   idea
     |
     v
 brainstorm  ----> spec  ----> plan  ----> sub-agents  ----> review  ----> ship
   (skill)        (file)      (file)      (parallel)        (diff)        (CI)
     |              |           |             |               |             |
  purpose,      design       file-by-file   independent     I read         pytest
  constraints,  alternatives, steps,        units of        every diff,    runs on
  alternatives  tradeoffs,   acceptance     work, each      run tests,     every
                decision     criteria,      with its own    push back      push
                             tests-first    plan + tests    where needed
```

Each step does one thing:

- **Brainstorm.** Before any file is touched, the `superpowers:brainstorming` skill forces me to state the purpose, the constraints, what success looks like, and at least two or three design alternatives. The output is a short document that I then either commit as a spec or throw away.
- **Spec.** A spec under `specs/` is the durable record of a design decision. It is written in human prose, not in code, and it answers "what are we building and why, and what did we reject."
- **Plan.** A plan under `plans/` decomposes the spec into file-by-file implementation steps with acceptance criteria and a test-first ordering. Plans are intended to be executable: a sub-agent can pick one up and work through it without re-litigating the design.
- **Sub-agents.** Independent plans are dispatched to parallel sub-agents. Each one operates on its own scope, writes tests first, then implementation, and reports back. I review the diff.
- **Review.** I read every diff. I run the tests locally. I push back when something is wrong. The `everything-claude-code:code-review` skill helps with the mechanical parts of this.
- **Ship.** A push triggers CI, which runs the full pytest suite. Nothing merges without green tests.

## Why this works

**Forced design-before-code.** The brainstorming step exists specifically to prevent the "I'll refactor it later" trap. Before any file is opened for writing, I have to write down what the thing is supposed to do, the constraints it has to live inside, and the alternatives I considered. When the alternatives are written down, it is much harder to drift into the first design that comes to mind and call it good. The spec for the multi-source data adapter (`specs/2026-05-21-multi-source-data-adapter-design.md`) is the clearest example: it considered a single-source design, a fan-out-with-merger design, and the router design that eventually shipped, and it explains why the router won. The plan decomposition followed from that choice, not the other way around.

**Parallelism via sub-agents.** Phase A3 was a hardening pass on the Layer 1 data ingestion. Once the spec was written, the work decomposed into ten roughly-independent units: watermarks, Finviz/Yahoo, EDGAR XBRL, EDGAR insider, FRED+FINRA, StockAnalysis, OpenBB router, the rate-limited orchestrator, news activity sub-signals, the Loughran-McDonald tone scorer, and the Yang-Zhang + composite signal. Each one got its own plan file (`plans/2026-05-21-phase-a3-sub01` through `sub10`, plus the `sub07_5` orchestrator that emerged mid-flight). Those plans were dispatched in parallel to sub-agents using the `superpowers:dispatching-parallel-agents` skill. The serial estimate for Phase A3 was about two weeks of focused work. The parallel build took two days of dispatch-and-review on my end.

**Test-first discipline.** Every adapter has a contract test that was written before the implementation. The `superpowers:test-driven-development` skill enforces this ordering: the plan specifies tests first, the sub-agent writes the tests first, and the implementation is only allowed to proceed once the tests fail in the expected way. The `tests/` tree mirrors `src/` one-to-one across more than sixty test files. CI runs the whole suite on every push. This is unglamorous and it is the single biggest reason I trust the code to behave the way the spec says it does.

**Audit trail.** Every design decision lives in a spec. Every implementation step lives in a plan. If I come back to this repo in six months and want to know why a particular function exists or why a particular choice was made, the answer is in this directory. This is the most underrated benefit of the workflow. The cost of writing a spec is small; the cost of not having a spec when you need one is large. The forecast layer v2 overhaul (`plans/2026-05-23-forecast-layer-v2-overhaul.md`) is a good example: v1 shipped, I evaluated it, found the gaps, and the v2 plan explicitly carries forward what survived and explains what was replaced. There is no archaeology to do.

**My role vs Claude's role.** I wrote the specs. I picked the methodology (Yang-Zhang for volatility, Loughran-McDonald for tone, the watermark-based incremental ingestion, the conformal-edge plan, the exact factor names I wanted). I debated tradeoffs in the brainstorms. I reviewed every diff before it merged. I ran every test locally. I made the call on what was "good enough" for each iteration and what needed another pass. Claude did the typing under strict test-first discipline and produced first drafts that I edited rather than wrote from scratch. The combination was faster than either alone would have been, and it produced code I can defend line-by-line because I had to read every line to merge it.

## Reading order

If you want to skim the engineering process rather than the code, here is the order I would read in.

1. **`specs/2026-05-21-multi-source-data-adapter-design.md`** — the foundational spec. Read this first. It triggered the ten-sub-plan decomposition and it shows what a spec looks like when it actually constrains the design.

2. **`specs/2026-05-21-phase-a3-layer1-hardening-design.md`** — the Layer 1 hardening design. This is the spec that converted the original prototype into a tested system: watermarks for incremental ingest, schemas for every fetcher, validation gates between layers. If you are wondering how the test infrastructure ended up so thorough, this is where the requirement came from.

3. **`plans/2026-05-21-phase-a3-sub07_5-rate-limited-orchestrator.md`** — a representative implementation plan. Explicit acceptance criteria, file-by-file steps, test-first ordering. This plan emerged mid-flight when it became clear the original sub07 split needed an orchestrator layer; that is itself worth seeing.

4. **`plans/2026-05-21-phase-a3-sub02-finviz-yahoo-fetch.md`** — a tightly-scoped data adapter sub-plan. Read this if you want to see what an "atomic" plan looks like. (Or substitute `plans/2026-05-21-phase-a3-sub03-edgar-xbrl-fundamentals.md` — both are good examples of the same shape.)

5. **`plans/2026-05-23-phase-b-and-forecast-layer.md`** — the largest plan. Phase B plus the forecast layer end-to-end. This is the one to read if you want to see how a multi-component build is sequenced.

6. **`plans/2026-05-23-forecast-layer-v2-overhaul.md`** — the iteration plan after v1 shipped and was evaluated. Shows what changes when you build, ship, measure, and then rebuild with the measurement in hand.

If you read those six, you have seen the workflow at its smallest atomic unit, at its largest, and across an iteration cycle.

## What I would change next time

The workflow worked. It is not perfect. The improvements I would make on the next project:

- **Smaller sub-plans.** A few of the Phase A3 sub-plans grew past two thousand lines. When that happens the plan stops being a single atomic unit of work and starts being a small project, which makes the dispatch-and-review pattern weaker. Next time I will split anything that hits about a thousand lines.

- **Earlier cross-layer integration tests.** The adapter contract tests were good. The cross-layer integration tests — does Layer 1 feeding Layer 2 actually produce the shapes Layer 2 expects — came later than they should have. I had to backfill some of those tests after I found schema drift, which is exactly the failure mode the contract tests were supposed to catch.

- **More explicit "what NOT to do" in specs.** The brainstorm gate catches most of these, but a couple of times a sub-agent picked a reasonable-but-wrong path that the spec did not explicitly rule out. A short "non-goals" or "explicitly rejected" section in every spec would have prevented those.

- **Cleaner separation of design spec and implementation plan.** Sometimes they bled together. A spec that contains file paths is not really a spec anymore. A plan that re-litigates design decisions is not really a plan anymore. Tightening that boundary would make both documents more useful in isolation.

- **Versioning of specs.** When a spec was amended mid-implementation it was hard to trace what changed. Next time I will treat spec amendments like code changes: a dated diff or an explicit `v2` file, rather than an in-place edit.

## The tooling

- **Claude Code** — Anthropic's CLI for Claude. The harness that runs the loop.
- **`superpowers` plugin** — the skills that enforce the workflow shape: `brainstorming`, `writing-plans`, `executing-plans`, `requesting-code-review`, `dispatching-parallel-agents`, `test-driven-development`. Each one is a small, focused skill that does one thing well; together they are the workflow.
- **`everything-claude-code` plugin** — `code-review` for the mechanical review pass, plus the agent harness for parallel dispatch and the opensource pipeline conventions.
- **Python toolchain** — `pytest` for the test suite, AST-based parse checks for cheap structural validation, `ruff` and `black` for style. CI runs the full suite on every push.

The tools are the tools. The workflow is what makes the tools produce code I would put my name on.
