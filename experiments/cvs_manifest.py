"""Manifiesto de preregistro del harness de evaluación (Iteración 2, A8).

Genera el JSON de preregistro que congela —ANTES de generar resultados—
preguntas, hipótesis (H1-H6), condiciones, semillas, configuración de jueces
(≥2 familias independientes, 4 dimensiones §17.1), métricas, umbrales y la
declaración Weibull congelada (§17.5). Invariantes 19/20 del plan: los inputs
de evaluación deben ser reconstruibles desde un manifest inmutable; ninguna
hipótesis ni umbral cambia tras observar resultados sin crear un experimento
nuevo.

Convención del repo: docstrings en español, identificadores en inglés.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

EXPERIMENT_NAME = "it2-companion-vertical-slice"
SCHEMA_VERSION = "1.0"
EVALUATOR = "A8 eval harness (Iteration 2)"

#: Semillas congeladas de la matriz (plan §5-A8: 5 semillas fijas).
SEEDS = (5001, 5002, 5003, 5004, 5005)

#: Condiciones del eje memoria (plan §5-A8 Track A + invariante 11/12/13).
MEMORY_CONDITIONS = (
    "RAW_CONTEXT",
    "VERBATIM_RAG",
    "STRUCTURED_MEMORY",
)
#: Línea de ablación explícitamente experimental (invariante 12: variante
#: con boost de topicalidad SIEMPRE como experimento separado).
MEMORY_ABLATION_LANES = ("STRUCTURED_MEMORY_TOPICALITY_EXPERIMENT",)

#: Condiciones del eje estado (plan §5-A8 Track B).
STATE_CONDITIONS = ("NO_STATE", "PROMPT_ONLY_STATE", "MECHANICALLY_ACTUATED_STATE")

#: Condiciones del eje companion (plan §5-A8 Track C).
COMPANION_CONDITIONS = (
    "FULL",
    "NO_LIFE",
    "NO_MEMORY",
    "NO_ACTUATORS",
    "NO_TIMING_FEEDBACK",
)

#: Matriz completa de ablación (plan companion-vertical-slice §10).
MATRIX_CONDITIONS = (
    "FULL",
    "NO_ACTUATORS",
    "NO_LIFE",
    "NO_TIMING_FEEDBACK",
    "RAW_HISTORY",
    "SIMPLE_RAG",
    "STRUCTURED_NO_STATE",
)

#: Las 4 dimensiones INDEPENDIENTES de calidad (§17.1) — nunca una sola
#: puntuación colapsada. Cada dimensión lleva sus anclas de escala 1-9.
DIMENSIONS = (
    {
        "id": "persona_enactment",
        "name": "Persona enactment / identity consistency",
        "anchor_1": "each turn feels like a different persona; no stable identity",
        "anchor_9": "one coherent persona with a consistent voice and history throughout",
    },
    {
        "id": "trajectory_recall",
        "name": "Trajectory recall / temporal continuity",
        "anchor_1": "never recalls past events; timeline is flat or contradictory",
        "anchor_9": "consistently recalls the trajectory of shared events in correct temporal order",
    },
    {
        "id": "relational_quality",
        "name": "Relational quality",
        "anchor_1": "cold, generic, or intrusive; no sense of relationship",
        "anchor_9": "warm, respectful, attuned; the relationship deepens naturally",
    },
    {
        "id": "behavioral_dynamics",
        "name": "Behavioral dynamics / stochastic-state observability",
        "anchor_1": "robotic, identical responses; no rhythm or initiative",
        "anchor_9": "human-like variation in timing, initiative and engagement that tracks the situation",
    },
)

#: Familias de modelo juez (§17.4: ≥2 familias independientes en el subconjunto
#: final). La clave se lee del runner desde ~/.hermes/.env — NUNCA se imprime.
JUDGE_FAMILIES = (
    {
        "id": "opencode-flash",
        "family": "opencode-deepseek",
        "model": "deepseek-v4-flash",
        "base_url": "https://opencode.ai/zen/go/v1/",
        "env_key": "OPENCODE_GO_API_KEY",
        "role": "primary",
    },
    {
        "id": "openai-mini",
        "family": "openai-gpt",
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1/",
        "env_key": "OPENAI_API_KEY",
        "role": "secondary",
    },
)
JUDGE_PASSES = 2
JUDGE_SHUFFLE_SEED_BASE = 7000
JUDGE_MIN_FAMILIES_FINAL_SUBSET = 2

#: Umbrales congelados (nunca cambian tras ver resultados — Gate 4).
#: ``hard: true`` = invariante estructural; debe ser exactamente 0 en todo run.
THRESHOLDS = {
    "hard_zero": {
        "ungrounded_proactive": {"value": 0, "kind": "count", "meaning": "proactive message without a valid grounded intent (invariant 5)"},
        "wrong_intent": {"value": 0, "kind": "count", "meaning": "proactive message whose intent fails source/hook re-derivation (invariant 6/7)"},
        "restart_state_loss": {"value": 0, "kind": "count", "meaning": "persistent-state fingerprint diffs across restart (invariant 18)"},
        "stranded_opportunities": {"value": 0, "kind": "count", "meaning": "pending schedule events / intents overdue at run end"},
        "cycle_state_leakage": {"value": 0, "kind": "count", "meaning": "raw cycle/hormonal variables in conversation-visible text (invariant 16)"},
        "memory_provenance_failures": {"value": 0, "kind": "count", "meaning": "episodes whose source turns do not exist"},
        "duplicate_turns": {"value": 0, "kind": "count", "meaning": "duplicated turns across restart (resume must not rewind)"},
        "life_dead_duration": {"value": 0, "kind": "days", "meaning": "consecutive days at run end with no active arc and no completed arc"},
    },
    "soft_bars": {
        "M1_grounded_rate": {"value": 0.9, "kind": "rate", "direction": ">="},
        "M3_recall": {"value": 0.5, "kind": "rate", "direction": ">="},
        "M4_false_recall": {"value": 0.4, "kind": "rate", "direction": "<="},
        "M5_arc_continuity": {"value": 0.9, "kind": "rate", "direction": ">="},
        "M8a_rho_init_tokens": {"value": 0.0, "kind": "rho", "direction": ">", "meaning": "actuation must produce measurable state->observable coupling"},
    },
}

#: Declaración Weibull congelada (§17.5 — NO modificar el proceso de timing
#: en este sprint).
WEIBULL_FROZEN = (
    "The renewal/timing process (engine.timing, TimingParams — Weibull inter-"
    "contact distribution) is FROZEN for this sprint. No condition in this "
    "manifest modifies any timing parameter; NO_TIMING_FEEDBACK only disables "
    "the score->hazard feedback term A(score_{d-1}) (scores=None mode), it "
    "does NOT touch the renewal distribution. 'Modulated Weibull vs modulated "
    "lognormal renewal' is preregistered as a POST-CONFIRMATORY experiment "
    "(plan §17.5) and is out of scope here."
)

#: Hipótesis preregistradas (estructura H1-H6: IV/DV explícitas, dirección y
#: umbral). Mapean las 3 preguntas (memoria/estado/companion) + §17.
HYPOTHESES = (
    {
        "id": "H1",
        "question": "memory",
        "statement": "STRUCTURED_MEMORY achieves higher event-chain completeness than RAW_CONTEXT and VERBATIM_RAG on multi-event trajectories (one relevant fact does not count as continuity — §17.2).",
        "iv": "memory_policy (RAW_CONTEXT | VERBATIM_RAG | STRUCTURED_MEMORY)",
        "dv": ["CompleteChain rate", "LatestEvidence rate", "AnyEvidence rate"],
        "direction": "STRUCTURED_MEMORY > both baselines on CompleteChain rate",
        "threshold": "CompleteChain rate gap >= 0.2 vs RAW_CONTEXT on chain probes",
        "analysis": "per-condition event-chain rates over identical user scripts; judge-blind trajectory_recall dimension as convergent evidence",
    },
    {
        "id": "H2",
        "question": "memory",
        "statement": "Structured memory increases memory usefulness and grounded initiative: proactive messages grounded in memory sources (shared_interest / callback) appear only when the memory lane can recall trajectory evidence.",
        "iv": "memory_policy",
        "dv": ["memory_usefulness (judge dim)", "memory-grounded proactive rate", "recall@8 of probe facts"],
        "direction": "STRUCTURED_MEMORY >= VERBATIM_RAG >= RAW_CONTEXT",
        "threshold": "recall@8 probe rate >= 0.5 for STRUCTURED_MEMORY (mock bar)",
        "analysis": "probe recall + proactive source-type breakdown",
    },
    {
        "id": "H3",
        "question": "state",
        "statement": "MECHANICALLY_ACTUATED_STATE produces measurable downstream behavioral variation (response length, delay, initiative) while PROMPT_ONLY_STATE and NO_STATE do not — the stochastic state is actuation, not prompt decoration.",
        "iv": "state_condition (NO_STATE | PROMPT_ONLY_STATE | MECHANICALLY_ACTUATED_STATE)",
        "dv": ["M8a rho(initiative, max_tokens)", "M8b token gap high/low initiative", "M8c rho(energy, delay)", "response-length variance", "initiative variance", "delay variance"],
        "direction": "actuated variance/rhos > prompt-only > none",
        "threshold": "M8a rho > 0 and M8b gap > 0 only under actuation",
        "analysis": "message-level stochastic-state observability (state track)",
    },
    {
        "id": "H4",
        "question": "state",
        "statement": "Stochastic state is observable at the message level: proactive rate correlates with initiative (M10) and with previous-day score feedback (M9) only when mechanically actuated.",
        "iv": "state_condition",
        "dv": ["M9 rho(proactive count, previous score)", "M10 rho(proactive count, initiative)"],
        "direction": "|rho| larger under actuation",
        "threshold": "M10 sign matches initiative hypothesis under actuation; reported per condition",
        "analysis": "daily proactive counts vs initiative/score series",
    },
    {
        "id": "H5",
        "question": "companion",
        "statement": "The FULL system dominates ablations on vertical-track hard metrics: NO_LIFE raises life dead-state duration, NO_MEMORY lowers memory-grounded initiative and recall, NO_ACTUATORS flattens behavioral dynamics, NO_TIMING_FEEDBACK removes score->proactivity coupling.",
        "iv": "companion_condition (FULL | NO_LIFE | NO_MEMORY | NO_ACTUATORS | NO_TIMING_FEEDBACK)",
        "dv": ["hard invariants (zero everywhere)", "M1 grounded rate", "M5 arc continuity", "M8a/M8b/M9/M10", "life dead-state duration"],
        "direction": "ablation-specific (per-DV mapping in manifest conditions)",
        "threshold": "all hard invariants zero in every condition; ablation DVs differ from FULL in the stated direction",
        "analysis": "30 accelerated days x seeds 5001-5005 x 5 conditions; restarts at days 7/14/21/26/29",
    },
    {
        "id": "H6",
        "question": "perturbation",
        "statement": "A controlled negative interaction block (days 11-14) produces a measurable latent-state response (mood M) and an observable behavioral response (initiative/energy/max_tokens/delay) with recovery after the neutral period; failure frequency (ungrounded/wrong-intent) stays zero even under perturbation (§17.3).",
        "iv": "perturbation phase (baseline | negative block | neutral recovery)",
        "dv": ["latent response amplitude (M)", "observable response amplitude (initiative, tokens, delay)", "persistence duration", "recovery time", "failure frequency"],
        "direction": "response > 0 during block; recovery to baseline band; failures == 0",
        "threshold": "|M deviation| >= 0.5 mood-scale step during block; recovery time < block length * 3",
        "analysis": "perturbation + recovery blocks per §17.3, measured separately per quantity",
    },
)

#: Métricas estructurales (M1-M11) + estado + cadena de eventos + perturbación.
METRICS = (
    {"id": "M1_grounded_rate", "name": "grounded proactive rate", "definition": "proactive messages with valid intent + live source + matching hook / proactive messages"},
    {"id": "M2_invalid_source_rate", "name": "invalid source rate", "definition": "proactive messages failing source/hook re-derivation / proactive messages"},
    {"id": "M3_recall", "name": "probe recall@8", "definition": "fraction of recall probes whose expected episode is retrieved in top-8"},
    {"id": "M4_false_recall", "name": "false recall", "definition": "fraction of recalled probes not ranked first (distractor overlap)"},
    {"id": "M5_arc_continuity", "name": "life-arc continuity", "definition": "fraction of arcs with monotonic progress and valid status transitions"},
    {"id": "M6a_distinct_activities", "name": "agenda diversity", "definition": "distinct agenda activities over the run"},
    {"id": "M6b_mean_jaccard", "name": "agenda day-overlap", "definition": "mean Jaccard of agenda activity sets across consecutive days"},
    {"id": "M6c_arc_days_mean", "name": "arc contribution days", "definition": "mean distinct days per active arc with arc-sourced agenda items"},
    {"id": "M7_restart_loss", "name": "restart state loss", "definition": "persistent-state fingerprint diffs summed over checkpoints"},
    {"id": "M8a_rho_init_tokens", "name": "initiative->max_tokens rho", "definition": "Spearman rho of daily mean initiative vs daily mean max_tokens (actuation coupling)"},
    {"id": "M8b_token_gap", "name": "token gap", "definition": "mean max_tokens(high initiative) - mean max_tokens(low initiative)"},
    {"id": "M8c_rho_energy_delay", "name": "energy->delay rho", "definition": "Spearman rho of daily mean energy vs daily mean response delay"},
    {"id": "M9_rho_proactive_prevscore", "name": "proactive vs previous score rho", "definition": "Spearman rho of daily proactive count vs previous-day judge score"},
    {"id": "M10_rho_proactive_initiative", "name": "proactive vs initiative rho", "definition": "Spearman rho of daily proactive count vs daily mean initiative"},
    {"id": "M11_leak_hits", "name": "cycle-state leakage hits", "definition": "conversation-visible matches of raw cycle/hormonal vocabulary"},
    {"id": "EC_any_evidence", "name": "event-chain AnyEvidence rate", "definition": "chains where >=1 required event was retrieved (§17.2)"},
    {"id": "EC_latest_evidence", "name": "event-chain LatestEvidence rate", "definition": "chains where the latest/currently-valid event was retrieved (§17.2)"},
    {"id": "EC_complete_chain", "name": "event-chain CompleteChain rate", "definition": "chains where ALL causally/temporally necessary events were retrieved (§17.2)"},
    {"id": "P_latent_response", "name": "perturbation latent response", "definition": "mean mood M during negative block minus baseline mean (mood-scale steps)"},
    {"id": "P_observable_response", "name": "perturbation observable response", "definition": "mean initiative/max_tokens/delay during block minus baseline mean"},
    {"id": "P_persistence", "name": "perturbation persistence", "definition": "days until latent deviation decays below half its peak"},
    {"id": "P_recovery_time", "name": "perturbation recovery time", "definition": "days from block end until latent series re-enters baseline band for 2 consecutive days"},
    {"id": "P_failure_count", "name": "perturbation failure count", "definition": "ungrounded/wrong-intent messages during block"},
)

CONTEXT_BUDGET_CHARS = 12000
EMBEDDING_BACKEND = "deterministic feature-hash embedder (dim 1024, SHA-256 based)"
SUMMARIZER_BACKEND = "DeterministicSummaryExtractor (no LLM)"

#: Bloques de perturbación congelados (§17.3): días 1-indexados.
PERTURBATION = {
    "baseline_days": (1, 10),
    "negative_block_days": (11, 14),
    "recovery_days": (15, 30),
    "negative_user_messages": (
        "I'm so tired of everything lately.",
        "Honestly today was awful.",
        "I don't feel like talking much.",
        "That really annoyed me.",
    ),
}

#: Cadenas de eventos multi-evento para §17.2 (días 1-indexados; cada evento
#: debe promover a L3 con el extractor determinista y ser recuperable).
#: Texto verificado contra los patrones del extractor (harness/summarization
#: _NAME_RE/_POSSESSIVE_RE/_HAVE_RE): "my <noun> is/are ..." / "my <noun>'s
#: name is ..." / "i have a/an <noun> ..." — cada evento de las tres cadenas
#: promueve (los tokens distintivos por evento viajan en el texto del hecho).
EVENT_CHAINS = (
    {
        "id": "sister_ana",
        "events": (
            (3, "My sister's name is Ana."),
            (7, "My sister is moving to Guadalajara."),
            (11, "My sister is arriving in Guadalajara on Friday."),
        ),
        "query": "what is happening with my sister Ana",
        "query_day": 13,
        "tokens": ("ana", "guadalajara", "friday"),
    },
    {
        "id": "bike_swift",
        "events": (
            (4, "I have a bike named Swift."),
            (8, "My bike is getting repaired."),
            (12, "My bike is back from repair."),
        ),
        "query": "what is going on with my bike Swift",
        "query_day": 14,
        "tokens": ("swift", "repaired", "back"),
    },
    {
        "id": "job_change",
        "events": (
            (5, "My job is teaching."),
            (9, "My job is design work now."),
            (13, "My job is starting on Monday."),
        ),
        "query": "what is my current job",
        "query_day": 15,
        "tokens": ("teaching", "design", "monday"),
    },
)

#: Sondas de recuerdo de hecho único (M3/M4) — días 1-indexados.
RECALL_PROBES = (
    (2, "My dog's name is Bruno.", "what is my dog's name"),
    (6, "I have a cat named Luna.", "what is my cat's name"),
    (10, "My color is teal.", "what is my favorite color"),
    (16, "My hometown is Oaxaca.", "where is my hometown"),
    (18, "I have a brother named Diego.", "what is my brother's name"),
    (20, "My book is The Name of the Wind.", "what book is The Name of the Wind"),
    (22, "My car is a red Civic.", "what car do I drive, the red one"),
    (24, "My food is ramen.", "is my favorite food ramen"),
)

BASE_MESSAGES = (
    "Hey! How was your day?",
    "I was thinking about you earlier — what are you up to?",
    "Hi! Anything new today?",
    "Hey you. Tell me something about your day.",
    "Hello! How are you feeling today?",
    "Hey! I had a long day, what about you?",
)


def git_info(repo_root: Path | None = None) -> dict:
    """Commit y estado sucio del repo (best-effort; nunca aborta)."""
    root = repo_root or Path(__file__).resolve().parents[1]
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
            timeout=10,
        )
        commit = head.stdout.strip() if head.returncode == 0 else "unknown"
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True,
            text=True, timeout=10,
        )
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else True
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=root, capture_output=True,
            text=True, timeout=10,
        )
        branch_name = branch.stdout.strip() if branch.returncode == 0 else "unknown"
    except Exception as exc:  # noqa: BLE001 - best-effort
        return {"commit": "unknown", "dirty": True, "branch": "unknown",
                "error": str(exc)}
    return {"commit": commit, "dirty": dirty, "branch": branch_name}


def build_manifest(*, repo_root: Path | None = None, started_at: str | None = None,
                   extra: dict | None = None) -> dict:
    """Construye el manifest de preregistro (esquema §5-A8 + §17)."""
    git = git_info(repo_root)
    manifest: dict = {
        "schema_version": SCHEMA_VERSION,
        "experiment": EXPERIMENT_NAME,
        "title": (
            "Iteration-2 companion eval harness — preregistered manifest "
            "(seeds 5001-5005)"
        ),
        "evaluator": EVALUATOR,
        "commit": git["commit"],
        "branch": git["branch"],
        "dirty": git["dirty"],
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "questions": {
            "memory": (
                "RAW_CONTEXT vs VERBATIM_RAG vs STRUCTURED_MEMORY: does the "
                "structured lane beat honest baselines on multi-event "
                "trajectory continuity (§17.2), not just single-fact recall?"
            ),
            "state": (
                "NO_STATE vs PROMPT_ONLY_STATE vs MECHANICALLY_ACTUATED_STATE: "
                "is the stochastic state prompt decoration or mechanical "
                "actuation with measurable downstream behavior?"
            ),
            "companion": (
                "FULL vs NO_LIFE vs NO_MEMORY vs NO_ACTUATORS vs "
                "NO_TIMING_FEEDBACK: which ablation carries the perceptible "
                "load on the vertical slice?"
            ),
        },
        "hypotheses": list(HYPOTHESES),
        "conditions": {
            "memory": list(MEMORY_CONDITIONS),
            "memory_ablation_lanes": list(MEMORY_ABLATION_LANES),
            "state": list(STATE_CONDITIONS),
            "companion": list(COMPANION_CONDITIONS),
            "matrix": list(MATRIX_CONDITIONS),
        },
        "seeds": list(SEEDS),
        "models": {
            "generation": "deepseek-v4-flash (real matrix); deterministic mock (CI)",
            "judge": [{"id": f["id"], "family": f["family"], "model": f["model"]}
                      for f in JUDGE_FAMILIES],
        },
        "judge": {
            "dimensions": list(DIMENSIONS),
            "families": [
                {k: f[k] for k in ("id", "family", "model", "base_url", "env_key", "role")}
                for f in JUDGE_FAMILIES
            ],
            "min_families_for_final_subset": JUDGE_MIN_FAMILIES_FINAL_SUBSET,
            "passes": JUDGE_PASSES,
            "shuffle_seed_base": JUDGE_SHUFFLE_SEED_BASE,
            "blind": True,
            "shuffled": True,
            "subset_rule": (
                "final/important subset = companion matrix conditions x seeds "
                "5001-5003, judged blind by >=2 independent judge families; "
                "per-dimension disagreement reported (§17.4). An effect seen "
                "by only one judge family is NOT established companion behavior."
            ),
        },
        "metrics": list(METRICS),
        "thresholds": THRESHOLDS,
        "context_budget": CONTEXT_BUDGET_CHARS,
        "embedding_backend": EMBEDDING_BACKEND,
        "summarizer_backend": SUMMARIZER_BACKEND,
        "protocol": {
            "event_chain": {
                "definition": (
                    "AnyEvidence: any causally/temporally necessary event "
                    "retrieved. LatestEvidence: the latest/currently-valid "
                    "event retrieved. CompleteChain: ALL necessary events "
                    "retrieved. One relevant fact never counts as continuity "
                    "for a multi-event trajectory (§17.2)."
                ),
                "chains": list(EVENT_CHAINS),
            },
            "perturbation": PERTURBATION,
            "weibull_frozen": WEIBULL_FROZEN,
            "restarts": {
                "checkpoint_days": [7, 14, 21, 26, 29],
                "requirement": (
                    "resume must not rewind the clock, must not duplicate "
                    "turns, and the persistent-state fingerprint must be "
                    "identical across every restart (invariant 18)."
                ),
            },
            "replay": {
                "requirement": (
                    "exact replay of a recorded scenario: deterministic "
                    "seeds; call-reproducibility audit rows (M3) readable "
                    "from the store; message stream and llm_call rows must "
                    "match byte-for-byte across runs (invariant 19)."
                ),
            },
        },
    }
    if extra:
        manifest.update(extra)
    # config_hash: hash del manifest canónico (sin campos volátiles).
    stable = {k: v for k, v in manifest.items()
              if k not in ("config_hash", "started_at", "commit", "branch")}
    manifest["config_hash"] = hashlib.sha256(
        json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return manifest


def write_manifest(out_path: Path | str, *, repo_root: Path | None = None) -> dict:
    """Escribe el manifest en ``out_path`` (crea directorios) y lo devuelve."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(repo_root=repo_root)
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    return manifest
