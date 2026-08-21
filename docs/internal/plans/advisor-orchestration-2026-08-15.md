I'll verify the owner's corrections in the code first, then write the plan. Starting with targeted searches on the inactivity/event machinery.

Now digging into the ~5-minute window claim and the event popup expiry/requeue mechanics.

Reading the steering queue and the session inactivity sections; also hunting for the ~5-minute window.

Searching for the ~5-minute window constant and the event popup enqueue/expiry path in session.py and runtime.py.

No 5-minute constant in harness yet. Checking runtime.py's idle/wake loop and searching repo-wide (engine, sim) for it.

Checking engine, sim, and docs for the ~5-minute window mention.

The ~5-minute figure isn't in harness constants. Checking the live launcher (run_async), gates.py, and the TimeScale ΓÇö the window may be real-time (virtual validity ├ù time scale).

The steering queue has no expiry ΓÇö checking the live launcher's time_scale (a ~5-min real window could be a virtual validity ├ù time_scale). Reading run_async.py and looking for live launch docs.

run_async default is real time (3600 s/vh). Checking POLL_INTERVAL_H ΓÇö at real time it could be the ~5-minute figure.

POLL_INTERVAL_H=0.05 vh ΓåÆ 180s real at live scale ΓÇö close to but not exactly "~5 min". Reading the idle-drain context, intent validity shapes, and fire path to complete the verification.

One last receipt: the scheduler max-gap line and telegram handler registration.

Verification complete. Here is the deliverable.

---

# Lily Orchestration Plan ΓÇö Round 2 (2026-08-15)

## PART 1 ΓÇö VERIFICATION REPORT

### Owner claim: "INACTIVITY EXISTS" ΓÇö **CONFIRMED. My round-1 claim was wrong.**

| Claim | Verdict | Receipts |
|---|---|---|
| `USER_LEFT_THRESHOLD_H = 12.0` | Γ£à Confirmed | `harness/session.py:143`; docstring at 139-141 ("virtual hours of user silence") |
| user_left close path | Γ£à Confirmed | `session.py:801-838` ΓÇö `check_conversation_lifecycle` closes with reason `"user_left"` when `t_h - anchor >= USER_LEFT_THRESHOLD_H` (835-837); anchor = last **user** turn, or `conv.opened_t_h` if the user never replied (832-834, 878-880). Schema: `store.py:475` |
| Close recorded AT the boundary, not lazily | Γ£à Confirmed | `session.py:840-869` ΓÇö `next_conversation_close_t_h` returns the earlier of the quiet-hours boundary and `anchor + USER_LEFT_THRESHOLD_H` (866); the runtime **parks the rollover** at that instant (`runtime.py:350-360`) and records the close idempotently at every wake (`runtime.py:335-337, 427-429`) |
| `BOUNDARY_IDLE` | Γ£à Confirmed | Defined `steering.py:70`; imported `session.py:104`; drained per-turn at `session.py:1303` |
| Scheduler max-gap 48h | Γ£à Confirmed | `scheduler.py:47` ("max-gap forcing (48 h)") |

### Owner claim: "EVENTS handle inactivity with a ~5-minute window" ΓÇö **REFUTED as stated; here is what actually exists.**

I searched harness/, engine/, sim/ exhaustively (constants `300`, `0.083`, "5 min", "minute"). **No ~5-minute event-inactivity window exists in code.** The actual machinery:

