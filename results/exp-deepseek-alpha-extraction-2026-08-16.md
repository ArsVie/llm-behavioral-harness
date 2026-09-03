# Spike — extract transferable alpha from the DeepSeek agent harness

Date: 2026-08-16
Mode: orchestrator (subagents)
Source: `/home/vruizes/deepseek-harness` (the DeepSeek coding-agent monorepo).
Type: **read-only extraction.** No behavior ships from this spike; it produces a
ranked memo that feeds the other spikes/plans.

## Why
Lily's actor **is** deepseek-v4-flash, and this harness is the agent the model was
optimized for. So their context construction, prompt-prefix ordering, system-prompt
shape, reasoning/think handling, tool conventions, and sampling defaults are
effectively the model's *native* usage patterns — defaults we are currently not
matching. Mine them for what transfers to a companion.

## Hard rules
- **Patterns, not code.** It is third-party licensed (LICENSE / THIRD_PARTY_NOTICES /
  vendoring policy). Extract *ideas*, cite `file:line`, and **reimplement** — never
  copy source. (Provenance discipline.)
- **Transferable-only.** It's a *coding* agent; the coding tools don't transfer.
  Keep what applies to a companion: context/prompt/serving mechanics.
- **Gateway reality.** Lily calls via the opencode OpenAI-compatible proxy; flag each
  technique as gateway-compatible or needs-native-DeepSeek-API (some cache/think
  fields may not pass through). This overlaps WS-D's usage-field discovery.

## Extraction dimensions (read against these; rank output by transferability × value)
1. **Context construction & layering** — order of system / persona / history / state;
   static-vs-volatile split. → feeds the prompt-cache spike.
2. **Prompt caching** — `cache_control` / prefix ordering / `cached_tokens`; how they
   structure the prompt for hits. → prompt-cache spike + WS-D.
3. **System-prompt shape** — sectioning, length, framing of persona/instructions for
   this model. → Lily's assembler / state card.
4. **Reasoning / think channel** — how they invoke & parse thinking, `reasoning_effort`
   defaults, think-tag conventions. → internal-thoughts spike.
5. **Tool-call conventions** — schema shape, `tool_choice`, naming, reliability
   tricks. → Lily's decision tools (`decide_event`/`decide_reply`).
6. **Sampling / params** — temperature, top_p, penalties, stop sequences the model's
   own harness uses. → Lily's client defaults.
7. **Compaction / summary** — how they compress long context. → Lily memory / S4.
8. **Formatting / delimiters** — any model-preferred section/message delimiters. →
   bubbling/delimiter spike.

## Grounded targets (start here, follow references)
- `packages/core/system-prompt/src/index.ts`
- `packages/core/session/src/request-header.ts`, `.../types.ts`
- `packages/core/agent-loop/src/agent.ts`
- `packages/host/apiproxy/src/api/llm.ts`, `llm.schema.ts`
- `packages/compaction/compaction-basic/src/summarizer.ts`
- `packages/session/session-title-llm/src/index.ts` (small-model use pattern)
- docs: `architecture.md`, `agent-lifecycle.md`, `tool-catalog.md`,
  `tool-execution-pipeline.*`, `capability-seams.md`, `config-catalog.md`,
  `persistence-catalog.*`, `defensive-patterns.md`. (Note: repo is bilingual —
  prefer the `.md` English; `.zh.md` for anything English is missing.)

## Method
Fan out one reader per dimension (each reads its targets, extracts patterns with
`file:line`, maps to Lily, proposes a testable hypothesis, flags gateway-compat),
then a synthesizer dedups + ranks into the memo. ~5–6 agents, read-only.

## Deliverable — the alpha memo
A ranked table, each row:
`pattern | where (file:line) | why it helps v4-flash | Lily mapping | gateway-compatible? | testable hypothesis | feeds which spike`
Plus a provenance note (reimplement, don't copy) and a short "top 3 to act on now."

## Gate / discipline
- Every claimed alpha cites a real `file:line` (no vibes).
- Each tagged transferable-vs-coding-specific and gateway-compatible-or-not.
- Zero source copied. Nothing ships from this spike — findings route into the
  prompt-cache, internal-thoughts, bubbling, and decision-tool work.

## Out of scope
Any implementation (findings feed other briefs); coding-agent-specific tooling.
