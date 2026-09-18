# DeepSeek-Harness parity check — tools and incoming events (2026-09-12)

Source read: `/home/vruizes/deepseek-harness` @ `47f943859b`, READ-ONLY, by two leaf
readers (one on the tool contract, one on event delivery). Full extracts:
`/tmp/dsh-tools-read.md`, `/tmp/dsh-events-read.md`. Prior extraction re-verified;
two corrections listed at the bottom. Nothing was copied from that repo.

Question: does our tool-calling and event-delivery implementation resemble theirs?

## Tools — the shape is the same, three deltas

| dimension | ours | dsh | verdict |
|---|---|---|---|
| schema type | `{name, description, parameters}` (`harness/tools.py:116`, inform variant `:215`) | `ToolSchema` = the same three fields, ALL required (`packages/llm/llm/src/types.ts:312-317`); `strict` deliberately removed | SAME |
| wire mapping | wrapped in the OpenAI shape at the boundary (`harness/session.py:2797`) | `{type:'function', function:{...}}` (`llm-deepseek/src/serialize.ts:161-168`), dropped when empty (`:182`) | SAME |
| how many | ONE per call — the pop-up's function (3 offered → wrong-tool 1-in-3, 1 offered → 0-in-3) | the catalog: 51 model-visible names; narrowed per call type (`subagent/child-agent.ts:174` restrict, code mode = `run_code` only, session-title = none) | DIVERGE (deliberate) |
| `tool_choice` | NEVER sent (forced choice 400s in thinking mode; omitting parsed 6/6 vs 5/6 for `auto` against the live gateway, 2026-09-12) | NEVER sent (`llm/llm/README.md:97`, `llm-deepseek/README.md:112`: "not part of the core vocabulary (MVP cut…)") | SAME |
| reply parsing | first call whose name matches wins; empty args → `{}`; malformed arguments salvaged from the raw text, never a hard reject (`harness/tools.py:_salvage_arguments`) | `try { return raw ? JSON.parse(raw) : {} } catch { return raw }` (`agent-loop/src/tool-calls.ts:104-110`) — bad JSON rides as the raw string, never rejects | SAME |
| parallelism | one verdict per call | cap 10 (`constants.ts:6`), results committed in contiguous model order (`tool-calls.ts:145-160`), abort synthesizes an error result | n/a (ours is a verdict channel) |
| results into the loop | the verdict rides the context as a native tool exchange — assistant `tool_calls` + `role:"tool"` result replayed at its boundary time (`harness/session.py:_decision_context_messages`) | `ToolResultBlock` → user-role message → `role:'tool'` + `tool_call_id`, empty text `'(no output)'` (`serialize.ts:126-138`) | SAME |
| guidance lives | INLINE in the schema description ("this is NOT a verdict: do not initiate…") | dedicated `tool:<name>` system-prompt sections, band 99-150, sorted, joined `\n\n` (`system-prompt/src/index.ts:56-61`) | DIVERGE |
| gates | no irreversible tool — verdicts only | 51 tools through a gate chain; approval = `allowed-once`, fail-closed; bash needs justification + human approval | different problem class |

Conclusion: our minimal 3-field schema and the OpenAI wire wrapper are the same design
they arrived at, and the two deltas this note originally measured have since closed:
malformed arguments are salvaged on both sides, neither side sends `tool_choice`, and
the verdict now feeds back as a tool exchange. The remaining differences are the
catalog size and the gate chain (a different problem class).

## Incoming events — one shared discipline, one capability they do not have

1. **Funnel — DIVERGE.** dsh forces ONE funnel: everything non-model arrives as a
   `UserMessage` (`llm/src/message.ts:197`) queued on a durable `Inbox`
   (`core/agent/src/inbox.ts:24-78`) with `next-turn`/`next-step` targets, persisted,
   replayed in log order, id-deduped; human input splits `steer` (mid-turn) from
   `followup` (new turn). We have three paths: runtime inbound, scheduler agenda
   events, and harness wakes (`popup_boundary_check`, `afk_deadline`, rollover) plus
   the steering queue. Our #98 bug — a wake armed inside a turn that nobody announced —
   is exactly the class a single inbox makes structural.