1. **Steers never expire.** The `steering_queue` schema has no `valid_until` (`store.py:537`; `SteerBackend` row contract `steering.py:87-104`). Steers are one-shot, persist across restarts, and are requeued only on (a) turn interrupt (`session.py:1319-1323`) or (b) decision parse failure (`DecisionRequeue`, `tools.py:160-162, 704-705` ΓåÆ `session.py:1596-1597, 1649-1651`). A pending event popup waits **indefinitely** for the next boundary drain.
2. **Event popups carry no user-response window.** Enqueued at agenda boundary crossings (`session.py:1478-1522`), decided at delivery by `tool_decide_event` (`session.py:1579-1609`). On an initiate verdict, "Starting X." goes out (`1599-1604`) ΓÇö and then **nothing tracks whether the user answers**. A silent user hits only conversation-level closes: `user_left` (12h) or `quiet_hours`.
3. **What DOES expire (hours, not minutes):** opportunities `OPPORTUNITY_VALIDITY_H = 3.0` (`scheduler.py:145,191`); reason validity 3-12h (`REASON_VALIDITY_H`, `scheduler.py:136-139`); intents bounded at resolution (`proactive.py:319-321`), checked at gate (`gates.py:57-58`) and at fire (`session.py:1753-1759`); past-validity rows expired by the runtime (`runtime.py:462-468, 650-654`).
4. **Closest minutes-scale real-time figures:** `POLL_INTERVAL_H = 0.05` vh (`runtime.py:89`) ΓåÆ **180 s** poll at live scale (`runtime.py:209-210`, `run_async.py:121` default 3600 s/vh); `min_gap_min = 15` cooldown (`gates.py:113-116`). Neither is a user-inactivity window. The likely source of the owner's memory: the **it3 L365 availability-windows note** ΓÇö "gym 3ΓÇô5 min windows for sets" ΓÇö explicitly deferred to a later iteration (`results/iteration-3-report.md:254-257`). It is **not implemented**.

### REVISED Item 2 (two-phase close) ΓÇö reconciled with existing machinery

The round-1 open question "what closes an unanswered wind-down?" is answered by **extending the existing boundary-close pattern, not inventing inactivity**:

- The draw (stream 6, keys `(conv_seq, turn_index)` unchanged) sets persisted `closing_pending_t_h` instead of closing; the assembler renders wind-down guidance via the existing `closing_guidance` channel (`domain.py:404`).
- If the user replies ΓåÆ Lily's goodbye turn closes deterministically, reason `"closing_tendency"` (unchanged taxonomy, `domain.py:471`).
- If the user never replies ΓåÆ **no new expiry mechanism**: add one candidate to `next_conversation_close_t_h` (`session.py:840-869`) ΓÇö `closing_pending_t_h + WIND_DOWN_GRACE_H` (proposed 1.0 vh) ΓÇö and one branch in `check_conversation_lifecycle`. The runtime already parks at whatever that function returns (`runtime.py:358`); **zero runtime changes**. `user_left` (12h) remains the outer backstop and wins only if grace > 12h (never). Close reason for the expired wind-down: `"closing_tendency"` (the draw already decided the close; the grace is delivery, not a new decision).

---

## PART 2 ΓÇö ORCHESTRATION PLAN (A+B): Lily orchestrates, workers execute

**Global rules (bind every worker):** main tree = Lily only; each worker in `llh-wt-<name>` worktree; venv = main tree `.venv` (editable install); tests green at every merge; `sim/run_interactive.py` works at every merge point; ALL new behavior flag-gated OFF (it4 default-inertness convention) ΓÇö the live bot (`@Lily_Vie_bot`, seed 5001, `results/live-companion/companion.db`) is byte-identical until the owner flips flags in the morning; English; no secrets; no pushes; surgical diffs.

### Shared seam contracts (published before Wave 1; workers code against these verbatim)

**S1 ΓÇö KV store + close column (migration vN, owned by W-close):**
```python
# harness/store.py ΓÇö SQLiteStore additions
def get_kv(self, key: str) -> str | None: ...
def set_kv(self, key: str, value: str) -> None: ...   # INSERT OR REPLACE
# migration vN: CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL);
#               ALTER TABLE conversations ADD COLUMN closing_pending_t_h REAL;  -- NULL = no wind-down
```

