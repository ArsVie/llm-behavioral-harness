---
type: concept
title: "Harness runtime context flow"
description: "Compact operational flow for context assembly, internal events, steering, and delivery."
tags: [harness, context, steering, tools]
timestamp: 2026-09-07
---

# Harness runtime context flow

This is the short operational view. The [architecture overview](architecture-overview.md)
is the authoritative system reference; the [verbatim design record](internal/verbatim-design-internal-context.md)
contains the user's wording and decisions.

## Context assembly

```text
stable system/persona/tools prefix
  → day/lifecycle markers and day state
  → global append-only transcript
  → typed internal events and tool results
  → current state card
  → current user turn or proactive hook
```

The prefix is byte-identical across calls where possible. Volatile state and new
internal material belong at the tail. Events are system inputs; decisions are
structured tools. Neither is represented semantically as user text.

## Runtime flow

1. An inbound user message or scheduled event arrives.
2. If a model turn is running, the item enters the persistent steering queue.
3. At the next safe boundary, queued items are delivered against the same global
   context. Running work is never interrupted; agenda items retain their order.
4. The model may answer normally or call a decision tool with a decision and
   `reason`.
5. The event, reasoning, tool result, and decision are appended to canonical
   context. During inactivity this can happen without creating a Telegram turn.
6. A visible outbound message is dispatched separately through the channel.

## Important boundary

Local persistence and Telegram delivery are not one transaction. Until the
outbox work is implemented, a failed or ambiguous Telegram request can leave
local context claiming that a message was sent. The target is durable
at-least-once delivery with confirmation, retry, and reconciliation—not assumed
exactly-once delivery.

## Current gaps

- Mainline assembly reads a sliding window of recent messages, so the request
  is not yet an extension of the previous one past the prefix.
- Decisions are projected back into context as prose blocks, not as
  provider-native tool calls and results.
- Day-scoped material is re-rendered into the tail every turn instead of being
  appended once at rollover.
- Durable Telegram outbox and delivery-confirmation events are not implemented.

Closed 2026-09-07: pop-up steers no longer use a synthetic user-role
representation — both steer injections and pop-up blocks render as
`role="system"`, and pop-up calls reuse the mainline stable prefix.
