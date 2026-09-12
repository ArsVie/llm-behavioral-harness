"""Virtual clock — the only time source for the harness (W-E1).

The engine is pure (no real-time reads); every driver, scheduler and
persistence layer reads time from a Clock so accelerated days are possible.
Time convention (frozen in engine/types.py): absolute hours since simulation
start, t_h = 0.0 is day 0 at 00:00; local hour = t_h % 24; day = int(t_h // 24).
"""

from __future__ import annotations

from dataclasses import dataclass


def hhmm(t_h: float) -> str:
    """Local wall-clock ``HH:MM`` (24h) of an absolute ``t_h``.

    The ONLY model-visible rendering of harness time. Absolute virtual hours
    (``80.98``) are an engine coordinate and must never reach the prompt --
    every surface the model reads (state card, agenda windows, pop-up
    inputs, steer blocks, proactive hooks) goes through this function.
    Minutes round to the nearest minute and wrap at the hour.
    """
    local = t_h % 24.0
    hh = int(local)
    mm = int(round((local - hh) * 60.0))
    if mm == 60:  # rounding pushed past the hour
        hh, mm = (hh + 1) % 24, 0
    return f"{hh:02d}:{mm:02d}"


def duration(hours: float) -> str:
    """Plain-language elapsed time: ``2h 05m`` / ``20m`` / ``just now``.

    Used where a DURATION rather than a clock time reaches the model (user
    silence, remaining window). Never a decimal hour count.
    """
    minutes = int(round(max(0.0, hours) * 60.0))
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


@dataclass
class VirtualClock:
    """Mutable absolute-time source. `t_h` in hours since simulation start."""

    t_h: float = 0.0

    def now_h(self) -> float:
        """Absolute hours since simulation start."""
        return self.t_h

    def day(self) -> int:
        """Day index of the current time (int(t_h // 24))."""
        return int(self.t_h // 24.0)

    def local_hour(self) -> float:
        """Local hour of day in [0, 24)."""
        return self.t_h % 24.0

    def advance_hours(self, hours: float) -> None:
        """Advance the clock; raises on negative jumps (no time travel)."""
        if hours < 0:
            raise ValueError(f"cannot advance backwards: {hours}h")
        self.t_h += hours

    def advance_to_day(self, day: int) -> None:
        """Jump to the start (00:00) of `day`. Raises if that is in the past."""
        target = day * 24.0
        if target < self.t_h:
            raise ValueError(f"cannot rewind to day {day} (current t_h={self.t_h})")
        self.t_h = target