**S2 ΓÇö Real-time anchor (owned by W-anchor):**
```python
# harness/anchor.py (NEW) ΓÇö pure, no I/O
@dataclass(frozen=True)
class RealTimeAnchor:
    epoch0_s: float   # wall-clock epoch seconds at t_h0
    t_h0: float       # virtual hours at epoch0_s
    tz: str           # IANA name, e.g. "America/Mexico_City"
    def t_h_at(self, epoch_s: float) -> float: ...      # t_h0 + (epoch_s - epoch0_s)/3600
    def epoch_of(self, t_h: float) -> float: ...        # inverse
def anchor_for_fresh_start(now_epoch_s: float, tz: str) -> RealTimeAnchor: ...
    # t_h0 = hours since local midnight in tz
# persistence (via S1): keys "anchor.epoch0_s", "anchor.t_h0", "anchor.tz"
```

**S3 ΓÇö Command seam (channel side owned by W-channel; semantics by W-commands):**
```python
# harness/channels/telegram.py
@dataclass(frozen=True)
class ControlCommand:
    name: str          # "tz", "status", ... (no slash)
    args: str          # raw remainder
    sender_id: int
# Channel.start(on_message, on_command=None) ΓÇö CommandHandler registered ONLY
# when on_command is not None (default None ΓçÆ today's behavior: commands dropped).
# Commands NEVER become InboundMessage; _on_command flushes the debounce buffer first.

# harness/commands.py (NEW, W-commands)
def handle_command(cmd: ControlCommand, ctx: CommandContext) -> str: ...
@dataclass
class CommandContext:   # read-only session facts + narrow hooks; NEVER touches Session.on_message
    store: ...; clock: ...; anchor: RealTimeAnchor | None
    persona_exists: bool; pending_proactive_count: int; flags: dict[str, bool]
    request_tz_change: Callable[[str], None]   # applied at next rollover (clock never jumps back)
    request_mute: Callable[[float], None]      # defer, never consume
```

**S4 ΓÇö Typing capability (owned by W-channel; runtime wiring by W-runtime):**
```python
# duck-typed, like enqueue_user_message_steer:
typing_ctx = getattr(channel, "typing_context", None)
# TelegramChannel.typing_context() -> async CM; send_chat_action("typing") every 4.5 s
# CLI/Fake lack it ΓçÆ no-op. Runtime wraps generation + response_delay_s sleep.
```

### Wave 1 ΓÇö three workers, disjoint files, all independently mergeable

| Worker | Worktree | Owns (exclusive) | Delivers |
|---|---|---|---|
| **W-anchor** | `llh-wt-anchor` | `harness/anchor.py` (new), `tests/test_anchor.py` (new) | S2 pure module + tests: math round-trips, DST-safe local-midnight, `anchor_for_fresh_start`. Zero integration ΓÇö merges first, trivially green. |
| **W-close** | `llh-wt-close` | `harness/session.py`, `harness/store.py`, `tests/test_session_close*.py`, migration vN | S1 migration; two-phase close behind `two_phase_close: bool = False` Session flag (env `HARNESS_TWO_PHASE_CLOSE`): draw stream 6 keys **unchanged**, meaning becomes "start wind-down"; `closing_pending_t_h` persisted; wind-down guidance via existing `closing_guidance` assembler channel; grace candidate in `next_conversation_close_t_h` + branch in `check_conversation_lifecycle` (Part 1 revised design). Tests: seeded 3-turn goodbye path; user-silent expiry via VirtualClock; **flag-off byte-parity test** (replay pins unchanged); flag-on turn-count re-baseline (B3-style). |
| **W-channel** | `llh-wt-channel` | `harness/channels/telegram.py`, its FakeApplication, `tests/test_telegram*.py` | (a) Debounce at `_on_update` (`telegram.py:178`): trailing-edge 2 s / max-wait 8 s, join `\n`, buffer AFTER owner filter, injectable sleeper, flag `HARNESS_DEBOUNCE` off; (b) `typing_context()` per S4, flag `HARNESS_TYPING` off; (c) S3 channel side: `ControlCommand` + optional `on_command` in `start()` (`telegram.py:123,140-141`) ΓÇö inert when callback is None; `/command` flushes debounce buffer; `stop()` flushes-or-drops deterministically; no typing during debounce. FakeApplication grows `send_chat_action` recording + command-update injection (**W-channel writes this shared fake; Wave 2 consumes it unmodified**). |

