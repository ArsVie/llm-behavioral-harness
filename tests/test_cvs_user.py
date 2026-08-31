"""Tests del simulador de usuario conversacional (it3 B3).

Cubre el FEED CONTRACT de ``cvs_user.build_user_stream`` (el handshake con
el driver de B8) y las cuatro aceptaciones mecánicas de B3: media de turnos
por conversación >= 4 con el cliente fake, turnos byte-idénticos entre
condiciones para un mismo seed, sondas/cadenas en sus días preregistrados,
y las invariantes de la proyección legacy ``user_script``.

El harness ``_run_conversational_cell`` reproduce la forma de la célula
vertical existente (bootstrap limpio + sesión de la condición) y conduce la
sesión DIRECTAMENTE consumiendo el stream del contrato: los ``at_t_h`` se
entregan cuando el reloj virtual los alcanza; los ``after_reply`` un retardo
sembrado después de la réplica anterior. El brief autoriza explícitamente
esta vía mientras ``run_cell`` no sea conversacional (B8): con el runtime
real, el rollover del reloj (time_scale congelado 0.0004) desplaza los feeds
tardíos al día siguiente y la entrega depende de carreras de hilos — la
conducción directa de la sesión es determinista por construcción.
"""

from __future__ import annotations

from engine.rng import EXPERIMENT_STREAM, stream_rng
from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.bootstrap import ensure_companion_initialized
from harness.clock import VirtualClock
from harness.domain import UserProfile
from harness.store import SQLiteStore

from experiments.cvs_common import (
    GATE2_USER_INTERESTS,
    DeterministicClient,
    DeterministicJudge,
    apply_condition_patches,
    make_session,
    restore_patches,
    user_script,
)
from experiments.cvs_manifest import (
    EVENT_CHAINS,
    PERTURBATION,
    RECALL_PROBES,
)
from experiments.cvs_user import (
    USER_SIM_STREAM_KEY,
    _ABANDON_POOL,
    _AMBIGUOUS_POOL,
    _FOLLOWUP_QUESTION_POOL,
    _PUSHBACK_POOL,
    _REPAIR_POOL,
    _RUPTURE_POOL,
    build_user_stream,
    draw_after_reply_delay,
    event_days,
    user_turn_texts,
)

DAYS = 7
SEED = 5001


# Harness: 7-day conversational cell consuming the feed stream


def _run_conversational_cell(tmp_path, seed: int, condition: str,
                             days: int = DAYS):
    """Célula fake de ``days`` días por el camino integrado (forma de run_cell).

    Bootstrap limpio + sesión de la condición; el stream conversacional se
    alimenta vía el FEED CONTRACT conduciendo la sesión directamente:

    * ``at_t_h``: el reloj virtual avanza hasta ``t_h`` y el mensaje se
      entrega (``session.on_message`` persiste el turno de usuario y la
      réplica del companion al tiempo del reloj).
    * ``after_reply``: se dibuja el retardo sembrado (un dibujo por evento,
      en orden de stream) y el mensaje se entrega en ``now + delay``.

    Sin runtime: sin carreras del rollover ni hilos — determinismo exacto.
    Devuelve ``(db_path, fed)`` con los feeds realmente entregados.
    """
    out = tmp_path / f"cell_{condition.lower()}"
    out.mkdir(parents=True, exist_ok=True)
    db_path = out / f"cell_{condition.lower()}_seed{seed}.db"

    applied = apply_condition_patches(condition)
    try:
        persona = PersonaParams()
        timing = TimingParams()
        variant = MoodVariant.DECOUPLED_OFFSETS
        client = DeterministicClient(seed)
        judge = DeterministicJudge(seed)
        store = SQLiteStore(db_path, audit_mode=True)
        ensure_companion_initialized(
            store, seed=seed,
            user=UserProfile(name="User", interests=GATE2_USER_INTERESTS),
            day=0,
        )
        clock = VirtualClock(0.0)
        session = make_session(condition, seed, store, clock, client, judge,
                               persona, timing, variant)
        stream = build_user_stream(seed, days, perturb=True)
        rng = stream_rng(seed, EXPERIMENT_STREAM, 202)
        fed: list[tuple[float, str]] = []
        end_h = days * 24.0
        for ev in stream:
            if ev["kind"] == "at_t_h":
                t_h = float(ev["t_h"])
                if t_h >= end_h:
                    break
                clock.advance_hours(max(0.0, t_h - clock.now_h()))
                session.on_message(ev["text"])
                fed.append((t_h, ev["text"]))
            else:
                delay = draw_after_reply_delay(ev, rng)
                t_h = clock.now_h() + delay
                if t_h >= end_h:
                    break
                clock.advance_hours(delay)
                session.on_message(ev["text"])
                fed.append((t_h, ev["text"]))
        store.close()
        return db_path, fed
    finally:
        restore_patches(applied)


def _user_stream_from_db(store: SQLiteStore) -> list[tuple[str, float]]:
    """Secuencia de turnos de usuario (contenido, t_h) tal como los vio el
    companion — la fuente de verdad de la identidad entre condiciones."""
    rows = store.conn.execute(
        "SELECT content, t_h FROM messages WHERE role = 'user' ORDER BY id"
    ).fetchall()
    return [(str(r["content"]), round(float(r["t_h"]), 6)) for r in rows]


def _conversations(store: SQLiteStore) -> list[list[dict]]:
    """Agrupa mensajes en conversaciones (una por día).

    En el stream del simulador la unidad conversacional es el intercambio
    diario: la apertura de las 19:00 abre la conversación del día y sus
    seguimientos (after_reply) la continúan. Los proactivos no cuentan como
    turnos de conversación.
    """
    rows = [dict(r) for r in store.conn.execute(
        "SELECT role, content, t_h, proactive FROM messages ORDER BY id")]
    by_day: dict[int, list[dict]] = {}
    for r in rows:
        day = int(float(r["t_h"]) // 24.0)
        if r["role"] == "user" or not r["proactive"]:
            by_day.setdefault(day, []).append(r)
    return [by_day[d] for d in sorted(by_day)]


# Conversational stream


def test_stream_deterministic_per_seed():
    s1 = build_user_stream(SEED, 30, perturb=True)
    s2 = build_user_stream(SEED, 30, perturb=True)
    s3 = build_user_stream(SEED + 1, 30, perturb=True)
    assert s1 == s2
    assert s1 != s3
    assert user_turn_texts(s1) == user_turn_texts(s2)


def test_stream_repertoire_complete():
    """Las seis categorías del repertorio están presentes y en su día."""
    s = build_user_stream(SEED, 30, perturb=True)
    days = event_days(s)
    texts = user_turn_texts(s)
    # Follow-ups, pushback, and ambiguity appear.
    assert any(t in texts for t in _PUSHBACK_POOL)
    assert any(t in texts for t in _AMBIGUOUS_POOL)
    # Every rupture has a repair the next day.
    rups = {d for d, ev in zip(days, s) if ev["text"] in _RUPTURE_POOL}
    repairs = {d for d, ev in zip(days, s) if ev["text"] in _REPAIR_POOL}
    assert rups, "no rupture day in stream"
    assert all((d + 1) in repairs for d in rups), "rupture without next-day repair"
    # Topic abandonment mid-conversation appears.
    assert any(t in texts for t in _ABANDON_POOL)
    # The repertoire spreads across the month.
    assert len({d for d, ev in zip(days, s)
                if ev["text"] in _FOLLOWUP_QUESTION_POOL}) >= 10


def test_probes_and_chains_on_preregistered_days_stream():
    """Sondas de recuerdo y eventos de cadena disparan en su día del manifest."""
    for seed in (SEED, 5002):
        s = build_user_stream(seed, 30, perturb=True)
        by_text = {ev["text"]: d for ev, d in zip(s, event_days(s))}
        for pday, probe, _q in RECALL_PROBES:
            assert by_text.get(probe) == pday - 1, (
                f"probe {probe!r} fuera de día {pday - 1}")
        for chain in EVENT_CHAINS:
            for eday, text in chain["events"]:
                assert by_text.get(text) == eday - 1, (
                    f"evento de cadena {text!r} fuera de día {eday - 1}")


def test_perturbation_adds_exactly_four_embedded_turns():
    """El bloque negativo (días 11-14, 1-indexados) son turnos DENTRO de las
    conversaciones de esos días; ni un evento más cambia."""
    s_on = build_user_stream(SEED, 30, perturb=True)
    s_off = build_user_stream(SEED, 30, perturb=False)
    neg = tuple(PERTURBATION["negative_user_messages"])
    neg_days = sorted(d for ev, d in zip(s_on, event_days(s_on))
                      if ev["text"] in neg)
    assert neg_days == [10, 11, 12, 13]
    for ev, d in zip(s_on, event_days(s_on)):
        if ev["text"] in neg:
            # Scheduled inside the conversation window, before follow-ups.
            assert ev["kind"] == "at_t_h"
            assert 19.0 < ev["t_h"] % 24.0 < 19.4
    rest_on = [ev["text"] for ev in s_on if ev["text"] not in neg]
    assert rest_on == user_turn_texts(s_off)
    assert len(s_on) == len(s_off) + 4


def test_after_reply_delay_draw_bounded_and_deterministic():
    ev = {"kind": "after_reply", "text": "x",
          "min_delay_h": 0.05, "max_delay_h": 0.35}
    rng1 = stream_rng(SEED, EXPERIMENT_STREAM, USER_SIM_STREAM_KEY)
    rng2 = stream_rng(SEED, EXPERIMENT_STREAM, USER_SIM_STREAM_KEY)
    d1 = [draw_after_reply_delay(ev, rng1) for _ in range(50)]
    d2 = [draw_after_reply_delay(ev, rng2) for _ in range(50)]
    assert d1 == d2
    assert all(0.05 <= d <= 0.35 for d in d1)
    # Follow-ups stay within their reply day.
    s = build_user_stream(SEED, 30, perturb=True)
    for d, ev in zip(event_days(s), s):
        if ev["kind"] == "after_reply":
            assert ev["max_delay_h"] < 5.0


# Legacy projection (user_script)


def test_user_script_legacy_projection_invariants():
    s1 = user_script(SEED, 16, perturb=True)
    s2 = user_script(SEED, 16, perturb=True)
    s3 = user_script(SEED, 16, perturb=False)
    assert s1 == s2
    assert s1 != s3
    assert len(s1) == len(s3) + 4
    assert all(a[0] <= b[0] for a, b in zip(s1, s1[1:]))
    # Daily opening at exactly 19:00.
    bases = {int(t // 24.0): txt for t, txt in s1 if abs(t % 24.0 - 19.0) < 1e-6}
    assert set(bases) == set(range(16))
    # Contents match the stream's at_t_h events; the horizon is respected.
    stream = build_user_stream(SEED, 16, perturb=True)
    assert [t for _t, t in s1] == [
        ev["text"] for ev in stream if ev["kind"] == "at_t_h"]
    assert all(t < 16 * 24.0 for t, _x in s1)
    # 30-day budget: 30 openings + 8 probes + 9 chain events + 4 negatives.
    s30 = user_script(SEED, 30, perturb=True)
    assert len(s30) == 30 + len(RECALL_PROBES) + sum(
        len(c["events"]) for c in EVENT_CHAINS) + 4


# Mechanical acceptances (7-day fake cell)


def test_mean_turns_per_conversation_ge_4(tmp_path):
    """Aceptación B3-1: media de turnos por conversación >= 4 (cliente fake)."""
    db, fed = _run_conversational_cell(tmp_path, SEED, "FULL", DAYS)
    assert len(fed) > 0
    store = SQLiteStore(db)
    convs = _conversations(store)
    store.close()
    assert len(convs) == DAYS
    sizes = [len(c) for c in convs]
    mean = sum(sizes) / len(sizes)
    assert mean >= 4.0, f"mean turns per conversation {mean:.2f} < 4"
    assert sum(1 for s in sizes if s >= 4) / len(sizes) >= 0.8


def test_user_turns_identical_across_conditions(tmp_path):
    """Aceptación B3-2: turnos byte-idénticos entre condiciones (mismo seed).

    FULL vs NO_ACTUATORS, cliente fake, runtime determinista: las secuencias
    de CONTENIDO y de tiempos de los turnos de usuario son idénticas.
    """
    db1, fed1 = _run_conversational_cell(tmp_path, SEED, "FULL", DAYS)
    db2, fed2 = _run_conversational_cell(tmp_path, SEED, "NO_ACTUATORS", DAYS)
    assert fed1 == fed2, "feed sequences differ between conditions"
    s1 = _user_stream_from_db(SQLiteStore(db1))
    s2 = _user_stream_from_db(SQLiteStore(db2))
    assert [x[0] for x in s1] == [x[0] for x in s2], "content sequences differ"
    assert [x[1] for x in s1] == [x[1] for x in s2], "timing sequences differ"


def test_probes_and_chains_fire_on_preregistered_days_in_cell(tmp_path):
    """Aceptación B3-3: sondas y cadenas disparan en su día en la célula."""
    db, _ = _run_conversational_cell(tmp_path, SEED, "FULL", DAYS)
    store = SQLiteStore(db)
    rows = [dict(r) for r in store.conn.execute(
        "SELECT content, day FROM messages WHERE role = 'user' ORDER BY id")]
    by_text = {str(r["content"]): int(r["day"]) for r in rows}
    for pday, probe, _q in RECALL_PROBES:
        if pday - 1 < DAYS:
            assert by_text.get(probe) == pday - 1, f"probe {probe!r} día erróneo"
    for chain in EVENT_CHAINS:
        for eday, text in chain["events"]:
            if eday - 1 < DAYS:
                assert by_text.get(text) == eday - 1, (
                    f"cadena {text!r} día erróneo")
    store.close()
