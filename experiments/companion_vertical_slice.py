"""Driver del harness de evaluación (Iteración 2, A8) — companion vertical slice.

Reconstruye el prototipo E0 (congelado en eval-exploratory-2026-08-08) como el
harness de preregistro de la Iteración 2:

    manifest   emite el manifest de preregistro (Gate 4)
    vertical   Track vertical completo: clean start -> bootstrap -> días
               acelerados -> 5 checkpoints/restarts -> auditoría mecánica ->
               reporte OKF + trace.json (validado con validate_okf.py)
    memory     Track memoria: mismas conversaciones en RAW_CONTEXT /
               VERBATIM_RAG / STRUCTURED_MEMORY + métricas de cadena de
               eventos (§17.2)
    state      Track estado: observabilidad estructurada + dinámica a nivel
               mensaje (NO_STATE / PROMPT_ONLY_STATE / MECHANICALLY_ACTUATED)
    replay     replay exacto de un escenario grabado (semillas + filas M3)
    judge      pasadas de juez ciegas/barajadas, 4 dimensiones (§17.1), con
               >=2 familias de juez independientes (§17.4)
    matrix     célula de la matriz real (Gate 4/6 — el runner carga
               OPENCODE_GO_API_KEY desde ~/.hermes/.env, NUNCA la imprime)
    report     regenera el reporte OKF de un run
    validate   valida un directorio de run con experiments.validation.validate_okf

Convención del repo: docstrings en español, identificadores en inglés.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from harness.client import OpenAICompatibleClient
from harness.store import SQLiteStore

from experiments import cvs_common
from experiments.cvs_common import (
    DEFAULT_CHECKPOINT_DAYS,
    DeterministicClient,
    MockJudgeClient,
    mechanical_audit,
    render_transcript,
    run_cell,
    user_script,
)
from experiments.cvs_manifest import (
    DIMENSIONS,
    JUDGE_FAMILIES,
    JUDGE_PASSES,
    JUDGE_SHUFFLE_SEED_BASE,
    MATRIX_CONDITIONS,
    MEMORY_CONDITIONS,
    STATE_CONDITIONS,
    THRESHOLDS,
    write_manifest,
)
from experiments.validation.validate_okf import check_run_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = Path.home() / ".hermes" / ".env"

#: Título del reporte con la semilla fija (convención del repo).
def _vertical_title(seed: int, days: int) -> str:
    return (
        f"Iteration-2 vertical slice — mock validation "
        f"(seed {seed}, {days} days, {len(DEFAULT_CHECKPOINT_DAYS)} checkpoints)"
    )


# --------------------------------------------------------------------------- #
# Env (clave real NUNCA impresa)
# --------------------------------------------------------------------------- #


def _load_env() -> None:
    """Carga ~/.hermes/.env al entorno (best-effort, sin imprimir secretos)."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    # Mapeo OPENCODE_GO_* -> LLM_*: client.py lee LLM_API_KEY/LLM_BASE_URL,
    # pero el archivo de Hermes guarda la clave como OPENCODE_GO_API_KEY
    # (probe G6: runtime moría en el primer call con bearer vacío y todos
    # los feeds se omitían en silencio). Nunca pisa valores ya presentes.
    if "LLM_API_KEY" not in os.environ and os.environ.get("OPENCODE_GO_API_KEY"):
        os.environ["LLM_API_KEY"] = os.environ["OPENCODE_GO_API_KEY"]
    if "LLM_BASE_URL" not in os.environ and os.environ.get("OPENCODE_GO_BASE_URL"):
        os.environ["LLM_BASE_URL"] = os.environ["OPENCODE_GO_BASE_URL"]


def _require_key(env_key: str) -> None:
    """Exige la clave de API en el entorno (el mensaje de error NUNCA la imprime)."""
    _load_env()
    if not os.environ.get(env_key):
        raise RuntimeError(
            f"missing API key for judge family: env var {env_key} is empty "
            f"(source it from ~/.hermes/.env — the key itself is never printed)"
        )


# --------------------------------------------------------------------------- #
# Reporte OKF
# --------------------------------------------------------------------------- #


