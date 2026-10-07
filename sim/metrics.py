"""Acceptance metrics — pure array → float functions (W1.5).

OWNERSHIP: task W1.5 (this file + tests/test_metrics.py). Implement
against engine/types.py (FROZEN). No I/O, no own RNG, no matplotlib.
Additional private helpers are allowed inside this file.
"""
from __future__ import annotations

from typing import Callable

import numpy as np


def mean_sd(x: np.ndarray) -> tuple[float, float]:
    """(mean, sample sd ddof=1) of a series."""
    x = np.asarray(x, dtype=float)
    return float(np.mean(x)), float(np.std(x, ddof=1))


def autocorr_lag1(x: np.ndarray) -> float:
    """Lag-1 autocorrelation (Pearson between x[:-1] and x[1:]).

    Criterion (6): for a human M(t) it is expected ∈ [0.2, 0.5].
    """
    x = np.asarray(x, dtype=float)
    a, b = x[:-1], x[1:]
    return float(np.corrcoef(a, b)[0, 1])


def var_ratio_by_gain(M: np.ndarray, g: np.ndarray, q: float = 0.25) -> float:
    """var(M | g in the upper quartile) / var(M | g in the lower quartile).

    Criterion (4): > 1 when the gain g amplifies reactivity.
    `q` defines the quantiles (default quartiles: g <= Q(q) vs g >= Q(1−q)).
    """
    M = np.asarray(M, dtype=float)
    g = np.asarray(g, dtype=float)
    lo_thresh = np.quantile(g, q)
    hi_thresh = np.quantile(g, 1.0 - q)
    M_lo = M[g <= lo_thresh]
    M_hi = M[g >= hi_thresh]
    var_lo = np.var(M_lo, ddof=1)
    var_hi = np.var(M_hi, ddof=1)
    return float(var_hi / var_lo)


def reversion_days(
    mu: np.ndarray, t_shock: int, baseline: float = 0.0
) -> float:
    """Reversion (e-folding) days of μ after a shock at t_shock.

    Let t_peak = argmax_{t>=t_shock} |mu[t] − baseline|. Returns the first
    (t − t_peak) with |mu[t] − baseline| <= |mu[t_peak] − baseline|/e, or inf if
    it never happens within the series. AR(1) theory: ≈ −1/ln(ρ) days
    (criterion (5): ~1/(1−ρ)).
    """
    mu = np.asarray(mu, dtype=float)
    dev = np.abs(mu - baseline)

    tail = dev[t_shock:]
    peak_offset = int(np.argmax(tail))
    t_peak = t_shock + peak_offset
    peak_val = dev[t_peak]

    threshold = peak_val / np.e
    for t in range(t_peak, len(mu)):
        if dev[t] <= threshold:
            return float(t - t_peak)
    return float("inf")


def daily_rate(times_h: np.ndarray, horizon_days: float) -> float:
    """Events per day: len(times_h)/horizon_days. Criterion (7): ∈ [1, 3]."""
    times_h = np.asarray(times_h, dtype=float)
    return float(len(times_h) / horizon_days)


def gap_stats(times_h: np.ndarray, bin_width_h: float = 1.0) -> dict[str, float]:
    """Statistics of the gaps between consecutive events (hours).

    Keys: "mean_h", "median_h", "mode_h" (center of the most populated bin of
    the histogram with width bin_width_h), "cv" (sd/mean), "burstiness"
    ((sd−mean)/(sd+mean) ∈ [−1, 1]; 0 = Poisson).
    Criterion (7): increasing hazard ⇒ mode_h > 0.
    """
    times_h = np.asarray(times_h, dtype=float)
    times_sorted = np.sort(times_h)
    gaps = np.diff(times_sorted)

    mean_h = float(np.mean(gaps))
    median_h = float(np.median(gaps))
    sd_h = float(np.std(gaps, ddof=1))
    cv = sd_h / mean_h
    burstiness = (sd_h - mean_h) / (sd_h + mean_h)

    n_bins = int(np.ceil((gaps.max() - gaps.min()) / bin_width_h)) if gaps.max() > gaps.min() else 1
    n_bins = max(n_bins, 1)
    bin_edges = gaps.min() + np.arange(n_bins + 1) * bin_width_h
    counts, edges = np.histogram(gaps, bins=bin_edges)
    top_bin = int(np.argmax(counts))
    mode_h = float((edges[top_bin] + edges[top_bin + 1]) / 2.0)

    return {
        "mean_h": mean_h,
        "median_h": median_h,
        "mode_h": mode_h,
        "cv": cv,
        "burstiness": burstiness,
    }


def hourly_histogram(times_h: np.ndarray, bins: int = 24) -> np.ndarray:
    """Event counts per local hour of the day (t % 24), shape (bins,)."""
    times_h = np.asarray(times_h, dtype=float)
    local_hours = times_h % 24.0
    counts, _ = np.histogram(local_hours, bins=bins, range=(0.0, 24.0))
    return counts


def envelope_violations(
    times_h: np.ndarray,
    envelope_fn: Callable[[float], float],
    eps: float = 1e-9,
) -> int:
    """Number of events at hours where envelope_fn(t % 24) < eps.

    Criterion (7): must be 0 (nothing in quiet hours).
    """
    times_h = np.asarray(times_h, dtype=float)
    local_hours = times_h % 24.0
    violations = sum(1 for h in local_hours if envelope_fn(float(h)) < eps)
    return int(violations)
