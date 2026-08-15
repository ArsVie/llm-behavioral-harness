# C8 — Message effects on high-valence days

**Plan ref:** `plans/advisor-orchestration-2026-08-15.md` §C8 (Part 3)
**Worktree:** `llh-wt-c-effects` (branch `wip/c-effects`)
**Date:** 2026-08-15
**Engine contract:** frozen — `sim.run_daily.run` (W2.1 driver) over `engine/` (Ola 0, read-only). No harness/engine/sim files touched.

## Verdict summary

| Criterion (plan §C8) | Verdict | Evidence |
|---|---|---|
| (a) API confirmed usable by bots (message effects in private chats) | **PASS** | `message_effect_id` documented on `sendMessage` + 19 other send methods, explicitly "for private chats only" (verbatim quotes below). |
| (b) Threshold selects only top-decile valence days with ≤1 effect/week expected frequency | **PASS** | threshold `p > 0.8131` (90th percentile of pooled daily valence, n=140) selects **exactly** the top-14 days (top decile by rank); expected frequency **0.70/week ≤ 1/week**. |
| Drop rule | **Item stands** | Per plan: "Fail (a) → drop the item entirely." (a) PASSED ⇒ C8 is **not dropped**; proceeds to implementation review. |

---

## Leg (a) — API verification: `message_effect_id` usable by bots in private chats

**Verdict: PASS.** Message effects are a documented, shipped Bot API feature usable by bots, restricted to private chats — exactly the C8 target context (bot ↔ owner private chat).

### Primary source — official Bot API reference
**URL:** https://core.telegram.org/bots/api (fetched 2026-08-15)

Verbatim from the `sendMessage` parameters table:

> `message_effect_id` | String | Optional | **Unique identifier of the message effect to be added to the message; for private chats only**

The same parameter (same wording, "for private chats only") is present on `sendPhoto`, `sendVideo`, `sendAnimation`, `sendAudio`, `sendDocument`, `sendSticker`, `sendVideoNote`, `sendVoice`, `sendLocation`, `sendVenue`, `sendContact`, `sendPoll`, `sendDice`, `sendInvoice`, `sendGame`, `sendMediaGroup`, `sendLivePhoto`, `sendRichMessage`, `sendChecklist`, and on `forwardMessage`/`copyMessage` ("only available when forwarding/copying to private chats").

Verbatim from the `Message` type (incoming/outgoing messages carry the effect back to the bot):

> `effect_id` | String | _Optional_. Unique identifier of the message effect added to the message

### Primary source — Bot API changelog (introduction)
**URL:** https://core.telegram.org/bots/api-changelog (fetched 2026-08-15)

Verbatim:

> **May 28, 2024 — Bot API 7.4** … Added the field `effect_id` to the class `Message`. Added the parameter `message_effect_id` to the methods `sendMessage`, `sendPhoto`, `sendVideo`, `sendAnimation`, `sendAudio`, `sendDocument`, `sendSticker`, `sendVideoNote`, `sendVoice`, `sendLocation`, `sendVenue`, `sendContact`, `sendPoll`, `sendDice`, `sendInvoice`, `sendGame`, and `sendMediaGroup`.

Later extended (Bot API 9.1, 2025-08): `message_effect_id` also added to `forwardMessage` and `copyMessage`.