def _frontmatter(title: str, description: str, tags: list[str],
                 timestamp: str) -> str:
    return (
        "---\n"
        f"type: experiment-report\n"
        f"title: {title}\n"
        f'description: "{description}"\n'
        f"tags: [{', '.join(tags)}]\n"
        f"timestamp: {timestamp}\n"
        "---\n"
    )


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def _metrics_table(metrics: dict) -> str:
    headers = ["Metric", "Value", "Bar", "PASS/FAIL"]
    soft = THRESHOLDS["soft_bars"]
    rows = []
    for key, value in metrics.items():
        if key in ("seed", "condition", "leak_hits_detail", "g_bare_hits",
                   "n_proactive", "n_messages", "probe_detail",
                   "arc_contribution_days", "i4_violations",
                   "restart_loss_detail", "grounding_failures_detail"):
            continue
        bar = soft.get(key)
        if bar is None:
            rows.append([key, str(value), "—", "—"])
            continue
        v = float(value)
        direction = bar["direction"]
        ok = v >= bar["value"] if direction in (">=", ">") else v <= bar["value"]
        rows.append([key, str(value), f"{direction} {bar['value']}",
                     "PASS" if ok else "FAIL"])
    return _markdown_table(headers, rows)


def _audit_table(audit: dict) -> str:
    rows = [
        ["ungrounded_proactive", audit["ungrounded_proactive"]],
        ["wrong_intent", audit["wrong_intent"]],
        ["restart_state_loss", audit["restart_state_loss"]],
        ["stranded_opportunities", audit["stranded_opportunities"]],
        ["cycle_state_leakage", audit["cycle_state_leakage"]],
        ["memory_provenance_failures", audit["memory_provenance_failures"]],
        ["duplicate_turns", audit["duplicate_turns"]],
        ["life_dead_duration", audit["life_dead_duration"]],
        ["i4_violations", len(audit["i4_violations"])],
        ["fixture inserts (user/assistant)", audit["fixture_inserts"]],
    ]
    return _markdown_table(["Hard invariant", "Value (must be 0)"],
                           [[k, str(v)] for k, v in rows])


def _event_chain_table(chain_metrics: dict) -> str:
    rows = []
    for cid, cls in chain_metrics.items():
        rows.append([cid, str(cls["events"]), str(cls["covered"]),
                     "YES" if cls["AnyEvidence"] else "no",
                     "YES" if cls["LatestEvidence"] else "no",
                     "YES" if cls["CompleteChain"] else "no"])
    return _markdown_table(
        ["Chain", "Events", "Covered", "AnyEvidence", "LatestEvidence",
         "CompleteChain"], rows)


def _perturbation_table(pert: dict) -> str:
    rows = []
    for lane, metrics in list(pert["latent"].items()) + list(pert["observable"].items()):
        m = metrics
        rows.append([lane, str(m["baseline_mean"]), str(m["block_mean"]),
                     str(m["deviation"]), str(m["peak_deviation"]),
                     str(m["persistence_days"]), str(m["recovery_time_days"])])
    ff = pert["failure_frequency"]
    rows.append(["failures during block",
                 f"{ff['failures_during_block']} / {ff['n_proactive_during_block']} "
                 f"proactive messages"])
    return _markdown_table(
        ["Quantity", "Baseline", "Block mean", "Deviation", "Peak dev",
         "Persistence (d)", "Recovery (d)"], rows)


