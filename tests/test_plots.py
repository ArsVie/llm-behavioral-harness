"""Tests de aceptación para sim/plots.py (W1.6).

Smoke tests, nombres deterministas, y cierre de figuras.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pytest

from engine.types import DayRecord, MoodVariant, PersonaParams, SimResult
from sim.plots import (
    plot_hourly_events,
    plot_mg,
    plot_mood_hist,
    plot_mood_series,
    plot_mu_eta,
    plot_variant_comparison,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def synthetic_result_decoupled_offsets() -> SimResult:
    """SimResult sintético con 60 días, M aleatorio-fijo, m/g senoidales, seed=777."""
    np.random.seed(777)
    rng = np.random.default_rng(777)

    days = 60
    N = 10
    seed = 777

    records = []
    for t in range(days):
        # M aleatorio dentro [0, N]
        M = int(rng.uniform(0, N + 1))

        # m(t) y g(t) senoidales
        m = 0.2 * np.sin(2 * np.pi * t / 28.0)
        g = 1.0 + 0.25 * np.cos(2 * np.pi * t / 28.0)

        # arg y p calculados
        arg = np.log(0.6 / (1 - 0.6)) + m + g * 0.15
        p = 1.0 / (1.0 + np.exp(-arg))

        # mu y eta simulados
        mu = 0.1 * np.sin(2 * np.pi * t / 28.0 + 1.0)
        eta = 0.05 * np.cos(2 * np.pi * t / 28.0 + 2.0)

        # phase_label alternante
        phase_label = "follicular" if t % 2 == 0 else "luteal_early"

        record = DayRecord(
            t=t,
            m=float(m),
            g=float(g),
            arg=float(arg),
            p=float(p),
            M=M,
            score=0.0,
            mu=float(mu),
            eta=float(eta),
            cycle_day=float(t % 28),
            phase_label=phase_label,
            seed=seed,
        )
        records.append(record)

    return SimResult(
        params=PersonaParams(N=N),
        variant=MoodVariant.DECOUPLED_OFFSETS,
        records=records,
    )


@pytest.fixture
def synthetic_result_original() -> SimResult:
    """SimResult sintético con variante ORIGINAL."""
    np.random.seed(777)
    rng = np.random.default_rng(777)

    days = 60
    N = 10
    seed = 777

    records = []
    for t in range(days):
        M = int(rng.uniform(0, N + 1))
        m = 0.2 * np.sin(2 * np.pi * t / 28.0)
        g = 1.0 + 0.25 * np.cos(2 * np.pi * t / 28.0)
        arg = np.log(0.6 / (1 - 0.6)) + m + g * 0.15
        p = 1.0 / (1.0 + np.exp(-arg))
        mu = 0.1 * np.sin(2 * np.pi * t / 28.0 + 1.0)
        eta = 0.05 * np.cos(2 * np.pi * t / 28.0 + 2.0)
        phase_label = "ovulatory" if t % 2 == 0 else "menstrual"

        record = DayRecord(
            t=t,
            m=float(m),
            g=float(g),
            arg=float(arg),
            p=float(p),
            M=M,
            score=0.0,
            mu=float(mu),
            eta=float(eta),
            cycle_day=float(t % 28),
            phase_label=phase_label,
            seed=seed,
        )
        records.append(record)

    return SimResult(
        params=PersonaParams(N=N),
        variant=MoodVariant.ORIGINAL,
        records=records,
    )


@pytest.fixture
def synthetic_result_decoupled() -> SimResult:
    """SimResult sintético con variante DECOUPLED."""
    np.random.seed(777)
    rng = np.random.default_rng(777)

    days = 60
    N = 10
    seed = 777

    records = []
    for t in range(days):
        M = int(rng.uniform(0, N + 1))
        m = 0.2 * np.sin(2 * np.pi * t / 28.0)
        g = 1.0 + 0.25 * np.cos(2 * np.pi * t / 28.0)
        arg = np.log(0.6 / (1 - 0.6)) + m + g * 0.15
        p = 1.0 / (1.0 + np.exp(-arg))
        mu = 0.1 * np.sin(2 * np.pi * t / 28.0 + 1.0)
        eta = 0.05 * np.cos(2 * np.pi * t / 28.0 + 2.0)
        phase_label = "menstrual" if t % 3 == 0 else ("follicular" if t % 3 == 1 else "ovulatory")

        record = DayRecord(
            t=t,
            m=float(m),
            g=float(g),
            arg=float(arg),
            p=float(p),
            M=M,
            score=0.0,
            mu=float(mu),
            eta=float(eta),
            cycle_day=float(t % 28),
            phase_label=phase_label,
            seed=seed,
        )
        records.append(record)

    return SimResult(
        params=PersonaParams(N=N),
        variant=MoodVariant.DECOUPLED,
        records=records,
    )


# ============================================================================
# Tests: Humo y nombres deterministas
# ============================================================================


class TestPlotMoodSeries:
    """Tests para plot_mood_series."""

    def test_smoke_creates_png(self, tmp_path, synthetic_result_decoupled_offsets):
        """Genera PNG sin error."""
        result_path = plot_mood_series(synthetic_result_decoupled_offsets, tmp_path)
        assert result_path.exists()
        assert result_path.suffix == ".png"
        assert result_path.stat().st_size > 1000

    def test_deterministic_filename(self, tmp_path, synthetic_result_decoupled_offsets):
        """Mismo input = misma ruta."""
        path1 = plot_mood_series(synthetic_result_decoupled_offsets, tmp_path)
        # Recrear para segundo llamado (limpiamos fig pero path debe ser igual)
        path2 = plot_mood_series(synthetic_result_decoupled_offsets, tmp_path)
        assert path1 == path2

    def test_filename_pattern(self, tmp_path, synthetic_result_decoupled_offsets):
        """Nombre sigue patrón mood_series_{variant}_s{seed}.png."""
        result_path = plot_mood_series(synthetic_result_decoupled_offsets, tmp_path)
        expected_name = "mood_series_decoupled_offsets_s777.png"
        assert result_path.name == expected_name

    def test_no_figures_open(self, tmp_path, synthetic_result_decoupled_offsets):
        """Sin figuras abiertas tras la llamada."""
        plot_mood_series(synthetic_result_decoupled_offsets, tmp_path)
        assert len(plt.get_fignums()) == 0


class TestPlotMg:
    """Tests para plot_mg."""

    def test_smoke_creates_png(self, tmp_path, synthetic_result_original):
        """Genera PNG sin error."""
        result_path = plot_mg(synthetic_result_original, tmp_path)
        assert result_path.exists()
        assert result_path.suffix == ".png"
        assert result_path.stat().st_size > 1000

    def test_deterministic_filename(self, tmp_path, synthetic_result_original):
        """Mismo input = misma ruta."""
        path1 = plot_mg(synthetic_result_original, tmp_path)
        path2 = plot_mg(synthetic_result_original, tmp_path)
        assert path1 == path2

    def test_filename_pattern(self, tmp_path, synthetic_result_original):
        """Nombre sigue patrón mg_{variant}_s{seed}.png."""
        result_path = plot_mg(synthetic_result_original, tmp_path)
        expected_name = "mg_original_s777.png"
        assert result_path.name == expected_name

    def test_no_figures_open(self, tmp_path, synthetic_result_original):
        """Sin figuras abiertas tras la llamada."""
        plot_mg(synthetic_result_original, tmp_path)
        assert len(plt.get_fignums()) == 0


class TestPlotMoodHist:
    """Tests para plot_mood_hist."""

    def test_smoke_creates_png(self, tmp_path, synthetic_result_decoupled_offsets):
        """Genera PNG sin error."""
        result_path = plot_mood_hist(synthetic_result_decoupled_offsets, tmp_path)
        assert result_path.exists()
        assert result_path.suffix == ".png"
        assert result_path.stat().st_size > 1000

    def test_deterministic_filename(self, tmp_path, synthetic_result_decoupled_offsets):
        """Mismo input = misma ruta."""
        path1 = plot_mood_hist(synthetic_result_decoupled_offsets, tmp_path)
        path2 = plot_mood_hist(synthetic_result_decoupled_offsets, tmp_path)
        assert path1 == path2

    def test_filename_pattern(self, tmp_path, synthetic_result_decoupled_offsets):
        """Nombre sigue patrón mood_hist_{variant}_s{seed}.png."""
        result_path = plot_mood_hist(synthetic_result_decoupled_offsets, tmp_path)
        expected_name = "mood_hist_decoupled_offsets_s777.png"
        assert result_path.name == expected_name

    def test_no_figures_open(self, tmp_path, synthetic_result_decoupled_offsets):
        """Sin figuras abiertas tras la llamada."""
        plot_mood_hist(synthetic_result_decoupled_offsets, tmp_path)
        assert len(plt.get_fignums()) == 0


class TestPlotMuEta:
    """Tests para plot_mu_eta."""

    def test_smoke_creates_png(self, tmp_path, synthetic_result_original):
        """Genera PNG sin error."""
        result_path = plot_mu_eta(synthetic_result_original, tmp_path)
        assert result_path.exists()
        assert result_path.suffix == ".png"
        assert result_path.stat().st_size > 1000

    def test_deterministic_filename(self, tmp_path, synthetic_result_original):
        """Mismo input = misma ruta."""
        path1 = plot_mu_eta(synthetic_result_original, tmp_path)
        path2 = plot_mu_eta(synthetic_result_original, tmp_path)
        assert path1 == path2

    def test_filename_pattern(self, tmp_path, synthetic_result_original):
        """Nombre sigue patrón mu_eta_{variant}_s{seed}.png."""
        result_path = plot_mu_eta(synthetic_result_original, tmp_path)
        expected_name = "mu_eta_original_s777.png"
        assert result_path.name == expected_name

    def test_no_figures_open(self, tmp_path, synthetic_result_original):
        """Sin figuras abiertas tras la llamada."""
        plot_mu_eta(synthetic_result_original, tmp_path)
        assert len(plt.get_fignums()) == 0


class TestPlotHourlyEvents:
    """Tests para plot_hourly_events."""

    def test_smoke_no_envelope(self, tmp_path):
        """Genera PNG sin error (sin envolvente)."""
        times_h = np.random.default_rng(777).uniform(0, 24 * 10, 100)
        result_path = plot_hourly_events(
            times_h, envelope_fn=None, out_dir=tmp_path, tag="test_s777"
        )
        assert result_path.exists()
        assert result_path.suffix == ".png"
        assert result_path.stat().st_size > 1000

    def test_smoke_with_envelope(self, tmp_path):
        """Genera PNG sin error (con envolvente)."""
        times_h = np.random.default_rng(777).uniform(0, 24 * 10, 100)

        def step_envelope(h: float) -> float:
            """Envolvente escalón: 1 durante el día, 0 por noche."""
            hour_local = h % 24
            return 1.0 if 8 <= hour_local < 22 else 0.5

        result_path = plot_hourly_events(
            times_h, envelope_fn=step_envelope, out_dir=tmp_path, tag="test_envelope_s777"
        )
        assert result_path.exists()
        assert result_path.suffix == ".png"
        assert result_path.stat().st_size > 1000

    def test_filename_pattern_no_envelope(self, tmp_path):
        """Nombre sigue patrón hourly_events_{tag}.png (sin envolvente)."""
        times_h = np.random.default_rng(777).uniform(0, 24 * 10, 100)
        result_path = plot_hourly_events(
            times_h, envelope_fn=None, out_dir=tmp_path, tag="mytest_s777"
        )
        expected_name = "hourly_events_mytest_s777.png"
        assert result_path.name == expected_name

    def test_filename_pattern_with_envelope(self, tmp_path):
        """Nombre sigue patrón hourly_events_{tag}.png (con envolvente)."""
        times_h = np.random.default_rng(777).uniform(0, 24 * 10, 100)

        def dummy_envelope(h: float) -> float:
            return 1.0

        result_path = plot_hourly_events(
            times_h, envelope_fn=dummy_envelope, out_dir=tmp_path, tag="env_test_s777"
        )
        expected_name = "hourly_events_env_test_s777.png"
        assert result_path.name == expected_name

    def test_deterministic_filename(self, tmp_path):
        """Mismo input = misma ruta."""
        times_h = np.array([1.5, 2.3, 14.7, 23.1, 8.9])
        path1 = plot_hourly_events(times_h, envelope_fn=None, out_dir=tmp_path, tag="fixed_s999")
        path2 = plot_hourly_events(times_h, envelope_fn=None, out_dir=tmp_path, tag="fixed_s999")
        assert path1 == path2

    def test_no_figures_open_no_envelope(self, tmp_path):
        """Sin figuras abiertas tras la llamada (sin envolvente)."""
        times_h = np.random.default_rng(777).uniform(0, 24 * 10, 50)
        plot_hourly_events(times_h, envelope_fn=None, out_dir=tmp_path, tag="test_s777")
        assert len(plt.get_fignums()) == 0

    def test_no_figures_open_with_envelope(self, tmp_path):
        """Sin figuras abiertas tras la llamada (con envolvente)."""
        times_h = np.random.default_rng(777).uniform(0, 24 * 10, 50)

        def dummy_envelope(h: float) -> float:
            return 1.0

        plot_hourly_events(
            times_h, envelope_fn=dummy_envelope, out_dir=tmp_path, tag="test_env_s777"
        )
        assert len(plt.get_fignums()) == 0


class TestPlotVariantComparison:
    """Tests para plot_variant_comparison."""

    def test_smoke_creates_png(self, tmp_path, synthetic_result_original, synthetic_result_decoupled_offsets):
        """Genera PNG sin error."""
        results = {
            "ORIGINAL": synthetic_result_original,
            "DECOUPLED": synthetic_result_decoupled_offsets,
        }
        result_path = plot_variant_comparison(results, tmp_path, "test_s777")
        assert result_path.exists()
        assert result_path.suffix == ".png"
        assert result_path.stat().st_size > 1000

    def test_filename_pattern(self, tmp_path, synthetic_result_original, synthetic_result_decoupled_offsets):
        """Nombre sigue patrón variants_{tag}.png."""
        results = {
            "VAR1": synthetic_result_original,
            "VAR2": synthetic_result_decoupled_offsets,
        }
        result_path = plot_variant_comparison(results, tmp_path, "comparison_s777")
        expected_name = "variants_comparison_s777.png"
        assert result_path.name == expected_name

    def test_deterministic_filename(self, tmp_path, synthetic_result_original, synthetic_result_decoupled_offsets):
        """Mismo input = misma ruta."""
        results = {
            "A": synthetic_result_original,
            "B": synthetic_result_decoupled_offsets,
        }
        path1 = plot_variant_comparison(results, tmp_path, "fixed_s888")
        path2 = plot_variant_comparison(results, tmp_path, "fixed_s888")
        assert path1 == path2

    def test_no_figures_open(self, tmp_path, synthetic_result_original, synthetic_result_decoupled_offsets):
        """Sin figuras abiertas tras la llamada."""
        results = {
            "ORIG": synthetic_result_original,
            "DECOUP": synthetic_result_decoupled_offsets,
        }
        plot_variant_comparison(results, tmp_path, "test_s777")
        assert len(plt.get_fignums()) == 0


# ============================================================================
# Tests: Creación de out_dir
# ============================================================================


class TestOutDirCreation:
    """Verificar que out_dir se crea si no existe."""

    def test_plot_mood_series_creates_dir(self, tmp_path, synthetic_result_decoupled):
        """plot_mood_series crea out_dir si no existe."""
        nested_dir = tmp_path / "plots" / "nested" / "mood"
        assert not nested_dir.exists()
        plot_mood_series(synthetic_result_decoupled, nested_dir)
        assert nested_dir.exists()

    def test_plot_mg_creates_dir(self, tmp_path, synthetic_result_original):
        """plot_mg crea out_dir si no existe."""
        nested_dir = tmp_path / "plots" / "nested" / "mg"
        assert not nested_dir.exists()
        plot_mg(synthetic_result_original, nested_dir)
        assert nested_dir.exists()

    def test_plot_mood_hist_creates_dir(self, tmp_path, synthetic_result_decoupled_offsets):
        """plot_mood_hist crea out_dir si no existe."""
        nested_dir = tmp_path / "plots" / "hist"
        assert not nested_dir.exists()
        plot_mood_hist(synthetic_result_decoupled_offsets, nested_dir)
        assert nested_dir.exists()

    def test_plot_mu_eta_creates_dir(self, tmp_path, synthetic_result_original):
        """plot_mu_eta crea out_dir si no existe."""
        nested_dir = tmp_path / "plots" / "mu_eta"
        assert not nested_dir.exists()
        plot_mu_eta(synthetic_result_original, nested_dir)
        assert nested_dir.exists()

    def test_plot_hourly_events_creates_dir(self, tmp_path):
        """plot_hourly_events crea out_dir si no existe."""
        nested_dir = tmp_path / "plots" / "hourly"
        assert not nested_dir.exists()
        times_h = np.array([1.0, 5.0, 10.0])
        plot_hourly_events(times_h, envelope_fn=None, out_dir=nested_dir, tag="test_s777")
        assert nested_dir.exists()

    def test_plot_variant_comparison_creates_dir(self, tmp_path, synthetic_result_original, synthetic_result_decoupled_offsets):
        """plot_variant_comparison crea out_dir si no existe."""
        nested_dir = tmp_path / "plots" / "variants"
        assert not nested_dir.exists()
        results = {"V1": synthetic_result_original, "V2": synthetic_result_decoupled_offsets}
        plot_variant_comparison(results, nested_dir, "test_s777")
        assert nested_dir.exists()
