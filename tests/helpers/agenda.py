"""Agenda/row builders — the consolidated ``_rows``/``_suppressed_codes``/
``_ground_agenda``/``_agenda_item`` helpers.

Fork resolution (documented per the review): ``rows`` uses the canonical
``abs(float(r["t_h"]))`` form — the more defensive variant used by 4 of the
5 files. ``test_adversarial_restart.py`` previously used the plain
``float(r["t_h"])`` form; schedule t_h values are always non-negative in
practice, so the canonical form is behavior-identical there and its
expectations were NOT changed.
"""

from __future__ import annotations

from harness.domain import AgendaItem, DailyAgenda

#: Safe awake hour (quiet hours are 23..8) — mirrors test_proactive.
AGENDA_HOUR = 14.0


def agenda_item(item_id="a1", *, start=None, end=None, status="planned",
                activity="pottery class", salience=0.7, source_type="arc",
                source_id="arc_pottery"):
    """AgendaItem builder — the test_proactive/agenda-family shape."""
    start = AGENDA_HOUR if start is None else start
    end = AGENDA_HOUR + 2.0 if end is None else end
    return AgendaItem(
        id=item_id, start_t_h=start, end_t_h=end, activity=activity,
        source_type=source_type, source_id=source_id, salience=salience,
        status=status,
    )


def ground_agenda(store, start_t_h, end_t_h, *, item_id="g1", salience=0.8,
                  activity="pottery class"):
    """Seed an agenda item covering [start, end) so a real IntentResolver
    finds a grounded candidate at those hours. Returns the item.

    (Consolidated from the byte-identical test_runtime /
    test_runtime_anchor / test_proactive_it2 / test_adversarial_runtime /
    test_adversarial_proactivity definitions; the test_proactive.py
    ``_agenda_item`` keyword shape is used, matching the callers that
    imported it from there.)
    """
    item = agenda_item(item_id=item_id, start=start_t_h, end=end_t_h,
                       salience=salience, activity=activity)
    store.save_agenda(0, DailyAgenda(0, (item,)))
    return item


def ground_item(item_id: str, start_t_h: float, end_t_h: float,
                activity: str = "pottery", salience: float = 0.8) -> AgendaItem:
    """Positional AgendaItem builder — the test_adversarial_restart shape."""
    return AgendaItem(item_id, start_t_h, end_t_h, activity, "arc", "arc1",
                      salience, "planned")


def rows(store, seed: int):
    """Schedule rows keyed by ``abs(float(t_h))`` (canonical fork variant)."""
    return {abs(float(r["t_h"])): r for r in store.schedule_events_for_seed(seed)}


def suppressed_codes(store):
    """The set of suppression gate codes logged since day 0."""
    return {
        e["detail"]
        for e in store.events_since(0)
        if e["event"] == "proactive_suppressed"
    }
