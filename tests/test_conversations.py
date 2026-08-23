"""it3 B2: multi-turn conversation (plan F6).

Covers the conversation lifecycle (open by either party, continue, close on
exactly one of the four preregistered reasons), per-conversation memory
sessions (L1->L2->L3->L4 at the conversation boundary), resume-no-rewind at
conversation granularity, the runtime's boundary closes (quiet hours,
user_left deadline), and the store conversation-persistence carve-out
(schema v4).

PREREGISTRATIONS (declared here, before the assertions they guard). The
original A1/A2/A3 preregistered the closing-tendency DRAW distribution;
since commit 365ad33 (harness/tunables.py) that draw is feature-flagged
OFF (CLOSING_TENDENCY_ENABLED=False) and MAX_TURNS is None, so the A1/A2/A3
pins are RE-BASELINED to the draw-OFF reality (owner-approved):

* A1 (_rebaseline, draw-OFF): a forced closing_tendency cannot close ANY
  conversation inside an awake window — one bounded feed yields exactly
  one open conversation whose turn count is exactly 2x the fed messages,
  well past the legacy 12-turn cap.
* A2 (_rebaseline, draw-OFF): even at a FORCED closing_tendency of 0.9
  the ``closing_tendency`` closure share is exactly 0.0 — the only closure
  an awake-window→boundary feed can produce is quiet_hours at 23:00.
* A3 (_rebaseline, draw-OFF): same seed, closing_tendency 0.1 vs 0.9 —
  with the draw flagged off the threshold is inert and both arms record
  IDENTICAL close patterns (a divergence would mean the gate regressed).
* A5 (quiet hours): a conversation open at 22:50 closes with
  close_reason == "quiet_hours" at the 23:00 boundary (closed_t_h <= 23.0)
  and no companion turn fires at t_h >= 23.0.

Boundary-close discipline still under test: ``check_conversation_lifecycle``
closes on exactly one of quiet_hours / wind-down expiry / user_left (the
latter at ``USER_LEFT_THRESHOLD_H`` of user silence, measured from the last
user turn), checked before every turn and at every runtime wake; the
runtime parks the rollover AT the deadline instant. Under draw-OFF the only
mid-conversation close levers are the silence backstop and the quiet-hours
boundary — a conversation otherwise runs unbounded.
"""

import asyncio

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.channels.base import FakeChannel
from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.domain import GenerationControls, ProactiveIntent
from harness.judge import ScriptedJudge
from harness.runtime import AsyncRuntime, TimeScale
from harness.scheduler import ProactiveSchedule
from harness.session import MAX_TURNS, Session, USER_LEFT_THRESHOLD_H
from harness.store import SCHEMA_VERSION, SQLiteStore

PERSONA = PersonaParams()
TIMING = TimingParams()
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SEED = 12345

#: 1 virtual hour = 20 ms real (same as tests/test_runtime.py FAST).
FAST = TimeScale(seconds_per_virtual_hour=0.02)


def _session(store, clock, *, replies=None):
    return Session(
        store,
        persona=PERSONA,
        timing=TIMING,
        variant=VARIANT,
        seed=SEED,
        client=FakeClient(responses=replies or ["ok!"]),
        clock=clock,
        judge=ScriptedJudge(score=0.5).judge_day,
    )


def _forced_controls(monkeypatch, closing_tendency: float) -> None:
    """Inject fixed GenerationControls so the close draw threshold is
    exactly ``closing_tendency`` (the directive itself is unchanged)."""

    def forced(directive):
        return GenerationControls(
            max_tokens=300, response_delay_s=1.0,
            closing_tendency=closing_tendency, initiative_factor=1.0,
        )

    monkeypatch.setattr("harness.session.controls_from_directive", forced)