**Merge order Wave 1:** W-anchor ΓåÆ W-close ΓåÆ W-channel. After each merge Lily runs the full suite + a `run_interactive.py --fake` smoke. CLI is untouched by all three (flags off; CLI channel has no typing/debounce/commands).

### Wave 2 ΓÇö two workers, disjoint files, starts after Wave 1 fully merged

| Worker | Worktree | Owns (exclusive) | Delivers |
|---|---|---|---|
| **W-runtime** | `llh-wt-runtime` | `harness/runtime.py`, `tests/test_runtime_anchor*.py` | `AsyncRuntime(anchor: RealTimeAnchor \| None = None)` ΓÇö None ΓçÆ today's behavior (accelerated fleet untouched). With anchor: sleeps become `anchor.epoch_of(target) - time.time()` (absolute, self-correcting, kills cumulative drift); resume fix: `t_h_start = anchor.t_h_at(now)` replacing the land-at-virtual-midnight `_resume_from` bug ΓÇö **fail loudly on clock skew** (persisted anchor implies t_h in the past ΓçÆ raise, don't guess); `_on_command` dispatch under `self._lock`, calling `harness.commands.handle_command` (S3) ΓÇö never `session.on_message`, no closing draws, no memory writes; typing wrap per S4 around generation + `response_delay_s`. Tests: existing runtime tests pass unchanged (anchor=None); one resume test pinning "restart at real 18:00 ΓçÆ local_hour Γëê 18". |
| **W-commands** | `llh-wt-commands` | `harness/commands.py` (new), `sim/run_async.py`, `tests/test_commands*.py` | Command semantics per round-1: `/help`, `/ping`, `/setup` (refuses once persona row exists), `/tz <IANA>` (via `request_tz_change`, applied at next rollover ΓÇö VirtualClock forbids backward jumps), `/status` (day, local hour, pending-proactive count, last-exchange age ΓÇö runtime facts only), `/state` (mood internals, behind debug flag ΓÇö contaminates the perceptual experiment otherwise), `/mute <hours>` (defer, never consume), `/version` (sha, seed, flags). **No `/reset`** ΓÇö destructive ops stay at the launcher. `run_async.py`: `--defer-bootstrap` (blank DB answers only `/setup`, then `ensure_companion_initialized`; default remains unconditional bootstrap ΓÇö CLI unchanged), `--tz`/`HARNESS_TZ`, `--enable-commands` (default off ΓçÆ `on_command=None` ΓçÆ live bot unchanged). Tests drive FakeApplication command updates end-to-end. |

**Merge order Wave 2:** W-runtime ΓåÆ W-commands. Final gate: full suite green; `run_interactive.py` smoke; `run_async.py --fake` smoke with all flags off (default-inertness), then once with flags on.

### Risks

1. **Replay parity (two-phase close)** ΓÇö highest risk. Mitigation: draw count/keys frozen, flag-off byte-parity test is a merge blocker; flag-on turn-count pins re-baselined explicitly, never silently.
2. **Anchor vs VirtualClock no-backward-jump** ΓÇö resume skew (system clock moved back) must raise, not guess; tz changes only at rollover.
3. **Debounce vs commands ordering** ΓÇö both live in `telegram.py` (single owner, W-channel) precisely to avoid a cross-worker race; contract: command flushes buffer first.
4. **Live DB migration vN** ΓÇö runs on `companion.db` at next restart; W-close must make it additive-only (new table + nullable column), tested against a copy of a real schema.
5. **Wave-2 file coupling** ΓÇö `runtime.py` and `run_async.py` both touch startup; the S2/S3 contracts above are frozen before Wave 1 so the workers never negotiate.

---

## PART 3 ΓÇö EXPERIMENT-GATED C (Telegram additions)

