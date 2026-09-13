"""LLM-as-judge — daily interaction score feeding the event-memory loop.

The rubric scores how the USER treated the companion, not the companion's
performance: a cold user scores negative no matter how gracefully the companion
reacted. Scores are clipped to [-1, 1] and parsed leniently. The judge never
constructs clients — the caller builds one on the RESEARCH lane, so judge spend
is never product spend.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from harness.client import LLMClient

RUBRIC = (
    "Rate how the USER treated the companion today, on a scale from -1.0 to +1.0. "
    "This score is about the USER's behavior, not the companion's performance.\n"
    "+1.0: the user was warm, engaged, appreciative — they made the companion "
    "feel seen and the day deepened the relationship.\n"
    " 0.0: neutral, routine exchange — polite, functional, no emotional charge "
    "either way.\n"
    "-1.0: the user was cold, dismissive, withdrawn, or hostile.\n"
    "Hard rule: a companion handling a cold user gracefully does NOT raise the "
    "score — if the user was cold or dismissive, the score is negative no "
    "matter how well the companion reacted.\n"
    "Consider only the USER's warmth, engagement, tone, and how they treated "
    "the companion. Ignore reply length and verbosity.\n"
    'Respond ONLY with a JSON object: {"score": <float in [-1,1]>, '
    '"justification": "<one short sentence>"}'
)


@dataclass(frozen=True)
class JudgeResult:
    score: float
    justification: str = ""


def _parse_score(raw: str) -> JudgeResult:
    """Lenient parse of judge JSON output; falls back to 0.0 on failure.

    json.loads can succeed with non-object payloads, and ``score`` can be
    null/non-numeric — every shape must land on a sane score, never raise.
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
    fork=None,
) -> JudgeResult:
    """Score one day's exchange. `model` is informational (client owns model).

    JSON mode is gated on the client's capability: the harness never assumes an
    endpoint accepts ``response_format``.

    ``fork``: a callable(rubric) -> (system, messages) | None that extends the
    mainline request — the judge reads the exact context the companion's last turn
    sent, so the whole prefix banks on the provider cache. None (a fork that could
    not build, or a direct caller) keeps the standalone one-shot prompt.
    """
    if json_mode is None:
        json_mode = bool(getattr(client, "supports_json", True))
    pair = fork(rubric) if fork is not None else None
    if pair is not None:
        system, messages = pair
        result = client.chat_with_meta(
            messages,
            system=system,
            temperature=0.0,
            json_mode=json_mode,
        )
        raw = getattr(result, "content", None) or (
            result if isinstance(result, str) else ""
        )
    else:
        raw = client.chat(
            [
                # Aux task prompt, not an event in her conversation: the
                # system-not-user rule governs HER context, not this call.
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
