"""Barrido de B promediado entre 30 semillas (aisla la onda del ruido).

PROPIEDAD: este archivo + la figura `engine_simulation/12_barrido_B_30seeds.png`
EN LA RAIZ del proyecto. No modifica engine_simulation.py ni
engine_simulation_lecturas.py; solo anade una linea a la seccion existente
"## Lecturas adicionales" del README.

Motivacion: `10_barrido_B.png` mostro el barrido de B con UNA sola semilla
(3001) — no se puede distinguir si la (in)visibilidad de la onda hormonal es
un efecto real de B o un artefacto de esa semilla en particular (ruido de
muestreo binomial + rachas endogenas de eta). Este script promedia M(t) y
N*p(t) entre 30 semillas (4001-4030) para cada B del barrido, aislando la
onda de ambas fuentes de variabilidad.

Nota de alineacion de fase: phi=0.0 en todas las semillas => los ciclos
arrancan alineados (cycle_day=0 el dia 0); L_0 (duracion del ciclo) varia un
poco por semilla ~ Normal(28, 1.5) => desfase leve creciente hacia el final
de la ventana de 30 dias. Por eso promediar entre semillas SI preserva la
onda (no la cancela): el desfase acumulado en 30 dias es pequeno frente al
periodo de ~28 dias.

30 dias, semillas 4001..4030 (30), variante DECOUPLED_OFFSETS, persona base
= PersonaParams() (defaults de Fase 1: rho_e=0.7, sigma_e=0.45 — no se
tocan, solo se sobreescribe B via dataclasses.replace).

Reproducir:
    wsl.exe -d Ubuntu -- bash -lc \
        'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && \
         MPLBACKEND=Agg .venv/bin/python -m experiments.engine_simulation_promedio'
"""
from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from engine.mood import logit, sigmoid
from engine.types import MoodVariant, PersonaParams, SimResult
from sim.run_daily import run

# Experiment constants

DAYS = 30
SEEDS = tuple(range(4001, 4031))  # 30 seeds.
VARIANT = MoodVariant.DECOUPLED_OFFSETS
OUT_DIR = Path(__file__).resolve().parent.parent / "engine_simulation"

_BASE_PERSONA = PersonaParams()

B_SWEEP = (0.15, 0.30, 0.50, 0.65)

_LAM_LOGIT = logit(_BASE_PERSONA.lam)  # logit(0.6).


def build_persona(b_val: float) -> PersonaParams:
    """Persona base con B=b_val via dataclasses.replace (resto = defaults)."""
    return dataclasses.replace(_BASE_PERSONA, B=b_val)


def run_scenario(b_val: float, seed: int) -> SimResult:
    """Corre DAYS dias con `seed`, variante fija y persona con B=b_val."""
    persona = build_persona(b_val)
    return run(days=DAYS, seed=seed, variant=VARIANT, persona=persona)


def theoretical_curve(b_val: float, t: np.ndarray, N: int) -> np.ndarray:
    """N·sigmoid(logit(lam) + b·sin(2π·t/28)) — onda pura sin η/μ/ruido."""
    arg = _LAM_LOGIT + b_val * np.sin(2.0 * math.pi * t / 28.0)
    p = np.array([sigmoid(a) for a in arg])
    return N * p


def collect_stats_for_b(b_val: float) -> dict[str, np.ndarray]:
    """Corre las 30 semillas para un B dado y devuelve medias/sd entre semillas."""
    N = _BASE_PERSONA.N
    M_runs = np.empty((len(SEEDS), DAYS))
    Np_runs = np.empty((len(SEEDS), DAYS))
    t = None

    for i, seed in enumerate(SEEDS):
        result = run_scenario(b_val, seed)
        if t is None:
            t = result.t
        M_runs[i, :] = result.M
        Np_runs[i, :] = N * result.p

    n_seeds = len(SEEDS)
    mean_M = M_runs.mean(axis=0)
    sd_M = M_runs.std(axis=0, ddof=1)
    sem_M = sd_M / math.sqrt(n_seeds)

    mean_Np = Np_runs.mean(axis=0)

    return {
        "t": t,
        "mean_M": mean_M,
        "sem_M": sem_M,
        "sd_M_mean": np.mean(sd_M),
        "mean_Np": mean_Np,
    }


# Figure: 12_barrido_B_30seeds.png.


