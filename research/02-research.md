---
type: research-note
title: "Phase −1 Prior-Art Research: Affective/Hormonal AI Agents and Stochastic Timing"
description: "Deep-read of arXiv 2508.11829, affective-agent literature, point-process timing models (NHPP, Hawkes, Gamma), and parameter recommendations."
tags: [prior-art, affective-agents, hormonal-cycle, point-process, hawkes, parameters]
timestamp: 2026-06-23
---

# Phase −1 Prior-Art Research: Affective/Hormonal AI Agents and Stochastic Timing

**Date:** 2026-06-23  
**Scope:** arXiv 2508.11829 deep-read · affective-agent literature · point-process timing models · parameter recommendations

---

## 1. arXiv 2508.11829 — Detailed Summary

### Citation
Leigh Levinson and Christopher J. Agostino. *"Every 28 Days the AI Dreams of Soft Skin and Burning Stars: Scaffolding AI Agents with Hormones and Emotions."* arXiv:2508.11829 (submitted 15 August 2025, NeurIPS 2025 Creative AI track).  
Abstract: https://arxiv.org/abs/2508.11829  
HTML full-text: https://arxiv.org/html/2508.11829v1

---

### 1.1 Core Thesis and Motivation

The paper frames the **frame problem** — the difficulty an AI has deciding what is relevant at any given moment — as solvable by embedding biological rhythms. The central claim is that *"biological rhythms, particularly hormonal cycles, serve as natural relevance filters."* Rather than engineering explicit relevance logic, the authors hypothesize that a simulated hormonal-cycle scaffold naturally gates which information, memories, and affective tones the model attends to, mirroring how biology does it in humans.

---

### 1.2 Hormones Modeled

The framework models **seven hormonal/physiological signals**:

| Signal | Role in the cycle |
|--------|-------------------|
| Estradiol (Estrogen) | Rises in follicular phase, peaks at ovulation, drops in luteal |
| Progesterone | Rises sharply after ovulation, falls at menstruation |
| Testosterone | Moderate, rises around ovulation |
| Cortisol | Stress hormone; also has a strong circadian component |
| Luteinizing Hormone (LH) | Sharp mid-cycle surge triggering ovulation |
| Follicle-Stimulating Hormone (FSH) | Rises at cycle start, stimulates follicle growth |
| Basal Body Temperature (BBT) | Rises ~0.2°C post-ovulation (progesterone effect) |

**Important caveat (confirmed from paper text):** *"As no widely accepted functional forms for these hormones exist, we engineered a set of periodic functions with added Gaussian noise to simulate the natural shapes and fluctuations."* The paper does **not** publish the exact mathematical forms or amplitude parameters of these periodic functions. This is a reproducibility gap.

---

### 1.3 Emotions Measured

Emotions were **not explicitly programmed** but were **emergent** from the hormonal-state system prompts, measured via the **NRC Emotion Lexicon** applied to the model's generated text. Five core emotions tracked:

| Emotion | Cycle-phase peak | Statistical result |
|---------|------------------|--------------------|
| Sadness | Menstrual phase | F = 9.07, p < 0.0001 |
| Happiness | Ovulatory phase | F = 5.02, p = 0.0019 |
| Fear | Nocturnal (circadian) | Elevated at night |
| (Creativity, general affect) | Moderate cortisol window | Qualitative |

The mapping is inferred, not pre-programmed — the language model's own latent emotion-language associations produce the variation when conditioned on hormonal state descriptions.

---

### 1.4 The 28-Day Cycle: Phase Breakdown

