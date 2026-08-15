---
type: experiment-report
title: C1 — emoji reactions (setMessageReaction): energy-driven reaction policy, sim 5x14
description: "Sim study of a log-only reaction-actuation decision per user message driven by the energy channel (BehaviorDirective.energy): Spearman rho(reaction-rate, daily energy), frequency cap, Telegram Bot API capability check."
seeds: [5001, 5002, 5003, 5004, 5005]
model: fake (FakeClient)
mode: sim
timestamp: 2026-08-15T11:03:21Z
tags: [llm-behavioral-harness, c1, reactions, energy-channel, telegram]
---

# C1 — Emoji reactions (`setMessageReaction`) — experiment report

Run 2026-08-15T11:03:21Z · mode **sim** (FakeClient) · 5 seeds × 14 virtual days · 280 user messages · 70 companion-days (280 reaction-actuation decisions LOGGED, nothing sent).

Plan: `plans/advisor-orchestration-2026-08-15.md` Part 3 §C1. Boundary: only `experiments/` and `results/` were touched; no harness/engine/sim file was modified; no API call was made (decisions are log-only).

## Methodology

- **Sim.** Scripted user sends 4 messages/day at fixed local hours 9.0 / 12.5 / 17.0 / 20.5 (identical across seeds). Every message is processed by the harness `Session` (engine: mood/cycle/phase state; `synthetic_score=True`, `feedback=True` — deterministic per seed, replay-identical to `sim.run_daily`). Replies come from `FakeClient`. Temp SQLite stores are deleted after each seed.
- **Energy channel.** The decision input is `TurnResult.directive.energy` — the energy channel computed by `harness/behavior.py::derive_behavior` at the message hour from `engine.circadian.energy(hour, phase_label, timing)`. Identity check in the sim: directive.energy ≡ circadian energy at every message (280/280; max abs diff 0.00e+00). Daily energy = mean of the 24 h energy curve for the day's phase (0.5 h samples).
- **Policies (log-only).** PRIMARY — threshold: react iff `energy >= 0.735` (design constant; see calibration note). CONTRAST — linear-Bernoulli: p = clamp(0.05 + b·E, 0, 1) with b calibrated on seed 5001 so mean p = 0.30, decisions drawn from an independent per-(seed, day) numpy stream (never touches the engine's RNG). The contrast shows why the deterministic threshold form is required.
- **Statistics.** Spearman rho pooled over the 70 (seed, day) points; 95% CI by pair bootstrap (10,000 resamples) and by seed-block bootstrap (5,000 resamples — conservative under within-seed autocorrelation). Frequency = total reactions / total user messages (pooled), plus per-seed rates. Mean ± SD across seeds for daily rate.

### Calibration note (threshold theta)

Per the DESIGN energy table (`engine/types.py`), the channel's daily-mean level by phase is menstrual ≈ 0.45 · follicular ≈ 0.65 · ovulatory ≈ 0.70. The threshold sits above the follicular daytime level so reactions concentrate on high-energy (ovulatory/late-follicular) days while the pooled frequency stays under the 1-in-3 cap. The theta sweep in the results below verifies the choice on the simulated data.

## Results

### Per-seed table (primary threshold policy)

| seed | phases seen (start→end) | mean daily energy | mean daily rate | freq (react/msg) | per-seed rho |
|---|---|---|---|---|---|
| 5001 | menstrual→ovulatory | 0.568 | 0.161 | 0.161 | 1.000 |
| 5002 | menstrual→ovulatory | 0.568 | 0.161 | 0.161 | 1.000 |
| 5003 | menstrual→ovulatory | 0.568 | 0.161 | 0.161 | 1.000 |
| 5004 | menstrual→ovulatory | 0.568 | 0.161 | 0.161 | 1.000 |
| 5005 | menstrual→ovulatory | 0.586 | 0.196 | 0.196 | 1.000 |

### Pooled statistics

| stat | threshold policy | linear contrast |
|---|---|---|
| Spearman rho(rate, daily energy) | **1.000** | -0.081 |
| 95% CI (pair bootstrap, 10k) | 1.000 – 1.000 | -0.319 – 0.161 |
| 95% CI (seed-block bootstrap, 5k) | 1.000 – 1.000 | — |
| reaction frequency (pooled) | **0.168** (0.168 ≤ 1/3) | 0.314 |
| daily rate mean ± SD (across seeds) | 0.168 ± 0.158 | — |
| total reactions / total messages | 47 / 280 | 88 / 280 |

The threshold policy's daily rate is a deterministic monotone step function of the phase-driven daily energy level, so the correlation is at ceiling by construction; the binding constraints are the frequency cap (theta calibration) and API availability. The linear-Bernoulli contrast shows the same energy signal *cannot* meet criterion (1) when the decision is stochastic at realistic message volumes (per-day Bernoulli noise dominates the ~0.25-wide phase signal): rho ≈ -0.08, CI including 0. A channel-side implementation should therefore use the deterministic threshold form.

### Theta sensitivity (threshold policy, pooled)

| theta | freq | rho |
|---|---|---|
| 0.700 | 0.314 | 1.000 |
| 0.705 | 0.314 | 1.000 |
| 0.710 | 0.314 | 1.000 |
| 0.715 | 0.314 | 1.000 |
| 0.720 | 0.314 | 1.000 |
| 0.725 | 0.314 | 1.000 |
| 0.730 | 0.189 | 1.000 |
| 0.735 | 0.168 | 1.000 |
| 0.740 | 0.168 | 1.000 |
| 0.745 | 0.168 | 1.000 |
| 0.750 | 0.168 | 1.000 |
| 0.755 | 0.043 | 0.541 |
| 0.760 | 0.043 | 0.541 |
| 0.765 | 0.043 | 0.541 |
| 0.770 | 0.043 | 0.541 |
| 0.775 | 0.043 | 0.541 |
| 0.780 | 0.043 | 0.541 |
| 0.785 | 0.043 | 0.541 |
| 0.790 | 0.021 | 0.541 |
| 0.795 | 0.021 | 0.541 |
| 0.800 | 0.021 | 0.541 |
| 0.805 | 0.021 | 0.541 |

## Criterion 3 — Telegram Bot API capability (`setMessageReaction`)

**VERDICT: ALLOWED** — bots can use `setMessageReaction` in **private chats**.

- Source: https://core.telegram.org/bots/api#setmessagereaction (official Telegram Bot API docs, retrieved 2026-08-15).
- Changelog: https://core.telegram.org/bots/api-changelog — Bot API 7.0 (Dec 2023): "Added the method setMessageReaction that allows bots to react to messages"; also announced on @BotNews: "Bots can now react to messages with setMessageReaction."
- Notes: chat_id accepts any chat (private chats included; no private-chat exclusion, contrast sendChatAction). Constraints: bots cannot use paid reactions; as non-premium users bots can set up to ONE reaction per message (matches the <=1/3-per-message policy); custom emoji only if already present on the message or allowed by chat admins (use default emoji); service messages of some types cannot be reacted to. Reaction updates (MessageReactionUpdated) are delivered to bots, so reactions are observable.

Official method text: *"Use this method to change the chosen reactions on a message. Service messages of some types can't be reacted to. Automatically forwarded messages from a channel to its discussion group have the same available reactions as messages in the channel. Bots can't use paid reactions. Returns True on success."* Parameters: `chat_id` (any chat, private chats included — no exclusion, unlike `sendChatAction`), `message_id`, `reaction` (Array of ReactionType; *"Currently, as non-premium users, bots can set up to one reaction per message"*), `is_big`.

## Verdicts

| criterion | requirement | result | verdict |
|---|---|---|---|
| 1 | rho ≥ 0.5, bootstrap 95% CI excludes 0 | rho = 1.000, CI 1.000–1.000 (pair), 1.000–1.000 (block) | **PASS** |
| 2 | frequency ≤ 1 per 3 user messages | 0.168 reactions/message (≤ 0.333) | **PASS** |
| 3 | API capability confirmed against Bot API docs | setMessageReaction available to bots in private chats (1 reaction/message cap) | **PASS** |

**OVERALL: PASS** — pass ⇒ implement channel-side only (threshold on directive energy, deterministic, `setMessageReaction` with a default emoji; emoji choice may later be valence-mapped).

