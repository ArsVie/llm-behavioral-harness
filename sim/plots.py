"""Standard validation figures (W1.6).

OWNERSHIP: task W1.6 (this file + tests/test_plots.py). Implement
against engine/types.py (FROZEN).

Frozen rules:
  - Agg backend: `matplotlib.use("Agg")` BEFORE importing pyplot (headless).
  - Each function receives `out_dir: Path`, creates it if missing, writes ONE
    png with a DETERMINISTIC name (pattern given in each docstring), and returns
    the written path.
  - The seed (SimResult.seed) and the variant go in the figure title.
  - Single, sober style; do not depend on external styles.
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
    """M(t) with a reference band N·p(t) ± binomial sd(N, p(t)).

    Name: mood_series_{variant}_s{seed}.png
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 6))
    N = result.params.N
    seed = result.seed
    variant = result.variant.value

    t = result.t
    M = result.M
    p = result.p

    # Reference band: N·p ± sd(binomial)
    sd_binom = np.sqrt(N * p * (1 - p))
    upper = N * p + sd_binom
    lower = N * p - sd_binom

    ax.fill_between(t, lower, upper, alpha=0.2, color="C0", label="N·p(t) ± σ")
    ax.plot(t, N * p, "C0--", alpha=0.5, label="N·p(t)")
    ax.plot(t, M, "o-", linewidth=2, markersize=4, color="C1", label="M(t)")

    ax.set_xlabel("Day")
    ax.set_ylabel(f"M (scale 0..{N})")
    ax.set_title(f"M(t) — {variant} · seed {seed}")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    ax.set_ylim([-0.5, N + 0.5])

    png_path = out_dir / f"mood_series_{variant}_s{seed}.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path


def plot_mg(result: SimResult, out_dir: Path) -> Path:
    """m(t) and g(t) (two axes or two panels). Name: mg_{variant}_s{seed}.png"""
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    seed = result.seed
    variant = result.variant.value

    t = result.t
    m = result.m
    g = result.g

    ax1.plot(t, m, "o-", linewidth=2, markersize=4, color="C0")
    ax1.set_ylabel("m(t)")
    ax1.set_title(f"m(t) and g(t) — {variant} · seed {seed}")
    ax1.grid(True, alpha=0.3)

    ax2.plot(t, g, "o-", linewidth=2, markersize=4, color="C1")
    ax2.set_xlabel("Day")
    ax2.set_ylabel("g(t)")
    ax2.grid(True, alpha=0.3)

    png_path = out_dir / f"mg_{variant}_s{seed}.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path


def plot_mood_hist(result: SimResult, out_dir: Path) -> Path:
    """Histogram of M (integer bins 0..N). Name: mood_hist_{variant}_s{seed}.png"""
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 6))
    N = result.params.N
    seed = result.seed
    variant = result.variant.value

    M = result.M

    # Bins centered on the integers 0..N
    bins = np.arange(-0.5, N + 1.5, 1.0)
    ax.hist(M, bins=bins, edgecolor="black", alpha=0.7, color="C0")
    ax.set_xlabel(f"M (scale 0..{N})")
    ax.set_ylabel("Frequency")
    ax.set_title(f"Histogram of M — {variant} · seed {seed}")
    ax.grid(True, alpha=0.3, axis="y")
    ax.set_xticks(np.arange(0, N + 1))

    png_path = out_dir / f"mood_hist_{variant}_s{seed}.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path


def plot_mu_eta(result: SimResult, out_dir: Path) -> Path:
    """μ(t) and η(t). Name: mu_eta_{variant}_s{seed}.png"""
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 6))
    seed = result.seed
    variant = result.variant.value

    t = result.t
    mu = result.mu
    eta = result.eta

    ax.plot(t, mu, "o-", linewidth=2, markersize=4, label="μ(t)", color="C0")
    ax.plot(t, eta, "s-", linewidth=2, markersize=4, label="η(t)", color="C1")

    ax.set_xlabel("Day")
    ax.set_ylabel("Amplitude")
    ax.set_title(f"μ(t) and η(t) — {variant} · seed {seed}")
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
    """Hourly histogram (24 bins) of events + scaled envelope if given.

    Name: hourly_events_{tag}.png (the tag must include the seed).
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 6))

    # Hourly histogram: 24 bins, each one represents an hour of the day
    hours = times_h % 24
    bins = np.linspace(0, 24, 25)
    counts, _ = np.histogram(hours, bins=bins)

    ax.bar(np.arange(24), counts, width=1.0, edgecolor="black", alpha=0.7, color="C0")

    # Scaled envelope if provided
    if envelope_fn is not None:
        # Fine grid to evaluate the envelope
        x_fine = np.linspace(0, 24, 500)
        envelope_vals = np.array([envelope_fn(h) for h in x_fine])

        # Scale to the maximum height of the histogram
        max_count = np.max(counts) if len(counts) > 0 else 1
        if np.max(envelope_vals) > 0:
            envelope_vals = envelope_vals / np.max(envelope_vals) * max_count

        ax.plot(x_fine, envelope_vals, "r-", linewidth=2, label="Envelope")
        ax.legend(loc="best")

    ax.set_xlabel("Hour of the day")
    ax.set_ylabel("Events")
    ax.set_title(f"Events per hour — {tag}")
    ax.set_xticks(np.arange(0, 25, 2))
    ax.grid(True, alpha=0.3, axis="y")

    png_path = out_dir / f"hourly_events_{tag}.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path


def plot_variant_comparison(
    results: dict[str, SimResult], out_dir: Path, tag: str
) -> Path:
    """M(t) series overlaid per variant (same seed), + means.

    `results`: {label: SimResult}. Name: variants_{tag}.png
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

    ax.set_xlabel("Day")
    ax.set_ylabel("M(t)")
    ax.set_title(f"Variant comparison — {tag}")
    ax.legend(loc="best", fontsize=9)
    ax.grid(True, alpha=0.3)

    png_path = out_dir / f"variants_{tag}.png"
    fig.savefig(png_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return png_path
