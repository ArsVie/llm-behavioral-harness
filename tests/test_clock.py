"""Virtual clock tests (W-E1)."""

import pytest

from harness.clock import VirtualClock


def test_initial_state():
    c = VirtualClock()
    assert c.now_h() == 0.0
    assert c.day() == 0
    assert c.local_hour() == 0.0


def test_advance_hours():
    c = VirtualClock(t_h=10.0)
    c.advance_hours(3.5)
    assert c.now_h() == 13.5
    assert c.day() == 0
    assert c.local_hour() == 13.5


def test_day_boundary():
    c = VirtualClock(t_h=23.5)
    c.advance_hours(1.0)
    assert c.day() == 1
    assert c.local_hour() == 0.5


def test_advance_to_day():
    c = VirtualClock(t_h=5.0)
    c.advance_to_day(3)
    assert c.now_h() == 72.0
    assert c.local_hour() == 0.0


def test_no_backwards_jumps():
    c = VirtualClock(t_h=100.0)
    with pytest.raises(ValueError):
        c.advance_hours(-1.0)
    with pytest.raises(ValueError):
        c.advance_to_day(2)
