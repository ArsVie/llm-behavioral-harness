"""Bootstrap del run controlado semana 1: DB fresca → cadena clean-start → core Lily-soul-RP.

Paso 1 (idempotente): ``ensure_companion_initialized`` sobre una DB nueva con
el mismo perfil Gate-2 del run seed-6001 (comparabilidad semana 1).
Paso 2: reemplaza el core de la persona persistida por el texto de
``--soul-file`` (nombre Lily). El run posterior reutiliza la fila existente
(la cadena es idempotente) y el assembler la lee vía ``snapshot.persona.core``.

Uso:
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
    parser = argparse.ArgumentParser(description="Bootstrap semana 1 + soul RP.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--soul-file", type=Path, required=True)
    parser.add_argument("--owner-name", default="User")
    parser.add_argument("--owner-interests", default="",
                        help="coma-separados; vacío = fixture Gate-2")
    parser.add_argument("--companion-name", default="Lily")
    args = parser.parse_args(argv)

    from experiments.cvs_common import GATE2_USER_INTERESTS
    from harness.bootstrap import ensure_companion_initialized
    from harness.credentials import load_env_file
    from harness.domain import UserProfile
    from harness.store import SQLiteStore

    load_env_file(REPO_ROOT / ".env")

    if args.db.exists():
        print(f"[bootstrap] DB ya existe — rehúso sin regenerar: {args.db}")
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
          f"intereses={len(result.persona.interests)} "
          f"arcos={len(result.life_arcs)}")

    soul = args.soul_file.read_text(encoding="utf-8").strip()
    persona = store.load_persona()
    assert persona is not None, "bootstrap no dejó fila persona"
    store.save_persona(dataclasses.replace(
        persona, name=args.companion_name, core=soul))
    check = store.load_persona()
    assert (check is not None and check.name == args.companion_name
            and check.core == soul)
    print(f"[bootstrap] core Lily-soul-RP persistido ({len(soul)} chars, "
          f"{len(check.interests)} intereses, {len(check.routines)} rutinas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
