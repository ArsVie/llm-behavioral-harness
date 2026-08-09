"""Validador de artefactos OKF del harness (Iteración 2, A8).

Valida un directorio de run del Track vertical:
  * manifest.json     — preregistro presente con las claves del esquema
  * report.md         — frontmatter OKF (type: experiment-report) + secciones
  * trace.json        — lista de cadenas de proveniencia con el esquema mínimo
  * metrics_*.json    — métricas estructurales con umbrales duros
  * vertical_summary.json — invariantes mecánicas (todas a cero)

Uso:
    python experiments/validation/validate_okf.py <run_dir>

Exit 0 = OK; exit 1 = cualquier fallo (imprime cada violación).
Convención del repo: docstrings en español, identificadores en inglés.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REQUIRED_MANIFEST_KEYS = (
    "schema_version", "experiment", "commit", "dirty", "questions",
    "hypotheses", "conditions", "seeds", "judge", "metrics", "thresholds",
    "context_budget", "embedding_backend", "summarizer_backend", "config_hash",
    "protocol",
)

REQUIRED_REPORT_SECTIONS = (
    "Run summary",
    "Mechanical audit",
    "Metrics vs frozen thresholds",
    "Event-chain (§17.2)",
    "Perturbation + recovery (§17.3)",
    "Judge protocol (§17.1/§17.4)",
    "Replay / reproducibility",
)

HARD_ZERO_KEYS = (
    "ungrounded_proactive",
    "wrong_intent",
    "restart_state_loss",
    "stranded_opportunities",
    "cycle_state_leakage",
    "memory_provenance_failures",
    "duplicate_turns",
    "life_dead_duration",
)


def _parse_frontmatter(text: str) -> dict | None:
    """Parsea el frontmatter YAML mínimo (type/title/description/tags)."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    block = text[3:end]
    meta: dict = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in ("type", "title", "description", "timestamp"):
            meta[key] = value
    return meta or None


def check_run_dir(run_dir: Path) -> list[str]:
    """Valida un directorio de run; devuelve la lista de violaciones."""
    run_dir = Path(run_dir)
    violations: list[str] = []

    # --- manifest.json ------------------------------------------------------ #
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        violations.append("missing manifest.json (preregistration)")
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            violations.append(f"manifest.json unparseable: {exc}")
            manifest = {}
        for key in REQUIRED_MANIFEST_KEYS:
            if key not in manifest:
                violations.append(f"manifest.json missing key: {key}")
        judge = manifest.get("judge", {})
        families = judge.get("families", [])
        if len(families) < 2:
            violations.append(
                "manifest judge.families has <2 independent judge families (§17.4)"
            )
        if len(judge.get("dimensions", [])) < 4:
            violations.append(
                "manifest judge.dimensions has <4 independent dimensions (§17.1)"
            )
        protocol = manifest.get("protocol", {})
        if not protocol.get("weibull_frozen"):
            violations.append("manifest protocol.weibull_frozen missing (§17.5)")

    # --- report.md (OKF) ---------------------------------------------------- #
    report_path = run_dir / "report.md"
    if not report_path.exists():
        violations.append("missing report.md (OKF experiment-report)")
    else:
        text = report_path.read_text(encoding="utf-8")
        meta = _parse_frontmatter(text)
        if meta is None:
            violations.append("report.md frontmatter missing or unparseable (OKF)")
        elif meta.get("type") != "experiment-report":
            violations.append(
                f"report.md frontmatter type={meta.get('type')!r} != 'experiment-report'"
            )
        for section in REQUIRED_REPORT_SECTIONS:
            if f"# {section}" not in text and f"## {section}" not in text:
                violations.append(f"report.md missing section: {section}")

    # --- trace.json --------------------------------------------------------- #
    trace_path = run_dir / "trace.json"
    if not trace_path.exists():
        violations.append("missing trace.json")
    else:
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            violations.append(f"trace.json unparseable: {exc}")
            trace = []
        entries = trace if isinstance(trace, list) else trace.get("entries", [])
        if not isinstance(entries, list) or not entries:
            violations.append("trace.json has no proactive-message entries")
        else:
            for i, entry in enumerate(entries[:5]):
                for key in ("message_id", "intent_id", "reason", "source_type",
                            "source_id", "ok"):
                    if key not in entry:
                        violations.append(f"trace.json entry[{i}] missing key: {key}")

    # --- metrics + summary -------------------------------------------------- #
    metrics_paths = sorted(run_dir.glob("metrics_*.json"))
    if not metrics_paths:
        violations.append("missing metrics_*.json")
    for mpath in metrics_paths:
        try:
            metrics = json.loads(mpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            violations.append(f"{mpath.name} unparseable: {exc}")
            continue
        for key in ("M1_grounded_rate", "M3_recall", "M5_arc_continuity",
                    "M7_restart_loss", "M11_leak_hits"):
            if key not in metrics:
                violations.append(f"{mpath.name} missing metric key: {key}")

    summary_path = run_dir / "vertical_summary.json"
    if not summary_path.exists():
        violations.append("missing vertical_summary.json")
    else:
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            violations.append(f"vertical_summary.json unparseable: {exc}")
            summary = {}
        invariants = summary.get("invariants", {})
        for key in HARD_ZERO_KEYS:
            if key not in invariants:
                violations.append(f"vertical_summary.json missing invariant: {key}")
                continue
            if int(invariants[key]) != 0:
                violations.append(
                    f"hard invariant violated: {key} = {invariants[key]} (must be 0)"
                )
        if not summary.get("validated"):
            violations.append("vertical_summary.json validated != true")
        if not summary.get("checkpoints"):
            violations.append("vertical_summary.json checkpoints empty (5 required)")
        elif len(summary["checkpoints"]) < 5:
            violations.append(
                f"vertical_summary.json checkpoints < 5 ({len(summary['checkpoints'])})"
            )

    return violations


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: validate_okf.py <run_dir>")
        return 2
    violations = check_run_dir(Path(argv[0]))
    if violations:
        print(f"FAIL: {len(violations)} violation(s) in {argv[0]}")
        for v in violations:
            print(f"  - {v}")
        return 1
    print(f"OK: {argv[0]} validated (manifest + OKF report + trace + invariants)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
