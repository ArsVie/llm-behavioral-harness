"""IntentResolver tests + the seam-faithful SeamStore (A7).

The A2 store seam (agenda, arcs, episodes, interests, proactive intents,
resolve_intent_source) has NOT landed in this repo yet — wip/vslice-a2
points at main and carries no store ops. Per the A7 brief's fallback, the
store-backed modules are tested against :class:`SeamStore`, an in-memory
implementation of the frozen A2 seam (plus the existing ops Session and the
runtime use), so the tests exercise the CONTRACT, not a stub. test_runtime
and test_gates import SeamStore from here.
"""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

import engine.rng as rng_mod
from harness.domain import (
    AgendaItem,
    DailyAgenda,
    EpisodicMemory,
    Interest,
    LifeArc,
    MemoryKind,
    ProactiveIntent,
)
from harness.proactive import (
    CHECK_IN_SALIENCE,
    SOURCE_AGENDA,
    SOURCE_CALLBACK,
    SOURCE_CHECK_IN,
    SOURCE_LIFE_EVENT,
    SOURCE_SHARED_INTEREST,
    IntentResolver,
    compose_hook,
)
from harness.scheduler import (
    REASON_CALLBACK,
    REASON_CHECK_IN,
    REASON_EVENT,
    REASON_SCHEDULE,
    REASON_SHARED_INTEREST,
    REASON_VALIDITY_H,
)

# --------------------------------------------------------------------------- #
# SeamStore — seam-faithful in-memory store (A2 seam + existing ops)
# --------------------------------------------------------------------------- #


