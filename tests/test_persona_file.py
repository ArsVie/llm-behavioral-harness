"""The authored voice comes from a file, and the file is authoritative.

The voice lives in a file named by ``HARNESS_PERSONA_FILE``, applied on every
start as the whole core; unset or broken config
falls back to Nova.
"""

from __future__ import annotations

from harness.bootstrap import ensure_companion_initialized
from harness.domain import UserProfile
from harness.interests import build_catalog
from harness.persona import (
    DEFAULT_NAME,
    DEFAULT_VOICE,
    build_persona,
)
from harness.persona_file import (
    MAX_CORE_CHARS,
    PERSONA_FILE_ENV,
    clean_core,
    load_authored_core,
    persona_file_path,
)
from tests.helpers.store import make_store

MINE = ("mathematics", "lifting", "anime", "history")
VOICE = (
    "You are Lily, smug and bratty, and you call him a dummy when he is "
    "being one. Trailing tildes, kaomoji and never emoji."
)


def _write(tmp_path, text, name="lily.md"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# loading


def test_unset_config_means_the_built_in_default(monkeypatch):
    monkeypatch.delenv(PERSONA_FILE_ENV, raising=False)
    assert persona_file_path() is None
    assert load_authored_core() is None


def test_blank_config_means_the_built_in_default(monkeypatch):
    monkeypatch.setenv(PERSONA_FILE_ENV, "   ")
    assert load_authored_core() is None


def test_a_missing_file_falls_back_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv(PERSONA_FILE_ENV, str(tmp_path / "nope.md"))
    lines = []
    assert load_authored_core(logger=lines.append) is None
    assert lines and "cannot read" in lines[0]


def test_a_blank_file_falls_back_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv(PERSONA_FILE_ENV, str(_write(tmp_path, "\n\n   \n")))
    lines = []
    assert load_authored_core(logger=lines.append) is None
    assert lines and "no prose" in lines[0]


def test_an_oversized_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(PERSONA_FILE_ENV, str(_write(tmp_path, "x" * (MAX_CORE_CHARS + 1))))
    lines = []
    assert load_authored_core(logger=lines.append) is None
    assert lines and "cap" in lines[0]


def test_the_file_content_is_the_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv(PERSONA_FILE_ENV, str(_write(tmp_path, VOICE + "\n")))
    assert load_authored_core() == VOICE


def test_frontmatter_and_headings_are_stripped():
    raw = (
        "---\ntitle: SOUL\ntags: [persona]\n---\n\n"
        "# SOUL.md — Lily Agent Personality\n\n"
        "She is smug.\n\n"
        "## Voice\n\n"
        "She says dummy a lot.\n"
    )
    assert clean_core(raw) == "She is smug.\n\nShe says dummy a lot."


def test_tilde_expansion(monkeypatch):
    monkeypatch.setenv(PERSONA_FILE_ENV, "~/somewhere/lily.md")
    path = persona_file_path()
    assert path is not None and "~" not in str(path)


# the core is the voice; her interests ride her day, not a sentence


def test_the_core_is_the_voice_alone():
    graph = build_catalog()
    persona = build_persona(8001, graph=graph, user_interests=MINE, voice=VOICE)
    assert persona.core == VOICE
    assert "absorbed in" not in persona.core


def test_no_voice_reproduces_the_default_exactly():
    graph = build_catalog()
    a = build_persona(8001, graph=graph, user_interests=MINE)
    b = build_persona(8001, graph=graph, user_interests=MINE, voice=None)
    assert a == b
    assert a.core.startswith(DEFAULT_VOICE)


def test_the_voice_does_not_disturb_the_seeded_draw():
    graph = build_catalog()
    plain = build_persona(8001, graph=graph, user_interests=MINE)
    voiced = build_persona(8001, graph=graph, user_interests=MINE, voice=VOICE)
    assert voiced.interests == plain.interests
    assert voiced.routines == plain.routines


def test_a_stored_core_with_the_old_sentence_is_rewritten(tmp_path, monkeypatch):
    import dataclasses
    monkeypatch.setenv(PERSONA_FILE_ENV, str(_write(tmp_path, VOICE)))
    store = make_store(tmp_path, "old.db")
    try:
        first = ensure_companion_initialized(
            store, seed=8001, user=UserProfile(name="Ars", interests=MINE), day=0,
        )
        store.save_persona(dataclasses.replace(
            first.persona,
            core=VOICE + "\n\nThese days you are absorbed in lifting, with a "
            "soft spot for poetry and a quiet curiosity about birds."))
        second = ensure_companion_initialized(
            store, seed=8001, user=UserProfile(name="Ars", interests=MINE), day=0,
        )
        assert second.persona.core == first.persona.core
    finally:
        store.close()


# the file is authoritative on EVERY start


def test_editing_the_file_changes_a_warm_start(tmp_path, monkeypatch):
    """The workflow: edit the file, restart, done. No reset, no hand-patching."""
    path = _write(tmp_path, VOICE)
    monkeypatch.setenv(PERSONA_FILE_ENV, str(path))
    store = make_store(tmp_path, "voice.db")
    try:
        first = ensure_companion_initialized(
            store, seed=8001, user=UserProfile(name="Ars", interests=MINE), day=0,
        )
        assert first.persona.core.startswith(VOICE)
        drawn = first.persona.interests

        path.write_text("You are Lily and you are done being nice.", encoding="utf-8")
        second = ensure_companion_initialized(
            store, seed=8001, user=UserProfile(name="Ars", interests=MINE), day=0,
        )
        assert second.persona.core.startswith("You are Lily and you are done")
        # The identity underneath is untouched: same portfolio, same routines.
        assert second.persona.interests == drawn
        assert second.persona.routines == first.persona.routines
    finally:
        store.close()


def test_removing_the_file_falls_back_without_losing_the_identity(tmp_path, monkeypatch):
    path = _write(tmp_path, VOICE)
    monkeypatch.setenv(PERSONA_FILE_ENV, str(path))
    store = make_store(tmp_path, "gone.db")
    try:
        first = ensure_companion_initialized(
            store, seed=8001, user=UserProfile(name="Ars", interests=MINE), day=0,
        )
        path.unlink()
        second = ensure_companion_initialized(
            store, seed=8001, user=UserProfile(name="Ars", interests=MINE), day=0,
        )
        assert second.persona.core.startswith(DEFAULT_NAME) or \
            second.persona.core.startswith(DEFAULT_VOICE)
        assert second.persona.interests == first.persona.interests
    finally:
        store.close()


def test_an_unchanged_file_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv(PERSONA_FILE_ENV, str(_write(tmp_path, VOICE)))
    store = make_store(tmp_path, "noop.db")
    try:
        first = ensure_companion_initialized(
            store, seed=8001, user=UserProfile(name="Ars", interests=MINE), day=0,
        )
        second = ensure_companion_initialized(
            store, seed=8001, user=UserProfile(name="Ars", interests=MINE), day=0,
        )
        assert first.persona == second.persona
    finally:
        store.close()


def test_a_reset_reproduces_the_voice(tmp_path, monkeypatch):
    """A fresh store must come back as HER."""
    monkeypatch.setenv(PERSONA_FILE_ENV, str(_write(tmp_path, VOICE)))
    cores = []
    for name in ("run1.db", "run2.db"):
        store = make_store(tmp_path, name)
        try:
            result = ensure_companion_initialized(
                store, seed=8001, user=UserProfile(name="Ars", interests=MINE), day=0,
            )
            cores.append(result.persona.core)
        finally:
            store.close()
    assert cores[0] == cores[1]
    assert cores[0].startswith(VOICE)
