---
type: plan
title: Proactive Enablement + Direct Interaction Channels — parallel implementation plan
description: "Two-part plan for parallel leaf subagents. Part A gives the companion a real-time asyncio runtime that genuinely initiates contact (daily rollover, firing loop, persisted schedule, content+context gates, typed reasons, initiator awareness). Part B adds first-class CLI + Telegram channels behind a single Channel protocol. Non-overlapping file ownership, exact seam contracts, sequencing, and merge order."
tags: [plan, proactive, channels, telegram, asyncio, harness, parallel]
timestamp: 2026-08-08
---

# Proactive Enablement + Direct Interaction — parallel plan

**Date:** 2026-08-08 · **Base:** `main` @ 5cae61c (312 tests green) · **Status:** active

This plan is written for **leaf subagents that cannot ask questions**. Every
seam gives the exact signature/data shape each side codes against. Every file
has exactly one owner. `sim/run_interactive.py` stays working at every merge
point (nobody edits it).

## Global rules (apply to every worker)

- **Env / venv / tests** (CONVENTIONS.md): native WSL, `.venv` via `uv`. Run
  your own test file only:
  `cd /home/vruizes/.hermes/projects/llm-behavioral-harness && MPLBACKEND=Agg .venv/bin/python -m pytest tests/<your_test>.py -q`
- **Frozen files (read-only):** `engine/types.py`, `engine/rng.py`,
  `tests/conftest.py`, `CONVENTIONS.md`. **`pyproject.toml` is unfrozen for
  exactly ONE worker (B3)** to add the optional Telegram dependency — no other
  worker may touch it.
- **Own your file + its test only.** If a foreign file looks wrong, report it
  in your summary; do not edit it.
- **No secrets in repo.** Telegram token and LLM creds come from env vars.
- **English only** (identifiers + docstrings). Surgical changes, no gratuitous
  abstractions. No safety/moderation mechanics (single-user POC; any safety
  hook is dead code for now).
- **Additive is mandatory for shared files.** `store.py`, `scheduler.py`,
  `session.py` changes must keep all existing tests green (`test_store.py`,
  `test_scheduler.py`, `test_session.py`, `test_assembler.py`).
- **The engine and `VirtualClock` remain the only time source.** No
  `time.time()`/`datetime.now()` in engine or session paths; the runtime (A3)
  is the *only* place allowed to read wall-clock, and only to pace the virtual
  clock.

## Reference: existing public API you build against (verified against source)

```python
# harness/session.py
class Session:
    store; persona; timing; variant; seed; client; clock; feedback
    current_day: int | None
    def on_message(self, user_text: str) -> TurnResult          # reactive turn
    def fire_proactive(self, reason: str = "schedule") -> TurnResult
    def ensure_day(self, day: int) -> None                      # rollover forward, no rewind
    def finalize_day(self, day: int) -> None                    # judge + end-of-day engine update
    def finalize_current(self) -> None                          # finalize current day if unjudged
    def state_summary(self) -> dict
@dataclass
class TurnResult: reply: str; directive: BehaviorDirective; day: int; hour: float

# harness/clock.py — the ONLY time source
class VirtualClock: t_h: float
    now_h()->float; day()->int; local_hour()->float
    advance_hours(h)->None; advance_to_day(day)->None

# harness/scheduler.py
REASON_SCHEDULE="schedule"; REASON_CALLBACK="callback"; VALID_REASONS=(...)
def plan_proactive_events(days,seed,persona,timing,scores=None)->np.ndarray
@dataclass
class ProactiveSchedule:
    event_hours: np.ndarray
    @classmethod def plan(cls,days,seed,persona,timing,scores=None)->"ProactiveSchedule"
    def due_at(self,t_h)->list[float]; def mark_fired(self,t_h)->None
    def next_pending(self,t_h)->float|None

# harness/store.py (SQLiteStore) — WAL, row_factory=Row, busy_timeout=10s
save_daily_state; load_daily_state; latest_daily_state; update_daily_score
add_message(role,content,t_h,day,proactive=False)->int
recent_messages(limit=12); messages_for_day(day); proactive_count(day)->int
save_judgement; load_judgement; log_event(day,t_h,event,detail); log_llm_call(...)

# harness/client.py — LLMClient Protocol {supports_json; chat(messages,*,system,temperature,json_mode)->str; close()}
class FakeClient(responses=None, echo=False)   # scripted, records .calls
# harness/judge.py — judge_day(transcript,client,*,model,rubric,json_mode)->JudgeResult
class ScriptedJudge(score=0.5).judge_day
```

---
---

# PART A — PROACTIVE ENABLEMENT

**Goal.** The companion initiates contact in **real time** (paced virtual
clock), not just on manual `/advance`. Delivers: (1) an asyncio runtime with a
day-boundary rollover, (2) a firing loop that waits for the next due event and
routes it through the active channel, (3) persisted `schedule_events` so
restarts resume, (4) a **content gate** (valid reason, unexpired validity
window) and **context gate** (cooldown, active window, quiet hours) enforced
*before* generation, (5) an LLM-facing reason restricted to the taxonomy and
stated in the message's first sentence, (6) initiator awareness (proactive
prompt material differs from reactive).

**Design decision (what changes where):** `session.py` keeps its synchronous,
replay-exact daily engine loop; it only gains *initiator-awareness* wording and
a taxonomy-typed reason. All async/real-time orchestration lives in a **new**
`harness/runtime.py` so the deterministic engine path is never entangled with
the event loop, and `run_interactive.py` stays untouched.

## A. Module ownership

| Worker | Concern | Owns (write) | New? |
|---|---|---|---|
| **A1** `schedule-persistence` | Persist the schedule + extend reason taxonomy | `harness/store.py` (additive), `harness/scheduler.py` (additive), `tests/test_schedule_persistence.py` | test new |
| **A2** `gates-and-initiator` | Content/context gates + initiator-aware prompting | `harness/gates.py`, `harness/session.py` (surgical), `tests/test_gates.py` | gates new |
| **A3** `async-runtime` | asyncio rollover + firing loop + entrypoint | `harness/runtime.py`, `sim/run_async.py`, `tests/test_runtime.py` | all new |

No file appears twice. `store.py`+`scheduler.py`→A1 only; `session.py`→A2 only;
`runtime.py`+`run_async.py`→A3 only.

## A. Seam contracts

### Seam A-1 — `schedule_events` persistence (A1 writes; A2 reads for gates; A3 reads/writes for firing)

A1 adds this table to `store.py` `_SCHEMA` (additive `CREATE TABLE IF NOT
EXISTS`, so existing DBs migrate transparently):

```sql
CREATE TABLE IF NOT EXISTS schedule_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    seed     INTEGER NOT NULL,
    t_h      REAL    NOT NULL,        -- absolute virtual hour of the planned firing
    day      INTEGER NOT NULL,        -- int(t_h // 24)
    reason   TEXT    NOT NULL,        -- one of VALID_REASONS
    status   TEXT    NOT NULL DEFAULT 'pending',  -- 'pending' | 'fired' | 'expired'
    fired_t_h REAL,                   -- actual virtual hour it fired (may differ slightly)
    UNIQUE(seed, t_h)
);
CREATE INDEX IF NOT EXISTS idx_schedule_events_seed_status
    ON schedule_events(seed, status);
```

A1 adds these `SQLiteStore` methods (exact signatures — A2/A3 code against them):

```python
def save_schedule_events(self, seed: int, events: list[dict]) -> None:
    """Upsert planned events. Each dict: {"t_h": float, "day": int,
    "reason": str}. INSERT OR IGNORE on (seed, t_h) so re-planning the same
    horizon is idempotent and never resurrects a fired/expired row."""

def pending_schedule_events(self, seed: int) -> list[dict]:
    """Rows with status='pending' for seed, ascending by t_h. Each dict has
    keys: id, seed, t_h, day, reason, status, fired_t_h."""

def mark_schedule_fired(self, seed: int, t_h: float, fired_t_h: float) -> None:
    """Set status='fired', fired_t_h=<arg> for the row (seed, t_h)."""

def mark_schedule_expired(self, seed: int, t_h: float) -> None:
    """Set status='expired' for the row (seed, t_h)."""

def last_proactive_t_h(self, seed: int) -> float | None:
    """Max fired_t_h over status='fired' rows for seed, else None. Used by
    the context gate for cooldown across restarts."""
```

