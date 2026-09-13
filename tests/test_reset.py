"""Reset keeps the onboarding cache, clears the trial, and never deletes.

A live writer is refused, and the old database is moved rather than removed.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from time import sleep, time

import pytest

from harness.domain import Interest, PersonaProfile, Routine, UserProfile
from harness.interests import InterestGraph
from harness.reset import (
    CLEAR_TABLES,
    LEARNED_TABLES,
    PRESERVE_TABLES,
    ResetRefused,
    active_writer,
    apply_reset,
    common_columns,
    main,
    plan_reset,
    render_plan,
    render_verification,
    verify_reset,
)
from harness.store import SQLiteStore
from tests.helpers.store import make_store


def _seeded_run(tmp_path, name: str = "run.db"):
    """A run that looks like a live one: onboarding done, a trial happening."""
    store = make_store(tmp_path, name)
    store.save_persona(PersonaProfile(
        name="Lily",
        core="authored voice",
        interests=(Interest("lifting", "exact", 0.9), Interest("anime", "adjacent", 0.6)),
        routines=(Routine("walk the dog", 0.3, 0.5, 0.8, 0.5),),
    ))
    store.save_user_profile(UserProfile(name="Ars", interests=("lifting", "anime", "woodworking")))
    catalog = InterestGraph()
    catalog.add_relation("lifting", "anime", 0.4)
    catalog.add_relation("lifting", "woodworking", 0.2)
    store.save_interest_graph(catalog, origin="catalog")
    placed = InterestGraph()
    placed.add_relation("anime", "woodworking", 0.9)   # the onboarding model call
    store.save_interest_graph(placed, origin="model")
    store.add_message("user", "hey", 10.0, 0)
    store.add_message("assistant", "hi", 10.1, 0)
    store.log_llm_call(0, 10.1, "chat", "prompt", "response", "deepseek/x", {})
    store.log_event(0, 10.1, "day_rollover", "M=8 phase=menstrual")
    store.set_kv("anchor.t_h0", "17.67")
    return store


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            row[0] for row in conn.execute(
                "select name from sqlite_master where type='table'"
                " and name not like 'sqlite_%'"
            )
        ]
        return {
            table: conn.execute(f"select count(*) from {table}").fetchone()[0]
            for table in tables
        }
    finally:
        conn.close()


def test_every_table_in_the_schema_is_a_decision(tmp_path):
    """A schema that grows a table must fail here, not survive a reset silently."""
    store = make_store(tmp_path, "schema.db")
    store.close()
    tables = set(_counts(tmp_path / "schema.db")) - {"schema_meta"}
    documented = set(PRESERVE_TABLES) | set(LEARNED_TABLES) | set(CLEAR_TABLES)
    assert not tables - documented, (
        f"undecided tables: {sorted(tables - documented)} — add each to "
        "PRESERVE_TABLES (onboarding paid for it) or CLEAR_TABLES (trial record)"
    )


def test_the_plan_keeps_onboarding_and_clears_the_trial(tmp_path):
    store = _seeded_run(tmp_path)
    before = _counts(tmp_path / "run.db")
    store.close()

    plan = plan_reset(tmp_path / "run.db", stamp="20260912-120000")

    assert set(plan.keep) == {"persona", "user_profile", "interests", "interest_relations"}
    assert plan.learned == [] and plan.unlisted == []
    assert "messages" in plan.clear and "state_events" in plan.clear
    assert plan.rows["persona"] == 1
    assert plan.rows["interest_relations"] == before["interest_relations"] > 2
    assert plan.rows["messages"] == 2, "the plan counts what it will drop"

    # A plan is a read: nothing moved, nothing written.
    assert _counts(tmp_path / "run.db") == before
    assert not plan.archive_dir.exists()
    assert "MOVED, never deleted" in render_plan(plan)


def test_applying_the_reset_keeps_the_cache_and_archives_the_old_run(tmp_path):
    store = _seeded_run(tmp_path)
    store.close()
    db = tmp_path / "run.db"
    before = _counts(db)
    plan = plan_reset(db, stamp="20260912-120000")

    report = apply_reset(plan)

    after = _counts(db)
    assert after["persona"] == 1, "her identity survives"
    assert after["user_profile"] == 1, "who the user is survives"
    assert after["interests"] == before["interests"] > 0, "the portfolio survives"
    assert after["interest_relations"] == before["interest_relations"], \
        "the graph survives, model-placed edges included"
    assert after["messages"] == 0 and after["state_events"] == 0
    assert after["llm_calls"] == 0 and after["kv_store"] == 0, "no anchor: a fresh run"

    old = plan.archive_dir / "run.db"
    assert old.exists(), "the previous run is archived, never deleted"
    assert _counts(old) == before, "and it is intact"
    assert report["copied"]["persona"] == 1
    assert len(report["cleared"]) >= 10
    assert not list(tmp_path.glob("run.db.reset-staging*")), "no staging left behind"

    # The model-placed edge kept its provenance — that is the onboarding work.
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        origins = {
            row[0] for row in conn.execute("select distinct origin from interest_relations")
        }
        routines = conn.execute("select routines_json from persona").fetchone()[0]
    finally:
        conn.close()
    assert origins == {"catalog", "model"}
    assert "walk the dog" in routines, "the built routine catalog rides in the persona"


def test_a_live_writer_is_refused_and_nothing_moves(tmp_path):
    store = _seeded_run(tmp_path)
    store.close()
    db = tmp_path / "run.db"
    plan = plan_reset(db)

    with pytest.raises(ResetRefused, match="live process"):
        apply_reset(plan, writer=".venv/bin/python -m experiments.live_companion --db run.db")
    assert not plan.archive_dir.exists()
    assert _counts(db)["messages"] == 2, "the run is untouched"


def test_active_writer_sees_a_real_holder_not_just_a_named_path(tmp_path):
    """The live service runs the bot with a RELATIVE --db, so a command-line
    comparison alone reported "nobody holds it" while the bot did."""
    store = _seeded_run(tmp_path)
    store.close()
    db = tmp_path / "run.db"
    assert active_writer(db) is None

    opener = subprocess.Popen(
        [sys.executable, "-c",
         "import sqlite3, sys, time;"
         " c = sqlite3.connect(sys.argv[1]); c.execute('select 1'); time.sleep(30)",
         "run.db"],                      # relative, like the service unit
        cwd=tmp_path,
    )
    def holder_appears(seconds: float = 10.0) -> bool:
        # The child needs a moment to import sqlite3 and open the file; the
        # open handle is the signal, so poll for it rather than assume it.
        deadline = time() + seconds
        while time() < deadline:
            if active_writer(db) is not None:
                return True
            sleep(0.05)
        return False

    try:
        assert holder_appears(), "an open handle is a holder"
    finally:
        opener.terminate()
        opener.wait(timeout=10)

    named = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(db)])
    try:
        assert active_writer(db) is not None, "a named path counts too (not open yet)"
    finally:
        named.terminate()
        named.wait(timeout=10)

    assert active_writer(db) is None, "and it clears once they are gone"


def test_an_older_database_still_copies_the_columns_it_has(tmp_path):
    """A reset must not abort on a schema older than the one it builds."""
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript(
        "create table persona (id integer primary key, name text, core text);"
        "create table messages (id integer primary key, role text);"
    )
    conn.execute("insert into persona (name, core) values ('Lily', 'voice')")
    conn.execute("insert into messages (role) values ('user')")
    conn.commit()
    conn.close()

    plan = plan_reset(old, stamp="20260912-120000")
    assert plan.keep == ["persona"], "only the tables the source has"
    report = apply_reset(plan)

    conn = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
    try:
        row = conn.execute("select id, name, core from persona").fetchone()
    finally:
        conn.close()
    assert row == (1, "Lily", "voice"), "the shared columns came across"
    assert report["copied"] == {"persona": 1}
    assert _counts(old)["messages"] == 0


def test_verify_requires_an_empty_trial_before_the_service_starts(tmp_path):
    """The script starts the bot, which writes its anchor and day-0 rows within
    seconds, so "is the trial empty?" is only askable BEFORE that."""
    store = _seeded_run(tmp_path)
    store.close()
    db = tmp_path / "run.db"

    before = verify_reset(db, expect_fresh=True)
    assert before["problems"], "a run with a transcript is not a fresh reset"
    assert any("old trial survived" in problem for problem in before["problems"])
    assert not verify_reset(db)["problems"], "post-start mode does not assert emptiness"

    apply_reset(plan_reset(db, stamp="20260912-120000"))
    fresh = verify_reset(db, expect_fresh=True)
    assert fresh["problems"] == [], fresh["problems"]
    assert fresh["kept"]["persona"] == 1 and not any(fresh["trial"].values())
    assert not fresh["anchor"], "the clock starts over"
    assert "verified" in render_verification(fresh, expect_fresh=True)


def test_verify_calls_a_lost_cache_a_wipe_not_a_reset(tmp_path):
    store = make_store(tmp_path, "bare.db")      # schema only: no onboarding
    store.close()
    report = verify_reset(tmp_path / "bare.db", expect_fresh=True)
    assert len(report["problems"]) == len(PRESERVE_TABLES)
    assert all("did not come across" in problem for problem in report["problems"])


def test_verify_after_the_run_starts_requires_the_new_anchor(tmp_path):
    store = _seeded_run(tmp_path)
    store.close()
    db = tmp_path / "run.db"
    apply_reset(plan_reset(db, stamp="20260912-120000"))

    # Post-start mode REQUIRES the anchor: it is the liveness signal that the
    # service actually began writing a new run after the swap.
    missing_anchor = verify_reset(db)
    assert missing_anchor["problems"] == [
        "no anchor: the run did not start writing after the reset"
    ]
    reopened = SQLiteStore(db)
    reopened.set_kv("anchor.t_h0", "21.66")
    reopened.close()
    live = verify_reset(db)
    assert live["anchor"] and live["problems"] == []


def test_the_cli_verify_mode_reports_and_exits(tmp_path, capsys):
    store = _seeded_run(tmp_path)
    store.close()
    db = tmp_path / "run.db"

    assert main(["--db", str(db), "--verify", "--expect-fresh"]) == 1
    assert "PROBLEM" in capsys.readouterr().out

    apply_reset(plan_reset(db, stamp="20260912-120000"))
    assert main(["--db", str(db), "--verify", "--expect-fresh"]) == 0
    assert "verified" in capsys.readouterr().out


def test_common_columns_keeps_the_target_order_and_intersection():
    assert common_columns(["a", "b"], ["b", "c"]) == ["b"]
    assert common_columns(["a", "b"], ["b", "a", "c"]) == ["b", "a"]
    assert common_columns([], ["a"]) == []
