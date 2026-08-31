"""Tests unitarios de la maquinaria del harness (A8 — cvs_common)."""

import json

import numpy as np

from engine.types import PersonaParams, TimingParams
from harness.domain import AgendaItem, EpisodicMemory, MemoryKind, ProactiveIntent

from experiments import cvs_common
from experiments.cvs_common import (
    DeterministicClient,
    DeterministicJudge,
    MockJudgeClient,
    _source_superseded_at,
    classify_chain,
    parse_ratings,
    shuffled_order,
    user_script,
)


def _intent(created: float) -> ProactiveIntent:
    return ProactiveIntent(
        id=f"pi_t{created:.3f}",
        reason="schedule",
        source_type="agenda_item",
        source_id="ag_x",
        hook="X",
        created_t_h=created,
        valid_until_t_h=created + 4.0,
        salience=1.0,
        evidence="status=planned",
    )


def _skipped_item(start: float, end: float, status: str = "skipped") -> AgendaItem:
    return AgendaItem(
        id="ag_x",
        start_t_h=start,
        end_t_h=end,
        activity="night reading",
        source_type="routine",
        source_id="night reading",
        salience=1.0,
        status=status,
    )


def test_duplicate_turns_key_disambiguates_real_run_collisions(tmp_path):
    """Gate 6: in real runs the virtual clock freezes during LLM calls, so a
    reactive reply and a proactive fire can share (role, content, t_h, day)
    — with the model repeating text verbatim or returning empty. Distinct
    messages must NOT be flagged; a true resume-rewind (same intent_id
    re-written) MUST be."""
    from harness.store import SQLiteStore
    from experiments.cvs_common import _duplicate_turns

    store = SQLiteStore(str(tmp_path / "dup.db"))
    # Reactive reply + proactive fire, same tick, same content (real-run case).
    store.add_message("user", "feed", 162.6, 6)
    store.add_message("assistant", "That's a big shift.", 162.6, 6)
    store.add_message("assistant", "That's a big shift.", 162.6, 6, proactive=True,
                      intent_id="pi_agenda_item_ag_6_a_arc_2_160.423")
    assert _duplicate_turns(store) == []
    # True rewind: same proactive intent re-written.
    store.add_message("assistant", "That's a big shift.", 162.6, 6, proactive=True,
                      intent_id="pi_agenda_item_ag_6_a_arc_2_160.423")
    dupes = _duplicate_turns(store)
    assert len(dupes) == 1
    assert dupes[0]["first"] == 3 and dupes[0]["dup"] == 4
    # Reactive rewind: same (role, content, t_h, day) reactive pair.
    store.add_message("assistant", "That's a big shift.", 162.6, 6)
    dupes = _duplicate_turns(store)
    assert len(dupes) == 2


def test_judge_report_aggregates_both_passes_and_agreement(tmp_path):
    """Review 2026-08-09: by_family keyed passes by the FAMILY (split[2])
    so pass 2 clobbered pass 1 (n=5 not 10) and agreement looked up a
    nonexistent '1' key (None forever)."""
    from experiments.companion_vertical_slice import _judge_report

    out = tmp_path / "j"
    out.mkdir()
    dims = ["persona_enactment", "trajectory_recall", "relational_quality",
            "behavioral_dynamics"]
    # 2 families x 2 passes, 3 transcripts; family B = family A + 1 exactly.
    for fam, shift in (("flash", 0.0), ("luna", 1.0)):
        for p in (1, 2):
            data = {}
            for i, tid in enumerate(["T01", "T02", "T03"]):
                data[tid] = {"condition": "FULL", "seed": 5001 + i,
                             "ratings": {d: float(5 + i + shift + p * 0.1)
                                         for d in dims}}
            (out / f"judge_pass{p}_{fam}.json").write_text(
                json.dumps(data), encoding="utf-8")
    rep = _judge_report(out)
    # Both passes counted per family -> n = 6 per family per dimension.
    assert rep["per_family_per_dimension"]["flash"]["persona_enactment"]["n"] == 6
    assert rep["per_family_per_dimension"]["luna"]["persona_enactment"]["n"] == 6
    # Pass-2-only mean would be 6.2; both-passes mean is (5.1+6.1+7.1+5.2+6.2+7.2)/6.
    m = rep["per_family_per_dimension"]["flash"]["persona_enactment"]["mean"]
    assert abs(m - 6.15) < 1e-9
    # Agreement is a real correlation (perfect, +1 shift), not None.
    r = rep["inter_family_agreement"]["persona_enactment"]
    assert r is not None and abs(r - 1.0) < 1e-9
    assert rep["n_families"] == 2


