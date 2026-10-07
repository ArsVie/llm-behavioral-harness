"""On-disk cache for the cold-start setup proposals.

The interest-graph extension and the routine catalog are each asked once, at
onboarding, and are slow, so an accepted answer is cached under a digest of
the exact inputs. The key is the QUESTION, not the run: collections are
order-normalized, and a per-namespace ``schema`` string invalidates entries
answered under older instructions.

Failure is always a miss, never an error: a corrupt or half-written entry
costs a model call, not an onboarding. Writes go through a temp file and are
renamed into place.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

#: Root of the on-disk cache; each namespace gets a subdirectory.
#: Overridable with ``HARNESS_PROPOSAL_CACHE`` (empty string disables it).
_DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "harness" / "setup-proposals"

#: Environment variable that overrides (or disables) the cache root.
CACHE_ENV_VAR = "HARNESS_PROPOSAL_CACHE"


def cache_root() -> Path | None:
    """The cache root, or None when caching is disabled."""
    override = os.environ.get(CACHE_ENV_VAR)
    if override is None:
        return _DEFAULT_CACHE_ROOT
    override = override.strip()
    return Path(override) if override else None


def _normalize(value: Any) -> Any:
    """Order-normalize a key payload so the same SET is the same key."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(_normalize(v) for v in value)
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items())}
    return value


def cache_key(namespace: str, schema: str, payload: dict) -> str:
    """Stable digest of one setup question.

    ``schema`` is the caller's version marker: bump it whenever the prompt or
    the accepted response shape changes; old entries stop being reused.
    """
    blob = json.dumps(
        {"ns": namespace, "schema": schema, "payload": _normalize(payload)},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _entry(namespace: str, schema: str, payload: dict) -> Path | None:
    """Where this question's answer is cached, or None when caching is off."""
    root = cache_root()
    if root is None:
        return None
    return root / namespace / f"{cache_key(namespace, schema, payload)}.json"


def load(namespace: str, schema: str, payload: dict) -> Any | None:
    """A previously cached answer to this exact question, or None."""
    path = _entry(namespace, schema, payload)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def store(namespace: str, schema: str, payload: dict, answer: Any) -> None:
    """Cache an accepted answer. Best-effort; failures are swallowed."""
    path = _entry(namespace, schema, payload)
    if path is None or answer is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(answer, fh, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError:
        return
