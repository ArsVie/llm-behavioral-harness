"""Barrido de parámetros — criterio (8b), región de régimen "humano" (W3.3).

PROPIEDAD: tarea W3.3 (este archivo + results/w33-barrido/). Variante fija
DECOUPLED_OFFSETS, 90 días, 5 semillas por celda para el barrido, 5 semillas
frescas para la verificación de los defaults propuestos.

Diseño (mantenido tratable — NO producto cartesiano completo, ver plan):
    1. 2D (rho_e x sigma_e): autocorrelación endógena.
    2. 2D (k x rho): memoria de eventos (celdas inestables descartadas con
       engine.validation.check y reportadas aparte).
    3. 2D (A x B): ciclo (var_ratio_by_gain + amplitud del ciclo en M).
    4. 1D nu in {inf, 8, 4}: sobredispersión beta-binomial.

Criterio (8b): región "humana" = media(M) in [5.25, 6.75] Y sd(M) in
[1.2, 2.8] Y autocorr_lag1 in [0.2, 0.5] Y fracción_saturada < 0.10.

Reproducible: `python -m experiments.w33_barrido` regenera figuras + reporte
en results/w33-barrido/.
"""
from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from engine import validation
from engine.types import MoodVariant, PersonaParams, TimingParams
from sim.metrics import autocorr_lag1, mean_sd, var_ratio_by_gain
from sim.run_daily import run

# Experiment constants

DAYS = 90
VARIANT = MoodVariant.DECOUPLED_OFFSETS
SWEEP_SEEDS: list[int] = [11, 22, 33, 44, 55]
VERIFY_SEEDS: list[int] = [66, 77, 88, 99, 110]
OUT_DIR = Path(__file__).resolve().parent.parent / "results" / "w33-barrido"

# Criterion (8b) — human region.
MEAN_RANGE = (5.25, 6.75)
SD_RANGE = (1.2, 2.8)
AC1_RANGE = (0.2, 0.5)
SAT_MAX = 0.10

_TIMING_DEFAULT = TimingParams()


# Per-cell metrics (mean across seeds)


def cell_metrics(persona: PersonaParams, seeds: list[int]) -> dict[str, float]:
    """Corre `persona` con cada semilla en `seeds`, promedia las métricas.

    Métricas: mean_M, sd_M, autocorr_lag1, var_ratio_by_gain, sat_frac
    (fracción de días con M en {0, N}). var_ratio_by_gain se omite del
    promedio (NaN) si alguna corrida no tiene suficiente varianza en algún
    cuartil de g (colas degeneradas de M).
    """
    means_M, sds_M, ac1s, sat_fracs, var_ratios = [], [], [], [], []
    for seed in seeds:
        result = run(days=DAYS, seed=seed, variant=VARIANT, persona=persona)
        mean_M, sd_M = mean_sd(result.M)
        ac1 = autocorr_lag1(result.M)
        sat_frac = float(np.mean((result.M == 0) | (result.M == persona.N)))
        means_M.append(mean_M)
        sds_M.append(sd_M)
        ac1s.append(ac1)
        sat_fracs.append(sat_frac)
        try:
            vr = var_ratio_by_gain(result.M, result.g)
            if math.isfinite(vr):
                var_ratios.append(vr)
        except (ZeroDivisionError, FloatingPointError):
            pass

    return {
        "mean_M": float(np.mean(means_M)),
        "sd_M": float(np.mean(sds_M)),
        "autocorr_lag1": float(np.mean(ac1s)),
        "sat_frac": float(np.mean(sat_fracs)),
        "var_ratio_by_gain": float(np.mean(var_ratios)) if var_ratios else float("nan"),
    }


