"""Her face tag: ``[smug] ...`` opens each reply (``harness.faces``).

Her stored row keeps the tag (she keeps tagging only when her history has
tags); every channel gets the text without it; the call gets the face as its
own event ahead of the first sentence; a reply without a tag gets one from a
single ``Sprite:`` continuation of the same request.
"""

from __future__ import annotations

import sqlite3

import pytest

from harness.client import FakeClient
from harness.clock import VirtualClock
from harness.faces import (
    CALL_FACE_RULE,
    FACE_INSTRUCTION,
    FALLBACK_PREFIX,
    FaceCut,
    FaceEvent,
    face_from_continuation,
    paragraph_faces,
    split_face,
    strip_face,
    with_face,
)
from tests.helpers.store import make_session, make_store
from tests.test_cache_prefix_gate import _persona
from tests.test_call_decision import _verdicts, decision_env  # noqa: F401 - fixture

SEED = 4242


# -- parsing ------------------------------------------------------------------ #

@pytest.mark.parametrize("text, face, rest", [
    ("[smug] Oh, so now you want help?", "smug", "Oh, so now you want help?"),
    ("[Pout]\nFine.", "pout", "Fine."),
    ("  **[laugh]** Ha!", "laugh", "Ha!"),
    ("[sigh] Whatever.", None, "[sigh] Whatever."),       # not a face: her text
    ("Oh, [smug] later.", None, "Oh, [smug] later."),      # only the lead counts
    ("No tag at all.", None, "No tag at all."),
    ("", None, ""),
])
def test_split_face(text, face, rest):
    assert split_face(text) == (face, rest)
    assert strip_face(text) == rest


def test_with_face_round_trips():
    assert with_face("smug", "Hi.") == "[smug] Hi."
    assert with_face(None, "Hi.") == "Hi."
    assert split_face(with_face("worried", "You okay?")) == ("worried", "You okay?")


@pytest.mark.parametrize("text, face", [
    (" smug", "smug"), ("smug", "smug"), (" [pout]", "pout"), (" Flustered", "flustered"),
    (" sigh", None), ("", None), ("Let me think about", None),
])
def test_face_from_continuation(text, face):
    assert face_from_continuation(text) == face


def _cut(pieces):
    cut = FaceCut()
    out = "".join(cut.feed(p) for p in pieces) + cut.flush()
    return cut.face, out


@pytest.mark.parametrize("pieces", [
    ["[smug] Oh, so."], ["[", "sm", "ug", "]", " Oh", ", so."], ["[smug]", "\n", "Oh, so."],
    ["  ", "[smug", "] Oh, so."],
])
def test_face_cut_across_chunks(pieces):
    assert _cut(pieces) == ("smug", "Oh, so.")


@pytest.mark.parametrize("pieces, text", [
    (["Oh, so."], "Oh, so."),
    (["[sigh", "] fine."], "[sigh] fine."),
    (["[", "not a face at all, this is long"], "[not a face at all, this is long"),
    (["["], "["),
])
def test_face_cut_passes_other_text_through(pieces, text):
    assert _cut(pieces) == (None, text)


# -- the text turn ------------------------------------------------------------ #

def _session(tmp_path, client, name="faces.db"):
    store = make_store(tmp_path, name)
    profile = _persona()
    store.save_persona(profile)
    session = make_session(store, clock=VirtualClock(t_h=9.6), client=client,
                           seed=SEED, persona_profile=profile)
    return store, session


def _her_rows(store):
    return [m["content"] for m in store.recent_messages(limit=100)
            if m["role"] == "assistant"]


def test_a_tagged_reply_is_stored_tagged_and_delivered_bare(tmp_path):
    client = FakeClient(responses=["[smug] Oh, so now you need me."])
    store, session = _session(tmp_path, client)
    try:
        result = session.on_message("help me with this")
        assert result.face == "smug"
        assert result.reply == "Oh, so now you need me."
        assert _her_rows(store) == ["[smug] Oh, so now you need me."]
        assert "[smug]" not in session._transcript_for(0)
        assert FACE_INSTRUCTION in client.calls[-1]["system"]
        # her next request replays her tagged row
        session.clock.advance_hours(0.05)
        session.on_message("please")
        assert any(m.get("content") == "[smug] Oh, so now you need me."
                   for m in client.calls[-1]["messages"])
    finally:
        store.close()