class SeamStore:
    """In-memory store implementing the frozen A2 seam + the existing ops
    the Session/runtime/scheduler use. Row dict shapes mirror SQLiteStore
    exactly (schedule rows: id/seed/t_h/day/reason/status/fired_t_h; daily
    state rows: day/M/m/g/p/arg/mu/eta/cycle_day/phase_label/seed/score;
    messages: id/role/content/t_h/day/proactive; judgements: day/score/
    justification/model/shadow; events: id/day/t_h/event/detail)."""

    def __init__(self):
        self._daily: dict[int, dict] = {}
        self._judgements: dict[int, dict] = {}
        self._messages: list[dict] = []
        self._events: list[dict] = []
        self._schedule: dict[tuple[int, float], dict] = {}
        self._agendas: dict[int, DailyAgenda] = {}
        self._agenda_items: dict[str, AgendaItem] = {}
        self._arcs: dict[str, LifeArc] = {}
        self._episodes: dict[str, EpisodicMemory] = {}
        self._episode_order: list[str] = []
        self._interests: list[Interest] = []
        self._intents: dict[str, tuple[ProactiveIntent, str]] = {}
        self._opportunities: dict[str, object] = {}
        self._next_id = 1

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        pass

    # -- daily state + judgements ------------------------------------------

    def save_daily_state(self, day: int, record: dict) -> None:
        self._daily[day] = dict(record)

    def load_daily_state(self, day: int) -> dict | None:
        return dict(self._daily[day]) if day in self._daily else None

    def latest_daily_state(self) -> dict | None:
        if not self._daily:
            return None
        day = max(self._daily)
        return dict(self._daily[day])

    def update_daily_score(self, day: int, score: float) -> None:
        if day in self._daily:
            self._daily[day]["score"] = score

    def save_judgement(self, day, score, justification, model, shadow) -> None:
        self._judgements[day] = {
            "day": day, "score": score, "justification": justification,
            "model": model, "shadow": int(bool(shadow)),
        }

    def load_judgement(self, day: int) -> dict | None:
        return dict(self._judgements[day]) if day in self._judgements else None

    def load_previous_judgement(self, day: int) -> float | None:
        j = self._judgements.get(day - 1)
        return float(j["score"]) if j else None

    # -- messages -----------------------------------------------------------

    def add_message(self, role, content, t_h, day, proactive, *,
                    session_id=None, intent_id=None) -> int:
        row = {
            "id": self._next_id, "role": role, "content": content,
            "t_h": float(t_h), "day": int(day), "proactive": int(bool(proactive)),
            "session_id": session_id, "intent_id": intent_id,
        }
        self._next_id += 1
        self._messages.append(row)
        return row["id"]

    def update_message_intent_id(self, message_id: int, intent_id: str) -> None:
        """Attach exact-intent provenance to an already-stored message row
        (mirrors A7 M1's messages.intent_id on the seam store)."""
        for m in self._messages:
            if m["id"] == message_id:
                m["intent_id"] = intent_id
                return
        raise KeyError(f"no message with id {message_id}")

    def recent_messages(self, limit: int = 12) -> list[dict]:
        return [dict(m) for m in self._messages[-limit:]]

    def messages_for_day(self, day: int) -> list[dict]:
        return [dict(m) for m in self._messages if m["day"] == day]

    def proactive_count(self, day: int) -> int:
        return sum(1 for m in self._messages if m["day"] == day and m["proactive"])

    # -- audit log ----------------------------------------------------------

    def log_event(self, day, t_h, event, detail=None) -> None:
        self._events.append({
            "id": len(self._events) + 1, "day": int(day),
            "t_h": float(t_h), "event": event, "detail": detail,
        })

    def log_llm_call(self, day, t_h, role, prompt, response, model, meta=None) -> None:
        pass

    def events_since(self, day: int) -> list[dict]:
        return [dict(e) for e in self._events if e["day"] >= day]

    # -- schedule_events (same semantics as SQLiteStore) --------------------

    def save_schedule_events(self, seed: int, events: list[dict]) -> None:
        for e in events:
            key = (seed, float(e["t_h"]))
            if key not in self._schedule:
                self._schedule[key] = {
                    "id": len(self._schedule) + 1, "seed": seed,
                    "t_h": float(e["t_h"]), "day": int(e["day"]),
                    "reason": e["reason"], "status": "pending", "fired_t_h": None,
                }

    def pending_schedule_events(self, seed: int) -> list[dict]:
        rows = [r for (s, _), r in self._schedule.items()
                if s == seed and r["status"] == "pending"]
        return [dict(r) for r in sorted(rows, key=lambda r: r["t_h"])]

    def schedule_events_for_seed(self, seed: int) -> list[dict]:
        rows = [r for (s, _), r in self._schedule.items() if s == seed]
        return [dict(r) for r in sorted(rows, key=lambda r: r["t_h"])]

    def mark_schedule_fired(self, seed: int, t_h: float, fired_t_h: float) -> None:
        key = (seed, float(t_h))
        if key in self._schedule:
            self._schedule[key]["status"] = "fired"
            self._schedule[key]["fired_t_h"] = float(fired_t_h)

    def mark_schedule_expired(self, seed: int, t_h: float) -> None:
        key = (seed, float(t_h))
        if key in self._schedule:
            self._schedule[key]["status"] = "expired"

    def last_proactive_t_h(self, seed: int) -> float | None:
        fired = [r["fired_t_h"] for (s, _), r in self._schedule.items()
                 if s == seed and r["status"] == "fired" and r["fired_t_h"] is not None]
        return max(fired) if fired else None

    # -- persona + interests ------------------------------------------------

    def save_interests(self, interests: list[Interest]) -> None:
        self._interests = list(interests)

    def list_interests(self) -> list[Interest]:
        return list(self._interests)

    # -- life arcs ----------------------------------------------------------

    def upsert_life_arc(self, arc: LifeArc) -> None:
        self._arcs[arc.id] = arc

    def get_life_arc(self, arc_id: str) -> LifeArc | None:
        return self._arcs.get(arc_id)

    def list_life_arcs(self, status: str | None = None) -> list[LifeArc]:
        arcs = list(self._arcs.values())
        if status is not None:
            arcs = [a for a in arcs if a.status == status]
        return arcs

    def update_life_arc_status(self, arc_id: str, status: str) -> None:
        arc = self._arcs.get(arc_id)
        if arc is not None:
            self._arcs[arc_id] = replace(arc, status=status)

    # -- agenda -------------------------------------------------------------

    def save_agenda(self, day: int, agenda: DailyAgenda) -> None:
        self._agendas[day] = agenda
        for item in agenda.items:
            self._agenda_items[item.id] = item

    def load_agenda(self, day: int) -> DailyAgenda | None:
        return self._agendas.get(day)

    def list_agenda_items(self, day: int | None = None,
                          status: str | None = None) -> list[AgendaItem]:
        items = list(self._agenda_items.values())
        if day is not None:
            items = [i for i in items if int(i.start_t_h // 24.0) == day]
        if status is not None:
            items = [i for i in items if i.status == status]
        return items

    def update_agenda_item_status(self, item_id: str, status: str) -> None:
        item = self._agenda_items.get(item_id)
        if item is not None:
            updated = replace(item, status=status)
            self._agenda_items[item_id] = updated
            for day, agenda in self._agendas.items():
                if any(i.id == item_id for i in agenda.items):
                    self._agendas[day] = replace(
                        agenda,
                        items=tuple(updated if i.id == item_id else i
                                    for i in agenda.items),
                    )

    # -- memory tiers (episodes only — the resolver's seam surface) ---------

    def insert_episode(self, ep: EpisodicMemory) -> str:
        self._episodes[ep.id] = ep
        self._episode_order.append(ep.id)
        return ep.id

    def get_episode(self, episode_id: str) -> EpisodicMemory | None:
        return self._episodes.get(episode_id)

    def list_episodes(self, limit: int = 500,
                      category: MemoryKind | None = None) -> list[EpisodicMemory]:
        eps = [self._episodes[i] for i in self._episode_order
               if i in self._episodes]
        if category is not None:
            eps = [e for e in eps if e.category == category]
        return eps[-limit:]

    # -- proactive intents --------------------------------------------------

    def save_proactive_intent(self, intent: ProactiveIntent) -> None:
        self._intents[intent.id] = (intent, "pending")

    def load_proactive_intent(self, intent_id: str) -> ProactiveIntent | None:
        entry = self._intents.get(intent_id)
        return entry[0] if entry else None

    def list_proactive_intents(self, status: str | None = None) -> list[ProactiveIntent]:
        # most-recent-first, mirroring SQLiteStore's created_t_h DESC ordering
        out = [i for i, s in self._intents.values() if status is None or s == status]
        return sorted(out, key=lambda i: i.created_t_h, reverse=True)

    def update_proactive_intent_status(self, intent_id: str, status: str) -> None:
        entry = self._intents.get(intent_id)
        if entry is not None:
            self._intents[intent_id] = (entry[0], status)

    def resolve_intent_source(self, intent: ProactiveIntent):
        """AgendaItem | LifeArc | EpisodicMemory | None, by source_type/id."""
        if intent.source_type in (SOURCE_AGENDA, SOURCE_LIFE_EVENT):
            return self._agenda_items.get(intent.source_id)
        if intent.source_type == "arc":
            return self._arcs.get(intent.source_id)
        if intent.source_type in (SOURCE_CALLBACK, SOURCE_SHARED_INTEREST,
                                  SOURCE_CHECK_IN):
            return self._episodes.get(intent.source_id)
        return None

    # -- contact opportunities (it2 A3 optional persistence seam) -----------

    def save_contact_opportunity(self, opp) -> None:
        """Optional A7 seam for ContactOpportunity persistence (the real
        SQLiteStore has no such table yet — flagged in the A3 handoff)."""
        self._opportunities[opp.id] = opp

    def load_contact_opportunities(self) -> list:
        return list(self._opportunities.values())

    # -- interactions -------------------------------------------------------

    def latest_interaction_t_h(self) -> float | None:
        user = [m["t_h"] for m in self._messages if m["role"] == "user"]
        return max(user) if user else None


# --------------------------------------------------------------------------- #
# fixtures + helpers
# --------------------------------------------------------------------------- #

AGENDA_HOUR = 14.0  # a safe awake hour (quiet hours are 23..8)


def _agenda_item(item_id="a1", *, start=None, end=None, status="planned",
                 activity="pottery class", salience=0.7, source_type="arc",
                 source_id="arc_pottery"):
    start = AGENDA_HOUR if start is None else start
    end = AGENDA_HOUR + 2.0 if end is None else end
    return AgendaItem(
        id=item_id, start_t_h=start, end_t_h=end, activity=activity,
        source_type=source_type, source_id=source_id, salience=salience,
        status=status,
    )


def _episode(ep_id="e1", *, category=MemoryKind.SHARED_EPISODE, occurred=None,
             importance=0.6, tags=(), summary="we talked about the dog park"):
    occurred = AGENDA_HOUR - 5.0 if occurred is None else occurred
    return EpisodicMemory(
        id=ep_id, summary=summary, category=category,
        occurred_at_t_h=occurred, created_at_t_h=occurred,
        importance=importance, access_count=0, last_accessed_t_h=None,
        affect=None, source_session_id="s1", source_turn_ids=(1,),
        verbatim_anchors=(), tags=tuple(tags),
    )


def _interest(name="photography", bucket="exact", salience=0.8):
    return Interest(name=name, bucket=bucket, salience=salience)


@pytest.fixture
def store():
    return SeamStore()


@pytest.fixture
def resolver(store):
    return IntentResolver(store, rng=rng_mod.stream_rng(7))


# --------------------------------------------------------------------------- #
# no grounded candidate ⇒ None (SUPPRESS)
# --------------------------------------------------------------------------- #


def test_resolve_none_on_empty_store(store):
    resolver = IntentResolver(store)
    assert resolver.resolve(AGENDA_HOUR) is None


def test_resolve_none_when_only_distant_agenda(store, resolver):
    store.save_agenda(0, DailyAgenda(0, (_agenda_item(start=AGENDA_HOUR + 12.0),)))
    assert resolver.resolve(AGENDA_HOUR) is None  # beyond the current/recent margin


# --------------------------------------------------------------------------- #
# agenda candidates (current/recent)
# --------------------------------------------------------------------------- #


def test_resolve_agenda_item_current(store, resolver):
    item = _agenda_item()
    store.save_agenda(0, DailyAgenda(0, (item,)))
    intent = resolver.resolve(AGENDA_HOUR + 0.5)
    assert intent is not None
    assert intent.source_type == SOURCE_AGENDA
    assert intent.source_id == item.id
    assert intent.reason == REASON_SCHEDULE
    assert intent.hook == compose_hook(item, REASON_SCHEDULE)
    assert intent.valid_until_t_h == pytest.approx(
        AGENDA_HOUR + 0.5 + REASON_VALIDITY_H[REASON_SCHEDULE]
    )
    assert item.id in intent.evidence and item.activity in intent.evidence
    assert intent.salience > 0.0


def test_resolve_recent_ended_agenda_item_within_margin(store, resolver):
    item = _agenda_item(start=AGENDA_HOUR - 3.0, end=AGENDA_HOUR - 1.0)
    store.save_agenda(0, DailyAgenda(0, (item,)))
    # 1.5 h after the slot ended — still inside AGENDA_MARGIN_H (2 h)
    intent = resolver.resolve(AGENDA_HOUR + 0.5)
    assert intent is not None and intent.source_id == item.id


def test_shifted_agenda_item_is_a_candidate(store, resolver):
    item = _agenda_item(status="shifted", start=AGENDA_HOUR, end=AGENDA_HOUR + 1.0)
    store.save_agenda(0, DailyAgenda(0, (item,)))
    assert resolver.resolve(AGENDA_HOUR + 0.2) is not None


def test_skipped_agenda_item_is_not_a_candidate(store, resolver):
    store.save_agenda(0, DailyAgenda(0, (_agenda_item(status="skipped"),)))
    assert resolver.resolve(AGENDA_HOUR + 0.2) is None


# --------------------------------------------------------------------------- #
# completed agenda items = companion life events
# --------------------------------------------------------------------------- #


def test_resolve_completed_agenda_item_as_life_event(store, resolver):
    item = _agenda_item(item_id="done1", status="completed",
                        start=AGENDA_HOUR - 4.0, end=AGENDA_HOUR - 2.0)
    store.save_agenda(0, DailyAgenda(0, (item,)))
    intent = resolver.resolve(AGENDA_HOUR)
    assert intent is not None
    assert intent.source_type == SOURCE_LIFE_EVENT
    assert intent.source_id == item.id
    assert intent.reason == REASON_EVENT
    assert intent.hook == compose_hook(item, REASON_EVENT)


def test_old_completed_item_not_a_life_event(store, resolver):
    item = _agenda_item(item_id="old", status="completed",
                        start=AGENDA_HOUR - 60.0, end=AGENDA_HOUR - 58.0)
    store.save_agenda(0, DailyAgenda(0, (item,)))
    assert resolver.resolve(AGENDA_HOUR) is None  # beyond LIFE_EVENT_RECENCY_H


# --------------------------------------------------------------------------- #
# CALLBACK memories
# --------------------------------------------------------------------------- #


def test_resolve_callback_memory(store, resolver):
    ep = _episode(category=MemoryKind.CALLBACK, summary="user asked me to remind them")
    store.insert_episode(ep)
    intent = resolver.resolve(AGENDA_HOUR)
    assert intent is not None
    assert intent.source_type == SOURCE_CALLBACK
    assert intent.reason == REASON_CALLBACK
    assert intent.hook == compose_hook(ep, REASON_CALLBACK)
    assert ep.id in intent.evidence


def test_old_callback_memory_not_a_candidate(store, resolver):
    ep = _episode(category=MemoryKind.CALLBACK, occurred=AGENDA_HOUR - 200.0)
    store.insert_episode(ep)
    assert resolver.resolve(AGENDA_HOUR) is None


# --------------------------------------------------------------------------- #
# shared-interest memories
# --------------------------------------------------------------------------- #


def test_resolve_shared_interest_memory(store, resolver):
    store.save_interests([_interest("photography")])
    ep = _episode(tags=("photography",), summary="user showed me their camera")
    store.insert_episode(ep)
    intent = resolver.resolve(AGENDA_HOUR)
    assert intent is not None
    assert intent.source_type == SOURCE_SHARED_INTEREST
    assert intent.reason == REASON_SHARED_INTEREST
    assert intent.hook == compose_hook(ep, REASON_SHARED_INTEREST)
    assert "photography" in intent.evidence


def test_episode_without_interest_tag_is_not_shared_interest(store, resolver):
    store.save_interests([_interest("photography")])
    store.insert_episode(_episode(tags=("cooking",)))
    assert resolver.resolve(AGENDA_HOUR) is None


def test_no_interests_means_no_shared_interest_candidates(store, resolver):
    store.insert_episode(_episode(tags=("photography",)))
    assert resolver.resolve(AGENDA_HOUR) is None


# --------------------------------------------------------------------------- #
# legitimate check-in context
# --------------------------------------------------------------------------- #


def test_resolve_check_in_morning_without_recent_contact(store, resolver):
    store.insert_episode(_episode())  # shared history anchors the check-in
    intent = resolver.resolve(9.0)  # 09:00 local — inside the morning window
    assert intent is not None
    assert intent.source_type == SOURCE_CHECK_IN
    assert intent.reason == REASON_CHECK_IN
    assert intent.hook == compose_hook(store.get_episode(intent.source_id),
                                       REASON_CHECK_IN)
    assert "gap_h" in intent.evidence


def test_check_in_blocked_by_recent_contact(store, resolver):
    store.insert_episode(_episode())
    store.add_message("user", "hi", t_h=8.5, day=0, proactive=False)
    # now=9.0, last contact 8.5 → gap 0.5 h < CHECK_IN_MIN_GAP_H
    assert resolver.resolve(9.0) is None


def test_check_in_only_in_time_of_day_windows(store, resolver):
    store.insert_episode(_episode())
    assert resolver.resolve(14.0) is None  # mid-afternoon: not a check-in window


def test_check_in_needs_shared_history(store, resolver):
    assert resolver.resolve(9.0) is None  # blank slate → not grounded


# --------------------------------------------------------------------------- #
# ranking + determinism
# --------------------------------------------------------------------------- #


def test_high_salience_recent_beats_low_salience_stale(store, resolver):
    strong = _agenda_item(item_id="strong", salience=0.9,
                          start=AGENDA_HOUR, end=AGENDA_HOUR + 1.0)
    stale = _agenda_item(item_id="stale", salience=0.5,
                         start=AGENDA_HOUR - 1.8, end=AGENDA_HOUR - 1.6)
    store.save_agenda(0, DailyAgenda(0, (strong, stale)))
    intent = resolver.resolve(AGENDA_HOUR + 0.1)
    assert intent is not None and intent.source_id == "strong"


def test_shared_interest_validity_beats_equal_agenda(store, resolver):
    # Same salience + recency profile: the 12 h shared-interest window
    # (validity factor 2.0) outranks the 3 h schedule window (0.5).
    store.save_interests([_interest("photography")])
    ep = _episode(tags=("photography",), importance=0.7)
    store.insert_episode(ep)
    item = _agenda_item(salience=0.7)
    store.save_agenda(0, DailyAgenda(0, (item,)))
    intent = resolver.resolve(AGENDA_HOUR)
    assert intent is not None
    assert intent.source_type == SOURCE_SHARED_INTEREST


def test_seeded_tie_break_is_deterministic(store):
    # Two identical candidates → the seeded rng picks one, reproducibly.
    a = _agenda_item(item_id="tie_a", salience=0.5,
                     start=AGENDA_HOUR, end=AGENDA_HOUR + 1.0)
    b = _agenda_item(item_id="tie_b", salience=0.5,
                     start=AGENDA_HOUR, end=AGENDA_HOUR + 1.0)
    store.save_agenda(0, DailyAgenda(0, (a, b)))
    r1 = IntentResolver(store, rng=rng_mod.stream_rng(42))
    r2 = IntentResolver(store, rng=rng_mod.stream_rng(42))
    i1 = r1.resolve(AGENDA_HOUR + 0.1)
    i2 = r2.resolve(AGENDA_HOUR + 0.1)
    assert i1 is not None and i2 is not None
    assert i1.source_id == i2.source_id
    assert i1.id == i2.id


def test_hook_is_deterministic_and_source_derived(store, resolver):
    item = _agenda_item()
    store.save_agenda(0, DailyAgenda(0, (item,)))
    i1 = resolver.resolve(AGENDA_HOUR + 0.2)
    i2 = resolver.resolve(AGENDA_HOUR + 0.2)
    assert i1 is not None and i2 is not None
    assert i1.hook == i2.hook
    assert i1.hook == compose_hook(item, REASON_SCHEDULE)
    # never invented free text: the hook is a template over source fields
    assert item.activity in i1.hook


# --------------------------------------------------------------------------- #
# intent shape (domain invariant 3: no optional source fields)
# --------------------------------------------------------------------------- #


def test_intent_is_fully_grounded(store, resolver):
    item = _agenda_item()
    store.save_agenda(0, DailyAgenda(0, (item,)))
    intent = resolver.resolve(AGENDA_HOUR)
    assert intent is not None
    for field in ("id", "reason", "source_type", "source_id", "hook", "evidence"):
        assert getattr(intent, field), f"{field} must be non-empty"
    assert intent.valid_until_t_h > intent.created_t_h
    assert 0.0 <= intent.salience <= 1.0


# --------------------------------------------------------------------------- #
# SeamStore sanity (used by runtime/gates tests too)
# --------------------------------------------------------------------------- #


def test_seamstore_schedule_semantics_match_sqlite():
    store = SeamStore()
    store.save_schedule_events(1, [{"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE}])
    store.mark_schedule_fired(1, 10.0, fired_t_h=10.4)
    # INSERT OR IGNORE: re-saving must not resurrect the fired row
    store.save_schedule_events(1, [{"t_h": 10.0, "day": 0, "reason": REASON_SCHEDULE}])
    assert store.pending_schedule_events(1) == []
    assert store.last_proactive_t_h(1) == 10.4
    rows = store.schedule_events_for_seed(1)
    assert set(rows[0]) == {"id", "seed", "t_h", "day", "reason", "status", "fired_t_h"}
    assert rows[0]["status"] == "fired"
    store.close()


def test_seamstore_latest_interaction_tracks_user_messages():
    store = SeamStore()
    assert store.latest_interaction_t_h() is None
    store.add_message("assistant", "hi", t_h=1.0, day=0, proactive=True)
    assert store.latest_interaction_t_h() is None  # assistant msgs don't count
    store.add_message("user", "hello", t_h=2.5, day=0, proactive=False)
    assert store.latest_interaction_t_h() == 2.5


def test_seamstore_proactive_intent_status_lifecycle():
    store = SeamStore()
    item = _agenda_item()
    store.save_agenda(0, DailyAgenda(0, (item,)))
    intent = IntentResolver(store).resolve(AGENDA_HOUR)
    assert intent is not None
    store.save_proactive_intent(intent)
    assert store.load_proactive_intent(intent.id) == intent
    store.update_proactive_intent_status(intent.id, "fired")
    assert [i.id for i in store.list_proactive_intents(status="fired")] == [intent.id]
    assert store.list_proactive_intents(status="pending") == []
    # gate resolves the source back
    assert store.resolve_intent_source(intent) == item
