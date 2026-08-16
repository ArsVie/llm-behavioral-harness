# Orchestration plan — UX feature enablement, delimiter spike, token split, spend accounting

Date: 2026-08-16
Mode: orchestrator (subagents)

## Secrets hygiene (hard, applies to every workstream)
Token/key VALUES are never printed, logged, committed, or written to any artifact
— only presence-by-name is ever reported. Env files are never committed. The
credential resolver logs the LANE and a redacted label, never the secret.

## Preconditions the user must supply
- `LILY_TOKEN` / `JUDGE_GENERATOR_TOKEN` are exported in the user's interactive
  shell, but an in-shell `export` is **session-only** (lost on reboot / new shell /
  detached relaunch) and was invisible to a login shell and a bare `env`. They now
  live in the **repo-root `.env`** (already gitignored via `*.env` and chmod 600);
  the launcher and experiment runners `source` it. The code **fails loudly** if a
  required lane token is missing — never a silent fallback to the opencode key.
  Note: today the launcher hard-sets `LLM_API_KEY` from `OPENCODE_GO_API_KEY`, so
  the live bot is STILL on the opencode key until WS-C lands — exporting
  `LILY_TOKEN` alone changes nothing.
- **Pricing numbers** for each model ($/1M input, $/1M output) are needed for real
  figures — WS-D ships with a pricing config the user fills/confirms.
- Open decision: does each token target the **same gateway** or a different
  provider? WS-C supports optional `LILY_BASE_URL` / `JUDGE_GENERATOR_BASE_URL`
  (default: current gateway) — confirm.

---

## WS-A — Enable the env-gated UX features
Turn on everything gated that helps UX; keep the one thing that contaminates the
read off.

- **Slash commands.** Wire `enable_commands` through `live_companion` (a live-entry
  gap, same class as the anchor was) + a launcher flag; register the user-facing
  set via Telegram `setMyCommands` so they appear in the client:
  `/help /ping /setup /tz /status /mute /version`. **Keep `/state` OFF** — mood
  internals in the user's view contaminate the perceptual read (standing decision).