def test_strip_face_cuts_every_line_opening_tag_and_nothing_else():
    text = "[angry] Off. Now.\n\n[embarrassed] I'm serious.\n[sigh] mine, and [sad] too"
    assert strip_face(text) == "Off. Now.\n\nI'm serious.\n[sigh] mine, and [sad] too"


def test_paragraph_faces_gives_each_paragraph_its_own_face():
    text = "[angry] Off.\n\n[sad] Don't go.\n\nSit down."
    assert paragraph_faces(text) == [
        ("angry", "Off."), ("sad", "Don't go."), ("sad", "Sit down.")]


def test_a_later_paragraph_tag_never_reaches_him(tmp_path, monkeypatch):
    """Live 2026-09-30: 41 of 60 replies gave a later paragraph its own tag,
    and each paragraph goes out as its own message."""
    monkeypatch.setenv("HARNESS_BUBBLES", "1")
    reply = "[angry] Off. Now, dummy.\n\n[embarrassed] I'm serious."
    client = FakeClient(responses=[reply])
    store, session = _session(tmp_path, client)
    try:
        result = session.on_message("hm?")
        assert result.face == "angry"
        assert result.reply == "Off. Now, dummy.\n\nI'm serious."
        assert result.bubbles == ("Off. Now, dummy.", "I'm serious.")
        assert _her_rows(store) == [reply]
    finally:
        store.close()

def test_a_reply_that_is_only_a_tag_is_an_empty_reply(tmp_path):
    client = FakeClient(responses=["[smug]", "[pout] Fine, hi."])
    store, session = _session(tmp_path, client)
    try:
        result = session.on_message("hi")
        assert result.reply == "Fine, hi."
        assert _her_rows(store) == ["[pout] Fine, hi."]
    finally:
        store.close()


class PrefixClient(FakeClient):
    supports_prefix = True
    session_id = "sess"

    def __init__(self, responses):
        super().__init__(responses=responses)
        self.sessions = []

    def chat_with_meta(self, messages, **kwargs):
        self.sessions.append(self.session_id)
        return super().chat_with_meta(messages, **kwargs)


def test_an_untagged_reply_gets_its_face_from_one_continuation(tmp_path):
    client = PrefixClient(responses=["Oh. You again.", " pout"])
    store, session = _session(tmp_path, client)
    try:
        result = session.on_message("hey")
        assert result.face == "pout"
        assert result.reply == "Oh. You again."
        assert _her_rows(store) == ["[pout] Oh. You again."]
        chat, pick = client.calls[-2], client.calls[-1]
        # the same request, extended by her reply as an assistant prefix
        assert pick["messages"][:-1] == chat["messages"]
        assert pick["messages"][-1] == {"role": "assistant", "prefix": True,
                                        "content": "Oh. You again." + FALLBACK_PREFIX}
        assert pick["system"] == chat["system"] and pick["tools"] == chat["tools"]
        assert pick["reasoning_effort"] == chat["reasoning_effort"]
        # never capped: a reasoning model spends a small cap on thinking
        assert pick["max_tokens"] is None
        roles = [r[0] for r in store.conn.execute("SELECT role FROM llm_calls")]
        assert roles.count("face_pick") == 1
    finally:
        store.close()


def test_a_pick_with_no_name_is_asked_once_more_on_a_fresh_session(tmp_path):
    client = PrefixClient(responses=["Oh. You again.", "Let me think", "Let me think"])
    store, session = _session(tmp_path, client)
    try:
        result = session.on_message("hey")
        assert result.face is None
        assert _her_rows(store) == ["Oh. You again."]
        assert client.sessions[-2:] == ["sess", "sess-face1"]
        assert client.session_id == "sess"          # restored
    finally:
        store.close()


def test_the_tag_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FACE_TAG", "0")
    client = PrefixClient(responses=["Oh. You again."])
    store, session = _session(tmp_path, client)
    try:
        result = session.on_message("hey")
        assert result.face is None
        assert len(client.calls) == 1               # no pick
        assert FACE_INSTRUCTION not in client.calls[0]["system"]
    finally:
        store.close()


# -- the call ----------------------------------------------------------------- #

class SmallChunks(FakeClient):
    def chat_stream(self, messages, **kwargs):
        kwargs["chunk_size"] = 3
        return super().chat_stream(messages, **kwargs)


