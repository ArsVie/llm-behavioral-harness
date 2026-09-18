# Verbatim design — internal context and event decisions

Status: current design record, not an implementation claim.
Last updated: 2026-09-06.

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

## Explicitly deferred

> 3. Remind me later.

Exact provider serialization is deferred. The semantic roles are already settled.

> 7. We need to research tool output prunning before answering. We could apply that same philosophy for events and other system messages such as states. And research deepseek cache duration and other things.

Retention/pruning and cache-duration research remain open.

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

For the short current reference, see [../architecture-overview.md](../architecture-overview.md).
