"""The study builders: the day view the calendar draws and the panel data.

Read-only shapes over the run's own tables — agenda, reach-out decisions,
message counts, the cycle, and the functions the panels mark events on.
Nothing here writes, so a run can be studied while it is live.
"""

from __future__ import annotations

import json

from harness.domain import AgendaItem, DailyAgenda
from observability import study
from tests.helpers.store import make_store


def _store(tmp_path):
    store = make_store(tmp_path)
    store.save_agenda(0, DailyAgenda(0, (
        AgendaItem("ag1", 8.0, 9.0, "pottery", "interest", "i1", 0.8, "completed"),
        AgendaItem("ag2", 14.0, 15.0, "history reading", "routine", "r1", 0.4, "planned"),
    )))
    store.save_daily_state(0, {
        "day": 0, "M": 7, "m": -0.4, "g": 0.2, "p": 0.5, "arg": 0.0,
        "mu": 0.0, "eta": 0.0, "cycle_day": 3.0, "phase_label": "follicular",
        "seed": 1, "score": 0.0,
    })
    store.add_message("user", "hi", 9.5, 0)
    store.add_message("assistant", "hello", 9.6, 0)
    # One reach-out the model talked itself out of, and one it sent.
    for t_h, verdict in ((10.0, {"initiate": False, "reason": "quiet hour"}),
                         (12.0, {"initiate": True, "reason": "she finished"})):
        store.record_decision(
            0, t_h, "tool_decide_proactive", None, None, None, str(t_h),
            json.dumps({"hook": "Agenda: pottery", "reason": "schedule"}),
            None, json.dumps(verdict), "model", "native", t_h, 0,
        )
    store.add_message("assistant", "the proof came out clean", 12.1, 0, proactive=True)
    return store


def test_the_day_view_carries_the_day(tmp_path):
    store = _store(tmp_path)
    view = study.day_view(store.conn, day=0)

    assert view["day"] == 0
    assert [a["id"] for a in view["agenda"]] == ["ag1", "ag2"]
    assert [a["status"] for a in view["agenda"]] == ["completed", "planned"]
    assert [(m["role"], m["hour"]) for m in view["messages"]] == [
        ("user", 9.5), ("assistant", 9.6), ("assistant", 12.1)]
    assert view["cycle"]["cycle_day"] == 3.0
    assert view["cycle"]["phase_label"] == "follicular"
    # The wheel's phases are the engine's own table, not a made-up wheel.
    phases = view["cycle"]["phases"]
    assert [phase["label"] for phase in phases][0] == "menstrual"
    assert phases[0]["from"] == 0.0 and phases[-1]["to"] == 1.0
    assert all(0.0 <= phase["from"] < phase["to"] <= 1.0 for phase in phases)
    assert view["mood"] == {"M": 7, "m_level": -0.4}
    assert view["quiet_hours"] == [23.0, 8.0]
    assert view["now_hour"] == 12.1          # the newest row is the run's present
    assert view["today"] == 0
    # The curve is the engine's own energy, sampled across the day.
    curve = view["circadian"]
    assert len(curve) == 49 and curve[0][0] == 0.0 and curve[-1][0] == 24.0
    assert all(0.0 <= value <= 1.0 for _, value in curve)
    # An unanchored store has no dates; the front-end falls back to day labels.
    assert view["dates"] == {} and view["date"] is None
    store.close()


def test_a_reach_out_is_a_marker_whether_or_not_it_was_sent(tmp_path):
    store = _store(tmp_path)
    view = study.day_view(store.conn, day=0)

    assert [(p["hour"], p["fired"]) for p in view["proactive"]] == [
        (10.0, False), (12.0, True)]
    assert view["proactive"][0]["reason"] == "quiet hour"
    store.close()


