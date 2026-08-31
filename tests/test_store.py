"""SQLite store tests (W-E1) — extended with the vertical-slice §15 seam (A2)."""

from harness.domain import (
    AffectMetadata,
    AgendaItem,
    DailyAgenda,
    EpisodicMemory,
    Interest,
    LifeArc,
    MemoryKind,
    PersonaProfile,
    ProactiveIntent,
    Routine,
    SessionSummary,
    UserModelAssertion,
)
from harness.store import SCHEMA_VERSION, SQLiteStore


def test_message_roundtrip(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.add_message("user", "hello", t_h=1.0, day=0)
    store.add_message("assistant", "hi!", t_h=1.2, day=0)
    recent = store.recent_messages()
    assert [m["role"] for m in recent] == ["user", "assistant"]
    assert [m["content"] for m in recent] == ["hello", "hi!"]
    store.close()


def test_daily_state_upsert(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    row = {"day": 0, "M": 7, "m": 0.1, "g": 1.0, "p": 0.7, "arg": 0.8,
           "mu": 0.2, "eta": 0.1, "cycle_day": 3.0, "phase_label": "follicular",
           "seed": 42, "score": None}
    store.save_daily_state(0, row)
    loaded = store.load_daily_state(0)
    assert loaded is not None
    assert loaded["M"] == 7
    assert loaded["phase_label"] == "follicular"
    row["score"] = 0.5
    store.save_daily_state(0, row)
    assert store.load_daily_state(0)["score"] == 0.5
    store.close()


def test_judgement_shadow_flag(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.save_judgement(0, 0.8, "great", "fake", shadow=True)
    j = store.load_judgement(0)
    assert j is not None and j["score"] == 0.8 and j["shadow"] == 1
    store.save_judgement(0, 0.2, "worse", "fake", shadow=False)
    j = store.load_judgement(0)
    assert j is not None and j["score"] == 0.2 and j["shadow"] == 0
    store.close()


def test_audit_logs(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.log_event(0, 0.5, "day_rollover", "M=5")
    store.log_llm_call(0, 1.0, "chat", "system\nuser: hi", "reply", "fake-model")
    events = store.events_since(0)
    assert events[0]["event"] == "day_rollover"
    calls = store.conn.execute("SELECT * FROM llm_calls").fetchall()
    assert len(calls) == 1
    assert calls[0]["prompt_hash"]
    store.close()


def test_proactive_count(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.add_message("assistant", "a", t_h=1.0, day=0, proactive=True)
    store.add_message("assistant", "b", t_h=2.0, day=0, proactive=True)
    store.add_message("assistant", "c", t_h=3.0, day=0)
    assert store.proactive_count(0) == 2
    store.close()


# Vertical-slice seam

def test_schema_version_recorded(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    row = store.conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row["version"] == SCHEMA_VERSION
    store.close()


def test_persona_roundtrip(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    assert store.load_persona() is None
    profile = PersonaProfile(
        name="Lily",
        core="Curious and warm.",
        interests=(
            Interest("photography", "exact", 0.9),
            Interest("mathematics", "adjacent", 0.6),
        ),
        routines=(Routine("morning walk", 0.25, 1.0, 0.9, 0.5),),
    )
    store.save_persona(profile)
    loaded = store.load_persona()
    assert loaded is not None
    assert loaded.name == "Lily" and loaded.core == profile.core
    assert [i.name for i in loaded.interests] == ["mathematics", "photography"]
    assert loaded.interests[0].bucket == "adjacent"
    assert len(loaded.routines) == 1 and loaded.routines[0].name == "morning walk"
    store.close()


def test_interests_replace(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.save_interests([Interest("metal", "exact", 0.8)])
    store.save_interests([Interest("jazz", "independent", 0.4)])
    interests = store.list_interests()
    assert [i.name for i in interests] == ["jazz"]
    assert interests[0].salience == 0.4
    store.close()


def test_life_arc_crud(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    arc = LifeArc("a1", "photography", "photography", 4, 0.37, "active",
                  "practice portraits")
    store.upsert_life_arc(arc)
    store.upsert_life_arc(LifeArc("a2", "novel", "reading", 1, 0.5, "active",
                                  "read chapter 12"))
    got = store.get_life_arc("a1")
    assert got is not None and got.progress == 0.37
    assert [a.id for a in store.list_life_arcs()] == ["a2", "a1"]
    store.upsert_life_arc(
        LifeArc("a1", "photography", "photography", 4, 0.5, "active",
                "practice portraits")
    )
    arc = store.get_life_arc("a1")
    assert arc is not None and arc.progress == 0.5
    store.update_life_arc_status("a1", "completed")
    assert [a.id for a in store.list_life_arcs(status="completed")] == ["a1"]
    assert [a.id for a in store.list_life_arcs(status="active")] == ["a2"]
    store.close()


def test_agenda_roundtrip_and_status(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    assert store.load_agenda(4) is None
    items = (
        AgendaItem("i1", 9.0, 10.0, "portraits", "arc", "a1", 0.8, "planned"),
        AgendaItem("i2", 18.0, 19.0, "walk", "routine", "morning walk", 0.5,
                   "planned"),
    )
    store.save_agenda(4, DailyAgenda(4, items))
    agenda = store.load_agenda(4)
    assert agenda is not None and agenda.day == 4
    assert [i.id for i in agenda.items] == ["i1", "i2"]
    store.update_agenda_item_status("i1", "completed")
    agenda = store.load_agenda(4)
    assert agenda is not None and agenda.items[0].status == "completed"
    assert [i.id for i in store.list_agenda_items(day=4, status="planned")] == ["i2"]
    # re-saving a day replaces its items (agendas are regenerated, not merged)
    store.save_agenda(4, DailyAgenda(4, items[:1]))
    agenda = store.load_agenda(4)
    assert agenda is not None and [i.id for i in agenda.items] == ["i1"]
    store.close()


def test_proactive_intent_crud_and_resolve(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    intent = ProactiveIntent(
        "p1", "arc progress", "life_arc", "a1", "progress",
        100.0, 104.0, 0.7, "arc a1 at progress 0.37",
    )
    store.save_proactive_intent(intent)
    loaded = store.load_proactive_intent("p1")
    assert loaded is not None and loaded.hook == "progress"
    assert [i.id for i in store.list_proactive_intents()] == ["p1"]
    assert [i.id for i in store.list_proactive_intents(status="active")] == ["p1"]
    store.update_proactive_intent_status("p1", "superseded")
    assert [i.id for i in store.list_proactive_intents(status="active")] == []
    # unresolved before the source exists; resolvable after
    assert store.resolve_intent_source(intent) is None
    store.upsert_life_arc(
        LifeArc("a1", "photography", "photography", 4, 0.37, "active",
                "practice portraits")
    )
    resolved = store.resolve_intent_source(intent)
    assert resolved is not None and resolved.id == "a1"
    # Unknown source types do not resolve.
    alien = ProactiveIntent("p2", "x", "universe", "u1", "h", 1.0, 2.0, 0.1, "e")
    assert store.resolve_intent_source(alien) is None
    store.close()


def test_resolve_intent_source_full_vocabulary(tmp_path):
    """MAJOR-1 gate fix: every source_type harness.proactive emits must
    resolve against the REAL store (agenda_item/life_event -> agenda_items;
    callback/shared_interest/check_in -> episodes; life_arc -> life_arcs)."""
    store = SQLiteStore(tmp_path / "s.db")

    # sources
    item = AgendaItem("i1", 100.0, 102.0, "pottery class", "arc", "arc1",
                      0.7, "completed")
    store.save_agenda(4, DailyAgenda(4, (item,)))
    arc = LifeArc("a1", "photography", "photography", 4, 0.37, "active",
                  "practice portraits")
    store.upsert_life_arc(arc)
    ep = EpisodicMemory("ep1", "user loves pottery", MemoryKind.CALLBACK,
                        160.0, 161.0, 0.9, 0, None, None, "s1", (7,),
                        ("exact quote",), ("pottery",))
    store.insert_episode(ep)

    def intent(st: str, sid: str) -> ProactiveIntent:
        return ProactiveIntent(f"p_{st}", "r", st, sid, "h", 100.0, 110.0,
                               0.5, "e")

    # agenda-backed: schedule + life events are both agenda items
    r1 = store.resolve_intent_source(intent("agenda_item", "i1"))
    assert r1 is not None and r1.id == "i1"
    r2 = store.resolve_intent_source(intent("life_event", "i1"))
    assert r2 is not None and r2.id == "i1"
    # arc-backed
    r3 = store.resolve_intent_source(intent("life_arc", "a1"))
    assert r3 is not None and r3.id == "a1"
    # episode-backed: callbacks, shared-interest anchors, check-in anchors
    for st in ("episodic_memory", "callback", "shared_interest", "check_in"):
        resolved = store.resolve_intent_source(intent(st, "ep1"))
        assert resolved is not None and resolved.id == "ep1", st
    # missing sources -> None (the content gate's existence check)
    for st in ("agenda_item", "life_event", "life_arc",
               "episodic_memory", "callback", "shared_interest", "check_in"):
        assert store.resolve_intent_source(intent(st, "ghost")) is None, st
    # superseded agenda items still RESOLVE (status is the gate's business)
    store.update_agenda_item_status("i1", "skipped")
    r4 = store.resolve_intent_source(intent("life_event", "i1"))
    assert r4 is not None and r4.id == "i1"
    store.close()


def test_supersede_assertion_cross_key(tmp_path):
    """M-1b gate fix: supersede_assertion flips current rows of any key."""
    store = SQLiteStore(tmp_path / "s.db")
    a1 = UserModelAssertion("user:cat", "user has a cat named Luna", 0.6,
                            10.0, ("ep1",), "current")
    store.upsert_assertion(a1)
    store.upsert_assertion(UserModelAssertion("user:luna", "user no longer has luna", 0.5,
                                              50.0, ("ep2",), "current"))
    assert store.get_assertion("user:cat") is not None
    store.supersede_assertion("user:cat")
    # get_assertion returns the most recent row (any status).
    flipped = store.get_assertion("user:cat")
    assert flipped is not None and flipped.status == "superseded"
    superseded = [a for a in store.list_assertions("superseded")]
    assert len(superseded) == 1 and superseded[0].key == "user:cat"
    # unrelated key untouched (still current)
    luna = store.get_assertion("user:luna")
    assert luna is not None and luna.status == "current"
    # idempotent
    store.supersede_assertion("user:cat")
    assert len(store.list_assertions("superseded")) == 1
    store.close()


def test_memory_sessions_turns_and_view(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.open_session("s1", 0.0)
    store.open_session("s1", 0.0)  # idempotent
    id1 = store.add_message("user", "my dog is Bruno", t_h=1.0, day=0,
                            session_id="s1")
    store.add_message("assistant", "noted!", t_h=1.1, day=0, session_id="s1")
    store.add_message("user", "legacy-style turn", t_h=2.0, day=0)  # no session
    turns = store.turns_for_session("s1")
    assert [t["content"] for t in turns] == ["my dog is Bruno", "noted!"]
    assert turns[0]["id"] == id1
    # the memory_turns view only exposes session-tracked turns
    view_ids = {
        r["id"]
        for r in store.conn.execute("SELECT id FROM memory_turns").fetchall()
    }
    assert view_ids == {id1, id1 + 1}
    store.close_session("s1", 3.0)
    store.close()


def test_session_summary_roundtrip(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.open_session("s1", 10.0)
    summary = SessionSummary(
        session_id="s1",
        started_at_t_h=10.0,
        ended_at_t_h=12.5,
        summary="User introduced Bruno.",
        topics=("dogs", "names"),
        user_facts=("dog named Bruno",),
        preference_updates=(),
        companion_events=(),
        relationship_events=(),
        callbacks=(),
        affect_observations=(
            AffectMetadata(0.8, 0.3, 0.5, 0.6, 0.1, 0.9, 0.2, 0.7, True),
        ),
        emotional_peak=True,
        importance=0.7,
        source_turn_ids=(1, 2),
    )
    store.save_session_summary(summary)
    loaded = store.load_session_summary("s1")
    assert loaded is not None
    assert loaded.summary == summary.summary
    assert loaded.topics == ("dogs", "names")
    assert loaded.user_facts == ("dog named Bruno",)
    assert loaded.source_turn_ids == (1, 2)
    assert loaded.emotional_peak is True
    assert loaded.affect_observations[0].user_valence == 0.8
    assert loaded.affect_observations[0].emotional_peak is True
    assert loaded.started_at_t_h == 10.0 and loaded.ended_at_t_h == 12.5
    # upsert updates in place
    store.save_session_summary(
        SessionSummary("s1", 10.0, 13.0, "Revised.", (), (), (), (), (), (), (),
                       False, 0.1, ())
    )
    loaded = store.load_session_summary("s1")
    assert loaded is not None and loaded.summary == "Revised."
    store.close()


def test_episode_roundtrip_touch_and_sources(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    episode = EpisodicMemory(
        id="e1",
        summary="User's dog is Bruno.",
        category=MemoryKind.USER_FACT,
        occurred_at_t_h=1.0,
        created_at_t_h=2.0,
        importance=0.8,
        access_count=0,
        last_accessed_t_h=None,
        affect=AffectMetadata(0.8, 0.3, 0.5, 0.6, 0.1, 0.9, 0.2, 0.7, True),
        source_session_id="s1",
        source_turn_ids=(1, 2),
        verbatim_anchors=("my dog is Bruno",),
        tags=("dog", "identity"),
    )
    store.insert_episode(episode)
    got = store.get_episode("e1")
    assert got is not None
    assert got.category == MemoryKind.USER_FACT
    assert got.verbatim_anchors == ("my dog is Bruno",)
    assert got.tags == ("dog", "identity")
    assert got.source_turn_ids == (1, 2)
    assert got.affect is not None and got.affect.comfort == 0.9
    assert store.list_episode_sources("e1") == [1, 2]
    assert store.episodes_for_turn(2) == ["e1"]
    assert store.touch_episode("e1", 9.0) == 1
    got = store.get_episode("e1")
    assert got is not None
    assert got.access_count == 1 and got.last_accessed_t_h == 9.0
    assert [e.id for e in store.list_episodes(category=MemoryKind.USER_FACT)] == ["e1"]
    assert store.list_episodes(category="user_fact")[0].id == "e1"
    assert store.list_episodes(limit=10, category=MemoryKind.CALLBACK) == []
    store.close()


def test_embeddings_roundtrip(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.insert_episode(
        EpisodicMemory("e1", "s", MemoryKind.USER_FACT, 1.0, 2.0, 0.5, 0,
                       None, None, "s1", (1,), ("a",), ())
    )
    store.save_embedding("e1", [0.1, 0.2, 0.3])
    emb = store.load_embeddings()
    assert len(emb) == 1
    eid, vec = emb[0]
    assert eid == "e1" and len(vec) == 3
    assert abs(vec[0] - 0.1) < 1e-6 and abs(vec[2] - 0.3) < 1e-6
    # overwrite in place
    store.save_embedding("e1", [1.0])
    assert store.load_embeddings()[0][1] == [1.0]
    store.close()


def test_assertions_supersede_and_user_model(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.upsert_assertion(
        UserModelAssertion("current_preferences:coffee", "espresso", 0.8, 1.0,
                           ("e1",), "current")
    )
    store.upsert_assertion(
        UserModelAssertion("identity", "Bruno's human", 0.9, 2.0, ("e2",),
                           "current")
    )
    store.upsert_assertion(
        UserModelAssertion("boundaries:late_night_chat", "avoid after 23h", 0.6,
                           3.0, ("e3",), "current")
    )
    # new evidence supersedes the same-key current (status flip, history kept)
    store.upsert_assertion(
        UserModelAssertion("current_preferences:coffee", "flat white", 0.85, 4.0,
                           ("e4",), "current")
    )
    current = store.list_assertions(status="current")
    assert {a.key for a in current} == {
        "current_preferences:coffee", "identity", "boundaries:late_night_chat",
    }
    superseded = store.list_assertions(status="superseded")
    assert [a.key for a in superseded] == ["current_preferences:coffee"]
    assert superseded[0].value == "espresso"  # provenance kept
    latest = store.get_assertion("current_preferences:coffee")
    assert latest is not None and latest.value == "flat white"
    assert store.get_assertion("missing") is None

    model = store.load_user_model()
    assert model.identity == "Bruno's human"
    assert [a.value for a in model.current_preferences] == ["flat white"]
    assert [a.key for a in model.boundaries] == ["boundaries:late_night_chat"]
    assert model.stable_preferences == ()
    store.close()


def test_previous_judgement_and_latest_interaction(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    assert store.load_previous_judgement(5) is None
    assert store.latest_interaction_t_h() is None
    store.save_judgement(0, 0.4, "j0", "fake", shadow=True)
    store.save_judgement(2, 0.9, "j2", "fake", shadow=True)
    assert store.load_previous_judgement(3) == 0.9
    assert store.load_previous_judgement(2) == 0.4
    store.add_message("assistant", "proactive ping", t_h=5.0, day=0,
                      proactive=True)
    assert store.latest_interaction_t_h() is None  # only user turns count
    store.add_message("user", "reply", t_h=7.0, day=0)
    assert store.latest_interaction_t_h() == 7.0
    store.close()


def test_add_message_session_id_optional_and_backward_compatible(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    with_session = store.add_message("user", "a", t_h=1.0, day=0, session_id="sx")
    without = store.add_message("user", "b", t_h=2.0, day=0)  # old call shape
    assert with_session == 1 and without == 2
    # recent_messages returns ascending order: [with session, without]
    assert store.recent_messages()[0]["session_id"] == "sx"
    assert store.recent_messages()[1]["session_id"] is None
    store.close()
