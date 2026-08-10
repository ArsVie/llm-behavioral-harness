"""Matriz confirmatoria de la iteración 3 (Gate G5) — 7 condiciones × 5
semillas × 30 días con el cliente REAL (deepseek-v4-flash vía opencode-go).

Diseño (disciplina de compuerta, heredada de G2):
- Cada célula corre por el camino integrado (run_cell) con checkpoints de
  reinicio (7/14/21/26/29) y perturbación habilitada — M7 real, no vacuo.
- Resiliencia a ventanas de proveedor: una célula que muere (p. ej.
  ``RuntimeError`` por contenido vacío — el cliente endurecido de it2) se
  reintenta con backoff creciente (2/4/8 min). El cliente ya reintenta
  internamente 4× con backoff corto; esta capa es la de nivel-célula.
- Honestidad: una célula que agota sus reintentos se registra como FAILED
  y la matriz CONTINÚA (no se mata la tanda por una ventana del proveedor);
  el reporte de cierre lista las fallas explícitamente. Nunca se escribe
  una célula vacía ni se fabrica un resultado.
- Checkpoint de progreso: status.json se actualiza tras cada célula
  (done/failed/retries/timestamps) — el orquestador puede commitear
  progreso y reanudar la tanda si el proceso muere.
- Exit: 0 solo si TODAS las células son válidas; si no, 1 con la lista.

Uso:
    python -m experiments.cvs_matrix [--conditions FULL,NO_LIFE,...]
                                     [--seeds 5001,...] [--days 30]
                                     [--out results/it3-g5-matrix]
                                     [--max-retries 3] [--retry-base-s 120]

Requiere OPENCODE_GO_API_KEY en el entorno (ver companion_vertical_slice).

Convención del repo: docstrings en español, identificadores en inglés.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from experiments.cvs_common import DEFAULT_CHECKPOINT_DAYS, run_cell
from experiments.cvs_manifest import MATRIX_CONDITIONS, SEEDS

STATUS_FILE = "status.json"


def _load_env() -> None:
    """Carga ~/.hermes/.env + mapeo OPENCODE_GO_* -> LLM_* (mismo patrón
    que companion_vertical_slice: client.py lee LLM_API_KEY/LLM_BASE_URL,
    el archivo de Hermes guarda OPENCODE_GO_*. Nunca pisa valores ya
    presentes; nunca imprime secretos)."""
    env_file = Path.home() / ".hermes/.env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    if "LLM_API_KEY" not in os.environ and os.environ.get("OPENCODE_GO_API_KEY"):
        os.environ["LLM_API_KEY"] = os.environ["OPENCODE_GO_API_KEY"]
    if "LLM_BASE_URL" not in os.environ and os.environ.get("OPENCODE_GO_BASE_URL"):
        os.environ["LLM_BASE_URL"] = os.environ["OPENCODE_GO_BASE_URL"]


def _require_key() -> None:
    _load_env()
    if not (os.environ.get("OPENCODE_GO_API_KEY") or os.environ.get("LLM_API_KEY")):
        raise SystemExit(
            "OPENCODE_GO_API_KEY is not set — the harness never stores "
            "credentials. Export it before running the matrix."
        )


def _cell_out(root: Path, condition: str, seed: int) -> Path:
    return root / condition.lower() / f"seed{seed}"


def _write_transcript(cell_out_dir: Path, record: dict, root: Path,
                      condition: str, seed: int) -> None:
    """Rinde el transcript de la célula para el juez (G6 lee
    root/transcripts/<COND>_seed<seed>.txt — contrato de cvs_judge)."""
    from experiments.companion_vertical_slice import render_transcript
    from harness.store import SQLiteStore

    db = record.get("db") or (cell_out_dir / f"cell_{condition.lower()}_seed{seed}.db")
    store = SQLiteStore(db)
    try:
        txt = render_transcript(store)
    finally:
        store.close()
    tdir = root / "transcripts"
    tdir.mkdir(exist_ok=True)
    (tdir / f"{condition}_seed{seed}.txt").write_text(txt, encoding="utf-8")


def _load_status(root: Path) -> dict:
    p = root / STATUS_FILE
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"cells": {}, "started_at": None, "finished_at": None}


def _save_status(root: Path, status: dict) -> None:
    (root / STATUS_FILE).write_text(
        json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def run_matrix(
    *,
    conditions: tuple[str, ...] = MATRIX_CONDITIONS,
    seeds: tuple[int, ...] = SEEDS,
    days: int = 30,
    out_root: str = "results/it3-g5-matrix",
    max_retries: int = 3,
    retry_base_s: float = 120.0,
    fake: bool = False,
) -> dict:
    """Corre la matriz completa; devuelve el resumen (y lo escribe a JSON)."""
    root = Path(out_root)
    root.mkdir(parents=True, exist_ok=True)
    status = _load_status(root)
    if status["started_at"] is None:
        status["started_at"] = time.time()

    cells = [(c, s) for c in conditions for s in seeds]
    results: list[dict] = []
    failed: list[tuple[str, int, str]] = []

    for condition, seed in cells:
        key = f"{condition}/seed{seed}"
        if status["cells"].get(key, {}).get("state") == "ok":
            results.append(status["cells"][key])
            continue
        out_dir = _cell_out(root, condition, seed)
        cell_state: dict = {"state": "running", "retries": 0, "started_at": time.time()}
        status["cells"][key] = cell_state
        _save_status(root, status)

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                record = run_cell(
                    condition, seed, out_dir,
                    days=days, checkpoints=DEFAULT_CHECKPOINT_DAYS,
                    fake=fake, perturb=True,
                )
                summary = record.get("summary") or record
                cell_state.update(
                    {"state": "ok", "finished_at": time.time(), "attempts": attempt + 1}
                )
                status["cells"][key] = cell_state
                _save_status(root, status)
                results.append({"condition": condition, "seed": seed, **summary})
                _write_transcript(out_dir, record, root, condition, seed)
                break
            except Exception as exc:  # noqa: BLE001 — retry loop por diseño
                last_error = exc
                cell_state["retries"] = attempt + 1
                cell_state["last_error"] = f"{type(exc).__name__}: {exc}"
                status["cells"][key] = cell_state
                _save_status(root, status)
                if attempt < max_retries:
                    wait = retry_base_s * (2 ** attempt)
                    print(
                        f"[matrix] {key} attempt {attempt + 1} failed "
                        f"({type(exc).__name__}: {exc}) — retry in {wait:.0f}s",
                        flush=True,
                    )
                    time.sleep(wait)
        else:
            failed.append((condition, seed, f"{type(last_error).__name__}: {last_error}"))
            cell_state["state"] = "failed"
            status["cells"][key] = cell_state
            _save_status(root, status)
            print(f"[matrix] {key} FAILED after {max_retries + 1} attempts", flush=True)

    status["finished_at"] = time.time()
    report = {
        "matrix": {
            "conditions": list(conditions),
            "seeds": list(seeds),
            "days": days,
            "n_cells": len(cells),
            "n_ok": len(results),
            "n_failed": len(failed),
            "checkpoints": list(DEFAULT_CHECKPOINT_DAYS),
            "perturbation": True,
        },
        "failed_cells": [{"condition": c, "seed": s, "error": e} for c, s, e in failed],
        "per_cell": {f"{c}/seed{s}": r for r in results for c, s in [(r["condition"], r["seed"])]},
    }
    _save_status(root, status)
    (root / "matrix_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="G5 confirmatory matrix — real LLM, checkpoints + perturbation."
    )
    parser.add_argument("--conditions", type=str, default=",".join(MATRIX_CONDITIONS))
    parser.add_argument("--seeds", type=str, default=",".join(map(str, SEEDS)))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--fake", action="store_true",
                        help="run_cell fake=True (CI/hook tests — no API)")
    parser.add_argument("--out", type=str, default="results/it3-g5-matrix")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-base-s", type=float, default=120.0)
    args = parser.parse_args(argv)

    if not args.fake:
        _require_key()
    conditions = tuple(c.strip() for c in args.conditions.split(",") if c.strip())
    seeds = tuple(int(s) for s in args.seeds.split(",") if s.strip())
    print(f"[matrix] {len(conditions)} conditions x {len(seeds)} seeds x {args.days} days "
          f"= {len(conditions) * len(seeds)} cells", flush=True)
    report = run_matrix(
        conditions=conditions, seeds=seeds, days=args.days,
        out_root=args.out, max_retries=args.max_retries,
        retry_base_s=args.retry_base_s, fake=args.fake,
    )
    print(f"[matrix] done: {report['matrix']['n_ok']}/{report['matrix']['n_cells']} "
          f"cells ok, {report['matrix']['n_failed']} failed", flush=True)
    if report["failed_cells"]:
        for f in report["failed_cells"]:
            print(f"  FAILED {f['condition']}/seed{f['seed']}: {f['error']}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