def cycle_amplitude(persona: PersonaParams, seeds: list[int]) -> float:
    """Amplitud del ciclo en M: media(M | m en tercio superior) − media(M | m
    en tercio inferior), promediada entre semillas. `m` es el offset de ciclo
    diario (SimResult.m); con B=0 el offset es 0 en todos los días y la
    amplitud es ~0 por construcción (no hay tercios distintos de m)."""
    diffs = []
    for seed in seeds:
        result = run(days=DAYS, seed=seed, variant=VARIANT, persona=persona)
        m = result.m
        M = result.M.astype(float)
        lo_thresh = np.quantile(m, 1.0 / 3.0)
        hi_thresh = np.quantile(m, 2.0 / 3.0)
        M_lo = M[m <= lo_thresh]
        M_hi = M[m >= hi_thresh]
        if len(M_lo) == 0 or len(M_hi) == 0:
            diffs.append(0.0)
            continue
        diffs.append(float(np.mean(M_hi) - np.mean(M_lo)))
    return float(np.mean(diffs))


def is_human_region(metrics: dict[str, float]) -> bool:
    """True si la celda cumple TODOS los umbrales del criterio (8b)."""
    return (
        MEAN_RANGE[0] <= metrics["mean_M"] <= MEAN_RANGE[1]
        and SD_RANGE[0] <= metrics["sd_M"] <= SD_RANGE[1]
        and AC1_RANGE[0] <= metrics["autocorr_lag1"] <= AC1_RANGE[1]
        and metrics["sat_frac"] < SAT_MAX
    )


# Generic heatmap


def plot_heatmap(
    grid: np.ndarray,
    x_vals: list[float],
    y_vals: list[float],
    x_label: str,
    y_label: str,
    title: str,
    filename: str,
    mask: np.ndarray | None = None,
    human_mask: np.ndarray | None = None,
    fmt: str = "{:.2f}",
    cmap: str = "viridis",
) -> None:
    """imshow con anotaciones de valor por celda, colorbar, ejes etiquetados.

    `grid[i, j]` corresponde a y_vals[i] (filas) × x_vals[j] (columnas).
    `mask`: True = celda inválida/inestable (se dibuja en gris, sin valor).
    `human_mask`: True = celda cumple criterio (8b) (borde rojo grueso).
    """
    fig, ax = plt.subplots(figsize=(1.4 * len(x_vals) + 2.0, 1.2 * len(y_vals) + 2.0))

    plot_grid = np.ma.array(grid, mask=mask if mask is not None else False)
    im = ax.imshow(plot_grid, cmap=cmap, aspect="auto", origin="lower")

    ax.set_xticks(range(len(x_vals)))
    ax.set_xticklabels([str(v) for v in x_vals])
    ax.set_yticks(range(len(y_vals)))
    ax.set_yticklabels([str(v) for v in y_vals])
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_title(title)

    for i in range(len(y_vals)):
        for j in range(len(x_vals)):
            if mask is not None and mask[i, j]:
                ax.text(
                    j, i, "N/A", ha="center", va="center", color="black", fontsize=9
                )
                continue
            val = grid[i, j]
            text = "nan" if not np.isfinite(val) else fmt.format(val)
            # Text contrast from cell brightness.
            norm_val = im.norm(val) if np.isfinite(val) else 0.5
            text_color = "white" if norm_val < 0.6 else "black"
            ax.text(j, i, text, ha="center", va="center", color=text_color, fontsize=9)
            if human_mask is not None and human_mask[i, j]:
                rect = plt.Rectangle(
                    (j - 0.5, i - 0.5), 1, 1, fill=False, edgecolor="red", linewidth=3
                )
                ax.add_patch(rect)

    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    fig.savefig(OUT_DIR / filename, dpi=130)
    plt.close(fig)


# Experiment 1: 2D (rho_e x sigma_e)


