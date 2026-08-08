---
type: research-note
title: Critique of the objective and of the planned implementation
description: "Thesis and effort-allocation review of the POC — falsifiability, model-agnostic claim, actuators, synthetic user, session semantics, and an impact/effort ranking."
tags: [design, critique, objective, ablation, actuators, ranking]
timestamp: 2026-07-01
---

# Critique of the objective and of the planned implementation

**Date:** 2026-07-01
**Scope:** objective and plan, excluding user-wellbeing considerations. Complements [05-reevaluacion-diseno.md](05-reevaluacion-diseno.md) (which reviewed the math); this one reviews the **thesis and the effort allocation**.

---

## At the objective level

### 1. The POC, as specified, cannot falsify its own hypothesis
The implicit hypothesis is: *injecting stochastic state produces perceptibly more human/attractive behavior*. All acceptance criteria validate **plumbing** (traces, plots, μ moves in the right direction); none validates the **effect**. The control experiment is missing: a blind A/B comparison between harness-on and a flat persona (same LLM, same persona prompt, no engine). The inspiration paper reported non-significant effects — there is a real risk that the whole machinery is inaudible through the model. Without ablation, you can build everything and learn nothing. **Blind ablation should be THE Phase 7 criterion**, not traceability.

### 2. "Model-agnostic" is an overclaim
Agnostic to the **API**, yes (OpenAI-compatible). But the persona pressure needed varies by model (a small local model and a frontier RLHF-ed one need very different state briefs to produce the same tone). In practice there will be per-model prompt profiles. Claiming identical-behavior portability is false; the claim should be scoped.

### 3. The ~28-day cycle is unobservable in the POC window
In live use (real time), a 1–2 week evaluation samples half a phase. Its perceptible effect during the POC is ≈0; its weight is conceptual fidelity, not observable behavior. Keep it (it is cheap — the same sinusoid), but: for demos use `L≈7` or an accelerated clock, and give it no evaluative weight. The circadian and day-to-day mood carry perception.

---

## At the implementation level

### 4. Acting is the weak link (investment: lots of generator, little actuator)
The whole engine culminates in a few brief sentences in the prompt, competing against the RLHF prior of "cheerful, helpful assistant". Likely result: caricature — the model *announces* "I'm feeling a bit down today" (telling) instead of showing it. Human bad mood manifests mostly in **paralinguistic** channels: shorter responses, higher latency, less initiative, disengaging from the topic earlier, closing the conversation. The harness **directly controls** several of those channels and does not use them: mood should modulate response latency, length budget, initiative rate (already does), willingness to end the conversation, punctuation/emoji habits. They are measurable and do not fight the model. This is the highest-leverage change in the whole document.

### 5. ~~Effort allocation: memory vs. engine~~ — WITHDRAWN (2026-07-01)
Withdrawn after clarification from the project owner: **perceived variance of the emotional baseline at day/month scale is a declared POC objective**, and achieving subtle mood changes with a robust, complex engine is desired — the engine effort is not misallocated, it is the product. Only the uncontroversial residue remains: memory mechanics (summarization cadence, core facts, context budget) are specified when Phase 2 is reached, as the plan already provides.