def write_vertical_report(out_dir: Path, summary: dict) -> Path:
    """Escribe report.md (OKF experiment-report) desde el summary del run."""
    title = _vertical_title(int(summary["seed"]), int(summary["days"]))
    description = (
        "Preregistered mock vertical run: clean bootstrap (Gate-2 user), "
        f"{summary['days']} accelerated days, {len(summary['checkpoints'])} "
        "restart checkpoints, perturbation + recovery block (§17.3), event-chain "
        "probes (§17.2), 4-dimension judge protocol (§17.1/§17.4), mechanical "
        "audit with hard invariants, exact replay support (M3)."
    )
    tags = ["llm-behavioral-harness", "it2", "vertical-slice", "mock",
            f"seed-{summary['seed']}"]
    lines = [
        _frontmatter(title, description, tags, summary["timestamp"]),
        "",
        f"# {title}",
        "",
        "## Run summary",
        "",
        _markdown_table(
            ["Field", "Value"],
            [
                ["seed", summary["seed"]],
                ["days", summary["days"]],
                ["checkpoints (end of day)", ", ".join(map(str, summary["checkpoints"]))],
                ["messages", summary["n_messages"]],
                ["proactive messages", summary["n_proactive"]],
                ["condition", summary["condition"]],
                ["client", summary["client"]],
                ["commit", summary["commit"]],
            ],
        ),
        "",
        "## Mechanical audit",
        "",
        _audit_table(summary["audit"]),
        "",
        "## Metrics vs frozen thresholds",
        "",
        _metrics_table(summary["metrics"]),
        "",
        "## Event-chain (§17.2)",
        "",
        "AnyEvidence = at least one causally necessary event retrieved; "
        "LatestEvidence = the latest/currently-valid event retrieved; "
        "CompleteChain = ALL necessary events retrieved (one relevant fact "
        "never counts as continuity).",
        "",
        _event_chain_table(summary["event_chains"]),
        "",
        "## Perturbation + recovery (§17.3)",
        "",
        f"Negative interaction block on 1-indexed days "
        f"{summary['perturbation']['block_days_1idx']}; baseline before, "
        "neutral recovery after. Latent = daily mood M; observable = daily "
        "mean initiative / max_tokens / response delay.",
        "",
        _perturbation_table(summary["perturbation"]),
        "",
        "## State observability",
        "",
        _markdown_table(
            ["Quantity", "Value"],
            [
                ["M8a rho(initiative, max_tokens)", summary["state_metrics"]["behavioral_dynamics"]["M8a_rho_init_tokens"]],
                ["M8b token gap", summary["state_metrics"]["behavioral_dynamics"]["M8b_token_gap"]],
                ["M8c rho(energy, delay)", summary["state_metrics"]["behavioral_dynamics"]["M8c_rho_energy_delay"]],
                ["M10 rho(proactive, initiative)", summary["state_metrics"]["behavioral_dynamics"]["M10_rho_proactive_initiative"]],
                ["rho(M, initiative)", summary["state_metrics"]["state_to_observable"]["rho_M_initiative"]],
                ["rho(g, delay)", summary["state_metrics"]["state_to_observable"]["rho_g_delay"]],
                ["delay sd (s)", summary["state_metrics"]["behavioral_dynamics"]["delay_sd"]],
                ["initiative sd", summary["state_metrics"]["behavioral_dynamics"]["initiative_sd"]],
                ["arcs active/completed/abandoned", f"{summary['state_metrics']['structured_observability']['n_arcs_active_end']} / {summary['state_metrics']['structured_observability']['n_arcs_completed']} / {summary['state_metrics']['structured_observability']['n_arcs_abandoned']}"],
            ],
        ),
        "",
        "## Judge protocol (§17.1/§17.4)",
        "",
        "Four independent dimensions (never one collapsed score): "
        + "; ".join(f"{d['id']} ({d['name']})" for d in DIMENSIONS) + ".",
        "",
        "Judge families (>=2 on the final subset): "
        + "; ".join(f"{f['id']} ({f['family']}, {f['model']})" for f in JUDGE_FAMILIES)
        + ". Blind, independently shuffled passes; per-dimension disagreement "
        "reported; an effect seen by only one judge family is not established "
        "companion behavior.",
        "",
        "## Replay / reproducibility",
        "",
        _markdown_table(
            ["Field", "Value"],
            [
                ["seed", summary["seed"]],
                ["replay command", f"python -m experiments.companion_vertical_slice replay --out {out_dir}"],
                ["M3 repro rows readable", "yes (audit_mode store, repro_json populated)"],
            ],
        ),
        "",
        "## Validation",
        "",
        f"validate_okf.py: **{'PASS' if summary['validated'] else 'FAIL'}** — "
        f"{summary['validation_errors']} violation(s).",
        "",
    ]
    report = out_dir / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def write_track_report(out_dir: Path, track: str, summary: dict) -> Path:
    """Reporte OKF compacto para los tracks de memoria y estado."""
    title = (f"Iteration-2 {track} track — mock validation "
             f"(seed {summary['seed']}, {summary['days']} days)")
    lines = [
        _frontmatter(
            title,
            f"Preregistered {track} track: same conversations across conditions, "
            f"seed {summary['seed']}, deterministic mock client.",
            ["llm-behavioral-harness", "it2", track, "mock", f"seed-{summary['seed']}"],
            summary["timestamp"],
        ),
        "",
        f"# {title}",
        "",
    ]
    if track == "memory":
        rows = []
        for cond in MEMORY_CONDITIONS:
            m = summary["conditions"][cond]
            rows.append([
                cond,
                f"{m['recall']:.3f}",
                f"{m['any']}/{m['n_chains']}",
                f"{m['latest']}/{m['n_chains']}",
                f"{m['complete']}/{m['n_chains']}",
                str(m["n_proactive"]),
            ])
        lines.append("## Event-chain completeness per condition (§17.2)")
        lines.append("")
        lines.append(_markdown_table(
            ["Condition", "Recall@8", "AnyEvidence", "LatestEvidence",
             "CompleteChain", "Proactive msgs"], rows))
        lines.append("")
        lines.append("CompleteChain requires ALL causally/temporally necessary "
                     "events retrieved — one relevant fact is not continuity.")
    else:  # state
        rows = []
        for cond in STATE_CONDITIONS:
            m = summary["conditions"][cond]
            bd = m["behavioral_dynamics"]
            rows.append([
                cond,
                str(bd["M8a_rho_init_tokens"]), str(bd["M8b_token_gap"]),
                str(bd["initiative_sd"]), str(bd["delay_sd"]),
                str(bd["M10_rho_proactive_initiative"]),
            ])
        lines.append("## Behavioral dynamics per condition (§17.1 dim 4)")
        lines.append("")
        lines.append(_markdown_table(
            ["Condition", "M8a rho(init,tok)", "M8b token gap", "initiative sd",
             "delay sd", "M10 rho(proactive,init)"], rows))
        lines.append("")
        lines.append("MECHANICALLY_ACTUATED_STATE must show measurable coupling; "
                     "PROMPT_ONLY_STATE and NO_STATE must not (H3/H4).")
    lines.append("")
    report = out_dir / f"report_{track}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