def test_the_panels_mark_the_whole_run(tmp_path):
    """The panels read the run, not the day the calendar happens to show."""
    store = _store(tmp_path)
    store.add_message("user", "next day", 25.0, 1)
    panels = study.day_view(store.conn, day=0)["panels"]

    assert [(event["day"], event["hour"], event["fired"])
            for event in panels["proactive"]] == [(0, 10.0, False), (0, 12.0, True)]
    assert panels["contact_days"] == [0, 1]
    assert panels["contact"][0][9] == [1, 1, 0]    # you wrote, she replied
    assert panels["contact"][0][12] == [0, 0, 1]   # her reach-out
    assert panels["contact"][1][1] == [1, 0, 0]
    store.close()


def test_the_mood_series_keeps_the_expectation_kernel(tmp_path):
    store = make_store(tmp_path)
    for day, mood in enumerate([0, 0, 0, 7, 0, 0, 0, 0]):
        store.save_daily_state(day, {
            "day": day, "M": mood, "m": 0.0, "g": 0.0, "p": 0.0, "arg": 0.0,
            "mu": 0.0, "eta": 0.0, "cycle_day": 0.0, "phase_label": "follicular",
            "seed": 1, "score": 0.0,
        })
    series = study.day_view(store.conn, day=3)["mood_series"]

    assert series["days"] == list(range(8))
    assert series["M"] == [0, 0, 0, 7, 0, 0, 0, 0]
    # A 7-wide box mean, padded at the edges: the one spike at day 3 is inside
    # every window up to day 6, and out of the window by day 7.
    assert series["expected"][0] == 1.0
    assert series["expected"][6] == 1.0
    assert series["expected"][7] == 0.0
    assert series["window"] == 7
    store.close()


def test_the_rhythm_curves_are_the_engines_own(tmp_path):
    store = _store(tmp_path)
    rhythm = study.day_view(store.conn, day=0)["rhythm"]

    assert rhythm["current"] == "follicular"
    assert [curve["phase"] for curve in rhythm["curves"]] == ["follicular"]
    points = rhythm["curves"][0]["points"]
    assert len(points) == 49 and points[0][0] == 0.0 and points[-1][0] == 24.0
    assert all(0.0 <= value <= 1.0 for _, value in points)
    assert rhythm["quiet_hours"] == [23.0, 8.0]
    store.close()


def test_the_span_rolls_the_run_up_per_day(tmp_path):
    store = _store(tmp_path)
    store.add_message("user", "next day", 25.0, 1)

    view = study.day_view(store.conn, day=0)
    first, second = view["span"]
    assert [entry["day"] for entry in view["span"]] == [0, 1]
    assert first["agenda"] == [
        {"hour": 8.0, "end": 9.0, "status": "completed", "source": "interest"},
        {"hour": 14.0, "end": 15.0, "status": "planned", "source": "routine"},
    ]
    assert first["sent"] == 1 and first["suppressed"] == 1
    assert first["reach_outs"] == [{"hour": 10.0, "fired": False},
                                   {"hour": 12.0, "fired": True}]
    assert first["messages"] == 3 and first["M"] == 7
    assert first["phase_label"] == "follicular" and first["cycle_day"] == 3.0
    # Day 1 only has the one message, and no row of its own.
    assert second["messages"] == 1 and second["agenda"] == []
    assert second["M"] is None and second["phase_label"] == ""
    store.close()


def test_a_day_that_is_not_now_has_no_now_line(tmp_path):
    store = _store(tmp_path)
    store.add_message("user", "next day", 25.0, 1)

    assert study.day_view(store.conn, day=0)["now_hour"] is None
    assert study.day_view(store.conn, day=1)["now_hour"] == 1.0
    assert study.day_numbers(store.conn) == [0, 1]
    assert study.latest_day(store.conn) == 1
    store.close()


def test_an_empty_run_reads_empty_but_whole(tmp_path):
    store = make_store(tmp_path)
    view = study.day_view(store.conn, day=0)

    assert view["agenda"] == [] and view["messages"] == []
    assert view["span"] == [] and view["days"] == []
    assert view["mood_series"]["expected"] == []
    assert view["rhythm"]["curves"] == []
    assert view["panels"] == {"proactive": [], "contact": [],
                              "contact_days": []}
    assert view["now_hour"] is None and view["today"] == 0
    assert study.run_now(store.conn) is None
    store.close()
