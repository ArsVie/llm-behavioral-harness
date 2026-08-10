"""Tests del pre-flight de ablación (Iteración 3, B8 / Gate G2).

Verifica el mecanismo de la compuerta barata: matriz completa × semillas en
el cliente fake, veredictos por AblationClaim contra FULL, registro aditivo y
el resumen por condición (hook records_summary). El veredicto es función del
código actual — estos tests fijan el comportamiento de HOY (código actual:
NO_LIFE/STRUCTURED_NO_STATE/NO_ACTUATORS son ablaciones nulas, F4) y se
actualizan cuando B2/B4/B5 arreglan las condiciones (el orquestador mantiene
el registro en G2).

Nota sobre determinismo: el runner de células del harness entrega los feeds
del usuario con polling de reloj real (``_run_segment``, cvs_common) y bajo
contention del event loop puede omitir feeds — el pre-flight corre FULL dos
veces y bloquea si la referencia no es reproducible (chequeo de determinismo).
Los tests del control positivo se saltan (skip) cuando la referencia del
propio run no es reproducible: una compuerta sin referencia reproducible ya
bloquea sola; el test solo fija que, con referencia reproducible, el control
positivo se detecta.

Convención del repo: docstrings en español, identificadores en inglés.
"""

import pytest

from harness.domain import AblationClaim

from experiments.cvs_common import records_summary, run_cell
from experiments.cvs_manifest import MATRIX_CONDITIONS, SEEDS
from experiments.cvs_preflight import CLAIMS, evaluate_claims, run_preflight
from harness.store import SQLiteStore

ALLOWED_CHANNELS = {"timing", "memory_store", "generation_controls", "life_state"}


def _seed_conversation_tables(store: SQLiteStore) -> None:
    """Crea el seam de conversaciones de B2 (tablas conversations +
    conversation_turns) para ejercitar el resumen con conversaciones."""
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS conversations ("
        " id TEXT PRIMARY KEY, opened_t_h REAL, closed_t_h REAL,"
        " opened_by TEXT, close_reason TEXT)"
    )
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_turns ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT,"
        " speaker TEXT, text TEXT, t_h REAL, turn_index INTEGER)"
    )
    store.conn.execute(
        "INSERT INTO conversations (id, opened_t_h, opened_by) "
        "VALUES ('c1', 0.0, 'user')"
    )
    store.conn.execute(
        "INSERT INTO conversation_turns "
        "(conversation_id, speaker, text, t_h, turn_index) "
        "VALUES ('c1', 'user', 'hello', 0.0, 0), "
        "       ('c1', 'companion', 'hi!', 0.1, 1), "
        "       ('c1', 'user', 'how are you?', 0.2, 2), "
        "       ('c1', 'companion', 'great!', 0.3, 3)"
    )
    store.conn.commit()


class TestRecordsSummary:
    """Hook de resumen por condición (contrato AblationClaim)."""

    def test_records_summary_contract_keys(self, tmp_path):
        """El resumen lleva las claves del contrato AblationClaim + las de
        canal; las cuentas cuadran con el store real."""
        out = tmp_path / "cell"
        records = run_cell("FULL", 5001, out, days=2, fake=True, perturb=True)
        store = SQLiteStore(records["db"])
        summary = records_summary(store, records)
        for key in ("n_proactive", "n_reactive", "n_assistant_turns",
                    "n_blank_assistant_turns", "n_conversations",
                    "mean_turns_per_conversation"):
            assert key in summary, f"missing contract key {key}"
        # Cuentas verificadas contra el store (no contra el records dict).
        msgs = store.conn.execute("SELECT * FROM messages").fetchall()
        n_assistant = sum(1 for m in msgs if m["role"] == "assistant")
        n_pro = sum(1 for m in msgs if m["proactive"])
        assert summary["n_assistant_turns"] == n_assistant
        assert summary["n_proactive"] == n_pro
        assert summary["n_reactive"] == n_assistant - n_pro
        assert summary["n_assistant_turns"] == (
            summary["n_reactive"] + summary["n_proactive"]
        )
        # B2 ha aterrizado: la v4 crea las tablas siempre — disponible, no
        # silencioso, con ceros contables en lugar de None.
        assert summary["conversations_available"] is True
        assert summary["n_conversations"] is not None
        assert summary["mean_turns_per_conversation"] is not None
        assert summary["memory_lane"] == "structured_memory"
        store.close()

    def test_records_summary_conversations_available_when_seam_present(
        self, tmp_path
    ):
        """Con el seam de B2 presente, el resumen cuenta conversaciones y
        turnos medios por conversación."""
        out = tmp_path / "cell"
        records = run_cell("FULL", 5001, out, days=2, fake=True, perturb=True)
        store = SQLiteStore(records["db"])
        _seed_conversation_tables(store)
        summary = records_summary(store, records)
        assert summary["conversations_available"] is True
        # La célula (con el feed de it3) ya forma conversaciones reales, más
        # la sembrada: lo que se verifica es que el seam CUENTA, no un total.
        assert summary["n_conversations"] >= 1
        assert summary["mean_turns_per_conversation"] is not None
        store.close()


