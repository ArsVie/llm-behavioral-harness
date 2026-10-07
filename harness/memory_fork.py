"""End-of-day memory: she looks back on the day and says what she learned
about him. The third fork (owner ruling 2026-09-26).

The call sees only the day it remembers (owner ruling 2026-10-03, "let's prune
the memory fork"): her stable system and tool menu, so that much still banks
on the cache, then that day's messages from both of them and the event parts
of its system rows (:func:`day_view`). No state cards, memory recalls, plans,
style rule, decisions or earlier days. Its output is memory rows, never
stream text.

Nothing the model says becomes memory on its word alone: every item must
quote his own words from one of today's messages, and the quote is what
links the item to its source turn. An item whose quote is not in any of his
messages today is dropped.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass

from harness.assembler import AFFECTIVE_HEADER, is_state_update
from harness.client import fresh_session
from harness.prompts import ACTIVITY_HEADER, TURN_STYLE
from harness.summarization_facts import _FACT_CATEGORY, _Fact

#: What the model calls a kind -> the fact kind the memory layer stores.
#: ``reminder`` is not a fact about him: it becomes a callback episode.
KINDS = {
    "fact": "user_fact",
    "preference": "preference",
    "relationship": "relationship",
    "reminder": "callback",
    "moment": "moment",
}
_KEY_PREFIX = {"user_fact": "user", "preference": "preference",
               "relationship": "relationship"}

#: At most this many items a day are kept; the model is asked for fewer.
MAX_ITEMS = 8

_TASK_TEMPLATE = (
    "It's the end of the day and you think back on it. {scope}\n\n"
    "What did you learn about him today that you'd want to remember later? "
    "Only what he said himself about himself, his life, what he likes, the "
    "people around him, how things are between you, or something he asked "
    "you to remember. Not what you said, not guesses, not small talk. Most "
    "days have one to four things; a day with nothing new is an empty "
    "list.\n\n"
    "You will read these on later days, so never write \"today\", "
    "\"tonight\", \"tomorrow\" or \"yesterday\": name the day instead "
    "(\"on {today}\"). A plan for a particular day or time is a reminder, "
    "not a fact or a preference: facts and preferences are what still holds "
    "after the day.\n\n"
    "{known}"
    "For each thing:\n"
    '- "kind": "fact" (about him or his life), "preference" (what he likes '
    'or doesn\'t), "relationship" (how things are between you), "reminder" '
    "(a plan, or something he wants you to remember or bring up later), or "
    '"moment" (something that happened between you two that you\'d want to '
    "remember: a date, a fight, a first time).\n"
    '- "key": a short lowercase slug naming what it is about, like '
    '"job" or "sister_name". Reuse the key of something you already know '
    "when today changes it.\n"
    '- "fact": one short sentence, as you\'d remember it; for a moment, '
    "what happened.\n"
    '- "quote": his exact words from today that tell you this, copied '
    "character for character.\n"
    '- "weight": 0.0 to 1.0, how much it matters to you.\n\n'
    'Then "summary": two or three sentences on how the day went between '
    "you, as you'd remember it, with the day named.\n\n"
    'Respond ONLY with a JSON object: {{"summary": "...", "items": [{{"kind": '
    '"...", "key": "...", "fact": "...", "quote": "...", "weight": 0.5}}]}}'
)

_KNOWN_HEADER = (
    "What you already know about him (key: what you know):\n{lines}\n\n"
)

#: The scope when the fork sees only the day (``day_view``).
_SCOPE_DAY = "Everything above is from today{named}."

#: The paragraphs of a state card or update that are events, not her state:
#: what she is doing, and what she is reaching out about.
_EVENT_LEADS = (ACTIVITY_HEADER, "You are reaching out first.")


def _event_part(text: str) -> str | None:
    """The event paragraphs of a state card or update row, or None."""
    paras = text.split("\n\n")
    if is_state_update(text):
        paras = paras[1:]
    kept = [p for p in paras if p.startswith(_EVENT_LEADS)]
    return "\n\n".join(kept) if kept else None


def day_view(rows: list[dict], *, plan_header: str | None = None) -> list[dict]:
    """The rows the memory fork reads: his and her messages, and the events
    in the system rows. State cards and updates keep only their activity and
    reaching-out paragraphs; the style rule and the day plan are dropped, and
    so are tool rows. Other system rows (a call starting or ending) stay."""
    out: list[dict] = []
    for row in rows:
        role = row.get("role")
        text = str(row.get("content") or "")
        if role in ("user", "assistant"):
            if role == "assistant" and row.get("tool_calls"):
                continue
            out.append(row)
        elif role == "system":
            if text == TURN_STYLE or (plan_header and text.startswith(plan_header)):
                continue
            if text.startswith(AFFECTIVE_HEADER) or is_state_update(text):
                part = _event_part(text)
                if part is None:
                    continue
                row = {**row, "content": part}
            out.append(row)
    return out


def task_for(known: list[tuple[str, str]], *, today: str | None = None) -> str:
    """The fork's task text. ``known`` is the current (key, value) user
    model, so a changed fact reuses its key and supersedes the old one.
    ``today`` names the day ("Wednesday, September 30"), so nothing is
    stored as "tomorrow"."""
    scope = _SCOPE_DAY.format(named=f", {today}" if today else "")
    known_text = ""
    if known:
        known_text = _KNOWN_HEADER.format(
            lines="\n".join(f"- {key}: {value}" for key, value in known))
    return _TASK_TEMPLATE.format(scope=scope, known=known_text,
                                 today=today or "Wednesday")


@dataclass(frozen=True)
class Remembered:
    """What one fork call yielded after the quote check."""

    facts: tuple[_Fact, ...]
    #: (excerpt, turn_id, sentence): his words and what she remembers.
    callbacks: tuple[tuple[str, int, str], ...]
    #: turn_id -> weight, for episode importance.
    weights: dict[int, float]
    dropped: int
    #: (sentence, turn_id, quote): something that happened between them.
    moments: tuple[tuple[str, int, str], ...] = ()
    #: How the day went, in her words; replaces the day's placeholder
    #: session summaries.
    summary: str = ""


def _norm(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(raw).lower()).strip("_")[:48]


def _payload(raw: str) -> dict:
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        payload = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse(raw: str, his_turns: list[dict]) -> Remembered:
    """Keep the items whose quote is in one of his messages today.

    ``his_turns``: today's user rows (``id``, ``content``). The key gets the
    store's prefix for its kind (``user:`` / ``preference:`` /
    ``relationship:``), so the fork's facts merge with the regex layer's.
    """
    payload = _payload(raw)
    items = payload.get("items")
    if not isinstance(items, list):
        return Remembered((), (), {}, 0)
    summary = re.sub(r"\s+", " ", str(payload.get("summary") or "")).strip()[:600]
    turns = [(int(t["id"]), str(t.get("content") or "")) for t in his_turns
             if t.get("id") is not None]
    facts: list[_Fact] = []
    callbacks: list[tuple[str, int, str]] = []
    moments: list[tuple[str, int, str]] = []
    weights: dict[int, float] = {}
    seen: set[str] = set()
    dropped = 0
    for item in items[:MAX_ITEMS]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        kind = KINDS.get(str(item.get("kind", "")).strip().lower())
        quote = str(item.get("quote") or "").strip()
        fact = re.sub(r"\s+", " ", str(item.get("fact") or "")).strip()
        slug = _slug(item.get("key") or "")
        turn_id = next((tid for tid, text in turns
                        if quote and _norm(quote) in _norm(text)), None)
        if kind is None or turn_id is None or not fact or not slug:
            dropped += 1
            continue
        try:
            weight = float(item.get("weight", 0.5))
        except (TypeError, ValueError):
            weight = 0.5
        weights[turn_id] = max(weights.get(turn_id, 0.0),
                               min(1.0, max(0.0, weight)))
        if kind == "callback":
            callbacks.append((quote[:80], turn_id, fact[:300]))
            continue
        if kind == "moment":
            moments.append((fact[:300], turn_id, quote))
            continue
        key = f"{_KEY_PREFIX[kind]}:{slug}"
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        facts.append(_Fact(key=key, value=fact[:300], kind=kind,
                           category=_FACT_CATEGORY[kind], turn_id=turn_id,
                           anchor=quote))
    return Remembered(tuple(facts), tuple(callbacks), weights, dropped,
                      tuple(moments), summary)


#: Calls per day: an empty or unparseable reply is asked once more.
ATTEMPTS = 2


def remember_day(client, fork, *, known: list[tuple[str, str]],
                 his_turns: list[dict],
                 reasoning_effort: str | None = None,
                 today: str | None = None) -> Remembered:
    """One forked call (plus one retry on an empty/unparseable reply).
    ``reasoning_effort`` is the mainline's, so the fork banks her system
    prefix. ``fork(task)`` builds the request over the day (``day_view``)."""
    pair = fork(task_for(known, today=today))
    if pair is None:
        return Remembered((), (), {}, 0)
    system, messages, tools = pair
    for attempt in range(ATTEMPTS):
        # a retry goes to another backend (harness.client.fresh_session)
        with (fresh_session(client, f"memory{attempt}") if attempt
              else contextlib.nullcontext()):
            reply = client.chat_with_meta(
                messages, system=system, temperature=0.0,
                # json_mode OFF, as on the forked judge: response_format is
                # part of the provider's cached prefix.
                json_mode=False, tools=tools, reasoning_effort=reasoning_effort,
            )
        raw = getattr(reply, "content", None) or (
            reply if isinstance(reply, str) else "")
        if isinstance(_payload(raw).get("items"), list):
            return parse(raw, his_turns)
    return Remembered((), (), {}, 0)