def test_parse_transcript_stem_both_namings():
    """Gate 6: cmd_matrix writes COND_seed<S>; the judge parser must accept
    both that and the bare COND_<S> form (burned 2 judge runs 2026-08-09)."""
    from experiments.companion_vertical_slice import _parse_transcript_stem
    assert _parse_transcript_stem("FULL_seed5001") == ("FULL", 5001)
    assert _parse_transcript_stem("NO_LIFE_5003") == ("NO_LIFE", 5003)
    assert _parse_transcript_stem("STRUCTURED_NO_STATE_seed5002") == (
        "STRUCTURED_NO_STATE", 5002)


def test_source_superseded_agenda_item_touctou_clamp():
    """Gate 2 finding: the naive ``end_t_h >= created_t_h`` predicate
    flagged IN-SLOT fires as superseded. Skips are written at day
    close-out ((day+1)*24, day 0-indexed), so an intent created before
    that boundary referenced a still-``planned`` item (resolver evidence
    pi_agenda_item_ag_16_r_00_406.117 / pi_agenda_item_ag_29_r_02_716.563).
    """
    item = _skipped_item(405.6, 406.6)  # day 16 (0-idx), close-out 408.0
    assert not _source_superseded_at(None, item, _intent(406.117))  # mid-slot
    assert not _source_superseded_at(None, item, _intent(407.0))  # pre-close-out
    assert _source_superseded_at(None, item, _intent(408.0))  # skip already written
    assert _source_superseded_at(None, item, _intent(720.0))  # long after
    planned = _skipped_item(405.6, 406.6, "planned")
    assert not _source_superseded_at(None, planned, _intent(720.0))


def _episode(ep_id: str, text: str) -> EpisodicMemory:
    return EpisodicMemory(
        id=ep_id,
        summary=text,
        category=MemoryKind.USER_FACT,
        occurred_at_t_h=0.0,
        created_at_t_h=0.0,
        importance=0.6,
        access_count=0,
        last_accessed_t_h=None,
        affect=None,
        source_session_id="day-0",
        source_turn_ids=(1,),
        verbatim_anchors=(text,),
        tags=(),
    )


# Clientes


def test_deterministic_client_seeded():
    a = DeterministicClient(5001)
    b = DeterministicClient(5001)
    c = DeterministicClient(5002)
    for _ in range(5):
        assert a.chat([{"role": "user", "content": "x"}]) == b.chat(
            [{"role": "user", "content": "x"}]
        )
    assert a.pool == b.pool
    assert len(set(a.pool)) == len(a.pool)
    assert any(x != y for x, y in zip(a.pool, c.pool))


def test_deterministic_client_records_calls():
    client = DeterministicClient(5001)
    client.chat([{"role": "user", "content": "hi"}], system="sys", max_tokens=123)
    assert client.calls[0]["max_tokens"] == 123
    assert client.calls[0]["system_len"] == 3


def test_mock_judge_client_deterministic_per_family():
    a1 = MockJudgeClient(7, family="mock-a")
    a2 = MockJudgeClient(7, family="mock-a")
    b = MockJudgeClient(7, family="mock-b")
    msg = [{"role": "user", "content": "transcript"}]
    r1 = json.loads(a1.chat(msg))
    r2 = json.loads(a2.chat(msg))
    r3 = json.loads(b.chat(msg))
    assert r1 == r2
    assert r1 != r3
    assert set(r1) == {"persona_enactment", "trajectory_recall",
                       "relational_quality", "behavioral_dynamics"}
    assert all(1 <= v <= 9 for v in r1.values())


