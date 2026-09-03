# Spikes registry

Date: 2026-08-16
The running list of candidate/ready spikes. Each is small, pre-registered, and
$0-or-cheap. Full briefs linked where they exist; candidates carry a one-para
scope until specced. Ranked by value/cost.

| # | Spike | Status | Cost | Gate / decision |
|---|---|---|---|---|
| 1 | Internal-thoughts marker (analysis vs immersion) | **READY** — [brief](exp-internal-thoughts-spike-2026-08-16.md) | cheap (API) | naturalness + decision-quality vs BASE; port analysis / flag immersion / no-op |
| 2 | Behavioral-signature detectability | candidate | cheap (sim) | blind judge recovers mood-day from the multi-channel signature above chance |
| 3 | Prompt-cache structure | candidate | cheap | cache-hit rate ↑ and $/turn ↓ vs current prompt order |
| 4 | AFK / double-message / no-goodbye | **DESIGNED** — [design note](../docs/design-note-afk-presence-2026-08-16.md) | cheap (sim + judged) | armed post-proactive double-text (`INACTIVITY_STREAM=8`, `P_DOUBLE_TEXT=0.5`) + deterministic no-goodbye detector; 6 gates drafted; pending review → full pre-registration |
| 5 | DeepSeek-harness alpha extraction | **DONE** — [brief](exp-deepseek-alpha-extraction-2026-08-16.md) → [memo](memo-deepseek-alpha-2026-08-16.md) | cheap (read-only) | ranked alpha memo (30 rows) delivered; top-3: static-prefix cache discipline, serializer hardening for reasoning-only turns, purpose-tagged cheap-call policy. Findings route into #1 (internal-thoughts) / #3 (prompt-cache) / bubbling / decision-tool / assembler / client-defaults / memory-S4 |

Adjacent, already in their own briefs (not re-listed as candidates):
- **Codebook re-gate** — [spike-2](exp-affect-codebook-spike2-2026-08-16.md) — SHELVED.
- **Model-driven delimiter/bubbling** — wave-2 plan WS-B ([plan](../plans/plan-ux-tokens-spend-2026-08-16.md)) — result in (bubbling helps; delimiter TBD on Telegram/gateway grounds).

Implementation plans (decided work, not spikes):
- **Conversation lifecycle (away≠close), cache, OpenRouter** — [plan](../plans/plan-lifecycle-away-checkpoint-2026-08-17.md) (WS-A..E; the decided items from the 2026-08-16/17 lifecycle discussion).
- **UX / tokens / spend** — [plan](../plans/plan-ux-tokens-spend-2026-08-16.md).
- **Unblockers / foundations (wave 1)** — [plan](../plans/plan-unblockers-foundations-2026-08-15.md) — DONE.

---

## 1. Internal-thoughts marker — READY
Full pre-registration: [exp-internal-thoughts-spike-2026-08-16.md](exp-internal-thoughts-spike-2026-08-16.md).
Ports the `pi-exp-final` finding (analysis vs immersion markers on deepseek-v4-flash)
into Lily's `CURRENT INTENT` slot; 3 conditions × 3 axes (naturalness, decision
quality, cost), warmed sessions, no-leak invariant. Winner becomes the S5 cognition step.

## 2. Behavioral-signature detectability — candidate
**Question:** the real product claim, renderer-agnostic — over a multi-day sim, can a
blind judge read "which day is she in a better mood" from the *multi-channel*
signature (initiative / warmth / verbosity / latency / topic-selection), NOT from
message count? **Why:** answers "does mood produce a perceptibly different Lily"
independent of the codebook (which is on hold). **How:** run the frozen engine over
N days, render with the current build, extract the W4 signature per day, blind judge
classifies mood band from the signature vector. **Gate:** classification above
chance, CI excludes chance, K≥30. Reuses the W4 harness — near-free.

## 3. Prompt-cache structure — candidate
**Question:** how much can we cut spend by making the prompt cache-friendly?
**Why:** the harness re-sends a large stable prefix (persona, day-block) every turn;
if it's ordered stable-prefix-first / volatile-state-card-last, the provider caches
the prefix (~10× cheaper). **How:** measure cache-hit rate + $/turn on the current
prompt order vs a reordered one (needs WS-D usage capture first). **Gate:** cache-hit
rate ↑ and $/turn ↓ with no behavior change (replay parity). Pairs with WS-D.

## 4. AFK / double-message / no-goodbye — DESIGNED
Full design pass: [design-note-afk-presence-2026-08-16.md](../docs/design-note-afk-presence-2026-08-16.md).
React when the user goes quiet after a proactive (an armed inactivity event);
send at most one gentle double-text; detect a conversation abandoned without a
goodbye. New `harness/inactivity.py` mirroring the negotiation seam; RNG on the
reserved `INACTIVITY_STREAM=8` only (never `day_rng`, replay stays byte-identical).
**Correction:** the "10-min idle-close" I originally cited here does **not** exist
in code — the only silence-close is `USER_LEFT_THRESHOLD_H=12.0` ([session.py:168](../harness/session.py:168));
the 10-min figure in code is the negotiation's `SHORT_AFK_MIN` Decide bomb, and the
idle-close is an unimplemented S3 proposal. The design keeps all three thresholds
distinct with an ordering invariant. **Status:** design + draft 6-gate
pre-registration done; pending your review before it becomes `exp-afk-presence-…`.

## 5. DeepSeek-harness alpha extraction — READY
Full brief: [exp-deepseek-alpha-extraction-2026-08-16.md](exp-deepseek-alpha-extraction-2026-08-16.md).
Read-only mine of `/home/vruizes/deepseek-harness` (the agent v4-flash was tuned
for) for context/prompt/cache/reasoning/tool/sampling patterns that transfer to a
companion. Patterns not code (licensed third-party). Output is a ranked memo whose
findings route into #1 (internal-thoughts), #3 (prompt-cache), the bubbling spike,
and Lily's decision-tool reliability — a force-multiplier that de-risks the others.
