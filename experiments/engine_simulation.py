"""Galeria de simulaciones — ciclos emocionales del motor bajo distintos
efectos diarios (tarea ad-hoc, fuera de las olas de Fase 1).

PROPIEDAD: este archivo + la carpeta `engine_simulation/` EN LA RAIZ del
proyecto (no bajo results/ — pedido explicito). No tocar nada mas del arbol.

Variante fija DECOUPLED_OFFSETS, 30 dias, semilla 3001 COMPARTIDA entre los
seis escenarios principales (asi las diferencias entre figuras vienen de los
overrides de PersonaParams / shocks, no del azar). Persona base =
PersonaParams() (defaults adoptados en Fase 1: rho_e=0.7, sigma_e=0.45 — no
se tocan aqui, solo se sobreescriben campos puntuales por escenario via
dataclasses.replace).

Escenarios (ver SCENARIOS mas abajo para los overrides exactos):
    01_baseline       — todos los efectos activos (m/g + eta + mu).
    02_solo_ciclo     — sigma_e=0, k=0 (eta==0, mu==0): solo la onda m/g.
    03_solo_endogeno  — B=0, A=0, sigma_eps=0, k=0: solo las rachas eta.
    04_racha_negativa — defaults + shocks dias 10..14 = -1.0 (via mu).
    05_alta_volatilidad — nu=4.0: sobredispersion beta-binomial.
    06_ciclo_fuerte   — A=0.4, B=0.3: fase "perceptible" (riesgo R2 del
                        informe de Fase 1, results/fase-1-informe.md).

Figuras adicionales:
    00_comparativa.png — small multiples 2x3 de M(t) para los 6 escenarios.
    07_intradia.png    — efecto circadiano (rapido) sobre el baseline:
                         heatmap p_h(d,h) + curvas de energia por fase.

Reproducir:
    wsl.exe -d Ubuntu -- bash -lc \
        'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && \
         MPLBACKEND=Agg .venv/bin/python -m experiments.engine_simulation'
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from engine import circadian, mood
from engine.types import MoodVariant, PersonaParams, SimResult, TimingParams
from sim.run_daily import run

# Experiment constants

DAYS = 30
SEED = 3001
VARIANT = MoodVariant.DECOUPLED_OFFSETS
OUT_DIR = Path(__file__).resolve().parent.parent / "engine_simulation"

_BASE_PERSONA = PersonaParams()
_TIMING = TimingParams()

# Negative streak window (days, inclusive) for scenario 04: 10..14.
STREAK_DAYS = range(10, 15)
STREAK_SCORE = -1.0
SHOCKS_STREAK: dict[int, float] = {t: STREAK_SCORE for t in STREAK_DAYS}


# Scenario definitions: (slug, short title, overrides, shocks).
# overrides = PersonaParams fields via dataclasses.replace; shocks = day->score dict (None = none).

ScenarioSpec = tuple[str, str, dict[str, float], dict[int, float] | None]

SCENARIOS: list[ScenarioSpec] = [
    (
        "01_baseline",
        "baseline",
        {},
        None,
    ),
    (
        "02_solo_ciclo",
        "solo ciclo hormonal",
        {"sigma_e": 0.0, "k": 0.0},
        None,
    ),
    (
        "03_solo_endogeno",
        "solo rachas endogenas",
        {"B": 0.0, "A": 0.0, "sigma_eps": 0.0, "k": 0.0},
        None,
    ),
    (
        "04_racha_negativa",
        "racha negativa (shocks 10-14)",
        {},
        SHOCKS_STREAK,
    ),
    (
        "05_alta_volatilidad",
        "alta volatilidad (nu=4.0)",
        {"nu": 4.0},
        None,
    ),
    (
        "06_ciclo_fuerte",
        "ciclo fuerte (A=0.4, B=0.3)",
        {"A": 0.4, "B": 0.3},
        None,
    ),
]


def build_persona(overrides: dict[str, float]) -> PersonaParams:
    """Persona base con `overrides` aplicados via dataclasses.replace."""
    return dataclasses.replace(_BASE_PERSONA, **overrides)


def run_scenario(overrides: dict[str, float], shocks: dict[int, float] | None) -> SimResult:
    """Corre DAYS dias con SEED compartida, variante fija y la persona del escenario."""
    persona = build_persona(overrides)
    return run(days=DAYS, seed=SEED, variant=VARIANT, persona=persona, shocks=shocks)


# Per-scenario figure: 3 panels (M with band, mu/eta, m/g).


def plot_scenario(slug: str, label: str, result: SimResult) -> Path:
    """Figura de 3 paneles (sharex) para un escenario. Nombre: {slug}.png."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    N = result.params.N
    t = result.t
    M = result.M
    p = result.p
    mu = result.mu
    eta = result.eta
    m = result.m
    g = result.g

    fig, (ax_m, ax_mu, ax_mg) = plt.subplots(3, 1, figsize=(10, 9), sharex=True, dpi=120)

    # Panel (a): M(t) with an N*p(t) +/- binomial sd band.
    sd_binom = np.sqrt(N * p * (1.0 - p))
    upper = N * p + sd_binom
    lower = N * p - sd_binom
    ax_m.fill_between(t, lower, upper, alpha=0.2, color="C0", label="N·p(t) ± σ")
    ax_m.plot(t, N * p, "C0--", alpha=0.6, linewidth=1.2, label="N·p(t)")
    ax_m.plot(t, M, "o-", color="C1", linewidth=1.8, markersize=4, label="M(t)")
    ax_m.set_ylabel(f"M (0..{N})")
    ax_m.set_ylim([-0.5, N + 0.5])
    ax_m.legend(loc="upper right", fontsize=8)
    ax_m.grid(True, alpha=0.3)

    # Panel (b): mu(t) and eta(t).
    ax_mu.plot(t, mu, "o-", color="C2", linewidth=1.6, markersize=3.5, label="μ(t)")
    ax_mu.plot(t, eta, "s-", color="C3", linewidth=1.6, markersize=3.5, label="η(t)")
    ax_mu.axhline(0.0, color="gray", linewidth=0.6)
    if slug == "04_racha_negativa":
        ax_mu.axvspan(
            min(STREAK_DAYS),
            max(STREAK_DAYS) + 1,
            color="red",
            alpha=0.12,
            label=f"racha shock (días {min(STREAK_DAYS)}-{max(STREAK_DAYS)})",
        )
    ax_mu.set_ylabel("μ, η")
    ax_mu.legend(loc="upper right", fontsize=8)
    ax_mu.grid(True, alpha=0.3)

    # Panel (c): m(t) and g(t), two y-axes.
    ax_mg2 = ax_mg.twinx()
    l1, = ax_mg.plot(t, m, "o-", color="C4", linewidth=1.6, markersize=3.5, label="m(t)")
    l2, = ax_mg2.plot(t, g, "s-", color="C5", linewidth=1.6, markersize=3.5, label="g(t)")
    ax_mg.set_xlabel("Día")
    ax_mg.set_ylabel("m(t)", color="C4")
    ax_mg2.set_ylabel("g(t)", color="C5")
    ax_mg.tick_params(axis="y", labelcolor="C4")
    ax_mg2.tick_params(axis="y", labelcolor="C5")
    ax_mg.legend(handles=[l1, l2], loc="upper right", fontsize=8)
    ax_mg.grid(True, alpha=0.3)

    fig.suptitle(f"{label} — 30 días · decoupled_offsets · seed {SEED}")
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.97))

    png_path = OUT_DIR / f"{slug}.png"
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path


