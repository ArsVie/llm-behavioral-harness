"""LLM-as-judge — daily interaction score feeding the event-memory loop (W-E1).

Construct (per research/06 §6b + advisor review 2026-08-08): the rubric
defines what `mu` measures — "quality of the interaction from the COMPANION's
perspective" (connection quality + authenticity + relational progression),
NOT raw user satisfaction. The judge is a NOISY SENSOR in the only closed
loop of the system, so:

  - anchored scale, JSON output, temperature 0;
  - `score_neutral` stays 0.0 (checkpoint decision 2026-07-03: mild positive
    bias is desirable);
  - SHADOW MODE by default: scores are recorded but do NOT touch `mu` until
    the judge is calibrated (advisor ordering: shadow → calibrate → enable);
  - the feedback judge is NEVER used as the ablation evaluator (separate
    evaluation path in experiments/e2e_ablation.py).

Scores are clipped to [-1, 1]; the rubric output is parsed leniently
(JSON object with `score` and `justification`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from harness.client import LLMClient

RUBRIC = (
    "Rate the quality of this interaction from the COMPANION's perspective, "
    "on a scale from -1.0 to +1.0.\n"
    "+1.0: deeply connected day — the user engaged warmly, the companion felt "
    "seen, the exchange deepened the relationship.\n"
    " 0.0: neutral, ordinary exchange — polite, functional, no emotional "
    "charge either way.\n"
    "-1.0: actively harmful day — conflict, dismissal, coldness, or the user "
    "pushed the companion away.\n"
    "Consider: user warmth and engagement, how authentically the companion "
    "held their own voice, and whether the day moved the relationship "
    "forward. Ignore reply length and verbosity.\n"
    'Respond ONLY with a JSON object: {"score": <float in [-1,1]>, '
    '"justification": "<one short sentence>"}'
)


@dataclass(frozen=True)
class JudgeResult:
    score: float
    justification: str = ""


def _parse_score(raw: str) -> JudgeResult:
    """Lenient parse of judge JSON output; falls back to 0.0 on failure.

    Review fix #2: json.loads can succeed with non-object payloads
    (strings, arrays, bare numbers) and `score` can be null/non-numeric —
    every shape must land on a sane score, never raise.
    """
    text = raw.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        try:
            score = float(payload.get("score", 0.0))
        except (TypeError, ValueError):
            return JudgeResult(score=0.0, justification="unparseable judge output")
        justification = str(payload.get("justification", ""))
        return JudgeResult(score=max(-1.0, min(1.0, score)), justification=justification)
    # Non-JSON or valid JSON that is not an object: regex for a bare number.
    match = re.search(r"[-+]?\d*\.?\d+", text)
    if match is None:
        return JudgeResult(score=0.0, justification="unparseable judge output")
    score = float(match.group(0))
    return JudgeResult(score=max(-1.0, min(1.0, score)))


def judge_day(
    transcript: str,
    client: LLMClient,
    *,
    model: str | None = None,
    rubric: str = RUBRIC,
    json_mode: bool | None = None,
) -> JudgeResult:
    """Score one day's exchange. `model` is informational (client owns model).

    JSON mode is gated on the client's capability (review fix #4): the
    harness never assumes an endpoint accepts `response_format`.
    """
    if json_mode is None:
        json_mode = bool(getattr(client, "supports_json", True))
    raw = client.chat(
        [
            {
                "role": "user",
                "content": f"{rubric}\n\nTranscript:\n{transcript}",
            }
        ],
        system="You are a careful interaction judge. Score precisely.",
        temperature=0.0,
        json_mode=json_mode,
    )
    return _parse_score(raw)


class ScriptedJudge:
    """Deterministic judge for tests: returns a fixed score (+ justification)."""

    def __init__(self, score: float = 0.5, justification: str = "scripted"):
        self.score = max(-1.0, min(1.0, score))
        self.justification = justification

    def judge_day(
        self,
        transcript: str,
        client: LLMClient | None = None,
        **kwargs,
    ) -> JudgeResult:
        return JudgeResult(score=self.score, justification=self.justification)