### Seam A-2 — `ProactiveSchedule` persistence bridge (A1 writes; A3 consumes)

A1 extends `ProactiveSchedule` (additive classmethods/methods; keep
`event_hours`, `due_at`, `mark_fired`, `next_pending` unchanged so
`test_scheduler.py` stays green):

```python
@classmethod
def plan_and_persist(cls, days, seed, persona, timing, store, *,
                     reason: str = REASON_SCHEDULE,
                     scores=None) -> "ProactiveSchedule":
    """plan() then store.save_schedule_events(seed, [{t_h, day, reason} ...]).
    Idempotent (INSERT OR IGNORE). Returns a schedule whose _fired set is
    pre-seeded from store.pending_schedule_events (i.e. already-fired rows are
    treated as fired)."""

@classmethod
def restore(cls, seed, store) -> "ProactiveSchedule":
    """Rebuild from store: event_hours = all rows' t_h for seed; _fired =
    every row whose status != 'pending'. For restart-resume without re-planning."""

def mark_fired_persisted(self, t_h: float, fired_t_h: float, seed: int,
                         store) -> None:
    """self.mark_fired(t_h) + store.mark_schedule_fired(seed, t_h, fired_t_h)."""
```

### Seam A-3 — reason taxonomy (A1 owns the constant; A2 + A3 import it)

A1 extends `scheduler.py` to the full DESIGN taxonomy (additive; keeps the two
existing names as members so nothing breaks):

```python
REASON_SCHEDULE       = "schedule"
REASON_CALLBACK       = "callback"
REASON_EVENT          = "event"
REASON_SHARED_INTEREST = "shared_interest"
REASON_CHECK_IN       = "check_in"
VALID_REASONS = (REASON_SCHEDULE, REASON_CALLBACK, REASON_EVENT,
                 REASON_SHARED_INTEREST, REASON_CHECK_IN)
#: default validity window (hours) after the planned t_h, per reason
REASON_VALIDITY_H = {
    REASON_SCHEDULE: 3.0, REASON_CALLBACK: 6.0, REASON_EVENT: 4.0,
    REASON_SHARED_INTEREST: 12.0, REASON_CHECK_IN: 12.0,
}
```
`session.py` (A2) already imports `VALID_REASONS` from `scheduler`; keep that
import. A3 imports `REASON_SCHEDULE`, `REASON_VALIDITY_H`.

### Seam A-4 — gates (A2 writes; A3 calls before every proactive generation)

New file `harness/gates.py`. Pure functions, no I/O except reads through the
injected store. Signatures A3 codes against:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    code: str   # 'ok'|'no_valid_reason'|'expired'|'cooldown'|'quiet_hours'|'daily_cap'

def content_gate(reason: str | None, planned_t_h: float, now_h: float, *,
                 valid_reasons=VALID_REASONS,
                 validity_h=REASON_VALIDITY_H) -> GateDecision:
    """PASS iff reason in valid_reasons AND now_h <= planned_t_h +
    validity_h[reason]. code='no_valid_reason' if reason invalid/None;
    'expired' if past the window."""

def context_gate(now_h: float, day: int, *, store, timing,
                 last_fired_t_h: float | None) -> GateDecision:
    """PASS iff ALL hold, else the first failing code:
      quiet_hours : engine.circadian.envelope(now_h % 24, timing) >= 1e-9
      cooldown    : last_fired_t_h is None OR
                    (now_h - last_fired_t_h) >= timing.min_gap_min/60
      daily_cap   : store.proactive_count(day) < timing.daily_cap
    (envelope==0 already encodes 'active window'/quiet hours by construction,
    matching run_events guards; the gate re-checks at FIRE time because
    user activity, restarts, and clock pacing can change state since planning.)"""
