"""Acceptance tests for sim.run_daily (W2.1).

Per-day composition order and replay contract: see the docstring of
sim/run_daily.py. Seeds and tolerances are documented in each test.
"""
from __future__ import annotations


import numpy as np
import pytest
import yaml

from engine.types import (
    PHASE_FOLLICULAR,
    PHASE_LUTEAL_EARLY,
    PHASE_LUTEAL_LATE,
    PHASE_MENSTRUAL,
    PHASE_OVULATORY,
    MoodVariant,
    PersonaParams,
)
from sim import run_daily
from sim.run_daily import main, run, synthetic_score

ALL_PHASES = {
    PHASE_MENSTRUAL,
    PHASE_FOLLICULAR,
    PHASE_OVULATORY,
    PHASE_LUTEAL_EARLY,
    PHASE_LUTEAL_LATE,
}


# --- 1. Basic SimResult shape ---


def test_run_basic_shape() -> None:
    """run(90, seed=42, DECOUPLED_OFFSETS): 90 records, t=0..89, M in [0,N],
    valid phase_label, seed=42 in all records."""
    result = run(90, seed=42, variant=MoodVariant.DECOUPLED_OFFSETS)

    assert len(result.records) == 90
    assert list(result.t) == list(range(90))

    N = result.params.N
    for r in result.records:
        assert 0 <= r.M <= N
        assert r.phase_label in ALL_PHASES
        assert r.seed == 42


# --- 2. Determinism ---


def test_run_deterministic() -> None:
    """Two identical calls produce identical records (M, mu, eta, m, g, arg)."""
    result_a = run(60, seed=123, variant=MoodVariant.DECOUPLED_OFFSETS)
    result_b = run(60, seed=123, variant=MoodVariant.DECOUPLED_OFFSETS)

    assert len(result_a.records) == len(result_b.records)
    for ra, rb in zip(result_a.records, result_b.records):
        assert ra.M == rb.M
        assert ra.mu == rb.mu
        assert ra.eta == rb.eta
        assert ra.m == rb.m
        assert ra.g == rb.g
        assert ra.arg == rb.arg
        assert ra.score == rb.score
        assert ra.p == rb.p
        assert ra.cycle_day == rb.cycle_day
        assert ra.phase_label == rb.phase_label
        assert ra.seed == rb.seed


# --- 3. Shocks: forced score and mu drop after the shock ---


def test_run_shocks_force_score_and_drop_mu() -> None:
    """shocks={10: -1.0, 11: -1.0, 12: -1.0}: records[10..12].score == -1.0
    exactly; mu drops after the shock (records[13].mu < records[10].mu)."""
    shocks = {10: -1.0, 11: -1.0, 12: -1.0}
    result = run(20, seed=42, variant=MoodVariant.DECOUPLED_OFFSETS, shocks=shocks)

    for t in (10, 11, 12):
        assert result.records[t].score == -1.0

    assert result.records[13].mu < result.records[10].mu


# --- 4. Records reflect mu/eta USED (input state, not the updated one) ---


def test_records_reflect_mu_eta_used(persona: PersonaParams) -> None:
    """records[0].mu == 0.0 and records[0].eta == 0.0 (initial state);
    records[1].mu == rho*0 + k*(score_0 - neutral) computed by hand from
    records[0].score."""
    result = run(5, seed=42, variant=MoodVariant.DECOUPLED_OFFSETS, persona=persona)

    assert result.records[0].mu == 0.0
    assert result.records[0].eta == 0.0

    expected_mu_1 = persona.rho * 0.0 + persona.k * (
        result.records[0].score - persona.score_neutral
    )
    assert result.records[1].mu == pytest.approx(expected_mu_1, abs=1e-12)


# --- 5. Variants: ORIGINAL vs DECOUPLED_OFFSETS differ in the arg series ---


def test_variants_differ_in_arg_series() -> None:
    """Same seed, ORIGINAL vs DECOUPLED_OFFSETS: the arg series is not
    identical (B != 0 and A != 0 by default, so m(t) and g(t) break the
    degeneracy)."""
    result_original = run(30, seed=99, variant=MoodVariant.ORIGINAL)
    result_decoupled = run(30, seed=99, variant=MoodVariant.DECOUPLED_OFFSETS)

    arg_original = result_original.arg
    arg_decoupled = result_decoupled.arg

    assert not np.array_equal(arg_original, arg_decoupled)


