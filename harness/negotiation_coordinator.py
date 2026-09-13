"""The availability-negotiation state machine, lifted out of ``Session``.

WHAT THIS IS: the 16 negotiation methods, moved VERBATIM out of
``harness.session`` and re-attached to it as a mixin. Session inherits from
``NegotiationMixin``, so every ``self.*`` reference resolves exactly as it
did before and the replay contract is untouched:

* decision ids stay byte-identical — ``neg-<item_id>-inform`` and
  ``neg-<item_id>-decide-<n>``;
* the ``negotiation_state`` JSON snapshot is persisted at the same instants
  by the same code (``state_to_dict`` / ``state_from_dict``).

WHAT THIS IS NOT: a decoupling. A mixin reaches into Session's attributes
(``_negotiations``, ``_conversation``, ``_steering``, ``_decision``,
``store``, ``client``, the clock helpers) as freely as the methods did when
they were inline — it moves the code, it does not untangle it. That was the
deliberate trade: the entanglement identified in the 2026-08-28 code-quality review is real,
and plumbing it through a constructor is a separate, riskier change that wants
its own pass.
What this buys is a 2523-line module becoming two readable ones, with the
parity tests proving nothing moved but the text.

Gate for any change here: test_negotiation_state.py,
test_availability_negotiation.py, test_session_close_parity.py,
test_adversarial_restart.py, then the full suite and `-m slow`.
"""

from __future__ import annotations

import json

from harness.negotiation_contract import (
    DEFER_TURNS_KEY,
    SHORT_AFK_H,
    NegotiationEpisode,
    NegotiationPhase,
    is_skippable,
)
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

try:  # A3's episode hook; checkouts without it emit no episodes.
    from harness.negotiation_episodes import emit_negotiation_episode
except ImportError:  # pragma: no cover — A3 not merged in this checkout
    emit_negotiation_episode = None


