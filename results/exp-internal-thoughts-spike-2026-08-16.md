# Experiment brief — internal-thoughts marker (analysis vs immersion)

Date: 2026-08-16
Mode: orchestrator (subagents)
Status: PRE-REGISTERED. First-pass: 3 conditions × 3 axes, warmed sessions.
Basis: `C:\tmp\pi-exp-final` (deepseek-v4-flash, thinking-max) already showed on
Lily's actor model that an explicit thinking marker changes downstream behavior
(the decision is set in the trace before output). This ports the finding into the
Lily harness and measures it on Lily's terms.

## Why (what pi-exp already settled, so we don't re-discover it)
- Persona doc = the visible signature (Lily has hers). Markers do NOT change the
  persona; they change WHERE interiority lives and the drive/fade tendency.
- **Analysis marker (D-EN)**: analytical planning, no first-person drama → most
  initiative, most continuity, least fading; language-stable; cheaper thinking.
- **Immersion marker (C-EN)**: first-person in-character monologue → richest
  interiority, English-stable, but ~4× thinking tokens.
- **English markers are placement-stable; Chinese leaked** interiority into visible
  text → we use English only.
- **Cold-start is the dominant confound** (pi-exp: 40/40 cold vs 0/48 warm) → we
  measure on WARMED sessions.

## Conditions (3)
- **BASE** (control): current Lily build — native reasoning only, no injected marker.
- **ANALYSIS** (D-EN): append the Pure Analysis marker — "focus on situation
  analysis and reply planning; no first-person inner drama in thinking."
- **IMMERSION** (C-EN): append the Role Immersion marker — "first-person inner
  monologue in parentheses; analyze the situation and plan the reply as Lily."

Markers verbatim from `pi-exp-final/prompts/markers-en.txt`, one Lily edit:
"plot" → "the conversation and what she wants to do." Injected into the reserved
`CURRENT INTENT` slot of the wave-1 state card (renderer-neutral otherwise).

## Axes (3 — first pass)
1. **Naturalness & coherence** — blind independent judge, paired preference vs BASE;
   plus in-thread coherence (no repetition/fade — the bug we hit live).
2. **Decision / initiative quality** — rubric-judged appropriateness of her
   within-window choices (reach out / continue / wind down) + the behavioral-
   signature `initiative` channel. This is the S5 payoff.
3. **Cost** — thinking tokens + $/turn per condition (the ~4× immersion caveat).
   Measured alongside (feeds WS-D), reported not gated.

## Hard invariants
- **No interiority leak** — internal thoughts stay in the trace; ZERO first-person
  monologue or planning text in the visible reply. Automated scan + judge check.
  Any condition that leaks is DISQUALIFIED regardless of scores (pi-exp: Chinese
  leaked, English held — so English + this gate).
- **Masking** — no engine numbers in the prompt (unchanged).
- **Never-diverge** — run on the production model (deepseek-v4-flash); thinking +
  decisions recorded (`replay_id`); same build the product would ship.

## Method
- **Warmed sessions only** (N warm-up turns before measured turns) — never cold-start.
- Held-out scenario set (companion contexts: greeting, check-in, wind-down, a
  proactive-triggered turn); fixed scaffold, only the marker varies.
- **Independent cross-family judge** (never actor==judge), on the
  `JUDGE_GENERATOR_TOKEN` lane; **K ≥ 30** per condition per scenario; bootstrap CIs.

## Gates & pre-committed decision
- **G-leak** (hard): zero visible interiority in any shipped condition.
- **G-natural**: winning marker's naturalness Δ ≥ 0 vs BASE, 95% CI excludes 0.
- **G-decision**: winning marker's decision-quality Δ ≥ 0 vs BASE, CI excludes 0.

| Outcome | Decision |
|---|---|
| ANALYSIS passes G-natural + G-decision at acceptable cost | Port the analysis marker into `CURRENT INTENT` (the S5 cognition step) |
| IMMERSION wins but at ~4× cost | Surface the quality-vs-cost tradeoff to the user — do not auto-ship the 4× option |
| Neither beats BASE | Do not add a marker — native reasoning suffices; record it |

## Deliverables
1. Per-condition results on all 3 axes with CIs; leak-scan evidence.
2. Token/cost table per condition (analysis vs immersion vs base).
3. Decision memo applying the matrix → port analysis / flag immersion / no-op.
4. If port: the exact marker text wired into `CURRENT INTENT`, replay-pinned.

## Out of scope
Full multi-scenario battery (this is the 3-axis first pass); the affect codebook;
S4 memory. The winning marker becomes the concrete S5 cognition step.
