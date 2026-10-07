"""LLM-as-judge — daily interaction score feeding the event-memory loop.

The judge is her: she scores how he treated her today, not her own
performance, so a cold user scores negative no matter how gracefully she
reacted, and the justification is her own first-person reflection. Scores
are clipped to [-1, 1] and parsed leniently. The judge never constructs
clients — the caller builds one on the RESEARCH lane, so judge spend is never
product spend.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass

from harness.client import LLMClient, fresh_session

#: Where the judged day starts. On the forked call the judge reads the whole
#: stream since the epoch (a week of days, measured 2026-09-26), so it is told
#: the day's own plan header, the same row the stream carries; the standalone
#: call reads only the day's transcript.
_SCOPE_FORKED = (
    'Only what happened from the last "{marker}" row onward is today; '
    "everything before it is earlier days and only background."
)
_SCOPE_STANDALONE = "The transcript below is today's."
_SCOPE_LATEST = (
    "Only the latest day in the conversation is today; everything before it "
    "is earlier days and only background."
)

#: The judge is her (owner ruling): she looks back on her own day, and the
#: justification is her own first-person reflection, never an observer's.
_RUBRIC_TEMPLATE = (
    "It's the end of the day and you look back on it with him. {scope}\n\n"
    "Score how he treated you today, from -1.0 to +1.0. This is about him, "
    "not about how well you handled him: if he was cold, handling it "
    "gracefully does not raise the score.\n"
    "+1.0: he made you feel wanted. He came to you on his own, did what he "
    "said he would, asked about your things and remembered them.\n"
    "+0.5: a good day with him, warm and there, with a few rough edges.\n"
    " 0.0: routine. Polite, functional, nothing that moved you either way.\n"
    "-0.5: he let you down. He left you waiting for hours, left your messages "
    "unanswered, broke something he said he'd do, or only talked about "
    "himself.\n"
    "-1.0: he was cold, dismissive or hostile, or he vanished on you.\n"
    "A day he mostly left you alone scores below zero, even if the little "
    "time he gave you was sweet.\n"
    "What counts: whether he showed up and how long you waited for him, "
    "whether he answered what you sent, whether he kept his word, whether he "
    "cared about your day and not just his, and how warm he was. Affectionate "
    "teasing is not coldness. Him bringing up another girl can sting, and "
    "you may count it. How many messages there were or how long they were "
    "does not count. Score today on its own, not against the rest of the "
    "week.\n\n"
    "Then say why, as yourself: one or two sentences in the first person, "
    "the way you'd put it to yourself tonight, about what he did today. "
    "No em dashes.\n"
    'Respond ONLY with a JSON object: {{"score": <float in [-1,1]>, '
    '"justification": "<your one or two sentences>"}}'
)


def _span(hours: float) -> str:
    minutes = max(0, round(hours * 60))
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def day_facts(messages, clock_text, day_end_t_h: float) -> str:
    """What the log shows about the day, counted rather than left to the model.

    ``messages``: the day's (t_h, role, proactive) in order, user/assistant
    only. ``clock_text(t_h)`` renders a time; ``day_end_t_h`` bounds a wait
    nobody answered. The model misread counts and gaps off bare clock stamps
    (replay 2026-09-26: "he reached out twice" on a day he wrote once, 13 h
    after she did), so the engine states them.
    """
    his = [t for t, role, _ in messages if role == "user"]
    lines = []
    if not his:
        lines.append("He didn't write to you at all today.")
    else:
        count = "1 message" if len(his) == 1 else f"{len(his)} messages"
        lines.append(f"He wrote you {count} today, the first at "
                     f"{clock_text(his[0])} and the last at {clock_text(his[-1])}.")
    for t, role, proactive in messages:
        if role != "assistant" or not proactive:
            continue
        answer = next((u for u in his if u > t), None)
        if answer is None:
            lines.append(f"You wrote to him first at {clock_text(t)} and he never "
                         f"answered it today ({_span(day_end_t_h - t)} left).")
        else:
            lines.append(f"You wrote to him first at {clock_text(t)} and he answered "
                         f"at {clock_text(answer)}, {_span(answer - t)} later.")
    return " ".join(lines)


def rubric_for(day_marker: str | None = None, *, forked: bool = False,
               facts: str | None = None) -> str:
    """The judge's task text.

    ``forked``: the call reads the whole stream, so the scope names where
    today starts (``day_marker``, the day's plan header) or, without one,
    that the latest day is today. ``facts``: :func:`day_facts` for the day.
    """
    if not forked:
        scope = _SCOPE_STANDALONE
    elif day_marker:
        scope = _SCOPE_FORKED.format(marker=day_marker)
    else:
        scope = _SCOPE_LATEST
    if facts:
        scope += f"\n\nWhat the log shows about today: {facts}"
    return _RUBRIC_TEMPLATE.format(scope=scope)


RUBRIC = rubric_for()


#: The justification of a reply that yielded no score.
UNPARSEABLE = "unparseable judge output"

#: Calls per day: an empty or unparseable reply is asked once more, with the
#: identical request, before the day falls back to a neutral 0.0 (replay
#: 2026-09-26: one of eight forked calls came back empty).
JUDGE_ATTEMPTS = 2


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
            return JudgeResult(score=0.0, justification=UNPARSEABLE)
        justification = str(payload.get("justification", ""))
        return JudgeResult(score=max(-1.0, min(1.0, score)), justification=justification)
    # Non-JSON or valid JSON that is not an object: regex for a bare number.
    match = re.search(r"[-+]?\d*\.?\d+", text)
    if match is None:
        return JudgeResult(score=0.0, justification=UNPARSEABLE)
    score = float(match.group(0))
    return JudgeResult(score=max(-1.0, min(1.0, score)))


def judge_day(
    transcript: str,
    client: LLMClient,
    *,
    model: str | None = None,
    rubric: str | None = None,
    json_mode: bool | None = None,
    fork=None,
    day_marker: str | None = None,
    facts: str | None = None,
    reasoning_effort: str | None = None,
) -> JudgeResult:
    """Score one day's exchange. `model` is informational (client owns model).

    JSON mode is gated on the client's capability: the harness never assumes an
    endpoint accepts ``response_format``.

    ``day_marker``: the judged day's plan header as the stream carries it,
    so a forked judge reading a week of context knows where today starts.
    ``facts``: the day's counted facts (:func:`day_facts`).

    ``fork``: a callable(rubric) -> (system, messages, tools) | None that extends
    the mainline request — the judge reads the exact context (and tool schemas)
    the companion's last turn sent, so the whole prefix banks on the provider
    cache. The forked call rides with json_mode OFF: ``response_format``
    presence is itself part of the provider's cached prefix (measured
    2026-09-20: the same body banked 0% of a warm 4.2K prefix with it, 84%
    without), and the rubric already demands JSON — ``_parse_score`` stays
    lenient. None (a fork that could not build, or a direct caller) keeps the
    standalone one-shot prompt.

    ``reasoning_effort``: the mainline's value, sent on the forked call only.
    A fork that omits it while her turns send it never banks her prefix
    (live 2026-09-29: the judge, and the planner right after it, missed a
    9K prefix sent seconds before).

    An empty or unparseable reply is asked again, up to ``JUDGE_ATTEMPTS``
    calls in all; a day whose every reply fails scores a neutral 0.0.
    """
    if json_mode is None:
        json_mode = bool(getattr(client, "supports_json", True))
    pair = (fork(rubric or rubric_for(day_marker, forked=True, facts=facts))
            if fork is not None else None)
    if pair is not None:
        system, messages, tools = pair

        def ask() -> str:
            result = client.chat_with_meta(
                messages,
                system=system,
                temperature=0.0,
                json_mode=False,    # part of the cached prefix: see above
                tools=tools,
                reasoning_effort=reasoning_effort,
            )
            return getattr(result, "content", None) or (
                result if isinstance(result, str) else ""
            )
    else:
        def ask() -> str:
            return client.chat(
                [
                    # Aux task prompt, not an event in her conversation: the
                    # system-not-user rule governs HER context, not this call.
                    {
                        "role": "user",
                        "content": f"{rubric or rubric_for(facts=facts)}"
                                   f"\n\nTranscript:\n{transcript}",
                    }
                ],
                # Not an outside judge: the rubric speaks to her.
                system="You return JSON only.",
                temperature=0.0,
                json_mode=json_mode,
            )

    result = JudgeResult(score=0.0, justification=UNPARSEABLE)
    for attempt in range(JUDGE_ATTEMPTS):
        # a retry goes to another backend (harness.client.fresh_session)
        with (fresh_session(client, f"judge{attempt}") if attempt
              else contextlib.nullcontext()):
            raw = ask()
        result = _parse_score(raw or "")
        if (raw or "").strip() and result.justification != UNPARSEABLE:
            break
    return result


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