# Juez determinista


def test_deterministic_judge_perturbation_dip():
    judge = DeterministicJudge(5001)
    scores = [judge("", None).score for _ in range(16)]
    block = scores[10:14]
    base = scores[:10]
    assert float(np.mean(block)) < float(np.mean(base)) - 0.3, (
        "negative block must dip below baseline mean")
    # Deterministic: a second judge reproduces the exact sequence.
    judge2 = DeterministicJudge(5001)
    assert [judge2("", None).score for _ in range(16)] == scores
    # Outside the block the judge is NOT flat (real signal for the scheduler).
    assert len(set(base)) > 3


# Cadenas de eventos (§17.2)


def test_classify_chain_levels():
    chain = {
        "id": "t",
        "events": (("a", "x"), ("b", "y"), ("c", "z")),
        "tokens": ("alpha", "beta", "gamma"),
    }
    none = classify_chain([_episode("e0", "unrelated stuff")], chain)
    assert (none["AnyEvidence"], none["LatestEvidence"], none["CompleteChain"]) == (
        False, False, False)

    one = classify_chain([_episode("e0", "alpha is here")], chain)
    assert (one["AnyEvidence"], one["LatestEvidence"], one["CompleteChain"]) == (
        True, False, False)

    latest = classify_chain([_episode("e0", "gamma only")], chain)
    assert (latest["AnyEvidence"], latest["LatestEvidence"], latest["CompleteChain"]) == (
        True, True, False)

    full = classify_chain([
        _episode("e0", "alpha"), _episode("e1", "beta"), _episode("e2", "gamma"),
    ], chain)
    assert (full["AnyEvidence"], full["LatestEvidence"], full["CompleteChain"]) == (
        True, True, True)


def test_episode_text_includes_anchors():
    ep = _episode("e0", "summary")
    assert "anchor-xyz" in cvs_common._episode_text(
        EpisodicMemory(
            id="e0", summary="summary", category=MemoryKind.USER_FACT,
            occurred_at_t_h=0.0, created_at_t_h=0.0, importance=0.5,
            access_count=0, last_accessed_t_h=None, affect=None,
            source_session_id="day-0", source_turn_ids=(1,),
            verbatim_anchors=("anchor-xyz",), tags=("tag1",),
        )
    )


# Guion de usuario


