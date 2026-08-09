"""Tests del manifest de preregistro (A8 — cvs_manifest)."""

import json

from experiments.cvs_manifest import (
    COMPANION_CONDITIONS,
    DIMENSIONS,
    EVENT_CHAINS,
    HYPOTHESES,
    JUDGE_FAMILIES,
    MEMORY_CONDITIONS,
    SEEDS,
    STATE_CONDITIONS,
    THRESHOLDS,
    WEIBULL_FROZEN,
    build_manifest,
)


def test_manifest_required_schema_keys():
    m = build_manifest()
    for key in (
        "schema_version", "experiment", "commit", "dirty", "questions",
        "hypotheses", "conditions", "seeds", "models", "judge", "metrics",
        "thresholds", "context_budget", "embedding_backend",
        "summarizer_backend", "protocol", "config_hash",
    ):
        assert key in m, key


def test_manifest_at_least_two_judge_families():
    m = build_manifest()
    families = m["judge"]["families"]
    assert len(families) >= 2
    ids = {f["id"] for f in families}
    assert len(ids) == len(families), "judge family ids must be unique"
    for f in families:
        assert f["model"] and f["env_key"] and f["base_url"]


def test_manifest_four_independent_dimensions():
    m = build_manifest()
    dims = m["judge"]["dimensions"]
    assert len(dims) >= 4
    ids = {d["id"] for d in dims}
    assert {"persona_enactment", "trajectory_recall", "relational_quality",
            "behavioral_dynamics"} <= ids


def test_manifest_hypotheses_h1_h6():
    m = build_manifest()
    assert [h["id"] for h in m["hypotheses"]] == ["H1", "H2", "H3", "H4", "H5", "H6"]
    for h in m["hypotheses"]:
        assert h["statement"] and h["iv"] and h["dv"] and h["direction"]


def test_manifest_conditions_match_plan():
    m = build_manifest()
    conds = m["conditions"]
    assert set(conds["memory"]) == set(MEMORY_CONDITIONS)
    assert "STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT" in conds["memory_ablation_lanes"]
    assert set(conds["state"]) == set(STATE_CONDITIONS)
    assert set(conds["companion"]) == set(COMPANION_CONDITIONS)
    assert "FULL" in conds["companion"] and "NO_LIFE" in conds["companion"]


def test_manifest_weibull_frozen_statement():
    m = build_manifest()
    assert m["protocol"]["weibull_frozen"] == WEIBULL_FROZEN
    assert "FROZEN" in WEIBULL_FROZEN
    assert "lognormal" in WEIBULL_FROZEN


def test_manifest_frozen_seeds_and_hard_zero_thresholds():
    m = build_manifest()
    assert list(m["seeds"]) == list(SEEDS)
    hard = m["thresholds"]["hard_zero"]
    for key in ("ungrounded_proactive", "wrong_intent", "restart_state_loss",
                "stranded_opportunities", "cycle_state_leakage",
                "memory_provenance_failures", "duplicate_turns",
                "life_dead_duration"):
        assert key in hard
        assert hard[key]["value"] == 0


def test_manifest_config_hash_stable():
    m1 = build_manifest(started_at="2026-08-09T00:00:00+00:00")
    m2 = build_manifest(started_at="2026-08-09T00:00:00+00:00")
    assert m1["config_hash"] == m2["config_hash"]
    assert m1["config_hash"] != build_manifest(
        started_at="2026-08-09T00:00:00+00:00", extra={"seeds": [1]}
    )["config_hash"]


def test_manifest_serializes():
    m = build_manifest()
    raw = json.dumps(m, ensure_ascii=False)
    assert '"schema_version"' in raw
    assert '"weibull_frozen"' in raw


def test_event_chain_tokens_are_distinctive_per_event():
    """Cada token de evento aparece en el texto de SU evento (promoción L3)."""
    for chain in EVENT_CHAINS:
        assert len(chain["events"]) == len(chain["tokens"]) == 3
        for (day, text), token in zip(chain["events"], chain["tokens"]):
            assert token.lower() in text.lower(), (
                f"chain {chain['id']} token {token!r} not in event text {text!r}"
            )
        assert chain["query_day"] > chain["events"][-1][0]


def test_judge_families_declared_globally():
    assert len(JUDGE_FAMILIES) >= 2
    assert JUDGE_FAMILIES[0]["env_key"] == "OPENCODE_GO_API_KEY"
    assert THRESHOLDS["soft_bars"]["M1_grounded_rate"]["value"] == 0.9
    assert len(DIMENSIONS) == 4