def plot_barrido_b_30seeds() -> tuple[Path, dict[float, dict[str, float]]]:
    """4 paneles apilados, uno por B: media entre semillas de M(t) y N·p(t)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    N = _BASE_PERSONA.N
    fig, axes = plt.subplots(
        len(B_SWEEP), 1, figsize=(10, 12), sharex=True, dpi=120
    )

    summary: dict[float, dict[str, float]] = {}

    for i, (ax, b_val) in enumerate(zip(axes, B_SWEEP)):
        stats = collect_stats_for_b(b_val)
        t = stats["t"]
        mean_M = stats["mean_M"]
        sem_M = stats["sem_M"]
        mean_Np = stats["mean_Np"]
        sd_M_mean = stats["sd_M_mean"]

        theo = theoretical_curve(b_val, t, N)

        amplitud_mean_M = float(np.max(mean_M) - np.min(mean_M))
        corr = float(np.corrcoef(mean_M, theo)[0, 1])

        summary[b_val] = {
            "amplitud_mean_M": amplitud_mean_M,
            "corr_mean_M_theo": corr,
            "sd_between_seeds_mean": sd_M_mean,
        }

        ax.plot(
            t, mean_M, "-", color="C1", linewidth=2.2,
            label="media entre semillas de M(t)",
        )
        ax.fill_between(
            t, mean_M - sem_M, mean_M + sem_M, color="C1", alpha=0.25,
            label="± sem (sd entre semillas / √30)",
        )
        ax.plot(
            t, mean_Np, "-", color="C0", linewidth=2.2,
            label="media entre semillas de N·p(t)",
        )
        ax.plot(
            t, theo, ":", color="black", linewidth=1.8,
            label="N·sigmoid(logit(0.6)+B·sin(2πt/28)) — onda pura",
        )

        ax.set_title(
            f"B={b_val} — amplitud pico-valle de la media de M: "
            f"{amplitud_mean_M:.2f} pasos",
            fontsize=10,
        )
        ax.set_ylabel(f"M (0..{N})")
        ax.set_ylim([3.5, 8.5])
        ax.grid(True, alpha=0.3)
        if i == 0:
            ax.legend(loc="upper right", fontsize=8)

    axes[-1].set_xlabel("Día")
    fig.suptitle(
        "Barrido de B promediado — 30 semillas (4001–4030) × 30 días"
    )
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.96))

    png_path = OUT_DIR / "12_barrido_B_30seeds.png"
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path, summary


# README.md: add one line to the existing "## Lecturas adicionales" section


def append_readme_line() -> Path:
    readme_path = OUT_DIR / "README.md"
    existing = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""

    marker = "## Lecturas adicionales"
    new_line = (
        "| `12_barrido_B_30seeds.png` | El mismo barrido de B ∈ {0.15, 0.30, 0.50, "
        "0.65} que `10_barrido_B.png`, pero promediado entre 30 semillas (4001–4030) "
        "en vez de mostrar una sola: media entre semillas de M(t) (naranja, ± sem "
        "sombreado), media entre semillas de N·p(t) (azul) y la onda teórica pura "
        "N·sigmoid(logit(0.6)+B·sin(2πt/28)) (negro punteado) | Al promediar 30 "
        "semillas el ruido de muestreo binomial y las rachas endógenas de η se "
        "cancelan en gran parte, dejando ver la onda hormonal incluso para B "
        "pequeño; compara la amplitud pico-valle medida (título de cada panel) "
        "contra la de la onda teórica para ver cuánto de la señal restante viene "
        "de μ/η residual |\n"
    )

    if marker in existing:
        # Insert the new row after the last table row, before the next blank line.
        idx_section = existing.index(marker)
        idx_after = existing.index("### Regenerar", idx_section)
        # Find the end of the table block by stepping back from idx_after.
        before = existing[:idx_after]
        after = existing[idx_after:]
        before = before.rstrip("\n") + "\n" + new_line + "\n"
        readme_path.write_text(before + after, encoding="utf-8")
    else:
        section_lines: list[str] = []
        if existing and not existing.endswith("\n"):
            section_lines.append("")
        section_lines.append("")
        section_lines.append(marker)
        section_lines.append("")
        section_lines.append("| Figura | Qué muestra | Cómo leerla |")
        section_lines.append("|---|---|---|")
        section_lines.append(new_line.rstrip("\n"))
        section_lines.append("")
        readme_path.write_text(existing + "\n".join(section_lines) + "\n", encoding="utf-8")

    return readme_path


# Orchestration


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    png_path, summary = plot_barrido_b_30seeds()
    print(f"escrito: {png_path}")

    readme_path = append_readme_line()
    print(f"escrito: {readme_path}")

    print()
    print(f"{'B':>6} | {'amplitud pico-valle mean(M)':>28} | {'corr(mean_M, teórica)':>22} | {'sd entre semillas (media)':>26}")
    for b_val in B_SWEEP:
        s = summary[b_val]
        print(
            f"{b_val:>6.2f} | {s['amplitud_mean_M']:>28.4f} | "
            f"{s['corr_mean_M_theo']:>22.4f} | {s['sd_between_seeds_mean']:>26.4f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
