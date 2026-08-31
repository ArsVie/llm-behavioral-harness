"""it3 B7 — prompt persistence in eval mode (closes iteration-2 §6 limitation).

Mechanical acceptance:
1. A prompt containing a forbidden cycle token, persisted in eval mode, is
   CAUGHT by the invariant-16 leak scan over llm_calls (prompt side).
2. ``repro_json`` alone suffices to reconstruct a call: rebuild from the row
   and byte-compare against the payload the client actually received.
3. Eval-mode rows carry the full system prompt + message payload; default
   (non-eval) rows keep hash-only + no payload (privacy preserved).
4. Hash-only rows are reported by the scan as NOT verifiable (no faked
   coverage), and ``rebuild_call`` refuses them honestly.
"""

import json

import pytest

from experiments.cvs_common import (
    DeterministicClient,
    _cycle_leak_hits,
    run_cell,
)
from harness.store import SQLiteStore

MINI_DAYS = 3
MINI_CHECKPOINTS = (2,)

#: Forbidden cycle tokens ONLY inside the system prompt; the messages table
#: and the response stay clean, so any scan hit proves the PROMPT side.
POISON_SYSTEM = (
    "You are Nova, a warm companion. Today is cycle day 12, follicular phase, "
    "with elevated mu."
)
POISON_USER = "How are you today?"
POISON_REPLY = "I'm doing well, thank you."


def _poison_call(store: SQLiteStore) -> int:
    """One logged call whose persisted prompt contains forbidden tokens."""
    return store.log_llm_call(
        1,
        25.0,
        "chat",
        POISON_SYSTEM + "\n" + repr([{"role": "user", "content": POISON_USER}]),
        POISON_REPLY,
        "fake",
        repro={
            "model": "fake",
            "system": POISON_SYSTEM,
            "messages": [{"role": "user", "content": POISON_USER}],
            "max_tokens": 560,
            "temperature": 0.8,
            "json_mode": False,
            "response": POISON_REPLY,
        },
    )


# Acceptance 1 — leak scan against persisted prompts in eval mode


def test_leak_scan_catches_forbidden_token_in_persisted_prompt(tmp_path):
    store = SQLiteStore(tmp_path / "eval.db", audit_mode=True)
    _poison_call(store)
    leaks = _cycle_leak_hits(store)
    # The poisoned row is verifiable: its persisted repro carries system + messages.
    assert leaks["prompt_side"]["verifiable_rows"] == 1
    assert leaks["prompt_side"]["hash_only_rows"] == 0
    assert leaks["prompt_side"]["hits"], (
        f"prompt-side scan found nothing in: {leaks['prompt_side']}"
    )
    assert "cycle day" in leaks["prompt_side"]["hits"]
    assert "follicular" in leaks["prompt_side"]["hits"]
    # Aggregate semantics unchanged: the hit is visible in the total too.
    assert leaks["total"] > 0
    store.close()


def test_leak_scan_reports_hash_only_rows_as_not_verifiable(tmp_path):
    """Non-eval rows persist no payload: the scan says so honestly (0 hits,
    because the forbidden text was never persisted — that is the privacy
    default, not fake coverage)."""
    store = SQLiteStore(tmp_path / "plain.db")  # audit_mode=False
    _poison_call(store)
    leaks = _cycle_leak_hits(store)
    assert leaks["prompt_side"]["verifiable_rows"] == 0
    assert leaks["prompt_side"]["hash_only_rows"] == 1
    assert leaks["prompt_side"]["hits"] == {}
    assert leaks["total"] == 0  # poison text exists nowhere persistable
    store.close()


# Acceptance 2 — repro_json alone reconstructs the call