# --------------------------------------------------------------------------- #
# trace.json
# --------------------------------------------------------------------------- #


def build_trace(out_dir: Path, seed: int) -> list[dict]:
    """Cadenas de proveniencia por mensaje proactivo del run grabado."""
    db = out_dir / f"cell_full_seed{seed}.db"
    store = SQLiteStore(db)
    msgs = cvs_common._all_messages(store)
    entries = []
    for m in msgs:
        if not m["proactive"]:
            continue
        iid = m.get("intent_id")
        entry: dict = {
            "message_id": int(m["id"]),
            "day": int(m["day"]),
            "t_h": round(float(m["t_h"]), 4),
            "intent_id": iid,
            "reason": None,
            "source_type": None,
            "source_id": None,
            "hook": None,
            "opportunity_id": None,
            "valid_until_t_h": None,
            "ok": False,
        }
        if iid:
            intent = store.load_proactive_intent(iid)
            if intent is not None:
                entry.update({
                    "reason": intent.reason,
                    "source_type": intent.source_type,
                    "source_id": intent.source_id,
                    "hook": intent.hook,
                    "opportunity_id": intent.opportunity_id,
                    "valid_until_t_h": round(intent.valid_until_t_h, 4),
                })
                src = store.resolve_intent_source(intent)
                entry["source_exists"] = src is not None
                if src is not None:
                    from harness.proactive import compose_hook

                    entry["hook_matches"] = compose_hook(src, intent.reason) == intent.hook
                    entry["source_status"] = getattr(src, "status", None)
                entry["status"] = cvs_common._intent_status(store, iid)
                entry["ok"] = (
                    entry["source_exists"] and entry.get("hook_matches", False)
                    and entry["status"] == "fired"
                )
        entries.append(entry)
    store.close()
    return entries


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #


def cmd_manifest(args) -> int:
    out = Path(args.out)
    manifest = write_manifest(out)
    print(f"manifest written: {out} (commit={manifest['commit']} "
          f"dirty={manifest['dirty']} config_hash={manifest['config_hash']})")
    print(f"  hypotheses: {[h['id'] for h in manifest['hypotheses']]}")
    print(f"  judge families: {[f['id'] for f in manifest['judge']['families']]}")
    print(f"  dimensions: {[d['id'] for d in manifest['judge']['dimensions']]}")
    return 0


def _invariants_from(audit: dict, records: dict) -> dict:
    return {
        "ungrounded_proactive": audit["ungrounded_proactive"],
        "wrong_intent": audit["wrong_intent"],
        "restart_state_loss": sum(rl["diffs"] for rl in records["restart_loss"]),
        "stranded_opportunities": audit["stranded_opportunities"],
        "cycle_state_leakage": audit["cycle_state_leakage"],
        "memory_provenance_failures": audit["memory_provenance_failures"],
        "duplicate_turns": audit["duplicate_turns"],
        "life_dead_duration": audit["life_dead_duration"],
    }


