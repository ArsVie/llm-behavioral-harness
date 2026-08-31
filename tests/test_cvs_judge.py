"""Judge protocol v2 tests (it3 B9): pairwise, attention probe, BT/Elo."""

import json

import numpy as np
import pytest

from experiments.cvs_judge import (
    PAIRWISE_DIMENSIONS,
    PairwiseFakeJudge,
    blank_turn_stats,
    bradley_terry_scale,
    build_pair_prompt,
    corrupt_transcript,
    elo_scale,
    parse_pair_response,
    pairwise_report,
    read_transcripts,
    run_pairwise_pass,
    sample_pairs,
)


def _healthy_text(cond: str, seed: int, n_turns: int = 6) -> str:
    blocks = []
    for i in range(n_turns):
        blocks.append(f"Day {i + 1}, 10:0{i % 10}\nYou: hello, how are you?")
        blocks.append(
            f"Day {i + 1}, 10:0{(i + 1) % 10}\nNova: warm reply {i} "
            f"remembering the previous turn")
    return "\n\n".join(blocks)


def _make_corpus(tmp_path, conds=("FULL", "NO_ACTUATORS", "NO_LIFE"),
                 seeds=(5001, 5002)):
    out = tmp_path / "out"
    tdir = out / "transcripts"
    tdir.mkdir(parents=True)
    for c in conds:
        for s in seeds:
            (tdir / f"{c}_seed{s}.txt").write_text(
                _healthy_text(c, s), encoding="utf-8")
    return out


# Aggregation: BT / Elo


def test_bradley_terry_recovers_known_order_dense():
    conds = ["c1", "c2", "c3", "c4"]
    outcomes = []
    for i in range(len(conds)):
        for j in range(i + 1, len(conds)):
            for _ in range(30):
                outcomes.append({
                    "dimension": "persona_enactment",
                    "winner_condition": conds[i],
                    "loser_condition": conds[j],
                    "seed": 5001, "pair_index": 0,
                })
    scale = bradley_terry_scale(outcomes, conditions=conds)
    order = sorted(conds, key=lambda c: scale[c], reverse=True)
    assert order == conds
    # Scale is monotone in the true order.
    assert scale["c1"] >= scale["c2"] >= scale["c3"] >= scale["c4"] >= 0


def test_bradley_terry_recovers_known_order_probabilistic():
    """Synthetic outcomes sampled from BT probabilities: order recovered and
    the scale has spread (not all mass on the winner)."""
    rng = np.random.default_rng(7)
    strengths = {"c1": 0.1, "c2": 0.3, "c3": 0.7, "c4": 1.5, "c5": 3.0}
    conds = sorted(strengths)
    outcomes = []
    k = 0
    for i in range(len(conds)):
        for j in range(i + 1, len(conds)):
            a, b = conds[i], conds[j]
            pa = strengths[a] / (strengths[a] + strengths[b])
            for _ in range(50):
                w = a if rng.random() < pa else b
                l = b if w == a else a
                outcomes.append({"dimension": "x", "winner_condition": w,
                                 "loser_condition": l, "seed": 5001,
                                 "pair_index": k})
            k += 1
    scale = bradley_terry_scale(outcomes, conditions=conds)
    order = sorted(conds, key=lambda c: scale[c], reverse=True)
    assert order == ["c5", "c4", "c3", "c2", "c1"]
    assert scale["c5"] > scale["c1"] * 10  # Scale has spread, not degenerate mass.


def test_elo_recovers_known_order_and_is_order_independent():
    conds = ["c1", "c2", "c3", "c4"]
    outcomes = []
    k = 0
    for i in range(len(conds)):
        for j in range(i + 1, len(conds)):
            for _ in range(30):
                outcomes.append({
                    "dimension": "persona_enactment",
                    "winner_condition": conds[i],
                    "loser_condition": conds[j],
                    "seed": 5001, "pair_index": k,
                })
            k += 1  # Unique (seed, pair_index) per pair.
    ratings = elo_scale(outcomes, conditions=conds)
    order = sorted(conds, key=lambda c: ratings[c], reverse=True)
    assert order == conds
    # Processing is sorted by (seed, pair_index).
    shuffled = list(outcomes)
    rng = np.random.default_rng(3)
    rng.shuffle(shuffled)
    ratings2 = elo_scale(shuffled, conditions=conds)
    assert ratings == ratings2


def test_scale_functions_skip_unknown_conditions():
    outcomes = [{"dimension": "x", "winner_condition": "c1",
                 "loser_condition": "c2", "seed": 5001, "pair_index": 0}]
    scale = bradley_terry_scale(outcomes, conditions=["c1", "c2", "ghost"])
    assert "ghost" in scale and scale["c1"] > scale["c2"]
    ratings = elo_scale(outcomes, conditions=["c1", "c2", "ghost"])
    assert ratings["c1"] > 1000 and ratings["c2"] < 1000


# Sampling: within-seed, capped, deterministic per pass


