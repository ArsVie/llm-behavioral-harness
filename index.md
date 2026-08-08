---
okf_version: "0.1"
---

# llm-behavioral-harness — knowledge bundle

Stochastic behavioral harness for LLMs: a mood engine (circadian rhythm + hormonal cycle + event memory) translated into observable behavior, with conversation initiative and stochastic message timing. OKF v0.1 bundle. Update history in [log](log.md).

## Root concepts

* [DESIGN](DESIGN.md) - system design spec: stochastic engine (beta-binomial mood in logit space, ~28-day hormonal cycle, circadian energy channel, Weibull-hazard message timing), behavioral actuation, initiative, memory, judge feedback loop, SQLite data model, initial parameters.
* [CONVENTIONS](CONVENTIONS.md) - repository operating rules: working environment (native WSL), git and Conventional Commits, frozen files, wave ownership, code conventions, tests, experiments.
* [engine_simulation/README](engine_simulation/README.md) - simulation gallery: 30-day engine effects (shared seed 3001), per-scenario overrides, and tuning sweeps (B, k, ρ).

## Subdirectories

* [research](research/) - phase research and design reevaluations (Phase −1 synthesis, product review, initiative, regulatory deferred).
* [plans](plans/) - task plans and phase iterations (e.g. [e2e vertical slice](plans/e2e-vertical-slice.md)).
* [results](results/) - experiment reports, plots, and run artifacts (w31–w35, behavior showcase, phase-1 report).
