"""Content + context gates for proactive contact (wave 2, seam A-4).

Pure functions: no I/O except reads through the injected store. The content
gate checks that the planned reason is still in the taxonomy and its validity
window has not elapsed; the context gate re-checks at FIRE time that the
moment is still good (quiet hours, cooldown, daily cap), because user
activity, restarts, and clock pacing can change state since planning.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.circadian import envelope
from engine.types import TimingParams
from harness.scheduler import REASON_VALIDITY_H, VALID_REASONS


@dataclass(frozen=True)
class GateDecision:
    """One gate's verdict: pass/fail plus the failing code (or 'ok')."""

    allowed: bool
    code: str  # 'ok'|'no_valid_reason'|'expired'|'cooldown'|'quiet_hours'|'daily_cap'


def content_gate(
    reason: str | None,
    planned_t_h: float,
    now_h: float,
    *,
    valid_reasons=VALID_REASONS,
    validity_h=REASON_VALIDITY_H,
) -> GateDecision:
    """PASS iff reason in valid_reasons AND now_h <= planned_t_h +
    validity_h[reason]. code='no_valid_reason' if reason invalid/None;
    'expired' if past the window."""
    if reason not in valid_reasons:
        return GateDecision(allowed=False, code="no_valid_reason")
    if now_h > planned_t_h + validity_h[reason]:
        return GateDecision(allowed=False, code="expired")
    return GateDecision(allowed=True, code="ok")


def context_gate(
    now_h: float,
    day: int,
    *,
    store,
    timing: TimingParams,
    last_fired_t_h: float | None,
) -> GateDecision:
    """PASS iff ALL hold, else the first failing code:
      quiet_hours : engine.circadian.envelope(now_h % 24, timing) >= 1e-9
      cooldown    : last_fired_t_h is None OR
                    (now_h - last_fired_t_h) >= timing.min_gap_min/60
      daily_cap   : store.proactive_count(day) < timing.daily_cap
    (envelope==0 already encodes 'active window'/quiet hours by construction,
    matching run_events guards; the gate re-checks at FIRE time because
    user activity, restarts, and clock pacing can change state since planning.)
    """
    if envelope(now_h % 24.0, timing) < 1e-9:
        return GateDecision(allowed=False, code="quiet_hours")
    if last_fired_t_h is not None and (
        now_h - last_fired_t_h
    ) < timing.min_gap_min / 60.0:
        return GateDecision(allowed=False, code="cooldown")
    if store.proactive_count(day) >= timing.daily_cap:
        return GateDecision(allowed=False, code="daily_cap")
    return GateDecision(allowed=True, code="ok")