```
A2 writes `tests/test_gates.py` using a real `SQLiteStore(tmp_path/"s.db")`,
`TimingParams()`, and `engine.circadian.envelope`. This is the shared
contract test both A2 and A3 rely on; A3 does not re-test gate internals.

### Seam A-5 — initiator-aware proactive turn (A2 modifies `session.py`; A3 calls existing signature)

A3 calls the **unchanged public signature** `session.fire_proactive(reason)`.
A2's change is internal to `Session._chat`: when initiating, the appended
system text must (a) name the typed reason, (b) instruct the model to **state
that reason naturally in the first sentence**, and (c) offer a concrete hook.
When reactive (`user_text is not None`), optionally prepend a short
"what she was doing" line derived from the current directive/energy (no numbers,
no phase labels — the leakage invariant in `test_assembler.py` must still hold).

Exact new proactive instruction text A2 injects (so A3/tests can assert on it):

```
You are reaching out first. Contact reason: <reason>.
State this reason naturally in your FIRST sentence, then open with a concrete,
verifiable hook. Never guilt-trip, nag, or imply the user owes you contact.
```
Invariants A2 must preserve: `fire_proactive` still raises `ValueError` for a
reason not in `VALID_REASONS` (keeps `test_fire_proactive_validates_reason`);
`TurnResult` shape unchanged; the brief still leaks no numbers/phase/mu/eta.

### Seam A-6 — Runtime ↔ Channel (A3 consumes Part B's `Channel`; see Seam B-1)

A3 imports `from harness.channels.base import Channel, InboundMessage,
OutboundMessage`. This is the **cross-part dependency**: B1 must land before A3
merges. A3 tests use B1's `FakeChannel`.

## A. Runtime spec (A3, authoritative)

`harness/runtime.py`:

```python
@dataclass
class TimeScale:
    seconds_per_virtual_hour: float = 3600.0   # 1.0 => 1 real hour per virtual hour
    # tests pass a tiny value (e.g. 0.001) to run days in milliseconds.

class AsyncRuntime:
    def __init__(self, session: Session, schedule: ProactiveSchedule,
                 channel: Channel, *, store: SQLiteStore, timing: TimingParams,
                 seed: int, time_scale: TimeScale = TimeScale(),
                 max_virtual_hours: float | None = None): ...

    async def run(self) -> None:
        """Start channel with self._on_inbound, then run _rollover_loop and
        _firing_loop concurrently until max_virtual_hours (or cancelled).
        On exit: session.finalize_current(); await channel.stop()."""

    async def _on_inbound(self, msg: InboundMessage) -> None:
        """Reactive path. Advance clock to msg time if the channel supplied a
        virtual t_h; else keep current clock. reply = await
        asyncio.to_thread(self.session.on_message, msg.text);
        await channel.send(OutboundMessage(text=reply.reply, proactive=False))."""

    async def _rollover_loop(self) -> None:
        """Sleep until the next virtual midnight (paced by time_scale), advance
        clock, call session.ensure_day(clock.day()) (finalizes prev day + judge
        + mu/eta update + samples the new day's mood), then re-plan+persist the
        schedule for the extended horizon via
        ProactiveSchedule.plan_and_persist(..., store=store) and refresh
        self.schedule from store.restore(seed, store)."""

    async def _firing_loop(self) -> None:
        """Loop: nxt = schedule.next_pending(clock.now_h()); if None await a
        short poll sleep. Else sleep (nxt-now)*seconds_per_virtual_hour real
        seconds, advance clock to nxt, then GATE before generating:
          cg = content_gate(reason, nxt, clock.now_h())
          xg = context_gate(clock.now_h(), clock.day(), store=store,
                             timing=timing,
                             last_fired_t_h=store.last_proactive_t_h(seed))
          if not (cg.allowed and xg.allowed):
              store.log_event(day, now_h, 'proactive_suppressed', code)
              schedule.mark_fired_persisted(nxt, clock.now_h(), seed, store)  # consume slot
              (mark_schedule_expired instead when cg.code=='expired')
              continue
          res = await asyncio.to_thread(session.fire_proactive, reason)
          await channel.send(OutboundMessage(text=res.reply, proactive=True,
                                             reason=reason))
          schedule.mark_fired_persisted(nxt, clock.now_h(), seed, store)."""
