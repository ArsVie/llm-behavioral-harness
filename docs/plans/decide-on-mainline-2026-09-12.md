# Plan — the decision rides the mainline call (2026-09-12)

Owner's ruling, verbatim: *"it should be a tool call on the mainline context and
just that"*, and *"there's no need for her to talk about her internal decisions …
it should obviously still be appended to the context as verbatim to enrich
behavior"*.

Status: SUPERSEDED by the 2026-09-13 amendment at the bottom (one steer, one
round). The text below is the 2026-09-12 batch design as it was landed: the
drain collects, the turn's generation answers, and the
verdict's effects run through the handlers that owned them, oldest boundary
first. Three rulings from the same session are in: this one, the temporal frame
on the day's first card only (`103f5df`), and the arrival time on every user
turn (`e4a4a4a`). STILL OPEN: the per-boundary SNAPSHOT below -- each catch-up
boundary is still rendered with the state at the drain instant, only its own
`Time:` is its own.

## What exists today

A pop-up decision is a SEPARATE model generation with its own request:

- `Session._execute_decision` (`harness/session.py:2781`) builds a
  `Capabilities(has_native_tools=...)` and passes
  `lambda request: self._popup_request_call(request)` to `DecisionRunner.execute`
  (`harness/session.py:2811`).
- `_popup_request_call` (`harness/session.py:2883`) rebuilds the turn's context
  from `self._context_turns()`, appends the state card and the pop-up block,
  offers ONLY the requested function, and makes its own
  `client.chat_with_meta(...)` call.
- Three call sites drive it, each with its own post-processing, each returning a
  steer outcome the drain aggregates:
  - `harness/session.py:2526` `tool_decide_reply` — verdict may SUPPRESS the
    ordinary reply (notice instead) or close the event (`:2537-2548`).
  - `harness/session.py:2603` `tool_decide_event` — an `abandon` closes the item
    (`:2620`), an `initiate` either omits a backlog send (`:2622-2630`) or
    records `drain.decided_intents` (`:2631+`).
  - `harness/session.py:2756` `tool_decide_proactive` — `drain.decided_intents`
    or suppressed-status bookkeeping (`:2765-2778`).

Because the drain runs BEFORE the mainline generation, and the verdict's effects
(suppression, notes, closes) must be known BEFORE the prose is written, the
decision cannot simply be moved after the turn: the turn has to become two
phases.

## The change

Phase A (before the mainline generation) — the drain COLLECTS instead of
deciding. Each of the three sites appends a deferred record
(`decision_id`, `popup_kind`, `inputs`, `steer`, `day`, `t_h`) to the drain and
returns a new `_STEER_DEFERRED` outcome. No model call.

Phase B (one mainline generation, then apply):
1. Offer the tools of the deferred kinds on the mainline request:
   `offered_tools(request)` for each deferred kind, unioned when several are
   pending, passed as OpenAI-shaped `{"type": "function", "function": ...}`.
   Attach only when at least one decision is deferred — a decision-free turn
   keeps the prose-only request shape.
2. Generate once. The model now answers the pop-up AND speaks in the SAME
   generation.
3. For each deferred record, in ascending `t_h`, run
   `DecisionRunner.execute(..., model_call=served)` where `served` returns the
   mainline reply's tool call for that kind; only a re-ask (parse failure, inside
   the existing `steering.MAX_ATTEMPTS` bound) falls back to
   `_popup_request_call`, and any record with no servable call gets its own call.
   The runner's parse/verdict/record/abandon logic and the existing replay
   persistence are used unchanged — that is what keeps the verdict VERBATIM in
   the context.
4. Apply each verdict's effects exactly as the three sites do today — move that
   post-processing (not copy it) into a `_apply_drain_decisions(drain, reply, ...)`
   called after the generation.

## Traps, all measured, do not rediscover them

- **Empty reply guard.** `_generate` (`harness/session.py:1938`) raises on an
  empty or whitespace-only reply. A turn whose only output is the decide tool
  call is now legal and must not raise; a turn with neither prose nor a call
  still raises. Do not relax it beyond that.
- **One function per pop-up.** Offering all three schemas made the model answer
  an event pop-up with `tool_decide_reply` at 1-in-3 against the live gateway;
  with one offered it was 0-in-3 (`harness/session.py:2921-2925`). Union only the
  kinds actually pending, and keep the pop-up block naming its kind.
- **`tool_choice` stays unset.** A named or forced choice 400s in thinking mode;
  omitting it parsed 6/6 against 5/6 for `auto` at identical cache
  (`harness/session.py:2927-2937`).
- **Role convention.** The pop-up block rides `role="system"`. Appending the
  harness's own clock to a user turn is a deliberate, owner-requested exception
  (`assembler._stamp_user_turn`); nothing else may enter a user turn.
