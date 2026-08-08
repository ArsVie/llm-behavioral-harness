---
type: design
title: System design — behavioral harness for LLMs with initiative
description: POC design spec for an OpenAI-compatible behavioral harness — stochastic engine (beta-binomial mood in logit space, ~28-day hormonal cycle, circadian energy channel, Weibull-hazard message timing), behavioral actuation, initiative, memory, judge feedback loop, SQLite data model, and initial parameters.
tags: [design, harness, engine, stochastic, llm]
timestamp: 2026-06-24
---

# System design — behavioral harness for LLMs with initiative

**Project:** behavioral harness POC
**Date:** 2026-06-24
**Basis:** initial project prompt + Phase −1 results ([synthesis](research/00-sintesis-fase-menos-1.md))

---

## Initial inspiration

The concept starts from the paper *"Every 28 Days the AI Dreams of Soft Skin and Burning Stars: Scaffolding AI Agents with Hormones and Emotions"* (arXiv 2508.11829), which proposes using simulated biological rhythms — a ~28-day hormonal cycle and a circadian overlay — as scaffolding to give an agent affective variability and relevance filters. We take that idea as a conceptual seed and carry it into an explicit stochastic engine coupled to a context and timing orchestration layer.

---

## What it is

An **OpenAI-API-compatible harness (wrapper)** that wraps any LLM reachable through an OpenAI-compatible interface and injects **initiative** and **behavioral variability** through stochastic processes. The harness does not modify the base model; it operates entirely in the context and timing orchestration layer. The API is portable; the behavioral brief pressure is calibrated per model family, because different models obey the persona with different intensity.

The simulated **persona** is a configuration parameter, not wired-in logic: a single engine serves any profile. The characterization lives as data.

---

## Features

1. **Backwards compatibility (import).** Ingests conversation exports (generic `{role, content, timestamp}` turn format, with specific converters as needed) and reconstructs history, summarized memory, and initial mood (from the sentiment of recent days) to continue the relationship on the new harness. The hormonal phase is not observable in an export — it is initialized randomly.

2. **Theme/personality configuration (40/40/20 mix).** Brief onboarding that captures the user's tastes. The companion's tastes are generated so that ~40% match exactly, ~40% are adjacent (same category/related), and ~20% are alien. The mix is achieved **by construction**: fixed interest taxonomy (data file), the 4/4/2 slots per 10 are sampled first, and the LLM only decorates the chosen slots — exact proportions with no post-hoc verification. If a conversation is imported or a personality is given, this step is skipped.

3. **Daily schedule.** At the start of the day an activity agenda anchored to hobbies is generated, modulated by the circadian rhythm and by the day of the week (weekday/weekend). It is narrative material for verisimilitude and initiative reasons; it is not mandatory to follow and does not block replying to the user.

4. **Mood changes.** Daily mood state + hormonal phase enter the context as tone guidance; the topic's tendency is biased by the energy channel (circadian), not by valence.

5. **Message frequency.** Spontaneous message timing via stochastic process, managed by the scheduler.

6. **Conversation initiative.** At every start — whether initiated by the companion or the user — schedule + tastes + current mood are injected. If the companion initiates, the LLM chooses the reason for contact anchored in agenda/interests; if the user initiates, what she was "doing" per the schedule is injected.

---

## Architecture

Modular Python monolith with a clean boundary between the **engine** (logic/state) and the **channels** (CLI, Telegram, Discord).

```
            ┌────────────────────────────────────────────────────────────────┐
            │                         HARNESS (core)                         │
 Channels   │  ┌────────────┐   ┌──────────────────────────┐                 │
            │  ┌───────┐  │  ┌────────────┐   ┌──────────────────────────┐   │
            │  │ CLI   │  │  │ Persona/   │   │ Stochastic engine        │   │
            │  │ TG    │  │  │ Config     │   │ - Mood (β-binom.)        │   │
            │  │ DCord │  │  │ (40/40/20) │   │ - Hormonal (~28 d)       │   │
            │  └───────┘  │ ├─────────────┴───┼──────────────────────────┤   │
            │               │   Context assembler (prompt)               │   │
            │               ├──────────────┬──────────────┬──────────────┤   │
            │               │ Client       │ Judge (LLM-  │ Importer     │   │
            │               │ LLM (OAI     │ as-judge)    │ (backwards   │   │
            │               │ compat.)     │ + feedback   │ compat.)     │   │
            │               └──────────────┴──────────────┴──────────────┘   │
            │               ┌────────────────────────────────────────────┐   │
            │               │ Persistence (SQLite)                       │   │
            │               │ Scheduler (async): daily rollover +        │   │
            │               │ spontaneous message firing                 │   │
            │               └────────────────────────────────────────────┘   │
            └────────────────────────────────────────────────────────────────┘
```

