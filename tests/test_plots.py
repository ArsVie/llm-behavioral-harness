"""Acceptance tests for sim/plots.py (W1.6).

One smoke test per plot: it writes a non-trivial PNG under a directory it has
to create, the filename is deterministic and follows the plot's pattern, a
second call returns the same path, and no matplotlib figure is left open.
"""
from __future__ import annotations

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

SEED = 777


def _synthetic(variant: MoodVariant, days: int = 60, n: int = 10) -> SimResult:
    """A 60-day SimResult with seeded-random M and sinusoidal m/g/mu/eta."""
    rng = np.random.default_rng(SEED)
    records = []
    for t in range(days):
        m = 0.2 * np.sin(2 * np.pi * t / 28.0)
        g = 1.0 + 0.25 * np.cos(2 * np.pi * t / 28.0)
        arg = np.log(0.6 / (1 - 0.6)) + m + g * 0.15
        records.append(DayRecord(
            t=t,
            m=float(m),
            g=float(g),
            arg=float(arg),
            p=float(1.0 / (1.0 + np.exp(-arg))),
            M=int(rng.uniform(0, n + 1)),
            score=0.0,
            mu=float(0.1 * np.sin(2 * np.pi * t / 28.0 + 1.0)),
            eta=float(0.05 * np.cos(2 * np.pi * t / 28.0 + 2.0)),
            cycle_day=float(t % 28),
            phase_label="follicular" if t % 2 == 0 else "luteal_early",
            seed=SEED,
        ))
    return SimResult(params=PersonaParams(N=n), variant=variant, records=records)


def _times_h() -> np.ndarray:
    return np.random.default_rng(SEED).uniform(0, 24 * 10, 100)


def _step_envelope(h: float) -> float:
    """1 during the day, 0.5 at night."""
    return 1.0 if 8 <= h % 24 < 22 else 0.5


#: (id, plot call taking out_dir, expected filename)
_PLOTS = [
    ("mood_series",
     lambda d: plot_mood_series(_synthetic(MoodVariant.DECOUPLED_OFFSETS), d),
     "mood_series_decoupled_offsets_s777.png"),
    ("mg",
     lambda d: plot_mg(_synthetic(MoodVariant.ORIGINAL), d),
     "mg_original_s777.png"),
    ("mood_hist",
     lambda d: plot_mood_hist(_synthetic(MoodVariant.DECOUPLED), d),
     "mood_hist_decoupled_s777.png"),
    ("mu_eta",
     lambda d: plot_mu_eta(_synthetic(MoodVariant.ORIGINAL), d),
     "mu_eta_original_s777.png"),
    ("hourly_events",
     lambda d: plot_hourly_events(_times_h(), envelope_fn=None, out_dir=d,
                                  tag="mytest_s777"),
     "hourly_events_mytest_s777.png"),
    ("hourly_events_envelope",
     lambda d: plot_hourly_events(_times_h(), envelope_fn=_step_envelope,
                                  out_dir=d, tag="env_test_s777"),
     "hourly_events_env_test_s777.png"),
    ("variant_comparison",
     lambda d: plot_variant_comparison(
         {"ORIGINAL": _synthetic(MoodVariant.ORIGINAL),
          "DECOUPLED": _synthetic(MoodVariant.DECOUPLED_OFFSETS)},
         d, "comparison_s777"),
     "variants_comparison_s777.png"),
]


@pytest.mark.parametrize("plot, expected_name", [p[1:] for p in _PLOTS],
                         ids=[p[0] for p in _PLOTS])
def test_plot_writes_a_deterministic_png_and_closes_its_figure(
        tmp_path, plot, expected_name):
    out_dir = tmp_path / "plots" / "nested"
    assert not out_dir.exists()

    path = plot(out_dir)

    assert out_dir.is_dir()  # created on demand
    assert path.name == expected_name
    assert path.exists() and path.stat().st_size > 1000
    assert len(plt.get_fignums()) == 0
    assert plot(out_dir) == path  # same input, same path