# 00_comparativa.png: 2x3 small multiples of M(t).


def plot_comparativa(results: dict[str, SimResult]) -> Path:
    """2x3 small multiples de M(t) + linea de media, mismo ylim. Nombre: 00_comparativa.png."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    N = _BASE_PERSONA.N
    fig, axes = plt.subplots(2, 3, figsize=(15, 8), sharex=True, sharey=True, dpi=120)

    for ax, (slug, _label, _overrides, _shocks) in zip(axes.flat, SCENARIOS):
        result = results[slug]
        t = result.t
        M = result.M
        mean_M = float(np.mean(M))

        ax.plot(t, M, "-", color="C1", linewidth=1.3, alpha=0.85)
        ax.axhline(mean_M, color="C1", linestyle=":", linewidth=1.4, label=f"media={mean_M:.2f}")
        ax.set_title(slug, fontsize=10)
        ax.set_ylim([-0.5, N + 0.5])
        ax.legend(loc="upper right", fontsize=7)
        ax.grid(True, alpha=0.3)

    for ax in axes[-1, :]:
        ax.set_xlabel("Día")
    for ax in axes[:, 0]:
        ax.set_ylabel(f"M (0..{N})")

    fig.suptitle(f"Comparativa de escenarios — 30 días · decoupled_offsets · seed {SEED}")
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.95))

    png_path = OUT_DIR / "00_comparativa.png"
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path


# 07_intradia.png: fast circadian effect on the baseline.


# Cycle phases, for the energy curves of panel (b).
_PHASE_LABELS = (
    "menstrual",
    "follicular",
    "ovulatory",
    "luteal_early",
    "luteal_late",
)


def plot_intradia(baseline_result: SimResult) -> Path:
    """Heatmap p_h(d,h) + curvas de energia por fase. Nombre: 07_intradia.png."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    days = baseline_result.t
    arg_by_day = baseline_result.arg

    hours = np.arange(24)
    # p_h(d, h) = sigmoid(arg(d) + c(h)).
    c_by_hour = np.array([circadian.c(float(h), _TIMING) for h in hours])
    p_h = np.array(
        [[mood.sigmoid(arg_d + c_h) for arg_d in arg_by_day] for c_h in c_by_hour]
    )  # shape (24, DAYS): row=hour, column=day.

    fig, (ax_hm, ax_energy) = plt.subplots(1, 2, figsize=(14, 6), dpi=120)

    im = ax_hm.imshow(
        p_h,
        aspect="auto",
        origin="lower",
        extent=(days[0] - 0.5, days[-1] + 0.5, -0.5, 23.5),
        cmap="viridis",
    )
    ax_hm.set_xlabel("Día")
    ax_hm.set_ylabel("Hora local")
    ax_hm.set_title("p_h(d, h) = sigmoid(arg(d) + c(h))")
    ax_hm.set_yticks(np.arange(0, 24, 3))
    fig.colorbar(im, ax=ax_hm, label="p_h(d, h)")

    h_fine = np.linspace(0.0, 24.0, 200, endpoint=False)
    for phase_label in _PHASE_LABELS:
        energy_vals = [circadian.energy(float(h), phase_label, _TIMING) for h in h_fine]
        ax_energy.plot(h_fine, energy_vals, linewidth=1.8, label=phase_label)

    ax_energy.set_xlabel("Hora local")
    ax_energy.set_ylabel("energy(h, fase)")
    ax_energy.set_xlim([0.0, 24.0])
    ax_energy.set_xticks(np.arange(0, 25, 3))
    ax_energy.set_title("Canal de energía por fase del ciclo")
    ax_energy.legend(loc="best", fontsize=8)
    ax_energy.grid(True, alpha=0.3)

    fig.suptitle(f"Efecto intradía (circadiano) sobre el baseline — seed {SEED}")
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.94))

    png_path = OUT_DIR / "07_intradia.png"
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path


