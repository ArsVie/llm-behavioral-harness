"""A7 persistence unit tests — proactive provenance, canonical L4 storage,
call-reproducibility audit (iteration-2 plan §5-A7)."""

from harness.domain import UserModelAssertion, UserModelCategory
from harness.store import SCHEMA_VERSION, SQLiteStore


# --------------------------------------------------------------------------- #
# M1 — messages.intent_id (proactive provenance)
# --------------------------------------------------------------------------- #

def test_add_message_intent_id_roundtrip(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    proactive = store.add_message(
        "assistant", "spontaneous ping", t_h=10.0, day=0,
        proactive=True, intent_id="intent-87",
    )
    reactive = store.add_message("user", "reply", t_h=10.5, day=0)
    session = store.add_message(
        "assistant", "in-session", t_h=11.0, day=0,
        session_id="s1", intent_id=None,
    )
    recent = store.recent_messages()
    assert [m["content"] for m in recent] == [
        "spontaneous ping", "reply", "in-session",
    ]
    assert recent[0]["intent_id"] == "intent-87"
    assert recent[0]["id"] == proactive
    assert recent[1]["intent_id"] is None  # reactive messages keep NULL
    assert recent[2]["session_id"] == "s1"
    assert recent[2]["intent_id"] is None
    day_msgs = store.messages_for_day(0)
    assert day_msgs[0]["intent_id"] == "intent-87"
    # memory_turns view exposes intent_id for session-tracked turns
    store.open_session("s1", 10.9)
    turns = store.turns_for_session("s1")
    assert turns[0]["content"] == "in-session"
    assert "intent_id" in turns[0]
    assert turns[0]["intent_id"] is None
    store.close()


def test_add_message_legacy_call_shape_still_works(tmp_path):
    """The pre-slice positional call shape must keep working."""
    store = SQLiteStore(tmp_path / "s.db")
    mid = store.add_message("assistant", "a", t_h=1.0, day=0, proactive=True)
    assert mid == 1
    assert store.recent_messages()[0]["intent_id"] is None
    store.close()


# --------------------------------------------------------------------------- #
# M2 — canonical L4 categories stored directly
# --------------------------------------------------------------------------- #

def test_upsert_assertion_explicit_canonical_category(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.upsert_assertion(
        UserModelAssertion("mystery:key", "some fact", 0.8, 1.0, ("e1",),
                           "current"),
        category=UserModelCategory.BOUNDARY,
    )
    assert store.get_assertion_category("mystery:key") is UserModelCategory.BOUNDARY
    # list filter by the canonical enum
    listed = store.list_assertions(status="current",
                                   category=UserModelCategory.BOUNDARY)
    assert [a.key for a in listed] == ["mystery:key"]
    assert store.list_assertions(status="current",
                                 category=UserModelCategory.VULNERABILITY) == []
    store.close()


def test_upsert_assertion_accepts_canonical_string_value(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.upsert_assertion(
        UserModelAssertion("k1", "v", 0.5, 1.0, (), "current"),
        category="stable_preference",
    )
    assert store.get_assertion_category("k1") is UserModelCategory.STABLE_PREFERENCE
    # non-canonical strings are rejected, never silently mapped
    try:
        store.upsert_assertion(
            UserModelAssertion("k2", "v", 0.5, 1.0, (), "current"),
            category="made_up_category",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("non-canonical category string must raise")
    store.close()


def test_upsert_assertion_legacy_prefix_derivation(tmp_path):
    """Callers that do not pass the enum (pre-slice memory.py shape) get the
    documented prefix derivation; the stored value is still canonical."""
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
        UserModelAssertion("user:dog:name", "Bruno", 0.7, 3.0, ("e3",), "current")
    )
    store.upsert_assertion(
        UserModelAssertion("noprefix_thing", "odd key", 0.4, 4.0, (), "current")
    )
    assert store.get_assertion_category("current_preferences:coffee") is \
        UserModelCategory.CURRENT_PREFERENCE
    assert store.get_assertion_category("identity") is UserModelCategory.IDENTITY
    assert store.get_assertion_category("user:dog:name") is \
        UserModelCategory.IMPORTANT_ENTITY
    assert store.get_assertion_category("noprefix_thing") is \
        UserModelCategory.IMPORTANT_ENTITY
    assert store.get_assertion_category("missing") is None
    store.close()


def test_load_user_model_buckets_by_stored_category_not_key(tmp_path):
    """The load path reads the category column ONLY — a key that looks like
    one category cannot override the stored canonical category, and keys are
    never parsed for grouping."""
    store = SQLiteStore(tmp_path / "s.db")
    store.upsert_assertion(
        UserModelAssertion("preference:coffee", "espresso", 0.8, 1.0, ("e1",),
                           "current"),
        category=UserModelCategory.STABLE_PREFERENCE,  # stored beats key look
    )
    store.upsert_assertion(
        UserModelAssertion("mystery:key", "a hard boundary", 0.9, 2.0, ("e2",),
                           "current"),
        category=UserModelCategory.BOUNDARY,  # key says nothing about this
    )
    store.upsert_assertion(
        UserModelAssertion("identity", "Bruno's human", 0.9, 3.0, ("e3",),
                           "current")
    )
    store.upsert_assertion(
        UserModelAssertion("odd:key", "unclassifiable", 0.5, 4.0, (), "current"),
        category=UserModelCategory.RECURRING_INTEREST,
    )
    model = store.load_user_model()
    assert model.identity == "Bruno's human"
    assert [a.key for a in model.stable_preferences] == ["preference:coffee"]
    assert [a.key for a in model.boundaries] == ["mystery:key"]
    assert [a.key for a in model.recurring_interests] == ["odd:key"]
    assert model.vulnerabilities == ()
    store.close()


def test_supersede_keeps_canonical_category_provenance(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    store.upsert_assertion(
        UserModelAssertion("current_preferences:coffee", "espresso", 0.8, 1.0,
                           ("e1",), "current")
    )
    store.upsert_assertion(
        UserModelAssertion("current_preferences:coffee", "flat white", 0.85,
                           2.0, ("e2",), "current")
    )
    superseded = store.list_assertions(status="superseded")
    assert len(superseded) == 1 and superseded[0].value == "espresso"
    # both rows carry the canonical category; the latest row reports it
    assert store.get_assertion_category("current_preferences:coffee") is \
        UserModelCategory.CURRENT_PREFERENCE
    model = store.load_user_model()
    assert [a.value for a in model.current_preferences] == ["flat white"]
    store.close()


# --------------------------------------------------------------------------- #
# M3 — call reproducibility audit (eval mode)
# --------------------------------------------------------------------------- #

def test_log_llm_call_privacy_default_drops_repro(tmp_path):
    """Production privacy default: only the hash is kept, the exact request
    payload is NOT persisted even when the caller hands it over."""
    store = SQLiteStore(tmp_path / "s.db")  # audit_mode=False
    call_id = store.log_llm_call(
        0, 1.0, "chat", "system\nuser: hi", "reply", "fake-model",
        repro={
            "model": "fake-model", "temperature": 0.7, "max_tokens": 200,
            "seed": 42, "system": "be yourself", "payload": ["hi"],
            "controls": {"max_tokens": 200}, "memory_policy": "structured_memory",
            "intent_id": "i1", "snapshot_refs": ["snap-1"],
            "timestamp": 1.0, "response": "reply",
        },
    )
    row = store.get_llm_call(call_id)
    assert row is not None
    assert row["prompt_hash"]  # hash always kept
    assert row["response"] == "reply"
    assert row["model"] == "fake-model"
    assert row["repro"] is None  # privacy: exact inputs dropped
    raw = store.conn.execute(
        "SELECT repro_json FROM llm_calls WHERE id = ?", (call_id,)
    ).fetchone()
    assert raw is not None and raw["repro_json"] is None
    store.close()


def test_log_llm_call_eval_mode_persists_repro(tmp_path):
    store = SQLiteStore(tmp_path / "s.db", audit_mode=True)
    repro = {
        "model": "fake-model", "temperature": 0.7, "max_tokens": 200,
        "seed": 42, "system": "be yourself", "payload": ["hi", "there"],
        "generation_controls": {"max_tokens": 200, "closing_tendency": 0.3},
        "memory_policy": "structured_memory", "intent_id": "intent-87",
        "snapshot_refs": ["snap-1", "arc-a1"], "timestamp": 12.5,
        "response": "exact reply",
    }
    call_id = store.log_llm_call(
        0, 12.5, "chat", "system\nuser: hi", "exact reply", "fake-model",
        meta={"state_version": 3}, repro=repro,
    )
    row = store.get_llm_call(call_id)
    assert row is not None
    assert row["repro"] == repro  # exact inputs reconstructable (invariant 19)
    assert row["meta"] == {"state_version": 3}
    assert row["prompt_hash"]
    assert row["t_h"] == 12.5
    # legacy shape (no repro) still fine inside audit mode
    other = store.log_llm_call(0, 13.0, "chat", "p", "r", None)
    assert store.get_llm_call(other)["repro"] is None
    assert store.get_llm_call(999999) is None
    store.close()


def test_schema_version_is_three(tmp_path):
    store = SQLiteStore(tmp_path / "s.db")
    assert SCHEMA_VERSION == 3
    rows = store.conn.execute("SELECT version FROM schema_meta").fetchall()
    assert len(rows) == 1 and rows[0]["version"] == 3
    store.close()