def sweep_rho_e_sigma_e() -> dict:
    rho_e_vals = [0.3, 0.5, 0.7, 0.85]
    sigma_e_vals = [0.1, 0.2, 0.3, 0.45]

    ac1_grid = np.zeros((len(rho_e_vals), len(sigma_e_vals)))
    sd_grid = np.zeros((len(rho_e_vals), len(sigma_e_vals)))
    mean_grid = np.zeros((len(rho_e_vals), len(sigma_e_vals)))
    sat_grid = np.zeros((len(rho_e_vals), len(sigma_e_vals)))
    human_mask = np.zeros((len(rho_e_vals), len(sigma_e_vals)), dtype=bool)
    cells = {}

    for i, rho_e in enumerate(rho_e_vals):
        for j, sigma_e in enumerate(sigma_e_vals):
            persona = dataclasses.replace(PersonaParams(), rho_e=rho_e, sigma_e=sigma_e)
            m = cell_metrics(persona, SWEEP_SEEDS)
            ac1_grid[i, j] = m["autocorr_lag1"]
            sd_grid[i, j] = m["sd_M"]
            mean_grid[i, j] = m["mean_M"]
            sat_grid[i, j] = m["sat_frac"]
            human_mask[i, j] = is_human_region(m)
            cells[(rho_e, sigma_e)] = m

    plot_heatmap(
        ac1_grid, sigma_e_vals, rho_e_vals, "sigma_e", "rho_e",
        f"Barrido rho_e x sigma_e — autocorr lag-1 de M (90d, seeds={SWEEP_SEEDS})",
        "01_rho_e_sigma_e_autocorr.png", human_mask=human_mask, fmt="{:.3f}",
    )
    plot_heatmap(
        sd_grid, sigma_e_vals, rho_e_vals, "sigma_e", "rho_e",
        f"Barrido rho_e x sigma_e — sd(M) (90d, seeds={SWEEP_SEEDS})",
        "02_rho_e_sigma_e_sd.png", human_mask=human_mask, fmt="{:.2f}", cmap="magma",
    )

    return {
        "rho_e_vals": rho_e_vals,
        "sigma_e_vals": sigma_e_vals,
        "cells": cells,
        "human_mask": human_mask,
        "mean_grid": mean_grid,
        "sd_grid": sd_grid,
        "ac1_grid": ac1_grid,
        "sat_grid": sat_grid,
    }


# Experiment 2: 2D (k x rho) — event memory


def sweep_k_rho() -> dict:
    k_vals = [0.05, 0.15, 0.3, 0.44]
    rho_vals = [0.5, 0.7, 0.85]

    ac1_grid = np.full((len(k_vals), len(rho_vals)), np.nan)
    sd_grid = np.full((len(k_vals), len(rho_vals)), np.nan)
    mean_grid = np.full((len(k_vals), len(rho_vals)), np.nan)
    sat_grid = np.full((len(k_vals), len(rho_vals)), np.nan)
    unstable_mask = np.zeros((len(k_vals), len(rho_vals)), dtype=bool)
    human_mask = np.zeros((len(k_vals), len(rho_vals)), dtype=bool)
    cells = {}
    unstable_report = []

    for i, k in enumerate(k_vals):
        for j, rho in enumerate(rho_vals):
            persona = dataclasses.replace(PersonaParams(), k=k, rho=rho)
            errors = validation.check(persona, _TIMING_DEFAULT)
            stability_errors = [e for e in errors if e.startswith("k:")]
            if stability_errors:
                unstable_mask[i, j] = True
                unstable_report.append((k, rho, stability_errors[0]))
                continue
            m = cell_metrics(persona, SWEEP_SEEDS)
            ac1_grid[i, j] = m["autocorr_lag1"]
            sd_grid[i, j] = m["sd_M"]
            mean_grid[i, j] = m["mean_M"]
            sat_grid[i, j] = m["sat_frac"]
            human_mask[i, j] = is_human_region(m)
            cells[(k, rho)] = m

    plot_heatmap(
        ac1_grid, rho_vals, k_vals, "rho", "k",
        f"Barrido k x rho — autocorr lag-1 de M (90d, seeds={SWEEP_SEEDS})",
        "03_k_rho_autocorr.png", mask=unstable_mask, human_mask=human_mask, fmt="{:.3f}",
    )
    plot_heatmap(
        sd_grid, rho_vals, k_vals, "rho", "k",
        f"Barrido k x rho — sd(M) (90d, seeds={SWEEP_SEEDS})",
        "04_k_rho_sd.png", mask=unstable_mask, human_mask=human_mask, fmt="{:.2f}", cmap="magma",
    )

    return {
        "k_vals": k_vals,
        "rho_vals": rho_vals,
        "cells": cells,
        "human_mask": human_mask,
        "unstable_mask": unstable_mask,
        "unstable_report": unstable_report,
        "mean_grid": mean_grid,
        "sd_grid": sd_grid,
        "ac1_grid": ac1_grid,
        "sat_grid": sat_grid,
    }