### Secondary sources (independent wrappers mirror the official wording)
- python-telegram-bot v22.5, `telegram.bot` / `telegram.message` (https://docs.python-telegram-bot.org/en/v22.5/telegram.bot.html): "message_effect_id (str, optional) – Unique identifier of the message effect to be added to the message; for private chats only. Added in version 21.3"
- aiogram 3.x docs (https://docs.aiogram.dev/en/v3.17.0/api/types/message.html): same wording, "for private chats only"
- grammyjs/types `methods.ts` (https://github.com/grammyjs/types/blob/main/methods.ts): `message_effect_id?: string` with the same comment.
- Community evidence of real bot usage with concrete effect IDs (e.g., https://stackoverflow.com/questions/78600012/message-effect-id-in-telegram-bot-api, https://gist.github.com/wiz0u/2a6d40c8f635687be363d72251a264da).

### Caveats (implementation notes, not blockers)
1. **Private chats only.** The parameter is ignored/rejected outside private chats — fine for C8 (the companion's chat with the owner is a private chat).
2. **No documented Premium requirement for bots.** The docs impose no plan restriction; the parameter is available to any bot in a private chat.
3. **No official effect-ID registry.** Telegram does not publish a list of valid `message_effect_id` values; known IDs circulate in community lists and can be harvested at runtime from the `effect_id` field of incoming/outgoing `Message` updates. Implementation must ship with a curated ID set (e.g. the documented community IDs) — a product decision, not an API blocker.

---

## Leg (b) — Threshold study (5 × 28-day valence traces)

### Method
- **Engine:** `sim.run_daily.run(days=28, seed=s, variant=DECOUPLED_OFFSETS, persona=PersonaParams())` — the frozen day-by-day driver (cycle + mood + synthetic score), production variant, default persona (DESIGN.md frozen parameters). Fully deterministic per seed (`day_rng(seed, t)`).
- **Seeds:** 5001–5005 (plan's cheap-run discipline) × 28 days = **140 days**.
- **Valence (primary):** `p` — the engine's continuous daily mood probability (`DayRecord.p`, sigmoid of the day's logit argument). Continuous ⇒ no ties ⇒ the top decile is well-defined by rank.
- **Valence (secondary, observable):** `M/N` — the discrete daily mood score (0..1 in 0.1 steps, `DayRecord.M/10`).
- **Rule sought:** threshold selecting ONLY top-decile valence days with expected frequency ≤ 1 effect/week (≤ 4 effects per 28-day trace).
- Reproducibility: rerun produces byte-identical JSON (SHA-256 verified across runs).

### Valence distribution (pooled, n = 140)

| metric | p (primary) | M/N (secondary) |
|---|---|---|
| mean | 0.589 | 0.568 |
| sd | 0.179 | 0.221 |
| p50 | 0.579 | 0.6 |
| p75 | 0.746 | 0.7 |
| **p90 (threshold)** | **0.8131** | **0.8** |
| p95 | 0.829 | 0.9 |
| min / max | 0.123 / 0.890 | 0.1 / 1.0 |

### Chosen threshold
**`threshold_p = 0.8131`** (90th percentile of the pooled daily-valence distribution, n=140).

**Selection rule:** attach a message effect on day *t* iff `p_t > 0.8131`.

This selects exactly the 14 highest-valence days pooled (the top decile by rank — verified: selected set == top-14 set, no ties). Expected frequency = 14/140 days = **0.70 effects/week ≤ 1/week** (PASS).

### Per-seed table (selected days / week)

| seed | selected days (t, 0-based) | count / 28 d | effects / week |
|---|---|---|---|
| 5001 | 9, 10, 11, 12, 13 | 5 | 1.25 |
| 5002 | 8, 10, 11, 13, 14, 15 | 6 | 1.50 |
| 5003 | — | 0 | 0.00 |
| 5004 | 14 | 1 | 0.25 |
| 5005 | 10, 13 | 2 | 0.50 |
| **pooled** | — | **14 / 140** | **0.70** |

### Plan criteria — PASS/FAIL

**(b) "chosen threshold selects only top-decile valence days with ≤1 effect/week expected frequency" → PASS**
- Only top-decile days selected: **TRUE** — selected set equals the top-14-by-rank set exactly (precision 14/14; every selected day's valence is in the pooled top decile).
- Expected frequency: **0.70/week ≤ 1/week → TRUE** (pooled expectation over days and seeds, per the plan's wording).

### Finding: high-valence days cluster into streaks (operational note)
Mood is autocorrelated (μ decay ρ=0.85, endogenous η AR(1) ρ_e=0.7), so top-decile days arrive in runs, not uniformly: seed 5001's five selected days are consecutive (t=9–13), seed 5002's six span t=8–15. A single month can therefore carry 5–6 effect-days (1.25–1.50/week) even though the long-run expected frequency is 0.70/week. This does **not** fail criterion (b) (which is stated on *expected* frequency), but an implementation should consider an optional hard guard: **at most 4 effects per 28 days per seed** (drop lowest-`p` extras). With that guard the study yields counts [4, 4, 0, 1, 2] → 11/140 = 0.55/week, and no month exceeds 1/week.

### Alternative observable rule (reference)
If the implementation prefers the directly observable daily mood: `M/N > 0.8` (strictly above the p90 of M/N). Selects 13/140 days (0.65/week ≤ 1), per-seed counts [4, 3, 1, 2, 3] (all ≤ 4/28 d), all selected days have M/N ∈ {0.9, 1.0} — also a PASS under both criteria. Note the measures diverge at the margins (binomial noise on N=10): the p-rule's 14 days include a couple with observed M/N as low as 0.6; the M/N-rule's days are all at the top of the observable scale. Both are defensible; the harness already replays the engine's daily `mood.step`, so either signal is reconstructable at runtime.

---

## Artifacts
- `experiments/c8_effects.py` — the study (seeded, deterministic; writes only `results/c8-effects/c8_effects.json`).
- `results/c8-effects/c8_effects.json` — full payload: distributions, threshold, per-seed selections, checks, supplementary cap analysis.
- `results/c8-effects/report.md` — this report.

## Reproduce
```
cd /home/vruizes/.hermes/projects/llh-wt-c-effects   # branch wip/c-effects
/home/vruizes/.hermes/projects/llm-behavioral-harness/.venv/bin/python -m experiments.c8_effects
```
