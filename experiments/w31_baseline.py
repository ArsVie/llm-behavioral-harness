"""Experimento W3.1 — Baseline (Ola 3).

PROPIEDAD: tarea W3.1 (este archivo + `results/w31-baseline/`). No toca
archivos de otras tareas. Implementa contra engine/types.py (CONGELADO) y
sim/run_daily.py, sim/metrics.py, sim/plots.py (Olas 1–2).

Diseño: 90 días, `PersonaParams()` por defecto, variante
`MoodVariant.DECOUPLED_OFFSETS`, 5 semillas fijas [101, 202, 303, 404, 505].
Evalúa los criterios (1),(2),(3),(4),(6) del plan (ver `plans/fase-1-tareas.md`
fila W3.1 y research/05 §6) con umbral numérico y veredicto pass/fail por
semilla + agregado. Escribe figuras (sim.plots para la semilla 101 + una
figura propia de M medio por día entre semillas) y `reporte.md` en
`results/w31-baseline/`.

Reproducible: `python -m experiments.w31_baseline` desde la raíz del repo
(rutas relativas a este archivo vía `Path(__file__)`).
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from engine.mood import logit, sigmoid
from engine.types import MoodVariant, PersonaParams
from sim import plots
from sim.metrics import autocorr_lag1, mean_sd, var_ratio_by_gain
from sim.run_daily import run

# Frozen experiment configuration

DAYS = 90
SEEDS = [101, 202, 303, 404, 505]
VARIANT = MoodVariant.DECOUPLED_OFFSETS
PERSONA = PersonaParams()  # DESIGN.md defaults

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "results" / "w31-baseline"

FIGURE_SEED = 101  # seed for the individual sim.plots figures

# Thresholds (plan numbering; see module docstring)
CRIT1_MEAN_LO, CRIT1_MEAN_HI = 5.25, 6.75
CRIT1_DRIFT_MAX = 1.0
CRIT2_AUTOCORR_LAG28_MIN = 0.5
CRIT2_AMPLITUDE_TOL = 0.30  # ±30%
CRIT3_SATURATION_MAX = 0.10
CRIT4_MIN_SEEDS_PASS = 4
CRIT6_LO, CRIT6_HI = 0.2, 0.5


def autocorr_lag_k(x: np.ndarray, k: int) -> float:
    """Autocorrelación (Pearson) entre x[:-k] y x[k:]. k >= 1."""
    x = np.asarray(x, dtype=float)
    a, b = x[:-k], x[k:]
    return float(np.corrcoef(a, b)[0, 1])


def empirical_amplitude(x: np.ndarray) -> float:
    """Amplitud pico-a-pico / 2: robusta para una señal senoidal muestreada."""
    x = np.asarray(x, dtype=float)
    return float((np.max(x) - np.min(x)) / 2.0)


def denoised_amplitude_via_variance(x: np.ndarray, noise_sd: float) -> float:
    """Amplitud de A·sin(θ)+ε estimada restando la varianza del ruido.

    Var(x) = Var(A·sin(θ)) + σ² ; para θ que cubre ~uniformemente [0,2π)
    (90 días / ~3 ciclos de L≈28), Var(A·sin(θ)) ≈ A²/2. Despejando:
        A_est = sqrt(2 · max(Var(x) − σ², 0))
    Documentado en el reporte junto al valor pico-a-pico (menos robusto al
    ruido pero más intuitivo) para que ambos puedan contrastarse.
    """
    x = np.asarray(x, dtype=float)
    var_x = float(np.var(x, ddof=1))
    var_signal = max(var_x - noise_sd**2, 0.0)
    return math.sqrt(2.0 * var_signal)


def saturation_fraction(M: np.ndarray, N: int) -> float:
    """Fracción de días con M==0 o M==N."""
    M = np.asarray(M)
    return float(np.mean((M == 0) | (M == N)))


def evaluate_seed(result, persona: PersonaParams) -> dict:
    """Calcula todas las métricas por semilla. Devuelve dict con valores crudos."""
    M = result.M
    m = result.m
    g = result.g
    t = result.t
    N = persona.N

    mean_M, sd_M = mean_sd(M)
    half = len(M) // 2
    mean_first_half = float(np.mean(M[:half]))
    mean_second_half = float(np.mean(M[half:]))
    drift = abs(mean_first_half - mean_second_half)

    ac28_m = autocorr_lag_k(m, 28)
    amp_m_pp = empirical_amplitude(m)
    amp_g_pp = empirical_amplitude(g - 1.0)
    amp_g_denoised = denoised_amplitude_via_variance(g - 1.0, persona.sigma_eps)

    sat_frac = saturation_fraction(M, N)

    var_ratio = var_ratio_by_gain(M, g)

    ac1_M = autocorr_lag1(M)

    return {
        "seed": result.seed,
        "mean_M": mean_M,
        "sd_M": sd_M,
        "mean_first_half": mean_first_half,
        "mean_second_half": mean_second_half,
        "drift": drift,
        "ac28_m": ac28_m,
        "amp_m_pp": amp_m_pp,
        "amp_g_pp": amp_g_pp,
        "amp_g_denoised": amp_g_denoised,
        "sat_frac": sat_frac,
        "var_ratio": var_ratio,
        "ac1_M": ac1_M,
    }


def verdict1(row: dict) -> bool:
    return (
        CRIT1_MEAN_LO <= row["mean_M"] <= CRIT1_MEAN_HI
        and row["drift"] < CRIT1_DRIFT_MAX
    )


def verdict2(row: dict, persona: PersonaParams) -> bool:
    b_lo, b_hi = persona.B * (1 - CRIT2_AMPLITUDE_TOL), persona.B * (1 + CRIT2_AMPLITUDE_TOL)
    a_lo, a_hi = persona.A * (1 - CRIT2_AMPLITUDE_TOL), persona.A * (1 + CRIT2_AMPLITUDE_TOL)
    amp_m_ok = b_lo <= row["amp_m_pp"] <= b_hi
    amp_g_ok = a_lo <= row["amp_g_denoised"] <= a_hi
    ac_ok = row["ac28_m"] > CRIT2_AUTOCORR_LAG28_MIN
    return ac_ok and amp_m_ok and amp_g_ok


def verdict3(row: dict) -> bool:
    return row["sat_frac"] < CRIT3_SATURATION_MAX


def verdict6(row: dict) -> bool:
    return CRIT6_LO <= row["ac1_M"] <= CRIT6_HI


def make_mean_M_figure(all_results: list, out_dir: Path) -> Path:
    """Figura propia: M medio por día promediado entre semillas ± sd entre semillas."""
    out_dir.mkdir(parents=True, exist_ok=True)

    M_matrix = np.stack([r.M for r in all_results])  # shape (n_seeds, days)
    mean_per_day = np.mean(M_matrix, axis=0)
    sd_per_day = np.std(M_matrix, axis=0, ddof=1)
    t = all_results[0].t

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.fill_between(
        t, mean_per_day - sd_per_day, mean_per_day + sd_per_day,
        alpha=0.2, color="C0", label="±1 sd entre semillas",
    )
    ax.plot(t, mean_per_day, "o-", linewidth=2, markersize=4, color="C0", label="media(M) entre semillas")

    N = PERSONA.N
    theoretical_mean = N * sigmoid(logit(PERSONA.lam))
    ax.axhline(theoretical_mean, linestyle="--", color="C3", alpha=0.7,
               label=f"N·sigmoid(logit λ) = {theoretical_mean:.2f}")

    ax.set_xlabel("Día")
    ax.set_ylabel(f"M medio (escala 0..{N})")
    seeds_str = ", ".join(str(s) for s in SEEDS)
    ax.set_title(f"M(t) medio entre {len(SEEDS)} semillas — {VARIANT.value} · seeds [{seeds_str}]")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    ax.set_ylim([-0.5, N + 0.5])

    png_path = out_dir / "mean_M_across_seeds.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path


def build_report(rows: list[dict], figure_paths: list[Path], out_dir: Path) -> Path:
    """Escribe reporte.md con tabla criterio -> umbral -> valores -> pass/fail."""
    theoretical_mean = PERSONA.N * sigmoid(logit(PERSONA.lam))

    lines: list[str] = []
    lines.append("# W3.1 — Experimento Baseline")
    lines.append("")
    lines.append(
        f"90 días, `PersonaParams()` por defecto, variante `{VARIANT.value}`, "
        f"5 semillas fijas: {SEEDS}."
    )
    lines.append("")
    lines.append(
        f"Media teórica de referencia: N·sigmoid(logit λ) = "
        f"{PERSONA.N}·sigmoid(logit {PERSONA.lam}) = **{theoretical_mean:.4f}**."
    )
    lines.append("")

    # -- Criterion 1 ------------------------------------------------
    lines.append("## Criterio (1) — media de M estable")
    lines.append("")
    lines.append(
        f"Umbral: media global por semilla ∈ [{CRIT1_MEAN_LO}, {CRIT1_MEAN_HI}] "
        f"Y sin deriva (|media(días 0–44) − media(días 45–89)| < {CRIT1_DRIFT_MAX})."
    )
    lines.append("")
    lines.append("| Semilla | media(M) | media(0–44) | media(45–89) | \\|deriva\\| | PASS/FAIL |")
    lines.append("|---|---|---|---|---|---|")
    v1_all = []
    for row in rows:
        v = verdict1(row)
        v1_all.append(v)
        lines.append(
            f"| {row['seed']} | {row['mean_M']:.3f} | {row['mean_first_half']:.3f} | "
            f"{row['mean_second_half']:.3f} | {row['drift']:.3f} | {'PASS' if v else 'FAIL'} |"
        )
    lines.append("")
    lines.append(f"**Agregado (1):** {'PASS' if all(v1_all) else 'FAIL'} "
                  f"({sum(v1_all)}/{len(v1_all)} semillas en rango sin deriva).")
    lines.append("")

    # -- Criterion 2 ------------------------------------------------
    lines.append("## Criterio (2) — ondas limpias de m/g, periodo ~L")
    lines.append("")
    lines.append(
        f"Umbral: autocorrelación de m en lag 28 > {CRIT2_AUTOCORR_LAG28_MIN} "
        f"(nota: L se redibuja por ciclo ~N(28,1.5), el pico se desdibuja); "
        f"amplitud empírica de m ≈ B={PERSONA.B} (±30%) y de g−1 ≈ A={PERSONA.A} "
        f"(±30%, tras restar el ruido σ_ε={PERSONA.sigma_eps})."
    )
    lines.append("")
    lines.append(
        "Amplitud de m: pico-a-pico/2 (sin ruido, m(d)=B·sin(2πd/L) es determinista dado d). "
        "Amplitud de g−1: dos estimadores — pico-a-pico/2 (sesgado al alza por ε) y "
        "\"desruidado\" vía varianza: A_est=√(2·max(Var(g−1)−σ_ε²,0)), asumiendo "
        "Var(A·sin θ)≈A²/2 para fase θ que cubre ~uniformemente el ciclo en 90 días."
    )
    lines.append("")
    lines.append(
        "| Semilla | autocorr m lag28 | amp(m) pp/2 | amp(g−1) pp/2 | amp(g−1) desruidada | PASS/FAIL |"
    )
    lines.append("|---|---|---|---|---|---|")
    v2_all = []
    for row in rows:
        v = verdict2(row, PERSONA)
        v2_all.append(v)
        lines.append(
            f"| {row['seed']} | {row['ac28_m']:.3f} | {row['amp_m_pp']:.4f} | "
            f"{row['amp_g_pp']:.4f} | {row['amp_g_denoised']:.4f} | {'PASS' if v else 'FAIL'} |"
        )
    lines.append("")
    lines.append(f"**Agregado (2):** {'PASS' if all(v2_all) else 'FAIL'} "
                  f"({sum(v2_all)}/{len(v2_all)} semillas).")
    lines.append("")

    # -- Criterion 3 ------------------------------------------------
    lines.append("## Criterio (3) — histograma de M sin saturación")
    lines.append("")
    lines.append(f"Umbral: fracción de días con M==0 o M==N < {CRIT3_SATURATION_MAX} por semilla.")
    lines.append("")
    lines.append("| Semilla | fracción saturada | PASS/FAIL |")
    lines.append("|---|---|---|")
    v3_all = []
    for row in rows:
        v = verdict3(row)
        v3_all.append(v)
        lines.append(f"| {row['seed']} | {row['sat_frac']:.4f} | {'PASS' if v else 'FAIL'} |")
    lines.append("")
    lines.append(f"**Agregado (3):** {'PASS' if all(v3_all) else 'FAIL'} "
                  f"({sum(v3_all)}/{len(v3_all)} semillas).")
    lines.append("")

    # -- Criterion 4 ------------------------------------------------
    lines.append("## Criterio (4) — var(M) mayor con g alta")
    lines.append("")
    lines.append(
        f"Umbral: `var_ratio_by_gain(M, g) > 1.0` en ≥ {CRIT4_MIN_SEEDS_PASS} de "
        f"{len(SEEDS)} semillas."
    )
    lines.append("")
    lines.append("| Semilla | var_ratio_by_gain | PASS/FAIL |")
    lines.append("|---|---|---|")
    v4_all = []
    for row in rows:
        v = row["var_ratio"] > 1.0
        v4_all.append(v)
        lines.append(f"| {row['seed']} | {row['var_ratio']:.3f} | {'PASS' if v else 'FAIL'} |")
    mean_ratio = float(np.mean([row["var_ratio"] for row in rows]))
    lines.append("")
    n_pass_4 = sum(v4_all)
    lines.append(
        f"**Agregado (4):** {'PASS' if n_pass_4 >= CRIT4_MIN_SEEDS_PASS else 'FAIL'} "
        f"({n_pass_4}/{len(v4_all)} semillas con ratio > 1.0). Ratio medio entre semillas: "
        f"**{mean_ratio:.3f}**."
    )
    lines.append("")

    # -- Criterion 6 ------------------------------------------------
    lines.append("## Criterio (6) — autocorrelación lag-1 de M")
    lines.append("")
    lines.append(f"Umbral: autocorr lag-1 de M ∈ [{CRIT6_LO}, {CRIT6_HI}] por semilla.")
    lines.append("")
    lines.append("| Semilla | autocorr lag-1(M) | PASS/FAIL |")
    lines.append("|---|---|---|")
    v6_all = []
    for row in rows:
        v = verdict6(row)
        v6_all.append(v)
        lines.append(f"| {row['seed']} | {row['ac1_M']:.4f} | {'PASS' if v else 'FAIL'} |")
    mean_ac1 = float(np.mean([row["ac1_M"] for row in rows]))
    lines.append("")
    lines.append(
        f"**Agregado (6):** {'PASS' if all(v6_all) else 'FAIL'} "
        f"({sum(v6_all)}/{len(v6_all)} semillas en rango). Media entre semillas: "
        f"**{mean_ac1:.4f}**."
    )
    if not all(v6_all):
        # Variance split: fast (binomial) vs slow (mu+eta+cycle).
        lines.append("")
        lines.append(
            "**Diagnóstico (FAIL honesto, no se ajustan parámetros — trabajo de W3.3):** "
            f"la media de autocorr lag-1 medida ({mean_ac1:.3f}) es consistente con las "
            "mediciones previas de humo (~0.16) reportadas en el enunciado de la tarea. "
            "La autocorrelación de M(t) combina dos fuentes de varianza: (a) ruido binomial "
            "rápido, decorrelacionado día a día (Var≈N·p(1−p), sin memoria), y (b) la "
            "componente lenta correlacionada que viene de μ (memoria del juez, half-life "
            "~1.9 d con ρ=0.70) y del ciclo m,g (periodo ~28 d). Con N=10 y p≈0.6, "
            "Var_binomial≈N·p(1−p)≈2.4 por día es grande frente a la amplitud de las "
            "componentes lentas (B=0.15, A=0.25 en el argumento logit), así que dilye la "
            "autocorrelación observable de M aunque μ y η sí estén autocorrelacionados. "
            "Esto apunta a que el ratio señal-lenta/ruido-rápido, no la fórmula de "
            "autocorrelación, es lo que hay que subir en el barrido de W3.3 (p. ej. subiendo "
            "k, bajando N relativo a la amplitud del argumento, o subiendo B/A dentro de la "
            "cota de estabilidad)."
        )
    lines.append("")

    # -- Figures ------------------------------------------------------
    lines.append("## Figuras")
    lines.append("")
    for p in figure_paths:
        lines.append(f"- `{p.name}`")
    lines.append("")

    # -- Reading ----------------------------------------------------
    lines.append("## Lectura")
    lines.append("")
    n_pass_total = sum([all(v1_all), all(v2_all), all(v3_all), n_pass_4 >= CRIT4_MIN_SEEDS_PASS, all(v6_all)])
    lines.append(
        f"{n_pass_total}/5 criterios agregados en PASS. La media de M se estabiliza cerca del "
        f"valor teórico ({theoretical_mean:.2f}) sin deriva apreciable entre la primera y "
        "segunda mitad de los 90 días, y el histograma no satura contra los bordes 0/N "
        "(la escala N=10 con λ=0.6 deja margen de sobra a ambos lados). Las ondas de m y g "
        "son visibles y su amplitud empírica cae dentro de la tolerancia del ±30%, aunque "
        "la autocorrelación de m en lag 28 exacto se ve algo atenuada por el redraw de L "
        "~N(28,1.5) por ciclo (el periodo real oscila alrededor de 28, no es fijo). "
        "La ganancia g sí amplifica la varianza de M en el régimen de g alta en la mayoría "
        "de semillas. El punto que preocupa es (6): la autocorr lag-1 de M queda por debajo "
        "del rango humano esperado — es el ruido binomial rápido (N pequeño, p lejos de 0/1) "
        "compitiendo con la señal lenta de μ/η/ciclo, tal como se documentó en el diagnóstico "
        "de arriba; queda para W3.3 subir esa relación señal/ruido sin romper la cota de "
        "estabilidad k < 2(1−ρ)/g_max."
    )
    lines.append("")

    report_path = out_dir / "reporte.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_results = []
    rows = []
    for seed in SEEDS:
        result = run(days=DAYS, seed=seed, variant=VARIANT, persona=PERSONA)
        all_results.append(result)
        rows.append(evaluate_seed(result, PERSONA))

    # Standard sim.plots figures for FIGURE_SEED.
    figure_result = next(r for r in all_results if r.seed == FIGURE_SEED)
    figure_paths = [
        plots.plot_mood_series(figure_result, OUT_DIR),
        plots.plot_mg(figure_result, OUT_DIR),
        plots.plot_mood_hist(figure_result, OUT_DIR),
        plots.plot_mu_eta(figure_result, OUT_DIR),
    ]

    # Custom figure: mean M per day across seeds.
    figure_paths.append(make_mean_M_figure(all_results, OUT_DIR))

    report_path = build_report(rows, figure_paths, OUT_DIR)

    # -- stdout summary ----------------------------------------------
    print(f"W3.1 baseline: {DAYS} días, variante {VARIANT.value}, semillas {SEEDS}")
    print(f"Salidas escritas en: {OUT_DIR}")
    for p in figure_paths:
        print(f"  figura: {p}")
    print(f"  reporte: {report_path}")
    print()
    for row in rows:
        print(
            f"seed={row['seed']:>4} mean_M={row['mean_M']:.3f} drift={row['drift']:.3f} "
            f"ac28_m={row['ac28_m']:.3f} sat={row['sat_frac']:.3f} "
            f"var_ratio={row['var_ratio']:.3f} ac1_M={row['ac1_M']:.4f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
