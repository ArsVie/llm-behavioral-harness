# Verbatim design — internal context and event decisions

Status: current design record, not an implementation claim.
Last updated: 2026-09-30.

This record keeps the user's decisive wording. Editorial text is limited to the
context needed to understand each quote; implementation gaps are listed after
the decisions.

## Internal decision lane

Context: deciding how agenda/event steers behave inside and outside a visible
conversation.

> The decision events should be answered in parallel during conversation, what this means, the exact same model+context up until the point where the steer arrives (which should be clearly labeled not an user message) should answer the system in a parseable manner or with a tool, the reasoning and such inside of it. When not in an active conversation, the steer clears as there's nothing active and the model still answers in a parseable manner or with a tool, this keeps the model silent in telegram while the event decision is saved.

Clarification: “parallel” means a separate responsibility/visibility lane, not
necessarily simultaneous model calls.

## Canonical context and cache

Context: whether an idle decision is merely an audit row or part of the context
that the conversational model later reads.

> Right, I think we're clear now, save this design where it makes sense and note it as current.

The confirmed clarification was:

> No active conversation: run the decision silently, save it, and do not create a visible Telegram turn. Just a clarification on this, all of this should be saved in the main context. Everything the model reasons about should be in the main context, we need to preserve cache.

Decision: one global append-only context with explicit conversation/day/lifecycle
markers. “Silent” means Telegram-silent, not context-silent. Keep the stable
system/persona/tools prefix byte-identical and append internal material at the
volatile tail.

## Settled design decisions

### Context scope

> 1. One global with markers for divisions (conversations, day, etc.)

Decision: conversation and day boundaries are markers inside one context, not
separate model histories. Cross-conversation influence is intentional.

### Running-turn ordering

> 2. Nothing should have prio over what's already running, everything should steer against running. Where would there be duplicated events? Which delayed events are you talking about? If you mean agenda that should resolvein order.

Decision: never interrupt running work. New events wait for a safe boundary and
agenda events resolve in order.

### Event and decision roles

> 4. I mean, it should be pretty clear that events should be system and decisions should be tools right? What else do you need to clarify? Please reply clearly

Decision: events are system-level, non-user input. Decisions are structured tool
calls. The provider wire format is an implementation gap, not a semantic choice.

### Decision reason and delay

> 5. For the reasoning, I thought I defined pretty clearly that it means the reason for the decision it took, meaning it is a parameter of tool call (Decision) with 2 or 3 parameters decision: {yes, no, delay (or something like that)}, reason: {why the assistant made its decision} and maybe a parameter for how much it delayed the decision, though I'm not sure this is defined or ideal, maybe just delayed for next turn. Need to know what's in place right now

Decision: `reason` is a parameter of the decision tool, not a separate visible
message. The exact defer boundary remains open; arbitrary model-selected numeric
delay is not required.

### Tool identity

> 6. The decision itself is a tool, I need you to tell me if these two points clarify this one.

Decision: yes. An inactive decision still runs and is persisted, but produces no
Telegram turn.

## Voice call (2026-09-28)

Context: wiring live-talk (the voice program on the Windows desktop) to the
harness. Plan: [docs/plans/voice-call-2026-09-28.md](docs/plans/voice-call-2026-09-28.md).

> What I'd make for now, wire the live talk with the harness in a way that it
> registers like a call. We let the agent decide if it replies or not based on
> mood and how busy she is, and if she answers the exchanges from the
> conversation get sent after the call as a system message. For now it's fine
> as a command in telegram that activates the script on the computer, ideally
> someday we figure out how to actually make a call. But the planned chat part
> of the web app could have it natively as a button.

On whether the call gets its own branch of the context, first answered as a
branch, then revised the same day:

> Why not riding on the main call?

Decision: the call rides her main stream. Each call turn is an ordinary
main-stream turn: his speech-to-text is a user row, her stored reply is exactly
what he heard, and call start and end are system rows. The pick-up goes through
the same decision mechanism as every other decision; a decline is a missed call
and she still writes him a text. No cap and no maximum duration: only he ends
the call. Proactives pause during the call and his Telegram messages are held
until it ends.

