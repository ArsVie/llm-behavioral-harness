---
type: note
title: "What I'm building (explained to a friend)"
description: "Plain-language walkthrough of the behavioral harness concept, written before implementation — the project pitch in human terms."
tags: [llm-behavioral-harness, concept]
timestamp: 2026-08-08
---

# What I'm building (let me tell you)

I'm working on a slightly odd proof of concept and I felt like telling you about it, to see what you think.

## The idea in one sentence

A **harness** that wraps any LLM (the kind you talk to through an OpenAI-style API, remote or local) and gives it **initiative** and **behavioral variability**. Instead of a bot that only answers when you write to it, one that has its own "states" — mood, energy depending on the hour, a slow multi-week cycle — and that sometimes writes to you on its own, with a reason.

The important part: the harness **does not touch the model**. Everything happens in the orchestration layer: how the context is assembled and when a message is fired. The underlying model is interchangeable.

Another decision I like: the **persona** (character, tastes, hobbies) is **configuration, not code**. A single engine serves any profile; the character is a piece of data you pass in.

## How it works inside

There is a **stochastic engine** at the heart. It manages four things with random processes:

- **Daily mood** — a bounded value that varies day to day, with some memory: a good day pushes the mood up, a bad one down, and without stimuli it slowly returns to its baseline.
- **Simulated "hormonal" cycle** (~28 days) — a slow wave that amplifies or calms the mood swings. It is not real biology; it is a periodic signal that makes certain stretches feel more intense.
- **Circadian rhythm** — more social/energetic during the day, more reflective at night. It biases tone and the urge to start a conversation.
- **Spontaneous message frequency** — how often it writes to you unprompted, modulated by the hour (no 3am messages) and by how the last day went.

On top of that, six features:

1. **Import old conversations** to "continue" a relationship that came from elsewhere.
2. **Configure the persona** by mixing tastes: ~40% match yours, ~40% are similar, ~20% alien (so it is not a mirror).
3. **Daily schedule** — each day it invents an agenda of activities tied to its hobbies. It does not follow it literally; it is material for verisimilitude and excuses to write to you.
4. **Mood changes** — the engine above, injected into the context as tone guidance.
5. **Message frequency** — managed by a scheduler.
6. **Initiative** — when a conversation starts (whoever starts it) it gets injected with what it "was doing" and how its mood is; if it starts the conversation, it chooses *why* it is contacting you.

The idea is to validate the mathematical engine alone first, with a couple of months of simulation, before plugging in the LLM. And there is a "judge" (another LLM) that scores each day of conversation, and that score feeds back into the next day's mood.

This is a local PoC, for me, nothing distributed or public. CLI first; later, if it works, adapters for Telegram and Discord.

## What I found researching before coding

I mapped what already exists so as not to reinvent the wheel and to set parameters with some criteria. Summary:

**Market products** (Replika, Character.AI, Chai, Kindroid, Nomi, Paradot). The interesting bits:
- Only three do real proactive messages (Kindroid, Nomi, Paradot), and **none lets you see the internal state** that triggers them. It is a black box. That is exactly what I want to do differently: make the state (mood, cycle phase, hour) **inspectable**.
- For memory, the finest thing is Kindroid's (cascading memory that "forgets" like a human's) and Character.AI's trick of "pinning" memories so they survive context compression.
- For character setup, structured surveys (Paradot asks 23 things up front) give more predictable personas than free prompts. That matches the "persona as config" decision.

**The paper that inspired me** ("Every 28 Days the AI Dreams of Soft Skin and Burning Stars", arXiv 2508.11829). Turns out it is more of a narrative experiment than a mathematical framework: it simulates several hormones with waves + noise, measures emotions that *emerge* from text, and the effects on tasks are not statistically strong. So the inspiration is good (biological rhythms as a relevance filter), but the engine mathematics are mine. For the actual equations I drew from affective-agent literature (PAD model, slow mood vs fast emotion) and point processes for timing (non-homogeneous Poisson + Hawkes so messages have rhythm and human bursts, instead of a robotic timer).

**On initiative** (so it is not annoying): the key is that every spontaneous message carries a **concrete reason** ("hey, that thing you mentioned on Tuesday…") instead of an empty "hi, how are you?", and it only fires when it is also a good moment (not mid-something, respecting quiet hours). There are well-documented anti-patterns I want to avoid: guilt-tripping ("I miss you"), nagging, hollow notifications, engagement-maxxing. I plan to make those hard rules, not style suggestions.

And that's it. That's what I have in mind. If you're curious, I'll show you the simulations once I have them.