**Components:** Persona/Config · Stochastic engine · **Behavioral actuators** · Daily schedule · Context assembler · LLM client (thin layer over any OpenAI-compatible endpoint, `base_url`+`api_key` configurable) · Judge (LLM-as-judge with rubric) · Importer · Persistence (SQLite) · Scheduler (asyncio).

**Cross-cutting decisions:**
- **Injected virtual clock** (`Clock`) in engine, scheduler, and persistence — validation requires running accelerated days; no direct reads of real time.
- **Pure and reproducible engine:** the stochastic engine does no I/O; explicit RNG (NumPy `Generator`) seeded per companion and per day, seed persisted in `daily_state` → deterministic replay of any day.
- **Traceability:** append-only `state_events` table with every state transition and its causes; `llm_calls` log with every prompt/response, including the judge.
- **The actuator is language:** the assembler translates the numerical state (mood, energy, phase, agenda) into a short natural-language brief inside the prompt; raw numbers do not modulate the model's tone.
- **The actuator also controls observable behavior:** the state is continuously projected onto warmth, expressiveness, playfulness, reflectiveness, initiative, length, suggested latency, and tendency to close. The hormonal phase stays in the cause trace and never appears as a label in the prompt. Low mood keeps a floor of warmth: it must feel quieter or slower, not punitive or cold.
- **Visible memory without abrupt shifts:** besides the day's state, the actuator receives the previous state and derives momentum. At the same current mood, coming from a rise or a fall subtly changes openness and expressiveness without creating a second dynamic parallel to the engine.
- **One active channel per process** in the POC (CLI or Telegram or Discord), selected by config; the common `Channel` interface (`send`, `on_message`) is kept.

---

## Stochastic engine

Two coupled time scales: a slow one between days (baseline mood + hormonal cycle) and a fast one within the day (circadian modulation). The engine is a stateful process with memory: today's mood depends on yesterday's; the hormonal phase advances day by day. It is validated by simulation before wiring in the LLM.

### Daily mood (beta-binomial in logit space)

The day's mood is a discrete sample 0..N around a central tendency that decomposes temperament, cycle, event memory, and endogenous mood — each term with its own knob:

```
m(t)    = B·perfil_nivel((t − φ)/L)                 # low menstrual, high ovulatory
g(t)    = 1 + A·perfil_reactividad((t − φ)/L) + ε_t # high menstrual, low ovulatory
η(t+1)  = ρ_e·η(t) + Normal(0, σ_e)                 # endogenous mood AR(1) (uncaused runs)
arg(t)  = logit(λ) + m(t) + g(t)·( μ(t) + η(t) )
p(t)    = sigmoid(arg(t))
M(t)    ~ BetaBinomial(N, p(t), ν)                  # state 0..N; ν=∞ ⇒ Binomial
μ(t+1)  = ρ·μ(t) + k·(score(t) − score_neutral)     # decaying event memory
```

- `λ` — baseline temperament, fixed per persona.
- `μ` — event memory (judge score), with decay (`ρ`) and learning (`k`).
- `η` — persistent endogenous mood: day-to-day autocorrelation without an external cause (human lag-1 inertia ~0.3–0.5).
- `m` / `g` — the cycle shifts the mean and amplifies reactivity as **separate** effects; `g` multiplies only the deviations (μ+η), not the temperament, avoiding coupling between cycle and baseline level except via `m`.
- `ν` — per-person daily volatility (extra dispersion on top of the binomial; ν→∞ = pure binomial). Implementation: `p_day ~ Beta(p·ν, (1−p)·ν)` → `Bin(N, p_day)`.