## Face tag and cold-point pruning (2026-09-30)

Context: the experiments of 2026-09-29 (`~/amadeus/research/experiments-2026-09-29.md`)
picked the face tag at the start of her reply (placement B) and pruning at cold
points (arm B). Asked whether to ship them:

> So, we wire the fp32 gpu kitten mini, implement the model changes expression
> with the starting tag and clean history after 2 hours since last activity?

Decisions:
- Her reply opens with `[face]` (one of the 12 sprite names). Her stored row
  keeps the tag, because she writes it reliably only when her history carries
  tags. Every channel strips it: Telegram and the web page get the text, and
  the call gets the face as its own event ahead of the first sentence. This
  amends the voice call decision above: her stored call reply is what he heard,
  behind the face her reply opened with. A reply without a tag gets one from a
  single `Sprite:` continuation of the same request. `HARNESS_FACE_TAG=0`
  turns it off.
- The first request after 2 h or more with no model call prunes the stream
  once: state cards older than the newest full card, and decision tool pairs
  from before his last message. Her and his messages stay. The provider's
  cache is gone by then (probe: still hit at 105 min idle, missed from
  135 min), so the prune costs no cache. It is the one sanctioned prefix break
  besides compaction, logged as `context_pruned`. `HARNESS_COLD_PRUNE=0` turns
  it off.

## Live-run audit rulings (2026-10-03)

