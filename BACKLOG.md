# BACKLOG — repo backlog

Convention (set by Ars, 2026-08-15): each entry is the user's ask
VERBATIM, followed by a short description (max 2 sentences) written by
the agent. New asks get appended here as they are spoken.

## Open

### Consecutive user messages — spike (debounce window?)
- Date: 2026-08-15
- Verbatim: "spike how consecutive user messages affect the output fo the model and whether we should have a small window for users to keep writting, backlog too"
- Summary: Spike how rapid consecutive user messages change the model's replies, and whether the channel should hold a small window for the user to finish writing before replying (debounce). Deliverable is a recommendation, including window size if adopted.

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