- **Debounce tuning.** Make `_DEBOUNCE_TRAILING_S` / `_DEBOUNCE_MAX_WAIT_S`
  env-configurable (today they're module consts) and bump defaults to a more human
  window (trailing ~4–5 s, cap ~12 s) so she waits for a normal-paced follow-up.
- **`sent_at` / clock-advance fix (S1 correctness, same inbound path).** Inbound
  messages currently carry no `t_h`, so the clock is frozen mid-conversation and
  `sent_at` is `real_at(frozen t_h)` — not the true wall-clock (all six trial
  messages shared one timestamp). Fix: stamp `sent_at` from real arrival and let
  the clock advance during a conversation. Matters for spend timelines and behavior.
- **Mid-reply folding (FLAG — do not silently over-enable).** The path that folds a
  message arriving *during* her reply (`user_message_mid_turn`) is coupled to the
  steering/decision layer (deferred S5). **Discovery step:** determine whether
  folding is separable from full event-cognition. If separable → enable folding
  (directly fixes the "recorded for after her turn" complaint). If all-or-nothing
  → surface as a decision; do NOT turn on full event-cognition as a side effect.

Gates: G-replay (additive, unanchored byte-identical) · G-mask (no engine numbers)
· G-cmd (each command works e2e; `setMyCommands` set; `/state` absent) · G-time
(`sent_at` = real arrival; clock advances) · determinism.

## WS-B — Model-driven message-formatting naturalness spike
Pre-registered. The *mechanical* punctuation splitter already **failed** this bar
(c-bubbles, naturalness Δ −0.50); this tests whether **cognition-driven** bubbling
beats non-split, and which delimiter the model follows best.

- **Delimiters/instructions tested:** `\n` / `\n\n`, `..`, `<enter>`, `<split>`,
  `<send>`, and a plain-language instruction ("blank line between texts"). Model-
  specific — test on the production model (DeepSeek-V4-Flash via API).
- **Metrics:** (a) instruction-following rate (emits the delimiter when asked, no
  spurious splits); (b) **naturalness Δ vs non-split** (independent judge, K≥30,
  bootstrap CI); (c) mid-sentence-split rate (~0 expected — model breaks at natural
  points); (d) optional: does bubble count track expressiveness if asked?
- **PRIMARY gate (same bar the mechanical splitter failed):** best delimiter's
  split rendering beats non-split on naturalness — Δ ≥ 0, 95% CI not below −0.05.
  **Reliability gate:** chosen delimiter followed ≥ 90% with <5% spurious splits.
- **Decision:** pass → ship model-driven bubbling (parse delimiter → paced multi-
  send, reuse the c-bubbles gap logic, ρ=0.54); fail → do not ship bubbling
  (mechanical already failed; this was the last attempt).
- Spend charged to **JUDGE_GENERATOR_TOKEN** (research lane).

## WS-C — Two-lane token split
- **Live companion (actor) → `LILY_TOKEN`** (+ optional `LILY_BASE_URL`); retire
  the opencode key from the live path.
- **Judges + all experiment-generated replies → `JUDGE_GENERATOR_TOKEN`** (+ optional
  base_url).
- Implementation: a small **credential resolver** that selects the token by LANE
  (`product` | `research`), reads the matching env var, **fails loudly if missing**,
  never logs the value, and stamps the client with its lane for WS-D attribution.
- **Provisioning:** the **repo-root `.env`** (already gitignored + chmod 600, holds
  `LILY_TOKEN` / `JUDGE_GENERATOR_TOKEN`), `source`d by BOTH the launcher
  (`set -a; . "$REPO/.env"; set +a`) and the experiment runners — not an interactive
  `export`. Remove the launcher's `LLM_API_KEY=OPENCODE_GO_API_KEY` line so the
  resolver's lane token wins.
- Gate: G-secret (no value ever emitted) · a live smoke (`--check`) on each lane
  confirms auth without sending content.

## WS-D — Spend accounting ("clear spending figures")
Depends on WS-C (lane tags).

- **Capture usage + cache hits.** Parse the OpenAI-compatible `usage` object
  (`prompt_tokens` / `completion_tokens` / `total_tokens`) from every response —
  currently discarded. **Discovery step first:** log one raw `usage` blob to see
  which cache fields the gateway actually returns (variants: DeepSeek
  `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`; OpenAI
  `prompt_tokens_details.cached_tokens`; Anthropic `cache_read_input_tokens` /
  `cache_creation_input_tokens`). Capture whichever is present.
- **Persist.** Additive migration **v7 → v8**: add `prompt_tokens`,
  `completion_tokens`, `total_tokens`, **`cached_tokens` / `cache_miss_tokens`**,
  `lane`, `model` to `llm_calls` (or an `llm_usage` ledger). Legacy rows NULL;
  replay parity preserved.
- **Price.** Pricing config with a **cached-input tier** (`$/1M in`,
  `$/1M in cached`, `$/1M out` per model — cached input is ~10× cheaper). Cost =
  cached×cached_rate + miss×in_rate + completion×out_rate.
- **Report.** A `spend` script/command: totals + **by lane (product vs research)**
  + by model + by time window / experiment, **plus cache-hit rate and the cache
  savings** (what you'd have paid uncached − actual). Payoff: "this week's Lily bot
  = $X (cache hit 68%, saved $Z), judges/experiments = $Y."
- Gate: G-cost (captured usage matches a hand-checked call; cache-hit tokens
  reconcile to `total`; cost = tiered tokens×price; lane attribution correct).

---

## Sequencing & parallelization
- Track 1 (foundation): **WS-C → WS-D** (credentials, then cost capture/report).
- Track 2 (parallel): **WS-A** (UX features).
- Track 3 (after WS-C): **WS-B** delimiter spike (so its spend lands on the research
  token and is counted).
- **Redeploy** the live bot after WS-A + WS-C + WS-D land: `LILY_TOKEN`, commands on
  (no `/state`), tuned debounce, `sent_at` fixed, spend tracking live. WS-B is a
  spike — no deploy; it decides whether bubbling ships later.
Peak ~5–6 agents.

## Deliverables
1. Command-enabled, debounce-tuned, `sent_at`-correct live entry + launcher flags
   (WS-A); mid-reply-folding coupling resolved (enabled or flagged).
2. Delimiter naturalness memo with the pre-committed ship/no-ship decision (WS-B).
3. Credential resolver + two-lane wiring, secrets clean (WS-C).
4. Usage capture + v8 migration + pricing config + spend report by lane (WS-D).
5. Redeployed trial on `LILY_TOKEN` with spend tracking; first spend figures.

## Decisions to surface (not silently taken)
- Mid-reply folding separable from full S5 cognition? (WS-A discovery)
- Per-token base_url (same gateway vs different provider)? (WS-C)
- Per-model pricing numbers **incl. the cached-input rate** for real figures. (WS-D)
- Tokens now live in the repo-root `.env` (gitignored + 600); WS-C sources it from
  the launcher + experiment runners.

## Out of scope
Full event-cognition (S5) beyond the folding path; the affect renderer swap
(codebook, on hold); S4 memory. All separate waves.