class TestPreflightGate:
    """La compuerta (Gate G2): veredictos por condición contra FULL."""

    def test_preflight_flags_null_ablations_on_current_code(self, tmp_path):
        """Aceptación B8 #2: sobre el código ACTUAL el pre-flight marca
        NO_LIFE, STRUCTURED_NO_STATE y NO_ACTUATORS como ablaciones nulas
        (F4); RAW_HISTORY/SIMPLE_RAG NO se marcan (su lane de memoria
        difiere por diseño). El veredicto es función del código, no una
        expectativa hardcodeada."""
        report = run_preflight(days=3, seeds=(5001,), out_dir=tmp_path)
        assert not report["ok"]
        flagged = set(report["null_ablations"])
        assert {"NO_LIFE", "STRUCTURED_NO_STATE", "NO_ACTUATORS"} <= flagged
        assert "RAW_HISTORY" not in flagged
        assert "SIMPLE_RAG" not in flagged
        for cond in MATRIX_CONDITIONS:
            assert cond in report["per_condition"]

    def test_preflight_positive_control_detectable_across_frozen_seeds(
        self, tmp_path
    ):
        """NO_TIMING_FEEDBACK (control positivo, la única ablación con efecto
        probado) se detecta agregando las 5 semillas congeladas: los disparos
        de agenda difieren de FULL >= 15% (12 vs 10 en el código actual).
        Si el pipeline de timing se rompe, esta claim lo delata. Se salta
        cuando la referencia FULL del propio run no es reproducible (carrera
        conocida de ``_run_segment`` bajo carga — la compuerta ya bloquea
        por sí sola en ese caso)."""
        report = run_preflight(
            days=3, seeds=SEEDS,
            conditions=("FULL", "NO_TIMING_FEEDBACK"),
            out_dir=tmp_path,
        )
        if not report["deterministic"]:
            pytest.skip(
                "reference FULL run not reproducible under load "
                "(known _run_segment feed race; gate already blocks)"
            )
        verdicts = [
            v for v in report["verdicts"]
            if v["condition"] == "NO_TIMING_FEEDBACK"
        ]
        assert verdicts and all(v["passed"] for v in verdicts)
        assert "NO_TIMING_FEEDBACK" not in report["null_ablations"]
        assert (
            report["per_condition"]["NO_TIMING_FEEDBACK"]["n_fired_schedule"]
            > report["full"]["n_fired_schedule"]
        )

    def test_positive_control_high_margin_seed5005(self, tmp_path):
        """El control positivo en la semilla 5005 tiene margen amplio
        (5 vs 3 disparos = 67%) — el canal de timing responde incluso en
        3 días. Se salta si la referencia no es reproducible."""
        report = run_preflight(
            days=3, seeds=(5005,),
            conditions=("FULL", "NO_TIMING_FEEDBACK"),
            out_dir=tmp_path,
        )
        if not report["deterministic"]:
            pytest.skip(
                "reference FULL run not reproducible under load "
                "(known _run_segment feed race; gate already blocks)"
            )
        assert (
            report["per_condition"]["NO_TIMING_FEEDBACK"]["n_fired_schedule"]
            >= report["full"]["n_fired_schedule"] + 2
        )

    def test_preflight_report_shape_and_verdict_fields(self, tmp_path):
        """Forma del reporte: resúmenes por condición + veredictos con
        condition/channel/assertion/passed."""
        report = run_preflight(
            days=3, seeds=(5001,),
            conditions=("FULL", "NO_LIFE"),
            out_dir=tmp_path,
        )
        assert set(report) >= {
            "ok", "days", "seeds", "conditions", "full",
            "per_condition", "verdicts", "null_ablations",
        }
        for v in report["verdicts"]:
            assert {"condition", "channel", "assertion", "passed"} <= set(v)
            assert v["channel"] in ALLOWED_CHANNELS
        assert report["per_condition"]["NO_LIFE"]["days"] == 3
        assert report["full"]["condition"] == "FULL"