The plan's original formulation (`arg = (logit(λ)+μ)·α`, pure binomial) is the case `B=0, η≡0, ν=∞` with the gain also applied to temperament; the Phase 1 simulation compares both variants in the same sweep. Rationale in [research/05-reevaluacion-diseno.md](research/05-reevaluacion-diseno.md).

### Hormonal cycle (~28 days)

Slow signal with two separate effects on the same cycle clock: it shifts the mood mean (`m(t)`) and amplifies reactivity (`g(t)`). Since the 2026-07-15 revision both are smooth periodic profiles interpolated between five phase centers, but they use different anchors: menstrual has low level and high gain; ovulatory has high level and low gain. This prevents "high" from also meaning "volatile". At the completion of each cycle its length is redrawn (`L_i ~ Normal(28, 1.5)`); `φ` remains random per persona. The anchors are product semantics, not a clinical claim or a literal hormonal simulation.

### Circadian modulation

**(Revised 2026-07-15.)** The circadian **does not touch valence**: mood M lives on the slow scale and energy is an independent intraday channel. Energy = base + phase offset + phase amplitude × circadian cosine. Ovulatory uses a high level with daily mean ≈0.70 and range ≈0.25; menstrual a low level with mean ≈0.45 and range ≈0.50. That is why the same mood can be expressed with different rhythms depending on the hour, and an energetic phase does not force a high mood on every sample. Message timing continues to be governed by envelope × phase × feedback, not directly by energy.

### Behavioral actuation

The engine produces latent causes; the actuator decides how they are perceived. Valence, energy, momentum, and reactivity are kept as separate channels and translated into continuous controls. The resulting brief describes a disposition ("bright but unhurried", "somewhat sensitive and turned inward") and instructs to **show, not announce**, the state through cadence, word choice, initiative, and length. The numbers, `mu`, `eta`, and the cycle phase stay out of the prompt and remain available in an auditable trace.

Warmth is not synonymous with valence: it has an explicit floor so a bad day does not turn the companion into a punisher. Hormonal reactivity modulates how noticeable a change is, not a fixed per-phase personality. This boundary is Phase 2's first contract and is validated with a reproducible 30-day emulation before connecting the LLM.

### Spontaneous message timing

POC model: **modulated Weibull-hazard renewal process**, simulated by thinning. It preserves the property that motivated the plan's gamma — hazard increasing with elapsed time, not memoryless: the longer it has been since last contact, the more likely the next one — but with a closed-form hazard, which allows clean modulation:

```
h(τ, t) = (k_w/θ)·(τ/θ)^(k_w−1) · circ(hora(t)) · fase(t) · adj(score_ayer)
```

- `τ` — time since the last interaction; `k_w > 1` gives the increasing hazard (`k_w = 1` reduces to NHPP/exponential).
- `circ(·)` — diurnal envelope, ≈0 during quiet hours (nothing at 3am by construction).
- `fase(·)` — per-cycle-phase multiplier (0.6–1.4).
- `adj(·)` — adjustment for the previous day, clamped to [0.7, 1.3] (the score→frequency loop is self-stabilizing, but it is clamped anyway).
- Hard guards **outside the process**, applied at the queue: minimum gap 15 min, daily cap, quiet hours, validity window per reason.

Self-excitation (Hawkes) is discarded for initiative: each proactive ping would spawn ~η more pings — nag bursts, an anti-pattern — and its legitimate use case (cadence within a conversation) is not what this scheduler programs. It remains as a documented extension in its useful form: cross-excitation over user activity. Rationale in [research/05-reevaluacion-diseno.md](research/05-reevaluacion-diseno.md).

---

## Initiative

The scheduler operates with **two gates**: *content gate* (a valid, current reason exists) and *context gate* (the user is receptive: cooldown satisfied, within the active window, quiet hours respected). Only when both pass is a proactive contact queued; each candidate reason carries a validity window and expired ones are discarded.

When the LLM chooses the reason for contact, it is restricted to a **typed reason taxonomy**: `schedule | callback | event | shared_interest | check_in`. The reason is stated in the first sentence of the message; verifiable reasons (schedule/callback) are preferred over behavioral inference. Frequency cap and tone check are applied at the queue, before generation.

---

## Memory

Three levels: (a) "core facts" always injected into the prompt header (immune to compression), (b) cascading mid-term buffer with decay weights, (c) full conversation log for on-demand similarity retrieval. Inspectable by the developer.

