"""On-disk cache for the cold-start setup proposals.

Two setup calls build the companion's world from the user's interests: the
interest-graph extension (:mod:`harness.interest_extension`) and the routine
catalog (:mod:`harness.routine_setup`). Both are asked once, at onboarding,
and both are slow — 20-90s on the live gateway, with a tail that overruns the
budget and drops to a heuristic.

Why this lives on DISK and not in the store
-------------------------------------------
A setup call happens exactly when the store does not exist yet, so a store
cache could never be warm on the run that needs it. The case that matters is
a DB reset with an UNCHANGED interest set: the previous run already paid for
a good answer, and re-rolling the dice is how two consecutive resets end up
incomparable. That happened on 2026-09-08 — same four interests, the
extension timed out at 90s, and the fresh graph had 31 relations where the
run before it had 45. An experiment cannot be reset cleanly if resetting
degrades it.

The key is the QUESTION, not the run
------------------------------------
Every key is a digest of the exact inputs the model is shown, with each
collection order-normalized: the same SET of interests is the same question
however the caller happened to order it. A ``schema`` string is folded in per
namespace, so changing a prompt or the validation shape invalidates the
entries answered under the old instructions rather than silently reusing them.

Failure is always a miss, never an error: a corrupt, unreadable or
half-written entry costs a model call, not an onboarding. Writes go through
a temp file in the same directory and are renamed into place, so a crash
mid-write cannot leave a half-parsed entry behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

#: Root of the on-disk cache. Each namespace gets a subdirectory.
#:
#: Overridable with ``HARNESS_PROPOSAL_CACHE``; set it to an empty string to
#: disable caching entirely (what ``tests/conftest.py`` does suite-wide, so
#: no test can write to a developer's home directory or make its neighbours
#: order-dependent by caching a stub's answer).
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

    ``schema`` is the caller's own version marker: bump it whenever the
    prompt or the accepted response shape changes, and every entry answered
    under the old wording stops being reused.
    """
    blob = json.dumps(
        {"ns": namespace, "schema": schema, "payload": _normalize(payload)},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def load(namespace: str, schema: str, payload: dict) -> Any | None:
    """A previously cached answer to this exact question, or None."""
    root = cache_root()
    if root is None:
        return None
    path = root / namespace / f"{cache_key(namespace, schema, payload)}.json"
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def store(namespace: str, schema: str, payload: dict, answer: Any) -> None:
    """Cache an accepted answer. Best-effort; failures are swallowed."""
    root = cache_root()
    if root is None or answer is None:
        return
    directory = root / namespace
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(answer, fh, sort_keys=True)
            os.replace(
                tmp, directory / f"{cache_key(namespace, schema, payload)}.json"
            )
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError:
        return
