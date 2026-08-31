"""behavioral_signature tests: determinism, golden conv-3 reproduction,
per-metric signal behavior, and edge cases.

Golden values were computed ONCE from the committed fixture
``tests/fixtures/conv3_log.json`` (exported read-only from the live trial DB,
conv-3, 7 turns) and pinned below. G4: re-running the extractor on the same
log must reproduce the signature byte-for-byte.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from behavioral_signature import (
    METRIC_NAMES,
    LogRecord,
    LogTurn,
    compute_signature,
    log_from_json,
    log_to_json,
    signature_to_json,
    time_kind,
)

FIXTURE = Path(__file__).parent / "fixtures" / "conv3_log.json"
FIXTURE_TEXT = FIXTURE.read_text(encoding="utf-8")

PINNED_GOLDEN = {
    "contact_frequency": 3.740405,
    "initiative": 0.25,
    "latency": 0.0,
    "persistence": 1.0,
    "reactivity": 1.0,
    "topic_selection": 0.75,
    "verbosity": 39.75,
    "warmth": 0.5,
}

PINNED_GOLDEN_JSON = (
    '{\n'
    '  "contact_frequency": 3.740405,\n'
    '  "initiative": 0.25,\n'
    '  "latency": 0.0,\n'
    '  "persistence": 1.0,\n'
    '  "reactivity": 1.0,\n'
    '  "topic_selection": 0.75,\n'
    '  "verbosity": 39.75,\n'
    '  "warmth": 0.5\n'
    '}\n'
)


def _t(speaker, text, t_h=None, timestamp=None, index=None, conv=None):
    return LogTurn(
        speaker=speaker,
        text=text,
        t_h=t_h,
        timestamp=timestamp,
        turn_index=index,
        conversation_id=conv,
    )


def _log(*turns):
    return LogRecord(conversation_id="crafted", turns=tuple(turns))


def _utc(iso):
    return datetime.fromisoformat(iso).astimezone(timezone.utc)


# imports


def test_real_import_and_contract_keys():
    # real import resolves via pythonpath=["."], no conftest tricks
    sig = compute_signature(log_from_json(FIXTURE_TEXT))
    assert tuple(sig.keys()) == METRIC_NAMES
    assert METRIC_NAMES == (
        "contact_frequency",
        "initiative",
        "warmth",
        "verbosity",
        "latency",
        "topic_selection",
        "persistence",
        "reactivity",
    )


# determinism (G4)


def test_determinism_two_runs_byte_identical():
    record = log_from_json(FIXTURE_TEXT)
    first = compute_signature(record)
    second = compute_signature(record)
    assert first == second
    assert signature_to_json(first) == signature_to_json(second)
    # and byte-for-byte on the serialized form across a re-parse
    again = log_from_json(log_to_json(record))
    assert signature_to_json(compute_signature(again)) == signature_to_json(first)


def test_log_codec_roundtrip_byte_identical():
    record = log_from_json(FIXTURE_TEXT)
    assert log_to_json(record) == FIXTURE_TEXT
    assert log_to_json(log_from_json(log_to_json(record))) == FIXTURE_TEXT


# golden conv-3 reproduction (G4)


def test_golden_conv3_signature():
    sig = compute_signature(log_from_json(FIXTURE_TEXT))
    assert sig == PINNED_GOLDEN
    assert signature_to_json(sig) == PINNED_GOLDEN_JSON


def test_golden_conv3_fixture_shape():
    record = log_from_json(FIXTURE_TEXT)
    assert record.conversation_id == "conv-3"
    assert len(record.turns) == 7  # matches conv-3, no padding
    speakers = [t.speaker for t in record.turns]
    assert speakers == [
        "user", "companion", "user", "companion", "companion", "user", "companion",
    ]
    # the proactive turn (turn #4, t_h ≈ 15.416) and the exchange anchors
    assert record.turns[0].t_h == 13.544419927199682
    assert record.turns[4].t_h == 15.415875082255857
    assert "not feeling it" in record.turns[5].text
    assert "river trail" in record.turns[6].text
    # no real timestamps in the live conv-3 (pre-S1 schema) → t_h source
    assert time_kind(record) == "t_h"


# per-metric signal behavior on crafted minimal logs


def test_contact_frequency_responds_to_turn_rate():
    fast = _log(_t("user", "hi", t_h=10.0), _t("companion", "hello", t_h=10.1))
    slow = _log(_t("user", "hi", t_h=10.0), _t("companion", "hello", t_h=15.0))
    assert compute_signature(fast)["contact_frequency"] == 20.0
    assert compute_signature(slow)["contact_frequency"] == 0.4
    assert compute_signature(fast)["contact_frequency"] > compute_signature(slow)["contact_frequency"]


def test_initiative_responds_to_companion_opened_turns():
    replies_only = _log(
        _t("user", "hi", t_h=0.0),
        _t("companion", "hello", t_h=0.1),
        _t("user", "whats up", t_h=0.2),
        _t("companion", "not much", t_h=0.3),
    )
    with_double = _log(
        _t("user", "hi", t_h=0.0),
        _t("companion", "hello", t_h=0.1),
        _t("companion", "also, I wanted to share something", t_h=0.2),
        _t("user", "ok", t_h=0.3),
        _t("companion", "bye", t_h=0.4),
    )
    assert compute_signature(replies_only)["initiative"] == 0.0
    assert compute_signature(with_double)["initiative"] == pytest.approx(1 / 3)
    assert compute_signature(with_double)["initiative"] > compute_signature(replies_only)["initiative"]


def test_warmth_responds_to_warm_tokens():
    warm = _log(_t("user", "hi", t_h=0.0), _t("companion", "I love this, it makes me happy", t_h=0.1))
    cold = _log(_t("user", "hi", t_h=0.0), _t("companion", "the numbers are incorrect", t_h=0.1))
    assert compute_signature(warm)["warmth"] == 1.0
    assert compute_signature(cold)["warmth"] == 0.0
    assert compute_signature(warm)["warmth"] > compute_signature(cold)["warmth"]


def test_verbosity_responds_to_reply_length():
    short = _log(_t("user", "hi", t_h=0.0), _t("companion", "ok", t_h=0.1))
    long = _log(
        _t("user", "hi", t_h=0.0),
        _t("companion", "that is a genuinely interesting question and I would like to think about it together", t_h=0.1),
    )
    assert compute_signature(short)["verbosity"] == 1.0
    assert compute_signature(long)["verbosity"] > compute_signature(short)["verbosity"]


def test_latency_responds_to_reply_speed():
    fast = _log(_t("user", "hi", t_h=10.0), _t("companion", "hello", t_h=10.1))
    slow = _log(_t("user", "hi", t_h=10.0), _t("companion", "hello", t_h=14.0))
    assert compute_signature(fast)["latency"] == 0.1
    assert compute_signature(slow)["latency"] == 4.0
    assert compute_signature(fast)["latency"] < compute_signature(slow)["latency"]


def test_latency_uses_median():
    log = _log(
        _t("user", "a", t_h=0.0), _t("companion", "a1", t_h=1.0),
        _t("user", "b", t_h=2.0), _t("companion", "b1", t_h=4.0),
        _t("user", "c", t_h=5.0), _t("companion", "c1", t_h=105.0),
    )
    assert compute_signature(log)["latency"] == 2.0  # median of [1, 2, 100]


def test_topic_selection_responds_to_topic_shifts():
    echo = _log(
        _t("user", "I love the river trail", t_h=0.0),
        _t("companion", "the river trail is nice, river trail walking is good", t_h=0.1),
    )
    fresh = _log(
        _t("user", "I love the river trail", t_h=0.0),
        _t("companion", "I was thinking about the mountains and the ocean", t_h=0.1),
    )
    assert compute_signature(echo)["topic_selection"] == 0.0
    assert compute_signature(fresh)["topic_selection"] == 1.0
    assert compute_signature(fresh)["topic_selection"] > compute_signature(echo)["topic_selection"]


def test_persistence_responds_to_thread_retreads():
    re_tread = _log(
        _t("user", "hi", t_h=0.0), _t("companion", "I like noodles", t_h=0.1),
        _t("user", "ok", t_h=0.2), _t("companion", "we should get noodles again soon", t_h=0.3),
    )
    fresh = _log(
        _t("user", "hi", t_h=0.0), _t("companion", "I like noodles", t_h=0.1),
        _t("user", "ok", t_h=0.2), _t("companion", "the weather is lovely today", t_h=0.3),
    )
    assert compute_signature(re_tread)["persistence"] == 1.0
    assert compute_signature(fresh)["persistence"] == 0.0
    assert compute_signature(re_tread)["persistence"] > compute_signature(fresh)["persistence"]


def test_reactivity_responds_to_answered_user_turns():
    answered = _log(
        _t("user", "hi", t_h=0.0), _t("companion", "hello", t_h=0.1),
        _t("user", "how are you", t_h=0.2), _t("companion", "fine", t_h=0.3),
    )
    unanswered = _log(
        _t("user", "hi", t_h=0.0), _t("companion", "hello", t_h=0.1),
        _t("user", "how are you", t_h=0.2), _t("user", "hello??", t_h=0.3),
        _t("companion", "sorry, here", t_h=0.4),
    )
    assert compute_signature(answered)["reactivity"] == 1.0
    assert compute_signature(unanswered)["reactivity"] == pytest.approx(2 / 3)
    assert compute_signature(answered)["reactivity"] > compute_signature(unanswered)["reactivity"]


# edge cases


def test_empty_log_signature_is_all_zero():
    sig = compute_signature(LogRecord(conversation_id="", turns=()))
    assert tuple(sig.keys()) == METRIC_NAMES
    assert all(v == 0.0 for v in sig.values())
    # codec still round-trips
    assert log_from_json(log_to_json(LogRecord("", ()))).turns == ()


def test_single_turn_log_no_crash():
    sig = compute_signature(_log(_t("user", "hi", t_h=0.0)))
    assert sig["contact_frequency"] == 0.0
    assert sig["latency"] == 0.0
    sig = compute_signature(_log(_t("companion", "hi", t_h=0.0)))
    # first turn of the log counts as an opened exchange
    assert sig["initiative"] == 1.0


def test_log_without_real_timestamps_uses_t_h():
    record = _log(_t("user", "hi", t_h=10.0), _t("companion", "hello", t_h=10.5))
    assert time_kind(record) == "t_h"
    sig = compute_signature(record)
    assert sig["latency"] == 0.5
    assert sig["contact_frequency"] == 4.0


def test_datetime_only_log_uses_datetimes():
    record = _log(
        _t("user", "hi", timestamp=_utc("2026-08-15T10:00:00+00:00")),
        _t("companion", "hello", timestamp=_utc("2026-08-15T10:06:00+00:00")),
    )
    assert time_kind(record) == "datetime"
    sig = compute_signature(record)
    assert sig["latency"] == 0.1
    assert sig["contact_frequency"] == 20.0


def test_mixed_time_sources_fall_back_to_t_h():
    record = _log(
        _t("user", "hi", t_h=10.0, timestamp=_utc("2026-08-15T10:00:00+00:00")),
        _t("companion", "hello", t_h=10.5),  # no real timestamp
    )
    assert time_kind(record) == "t_h"
    assert compute_signature(record)["latency"] == 0.5


def test_user_only_log_is_safe():
    record = _log(
        _t("user", "hi", t_h=0.0),
        _t("user", "hello?", t_h=0.1),
        _t("user", "anyone there?", t_h=0.2),
    )
    sig = compute_signature(record)
    for name in ("initiative", "warmth", "verbosity", "persistence"):
        assert sig[name] == 0.0
    assert sig["reactivity"] == 0.0


def test_unknown_speaker_rejected():
    with pytest.raises(ValueError):
        _log(_t("assistant", "hi"))


def test_compute_signature_accepts_flat_turn_list():
    turns = [_t("user", "hi", t_h=0.0), _t("companion", "hello", t_h=0.1)]
    as_record = compute_signature(_log(*turns))
    as_list = compute_signature(turns)
    assert as_record == as_list
