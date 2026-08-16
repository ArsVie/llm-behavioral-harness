# Spec — context assembly, event reasoning, and real-time grounding

Date: 2026-08-15
Status: DRAFT for review (spec-first; no implementation yet)
Trigger: live-trial log review of `results/live-companion/companion.db` (conv-3,
today's anchored session).

This spec covers six interlocking defects surfaced by the first live day. They
share two roots: (a) everything is timestamped in *virtual* hours with no real
time attached, and (b) the model receives a flat, boundary-less message tail and
— in the live build — no event-reasoning layer at all.

Each section: **Problem** (with evidence) → **Root cause** (code ref) →
**Proposed change** → **Decisions needed**.

---

## Evidence base (what the live DB shows)

conv-3, day 0, anchored (tz America/Chihuahua):

| turn | t_h | speaker | note |
|---|---|---|---|
| #0–#3 | 13.544 | user/companion | greeting → "what time is it?" → "just past seven, coffee warm" |
| #4 (m19) | 15.416 | companion | **proactive**, intent `pi_agenda_item_ag_0_i_movies_15.416`; re-treads noodles/sleep |
| #5–#6 | 15.416 | user/companion | "not feeling it" → river-trail suggestion |

- She reports "**just past seven / morning**" at both 13.5h (~1:30 PM) and 15.4h
  (~3:25 PM). She reads the agenda's `morning coffee (06:58)` item as "now."
- m19 fired from the **"try a small movies exercise"** agenda item but its content
  ignored movies and continued the noodle thread.
- `decision_records` and `steering_queue` are both **empty** — the decision layer
  never ran.

---

## S1 — Attach real time to every event and conversation

**Problem.** All rows store only virtual `t_h` (a float). The real-time anchor
(`kv_store`: `anchor.epoch0_s`, `anchor.t_h0`, `anchor.tz`) is the *only* bridge
to wall-clock time, and it is global, not attached to any event. Nothing in
`conversations`, `agenda_items`, `messages`, or `proactive_intents` records when
a thing actually happened in real time.

**Root cause.** Schema (`harness/store.py`): every table uses `*_t_h REAL`. The
only real timestamp in the DB is `schema_migrations.applied_at`.

**Proposed change.**
- Treat the anchor as authoritative and add a derived-real-time accessor
  (`anchor.real_at(t_h) -> aware datetime`). Persist the resolved real timestamp
  alongside `t_h` on rows the model or the user reads back:
  `conversations.opened_at`, `agenda_items.start_at/end_at`,
  `proactive_intents.created_at`, `messages.sent_at` (all nullable; NULL for
  pre-anchor / unanchored rows so replay parity holds).
- Additive migration only (v6 → v7): new nullable columns, no backfill required,
  no change to `t_h` semantics or the frozen engine.

**Decisions needed.**
1. Store real timestamps as columns, or derive on read from `t_h` + anchor only?
   (Columns cost a migration but make the logs legible and audit-stable; derive-
   on-read keeps the schema frozen but leaves the DB unreadable without the anchor.)
2. Store UTC + tz, or local wall-clock string? (Recommend UTC instant + tz name.)

---

## S2 — Time-aware agenda (stop showing past items as "now")

**Problem.** At 15.4h the model is shown the whole day's plan, `morning coffee
(06:58–07:46)` included, with nothing marking it past. No line tells her the
current time, so she anchors on the earliest salient item and believes it is 7 AM.

**Root cause.** `harness/life.py:264 generate_agenda` writes every item with
`status="planned"` and never transitions it as its window passes.
`harness/assembler.py:278` renders all `planned`/`shifted` items regardless of
`t_h`. `clock.local_hour()` exists (`session.py:2453`) but is not put in the prompt.

**Proposed change.**
- Add a **current-time / day line** at the top of the state card:
  `It is 15:24, day 0 (Friday afternoon).` from `anchor.real_at(now)`.
- Partition the rendered agenda by the current clock: `Done earlier` /
  `Happening now` / `Later today`, or drop past items entirely (decision below).
- Transition agenda item status as windows pass (`planned → done`/`missed`) so the
  memory/close logic and the render agree.

**Decisions needed.**
3. Render past items as "done earlier today" (gives continuity — she can reference
   the coffee she had), or omit them (leaner prompt)? Recommend keep, labeled.

---

## S3 — Conversation lifecycle and the proactive-into-open-conversation rule

**Problem.** conv-3 opened 13.544 and never closed through the 1.9 h idle gap, so
the movies proactive appended to it (turn #4). Two issues: the idle threshold is
far too long, and a proactive should arguably start a *new* conversation rather
than append to a stale-open one.

**Root cause.** `USER_LEFT_THRESHOLD_H = 12.0` (`session.py:168`) — 12 virtual
hours of silence before `check_conversation_lifecycle` closes a conversation.
`_ensure_conversation` (`session.py:996`) reuses any open conversation for both
reply and proactive turns.

**Proposed change.**
- Lower the idle-close threshold to ≈10 **minutes**, aligned with the AFK window
  (`SHORT_AFK_MIN = 10.0`), and make it a named, tunable param — **value to be
  tuned later**, not fixed here.
- Rule: a proactive that fires when the last conversation is **past the idle
  threshold** opens a **new** conversation (with its own real timestamp, S1); a
  proactive that fires while a conversation is genuinely active injects into it
  (S5 — she stays available mid-conversation, matching the availability design).

**Decisions needed.**
4. Confirm ≈10 min as the starting idle-close value (tunable).
5. Confirm the new-conversation-on-idle-proactive rule (vs always-append).

---

## S4 — Context assembly: replace the flat global tail with conversation-scoped history + compression

**Problem.** The "nasty logging." At m19 the model saw a flat last-12-messages
window spanning **three different conversations** (conv-1 noodles, conv-2
"let's think tomorrow / goodnight", conv-3 morning) with no boundaries, no
timestamps, no marker that sessions ended or time passed. Her reasoning quotes
conv-2 verbatim as if current. The apparent "repetition" is her continuing a
blended stream, not missing memory.

**Root cause.** `session.py:1425` `recent = self.store.recent_messages()`;
`store.py:772` `recent_messages(limit=12)` = global `ORDER BY id DESC LIMIT 12`,
not conversation-scoped, no separators. The proactive path (`session.py:1436`)
sends raw `{role, content}` with no structure.

**Proposed change.** Assemble context as explicit, ordered blocks:

```
{system: persona + personality}
{compressed summaries of prior conversations}   ← from memory session summaries
{conversation boundary + elapsed-time marker}   ← "— new conversation, ~2h later —"
{current conversation turns, time-stamped}
{state card: current time (S2), mood, agenda (S2)}   ← refreshed at turn/boundary
{live user turn | proactive hook}
```

- **Scope the raw tail to the current conversation** (`messages` filtered by
  `conversation_id`), not a global id tail.
- **Wire the existing memory into the transcript.** `memory_session_summaries`
  (one per closed conversation) already exist and are the compression layer — feed
  them in as the "earlier conversations" block instead of raw old messages.
- **Insert boundary + elapsed-time markers** between conversations and before a
  proactive ("it has been ~2h since you last spoke").
- This is the concrete form of the proposed
  `{system}{personality}{message history + compression}{state at boundary}` model.

**Decisions needed.**
6. Compression source: reuse `memory_session_summaries` as-is, or add a dedicated
   rolling-summary pass? (Recommend reuse first; add rolling summary only if the
   summaries prove too coarse.)
7. Keep the state card refreshed **every turn** (current behavior — state changes)
   or only at day/conversation boundary (your phrasing)? Recommend every turn for
   mood/time; day-block already caches once/day.

---

## S5 — Reason over events as time passes, even with no activity (the steerability mechanic)

**Problem.** When the movies window opened at 15.0h during the idle gap, the system
should have surfaced it to the model as a decision — "you have movies planned now,
go or skip?" — injected into the last conversation, and let her reason and
optionally reach out about it. Instead a content-blind proactive fired 0.4h later.

**Root cause (decisive).** The decision/steering layer was **not enabled** in the
live run. `_decision_enabled` (`session.py:441`) is true only if a
`decision_config` is injected or an env var in `_DECISION_ENV_VARS`
(`session.py:211`) is set. The launcher sets only `HARNESS_TZ / DEBOUNCE / TYPING /
TWO_PHASE_CLOSE` — **none** are decision vars. So `_enqueue_event_popups`
(`session.py:1455`) never ran and no steer/decision was ever created (empty
`decision_records` / `steering_queue`). Two parallel proactive paths exist and only
the blind one was live:

- **Path A (blind, was live):** `ProactiveSchedule → fire_proactive(intent)` →
  generate a message from `intent.hook`. This fired m19.
- **Path B (reasoned, was off):** `_enqueue_event_popups` → steer → `DecisionRunner`
  (`tool_decide_event`: initiate / follow / abandon / defer) rendered into the turn.

Additionally, even Path B only drains steers **at a turn boundary** (`_chat` start),
so an event arriving during pure inactivity is not surfaced until the next turn.

**Proposed change.**
- **Enable the decision/steering layer in the live launcher** (add the decision env
  contract), so events route through Path B, not the blind proactive.
- **Autonomous event tick during inactivity.** The runtime already parks and wakes
  at agenda/close boundaries (`_firing_loop`, negotiation park instants). At an
  event-window boundary with an open (or recently-idle) conversation, enqueue the
  event as a decide steer and run one decision turn — injecting "movies is planned
  now, go/skip/defer?" into the conversation — instead of (or before) a blind
  scheduled message.
- **Reconcile A and B:** an agenda-item proactive should carry its event through the
  decision path so the *content* reflects the actual event (movies), not whatever
  thread the flat tail ended on.
- **Overlap with availability-negotiation (now merged, `fa4cd83`).** The
  inform→decide-loop is exactly this mechanism for event availability; it was also
  gated off in the live build. S5 is largely "turn it on + add the inactivity tick,"
  not new machinery.

**Decisions needed.**
8. Which decision env contract does the live launcher adopt (tool mode, budget,
   thinking effort, decision source)? This changes live behavior and cost.
9. During inactivity, should an arriving event (a) inject a decide-steer into the
   still-open conversation, (b) open a new conversation to raise it, or (c) stay
   silent and only reason internally unless she decides to initiate? Ties to S3.

---

## S6 — Event content quality (the placeholder smell)

**Problem.** "Try a small movies exercise" reads as a placeholder.

**Root cause.** `harness/life.py:154 _INTEREST_ACTIVITIES` — a 5-string template
pool (`"read about {interest}"`, `"practice {interest}"`,
`"try a small {interest} exercise"`, …) with the interest name substituted.
Routines use fixed `persona.routines` names; arcs use `arc.next_intention`.

**Proposed change (lower urgency).** Options: (a) richer persona-authored activity
library per interest; (b) LLM-authored intentions at day-plan time (one cheap call
per day generating that day's concrete activities); (c) accept the template as an
internal hook and let the turn LLM elaborate it into concrete prose (cheapest —
pairs with S5, where the event is elaborated in the decision turn anyway).

**Decisions needed.**
10. Template-as-hook + LLM elaboration (recommended, cheapest), or generate a real
    daily activity plan up front?

---

## Summary of decisions

| # | Decision | Recommendation |
|---|---|---|
| 1 | Real time: columns vs derive-on-read | Columns (legible, audit-stable) |
| 2 | UTC+tz vs local string | UTC instant + tz name |
| 3 | Past agenda items: keep-labeled vs omit | Keep, labeled "done earlier" |
| 4 | Idle-close threshold start value | ≈10 min, tunable |
| 5 | Proactive-on-idle opens new conversation | Yes |
| 6 | Compression source | Reuse memory session summaries first |
| 7 | State card refresh cadence | Every turn (time/mood); day-block cached |
| 8 | Live decision env contract | TBD — your call (behavior + cost) |
| 9 | Inactivity event → inject/new-conv/silent | Inject into open; new conv if idle-closed |
| 10 | Event content generation | Template-as-hook + LLM elaboration |

## Out of scope / frozen
- Engine stochastics (mood/cycle/circadian/timing) — untouched.
- `t_h` semantics and replay determinism — preserved; all changes additive.
- No live restart is implied by this spec; deploying any of it mid-trial is a
  separate, explicit decision.

## Sequencing note
S1 (real time) and S2 (time-aware agenda + current-time line) are the smallest,
highest-value, lowest-risk fixes and unblock the rest. S4 (context assembly) and
S5 (event reasoning) are the substantive redesign and should be gated behind their
decisions above. S3 is a small tunable. S6 is content polish.
