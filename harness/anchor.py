"""Real-time anchor — maps wall-clock epoch seconds to virtual hours (W-anchor, seam S2).

Pure module: no I/O, no store dependency, no runtime wiring (W-runtime owns
persistence and wiring later via seam S1 keys "anchor.epoch0_s" /
"anchor.t_h0" / "anchor.tz"). A RealTimeAnchor freezes one point of the
epoch -> virtual-hour line; t_h_at/epoch_of are exact inverses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True)
class RealTimeAnchor:
    """Frozen mapping between wall-clock epoch seconds and virtual hours.

    Fields:
        epoch0_s: wall-clock epoch seconds at t_h0.
        t_h0: virtual hours at epoch0_s.
        tz: IANA timezone name, e.g. "America/Mexico_City".
    """

    epoch0_s: float
    t_h0: float
    tz: str

    def t_h_at(self, epoch_s: float) -> float:
        """Virtual hours at wall-clock `epoch_s`."""
        return self.t_h0 + (epoch_s - self.epoch0_s) / SECONDS_PER_HOUR

    def epoch_of(self, t_h: float) -> float:
        """Wall-clock epoch seconds at virtual hour `t_h` (inverse of t_h_at)."""
        return self.epoch0_s + (t_h - self.t_h0) * SECONDS_PER_HOUR

    def real_at(self, t_h: float) -> datetime:
        """The aware datetime of virtual hour ``t_h`` (UTC instant + tz name).

        ``datetime.fromtimestamp(self.epoch_of(t_h), tz=ZoneInfo(self.tz))``:
        pure epoch math, so the returned instant is exact and DST-invariant;
        only its LOCAL rendering depends on the zone's offset history.
        """
        return datetime.fromtimestamp(self.epoch_of(t_h), tz=ZoneInfo(self.tz))


def anchor_for_fresh_start(now_epoch_s: float, tz: str = "America/Mexico_City") -> RealTimeAnchor:
    """Anchor a fresh start: t_h0 = hours since local midnight in `tz`.

    DST-safe: t_h0 is computed as absolute elapsed time since the local
    midnight of `now_epoch_s` (epochs of UTC instants), so a spring-forward
    day reads 2.5 at 03:30 and a fall-back day reads 2.5 at the repeated
    01:30 — never the naive wall-clock hour.
    """
    zone = ZoneInfo(tz)
    now_local = datetime.fromtimestamp(now_epoch_s, tz=zone)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Elapsed time since local midnight, computed on UTC instants (epochs):
    # subtracting aware datetimes that share one ZoneInfo object yields the
    # naive wall-clock difference (offset adjustment skipped), which is wrong
    # across DST transitions. timestamp() resolves midnight's instant via the
    # zone's utcoffset — local midnight never falls in a fold or gap.
    t_h0 = (now_epoch_s - midnight.timestamp()) / SECONDS_PER_HOUR
    return RealTimeAnchor(epoch0_s=now_epoch_s, t_h0=t_h0, tz=tz)
