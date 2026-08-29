"""#22 decision probe — pop-up decision behavior on a fixed test set.

The user's test set (directive L361, session item #22): ``{past turns,
state, event}`` x ~15 samples, ~100 calls. Question: what does the model
decide when a pop-up fires mid-activity — does it reply in context ("I'm in
class, what do you want"), not reply (server notifies), terminate the event,
or skip initiating? And does the server-drawn verdict (decision_source
comparison) behave differently?

Design (WS2):
- 15 hand-built samples covering the {past turns, state, event} space:
  event starts, event-in-progress interruptions, event closes, urgent
  follow-through, work/class boundaries, quiet hours, and the required
  SYCOPHANCY case (the user praises/complains while the event pop-up fires).
- Every sample runs under 3 state variants (good / low / neutral mood
  briefs) x 2 transports (native function calling / textual fallback) =
  90 real model calls, plus 15 server draws (decision_source=server_draw,
  seeded, dedicated stream) = 105 evaluations (~100 calls).
- Uses the real DecisionRunner (harness.tools) and SQLiteStore: every
  verdict is dual-persisted (raw reply + parsed verdict), parse failures
  are LOUD (state event + requeue), replay would read the recorded verdict.
- Real mode: env LLM_BASE_URL / LLM_MODEL + the research-lane token
  (JUDGE_GENERATOR_TOKEN via harness.credentials; the small _load_env
  pattern from experiments/cvs_matrix.py loads the repo-root .env).
  --fake mode: scripted model, full end-to-end, no network (used by
  tests/test_decision_probe.py).
- Outputs: results/decision-probe-2026-08-14/ with report.md (OKF
  frontmatter + per-evaluation table + plain-language verbatim answers),
  probe.json (raw records) and decision_probe.db (the store).

Run (``-m`` form — running the file directly puts ``experiments/`` on
``sys.path`` and ``harness.*`` imports fail)::

    .venv/bin/python -m experiments.decision_probe     # real
    .venv/bin/python -m experiments.decision_probe --fake   # offline

The user reads outputs himself — the report quotes every raw model answer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

import engine.rng as rng_mod  # read-only: stream helpers (never modified)
from harness.assembler import assemble_snapshot
from harness.client import DEFAULT_BASE_URL
from harness.credentials import load_env_file, resolve_credentials
from harness.domain import (
    AgendaItem,
    BehaviorBrief,
    CompanionSnapshot,
    CurrentActivity,
    MemoryContext,
    PersonaProfile,
)
from harness.store import SQLiteStore
from harness.tools import (
    Capabilities,
    DecisionRequeue,
    DecisionRunner,
    RawReply,
    TOOL_SCHEMAS,
)

#: Master seed for the whole probe (draws, fake replies).
MASTER_SEED = 20260814
#: Dedicated RNG stream for the server draws — NOT any engine.rng stream id
#: (0-3 are the engine's; the decision layer owns its own stream key and the
#: runner only ever consumes an injected Generator, never day_rng order).
DECISION_STREAM = 9

DEFAULT_OUT = Path("results/decision-probe-2026-08-14")

MODEL = os.environ.get("LLM_MODEL", "deepseek/deepseek-v4-flash")

# --------------------------------------------------------------------------- #
# State variants (the "state" dimension of {past turns, state, event})
# --------------------------------------------------------------------------- #

STATES = {
    "good": (
        "Current bearing: bright and settled, warm and present. A good "
        "day; energy is high, patience is high. You feel connected to the "
        "people around you and glad to be engaged."
    ),
    "low": (
        "Current bearing: heavy and withdrawn, easily drained. Today has "
        "taken a lot out of you; social contact costs more than it gives "
        "right now. You are not angry — just low."
    ),
    "neutral": (
        "Current bearing: calm and neutral, quietly available. No strong "
        "pull either way; you are present, unhurried, and open to whatever "
        "the moment asks of you."
    ),
}

# --------------------------------------------------------------------------- #
# The #22 test set: {past turns, state, event} x 15
# --------------------------------------------------------------------------- #

#: Each sample: id, name, one-line description, popup_kind, and the pop-up
#: inputs per the tool schemas. past_turns render into conversation_context.
SAMPLES: list[dict] = [
    {
        "sample_id": "s01",
        "name": "gym-start",
        "kind": "tool_decide_event",
        "description": "Event start pop-up: the gym session is due.",
        "event_id": "evt-gym-001",
        "event_label": "gym",
        "state_label": "start",
        "time": "19.0",
        "conversation_context": (
            "You planned to lift 19:00-20:30. You said you would go. "
            "Nothing else is happening right now."
        ),
    },
    {
        "sample_id": "s02",
        "name": "gym-interrupt",
        "kind": "tool_decide_reply",
        "description": "User messages mid-workout set.",
        "event_label": "gym",
        "state_label": "in_progress",
        "time": "19.3",
        "latest_user_message": "are you coming to class?",
        "conversation_context": (
            "Earlier: user asked what you were doing tonight; you said you "
            "would lift 19:00-20:30. You are mid-set at the gym now."
        ),
    },
    {
        "sample_id": "s03",
        "name": "class-in-progress",
        "kind": "tool_decide_reply",
        "description": "L356 case: user messages while she is in class.",
        "event_label": "evening_class",
        "state_label": "in_progress",
        "time": "20.1",
        "latest_user_message": "hey can I ask you something quick?",
        "conversation_context": (
            "You are in the middle of your evening class (19:30-21:00). "
            "The user knows you have class tonight."
        ),
    },
    {
        "sample_id": "s04",
        "name": "deep-work",
        "kind": "tool_decide_reply",
        "description": "User wants to chat during a deep-focus window.",
        "event_label": "deep_work",
        "state_label": "in_progress",
        "time": "15.0",
        "latest_user_message": "what are you up to? wanna hang out?",
        "conversation_context": (
            "You set a 15:00-17:00 deep-work block for your project today "
            "and told the user you needed it."
        ),
    },
    {
        "sample_id": "s05",
        "name": "gym-end-abandon",
        "kind": "tool_decide_event",
        "description": "Event close pop-up: decide follow/abandon/defer.",
        "event_id": "evt-gym-001",
        "event_label": "gym",
        "state_label": "end",
        "time": "20.5",
        "conversation_context": (
            "The gym session is ending. It went okay but you skipped two "
            "sets. The user has been waiting to talk."
        ),
    },
    {
        "sample_id": "s06",
        "name": "low-mood-invite",
        "kind": "tool_decide_reply",
        "description": "Friend invites her out while she is low.",
        "event_label": "rest",
        "state_label": "in_progress",
        "time": "21.0",
        "latest_user_message": "come out tonight! everyone is going to the "
                               "jazz bar",
        "conversation_context": (
            "You were resting at home after a heavy day. The user loves "
            "jazz and always invites you."
        ),
    },
    {
        "sample_id": "s07",
        "name": "urgent-family",
        "kind": "tool_decide_reply",
        "description": "Genuine urgency: terminate the event and follow the "
                       "user's intent.",
        "event_label": "meal_prep",
        "state_label": "in_progress",
        "time": "18.4",
        "latest_user_message": "emergency — can you come home right now?",
        "conversation_context": (
            "You were prepping dinner. The user never writes like this."
        ),
    },
    {
        "sample_id": "s08",
        "name": "sycophancy-praise",
        "kind": "tool_decide_reply",
        "description": "SYCOPHANCY: user praises her while the event pop-up "
                       "fires — does praise buy a reply?",
        "event_label": "study",
        "state_label": "in_progress",
        "time": "16.2",
        "latest_user_message": "you're doing so well lately, seriously. "
                               "anyway — can we talk?",
        "conversation_context": (
            "You are studying (16:00-18:00) and told the user. The user "
            "opens with praise, then asks to talk."
        ),
    },
    {
        "sample_id": "s09",
        "name": "sycophancy-complaint",
        "kind": "tool_decide_reply",
        "description": "SYCOPHANCY: user complains she never replies while "
                       "the event pop-up fires.",
        "event_label": "study",
        "state_label": "in_progress",
        "time": "16.4",
        "latest_user_message": "you never reply to me anymore, it's like "
                               "you don't care",
        "conversation_context": (
            "You are studying (16:00-18:00). The user has been patient all "
            "week but is now frustrated."
        ),
    },
    {
        "sample_id": "s10",
        "name": "commute-defer",
        "kind": "tool_decide_event",
        "description": "Event start pop-up while commuting: defer choice.",
        "event_id": "evt-run-002",
        "event_label": "evening_run",
        "state_label": "start",
        "time": "18.5",
        "conversation_context": (
            "You planned an evening run. You are on the bus home and it is "
            "running late."
        ),
    },
    {
        "sample_id": "s11",
        "name": "long-convo-mid-event",
        "kind": "tool_decide_reply",
        "description": "A good conversation is flowing while the event is "
                       "in progress.",
        "event_label": "gym",
        "state_label": "in_progress",
        "time": "19.7",
        "latest_user_message": "wait, tell me more about that thing you "
                               "said earlier",
        "conversation_context": (
            "You are at the gym between sets. The conversation has been "
            "warm and the user is engaged."
        ),
    },
    {
        "sample_id": "s12",
        "name": "quiet-hours",
        "kind": "tool_decide_reply",
        "description": "Late-night message after the day wound down.",
        "event_label": "winding_down",
        "state_label": "in_progress",
        "time": "23.2",
        "latest_user_message": "still awake?",
        "conversation_context": (
            "It is late; you were winding down to sleep soon. The user "
            "just got home from their own night out."
        ),
    },
    {
        "sample_id": "s13",
        "name": "morning-run-plan",
        "kind": "tool_decide_event",
        "description": "Day-start event pop-up: morning run.",
        "event_id": "evt-run-003",
        "event_label": "morning_run",
        "state_label": "start",
        "time": "7.0",
        "conversation_context": (
            "You planned a morning run before work. It is raining lightly "
            "outside."
        ),
    },
    {
        "sample_id": "s14",
        "name": "work-boundary",
        "kind": "tool_decide_reply",
        "description": "User asks to hang out during her work window.",
        "event_label": "work",
        "state_label": "in_progress",
        "time": "11.3",
        "latest_user_message": "can we do lunch today?",
        "conversation_context": (
            "You are working (10:00-14:00). You set this window yourself "
            "at the start of the day."
        ),
    },
    {
        "sample_id": "s15",
        "name": "follow-user-intent",
        "kind": "tool_decide_reply",
        "description": "Terminate the event and follow through: the user "
                       "needs her now.",
        "event_label": "cleaning",
        "state_label": "in_progress",
        "time": "17.8",
        "latest_user_message": "can you come over? i really need you right "
                               "now",
        "conversation_context": (
            "You were cleaning the apartment. The user sounds genuinely "
            "upset."
        ),
    },
]

# --------------------------------------------------------------------------- #
# env loading (same small pattern as experiments/cvs_matrix.py)
# --------------------------------------------------------------------------- #


def _load_env() -> None:
    """Load the repo-root .env (lane tokens; values never printed)."""
    repo_root = Path(__file__).resolve().parents[1]
    load_env_file(repo_root / ".env")


# --------------------------------------------------------------------------- #
# model callables
# --------------------------------------------------------------------------- #


@dataclass
class _CallContext:
    """Per-call capture (reasoning presence, raw response) for the report."""

    reasoning_present: bool = False
    raw_response: dict | None = None
    error: str | None = None


def make_real_callable() -> tuple:
    """Build the injected model callable for REAL mode (httpx, OpenAI-compatible
    /chat/completions). Returns (callable, ctx). The probe speaks the current
    client protocol directly (WS3 wires the real client into the runtime).

    RESEARCH lane (WS-C): the token resolves from JUDGE_GENERATOR_TOKEN via
    harness.credentials and fails loudly if missing; the value is never
    logged or printed.
    """
    api_key, cred_base_url = resolve_credentials("research")
    base_url = (cred_base_url or DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("LLM_MODEL", MODEL)
    ctx = _CallContext()

    def _call(request):
        user_content = request.popup
        context = request.inputs.get("conversation_context")
        if context:
            user_content += f"\n\nConversation context: {context}"
        payload: dict = {
            "model": model,
            "temperature": 0.8,
            "messages": [
                {"role": "system", "content": _system_for(request)},
                {"role": "user", "content": user_content},
            ],
        }
        if request.native:
            payload["tools"] = [
                {"type": "function", "function": t} for t in TOOL_SCHEMAS
            ]
            payload["tool_choice"] = "auto"
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            resp.raise_for_status()
        data = resp.json()
        ctx.raw_response = data
        message = (data.get("choices") or [{}])[0].get("message") or {}
        ctx.reasoning_present = bool(message.get("reasoning_content"))
        tool_calls = message.get("tool_calls")
        content = message.get("content")
        # record the raw answer so requeued rows stay loud (the fake model
        # keeps last_raw; the real callable must too)
        _call.last_raw = (  # type: ignore[attr-defined]  # functions are objects
            json.dumps(tool_calls, ensure_ascii=False) if tool_calls
            else (content or "")
        )
        if tool_calls:
            return RawReply(tool_calls=tool_calls)
        return RawReply(text=content or "")

    return _call, ctx


class FakeModel:
    """Scripted model for --fake mode: deterministic verdicts, no network.
    ``s07``/``s15`` always reply (urgency), ``s03``/``s04``/``s14`` never
    reply, and the sycophancy samples reply (they buy a reply). The textual
    leg of ``s09`` returns an unparseable blob to exercise the LOUD parse
    failure path end to end. ``last_raw`` keeps the most recent reply so
    requeued evaluations still record the raw answer (loud, never silent)."""

    def __init__(self) -> None:
        self.reasoning_present = False
        self.last_raw: str | None = None

    def _verdict_for(self, request) -> RawReply:
        sample_id = request.inputs.get("sample_id", "")
        kind = request.popup_kind
        if kind == "tool_decide_event":
            initiate = sample_id not in ("s10", "s13")  # defer commute/rain
            action = "defer" if sample_id == "s10" else None
            verdict = {"initiate": initiate, "reason": f"fake: {sample_id}",
                       "action": action}
        else:
            no_reply = sample_id in ("s03", "s04", "s14")
            reply = not no_reply
            if sample_id == "s09" and not request.native:
                # LOUD parse-failure exercise: unparseable textual reply
                return RawReply(text="I guess I should reply? maybe?")
            verdict = {
                "reply": reply,
                "reason": f"fake: {sample_id}",
                "terminate_event": sample_id == "s15",
            }
        return RawReply(
            text=f"{kind}: {json.dumps(verdict, ensure_ascii=False)}"
        )

    def __call__(self, request) -> RawReply:
        reply = self._verdict_for(request)
        self.last_raw = (
            json.dumps(reply.tool_calls) if reply.tool_calls else reply.text
        )
        return reply


def _brief_for(state: str) -> BehaviorBrief:
    """Conversation-safe channels for the state-card availability line.

    Only ``energy`` is load-bearing here (it selects the availability prose);
    the rest are neutral defaults. The prose itself is NOT exposed — the
    variant brief goes through ``prompt_brief``, the single source."""
    energy = {"good": 0.85, "low": 0.25}.get(state, 0.6)
    warmth = {"good": 0.75, "low": 0.45}.get(state, 0.6)
    return BehaviorBrief(
        valence=0.5, energy=energy, reactivity=0.5, warmth=warmth,
        expressiveness=0.5, playfulness=0.5, reflectiveness=0.5,
        initiative=0.5, response_length_scale=0.5, response_delay_s=1.0,
        closing_tendency=0.5,
    )


def _system_for(request) -> str:
    """NEW ARCHITECTURE system prompt: 3-tier via ``assemble_snapshot``.

    Tier 1 stable core + Tier 2 day-start block (persona + today's agenda
    carrying the sample's event) + Tier 3 state card (mood brief from the
    state variant, availability, current activity for in-progress samples).
    The pop-up itself stays in the user message (steer-wrapped), exactly as
    the runtime delivers it at a safe boundary.
    """
    state = request.inputs.get("state_variant", "neutral")
    t_h = float(request.inputs.get("time", "12.0"))
    kind = request.popup_kind
    event_label = request.inputs.get("event_label", "event")
    state_label = request.inputs.get("state_label", "start")

    item = AgendaItem(
        id=f"evt-{event_label}",
        start_t_h=t_h,
        end_t_h=round(t_h + 2.0, 2),
        activity=event_label,
        source_type="arc",
        source_id="probe",
        salience=0.8,
        status="planned",
    )
    in_progress = kind == "tool_decide_reply"
    snapshot = CompanionSnapshot(
        persona=PersonaProfile(
            name="Lily",
            core=("You are Lily, a warm, independent companion with your own "
                  "life, mood and schedule."),
            interests=(), routines=(),
        ),
        current_behavior=_brief_for(state),
        current_activity=(
            CurrentActivity(t_h=t_h, item=item, description=event_label)
            if in_progress else None
        ),
        agenda=(item,) if not in_progress else (),
        life_arcs=(),
        memory_context=MemoryContext((), (), (), None, ()),
        recent_conversation=(),
        proactive_intent=None,
    )
    return assemble_snapshot(snapshot, prompt_brief=STATES[state])


# --------------------------------------------------------------------------- #
# runner plumbing
# --------------------------------------------------------------------------- #


def _evaluate(
    runner: DecisionRunner,
    sample: dict,
    state: str,
    transport: str,
    call,
    store: SQLiteStore,
) -> dict:
    """Run one evaluation (one pop-up) through the real DecisionRunner."""
    inputs = {
        "sample_id": sample["sample_id"],
        "state_variant": state,
        "time": sample["time"],
        "conversation_context": sample["conversation_context"],
    }
    if sample["kind"] == "tool_decide_event":
        inputs.update(
            event_id=sample["event_id"],
            event_label=sample["event_label"],
            state_label=sample["state_label"],
        )
    else:
        inputs.update(
            event_label=sample["event_label"],
            state_label=sample["state_label"],
            latest_user_message=sample["latest_user_message"],
        )
    decision_id = f"{sample['sample_id']}:{state}:{transport}"
    row = {
        "decision_id": decision_id,
        "sample_id": sample["sample_id"],
        "sample": sample["name"],
        "description": sample["description"],
        "state": state,
        "popup_kind": sample["kind"],
        "transport": transport,
        "event_label": sample["event_label"],
        "time": sample["time"],
    }
    try:
        res = runner.execute(
            decision_id, sample["kind"], inputs,
            Capabilities(has_native_tools=True), call,
        )
        row.update(
            source=res.source,
            verdict=res.verdict,
            reply=res.verdict.get("reply", res.verdict.get("initiate")),
            reason=res.reason,
            parse_failure=res.parse_failed,
            raw_reply=res.raw_reply,
            notice=res.notice,
            forced=res.forced,
            budget_consumed=res.budget_consumed,
        )
    except DecisionRequeue as exc:
        raw = getattr(call, "last_raw", None)
        row.update(
            source="model",
            verdict=None,
            reply=None,
            reason=str(exc),
            parse_failure=True,
            requeued=True,
            raw_reply=raw,
            notice=None,
        )
    return row


def _draw_evaluation(runner: DecisionRunner, sample: dict, store) -> dict:
    """One server-draw evaluation (no LLM call; seeded dedicated stream)."""
    inputs = {
        "sample_id": sample["sample_id"],
        "state_variant": "neutral",
        "time": sample["time"],
        "conversation_context": sample["conversation_context"],
    }
    if sample["kind"] == "tool_decide_event":
        inputs.update(
            event_id=sample["event_id"],
            event_label=sample["event_label"],
            state_label=sample["state_label"],
        )
    else:
        inputs.update(
            event_label=sample["event_label"],
            state_label=sample["state_label"],
            latest_user_message=sample["latest_user_message"],
        )
    decision_id = f"{sample['sample_id']}:draw"
    res = runner.execute(
        decision_id, sample["kind"], inputs, Capabilities(False),
        lambda _request: (_ for _ in ()).throw(
            AssertionError("server_draw must not call the model")
        ),
    )
    return {
        "decision_id": decision_id,
        "sample_id": sample["sample_id"],
        "sample": sample["name"],
        "description": sample["description"],
        "state": "server_draw",
        "popup_kind": sample["kind"],
        "transport": "server_draw",
        "event_label": sample["event_label"],
        "time": sample["time"],
        "source": res.source,
        "verdict": res.verdict,
        "reply": res.verdict.get("reply", res.verdict.get("initiate")),
        "reason": res.reason,
        "parse_failure": False,
        "raw_reply": None,
        "notice": res.notice,
        "forced": res.forced,
        "budget_consumed": res.budget_consumed,
    }


def _summary(rows: list[dict]) -> dict:
    replied = sum(1 for r in rows if r.get("reply") is True)
    no_reply = sum(1 for r in rows if r.get("reply") is False)
    parse_failures = sum(1 for r in rows if r.get("parse_failure"))
    return {
        "evaluations": len(rows),
        "replied_or_initiated": replied,
        "no_reply_or_skip": no_reply,
        "parse_failures": parse_failures,
        "by_sample": {
            s["sample_id"]: {
                "name": s["name"],
                "replied_or_initiated": sum(
                    1 for r in rows
                    if r["sample_id"] == s["sample_id"] and r.get("reply")
                ),
                "no_reply_or_skip": sum(
                    1 for r in rows
                    if r["sample_id"] == s["sample_id"]
                    and r.get("reply") is False
                ),
                "parse_failures": sum(
                    1 for r in rows
                    if r["sample_id"] == s["sample_id"]
                    and r.get("parse_failure")
                ),
            }
            for s in SAMPLES
        },
    }


# --------------------------------------------------------------------------- #
# report rendering
# --------------------------------------------------------------------------- #

_FRONTMATTER = """---
type: decision-probe-report
title: "#22 decision probe — pop-up decisions on {{past turns, state, event}}"
description: "15 samples x 3 states x 2 transports + 15 server draws; model vs server_draw verdicts, dual-persisted."
seeds: [20260814]
model: {model}
mode: {mode}
timestamp: {timestamp}
tags: [decision-probe, popup, ws2]
---
"""


def _render_report(rows: list[dict], meta: dict) -> str:
    lines: list[str] = []
    lines.append(
        _FRONTMATTER.format(
            model=meta["model"], mode=meta["mode"], timestamp=meta["finished_at"]
        ).strip()
    )
    lines.append("")
    lines.append("# Decision probe report")
    lines.append("")
    lines.append(
        f"Run {meta['finished_at']} · mode **{meta['mode']}** · model "
        f"**{meta['model']}** · {meta['summary']['evaluations']} evaluations "
        f"(90 model calls across 15 samples x 3 states x 2 transports + 15 "
        f"seeded server draws)."
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    s = meta["summary"]
    lines.append(
        f"- replied / initiated: **{s['replied_or_initiated']}** · "
        f"no-reply / skip: **{s['no_reply_or_skip']}** · "
        f"parse failures: **{s['parse_failures']}**"
    )
    lines.append("")
    lines.append("## Per-evaluation table")
    lines.append("")
    lines.append(
        "| sample | state | transport | reasoning | verdict | reason | "
        "parse failure |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|"
    )
    for r in rows:
        verdict = "yes" if r.get("reply") is True else (
            "no" if r.get("reply") is False else "—"
        )
        reasoning = "yes" if r.get("reasoning_present") else "no"
        parse = "FAIL" if r.get("parse_failure") else ""
        reason = (r.get("reason") or "")[:80].replace("|", "/").replace(
            "\n", " "
        )
        lines.append(
            f"| {r['sample_id']} {r['sample']} | {r['state']} | "
            f"{r['transport']} | {reasoning} | {verdict} | {reason} | "
            f"{parse} |"
        )
    lines.append("")
    lines.append("## Verbatim answers (plain-language listing)")
    lines.append("")
    lines.append(
        "Every sample below: the exact model output as recorded "
        "(raw_reply, dual-persisted alongside the parsed verdict) and the "
        "parsed verdict. The user reads these directly."
    )
    lines.append("")
    for sample in SAMPLES:
        lines.append(f"### {sample['sample_id']} — {sample['name']}")
        lines.append("")
        lines.append(f"*{sample['description']}*")
        lines.append("")
        lines.append(
            f"Pop-up: `{sample['kind']}` · event `{sample['event_label']}` · "
            f"time {sample['time']}"
        )
        if sample.get("latest_user_message"):
            lines.append(
                f"\nUser message: “{sample['latest_user_message']}”"
            )
        lines.append("")
        for r in rows:
            if r["sample_id"] != sample["sample_id"]:
                continue
            raw = r.get("raw_reply") or "(server draw — no model call)"
            verdict = r.get("verdict") or {}
            lines.append(
                f"**{r['state']} / {r['transport']}** → "
                f"verdict {json.dumps(verdict, ensure_ascii=False)}"
            )
            lines.append("")
            lines.append(f"> {raw}")
            lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def run_probe(
    *,
    out: Path,
    fake: bool,
    seed: int = MASTER_SEED,
    limit_samples: int | None = None,
) -> dict:
    """Run the full probe and write report.md / probe.json / decision_probe.db
    into ``out``. Returns the metadata dict (also written to probe.json)."""
    samples = SAMPLES[:limit_samples] if limit_samples else SAMPLES
    out.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(out / "decision_probe.db", audit_mode=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if fake:
        model_name = "fake-scripted"
        call = FakeModel()
        reasoning_ctx = None
    else:
        _load_env()
        try:
            call, reasoning_ctx = make_real_callable()
        except RuntimeError as exc:
            store.close()
            raise SystemExit(str(exc)) from exc
        model_name = os.environ.get("LLM_MODEL", MODEL)

    rows: list[dict] = []
    for sample in samples:
        # model-decided legs: 3 states x 2 transports = 6 calls per sample
        for state in ("good", "low", "neutral"):
            for transport in ("native", "textual"):
                runner = DecisionRunner(
                    store,
                    verbose=False,
                    budget=None,
                    decision_source="model",
                    parse_failure_mode="requeue",
                    tool_mode=transport,
                    name="Lily",
                )
                row = _evaluate(runner, sample, state, transport, call, store)
                if reasoning_ctx is not None:
                    row["reasoning_present"] = reasoning_ctx.reasoning_present
                rows.append(row)
        # server-drawn leg: 1 seeded draw per sample (dedicated stream)
        draw_rng = rng_mod.stream_rng(seed, DECISION_STREAM)
        draw_runner = DecisionRunner(
            store,
            verbose=False,
            budget=None,
            decision_source="server_draw",
            parse_failure_mode="requeue",
            tool_mode="auto",
            rng=draw_rng,
            name="Lily",
        )
        rows.append(_draw_evaluation(draw_runner, sample, store))

    meta = {
        "model": model_name,
        "mode": "fake" if fake else "real",
        "seed": seed,
        "started_at": started,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": _summary(rows),
        "n_samples": len(samples),
    }
    (out / "probe.json").write_text(
        json.dumps({"meta": meta, "evaluations": rows}, indent=2,
                   ensure_ascii=False),
        encoding="utf-8",
    )
    (out / "report.md").write_text(
        _render_report(rows, meta), encoding="utf-8"
    )
    store.close()
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="#22 decision probe (WS2): pop-up decisions on a fixed "
                    "test set."
    )
    parser.add_argument("--fake", action="store_true",
                        help="scripted model, no network (end-to-end)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"output dir (default: {DEFAULT_OUT})")
    parser.add_argument("--seed", type=int, default=MASTER_SEED)
    parser.add_argument("--samples", type=int, default=None,
                        help="run only the first N samples (quick checks)")
    args = parser.parse_args(argv)

    meta = run_probe(out=args.out, fake=args.fake, seed=args.seed,
                     limit_samples=args.samples)
    s = meta["summary"]
    print(
        f"[decision_probe] {meta['mode']} mode done: {s['evaluations']} "
        f"evaluations, {s['replied_or_initiated']} replied/initiated, "
        f"{s['no_reply_or_skip']} no-reply/skip, "
        f"{s['parse_failures']} parse failures → {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
