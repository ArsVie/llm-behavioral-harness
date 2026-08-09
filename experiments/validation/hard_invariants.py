"""Invariantes duras del validador (Iteración 3, B8) — los dientes que la
auditoría mecánica no tenía.

Cierra F1 (auditoría ciega): una célula donde el compañero calla el 40% de
las veces devolvía ``validated: true``. Estas cuatro invariantes fallan FUERTE
y con el conteo en el mensaje de fallo:

1. ``empty_assistant_turns == 0`` (cero duro) — un turno assistant vacío o de
   solo espacios en blanco es un fallo de la célula.
2. ``blank_rate < BLANK_RATE_CEILING`` (techo preregistrado a nivel de run) —
   techo declarado: < 1% (plan §11 DoD item 1; B10 lo revisa). El corpus it2
   corrió 18–40% de blancos; el techo lo hace imposible.
3. ``truncated_replies == 0`` — detección de truncamiento: ``finish_reason ==
   'length'`` en las filas ``llm_calls`` cuando esté disponible (meta JSON;
   hoy ``meta`` es NULL — vía aditiva, se activa sola cuando B7 lo pueble) y
   heurística de réplica final corta: la ÚLTIMA réplica assistant del run con
   <= 4 caracteres no-blancos (el corpus it2 termina en "Nova: Hey") es un
   truncamiento sospechoso (umbral declarado: SHORT_FINAL_MAX_CHARS=4,
   MIN_ASSISTANT_TURNS_FOR_SHORT_FINAL=5 para no disparar en células
   miniatura).
4. Coherencia de conversación — ninguna conversación con CERO turnos del
   compañero (seam de B2: tablas ``conversations``/``conversation_turns`` o
   columna ``messages.conversation_id``). Degrada con gracia cuando B2 no ha
   aterrizado (tabla ausente): ``available=False`` — se REPORTA, no se calla
   ni se falla.

Convención del repo: docstrings en español, identificadores en inglés.
"""

from __future__ import annotations

from typing import Callable

#: Techo preregistrado de tasa de blancos a nivel de run (< 1%, plan §11).
BLANK_RATE_CEILING = 0.01
#: Umbral declarado de la heurística de réplica final corta (caracteres
#: no-blancos). "Nova: Hey" (corpus it2) = 3 -> sospechoso.
SHORT_FINAL_MAX_CHARS = 4
#: Mínimo de turnos assistant para aplicar la heurística de réplica final
#: corta (una célula miniatura de 1 turno no es un corpus truncado).
MIN_ASSISTANT_TURNS_FOR_SHORT_FINAL = 5

#: Claves del resumen de invariantes (contrato del pre-flight y del hook).
INVARIANT_KEYS = (
    "empty_assistant_turns",
    "blank_rate",
    "truncated_replies",
    "conversation_coherence",
)


def _table_exists(store, name: str) -> bool:
    row = store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(store, table: str, column: str) -> bool:
    if not _table_exists(store, table):
        return False
    cols = {r[1] for r in store.conn.execute(f"PRAGMA table_info({table})")}
    return column in cols


def empty_assistant_turns(store) -> int:
    """Turnos assistant vacíos o de solo espacios (invariante 1, cero duro)."""
    rows = store.conn.execute(
        "SELECT id, content FROM messages WHERE role = 'assistant'"
    ).fetchall()
    return sum(1 for r in rows if not str(r["content"] or "").strip())


def blank_rate(store) -> float:
    """Tasa de blancos a nivel de run (invariante 2): blanks / assistant.

    0.0 cuando no hay turnos assistant (sin división por cero; una célula
    hueca se ve en el resumen por ``n_assistant_turns`` y en la coherencia).
    """
    row = store.conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE role = 'assistant'"
    ).fetchone()
    n = int(row["n"])
    if n == 0:
        return 0.0
    return empty_assistant_turns(store) / n


def truncated_reply_hits(store) -> list[dict]:
    """Detección de truncamiento (invariante 3).

    Dos vías, ambas declaradas:
    * ``finish_reason == 'length'`` parseado del JSON de ``llm_calls.meta``
      cuando la fila lo traiga (hoy ``meta`` es NULL; vía aditiva para cuando
      el seam de persistencia de prompts de B7 lo pueble).
    * Heurística de réplica final corta: la última réplica assistant del run
      (por t_h, id) con <= SHORT_FINAL_MAX_CHARS caracteres no-blancos, si el
      run tiene >= MIN_ASSISTANT_TURNS_FOR_SHORT_FINAL turnos assistant.
    """
    hits: list[dict] = []
    for r in store.conn.execute(
        "SELECT id, meta FROM llm_calls WHERE meta IS NOT NULL"
    ).fetchall():
        meta = r["meta"]
        if not isinstance(meta, str):
            continue
        try:
            import json

            parsed = json.loads(meta)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("finish_reason") == "length":
            hits.append({
                "kind": "finish_reason",
                "call_id": int(r["id"]),
                "detail": "llm_calls meta finish_reason=length",
            })
    rows = store.conn.execute(
        "SELECT id, content FROM messages WHERE role = 'assistant' "
        "ORDER BY t_h, id"
    ).fetchall()
    if len(rows) >= MIN_ASSISTANT_TURNS_FOR_SHORT_FINAL:
        last = rows[-1]
        n_chars = len(str(last["content"] or "").strip())
        if 0 < n_chars <= SHORT_FINAL_MAX_CHARS:
            hits.append({
                "kind": "short_final",
                "message_id": int(last["id"]),
                "detail": (
                    f"final assistant reply has {n_chars} non-whitespace "
                    f"chars (<= {SHORT_FINAL_MAX_CHARS}) — suspected truncation"
                ),
            })
    return hits