2. **Tail-only writes — SAME IN SPIRIT.** They commit appended input only at the surface
   tail (`agent.ts:282-284`), runtime-context snapshot LAST (`agent.ts:238`); the only
   mid-array writer is the `replace` op, used solely by compaction and the pruner
   (`session/src/types.ts:363-374`), enforced by a derivation invariant comparing the
   request against `session.deriveMessages()` (`agent-loop/src/invariant.ts:39-42`).
   We fold the pop-up, steer marker, state card and nudge into ONE trailing
   `role=system` block (`session.py:2751-2767`) and fold system rows store-side
   (`fold_system_history`). The cache postmortem reached the same rule independently
   ("append-only or nothing"). Difference of kind: our folding is a REPAIR we shipped;
   theirs is a runtime assertion. Cheap to adopt.

3. **Injection framing — CHECKED, clean.** They REMOVED `<steering>`/`<context>`
   envelopes after transcripts showed the model refusing them; only `<system-reminder>`
   is trained (`.agents/notes/implemented/simplification/2026-07-20-unwrap-injected-content-envelopes.md:13`
   "No model is trained on these tags."; `:18` "Injected session content projects
   verbatim; the caller owns any framing."). We use no tag family: the pop-up rides as
   `role=system` and the delimiter is a self-describing square-bracket marker
   (`[STEER — a real arriving event from the harness…] … [/STEER]`, `steering.py:439-444`).
   So we are not on the refuted family. Whether our JSON-ish `System: {Event: …}` block
   reads better than prose is still an open probe, not a settled win.
   **The role divergence stands by decision (2026-09-12):** dsh forces `user`,
   we keep `system` because CONVENTIONS:27 says events are system-level inputs
   that are never user messages. Their `user`-message funnel is therefore NOT a
   gap to close — and the earlier "0 failures in 27 decisions as a user message"
   observation does not buy the rule either. Do not "align" this one to dsh.

4. **Event/trigger system — PARTIAL MATCH.** Both persist and dedup: ours via
   `state_events` + replay-by-`decision_id` ("a record already present for it is replayed
   verbatim"); theirs via the Inbox + `agent/inbox/spliced`. Theirs is generic (Cordis
   registry; hooks are an inline serial loop and explicitly NOT surface events); ours is
   domain tables (`agenda_items`, `schedule_events`, `steering_queue`).

5. **Decision gate — THEIRS: NONE (verified negative).** Searches run there:
   `toolChoice|tool_choice` → 0 hits; every `llm.stream` call site → 3; no verdict
   concept. Their event-driven model calls are DERIVATION calls whose output is data
   (session title, compaction summary) and their gates are non-model (`pre-step` reject,
   `tools/pre-execute` deny/ask, human approval). Ours is the opposite: decide legs
   return verdicts with `decision_records`, replay and budget. On events our design is an
   EXTENSION past dsh, not an imitation of it.

6. **Interruption/ordering — same boundaries, theirs more explicit.** No event interrupts
   a streaming request in dsh (only `cancel()`); steering lands only at step boundaries,
   with a serial `turn-stopping` dispatch then re-read; wakes during maintenance/abort are
   LATCHED and replayed; waking input after an abort reclassifies to `next-turn`. We land
   steers at boundaries too, but our wake must be announced by every turn
   (`request_retarget`) — their latch is the more robust contract.

## Portable, cheapest first

1. (S) **Tolerant argument parsing** — `except: keep the raw string` instead of raising.
   One line; a malformed-args reply becomes something the textual parser can still read.
2. (S) **Runtime prefix invariant** — assert request messages equal the derived surface
   behind a debug flag (`invariant.ts:39-42`); we pin it with one test today.
3. (S) **A/B `tool_choice` omitted** — they never send it; measure parse outcome, latency
   and cache with the existing probe rig before deciding.
4. (M) **One inbox for wakes** — give every arriving thing `next-turn`/`next-step`
   targets behind one durable queue; makes the #98 class structural instead of a duty.
5. (M) **Verdict → message** — `role:'tool'` + `tool_call_id` + `'(no output)'` is the
   reference shape if we ever persist verdicts into the main context (gap #79).

## Corrections to the 2026-08-16 extraction

- `runtime-context.ts` now lives at `packages/core/agent-loop/src/runtime-context.ts`
  (content at the cited lines is intact).
- The envelope design note moved to `.agents/notes/implemented/simplification/`; the
  previously cited `docs/design/…` path no longer exists.
- The gloss "arrange decisions via prose instructions instead" (attributed to the dsh
  README) is NOT verbatim anywhere in the tree — the READMEs say only that `tool_choice`
  is unmapped vocabulary, with no rationale paragraph. Quote the README, not the gloss.
