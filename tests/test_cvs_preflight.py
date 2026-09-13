"""Tests del pre-flight de ablación: matriz completa × semillas en el cliente
fake, veredictos por AblationClaim contra FULL y resumen por condición."""

import pytest

from harness.domain import AblationClaim

from experiments.cvs_common import records_summary, run_cell
from experiments.cvs_manifest import MATRIX_CONDITIONS
from experiments.cvs_preflight import (
    CLAIMS,
    GATE_MIN_DIVERGENCE,
    _aggregate,
    evaluate_claims,
    run_preflight,
)
from harness.store import SQLiteStore

ALLOWED_CHANNELS = {"timing", "memory_store", "generation_controls", "life_state"}


def _seed_conversation_tables(store: SQLiteStore) -> None:
    """Crea las tablas conversations/conversation_turns para ejercitar el resumen."""
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

        msgs = store.conn.execute("SELECT * FROM messages").fetchall()
        n_assistant = sum(1 for m in msgs if m["role"] == "assistant")
        n_pro = sum(1 for m in msgs if m["proactive"])
        assert summary["n_assistant_turns"] == n_assistant
        assert summary["n_proactive"] == n_pro
        assert summary["n_reactive"] == n_assistant - n_pro
        assert summary["n_assistant_turns"] == (
            summary["n_reactive"] + summary["n_proactive"]
        )

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
        # The cell plus the seeded conversation are counted by the seam.
        assert summary["n_conversations"] >= 1
        assert summary["mean_turns_per_conversation"] is not None
        store.close()


class TestControlsStats:
    """Estadísticas de controles de generación por célula/condición."""

    RECORDED_CONTROLS = (
        "max_tokens", "response_delay_s", "closing_tendency",
        "initiative_factor", "closing_guidance",
    )

    def test_full_cell_controls_stats_varied(self, tmp_path):
        """FULL registra controls_stats con los 5 controles, numéricos y variados."""
        out = tmp_path / "cell"
        records = run_cell("FULL", 5001, out, days=2, fake=True, perturb=True)
        store = SQLiteStore(records["db"])
        summary = records_summary(store, records)
        store.close()
        cs = summary["controls_stats"]
        for key in self.RECORDED_CONTROLS:
            assert key in cs, f"missing control key {key}"
        mt = cs["max_tokens"]
        assert mt["n"] >= 1
        assert mt["min"] is not None
        assert mt["max"] is not None
        assert mt["mean"] is not None
        assert mt["min"] <= mt["mean"] <= mt["max"]
        assert mt["varied"] is True
        # closing_guidance is textual: no min/max/mean, varied is reported.
        assert cs["closing_guidance"]["min"] is None
        assert isinstance(cs["closing_guidance"]["varied"], bool)

    def test_no_actuators_cell_controls_not_varied(self, tmp_path):
        """NO_ACTUATORS deja todos los controles en varied=False, colapsados a un punto."""
        out = tmp_path / "cell"
        records = run_cell("NO_ACTUATORS", 5001, out, days=2,
                           fake=True, perturb=True)
        store = SQLiteStore(records["db"])
        summary = records_summary(store, records)
        store.close()
        cs = summary["controls_stats"]
        assert cs, "NO_ACTUATORS cell must record per-message controls"
        for name, st in cs.items():
            assert name in self.RECORDED_CONTROLS
            assert st["n"] >= 1
            assert st["varied"] is False, (
                f"{name} must be pinned (actuator controls do not vary)"
            )
            if st["min"] is not None:
                assert st["min"] == st["max"] == st["mean"]
        # Message lane is intact: real messages with varying lengths.
        assert summary["n_messages"] > 0
        assert summary["std_reply_len"] > 0

    def test_preflight_report_includes_controls_stats(self, tmp_path):
        """El reporte JSON lleva controls_stats por condición y en FULL."""
        report = run_preflight(
            days=3, seeds=(5001,),
            conditions=("FULL", "NO_ACTUATORS"),
            out_dir=tmp_path,
        )
        for cond in ("FULL", "NO_ACTUATORS"):
            agg = report["per_condition"][cond]
            assert "controls_stats" in agg
            assert "max_tokens" in agg["controls_stats"]
            assert agg["controls_stats"]["max_tokens"]["n"] >= 1
        assert "max_tokens" in report["full"]["controls_stats"]
        assert (
            report["per_condition"]["FULL"]["controls_stats"]
            ["max_tokens"]["varied"] is True
        )
        assert (
            report["per_condition"]["NO_ACTUATORS"]["controls_stats"]
            ["max_tokens"]["varied"] is False
        )
        # The report is JSON-serializable.
        import json
        json.dumps(report)


