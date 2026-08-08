"""Tests de aceptación para sim.run_daily (W2.1).

Orden de composición por día y contrato de replay: ver docstring de
sim/run_daily.py. Semillas y tolerancias documentadas en cada test.
"""
from __future__ import annotations

import dataclasses
import math

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


# ---------------------------------------------------------------------------
# 1. Forma básica del SimResult


def test_run_basic_shape() -> None:
    """run(90, seed=42, DECOUPLED_OFFSETS): 90 records, t=0..89, M in [0,N],
    phase_label válido, seed=42 en todos los records."""
    result = run(90, seed=42, variant=MoodVariant.DECOUPLED_OFFSETS)

    assert len(result.records) == 90
    assert list(result.t) == list(range(90))

    N = result.params.N
    for r in result.records:
        assert 0 <= r.M <= N
        assert r.phase_label in ALL_PHASES
        assert r.seed == 42


# ---------------------------------------------------------------------------
# 2. Determinismo


def test_run_deterministic() -> None:
    """Dos llamadas idénticas producen records idénticos (M, mu, eta, m, g, arg)."""
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


# ---------------------------------------------------------------------------
# 3. Shocks: score forzado y caída de mu tras el shock


def test_run_shocks_force_score_and_drop_mu() -> None:
    """shocks={10: -1.0, 11: -1.0, 12: -1.0}: records[10..12].score == -1.0
    exacto; mu cae tras el shock (records[13].mu < records[10].mu)."""
    shocks = {10: -1.0, 11: -1.0, 12: -1.0}
    result = run(20, seed=42, variant=MoodVariant.DECOUPLED_OFFSETS, shocks=shocks)

    for t in (10, 11, 12):
        assert result.records[t].score == -1.0

    assert result.records[13].mu < result.records[10].mu


# ---------------------------------------------------------------------------
# 4. Records reflejan mu/eta USADOS (estado de entrada, no el actualizado)


def test_records_reflect_mu_eta_used(persona: PersonaParams) -> None:
    """records[0].mu == 0.0 y records[0].eta == 0.0 (estado inicial);
    records[1].mu == rho*0 + k*(score_0 - neutral) calculado a mano desde
    records[0].score."""
    result = run(5, seed=42, variant=MoodVariant.DECOUPLED_OFFSETS, persona=persona)

    assert result.records[0].mu == 0.0
    assert result.records[0].eta == 0.0

    expected_mu_1 = persona.rho * 0.0 + persona.k * (
        result.records[0].score - persona.score_neutral
    )
    assert result.records[1].mu == pytest.approx(expected_mu_1, abs=1e-12)


# ---------------------------------------------------------------------------
# 5. Variantes: ORIGINAL vs DECOUPLED_OFFSETS difieren en la serie arg


def test_variants_differ_in_arg_series() -> None:
    """Misma semilla, ORIGINAL vs DECOUPLED_OFFSETS: la serie arg no es idéntica
    (B != 0 y A != 0 por defecto, así que m(t) y g(t) rompen la degeneración)."""
    result_original = run(30, seed=99, variant=MoodVariant.ORIGINAL)
    result_decoupled = run(30, seed=99, variant=MoodVariant.DECOUPLED_OFFSETS)

    arg_original = result_original.arg
    arg_decoupled = result_decoupled.arg

    assert not np.array_equal(arg_original, arg_decoupled)


# ---------------------------------------------------------------------------
# 6. synthetic_score: override exacto sin consumir RNG; sin override, media ~
#    2*(M/N - 0.5)


def test_synthetic_score_override_exact_and_no_rng_consumption() -> None:
    """Con override, devuelve el valor exacto clipped y NO consume RNG (mismo
    Generator produce la misma secuencia posterior que un Generator gemelo
    que nunca vio la llamada con override)."""
    rng_a = np.random.default_rng(555)
    rng_b = np.random.default_rng(555)

    score = synthetic_score(7, 10, rng_a, override=0.42)
    assert score == pytest.approx(0.42, abs=1e-12)

    # rng_a no debió avanzar: debe seguir produciendo la misma secuencia que
    # rng_b, que nunca fue tocado.
    next_a = rng_a.normal(0, 1)
    next_b = rng_b.normal(0, 1)
    assert next_a == next_b


def test_synthetic_score_override_clips_out_of_range() -> None:
    """override fuera de [-1, 1] se clipea."""
    rng = np.random.default_rng(1)
    assert synthetic_score(5, 10, rng, override=5.0) == 1.0
    assert synthetic_score(5, 10, rng, override=-3.0) == -1.0


def test_synthetic_score_no_override_mean_matches_formula() -> None:
    """Sin override, sobre muchas muestras la media de synthetic_score debe
    acercarse a 2*(M/N - 0.5) (el ruido es de media 0), tolerancia ±0.02
    (n=20000, sd del ruido=0.2 -> sem ~= 0.2/sqrt(20000) ~= 0.0014, margen
    holgado). El valor siempre debe caer en [-1, 1]."""
    rng = np.random.default_rng(2024)
    M, N = 7, 10
    n_samples = 20000
    samples = np.array([synthetic_score(M, N, rng) for _ in range(n_samples)])

    expected_mean = 2.0 * (M / N - 0.5)
    assert np.mean(samples) == pytest.approx(expected_mean, abs=0.02)
    assert np.all(samples >= -1.0) and np.all(samples <= 1.0)


# ---------------------------------------------------------------------------
# 7. CLI smoke


def test_cli_smoke_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """main(["--days", "30", "--seed", "7"]) devuelve 0."""
    exit_code = main(["--days", "30", "--seed", "7"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "30" in captured.out
    assert "7" in captured.out


def test_cli_params_yaml_changes_result(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    """--params con YAML válido (lam: 0.7) cambia el resultado respecto al
    default. Se compara indirectamente vía dos runs equivalentes a lo que
    hace la CLI, con y sin el override."""
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
    """Llave inválida en el YAML de --params produce exit code 2."""
    params_path = tmp_path / "bad_params.yaml"
    params_path.write_text(yaml.safe_dump({"not_a_real_field": 1.0}))

    exit_code = main(["--days", "10", "--seed", "1", "--params", str(params_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert captured.err != ""


# ---------------------------------------------------------------------------
# Extras: default persona/shocks, robustez de composición con la firma pública


def test_run_default_persona_and_no_shocks() -> None:
    """run sin persona ni shocks explícitos usa PersonaParams() y no falla."""
    result = run(15, seed=1, variant=MoodVariant.DECOUPLED)
    assert len(result.records) == 15
    assert result.params == PersonaParams()


def test_run_cycle_day_and_phase_are_entry_state_not_next() -> None:
    """cycle_day/phase_label del record del día t deben corresponder al estado
    de ENTRADA a cycle.step (antes de avanzar), no al cycle_next."""
    from engine import cycle as cycle_mod
    from engine import rng as rng_mod
    from engine.types import MoodState

    seed = 321
    persona = PersonaParams()
    cycle_state = cycle_mod.init_state(persona, rng_mod.init_rng(seed))

    result = run(5, seed=seed, variant=MoodVariant.DECOUPLED_OFFSETS, persona=persona)

    # Recalculamos a mano el cycle_day de entrada para t=0 y lo comparamos.
    assert result.records[0].cycle_day == pytest.approx(cycle_state.cycle_day)
