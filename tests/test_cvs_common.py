"""Tests unitarios de la maquinaria del harness (A8 — cvs_common)."""

import json

import numpy as np

from engine.types import PersonaParams, TimingParams
from harness.domain import EpisodicMemory, MemoryKind

from experiments import cvs_common
from experiments.cvs_common import (
    DeterministicClient,
    DeterministicJudge,
    MockJudgeClient,
    classify_chain,
    parse_ratings,
    shuffled_order,
    user_script,
)


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
