"""SQLite store tests (W-E1)."""

from harness.store import SQLiteStore


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
