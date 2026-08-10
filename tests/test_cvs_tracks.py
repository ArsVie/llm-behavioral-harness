"""Tests de integración del harness (A8): célula vertical mínima, replay y
tracks de memoria/estado (plumbing determinista, sin LLM real)."""

import json

import pytest

from harness.store import SQLiteStore

from experiments.cvs_common import (
    DeterministicClient,
    mechanical_audit,
    message_stream,
    run_cell,
    run_replay,
    user_script,
)
from experiments.cvs_manifest import (
    EVENT_CHAINS,
    MEMORY_CONDITIONS,
    RECALL_PROBES,
    STATE_CONDITIONS,
)

MINI_DAYS = 3
MINI_CHECKPOINTS = (2,)


def _mini_run(tmp_path, seed=5001):
    out = tmp_path / "run"
    out.mkdir(parents=True, exist_ok=True)
    records = run_cell("FULL", seed, out, days=MINI_DAYS,
                       checkpoints=MINI_CHECKPOINTS, fake=True, perturb=True)
    return out, records


def test_vertical_cell_mini_invariants(tmp_path):
    out, records = _mini_run(tmp_path)
    assert records["restart_loss"] == [{"checkpoint_h": 48.0, "diffs": 0}]
    store = SQLiteStore(records["db"])
    client = DeterministicClient(5001)
    # it3 B3 gate fix: el run consume el stream conversacional COMPLETO de
    # cvs_user (at_t_h + after_reply) — el fixture de la auditoría debe ser
    # ese mismo stream, no la proyección plana legacy (que excluye los
    # after_reply y rompía el conteo de mensajes user).
    from experiments.cvs_user import build_user_stream
    stream = build_user_stream(5001, MINI_DAYS, perturb=True)
    fixture = [(ev.get("t_h") or 0.0, ev["text"]) for ev in stream]
    audit = mechanical_audit(
        store, 5001, MINI_DAYS * 24.0, MINI_DAYS,
        fixture, pool=client.pool,
    )
    assert audit["all_hard_zero"]
    assert audit["ungrounded_proactive"] == 0
    assert audit["stranded_opportunities"] == 0
    assert audit["cycle_state_leakage"] == 0
    assert audit["duplicate_turns"] == 0
    assert audit["fixture_inserts"]["user_ok"]
    assert audit["fixture_inserts"]["assistant_ok"]
    store.close()


def test_vertical_cell_promotes_probe_and_chain_episodes(tmp_path):
    out, records = _mini_run(tmp_path)
    store = SQLiteStore(records["db"])
    eps = list(store.list_episodes(limit=50))
    ids = [e.id for e in eps]
    # Día 1 (0-based) = sonda Bruno; día 2 = evento de cadena sister_ana E1.
    assert any("bruno" in (e.summary + " " + " ".join(e.verbatim_anchors)).lower()
               for e in eps), f"no Bruno episode in {ids}"
    assert any("ana" in (e.summary + " " + " ".join(e.verbatim_anchors)).lower()
               for e in eps), f"no Ana episode in {ids}"
    store.close()


def test_vertical_cell_no_duplicate_turns_across_restart(tmp_path):
    out, records = _mini_run(tmp_path)
    store = SQLiteStore(records["db"])
    stream = message_stream(store)
    keys = [(m["role"], m["content"], m["t_h"], m["day"]) for m in stream]
    assert len(keys) == len(set(keys))
    ids = [m["id"] for m in stream] if "id" in stream[0] else None
    if ids is not None:
        assert ids == sorted(ids)
    store.close()


def test_replay_mini_exact(tmp_path):
    out, records = _mini_run(tmp_path)
    audit = run_replay(5001, MINI_DAYS, out, tmp_path / "replay")
    assert audit["replay_exact"]
    assert audit["messages_equal"]
    assert audit["llm_calls_equal"]
    assert audit["schedule_equal"]
    assert audit["m3_rows_readable"]
    assert audit["n_llm_calls"] > 0
    saved = json.loads((tmp_path / "replay" / "reproducibility_audit.json")
                       .read_text(encoding="utf-8"))
    assert saved["replay_exact"]


def test_memory_track_conditions_are_canonical():
    assert MEMORY_CONDITIONS == ("RAW_CONTEXT", "VERBATIM_RAG", "STRUCTURED_MEMORY")


def test_state_track_conditions_are_canonical():
    assert STATE_CONDITIONS == (
        "NO_STATE", "PROMPT_ONLY_STATE", "MECHANICALLY_ACTUATED_STATE")


def test_recall_probes_cover_structured_promotion_patterns(tmp_path):
    """Las sondas deben promover con el extractor determinista (patrones
    name/possessive/have)."""
    import re

    from experiments.cvs_manifest import RECALL_PROBES

    name_re = re.compile(r"\bmy\s+[a-z]+'s\s+name\s+is\b", re.IGNORECASE)
    poss_re = re.compile(r"\bmy\s+[a-z]+\s+is\b", re.IGNORECASE)
    have_re = re.compile(r"\bi\s+have\s+(?:a|an)\b", re.IGNORECASE)
    for _day, text, _q in RECALL_PROBES:
        assert (name_re.search(text) or poss_re.search(text)
                or have_re.search(text)), f"probe not promotable: {text}"


def test_chain_events_promotable(tmp_path):
    import re

    name_re = re.compile(r"\bmy\s+[a-z]+'s\s+name\s+is\b", re.IGNORECASE)
    poss_re = re.compile(r"\bmy\s+[a-z]+\s+is\b", re.IGNORECASE)
    have_re = re.compile(r"\bi\s+have\s+(?:a|an)\b", re.IGNORECASE)
    for chain in EVENT_CHAINS:
        for _day, text in chain["events"]:
            assert (name_re.search(text) or poss_re.search(text)
                    or have_re.search(text)), f"chain event not promotable: {text}"


def test_state_condition_patch_restores_between_cells(tmp_path):
    """Las células secuenciales del state track no deben filtrar parches."""
    import harness.session as session_mod

    orig = session_mod.controls_from_directive
    out = tmp_path / "state"
    out.mkdir(parents=True, exist_ok=True)
    for cond in STATE_CONDITIONS:
        records = run_cell(cond, 5001, out, days=MINI_DAYS, fake=True,
                           perturb=False, checkpoints=())
        assert session_mod.controls_from_directive is orig, (
            f"patch leaked after {cond}")
        store = SQLiteStore(records["db"])
        assert records["n_messages"] > 0
        store.close()


def test_no_memory_condition_empty_lane(tmp_path):
    out = tmp_path / "nomem"
    out.mkdir(parents=True, exist_ok=True)
    records = run_cell("NO_MEMORY", 5001, out, days=MINI_DAYS, fake=True,
                       perturb=False, checkpoints=())
    store = SQLiteStore(records["db"])
    assert list(store.list_episodes(limit=10)) == []
    store.close()


@pytest.mark.parametrize("seed", [5001, 5002])
def test_mini_cell_deterministic_across_runs(tmp_path, seed):
    out1, rec1 = _mini_run(tmp_path / "a", seed)
    out2, rec2 = _mini_run(tmp_path / "b", seed)
    s1 = SQLiteStore(rec1["db"])
    s2 = SQLiteStore(rec2["db"])
    assert message_stream(s1) == message_stream(s2)
    s1.close()
    s2.close()
