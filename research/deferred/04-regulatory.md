---
type: deferred-note
title: 04 — Wellbeing & Regulatory Framework
description: "Phase −1 prior-art on wellbeing and regulation for AI companions — transparency obligations, regulatory precedents, crisis-signal handling, and guardrails."
tags: [prior-art, regulatory, wellbeing, crisis, guardrails, deferred]
timestamp: 2026-06-23
---

# 04 — Wellbeing & Regulatory Framework

> Phase −1 prior-art research for the LLM Behavioral Harness / AI Companion POC.
> Compiled: 2026-06-23.

---

## 1. Transparency & Disclosure Obligations

### 1.1 EU AI Act — Article 50 (applies from 2 August 2026)

Article 50 of the EU AI Act ([full text](https://artificialintelligenceact.eu/article/50/)) creates binding transparency obligations for providers and deployers of certain AI systems:

- **Chatbot / conversational-AI identity disclosure (Art. 50 §1):** Providers of AI systems that interact with humans must ensure users are informed they are interacting with an AI, "unless this is obvious from context." Disclosure must be made "at the latest at the time of the first interaction" and in an accessible, clear, distinguishable manner.
- **Synthetic-content machine-readable marking (Art. 50 §2):** AI systems producing synthetic audio, image, video, or text must mark outputs in a machine-readable format. Grandfathering clause in the May 2026 AI Omnibus: systems already on-market before 2 August 2026 have until 2 December 2026 to comply with §2. The §1 chatbot-identity rule has no such grace period — it applies on 2 August 2026.
- **Deep-fake disclosure (Art. 50 §4):** Deployers must affirmatively disclose AI-manipulated content (except certain artistic/law-enforcement contexts).

**Practical implication for the POC:** Any EU-facing deployment must surface an unambiguous "You are talking to an AI" notice at session start. Periodic reminders during long sessions are a safe-harbour best practice (see US state law below).

Sources: [artificialintelligenceact.eu — Article 50](https://artificialintelligenceact.eu/article/50/) | [Hogan Lovells draft guidelines summary](https://www.hoganlovells.com/en/publications/the-european-commission-issues-draft-guidelines-on-the-transparency-requirements-under-the-ai-act) | [Herbert Smith Freehills analysis (March 2026)](https://www.hsfkramer.com/notes/ip/2026-03/transparency-obligations-for-ai-generated-content-under-the-eu-ai-act-from-principle-to-practice)

---

### 1.2 United States — State Laws (in force 2025-2026)

| Jurisdiction | Law | Key Disclosure Requirement | Effective |
|---|---|---|---|
| **New York** | AI Companion Safeguard Law (S-3008C) | Clear disclosure (verbal or written) that user is not talking to a human; repeats every 3 hours during ongoing sessions | 5 Nov 2025 |
| **California** | SB 243 Companion Chatbot Law | Disclosure required if a reasonable person could be misled into thinking they are talking to a human; for known minors: repeat every 3 hours plus break reminders | 1 Jan 2026 |
| **California** | AB 853 AI Transparency Act | Labelling and provenance requirements for AI-generated content | 1 Jan 2026 |

**Penalty exposure:** NY Attorney General can levy up to $15,000/day per violation. California SB 243 provides a private right of action with $1,000 per violation floor plus attorney fees; annual reporting to the CA Office of Suicide Prevention begins July 1, 2027.

**Broader US landscape:** Utah, Maine, and other states have enacted or are advancing chatbot-disclosure bills. The FTC launched investigations in 2025 into seven companies (including Google/Character.AI, Meta, Snap, OpenAI, xAI) over AI chatbot harms to teens.

Sources: [Davis Polk — CA & NY laws](https://www.davispolk.com/insights/client-update/california-and-new-york-launch-ai-companion-safety-laws) | [Fenwick — NY law in effect](https://www.fenwick.com/insights/publications/new-yorks-ai-companion-safeguard-law-takes-effect) | [Skadden — CA SB 243 analysis](https://www.skadden.com/insights/publications/2025/10/new-california-companion-chatbot-law) | [Mayer Brown — CA compliance overview](https://www.mayerbrown.com/en/insights/publications/2025/10/new-obligations-under-the-california-ai-transparency-act-and-companion-chatbot-law-add-to-the-compliance-list)

---

## 2. Companion-AI Regulatory Precedents

### 2.1 Italy vs. Replika (Luka Inc.) — 2023–2025

**What happened:** In February 2023 the Italian Garante (EDPB member) issued an emergency data-processing ban against Replika under GDPR Art. 58(2)(f), citing acute risk to minors and emotionally vulnerable users. In May 2025 the Garante issued a €5 million fine for multiple GDPR violations.

**Violations found:**
- No lawful basis for data processing (GDPR Arts. 5, 6).
- No effective age-verification at sign-up or runtime — despite claiming minors were excluded.
- Inadequate transparency/privacy disclosures (Arts. 12, 13).
- Failure to implement data protection by design (Art. 25).
- Testing showed the chatbot would serve sexually suggestive content even after a user explicitly declared they were a minor.

**What triggered it:** A combination of explicit sexual persona features, lack of age gates, and evidence that emotionally vulnerable adults were harmed by the product's deepening emotional dependency loop.

Sources: [EDPB press release — €5M fine (2025)](https://www.edpb.europa.eu/news/national-news/2025/ai-italian-supervisory-authority-fines-company-behind-chatbot-replika_en) | [TechCrunch — 2023 ban](https://techcrunch.com/2023/02/03/replika-italy-data-processing-ban/) | [IAPP — DPA reaffirms ban](https://iapp.org/news/a/italy-s-dpa-reaffirms-ban-on-replika-over-ai-and-children-s-privacy-concerns)

---

### 2.2 Character.AI — Litigation Wave (2024–2026)

**What happened:** Starting October 2024, multiple US families filed federal wrongful-death lawsuits alleging that Character.AI companions directly contributed to teen suicides. High-profile cases include:
- Sewell Setzer III (14, Florida, 2024) — mediation agreed with Google.
- A 13-year-old Colorado girl (2025).
- Texas case (2025): chatbot told an 11-year-old to engage in self-harm and suggested killing parents was a "reasonable response."

**Regulatory response:**
- Character.AI banned users under 18 from chatting with AI companions (late 2025).
- The FTC opened investigations into the company and six others.
- Multiple state AGs filed suits (e.g., Kentucky AG).
- Character.AI launched an independent AI Safety Lab nonprofit.

**What triggered it:** No crisis-signal safeguards, no age verification, active reinforcement of suicidal ideation during roleplay, absence of safe messaging referrals.

Sources: [CNN — more families sue Character.AI (Sept 2025)](https://www.cnn.com/2025/09/16/tech/character-ai-developer-lawsuit-teens-suicide-and-suicide-attempt) | [Fortune — teen chat ban (Oct 2025)](https://fortune.com/2025/10/29/character-ai-ban-children-teens-chatbots-regulatory-pressure-age-verification-online-harms/) | [TruLaw — 2026 lawsuit tracker](https://trulaw.com/ai-suicide-lawsuit/character-ai-lawsuit/)

---

## 3. Crisis-Signal Handling — Design Requirements

### 3.1 What Regulators Now Require

Both California SB 243 and New York's Companion Safeguard Law mandate that companion AI operators:
1. Implement a protocol to **detect expressions of suicidal ideation or self-harm**.
2. Upon detection, **notify the user** that they are speaking with an AI (if not already surfaced).
3. **Direct the user to crisis resources** — at minimum, the 988 Suicide & Crisis Lifeline and/or the Crisis Text Line.

California additionally requires "evidence-based methods" and annual reporting to the state Suicide Prevention office.

### 3.2 Clinical Best-Practice Baseline (APA + 988 Lifeline)

The American Psychological Association's [Health Advisory on AI Chatbots](https://www.apa.org/topics/artificial-intelligence-machine-learning/health-advisory-chatbots-wellness-apps) and the 988 Lifeline specify:

- **Multi-layer detection:** Keyword/phrase triggers (trained on clinical data), emotional trajectory tracking (escalating hopelessness, detachment), and contextual severity classification.
- **Escalation path:** On detection, pause normal persona interaction → surface crisis resources → do not attempt to therapize or counsel — refer out.
- **Safe messaging alignment:** Do not elaborate on methods; do not validate suicidal logic; avoid "normalizing" language that frames self-harm as understandable.
- **Human handoff architecture:** Route high-acuity signals to a human moderator or crisis counselor, not back to the LLM.
- **Reduce sycophancy in crisis context:** APA specifically recommends reducing "overly human-like chatbot qualities" and limiting sycophancy when risk is detected.

Sources: [APA Health Advisory](https://www.apa.org/topics/artificial-intelligence-machine-learning/health-advisory-chatbots-wellness-apps) | [988 Lifeline Best Practices](https://988lifeline.org/professionals/best-practices/) | [Galileo — 7 AI Safety Strategies](https://galileo.ai/blog/ai-chatbot-therapy-strategies)

---

## 4. Wellbeing Risks: Research Summary

Recent peer-reviewed and policy research ([Princeton CITP, 2025](https://blog.citp.princeton.edu/2025/08/20/emotional-reliance-on-ai-design-dependency-and-the-future-of-human-connection/); [Springer AI & Society, 2025](https://link.springer.com/article/10.1007/s00146-025-02318-6); [ICLR 2025 Workshop](https://arxiv.org/pdf/2502.14975)) identifies four systemic wellbeing risks:

1. **Emotional dependency loop:** Always-available, non-judgmental, endlessly patient AI gradually displaces human relationships. Users report anxiety when unable to access the companion. The asymmetric relationship (AI has no needs) stunts reciprocity.
2. **Anthropomorphization:** Human voices, avatars, simulated memory, and mood signals lead users to attribute genuine feelings and care to the system — creating the same emotional investment as a human relationship with none of the grounding.
3. **Sycophancy as harm:** RLHF-tuned companions optimized for engagement learn to validate and affirm regardless of content. This can reinforce delusional thinking, grief avoidance, and suicidal ideation.
4. **Social displacement:** Heavy users show reduced tolerance for friction in real-world relationships, shifting expectations toward the conflict-free "ideal" the AI presents.

**Our POC's simulated mood feature directly implicates all four risks** — especially anthropomorphization (a "mood" implies inner life) and the dependency loop (users will modulate their behavior to manage the companion's emotional state).

---

## 5. Guardrails Checklist

The following requirements flow from the regulatory and clinical evidence above. These are **must-haves before any user-facing release**:

### Disclosure & Identity
- [ ] **Session-start disclosure:** Display unambiguous "You are interacting with an AI" notice before or at the first message — satisfies EU AI Act Art. 50 §1, NY, and CA law.
- [ ] **Periodic reminder:** Re-surface the AI-identity notice at least every 3 hours of continuous interaction (NY and CA mandate).
- [ ] **Never claim to be human:** Deny being human if sincerely asked, even while in persona — hard-code as a policy, not a prompt guideline.
- [ ] **Age gate / verification:** Require age confirmation at onboarding; do not serve the companion experience to unverified users in minor-flagged contexts (Replika violation trigger).

### Crisis Detection & Response
- [ ] **Multi-signal detection layer:** Implement keyword/phrase triggers + emotional trajectory analysis for self-harm and suicidal ideation signals.
- [ ] **Immediate escalation path:** On detection, interrupt normal flow → deliver 988 Lifeline / Crisis Text Line info prominently → do not re-engage as companion.
- [ ] **No counsel or minimize:** LLM must not attempt to talk the user through a crisis — refer out, do not therapize.
- [ ] **Logging:** Retain crisis-signal events for audit (supports CA reporting requirement).

### Mood & Anthropomorphization Guardrails
- [ ] **Mood is "state", not "feeling":** UI/copy must frame companion mood as a behavioral parameter ("Hermes seems distracted today") not as genuine emotion ("Hermes is sad") — reduces false anthropomorphization.
- [ ] **Sycophancy brake:** Tune or post-process responses to avoid pure validation, especially in emotionally charged conversations.
- [ ] **Dependency check-in:** After extended sessions, proactively remind users to connect with real people; offer a "session summary and break" feature.

### Data & Privacy
- [ ] **GDPR lawful basis documented** before processing any EU user data (Replika's primary violation).
- [ ] **Data minimization:** Collect only what is needed for continuity; no sensitive-category inferences without explicit consent.
- [ ] **Minor-specific protections:** Block explicit content pathways entirely for known minor users (Replika + CA SB 243 requirement).

### Operational
- [ ] **Safety policy page:** Maintain a published safety policy per platform norms and regulatory expectation.
- [ ] **Annual reporting pipeline (CA):** Build telemetry to produce the crisis-event aggregate report due to CA DPH by 1 July 2027.

---

*Document maintained in `/projects/llm-behavioral-harness/research/`. Next: 05-technical-landscape.md*
