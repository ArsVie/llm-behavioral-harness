"""A scripted day judge for runtime tests (no model call)."""

from __future__ import annotations

import math

from harness.judge import JudgeResult


def score_schedule(seed: int, day: int) -> float:
    """Scripted score for a day: a smooth seed-phased sinusoid in [-0.75, 0.75]."""
    phase = (seed % 13) / 13.0
    s = 0.62 * math.sin(2.0 * math.pi * (day / 9.0 + phase)) + 0.12 * math.sin(
        2.0 * math.pi * (day / 3.0 + phase * 2.0)
    )
    return float(max(-0.75, min(0.75, s)))


class DeterministicJudge:
    """Scores days in order from ``score_schedule``; days inside
    ``block_start..block_end`` (0-indexed, inclusive) dip by ``dip``."""

    def __init__(self, seed: int, *, block_start: int = 10, block_end: int = 13,
                 dip: float = 0.55):
        self.seed = seed
        self.block_start = block_start
        self.block_end = block_end
        self.dip = dip
        self.n = 0

    def __call__(self, transcript: str, client=None, *, model=None, **kw) -> JudgeResult:
        day = self.n
        self.n += 1
        score = score_schedule(self.seed, day)
        if self.block_start <= day <= self.block_end:
            score = max(-0.85, score - self.dip)
        return JudgeResult(score=score, justification="scripted")
