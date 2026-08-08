"""Gate tests (wave 2, seam A-4) — the shared contract A3 relies on.

content_gate is pure; context_gate reads only through the injected store.
Both use the REAL defaults (VALID_REASONS / REASON_VALIDITY_H from
harness.scheduler, TimingParams(), engine.circadian.envelope).
"""

from engine.circadian import envelope
from engine.types import TimingParams
from harness.gates import GateDecision, content_gate, context_gate
from harness.scheduler import (
    REASON_CALLBACK,
    REASON_SCHEDULE,
    REASON_VALIDITY_H,
    VALID_REASONS,
)
from harness.store import SQLiteStore

TIMING = TimingParams()


def _store(tmp_path):
    return SQLiteStore(tmp_path / "s.db")


def _seed_proactives(store, day: int, n: int) -> None:
    for i in range(n):
        store.add_message("assistant", f"p{i}", t_h=float(i), day=day, proactive=True)


# --------------------------------------------------------------------------- #
# content_gate
# --------------------------------------------------------------------------- #


def test_content_gate_ok_within_window():
    decision = content_gate(REASON_SCHEDULE, planned_t_h=10.0, now_h=10.0)
    assert decision.allowed is True
    assert decision.code == "ok"


def test_content_gate_ok_at_validity_boundary():
    # PASS iff now_h <= planned + validity: exactly at the boundary is ok.
    decision = content_gate(REASON_SCHEDULE, planned_t_h=10.0, now_h=13.0)
    assert decision.allowed is True
    assert decision.code == "ok"


def test_content_gate_expired_past_window():
    decision = content_gate(REASON_SCHEDULE, planned_t_h=10.0, now_h=13.01)
    assert decision.allowed is False
    assert decision.code == "expired"


def test_content_gate_invalid_reason():
    decision = content_gate("nagging", planned_t_h=10.0, now_h=10.0)
    assert decision.allowed is False
    assert decision.code == "no_valid_reason"


def test_content_gate_none_reason():
    decision = content_gate(None, planned_t_h=10.0, now_h=10.0)
    assert decision.allowed is False
    assert decision.code == "no_valid_reason"


def test_content_gate_invalid_reason_wins_over_expiry():
    # Invalid taxonomy wins even if the window would also be past.
    decision = content_gate("nagging", planned_t_h=10.0, now_h=100.0)
    assert decision.code == "no_valid_reason"


def test_content_gate_per_reason_validity():
    # Each reason uses its own window from REASON_VALIDITY_H.
    for reason in VALID_REASONS:
        validity = REASON_VALIDITY_H[reason]
        ok = content_gate(reason, planned_t_h=10.0, now_h=10.0 + validity)
        assert ok.allowed and ok.code == "ok", reason
        expired = content_gate(reason, planned_t_h=10.0, now_h=10.0 + validity + 0.01)
        assert not expired.allowed and expired.code == "expired", reason


def test_content_gate_custom_taxonomy_kwargs():
    # The *valid_reasons / *validity_h kwargs are part of the seam.
    ok = content_gate(
        REASON_SCHEDULE, 10.0, 10.0, valid_reasons=(REASON_SCHEDULE,)
    )
    assert ok.code == "ok"
    rejected = content_gate(
        REASON_CALLBACK, 10.0, 10.0, valid_reasons=(REASON_SCHEDULE,)
    )
    assert rejected.code == "no_valid_reason"


# --------------------------------------------------------------------------- #
# context_gate
# --------------------------------------------------------------------------- #