#: The departure note appended to a turn when a go verdict resolves.
#:
#: Says WHAT she decided and leaves the wording to her. The alternative --
#: pasting the verdict's ``reason`` -- put machine rationale in the channel.
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
    #: The drain of the turn currently being generated, when a turn is in
    #: flight. A verdict that resolves during a turn writes its note here so
    #: the turn speaks for it; None means a clock-driven path with no turn to
    #: carry the words, and the channel fallback applies.
    _active_drain = None

    """Session's availability-negotiation half. Never instantiated alone."""

    def _restore_negotiations(self) -> dict[str, NegotiationState]:
        """Rebuild active negotiations from persisted ``negotiation_state``
        state-event snapshots (latest per item wins). Runs once at session
        init, so a restart resumes the loop without re-Informing."""
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
        """Persist one negotiation as a full JSON snapshot (state event).
        Every mutation writes a snapshot, so restart recovery is exact."""
        if not hasattr(self.store, "log_event"):
            return
        self.store.log_event(
            int(t_h // 24.0), t_h, "negotiation_state",
            json.dumps(state_to_dict(st), sort_keys=True),
        )

    def _find_agenda_item(self, item_id: str, day: int):
        """Today's AgendaItem by id (the popup payload carries the id; the
        item's source_type/salience/end_t_h drive the negotiation)."""
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
        """The AFK bomb's anchor: the conversation's last USER turn (or its
        opening when the companion opened and the user never replied —
        same fallback as the ``user_left`` close). ``None`` when no
        conversation is open (a negotiation only exists while one is)."""
        conv = self._conversation
        if conv is None:
            return None
        anchor = self._last_user_turn_t_h(conv)
        if anchor is None:
            anchor = conv.opened_t_h
        return anchor

    def next_negotiation_trigger_t_h(self, now: float) -> float | None:
        """Next strictly-future negotiation wake instant for the runtime's
        rollover park: the earlier of the AFK-bomb decide instant and the
        window-close backstop instant of the earliest active negotiation.
        None when no negotiation is pending. Mirrors
        :meth:`next_conversation_close_t_h` exactly (future instants only;
        a past deadline fires at the next wake of any kind)."""
        candidates: list[float] = []
        for st in self._negotiations.values():
            nxt = next_trigger_t_h(st, now)
            if nxt is not None:
                candidates.append(nxt)
        return min(candidates) if candidates else None

    def check_negotiation(self, now: float) -> tuple[tuple[str, str], ...]:
        """Runtime wake hook (the parallel of ``check_conversation_lifecycle``).

        Runs lazy event-boundary detection (so start/end pop-ups enqueue
        even between turns) and then every due decide leg of the active
        negotiations: the AFK bomb fired (silence > SHORT_AFK) or the
        window closed (backstop — forced skip, no model call). Returns the
        proactive ``(reason, text)`` messages the decide legs produced
        (her natural close on ``go``) for the runtime to send through the
        channel. Idempotent per virtual instant: a decide leg executes at
        most once per instant per item, so repeated wakes can never
        double-fire or hot-loop the model."""
        outs: list[tuple[str, str]] = []
        if self._decision is None or self._steering is None:
            return ()
        day = int(now // 24.0)
        if self._decision_enabled:
            self._enqueue_event_popups(day, now)
        for item_id in list(self._negotiations):
            st = self._negotiations[item_id]
            status = decide_status_at(st, now=now, companion_turn=False)
            if status == "forced":
                self._resolve_forced(st, now)
            elif status == "due":
                self._run_decide_leg(st, day, now, outs, afk_path=True)
        return tuple(outs)

    def _maybe_heads_up(
        self,
        item_id: str,
        day: int,
        t_h: float,
        steer: Steer,
        proactive_out: list[tuple[str, str]],
    ) -> None:
        """The "incoming event" steer: fire INFORM ``HEADS_UP_LEAD_H`` ahead.

        She learns something is about to start so she can get ready for it,
        and mention it if he is there. NO verdict — the one decision waits
        for the window to actually open (``decide_status_at`` returns
        ``"waiting"`` until then, without spending companion turns).

        Requires an OPEN conversation: a heads-up nobody hears is a model
        call for nothing, so an unobserved stretch of her day skips it and
        the window's own boundary carries the decision instead. Idempotent
        through the same responded-bool marker Inform always used, so a
        re-delivered steer never announces twice (G0 no-nag floor).
        """
        if self._decision is None or self._steering is None:
            return
        conv = self._conversation
        if conv is None:
            return
        st = self._negotiations.get(item_id)
        if st is not None:
            # A re-delivered heads-up (interrupted-turn requeue, or the
            # deferred pop-up's re-apply once the turn's own generation
            # answered it) re-runs Inform only while the responded-bool
            # marker is not True; a negotiation that already informed just
            # consumes the steer. Mirrors _maybe_start_negotiation's
            # re-delivery branch: without this the deferred Inform never
            # completes on its own turn -- the state exists but informed is
            # not True -- and the next START pop-up re-fires it late, eating
            # the first decide's script slot.
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
        """Route a START event pop-up into the availability negotiation.

        Returns True when the pop-up belongs to the negotiation machine
        (consumed without the plain start-popup semantics); False lets the
        caller keep the EXACT existing tool_decide_event semantics.

        The negotiation activates only when a conversation was OPEN at the
        item's start boundary (``conv.opened_t_h <= start_t_h``): an item
        whose boundary landed with NO open conversation skips Inform and
        keeps the plain start popup as its Decide (G0 floor) — the pop-up
        is then delivered at the first later turn unchanged. A negotiation
        that already exists owns the pop-up: a re-delivered start pop-up
        (interrupted-turn requeue) re-runs Inform only while the responded-
        bool marker ``informed`` is not True; a resolved negotiation just
        consumes it.
        """
        if self._decision is None or self._steering is None:
            return False
        conv = self._conversation
        if conv is None:
            return False  # no open conversation: plain semantics
        st = self._negotiations.get(item_id)
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
            # The conversation opened after the boundary landed, so no
            # negotiation; the start pop-up is the Decide.
            return False
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
        return True

    def _run_inform(
        self,
        st: NegotiationState,
        day: int,
        t_h: float,
        steer: Steer,
        proactive_out: list[tuple[str, str]],
    ) -> None:
        """INFORM leg: the model mentions the event naturally (the reason
        text rides out through ``proactive_out`` as a channel message). NO
        verdict is executed — she does not leave, nothing is resolved. The
        responded-bool marker ``informed`` flips to True exactly once; the
        decision id is deterministic (``neg-<item_id>-inform``), so
        DecisionRunner's replay-by-decision_id makes a restart replay the
        recorded mention instead of re-rolling.
        """
        assert self._decision is not None
        decision_id = f"neg-{st.item_id}-inform"
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
            # Parse failure: the steer re-queues; the state stays
            # INFORM and the pop-up re-runs the same decision id.
            return
        # ONLY the ``message`` key reaches the channel. The parser already
        # normalizes a legacy ``{initiate, reason}`` inform verdict onto
        # ``message`` (tools._normalize_verdict, phase="inform"), so reading
        # ``reason`` here added nothing except the one case that must never
        # happen: a genuine audit reason — third-person machine rationale —
        # sent to the user as her own words.
        mention = str((result.verdict or {}).get("message") or "").strip()
        if not mention:
            mention = f"I've got {st.activity} coming up soon."

        proactive_out.append(("event_popup", mention))
        # Responded-bool idempotency marker: checked as VALUE True
        # (``informed is True``), never key presence.
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
        proactive_out: list[tuple[str, str]],
        *,
        active_before: set[str],
    ) -> bool:
        """Companion-turn decide trigger: run the due decide leg of every
        negotiation that was ALREADY in DECIDE before this turn (the
        Inform turn itself never decides — the loop fires from the NEXT
        companion turn on). Returns True when a ``go`` resolved this turn:
        the ordinary reply is suppressed (her natural close is the only
        message — single reply-path invariant)."""
        suppress = False
        for item_id in active_before:
            st = self._negotiations.get(item_id)
            if st is None or st.resolved:
                continue
            status = decide_status_at(st, now=t_h, companion_turn=True)
            if status == "forced":
                self._resolve_forced(st, t_h)
            elif status == "due":
                self._run_decide_leg(st, day, t_h, proactive_out)
                if (
                    st.phase == NegotiationPhase.RESOLVED_GO.value
                    and self._active_drain is None
                ):
                    # Only the clock-driven path still suppresses: there is
                    # no turn to carry the departure, so the channel text is
                    # the only message. On a real turn the reply IS her
                    # leaving, so suppressing it produced silence.
                    suppress = True
        return suppress

    def _run_decide_leg(
        self,
        st: NegotiationState,
        day: int,
        t_h: float,
        proactive_out: list[tuple[str, str]],
        *,
        afk_path: bool = False,
    ) -> None:
        """One DECIDE leg. The decision id is deterministic per
        (item, delay index): ``neg-<item_id>-decide-<delay_count>``, so a
        restart replays the recorded verdict instead of re-rolling. The
        request carries the A2 schema keys (phase/skippable/delay_count/
        window_ending) plus the converging-pull context (delay_count,
        pull, remaining window) the MODEL sees — the server never overrides
        the verdict. State mutation is synchronous, so a same-instant
        double fire (turn + runtime wake) is a no-op."""
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
            # Parse failure: the synthetic steer has no queue row to
            # requeue; the next decide instant retries the same id.
            return
        verdict = result.verdict or {}
        reason = str(verdict.get("reason") or "")
        action = verdict.get("action")
        if action not in ("follow", "abandon", "defer"):
            # Pre-A2 verdicts carry no action; initiate is the
            # fallback (True -> go, False -> skip).
            action = (
                "follow" if verdict.get("initiate") is True
                else "abandon" if verdict.get("initiate") is False
                else None
            )
        if action == "follow":
            self._resolve_go(st, t_h, reason, proactive_out)
        elif action == "abandon":
            self._resolve_skip(st, t_h, reason)
        elif action == "defer":
            self._resolve_delay(st, t_h, verdict)
        else:  # pragma: no cover — defensive: terminal and bounded
            self._resolve_skip(st, t_h, reason or "no actionable verdict")

    def _resolve_go(
        self,
        st: NegotiationState,
        t_h: float,
        reason: str,
        proactive_out: list[tuple[str, str]],
    ) -> None:
        """go (follow): the TURN generates her leaving, then the conversation
        closes gracefully (close_reason ``followed_event``), the agenda item
        completes, and the episode hook fires (A3's module; no emission when
        it has not landed).

        Changed 2026-09-08. This used to push the verdict's ``reason`` into
        ``proactive_out`` as her message and close the conversation
        immediately. Two things were wrong with that. The reason is
        machine-facing rationale, so what reached the channel was
        "Evening cooking is still in progress and he's heading off — I'll
        send a warm in-character send-off and keep cooking": third person,
        describing an intention instead of acting on it. And closing before
        generation meant the ordinary reply was suppressed, so a user
        goodbye got silence.

        Now the departure is a NOTE on the turn (``drain.decided_notes``) and
        the close is deferred to after the reply is persisted
        (``drain.close_after``). The reason keeps being recorded in
        ``decision_records`` — the engine study reads it there.
        """
        drain = self._active_drain
        if drain is not None:
            drain.decided_notes.append(GO_NOTE.format(activity=st.activity))
            drain.close_after = "followed_event"
        else:
            # No turn to carry it (clock-driven AFK path): fall back to the
            # channel so the departure is not silently dropped.
            text = (reason or "").strip() or f"Time to go to {st.activity}."
            proactive_out.append(("event_popup", text))
        conv = self._conversation
        source_session_id = ""
        if conv is not None:
            source_session_id = self._memory_session_id(conv.id)
            if drain is None:
                self._close_conversation(conv, t_h, "followed_event")
        if hasattr(self.store, "update_agenda_item_status"):
            self.store.update_agenda_item_status(st.item_id, "completed")
        st.phase = NegotiationPhase.RESOLVED_GO.value
        st.resolved_action = "follow"
        st.resolved_t_h = t_h
        self._persist_negotiation(st, t_h)
        self._log_resolution(st, t_h, "go")
        self._emit_episode(st, "GO", t_h, source_session_id)

    def _resolve_skip(self, st: NegotiationState, t_h: float, reason: str) -> None:
        """skip (abandon): the activity is dropped (status ``skipped``,
        recorded), the conversation continues. Terminal."""
        if hasattr(self.store, "update_agenda_item_status"):
            self.store.update_agenda_item_status(st.item_id, "skipped")
        st.phase = NegotiationPhase.RESOLVED_SKIP.value
        st.resolved_action = "abandon"
        st.resolved_t_h = t_h
        self._persist_negotiation(st, t_h)
        self._log_resolution(st, t_h, "skip")
        self._emit_episode(st, "SKIP", t_h, self._memory_session_id(
            self._conversation.id
        ) if self._conversation is not None else "")

    def _resolve_forced(self, st: NegotiationState, t_h: float,
                        reason: str | None = None) -> None:
        """BACKSTOP: ``now >= end_t_h`` at a decide instant (or a delay
        whose re-arm would land at/after the window close) — forced skip
        ("missed it entirely"), NO model call. Recorded as a decision row
        (source ``backstop``) so the trace is complete and a restart
        replays the forced outcome instead of re-asking. Terminal."""
        if st.resolved:
            return
        if hasattr(self.store, "update_agenda_item_status"):
            self.store.update_agenda_item_status(st.item_id, "skipped")
        if hasattr(self.store, "record_decision"):
            self.store.record_decision(
                int(t_h // 24.0), t_h, "tool_decide_event",
                st.item_id, st.activity, "decide", str(t_h), None, None,
                json.dumps({
                    "initiate": False,
                    "reason": reason or "missed it entirely — window closed",
                    "action": "abandon",
                    "forced_skip": True,
                }, sort_keys=True),
                "backstop", "server_draw", t_h, 0,
                replay_id=f"neg-{st.item_id}-decide-{st.delay_count}",
            )
        st.phase = NegotiationPhase.RESOLVED_FORCED.value
        st.resolved_action = "forced"
        st.resolved_t_h = t_h
        self._persist_negotiation(st, t_h)
        self._log_resolution(st, t_h, "forced")
        self._emit_episode(st, "FORCED", t_h, self._memory_session_id(
            self._conversation.id
        ) if self._conversation is not None else "")

    def _resolve_delay(self, st: NegotiationState, t_h: float,
                       verdict: dict) -> None:
        """delay (defer): the server maps the reason text to N
        (``DEFER_N_PATTERNS``, clamped) and re-arms BOTH triggers — the
        companion-turn counter (N turns) and the AFK bomb (last user turn
        + SHORT_AFK). A re-arm that would land at/after ``end_t_h``
        resolves immediately as a forced skip instead (the backstop clamp:
        defer never re-arms past the window)."""
        # Prefer the runner's server-filled defer_turns; fall back to
        # the same mapping for older runners. The model never emits N.
        n = verdict.get(DEFER_TURNS_KEY)
        if not isinstance(n, int):
            n = map_defer_n(str(verdict.get("reason") or ""))
        anchor = self._afk_anchor()
        if not rearm_after_delay(
            st, now=t_h, last_user_turn_t_h=anchor, n=n
        ):
            # The AFK bomb landing at/after window close resolves
            # immediately (forced skip), never re-arming past end_t_h.
            self._resolve_forced(st, t_h, "window closed before the next decide")
            return
        self._persist_negotiation(st, t_h)
        self.store.log_event(
            int(t_h // 24.0), t_h, "negotiation_delay",
            f"item={st.item_id} n={n} delays={st.delay_count}",
        )

    def _log_resolution(self, st: NegotiationState, t_h: float,
                        outcome: str) -> None:
        if hasattr(self.store, "log_event"):
            self.store.log_event(
                int(t_h // 24.0), t_h, "negotiation_resolved",
                f"item={st.item_id} outcome={outcome} "
                f"delays={st.delay_count}",
            )

    def _emit_episode(self, st: NegotiationState, outcome: str, t_h: float,
                      source_session_id: str) -> None:
        """Call the A3 episode hook (imported defensively — checkouts
        without ``harness/negotiation_episodes.py`` emit nothing). The
        salience gate (a plain zero-delay go does not emit) lives in A3's
        module; the hook is replay-idempotent (deterministic id upsert)."""
        if emit_negotiation_episode is None or not hasattr(
            self.store, "insert_episode"
        ):
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
            # the negotiation's own resolution (A3 seam, best-effort).
            self.store.log_event(
                int(t_h // 24.0), t_h, "negotiation_episode_error",
                f"item={st.item_id} outcome={outcome}",
            )
