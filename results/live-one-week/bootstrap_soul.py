"""Bootstrap for the controlled week-1 run: fresh DB → clean-start chain → Lily-soul-RP core.

Step 1 (idempotent): ``ensure_companion_initialized`` on a new DB with the
same Gate-2 profile as the seed-6001 run (week-1 comparability).
Step 2: replaces the persisted persona core with the text from
``--soul-file`` (name Lily). The subsequent run reuses the existing row
(the chain is idempotent) and the assembler reads it via ``snapshot.persona.core``.

Usage:
    .venv/bin/python results/live-one-week/bootstrap_soul.py \
        --db results/live-one-week/companion.db --seed 7001 \
        --soul-file results/live-one-week/soul-lily-rp.md
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap week 1 + RP soul.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--soul-file", type=Path, required=True)
    parser.add_argument("--owner-name", default="User")
    parser.add_argument("--owner-interests", default="",
                        help="comma-separated; empty = Gate-2 fixture")
    parser.add_argument("--companion-name", default="Lily")
    args = parser.parse_args(argv)

    from experiments.cvs_common import GATE2_USER_INTERESTS
    from harness.bootstrap import ensure_companion_initialized
    from harness.credentials import load_env_file
    from harness.domain import UserProfile
    from harness.store import SQLiteStore

    load_env_file(REPO_ROOT / ".env")

    if args.db.exists():
        print(f"[bootstrap] DB already exists — refusing to regenerate: {args.db}")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(args.db, audit_mode=True)

    interests = tuple(
        x.strip() for x in args.owner_interests.split(",") if x.strip()
    )
    result = ensure_companion_initialized(
        store,
        seed=args.seed,
        user=UserProfile(name=args.owner_name,
                         interests=interests or GATE2_USER_INTERESTS),
        day=0,
    )
    print(f"[bootstrap] persona={result.persona.name} "
          f"interests={len(result.persona.interests)} "
          f"arcs={len(result.life_arcs)}")

    soul = args.soul_file.read_text(encoding="utf-8").strip()
    persona = store.load_persona()
    assert persona is not None, "bootstrap left no persona row"
    store.save_persona(dataclasses.replace(
        persona, name=args.companion_name, core=soul))
    check = store.load_persona()
    assert (check is not None and check.name == args.companion_name
            and check.core == soul)
    print(f"[bootstrap] core Lily-soul-RP persisted ({len(soul)} chars, "
          f"{len(check.interests)} interests, {len(check.routines)} routines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