class TestPreflightGate:
    """La compuerta (Gate G2): veredictos por condición contra FULL."""

    @pytest.mark.slow
    def test_preflight_flags_null_ablations_on_current_code(self, tmp_path):
        """Sobre el código actual y en el horizonte confirmatorio (30 días) el
        pre-flight no marca ninguna ablación nula."""

        report = run_preflight(
            days=30, seeds=(5001, 5002),
            conditions=(
                "FULL", "NO_LIFE", "NO_ACTUATORS", "NO_TIMING_FEEDBACK",
                "STRUCTURED_NO_STATE", "RAW_HISTORY", "SIMPLE_RAG",
            ),
            out_dir=tmp_path,
        )
        assert report["null_ablations"] == []
        assert report["claim_errors"] == []
        assert report["ok"]
        for cond in report["conditions"]:
            assert cond in report["per_condition"]

    def test_below_horizon_claims_are_not_evaluable_not_fail(self, tmp_path):
        """Bajo el horizonte (3 días) las claims con min_days mayor se reportan
        NOT EVALUABLE, nunca FAIL."""
        report = run_preflight(
            days=3, seeds=(5001,),
            conditions=("FULL", "NO_TIMING_FEEDBACK", "STRUCTURED_NO_STATE",
                        "SIMPLE_RAG"),
            out_dir=tmp_path,
        )
        assert report["null_ablations"] == []
        ne = {v["condition"] for v in report["not_evaluable"]}
        assert {"STRUCTURED_NO_STATE", "NO_TIMING_FEEDBACK", "SIMPLE_RAG"} <= ne
        for v in report["verdicts"]:
            if v.get("status") == "not_evaluable":
                assert v["passed"] is None
                assert "min_days" in v["reason"]

    @pytest.mark.slow
    def test_preflight_positive_control_detectable_across_frozen_seeds(
        self, tmp_path
    ):
        """El control positivo se detecta y registra qué pata disparó con su
        margen medido; se salta si la referencia no es reproducible."""
        report = run_preflight(
            days=30, seeds=(5001, 5002),
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
        # The fired leg is recorded with its measured margin.
        m = verdicts[0]["measured"]
        assert m["fired_div"] >= GATE_MIN_DIVERGENCE
        assert m["count_div"] is not None
        assert "gap_div" in m and "times_identical" in m

    @pytest.mark.slow
    def test_positive_control_high_margin_seed5005(self, tmp_path):
        """En la semilla 5005 el control positivo tiene margen amplio en la pata
        de disparos; se salta si la referencia no es reproducible."""
        report = run_preflight(
            days=30, seeds=(5005,),
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
        assert verdicts and verdicts[0]["passed"]
        m = verdicts[0]["measured"]
        assert m["fired_div"] is not None and m["fired_div"] >= GATE_MIN_DIVERGENCE
        assert (
            report["per_condition"]["NO_TIMING_FEEDBACK"]["n_fired_schedule"]
            > report["full"]["n_fired_schedule"]
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
            "not_evaluable",
        }
        for v in report["verdicts"]:
            assert {"condition", "channel", "assertion", "passed"} <= set(v)
            assert v["channel"] in ALLOWED_CHANNELS
        assert report["per_condition"]["NO_LIFE"]["days"] == 3
        assert report["full"]["condition"] == "FULL"


class TestNoLifeGoldfish:
    """NO_LIFE: ablación goldfish — cada día re-siembra arcos con ids nuevos;
    FULL conserva los ids entre días."""

    def test_no_life_arcs_fresh_each_day_disjoint(self, tmp_path):
        """2 días NO_LIFE: cada día tiene arcos (>0) y los conjuntos de ids
        de días consecutivos son DISJUNTOS — nada cruza la medianoche."""
        out = tmp_path / "cell"
        records = run_cell("NO_LIFE", 5001, out, days=2, fake=True, perturb=True)
        by_day = records["arc_progress_by_day"]
        assert len(by_day) >= 2, "two-day cell must snapshot arcs both days"
        day_ids = {int(d): set(by_day[d]) for d in by_day}
        days = sorted(day_ids)
        for d in days:
            assert day_ids[d], f"day {d} must have life arcs (grounding intact)"
        for a, b in zip(days, days[1:]):
            shared = day_ids[a] & day_ids[b]
            assert not shared, (
                f"NO_LIFE arc ids must not survive midnight: day {a} and "
                f"{b} share {shared}"
            )

    def test_full_arcs_persist_across_days(self, tmp_path):
        """FULL: algún id de arco aparece en >= 2 días."""
        out = tmp_path / "cell"
        records = run_cell("FULL", 5001, out, days=2, fake=True, perturb=True)
        seen: set[str] = set()
        overlap = False
        for arcs in records["arc_progress_by_day"].values():
            for aid in arcs:
                if aid in seen:
                    overlap = True
                seen.add(aid)
        assert overlap, "FULL must persist arc ids across days"

    def test_no_life_claim_discriminates(self, tmp_path):
        """La claim discrimina: pasa con NO_LIFE goldfish y falla con FULL."""
        out = tmp_path / "cells"
        cells: dict[str, dict] = {}
        for cond in ("FULL", "NO_LIFE"):
            records = run_cell(cond, 5001, out / cond, days=2,
                               fake=True, perturb=True)
            store = SQLiteStore(records["db"])
            cells[cond] = _aggregate([records_summary(store, records)])
            store.close()
        no_life, full = cells["NO_LIFE"], cells["FULL"]

        verdicts = evaluate_claims("NO_LIFE", no_life, full, CLAIMS, days=3)
        assert verdicts and all(v["passed"] for v in verdicts), verdicts

        verdicts_full = evaluate_claims("NO_LIFE", full, full, CLAIMS, days=3)
        assert verdicts_full and not any(v["passed"] for v in verdicts_full)


class TestClaimsRegistry:
    """Registro de claims: lista plana, contrato congelado, aditivo."""

    def test_registry_is_plain_list_of_ablation_claims(self):
        """Toda entrada es AblationClaim y cada condición no-FULL declara una claim."""
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
        """El mecanismo acepta claims añadidas (append a la lista plana) y las evalúa."""
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
        # The failing appended claim blocks the matrix.
        assert "FULL" in report["null_ablations"]
        assert not report["ok"]
        # Existing claims are still evaluated (NO_LIFE passes).
        assert "NO_LIFE" not in report["null_ablations"]

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
        verdicts = evaluate_claims("NO_LIFE", cell, full, CLAIMS, days=3)
        assert verdicts and all(v["condition"] == "NO_LIFE" for v in verdicts)
        assert not any(v["passed"] for v in verdicts)
        verdicts_full = evaluate_claims("FULL", cell, full, CLAIMS, days=3)
        assert verdicts_full == []


class TestDeterminismCheck:
    """El run de referencia debe ser reproducible: el pre-flight corre FULL dos
    veces y bloquea si divergen."""

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
        assert not any("memory_lane" in d for d in diffs)  # identity excluded

    def test_preflight_reports_deterministic_on_idle_run(self, tmp_path):
        """En un run sin carga el chequeo de determinismo pasa."""
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
        # With only FULL+NO_LIFE in the run set, no claim fails.
        assert report["ok"] is True


class TestPreregisteredClaimsG2:
    """Cada claim pasa para la célula de su condición y falla contra FULL
    (sin tautología)."""

    @staticmethod
    def _flat_controls_stats() -> dict:
        """NO_ACTUATORS: set plano pinnado 600 / 5.0 / 0.5 / banda media."""
        return {
            "max_tokens": {"n": 30, "min": 600.0, "max": 600.0, "mean": 600.0,
                           "varied": False},
            "response_delay_s": {"n": 30, "min": 5.0, "max": 5.0, "mean": 5.0,
                                 "varied": False},
            "closing_tendency": {"n": 30, "min": 0.5, "max": 0.5, "mean": 0.5,
                                 "varied": False},
            "initiative_factor": {"n": 30, "min": 1.0, "max": 1.0, "mean": 1.0,
                                  "varied": False},
            "closing_guidance": {"n": 30, "min": None, "max": None, "mean": None,
                                 "varied": False},
        }

    @staticmethod
    def _full_controls_stats() -> dict:
        """FULL: controles no degenerados (delay max 27.0 s = 3.0x el delay plano)."""
        return {
            "max_tokens": {"n": 30, "min": 380.0, "max": 625.0, "mean": 500.0,
                           "varied": True},
            "response_delay_s": {"n": 30, "min": 8.5, "max": 27.0, "mean": 15.0,
                                 "varied": True},
            "closing_tendency": {"n": 30, "min": 0.25, "max": 0.8, "mean": 0.5,
                                 "varied": True},
            "initiative_factor": {"n": 30, "min": 0.7, "max": 1.3, "mean": 1.0,
                                  "varied": True},
            "closing_guidance": {"n": 30, "min": None, "max": None, "mean": None,
                                 "varied": True},
        }

    @staticmethod
    def _memory_evidence(ids: list[str], *, lane: str = "episode_retrieval",
                         ctx: int = 0) -> dict:
        return {
            "probe_lane": lane,
            "n_retrieved": len(ids),
            "retrieved_ids": sorted(ids),
            "context_turns": ctx,
            "AnyEvidence": 0.5,
            "M3_recall": 0.25,
        }

    def _summary(self, *, controls: dict | None = None,
                 evidence: dict | None = None, n_proactive: int = 5,
                 proactive_times: list[float] | None = None) -> dict:
        base = {
            "n_proactive": n_proactive,
            "n_fired_schedule": 5,
            "mean_reply_len": 30.0,
            "n_life_arcs": 2,
            "n_agenda_items": 14,
            "memory_lane": "x",
            "controls_stats": controls or self._full_controls_stats(),
            "memory_evidence": evidence or self._memory_evidence(["ep-a"]),
            "proactive_times": proactive_times or [10.0, 20.0, 30.0, 40.0],
        }
        return base

    def _verdict(self, condition: str, cell: dict, full: dict) -> list[dict]:
        return [v for v in evaluate_claims(condition, cell, full, CLAIMS, days=30)
                if v["condition"] == condition]

    def test_b4_no_actuators_claim_discriminates(self):
        """La célula plana pasa contra FULL no degenerado; FULL contra sí misma falla."""
        cell = self._summary(controls=self._flat_controls_stats())
        full = self._summary(controls=self._full_controls_stats())
        verdicts = self._verdict("NO_ACTUATORS", cell, full)
        assert verdicts and all(v["passed"] for v in verdicts)

        verdicts_full = self._verdict("NO_ACTUATORS", full, full)
        assert verdicts_full and all(not v["passed"] for v in verdicts_full)
        # FULL with delay max under 3.0x the flat delay does not pass.
        full_weak = self._summary(controls={
            **self._full_controls_stats(),
            "response_delay_s": {"n": 30, "min": 4.0, "max": 12.0,
                                 "mean": 8.0, "varied": True},
        })
        verdicts_weak = self._verdict("NO_ACTUATORS", cell, full_weak)
        assert verdicts_weak and all(not v["passed"] for v in verdicts_weak)

    def test_b5_structured_no_state_claim_discriminates(self):
        """STRUCTURED_NO_STATE: divergencia de conteo >= 15% y de gaps >= 10%
        (con >= 4 horas por lado); sin divergencia la claim falla."""
        from harness.scheduler import structured_no_state_claim

        claim = structured_no_state_claim()
        cell = {"n_proactive": 12, "proactive_times": [10.0, 30.0, 50.0, 70.0]}
        full = {"n_proactive": 8, "proactive_times": [10.0, 20.0, 30.0, 40.0]}
        assert claim.check(cell, full)

        same = {"n_proactive": 8, "proactive_times": [10.0, 20.0, 30.0, 40.0]}
        assert not claim.check(same, full)
        # Counts diverge >= 15% but mean gaps differ < 10%: claim fails.
        close_gaps = {"n_proactive": 8, "proactive_times": [10.0, 22.0, 34.0, 46.0]}
        gap_full = {"n_proactive": 6, "proactive_times": [10.0, 21.0, 32.0, 43.0]}
        assert not claim.check(close_gaps, gap_full)
        # Few hours: only the count leg applies.
        sparse = {"n_proactive": 6, "proactive_times": [10.0, 30.0, 50.0]}
        sparse_full = {"n_proactive": 4, "proactive_times": [10.0, 20.0, 30.0]}
        assert claim.check(sparse, sparse_full)

    def test_simple_rag_claim_fails_on_degenerate_lane(self):
        """Lane que no recupera nada (n_retrieved=0): la claim conductual falla."""
        full = self._summary(evidence=self._memory_evidence(["ep-a", "ep-b"]))
        dead = self._summary(evidence=self._memory_evidence([]))
        verdicts = self._verdict("SIMPLE_RAG", dead, full)
        assert verdicts and all(not v["passed"] for v in verdicts)

    def test_simple_rag_claim_passes_on_real_retrieval(self):
        """SIMPLE_RAG con recuperación real y conjunto distinto de FULL: pasa."""
        cell = self._summary(evidence=self._memory_evidence(["ep-x", "ep-y"]))
        full = self._summary(evidence=self._memory_evidence(["ep-a", "ep-b"]))
        verdicts = self._verdict("SIMPLE_RAG", cell, full)
        assert verdicts and all(v["passed"] for v in verdicts)
        # Identical set to FULL: the claim fails (set comparison).
        same = self._summary(evidence=self._memory_evidence(["ep-a", "ep-b"]))
        verdicts_same = self._verdict("SIMPLE_RAG", same, full)
        assert verdicts_same and all(not v["passed"] for v in verdicts_same)

        verdicts_full = self._verdict("SIMPLE_RAG", full, full)
        assert verdicts_full and all(not v["passed"] for v in verdicts_full)

    def test_raw_history_claim_discriminates(self):
        """RAW_HISTORY: ventana cruda no nula y sin ids de episodio: pasa."""
        cell = self._summary(
            evidence=self._memory_evidence([], lane="raw_history", ctx=36)
        )
        full = self._summary(evidence=self._memory_evidence(["ep-a", "ep-b"]))
        verdicts = self._verdict("RAW_HISTORY", cell, full)
        assert verdicts and all(v["passed"] for v in verdicts)

        dead = self._summary(
            evidence=self._memory_evidence([], lane="raw_history", ctx=0)
        )
        verdicts_dead = self._verdict("RAW_HISTORY", dead, full)
        assert verdicts_dead and all(not v["passed"] for v in verdicts_dead)

        verdicts_full = self._verdict("RAW_HISTORY", full, full)
        assert verdicts_full and all(not v["passed"] for v in verdicts_full)

    def test_records_summary_wires_behavioral_legs_to_real_store(self, tmp_path):
        """Las piernas conductuales salen del store real: proactive_times y
        memory_evidence alinean con mensajes y recuperación."""
        out = tmp_path / "cell"
        records = run_cell("FULL", 5001, out, days=2, fake=True, perturb=True)
        store = SQLiteStore(records["db"])
        summary = records_summary(store, records)
        msgs = store.conn.execute("SELECT * FROM messages").fetchall()
        n_pro = sum(1 for m in msgs if m["proactive"])
        assert len(summary["proactive_times"]) == n_pro
        assert summary["proactive_times"] == sorted(summary["proactive_times"])
        ev = summary["memory_evidence"]
        assert ev["probe_lane"] == "episode_retrieval"
        assert ev["n_retrieved"] >= 1
        assert ev["retrieved_ids"]
        store.close()