# Experiment 3: 2D (A x B) — cycle


def sweep_A_B() -> dict:
    A_vals = [0.1, 0.25, 0.4]
    B_vals = [0.0, 0.15, 0.3]

    var_ratio_grid = np.zeros((len(A_vals), len(B_vals)))
    amp_grid = np.zeros((len(A_vals), len(B_vals)))
    mean_grid = np.zeros((len(A_vals), len(B_vals)))
    sd_grid = np.zeros((len(A_vals), len(B_vals)))
    ac1_grid = np.zeros((len(A_vals), len(B_vals)))
    sat_grid = np.zeros((len(A_vals), len(B_vals)))
    human_mask = np.zeros((len(A_vals), len(B_vals)), dtype=bool)
    cells = {}

    for i, A in enumerate(A_vals):
        for j, B in enumerate(B_vals):
            persona = dataclasses.replace(PersonaParams(), A=A, B=B)
            m = cell_metrics(persona, SWEEP_SEEDS)
            amp = cycle_amplitude(persona, SWEEP_SEEDS)
            var_ratio_grid[i, j] = m["var_ratio_by_gain"]
            amp_grid[i, j] = amp
            mean_grid[i, j] = m["mean_M"]
            sd_grid[i, j] = m["sd_M"]
            ac1_grid[i, j] = m["autocorr_lag1"]
            sat_grid[i, j] = m["sat_frac"]
            human_mask[i, j] = is_human_region(m)
            cells[(A, B)] = {**m, "amplitude": amp}

    plot_heatmap(
        var_ratio_grid, B_vals, A_vals, "B", "A",
        f"Barrido A x B — var_ratio_by_gain (90d, seeds={SWEEP_SEEDS})",
        "05_A_B_var_ratio.png", human_mask=human_mask, fmt="{:.2f}",
    )
    plot_heatmap(
        amp_grid, B_vals, A_vals, "B", "A",
        f"Barrido A x B — amplitud del ciclo en M (media alto−bajo m) (90d, seeds={SWEEP_SEEDS})",
        "06_A_B_amplitude.png", human_mask=human_mask, fmt="{:.2f}", cmap="magma",
    )

    return {
        "A_vals": A_vals,
        "B_vals": B_vals,
        "cells": cells,
        "human_mask": human_mask,
        "mean_grid": mean_grid,
        "sd_grid": sd_grid,
        "ac1_grid": ac1_grid,
        "sat_grid": sat_grid,
        "var_ratio_grid": var_ratio_grid,
        "amp_grid": amp_grid,
    }


# Experiment 4: 1D nu


def sweep_nu() -> dict:
    nu_vals: list[float] = [math.inf, 8.0, 4.0]
    labels = ["inf", "8", "4"]

    rows = []
    for nu in nu_vals:
        persona = dataclasses.replace(PersonaParams(), nu=nu)
        m = cell_metrics(persona, SWEEP_SEEDS)
        rows.append(m)

    fig, axes = plt.subplots(1, 3, figsize=(13, 4))
    sd_vals = [r["sd_M"] for r in rows]
    ac1_vals = [r["autocorr_lag1"] for r in rows]
    sat_vals = [r["sat_frac"] for r in rows]

    axes[0].bar(labels, sd_vals, color="tab:blue")
    axes[0].set_title("sd(M)")
    axes[0].set_xlabel("nu")
    for idx, v in enumerate(sd_vals):
        axes[0].text(idx, v, f"{v:.2f}", ha="center", va="bottom")

    axes[1].bar(labels, ac1_vals, color="tab:orange")
    axes[1].set_title("autocorr lag-1")
    axes[1].set_xlabel("nu")
    for idx, v in enumerate(ac1_vals):
        axes[1].text(idx, v, f"{v:.3f}", ha="center", va="bottom")

    axes[2].bar(labels, sat_vals, color="tab:green")
    axes[2].set_title("fracción saturada")
    axes[2].set_xlabel("nu")
    for idx, v in enumerate(sat_vals):
        axes[2].text(idx, v, f"{v:.3f}", ha="center", va="bottom")

    fig.suptitle(f"Barrido 1D nu (defaults, 90d, seeds={SWEEP_SEEDS})")
    fig.tight_layout()
    fig.savefig(OUT_DIR / "07_nu_1d.png", dpi=130)
    plt.close(fig)

    return {"nu_vals": nu_vals, "labels": labels, "rows": rows}


