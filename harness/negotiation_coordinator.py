"""Session's availability-negotiation half: a mixin, never instantiated alone.

Reaches freely into Session's attributes (``_negotiations``, ``_conversation``,
``_steering``, ``_decision``, ``store``, ``client``, clock helpers).
"""

from __future__ import annotations

import json

from harness.faces import strip_face
from harness.negotiation_contract import (
    DEFER_TURNS_KEY,
    SHORT_AFK_H,
    NegotiationEpisode,
    NegotiationPhase,
    is_skippable,
)
from harness.negotiation_episodes import emit_negotiation_episode
from harness.negotiation_state import (
    NegotiationState,
    decide_status_at,
    map_defer_n,
    next_trigger_t_h,
    pull_toward_go,
    rearm_after_delay,
    state_from_dict,
    state_to_dict,
    window_ending_at,
)
from harness.steering import KIND_EVENT_POPUP, Steer

#: Departure note appended to the turn when a go verdict resolves; says WHAT she
#: decided and leaves the wording to her (the verdict's ``reason`` stays internal).
GO_NOTE = (
    "You have decided to go to {activity} now. Say what you would say as you "
    "leave, in your own words, and then go."
)

#: The note appended when an event-start verdict says she initiates.
START_NOTE = (
    "You have just started {activity}, and you are the one reaching out about "
    "it. Open in your own words."
)