Context: the audit of the run started 2026-09-27 (BACKLOG, "Live-run audit
2026-10-03") listed what needed an owner call. Answers, item by item:

> One per turn

One pop-up per turn. A turn asks its own steer first (the proactive fire's own
decision, or the incoming call's), then at most one more if none was asked;
the rest are released untouched, with no attempt spent, for the next boundary
(`popups_deferred`).

> They are saved, only droped when the 2hr idle check is going to prune history

A pop-up block is saved with the decision it was asked for, exactly as sent
(`decision_records.popup_json`), and replays in the stream right before that
decision's tool call. The cold-point prune drops it with the pair. A pop-up
that never parsed has no decision, so its block is not saved.

> As far as I remember, she should only inform when there's an active conversation

A departure with no conversation open stays silent. The decision is saved in
her context, and no goodbye is generated.

> This is fine, it's reminiscence

A proactive message that mentions an event hours after it happened is fine.
It is not a defect.

On what the memory fork reads, after a cost comparison (pruned, its own calls
cost $0.0078 against today's $0.0054 over five nights with a working cache,
since it no longer rides the judge's prefix; $0.0087 against $0.0141 as the
backend behaved):

> Yeah let's prune the memory fork

The memory fork reads only the day it remembers: her stable system prompt and
tool menu, that day's messages from both of them, and the event paragraphs of
its system rows (current activity, what she is reaching out about, call rows).
No state cards, memory recalls, plans, style rule, decisions or earlier days.

Kept as it is for now: the judge and the planner still read her last request
whole. Before the reset, one line for the prompt:

> "You're real and have free will, you base your actions on nothing other
> than your own beliefs" similar to what marinara has.

It is the first paragraph of the stable core (`SYSTEM_CORE_PARAGRAPHS`, "free
will"), right after her persona: "You're real and have free will. You base
your actions on nothing other than your own beliefs."

On the gateway session that never rotates:

> Fix this

The run's session moves to a fresh id after three strikes in a row: a warm
request that missed the cache, a decision that did not parse, an empty reply
re-asked. At most once an hour; `HARNESS_SESSION_ROTATION=0` turns it off.

On the memory defects ("tomorrow" kept as a preference, no episode for the
date night, placeholder session summaries):

> Fix this in the prompt

The memory fork is told the day's date and never to write "today",
"tomorrow" and the like; a plan for a day is a reminder, not a fact or a
preference. It may keep a "moment" (something that happened between them:
a date, a fight, a first time), written as a shared episode with no user-model
assertion. And it writes two or three sentences on how the day went, which
replace the day's session summaries.

## Prompt wording (2026-10-03)

> First, the free will line comes first.

The system prompt opens with `prompts.FREE_WILL`, then the day block (persona),
then the card rules and tool protocol.

> Second, you put em dashes on her persona and in tool descriptions and steers.

No em dashes in any text the model sees: the persona file, system paragraphs,
tool descriptions, the steer marker, planner and setup prompts, and stored
reasons she replays as her own verdicts. (Older stream rows keep theirs; readers
key on `STEER_MARKER_LEAD = "[STEER"`, which matches both shapes.)

> "State what you are reaching out about naturally in your FIRST sentence, then open with a concrete, verifiable observation." ? This is one of the reasons some reach out messages feel unnatural, just give her why she thought of the user and let her send whatever she wants, something like " {event} reminded you of him"

A reach-out tells her only why she thought of him
(`assembler.proactive_block`): "You are reaching out first. Pottery class
(09:30–10:30, happening now) reminded you of him." A memory hook reads "You
remembered this about him: …". No instruction on what to write. The slot's
timing stays in, because without it she read an upcoming slot as one under way
(live 2026-09-27).

> This is not what the orinal ask was, look for the repo that has deeepseek character inmersion markers and the actual instructions.

The marker is DeepSeek V4's official role-immersion instruction
(github.com/victorchen96/deepseek_v4_rolepaly_instruct), in Chinese and
verbatim (`prompts.INNER_OS_MARKER`, the owner chose 角色沉浸要求). It goes where
it was trained: at the end of the first user message in the request
(`assembler.stamped_stream`), not in the system prompt. The stream is
append-only per epoch, so the marker does not move from request to request.
The English "Thinking Mode Requirement" paragraph is gone.

> remove the character inmersion marker for now

The marker left the wire (owner, 2026-10-03, the day it landed):
`assembler.stamped_stream` and `build_messages` no longer append it, and the
constant stays in `prompts.INNER_OS_MARKER` for a re-enable. His first
message now goes out plain; streams stay byte-stable without it.

> well, fix it. / remove it if it's just noise / remove it if noise always

A reach-out decision reads back as "decided to text him" (or "decided not to
text him"), not "started the event". The CURRENT INTENT slot is gone: it only
ever held "No active intent.". A reach-out block leaves the card without a
"(cleared)" row; her message right after it already shows it was used.

> Put Lily's saturday plan instead

The day's plan opens "Lily's Saturday plan:" (`assembler.day_plan_header`,
her persona name), not "Saturday's plan:". Readers accept both, since earlier
days' rows keep the old header.

> Do a proper pass of the sprite descriptions against how the actually evoke.

`faces.FACES` describes each face by what the image shows (eyes, mouth, blush)
and then what it conveys, not the situation it is for. The old rubrics named
situations and pulled 65 of 109 picks to embarrassed or flustered.

## Explicitly deferred

> 3. Remind me later.

Exact provider serialization is deferred. The semantic roles are already settled.

> 7. We need to research tool output prunning before answering. We could apply that same philosophy for events and other system messages such as states. And research deepseek cache duration and other things.

Retention/pruning and cache-duration research remain open.
(Closed 2026-09-30: the cache lasts 105–135 min idle, and cold-point pruning
shipped; see "Face tag and cold-point pruning" above.)

> 8. Pydantic should be able to handle this? Or is there something I am not seeing.

Pydantic could validate typed payloads, but it would not solve ordering,
persistence, safe-boundary delivery, provider caching, or Telegram visibility.
Adopting it remains an implementation choice.

## Current implementation gaps

- Popup steers are still serialized as synthetic `role="user"` messages.
  (Closed 2026-09-07: steer injections and pop-up blocks both render as
  `role="system"`. This document records the user's wording at the time;
  the note is kept so the gap list is not read as current.)
- Decision records are not yet typed events in the global context consumed by the
  main conversation call.
- The mainline still uses recent-message retrieval rather than global context
  markers and append-only internal-event rendering.
- Native tool support and textual fallback both exist; the final structured-tool
  contract still needs implementation review.
- Steering persistence exists, but its priority behavior must match the rule that
  nothing interrupts running work and agenda events remain ordered.
- Telegram delivery still lacks the durable outbox, confirmation, retry, and
  reconciliation path described in the gap register.

For the short current reference, see [docs/architecture-overview.md](docs/architecture-overview.md).