```

`sim/run_async.py` — new async entrypoint (mirrors `run_interactive`'s args)
that builds `Session` + `ProactiveSchedule.restore_or_plan` + the channel from
`harness/config.select_channel(...)` (Seam B-3) and `asyncio.run(runtime.run())`.
It must NOT import or edit `run_interactive.py`.

## A. Sequencing & merge order

1. **A1 lands first** (foundation for Part A): schema + schedule persistence +
   taxonomy. Purely additive → all existing tests green.
2. **A2 in parallel with A1's dependents:** A2 needs A1's `VALID_REASONS`
   extension and `REASON_VALIDITY_H`; if A1 not yet merged, A2 codes against the
   exact constants above and rebases. gates.py has no A1 dependency except the
   taxonomy import → A2 can start immediately, merge after A1.
3. **A3 lands last in Part A:** depends on A1 (persistence), A2 (gates +
   initiator), and **B1 (Channel + FakeChannel)**.

**Merge order:** `A1 → A2 → A3` (with B1 merged before A3; see global order).

## A. Risks

- **Blocking the event loop:** LLM/judge calls are synchronous. Mitigation:
  always wrap `session.*` calls in `asyncio.to_thread`. Single user ⇒ no
  reentrancy, but guard `_on_inbound`/`_firing_loop` against overlapping
  session calls with an `asyncio.Lock` (A3).
- **Replay contract:** the engine's per-day RNG order (cycle.step, mood.step,
  score, step_endogenous) must not change. A3 must only drive rollover through
  `session.ensure_day`/`finalize_*`; it must never call engine steps directly.
- **Double-finalize on restart:** `Session._resume_from` already re-applies a
  finalized latest day. A3's rollover must call `ensure_day` (idempotent
  guard inside) rather than manual finalize to avoid a second update.
- **Schedule re-plan drift:** re-planning at each rollover with `INSERT OR
  IGNORE` keyed on (seed, t_h) must not resurrect fired rows — enforced by only
  inserting, never updating status back to pending (A1 test covers this).

---
---

# PART B — DIRECT INTERACTION (CLI + Telegram channels)

**Goal.** Make CLI and Telegram first-class channels behind ONE `Channel`
protocol; one active channel per process; inbound user messages reach
`Session.on_message`, outbound/proactive replies reach `channel.send`; config
selects the active channel; channels are testable with fakes (no network).

## B. Module ownership

| Worker | Concern | Owns (write) | New? |
|---|---|---|---|
| **B1** `channel-core` | Protocol + data shapes + FakeChannel + config/selection | `harness/channels/__init__.py`, `harness/channels/base.py`, `harness/config.py`, `tests/test_channels_base.py` | all new |
| **B2** `cli-channel` | Async stdin/stdout CLI channel | `harness/channels/cli.py`, `tests/test_channel_cli.py` | all new |
| **B3** `telegram-channel` | python-telegram-bot channel (env token) + dep | `harness/channels/telegram.py`, `pyproject.toml` (dep only), `tests/test_channel_telegram.py` | telegram new |

No overlap. `pyproject.toml` is touched by **B3 only**, and only to add an
optional dependency group.

## B. Seam contracts

### Seam B-1 — the `Channel` protocol + message shapes (B1 writes; A3, B2, B3 implement/consume)

`harness/channels/base.py` (THE most important seam; must land before A3, B2,
B3):

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

@dataclass
class InboundMessage:
    text: str
    sender_id: str | None = None      # channel-native id (chat id, "cli", ...)
    t_h: float | None = None          # virtual hour if the channel drives one; else None
    received_at: float | None = None  # real epoch seconds (optional, telemetry only)

@dataclass
class OutboundMessage:
    text: str
    proactive: bool = False
    reason: str | None = None         # taxonomy tag when proactive

InboundHandler = Callable[[InboundMessage], Awaitable[None]]

class Channel(Protocol):
    name: str
    async def start(self, on_message: InboundHandler) -> None:
        """Begin delivering inbound messages to on_message. Returns once the
        channel is ready (long-lived listeners run as background tasks the
        channel owns)."""
    async def send(self, message: OutboundMessage) -> None:
        """Deliver an outbound (reactive or proactive) message to the user."""
    async def stop(self) -> None:
        """Stop listeners and release resources. Idempotent."""
```

