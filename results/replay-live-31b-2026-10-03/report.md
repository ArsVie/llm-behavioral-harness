---
type: experiment-report
title: Replaying live companion calls on gemma-4-31B — behavioral card ablation + valence steering
description: "Five frozen live-run calls replayed on the gemma-4 family (100 generations). The 31B carries the persona cleanly; card ablation and gentle steering are surface-null; steering shows a monotone dose-response (+0.04..+0.09 valence) up to a coherence collapse threshold near 5x disp."
tags: [llm-behavioral-harness, live-run, replay, steering, gemma-4, emotion-geometry]
timestamp: 2026-10-03
---

# Replaying live companion calls on gemma-4-31B

Owner request: replay certain calls from the running live companion on 31B — full context,
minus the latest behavioral card, minus all behavioral cards, and with an emotion control
vector added — plus any other experiment worth chasing with this data.

## What ran

Five frozen calls from the live companion run (2026-09-27..): ids 188 (proactive reach-out),
162 (conflict), 144 (intimate-flustered), 122 (playful), 107 (casual). Contexts 7k–11k tokens,
rendered through the gemma-4 chat template exactly as recorded (tool rows become system rows).

Four runs, 100 generations total (greedy, max 200 tokens, single sample per cell):

| run | model | conditions | n |
|---|---|---|---|
| main | gemma-4-31B (nf4, L40S) | A full · B minus latest bearing · B2 minus all bearings · C = B2 + valence steer (disp) · D = A + steer | 25 |
| mult dose | gemma-4-31B | C, D at 5x disp (s=73.2) | 10 |
| cross-model | gemma-4-e2b / e4b / 12b | A, B2, C (each model's own disp scale) | 45 |
| dose sweep | gemma-4-31B | C at s = 21.2 / 28.2 / 42.4 / 56.5 | 20 |
| samples | gemma-4-31B | A, B2, C42 (s=42.4) x 4 samples, temp 0.8 (the live setting) | 60 |

Steering = additive valence direction (row 3 of the chat-format directions at the headline
layer) scaled by each model's dispersion unit. The bearings removed in B/B2 are the repeated
"Current bearing: bright/even, …" and "You reach out when… / You respond readily…" blocks
inside STATE UPDATE rows; state facts and reach-out instructions stay.

## Headline

1. **The 31B carries the live lane cleanly.** 100/100 generations open with a valid sprite tag,
   no condition leaks card text (0/100), no emoji, tone and content in-line with the recorded
   deepseek replies — e.g. call 162 in both: "I do not snore, you liar. ♡ [smug] Weekend, then…".
   Replies stay short (≈190 chars, comparable to the recorded ones).

2. **Card ablation is surface-null in 31B, small-positive in the smaller models.**
   Mean reply valence with vs without all bearings: 31B −0.012 (greedy; temp-0.8 samples
   +0.009 ± 0.020 SEM — null); e2b +0.056 (5/5 calls positive); 12b +0.099 (4/5);
   e4b −0.024 (mixed). Removing only the latest bearing (B) moves nothing beyond B2 (+0.011).

3. **Gentle steering (disp) is surface-null in 31B, small-positive below.** 31B +0.010;
   e2b +0.015, e4b +0.049, 12b +0.037. At this dose the steering instead changes *which*
   continuation is chosen in some cells (call 162 turns into "you just wanted an excuse to
   sneak away…"), without moving the lexicon valence.

4. **Steering dose-response is monotone, then collapses.** 31B condition C, mean valence:
   0.498 (no steer) → 0.534 (s=21.2) → 0.544 (28.2) → 0.567 (42.4) → 0.583 (56.5) → degenerate
   at 73.2 (repetition ratio 0.60; "a la… la l… la l…" loops). 17/20 sweep cells sit above
   their own no-steer baseline. Coherence is clean through 42.4, mild onset at 56.5 (rep 0.03
   on two calls), broken at 73.2. Sampled at temp 0.8 the s=42.4 dose holds: +0.071 ± 0.025
   SEM vs B2, 5/5 calls positive.

5. **The card is not what drives a single reply; the persona and history are.** With all
   bearings removed the 31B still produces in-character, on-scene replies; the measurable
   marginal contribution of the card text at the single-reply surface is below the floor
   for 31B. Smaller models, anchored less strongly by context, show the card and the steering
   a little more.

## Verdict against the pre-registered criteria

- P1 format (sprite tag): **met** — 100/100 generations.
- P2 card (valence A > B2, |Δ| ≥ 0.05): **not met on 31B** (−0.012 greedy; +0.009 ± 0.020
  under temp-0.8 sampling). Directionally met on e2b (+0.056) and 12b (+0.099), not on e4b
  (−0.024). Extension finding, not pre-registered for those models.
- P3 steering (C > B2, Δ ≥ 0.05 at disp): **not met on 31B** (+0.010); small-positive on
  e2b/e4b/12b (+0.015/+0.049/+0.037). The dose sweep (post-hoc extension) shows the
  effect is real but dose-gated: +0.036..+0.085 for s in 21.2..56.5.
- P4 steering on top of the card (D vs A): **null** (+0.006).
- P5 no card leaks: **met** — 0/100.
- P6 persona (tone, name-calling, ♡, no emoji): **met**.

Greedy decoding; no sampling seeds. Thresholds as frozen in the pre-registration.

## Read

- There is a usable steering window for the live lane: ~1.5x–4x disp moves reply valence
  without breaking the text (31B). Below it, the surface is inert; above it, generation
  collapses. The window's floor is why the disp-scale experiments in the emotion-geometry
  battery never moved generated text.
- Effects are small (≤0.1 lexicon units) with n=5 calls and one greedy sample per cell.
  Read them as directions, not magnitudes. The dose-response monotonicity is the strongest
  single signal here.
- The 31B is a credible drop-in for the live lane at the single-reply level: format, tone,
  no leaks, content parity.

## Limitations

The main runs are single greedy samples; the temp-0.8 x4-sample extension puts the sampling
spread at roughly ±0.02–0.04 per cell and confirms the dose effect (5/5 calls positive) while
keeping the card effect inside noise. Still: n=5 calls; the NRC-VAD lexicon mean over content
words is a proxy for tone; cross-model steering uses each model's own scale (not a common
dose); the recorded deepseek replies differ in sampling and model, so 31B-vs-recorded is
qualitative only.

## Artifacts

- Pre-registration (frozen before the main run): `results/replay-live-31b-2026-10-03/README.md`
- Scenarios (untracked, conversation content): `raw/scenarios.json`
- Generations (untracked): `raw/generations{,-mult,-xmodels,-dose,-samples}.json`
- Metrics + side-by-side: `metrics.json`, `table.md`
- Code: `experiments/replay_live_31b/{extract,analyze,modal_replay,modal_replay_models,modal_replay_dose,modal_replay_samples}.py`
- Modal runs: ap-4W4FD3Mh4jMJyjJdrvuivZ (main), ap-scEm94amFSx11RUVeFMlHN (mult),
  ap-b7qYoAbQ0rgEZF5BXTDUjT (cross-model), ap-cU0kwQs63CzbJoPFfaZvDt (dose),
  ap-THkt8sFVQgZ6SKPMcOJ5Gn (samples)

## Next ideas

- Sample 3–5 seeds at temp 0.8 to separate dose effect from sampling noise.
- Ablate the card over a multi-turn continuation instead of one reply (single-reply may be
  the wrong resolution for a slow-state signal).
- Use the 2–4x disp window for any future live-lane steering experiments.
- Replay a tool-decide call (the wire dumps hold the full tool menu) to probe local
  tool-calling on 31B.