class NegotiationMixin:
    """Session's availability-negotiation half. Never instantiated alone."""

    #: The drain of the turn currently being generated; a verdict resolving
    #: mid-turn writes its note here. None = clock-driven path (channel fallback).
    _active_drain = None

    def _restore_negotiations(self) -> dict[str, NegotiationState]:
        """Rebuild active negotiations from persisted ``negotiation_state`` state
        events (latest per item wins); a restart resumes without re-Informing."""
        out: dict[str, NegotiationState] = {}
        if not hasattr(self.store, "events_since"):
            return out
        for event in self.store.events_since(0):
            if event.get("event") != "negotiation_state":
                continue
            st = state_from_dict(json.loads(event.get("detail") or "{}"))
            if st is not None:
                out[st.item_id] = st
        return out

    def _persist_negotiation(self, st: NegotiationState, t_h: float) -> None:
        """Persist one negotiation as a full JSON snapshot state event."""
        if not hasattr(self.store, "log_event"):
            return
        self.store.log_event(
            int(t_h // 24.0), t_h, "negotiation_state",
            json.dumps(state_to_dict(st), sort_keys=True),
        )

    def _find_agenda_item(self, item_id: str, day: int):
        """Today's AgendaItem by id (its source_type/salience/end_t_h drive the
        negotiation)."""
        items = (
            self.store.list_agenda_items(day=day)
            if hasattr(self.store, "list_agenda_items")
            else ()
        )
        for it in items:
            if it.id == item_id:
                return it
        return None

    def _afk_anchor(self) -> float | None:
        """The AFK bomb's anchor: the conversation's last USER turn (or its opening
        when the user never replied). None when no conversation is open."""
        conv = self._conversation
        if conv is None:
            return None
        return self._user_anchor_t_h(conv)

    def next_negotiation_trigger_t_h(self, now: float) -> float | None:
        """Next strictly-future negotiation wake instant for the runtime's rollover
        park: the earliest AFK-bomb decide or window-close backstop. None when no
        negotiation is pending."""
        candidates: list[float] = []
        for st in self._negotiations.values():
            nxt = next_trigger_t_h(st, now)
            if nxt is not None:
                candidates.append(nxt)
        return min(candidates) if candidates else None

    def check_negotiation(self, now: float) -> tuple[tuple[str, str], ...]:
        """Runtime wake hook: run event-boundary detection and every due decide leg
        (AFK bomb or window close) of the active negotiations. Returns the
        channel messages, always none: a decision outside a conversation is
        Telegram-silent. At most one decide leg per virtual instant per item,
        and one decision call per wake (owner ruling 2026-10-03, one pop-up per
        turn): a further due leg stays due and runs at the next wake."""
        if self._decision is None or self._steering is None:
            return ()
        day = int(now // 24.0)
        if self._decision_enabled:
            self._enqueue_event_popups(day, now)
        calls_before = self._decision_calls
        for item_id in list(self._negotiations):
            st = self._negotiations[item_id]
            status = decide_status_at(st, now=now, companion_turn=False)
            if status == "forced":
                self._resolve_forced(st, now)
            elif status == "due" and self._decision_calls > calls_before:
                continue
            elif status == "due":
                self._run_decide_leg(st, day, now, afk_path=True)
        return ()

    def _maybe_heads_up(
        self,
        item_id: str,
        day: int,
        t_h: float,
        steer: Steer,
        proactive_out: list[tuple[str, str]],
    ) -> None:
        """The "incoming event" steer: fire INFORM ``HEADS_UP_LEAD_H`` ahead of the
        window. No verdict; requires an OPEN conversation, so an unobserved stretch
        skips it. Idempotent via the responded-bool ``informed``, so a re-delivered
        steer never announces twice."""
        if self._decision is None or self._steering is None:
            return
        conv = self._conversation
        if conv is None:
            return
        st = self._negotiations.get(item_id)
        if st is not None:
            # Re-delivered heads-up: re-run Inform only while ``informed`` is
            # not True; an already-informed negotiation just consumes the steer.
            if (
                st.phase == NegotiationPhase.INFORM.value
                and st.informed is not True
            ):
                self._run_inform(st, day, t_h, steer, proactive_out)
            return
        item = self._find_agenda_item(item_id, day)
        if item is None or item.status != "planned":
            return
        if t_h >= item.start_t_h - 1e-12 or t_h >= item.end_t_h - 1e-12:
            return                      # not a lead-time instant any more
        self._open_negotiation(item, day, t_h, steer, proactive_out)

    def _open_negotiation(self, item, day: int, t_h: float, steer: Steer,
                          proactive_out: list[tuple[str, str]]) -> None:
        """Start a negotiation for ``item``, persist it and run its Inform leg."""
        st = NegotiationState(
            item_id=item.id,
            activity=item.activity,
            source_type=item.source_type,
            start_t_h=item.start_t_h,
            end_t_h=item.end_t_h,
            salience=item.salience,
        )
        self._negotiations[item.id] = st
        self._persist_negotiation(st, t_h)
        self._run_inform(st, day, t_h, steer, proactive_out)

    def _maybe_start_negotiation(
        self,
        item_id: str,
        day: int,
        t_h: float,
        steer: Steer,
        proactive_out: list[tuple[str, str]],
    ) -> bool:
        """Route a START event pop-up into the negotiation: True when the machine
        consumes the pop-up, False for the plain start-popup semantics.

        Starts only when a conversation was open at the item's boundary
        (``conv.opened_t_h <= start_t_h``); an existing negotiation re-runs Inform
        only while ``informed`` is not True, and a resolved one is just consumed."""
        if self._decision is None or self._steering is None:
            return False
        conv = self._conversation
        st = self._negotiations.get(item_id)
        if st is not None and conv is None and st.informed is True:
            # Informed, it is the machine's to decide even after the
            # conversation closed: handing the pop-up to plain semantics
            # decided it twice (live 2026-09-21 21:50 — the pop-up here, then
            # the negotiation's own decide leg at the next wake).
            return True
        if conv is None:
            return False  # no open conversation: plain semantics
        if st is not None:
            if (
                st.phase == NegotiationPhase.INFORM.value
                and st.informed is not True
            ):
                self._run_inform(st, day, t_h, steer, proactive_out)
            return True
        item = self._find_agenda_item(item_id, day)
        if item is None:
            return False
        if (
            item.status != "planned"
            or t_h < item.start_t_h - 1e-12
            or t_h >= item.end_t_h - 1e-12
        ):
            return False  # dead / not-yet / closed window: plain semantics
        if conv.opened_t_h > item.start_t_h + 1e-12:
            # Opened after the boundary: no negotiation, the start pop-up decides.
            return False
        self._open_negotiation(item, day, t_h, steer, proactive_out)
        return True

    def _run_inform(
        self,
        st: NegotiationState,
        day: int,
        t_h: float,
        steer: Steer,
        proactive_out: list[tuple[str, str]],
    ) -> None:
        """INFORM leg: the model mentions the event naturally (the text rides out
        through ``proactive_out``); no verdict. Flips ``informed`` once; the
        deterministic decision id makes a restart replay, not re-roll."""
        assert self._decision is not None
        decision_id = f"neg-{st.item_id}-inform"
        if t_h >= st.end_t_h - 1e-12:
            # The window is over: there is nothing left to mention, and a late
            # inform answered about a missed event (live 2026-10-02 14:25).
            # Informed without words; the backstop settles the item.
            st.informed = True
            st.phase = NegotiationPhase.DECIDE.value
            self._persist_negotiation(st, t_h)
            self.store.log_event(
                int(t_h // 24.0), t_h, "negotiation_inform_skipped",
                f"item={st.item_id} window_closed",
            )
            return
        inputs = {
            "event_id": st.item_id,
            "event_label": st.activity,
            "state_label": "inform",
            "time": str(st.start_t_h),
            "phase": NegotiationPhase.INFORM.value,
            "skippable": is_skippable(st.source_type),
            "conversation_context": self._conversation_context(),
        }
        result = self._execute_decision(
            decision_id, "tool_decide_event", inputs, steer=steer,
            day=day, t_h=t_h,
        )
        if result is None:
            # Parse failure: the steer re-queues and the same decision id re-runs.
            return
        # ONLY the ``message`` key reaches the channel; the parser normalizes a
        # legacy ``{initiate, reason}`` verdict onto it.
        mention = str((result.verdict or {}).get("message") or "").strip()
        # A server-drawn verdict carries no words of hers, and an empty
        # mention is not filled in with harness text: the inform goes silent.
        if result.source == "server_draw" or not strip_face(mention).strip():
            mention = ""

        # Her row keeps her face tags; what reaches him has them cut.
        if mention and self._record_channel_text(
            mention, t_h, decision_id=decision_id,
        ):
            proactive_out.append(("event_popup", strip_face(mention)))
        # Responded-bool marker: checked as ``informed is True``, never key presence.
        st.informed = True
        st.phase = NegotiationPhase.DECIDE.value
        st.turns_to_decide = 0            # the NEXT companion turn decides
        st.afk_deadline_t_h = None
        anchor = self._afk_anchor()
        if anchor is not None:
            st.afk_deadline_t_h = anchor + SHORT_AFK_H
        st.last_decide_at_t_h = t_h       # this turn must not decide
        self._persist_negotiation(st, t_h)
        self.store.log_event(
            int(t_h // 24.0), t_h, "negotiation_inform",
            f"item={st.item_id}",
        )

    def _run_turn_decides(
        self,
        day: int,
        t_h: float,
        *,
        active_before: set[str],
        calls_before: int | None = None,
    ) -> bool:
        """Companion-turn decide trigger: run the due decide leg of the
        negotiations already in DECIDE before this turn. True when a ``go``
        resolved (the ordinary reply is suppressed — single reply-path
        invariant).

        One pop-up per turn (owner ruling 2026-10-03): once this turn has
        made a decision call (``_decision_calls`` past ``calls_before``), a
        due leg waits for the next turn. A forced resolution makes no call
        and always runs."""
        suppress = False
        for item_id in sorted(active_before):
            st = self._negotiations.get(item_id)
            if st is None or st.resolved:
                continue
            status = decide_status_at(st, now=t_h, companion_turn=True)
            if status == "forced":
                self._resolve_forced(st, t_h)
            elif status == "due" and calls_before is not None and (
                self._decision_calls > calls_before
            ):
                continue
            elif status == "due":
                self._run_decide_leg(st, day, t_h)
                if (
                    st.phase == NegotiationPhase.RESOLVED_GO.value
                    and self._active_drain is None
                ):
                    # Clock-driven path only: there is no turn to carry the
                    # departure, so the channel text is the only message.
                    suppress = True
        return suppress

    def _run_decide_leg(
        self,
        st: NegotiationState,
        day: int,
        t_h: float,
        *,
        afk_path: bool = False,
    ) -> None:
        """One DECIDE leg. The decision id is deterministic per (item, delay
        index): ``neg-<item_id>-decide-<delay_count>``, so a restart replays the
        recorded verdict. State mutation is synchronous, so a same-instant double
        fire (turn + wake) is a no-op."""
        assert self._decision is not None
        decision_id = f"neg-{st.item_id}-decide-{st.delay_count}"
        remaining = max(0.0, st.end_t_h - t_h)
        inputs = {
            "event_id": st.item_id,
            "event_label": st.activity,
            "state_label": "decide",
            "time": str(t_h),
            "phase": NegotiationPhase.DECIDE.value,
            "skippable": is_skippable(st.source_type),
            "delay_count": st.delay_count,
            "window_ending": window_ending_at(st, t_h),
            "pull": round(pull_toward_go(st), 4),
            "remaining_h": round(remaining, 4),
            "conversation_context": self._conversation_context(),
        }
        steer = Steer(
            steer_id=-1,  # synthetic: clock/turn driven, not a queued steer
            day=day,
            t_h=(
                st.afk_deadline_t_h
                if afk_path and st.afk_deadline_t_h is not None
                else t_h
            ),
            kind=KIND_EVENT_POPUP,
            payload={
                "item_id": st.item_id, "event": st.activity,
                "state": "decide", "time": t_h,
            },
            delivered_t_h=t_h,
        )
        result = self._execute_decision(
            decision_id, "tool_decide_event", inputs, steer=steer,
            day=day, t_h=t_h,
        )
        st.last_decide_at_t_h = t_h
        self._persist_negotiation(st, t_h)
        if result is None:
            # Parse failure: no queue row to requeue; the next instant retries.
            return
        verdict = result.verdict or {}
        action = verdict.get("action")
        if action not in ("follow", "abandon", "defer"):
            # Pre-A2 verdicts carry no action; fall back to ``initiate``
            # (True -> go, False -> skip).
            action = (
                "follow" if verdict.get("initiate") is True
                else "abandon" if verdict.get("initiate") is False
                else None
            )
        if action == "follow":
            self._resolve_go(st, t_h)
        elif action == "defer":
            self._resolve_delay(st, t_h, verdict)
        else:  # abandon, or (defensive) no actionable verdict: terminal skip
            self._resolve_skip(st, t_h)

    def _mark_item(self, st: NegotiationState, status: str) -> None:
        """Record the item's outcome status (a store without agenda items
        keeps none)."""
        if hasattr(self.store, "update_agenda_item_status"):
            self.store.update_agenda_item_status(st.item_id, status)

    def _finish_resolution(self, st: NegotiationState, t_h: float,
                           phase: NegotiationPhase, action: str, label: str,
                           source_session_id: str | None = None) -> None:
        """The terminal tail every resolution shares: phase, action and time
        on the state, persisted, logged, and the episode emitted (``label``
        upper-cased is the episode outcome). ``source_session_id`` defaults
        to the open conversation's memory session."""
        st.phase = phase.value
        st.resolved_action = action
        st.resolved_t_h = t_h
        self._persist_negotiation(st, t_h)
        if hasattr(self.store, "log_event"):
            self.store.log_event(
                int(t_h // 24.0), t_h, "negotiation_resolved",
                f"item={st.item_id} outcome={label} delays={st.delay_count}",
            )
        if source_session_id is None:
            conv = self._conversation
            source_session_id = (self._memory_session_id(conv.id)
                                 if conv is not None else "")
        self._emit_episode(st, label.upper(), t_h, source_session_id)

    def _resolve_go(self, st: NegotiationState, t_h: float) -> None:
        """go (follow): the turn generates her leaving, then the conversation
        closes (``followed_event``), the item completes and the episode fires.

        The departure rides as a note on the turn (``drain.decided_notes``) with
        the close deferred past the reply (``drain.close_after``); no turn in
        flight (clock-driven) -> she leaves silently: ``reason`` is the
        decision's audit field, never her words (VERBATIM "Decision reason and
        delay"; live 2026-09-30 message 238 sent the rationale to him)."""
        drain = self._active_drain
        if drain is not None:
            drain.decided_notes.append(GO_NOTE.format(activity=st.activity))
            drain.close_after = "followed_event"
        conv = self._conversation
        source_session_id = ""
        if conv is not None:
            source_session_id = self._memory_session_id(conv.id)
            if drain is None:
                self._close_conversation(conv, t_h, "followed_event")
        self._mark_item(st, "completed")
        self._finish_resolution(st, t_h, NegotiationPhase.RESOLVED_GO, "follow",
                                "go", source_session_id)

    def _resolve_skip(self, st: NegotiationState, t_h: float) -> None:
        """skip (abandon): the activity is dropped (status ``skipped``, recorded);
        the conversation continues. Terminal."""
        self._mark_item(st, "skipped")
        self._finish_resolution(st, t_h, NegotiationPhase.RESOLVED_SKIP, "abandon",
                                "skip")

    def _resolve_forced(self, st: NegotiationState, t_h: float,
                        reason: str | None = None) -> None:
        """BACKSTOP: ``now >= end_t_h`` at a decide instant (or a delay whose
        re-arm would land past the window) — forced skip ("missed it entirely"),
        no model call. Recorded as a decision row (source ``backstop``). Terminal."""
        if st.resolved:
            return
        self._mark_item(st, "skipped")
        if hasattr(self.store, "record_decision"):
            self.store.record_decision(
                int(t_h // 24.0), t_h, "tool_decide_event",
                st.item_id, st.activity, "decide", str(t_h), None, None,
                json.dumps({
                    "initiate": False,
                    "reason": reason or "missed it entirely, the window closed",
                    "action": "abandon",
                    "forced_skip": True,
                }, sort_keys=True),
                "backstop", "server_draw", t_h, 0,
                replay_id=f"neg-{st.item_id}-decide-{st.delay_count}",
            )
        self._finish_resolution(st, t_h, NegotiationPhase.RESOLVED_FORCED, "forced",
                                "forced")

    def _resolve_delay(self, st: NegotiationState, t_h: float,
                       verdict: dict) -> None:
        """delay (defer): map the reason text to N (``DEFER_N_PATTERNS``, clamped)
        and re-arm BOTH triggers (turn counter, AFK bomb). A re-arm landing
        at/after ``end_t_h`` resolves immediately as a forced skip instead."""
        # Prefer the runner's server-filled defer_turns; the model never emits N.
        n = verdict.get(DEFER_TURNS_KEY)
        if not isinstance(n, int):
            n = map_defer_n(str(verdict.get("reason") or ""))
        anchor = self._afk_anchor()
        if not rearm_after_delay(
            st, now=t_h, last_user_turn_t_h=anchor, n=n
        ):
            # AFK bomb landing at/after window close: forced skip, no re-arm.
            self._resolve_forced(st, t_h, "window closed before the next decide")
            return
        self._persist_negotiation(st, t_h)
        self.store.log_event(
            int(t_h // 24.0), t_h, "negotiation_delay",
            f"item={st.item_id} n={n} delays={st.delay_count}",
        )

    def _emit_episode(self, st: NegotiationState, outcome: str, t_h: float,
                      source_session_id: str) -> None:
        """Emit the negotiation episode (no-op on a store without episodes). The
        salience gate lives in the hook; it is replay-idempotent."""
        if not hasattr(self.store, "insert_episode"):
            return
        try:
            emit_negotiation_episode(self.store, NegotiationEpisode(
                item_id=st.item_id,
                activity=st.activity,
                outcome=outcome,
                delay_count=st.delay_count,
                salience=st.salience,
                occurred_at_t_h=t_h,
                summary="",
                source_session_id=source_session_id,
                tags=(),
            ))
        except Exception:  # pragma: no cover — the hook must never break
            # best-effort: a hook failure must never break the resolution.
            self.store.log_event(
                int(t_h // 24.0), t_h, "negotiation_episode_error",
                f"item={st.item_id} outcome={outcome}",
            )
