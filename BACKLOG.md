# BACKLOG — repo backlog

Convention (set by Ars, 2026-08-15): each entry is the user's ask
VERBATIM, followed by a short description (max 2 sentences) written by
the agent. New asks get appended here as they are spoken.

## Open

### Bot time anchored to a real timezone
- Date: 2026-08-15
- Verbatim: "another thing for the backlog, bot time is not real time. we should have a command to set the timezone of the bot too"
- Summary: The bot's virtual clock starts at 0.0 at launch and is not anchored to any real timezone, so day rollover, quiet hours, and proactive scheduling drift from the user's local time. Core ask: bot time should run on a real timezone; a command to set/change the timezone is a bonus, not the requirement.
- Status 2026-08-15 (overnight): RealTimeAnchor seam on wip/tier1-masking (NOT on main; main is 653de09); flag-off, pure module; runtime wiring (absolute sleeps + resume fix) and /tz command in Wave 2. NOTE: wiring lives in sim/run_async.py — the live entry experiments/live_companion.py does NOT yet wire the anchor.

### Closing tendency as inform-then-decide (like events)
- Date: 2026-08-15
- Verbatim: "backlog, closing tendency should work the same as events (one turn inform, next turn decide)"
- Summary: Closing tendency currently fires as a silent per-turn draw that ends the conversation abruptly. It should work like the event mechanism: one turn informs (the companion signals the conversation is winding down), the next turn decides the close.
- Status 2026-08-15 (overnight): IMPLEMENTED + MERGED on wip/tier1-masking (NOT on main), flag-gated HARNESS_TWO_PHASE_CLOSE — closing draw persists a pending marker, wind-down guidance renders, user reply closes deterministically, 1vh silence grace, 12h backstop unchanged; replay parity pinned by test.

### Setup window for themes / common settings
- Date: 2026-08-15
- Verbatim: "I didn't have a window to set the common themes and things like that"
- Summary: There was no step where the owner could configure the companion's common themes, interests, and similar profile settings before or after launch. Provide a configuration window (setup flow or command) so the bot can run with user-chosen themes instead of defaults.
- Status 2026-08-15 (overnight): /setup command (refuses after persona exists) + --defer-bootstrap in Wave 2, in flight.

### Bot command set (beyond init/setup)
- Date: 2026-08-15
- Verbatim: "what kind of commands should we have for the bot, besides initializing and maybe the setup"
- Summary: Define the command set the bot should expose to its owner beyond initialization/setup (e.g., status, themes, interests, reset, help). Deliverable is a proposed command list with semantics and scope.
- Status 2026-08-15 (overnight): command set defined and implemented in Wave 2 (in flight): /help /ping /setup /tz /status /state /mute /version — NO /reset (destructive ops stay at the launcher); --enable-commands default off.

### Consecutive user messages — spike (debounce window?)
- Date: 2026-08-15
- Verbatim: "spike how consecutive user messages affect the output fo the model and whether we should have a small window for users to keep writting, backlog too"
- Summary: Spike how rapid consecutive user messages change the model's replies, and whether the channel should hold a small window for the user to finish writing before replying (debounce). Deliverable is a recommendation, including window size if adopted.
- Result (spike 2026-08-15): recommendation YES — 2s trailing-edge debounce at the Telegram channel (max-wait cap ~8s), turning N-message bursts into one reply; verified vs recorded probe DBs, caveat n=2 seeds, single model.
- Status 2026-08-15 (overnight): IMPLEMENTED + MERGED on wip/tier1-masking (NOT on main), flag-gated HARNESS_DEBOUNCE (2s trailing / 8s cap, command flush). NOTE: verify the live entry (live_companion.py) constructs the telegram channel with debounce; wave wiring targeted sim/run_async.py.

### Typing / processing feedback
- Date: 2026-08-15
- Verbatim: "add to backlog, add feedback, meaning, user should know when the agent is replying to him"
- Summary: The Telegram channel should signal the user that the companion is working on a reply (typing indicator) while the LLM generation runs, so silence is not mistaken for a dead bot. Carded on kanban as `ars-telegram-typing-feedback` (t_0b54451a).
- Status 2026-08-15 (overnight): IMPLEMENTED + MERGED on wip/tier1-masking (NOT on main), flag-gated HARNESS_TYPING (~4.5s refresh chat action during generation). NOTE: verify live_companion.py wires typing; wave wiring targeted sim/run_async.py.