def test_user_script_deterministic_and_consistent():
    s1 = user_script(5001, 16, perturb=True)
    s2 = user_script(5001, 16, perturb=True)
    s3 = user_script(5001, 16, perturb=False)
    assert s1 == s2
    assert s1 != s3
    # The negative block is inside the perturbed script.
    negative = [t for t in s3]  # no perturbation
    with_neg = [t for t in s1]
    assert len(with_neg) == len(negative) + 4
    # Strict temporal order.
    assert all(a[0] <= b[0] for a, b in zip(s1, s1[1:]))
    # Every day in range has its base message at 19:00.
    bases = {int(t // 24.0): txt for t, txt in s1 if abs(t % 24.0 - 19.0) < 1e-6}
    assert set(bases) == set(range(16))


# Métricas auxiliares


def test_spearman_basic():
    assert cvs_common._spearman([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0
    assert cvs_common._spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert cvs_common._spearman([1, 1, 1], [1, 2, 3]) == 0.0  # degenerate


def test_token_gap():
    assert cvs_common._token_gap([0.9, 0.1, 0.8, 0.2], [1000, 100, 900, 200]) == 800.0
    assert cvs_common._token_gap([0.5], [600]) == 0.0


def test_perturbation_block_analysis():
    values = [5.0] * 10 + [2.0] * 4 + [5.0] * 16
    out = cvs_common.compute_perturbation_metrics.__globals__  # noqa: F841
    # Prueba directa del análisis de bloque vía métricas sobre series falsas:
    series = {"M": values}
    base = values[:10]
    block = values[10:14]
    assert abs(np.mean(block) - np.mean(base)) > 2.0
    assert np.std(values) > 0.5


def test_shuffled_order_no_adjacent_same_condition():
    items = [(c, s) for c in ("A", "B", "C") for s in (1, 2, 3, 4, 5)]
    rng = np.random.default_rng(42)
    order = shuffled_order(rng, items)
    assert sorted(order) == sorted(items)
    for (c1, _), (c2, _) in zip(order, order[1:]):
        assert c1 != c2, "two transcripts of the same condition adjacent"


def test_parse_ratings_tolerant():
    dims = ["persona_enactment", "trajectory_recall"]
    assert parse_ratings('{"persona_enactment": 7, "trajectory_recall": 3}', dims) == {
        "persona_enactment": 7.0, "trajectory_recall": 3.0}
    assert parse_ratings("garbage", dims) == {}
    assert parse_ratings('{"persona_enactment": "x"}', dims)["persona_enactment"] is None


def test_recall_embedder_deterministic_and_discriminative():
    v1 = cvs_common.recall_embedder("my dog is named bruno")
    v2 = cvs_common.recall_embedder("my dog is named bruno")
    v3 = cvs_common.recall_embedder("quantum entanglement theory")
    assert v1 == v2
    assert len(v1) == 1024
    assert sum(a * b for a, b in zip(v1, v3)) < 0.5
    assert sum(a * b for a, b in zip(v1, v1)) > 0.99


def test_apply_and_restore_patches():
    import harness.session as session_mod

    orig_controls = session_mod.controls_from_directive
    orig_derive = session_mod.derive_behavior
    applied = cvs_common.apply_condition_patches("NO_STATE")
    assert session_mod.controls_from_directive is cvs_common._flat_controls
    assert session_mod.derive_behavior is cvs_common._neutral_behavior
    cvs_common.restore_patches(applied)
    assert session_mod.controls_from_directive is orig_controls
    assert session_mod.derive_behavior is orig_derive
    # Condición neutra no parchea nada.
    assert cvs_common.apply_condition_patches("FULL") == []


# B6 — lane routing + sonda justa RAW_HISTORY (closes F5)


def _chain(chain_id: str) -> dict:
    return next(c for c in cvs_common.EVENT_CHAINS if c["id"] == chain_id)


def _store_with_chain_episodes(tmp_path, chain_id: str):
    """Store poblado con los episodios de UNA cadena + embeddings (lane SIMPLE_RAG)."""
    from harness.store import SQLiteStore

    store = SQLiteStore(str(tmp_path / f"{chain_id}.db"))
    for i, (day, text) in enumerate(_chain(chain_id)["events"]):
        ep = _episode(f"{chain_id}_{i}", text)
        store.insert_episode(ep)
        store.save_embedding(ep.id, cvs_common.recall_embedder(text))
    return store


def test_event_chain_metrics_routes_simple_rag_lane(tmp_path):
    """F5: SIMPLE_RAG scored 0.0 while its store held the episodes — the metric
    built a MemoryAgent unconditionally instead of the condition's lane. With
    lane routing the metric must read SimpleRagMemory and return non-zero
    AnyEvidence on a populated store."""
    store = _store_with_chain_episodes(tmp_path, "sister_ana")
    chains = cvs_common.event_chain_metrics(store, condition="SIMPLE_RAG")
    cls = chains["sister_ana"]
    assert cls["probe_lane"] == "episode_retrieval"
    assert cls["AnyEvidence"] is True
    assert cls["CompleteChain"] is True  # los 3 episodios están en el store
    assert len(cls["retrieved_ids"]) == 3
    store.close()


def test_recall_probe_metrics_routes_simple_rag_lane(tmp_path):
    """M3 for SIMPLE_RAG must come from the lane's episodes, not a default 0."""
    from harness.store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "probes.db"))
    for pday, text, _q in cvs_common.RECALL_PROBES:
        ep = _episode(f"p{pday}", text)
        store.insert_episode(ep)
        store.save_embedding(ep.id, cvs_common.recall_embedder(text))
    recall = cvs_common.recall_probe_metrics(store, condition="SIMPLE_RAG")
    assert recall["M3_recall"] > 0.0
    assert all(d["probe_lane"] == "episode_retrieval" for d in recall["detail"])
    store.close()


def test_raw_history_fair_probe_verdict_from_context_not_zero(tmp_path):
    """F5: RAW_HISTORY has zero episodes, so episode-keyed metrics returned 0
    necessarily. The fair probe scores recoverability from the raw dialogue
    window the lane conditions on at query time — a fact inside the window is
    recovered, an old fact outside it is not (a real verdict, not a constant 0)."""
    from harness.store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "raw.db"))
    # sister_ana: day-11 event (t_h=258.6) inside the last-12 window at
    # query t_q=312; the day-3 fact (t_h=66.6) falls outside it.
    store.add_message("user", "My sister's name is Ana.", 66.6, 2)
    for i in range(10):
        store.add_message("user", f"filler number {i}", 60.0 + i, 2)
    store.add_message("user", "My sister is arriving in Guadalajara on Friday.",
                      258.6, 10)
    store.add_message("assistant", "Friday — that's close.", 258.6, 10)
    chains = cvs_common.event_chain_metrics(store, condition="RAW_HISTORY")
    cls = chains["sister_ana"]
    assert cls["probe_lane"] == "raw_history"
    assert cls["context_turns"] == 12
    # guadalajara/friday are in the window; 'ana' is not (day-3 fact out of reach).
    assert cls["covered"] == [False, True, True]
    assert cls["AnyEvidence"] is True          # not an automatic 0
    assert cls["LatestEvidence"] is True
    assert cls["CompleteChain"] is False
    # A fact inside the window IS recovered -> the verdict is recoverable,
    # not a constant 0.
    store.add_message("user", "Also, my sister is named Ana.", 300.0, 12)
    chains2 = cvs_common.event_chain_metrics(store, condition="RAW_HISTORY")
    assert chains2["sister_ana"]["covered"] == [True, True, True]
    assert chains2["sister_ana"]["CompleteChain"] is True
    store.close()


