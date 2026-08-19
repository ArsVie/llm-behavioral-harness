---
okf_version: "0.1"
---

# llm-behavioral-harness — knowledge bundle

Stochastic behavioral harness for LLMs: a mood engine (circadian rhythm + hormonal cycle + event memory) translated into observable behavior, with conversation initiative and stochastic message timing. OKF v0.1 bundle. Update history in [log](log.md).

## Root concepts

* [DESIGN](DESIGN.md) - system design spec: stochastic engine (beta-binomial mood in logit space, ~28-day hormonal cycle, circadian energy channel, Weibull-hazard message timing), behavioral actuation, initiative, memory, judge feedback loop, SQLite data model, initial parameters.
* [CONVENTIONS](CONVENTIONS.md) - repository operating rules: working environment (native WSL), git and Conventional Commits, frozen files, wave ownership, code conventions, tests, experiments.
* [BACKLOG](BACKLOG.md) - running list of user asks (verbatim + summary) and their status; the work index.
* [engine_simulation/README](engine_simulation/README.md) - simulation gallery: 30-day engine effects (shared seed 3001), per-scenario overrides, and tuning sweeps (B, k, ρ).

## Subdirectories

* [research](research/) - phase research and design reevaluations (Phase −1 synthesis, product review, initiative, regulatory deferred).
* [plans](plans/) - task plans and phase iterations (e.g. [e2e vertical slice](plans/e2e-vertical-slice.md)).
* [results](results/) - experiment reports, plots, and run artifacts (w31–w35, behavior showcase, phase-1 report).
* [docs](docs/) - design notes, specs, and contracts — the architecture (indexed below).

## Document index (docs/ · plans/ · results/)

Design & specs:
* [DESIGN note: cognition principle](docs/design-note-cognition-principle-2026-08-15.md) - engine owns timing, the model owns the decision (the causal claim).
* [DESIGN note: AFK / presence](docs/design-note-afk-presence-2026-08-16.md) - armed post-proactive double-text + no-goodbye detection; away-as-presence.
* [spec: context / events / time](docs/spec-context-events-time-2026-08-15.md) - S1–S6 (real time on events, time-aware agenda, conversation lifecycle, context assembly, event reasoning, templates).
* [context flow](docs/context-flow-2026-08-14.md) - how context is assembled per turn.
* [availability-negotiation contract](docs/availability-negotiation-contract.md) - inform-then-decide event negotiation; SHORT_AFK / away thresholds.
* [architecture overview](docs/architecture-overview.md) - living reference: what the system is and how it's built (engine → behavior → assembly → LLM; lifecycle; runtime).

Plans:
* [lifecycle (away≠close), cache, OpenRouter](plans/plan-lifecycle-away-checkpoint-2026-08-17.md) - WS-A..E; the decided lifecycle work.
* [UX / tokens / spend](plans/plan-ux-tokens-spend-2026-08-16.md) - commands/debounce, delimiter spike, token-lane split, spend accounting.
* [unblockers / foundations (wave 1)](plans/plan-unblockers-foundations-2026-08-15.md) - real-time substrate, time prompt, state card, signature harness (DONE).
* [handoff — iteration 2](plans/handoff-2026-08-09-iteration2.md) - iteration-2 handoff (handoffs live with plans).

Spikes & experiments ([registry](results/spikes-registry.md)):
* [affect codebook — spike 1](results/exp-affect-codebook-pipeline-2026-08-15.md) - value→words pipeline (NO-GO).
* [affect codebook — spike 2](results/exp-affect-codebook-spike2-2026-08-16.md) - behavioral re-gate (SHELVED).
* [internal-thoughts marker](results/exp-internal-thoughts-spike-2026-08-16.md) - analysis vs immersion markers.
* [DeepSeek-harness alpha — brief](results/exp-deepseek-alpha-extraction-2026-08-16.md) → [memo](results/memo-deepseek-alpha-2026-08-16.md) - transferable patterns (30 ranked).
* [mid-reply folding discovery](results/ws-a-folding-discovery.md) - WS-A folding separability note.

Status & reviews:
* [state report](results/state-report-2026-08-16.md) - consolidated snapshot of spikes, results, decisions.
* [architecture review (2026-08-15)](results/architecture-review-2026-08-15.md) - measured results, known-issues status (S1–S6), risks.