# README.md: short index of the gallery.


def write_readme(results: dict[str, SimResult]) -> Path:
    """Escribe engine_simulation/README.md (indice + tabla de overrides)."""
    lines: list[str] = []
    lines.append("# Galería de simulaciones — efectos diarios del motor")
    lines.append("")
    lines.append(
        f"30 días · variante `decoupled_offsets` · semilla **{SEED}** compartida entre "
        "los 6 escenarios (las diferencias vienen de los overrides, no del azar). "
        "Persona base = `PersonaParams()` (defaults adoptados en Fase 1)."
    )
    lines.append("")
    lines.append("## Figuras")
    lines.append("")
    lines.append("| Figura | Qué muestra | Qué mirar |")
    lines.append("|---|---|---|")
    lines.append(
        "| `00_comparativa.png` | Small multiples 2×3 de M(t) para los 6 escenarios, "
        "mismo eje y | Contraste rápido de dispersión y nivel medio entre escenarios |"
    )
    lines.append(
        "| `01_baseline.png` | Todos los efectos activos: ciclo m/g + rachas endógenas η "
        "+ memoria de eventos μ | Línea de base con la que comparar los demás escenarios |"
    )
    lines.append(
        "| `02_solo_ciclo.png` | σ_e=0, k=0 ⇒ η≡0 y μ≡0: solo queda la onda hormonal m/g | "
        "Periodicidad ~28 días pura, sin ruido de rachas ni memoria |"
    )
    lines.append(
        "| `03_solo_endogeno.png` | B=0, A=0, σ_ε=0, k=0: solo quedan las rachas "
        "endógenas η | Deriva tipo \"amanecí así, sin motivo\", sin periodicidad del ciclo |"
    )
    lines.append(
        "| `04_racha_negativa.png` | Defaults + shocks días 10–14 = −1.0 (vía μ) | "
        "Profundidad de la caída de μ durante la racha y velocidad de recuperación al soltar |"
    )
    lines.append(
        "| `05_alta_volatilidad.png` | ν=4.0: sobredispersión beta-binomial | "
        "M(t) más errático día a día que el baseline, banda de referencia más ancha |"
    )
    lines.append(
        "| `06_ciclo_fuerte.png` | A=0.4, B=0.3: variante \"fase perceptible\" (riesgo R2, "
        "results/fase-1-informe.md) | Oscilación de m/g y su arrastre sobre M(t) mucho más visible "
        "en un solo ciclo |"
    )
    lines.append(
        "| `07_intradia.png` | Efecto circadiano (rápido) sobre el baseline: heatmap "
        "p_h(d,h) y curvas de energía por fase | Pico diario de probabilidad de mensaje "
        "alrededor de `peak_hour`, y cómo el offset de energía por fase desplaza cada curva |"
    )
    lines.append("")
    lines.append("## Regenerar")
    lines.append("")
    lines.append("```powershell")
    lines.append(
        "wsl.exe -d Ubuntu -- bash -lc "
        "'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && "
        "MPLBACKEND=Agg .venv/bin/python -m experiments.engine_simulation'"
    )
    lines.append("```")
    lines.append("")
    lines.append(f"Semilla compartida: **{SEED}** · variante: `{VARIANT.value}` · días: {DAYS}")
    lines.append("")
    lines.append("### Persona base y overrides por escenario")
    lines.append("")
    lines.append("Persona base = `PersonaParams()` (defaults): "
                  f"lam={_BASE_PERSONA.lam}, nu={_BASE_PERSONA.nu}, k={_BASE_PERSONA.k}, "
                  f"rho={_BASE_PERSONA.rho}, rho_e={_BASE_PERSONA.rho_e}, "
                  f"sigma_e={_BASE_PERSONA.sigma_e}, B={_BASE_PERSONA.B}, A={_BASE_PERSONA.A}, "
                  f"sigma_eps={_BASE_PERSONA.sigma_eps}.")
    lines.append("")
    lines.append("| Escenario | Overrides (dataclasses.replace) | Shocks |")
    lines.append("|---|---|---|")
    for slug, label, overrides, shocks in SCENARIOS:
        overrides_str = (
            ", ".join(f"{k}={v}" for k, v in overrides.items()) if overrides else "—"
        )
        if shocks:
            days_str = f"{min(shocks)}–{max(shocks)}"
            shocks_str = f"días {days_str} = {next(iter(shocks.values()))}"
        else:
            shocks_str = "—"
        lines.append(f"| `{slug}` ({label}) | {overrides_str} | {shocks_str} |")
    lines.append("")

    readme_path = OUT_DIR / "README.md"
    readme_path.write_text("\n".join(lines), encoding="utf-8")
    return readme_path


# Orchestration


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    results: dict[str, SimResult] = {}
    for slug, label, overrides, shocks in SCENARIOS:
        result = run_scenario(overrides, shocks)
        results[slug] = result
        png_path = plot_scenario(slug, label, result)
        print(f"escrito: {png_path}")

    comparativa_path = plot_comparativa(results)
    print(f"escrito: {comparativa_path}")

    intradia_path = plot_intradia(results["01_baseline"])
    print(f"escrito: {intradia_path}")

    readme_path = write_readme(results)
    print(f"escrito: {readme_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