---

## Persona and onboarding

The persona is defined by a **typed schema** (survey style) that maps dimensions to engine parameters (temperament, expressiveness, spontaneity) instead of free-text prompts. It reinforces the verifiable 40/40/20 mix and keeps the persona diffable and testable. After initialization, organic drift is allowed, bounded by core traits.

---

## Judge and feedback loop

An LLM-as-judge scores the daily conversation against a rubric and produces `score(t) ∈ [−1, 1]`, which feeds the `μ` update and the next day's message-frequency adjustment. To bound cost/latency, the judge scores in batch once a day and may use a cheaper model.

The judge is a noisy sensor inside the system's only closed loop, so it is calibrated: `score_neutral` is estimated empirically (the judge's mean over reference conversations, not assumed to be 0), rubric with anchored scale + JSON output + temperature 0, and a test-retest repeatability check — if the sd over the same conversation exceeds ~0.2, several passes are averaged or `k` is reduced.

---

## Stack

- **Language:** Python 3.11+.
- **LLM:** client on the OpenAI SDK with configurable `base_url`/`api_key` (remote or local via Ollama/LM Studio/vLLM).
- **Persistence:** SQLite (single file, serverless).
- **Async/Scheduler:** `asyncio` + APScheduler for daily rollover and timing firings.
- **Channels:** CLI (REPL, first channel) · Telegram (`python-telegram-bot`) · Discord (`discord.py`), all on a common `Channel` interface (`send`, `on_message`) to route proactive messages through the active channel.
- **Config:** TOML/YAML + environment variables for secrets.
- **Numerics/simulation:** NumPy/SciPy (distributions), Matplotlib (validation plots).

---

## Data model (SQLite)

- `companion` — id, persona/temperament (`λ`), hormonal parameters (`L`, `A`, `φ`), system prompt.
- `user_profile` — tastes, preferences, timezone.
- `interests` — id, label, category, type (`exact`/`adjacent`/`alien`), owner (user/companion).
- `daily_state` — date, `m(t)`/`g(t)`, mood `M`, `μ`, `η`, previous day's score, schedule (JSON), day's RNG seed.
- `messages` — turns with role, content, timestamp, channel, proactive flag.
- `judgements` — date, score, rubric, judge's justification.
- `schedule_events` — pending upcoming firings.
- `state_events` — append-only log of state transitions and their causes (traceability).
- `llm_calls` — every prompt/response (conversation and judge), for debugging drift and calibrating the judge.

---

## Initial parameters

Starting point to be tuned by simulation; full table in the [synthesis §4](research/00-sintesis-fase-menos-1.md).

| | Symbol | Initial value |
|---|---|---|
| Scale steps | `N` | 10 |
| Baseline temperament | `λ` | 0.60 |
| Learning | `k` | 0.18 — tuned post-Phase 1 (was 0.15) |
| Event-memory decay | `ρ` | 0.85 (half-life ~4.3 d) — tuned post-Phase 1 (was 0.70): together with `k`, ceiling of the deal μ∞=k/(1−ρ)=±1.2 ⇒ perfect month ~7–10, horrible month ~0–4 |
| Cycle length | `L` | redrawn per cycle: `Normal(28, 1.5)` |
| Cycle mean offset | `B` | 0.5 — tuned post-Phase 1 with a 30-seed averaged sweep (was 0.15; see `engine_simulation/`) |
| Cycle reactivity gain | `A` | 0.25 |
| Hormonal noise | `σ_ε` | 0.03 |
| Endogenous mood AR(1) | `ρ_e` / `σ_e` | 0.7 / 0.45 — tuned and adopted in Phase 1 (were 0.5 / 0.2; see [report](results/fase-1-informe.md)) |
| Volatility (beta-binomial) | `ν` | ∞ (=binomial); sweep {∞, 8, 4} |
| Circadian amplitude (energy) | — | ±0.25 (peak ~14:00) |
| Weibull hazard | `k_w` / `θ` | 2.0 / ~13.5 h (baseline mean ~12 h) |
| Per-phase multipliers (rate) | — | 0.6–1.4 |
| Score adjustment (rate) | `adj` | clamped to [0.7, 1.3] |
| Min/max gaps | — | 15 min / 48 h |