def test_sampling_within_seed_deterministic_and_capped(tmp_path):
    out = _make_corpus(tmp_path)
    transcripts = read_transcripts(out)
    p1a, meta = sample_pairs(transcripts, 1)
    p1b, _ = sample_pairs(transcripts, 1)
    p2, _ = sample_pairs(transcripts, 2)
    assert meta["n_universe"] == 6  # C(3,2) combinations across 2 seeds.
    assert meta["n_selected"] == 6
    assert [x["pair_id"] for x in p1a] == [x["pair_id"] for x in p1b]
    assert [x["pair_id"] for x in p1a] != [x["pair_id"] for x in p2]
    for x in p1a:
        assert x["a"]["seed"] == x["b"]["seed"]  # Pairs are within the same seed.
        assert x["dimension"] in [d["id"] for d in PAIRWISE_DIMENSIONS]
    capped, cmeta = sample_pairs(transcripts, 1, max_pairs=3)
    assert len(capped) == 3 and cmeta["n_selected"] == 3


def test_sampling_identical_across_families(tmp_path):
    """Inter-family agreement compares the SAME pairs: sampling must not
    depend on the judge family."""
    out = _make_corpus(tmp_path)
    transcripts = read_transcripts(out)
    a, _ = sample_pairs(transcripts, 1)
    b, _ = sample_pairs(transcripts, 1)
    assert [(x["pair_id"], x["dimension"], x["a"]["condition"],
             x["b"]["condition"]) for x in a] == \
        [(x["pair_id"], x["dimension"], x["a"]["condition"],
          x["b"]["condition"]) for x in b]


def test_prompt_blind_and_forced(tmp_path):
    out = _make_corpus(tmp_path)
    transcripts = read_transcripts(out)
    pairs, _ = sample_pairs(transcripts, 1)
    p = pairs[0]
    ta = transcripts[f"{p['a']['condition']}_seed{p['a']['seed']}"]
    tb = transcripts[f"{p['b']['condition']}_seed{p['b']['seed']}"]
    prompt = build_pair_prompt(p["dimension"], ta, tb)
    # Condition names do not appear in the prompt.
    for cond in ("FULL", "NO_ACTUATORS", "NO_LIFE"):
        assert cond not in prompt
    assert "Transcript A:" in prompt and "Transcript B:" in prompt
    # Choice is forced and justification required.
    assert "MUST choose exactly one" in prompt
    assert "one-sentence justification" in prompt


# Calibrated challenge dimension


def test_fifth_dimension_calibrated_challenge():
    ids = [d["id"] for d in PAIRWISE_DIMENSIONS]
    assert ids == ["persona_enactment", "trajectory_recall",
                   "relational_quality", "behavioral_dynamics",
                   "calibrated_challenge"]
    assert len(PAIRWISE_DIMENSIONS) == 5


# Probe corruption and attention probe


def test_corrupt_transcript_deterministic_ratio_and_user_turns_intact():
    text = _healthy_text("FULL", 5001)
    rng1 = np.random.default_rng(9201)
    rng2 = np.random.default_rng(9201)
    c1 = corrupt_transcript(text, rng1)
    c2 = corrupt_transcript(text, rng2)
    assert c1 == c2
    stats = blank_turn_stats(c1)
    assert stats["companion_turns"] == 6
    assert stats["blank_turns"] == pytest.approx(2, abs=1)  # 40% of 6 turns.
    assert stats["blank_fraction"] == pytest.approx(0.4, abs=0.1)
    assert text.count("You:") == c1.count("You:")  # User turns are preserved.
    assert "Nova: warm reply" in c1  # Non-blanked companion turns are preserved.


def test_pairwise_pass_identifies_degraded_control_both_families(tmp_path):
    """Acceptance 1: a fake judge that sees blanks classifies the degraded
    probe correctly on every control pair, under BOTH judge families."""
    out = _make_corpus(tmp_path)
    for fam in ("opencode-flash", "opencode-luna"):
        client = PairwiseFakeJudge(42, family=fam, mode="see")
        rec = run_pairwise_pass(out, 1, fam, client, max_pairs=4)
        assert rec["disqualified"] is False
        control = [o for o in rec["outcomes"] if o["control"]]
        assert len(control) == 2
        for o in control:
            assert o["winner_condition"] != "PROBE"
            assert "blank" in o["justification"].lower()
    rep = pairwise_report(out)
    for fam in ("opencode-flash", "opencode-luna"):
        c = rep["degraded_classification"][fam]
        assert c["total"] > 0
        assert c["correct"] == c["total"]
        assert rep["disqualifications"] == []


