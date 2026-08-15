# BACKLOG — repo backlog

Convention (set by Ars, 2026-08-15): each entry is the user's ask
VERBATIM, followed by a short description (max 2 sentences) written by
the agent. New asks get appended here as they are spoken.

## Open

### Bot time anchored to a real timezone
- Date: 2026-08-15
- Verbatim: "another thing for the backlog, bot time is not real time. we should have a command to set the timezone of the bot too"
- Summary: The bot's virtual clock starts at 0.0 at launch and is not anchored to any real timezone, so day rollover, quiet hours, and proactive scheduling drift from the user's local time. Core ask: bot time should run on a real timezone; a command to set/change the timezone is a bonus, not the requirement.

### Closing tendency as inform-then-decide (like events)
- Date: 2026-08-15
- Verbatim: "backlog, closing tendency should work the same as events (one turn inform, next turn decide)"
- Summary: Closing tendency currently fires as a silent per-turn draw that ends the conversation abruptly. It should work like the event mechanism: one turn informs (the companion signals the conversation is winding down), the next turn decides the close.

### Setup window for themes / common settings
- Date: 2026-08-15
- Verbatim: "I didn't have a window to set the common themes and things like that"
- Summary: There was no step where the owner could configure the companion's common themes, interests, and similar profile settings before or after launch. Provide a configuration window (setup flow or command) so the bot can run with user-chosen themes instead of defaults.

### Bot command set (beyond init/setup)
- Date: 2026-08-15
- Verbatim: "what kind of commands should we have for the bot, besides initializing and maybe the setup"
- Summary: Define the command set the bot should expose to its owner beyond initialization/setup (e.g., status, themes, interests, reset, help). Deliverable is a proposed command list with semantics and scope.

### Consecutive user messages — spike (debounce window?)
- Date: 2026-08-15
- Verbatim: "spike how consecutive user messages affect the output fo the model and whether we should have a small window for users to keep writting, backlog too"
- Summary: Spike how rapid consecutive user messages change the model's replies, and whether the channel should hold a small window for the user to finish writing before replying (debounce). Deliverable is a recommendation, including window size if adopted.
- Result (spike 2026-08-15): recommendation YES — 2s trailing-edge debounce at the Telegram channel (max-wait cap ~8s), turning N-message bursts into one reply; verified vs recorded probe DBs, caveat n=2 seeds, single model.

### Typing / processing feedback
- Date: 2026-08-15
- Verbatim: "add to backlog, add feedback, meaning, user should know when the agent is replying to him"
- Summary: The Telegram channel should signal the user that the companion is working on a reply (typing indicator) while the LLM generation runs, so silence is not mistaken for a dead bot. Carded on kanban as `ars-telegram-typing-feedback` (t_0b54451a).

## Done

### Wire the full harness instance as a live Telegram bot
- Date: 2026-08-15
- Verbatim: "Ok, I think it's time to wire the telegram over then. Full instance fully running as a bot"
- Summary: The full harness instance (FULL, seed 5001, resume-safe DB) runs live as @Lily_Vie_bot on its own token. Round-trip verified end-to-end with a real model reply; launcher at ~/.hermes/scripts/live_telegram.sh.

### Launcher not in /tmp
- Date: 2026-08-15
- Verbatim: "I'm sure this is obvious, but the launch file should not be in a tmp"
- Summary: The live-companion launcher was moved out of /tmp to a durable location outside the repo (~/.hermes/scripts/live_telegram.sh). The launcher also gained the LLM credential mapping needed for real replies.
