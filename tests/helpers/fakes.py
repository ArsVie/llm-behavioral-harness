"""Store fakes — SeamStore (ex test_proactive) and FakeStore (ex test_memory).

Both fakes are moved VERBATIM from their home test files; the only
intentional change is the ``save_judgement`` divergence fix mandated by the
review: both now carry the uniform signature

    save_judgement(day, score, justification="", model="test", shadow=1)

(FakeStore already had it; SeamStore previously required all five
positionally. The SQLiteStore requires all five, so callers that passed
positional args keep working; callers that passed ``store.save_judgement(day,
score)`` now hit the defaulted kwargs instead of a TypeError.)
"""

from __future__ import annotations

from dataclasses import replace

from harness.domain import (
    AgendaItem,
    DailyAgenda,
    EpisodicMemory,
    Interest,
    LifeArc,
    MemoryKind,
    ProactiveIntent,
    SessionSummary,
    UserModel,
    UserModelAssertion,
    UserModelCategory,
)
from harness.proactive import (
    SOURCE_AGENDA,
    SOURCE_CALLBACK,
    SOURCE_CHECK_IN,
    SOURCE_LIFE_EVENT,
    SOURCE_SHARED_INTEREST,
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

    def save_judgement(self, day, score, justification="", model="test",
                       shadow=1) -> None:
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
# FakeStore — in-memory mirror of the A7 §15 store contract (canonical L4)
# --------------------------------------------------------------------------- #

_LEGACY_PREFIX_CATEGORIES = [
    ("identity", UserModelCategory.IDENTITY),
    ("stable_preferences", UserModelCategory.STABLE_PREFERENCE),
    ("current_preferences", UserModelCategory.CURRENT_PREFERENCE),
    ("preference", UserModelCategory.CURRENT_PREFERENCE),
    ("boundaries", UserModelCategory.BOUNDARY),
    ("boundary", UserModelCategory.BOUNDARY),
    ("vulnerabilities", UserModelCategory.VULNERABILITY),
    ("vulnerability", UserModelCategory.VULNERABILITY),
    ("recurring_interests", UserModelCategory.RECURRING_INTEREST),
    ("interest", UserModelCategory.RECURRING_INTEREST),
    ("relationship_patterns", UserModelCategory.RELATIONSHIP_PATTERN),
    ("relationship", UserModelCategory.RELATIONSHIP_PATTERN),
    ("important_entities", UserModelCategory.IMPORTANT_ENTITY),
    ("entity", UserModelCategory.IMPORTANT_ENTITY),
]

_CATEGORY_TO_GROUP = {
    UserModelCategory.STABLE_PREFERENCE: "stable_preferences",
    UserModelCategory.CURRENT_PREFERENCE: "current_preferences",
    UserModelCategory.BOUNDARY: "boundaries",
    UserModelCategory.VULNERABILITY: "vulnerabilities",
    UserModelCategory.RECURRING_INTEREST: "recurring_interests",
    UserModelCategory.RELATIONSHIP_PATTERN: "relationship_patterns",
    UserModelCategory.IMPORTANT_ENTITY: "important_entities",
}


def _legacy_category_from_key(key: str) -> UserModelCategory:
    """Mirror of the A7 store's write-time derivation for legacy keys."""
    head, _, _ = key.partition(":")
    if key == "identity" or head == "identity":
        return UserModelCategory.IDENTITY
    for prefix, cat in _LEGACY_PREFIX_CATEGORIES:
        if head == prefix:
            return cat
    return UserModelCategory.IMPORTANT_ENTITY


class FakeStore:
    """In-memory mirror of the A7 §15 store contract (canonical L4 categories).

    Matches SQLiteStore's Iteration-2 semantics: ``upsert_assertion``
    accepts the canonical ``category`` kwarg and stores it on the row;
    ``load_user_model`` buckets current assertions by their STORED category
    (never by parsing keys); ``get_assertion`` returns the most recent row
    of a key regardless of status (full history, pitfall 38).
    """

    def __init__(self) -> None:
        self._messages: list[dict] = []
        self._next_id = 1
        self._judgements: dict[int, dict] = {}
        self._summaries: dict[str, SessionSummary] = {}
        self._episodes: dict[str, EpisodicMemory] = {}
        self._embeddings: dict[str, list[float]] = {}
        self._assertions: list[UserModelAssertion] = []
        self._assertion_categories: list[UserModelCategory] = []

    # -- L1 (messages) ------------------------------------------------------
    def add_message(self, role, content, t_h, day, proactive=False, session_id=None) -> int:
        row = {
            "id": self._next_id,
            "role": role,
            "content": content,
            "t_h": float(t_h),
            "day": day,
            "proactive": int(proactive),
            "session_id": session_id,
        }
        self._next_id += 1
        self._messages.append(row)
        return row["id"]

    def recent_messages(self, limit: int = 12) -> list[dict]:
        # Mirrors the real store: newest `limit` rows, oldest -> newest.
        return self._messages[-limit:]

    def messages_for_day(self, day: int) -> list[dict]:
        return [m for m in self._messages if m["day"] == day]

    def messages_for_session(self, session_id: str) -> list[dict]:
        return [m for m in self._messages if m.get("session_id") == session_id]

    def load_judgement(self, day: int) -> dict | None:
        return self._judgements.get(day)

    def save_judgement(self, day, score, justification="", model="test", shadow=1) -> None:
        self._judgements[day] = {
            "day": day, "score": score, "justification": justification,
            "model": model, "shadow": shadow,
        }

    # -- L2 ----------------------------------------------------------------
    def save_session_summary(self, summary: SessionSummary) -> None:
        self._summaries[summary.session_id] = summary

    def load_session_summary(self, session_id: str) -> SessionSummary | None:
        return self._summaries.get(session_id)

    # -- L3 ----------------------------------------------------------------
    def insert_episode(self, ep: EpisodicMemory) -> str:
        self._episodes[ep.id] = ep
        return ep.id

    def get_episode(self, episode_id: str) -> EpisodicMemory | None:
        return self._episodes.get(episode_id)

    def list_episodes(self, limit: int = 500, category=None) -> list[EpisodicMemory]:
        eps = list(self._episodes.values())
        if category is not None:
            eps = [e for e in eps if e.category == category]
        return eps[:limit]

    def touch_episode(self, episode_id: str, t_h: float) -> None:
        ep = self._episodes.get(episode_id)
        if ep is not None:
            self._episodes[episode_id] = replace(
                ep, access_count=ep.access_count + 1, last_accessed_t_h=float(t_h)
            )

    def save_embedding(self, episode_id: str, vector: list[float]) -> None:
        self._embeddings[episode_id] = list(vector)

    def load_embeddings(self) -> list[tuple[str, list[float]]]:
        return list(self._embeddings.items())

    # -- L4 ----------------------------------------------------------------
    def upsert_assertion(self, a: UserModelAssertion, *, category=None) -> None:
        """Insert an assertion; a new ``current`` one supersedes (status
        flip, provenance kept) the previous ``current`` row of the same key.
        The canonical category is stored on the row (A7 contract)."""
        if category is None:
            cat = _legacy_category_from_key(a.key)
        elif isinstance(category, UserModelCategory):
            cat = category
        else:
            cat = UserModelCategory(str(category))  # canonical only; raises
        if a.status == "current":
            for i, old in enumerate(self._assertions):
                if old.key == a.key and old.status == "current":
                    self._assertions[i] = replace(old, status="superseded")
        self._assertions.append(a)
        self._assertion_categories.append(cat)

    def list_assertions(self, status: str | None = "current", category=None) -> list[UserModelAssertion]:
        out = []
        for a, cat in zip(self._assertions, self._assertion_categories):
            if status is not None and a.status != status:
                continue
            if category is not None:
                want = (
                    category.value
                    if isinstance(category, UserModelCategory)
                    else UserModelCategory(str(category)).value
                )
                if cat.value != want:
                    continue
            out.append(a)
        return out

    def supersede_assertion(self, key: str, *, source_memory_ids=None, updated_at_t_h=None) -> None:
        """Flip every current assertion of ``key`` to superseded; optional
        provenance/timestamp rewrite (A9 M-1b leg)."""
        for i, old in enumerate(self._assertions):
            if old.key == key and old.status == "current":
                kw: dict = {"status": "superseded"}
                if source_memory_ids is not None:
                    kw["source_memory_ids"] = tuple(source_memory_ids)
                if updated_at_t_h is not None:
                    kw["updated_at_t_h"] = float(updated_at_t_h)
                self._assertions[i] = replace(old, **kw)

    def get_assertion(self, key: str) -> UserModelAssertion | None:
        """Most recent assertion row for ``key`` (any status — full history)."""
        for a in reversed(self._assertions):
            if a.key == key:
                return a
        return None

    def get_assertion_category(self, key: str) -> UserModelCategory | None:
        """Canonical category of the most recent assertion row for ``key``."""
        for i in range(len(self._assertions) - 1, -1, -1):
            if self._assertions[i].key == key:
                return self._assertion_categories[i]
        return None

    def load_user_model(self) -> UserModel:
        identity = ""
        buckets: dict[str, tuple[UserModelAssertion, ...]] = {
            name: ()
            for name in (
                "stable_preferences", "current_preferences", "boundaries",
                "vulnerabilities", "recurring_interests", "relationship_patterns",
                "important_entities",
            )
        }
        for a, cat in zip(self._assertions, self._assertion_categories):
            if a.status != "current":
                continue
            if cat is UserModelCategory.IDENTITY:
                identity = a.value  # projection string, not an assertion tuple
            else:
                group = _CATEGORY_TO_GROUP[cat]
                buckets[group] = buckets[group] + (a,)
        return UserModel(identity=identity, **buckets)