**Universal rules for Part 3:** NOTHING here runs now ΓÇö this defines the experiments. Every experiment writes to `results/<exp-name>/` (JSON + report.md). Cheap-run discipline: 5 seeds (5001-5005) ├ù 14 virtual days unless stated, report mean ┬▒ SD and bootstrap 95% CI. **Every approved implementation lands in its OWN worktree (`llh-wt-c-<name>`), one per idea/cluster, presented separately for the owner's morning review ΓÇö none merges without explicit owner approval.** Boundary: C experiments may add files ONLY under `experiments/` and `results/`; C implementations may add new modules but **must not modify** `session.py`, `runtime.py`, `store.py`, `scheduler.py`, `telegram.py` while any Part 2 wave is unmerged; C items that need those files (marked ΓÜá) start only after Part 2 is fully merged.

### D ΓÇö OWNER'S NEW EXPERIMENT: follow-up arm (double-message + left-without-goodbye) ΓÜá
*Worktree: `llh-wt-c-followup` ΓÇö separate; owner reviews before merge.*

**Design.** New armed-event reason `"followup"`, planned like proactives but armed reactively:
- **Trigger A (unanswered proactive):** proactive fires, no user turn follows ΓåÆ arm one follow-up at `fire_t_h + ╬ö`, ╬ö ~ log-normal (proposed median 0.5 vh, ╧â=0.6, clipped [0.25, 2.0] vh), gated by the circadian envelope.
- **Trigger B (left mid-conversation, no goodbye):** conversation open, last speaker = companion, judge/heuristic detects no farewell in the user's last turn, silence > drawn threshold (same family, median ~0.75 vh) ΓåÆ arm follow-up.
- **Seeding:** dedicated stream (`stream_rng(seed, FOLLOWUP_STREAM)`), keyed `(day, intent_id | conv_seq)` ΓÇö replay parity by construction.
- **Interaction with existing machinery:** follow-up is a schedule row with `REASON_VALIDITY_H["followup"] = 1.5` vh; expired-by-validity or superseded by any user turn ΓçÆ expire (existing runtime expiry path, `runtime.py:650-654`); `user_left` (12h) closes long before any follow-up survives ΓÇö the follow-up must check the conversation is still open at fire. Caps: max 1 follow-up per trigger, Γëñ2/day, respects min_gap 15 min + daily cap + quiet hours (existing `context_gate`).
- **Data collected:** per-arm record (trigger type, ╬ö drawn, fired/expired/suppressed, user reply within 2 vh, reply latency), transcripts, daily judge scores.

**Success criteria (both stages must pass before any live default-on):**
- *Stage 1 ΓÇö sim (5 seeds ├ù 14 days, FakeClient + scripted silent/leaving user):* zero gate violations (min_gap/daily_cap/quiet) across all seeds; byte-exact replay of the run with the follow-up stream re-seeded; zero double-fires; ΓëÑ90% of armed follow-ups resolve (fire or expire) inside their validity window.
- *Stage 2 ΓÇö live A/B, 2 weeks, alternate days on/off:* owner reply rate to follow-ups ΓëÑ 30%; weekly perception poll (C4) rating on follow-up days ΓëÑ off days (no degradation); annoyance guard ΓÇö zero `/mute` invocations attributable to follow-ups and <1 negative owner reaction/week. Fail either ΓçÆ feature stays flag-off, data kept.

