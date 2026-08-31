"""Dos lecturas adicionales sobre la galeria de engine_simulation.py.

PROPIEDAD: este archivo + las figuras `engine_simulation/10_*.png` y
`11_*.png` EN LA RAIZ del proyecto. No modifica engine_simulation.py ni
borra nada del README existente (solo le anade una seccion al final).

Motivacion: con B=0.15 (default de PersonaParams) el ciclo hormonal mueve
el animo real N*p(t) solo +/-0.36 pasos (sensibilidad ~ N*p*(1-p) ~ 2.4
pasos/logit), contra un ruido de muestreo binomial de sd ~ 1.55 pasos:
invisible mirando solo los puntos M(t) del dado diario. Este script ofrece
dos lecturas:

    (A) 10_barrido_B.png     — barrido de B para ver a partir de que
                               amplitud el ciclo se vuelve visible en
                               N*p(t) y en la media movil de M(t).
    (B) 11_lectura_suavizada.png — la misma galeria de 6 escenarios de
                               engine_simulation.py, pero releida mirando
                               el estado N*p(t) y MA7(M) en vez del dado
                               diario M(t).

30 dias, semilla 3001, variante DECOUPLED_OFFSETS, persona base =
PersonaParams() (defaults de Fase 1: rho_e=0.7, sigma_e=0.45 — no se
tocan, solo se sobreescriben campos puntuales via dataclasses.replace).

Reproducir:
    wsl.exe -d Ubuntu -- bash -lc \
        'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && \
         MPLBACKEND=Agg .venv/bin/python -m experiments.engine_simulation_lecturas'
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from engine.types import MoodVariant, PersonaParams, SimResult
from sim.run_daily import run

# Experiment constants

DAYS = 30
SEED = 3001
VARIANT = MoodVariant.DECOUPLED_OFFSETS
OUT_DIR = Path(__file__).resolve().parent.parent / "engine_simulation"

_BASE_PERSONA = PersonaParams()

# Negative streak window (days, inclusive) for scenario 04: 10..14.
STREAK_DAYS = range(10, 15)
STREAK_SCORE = -1.0
SHOCKS_STREAK: dict[int, float] = {t: STREAK_SCORE for t in STREAK_DAYS}

# Local theoretical sensitivity N*p*(1-p) at p=lam=0.6, N=10: ~2.4 steps/logit.
_SENSITIVITY = _BASE_PERSONA.N * 0.6 * (1 - 0.6)

# Reference binomial sd at p=lam=0.6, N=10: ~1.55 steps.
_NOISE_SD = np.sqrt(_BASE_PERSONA.N * 0.6 * (1 - 0.6))

MA_WINDOW = 7


def build_persona(overrides: dict[str, float]) -> PersonaParams:
    """Persona base con `overrides` aplicados via dataclasses.replace."""
    return dataclasses.replace(_BASE_PERSONA, **overrides)


def run_scenario(overrides: dict[str, float], shocks: dict[int, float] | None) -> SimResult:
    """Corre DAYS dias con SEED compartida, variante fija y la persona del escenario."""
    persona = build_persona(overrides)
    return run(days=DAYS, seed=SEED, variant=VARIANT, persona=persona, shocks=shocks)


def moving_average_backward(values: np.ndarray, window: int) -> np.ndarray:
    """Media movil hacia atras de `window` dias; NaN mientras no hay ventana completa."""
    n = len(values)
    ma = np.full(n, np.nan, dtype=float)
    for i in range(window - 1, n):
        ma[i] = float(np.mean(values[i - window + 1 : i + 1]))
    return ma


# Figure A — 10_barrido_B.png


B_SWEEP = (0.15, 0.30, 0.50, 0.65)


def plot_barrido_b() -> Path:
    """4 paneles apilados, uno por B, mostrando M(t), N*p(t)+banda y MA7(M)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    N = _BASE_PERSONA.N
    fig, axes = plt.subplots(
        len(B_SWEEP), 1, figsize=(10, 12), sharex=True, sharey=True, dpi=120
    )

    for i, (ax, b_val) in enumerate(zip(axes, B_SWEEP)):
        result = run_scenario({"B": b_val}, None)
        t = result.t
        M = result.M
        p = result.p
        Np = N * p
        sd_binom = np.sqrt(Np * (1.0 - p))
        ma7 = moving_average_backward(M, MA_WINDOW)

        ax.plot(
            t, M, "o", color="gray", alpha=0.35, markersize=4,
            label="M(t) — dado diario",
        )
        ax.plot(t, Np, "-", color="C0", linewidth=2.4, label="N·p(t) — ánimo real")
        ax.fill_between(
            t, Np - sd_binom, Np + sd_binom, color="C0", alpha=0.15,
            label="N·p(t) ± σ_binom",
        )
        ax.plot(
            t, ma7, "--", color="C1", linewidth=1.8,
            label=f"MA{MA_WINDOW}(M) — media móvil {MA_WINDOW}d",
        )

        amplitud = _SENSITIVITY * b_val
        ax.set_title(
            f"B={b_val} — amplitud teórica ≈ {amplitud:.2f} pasos "
            f"(ruido diario sd≈{_NOISE_SD:.2f})",
            fontsize=10,
        )
        ax.set_ylabel(f"M (0..{N})")
        ax.set_ylim([-0.5, N + 0.5])
        ax.grid(True, alpha=0.3)
        if i == 0:
            ax.legend(loc="upper right", fontsize=8)

    axes[-1].set_xlabel("Día")
    fig.suptitle(f"¿Cuánto B hace visible el ciclo? — 30 días · seed {SEED}")
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.96))

    png_path = OUT_DIR / "10_barrido_B.png"
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path


