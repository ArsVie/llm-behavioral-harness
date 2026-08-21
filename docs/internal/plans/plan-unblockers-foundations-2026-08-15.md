# Orchestration plan — unblockers & foundations (wave 1)

Date: 2026-08-15
Mode: orchestrator (subagents)
Runs alongside: the affect-codebook pipeline experiment (separate brief).

## Governing decisions (aligned 2026-08-15)
- **Never diverge:** every change is measurable/replayable; the trial build IS the
  measured build. No un-recorded nondeterminism enters the live loop.
- **Renderer-neutral:** the affect renderer does NOT change in production in this
  wave. It changes once, later, when the V4-Flash codebook is validated. All
  state-card work here leaves affect *wording* untouched and builds a clean slot.
- **Engine frozen:** no changes to mood/cycle/circadian/timing. Schema changes are
  additive only; replay parity preserved.
- **Cognition principle (recorded for the deferred S5, NOT built here):** the
  engine owns *timing and the opportunity window*; Lily (LLM) decides whether/how
  to act *within* that window; every such decision is recorded (`replay_id`) so the
  live loop stays replayable and the opportunity-rate causal claim stays clean.

## Scope
IN: S1 (real time on events/conversations), S2 (time-aware agenda + current-time
line), state-card sectioning (foundation), behavioral-signature harness, and
redeploy of the personal trial on the fixed build.

OUT (deferred to wave 2, after codebook results): S4 memory redesign, S5 decision
layer activation, S3 conversation-close model, the AFK/double-message experiment,
and the affect renderer swap.

---

## Workstreams

### W1 — Real-time substrate (S1)
- `anchor.real_at(t_h) -> aware datetime` (UTC instant + tz name).
- Additive migration **v6 → v7**: nullable real-timestamp columns —
  `conversations.opened_at/closed_at`, `agenda_items.start_at/end_at`,
  `proactive_intents.created_at/valid_until_at`, `messages.sent_at`. NULL when no
  anchor (pre-anchor / replay rows) → replay parity holds.
- Write path populates them at row creation **only when an anchor is present**.
- Tests: round-trip; tz correctness; **replay parity** (unanchored run byte-identical;
  anchored run only adds timestamps, changes no behavior).

### W2 — Time-aware prompt (S2)
- Agenda item status transitions as windows pass (`planned → done`/`missed`),
  keyed off current `t_h`, persisted.
- Assembler renders a **current-time/day line** from `anchor.real_at(now)`
  ("It is 15:24, Saturday afternoon — day N") and partitions the agenda into
  *done earlier / happening now / later today* (past items labeled, not dropped).
- Tests: at `t_h=15.4`, `morning coffee (06:58)` renders as "done earlier" and the
  current-time line is correct; **no engine numbers in the prompt** (masking scan).

### W3 — State-card sectioning (foundation)
Owned by the **same agent as W2** (both edit the assembler — avoid collisions).
- Restructure the state card into named sections:
  `TEMPORAL FRAME` (W2) · `AFFECTIVE BEARING` · `BEHAVIORAL BEARING` · `CURRENT INTENT`.
- **AFFECTIVE BEARING keeps the current renderer's output verbatim** — it becomes a
  clean slot the codebook fills later with zero change elsewhere. Pin the affect
  wording as unchanged with a test (G5) so the "renderer changes once" promise holds.
- **BEHAVIORAL BEARING** surfaces the behavioral channels `derive_behavior` already
  computes (initiative / reactivity / persistence), split out from affect.
- **CURRENT INTENT** is an empty reserved slot until S5.
- Tests: sections present and ordered; affect content unchanged; replay parity.

### W4 — Behavioral-signature harness (parallel, independent)
- Extractor module computing the signature set from a conversation log:
  contact-frequency, initiative, warmth, verbosity, latency, topic-selection,
  persistence, reactivity.
- **Built once, used twice:** it is the product surface AND the codebook
  experiment's H4 evaluator (honors never-diverge — same metric both sides).
- Does not touch the assembler → fully parallel with W1–W3.
- Tests: deterministic; reproduces on the existing conv-3 live log.

### W5 — Deploy + restart trial fresh
- Land W1–W4 on main (merge discipline; no pushes).
- **Archive** the current `results/live-companion/companion.db` (do not delete —
  it holds real trial data), start a fresh trial DB so the personal week runs on
  the time-aware build from turn 1.
- Stop the running instance gracefully; relaunch on the new build; anchor resumes
  at real local hour.
- Verify: current-time line correct, agenda expiry working, masking scan clean,
  no Telegram 409.

---

## Gates (pre-registered; a failure blocks merge)
- **G1 replay parity** — additive only; unanchored replay byte-identical.
- **G2 masking** — automated scan: zero engine numbers in any assembled prompt.
- **G3 time correctness** — current-time line + agenda partition match `real_at(now)`
  at known `t_h` fixtures.
- **G4 signature determinism** — extractors reproduce on conv-3 log.
- **G5 affect-unchanged** — AFFECTIVE section wording identical to the current
  renderer (only structure + surrounding sections change).

## Parallelization
Track A: `W1 → (W2 + W3, same agent)`. Track B: `W4` (independent, concurrent).
`W5` after A+B merge. Peak ~2–3 agents + a verification agent. Well within budget.

## Deliverables
1. v7 migration + `anchor.real_at` + timestamped write path.
2. Time-aware, sectioned assembler (TEMPORAL/AFFECTIVE/BEHAVIORAL/INTENT).
3. `behavioral_signature/` extractor module (shared with the codebook experiment).
4. Redeployed trial on the time-aware build (fresh DB, old one archived), verified.
5. Design note recording the cognition principle for the deferred S5 wave.

## Out of scope (explicit)
S4 memory, S5 decision-layer activation, S3 close model, AFK/double-message
experiment, affect renderer swap — all wave 2, gated on codebook results.