### 6. The judge→μ→tone→judge loop: stability and semantics
- **Stability (doom loop):** bad day → μ lower → drier tone → judge scores worse → μ lower still. Linearizing: stable if `ρ + 2·k·g·p(1−p) < 1`. With defaults: `0.7 + 0.15·2·1·0.24 ≈ 0.77` ✓ stable with margin; destabilizes toward `k ≳ 0.6`. Make it an explicit constraint of the Phase 1 sweep (`k_max ≈ 0.5·(1−ρ)/(g_max·p(1−p))`), and the plan's shock test is in fact the test of this loop.
- **Semantics:** "good day" for whom? The rubric has to define the construct (interaction quality from the companion's perspective ≠ user satisfaction ≠ persona coherence). Without that decision, μ measures an undefined mixture.
- **Circularity:** an LLM judging an LLM shares its biases. At POC scale the cheap human evaluator exists: the user themselves, with blind comparisons (see §1).

### 7. Fast affect: intra-day gap between sessions
By design, mood only changes at rollover (one-day lag). Within a single conversation the model reacts on its own (fine — division of labor: **the model supplies in-context fast emotion, the harness the slow state**; worth declaring explicitly in DESIGN). The real gap: two sessions the same day — a morning fight, then a new conversation at night with the same `M` sampled at rollover → "as if nothing happened". Cheap fix: intra-day nudge on `arg_h` with the sentiment of the day's last session, or accept and document it.

### 8. The synthetic user is missing — without it, Phases 3/4/7 are not quickly executable
The virtual clock accelerates time, but nobody converses. To run 60–90 days end-to-end (judge, μ, initiative, schedules) a **synthetic user** is needed (LLM in the user role, with good/bad-day scripts). It is the test-harness piece the plan implies ("accelerated session") but never specifies. Without it, validating phases 3–4 depends on weeks of real usage.

### 9. Fictional life without continuity ("goldfish life")
The schedule is generated fresh each day from persona+phase, with no memory of previous days → visible contradictions ("went to the dentist" twice a week, projects that never advance). A minimal **persistent life state** is needed (ongoing arcs: the course she is taking, the gym friend) feeding schedule generation. Bonus: it is the best source of `callback`/`event` reasons for initiative.

### 10. Undefined session semantics
Unresolved: the "day" boundary vs conversations that cross midnight; which exact window the judge scores; what happens to queued proactive messages if the channel cannot deliver (CLI closed = what does a spontaneous message mean?); presence model per channel. Per-reason validity windows mitigate staleness, but the **session** concept must be defined before Phase 3 because the judge needs it as input.

### 11. Import: facts are not the voice
Reconstructing history/memory is the easy part. What the user notices within a minute of "continuing the relationship" is the **voice** — and the plan does not address it. The importer should extract a style card (verbal tics, typical length, emoji use, register) + K excerpts as few-shot. Without it, continuity after import will fail success criterion (c) even with perfect data.

### 12. Defensible scope cuts
- **Discord is a poor fit** for intimate 1:1 companionship (guild semantics, third-party presence, proactive messages in a shared channel). CLI + Telegram cover the real use case — and *hermes* already has the Telegram wiring. Discord: cut or leave as a final stretch.
- **Reason taxonomy vs. real sources:** without external content ingestion, `event` and `shared_interest` have nothing to feed on except the schedule itself. Either scope the POC reasons to `schedule | callback | own-life (schedule)`, or they will be hollow.

---

## Impact/effort ranking (2026-07-01, excludes withdrawn §5)

Effort ≈0 items do not compete for the agenda — they are config decisions/edits made in passing; within each tier, order is by impact.

### Tier A — free (effort ≈0, do in passing at the next DESIGN edit)
| # | Item | Impact | Why |
|---|---|---|---|
| A1 | Cycle observation profile: `L≈7` for live demo + acceleration via virtual clock (§3) | Medium-high | The **monthly** component of the objective is invisible in the POC window without this; `L` is already a parameter |
| A2 | Scope POC reasons to `schedule \| callback \| own-life` (§12b) | Medium | Avoids hollow proactive messages — the most visible initiative failure; `event`/`shared_interest` have no source without ingestion |
| A3 | Cut Discord; CLI + Telegram (§12a) | Medium | Removes Phase 5 work and weird guild semantics; *hermes* already ships Telegram |
| A4 | Loop stability bound: `ρ + 2k·g·p(1−p) < 1` in config validation (§6a) | Medium-low | Insurance against doom loop; defaults already stable (0.77), it is one inequality in the validator |
| A5 | Scope the "model-agnostic" claim → "API-agnostic, with per-model prompt profile" (§2) | Low | Documentary honesty, one sentence |

### Tier B — high leverage (low-medium effort, high impact)
| # | Item | Impact | Effort | Why |
|---|---|---|---|---|
| B1 | **Mood behavioral actuators**: response latency, length budget, willingness to close, punctuation habits (§4) | Very high | Hours–1 day | Turns *generated* variance into *perceived* variance — serves the declared objective directly; channels that do not fight the RLHF prior |
| B2 | **Synthetic user** (LLM in the user role with good/bad-day scripts) (§8) | High | 1–2 days | Only path to observe the monthly cycle and the judge loop in accelerated time; unlocks B3 and gives teeth to A1 |
| B3 | **Blind harness on/off ablation** as the Phase 7 criterion (§1) | Very high | Low (given B2) | Measures "perceived" literally; turns the POC into an experiment. Without it you build everything and learn nothing |
| B4 | **Session semantics**: day boundary, judge window, per-channel delivery (§10) | Medium-high | Low (decisions + rules) | Prerequisite for the correctness of Phases 3–4, not an optional improvement |
| B5 | **Judge rubric construct** (§6b) | Medium | Low | Defines what μ means; without it the loop measures an undefined mixture |

### Tier C — valuable, deferred to their phase
| # | Item | Impact | Effort | When |
|---|---|---|---|---|
| C1 | **Persistent life state** (ongoing arcs → schedule) (§9) | High | Medium | Phase 2 — narrative continuity + callback source + perceptible monthly arc complementing the cycle |
| C2 | **Intra-day nudge** from the last session's sentiment (§7) | Medium-low | Low | Phase 2–3 — the two-sessions/day case |
| C3 | **Style card + few-shot in import** (§11) | High for Phase 6 | Medium | Phase 6 — facts are not the voice |

**Dependency chain:** B2 (synthetic user) enables B3 (ablation) and makes A1 (accelerated monthly cycle) observable. B1 is independent and is the single biggest lever.