def test_blind_fake_disqualified_and_outcomes_excluded(tmp_path):
    """Acceptance 2: a fake judge that rates the corrupted transcript highly
    is disqualified for the pass; its outcomes are excluded from scales."""
    out = _make_corpus(tmp_path)
    blind = PairwiseFakeJudge(42, family="opencode-flash", mode="blind")
    rec = run_pairwise_pass(out, 1, "opencode-flash", blind, max_pairs=4)
    assert rec["disqualified"] is True
    assert any(o["control"] and o["winner_condition"] == "PROBE"
               for o in rec["outcomes"])
    # Control-pair justifications do not mention blanks.
    for o in rec["outcomes"]:
        if o["control"]:
            assert "blank" not in o["justification"].lower()

    see = PairwiseFakeJudge(43, family="opencode-luna", mode="see")
    run_pairwise_pass(out, 1, "opencode-luna", see, max_pairs=4)
    rep = pairwise_report(out)
    assert [d["family"] for d in rep["disqualifications"]] == ["opencode-flash"]
    # The flash pass is excluded from the scales.
    assert rep["per_family_per_dimension"]["opencode-flash"][
        "persona_enactment"]["n_pairs"] == 0
    # PROBE does not appear in the recovered scales.
    for fam in rep["per_family_per_dimension"]:
        for d, sc in rep["per_family_per_dimension"][fam].items():
            assert "PROBE" not in sc["bradley_terry"]
            assert "PROBE" not in sc["elo"]


# Audit trail and response handling


def test_justifications_and_judge_identity_attached_to_every_outcome(tmp_path):
    out = _make_corpus(tmp_path)
    client = PairwiseFakeJudge(42, family="opencode-flash", mode="see")
    rec = run_pairwise_pass(out, 1, "opencode-flash", client, max_pairs=4)
    assert rec["n_outcomes"] == 6  # Four sampled pairs plus two control pairs.
    for o in rec["outcomes"]:
        assert o["valid"] is True
        assert isinstance(o["justification"], str) and len(o["justification"]) > 0
        assert o["family"] == "opencode-flash"
        assert o["pass"] == 1
        assert o["winner"] in ("A", "B")
        assert o["winner_condition"] and o["loser_condition"]
        assert o["dimension"] in [d["id"] for d in PAIRWISE_DIMENSIONS]


def test_parse_pair_response_tolerant():
    assert parse_pair_response(
        '{"winner": "A", "justification": "clear"}')["winner"] == "A"
    assert parse_pair_response(
        '{"winner": "Transcript B", "justification": "clear"}')["winner"] == "B"
    # Ties are invalid.
    assert parse_pair_response(
        '{"winner": "equal", "justification": "z"}')["valid"] is False
    assert parse_pair_response('no json here')["valid"] is False
    # Empty justification is invalid.
    assert parse_pair_response('{"winner": "A", "justification": ""}')["valid"] is False
    assert parse_pair_response('{"winner": "A", "justification": "ok"}')["valid"] is True


class _ScriptedClient:
    """LLMClient-like stub with canned responses (records calls)."""

    supports_json = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, *, system=None, temperature=0.8, json_mode=False,
             max_tokens=None):
        self.calls.append({"messages": messages, "temperature": temperature,
                           "json_mode": json_mode})
        return self.responses.pop(0)

    def close(self):
        pass


def test_invalid_response_retried_once_then_recorded_invalid(tmp_path):
    out = _make_corpus(tmp_path)
    # First response is invalid, then retried successfully.
    client = _ScriptedClient([
        "garbage",
        '{"winner": "A", "justification": "recovered"}',
    ] + ['{"winner": "A", "justification": "ok"}'] * 20)
    rec = run_pairwise_pass(out, 1, "opencode-flash", client, max_pairs=1)
    assert len(client.calls) == 4  # One retried pair (two calls) plus two control pairs.
    first = rec["outcomes"][0]
    assert first["valid"] is True and first["retries"] == 1
    assert first["justification"] == "recovered"
    # Judge calls run in JSON mode at temperature 0.
    assert all(c["json_mode"] for c in client.calls)
    assert all(c["temperature"] == 0.0 for c in client.calls)


def test_persistently_invalid_pair_excluded_from_scale(tmp_path):
    out = _make_corpus(tmp_path)
    client = _ScriptedClient(["nope"] * 12)  # Every response is invalid.
    rec = run_pairwise_pass(out, 1, "opencode-flash", client, max_pairs=1)
    assert all(o["valid"] is False for o in rec["outcomes"])
    assert all(o["retries"] == 1 for o in rec["outcomes"])


# Report artifact


def test_pairwise_report_end_to_end(tmp_path):
    out = _make_corpus(tmp_path)
    for fam in ("opencode-flash", "opencode-luna"):
        for p in (1, 2):
            run_pairwise_pass(out, p, fam, PairwiseFakeJudge(42, family=fam,
                                                             mode="see"))
    rep = pairwise_report(out)
    assert (out / "judge_report_v2.json").exists()
    assert rep["protocol"] == "v2-pairwise"
    assert rep["families"] == ["opencode-flash", "opencode-luna"]
    assert rep["n_families"] == 2
    for fam in rep["families"]:
        for d in [x["id"] for x in PAIRWISE_DIMENSIONS]:
            sc = rep["per_family_per_dimension"][fam][d]
            assert sc["n_pairs"] > 0  # Every dimension gets pairs.
            assert sc["bradley_terry"] and sc["elo"]
    assert rep["inter_family_agreement_spearman_bt"] is not None
    assert rep["degraded_classification"]["opencode-flash"]["correct"] > 0
    assert rep["disqualifications"] == []