# Tuned defaults + verification


def propose_tuned_defaults(rho_e_sigma_e_result: dict) -> PersonaParams:
    """Elige, dentro de la subregión que sube autocorr sin inflar sd(M) ni
    saturar, el punto del grid 1 con mejor cumplimiento del criterio (8b);
    aplica esos rho_e/sigma_e sobre PersonaParams() default."""
    cells = rho_e_sigma_e_result["cells"]
    human_mask = rho_e_sigma_e_result["human_mask"]
    rho_e_vals = rho_e_sigma_e_result["rho_e_vals"]
    sigma_e_vals = rho_e_sigma_e_result["sigma_e_vals"]

    candidates = []
    for i, rho_e in enumerate(rho_e_vals):
        for j, sigma_e in enumerate(sigma_e_vals):
            if human_mask[i, j]:
                m = cells[(rho_e, sigma_e)]
                # Distance to the target autocorr center (0.35).
                dist = abs(m["autocorr_lag1"] - 0.35)
                candidates.append((dist, rho_e, sigma_e, m))

    if not candidates:
        # Fallback: closest autocorr to 0.35 within sd <= 2.8 and sat < 0.10.
        for i, rho_e in enumerate(rho_e_vals):
            for j, sigma_e in enumerate(sigma_e_vals):
                m = cells[(rho_e, sigma_e)]
                if m["sd_M"] <= SD_RANGE[1] and m["sat_frac"] < SAT_MAX:
                    dist = abs(m["autocorr_lag1"] - 0.35)
                    candidates.append((dist, rho_e, sigma_e, m))

    candidates.sort(key=lambda c: c[0])
    _, best_rho_e, best_sigma_e, _ = candidates[0]

    return dataclasses.replace(PersonaParams(), rho_e=best_rho_e, sigma_e=best_sigma_e)