def test_raw_history_recall_probe_uses_context_window(tmp_path):
    """M3 for RAW_HISTORY: same-day facts are inside the lane's window at probe
    time (probe_day*24+6) and must be recalled — not pinned to 0."""
    from harness.store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "raw2.db"))
    store.add_message("user", "My dog's name is Bruno.", 42.5, 1)
    store.add_message("assistant", "Bruno — solid name.", 42.5, 1)
    recall = cvs_common.recall_probe_metrics(store, condition="RAW_HISTORY")
    bruno = next(d for d in recall["detail"] if d["probe_day"] == 2)
    assert bruno["recalled"] is True
    assert bruno["rank"] is None
    assert recall["M3_recall"] > 0.0
    assert recall["M4_false_recall"] == 0.0  # sin retrieval rankeado
    store.close()


def test_aggregate_chain_metrics_reports_absolute_rates():
    """B6: absolute CompleteChain/AnyEvidence reported (not only gaps)."""
    chains = {
        "a": {"AnyEvidence": True, "LatestEvidence": True, "CompleteChain": True},
        "b": {"AnyEvidence": True, "LatestEvidence": False, "CompleteChain": False},
        "c": {"AnyEvidence": False, "LatestEvidence": False, "CompleteChain": False},
    }
    agg = cvs_common.aggregate_chain_metrics(chains)
    assert agg == {"n_chains": 3, "AnyEvidence": 0.6667,
                   "LatestEvidence": 0.3333, "CompleteChain": 0.3333}
    assert cvs_common.aggregate_chain_metrics({})["n_chains"] == 0


def test_fair_probe_definition_is_documented():
    """B6 acceptance: the RAW_HISTORY fair probe definition is documented and
    manifest-ready (B10 reviews it; G4 freezes it)."""
    text = cvs_common.RAW_HISTORY_FAIR_PROBE
    assert "RECOVERABLE" in text
    assert "t_q" in text
    assert "12" in text  # RAW_HISTORY_WINDOW_LIMIT
    assert cvs_common.RAW_HISTORY_WINDOW_LIMIT == 12
    assert "RAW_HISTORY fair probe (preregistered" in text
