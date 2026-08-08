"""Scripted synthetic user for accelerated runs (W-E1).

Per research/06 §8: accelerated days need someone to talk to. The slice uses
a SCRIPTED user (not an LLM-as-user) — deterministic day→message scripts so
ablation cells share the exact same user behavior. Good-month and bad-month
scripts differ in warmth/engagement; a flat script is neutral.

Each script maps day index → user message (one message per day, sent at the
script's configured hour). `None` means the user stays silent that day.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SyntheticUser:
    """Deterministic per-day script; `messages[day]` or None for silence."""

    messages: dict[int, str] = field(default_factory=dict)
    hour: float = 19.0

    def message_for(self, day: int) -> str | None:
        return self.messages.get(day)

    @classmethod
    def good_month(cls, days: int = 30, hour: float = 19.0) -> "SyntheticUser":
        """Warm, engaged, appreciative — a month that should feel good."""
        scripts = {
            d: "Hey! How was your day? I was thinking about you earlier."
            for d in range(days)
        }
        return cls(scripts, hour)

    @classmethod
    def bad_month(cls, days: int = 30, hour: float = 19.0) -> "SyntheticUser":
        """Distant, critical, dismissive — a month that should feel awful."""
        scripts = {
            d: "Whatever. Just leaving this here. Not in the mood to talk."
            for d in range(days)
        }
        return cls(scripts, hour)

    @classmethod
    def flat(cls, days: int = 30, hour: float = 19.0) -> "SyntheticUser":
        """Neutral, functional exchanges."""
        scripts = {d: "Hi. Anything new?" for d in range(days)}
        return cls(scripts, hour)