# Figure B — 11_lectura_suavizada.png (gallery re-read)

ScenarioSpec = tuple[str, str, dict[str, float], dict[int, float] | None]

SCENARIOS: list[ScenarioSpec] = [
    ("01_baseline", "baseline", {}, None),
    ("02_solo_ciclo", "solo ciclo hormonal", {"sigma_e": 0.0, "k": 0.0}, None),
    (
        "03_solo_endogeno",
        "solo rachas endogenas",
        {"B": 0.0, "A": 0.0, "sigma_eps": 0.0, "k": 0.0},
        None,
    ),
    ("04_racha_negativa", "racha negativa (shocks 10-14)", {}, SHOCKS_STREAK),
    ("05_alta_volatilidad", "alta volatilidad (nu=4.0)", {"nu": 4.0}, None),
    ("06_ciclo_fuerte", "ciclo fuerte (A=0.4, B=0.3)", {"A": 0.4, "B": 0.3}, None),
]


def plot_lectura_suavizada() -> Path:
    """2x3 small multiples: N*p(t) + MA7(M) sobre M(t), para los 6 escenarios."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    N = _BASE_PERSONA.N
    fig, axes = plt.subplots(2, 3, figsize=(15, 8), sharex=True, sharey=True, dpi=120)

    for ax, (slug, _label, overrides, shocks) in zip(axes.flat, SCENARIOS):
        result = run_scenario(overrides, shocks)
        t = result.t
        M = result.M
        p = result.p
        Np = N * p
        ma7 = moving_average_backward(M, MA_WINDOW)

        ax.plot(t, M, "o", color="gray", alpha=0.3, markersize=3.5, label="M(t)")
        ax.plot(t, Np, "-", color="C0", linewidth=2.2, label="N·p(t)")
        ax.plot(t, ma7, "--", color="C1", linewidth=1.6, label=f"MA{MA_WINDOW}(M)")

        if slug == "04_racha_negativa":
            ax.axvspan(
                min(STREAK_DAYS),
                max(STREAK_DAYS) + 1,
                color="red",
                alpha=0.12,
                label=f"racha (días {min(STREAK_DAYS)}-{max(STREAK_DAYS)})",
            )

        ax.set_title(slug, fontsize=10)
        ax.set_ylim([-0.5, N + 0.5])
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=7)

    for ax in axes[-1, :]:
        ax.set_xlabel("Día")
    for ax in axes[:, 0]:
        ax.set_ylabel(f"M (0..{N})")

    fig.suptitle(
        "Galería releída: el ánimo real N·p(t) y la media móvil, "
        f"sobre el dado diario — seed {SEED}"
    )
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.95))

    png_path = OUT_DIR / "11_lectura_suavizada.png"
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path


# README.md: append a "Lecturas adicionales" section at the end


def append_readme_section() -> Path:
    readme_path = OUT_DIR / "README.md"
    existing = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""

    section_lines: list[str] = []
    if existing and not existing.endswith("\n"):
        section_lines.append("")
    section_lines.append("")
    section_lines.append("## Lecturas adicionales")
    section_lines.append("")
    section_lines.append(
        f"Con B={_BASE_PERSONA.B} (default) el ciclo hormonal mueve el ánimo real "
        f"N·p(t) solo ≈{_SENSITIVITY * _BASE_PERSONA.B:.2f} pasos (sensibilidad local "
        f"N·p·(1−p)≈{_SENSITIVITY:.1f} pasos/logit) contra un ruido de muestreo binomial "
        f"de sd≈{_NOISE_SD:.2f} pasos: invisible mirando solo los puntos M(t) del dado "
        "diario. Estas dos figuras separan la señal del ruido de muestreo."
    )
    section_lines.append("")
    section_lines.append(
        "| Figura | Qué muestra | Cómo leerla |"
    )
    section_lines.append("|---|---|---|")
    section_lines.append(
        "| `10_barrido_B.png` | 4 paneles (B ∈ {0.15, 0.30, 0.50, 0.65}, resto de la "
        "persona = defaults): M(t) (dado diario, gris), N·p(t) ± σ_binom (ánimo real, "
        "azul) y MA7(M) (media móvil 7 días, naranja discontinua) | Compara la amplitud "
        "teórica del título de cada panel (≈2.4·B pasos) contra el ruido de muestreo "
        "sd≈1.55 pasos: recién con B≈0.5–0.65 la onda se distingue a simple vista en "
        "N·p(t) y, más suavizada aún, en MA7(M) |"
    )
    section_lines.append(
        "| `11_lectura_suavizada.png` | Los mismos 6 escenarios de la galería principal, "
        "pero releídos con N·p(t) (ánimo real) y MA7(M) (media móvil) superpuestos sobre "
        "M(t) (dado diario, gris) | Compara qué sobrevive al promediar: en `02_solo_ciclo` "
        "y `06_ciclo_fuerte` la onda hormonal emerge con claridad en N·p(t); en "
        "`04_racha_negativa` la caída y recuperación de la racha se ve mucho más nítida en "
        "MA7(M) que en el M(t) crudo; en `05_alta_volatilidad` el suavizado reduce el "
        "aspecto errático pero no cambia la tendencia central |"
    )
    section_lines.append("")
    section_lines.append("### Regenerar")
    section_lines.append("")
    section_lines.append("```powershell")
    section_lines.append(
        "wsl.exe -d Ubuntu -- bash -lc "
        "'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && "
        "MPLBACKEND=Agg .venv/bin/python -m experiments.engine_simulation_lecturas'"
    )
    section_lines.append("```")
    section_lines.append("")

    readme_path.write_text(existing + "\n".join(section_lines), encoding="utf-8")
    return readme_path


# Orchestration


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    barrido_path = plot_barrido_b()
    print(f"escrito: {barrido_path}")

    lectura_path = plot_lectura_suavizada()
    print(f"escrito: {lectura_path}")

    readme_path = append_readme_section()
    print(f"escrito: {readme_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