B1 also writes the shared **`FakeChannel`** in the same module (used by A3, B2
tests, and B1's own conformance test):

```python
class FakeChannel:
    name = "fake"
    def __init__(self, inbound: list[str] | None = None): ...
    #  .sent: list[OutboundMessage]           # everything send() received
    #  .handler: InboundHandler | None
    async def start(self, on_message): self.handler = on_message
    async def send(self, message): self.sent.append(message)
    async def stop(self): ...
    async def feed(self, text: str, *, t_h: float | None = None) -> None:
        """Test helper: deliver one inbound message through the handler."""
```

B1 writes `tests/test_channels_base.py`: a protocol-conformance test that any
Channel (FakeChannel here) satisfies `Channel`, plus round-trip `feed→handler`
and `send→.sent`.

### Seam B-2 — routing (A3 owns the handler; channels stay dumb)

Channels never touch `Session`. The runtime (A3) supplies the handler:
`await channel.start(runtime._on_inbound)` and calls `channel.send(...)`.
B2/B3 code only against `InboundHandler`/`OutboundMessage`. This keeps the
routing seam entirely inside `base.py` types.

### Seam B-3 — active-channel selection (B1 writes; A3's `run_async` consumes)

`harness/config.py`:

```python
def select_channel(name: str, *, inbound: list[str] | None = None) -> Channel:
    """Factory for the single active channel (one per process).
      name=='cli'      -> harness.channels.cli.CLIChannel()
      name=='telegram' -> harness.channels.telegram.TelegramChannel.from_env()
      name=='fake'     -> harness.channels.base.FakeChannel(inbound=inbound)
    Import the telegram module LAZILY inside the branch so importing config
    never requires python-telegram-bot to be installed."""

DEFAULT_CHANNEL = "cli"
```
`run_async.py` reads `--channel` (default `DEFAULT_CHANNEL`) and calls
`select_channel`. Env var `HARNESS_CHANNEL` may override (B1's choice; document
in the module docstring).

### Seam B-4 — Telegram env contract (B3)

`TelegramChannel.from_env()` reads:
- `TELEGRAM_BOT_TOKEN` (required at runtime; raise a clear error if missing,
  mirroring `OpenAICompatibleClient`'s missing-key message),
- `TELEGRAM_CHAT_ID` (optional; the single owner's chat — used as the default
  `send` target and to filter inbound to the owner only).

No token in the repo, in tests, or in logs. `send` posts to the owner chat;
`start` registers a message handler that wraps each update as
`InboundMessage(text=update.message.text, sender_id=str(chat_id),
received_at=time.time())` and forwards to the handler.

## B. CLI channel spec (B2)

`harness/channels/cli.py` — `CLIChannel` implements `Channel` using asyncio
stdin/stdout (NOT the synchronous `run_interactive` REPL, which stays as the
dev tool):

```python
class CLIChannel:
    name = "cli"
    async def start(self, on_message):
        # background task: loop reading lines via
        # await asyncio.to_thread(sys.stdin.readline); on non-empty line call
        # await on_message(InboundMessage(text=line.strip(), sender_id="cli")).
        # Blank line / EOF => stop signal.
    async def send(self, message):
        prefix = "[proactive] " if message.proactive else ""
        print(prefix + message.text, flush=True)
    async def stop(self): ...   # cancel the reader task
```
B2 tests: drive `CLIChannel` with a monkeypatched stdin (feed lines) and a
capture of stdout; assert inbound reaches the handler and `send` prints with the
proactive prefix. No real terminal needed.

## B. Telegram channel spec (B3)

- Add to `pyproject.toml` (the ONLY edit B3 makes there):
  ```toml
  [project.optional-dependencies]
  dev = ["pytest>=8"]
  channels = ["python-telegram-bot>=21"]
  ```
  Install locally with `uv pip install --python .venv/bin/python -e ".[dev,channels]"`.
- `telegram.py` imports `telegram`/`telegram.ext` at module top is fine ONLY if
  guarded; to keep the suite importable without the dep, do the import inside
  the class/functions OR wrap the module body so `config.select_channel` never
  imports it unless `name=='telegram'`.
- `tests/test_channel_telegram.py` MUST NOT hit the network and MUST NOT require
  a token: use `pytest.importorskip("telegram")`, then test only the pure seams
  — `from_env` raising without `TELEGRAM_BOT_TOKEN`, and the update→`InboundMessage`
  mapping via a hand-built fake update object. The `Application`/polling loop is
  exercised behind a fake `Bot`/`Application` (monkeypatched), so `send` is
  asserted by capturing the fake bot's `send_message` calls.

## B. Sequencing & merge order

1. **B1 lands first** (foundation for Part B AND unblocks A3): protocol +
   `FakeChannel` + config. Fully self-contained (new files) → trivially green.
2. **B2 and B3 in parallel behind B1.** Both depend only on B1's `base.py`
   types + `config.select_channel` branch (B3 registers its lazy import; B1's
   config branch calls into `cli`/`telegram` modules — B1 writes the branch,
   B2/B3 provide the classes the branch imports lazily).
3. `run_async.py` (owned by A3) wires selection; it merges with A3.

**Merge order:** `B1 → {B2, B3}` (order between B2 and B3 irrelevant).

## B. Risks

- **`pyproject` freeze:** only B3 edits it, and only the optional group. If two
  workers race it, ownership breaks — enforce single-owner (B3) and no other
  worker adds deps.
- **Import-time dependency on telegram:** guarded lazy import + `importorskip`
  keeps the suite green on machines without the extra installed.
- **stdin in tests:** never block on a real TTY; B2 must inject stdin. CLIChannel
  reader uses `asyncio.to_thread(sys.stdin.readline)` so it is cancellable.
- **One active channel per process:** `select_channel` returns a single
  instance; `run_async` must not start two. Documented invariant, not enforced
  by types.

---
---

# GLOBAL SEQUENCING & MERGE ORDER (both parts)

The two "land-first" foundations are **independent files** and can be built in
parallel by A1 and B1:

```
                ┌── A1 (store+scheduler+taxonomy) ──┐
 foundation ────┤                                   ├── A2 (gates+session) ──┐
                └── B1 (Channel proto+Fake+config) ─┘                        │
                                        │                                    ├── A3 (runtime+run_async)  ← Part A complete
                                        ├── B2 (cli channel) ────────────────┘
                                        └── B3 (telegram channel + dep)      ← Part B complete
```

**Strict merge order into `main`:**

1. **A1** and **B1** (either order; independent). ← both foundations
2. **A2**, **B2**, **B3** (parallel; A2 behind A1, B2/B3 behind B1).
3. **A3** (behind A1 + A2 + B1). ← integrates everything; merges last.

**Green-at-every-merge guarantees:**
- After A1: additive tables/methods/constants only → 312 tests still green.
- After B1: all-new files → green.
- After A2: `session.py` change keeps `TurnResult`, `fire_proactive` validation,
  and the assembler leakage invariant → run `test_session.py`, `test_scheduler.py`,
  `test_assembler.py`, `test_gates.py`.
- After B2/B3: all-new files (+ optional dep) → green; telegram test skips
  without the extra installed.
- After A3: new `runtime.py`/`run_async.py` + `test_runtime.py` using
  `FakeChannel` + `FakeClient` + `ScriptedJudge` + tiny `TimeScale` → runs a
  full multi-day proactive cycle in milliseconds, asserting: (a) gates suppress
  in quiet hours/cooldown, (b) `FakeChannel.sent` receives proactive
  `OutboundMessage`s with a reason, (c) restart via `ProactiveSchedule.restore`
  does not re-fire, (d) rollover advances the day and persists new pending
  events.
- `sim/run_interactive.py` is edited by **nobody** → the existing synchronous
  REPL demo keeps working throughout.

# Shared fakes / test-owner map

| Fake / helper | Written by | Reused by |
|---|---|---|
| `FakeChannel` (+ `feed`) | B1 (`channels/base.py`) | A3, B2 tests |
| `FakeClient` (exists) | — (`client.py`) | A3, B2, B3 tests |
| `ScriptedJudge` (exists) | — (`judge.py`) | A3 tests |
| gate contract test (real `SQLiteStore`) | A2 (`test_gates.py`) | A3 relies on it (no re-test) |
| schedule-persistence round-trip test | A1 (`test_schedule_persistence.py`) | A3 relies on it |
| Channel-conformance test | B1 (`test_channels_base.py`) | B2, B3 mirror the shape |

# Out of scope (explicit — do NOT build)

Safety/moderation/crisis mechanics (dead code only); Discord channel; memory
three-tier compression + FTS5 retrieval; tastes onboarding; agenda/life-state
generation; backwards-compat importer; multi-user; APScheduler (the asyncio
`TimeScale` loop replaces it for the POC — note as a documented substitution).
