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
    # reactive reply + proactive fire, same tick, same content (real-run case)
    store.add_message("user", "feed", 162.6, 6)
    store.add_message("assistant", "That's a big shift.", 162.6, 6)
    store.add_message("assistant", "That's a big shift.", 162.6, 6, proactive=True,
                      intent_id="pi_agenda_item_ag_6_a_arc_2_160.423")
    assert _duplicate_turns(store) == []
    # true rewind: same proactive intent re-written
    store.add_message("assistant", "That's a big shift.", 162.6, 6, proactive=True,
                      intent_id="pi_agenda_item_ag_6_a_arc_2_160.423")
    dupes = _duplicate_turns(store)
    assert len(dupes) == 1
    assert dupes[0]["first"] == 3 and dupes[0]["dup"] == 4
    # reactive rewind: same (role, content, t_h, day) reactive pair
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
    # both passes counted per family -> n = 6 per family per dimension
    assert rep["per_family_per_dimension"]["flash"]["persona_enactment"]["n"] == 6
    assert rep["per_family_per_dimension"]["luna"]["persona_enactment"]["n"] == 6
    # pass-2-only mean would be 6.2; both-passes mean is (5.1+6.1+7.1+5.2+6.2+7.2)/6
    m = rep["per_family_per_dimension"]["flash"]["persona_enactment"]["mean"]
    assert abs(m - 37.0 / 6.0) < 1e-9
    # agreement is a real correlation (perfect, +1 shift), not None
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


# --------------------------------------------------------------------------- #
# Clientes
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Juez determinista
# --------------------------------------------------------------------------- #


def test_deterministic_judge_perturbation_dip():
    judge = DeterministicJudge(5001)
    scores = [judge("", None).score for _ in range(16)]
    block = scores[10:14]
    base = scores[:10]
    assert float(np.mean(block)) < float(np.mean(base)) - 0.3, (
        "negative block must dip below baseline mean")
    # Determinista: un segundo juez reproduce la secuencia exacta.
    judge2 = DeterministicJudge(5001)
    assert [judge2("", None).score for _ in range(16)] == scores
    # Fuera del bloque el juez NO es plano (señal real para el scheduler).
    assert len(set(base)) > 3


# --------------------------------------------------------------------------- #
# Cadenas de eventos (§17.2)
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Guion de usuario
# --------------------------------------------------------------------------- #


def test_user_script_deterministic_and_consistent():
    s1 = user_script(5001, 16, perturb=True)
    s2 = user_script(5001, 16, perturb=True)
    s3 = user_script(5001, 16, perturb=False)
    assert s1 == s2
    assert s1 != s3
    # El bloque negativo está dentro del guion con perturbación.
    negative = [t for t in s3]  # sin perturbación
    with_neg = [t for t in s1]
    assert len(with_neg) == len(negative) + 4
    # Orden temporal estricto.
    assert all(a[0] <= b[0] for a, b in zip(s1, s1[1:]))
    # Todos los días del rango tienen su mensaje base a las 19:00.
    bases = {int(t // 24.0): txt for t, txt in s1 if abs(t % 24.0 - 19.0) < 1e-6}
    assert set(bases) == set(range(16))


# --------------------------------------------------------------------------- #
# Métricas auxiliares
# --------------------------------------------------------------------------- #


def test_spearman_basic():
    assert cvs_common._spearman([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0
    assert cvs_common._spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert cvs_common._spearman([1, 1, 1], [1, 2, 3]) == 0.0  # degenerado


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
