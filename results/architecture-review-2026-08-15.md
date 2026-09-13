# Architecture review — results, known issues, risks (2026-08-15 snapshot)

Point-in-time results and second-opinion handoff, split out from the architecture
overview ([docs/architecture-overview.md](../docs/architecture-overview.md)) so the
architecture doc stays a living reference and this stays a dated snapshot.
Status-update notes (2026-08-17) mark what has since been resolved.

Confidence tags: **[V]** verified by independent recompute this cycle; **[P]**
provisional (small n, single model, or one run); **[D]** design intent, not yet
measured.

---

## 1. Results

### 1.1 Mood → behavior is real, and correctly scoped [V]
On discretionary decisions the latent state measurably moves behavior:
- s06 conversation-terminate probability: valence 52% → 88% (p=0.012); energy
  0% → 64% (p<0.0001), K=25 per cell, real model.
- On **boundary/obligation** decisions the effect is flat — the *correct*
  discipline (she shouldn't skip a committed event because she's in a good mood).

Caveat: single model (deepseek-v4-flash as judge/actor in probes), moderate K.

### 1.2 Renderer quantization — found and fixed [V]
The ±0.35 / 0.35–0.7 bucketing collapsed mood to ~9 states; verified two distinct
moods produced byte-identical briefs. Fixed to a finer band grid (8×6 = 48 states)
with an anti-collapse regression test. (Note: the separate value→words *codebook*
attempt to replace this renderer was later SHELVED — see the spikes registry.)

### 1.3 Timing elasticity — corrected from a false null [V]
An early "mood barely affects proactivity (−1.35%)" result was **n=5 sampling
noise**. Re-measured at n=500 seeds through the frozen `next_event`:
- state multiplier `S_d` elasticity ≈ **+15%** proactive-count swing across the
  clipped range; underlying hazard swings **+63%** across the clip.
- Practical proactivity envelope ≈ **31–58 msgs/month** within the `S_d` clip;
  structural envelope (guards only) ≈ 15–90.

### 1.4 Corpus quality & judge reliability — a caution [V]
- The iteration-2 corpus was **27.7% blank** (579/2090 empty companion turns); the
  report had claimed "2 empty instances." The judge did not detect the blanks.
- The judge aggregation had a keying bug (pass-2 overwrote pass-1). Fixed.
- **Takeaway:** historical eval headline numbers should be treated with suspicion
  unless recomputed; corpus-integrity invariants are not yet hard gates.

### 1.5 Tier-1 masking [V]
Accepted — the state is not trivially leaking into the prompt text; the behavior
shift comes from the rendered brief, not from seeing the numbers.

### 1.6 Live trial, day 1 — product-layer findings [V]
First real anchored Telegram session (conv-3) exposed four product issues, all
specced in [docs/spec-context-events-time-2026-08-15.md](../docs/spec-context-events-time-2026-08-15.md)
(S1–S6). Their current status is in §2.

---

## 2. Known issues (status updated 2026-08-17)

| ID | Issue | Severity | Status (2026-08-17) |
|---|---|---|---|
| S1 | No real timestamp on events/conversations/messages (virtual `t_h` only) | high | **DONE** — RealTimeAnchor / `real_at` (wave-1 W1) |
| S2 | Agenda never expires; no current-time line → time-blind | high | **DONE** — time-aware assembler + current-time line (wave-1 W2) |
| S3 | Idle-close threshold 12 h; proactive appends to stale-open conversation | medium | **RESOLVED as away≠close** — 15-min silence marks the user *away* (dormant, presence signal), not a close; conversations close only at a checkpoint. See the architecture overview / BACKLOG |
| S4 | Flat global message tail crosses conversation boundaries; summaries not wired into the transcript | high | **PARTIAL** — state-card sectioning landed (wave-1 W3); continuity-across-conversations + no-rebuild-from-summary remain open |
| S5 | Decision/steering + availability layers exist but were OFF live | high | **OPEN** — still env-gated off; events not yet reasoned over as time passes |
| S6 | Agenda activities are template strings, not generated | low | **OPEN** |

The engine and its determinism are **not** in question — these are all in the
product/context/orchestration layer above the frozen core.

---

## 3. Risks & caveats

- **Single-model dependence.** Behavioral effects were measured with one model as
  both actor and judge; an independent cross-provider judge lane now exists (local
  judges) but cross-model generalization is still lightly verified.
- **Sample sizes.** Several results are K=25 or n=5–500 depending on the leg; the
  tiny-n false-null (§1.3) is a live reminder to check n before trusting a direction.
- **Corpus integrity is not gated.** Blank-turn and judge-agreement invariants are
  recommended but not enforced (§1.4).
- **Product vs. engine confidence gap.** The stochastic engine is well-tested and
  frozen; the product layer is where the live defects are and is being iterated.

---

## 4. Questions raised for second opinion (2026-08-15)

1. Is the frozen-engine + LLM-renderer split the right architecture, or should more
   behavior be model-driven?
2. Is the mood→behavior effect size (§1.1/§1.3) product-meaningful, or too subtle?
3. For event reasoning (S5): should events route through the decision layer by
   default, and should she reason over them autonomously during inactivity?
4. Is per-conversation history + summary compression (S4) the right context model?
5. Are we over-investing in measurement rigor relative to product iteration speed?
