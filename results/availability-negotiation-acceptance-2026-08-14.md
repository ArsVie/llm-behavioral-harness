# Availability Negotiation — Acceptance Report (2026-08-14)

Branch `wip/availability-negotiation` off main `653de09`. Capability build, NOT
measurement. Q1 replan work out of scope (deferred, per G0).

## Commit chain

| Commit | Content |
|---|---|
| `a8d4864` | G0 contract freeze — `harness/negotiation_contract.py` + `docs/availability-negotiation-contract.md` |
| `69522e2` | A3 decisions-to-episodes emission hook (`harness/negotiation_episodes.py`) |
| `6e5f054` | A2 verdict schema — phase+skippable on request, server-filled defer N (`harness/tools.py`) |
| `33e172c` | A1 phase machine + triggers (`harness/negotiation_state.py`, `session.py`, `runtime.py`) |
| `430e8b3` | A4 six scripted scenarios (`experiments/negotiation_scenarios.py`) |
| `f494ee7` | G2 real-model fixes — native tool-schema wrap, prose inform mention, phase-independent backstop |
| `9c2b5f1` | G2 regression tests (prose inform, INFORM-phase backstop) |

Merge order enforced: A2 → A1 → A3 → A4 (declared in the G0 brief), suite green
at each step. 12 files changed, +3898/−36.

## Gates

- **G0 (contract freeze):** PASS — `a8d4864`, single commit, frozen file untouched since.
- **G1 (scripted gate):** PASS — 76 negotiation tests (schema 37, state 23,
  episodes 8, scenarios 8) green, including deterministic replay
  (identical `(replay_id, t_h, verdict_json)` sequences across fresh stores).
  A4's own final run never completed (its summary admits it); the integrator
  independently ran the suite, found 4 REAL failures, fixed all 3 root causes
  (legacy `closing_tendency` draw firing mid-negotiation, "class" keyword in a
  scripted message, missing parent dir in the replay test), then re-ran green.
- **G2 (real-model smoke):** PASS — real OpenAI-compatible client, real Session
  mechanics, fresh store per run.
  - G2a (pleading "stay? just a bit?"): inform 19.05 → decide 19.15 → **abandon**.
  - G2b (quiet after inform): AFK bomb decide 19.2167 → **defer** (server-filled
    defer_turns=2) → **backstop forced skip at 21.0**. Termination holds even
    when the model delays at the AFK bomb.

## G2-discovered defects (all fixed + regression-pinned)

1. **Pre-existing, exposed by G2:** native popup path never wrapped tool schemas
   in the OpenAI shape → 400 `tools[0]: missing field 'type'`. Fixed surgically
   in `_popup_request_call` (only consumer of request.tools). The probe callable
   had masked it by self-wrapping.
2. **Inform is a mention, not a verdict:** the real model produced natural plain
   prose ("heads up — it's gym o'clock...") with no tool call; the strict parser
   rejected it → stuck-INFORM, no AFK bomb, no backstop (termination hole).
   `_parse_raw` now accepts non-empty prose for phase=="inform"; decide phase
   remains strict (DecisionParseError).
3. **Backstop made phase-independent:** forced-skip at/after end_t_h now applies
   in ANY phase, INFORM included.
4. **Smoke-hygiene:** crashed-run DBs contaminate retries (stale markers,
   restored state) — smoke scripts must use a fresh store per run.

## A5 independent review (read-only, `deleg_2156c40e`)

**Overall: APPROVE** — all 10 floor/contract items PASS with code-level evidence:

1. ENGINE FLOOR: `git diff 653de09..HEAD -- engine/` empty. PASS
2. Inform-once via responded-bool VALUE check (`st.informed is not True`, set-once then phase→DECIDE; restore is `bool(data.get("informed"))` — never key presence). PASS
3. Backstop phase-independent (`now >= end_t_h - 1e-12` → "forced" before phase check); delay re-arm past end resolves immediately; runtime park min'd into rollover target (`next_negotiation_trigger_t_h`). PASS
4. Defer N server-filled; `map_defer_turns` (tools) ≡ `map_defer_n` (state) code-identical, first-match-wins over frozen `DEFER_N_PATTERNS`, clamp [1..4], model-emitted N overridden. PASS
5. `SHORT_AFK_H = 10/60` distinct from `USER_LEFT_THRESHOLD_H = 12.0` (untouched in base and HEAD). PASS
6. No new tool; only `tool_decide_event` / `tool_decide_reply` popup kinds; phase/skippable/delay_count/window_ending ride in `inputs`. PASS
7. Determinism: decision ids `neg-<item>-inform` / `neg-<item>-decide-<delay_count>`; replay-by-decision_id before re-roll; `test_scenarios_deterministic_replay` green. PASS
8. Q1 ABSENT from added lines (only mentions in the contract's deferred-items list); `self._replan()` pre-existing context. PASS
9. Backward compat: `render_popup` adds negotiation lines only when keys supplied (`test_render_popup_legacy_inputs_byte_identical`); suppression scoped to `_maybe_close_conversation` only — the 12h user-left close untouched. PASS
10. Commit hygiene: each commit touches exactly its declared files; no results/ or unrelated files. PASS

## Orchestrator's independent checks (acceptance)

- Floor diff engine/: EMPTY (verified via `/tmp/floor_check.sh`).
- Q1 scan: single `self._replan()` hit — count 1 on base `653de09`, context line, not an addition.
- SHORT_AFK_H = 10/60 h ≠ USER_LEFT_THRESHOLD_H = 12.0.
- Full suite: 1133 tests, green (baseline 1057 + 76 negotiation). Final confirmation run logged to `/tmp/final_suite.log`.
- Working tree: no negotiation files uncommitted; results/ noise pre-existing.

## Floor compliance (never-relaxed)

- Model chooses go/skip/delay from feeling + conversation context; server owns mechanics.
- Termination guaranteed by window-close backstop — defer never loops past end_t_h.
- Inform fires exactly once per event (responded-bool idempotency).
- Converging pull-to-go presented as context the model sees (delay_count +
  remaining window + PULL_PER_DELAY), never a server override.
- Engine/ and harness/ rules: engine/ READ-ONLY; harness/ modified only at the
  seams named in the brief.
- Deterministic given seed (virtual-clock replay exact).

## Deferred (per G0, out of scope)

- Per-activity availability windows.
- General user-response monitoring / AFK reactions (silence only triggers Decide).
- Optional delay cap off.
- Q1 replan integration.

## Artifacts

- `results/availability-negotiation-g2-smoke-2026-08-14/` — G2 real-model smoke outputs (inform/decide records, agenda states).
- Scratch: `/tmp/g2_smoke.py`, `/tmp/floor_check.sh`, `/tmp/final_suite.log`.
- Skill ref: `skills/reference/llm-behavioral-harness/references/availability-negotiation-a1.md` (RESOLVED at integration + G2 findings).
