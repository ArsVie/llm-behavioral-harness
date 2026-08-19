# Availability Event Negotiation — G0 Contract (2026-08-14)

Branch `wip/availability-negotiation` off `main` (653de09). Capability build,
NOT a measurement. Scripted-user only (no judged run). Q1 state-aware-replan
work is OUT of this branch.

Frozen code: `harness/negotiation_contract.py` (import, don't edit).
Parallel agents: A1 (session/runtime), A2 (tools), A3 (episode emission),
A4 (scenarios + tests), A5 (review, read-only).

## Objective

When a scheduled activity boundary (AgendaItem.start_t_h) lands while a
conversation is open, don't yank her out. Soft, negotiable interrupt with a
grace period: she informs the user once, then decides — repeatedly — whether
to go, skip, or hold. The user can retain her by staying engaged, and
release her by going quiet. Bounded: always terminates.

## State machine (the contract to build)

```
event boundary reached (AgendaItem.start_t_h), conversation OPEN
  |
  |-- no open conversation -> skip Inform, go straight to Decide
  v
INFORM  (once, idempotent) -- one-shot context injection: she mentions the
        event naturally ("I've got gym soon"). NO verdict. She does not
        leave. Message goes through the channel (proactive_out).
  |
  v
DECIDE  (recurring) -- fires at min(next companion turn, user-silence >
        SHORT_AFK_H). verdict in {go, skip, delay(N)}
          go    -> follow: graceful close of the conversation -> into the
                   activity. TERMINAL. AgendaItem.status = "completed"
          skip  -> abandon: activity dropped, status = "skipped", recorded
                   (decision_records + agenda status). TERMINAL;
                   conversation continues
          delay -> defer(N): stay for now; RE-ARM both triggers (N more
                   companion turns AND the AFK bomb) -> loop back to DECIDE
  |
  v
BACKSTOP  AgendaItem.end_t_h passes -> forced skip ("missed it entirely"),
          status = "skipped", recorded. Guarantees termination. defer can
          NEVER re-arm past end_t_h (clamp: a delay whose re-arm would land
          at/after end_t_h resolves immediately instead).
```

## Steer-invariant floor (never relax, whatever live signals say)

1. **Model chooses the action** (go/skip/delay) from feeling + conversation
   context; the **server owns the mechanics** (maps "a bit longer" ->
   concrete N, arms the AFK bomb, enforces the backstop). The negotiation is
   not a server calculator.
2. **Termination is guaranteed** by the window-close backstop. defer must
   never loop past AgendaItem.end_t_h. Build this or the feature is
   unbounded.
3. **Inform fires exactly once per event** — idempotency marker is a
   responded-bool (`rec.get("informed") is True`), never key presence
   (commit 3005b9e discipline). No re-announcing every N turns: pending-event
   pressure is internal state, surfaced to the user only on resolution (or
   if she herself raises it again). Not a naggy companion.
4. **Converging pull-to-go**: each delay raises the weight toward go
   (mounting cost / window eroding) so the loop trends to resolution instead
   of deferring at constant probability. Server presents the rising pressure
   in the decide request; model still chooses.
5. **Two thresholds on one silence signal, kept distinct**: SHORT_AFK
   (~10 min — the Decide trigger / time bomb) vs USER_LEFT_THRESHOLD_H (the
   away/user_left threshold in `harness/tunables.py`)
   (conversation close). Both measured from `_last_user_turn_t_h`. Do NOT
   collapse them.
6. **Reuse existing seams, add no new tool**: `tool_decide_event.action`
   {follow, abandon, defer} already IS go/skip/delay — extend defer with an
   N payload and make the runtime loop on it. Conversation lifecycle (B2),
   decision_records, AgendaItem.start/end_t_h all stay as-is.
7. **Deterministic given seed (replay)**: the turn/AFK triggers are
   virtual-clock driven and must replay exactly. Decision ids for decide
   legs are deterministic per (item, phase, delay_index).

## File ownership (strict — no two-owner files)

- A1: `harness/session.py`, `harness/runtime.py` (+ the new negotiation state
  machine; may read tools.py but not edit it).
- A2: `harness/tools.py` (verdict schema: defer gains N; request carries
  phase + skippable flag; Inform idempotency lives in A1's state, verdict
  parse unchanged otherwise).
- A3: episode emission hook (new module `harness/negotiation_episodes.py`
  implementing the NegotiationEpisode -> store.insert_episode mapping) +
  tests. memory.py is must-not-touch; use the store seam.
- A4: `experiments/cvs_user.py` scenario additions + `tests/` (six
  scenarios + deterministic gate).
- A5: review only. Never edits.

Merge order: A2 -> A1 -> A3 -> A4, suite green after each.

## Verdict schema deltas (A2)

- `tool_decide_event` request/inputs gain: `phase` ("inform" | "decide"),
  `skippable` (bool), `delay_count` (int, decide only), `window_ending`
  (bool, decide only).
- `defer` action gains the server-filled `defer_turns` key (DEFER_TURNS_KEY)
  in the verdict/decision record. The model NEVER emits N; the server maps
  the reason text (DEFER_N_PATTERNS) deterministically.
- Inform phase verdict: the model produces the natural mention; NO
  go/skip/delay action. (Verdict shape: `{message: str}` or reuse
  `{initiate: false, reason: <mention>}` — A2 decides, document it.)
- Runtime verdict contract otherwise unchanged.

## Trigger contract (A1)

- Decide fires at `min(next_companion_turn, last_user_turn_t_h + SHORT_AFK_H)`.
- After delay(N): re-arm = next Decide at `min(now + N companion turns,
  last_user_turn_t_h + SHORT_AFK_H)` — turn counter AND AFK bomb both
  re-armed. The AFK bomb re-arms off the LAST user turn (active talk keeps
  pushing it out; silence lets it fire).
- Both triggers are virtual-clock driven; the runtime parks at the AFK
  bomb instant exactly like `next_conversation_close_t_h` parks today.
- Backstop: at any Decide instant where now >= end_t_h -> forced skip, no
  model call. If a delay's re-arm would land at/after end_t_h -> resolve
  immediately (no re-arm past the window).
- Conversation close on go: graceful close via the existing close path with
  a distinct close_reason (e.g. "followed_event"); the model's final
  message is her natural close.

## Skippable / unskippable

`UNSKIPPABLE_SOURCE_TYPES = {"routine"}` — class/work (routine source_type)
is a heads-up not a negotiation: Inform still fires once, Decide is offered
but the model is told the event is unskippable (deterministic go expected —
ties to the decision-probe boundary-vs-discretionary finding). arc/interest
items are discretionary (skippable).

## Episode emission (A3)

- On resolution (go after >=1 delay, skip, forced skip) A1 calls the hook
  with a NegotiationEpisode (summary e.g. "kept choosing to stay with you
  instead of the gym (3 delays), then went" / "skipped the gym for you" /
  "missed the gym entirely — window closed").
- The hook writes via `store.insert_episode` (existing seam) with category
  COMPANION_EPISODE, tags negotiation_go/skip/forced/delay, salience from
  the item, source_session_id = the conversation's memory session id.
- Plain go with zero delays: NOT emitted (not consequential).
- Retrievable in later conversations via the existing L3 episodes path.

## Test scenarios (A4 / G1) — deterministic, fake client, no LLM

1. **Retain** — user keeps actively talking -> repeated delay, she stays
   past the boundary, then resolves when they pause.
2. **Release** — user goes quiet after Inform -> AFK bomb fires Decide -> go.
3. **Window-close** — user holds her past end_t_h -> forced skip ("missed it
   entirely"), recorded.
4. **Unskippable** — routine item -> Inform is a heads-up, Decide ~
   deterministic go regardless of pleading.
5. **No-nag** — Inform emits exactly once; no re-announcement across N
   delays.
6. **Termination** — no configuration loops forever; every path lands on go
   or skip by end_t_h.

## G2 (real-model smoke, small)

A handful of legs with the real model: a user pleading "stay a bit?"
produces delay; going quiet lets the AFK bomb resolve to go.

## Acceptance

- Inform once per event; Decide loops on defer(N); both triggers re-armed
  each delay; window-close forces resolution.
- Skippable vs unskippable routes both phases.
- go / skip / delay-count reach memory via episodes and are retrievable in
  a later conversation.
- Deterministic replay holds; runtime tool schema otherwise unchanged;
  SHORT_AFK != USER_LEFT_THRESHOLD_H (the away/user_left threshold in
  `harness/tunables.py`).
- Q1 replan work absent; scripted-user only (no judged run).

## Explicitly deferred

- Fine per-activity availability windows (gym gaps, class blocks) — the
  AgendaItem window is the only window in v1.
- General user-response monitoring / AFK reactions — this build uses
  "user silent > SHORT_AFK" only as a Decide trigger.