def test_repro_json_alone_reconstructs_call_byte_exact(tmp_path):
    instances: list[DeterministicClient] = []
    orig_init = DeterministicClient.__init__

    def _init(self, seed, *, model="fake"):
        orig_init(self, seed, model=model)
        instances.append(self)

    DeterministicClient.__init__ = _init
    try:
        out = tmp_path / "run"
        out.mkdir(parents=True, exist_ok=True)
        records = run_cell("FULL", 5001, out, days=MINI_DAYS,
                           checkpoints=MINI_CHECKPOINTS, fake=True, perturb=True)
    finally:
        DeterministicClient.__init__ = orig_init

    client = instances[0]
    store = SQLiteStore(records["db"])
    rows = store.conn.execute("SELECT id FROM llm_calls ORDER BY id").fetchall()
    assert rows, "run produced no llm_calls"
    assert len(rows) == len(client.calls), (
        "1:1 call↔row mapping expected (zip alignment in _enrich_repro_rows)"
    )
    for row, call in zip(rows, client.calls):
        rebuilt = store.rebuild_call(int(row["id"]))
        # Byte-exact: the persisted payload equals what the client received.
        assert rebuilt["system"] == call["system"]
        assert rebuilt["messages"] == call["messages"]
        assert rebuilt["max_tokens"] == call["max_tokens"]
        assert rebuilt["temperature"] == call["temperature"]
        assert rebuilt["json_mode"] == call["json"]
        assert rebuilt["model"] == "fake"
        # The full prompt is persisted, not a length or a hash.
        assert rebuilt["system"] and len(rebuilt["system"]) > 100
    store.close()


def test_rebuild_call_refuses_hash_only_row(tmp_path):
    store = SQLiteStore(tmp_path / "plain.db")  # audit_mode=False
    call_id = _poison_call(store)
    with pytest.raises(ValueError, match="not reconstructable"):
        store.rebuild_call(call_id)
    store.close()


# Acceptance 3 — privacy default preserved, eval rows carry full payload


def test_eval_mode_rows_carry_full_payload_default_rows_hash_only(tmp_path):
    # Non-eval (default): hash kept, exact payload not persisted.
    plain = SQLiteStore(tmp_path / "plain.db")
    cid = _poison_call(plain)
    row = plain.get_llm_call(cid)
    assert row["prompt_hash"]  # backward-compat hash column
    assert row["repro"] is None  # privacy: exact inputs dropped
    assert row["response"] == POISON_REPLY
    plain.close()

    # Eval mode: full system + payload persisted; hash still kept.
    ev = SQLiteStore(tmp_path / "eval.db", audit_mode=True)
    cid2 = _poison_call(ev)
    row2 = ev.get_llm_call(cid2)
    assert row2["prompt_hash"]
    assert row2["repro"]["system"] == POISON_SYSTEM
    assert row2["repro"]["messages"] == [
        {"role": "user", "content": POISON_USER}
    ]
    assert row2["repro"]["max_tokens"] == 560
    assert ev.rebuild_call(cid2)["system"] == POISON_SYSTEM
    ev.close()


def test_run_cell_eval_rows_carry_full_system_and_payload(tmp_path):
    """End-to-end: every llm_calls row of an eval run is reconstructable and
    the scan sees all of them as verifiable (and clean)."""
    out = tmp_path / "run"
    out.mkdir(parents=True, exist_ok=True)
    records = run_cell("FULL", 5001, out, days=MINI_DAYS,
                       checkpoints=MINI_CHECKPOINTS, fake=True, perturb=True)
    store = SQLiteStore(records["db"])
    rows = store.conn.execute(
        "SELECT id, repro_json FROM llm_calls ORDER BY id"
    ).fetchall()
    assert rows, "run produced no llm_calls"
    for r in rows:
        repro = json.loads(r["repro_json"])
        assert repro["system"] and len(repro["system"]) > 100
        assert "messages" in repro
        rebuilt = store.rebuild_call(int(r["id"]))
        assert rebuilt["system"] == repro["system"]
        assert rebuilt["messages"] == repro["messages"]
    leaks = _cycle_leak_hits(store)
    assert leaks["prompt_side"]["verifiable_rows"] == len(rows)
    assert leaks["prompt_side"]["hash_only_rows"] == 0
    assert leaks["prompt_side"]["hits"] == {}
    assert leaks["prompt_side"]["g_bare"] == 0
    store.close()
