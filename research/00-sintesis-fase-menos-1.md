---
type: research-note
title: Phase −1 — Synthesis of prior research (prior-art)
description: "Phase −1 deliverable: comparison table of comparable products, adopted/discarded design decisions, and initial parameter ranges for the stochastic engine."
tags: [phase-minus-1, synthesis, prior-art, parameters]
timestamp: 2026-06-24
---

# Phase −1 — Synthesis of prior research (prior-art)

**Project:** LLM behavioral harness with initiative (POC)
**Date:** 2026-06-24
**Sources:** research notes [01-products](01-products.md) · [02-research](02-research.md) · [03-initiative](03-initiative.md). Regulatory framework archived in [`deferred/04-regulatory.md`](deferred/04-regulatory.md) (out of POC scope).

This document is the **Phase −1 deliverable** of the plan: comparison table, design decisions we adopted/discarded, and the initial parameter ranges for the stochastic engine. Notes 01–03 contain the detail and citations.

---

## 1. Comparable products (table)

| Product | Memory | Proactive messages | Persona onboarding | Safeguards |
|---|---|---|---|---|
| **Replika** | Curated Memory Tab + sliding window; ~80–85% recall at one month | Follow-up notifications; no autonomous timing algorithm (appears to be a simple scheduler) | Personality quiz + relationship role + free-text backstory; up/down-vote | 5-level classifier; crisis button → hotline; 18+ gate (circumventable, fined in Italy) |
| **Character.AI** | Auto Facts + *pinned* Story Memory (protected from compression) + window | No first-party proactivity documented | User Persona ≤728 chars + long "definition" | Crisis pop-up → 988; facial/ID age verification; teen restrictions Nov 2025 |
| **Chai** | Window only ~20–40 msgs; manually editable memory; annoying resets | No proactivity | Deep creation UI; community bots | Reactive moderation; eSafety (Oct 2025) found crisis-redirection failures |
| **Kindroid** | **5-tier cascaded memory** with decay + keyphrase recall (journal) | **"Advanced Proactivity"** (Ultra/MAX): messages/voice/selfies; quiet hours; calendar-aware | "Codex": 47 configurable parameters + backstory | 3 "Red Lines" via automated scan; warning before lockout |
| **Nomi** | Full history server-side; expanded window | **Configurable frequency** (5 levels); content from "what the AI is thinking/doing" | ~3 min: role + 3–7 traits + backstory + interests (icebreakers only) | Historically weak (suicide incident, MIT 2025); forced update Jan 2026 per NY law |
| **Paradot** | "Memory-to-Understanding": captures facts+emotions+opinions; ~90% recall at one month | Contextual re-engagement documented; mechanism not public | **23-question survey** + sliders; first 72h "critical" | Permissive; limited crisis documentation; covered by NY law |

### Key takeaways
- **Only 3 products do real proactivity** (Kindroid, Nomi, Paradot) and **none exposes the internal state that drives it** — that black box is exactly our differentiator.
- **Structured onboarding > free prompt.** Typed surveys (Paradot 23-Q, Kindroid Codex) produce more predictable and *testable* personas. Matches our decision of persona-as-config.
- **Character.AI's "pin"** is the cheapest solution to the context-compression problem: core memory immune to eviction.

---

## 2. Design decisions (adopt / discard)

### We adopt
1. **Inspectable, model-agnostic behavioral state** as a first-class object (circadian phase + mood + cycle phase). It is the competitive gap; no product exposes it.
2. **Dual-speed dynamics (PAD)** — slow *mood* (hours/days, set by the hormonal phase) + fast *emotion* (per-turn, decays to baseline). It is the consensus of the affective literature (Sentipolis). Fits plan Section 3.5 (slow inter-day scale + fast intra-day scale).
3. **Three-tier memory:** (a) "core facts" always injected (pin style), (b) cascaded buffer with decay (Kindroid style), (c) full log for retrieval. Inspectable by the developer.
4. **Typed-schema onboarding** (survey style) that maps to parameters, not free-text prompts. Reinforces the verifiable 40/40/20 mix.
5. **Two-gate scheduler** for initiative: *content gate* (is there a valid, current reason?) + *context gate* (is the user receptive? cooldown, quiet hours). ProActor's readiness/termination model.
6. **Typed taxonomy of reasons** for proactive messages: `schedule | callback | event | shared_interest | check_in`. `check_in` is the least grounded → lowest frequency.
7. **Clamp PAD to moderate ranges** (±0.6–0.8): arXiv 2604.00005 shows inverted-U curves — extremes degrade response quality.

### We adopt for timing (refines plan Section 3.4)
8. **Recommended model: NHPP + Hawkes** (sinusoidal diurnal envelope + self-excitation with `η<1`). Gives the day/night rhythm **and** conversation bursts. The plan's **Gamma stays as a simple POC variant** (k<1 bursty, k>1 metronomic), documented as not modeling the causal "response-triggers-response" chain that Hawkes does. **[Superseded in the 2026-07-01 re-evaluation: self-excitation over one's own pings generates nag bursts; the POC uses renewal with a modulated Weibull hazard — see [05-reevaluacion-diseno.md](05-reevaluacion-diseno.md) §3.]**