def _scripted_feed(session, clock, n_messages, *, gap_h=0.05, start_h=8.1667):
    """Send exactly ``n_messages`` user messages every ``gap_h`` virtual
    hours from ``start_h`` (day 0). Draw-OFF feed helper: closures are
    BOUNDARY-driven now (quiet_hours / user_left), so the caller bounds
    the feed explicitly instead of waiting for N conversations to close.
    Returns ``(turn_counts, close_reasons)`` for the conversations closed
    SO FAR (the last conversation is typically still open)."""
    clock.advance_to_day(0)
    clock.advance_hours(start_h)
    counts: list[int] = []
    reasons: list[str] = []
    for i in range(n_messages):
        clock.advance_hours(gap_h)
        session.on_message(f"scripted message {i}")
        closed = [
            c for c in session.store.list_conversations()
            if c.close_reason is not None
        ]
        while len(reasons) < len(closed):
            counts.append(len(closed[len(reasons)].turns))
            reasons.append(closed[len(reasons)].close_reason)
    return counts, reasons


# --------------------------------------------------------------------------- #
# store carve-out: schema v4 + conversation persistence seam
# --------------------------------------------------------------------------- #


def test_store_v4_fresh_db_tables_and_backward_compatible_add_message(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION
    tables = {
        r["name"]
        for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"conversations", "conversation_turns"} <= tables
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(messages)")}
    assert "conversation_id" in cols
    # backward compatible: the legacy add_message shape still works
    mid = store.add_message("user", "legacy call", 1.0, 0)
    assert mid > 0
    mid2 = store.add_message(
        "assistant", "with linkage", 1.1, 0, conversation_id="conv-0"
    )
    row = store.recent_messages()[-1]  # chronological order, newest last
    assert row["id"] == mid2 and row["conversation_id"] == "conv-0"
    store.close()


def test_store_v2_db_migrates_to_v4_additive(tmp_path):
    """A pre-v4 database (v2 base + schema_meta=2) migrates to v4: the new
    tables appear, messages gains conversation_id, and every legacy row
    survives intact."""
    import sqlite3

    from harness.store import _SCHEMA, _V2_TABLES

    db = tmp_path / "v2.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.executescript(_V2_TABLES)
    # the real v2 migration adds messages.session_id; mirror it so the
    # hand-built v2 db is a faithful pre-migration artifact
    conn.execute("ALTER TABLE messages ADD COLUMN session_id TEXT")
    conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_meta (version) VALUES (2)")
    conn.execute(
        "INSERT INTO messages (role, content, t_h, day, proactive, session_id) "
        "VALUES ('user', 'legacy hello', 19.0, 0, 0, 'day-0')"
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(db)
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == SCHEMA_VERSION
    tables = {
        r["name"]
        for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"conversations", "conversation_turns"} <= tables
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(messages)")}
    assert {"session_id", "intent_id", "conversation_id"} <= cols
    # legacy data intact and still readable through the L1 seam
    assert store.messages_for_session("day-0")[0]["content"] == "legacy hello"
    assert store.messages_for_day(0)[0]["conversation_id"] is None
    store.close()


def test_store_conversation_roundtrip(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.open_conversation("conv-0", 19.0, "user")
    store.add_conversation_turn("conv-0", "user", "hi", 19.0, 0, message_id=1)
    store.add_conversation_turn(
        "conv-0", "companion", "hello", 19.1, 1, message_id=2
    )
    store.close_conversation("conv-0", 19.5, "closing_tendency")
    store.open_conversation("conv-1", 20.0, "companion")

    conv = store.load_conversation("conv-0")
    assert conv is not None
    assert conv.opened_by == "user"
    assert conv.closed_t_h == 19.5 and conv.close_reason == "closing_tendency"
    assert [(t.speaker, t.turn_index) for t in conv.turns] == [
        ("user", 0), ("companion", 1),
    ]
    open_conv = store.load_open_conversation()
    assert open_conv is not None and open_conv.id == "conv-1"
    assert open_conv.close_reason is None and open_conv.closed_t_h is None
    assert [c.id for c in store.list_conversations()] == ["conv-0", "conv-1"]
    # idempotent re-close (later reason wins; no crash)
    store.close_conversation("conv-0", 20.0, "user_left")
    assert store.load_conversation("conv-0").close_reason == "user_left"
    store.close()


def test_store_messages_for_session_seam_read(tmp_path):
    """messages_for_session — the documented MemoryAgent seam read over the
    L1 session column — returns exactly the session's turns in order."""
    store = SQLiteStore(tmp_path / "s.db")
    store.add_message("user", "a", 1.0, 0, session_id="day-1000")
    store.add_message("assistant", "b", 1.1, 0, session_id="day-1000")
    store.add_message("user", "other", 2.0, 0, session_id="day-1001")
    assert [r["content"] for r in store.messages_for_session("day-1000")] == ["a", "b"]
    assert [r["content"] for r in store.messages_for_session("day-1001")] == ["other"]
    assert store.messages_for_session("day-9999") == []
    store.close()


# --------------------------------------------------------------------------- #
# lifecycle: open / continue / close reasons
# --------------------------------------------------------------------------- #


def test_conversation_opens_on_first_user_message(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock(t_h=19.0)
    session = _session(store, clock)
    session.on_message("hello there")
    conv = store.load_open_conversation()
    assert conv is not None
    assert conv.id == "conv-0" and conv.opened_by == "user"
    assert conv.close_reason is None
    assert [t.speaker for t in conv.turns] == ["user", "companion"]
    assert [t.turn_index for t in conv.turns] == [0, 1]
    # messages carry the conversation linkage + the derived memory session id
    msgs = store.messages_for_day(0)
    assert all(m["conversation_id"] == "conv-0" for m in msgs)
    assert all(m["session_id"] == "day-1000" for m in msgs)
    store.close()


def test_proactive_opener_opens_conversation(tmp_path):
    """A companion proactive opener opens the conversation (opened_by
    companion). The opener is the FIRST companion turn — the no-taper
    floor — so it never closes the conversation on its own."""
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock(t_h=10.0)
    session = _session(store, clock, replies=["proactive hello!"])
    store.save_proactive_intent(ProactiveIntent(
        id="pi_open", reason="schedule", source_type="agenda_item",
        source_id="ag_1", hook="You just finished the pottery class.",
        created_t_h=10.0, valid_until_t_h=13.0, salience=0.5,
        evidence="agenda_item:ag_1",
    ))
    result = session.fire_proactive("pi_open")
    assert result.reply == "proactive hello!"
    conv = store.load_conversation("conv-0")
    assert conv is not None and conv.opened_by == "companion"
    assert len(conv.turns) == 1 and conv.turns[0].speaker == "companion"
    assert conv.close_reason is None
    store.close()


def test_conversation_continues_while_user_replies(tmp_path, monkeypatch):
    """Replies within the silence threshold continue the SAME conversation:
    one conversation row, turn_index strictly increasing, no duplicates."""
    _forced_controls(monkeypatch, closing_tendency=0.0)
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock(t_h=19.0)
    session = _session(store, clock)
    for i in range(3):
        clock.advance_hours(0.5)
        session.on_message(f"message {i}")
    convs = store.list_conversations()
    assert len(convs) == 1 and convs[0].id == "conv-0"
    assert [t.speaker for t in convs[0].turns] == [
        "user", "companion", "user", "companion", "user", "companion",
    ]
    assert [t.turn_index for t in convs[0].turns] == [0, 1, 2, 3, 4, 5]
    assert len(store.messages_for_day(0)) == 6  # no duplicates
    store.close()


def test_turn_counts_non_degenerate_distribution(tmp_path, monkeypatch):
    """A1 (_rebaseline to draw-OFF reality, owner-approved 2026-08): with
    CLOSING_TENDENCY_ENABLED=False the per-turn taper draw never fires — a
    forced closing_tendency of 0.5 cannot close ANY conversation inside an
    awake window. One bounded feed yields exactly ONE open conversation
    whose turn count is exactly 2x the fed messages (deterministic given
    the seed) and well past the legacy 12-turn cap (MAX_TURNS is None)."""
    _forced_controls(monkeypatch, closing_tendency=0.5)
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock()
    session = _session(store, clock)
    counts, reasons = _scripted_feed(session, clock, n_messages=40)
    convs = store.list_conversations()
    assert len(convs) == 1, f"draw-OFF feed must not split conversations: {convs}"
    assert reasons == [] and counts == [], (
        f"no closure can fire inside the awake window: {reasons}"
    )
    assert convs[0].close_reason is None
    assert len(convs[0].turns) == 80  # exact: 2 turns per fed message
    assert len(convs[0].turns) > 12   # legacy MAX_TURNS cap no longer applies
    assert MAX_TURNS is None          # the cap tunable itself is OFF
    store.close()


def test_closing_tendency_share_under_high_tendency(tmp_path, monkeypatch):
    """A2 (_rebaseline to draw-OFF reality, owner-approved 2026-08): even
    at a FORCED closing_tendency of 0.9 the flagged-off draw produces zero
    ``closing_tendency`` closures — the share is exactly 0.0 and the only
    closure a feed crossing one 23:00 boundary can produce is the
    quiet_hours close, registered just past the boundary. Sentinel: flip
    this pin when the draw is re-enabled."""
    _forced_controls(monkeypatch, closing_tendency=0.9)
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock()
    session = _session(store, clock)
    counts, reasons = _scripted_feed(session, clock, n_messages=300)
    closed = [c for c in store.list_conversations() if c.close_reason is not None]
    assert closed, "a feed crossing one midnight must produce the quiet_hours close"
    share = sum(1 for r in reasons if r == "closing_tendency") / len(reasons)
    assert share == 0.0, f"closing_tendency fired while flagged OFF: {reasons}"
    assert reasons == ["quiet_hours"] and counts == [592]
    assert closed[0].closed_t_h is not None and closed[0].closed_t_h > 23.0
    store.close()


def test_closing_tendency_ab_high_vs_low(tmp_path, monkeypatch):
    """A3 (_rebaseline to draw-OFF reality, owner-approved 2026-08): SAME
    seed, closing_tendency 0.1 vs 0.9 — with the draw flagged OFF the
    threshold cannot influence behavior: both arms record IDENTICAL close
    patterns over the same bounded feed (ids, reasons, turn counts and
    close instants all equal). A divergence would mean the flag gate
    regressed; sentinel for re-enabling the draw."""
    patterns: dict[float, list] = {}
    for ct in (0.1, 0.9):
        _forced_controls(monkeypatch, ct)
        store = SQLiteStore(tmp_path / f"ab-{ct}.db")
        clock = VirtualClock()
        session = _session(store, clock)
        _scripted_feed(session, clock, n_messages=300)
        patterns[ct] = [
            (c.id, c.close_reason, c.closed_t_h, len(c.turns))
            for c in store.list_conversations()
        ]
        store.close()
    assert patterns[0.1] == patterns[0.9], (
        "forced closing_tendency influenced behavior while the draw is "
        f"flagged OFF: {patterns}"
    )
    closed = [
        (cid, reason) for cid, reason, _, _ in patterns[0.1]
        if reason is not None
    ]
    assert closed == [("conv-0", "quiet_hours")]


def test_max_turns_cap_closes_conversation(tmp_path, monkeypatch):
    """max_turns (_rebaseline to tunables reality, owner-approved 2026-08):
    the hard turn cap is OFF (``tunables.MAX_TURNS is None``) and the
    closing draw is OFF, so NOTHING closes the conversation at the legacy
    12-turn mark — it keeps accepting exchanges as the SAME open
    conversation."""
    _forced_controls(monkeypatch, closing_tendency=0.0)
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock(t_h=19.0)
    session = _session(store, clock)
    assert MAX_TURNS is None  # the cap tunable itself is OFF
    for i in range(8):  # 8 exchanges = 16 turns > legacy cap of 12
        clock.advance_hours(0.05)
        session.on_message(f"m{i}")
    conv = store.load_conversation("conv-0")
    assert conv is not None
    assert conv.close_reason is None          # no max_turns close ...
    assert len(conv.turns) == 16              # ... even past the legacy cap
    clock.advance_hours(0.05)
    result = session.on_message("m8")
    assert store.load_open_conversation().id == "conv-0"  # SAME conversation
    conv_after = store.load_conversation("conv-0")
    assert conv_after is not None and len(conv_after.turns) == 18
    assert result.reply  # the exchange still completed normally
    store.close()


# --------------------------------------------------------------------------- #
# boundary closes: user_left + quiet_hours
# --------------------------------------------------------------------------- #


def test_user_left_closes_on_next_turn_session(tmp_path):
    """user_left (session/lazy path): silence past USER_LEFT_THRESHOLD_H
    (12 h) closes the conversation with user_left before the next message
    opens a fresh one."""
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock(t_h=10.0)
    session = _session(store, clock)
    session.on_message("morning")  # conv-0 opens at 10:00
    clock.advance_hours(12.1)
    session.on_message("afternoon")  # 22:06 — silence deadline passed
    c0 = store.load_conversation("conv-0")
    assert c0 is not None and c0.close_reason == "user_left"
    assert c0.closed_t_h == 22.1
    c1 = store.load_open_conversation()
    assert c1 is not None and c1.id == "conv-1"
    store.close()


def test_user_left_close_at_deadline_runtime(tmp_path):
    """user_left (runtime path): the rollover parks at the silence deadline
    (last user turn 10:00 + USER_LEFT_THRESHOLD_H) and records the close
    there — not lazily at the next turn. The threshold is read from
    harness.tunables (6 h since commit 365ad33), never hard-coded."""
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock()
    session = _session(store, clock)
    channel = FakeChannel()
    deadline = 10.0 + USER_LEFT_THRESHOLD_H

    async def driver():
        feed = asyncio.create_task(channel.feed("morning", t_h=10.0))
        try:
            await AsyncRuntime(
                session, ProactiveSchedule.restore(SEED, store), channel,
                store=store, timing=TIMING, seed=SEED,
                time_scale=FAST, max_virtual_hours=deadline + 0.5,
            ).run()
        finally:
            if not feed.done():
                feed.cancel()

    asyncio.run(driver())
    conv = store.load_conversation("conv-0")
    assert conv is not None and conv.close_reason == "user_left"
    assert conv.closed_t_h is not None
    assert abs(conv.closed_t_h - deadline) < 1e-9
    store.close()


def test_quiet_hours_close_at_boundary_runtime(tmp_path):
    """A5: a conversation open at 22:50 closes with quiet_hours AT the
    23:00 boundary (closed_t_h <= 23.0) and no companion turn fires inside
    quiet hours (t_h >= 23.0)."""
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock()
    session = _session(store, clock, replies=["night reply"])
    channel = FakeChannel()

    async def driver():
        feed = asyncio.create_task(channel.feed("good evening", t_h=22.833))
        try:
            await AsyncRuntime(
                session, ProactiveSchedule.restore(SEED, store), channel,
                store=store, timing=TIMING, seed=SEED,
                time_scale=FAST, max_virtual_hours=23.5,
            ).run()
        finally:
            if not feed.done():
                feed.cancel()

    asyncio.run(driver())
    conv = store.load_conversation("conv-0")
    assert conv is not None
    assert conv.close_reason == "quiet_hours"
    assert conv.closed_t_h <= 23.0
    msgs = store.messages_for_day(0)
    assert all(
        m["role"] != "assistant" or m["t_h"] < 23.0 for m in msgs
    ), "a companion turn fired inside quiet hours"
    # the reactive reply was still delivered
    assert [m.text for m in channel.sent] == ["night reply"]
    store.close()


def test_quiet_hours_lazy_close_on_next_turn_session(tmp_path):
    """quiet_hours (session/lazy path): continuing an open conversation
    after the boundary closes the old one with quiet_hours and opens a
    fresh one; the reactive reply is still delivered."""
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock(t_h=22.833)
    session = _session(store, clock, replies=["night", "still here"])
    session.on_message("good evening")  # conv-0, 22:50
    clock.advance_hours(0.2)  # 23:02
    result = session.on_message("still here")
    c0 = store.load_conversation("conv-0")
    assert c0 is not None and c0.close_reason == "quiet_hours"
    assert store.load_open_conversation().id == "conv-1"
    assert result.reply == "still here"
    store.close()


def test_conversation_opened_inside_quiet_hours_keeps_running(tmp_path, monkeypatch):
    """A conversation that OPENED inside quiet hours crossed no boundary:
    it keeps running (no spurious quiet_hours close)."""
    _forced_controls(monkeypatch, closing_tendency=0.0)
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock(t_h=3.0)
    session = _session(store, clock)
    session.on_message("night owl")  # conv-0 at 03:00
    clock.advance_hours(0.5)
    session.on_message("still here")  # 03:30, same conversation
    conv = store.load_open_conversation()
    assert conv is not None and conv.id == "conv-0"
    assert conv.close_reason is None
    assert len(conv.turns) == 4
    store.close()


# --------------------------------------------------------------------------- #
# resume at conversation granularity (no rewind)
# --------------------------------------------------------------------------- #


def test_resume_mid_conversation_no_rewind(tmp_path, monkeypatch):
    """A4: kill/reopen mid-conversation (same store, same seed) — the
    resumed run continues the SAME conversation: no duplicate turns,
    turn_index continues from the persisted count."""
    _forced_controls(monkeypatch, closing_tendency=0.0)
    path = tmp_path / "s.db"
    store = SQLiteStore(path)
    clock = VirtualClock(t_h=19.0)
    session = _session(store, clock)
    for i in range(3):
        clock.advance_hours(0.1)
        session.on_message(f"turn {i}")
    conv = store.load_open_conversation()
    assert conv is not None and conv.id == "conv-0" and len(conv.turns) == 6
    store.close()

    store2 = SQLiteStore(path)
    clock2 = VirtualClock(t_h=20.0)
    session2 = _session(store2, clock2, replies=["resumed reply"])
    assert session2.open_conversation_id() == "conv-0"  # reopened
    session2.on_message("resumed message")
    conv2 = store2.load_open_conversation()
    assert conv2 is not None and conv2.id == "conv-0"  # SAME conversation
    assert len(conv2.turns) == 8  # 6 + 2, no rewind, no duplicates
    assert [t.turn_index for t in conv2.turns] == list(range(8))
    n_msgs = store2.conn.execute(
        "SELECT COUNT(*) AS n FROM messages"
    ).fetchone()["n"]
    assert n_msgs == 8
    store2.close()


def test_resume_closed_conversation_stays_closed(tmp_path, monkeypatch):
    """A closed conversation stays closed across a restart: the resumed
    session attaches to the successor conversation instead of reopening
    the closed one. (_rebaseline to draw-OFF reality, owner-approved
    2026-08: conv-0 is closed by the user_left silence backstop — the
    forced closing_tendency=1.0 must NOT close anything while the draw is
    flagged OFF, which this test also pins.)"""
    _forced_controls(monkeypatch, closing_tendency=1.0)
    path = tmp_path / "s.db"
    store = SQLiteStore(path)
    clock = VirtualClock(t_h=10.0)
    session = _session(store, clock)
    session.on_message("one")     # conv-0 opens at 10:00
    clock.advance_hours(0.1)
    session.on_message("two")     # 10.1 — still open (draw flagged OFF)
    c0 = store.load_conversation("conv-0")
    assert c0 is not None and c0.close_reason is None
    clock.advance_hours(USER_LEFT_THRESHOLD_H + 0.1)   # 16.2 > deadline 16.1
    session.on_message("three")   # silence backstop closes conv-0; conv-1 opens
    c0 = store.load_conversation("conv-0")
    assert c0 is not None and c0.close_reason == "user_left"
    store.close()

    store2 = SQLiteStore(path)
    clock2 = VirtualClock(t_h=16.4)
    session2 = _session(store2, clock2)
    assert session2.open_conversation_id() == "conv-1"  # successor, no reopen
    session2.on_message("four")
    assert store2.load_open_conversation().id == "conv-1"
    c0_after = store2.load_conversation("conv-0")
    assert c0_after is not None and c0_after.close_reason == "user_left"
    store2.close()


# --------------------------------------------------------------------------- #
# per-conversation memory sessions + crash-window recovery
# --------------------------------------------------------------------------- #


def test_one_memory_session_per_conversation(tmp_path, monkeypatch):
    """Two conversations get two DISTINCT memory sessions (day-1000,
    day-1001); each closes its own L2/L3/L4 tail at its own boundary.
    (_rebaseline to draw-OFF reality, owner-approved 2026-08: the
    conversations are closed by the user_left silence backstop instead of
    a closing_tendency draw; the still-open successor holds an eager L1
    row but NO closed tail.)"""
    _forced_controls(monkeypatch, closing_tendency=1.0)
    store = SQLiteStore(tmp_path / "s.db")
    clock = VirtualClock(t_h=10.0)
    session = _session(store, clock)
    session.on_message("first")    # conv-0 opens at 10:00
    clock.advance_hours(USER_LEFT_THRESHOLD_H + 0.1)   # 16.1 > deadline 16.0
    session.on_message("second")   # silence closes conv-0; conv-1 opens
    clock.advance_hours(USER_LEFT_THRESHOLD_H + 0.1)   # 22.2 > deadline 22.1
    session.on_message("third")    # silence closes conv-1; conv-2 opens
    sessions = [
        r["session_id"]
        for r in store.conn.execute(
            "SELECT session_id FROM memory_sessions ORDER BY session_id"
        )
    ]
    # one eager L1 row per conversation (incl. the still-open successor)
    assert sessions == ["day-1000", "day-1001", "day-1002"]
    s0 = store.load_session_summary("day-1000")
    s1 = store.load_session_summary("day-1001")
    assert s0 is not None and s0.source_turn_ids  # L2/L3/L4 tails ran ...
    assert s1 is not None and s1.source_turn_ids  # ... at their own boundary
    assert store.load_session_summary("day-1002") is None  # open conv: no tail
    convs = {m["conversation_id"] for m in store.messages_for_day(0)}
    assert convs == {"conv-0", "conv-1", "conv-2"}
    reasons = {c.id: c.close_reason for c in store.list_conversations()}
    assert reasons == {
        "conv-0": "user_left", "conv-1": "user_left", "conv-2": None,
    }
    store.close()


def test_crash_between_close_and_memory_tail_recovers(tmp_path, monkeypatch):
    """it3 B2 equivalent of the A1 Case-40 crash window: process death
    between close_conversation (persisted) and the conversation memory
    tail. On resume the tail is completed at conversation granularity —
    L2/L3/L4 match a clean run byte-for-byte. (_rebaseline to draw-OFF
    reality, owner-approved 2026-08: the close is triggered by the
    user_left silence backstop instead of a closing_tendency draw.)"""
    _forced_controls(monkeypatch, closing_tendency=1.0)
    path = tmp_path / "s.db"

    def crashed_run():
        store = SQLiteStore(path)
        clock = VirtualClock(t_h=10.0)
        session = _session(store, clock)

        def boom(conv):
            raise RuntimeError(
                "process died between close_conversation and memory tail"
            )

        session._close_conversation_memory = boom
        session.on_message("I have a cat named Luna")       # conv-0 at 10:00
        clock.advance_hours(USER_LEFT_THRESHOLD_H + 0.1)    # past the deadline
        try:
            session.on_message("thanks")  # silence closes; memory tail crashes
        except RuntimeError:
            pass
        c0 = store.load_conversation("conv-0")
        assert c0 is not None and c0.close_reason == "user_left"
        assert store.load_session_summary("day-1000") is None  # tail never ran
        store.close()

    crashed_run()
    # resume: the day finalize completes the missing conversation tail
    store2 = SQLiteStore(path)
    clock2 = VirtualClock(t_h=16.3)
    session2 = _session(store2, clock2)
    clock2.advance_to_day(1)
    session2.ensure_day(1)
    summary = store2.load_session_summary("day-1000")
    assert summary is not None, "conversation memory tail lost after crash"
    assert any("Luna" in e.summary for e in store2.list_episodes())
    assert store2.get_assertion("user:cat") is not None
    # byte-identical to a clean run (same seed, same turns, same timeline)
    store3 = SQLiteStore(tmp_path / "control.db")
    clock3 = VirtualClock(t_h=10.0)
    session3 = _session(store3, clock3)
    session3.on_message("I have a cat named Luna")
    clock3.advance_hours(USER_LEFT_THRESHOLD_H + 0.1)
    session3.on_message("thanks")
    clock3.advance_to_day(1)
    session3.ensure_day(1)
    clean_eps = {(e.id, e.summary) for e in store3.list_episodes()}
    rec_eps = {(e.id, e.summary) for e in store2.list_episodes()}
    assert rec_eps == clean_eps
    store2.close()
    store3.close()
