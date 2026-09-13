"""Environment parsing shared by every flag-gated feature.

A leaf module: it imports nothing from ``harness``, so the runtime, the
channels and the tool layer can all use it without an import cycle.
"""

from __future__ import annotations

import os

#: Values that read as True. Everything else — including a typo — is False,
#: so a mistyped flag leaves the feature OFF.
_TRUTHY = ("1", "true", "yes", "on")


def env_bool(name: str, default: bool = False) -> bool:
    """Harness env-bool convention: unset or blank takes ``default``."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY
