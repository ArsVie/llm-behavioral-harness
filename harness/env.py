"""Environment parsing shared by every flag-gated feature.

A leaf module on purpose: it imports nothing from ``harness``, so the
runtime, the channels and the tool layer can all use it without creating a
cycle (``runtime.py`` in particular must not import ``harness.commands`` at
module scope — pinned by test_runtime_anchor).

``env_bool`` was copy-pasted into five modules with byte-identical bodies
before this existed.
"""

from __future__ import annotations

import os

#: Values that read as True. Everything else — including a typo — is False,
#: so a mistyped flag leaves the feature OFF rather than half-enabled.
_TRUTHY = ("1", "true", "yes", "on")


def env_bool(name: str, default: bool = False) -> bool:
    """Harness env-bool convention: unset or blank takes ``default``."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY
