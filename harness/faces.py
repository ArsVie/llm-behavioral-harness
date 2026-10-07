"""Her face per reply: a ``[name]`` tag at the start, gated by ``HARNESS_FACE_TAG``.

The system prompt asks her to open every reply with the face she makes while
saying it, ``[smug] Oh, so...``. The tag is kept in her stored row (she writes
it reliably only once her history carries tags: 25/25 against 4/25 without,
amadeus/expression/README.md, E3/E4) and stripped at every channel. On the call
it arrives before the first sentence (tag placement B, 2026-09-29: valid 25/25,
known at token 5, 84% the same face as her end-of-reply pick).

A reply without a tag is tagged after the fact by one continuation of the same
request, ``<reply>\\n\\nSprite:`` as an assistant prefix, on the turn's cached
prefix: the E1 method (0.89-0.92 against labelled lines). The first known name
it writes is the face; the pick is not capped, because a reasoning model spends
a small cap on thinking and returns nothing (live, 2026-09-30: 4 tokens gave 0
chars twice; uncapped it wrote ~76). See ``Session._face_for_untagged``.

Modifier tags may follow the face tag: ``[smug][blush]`` raises the blush,
``[blush:off]`` clears it (``[blush:light]`` explicit; bare ``[blush]`` is
strong). A modifier changes a layer, not the face; ``split_face_mods``
returns them and every stripper removes them too.
"""

from __future__ import annotations

import re

from harness.env import env_bool

#: The 19 taggable faces of her sprite set (amadeus sprites/runs/krea-v4s1),
#: each described by what the drawn face shows and then the feeling it
#: carries. Rewritten 2026-10-03 from the images: the earlier rubrics named
#: situations ("scolding him", "pretending not to care", "overwhelmed by
#: romance"), so on the run before the 2026-10-03 reset 65 of 109 tags went
#: to the two full-blush faces, and smile, laugh, worried, surprised and
#: crying were never used. `aroused` was added 2026-10-04 with its drawn face:
#: the first intimate scene had no wanting face, so 20 of 33 tags went to
#: flustered. Six mood faces (soft, annoyed, caught, sleepy, afterglow,
#: cocky) joined 2026-10-04.
FACES: tuple[tuple[str, str], ...] = (
    ("smug", "half-lidded eyes and a small closed smile: pleased with herself, teasing"),
    ("neutral", "level eyes and a flat mouth: plain, matter-of-fact"),
    ("smile", "open eyes and a soft closed smile: warm, gentle, content"),
    ("laugh", "eyes squeezed shut and mouth wide open: laughing out loud"),
    ("sad", "lowered eyes and a small frown: hurt or down, no tears"),
    ("crying", "eyes brimming over and a wobbling mouth: in tears"),
    ("angry", "a glare, knitted brows and gritted teeth: really furious"),
    ("pout", "puffed cheeks and a sideways look: sulking, childishly annoyed"),
    ("worried", "raised inner brows and parted lips: uneasy, anxious"),
    ("surprised", "wide eyes and a small open mouth: caught off guard"),
    ("embarrassed", "a deep red blush, sweat drops and a wavy mouth: caught out, mortified"),
    ("flustered", "spiral eyes and a burning red face: so overwhelmed she can't think"),
    ("aroused", "heavy-lidded, hazy eyes and softly parted lips over a deep blush: wanting him, breathless"),
    ("soft", "open, relaxed eyes and a small tender smile: warm and comfortable with him"),
    ("annoyed", "half-lidded unimpressed eyes, a small flat frown and puffed cheeks: mildly annoyed, not angry"),
    ("caught", "widened eyes, raised brows and a small pursed mouth: caught out, flustered that he noticed"),
    ("sleepy", "heavy drooping eyelids and a loose relaxed mouth: drowsy and half awake"),
    ("afterglow", "almost-closed eyes and a small satisfied smile over a deep blush: softly spent and content"),
    ("cocky", "narrowed knowing eyes, a confident crooked smirk and one raised eyebrow: sure of herself, pleased with herself"),
)
NAMES: frozenset[str] = frozenset(n for n, _ in FACES)

#: Appended to the system prompt when the tag is on (the wording tested in
#: amadeus/expression/tag_placement.py).
FACE_INSTRUCTION = (
    "Sprite: start every reply with the face you make while saying it, in "
    "brackets, like `[smug]`, then the reply. The tag is stripped before he "
    "sees the message. To change your blush, add a modifier right after it: "
    "`[blush]` for a strong blush, `[blush:off]` to clear it. You can also "
    "change the room around you with `[bg:bedroom]` (bedroom, cafe, "
    "night_city; `[bg:off]` clears it). Names:\n"
    + "\n".join(f"- {n}: {r}" for n, r in FACES)
)

#: The fallback's assistant prefix after her reply.
FALLBACK_PREFIX = "\n\nSprite:"

_LEAD = re.compile(r"^\s*[`*]*\[\s*([A-Za-z]+)\s*(?::\s*([A-Za-z][A-Za-z0-9_-]*)\s*)?\][`*]*[ \t]*\n?[ \t]*")
#: A tag opening any later line: she often gives each paragraph, which goes
#: out as its own message, a face of its own (41 of 60 replies on the live
#: run, 2026-09-30). Modifiers riding a line-opening face tag are cut with
#: it; a line opening with a bare modifier is cut by ``_LINE_MOD``.
_MOD_BODY = r"(?:blush(?::\s*(?:off|light|strong)\s*)?|bg\s*:\s*[A-Za-z][A-Za-z0-9_-]*)"
_MOD_TAIL = rf"(?:\[\s*{_MOD_BODY}\s*\][`*]*[ \t]*)*"
_LINE_TAG = re.compile(r"(?m)^[ \t]*[`*]*\[\s*([A-Za-z]+)\s*\][`*]*[ \t]*" + _MOD_TAIL)
_LINE_MOD = re.compile(rf"(?m)^[ \t]*[`*]*\[\s*{_MOD_BODY}\s*\][`*]*[ \t]*")
_PARAGRAPH = re.compile(r"\n\s*\n")
_NAME = re.compile(r"[A-Za-z]+")


def face_tag_enabled() -> bool:
    return env_bool("HARNESS_FACE_TAG", True)


def with_face_instruction(system: str) -> str:
    return system + "\n\n" + FACE_INSTRUCTION if face_tag_enabled() else system


#: The call's voice rules forbid stage directions and say everything she
#: writes is spoken, so on the call she drops the tag: 1/9 call replies tagged
#: without this line, 9/9 with it, on a copy of the live DB with tagged
#: history (2026-09-30, three 3-turn calls each, amadeus/research §5).
CALL_FACE_RULE = (
    "- Still open every reply with your face tag, like `[smug]`: it is cut "
    "out before the voice engine and never spoken."
)


def with_call_face_rule(row: str) -> str:
    """The call-start row with ``CALL_FACE_RULE`` when the tag is on."""
    return row + "\n" + CALL_FACE_RULE if face_tag_enabled() else row


#: Blush modifier levels (``[blush]`` alone is strong).
BLUSH_LEVELS = ("off", "light", "strong")


def _lead_tags(text: str) -> tuple[str | None, tuple[str, ...], str]:
    """``(face, modifiers, rest)`` from the start of ``text``.

    A run of known tags: modifiers (``[blush]``, ``[blush:off|light|strong]``)
    and at most one face name, possibly separated by spaces. Only known names
    count as faces (``[sigh] ...`` is her text) and anything else stops the
    run and stays in ``rest``."""
    face: str | None = None
    mods: list[str] = []
    rest = text or ""
    while True:
        m = _LEAD.match(rest)
        if not m:
            break
        name = m.group(1).lower()
        value = m.group(2).lower() if m.group(2) else None
        if name in NAMES and face is None:
            face = name
        elif name == "blush" and (value is None or value in BLUSH_LEVELS):
            mods.append(f"blush:{value or 'strong'}")
        elif name == "bg" and value:
            mods.append(f"bg:{value.lower()}")
        else:
            break
        rest = rest[m.end():]
    return face, tuple(mods), rest


def split_face_mods(text: str) -> tuple[str | None, tuple[str, ...], str]:
    """``("smug", ("blush:strong",), "Oh, so...")`` from ``"[smug][blush] Oh, so..."``."""
    return _lead_tags(text)


def mod_blush(mods: tuple[str, ...]) -> str | None:
    """The last blush modifier in a tag run (``"blush:strong"``) or None."""
    return next((m for m in reversed(mods) if m.startswith("blush:")), None)


def mod_scene(mods: tuple[str, ...]) -> str | None:
    """The last scene modifier in a tag run (``"bedroom"``, ``"off"``) or None."""
    return next((m.split(":", 1)[1] for m in reversed(mods) if m.startswith("bg:")), None)


def split_face(text: str) -> tuple[str | None, str]:
    """``("smug", "Oh, so...")`` from ``"[smug] Oh, so..."``.

    Only a known name counts as a tag: ``[sigh] ...`` is her text and stays.
    Without a tag: ``(None, text)`` unchanged. Modifier tags (``[blush]``...)
    are stripped like the face tag; ``split_face_mods`` returns them."""
    face, _mods, rest = _lead_tags(text)
    return face, rest


def with_face(face: str | None, text: str) -> str:
    """The stored form: ``[face] text`` (text alone when there is no face)."""
    return f"[{face}] {text}" if face else text


def strip_face(text: str) -> str:
    """The text any reader outside her own stream should see: her lead tags
    and every tag opening a later line cut out (her row keeps them)."""
    return _LINE_MOD.sub("", _LINE_TAG.sub(
        lambda m: "" if m.group(1).lower() in NAMES else m.group(0),
        _lead_tags(text)[2]))


def paragraph_faces(text: str) -> list[tuple[str | None, str]]:
    """``(face, text)`` per paragraph (blank-line split), each paragraph's
    own tag cut out; an untagged paragraph keeps the face before it."""
    out: list[tuple[str | None, str]] = []
    face: str | None = None
    for part in _PARAGRAPH.split(text or ""):
        own, said = split_face(part)
        face = own or face
        said = strip_face(said).strip()
        if said:
            out.append((face, said))
    return out


def face_from_continuation(text: str) -> str | None:
    """The name a ``Sprite:`` continuation wrote (`` smug``, ``[pout]``...)."""
    m = _NAME.search(text or "")
    name = m.group(0).lower() if m else None
    return name if name in NAMES else None


_MOD_COMPACT = re.compile(
    r"^\s*[`*]*\[\s*(?:blush\s*(?::\s*(off|light|strong)\s*)?"
    r"|bg\s*:\s*([A-Za-z][A-Za-z0-9_-]*))\s*\][`*]*[ \t]*\n?[ \t]*", re.I)


def _mod_at(s: str) -> tuple[str, str] | None:
    """``("blush:strong", rest)`` when a modifier tag opens ``s``."""
    m = _MOD_COMPACT.match(s)
    if not m:
        return None
    if m.group(2):
        return f"bg:{m.group(2).lower()}", s[m.end():]
    level = m.group(1).lower() if m.group(1) else "strong"
    return f"blush:{level}", s[m.end():]


def _mod_could_open(s: str) -> bool:
    """Could ``s`` (so far) still become a compact modifier tag?"""
    t = s.lstrip()
    if not t:
        return True                       # more input may start the tag
    if not t.startswith("["):
        return False
    inner = "".join(t[1:].split()).lower()
    if any(full.startswith(inner) for full in
           ("blush]", "blush:off]", "blush:light]", "blush:strong]")):
        return True
    return "bg:".startswith(inner) or (inner.startswith("bg:")
                                       and re.fullmatch(r"[a-z0-9_-]*", inner[3:]) is not None)


class FaceCut:
    """Cuts the lead tags off a token stream.

    ``feed(piece)`` returns the text to pass on; it holds the stream back while
    it could still be opening a known tag (``[``, ``[sm``, ``[blush:``...).
    ``face`` and ``blush`` are set as the tags close; a stream that opens with
    anything else passes through from its first character."""

    _HOLD = 24      # a tag is ``[embarrassed]``; past this it is not one
    _MOD_HOLD = 40  # face + ``[blush:strong]`` and room for the next tag

    def __init__(self) -> None:
        self.face: str | None = None
        self.blush: str | None = None
        self.scene: str | None = None
        self._buf = ""
        self._done = False
        self._trim = False

    def feed(self, piece: str) -> str:
        if self._done:
            if self._trim:          # the space or newline after a closed tag
                piece = piece.lstrip()
                self._trim = not piece
            return piece
        self._buf += piece
        head = self._buf.lstrip()
        if not head:
            return ""
        if not head.startswith(("[", "`", "*")):
            return self._release(self._buf)
        # a run of known tags: modifiers and at most one face name
        while True:
            m = _LEAD.match(self._buf)
            if m:
                name = m.group(1).lower()
                value = m.group(2).lower() if m.group(2) else None
                if name in NAMES and self.face is None:
                    self.face = name
                elif name == "blush" and (value is None or value in BLUSH_LEVELS):
                    self.blush = f"blush:{value or 'strong'}"
                elif name == "bg" and value:
                    self.scene = f"bg:{value.lower()}"
                else:
                    return self._release(self._buf)  # unknown tag: her text
                self._buf = self._buf[m.end():]
                head = self._buf.lstrip()
                if not head:
                    return ""            # could be more tags, or the text
                if not head.startswith(("[", "`", "*")):
                    return self._release(self._buf)
                continue
            # nothing (more) to match: hold only while it could still open one
            if self.face is None:
                if "]" in head or len(head) > self._HOLD:
                    return self._release(self._buf)  # not a known tag: her text
                return ""
            if _mod_could_open(self._buf) and len(self._buf) <= self._MOD_HOLD:
                return ""
            return self._release(self._buf)

    def flush(self) -> str:
        """What is still held when the stream ends."""
        return "" if self._done else self._release(self._buf)

    def _release(self, text: str) -> str:
        self._done = True
        self._buf = ""
        if self.face:
            text = text.lstrip()
            self._trim = not text
        return text


class FaceEvent:
    """What ``Session.call_turn`` yields, once, when the reply's tags close:
    the call server sends it as ``{"face": name}`` ahead of the sentences
    (``blush`` / ``scene`` ride along when a modifier was given). Anything
    else a call turn yields is a sentence (``str``)."""

    __slots__ = ("face", "blush", "scene")

    def __init__(self, face: str, blush: str | None = None,
                 scene: str | None = None) -> None:
        self.face = face
        self.blush = blush
        self.scene = scene

    def __repr__(self) -> str:
        parts = [repr(self.face)]
        if self.blush:
            parts.append(repr(self.blush))
        if self.scene:
            parts.append(repr(self.scene))
        return f"FaceEvent({', '.join(parts)})"