# --- 6. synthetic_score: exact override, no RNG; mean ~ 2*(M/N - 0.5) ---


def test_synthetic_score_override_exact_and_no_rng_consumption() -> None:
    """With override, returns the exact clipped value and does NOT consume RNG
    (the same Generator produces the same subsequent sequence as a twin
    Generator that never saw the override call)."""
    rng_a = np.random.default_rng(555)
    rng_b = np.random.default_rng(555)

    score = synthetic_score(7, 10, rng_a, override=0.42)
    assert score == pytest.approx(0.42, abs=1e-12)

# rng_a did not advance: it still produces the same sequence as rng_b, untouched.
    next_a = rng_a.normal(0, 1)
    next_b = rng_b.normal(0, 1)
    assert next_a == next_b


def test_synthetic_score_override_clips_out_of_range() -> None:
    """An override outside [-1, 1] is clipped."""
    rng = np.random.default_rng(1)
    assert synthetic_score(5, 10, rng, override=5.0) == 1.0
    assert synthetic_score(5, 10, rng, override=-3.0) == -1.0


def test_synthetic_score_no_override_mean_matches_formula() -> None:
    """Without override, over many samples the mean of synthetic_score must
    approach 2*(M/N - 0.5) (the noise has mean 0), tolerance ±0.02
    (n=20000, noise sd=0.2 -> sem ~= 0.2/sqrt(20000) ~= 0.0014, comfortable
    margin). The value must always fall in [-1, 1]."""
    rng = np.random.default_rng(2024)
    M, N = 7, 10
    n_samples = 20000
    samples = np.array([synthetic_score(M, N, rng) for _ in range(n_samples)])

    expected_mean = 2.0 * (M / N - 0.5)
    assert np.mean(samples) == pytest.approx(expected_mean, abs=0.02)
    assert np.all(samples >= -1.0) and np.all(samples <= 1.0)


# --- 7. CLI smoke ---


def test_cli_smoke_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """main(["--days", "30", "--seed", "7"]) returns 0."""
    exit_code = main(["--days", "30", "--seed", "7"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "30" in captured.out
    assert "7" in captured.out


def test_cli_params_yaml_changes_result(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    """--params with valid YAML (lam: 0.7) changes the result vs the default.
    Compared indirectly via two runs equivalent to what the CLI does, with
    and without the override."""
    params_path = tmp_path / "params.yaml"
    params_path.write_text(yaml.safe_dump({"lam": 0.7}))

    persona_override = run_daily._load_persona_overrides(str(params_path))
    assert persona_override.lam == pytest.approx(0.7)

    result_default = run(30, seed=7, variant=MoodVariant.DECOUPLED_OFFSETS)
    result_override = run(30, seed=7, variant=MoodVariant.DECOUPLED_OFFSETS, persona=persona_override)

    assert not np.array_equal(result_default.M, result_override.M) or not np.array_equal(
        result_default.arg, result_override.arg
    )

    exit_code = main(["--days", "30", "--seed", "7", "--params", str(params_path)])
    assert exit_code == 0
    capsys.readouterr()


def test_cli_params_invalid_key_exits_2(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    """An invalid key in the --params YAML yields exit code 2."""
    params_path = tmp_path / "bad_params.yaml"
    params_path.write_text(yaml.safe_dump({"not_a_real_field": 1.0}))

    exit_code = main(["--days", "10", "--seed", "1", "--params", str(params_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert captured.err != ""


# --- Extras: default persona/shocks, composition robustness with the public signature ---


def test_run_cycle_day_and_phase_are_entry_state_not_next() -> None:
    """A day-t record's cycle_day/phase_label must correspond to the INPUT
    state to cycle.step (before advancing), not to cycle_next."""
    from engine import cycle as cycle_mod
    from engine import rng as rng_mod

    seed = 321
    persona = PersonaParams()
    cycle_state = cycle_mod.init_state(persona, rng_mod.init_rng(seed))

    result = run(5, seed=seed, variant=MoodVariant.DECOUPLED_OFFSETS, persona=persona)

# The input cycle_day for t=0 is recomputed by hand and compared.
    assert result.records[0].cycle_day == pytest.approx(cycle_state.cycle_day)