def test_the_call_sends_the_face_first_and_stores_it_with_what_he_heard(
        tmp_path, decision_env):  # noqa: F811 - fixture
    store = make_store(tmp_path, "call-face.db")
    profile = _persona()
    store.save_persona(profile)
    client = SmallChunks(responses=["reply 0.\n" + _verdicts(True), _verdicts(True),
                                    "[smug] Took you long enough. Hi."])
    session = make_session(store, clock=VirtualClock(t_h=9.6), client=client,
                           seed=SEED, persona_profile=profile)
    try:
        session.on_message("hey")
        assert session.incoming_call("cid").call_answered
        items = list(session.call_turn(None))
        assert isinstance(items[0], FaceEvent) and items[0].face == "smug"
        assert items[1:] == ["Took you long enough.", "Hi."]
        session.call_heard("Took you long enough.", True)
        assert _her_rows(store)[-1] == "[smug] Took you long enough."
    finally:
        store.close()


def test_a_later_sentence_tag_on_the_call_changes_the_face_unspoken(
        tmp_path, decision_env):  # noqa: F811 - fixture
    store = make_store(tmp_path, "call-face2.db")
    profile = _persona()
    store.save_persona(profile)
    client = SmallChunks(responses=["reply 0.\n" + _verdicts(True), _verdicts(True),
                                    "[smug] Took you long enough.\n[pout] Hi."])
    session = make_session(store, clock=VirtualClock(t_h=9.6), client=client,
                           seed=SEED, persona_profile=profile)
    try:
        session.on_message("hey")
        assert session.incoming_call("cid").call_answered
        items = list(session.call_turn(None))
        faces = [i.face for i in items if isinstance(i, FaceEvent)]
        said = [i for i in items if not isinstance(i, FaceEvent)]
        assert faces == ["smug", "pout"]
        assert said == ["Took you long enough.", "Hi."]
    finally:
        store.close()

def _call_start_row(store) -> str:
    rows = store.conn.execute(
        "select content from messages where role = 'system' "
        "and content like 'Voice call started%'").fetchall()
    return rows[-1][0]


@pytest.mark.parametrize("tag_on", [True, False])
def test_the_call_start_row_asks_for_the_tag_only_when_it_is_on(
        tmp_path, decision_env, monkeypatch, tag_on):  # noqa: F811 - fixture
    """The voice rules forbid stage directions, so without this line she drops
    the tag on the call (1/9 tagged against 9/9 with it)."""
    monkeypatch.setenv("HARNESS_FACE_TAG", "1" if tag_on else "0")
    store = make_store(tmp_path, "call-rule.db")
    profile = _persona()
    store.save_persona(profile)
    client = SmallChunks(responses=["reply 0.\n" + _verdicts(True), _verdicts(True),
                                    "Took you long enough."])
    session = make_session(store, clock=VirtualClock(t_h=9.6), client=client,
                           seed=SEED, persona_profile=profile)
    try:
        session.on_message("hey")
        assert session.incoming_call("cid").call_answered
        assert (CALL_FACE_RULE in _call_start_row(store)) is tag_on
    finally:
        store.close()


def test_chat_history_shows_her_text_and_her_face(tmp_path):
    from observability.chat import history

    client = FakeClient(responses=["[laugh] Ha! No."])
    store, session = _session(tmp_path, client)
    try:
        session.on_message("marry me")
        path = store.path if hasattr(store, "path") else None
    finally:
        store.close()
    conn = sqlite3.connect(path or str(tmp_path / "faces.db"))
    try:
        rows = history(conn, None)["messages"]
    finally:
        conn.close()
    her = [r for r in rows if r["who"] == "her"]
    assert her[-1]["text"] == "Ha! No." and her[-1]["face"] == "laugh"
    assert [r["face"] for r in rows if r["who"] == "him"] == [None]


def test_chat_history_gives_each_paragraph_its_face(tmp_path):
    from observability.chat import history

    client = FakeClient(responses=["[angry] Off.\n\n[sad] Don't go."])
    store, session = _session(tmp_path, client)
    try:
        session.on_message("bye")
    finally:
        store.close()
    conn = sqlite3.connect(str(tmp_path / "faces.db"))
    try:
        [her] = [r for r in history(conn, None)["messages"] if r["who"] == "her"]
    finally:
        conn.close()
    assert her["text"] == "Off.\n\nDon't go."
    assert her["face"] == "angry" and her["faces"] == ["angry", "sad"]