class TestClaimsRegistry:
    """Registro de claims: lista plana, contrato congelado, aditivo."""

    def test_registry_is_plain_list_of_ablation_claims(self):
        """Toda entrada es AblationClaim (contrato congelado, invariante 9);
        cada condición no-FULL de la matriz declara al menos una claim."""
        assert isinstance(CLAIMS, list)
        assert CLAIMS, "registry must be seeded"
        assert all(isinstance(c, AblationClaim) for c in CLAIMS)
        for c in CLAIMS:
            assert c.channel in ALLOWED_CHANNELS
            assert c.condition != "FULL"
            assert c.assertion.strip()
        declared = {c.condition for c in CLAIMS}
        for cond in MATRIX_CONDITIONS:
            if cond != "FULL":
                assert cond in declared, (
                    f"condition {cond} has no AblationClaim (invariant 9)"
                )

    def test_registry_accepts_additive_claims(self, tmp_path):
        """Aceptación B8 #3: el mecanismo acepta claims añadidas (append a la
        lista plana, sin reestructurar) y las evalúa."""
        extra_pass = AblationClaim(
            condition="FULL",
            channel="generation_controls",
            assertion="appended claim: FULL produces at least one assistant turn",
            check=lambda cell, full: cell["n_assistant_turns"] >= 1,
        )
        extra_fail = AblationClaim(
            condition="FULL",
            channel="timing",
            assertion="appended claim: FULL produces > 1e9 proactive messages",
            check=lambda cell, full: cell["n_proactive"] > 1e9,
        )
        claims = [*CLAIMS, extra_pass, extra_fail]
        report = run_preflight(
            days=3, seeds=(5001,),
            conditions=("FULL", "NO_LIFE"),
            claims=claims,
            out_dir=tmp_path,
        )
        by_assertion = {v["assertion"]: v for v in report["verdicts"]}
        assert by_assertion[extra_pass.assertion]["passed"] is True
        assert by_assertion[extra_fail.assertion]["passed"] is False
        # La claim añadida que falla bloquea la matriz (mecanismo intacto).
        assert "FULL" in report["null_ablations"]
        assert not report["ok"]
        # Las claims existentes siguen evaluándose (NO_LIFE sigue nula).
        assert "NO_LIFE" in report["null_ablations"]

    def test_claim_check_error_is_loud(self, tmp_path):
        """Una claim cuyo check lanza se reporta como error y bloquea."""
        bad = AblationClaim(
            condition="NO_LIFE",
            channel="life_state",
            assertion="broken claim",
            check=lambda cell, full: (_ for _ in ()).throw(KeyError("boom")),
        )
        report = run_preflight(
            days=3, seeds=(5001,),
            conditions=("FULL", "NO_LIFE"),
            claims=[*CLAIMS, bad],
            out_dir=tmp_path,
        )
        assert any("error" in v for v in report["verdicts"])
        assert not report["ok"]

    def test_evaluate_claims_filters_by_condition(self):
        """evaluate_claims solo evalúa las claims de la condición pedida."""
        cell = {"n_proactive": 1, "n_fired_schedule": 1,
                "mean_reply_len": 30.0, "n_life_arcs": 2,
                "n_agenda_items": 14, "memory_lane": "structured_memory"}
        full = dict(cell)
        verdicts = evaluate_claims("NO_LIFE", cell, full, CLAIMS)
        assert verdicts and all(v["condition"] == "NO_LIFE" for v in verdicts)
        assert not any(v["passed"] for v in verdicts)
        verdicts_full = evaluate_claims("FULL", cell, full, CLAIMS)
        assert verdicts_full == []


class TestDeterminismCheck:
    """Chequeo de determinismo: el run de referencia debe ser reproducible.

    El runner de células del harness entrega feeds con polling de reloj real
    (TIME_SCALE_S_PER_VH=0.0004) y bajo carga puede omitir feeds — el
    pre-flight corre FULL dos veces y bloquea si divergen (una compuerta con
    referencia no reproducible no es una compuerta).
    """

    def test_summary_diff_detects_divergence(self):
        """_summary_diff compara las claves numéricas y reporta diferencias."""
        from experiments.cvs_preflight import _summary_diff

        a = {"n_proactive": 7, "n_fired_schedule": 10, "n_messages": 77,
             "mean_reply_len": 32.6, "memory_lane": "structured_memory",
             "per_seed": {"5001": {"n_proactive": 1}}}
        b = dict(a)
        assert _summary_diff(a, b) == []
        b["n_fired_schedule"] = 12
        b["mean_reply_len"] = 31.0
        diffs = _summary_diff(a, b)
        assert any("n_fired_schedule" in d for d in diffs)
        assert any("mean_reply_len" in d for d in diffs)
        assert not any("memory_lane" in d for d in diffs)  # identidad: excluida

    def test_preflight_reports_deterministic_on_idle_run(self, tmp_path):
        """En un run sin carga, el chequeo de determinismo pasa y el reporte
        lo declara (deterministic=True)."""
        report = run_preflight(
            days=3, seeds=(5001,),
            conditions=("FULL", "NO_LIFE"),
            out_dir=tmp_path,
        )
        assert report["deterministic"] is True
        assert report["determinism_failures"] == []

    def test_preflight_blocks_when_determinism_check_disabled_ok(self, tmp_path):
        """Sin el chequeo, ok depende solo de las claims (mecanismo intacto)."""
        report = run_preflight(
            days=3, seeds=(5001,),
            conditions=("FULL", "NO_LIFE"),
            out_dir=tmp_path,
            determinism_check=False,
        )
        assert "deterministic" in report
        assert report["ok"] is False  # NO_LIFE sigue siendo ablación nula
