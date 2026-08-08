"""Figuras estándar de validación (W1.6).

PROPIEDAD: tarea W1.6 (este archivo + tests/test_plots.py). Implementar
contra engine/types.py (CONGELADO).

Reglas congeladas:
  - Backend Agg: `matplotlib.use("Agg")` ANTES de importar pyplot (headless).
  - Cada función recibe `out_dir: Path`, la crea si no existe, escribe UN png
    con nombre DETERMINISTA (patrón indicado en cada docstring) y devuelve la
    ruta escrita.
  - La semilla (SimResult.seed) y la variante van en el título de la figura.
  - Estilo único y sobrio; no depender de estilos externos.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from engine.types import SimResult


def plot_mood_series(result: SimResult, out_dir: Path) -> Path:
    """M(t) con banda de referencia N·p(t) ± sd binomial(N, p(t)).

    Nombre: mood_series_{variant}_s{seed}.png
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 6))
    N = result.params.N
    seed = result.seed
    variant = result.variant.value

    t = result.t
    M = result.M
    p = result.p

    # Banda de referencia: N·p ± sd(binomial)
    sd_binom = np.sqrt(N * p * (1 - p))
    upper = N * p + sd_binom
    lower = N * p - sd_binom

    ax.fill_between(t, lower, upper, alpha=0.2, color="C0", label="N·p(t) ± σ")
    ax.plot(t, N * p, "C0--", alpha=0.5, label="N·p(t)")
    ax.plot(t, M, "o-", linewidth=2, markersize=4, color="C1", label="M(t)")

    ax.set_xlabel("Día")
    ax.set_ylabel(f"M (escala 0..{N})")
    ax.set_title(f"M(t) — {variant} · seed {seed}")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    ax.set_ylim([-0.5, N + 0.5])

    png_path = out_dir / f"mood_series_{variant}_s{seed}.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path


def plot_mg(result: SimResult, out_dir: Path) -> Path:
    """m(t) y g(t) (dos ejes o dos paneles). Nombre: mg_{variant}_s{seed}.png"""
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    seed = result.seed
    variant = result.variant.value

    t = result.t
    m = result.m
    g = result.g

    ax1.plot(t, m, "o-", linewidth=2, markersize=4, color="C0")
    ax1.set_ylabel("m(t)")
    ax1.set_title(f"m(t) y g(t) — {variant} · seed {seed}")
    ax1.grid(True, alpha=0.3)

    ax2.plot(t, g, "o-", linewidth=2, markersize=4, color="C1")
    ax2.set_xlabel("Día")
    ax2.set_ylabel("g(t)")
    ax2.grid(True, alpha=0.3)

    png_path = out_dir / f"mg_{variant}_s{seed}.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path


def plot_mood_hist(result: SimResult, out_dir: Path) -> Path:
    """Histograma de M (bins enteros 0..N). Nombre: mood_hist_{variant}_s{seed}.png"""
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 6))
    N = result.params.N
    seed = result.seed
    variant = result.variant.value

    M = result.M

    # Bins centrados en enteros 0..N
    bins = np.arange(-0.5, N + 1.5, 1.0)
    ax.hist(M, bins=bins, edgecolor="black", alpha=0.7, color="C0")
    ax.set_xlabel(f"M (escala 0..{N})")
    ax.set_ylabel("Frecuencia")
    ax.set_title(f"Histograma de M — {variant} · seed {seed}")
    ax.grid(True, alpha=0.3, axis="y")
    ax.set_xticks(np.arange(0, N + 1))

    png_path = out_dir / f"mood_hist_{variant}_s{seed}.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path


def plot_mu_eta(result: SimResult, out_dir: Path) -> Path:
    """μ(t) y η(t). Nombre: mu_eta_{variant}_s{seed}.png"""
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 6))
    seed = result.seed
    variant = result.variant.value

    t = result.t
    mu = result.mu
    eta = result.eta

    ax.plot(t, mu, "o-", linewidth=2, markersize=4, label="μ(t)", color="C0")
    ax.plot(t, eta, "s-", linewidth=2, markersize=4, label="η(t)", color="C1")

    ax.set_xlabel("Día")
    ax.set_ylabel("Amplitud")
    ax.set_title(f"μ(t) y η(t) — {variant} · seed {seed}")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)

    png_path = out_dir / f"mu_eta_{variant}_s{seed}.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path


def plot_hourly_events(
    times_h: np.ndarray,
    envelope_fn: Callable[[float], float] | None,
    out_dir: Path,
    tag: str,
) -> Path:
    """Histograma horario (24 bins) de eventos + envolvente escalada si se da.

    Nombre: hourly_events_{tag}.png (el tag debe incluir la semilla).
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 6))

    # Histograma horario: 24 bins, cada uno representa una hora del día
    hours = times_h % 24
    bins = np.linspace(0, 24, 25)
    counts, _ = np.histogram(hours, bins=bins)

    ax.bar(np.arange(24), counts, width=1.0, edgecolor="black", alpha=0.7, color="C0")

    # Envolvente escalada si se proporciona
    if envelope_fn is not None:
        # Malla fina para evaluar la envolvente
        x_fine = np.linspace(0, 24, 500)
        envelope_vals = np.array([envelope_fn(h) for h in x_fine])

        # Escalar a altura máxima del histograma
        max_count = np.max(counts) if len(counts) > 0 else 1
        if np.max(envelope_vals) > 0:
            envelope_vals = envelope_vals / np.max(envelope_vals) * max_count

        ax.plot(x_fine, envelope_vals, "r-", linewidth=2, label="Envolvente")
        ax.legend(loc="best")

    ax.set_xlabel("Hora del día")
    ax.set_ylabel("Eventos")
    ax.set_title(f"Eventos por hora — {tag}")
    ax.set_xticks(np.arange(0, 25, 2))
    ax.grid(True, alpha=0.3, axis="y")

    png_path = out_dir / f"hourly_events_{tag}.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path


def plot_variant_comparison(
    results: dict[str, SimResult], out_dir: Path, tag: str
) -> Path:
    """Series M(t) superpuestas por variante (misma semilla), + medias.

    `results`: {etiqueta: SimResult}. Nombre: variants_{tag}.png
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(12, 7))

    colors = plt.cm.tab10(np.linspace(0, 1, len(results)))

    for (label, result), color in zip(results.items(), colors):
        t = result.t
        M = result.M
        mean_M = np.mean(M)

        ax.plot(t, M, "o-", linewidth=2, markersize=4, label=label, color=color, alpha=0.7)
        ax.axhline(mean_M, linestyle="--", color=color, alpha=0.5)

    ax.set_xlabel("Día")
    ax.set_ylabel("M(t)")
    ax.set_title(f"Comparación de variantes — {tag}")
    ax.legend(loc="best", fontsize=9)
    ax.grid(True, alpha=0.3)

    png_path = out_dir / f"variants_{tag}.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path
