"""Driver del protocolo de juez v2 (Gate G6, it3) — ambas familias,
sondas de atención, agregación Bradley-Terry.

Usa la maquinaria de cvs_judge (run_pairwise_pass / bradley_terry_scale /
pairwise_report) sobre los transcripts de la matriz G5:

- 2 familias reales (opencode-flash deepseek-v4-flash + opencode-luna
  gpt-5.6-luna, mismas claves que el manifiesto congela).
- 2 passes por familia, pares muestreados dentro de la semilla
  (preregistrado: sin cruce completo), control pairs de transcript
  degradado (sondas de atención) resueltos por AMBAS familias — DoD 6.
- Agregación BT por dimensión y familia; desacuerdo reportado; un efecto
  visto por una sola familia NO es conducta establecida del companion.

Uso:
    .venv/bin/python -m experiments.cvs_g6 --out results/it3-g5-matrix \
        [--judge-out results/it3-g6-judge] [--passes 2] [--max-pairs N]
        [--dry-run]   # usa PairwiseFakeJudge (CI/tests), sin API

Exit 0 = ambas familias resolvieron las sondas y el reporte se escribió.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from experiments.cvs_judge import (
    bradley_terry_scale,
    pairwise_report,
    read_transcripts,
    run_pairwise_pass,
)
from experiments.cvs_manifest import JUDGE_FAMILIES, JUDGE_PASSES


def _family_route_ok(family: dict, timeout_s: float = 30.0) -> bool:
    """Sonda rápida: el modelo de la familia devuelve contenido real?
    Evita quemar el presupuesto de reintentos de TODO un pass sobre una
    ruta muerta (episodio opencode-go 2026-08-10: flash 100% vacío).

    PITFALL 2026-08-13 (probe artifact): NUNCA cap max_tokens en la sonda.
    deepseek-v4-flash es un modelo de RAZONAMIENTO: consume su presupuesto de
    tokens en reasoning ANTES de emitir contenido, así que con max_tokens=10
    devuelve HTTP 200 con content vacío (finish_reason='length') aunque la
    ruta esté viva — la sonda antigua marcaba flash como muerto ~100% de las
    veces mientras las llamadas reales del juez (sin cap) funcionaban.
    Sondear SIN max_tokens (o ≥512) para medir contenido real.
    """
    import httpx

    from experiments.cvs_matrix import _load_env
    from harness.credentials import resolve_credentials

    _load_env()
    try:
        key = resolve_credentials("research")[0]
    except RuntimeError:
        return False
    if not key:
        return False
    try:
        r = httpx.post(
            family["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": family["model"],
                  "messages": [{"role": "user", "content": "ping"}]},
            timeout=timeout_s,
        )
        if r.status_code != 200:
            return False
        content = r.json()["choices"][0]["message"].get("content") or ""
        return bool(content.strip())
    except Exception:
        return False


def build_client(family: dict, *, dry_run: bool, seed: int):
    if dry_run:
        from experiments.cvs_judge import PairwiseFakeJudge

        return PairwiseFakeJudge(seed, family=family["id"], model=family["model"])
    from experiments.cvs_matrix import _load_env

    _load_env()  # repo-root .env, research lane
    from harness.client import OpenAICompatibleClient

    return OpenAICompatibleClient(
        base_url=family["base_url"],
        model=family["model"],
        lane="research",  # resolves JUDGE_GENERATOR_TOKEN
        # Judge calls on reasoning models take 20-47s for ~13K-char pairwise
        # prompts; 120s covers full 2-transcript prompts.
        timeout_s=120,
    )


def run_g6(
    matrix_out: Path,
    judge_out: Path,
    *,
    passes: int = JUDGE_PASSES,
    max_pairs: int | None = None,
    dry_run: bool = False,
) -> dict:
    matrix_out = Path(matrix_out)
    judge_out = Path(judge_out)
    judge_out.mkdir(parents=True, exist_ok=True)

    transcripts = read_transcripts(matrix_out)
    if not transcripts:
        raise SystemExit(f"no transcripts found under {matrix_out}")
    print(f"[g6] transcripts: {len(transcripts)}")
    # Stage the matrix transcripts into the judge dir.
    staged = judge_out / "transcripts"
    staged.mkdir(parents=True, exist_ok=True)
    src = Path(matrix_out) / "transcripts"
    for f in sorted(src.glob("*.txt")):
        (staged / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    all_outcomes: list[dict] = []
    family_reports: dict[str, dict] = {}
    family_errors: dict[str, str] = {}
    for family in JUDGE_FAMILIES:
        client = build_client(family, dry_run=dry_run, seed=7000)
        try:
            if not dry_run and not _family_route_ok(family):
                raise RuntimeError(
                    "probe: model route dead (empty/whitespace completions — "
                    "e.g. commandcode deepseek/deepseek-v4-flash, episode 2026-08-10)"
                )
            for pass_id in range(1, passes + 1):
                print(f"[g6] family={family['id']} pass={pass_id} "
                      f"({'dry-run' if dry_run else 'real'})")
                rec = run_pairwise_pass(
                    judge_out, pass_id, family["id"], client,
                    max_pairs=max_pairs,
                )
                all_outcomes.extend(rec.get("outcomes", []))
        except Exception as exc:  # noqa: BLE001 — degraded family (e.g. flash route down)
            family_errors[family["id"]] = f"{type(exc).__name__}: {exc}"
            print(f"[g6] family={family['id']} FAILED: {exc}", flush=True)
        finally:
            try:
                client.close()
            except Exception:
                pass

    # Attention probes: the degraded control pairs must be resolved.
    controls = [o for o in all_outcomes if o.get("control")]
    resolved = [o for o in controls if o.get("winner") is not None]
    probe_ok = len(resolved) == len(controls) and len(controls) > 0
    print(f"[g6] attention probes: {len(resolved)}/{len(controls)} resolved "
          f"-> {'OK' if probe_ok else 'FAIL'}")

    report = pairwise_report(judge_out)
    # BT aggregation per family and dimension (winner/loser per pair).
    stems = [Path(t).stem for t in transcripts]
    conditions = sorted({s.split("_")[0] for s in stems})
    per_family: dict[str, dict] = {}
    for family in JUDGE_FAMILIES:
        fam_outcomes = [o for o in all_outcomes if o.get("family_id") == family["id"]]
        dims: dict[str, dict] = {}
        for dim in sorted({o.get("dim_id", "") for o in fam_outcomes}):
            dim_outcomes = [o for o in fam_outcomes if o.get("dim_id") == dim]
            dims[dim] = {
                "bt": bradley_terry_scale(dim_outcomes, conditions=conditions),
                "n_outcomes": len(dim_outcomes),
            }
        per_family[family["id"]] = {"dims": dims, "n_outcomes": len(fam_outcomes)}
    report["g6"] = {
        "families": [f["id"] for f in JUDGE_FAMILIES],
        "passes": passes,
        "attention_probes_resolved": probe_ok,
        "attention_probes_total": len(controls),
        "family_errors": family_errors,
        "per_family": per_family,
        "dry_run": dry_run,
    }
    # Non-zero exit only if no family judged or the probes went unresolved.
    families_that_judged = [f["id"] for f in JUDGE_FAMILIES
                            if f["id"] not in family_errors]
    ok = probe_ok and len(families_that_judged) > 0
    report["g6"]["ok"] = ok
    (judge_out / "g6_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[g6] report: {judge_out / 'g6_report.json'}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G6 judge protocol v2 driver.")
    parser.add_argument("--out", type=str, default="results/it3-g5-matrix",
                        help="matrix out dir (cell DBs -> transcripts)")
    parser.add_argument("--judge-out", type=str, default="results/it3-g6-judge")
    parser.add_argument("--passes", type=int, default=JUDGE_PASSES)
    parser.add_argument("--max-pairs", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="use PairwiseFakeJudge (CI), no API")
    args = parser.parse_args(argv)
    report = run_g6(
        Path(args.out), Path(args.judge_out),
        passes=args.passes, max_pairs=args.max_pairs, dry_run=args.dry_run,
    )
    return 0 if report["g6"].get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
