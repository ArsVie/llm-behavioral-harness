---
type: design-note
title: Cognition principle — engine owns timing, Lily owns the decision (deferred S5 contract)
description: Recorded for the deferred S5 wave: the engine owns timing and the opportunity window; Lily (LLM) decides whether/how to act within that window; every decision is recorded (replay_id) so the live loop stays replayable and the opportunity-rate causal claim stays clean.
tags: [design, s5, decision-layer, replay, wave-1]
timestamp: 2026-08-15
---

# Cognition principle (recorded for the deferred S5 wave — NOT built here)

## The principle (aligned 2026-08-15, governing decision of wave 1)

The engine owns *timing and the opportunity window*; Lily (the LLM) decides
*whether and how to act* within that window. Every such decision is recorded
(`replay_id`) so the live loop stays replayable and the opportunity-rate
causal claim stays clean.

- Engine: mood/cycle/circadian/timing — when an opportunity exists, how long
  it stays open, when the window closes. Deterministic, replayable, frozen.
- Lily (LLM): whether to take the opportunity (initiate / follow / abandon /
  defer) and how (content, tone, timing of the message *inside* the window).
  Stochastic, model-bound, judged.
- Recording: every decision is persisted with a `replay_id` linking it to the
  opportunity that produced it, so a later replay run sees exactly what was
  decided, when, and why the window was in that state.

## Why it is recorded now (wave 1, 2026-08-15)

The wave-1 plan explicitly defers S5 (decision-layer activation) to wave 2,
gated on the affect-codebook results. This note pins the contract so the
deferred wave cannot drift:

- The live trial's decision/steering layer was OFF on day 0 (spec
  `docs/spec-context-events-time-2026-08-15.md` §S5): `decision_records` and
  `steering_queue` were empty; a content-blind proactive fired instead of a
  reasoned event decision. S5 is "turn it on + add the inactivity tick", not
  new machinery — the inform→decide loop already exists (availability
  negotiation, merged `fa4cd83`).
- What wave 1 leaves in place for S5:
  - `CURRENT INTENT` reserved slot in the state card (empty, masked-clean).
  - Real timestamps on conversations/agenda/proactive intents/messages (v7)
    so every window and decision has a wall-clock instant.
  - `decision_records` + `steering_queue` tables (v5) — the persistence
    backend the decision runner writes to.
  - The behavioral-signature module (`behavioral_signature/`) — the
    opportunity-rate evaluator surface shared with the codebook experiment.
- What must NOT change meanwhile: the affect renderer (one swap, after a
  validated codebook) and the engine (frozen).

## The causal claim S5 must keep clean

The opportunity-rate experiment asks: does giving Lily the event window
(and recording her decisions) change the rate at which she reaches out?
For that claim to stay clean:

1. The opportunity window is determined by the engine alone — the LLM never
   extends, shrinks, or re-derives it.
2. Every decision inside a window is recorded with its `replay_id` — no
   un-recorded nondeterminism enters the live loop (the wave-1 "never
   diverge" rule).
3. Replay reproduces the recorded run: same seeds, same opportunities, same
   decisions → byte-identical state (minus the real-time columns, which are
   additive and NULL when unanchored).

## Out of scope (still, after this note)

S4 memory redesign, S5 activation, S3 close-model tuning, AFK/double-message
experiment, affect renderer swap. All wave 2 — gated on codebook results and
the owner's go.
