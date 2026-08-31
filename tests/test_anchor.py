"""Real-time anchor tests (W-anchor, seam S2)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from harness.anchor import RealTimeAnchor, anchor_for_fresh_start

SECONDS_PER_HOUR = 3600.0


def _epoch(y, mo, d, h=0, mi=0, s=0, tz="America/New_York", fold=0):
    """Epoch seconds of a local wall-clock time (fold selects the DST occurrence)."""
    dt = datetime(y, mo, d, h, mi, s, tzinfo=ZoneInfo(tz), fold=fold)
    return dt.timestamp()


def test_t_h_at_maps_epoch_to_virtual_hours():
    a = RealTimeAnchor(epoch0_s=1_000.0, t_h0=42.0, tz="America/Mexico_City")
    assert a.t_h_at(1_000.0) == pytest.approx(42.0)
    assert a.t_h_at(1_000.0 + SECONDS_PER_HOUR) == pytest.approx(43.0)
    assert a.t_h_at(1_000.0 - SECONDS_PER_HOUR) == pytest.approx(41.0)
    assert a.t_h_at(1_000.0 + 30 * 60) == pytest.approx(42.5)


def test_epoch_of_is_inverse_of_t_h_at():
    a = RealTimeAnchor(epoch0_s=123_456.789, t_h0=7.25, tz="America/Mexico_City")
    for epoch_s in (-1e6, 0.0, 123_456.789, 1e9, 1.7e9):
        assert a.epoch_of(a.t_h_at(epoch_s)) == pytest.approx(epoch_s, abs=1e-6)


def test_t_h_at_is_inverse_of_epoch_of():
    a = RealTimeAnchor(epoch0_s=123_456.789, t_h0=7.25, tz="America/Mexico_City")
    for t_h in (-5.0, 0.0, 7.25, 18.5, 1_000.0):
        assert a.t_h_at(a.epoch_of(t_h)) == pytest.approx(t_h, abs=1e-9)


def test_fresh_start_sanity_1830_local():
    now = _epoch(2026, 8, 15, 18, 30, tz="America/Mexico_City")
    a = anchor_for_fresh_start(now, "America/Mexico_City")
    assert a.epoch0_s == now
    assert a.tz == "America/Mexico_City"
    assert a.t_h0 == pytest.approx(18.5)
    assert a.t_h_at(now) == pytest.approx(18.5)
    assert a.epoch_of(a.t_h0) == pytest.approx(now)


def test_fresh_start_default_tz():
    now = _epoch(2026, 8, 15, 9, 0, tz="America/Mexico_City")
    a = anchor_for_fresh_start(now)
    assert a.tz == "America/Mexico_City"
    assert a.t_h0 == pytest.approx(9.0)


def test_fresh_start_various_times_of_day():
    for h, mi, expected in ((0, 0, 0.0), (0, 30, 0.5), (12, 0, 12.0), (23, 45, 23.75)):
        now = _epoch(2026, 8, 15, h, mi, tz="America/New_York")
        a = anchor_for_fresh_start(now, "America/New_York")
        assert a.t_h0 == pytest.approx(expected)


def test_fresh_start_dst_spring_forward():
    # America/New_York 2026-03-08: 02:00 -> 03:00 (23-hour day).
    # At 03:30 EDT only 2.5 h have elapsed since midnight.
    now = _epoch(2026, 3, 8, 3, 30, tz="America/New_York")
    a = anchor_for_fresh_start(now, "America/New_York")
    assert a.t_h0 == pytest.approx(2.5)


def test_fresh_start_dst_fall_back():
    # America/New_York 2026-11-01: 02:00 -> 01:00 (25-hour day).
    # At the repeated 01:30 EST (fold=1) 2.5 h have elapsed since midnight.
    now = _epoch(2026, 11, 1, 1, 30, tz="America/New_York", fold=1)
    a = anchor_for_fresh_start(now, "America/New_York")
    assert a.t_h0 == pytest.approx(2.5)


def test_fresh_start_dst_fall_back_first_occurrence():
    # Same day, the first 01:30 (EDT, fold=0) — only 1.5 h have elapsed.
    now = _epoch(2026, 11, 1, 1, 30, tz="America/New_York", fold=0)
    a = anchor_for_fresh_start(now, "America/New_York")
    assert a.t_h0 == pytest.approx(1.5)


def test_fresh_start_dst_historical_mexico_city():
    # tzdata keeps Mexico's pre-2023 DST history: 2022-04-03 02:00 -> 03:00
    # in America/Mexico_City; at 03:30 CDT only 2.5 h have elapsed.
    now = _epoch(2022, 4, 3, 3, 30, tz="America/Mexico_City")
    a = anchor_for_fresh_start(now, "America/Mexico_City")
    assert a.t_h0 == pytest.approx(2.5)


# -- real_at (S1): aware datetime of a virtual hour ---------------------------


def test_real_at_round_trip():
    a = RealTimeAnchor(epoch0_s=1_750_000_000.0, t_h0=7.25,
                       tz="America/New_York")
    for t_h in (0.0, 7.25, 15.4, 23.75, 100.0):
        dt = a.real_at(t_h)
        assert dt.tzinfo is not None, "real_at must return an AWARE datetime"
        assert dt.timestamp() == pytest.approx(a.epoch_of(t_h), abs=1e-6)


def test_real_at_timezone_correctness():
    # Anchors built at local midnight (t_h0=0) of 2026-08-15 in Chihuahua (UTC-6)
    # and New York (EDT, UTC-4); virtual hour 15.4 renders as 15:24 in both.
    a_chih = RealTimeAnchor(epoch0_s=_epoch(2026, 8, 15, 0, 0, tz="America/Chihuahua"),
                            t_h0=0.0, tz="America/Chihuahua")
    a_nyc = RealTimeAnchor(epoch0_s=_epoch(2026, 8, 15, 0, 0, tz="America/New_York"),
                           t_h0=0.0, tz="America/New_York")

    dt_chih = a_chih.real_at(15.4)
    dt_nyc = a_nyc.real_at(15.4)
    # correct local wall clock in both zones: 15.4 h -> 15:24
    assert (dt_chih.hour, dt_chih.minute) == (15, 24)
    assert (dt_nyc.hour, dt_nyc.minute) == (15, 24)
    # correct offsets applied (Chihuahua UTC-6, New York EDT UTC-4)
    assert dt_chih.utcoffset().total_seconds() == -6 * SECONDS_PER_HOUR
    assert dt_nyc.utcoffset().total_seconds() == -4 * SECONDS_PER_HOUR
    # same t_h -> instants differ by exactly the offset difference
    assert (dt_chih.timestamp() - dt_nyc.timestamp()) == pytest.approx(
        2 * SECONDS_PER_HOUR, abs=1e-6)


def test_real_at_dst_spring_forward_instant_arithmetic():
    # America/New_York 2026-03-08 DST gap: real_at is pure epoch math, so the
    # round-trip holds; 3.5 h after midnight (EST) renders 04:30 EDT.
    a = RealTimeAnchor(epoch0_s=_epoch(2026, 3, 8, 0, 0, tz="America/New_York"),
                       t_h0=0.0, tz="America/New_York")
    for t_h in (1.5, 2.0, 2.5, 3.0, 3.5, 15.4):
        assert a.real_at(t_h).timestamp() == pytest.approx(
            a.epoch_of(t_h), abs=1e-6)
    dt = a.real_at(3.5)
    assert (dt.hour, dt.minute) == (4, 30)
    assert dt.utcoffset().total_seconds() == -4 * SECONDS_PER_HOUR