def conversation_coherence(store) -> tuple[list[str], bool]:
    """Coherencia de conversación (invariante 4): ninguna conversación con
    cero turnos del compañero.

    Devuelve (violaciones, available). ``available=False`` cuando el seam de
    B2 no está presente (tabla ``conversations`` ausente o sin fuente de
    turnos reconocible) — degradación con gracia, se reporta. Fuentes de
    turnos reconocidas, por orden: tabla ``conversation_turns`` (columna
    ``speaker``), columna ``messages.conversation_id``.
    """
    if not _table_exists(store, "conversations"):
        return [], False
    violations: list[str] = []
    if _table_exists(store, "conversation_turns") and _column_exists(
        store, "conversation_turns", "speaker"
    ):
        rows = store.conn.execute(
            "SELECT conversation_id, "
            "SUM(CASE WHEN speaker = 'companion' THEN 1 ELSE 0 END) AS n_comp, "
            "COUNT(*) AS n_turns "
            "FROM conversation_turns GROUP BY conversation_id"
        ).fetchall()
        for r in rows:
            if int(r["n_comp"]) == 0:
                violations.append(
                    f"conversation {r['conversation_id']} has 0 companion turns "
                    f"({int(r['n_turns'])} turns)"
                )
        return violations, True
    if _column_exists(store, "messages", "conversation_id"):
        rows = store.conn.execute(
            "SELECT conversation_id, "
            "SUM(CASE WHEN role = 'assistant' THEN 1 ELSE 0 END) AS n_comp, "
            "COUNT(*) AS n_turns "
            "FROM messages GROUP BY conversation_id"
        ).fetchall()
        for r in rows:
            if int(r["n_comp"]) == 0:
                violations.append(
                    f"conversation {r['conversation_id']} has 0 companion turns "
                    f"({int(r['n_turns'])} turns)"
                )
        return violations, True
    return [], False


def check_hard_invariants(store) -> dict:
    """Evalúa las cuatro invariantes duras; devuelve el resumen con conteos.

    Forma (contrato del pre-flight y de los tests):
        {
          "empty_assistant_turns": {"count": int, "ok": bool},
          "blank_rate":           {"rate": float, "ceiling": float, "ok": bool},
          "truncated_replies":    {"count": int, "hits": [...], "ok": bool},
          "conversation_coherence": {"violations": [...], "available": bool,
                                     "ok": bool},
        }
    ``conversation_coherence.ok`` es True cuando no está disponible (check
    saltado por degradación, no por aprobación).
    """
    n_empty = empty_assistant_turns(store)
    rate = blank_rate(store)
    hits = truncated_reply_hits(store)
    violations, available = conversation_coherence(store)
    return {
        "empty_assistant_turns": {"count": n_empty, "ok": n_empty == 0},
        "blank_rate": {
            "rate": round(rate, 6),
            "ceiling": BLANK_RATE_CEILING,
            "ok": rate < BLANK_RATE_CEILING,
        },
        "truncated_replies": {"count": len(hits), "hits": hits, "ok": not hits},
        "conversation_coherence": {
            "violations": violations,
            "available": available,
            "ok": (not available) or not violations,
        },
    }


def failure_messages(result: dict) -> list[str]:
    """Mensajes de fallo FUERTES, con el conteo en el mensaje (aceptación B8)."""
    msgs: list[str] = []
    e = result["empty_assistant_turns"]
    if not e["ok"]:
        msgs.append(
            f"hard invariant violated: empty_assistant_turns = {e['count']} "
            f"(must be 0) — blank/whitespace assistant turns persisted"
        )
    b = result["blank_rate"]
    if not b["ok"]:
        msgs.append(
            f"hard invariant violated: blank_rate = {b['rate']:.4f} "
            f"(ceiling {b['ceiling']:.4f}) — run above the preregistered "
            f"blank-rate ceiling"
        )
    t = result["truncated_replies"]
    if not t["ok"]:
        detail = "; ".join(h["detail"] for h in t["hits"])
        msgs.append(
            f"hard invariant violated: truncated_replies = {t['count']} "
            f"(must be 0) — {detail}"
        )
    c = result["conversation_coherence"]
    if c["available"] and not c["ok"]:
        msgs.append(
            f"hard invariant violated: conversations_with_zero_companion_turns "
            f"= {len(c['violations'])} (must be 0) — "
            f"{'; '.join(c['violations'][:5])}"
        )
    return msgs


def assert_cell_valid(store) -> None:
    """Eleva AssertionError con los conteos si la célula viola una invariante."""
    result = check_hard_invariants(store)
    failures = failure_messages(result)
    if failures:
        raise AssertionError("; ".join(failures))