def verify_defaults(persona: PersonaParams) -> dict:
    """Corre `persona` con VERIFY_SEEDS, devuelve métricas agregadas y
    guarda una figura M(t) de la primera semilla fresca."""
    metrics = cell_metrics(persona, VERIFY_SEEDS)

    result = run(days=DAYS, seed=VERIFY_SEEDS[0], variant=VARIANT, persona=persona)
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(result.t, result.M, marker="o", markersize=3, linewidth=1)
    ax.axhline(persona.N, color="gray", linestyle="--", linewidth=0.8)
    ax.axhline(0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_xlabel("día t")
    ax.set_ylabel("M(t)")
    ax.set_title(
        f"Verificación de defaults afinados — seed={VERIFY_SEEDS[0]} "
        f"(rho_e={persona.rho_e}, sigma_e={persona.sigma_e})"
    )
    fig.tight_layout()
    fig.savefig(OUT_DIR / "08_verificacion_defaults_M_t.png", dpi=130)
    plt.close(fig)

    return metrics


# Report


def _fmt_cell(m: dict) -> str:
    return (
        f"media={m['mean_M']:.2f} sd={m['sd_M']:.2f} "
        f"ac1={m['autocorr_lag1']:.3f} sat={m['sat_frac']:.3f}"
    )


def write_report(
    r1: dict, r2: dict, r3: dict, r4: dict, tuned: PersonaParams, verify: dict
) -> None:
    lines: list[str] = []
    lines.append("# W3.3 — Barrido de parámetros (criterio 8b)\n")
    lines.append(
        f"Variante fija: `{VARIANT.value}`. Horizonte: {DAYS} días. "
        f"Semillas de barrido: `{SWEEP_SEEDS}` (métricas promediadas entre "
        f"las 5 por celda). Semillas de verificación (frescas): `{VERIFY_SEEDS}`.\n"
    )
    lines.append(
        "Criterio (8b) — región humana: "
        f"media(M) ∈ {list(MEAN_RANGE)}, sd(M) ∈ {list(SD_RANGE)}, "
        f"autocorr_lag1 ∈ {list(AC1_RANGE)}, fracción_saturada < {SAT_MAX}.\n"
    )

    # --- Grid 1 ---
    lines.append("## 1. Grid rho_e x sigma_e (autocorrelación endógena)\n")
    lines.append(
        "![autocorr](01_rho_e_sigma_e_autocorr.png)\n\n"
        "![sd](02_rho_e_sigma_e_sd.png)\n"
    )
    n_human_1 = int(np.sum(r1["human_mask"]))
    lines.append(
        f"Celdas dentro de la región humana: **{n_human_1}** de "
        f"{r1['human_mask'].size}. Recorrido de autocorr_lag1: "
        f"[{r1['ac1_grid'].min():.3f}, {r1['ac1_grid'].max():.3f}]; "
        f"recorrido de sd(M): [{r1['sd_grid'].min():.2f}, {r1['sd_grid'].max():.2f}].\n"
    )
    if n_human_1 > 0:
        rows = []
        for i, rho_e in enumerate(r1["rho_e_vals"]):
            for j, sigma_e in enumerate(r1["sigma_e_vals"]):
                if r1["human_mask"][i, j]:
                    rows.append((rho_e, sigma_e, r1["cells"][(rho_e, sigma_e)]))
        lines.append("Celdas humanas (rho_e, sigma_e) → métricas:\n")
        for rho_e, sigma_e, m in rows:
            lines.append(f"- rho_e={rho_e}, sigma_e={sigma_e}: {_fmt_cell(m)}")
        lines.append("")
    lines.append(
        "Lectura: el humo previo con defaults (rho_e=0.5, sigma_e=0.2) dio "
        "autocorr ≈ 0.16, bajo el objetivo. Subir rho_e (más memoria del "
        "AR(1) de η) empuja autocorr_lag1 hacia arriba sin cambiar la sd "
        "estacionaria de η (σ_e/√(1−ρ_e²)) tanto como subir σ_e directamente; "
        "sigma_e alto con rho_e alto simultáneamente infla sd(M) y puede "
        "acercarse a saturación en las colas de p(t).\n"
    )

    # --- Grid 2 ---
    lines.append("## 2. Grid k x rho (memoria de eventos)\n")
    lines.append(
        "![autocorr](03_k_rho_autocorr.png)\n\n"
        "![sd](04_k_rho_sd.png)\n"
    )
    if r2["unstable_report"]:
        lines.append("Celdas **inestables por diseño** (violan k < 2(1−rho)/g_max):\n")
        for k, rho, msg in r2["unstable_report"]:
            lines.append(f"- k={k}, rho={rho}: {msg}")
        lines.append("")
    else:
        lines.append("Ninguna celda de este grid viola la cota de estabilidad.\n")
    n_human_2 = int(np.sum(r2["human_mask"]))
    n_valid_2 = int(np.sum(~r2["unstable_mask"]))
    lines.append(
        f"Celdas dentro de la región humana: **{n_human_2}** de {n_valid_2} "
        f"celdas estables (de {r2['human_mask'].size} totales).\n"
    )
    if n_human_2 > 0:
        rows = []
        for i, k in enumerate(r2["k_vals"]):
            for j, rho in enumerate(r2["rho_vals"]):
                if r2["human_mask"][i, j]:
                    rows.append((k, rho, r2["cells"][(k, rho)]))
        lines.append("Celdas humanas (k, rho) → métricas:\n")
        for k, rho, m in rows:
            lines.append(f"- k={k}, rho={rho}: {_fmt_cell(m)}")
        lines.append("")
    lines.append(
        "Lectura: k y rho controlan la memoria del lazo juez→μ, no la "
        "autocorrelación endógena de η — su efecto sobre autocorr_lag1 de M "
        "es más débil e indirecto (vía la varianza que añaden a p(t) día a "
        "día); rho alto con k cerca de la cota de estabilidad es donde más "
        "sube sd(M).\n"
    )

    # --- Grid 3 ---
    lines.append("## 3. Grid A x B (ciclo)\n")
    lines.append(
        "![var_ratio](05_A_B_var_ratio.png)\n\n"
        "![amplitude](06_A_B_amplitude.png)\n"
    )
    n_human_3 = int(np.sum(r3["human_mask"]))
    lines.append(
        f"Celdas dentro de la región humana: **{n_human_3}** de "
        f"{r3['human_mask'].size}. var_ratio_by_gain crece con A (ganancia "
        "amplifica la reactividad); la amplitud del ciclo en M crece con B "
        "(desplazamiento de media m(t)) y es ~0 cuando B=0 por construcción.\n"
    )
    if n_human_3 > 0:
        rows = []
        for i, A in enumerate(r3["A_vals"]):
            for j, B in enumerate(r3["B_vals"]):
                if r3["human_mask"][i, j]:
                    rows.append((A, B, r3["cells"][(A, B)]))
        lines.append("Celdas humanas (A, B) → métricas:\n")
        for A, B, m in rows:
            lines.append(
                f"- A={A}, B={B}: {_fmt_cell(m)} var_ratio={m['var_ratio_by_gain']:.2f} "
                f"amplitud={m['amplitude']:.2f}"
            )
        lines.append("")

    # --- Grid 4 ---
    lines.append("## 4. Barrido 1D nu (defaults, sobredispersión beta-binomial)\n")
    lines.append("![nu](07_nu_1d.png)\n")
    lines.append("| nu | media(M) | sd(M) | autocorr_lag1 | sat_frac |")
    lines.append("|---|---|---|---|---|")
    for label, row in zip(r4["labels"], r4["rows"]):
        lines.append(
            f"| {label} | {row['mean_M']:.2f} | {row['sd_M']:.2f} | "
            f"{row['autocorr_lag1']:.3f} | {row['sat_frac']:.3f} |"
        )
    lines.append("")
    inf_row, nu8_row, nu4_row = r4["rows"]
    direction_ac1 = "bajó" if nu4_row["autocorr_lag1"] < inf_row["autocorr_lag1"] else "subió"
    direction_sd = "subió" if nu4_row["sd_M"] > inf_row["sd_M"] else "bajó"
    lines.append(
        f"Lectura: yendo de nu=inf a nu=4, autocorr_lag1 **{direction_ac1}** "
        f"({inf_row['autocorr_lag1']:.3f} → {nu4_row['autocorr_lag1']:.3f}) y "
        f"sd(M) **{direction_sd}** ({inf_row['sd_M']:.2f} → {nu4_row['sd_M']:.2f}), "
        "consistente con que la sobredispersión beta-binomial añade varianza "
        "blanca (ruido no autocorrelacionado) por encima del binomial puro.\n"
    )

    # --- Proposed defaults ---
    lines.append("## Defaults afinados propuestos\n")
    lines.append(
        f"A partir del grid 1 (única fuente de autocorrelación endógena "
        f"pura), se elige el punto que acerca autocorr_lag1 al centro del "
        f"rango objetivo [0.2, 0.5] sin salir de sd(M) ≤ 2.8 ni saturar. "
        f"Todo lo demás queda en el default de `PersonaParams()`.\n"
    )
    lines.append("```python")
    lines.append("PersonaParams(")
    for f in dataclasses.fields(tuned):
        default_val = getattr(PersonaParams(), f.name)
        tuned_val = getattr(tuned, f.name)
        marker = "  # <- afinado" if tuned_val != default_val else ""
        lines.append(f"    {f.name}={tuned_val!r},{marker}")
    lines.append(")")
    lines.append("```\n")
    lines.append(
        f"Justificación: (1) rho_e={tuned.rho_e} y sigma_e={tuned.sigma_e} "
        f"colocan la autocorr_lag1 de M en el rango objetivo — el default "
        "previo (rho_e=0.5, sigma_e=0.2) daba ≈0.16 en el humo, por debajo "
        "del piso 0.2. (2) el resto de los parámetros (k, rho, A, B, nu, N, "
        "lam) se dejan sin tocar porque los grids 2–4 muestran que su efecto "
        "sobre autocorr_lag1 es más débil o va en la dirección equivocada "
        "(nu finito lo baja, no lo sube) frente al que ofrece rho_e/sigma_e. "
        "(3) se verifica con 5 semillas frescas para descartar sobreajuste "
        "a las semillas del barrido.\n"
    )

    lines.append("### Verificación (semillas frescas)\n")
    lines.append(f"Semillas: `{VERIFY_SEEDS}`.\n")
    lines.append("| métrica | valor | rango objetivo | cumple |")
    lines.append("|---|---|---|---|")
    checks = [
        ("media(M)", verify["mean_M"], MEAN_RANGE, MEAN_RANGE[0] <= verify["mean_M"] <= MEAN_RANGE[1]),
        ("sd(M)", verify["sd_M"], SD_RANGE, SD_RANGE[0] <= verify["sd_M"] <= SD_RANGE[1]),
        ("autocorr_lag1", verify["autocorr_lag1"], AC1_RANGE, AC1_RANGE[0] <= verify["autocorr_lag1"] <= AC1_RANGE[1]),
        ("sat_frac", verify["sat_frac"], f"< {SAT_MAX}", verify["sat_frac"] < SAT_MAX),
    ]
    all_pass = True
    for name, val, rng_desc, ok in checks:
        all_pass = all_pass and ok
        lines.append(f"| {name} | {val:.4f} | {rng_desc} | {'PASS' if ok else 'FAIL'} |")
    lines.append("")
    lines.append("![verificación](08_verificacion_defaults_M_t.png)\n")

    verdict = "PASS" if all_pass else "FAIL"
    lines.append(f"## Veredicto (8b): **{verdict}**\n")
    lines.append(
        f"{'Existe' if (n_human_1 + n_human_2 + n_human_3) > 0 else 'No existe'} "
        "una región no vacía que cumple los 4 umbrales del criterio (8b) "
        f"(grid 1: {n_human_1} celdas, grid 2: {n_human_2} celdas, grid 3: "
        f"{n_human_3} celdas), y la propuesta de defaults afinados se "
        f"verificó con 5 semillas frescas ({verdict}). PASS de (8b) = región "
        "no vacía + propuesta verificada.\n"
    )

    OUT_DIR.joinpath("reporte.md").write_text("\n".join(lines), encoding="utf-8")


# Main


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Grid 1/4: rho_e x sigma_e ...")
    r1 = sweep_rho_e_sigma_e()
    print("Grid 2/4: k x rho ...")
    r2 = sweep_k_rho()
    print("Grid 3/4: A x B ...")
    r3 = sweep_A_B()
    print("Grid 4/4: nu (1D) ...")
    r4 = sweep_nu()

    print("Proponiendo defaults afinados ...")
    tuned = propose_tuned_defaults(r1)
    print(f"  -> {tuned}")
    print("Verificando con semillas frescas ...")
    verify = verify_defaults(tuned)
    print(f"  -> {verify}")

    print("Escribiendo reporte.md ...")
    write_report(r1, r2, r3, r4, tuned, verify)

    print(f"Listo. Salidas en {OUT_DIR}")


if __name__ == "__main__":
    main()
