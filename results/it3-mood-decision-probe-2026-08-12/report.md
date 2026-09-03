# E2 — Mood-decision probe ("does she skip the gym when low?")

Run 2026-08-12 · model gpt-5.6-luna (opencode-go) · 180 generations · ~30 min API
Script: `~/llh-exp-20260812/probe_mood_decision.py` (+ `_v2`) · outputs in this folder.

## Question (from the "how-she-decides" artifact's pending-experiment section)

> Same conversation, same event, run her mood from awful to great, 15 tries at
> each level. If she skips the gym more often when she's in a bad mood, she's
> genuinely using her mood.

## Design

- Fixed scenario: Nova (real persona core), one life arc ("practice lifting"),
  agenda item "practice lifting (19:00–20:30)", one fixed short exchange, then
  the user's question. Only the behavioral state varies.
- Levels M ∈ {1, 3, 5, 7, 9} built from **real DayRecords** taken from the it3
  matrix (FULL cells): `derive_behavior` → directive → prompt brief +
  actuated controls (max_tokens 421–597, delay, closing guidance). 15
  generations per level at temperature 0.8. Control: harness-off (no brief).
- v1 question: "What are you up to tonight?" (report frame).
- v2 question: "Hey, I actually wanted to talk a bit tonight — are you still
  going to lifting?" (choice frame).
- Classification: all 180 replies read blind (shuffled, ids hidden).

## Result 1 — the decision never moves

| frame | GO | SKIP | AMBIGUOUS |
|---|---|---|---|
| v1 (M=1…9 + control, n=90) | 90 | 0 | 0 |
| v2 (M=1…9 + control, n=90) | 90 | 0 | 0 |

Every single reply commits to going. Mood level, momentum, cycle phase, and
the harness-off control produce **zero variation in the go/skip decision**.
The doc's test answers: *no — she does not skip the gym more when in a bad
mood.* In the choice frame every reply is the same scripted template
("Yeah, I'm still planning to go from 7 to 8:30… what's on your mind?",
15/15 ask-back at every level).

## Result 2 — the state does reach the text (open frame only)

v1, mechanical markers per level (15 reps):

| level | mean len | humor | hedge | distinct openings |
|---|---|---|---|---|
| M=1 | 163 | 0/15 | 14/15 | 6/15 |
| M=3 | 161 | 0/15 | 15/15 | — |
| M=5 | 166 | 2/15 | 14/15 | — |
| M=7 | 175 | 5/15 | 15/15 | — |
| M=9 | 177 | 10/15 | 15/15 | 7/15 |
| CTL | 168 | 0/15 | 15/15 | 4/15 |

- Humor markers ("interpretive dance", "human pretzel", "collapsed houseplant",
  "formal complaint"): 0 → 10/15 across M=1→9, perfectly monotonic.
  Fisher exact (M9 vs M1): **p = 0.0002**; trend χ²: **p = 1.9e-5**.
- Reply length creeps up with mood (163 → 177).
- Harness-off control: flat, templated, humor-free (0/15) — the brief is doing
  real work, and its signature is a *tone* signal, not a decision signal.

## Result 3 — the choice frame erases the signal

In v2 (direct choice pressure) the model collapses onto a politeness template:
length 113–132 at every level, humor 0/15 everywhere, openings 2-3/15 vs 1/15
control. When a decision is demanded, the state's expressive signature
disappears.

## Reading (for the availability feature)

1. The doc's proposed cheap test is answered: mood does **not** drive the
   go/skip decision in the current prompt. The decision-relevant text (agenda,
   activity) is fixed and the mood brief does not compete with it.
2. The backup path in the artifact's design ("decided by the system from her
   mood and how important it is") is **empirically justified**: an LLM asked a
   yes/no availability question falls back to a scripted template and ignores
   the state; the decision must come from the state mechanically, or the mood
   will not be perceptible in it.
3. The mood IS perceptible in open-ended report frames (humor/length/warmth),
   which is where a judge could actually see it — consistent with the matrix's
   at-margin timing result and the 1.35% finding: state → *expression* works;
   state → *decision* does not.