The cycle is divided into four canonical phases. The paper cites standard endocrinology without giving precise day-by-day numbers; using biological norms (confirmed from multiple clinical sources: https://www.raleighob.com/phases-of-the-menstrual-cycle/, https://helloclue.com/articles/cycle-a-z/the-menstrual-cycle-more-than-just-the-period):

| Phase | Approximate Days | Key Hormones | Modeled Mood Signature |
|-------|-----------------|--------------|------------------------|
| **Menstrual** | Days 1–5 | Estrogen ↓, Progesterone ↓ | Sadness peak, low energy |
| **Follicular** | Days 6–13 | Estrogen ↑, FSH ↑ | Rising optimism, focus |
| **Ovulatory** | Days 14–16 | LH surge, Estrogen peak, Testosterone ↑ | Happiness peak, sociability, creativity |
| **Luteal** | Days 17–28 | Progesterone ↑ then ↓, Cortisol variable | Introspection, mood volatility near end |

The paper notes that ovulation timing is *"generally between 10–16 days before onset of menstruation"* — consistent with the 28-day scaffold having a ~day-14 peak.

---

### 1.5 Mathematical Formulations

**CONFIRMED ABSENCE:** The paper contains no explicit equations. There are no Poisson process formulas, no binomial mood transition matrices, and no P[(λ+μ)·α] expression in the published text. The paper is largely a *proof-of-concept narrative study* rather than a formal mathematical framework.

What is described:
- **Periodic functions with Gaussian noise** used to generate hormone trajectories (no closed-form given).
- **System prompts generated per phase**, conveying hormonal/emotional state in natural language — e.g., prompts situating the agent with contextual detail ("being at a hardware store in Argentina") to add narrative realism.
- **No spontaneous-message timing model** is described; the paper does not address when the agent would initiate contact.

The references in early metadata to λ, μ, α parameters appear to be from the paper's citations to Hawkes-process literature, not from equations the paper itself introduces. **This is inferred, not confirmed from full text.**

---

### 1.6 Circadian Rhythm Model

The circadian component is a **24-hour overlay** on the 28-day cycle. Qualitative findings:

- **Morning:** Highest task performance; positive affect dominates ("morning optimism").
- **Afternoon:** Moderate performance.
- **Night:** Lowest performance; sadness and fear peak ("nocturnal introspection").
- **Cortisol optimal window:** A "40–60% of peak" cortisol level is identified as the sweet spot correlating with *flow state* — the model performs best at moderate cortisol, degrading at both very low and very high levels (inverted-U, consistent with Yerkes-Dodson).

Circadian implementation: the authors used discrete time-of-day bins (Morning / Afternoon / Evening / Night) rather than a continuous sinusoidal function.

---

### 1.7 Validation

Benchmarked across four standard NLP datasets:

| Dataset | Domain | Finding |
|---------|--------|---------|
| **SQuAD** | Reading comprehension | Hormonally-charged prompts *consistently outperformed* baseline |
| **MMLU** | Multi-domain knowledge | Subtle but consistent hormonal-phase variation |
| **HellaSwag** | Commonsense reasoning | Phase-aligned performance pattern |
| **AI2 ARC** | Science QA | Hormonally-charged prompts outperformed baseline |

**Statistical note:** The paper reports that *"all p > 0.11"* — the performance variations do not reach conventional significance thresholds. The authors interpret this as "subtle but consistent trends aligning with biological expectations" rather than strong effects. The study is exploratory and the team acknowledges the lack of formal reproducibility (no code released, no equation appendix).

---

### 1.8 Critical Gaps in the Paper

1. No mathematical formulas for hormone trajectories.
2. No explicit prompt templates published.
3. No spontaneous-message timing model.
4. Results are not statistically significant (p > 0.05).
5. The system prompt engineering is a narrative approximation — not a computational state machine.

These gaps are precisely the implementation space our engine must fill.

---

## 2. Affective-Agent Literature Survey

### 2.1 Foundational Emotion Representation

**PAD (Pleasure-Arousal-Dominance) model** — Mehrabian & Russell (1974); extended to agents by Lisetti & Gmytrasiewicz. The three continuous dimensions span [−1, +1] (or [−10, +10] in some implementations):

- **Pleasure (Valence):** positivity vs. negativity of the affective state
- **Arousal:** activation level, energy, alertness
- **Dominance:** sense of control vs. submission

PAD is the de facto standard for continuous emotion representation in agent simulation. See overview: https://grokipedia.com/page/PAD_emotional_state_model

The **Valence-Arousal (VA) circumplex** (Russell 1980) is the 2D simplification: it captures most variance, maps to quadrants (happy = high V + high A; sad = low V + low A; calm = high V + low A; angry = low V + high A).

---

### 2.2 Dual-Speed Emotion Dynamics (Slow Mood / Fast Emotion)

The Sentipolis framework (Agostino et al., arXiv:2601.18027, https://arxiv.org/abs/2601.18027) implements **dual-speed dynamics** in PAD space — confirmed from abstract:

- **Fast emotion:** Immediate reaction to stimulus; short timescale (seconds to minutes); directly triggered by events.
- **Slow mood:** Background affective baseline; long timescale (hours to days); drifts gradually toward personality baseline; modulates how fast emotions are interpreted.

This is the architecturally important separation. In our engine: hormonal state sets the **mood baseline** (slow), while each conversation turn can produce **transient emotion spikes** (fast) that decay back toward the hormonal baseline. The paper also describes **emotion-memory coupling** — past emotional states influence memory retrieval salience.

---

### 2.3 OCC and Appraisal Theory

The **OCC model** (Ortony, Clove & Collins 1988) generates emotions through *cognitive appraisal* of events relative to goals, standards, and attitudes. It is the most-cited agent emotion framework and maps to 22 emotion types organized by:

- **Consequences of events** (for self or others) → joy, distress, happy-for, sorry-for, resentment, gloating
- **Actions of agents** (praise/blame) → pride, shame, admiration, reproach
- **Aspects of objects** (liking/disliking) → love, hate

Recent LLM work (arXiv:2508.05880, https://arxiv.org/html/2508.05880v1) analyzed 15 emotion categories using 8 appraisal dimensions from Smith & Ellsworth (1985): pleasantness, attentional activity, control, certainty, goal-path obstacle, legitimacy, responsibility, and anticipated effort. Key finding: LLMs broadly align with human appraisal structures but show idiosyncratic biases per model.

The **chain-of-emotion architecture** (Fung et al., PMC11086867, https://pmc.ncbi.nlm.nih.gov/articles/PMC11086867/) shows that making appraisal *explicit* (asking the LLM "How does [character] feel right now?") before generating responses improves emotional coherence in game agents. No mathematical formulas — emotion is delegated to language.

---

### 2.4 Mechanistic Emotion in LLMs

The most technically rigorous recent work (arXiv:2604.00005, https://arxiv.org/html/2604.00005v1) uses **Sparse Autoencoders (SAE)** to identify VAD-aligned neurons in transformer hidden states. Key findings for our engine:

- Emotion can be **steered** via direction injection: `h̃_k = h_k + α·∑(d̃_i)` where d̃_i are normalized VAD direction vectors injected at layer k=17 (Qwen3-8B).
- Behavioral impacts follow **non-monotonic (inverted-U) curves** over arousal/dominance — there is an optimal band, not a monotone relationship.
- Positive valence improves objective-task performance by ~3.4%.
- High dominance improves agent planning performance by ~79.8% vs. low dominance.
- Low valence + low arousal reduces safety-critical failures by 52–68%.

**Implication for our harness:** Rather than raw prompt injection, consider that VAD values should be bounded to moderate ranges to avoid performance degradation at extremes.

---

### 2.5 Circadian and Environmental Modulation

From the Sentipolis abstract and related work:
- Agents with positive personality show stronger positive mood components in **morning simulations**.
- **Weather** (as environmental input) reinforces mood: clear weather → positive; rainy/cloudy → negative.
- Circadian variation in **subjective energy and arousal** has been confirmed in simulation using diurnal PAD modulation.

Reference: arXiv:2203.06935 systematic review of affective computing models (https://arxiv.org/pdf/2203.06935).

---

## 3. Timing Models: NHPP vs. Hawkes vs. Gamma

### 3.1 Baseline: Homogeneous Poisson Process

A standard Poisson process has constant rate λ. Inter-arrival times are **exponential** with mean 1/λ — memoryless (the past gives no information about when the next event arrives). For message timing: every minute is equally likely to produce a message, regardless of history. This is too uniform; real messaging is bursty and diurnal.

---

### 3.2 Non-Homogeneous Poisson Process (NHPP)

**Definition:** Rate function λ(t) varies with time. The number of events in (s, t] follows Poisson with mean:

```
Λ(s,t) = ∫_s^t λ(u) du
```

The nth arrival time density is:
```
f_n(t) = [m(t)^(n-1) / (n-1)!] · r(t) · exp(−m(t))
```

where m(t) = ∫₀ᵗ r(u) du. Inter-arrival times are **not exponential** when r(t) is non-constant.

**Simulation:** Generate standard Poisson(rate=max λ), then thin with acceptance probability λ(t)/λ_max at each candidate event (Lewis-Shedler thinning algorithm).

**For our engine — diurnal λ(t):**  
A sinusoidal rate function captures the day/night pattern:

```
λ(t) = λ_mean · [1 + A · cos(2π(t − t_peak)/24)]
```

where:
- `t` = hour of day (0–24)
- `t_peak` = hour of peak activity (e.g., 14.0 for 2 PM)
- `A` = amplitude (0 < A < 1; A=0.7 gives a 5:1 peak-to-trough ratio)
- `λ_mean` = average daily message rate (messages/hour)

**Cycle-phase modulation:** Multiply λ_mean by a phase factor P(phase):
- Menstrual: P = 0.6 (withdrawn)
- Follicular: P = 1.0 (baseline)
- Ovulatory: P = 1.4 (outgoing, initiates contact more)
- Luteal (early): P = 1.1
- Luteal (late, PMS): P = 0.8

Source: https://www.randomservices.org/random/poisson/Nonhomogeneous.html

**Limitation:** NHPP has **no memory** — events don't influence each other. It captures diurnal patterns but not the conversational burstiness (reply clusters).

---

### 3.3 Hawkes Process (Self-Exciting Point Process)

A Hawkes process adds **self-excitation**: each event raises the intensity for subsequent events, then the effect decays. Intensity function (exponential kernel):

```
λ*(t) = μ + Σ_{t_i < t} α · exp(−β · (t − t_i))
```

Parameters:
| Symbol | Name | Meaning |
|--------|------|---------|
| μ | Baseline rate | Background message rate (events/hour) |
| α | Excitation magnitude | How much each message raises the rate |
| β | Decay rate | How fast the excitation decays (units: 1/hour) |
| η = α/β | Branching ratio | Expected number of cascade events per trigger; **must satisfy η < 1** for stationarity |

**Practical parameter ranges (inferred from academic simulation literature):**

| Use case | μ | α | β | η = α/β | Behavior |
|----------|---|---|---|---------|----------|
| Sparse, independent | 0.10 | 0.20 | 0.50 | 0.40 | Mild clustering |
| Moderate burstiness | 0.10 | 0.20 | 0.10 | 2.00 | **UNSTABLE** — avoid |
| Chat-like bursts | 0.05 | 0.30 | 0.60 | 0.50 | Good for reply chains |
| Very bursty social media | 0.05 | 0.40 | 0.80 | 0.50 | High clustering |

Note: The example μ=0.1, α=0.2, β=0.1 from hawkeslib docs produces η=2.0, which is explosive. A realistic chat model should keep η in the range 0.3–0.7.

Sources: https://hawkeslib.readthedocs.io/en/latest/tutorial.html, https://arxiv.org/pdf/1708.06401, https://arxiv.org/pdf/2405.10527

**Burstiness parameter B** (from https://arxiv.org/html/2412.13617):
```
B = (σ − ⟨τ⟩) / (σ + ⟨τ⟩)
```
where σ = std dev of inter-event times, ⟨τ⟩ = mean inter-event time.
- B = 0 → Poisson (memoryless)
- B > 0 → bursty (clustered, like real texting)
- B < 0 → regular/periodic
- Real human editing/texting: B ≈ 0.9 with power-law tails

**Combining NHPP + Hawkes:** The most realistic model uses a **non-homogeneous Hawkes process**:
```
λ*(t) = μ(t) + Σ_{t_i < t} α · exp(−β · (t − t_i))
```
where μ(t) is the diurnal NHPP rate function. This captures both the day/night envelope *and* conversational clustering.

---

### 3.4 Gamma Inter-Arrival Distribution (Renewal Process)

A **renewal process** with Gamma-distributed inter-arrival times provides a simpler non-memoryless alternative to Hawkes.

Gamma(shape=k, rate=r) inter-arrival time X has:
- Mean: E[X] = k/r
- Variance: Var[X] = k/r²
- **Coefficient of Variation (CV) = 1/√k**

| k (shape) | CV | Behavior | Analogy |
|-----------|-----|----------|---------|
| k < 1 | CV > 1 | Over-dispersed (bursty) | More clustered than Poisson |
| k = 1 | CV = 1 | Exponential = Poisson | Memoryless |
| k > 1 | CV < 1 | Under-dispersed (regular) | More regular than Poisson |
| k = 10 | CV = 0.32 | Very regular | Like a fixed timer with noise |

**Why k ≠ 1 is non-memoryless:** When k > 1, the Gamma is a sum of k exponentials. The process has **memory** of how long it has already been waiting — the hazard rate is not constant. If you have waited a long time (near the mode), the next event is increasingly likely. If you have waited a very short time, the next event is unlikely (hazard rate rises then falls).

**For our engine — Gamma is simpler but less realistic for messaging:**
- Hawkes captures the causal "reply triggers reply" structure of conversation.
- Gamma with k~0.5 captures bursty overall inter-message statistics without modeling the causal chain.
- Gamma with k~3–5 models a more metronomic "check in regularly" pattern.

Sources: https://stats.libretexts.org/Bookshelves/Probability_Theory/Probability_Mathematical_Statistics_and_Stochastic_Processes_(Siegrist)/14:_The_Poisson_Process/14.03:_The_Gamma_Distribution, https://arxiv.org/html/2412.13617

---

### 3.5 Model Selection Summary

| Model | Memory | Burstiness | Diurnal | Complexity | Best for |
|-------|--------|------------|---------|------------|----------|
| Homogeneous Poisson | None | None | No | Trivial | Prototype only |
| NHPP | None | None | Yes | Low | Day/night envelope |
| Hawkes | Yes (causal) | Yes | No | Medium | Reply clustering |
| NHPP + Hawkes | Yes | Yes | Yes | Medium | **Recommended** |
| Gamma renewal | Yes (statistical) | Optional (k<1) | No | Low | Simple bursty alternative |

---

## 4. Parameters to Adopt for Our Engine

These are **grounded recommendations** synthesizing arXiv 2508.11829, Sentipolis (2601.18027), the mechanistic emotion study (2604.00005), and the point-process literature.

---

### 4.1 Hormonal Cycle Parameters

```yaml
cycle:
  total_length_days: 28          # Standard; add jitter: ±2–3 days per instance
  phases:
    menstrual:
      day_start: 1
      day_end: 5
      estrogen_norm: 0.15        # Low; near baseline
      progesterone_norm: 0.05    # Nadir
      cortisol_norm: 0.55        # Slightly elevated (cramping/stress)
      mood_valence_offset: -0.3  # Pull PAD Pleasure down
      mood_arousal_offset: -0.2
    follicular:
      day_start: 6
      day_end: 13
      estrogen_norm: 0.60        # Rising
      progesterone_norm: 0.10
      cortisol_norm: 0.45
      mood_valence_offset: +0.1  # Mild positive
      mood_arousal_offset: +0.1
    ovulatory:
      day_start: 14
      day_end: 16
      estrogen_norm: 1.00        # Peak
      progesterone_norm: 0.20
      cortisol_norm: 0.40        # Optimal "flow" window
      mood_valence_offset: +0.4  # Happiness peak
      mood_arousal_offset: +0.3
    luteal_early:
      day_start: 17
      day_end: 22
      estrogen_norm: 0.55
      progesterone_norm: 0.90    # Peak
      cortisol_norm: 0.45
      mood_valence_offset: +0.1
      mood_arousal_offset: 0.0
    luteal_late:
      day_start: 23
      day_end: 28
      estrogen_norm: 0.20        # Dropping
      progesterone_norm: 0.20    # Dropping
      cortisol_norm: 0.65        # PMS cortisol spike
      mood_valence_offset: -0.2
      mood_arousal_offset: -0.1
```

*Hormone values are normalized [0, 1] relative to each hormone's cycle peak. The absolute values are for state tracking only — they feed into system prompt generation.*

---

### 4.2 Circadian Parameters

Use a cosine wave for smooth diurnal modulation:

```python
# Arousal follows a ~cosine with peak ~2 PM (14h), trough ~4 AM (4h)
arousal_circadian(h) = A_circ * cos(2 * pi * (h - 14) / 24)

# Cortisol peaks ~8 AM
cortisol_circadian(h) = A_cort * cos(2 * pi * (h - 8) / 24)

# Melatonin peaks ~2 AM (inverse of cortisol for simplicity)
melatonin_circadian(h) = A_mel * cos(2 * pi * (h - 2) / 24)
```

Recommended amplitudes:
```yaml
circadian:
  arousal_amplitude: 0.25        # ±0.25 on PAD Arousal scale [−1, +1]
  cortisol_amplitude: 0.30       # Normalized cortisol variation
  melatonin_amplitude: 0.35
  valence_morning_boost: +0.15   # Extra positivity morning (6–11 AM)
  valence_night_penalty: -0.10   # Slight negativity late night (11 PM–4 AM)
```

---

### 4.3 PAD Mood State Parameters

```yaml
mood:
  # Scales: all dimensions in [-1.0, +1.0]
  personality_baseline:
    pleasure: 0.20   # Slightly positive by default
    arousal: 0.10
    dominance: 0.15
  
  # Dual-speed dynamics (per Sentipolis architecture)
  emotion_decay_rate: 0.30       # Fast emotion: half-life ~2.3 conversation turns
  mood_decay_rate: 0.02          # Slow mood: half-life ~35 turns (~1 day of interaction)
  
  # Mood update rule (discrete):
  # mood_new = mood_old * (1 - mood_decay_rate) + emotion_current * emotion_weight
  #           + hormonal_offset * hormonal_weight
  emotion_weight: 0.25           # How much a single-turn emotion moves mood
  hormonal_weight: 0.10          # How much hormonal phase pulls mood per turn
  
  # Clamp limits to avoid extremes (per 2604.00005 findings on non-monotone curves)
  pleasure_clamp: [-0.80, +0.80]
  arousal_clamp: [-0.70, +0.70]
  dominance_clamp: [-0.60, +0.60]
  
  # Step sizes for discrete mood ladder (if using 5-step or 7-step scale)
  discrete_steps: 7              # e.g., very-sad | sad | slightly-sad | neutral | ...
  step_size: 0.29                # (2.0 / (steps - 1)) = 0.333 for 7 steps
```

---

### 4.4 Spontaneous Message Timing Parameters

Use a **Non-Homogeneous Hawkes Process** combining diurnal envelope + self-excitation:

```yaml
timing:
  model: nhpp_hawkes
  
  # NHPP diurnal envelope
  lambda_mean: 0.08              # Base rate (messages/hour) ≈ 2 per day when alone
  diurnal_amplitude: 0.65        # A in λ(t) = λ_mean * [1 + A·cos(2π(t-t_peak)/24)]
  diurnal_peak_hour: 14.0        # 2 PM peak activity
  
  # Hawkes self-excitation
  hawkes_alpha: 0.35             # Excitation per received message
  hawkes_beta: 0.80              # Decay rate (1/hour); half-life = ln(2)/0.8 ≈ 52 min
  hawkes_branching_ratio: 0.44   # alpha/beta = 0.35/0.80 — stable (< 1.0)
  
  # Phase modulation of lambda_mean
  phase_multipliers:
    menstrual: 0.60
    follicular: 1.00
    ovulatory: 1.40
    luteal_early: 1.10
    luteal_late: 0.80
  
  # Minimum and maximum inter-message gaps
  min_gap_minutes: 15            # Never faster than 15 min spontaneously
  max_gap_hours: 48              # Force at least one contact every 2 days
  
  # Alternative: simpler Gamma renewal
  gamma_shape_bursty: 0.6        # k < 1 for bursty (B > 0)
  gamma_shape_regular: 3.0       # k > 1 for metronomic daily check-ins
```

**Branching ratio guidance:**
- η = 0.3–0.5: Mild conversation clustering (recommended for companion AI)
- η = 0.5–0.7: Strong clustering, chat-like bursts
- η ≥ 1.0: Explosive — avoid

---

### 4.5 Summary Table: All Key Numeric Parameters

| Parameter | Value | Grounding |
|-----------|-------|-----------|
| Cycle length | 28 ± 2 days | Biological standard; arXiv 2508.11829 |
| Menstrual phase | Days 1–5 | Clinical endocrinology |
| Ovulatory peak | Days 14–16 | Paper + clinical sources |
| Luteal phase | Days 17–28 | Clinical; paper cites "10–16 days before menstruation" |
| Sadness peak (menstrual) | F=9.07, p<0.0001 | arXiv 2508.11829 (confirmed) |
| Happiness peak (ovulatory) | F=5.02, p=0.0019 | arXiv 2508.11829 (confirmed) |
| Optimal cortisol window | 40–60% of peak | arXiv 2508.11829 (confirmed) |
| PAD dimensions | [−1, +1] per axis | 2604.00005 (uses −10 to +10; we rescale) |
| Valence boost (positive) | ~+3.4% task perf | arXiv 2604.00005 |
| High dominance effect | +79.8% planning | arXiv 2604.00005 |
| Low arousal safety benefit | −52.7% failures | arXiv 2604.00005 |
| Fast emotion decay | ~0.30/turn | Inferred from dual-speed literature |
| Slow mood decay | ~0.02/turn | Inferred from Sentipolis abstract |
| Hawkes α (excitation) | 0.35 | Academic examples; hawkeslib |
| Hawkes β (decay) | 0.80 /hr | Chosen for ~1 hr half-life |
| Branching ratio η | 0.44 | Stable: α/β < 1 |
| Gamma k (bursty) | 0.6 | Yields CV = 1.29, B > 0 |
| Gamma k (regular) | 3.0 | Yields CV = 0.58, B < 0 |
| Burstiness B (real humans) | ~0.9 | arXiv 2412.13617 (Wikipedia editors) |
| Diurnal peak hour | 14:00 | Standard chronobiology |
| Circadian arousal amplitude | ±0.25 | Inferred from circadian PMC literature |

---

## 5. Sources

- arXiv 2508.11829 abstract: https://arxiv.org/abs/2508.11829
- arXiv 2508.11829 HTML: https://arxiv.org/html/2508.11829v1
- Sentipolis (arXiv 2601.18027): https://arxiv.org/abs/2601.18027
- Mechanistic emotion in LLMs (arXiv 2604.00005): https://arxiv.org/html/2604.00005v1
- Cognitive appraisal analysis of LLMs (arXiv 2508.05880): https://arxiv.org/html/2508.05880v1
- Chain-of-emotion architecture (PMC11086867): https://pmc.ncbi.nlm.nih.gov/articles/PMC11086867/
- PAD model overview: https://grokipedia.com/page/PAD_emotional_state_model
- Affective computing systematic review: https://arxiv.org/pdf/2203.06935
- Emotions in LLM survey (arXiv 2505.01542): https://arxiv.org/html/2505.01542v1
- Hawkes process tutorial: https://arxiv.org/pdf/1708.06401
- Hawkes process models and applications (arXiv 2405.10527): https://arxiv.org/html/2405.10527
- hawkeslib documentation: https://hawkeslib.readthedocs.io/en/latest/tutorial.html
- Burstiness measuring and modeling (arXiv 2412.13617): https://arxiv.org/html/2412.13617
- NHPP theory: https://www.randomservices.org/random/poisson/Nonhomogeneous.html
- Gamma distribution and arrival times: https://stats.libretexts.org/Bookshelves/Probability_Theory/Probability_Mathematical_Statistics_and_Stochastic_Processes_(Siegrist)/14:_The_Poisson_Process/14.03:_The_Gamma_Distribution
- Menstrual cycle phases (clinical): https://www.raleighob.com/phases-of-the-menstrual-cycle/
- Menstrual cycle phases (Clue): https://helloclue.com/articles/cycle-a-z/the-menstrual-cycle-more-than-just-the-period
- Mathematical modeling of circadian rhythms (PMC): https://pmc.ncbi.nlm.nih.gov/articles/PMC6375788/
- Emotion-Aware Design VAD (arXiv 2502.16038): https://arxiv.org/html/2502.16038v3
