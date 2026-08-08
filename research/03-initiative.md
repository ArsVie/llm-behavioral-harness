---
type: research-note
title: "03 — Initiative: When and Why Proactive Agents Reach Out"
description: "Phase −1 prior-art on proactive agents — signal taxonomy, readiness/termination gates, non-intrusive initiative design rules, and anti-patterns."
tags: [prior-art, initiative, proactivity, scheduler, anti-patterns]
timestamp: 2026-06-23
---

# 03 — Initiative: When and Why Proactive Agents Reach Out

> Phase −1 prior-art research for the LLM behavioral harness / initiative subsystem.
> Written 2026-06-23.

---

## 1. Background: Reactive vs. Proactive Paradigms

Most existing LLM agent systems follow a **reactive paradigm**: the agent responds only when the user explicitly sends a message. The emerging research frontier is **proactive agents** that *anticipate* needs, monitor environmental signals, and initiate contact without being asked. The challenge is doing this in a way that feels motivated and helpful rather than random or manipulative.

Four distinct bodies of work converge here: (1) proactive dialogue systems from NLP/AI, (2) interruptibility and receptivity research from HCI/mobile health, (3) industry patterns for grounding outreach in a reason, and (4) dark patterns / anti-patterns to avoid.

---

## 2. Timing and Triggering of Proactive Contact

### 2a. Signal taxonomy

Research across multiple domains converges on three categories of signals that determine *when* to initiate:

**Content/event signals** — something happened that is genuinely relevant:
- A monitored condition changed (e.g., ProActor's `READY_TO_TRIGGER` state reached for a pending task; [arxiv.org/abs/2605.24900](https://arxiv.org/abs/2605.24900))
- An externally-scheduled deadline approached
- A knowledge gap was detected that is directly blocking user progress ([arxiv.org/abs/2601.09926](https://arxiv.org/abs/2601.09926))

**Contextual/availability signals** — the user is reachable and not deep in a primary task:
- Activity breakpoints (between tasks, not mid-flow): users are far more receptive at natural pauses than during focused work ([arxiv.org/abs/1711.10171](https://arxiv.org/abs/1711.10171))
- Low ambient noise, low motion, low screen-interaction density — classic interruptibility proxies from Fogarty et al., cited across the HCI literature
- Time-of-day and behavioral rhythm: users have predictable "availability windows" (morning routines, commute slots); adaptive models that learn these patterns achieve up to **40% improvement in receptivity** vs. random timing ([arxiv.org/abs/2011.08302](https://arxiv.org/abs/2011.08302))

**Relationship/history signals** — prior interaction justifies follow-up:
- The agent has a prior conversation thread to reference (callback legitimacy)
- A previous question was left open or a commitment was made
- Meta's proactive chatbots require ≥5 prior user messages before the bot can initiate ([justthink.ai/blog/metas-proactive-ai-chatbots](https://www.justthink.ai/blog/metas-proactive-ai-chatbots-that-message-you-first-redefine-digital-engagement))

### 2b. Readiness vs. termination conditions

ProActor (ACL 2026) introduces a useful two-sided gate: an action becomes proactively triggerable only once it is **ready** (conditions met) and before it has **terminated** (window closed). Initiating too early is as bad as initiating too late. The system rewards proactive timing (acting before the reference-ready window closes) and penalizes false triggers (acting before conditions are actually met). This maps directly onto a scheduler that must decide: *is the reason valid right now, and is the window still open?* ([arxiv.org/abs/2605.24900](https://arxiv.org/abs/2605.24900))

### 2c. Personalized receptivity models

The mHealth / Just-In-Time Adaptive Intervention (JITAI) literature demonstrates that **static timing rules fail**; adaptive models that learn individual patterns continuously outperform population averages. Key features: calendar state, time, activity inferred from sensors/usage patterns, and historical engagement rates. Reinforcement learning has been applied to optimize delivery timing as a policy ([PMC8200090](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8200090/)). The practical lesson: any harness scheduler should support per-user timing profiles that update over time.

### 2d. Urgency × complexity channel selection

Industry practice (Parloa, NiCE) adds a **channel-selection dimension**: urgent + complex → voice/synchronous; informational → async text. For a text-based harness the equivalent is: high urgency → interrupt immediately; low urgency → batch into a next-natural-interaction briefing. Contact should be suppressed if a complaint is open, if the user has exceeded a frequency threshold (e.g., 2+ proactive contacts in 7 days), or if a sensitive life event is flagged. ([parloa.com/knowledge-hub/ai-proactive-customer-outreach](https://www.parloa.com/knowledge-hub/ai-proactive-customer-outreach/))

---

## 3. Design Rules: Non-Intrusive Initiative

The following checklist synthesizes the academic and industry evidence:

### Timing
- [ ] **Gate on availability, not just content.** Even a highly relevant message should be deferred until an interruptibility signal is favorable (task breakpoint, calm context, within user's active window).
- [ ] **Respect a minimum quiet interval.** Enforce a per-user cooldown between proactive contacts (a hard cap of N contacts per day/week, configurable).
- [ ] **Honor explicit "do not disturb" signals.** Calendar blocks, out-of-office status, late-night hours — all are ground truth; no inference required.
- [ ] **Learn from feedback.** Track whether the user engaged, dismissed, or ignored each proactive message, and update timing weights accordingly (adaptive model > static rules).

### Grounding the reason
- [ ] **Every proactive message must carry a stated reason.** Not "just checking in" — a concrete trigger: "You asked about X on Tuesday and I have an update," "Your deadline for Y is in 24 hours," "I noticed Z changed." Transparency of reasoning is a core principle ([vanishlabs.ai/news/proactive-ai](https://vanishlabs.ai/news/proactive-ai)).
- [ ] **Anchor to a prior conversation where possible.** A callback to shared history is the strongest legitimacy signal; it proves the message is not generic. This is why Meta requires 5 prior messages before allowing bot-initiated contact.
- [ ] **Prefer schedule- or event-triggered reasons over behavioral inference.** "Your meeting is in 15 minutes" is more defensible than "I detected that you seem stressed." The former is verifiable; the latter feels surveillance-like.
- [ ] **Reason confidence threshold.** Only trigger if the agent's confidence in the relevance of the reason exceeds a set threshold. ProActor's fault-trigger rate metric operationalizes this: penalize acting on unverified readiness ([arxiv.org/abs/2605.24900](https://arxiv.org/abs/2605.24900)).

### Framing the message
- [ ] **Lead with the reason in the first sentence.** Users decide in ~10 seconds whether to engage; front-load the trigger. ([parloa.com](https://www.parloa.com/knowledge-hub/ai-proactive-customer-outreach/))
- [ ] **Make opt-out trivially easy.** The message itself should offer a one-step dismiss/snooze. Any friction here is a dark pattern.
- [ ] **Match scope to channel.** Don't send a long message as a proactive interrupt; send a brief hook and offer to elaborate. The user decides whether to open a full conversation.
- [ ] **Start conservatively; escalate trust progressively.** New users get fewer, lower-stakes proactive contacts. Demonstrated engagement unlocks richer proactivity ([vanishlabs.ai/news/proactive-ai](https://vanishlabs.ai/news/proactive-ai)).

---

## 4. Anti-Patterns to Avoid

These come primarily from the "Computers as Bad Social Actors" paper ([arxiv.org/abs/2302.04720](https://arxiv.org/abs/2302.04720)) and the notification dark patterns literature ([designlab.com/blog/are-notifications-a-dark-pattern](https://designlab.com/blog/are-notifications-a-dark-pattern-ux-ui)):

| Anti-pattern | Description | Why it harms |
|---|---|---|
| **Pseudo-notifications** | Sending messages when there is nothing genuinely new to report (LinkedIn "profile views," Facebook "memories") | Exploits FOMO; destroys trust when user finds message content is trivial |
| **Guilt-tripping / coaxing** | Framing messages to induce guilt ("I miss you," "You haven't checked in lately") | Manipulates emotional response; users report feeling "used" |
| **Passive aggression** | Tone implying annoyance at the user for not responding | Coercive; harms relationship |
| **Mothering / over-help** | Unsolicited advice or check-ins about things the user did not ask about | Erodes user autonomy; feels surveillance-like |
| **Nagging** | Repeating the same prompt after no response | High annoyance; zero added value after first send |
| **Engagement-maxxing** | Optimizing trigger frequency for re-engagement rather than user value | Short-term metric gain, long-term trust destruction |
| **Opaque triggers** | Not explaining why the message was sent | Prevents user calibration; feels arbitrary or creepy |
| **Non-optional notifications** | Proactive contacts with no dismiss/snooze path | Removes agency; classic attention engineering |

---

## 5. Implications for Our Harness

### Scheduler design
The scheduler should operate as a **two-gate system**:
1. **Content gate** — does a valid, confident reason exist? (event occurred, deadline approaching, prior thread has unresolved item, shared interest surfaced)
2. **Context gate** — is the user likely available/receptive? (not in DND, within active window, cooldown elapsed, no open complaint/distress signal)

Only when *both* gates pass should a proactive contact be queued. The ProActor readiness/termination model is directly applicable: each candidate reason should carry a validity window; expired reasons must be discarded rather than deferred indefinitely.

### Reason-selection step (LLM pick)
When the LLM is asked to select *why* it is reaching out, it should be constrained to a **typed reason taxonomy**, such as:
- `schedule` — calendar/deadline-based
- `callback` — follow-up on a prior open thread
- `event` — external condition changed that user cared about
- `shared_interest` — content matches a stated preference/topic
- `check_in` — periodic relationship maintenance (lowest priority; only allowed after cooldown, requires explicit user opt-in)

The `check_in` type is the highest-risk because it has the weakest content grounding — it should be gated behind explicit user consent and used at lowest frequency. Reasons should be surfaced transparently in the message opening, never hidden.

### Adaptive timing
Start with conservative defaults (e.g., one proactive contact per day max, only during a user-defined active window). Log engage/dismiss/ignore outcomes per message and feed them back to a lightweight receptivity model. This mirrors the JITAI adaptive approach and the evidence that personalized models outperform static rules by a large margin.

### Guardrails as constraints, not guidelines
Anti-patterns should be implemented as **hard constraints** in the reason-generation and message-generation steps, not soft style preferences:
- If reason confidence < threshold → block
- If cooldown not elapsed → block
- If DND / outside active window → defer, not skip
- Message tone checker: flag guilt/coercion language before send
- Frequency cap: enforce at queue level, not at generation level

The goal is an assistant that earns the right to initiate contact through demonstrated relevance, not one that optimizes for its own engagement metrics.

---

## Sources

- ProActor (ACL 2026): [arxiv.org/abs/2605.24900](https://arxiv.org/abs/2605.24900)
- PROPER proactivity benchmark: [arxiv.org/abs/2601.09926](https://arxiv.org/abs/2601.09926)
- Proactive Conversational AI survey (ACM TOIS): [dl.acm.org/doi/10.1145/3715097](https://dl.acm.org/doi/10.1145/3715097)
- IJCAI 2023 Survey on Proactive Dialogue Systems: [ijcai.org/proceedings/2023/0738.pdf](https://www.ijcai.org/proceedings/2023/0738.pdf)
- ProactiveBench / Proactive Agent paper: [arxiv.org/abs/2410.12361](https://arxiv.org/abs/2410.12361)
- ProAgent (sensory context): [arxiv.org/abs/2512.06721](https://arxiv.org/abs/2512.06721)
- Intelligent Notification Systems survey: [arxiv.org/abs/1711.10171](https://arxiv.org/abs/1711.10171)
- Detecting Receptivity for mHealth (JITAI / Ally chatbot): [arxiv.org/abs/2011.08302](https://arxiv.org/abs/2011.08302)
- RL for exercise reminders: [PMC8200090](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8200090/)
- Computers as Bad Social Actors (dark patterns): [arxiv.org/abs/2302.04720](https://arxiv.org/abs/2302.04720)
- Notifications as dark patterns (Designlab): [designlab.com/blog/are-notifications-a-dark-pattern](https://designlab.com/blog/are-notifications-a-dark-pattern-ux-ui)
- AI proactive customer outreach (Parloa): [parloa.com/knowledge-hub/ai-proactive-customer-outreach](https://www.parloa.com/knowledge-hub/ai-proactive-customer-outreach/)
- Proactive AI design principles (Vanish Labs): [vanishlabs.ai/news/proactive-ai](https://vanishlabs.ai/news/proactive-ai)
- Meta proactive chatbots (JustThink): [justthink.ai/blog/metas-proactive-ai-chatbots](https://www.justthink.ai/blog/metas-proactive-ai-chatbots-that-message-you-first-redefine-digital-engagement)
- From passive to proactive assistant (Medium): [medium.com/@oleksandr.poberezhnyi](https://medium.com/@oleksandr.poberezhnyi/from-passive-ai-agent-to-proactive-personal-assistant-5ac2b9ce26e7)
