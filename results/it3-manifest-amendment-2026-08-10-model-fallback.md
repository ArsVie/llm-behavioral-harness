# Amendment 2026-08-10 — generation model fallback (G3/G5)

**Decision (autonomous, user's preregistered escalation rule):
switch the generation model from deepseek-v4-flash to gpt-5.6-luna for G3/G5.**

## Evidence
- deepseek-v4-flash returned 100% empty responses (200 + empty content)
  from 03:23 until the moment of this fallback (30 consecutive
  probes, ~1h). The 4 G3 cells launched in that period died
  with the same RuntimeError (4-7 attempts exhausted).
- gpt-5.6-luna (same opencode-go endpoint, same key) returned
  normal content during the episode (probes with real finish/usage).
- User rule (it2): a model with a high empty-response rate is unfit
  for a 30-day corpus — escalate to a model change before G5.

## Decision
- G3+G5 generation: gpt-5.6-luna. All matrix cells use the SAME
  model (comparability within the experiment intact).
- Judges (G6): families unchanged (opencode-flash / opencode-luna); if
  flash stays degraded in G6, family 1 is re-evaluated with luna and
  reported (disagreement between families is already the design).
- Reversible: if flash recovers, re-running G3/G5 with the frozen
  model is an orchestrator review decision.

## Preregistered status
- The it3 manifest (results/it3-g4-manifest-*.json) freezes the generation
  model; this amendment modifies it BEFORE matrix generation
  (G4 is the review gate — the orchestrator reviews on wake-up).
