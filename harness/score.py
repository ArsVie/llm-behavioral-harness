"""Score sources shared by drivers and the harness (review fix #5).

`synthetic_score` belongs to the product layer, not to a single experiment
driver; `sim.run_daily` re-exports it for backward compatibility and to keep
its own CLI unchanged.
"""

from __future__ import annotations

import numpy as np

#: sd of the synthetic score noise (frozen in the Phase-1 plan).
SCORE_NOISE_SD = 0.2


def synthetic_score(
    M: int, N: int, rng: np.random.Generator, override: float | None = None
) -> float:
    """Synthetic daily score; with `override` returns that value clipped.

    score = clip(2·(M/N − 0.5) + Normal(0, SCORE_NOISE_SD), −1, 1).
    Consumes one RNG draw (replay contract: after mood.step, before
    mood.step_endogenous on the same day generator).
    """
    if override is not None:
        return float(np.clip(override, -1.0, 1.0))
    raw = 2.0 * (M / N - 0.5) + rng.normal(0.0, SCORE_NOISE_SD)
    return float(np.clip(raw, -1.0, 1.0))
