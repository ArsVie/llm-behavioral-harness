"""A4 — scripted scenarios + offline driver for availability negotiation (G0).

Deterministic, offline (no LLM, no network) scripted runs of the six G0
scenarios (docs/availability-negotiation-contract.md +
harness/negotiation_contract.py — frozen, import only):

  1. retain        — user keeps actively talking past the boundary -> repeated
                     delay, she stays past start_t_h, then pauses -> the AFK
                     bomb resolves to go.
  2. release       — user goes quiet right after Inform -> silence >
                     SHORT_AFK_H (the AFK bomb) fires Decide -> go.
  3. window-close  — user holds her past end_t_h -> forced skip ("missed it
                     entirely"), recorded (agenda skipped + decision record).
  4. unskippable   — routine source_type -> Inform is a heads-up not a
                     negotiation; Decide is offered with skippable=False and
                     the scripted verdicts still go.
  5. no-nag        — Inform emits exactly once; no re-announcement across N
                     delays (count channel messages containing the mention).
  6. termination   — no configuration loops forever: an always-delay model
                     still resolves by end_t_h (backstop), and a delay whose
                     re-arm would land at/after end_t_h resolves immediately
                     (clamp).

Each scenario is a deterministic scripted run: a scripted user turn stream
(at_t_h events under a virtual clock — the build_user_stream event shape)
+ a scripted model (ScriptedClient — the FakeModel/FakeClient pattern from
experiments/decision_probe.py) + a one-item agenda (AgendaItem with
start_t_h / end_t_h / source_type / salience).

The driver (:func:`run_scenario`) drives the REAL harness mechanics end to
end — SQLiteStore + Session (the G0 A1 negotiation state machine wired into
_apply_steer/_run_turn_decides/check_negotiation) + the REAL DecisionRunner
(harness.tools, A2: phase-aware verdict parsing, server-filled defer_turns,
replay by decision id) — with the scripted client answering pop-up calls
(native transport, exactly like the runtime's _popup_request_call path).
The wake discipline mirrors the runtime: turns at scripted instants, parks
at Session.next_negotiation_trigger_t_h (AFK bomb / window-close backstop)
with Session.check_negotiation at each park, exactly like the rollover loop.

Every observable asserted by tests/test_availability_negotiation.py is
store/contract-level (agenda status, decision_records rows, audit events,
channel output, conversation close reasons), so the tests hold against the
merged A1/A2 implementation without redesign.

The real implementation's seam choices (this module adapts to them):

* Decision ids: ``neg-<item_id>-inform`` and ``neg-<item_id>-decide-<n>``
  (n = delays already taken) — deterministic per (item, phase, delay_index).
* Inform: fires at the first TURN at/after start_t_h (the start pop-up is
  drained into the negotiation by ``_maybe_start_negotiation``); the Inform
  turn itself never decides (the loop fires from the next companion turn
  on). The mention verdict shape is ``{message: str}`` (A2); the session
  reads ``reason`` for the channel text with a deterministic fallback.
* Forced skip: recorded via store.record_decision with source="backstop",
  verdict {action: abandon, forced_skip: true, reason: "missed it entirely
  — window closed" / "window closed before the next decide"}, raw_reply
  None (no model call).
* go -> existing close path with close_reason "followed_event" + agenda
  status "completed" + the ordinary reply suppressed (her natural close is
  the only message). skip / forced -> status "skipped"; conversation
  continues.
* delay(N): N mapped server-side from the reason (DEFER_N_PATTERNS); both
  triggers re-armed (turn counter + AFK bomb off the last user turn); a
  re-arm landing at/after end_t_h resolves immediately as a forced skip.
* Runtime wake set: user events + next_negotiation_trigger_t_h parks. The
  agenda START boundary is detected lazily at the next turn (the runtime's
  _enqueue_event_popups discipline) — every scenario has a turn at/after
  start, so Inform fires there deterministically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.clock import VirtualClock
from harness.client import ChatResult
from harness.domain import AgendaItem, Conversation, DailyAgenda
from harness.judge import ScriptedJudge
from harness.negotiation_contract import SHORT_AFK_H
from harness.session import Session
from harness.store import SQLiteStore
from harness.tools import DecisionConfig

# popup_kind for both negotiation phases.
POPUP_KIND = "tool_decide_event"
# Distinct close reason on go.
CLOSE_REASON_FOLLOWED = "followed_event"
# t_h comparison tolerance for virtual-clock instants.
_EPS = 1e-9


# scenario vocabulary


def at_t_h(t_h: float, text: str) -> dict:
    """One scripted user turn at an absolute virtual instant (the
    build_user_stream at_t_h event shape)."""
    return {"kind": "at_t_h", "t_h": t_h, "text": text}


def v_go(reason: str = "ok, going now") -> dict:
    return {"initiate": True, "reason": reason, "action": "follow"}


def v_skip(reason: str = "skipping it after all") -> dict:
    return {"initiate": False, "reason": reason, "action": "abandon"}


def v_delay(reason: str = "just a sec") -> dict:
    return {"initiate": True, "reason": reason, "action": "defer"}


# scenario definitions


@dataclass(frozen=True)
class Scenario:
    """One deterministic scripted negotiation run."""

    id: str
    name: str
    item: AgendaItem
    # Scripted user turns (at_t_h events) in stream order.
    user_stream: tuple[dict, ...]
    # Scripted decide verdicts, consumed in order by each Decide call.
    verdicts: tuple[dict, ...]
    # One-shot Inform mention ({message} verdict; channel text is deterministic either way).
    inform_mention: str
    # Always delay on every Decide call.
    always_delay: bool = False
    # Master seed; engine and conversation-closing draws are deterministic per seed.
    seed: int = 20260814


def _item(
    item_id: str,
    activity: str,
    start_t_h: float,
    end_t_h: float,
    *,
    source_type: str = "arc",
    salience: float = 0.8,
) -> AgendaItem:
    return AgendaItem(
        id=item_id, start_t_h=start_t_h, end_t_h=end_t_h, activity=activity,
        source_type=source_type, source_id="a4-scenario", salience=salience,
        status="planned",
    )


# The six G0 scenarios; times are virtual hours of day 0. Inform fires at the
# first turn at/after start_t_h; Decide fires at companion turns and AFK-bomb parks.
SCENARIOS: dict[str, Scenario] = {
    # 1. Retain: active talk past the boundary, then a pause fires the AFK bomb -> go.
    "retain": Scenario(
        id="retain",
        name="retain",
        item=_item("retain-gym", "gym", 19.0, 21.5),
        user_stream=(
            at_t_h(18.95, "hey! you around?"),
            at_t_h(19.05, "wait, tell me more about that"),
            at_t_h(19.12, "really? how did that go?"),
            at_t_h(19.19, "and what did she say to that?"),
            at_t_h(19.26, "haha okay, go on"),
        ),
        verdicts=(v_delay("just a sec"), v_delay("just a sec"),
                  v_delay("just a sec"), v_go("ok, heading out — talk soon")),
        inform_mention="I've got gym soon — just letting you know",
    ),
    # 2. Release: silence after Inform fires the AFK bomb -> go.
    "release": Scenario(
        id="release",
        name="release",
        item=_item("release-gym", "gym", 19.0, 21.0),
        user_stream=(
            at_t_h(18.95, "hey! you around?"),
            at_t_h(19.05, "so anyway, that's the whole story"),
        ),
        verdicts=(v_go("ok, going to the gym now"),),
        inform_mention="I've got gym soon — just letting you know",
    ),
    # 3. Window-close: user holds past end_t_h -> forced skip, recorded.
    "window-close": Scenario(
        id="window-close",
        name="window-close",
        item=_item("window-gym", "gym", 19.0, 19.5),
        user_stream=(
            at_t_h(18.95, "hey! you around?"),
            at_t_h(19.05, "so anyway, about yesterday —"),
        ),
        verdicts=(v_delay("just a sec"),),
        inform_mention="I've got gym soon — just letting you know",
    ),
    # 4. Unskippable: routine source_type; Decide is offered with skippable=False.
    "unskippable": Scenario(
        id="unskippable",
        name="unskippable",
        item=_item("class-1", "evening class", 19.0, 20.5,
                   source_type="routine", salience=0.9),
        user_stream=(
            at_t_h(18.95, "hey! you around?"),
            at_t_h(19.05, "stay? just a bit?"),
            at_t_h(19.15, "please? it's important"),
        ),
        verdicts=(v_go("right, I'm heading in now"),),
        inform_mention="I've got evening class soon — heads up",
    ),
    # 5. No-nag: Inform emits once; the go at 19.33 closes the conversation.
    "no-nag": Scenario(
        id="no-nag",
        name="no-nag",
        item=_item("no-nag-gym", "gym", 19.0, 22.0),
        user_stream=(
            at_t_h(18.95, "hey! you around?"),
            at_t_h(19.05, "wait —"),
            at_t_h(19.12, "and then?"),
            at_t_h(19.19, "haha, nice"),
            at_t_h(19.26, "so anyway —"),
            at_t_h(19.33, "thanks for listening, really"),
        ),
        verdicts=(v_delay("just a sec"), v_delay("just a sec"),
                  v_delay("just a sec"), v_go("ok, going now")),
        inform_mention="I've got gym soon — just letting you know",
    ),
    # 6. Termination: an always-delay model resolves at end_t_h via the backstop.
    "termination": Scenario(
        id="termination",
        name="termination",
        item=_item("term-gym", "gym", 19.0, 20.0),
        user_stream=(
            at_t_h(18.95, "hey! you around?"),
            at_t_h(19.05, "wait —"),
            at_t_h(19.12, "and then?"),
            at_t_h(19.19, "haha, nice"),
            at_t_h(19.26, "so anyway —"),
        ),
        verdicts=(),
        always_delay=True,
        inform_mention="I've got gym soon — just letting you know",
    ),
    # 6b. Termination clamp: a re-armed delay at/after end_t_h resolves immediately.
    "termination-clamp": Scenario(
        id="termination-clamp",
        name="termination-clamp",
        item=_item("clamp-gym", "gym", 19.0, 19.3),
        user_stream=(
            at_t_h(18.95, "hey! you around?"),
            at_t_h(19.05, "wait —"),
            at_t_h(19.15, "and then?"),
            at_t_h(19.25, "one more thing —"),
        ),
        verdicts=(v_delay("just a sec"),),
        inform_mention="I've got gym soon — just letting you know",
    ),
}


# scripted model client (FakeModel/FakeClient pattern)


class ScriptedClient:
    """Scripted LLMClient: canned companion replies + scripted pop-up
    verdicts, no network.

    Pop-up calls are the session's native-transport calls (``tools`` is
    not None, exactly like ``_popup_request_call``): the FIRST pop-up call
    of a run is the Inform mention (``{message: ...}``, the A2 canonical
    shape), the rest are Decide legs consuming the per-scenario verdict
    script in order (or always delay). The model never emits ``defer_turns``
    — the server fills it (contract floor 1). Records every call.
    """

    supports_json: bool = True
    supports_tools: bool = True

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self._script = list(scenario.verdicts)
        self.popup_calls = 0
        self.calls: list[dict] = []

    def _popup_verdict(self) -> dict:
        if self.popup_calls == 0:
            return {"message": self.scenario.inform_mention}
        if self.scenario.always_delay or not self._script:
            return v_delay("just a sec")
        verdict = dict(self._script.pop(0))
        verdict.pop("defer_turns", None)  # the model does not emit N
        return verdict

    def chat_with_meta(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        self.calls.append(
            {
                "messages": messages,
                "system": system,
                "tools": tools,
                "max_tokens": max_tokens,
            }
        )
        if tools is not None:
            # Pop-up call (native transport): the first popup call is the Inform mention.
            verdict = self._popup_verdict()
            self.popup_calls += 1
            return ChatResult(
                content="",
                tool_calls=[
                    {
                        "id": f"call-{self.popup_calls}",
                        "name": POPUP_KIND,
                        "arguments_json": json.dumps(
                            verdict, ensure_ascii=False
                        ),
                    }
                ],
            )
        n_chat = sum(1 for c in self.calls if c["tools"] is None)
        return ChatResult(content=f"(companion reply {n_chat})")

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        temperature: float = 0.8,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        return self.chat_with_meta(
            messages, system=system, temperature=temperature,
            json_mode=json_mode, max_tokens=max_tokens,
        ).content

    def close(self) -> None:
        pass


# run driver (runtime wake discipline over the real session)


@dataclass
class ScenarioResult:
    """Observable outcome of one scripted negotiation run.

    Every field is a contract-level observable: the agenda item's final
    status, the decision_records rows (with parsed inputs/verdict), the
    store's audit events, the channel output (inform mention / natural
    close), the model calls, and the final conversation states.
    """

    scenario_id: str
    agenda_status: str
    decision_records: list[dict]
    audit_events: list[dict]
    channel_out: list[tuple[str, str, float]]  # (kind, text, t_h)
    model_calls: list[dict]
    conversations: list[Conversation]
    final_t_h: float
    informed: bool
    store: SQLiteStore = field(repr=False, default=None)  # type: ignore[assignment]


def run_scenario(
    store: SQLiteStore,
    scenario: Scenario,
    *,
    seed: int | None = None,
) -> ScenarioResult:
    """Run one scripted scenario end to end on the real harness mechanics.

    ``store`` must be an OPEN SQLiteStore (tmp_path in tests); the caller
    owns its lifecycle. The conversation is driven through a real Session
    (decision layer enabled, real DecisionRunner, scripted client, virtual
    clock) with the runtime's wake discipline: user turns at scripted
    instants, parks at ``next_negotiation_trigger_t_h`` (AFK bomb /
    window-close backstop) with ``check_negotiation`` at each park.
    """
    seed = scenario.seed if seed is None else seed
    clock = VirtualClock()
    client = ScriptedClient(scenario)
    session = Session(
        store,
        persona=PersonaParams(),
        timing=TimingParams(),
        variant=MoodVariant.DECOUPLED_OFFSETS,
        seed=seed,
        client=client,
        clock=clock,
        judge=ScriptedJudge(score=0.5).judge_day,
        decision_config=DecisionConfig(),
    )
    store.save_agenda(0, DailyAgenda(day=0, items=(scenario.item,)))

    events = sorted(
        (ev for ev in scenario.user_stream if ev["kind"] == "at_t_h"),
        key=lambda ev: float(ev["t_h"]),
    )
    stream_idx = 0
    channel_out: list[tuple[str, str, float]] = []
    resolved = False
    guard = 0

    def item_status() -> str:
        for it in store.list_agenda_items(day=0):
            if it.id == scenario.item.id:
                return it.status
        return "planned"

    while not resolved:
        guard += 1
        if guard > 200:
            raise RuntimeError(
                "scenario driver loop guard exceeded — the negotiation did "
                "not resolve (contract termination violated)"
            )
        now = clock.now_h()
        nxt_event = (
            float(events[stream_idx]["t_h"])
            if stream_idx < len(events) else None
        )
        nxt_neg = session.next_negotiation_trigger_t_h(now)
        candidates = [c for c in (nxt_event, nxt_neg) if c is not None]
        if not candidates:
            # Stream exhausted: run one final negotiation check.
            outs = session.check_negotiation(now)
            for reason, text in outs:
                channel_out.append((str(reason), str(text), now))
            if item_status() in ("completed", "skipped"):
                resolved = True
            break
        wake = min(candidates)
        if wake > now + _EPS:
            clock.advance_hours(wake - now)
        if nxt_event is not None and abs(nxt_event - clock.now_h()) <= 1e-6:
            # A user turn at this wake runs inside on_message.
            result = session.on_message(str(events[stream_idx]["text"]))
            for kind, text in result.proactive_out:
                channel_out.append((str(kind), str(text), clock.now_h()))
            stream_idx += 1
        else:
            # A negotiation park (AFK bomb / backstop): run the due legs.
            outs = session.check_negotiation(clock.now_h())
            for reason, text in outs:
                channel_out.append((str(reason), str(text), clock.now_h()))
        resolved = item_status() in ("completed", "skipped")

    informed = any(
        e.get("event") == "negotiation_inform" for e in store.events_since(0)
    )
    return ScenarioResult(
        scenario_id=scenario.id,
        agenda_status=item_status(),
        decision_records=store.decisions_for_day(0),
        audit_events=store.events_since(0),
        channel_out=channel_out,
        model_calls=[
            c for c in client.calls if c["tools"] is not None
        ],
        conversations=(
            store.list_conversations()
            if hasattr(store, "list_conversations") else []
        ),
        final_t_h=clock.now_h(),
        informed=informed,
        store=store,
    )