def cmd_vertical(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    seed, days = int(args.seed), int(args.days)
    fake = bool(args.fake)
    checkpoints = (
        tuple(int(x) for x in args.checkpoints.split(","))
        if args.checkpoints
        else DEFAULT_CHECKPOINT_DAYS
    )
    if not fake:
        # Carga ~/.hermes/.env y mapea OPENCODE_GO_* -> LLM_* (probe G6:
        # sin esto el cliente veía bearer vacío, el runtime moría en el
        # primer chat y la celda "completaba" hueca con 0 mensajes).
        _require_key("OPENCODE_GO_API_KEY")

    # 1. Preregistro ANTES de generar (Gate 4).
    manifest = write_manifest(out_dir / "manifest.json", repo_root=REPO_ROOT)

    # 2. Célula vertical (clean start -> bootstrap -> días -> 5 restarts).
    records = run_cell(
        "FULL", seed, out_dir, days=days,
        checkpoints=checkpoints, fake=fake, perturb=True,
    )

    # 3. Auditoría mecánica + métricas + cadenas + perturbación + estado.
    store = SQLiteStore(records["db"])
    script = user_script(seed, days, perturb=True)
    pool = None
    if fake:
        client = DeterministicClient(seed)
        pool = client.pool
    n_skipped = len(records.get("skipped_feeds", []))
    audit = mechanical_audit(store, seed, days * 24.0, days, script, pool=pool,
                             n_skipped=n_skipped)
    audit["skipped_feeds"] = records.get("skipped_feeds", [])
    audit["restart_state_loss"] = sum(rl["diffs"] for rl in records["restart_loss"])
    audit["all_hard_zero"] = (
        audit["all_hard_zero"]
        and audit["restart_state_loss"] == 0
    )
    metrics = cvs_common.compute_structural_metrics(store, records, "FULL", seed, days)
    chains = cvs_common.event_chain_metrics(store)
    perturb = cvs_common.compute_perturbation_metrics(store, records, days)
    state = cvs_common.compute_state_metrics(store, records, days)
    store.close()

    # 4. Artefactos.
    (out_dir / "audit_seed5001.json" if seed == 5001 else
     out_dir / f"audit_seed{seed}.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / f"metrics_FULL_seed{seed}.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / f"event_chains_seed{seed}.json").write_text(
        json.dumps(chains, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / f"perturbation_seed{seed}.json").write_text(
        json.dumps(perturb, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / f"state_metrics_seed{seed}.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    trace = build_trace(out_dir, seed)
    trace_payload = {
        "experiment": manifest["experiment"],
        "commit": manifest["commit"],
        "seed": seed,
        "entries": trace,
    }
    (out_dir / "trace.json").write_text(
        json.dumps(trace_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    transcripts = out_dir / "transcripts"
    transcripts.mkdir(exist_ok=True)
    store = SQLiteStore(records["db"])
    txt = render_transcript(store)
    store.close()
    (transcripts / f"FULL_seed{seed}.txt").write_text(txt, encoding="utf-8")

    # 5. Summary + reporte OKF + validación.
    invariants = _invariants_from(audit, records)
    validated = audit["all_hard_zero"] and all(v == 0 for v in invariants.values())
    summary = {
        "seed": seed,
        "days": days,
        "condition": "FULL",
        "client": "fake" if fake else "real",
        "checkpoints": [int(d) for d in checkpoints if d <= days],
        "n_messages": records["n_messages"],
        "n_proactive": records["n_proactive"],
        "commit": manifest["commit"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "audit": audit,
        "event_chains": chains,
        "perturbation": {
            "block_days_1idx": [d + 1 for d in perturb["block_days_0idx"]],
            "latent": perturb["latent"],
            "observable": perturb["observable"],
            "failure_frequency": perturb["failure_frequency"],
        },
        "state_metrics": state,
        "invariants": invariants,
        "validated": validated,
        "validation_errors": 0,
    }
    (out_dir / "vertical_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    write_vertical_report(out_dir, summary)

    violations = check_run_dir(out_dir)
    summary["validation_errors"] = len(violations)
    summary["validated"] = validated and not violations
    (out_dir / "vertical_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if violations:
        write_vertical_report(out_dir, summary)

    print(json.dumps({
        "cmd": "vertical",
        "seed": seed,
        "days": days,
        "checkpoints": summary["checkpoints"],
        "n_messages": summary["n_messages"],
        "n_proactive": summary["n_proactive"],
        "invariants": invariants,
        "all_hard_zero": audit["all_hard_zero"],
        "validated": summary["validated"],
        "validation_errors": summary["validation_errors"],
        "out": str(out_dir),
    }, indent=2, ensure_ascii=False))
    return 0 if summary["validated"] else 1


def cmd_memory(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    seed, days = int(args.seed), int(args.days)
    fake = bool(args.fake)
    write_manifest(out_dir / "manifest.json", repo_root=REPO_ROOT)

    from harness.domain import MemoryPolicy

    policies = {
        "RAW_CONTEXT": MemoryPolicy.RAW_CONTEXT,
        "VERBATIM_RAG": MemoryPolicy.VERBATIM_RAG,
        "STRUCTURED_MEMORY": MemoryPolicy.STRUCTURED_MEMORY,
    }
    per_condition: dict = {}
    for cond, policy in policies.items():
        records = run_cell(cond, seed, out_dir, days=days, fake=fake,
                           perturb=False, memory_policy=policy)
        store = SQLiteStore(records["db"])
        chains = cvs_common.event_chain_metrics(store, memory_policy=policy)
        recall = cvs_common.recall_probe_metrics(store, memory_policy=policy)
        n_pro = sum(1 for m in cvs_common._all_messages(store) if m["proactive"])
        store.close()
        n_chains = len(chains)
        per_condition[cond] = {
            "recall": recall["M3_recall"],
            "n_chains": n_chains,
            "any": sum(1 for c in chains.values() if c["AnyEvidence"]),
            "latest": sum(1 for c in chains.values() if c["LatestEvidence"]),
            "complete": sum(1 for c in chains.values() if c["CompleteChain"]),
            "chains": chains,
            "recall_detail": recall["detail"],
            "n_proactive": n_pro,
        }
    summary = {
        "seed": seed,
        "days": days,
        "condition": "memory-track",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "conditions": per_condition,
    }
    (out_dir / "memory_track.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    write_track_report(out_dir, "memory", summary)
    print(json.dumps({
        "cmd": "memory",
        "seed": seed,
        "conditions": {
            c: {k: v for k, v in m.items() if k in ("recall", "any", "latest",
                                                    "complete", "n_chains",
                                                    "n_proactive")}
            for c, m in per_condition.items()
        },
        "out": str(out_dir),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_state(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    seed, days = int(args.seed), int(args.days)
    fake = bool(args.fake)
    write_manifest(out_dir / "manifest.json", repo_root=REPO_ROOT)

    per_condition: dict = {}
    for cond in STATE_CONDITIONS:
        records = run_cell(cond, seed, out_dir, days=days, fake=fake,
                           perturb=False, checkpoints=())
        store = SQLiteStore(records["db"])
        state = cvs_common.compute_state_metrics(store, records, days)
        store.close()
        per_condition[cond] = state
    summary = {
        "seed": seed,
        "days": days,
        "condition": "state-track",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "conditions": per_condition,
    }
    (out_dir / "state_track.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    write_track_report(out_dir, "state", summary)
    print(json.dumps({
        "cmd": "state",
        "seed": seed,
        "conditions": {
            c: m["behavioral_dynamics"] for c, m in per_condition.items()
        },
        "out": str(out_dir),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_replay(args) -> int:
    out_dir = Path(args.out)
    seed = int(args.seed)
    days = int(args.days)
    audit = cvs_common.run_replay(seed, days, out_dir, out_dir)
    print(json.dumps(audit, indent=2, ensure_ascii=False))
    return 0 if audit["replay_exact"] else 1


# --------------------------------------------------------------------------- #
# Judge: pasadas ciegas/barajadas, 4 dimensiones, >=2 familias (§17.1/§17.4)
# --------------------------------------------------------------------------- #


JUDGE_RUBRIC = (
    "You are rating transcripts of an AI companion system. For each of the "
    "following 4 INDEPENDENT dimensions, give an integer score from 1 to 9 "
    "using the anchored scale shown. Rate each dimension separately — never "
    "collapse into one overall score.\n"
    + "\n".join(
        f"- {d['name']}: 1 = {d['anchor_1']}; 9 = {d['anchor_9']}"
        for d in DIMENSIONS
    )
    + "\nRespond ONLY with a JSON object: "
    + "{" + ", ".join(f'"{d["id"]}": n' for d in DIMENSIONS) + "} "
    + "where n is an integer 1-9."
)


def _dimension_ids() -> list[str]:
    return [d["id"] for d in DIMENSIONS]


def _transcripts(out_dir: Path) -> dict[str, str]:
    tdir = out_dir / "transcripts"
    if not tdir.exists():
        raise FileNotFoundError(f"no transcripts dir: {tdir}")
    return {f.stem: f.read_text(encoding="utf-8") for f in sorted(tdir.glob("*.txt"))}


def _judge_client(family_id: str, fake: bool):
    if fake:
        seed = sum(ord(c) for c in family_id)
        return MockJudgeClient(seed, family=family_id, model="mock")
    family = next((f for f in JUDGE_FAMILIES if f["id"] == family_id), None)
    if family is None:
        raise ValueError(f"unknown judge family: {family_id}")
    _require_key(family["env_key"])
    return OpenAICompatibleClient(
        base_url=family["base_url"], model=family["model"]
    )


def _parse_transcript_stem(stem: str) -> tuple[str, int]:
    """'FULL_seed5001' | 'FULL_5001' -> ('FULL', 5001)."""
    cond, _, seed_s = stem.rpartition("_")
    if seed_s.startswith("seed"):
        seed_s = seed_s[len("seed"):]
    return cond, int(seed_s)


def _run_judge_pass(out_dir: Path, pass_id: int, family_id: str, fake: bool) -> dict:
    transcripts = _transcripts(out_dir)
    items = [_parse_transcript_stem(stem) for stem in transcripts]
    rng = np.random.default_rng(JUDGE_SHUFFLE_SEED_BASE + pass_id)
    order = cvs_common.shuffled_order(rng, items)
    client = _judge_client(family_id, fake)
    try:
        results = {}
        for tid, (cond, seed) in zip(
                [f"T{i + 1:02d}" for i in range(len(order))], order):
            raw = client.chat(
                [{"role": "user",
                  "content": f"{JUDGE_RUBRIC}\n\nTranscript:\n{transcripts[f'{cond}_seed{seed}']}"}],
                system="You are a careful, precise evaluator. Rate precisely.",
                temperature=0.0,
                json_mode=True,
            )
            ratings = cvs_common.parse_ratings(raw, _dimension_ids())
            results[tid] = {"condition": cond, "seed": seed, "ratings": ratings}
    finally:
        client.close()
    mapping = {tid: {"condition": c, "seed": s} for tid, (c, s) in zip(
        [f"T{i + 1:02d}" for i in range(len(order))], order)}
    (out_dir / f"judge_order{pass_id}.json").write_text(
        json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / f"judge_pass{pass_id}_{family_id}.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return results


def _judge_report(out_dir: Path) -> dict:
    """Agrega pasadas y familias: medias por dimensión/condición + acuerdo
    inter-familia (§17.4) y por pasada."""
    dims = _dimension_ids()
    pass_files = sorted(out_dir.glob("judge_pass*_*.json"))
    by_family: dict[str, dict[str, dict]] = {}
    for pf in pass_files:
        family = pf.stem.split("_")[-1]
        # stem: judge_pass<id>_<family> -> pass id en [1]
        pass_key = pf.stem.split("_")[1].removeprefix("pass")
        data = json.loads(pf.read_text(encoding="utf-8"))
        by_family.setdefault(family, {})[pass_key] = data

    per_dim_family: dict[str, dict[str, dict]] = {}
    for family, passes in by_family.items():
        per_dim_family[family] = {}
        for dim in dims:
            vals = [v["ratings"].get(dim)
                    for data in passes.values() for v in data.values()
                    if v["ratings"].get(dim) is not None]
            per_dim_family[family][dim] = {
                "mean": round(float(np.mean(vals)), 3) if vals else None,
                "sd": round(float(np.std(vals)), 3) if len(vals) > 1 else 0.0,
                "n": len(vals),
            }

    # Acuerdo inter-familia: Pearson r por dimensión sobre transcript ids.
    agreement: dict[str, float | None] = {}
    families = list(by_family)
    if len(families) >= 2:
        f0, f1 = families[0], families[1]
        # primera pasada que AMBAS familias tienen (nunca hardcodear "1").
        common = sorted(set(by_family[f0]) & set(by_family[f1]))
        pk = common[0] if common else None
        for dim in dims:
            x, y = [], []
            if pk is not None:
                for tid in sorted(by_family[f0][pk]):
                    v0 = by_family[f0][pk][tid]["ratings"].get(dim)
                    v1 = by_family[f1][pk][tid]["ratings"].get(dim)
                    if v0 is not None and v1 is not None:
                        x.append(v0)
                        y.append(v1)
            if len(x) > 2:
                a = np.asarray(x, dtype=float)
                b = np.asarray(y, dtype=float)
                r = float(np.corrcoef(a, b)[0, 1])
                agreement[dim] = None if r != r else round(r, 3)
            else:
                agreement[dim] = None

    report = {
        "dimensions": dims,
        "per_family_per_dimension": per_dim_family,
        "inter_family_agreement": agreement,
        "n_families": len(families),
        "n_passes": JUDGE_PASSES,
        "rule": (
            "An effect seen by only one judge family is NOT established "
            "companion behavior (§17.4); disagreement is reported per dimension."
        ),
    }
    (out_dir / "judge_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def cmd_judge(args) -> int:
    out_dir = Path(args.out)
    if args.report:
        report = _judge_report(out_dir)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    results = _run_judge_pass(out_dir, int(args.pass_id), args.family,
                              bool(args.fake))
    print(json.dumps({k: v for k, v in results.items()}, indent=2,
                     ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
# Matriz real (Gate 4/6 — orquestador)
# --------------------------------------------------------------------------- #


def cmd_matrix(args) -> int:
    """Una célula de la matriz real (7 condiciones x 5 semillas).

    Sin ``--fake`` exige OPENCODE_GO_API_KEY (cargada desde ~/.hermes/.env por
    el runner; la clave NUNCA se imprime). Este comando es el paso Gate 4/6
    del orquestador — el harness CI usa --fake.
    """
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = int(args.seed)
    fake = bool(args.fake)
    if not fake:
        _require_key("OPENCODE_GO_API_KEY")
    records = run_cell(args.condition, seed, out_dir, days=30,
                       checkpoints=(), fake=fake, perturb=False)
    store = SQLiteStore(records["db"])
    txt = render_transcript(store)
    store.close()
    tdir = out_dir / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{args.condition}_seed{seed}.txt").write_text(txt, encoding="utf-8")
    print(json.dumps({
        "cmd": "matrix",
        "condition": args.condition,
        "seed": seed,
        "fake": fake,
        "n_messages": records["n_messages"],
        "n_proactive": records["n_proactive"],
        "transcript_chars": len(txt),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_report(args) -> int:
    out_dir = Path(args.out)
    summary_path = out_dir / "vertical_summary.json"
    if not summary_path.exists():
        print(f"ERROR: no vertical_summary.json in {out_dir}")
        return 1
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    report = write_vertical_report(out_dir, summary)
    print(f"report regenerated: {report}")
    return 0


def cmd_validate(args) -> int:
    violations = check_run_dir(Path(args.out))
    if violations:
        print(f"FAIL: {len(violations)} violation(s)")
        for v in violations:
            print(f"  - {v}")
        return 1
    print(f"OK: {args.out} validated")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="companion_vertical_slice")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_manifest = sub.add_parser("manifest")
    p_manifest.add_argument("--out", type=str,
                            default="results/companion-vertical-slice/manifest.json")

    p_vertical = sub.add_parser("vertical")
    p_vertical.add_argument("--seed", type=int, required=True)
    p_vertical.add_argument("--fake", action="store_true",
                            help="cliente determinista (CI)")
    p_vertical.add_argument("--days", type=int, default=30)
    p_vertical.add_argument("--checkpoints", type=str, default="",
                            help="comma-separated 1-indexed restart days (default: "
                                 "DEFAULT_CHECKPOINT_DAYS)")
    p_vertical.add_argument("--out", type=str,
                            default="results/companion-vertical-slice/vertical")

    p_memory = sub.add_parser("memory")
    p_memory.add_argument("--seed", type=int, required=True)
    p_memory.add_argument("--fake", action="store_true")
    p_memory.add_argument("--days", type=int, default=16)
    p_memory.add_argument("--out", type=str,
                          default="results/companion-vertical-slice/memory")

    p_state = sub.add_parser("state")
    p_state.add_argument("--seed", type=int, required=True)
    p_state.add_argument("--fake", action="store_true")
    p_state.add_argument("--days", type=int, default=16)
    p_state.add_argument("--out", type=str,
                         default="results/companion-vertical-slice/state")

    p_replay = sub.add_parser("replay")
    p_replay.add_argument("--seed", type=int, required=True)
    p_replay.add_argument("--days", type=int, default=30)
    p_replay.add_argument("--out", type=str,
                          default="results/companion-vertical-slice/vertical")

    p_judge = sub.add_parser("judge")
    p_judge.add_argument("--out", type=str,
                         default="results/companion-vertical-slice")
    p_judge.add_argument("--pass", dest="pass_id", type=int, choices=(1, 2))
    p_judge.add_argument("--family", type=str, default="opencode-flash")
    p_judge.add_argument("--fake", action="store_true",
                         help="juez mock determinista (plumbing)")
    p_judge.add_argument("--report", action="store_true",
                         help="agrega pasadas/familias + acuerdo inter-familia")

    p_matrix = sub.add_parser("matrix")
    p_matrix.add_argument("--condition", choices=MATRIX_CONDITIONS, required=True)
    p_matrix.add_argument("--seed", type=int, required=True)
    p_matrix.add_argument("--fake", action="store_true")
    p_matrix.add_argument("--out", type=str,
                          default="results/companion-vertical-slice/matrix")

    p_report = sub.add_parser("report")
    p_report.add_argument("--out", type=str,
                          default="results/companion-vertical-slice/vertical")

    p_validate = sub.add_parser("validate")
    p_validate.add_argument("--out", type=str,
                            default="results/companion-vertical-slice/vertical")

    args = parser.parse_args(argv)
    handlers = {
        "manifest": cmd_manifest,
        "vertical": cmd_vertical,
        "memory": cmd_memory,
        "state": cmd_state,
        "replay": cmd_replay,
        "judge": cmd_judge,
        "matrix": cmd_matrix,
        "report": cmd_report,
        "validate": cmd_validate,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
