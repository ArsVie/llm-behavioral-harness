"""Tests de sim/run_events.py (W2.2 — Ola 2).

PROPIEDAD: tarea W2.2 (sim/run_events.py + este archivo). Usa los defaults de
PersonaParams/TimingParams (fixtures `persona`/`timing` de tests/conftest.py,
CONGELADO) salvo cuando el propio test necesita variarlos explícitamente.
"""
from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from engine import circadian
from engine.types import PersonaParams, TimingParams
from sim import run_events


# ---------------------------------------------------------------------------
# 1. Sanity: corre, array ordenado, rango horario válido, tasa diaria sana.


def test_run_smoke_sorted_in_range_sane_rate():
    days = 90
    events = run_events.run(days, seed=123)

    assert isinstance(events, np.ndarray)
    assert events.ndim == 1
    # Ordenado (no decreciente; de hecho estrictamente creciente por min_gap).
    assert np.all(np.diff(events) >= 0.0)
    # Rango horario absoluto válido.
    assert np.all(events >= 0.0)
    assert np.all(events < days * 24.0)

    daily_rate = len(events) / days
    assert 0.3 <= daily_rate <= 3.0


# ---------------------------------------------------------------------------
# 2. Determinismo.


def test_run_deterministic_same_seed():
    a = run_events.run(90, seed=123)
    b = run_events.run(90, seed=123)
    assert np.array_equal(a, b)


def test_run_deterministic_with_scores_and_differs_from_no_scores():
    days = 90
    scores = np.linspace(-1.0, 1.0, days)

    with_scores_a = run_events.run(days, seed=123, scores=scores)
    with_scores_b = run_events.run(days, seed=123, scores=scores)
    assert np.array_equal(with_scores_a, with_scores_b)

    without_scores = run_events.run(days, seed=123)
    assert not np.array_equal(with_scores_a, without_scores)


# ---------------------------------------------------------------------------
# 3. Guards, verificados sobre dos semillas.


@pytest.mark.parametrize("seed", [123, 456])
def test_guards_on_default_run(seed: int):
    timing = TimingParams()
    events = run_events.run(90, seed=seed)

    assert len(events) > 0, "la corrida debe producir al menos un evento"

    min_gap_h = timing.min_gap_min / 60.0

    # Gap mínimo entre eventos consecutivos (aceptados, que es todo `events`).
    gaps = np.diff(events)
    assert np.all(gaps >= min_gap_h - 1e-9)

    # Tope diario.
    days_of_events = (events // 24.0).astype(int)
    _, counts = np.unique(days_of_events, return_counts=True)
    assert np.all(counts <= timing.daily_cap)

    # Cero eventos en quiet hours: envolvente >= 1e-9 en todo evento aceptado.
    for t_h in events:
        assert circadian.envelope(float(t_h) % 24.0, timing) >= 1e-9

    # Silencio máximo entre aceptados consecutivos (con margen para el
    # desplazamiento del forzado hasta una ventana despierta).
    assert np.all(gaps <= timing.max_gap_h + 12.0)


# ---------------------------------------------------------------------------
# 4. daily_cap se ejercita de verdad con tasa alta.


def test_daily_cap_is_exercised_with_high_rate():
    timing = TimingParams(theta_h=2.0)
    days = 90
    events = run_events.run(days, seed=123, timing=timing)

    days_of_events = (events // 24.0).astype(int)
    _, counts = np.unique(days_of_events, return_counts=True)

    assert np.any(counts == timing.daily_cap)
    assert np.all(counts <= timing.daily_cap)


# ---------------------------------------------------------------------------
# 5. max_gap se ejercita con tasa bajísima.


def test_max_gap_is_exercised_with_low_rate():
    timing = TimingParams(theta_h=2000.0)
    days = 10
    events = run_events.run(days, seed=123, timing=timing)

    assert len(events) > 0, "el guard max_gap debe forzar al menos un contacto"

    # Gap desde el inicio de la simulación (t_last=0) hasta el primer evento,
    # y entre eventos consecutivos: todos acotados por max_gap_h + margen.
    gaps = np.diff(np.concatenate(([0.0], events)))
    assert np.all(gaps <= timing.max_gap_h + 12.0)


# ---------------------------------------------------------------------------
# 6. adj_from_score: valores ancla.


def test_adj_from_score_anchors_default_bounds():
    timing = TimingParams()
    assert run_events.adj_from_score(1.0, timing) == pytest.approx(1.3)
    assert run_events.adj_from_score(-1.0, timing) == pytest.approx(0.7)
    assert run_events.adj_from_score(0.0, timing) == pytest.approx(1.0)
    assert run_events.adj_from_score(None, timing) == pytest.approx(1.0)


def test_adj_from_score_respects_custom_bounds():
    timing = TimingParams(adj_bounds=(0.9, 1.1))
    assert run_events.adj_from_score(1.0, timing) == pytest.approx(1.1)


# ---------------------------------------------------------------------------
# 7. Efecto de fase medible en el stream (no solo en la fórmula del modulador).


def test_phase_multipliers_affect_the_stream():
    days = 90
    seed = 123
    timing_default = TimingParams()
    timing_flat = TimingParams(
        phase_multipliers={label: 1.0 for label in timing_default.phase_multipliers}
    )

    events_default = run_events.run(days, seed=seed, timing=timing_default)
    events_flat = run_events.run(days, seed=seed, timing=timing_flat)

    assert not np.array_equal(events_default, events_flat)


# ---------------------------------------------------------------------------
# 8. main(): smoke CLI.


def test_main_smoke(capsys):
    exit_code = run_events.main(["--days", "20", "--seed", "5"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "eventos" in captured.out
    assert "tasa" in captured.out


# ---------------------------------------------------------------------------
# Extras: cobertura directa de los helpers de composición (no exigidos por el
# enunciado pero baratos y útiles para aislar fallos).


def test_run_accepts_explicit_persona_and_timing_defaults():
    persona = PersonaParams()
    timing = TimingParams()
    events = run_events.run(30, seed=7, persona=persona, timing=timing)
    assert isinstance(events, np.ndarray)
    assert np.all(events >= 0.0)
    assert np.all(events < 30 * 24.0)


def test_run_with_theta_replace_matches_dataclasses_replace_pattern():
    # El enunciado sugiere dataclasses.replace(timing, theta_h=2.0); confirma
    # que run() acepta ese patrón sin errores y sigue siendo determinista.
    base_timing = TimingParams()
    high_rate_timing = dataclasses.replace(base_timing, theta_h=2.0)

    a = run_events.run(30, seed=42, timing=high_rate_timing)
    b = run_events.run(30, seed=42, timing=high_rate_timing)
    assert np.array_equal(a, b)