### C1 ΓÇö Emoji reactions (`setMessageReaction`)
*Worktree: `llh-wt-c-reactions` ΓÇö separate; owner reviews before merge.*
**Experiment:** sim 5├ù14, log (don't send) a reaction-actuation decision per user message driven by the energy channel. **Success:** Spearman ╧ü(reaction-rate, daily energy) ΓëÑ 0.5 with bootstrap 95% CI excluding 0; reaction frequency Γëñ 1 per 3 user messages; API capability confirmed against Bot API docs. Pass ΓçÆ implement channel-side only.

### C2 ΓÇö Mood-paced multi-bubble replies
*Worktree: `llh-wt-c-bubbles` ΓÇö separate; owner reviews before merge.*
**Experiment:** offline splitter over the 84 live exchanges + 30 sim replies; gaps driven by expressiveness. **Success:** zero mid-sentence splits (mechanical check, n=114); judge-scored naturalness of split renderings ΓëÑ unsplit baseline (paired, n=30, mean diff ΓëÑ 0 with 95% CI not below ΓêÆ0.05); gap-vs-expressiveness ╧ü ΓëÑ 0.5. Pass ΓçÆ channel-side actuation only.

### C3 ΓÇö Good-morning/good-night boundary messages ΓÜá (needs tz anchor merged)
*Worktree: `llh-wt-c-boundary` ΓÇö separate; owner reviews before merge.*
**Experiment:** sim 5├ù14 with envelope-edge greeting candidates routed through content/context gates. **Success:** greeting fires within ┬▒0.5 vh of envelope edge on ΓëÑ95% of eligible days; zero daily-cap or quiet-hours violations; zero duplicate greetings per edge; replay byte-exact with feature off.

### C4 ΓÇö Weekly perception poll (the research gold ΓÇö rolling G6)
*Worktree: `llh-wt-c-poll` ΓÇö separate; owner reviews before merge.*
**Experiment IS the instrument; gate is staged.** Stage 1 (implementation gate): dry-run ΓÇö poll renders, answer persists to a `perception_polls` table, zero contact with Session. Success: FakeApplication round-trip test green. Stage 2 (keep-alive gate, after 4 live weeks): owner response ΓëÑ 3/4 weeks; report ╧ü(owner rating, weekly judge mean) and ╧ü(owner rating, true mood M) with CIs ΓÇö **any** response rate ΓëÑ 3/4 keeps it; the correlations are the research output, not a pass/fail bar.

### C5 ΓÇö Pinned status line ΓÜá (needs command router merged)
*Worktree: `llh-wt-c-pinned` ΓÇö separate; owner reviews before merge.*
**Experiment:** rate-limit dry-run ΓÇö simulate one day of edit cadence (every state change, coalesced ΓëÑ60 s) against recorded limits. **Success:** Γëñ48 edits/day, zero simulated 429s; owner approves the wording template. Runtime-only surface, zero session contact.

### C6+C7 cluster ΓÇö Edited-message steer + inline quick-replies (it4 layer on) ΓÜá
*Worktree: `llh-wt-c-it4probes` ΓÇö separate; owner reviews before merge.*
**Experiment:** sim 5├ù7 with decision layer ON: (a) edit events become `KIND_USER_MESSAGE` correction steers ΓÇö success: 100% of edits delivered exactly once at next boundary, zero replay divergence with feature off; (b) quick-reply buttons on event popups feed `tool_decide_event` inputs ΓÇö success: verdict distribution shifts measurably between "tell me more"/"have fun" (╧ç┬▓ p < 0.05 across pooled seeds), zero parse failures. Both inert while it4 flags are off (live bot runs classic).

### C8 ΓÇö Message effects on high-valence days
*Worktree: `llh-wt-c-effects` ΓÇö separate; owner reviews before merge.*
**Experiment:** (a) API verification ΓÇö `message_effect_id` availability for bots in private chats (documented check, no code); (b) sim threshold study on 5├ù28 mood traces. **Success:** API confirmed usable by bots; chosen threshold selects only top-decile valence days with Γëñ1 effect/week expected frequency. Fail (a) ΓçÆ drop the item entirely.

**Deliberately still skipped:** read-receipts (impossible for bots in private chats), voice/photo (L, dependency-heavy), safety layer (by design).

**Closing note (unchanged from round 1, now with D attached):** the effect finding says the feedback loop ΓÇö what Lily hears back ΓÇö dominates. C4 (poll), C7 (quick-replies), C1 (reactions), and now D's reply-rate telemetry all enrich exactly that channel; they are the highest-leverage items and the reason D's success criteria are measured through C4's instrument.

