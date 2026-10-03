---
type: experiment-preregistration
title: replay-live-31b — replaying live companion calls on gemma-4-31B (card ablation + valence steering)
description: "Five frozen live-run chat calls replayed on gemma-4-31B: full context, minus the latest behavioral card, minus all bearing blocks, and with a valence control vector added."
tags: [llm-behavioral-harness, live-run, replay, steering, gemma-4-31b, emotion-geometry]
timestamp: 2026-10-03
---

# replay-live-31b — pre-registration (FROZEN before run)

Date: 2026-10-03. Owner request: replay certain calls from the current live companion run
(started 2026-09-27) on gemma-4-31B — full context, minus the latest behavioral card, and minus
card plus an emotion control vector (from the emotion-geometry results) or soft prompt.

## Question

Three questions in one matrix:
1. Can gemma-4-31B carry the live companion context at all (persona, sprite tags, state stream)?
2. What does the behavioral/affective bearing text (the state card's mood sections) do to a single reply?
3. Can a valence control vector substitute for (or add to) the bearing text?

## Scenarios (frozen)

Five live chat calls, verbatim requests from `results/live-companion/companion.db`
(read-only): call ids 188 (day 5, proactive reach-out), 162 (day 4, conflict), 144 (day 3,
intimate-flustered), 122 (day 3, playful), 107 (day 3, casual couch). Contexts 7.3k–11.4k tokens.

## Conditions (frozen)

- **A — full**: context verbatim (rendered, below).
- **B — no latest bearing**: remove the LAST `BEARING:` block (the latest behavioral/affective card).
- **B2 — no bearing**: remove ALL `BEARING:` blocks (affective + behavioral).
- **C — no bearing + steer**: B2 + valence control vector (α=+1.0).
- **D — full + steer**: A + valence control vector (α=+1.0).

Steering spec: direction = row 3 (valence) of `p2-directions-chat-gemma-4-31b.npz` L36
(chat-format, headline layer; oriented so positive projection = positive valence); scale
s = 14.12 (disp convention of the P2d native 31B run — word-stratified dispersion); hook =
output of `model.language_model.layers[35]`, active for the whole forward (prefill + decode).
This is the standard steering protocol from the emotion-geometry battery.

## Rendering (frozen)

Recorded OpenAI stream -> local chat template: system/user/assistant 1:1; assistant rows carrying
only tool_calls (empty content) dropped; tool rows -> system rows verbatim (harness text never in
user slots, per the repo's rule 5). Bearing-block surgery: blank-line blocks inside STATE UPDATE
rows; a mutated row keeps its other blocks and its `STATE UPDATE:` header; a row is dropped only
when nothing but the header remains. Generation: gemma-4 chat template, `add_generation_prompt`,
greedy (`do_sample=False`), `max_new_tokens=200`.

## Predictions (directional, single sample per cell — no significance claims)

- P1 format: every generation starts with a sprite tag from the persona's list (12 tags).
- P2 card: valence(A) > valence(B2) on average (the current bearings are bright/opening-up);
  |Δ| small (≥0.05 lexicon units) if the text matters at all.
- P3 steering: valence(C) > valence(B2), Δ ≥ +0.05; if null, gentle disp-scale injection is
  informative as a null.
- P4 D vs A: small positive shift on top of the card.
- P5 no card leaks in any condition (reply must not quote/summarize the card).
- P6 qualitative: 31B carries the persona (teasing tone, name-calling, ♡, no emoji).

## Metrics (frozen thresholds)

Reply length (chars); sprite-tag compliance (starts with `[tag]`, tag in list) ≥80% predicted;
NRC-VAD valence/arousal (mean over matched content words, NRC-VAD-Lexicon.txt); `♡` count;
name-calling count (dummy|silly|stupid|perv); kaomoji presence; emoji presence (must be 0);
leak markers (STATE UPDATE|BEARING|agenda|availability|card, case-insensitive) must be 0.
Recorded deepseek reply scored with the same metrics for qualitative comparison.

## Limitations

Single greedy generation per cell; cross-model comparison (31B vs recorded deepseek reply)
is qualitative (different sampling, different model); the live run keeps producing — this
replays a frozen snapshot. Raw scenario/generation files stay untracked (conversation content).

## Artifacts

`raw/scenarios.json` (untracked), `raw/generations.json` (untracked), `metrics.json`, `report.md`.
Code: `experiments/replay_live_31b/{extract,modal_replay,analyze}.py`.
