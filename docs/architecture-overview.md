# Companion harness — architecture overview

Living architecture reference: what the system is and how it's built. Point-in-time
**results, measurements, and known issues** live separately in
[results/architecture-review-2026-08-15.md](../results/architecture-review-2026-08-15.md).
Last reconciled: 2026-08-17.

---

## 1. What it is

An AI companion ("Lily") delivered over Telegram. A persona with a simulated
inner life — daily mood, a menstrual-style cycle, circadian energy — that drives
**when she reaches out** (proactive messaging) and **how she comes across**
(warmth, energy, initiative). The behavioral variation is produced by a **frozen,
seeded stochastic engine**; an LLM renders that state into natural language. The
research goal is to show the latent state measurably changes behavior in a way a
blind judge can detect, without the LLM being told the raw numbers. Product and
research are the same instrument — every stochastic/LLM step is recorded and
replayable (never-diverge).

---

## 2. Architecture

Data flows in one direction each turn:

```
frozen engine (numbers) → behavior derivation (channels) → context assembly (prose)
                                                              → LLM → message
        ▲                                                          │
        └─────────────── memory / decisions / timing ◄────────────┘
```

### 2.1 Frozen stochastic engine (`engine/`) — the source of variation
All seeded, deterministic on replay, never told to the model directly.

- **Daily mood** (`mood.py`): `M ~ BetaBinomial(N, p̃, ν)` (ν=∞ ⇒ exact Binomial).
  Two AR(1) latents feed the daily logit:
  - event memory `μ`: `μ' = ρ·μ + k·(score − neutral)`, ρ=0.85, k=0.18 — mood
    carries reaction to what happened.
  - endogenous drift `η`: `η' = ρ_e·η + N(0, σ_e)`, ρ_e=0.7, σ_e=0.45 — mood
    moves on its own.
- **Cycle** (`cycle.py`): a slow level/gain offset — level `m = B·C_m` (B=0.5),
  gain `g = 1 + A·C_g` (A=0.25). Amplifies or damps the day's mood.
- **Circadian** (`circadian.py`): energy `E(h)` over the day and a quiet-hours
  envelope `W(h)` (23:00–08:00) that gates contact.
- **Contact timing** (`timing.py`): a **modulated Weibull renewal process**.
  Hazard `λ(τ,t) = h₀(τ)·W(t)·P_φ·A(s)·S_d`, sampled by thinning. Weibull shape
  `k_w=2` (rising hazard), scale `θ=13.5 h`. Tail guards: min gap 15 min, daily
  cap 3, max silence 48 h. `S_d` is the per-day **state multiplier** — the single
  lever by which mood changes proactivity frequency.

### 2.2 Behavior derivation (`behavior.py`)
`derive_behavior` maps the day's engine record → **12 continuous channels**
(valence, energy, initiative, reactivity, warmth, closing_tendency, …). This is
the honest, high-resolution state. The brief renderer buckets these into discrete
"states" for the prompt (a lossy step — see the review doc for the quantization
history).

### 2.3 Context assembly (`assembler.py`) — three tiers
- **Persona core** (static) — the stable prefix; kept byte-identical across turns
  and conversations so provider prompt-caching hits (structural caching).
- **Day-start block** (once/day, cached): personality + the day's agenda.
- **State card** (every turn, volatile — kept at the tail): current bearing (mood
  prose), energy/availability, current activity, and — when enabled — the
  event/decision payload.
- **Transcript**: recent message history + the current turn.

### 2.4 Memory (`life.py`, memory tables)
Per-conversation memory with an L1→L4 formation (turns → episodes → summaries).
`memory_session_summaries` (one per closed conversation) is the compression layer.
Life "arcs" and a templated daily **agenda** give her things to be doing and to
bring up. **Principle (never-reset):** a conversation close is a background
checkpoint that promotes memory — it never rebuilds her from a summary; on the
user's return the same conversation continues with raw continuity. Compaction only
happens if the token window forces it (see the lifecycle plan / BACKLOG).

### 2.5 Decision / steering layer (`session.py`, `steering.py`, `tools.py`)
The mechanism by which she *reasons over events* rather than blindly firing:
- `tool_decide_event {initiate, reason, action: follow|abandon|defer}` and
  `tool_decide_reply {reply, reason, terminate_event}`, run by a `DecisionRunner`.
- A `SteeringQueue` drains "event pop-ups" at each turn boundary and injects them
  into the LLM call wrapped in a trust marker.
- **Availability negotiation** (merged `fa4cd83`): an Inform→Decide loop
  (go / skip / delay N) with an AFK trigger and a window-close backstop.
- This whole layer is env-gated (was OFF in the first live trial).

### 2.6 Conversation lifecycle (`session.py`, `harness/tunables.py`)
Lifecycle tuning constants live in ONE place, `harness/tunables.py` (imported by
`session.py` and `negotiation_contract.py` — single source, no drift). Current
policy:
- **Away ≠ close.** `USER_AWAY_THRESHOLD_H` (~15 min of silence) marks the user
  *away* — a presence signal that enables proactive/double-text behavior; the
  conversation goes dormant and continues on return, it is not torn down.
- A conversation truly closes only at a natural checkpoint (day / quiet-hours
  boundary) or a long abandoned-chat backstop (`USER_LEFT_THRESHOLD_H`).
- `CLOSING_TENDENCY_ENABLED = False` (the mood-driven close draw is off behind a
  flag; redesign pending) and `MAX_TURNS = None` (no turn cap; "running out of
  room" is a compaction concern). See [lifecycle plan](../plans/plan-lifecycle-away-checkpoint-2026-08-17.md).

### 2.7 Runtime & real-time anchor (`runtime.py`, `anchor.py`)
`AsyncRuntime` runs a real-time loop: proactive firing gated by validity windows
and quiet hours, midnight rollover/replan, single-lock invariant (inbound and
proactive turns never overlap). The **anchor** maps virtual time ↔ real local
time so a restart resumes at the real local hour. Time-scale in live mode is 1:1
(1 virtual hour = 1 real hour, `seconds_per_virtual_hour = 3600`).

### 2.8 Channels & serving (`channels/`, `client.py`)
Telegram (live) and CLI behind one interface. Debounce (~2 s trailing / ~8 s cap),
typing indicator, and two-phase close are env-gated. The product lane runs on
**deepseek-v4-flash via OpenRouter**; the research/judge lane runs local.
`OpenAICompatibleClient` captures usage and reasoning per response.

---

## Pointers
- Results, measurements & known issues: [results/architecture-review-2026-08-15.md](../results/architecture-review-2026-08-15.md)
- Open-items spec (S1–S6): [spec-context-events-time-2026-08-15.md](spec-context-events-time-2026-08-15.md)
- Conversation lifecycle plan: [plans/plan-lifecycle-away-checkpoint-2026-08-17.md](../plans/plan-lifecycle-away-checkpoint-2026-08-17.md)
- Availability negotiation contract: [availability-negotiation-contract.md](availability-negotiation-contract.md)
- Context flow: [context-flow-2026-08-14.md](context-flow-2026-08-14.md)
- Backlog (verbatim asks): [BACKLOG.md](../BACKLOG.md)