- **Cache.** The mainline request gains a tools payload on decision turns: first
  appearance costs the prefix, later calls re-hit. Do not chase this as a
  regression — measured on the live run 2026-09-12 (`#4 896/1187 = 75.5%` →
  `#5 384/1115 = 34.4%`, faithful replay ceiling 76-86% = first-appearance
  priming). See the skill's `references/prompt-cache-forensics.md`.
- **Deterministic replay.** `decision_id` is the steer id, so a re-drained steer
  replays its recorded verdict instead of re-rolling. Keep that property: Phase B
  must look up the record before it decides.

## Second ruling — stale events in their own time

Owner, verbatim: *"they should play as if they were decided by the model when
they should have happened when day 0 or disconnects don't let them play in the
time they should"*.

Today a catch-up batch drains in one pass at the current instant (live evidence:
seven boundaries all stamped 21.42.50 on the reset run), so the model correctly
answers "already happened" and abandons them.

Ordering (`t_h` ascending) is the cheap half and belongs with the change above:
sort the deferred records before Phase B and decide each at its own boundary
time, which the pop-up block already carries (`Time: 07:28`, `at =
payload["time"]`, `harness/session.py:2600-2601`).

The expensive half is the SNAPSHOT: each boundary should be rendered with the
state as of that `t_h` (agenda partition, availability, mood), not the state at
the drain instant. `assemble_snapshot` is already a pure function of
`(snapshot, t_h, anchor)`, so this is a per-boundary snapshot build rather than
new machinery — but it is a separate landing with its own test, and it must not
be claimed as done before a replay test shows the morning items resolving in
morning order.

## Amendment — one steer, one round (2026-09-13)

Owner, verbatim: *"it should go state card + {steer} -> {model} -> {decision
output} -> {steer}"*, and *"proactive ones are different in the sense that they
should always get a reply from the model"*.

Phase A/B above is retired. The drain no longer collects: each pending steer
runs its own bounded model call, in order — the pop-up block rides that request
(`_popup_request_call`), the verdict comes back as its tool call, and the
recorded decide pair extends the context the NEXT ROUND and the later turns
read (the pair replays through `_context_turns` at its boundary `t_h`). The
The turn's generation carries no pop-up block — and the SAME constant tools
menu as every call (tools never toggle; see the third amendment); it still
folds the drain's injections and decided notes into the trailing card.

Consequences the implementation pins:

- A no-reply verdict still suppresses the ordinary reply before persistence.
- A generation that comes back without prose is re-asked once over the SAME
  request (`REPLY_NUDGE`, `Session._reask_for_reply`); a second empty answer is
  dropped loudly (`empty_reply_dropped`) instead of persisting an empty
  assistant turn. For a proactive steer the generation reply IS the message.
- Reactive and proactive turns share the shape; a declined proactive fire still
  suppresses the reply before anything is persisted.
- The per-boundary SNAPSHOT half of the second ruling stays open: each round
  renders the current state; only `Time:` is the boundary's own (moot for the
  CARD since the third amendment — no time-of-day state rides it).

Tests: `tests/test_decide_on_mainline.py` pins the shape — one steer, one
round; pairs extend the next round; the prose-less re-ask and the drop; the
proactive decide-then-message flow; the clamp (a boundary-stamped decision
predating the stream head still replays).

## Third amendment — 2026-09-13 rulings (owner)

Owner, verbatim: *"Let's just remove the temporal frame at all from the state
card. The decisions and such are already recorded in the context, no need to
tell her what she already did, she knows that and her agenda at the start of
the day."* — `render_temporal_section` is deleted; the card carries no clock
reading, no partition, no window times. The day block (emitted once at
rollover) carries the plan; status lives in the store and the context stream.

Owner, verbatim: *"ALL MODELS WORK LIKE THIS, tools are part of the context,
you can't enable and disable them as you please."* — every conversation call
sends one constant menu (`harness.tools.TOOL_PAYLOAD`); the per-kind
one-function narrowing and the inform-phase schema variant are retired (the
event schema carries the inform's `message` field). "Chat carries no tools" is
gone from the design.

Owner, verbatim: *"wire out a method to show you what is sent to the model
verbatim each call... And wire it to observability too."* — `harness/wire.py`
dumps every request body at send time to `<run>/wire/` (+ `index.jsonl`), and
the observability app lists, opens and diffs the dumps (Wire card).

Pair replay fix: `_context_turns` no longer drops decisions whose boundary
`t_h` precedes the stream's first row — they clamp to the stream head (after
the user row, in record order); the epoch filter is delivery time, so a
decision delivered inside the stream replays even when its boundary stamp is
older.
