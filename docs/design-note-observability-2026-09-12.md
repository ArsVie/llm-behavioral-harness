---
type: design-note
title: Observability app — context, cache and event flow
date: 2026-09-12
status: working
---

# Observability app (`observability/`)

Live, read-only view over any harness run database. Inspired by DeepSeek
Harness' token meter and trajectory views: the ordered model-visible surface
with one token price per node, reconciled against the provider's own usage
report, plus one chronological stream of everything the run did.

    python -m observability --port 8788        # then http://127.0.0.1:8788

## Why it exists

`harness.trace` and `harness.audit` answer "what happened" and "what did the
model see on call N" — as CLI text. Reading a live run meant running scripts
and querying the DB by hand. This app is a *presentation* layer over those
same answers: it reuses `harness.trace.collect_findings` (the checks
verdicts), `harness.trace.load_anchor` (virtual hour → local clock),
`harness.spend.GroupStats` (spend and cache-hit math) and
`harness.tools.TOOL_SCHEMAS` (the decide-leg schemas), so it can never
disagree with the inspectors.

## Views

| Panel | Question it answers |
| --- | --- |
| Context on the model | What is on the context right now, block by block, priced; how full is the window |
| Ordered surface | Every message in positional order with its price and whether it sits inside the byte-proven cacheable prefix |
| Calls & prefix cache | Per call: prompt, cached, provider hit%, measured shared %, verdicts ("stable prefix CHANGED", "prefix shared but not cached") |
| Event flow | Live tail of messages, state events, decisions, steers, proactive intents, schedule fires, calls, judgements, conversation lifecycles |
| Invariant checks | The same findings `python -m harness.trace checks` reports, click-to-open with detail lines |

## Honesty rules (rendered, not hidden)

- Token prices are heuristic (3.6 chars/token + role framing) and are scaled to
  the provider's reported `prompt_tokens` when one exists; the anchor kind says
  which way (`usage` = estimate undershot, `estimated` = overshot).
- Tool schemas are NOT in the persisted envelope (the store keeps the request
  body minus `tools`), so they are rebuilt from `harness.tools` and labelled
  "rebuilt from harness.tools (not persisted in the envelope)". The mainline
  reply sends no tools at all.
- Rows whose stored `cached + miss` does not sum to their `prompt_tokens` are
  flagged; the fresh share is then derived from the prompt total, and the
  aggregate cache figure switches to `cached ÷ prompt` with a tooltip.

## API

    GET /api/runs                          run list with liveness (write age + process)
    GET /api/run?id=<path>[&call=&limit=]  every panel in one payload
    GET /api/run/events?id=&after=&limit=  unified event stream, cursor-filtered
    GET /api/run/context?id=&call=         context composition for one call
    GET /api/run/call?id=&call=            full envelope + response + reasoning + tools
    GET /api/run/stream?id=                SSE: cheap id/mtime probe every 1.5 s

`id` is only accepted if a fresh scan returns that path, so no request can
read outside the scan root. `?nostream=1` renders one static frame (headless
capture, tests).

## Liveness

`active` = the db or its `-wal` sidecar was written in the last 180 s.
`process_up` = a `live_companion` process is serving that same `--db` path
(read from `ps`), so a quiet bot is distinguishable from a dead one.
