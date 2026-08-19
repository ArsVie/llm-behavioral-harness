# WS-A discovery — mid-reply folding separability (2026-08-16)

Status: RESOLVED as ALL-OR-NOTHING with today's code. Folding is NOT enabled in
this wave; surfaced to the user per the orchestration contract §WS-A ("if
all-or-nothing → surface as a decision; do NOT turn on full event-cognition as
a side effect").

## Question
The path that folds a message arriving *during* her reply
(`user_message_mid_turn`) is coupled to the steering/decision layer (deferred
S5 event-cognition). Is folding separable from full event-cognition, so the
UX win ("recorded for after her turn") can ship without S5?

## Verdict
**ALL-OR-NOTHING with today's code.** The fold path IS the S5 decision layer.

## Evidence (verified by the WS-A lane, code reads)
- `harness/runtime.py` `_on_inbound` routes mid-turn messages to
  `session.enqueue_user_message_steer`, which returns None unless
  `self._decision_enabled` (session.py:~1670).
- `DecisionRunner` is constructed only via injected `DecisionConfig` /
  `HARNESS_*` env (session.py:~441/~461) — i.e. the decision layer is off by
  default and on only when S5-style cognition is configured.
- The fold path (`_apply_steer` with `KIND_USER_MESSAGE` → `_current_activity`
  + `_execute_decision`, session.py:~1758–1786) IS the decision layer's
  machinery. With the decision layer off, no steer is ever enqueued, so
  folding is currently dead code.

## What a separable slice would look like (if we want it later)
In principle: enqueue the mid-turn message WITHOUT the decision layer, and
render it as an injected follow-up (`render-as-inject`) without `_apply_steer`,
flag-gated. That is NEW minimal plumbing — not "turn on folding" — and is a
small S5-precursor lane (pre-registered, own gates), not a side effect of UX
feature enablement. See also `docs/design-note-cognition-principle-2026-08-15.md`
(cognition principle for deferred S5).

## Decision for this wave
- Do NOT enable folding.
- If the "recorded for after her turn" complaint persists, the separable-slice
  lane is the candidate path — proposed as its own pre-registered S5-precursor
  workstream, not bundled into UX enablement.