def test_context_gate_ok_when_all_hold(tmp_path):
    store = _store(tmp_path)
    decision = context_gate(
        14.0, day=0, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.allowed is True
    assert decision.code == "ok"
    store.close()


def test_envelope_sanity_default_timing():
    # Quiet hours (23.0, 8.0) cross midnight: 2.0 is deep quiet, 14.0 is awake.
    assert envelope(2.0, TIMING) == 0.0
    assert envelope(14.0, TIMING) > 1e-9


def test_context_gate_quiet_hours(tmp_path):
    store = _store(tmp_path)
    decision = context_gate(
        2.0, day=0, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.allowed is False
    assert decision.code == "quiet_hours"
    store.close()


def test_context_gate_quiet_hours_after_midnight_rollover(tmp_path):
    # now_h=26.0 is day 1 at 02:00 local — still quiet.
    store = _store(tmp_path)
    decision = context_gate(
        26.0, day=1, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.code == "quiet_hours"
    store.close()


def test_context_gate_cooldown_via_last_fired(tmp_path):
    store = _store(tmp_path)
    min_gap_h = TIMING.min_gap_min / 60.0
    # Just under the gap -> cooldown.
    blocked = context_gate(
        14.0, day=0, store=store, timing=TIMING,
        last_fired_t_h=14.0 - min_gap_h + 0.01,
    )
    assert blocked.allowed is False
    assert blocked.code == "cooldown"
    # Exactly at the gap -> allowed.
    ok = context_gate(
        14.0, day=0, store=store, timing=TIMING,
        last_fired_t_h=14.0 - min_gap_h,
    )
    assert ok.allowed and ok.code == "ok"
    store.close()


def test_context_gate_no_last_fired_passes_cooldown(tmp_path):
    store = _store(tmp_path)
    decision = context_gate(
        14.0, day=0, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.allowed is True
    store.close()


def test_context_gate_daily_cap(tmp_path):
    store = _store(tmp_path)
    # Exactly at the cap -> blocked; one under -> allowed.
    _seed_proactives(store, day=0, n=TIMING.daily_cap)
    blocked = context_gate(
        14.0, day=0, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert blocked.allowed is False
    assert blocked.code == "daily_cap"
    store.close()


def test_context_gate_daily_cap_under_limit(tmp_path):
    store = _store(tmp_path)
    _seed_proactives(store, day=0, n=TIMING.daily_cap - 1)
    decision = context_gate(
        14.0, day=0, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.allowed is True
    assert decision.code == "ok"
    store.close()


def test_context_gate_daily_cap_is_per_day(tmp_path):
    store = _store(tmp_path)
    _seed_proactives(store, day=0, n=TIMING.daily_cap)
    # Day 1 has no proactive messages yet -> cap does not block.
    decision = context_gate(
        14.0 + 24.0, day=1, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.allowed is True
    store.close()


def test_context_gate_reactive_messages_do_not_count(tmp_path):
    store = _store(tmp_path)
    store.add_message("user", "hi", t_h=1.0, day=0, proactive=False)
    decision = context_gate(
        14.0, day=0, store=store, timing=TIMING, last_fired_t_h=None
    )
    assert decision.allowed is True
    store.close()


def test_context_gate_first_failing_code_wins(tmp_path):
    store = _store(tmp_path)
    _seed_proactives(store, day=0, n=TIMING.daily_cap)
    min_gap_h = TIMING.min_gap_min / 60.0
    # Quiet hours + cooldown + cap all failing -> quiet_hours reported first.
    quiet = context_gate(
        2.0, day=0, store=store, timing=TIMING,
        last_fired_t_h=2.0 - min_gap_h + 0.01,
    )
    assert quiet.code == "quiet_hours"
    # Awake + cooldown + cap failing -> cooldown reported before daily_cap.
    cooldown = context_gate(
        14.0, day=0, store=store, timing=TIMING,
        last_fired_t_h=14.0 - min_gap_h + 0.01,
    )
    assert cooldown.code == "cooldown"
    store.close()


def test_gate_decision_is_frozen_dataclass():
    decision = GateDecision(allowed=True, code="ok")
    assert decision.allowed and decision.code == "ok"
    try:
        decision.allowed = False
    except Exception as exc:  # noqa: BLE001 - frozen dataclass raises
        assert isinstance(exc, (AttributeError, TypeError))
    else:
        raise AssertionError("GateDecision must be immutable")