### We discard / qualify
- **Attributing the binomial/gamma model to the paper** — not warranted; they are our own design choice. We keep the binomial (bounded, controllable variance) and validate it by simulation.
- **Open community model (Chai)** without a prior moderation gate — out of POC scope and risky.
- **7 explicit hormones** (paper) — for the POC, **1 cycle signal** (amplitude) suffices as the plan says; we leave the multi-hormone decomposition as an extension.
- **Simple-timer proactivity (Replika)** — insufficient; we use internal state + two gates.

---

## 3. Non-intrusive initiative rules (operational checklist)

> **Scope:** this is **product quality** (that initiative does not feel annoying), not regulatory compliance. The wellbeing/regulatory framework is **out of POC scope** (local, single-user, neither distributed nor public) and is archived in [`deferred/04-regulatory.md`](deferred/04-regulatory.md) in case it is ever published.

From note 03 (ProActor, JITAI, "Computers as Bad Social Actors"):

- **Two gates:** valid and current reason **AND** receptive user (breakpoint, active window, cooldown).
- **Every reason has a validity window** — expired ones are discarded, not deferred.
- **Minimum relationship depth before initiating** (Meta requires ≥5 prior user messages).
- **Explicit reason in the first sentence**; prefer verifiable reasons (agenda/callback) over behavioral inference ("you seem stressed" → surveillance).
- **Anti-patterns as hard constraints** (not style): no pseudo-notifications, guilt ("I miss you"), passive-aggressiveness, "mothering", nagging, engagement-maxxing, opaque triggers. Tone is checked **before** sending; the frequency cap applies at the queue, not at generation.
- **Start conservative** (e.g., max 1 proactive contact/day in the active window) and learn from engage/dismiss/ignore.

---

## 4. Initial parameter ranges for the engine (Phase 1)

Synthesis of note 02. Starting point to be **tuned by simulation** (Phase 1 acceptance criterion). Note that the plan uses a **binomial in logit space**; here we also give the equivalent recommended PAD/timing parameters.

### 4.1 Mood (plan model, logit space + binomial)
| Parameter | Symbol | Initial value | Note |
|---|---|---|---|
| Scale steps | `N` | 10 | states 0..10 |
| Base temperament (valence) | `λ` | 0.60 | slightly positive |
| Neutral | `score_neutral` | 0.0 | score in [−1,1] |
| Learning | `k` | 0.15 | weight of the previous day |
| Decay | `ρ` | 0.70 | memory ~3–4 days (≈ 1/(1−ρ)) |
| Clamp valence/arousal/dominance | — | ±0.80 / ±0.70 / ±0.60 | avoids extremes (2604.00005) |

### 4.2 Hormonal cycle (~28 d)
| Parameter | Symbol | Initial value | Note |
|---|---|---|---|
| Cycle length | `L` | 28 (±2–3 jitter) | standard; jitter per instance |
| Amplitude | `A` | 0.25 | strength of the swing |
| Phase | `φ` | random | per person |
| Noise | `σ_ε` | 0.03 | small |

Valence offsets by phase (from note 02, if per-phase granularity is wanted instead of a single sinusoid): menstrual −0.3 · follicular +0.1 · ovulatory +0.4 · early-luteal +0.1 · late-luteal −0.2. **Message-rate multipliers** by phase: 0.60 / 1.00 / 1.40 / 1.10 / 0.80.

### 4.3 Circadian
| Parameter | Initial value | Note |
|---|---|---|
| Arousal amplitude | ±0.25 | `cos(2π(h−14)/24)`, peak ~14:00 |
| Morning boost / night penalty | +0.15 / −0.10 | 6–11h / 23–4h |

### 4.4 Spontaneous message timing
| Parameter | Initial value | Note |
|---|---|---|
| Model | `NHPP + Hawkes` (recommended) / `Gamma` (simple POC) | — |
| Base rate `λ_mean` | 0.08 msg/h (~2/day when alone) | NHPP envelope |
| Diurnal amplitude `A` | 0.65 | peak `t_peak`=14:00 |
| Hawkes `α` / `β` | 0.35 / 0.80 /h | half-life ~52 min |
| Branching ratio `η=α/β` | 0.44 | **stable (<1)**; healthy range 0.3–0.7 |
| Gamma bursty / regular | `k`=0.6 / `k`=3.0 | CV=1.29 / 0.58 |
| Min/max gaps | 15 min / 48 h | never <15 min; at least 1 contact/2 days |

### 4.5 Dual-speed dynamics (if PAD is adopted)
| Parameter | Initial value | Note |
|---|---|---|
| Fast emotion decay | ~0.30/turn | half-life ~2.3 turns |
| Slow mood decay | ~0.02/turn | half-life ~35 turns (~1 day) |
| Emotion → mood weight | 0.25 | — |
| Hormonal → mood weight | 0.10 | — |

---

## 5. Next step

With this closed, the plan recommends: **Phase 0 (scaffolding)** and **Phase 1 (isolated stochastic engine + 60–90 day simulation with validated plots)**, reviewing the parameters together before wiring up the LLM. The NumPy skeleton of plan Section 3.5 is the Phase 1 starting point.

Before Phase 1, one design decision is worth making: **binomial in logit space (plan) or continuous PAD (literature)?** Both are compatible with dual-speed dynamics; the binomial is simpler and gives bounded variance for free, PAD is richer and better cited. Recommendation: **binomial for the POC**, leaving PAD as a documented extension.
