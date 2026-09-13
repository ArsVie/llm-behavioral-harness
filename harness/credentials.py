"""Two-lane credential resolution.

Lane env contract:

    product  — live companion actor: LILY_TOKEN, LILY_BASE_URL (optional)
    research — judges + experiment replies: JUDGE_GENERATOR_TOKEN,
               JUDGE_GENERATOR_BASE_URL (optional)

Rules:

- The lane token is REQUIRED for live calls: :func:`resolve_credentials`
  raises RuntimeError naming the lane and the env var — never the value.
- Base URL precedence: lane-specific var -> ``LLM_BASE_URL`` -> ``None``
  (caller applies the client's DEFAULT_BASE_URL).
- Values are never logged or printed: only the lane + the env var NAME.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Mapping

LANES = ("product", "research")

#: lane -> (token env var, base-url env var)
_LANE_ENV: dict[str, tuple[str, str | None]] = {
    "product": ("LILY_TOKEN", "LILY_BASE_URL"),
    "research": ("JUDGE_GENERATOR_TOKEN", "JUDGE_GENERATOR_BASE_URL"),
}

_logger = logging.getLogger(__name__)


def load_env_file(path: str | Path, env: dict[str, str] | None = None) -> None:
    """Load a dotenv-style file into the environment (values never printed).

    Missing files are a no-op; keys already present are never overridden.
    Shell equivalent: ``set -a; . "$REPO/.env"; set +a``.
    """
    target = env if env is not None else os.environ
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in target:
            target[key] = value


def resolve_credentials(
    lane: str, env: Mapping[str, str] | None = None
) -> tuple[str, str | None]:
    """Resolve (api_key, base_url) for the lane.

    Raises RuntimeError naming the lane and the required env var when the
    lane token is missing — never the value, and with no fallback to
    ``LLM_API_KEY`` / ``OPENCODE_GO_API_KEY``. Logged labels carry the env
    var NAME only.
    """
    if lane not in _LANE_ENV:
        raise ValueError(f"unknown lane {lane!r} — expected one of {LANES}")
    token_var, base_url_var = _LANE_ENV[lane]
    src = env if env is not None else os.environ
    token = src.get(token_var, "")
    if not token:
        raise RuntimeError(
            f"{lane} lane credential missing: env var {token_var} is not set — "
            "the harness never stores credentials. Source the repo-root .env "
            f"(set -a; . <repo>/.env; set +a) or export {token_var} before "
            "running live."
        )
    _logger.info("credentials: %s lane — token present (%s)", lane, token_var)
    base_url: str | None = None
    if base_url_var and src.get(base_url_var):
        base_url = src[base_url_var]
    elif src.get("LLM_BASE_URL"):
        # Generic fallback for un-laned call sites.
        base_url = src["LLM_BASE_URL"]
    return token, base_url


def probe_lane(lane: str, *, timeout_s: float = 15.0, env: Mapping[str, str] | None = None) -> str:
    """Content-free auth probe for a lane: GET ``{base}/models``.

    Sends no content; confirms auth by a 200 on the models endpoint. Returns
    the base URL that authenticated; raises RuntimeError otherwise.
    """
    import httpx

    from harness.client import DEFAULT_BASE_URL  # lazy: avoid import cycle

    token, base_url = resolve_credentials(lane, env=env)
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    try:
        resp = httpx.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"{lane} lane auth probe failed (transport): {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"{lane} lane auth probe failed: GET {base}/models -> HTTP "
            f"{resp.status_code}"
        )
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lane credential presence + auth probe (values never printed)."
    )
    parser.add_argument("lane", choices=LANES, help="product | research")
    parser.add_argument(
        "--check",
        action="store_true",
        help="auth probe via GET {base}/models — no content sent",
    )
    args = parser.parse_args(argv)
    try:
        resolve_credentials(args.lane)
        token_var = _LANE_ENV[args.lane][0]
        print(f"{args.lane} lane: token present ({token_var})")
        if args.check:
            base = probe_lane(args.lane)
            print(f"{args.lane} lane: auth OK via GET {base}/models -> 200")
    except (RuntimeError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