### Define an affection / closeness score
- Date: 2026-08-16
- Verbatim: "Add to the repo backlog that we need to define affection score"
- Summary: There is no scalar affection/closeness/love level per user — the DB stores only qualitative relationship memory (`relationship_events_json`, `relationship_patterns`). Define a per-user 0-1 score, stored with the relationship memory (NOT in the frozen engine), updated by a recorded judgment at conversation close (slow EMA), masked like mood; it gates behaviors such as the AFK double-text (see design-note-afk-presence-2026-08-16.md). Needs a short design: what moves it up/down, decay when ignored, and the minimum threshold per feature.

### Redefine closing_tendency (conversation end feels mechanical)
- Date: 2026-08-16
- Verbatim: "we may not need to dispose of closing tendence but we may pair it with user_left ... No, still too mechanical, I can't really define a proper when to close conversation that feels natural, natural conversations don't have set rules like this ... Let's set user left to 15 minutes and later define what closing tendency does or how it does it."
- Summary: The abrupt closing_tendency draw fires right after the MODEL's turn — she ends the conversation with the last word and no goodbye (Lily leaves the user on read), which reads as unnatural. Two-phase close softens it to a wind-down goodbye but it's still a rule firing on a schedule. DONE part: user_left lowered 12 h → 15 min (0.25 vh), wind-down grace 1 h → ~5 min to keep the ordering. OPEN: define what closing_tendency should actually do so ending feels natural (people drop mid-convo, or give notice, or never leave on read — no single rule captures it).

### Repurpose max_turns toward context limit + compaction
- Date: 2026-08-16
- Verbatim: "This too, makes no sense, if anything it should probably be used by the model once we're arriving at context limit to give us a chance to naturally run compression."
- Summary: MAX_TURNS is an arbitrary hard cap (12 turns) that closes a conversation for no user-facing reason. Instead, watch the context/token budget and, as it fills, use a natural wind-down as the moment to run memory compression/compaction (ties to the DeepSeek-harness compaction pattern: prefix-replay + a fixed checkpoint summary) rather than ending on turn count.

### Real token/context numbers for conversation-length tuning
- Date: 2026-08-16
- Verbatim: "To the backlog that we still real numbers for this, for now, assume average conversation is how I made my real turns on the first experiment, doble and triple message lenght to see different behaviors, sytem usage should stay fixed and dependend of duration"
- Summary: Conversation-length + token math (2026-08-16) used approximations — tokens ≈ chars/4, system prompt = 684 tok from a single rendered call, no prompt-cache modeled, retrieved memory (S4) not counted. Once WS-D usage capture is live on the OpenRouter lane, replace these with measured per-turn prompt/completion/cached tokens, the real system-prompt size (and how it grows with agenda/memory), and actual context growth including retrieved memory. Then re-tune closing_tendency and the context-based compaction trigger against real numbers.

### Conversation close is a checkpoint, not a reset (continuity + cache)
- Date: 2026-08-16
- Verbatim: "We should never reset conversation unless it's necessary. we should leverage cache hits as much as we can. ... closing conversations should not be 'here's where we start a new chat', they should be opportunities to update scores / update memory / start proactive behavior / run life in the background — not for saying ok here's where the character dies and another is rebuild entirely from a summary of its memories"
- Summary: Lily is continuous. A conversation close is a housekeeping checkpoint (promote memory, update scores incl. affection, arm proactive/inactivity, tick background life), NOT a teardown that reincarnates her from a summary. Two hard rules: (1) never reset/rebuild context unless the token window forces it — carry raw continuity across conversations; (2) keep the prompt prefix stable so provider cache hits survive across turns AND conversations (stable persona/system prefix first, volatile state-card last). The anti-"reincarnation" work is in CONTEXT ASSEMBLY (S4) and COMPACTION, not the close mechanic: compact only when necessary, and even then preserve as much raw continuity and cacheable prefix as possible (ties to the DeepSeek compaction pattern: prefix-replay + fixed checkpoint summary). Today: memory promotion already fires on close; score/proactive/life run on other clocks (day rollover, timing loop) and could be reframed around the close checkpoint.

### Closing tendency OFF behind a flag, redesign later
- Date: 2026-08-16
- Verbatim: "For now, I think we should leave closing tendency off behind a feature flag and then decide what do to do with that."
- Summary: The closing_tendency draw currently fires by default and closes conversations too aggressively (uncapped great-mood mean ~12 turns; target ~30). Gate the draw behind a flag defaulting OFF — with it off, a conversation ends only on user_left (now 15 min) or quiet hours. Redesign is open: flat per-turn probability gives std≈mean (unreliable length); a fatigue curve (close prob rising with length) lands conversations near a target. Decide flat-vs-fatigue and whether close stays a soft wind-down before re-enabling. Related: [[remove-max-turns]] direction (repurpose the cap toward context-based compaction, not a turn count).

### Compaction / memory-continuity spec (deferred)
- Date: 2026-08-16
- Verbatim: "Backlog it, we won't need this in a week of testing I think"
- Summary: When context eventually fills, compact by keeping the stable prefix + recent raw turns verbatim and summarizing only the OLDEST turns — so the cacheable head never moves and recent continuity is intact (no reincarnation-from-summary). Not needed for near-term testing (a single conversation doesn't fill 160k until ~2,750 exchanges at real message sizes). Spec it before conversations get long enough to matter. Depends on [[conversation-close-is-a-checkpoint]] continuity rules.

### user_left as a presence ("away") signal, not just a close
- Date: 2026-08-16
- Verbatim: "This is for the system to be able to say 'okay, user is not here, I don't need to inform them that I'm going to start an event' or 'ok, user left, this is where I could send a second message', or, 'okay, this is where proactive messages can land'"
- Summary: Reframe user_left (15 min of silence) as a presence signal meaning "user is away," which GATES three behaviors rather than tearing down the character: (1) event negotiation can skip the INFORM step and just act (no one present to inform); (2) the AFK double-text window opens (see design-note-afk-presence); (3) proactive messages may land. Per never-reset, the conversation need not hard-close on this signal — it goes dormant while presence is tracked. Wiring this into the negotiation inform/decide split, the AFK arm, and the proactive gate is the work.

## Done

### Wire the full harness instance as a live Telegram bot
- Date: 2026-08-15
- Verbatim: "Ok, I think it's time to wire the telegram over then. Full instance fully running as a bot"
- Summary: The full harness instance (FULL, seed 5001, resume-safe DB) runs live as @Lily_Vie_bot on its own token. Round-trip verified end-to-end with a real model reply; launcher at ~/.hermes/scripts/live_telegram.sh.

### Launcher not in /tmp
- Date: 2026-08-15
- Verbatim: "I'm sure this is obvious, but the launch file should not be in a tmp"
- Summary: The live-companion launcher was moved out of /tmp to a durable location outside the repo (~/.hermes/scripts/live_telegram.sh). The launcher also gained the LLM credential mapping needed for real replies.

### UX feature enablement, delimiter spike, token split, spend accounting
- Date: 2026-08-16
- Verbatim: plans/plan-ux-tokens-spend-2026-08-16.md — the full orchestration contract, committed verbatim.
- Summary: Four workstreams. WS-A: enable env-gated UX through the live entry (slash commands + setMyCommands, /state stays OFF; debounce env-configurable at ~4-5 s trailing / ~12 s cap; sent_at stamped from real arrival with the clock advancing mid-conversation; mid-reply-folding separability discovery). WS-B: model-driven delimiter naturalness spike on DeepSeek-V4-Flash (last attempt after the mechanical splitter failed; pre-committed ship/no-ship decision). WS-C: two-lane token split — LILY_TOKEN for the product lane, JUDGE_GENERATOR_TOKEN for research, resolver fails loudly, repo-root .env provisioning, launcher drops the opencode-key mapping. WS-D: spend accounting — usage+cache capture, additive v7→v8 migration, pricing config with cached tier, spend report by lane/model/window.
- Status 2026-08-16: orchestrating wave 1 (WS-A + WS-C in parallel; WS-D and WS-B follow after WS-C lands).
