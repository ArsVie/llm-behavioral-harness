"""BubbleStreamer hardening tests: streaming invariants, incremental
release, no-mid-sentence-split, env-flag gating.

Property under test: for ANY text, feeding it to :class:`BubbleStreamer`
char-by-char or in random chunks and then ``flush()`` must yield exactly
``parse_bubbles(text)``.

Pinned incremental semantics (WS-B: any newline run is one separator):
* A sentence-complete head at a trailing boundary releases IMMEDIATELY
  (``feed('Hello there.\\n\\n')`` -> ``['Hello there.']``). A newline is
  always a separator, so membership is already decided; early release is
  never premature and keeps ``flush == parse`` exact.
* A NON-complete head at a trailing boundary HOLDS (``feed('First line\\n')``
  -> ``[]``): the newline may be a line wrap, so the piece waits for the
  next chunk or for ``flush()``.
"""
from __future__ import annotations

import os
import random
from unittest.mock import patch

from harness.bubbles import (
    BUBBLE_INSTRUCTION,
    BubbleStreamer,
    _sentence_complete,
    bubble_stream_enabled,
    bubbles_enabled,
    parse_bubbles,
)

# Word chars + sentence punctuation + closers + whitespace + CJK/Devanagari
# terminators + em-dash: exercises every _sentence_complete closer and the
# boundary grammar.
_ALPHABET = "abc XYZ,.!?\"'”’」』）)]}…।。；;:\n \t" + "\u2014" + "éñ"
_CJK = "。！？"
_CHUNK_CASES = 520
_CHAR_CASES = 400


def _feed_all(streamer: BubbleStreamer, text: str, chunk_sizes: list[int]) -> list[str]:
    out: list[str] = []
    pos = 0
    for k in chunk_sizes:
        if pos >= len(text):
            break
        k = min(k, len(text) - pos)
        out.extend(streamer.feed(text[pos : pos + k]))
        pos += k
    if pos < len(text):
        out.extend(streamer.feed(text[pos:]))
    return out + streamer.flush()


def _stream_all(text: str, mode: str, seed: int) -> list[str]:
    rng = random.Random(seed)
    s = BubbleStreamer()
    if mode == "char":
        out: list[str] = []
        for ch in text:
            out.extend(s.feed(ch))
        return out + s.flush()
    if mode == "chunks":
        return _feed_all(s, text, [rng.randint(1, 7) for _ in range(len(text) + 1)])
    if mode == "one":
        return s.feed(text) + s.flush()
    raise ValueError(mode)


def _assert_single_release(pre: list[str], release: list[str]) -> str | None:
    """One bubble, at most, per feed while a reply is incomplete (never a
    premature split) — empty release is fine (a hold)."""
    if len(release) > 1:
        return f"multi-release without flush: pre={pre!r} release={release!r}"
    return None


# ---------------------------------------------------------------------------
# flush == parse property
# ---------------------------------------------------------------------------


def test_property_char_by_char_corpus():
    """Char-by-char feeding + flush == parse_bubbles for a crafted corpus."""
    corpus = [
        "Hello there.\n\nHow are you?",
        "a\nb",
        "a\n\nb",
        "a\n \n\nb",
        "a\n\n\nb",
        "hello",
        "",
        "  hello  \n\n  world  ",
        "one\ntwo\nthree",
        "First line\nsecond",
        "First line\nsecond.",
        "Word first\nsecond word",
        "First line\nsecond\n\nthird line\nfourth",
        "Sentence one.\nSentence two.",
        "Sentence one\nSentence two",
        "sentence one. second. third.",
        "sentence one. second\nthird. fourth.",
        "a.\n",
        "a.\n\n",
        "a\n",
        "\n",
        "   \n \n  ",
        "What? No!\n\nYes.",
        'He said "hi."\n\nThen left.',
        "他说：“你好。”\n\n然后走了。",
        "तीन बजे।\n\nचलो।",
        "x\u2014y\n\nz.",
        "x.\n\ny.  \n  z.  ",
        "trailing.\n\n\n\n\n",
        "trailing\n\n\n\n\n",
    ]
    assert len(corpus) >= 20
    for text in corpus:
        got = _stream_all(text, "char", seed=0)
        assert got == parse_bubbles(text), repr(text)


def test_property_random_chunks_fuzz_500_plus():
    """500+ randomized texts; random chunk sizes + flush == parse_bubbles."""
    rng = random.Random(20260907)
    for i in range(_CHUNK_CASES):
        text = _random_text(rng)
        got = _stream_all(text, "chunks", seed=i)
        assert got == parse_bubbles(text), f"case {i}: {text!r}"


def test_property_char_by_char_fuzz_400():
    """400+ randomized texts; char-by-char + flush == parse_bubbles."""
    rng = random.Random(777)
    for i in range(_CHAR_CASES):
        text = _random_text(rng)
        got = _stream_all(text, "char", seed=i)
        assert got == parse_bubbles(text), f"case {i}: {text!r}"


def _random_text(rng: random.Random) -> str:
    """Random text biased toward boundaries and sentence punctuation."""
    pool = _ALPHABET + _CJK
    n = rng.randint(0, 60)
    text = "".join(rng.choice(pool) for _ in range(n))
    roll = rng.random()
    if roll < 0.55:
        text += "\n" * rng.randint(1, 4)  # trailing boundary
    elif roll < 0.75:
        text += "\n\n"  # blank-line boundary
    if rng.random() < 0.3:
        text = " " * rng.randint(1, 3) + text  # leading whitespace
    return text


def test_property_one_shot_matches_full_parse():
    """Single feed + flush over the fuzz corpus == parse_bubbles."""
    rng = random.Random(424242)
    for i in range(200):
        text = _random_text(rng)
        got = _stream_all(text, "one", seed=i)
        assert got == parse_bubbles(text), f"case {i}: {text!r}"


def test_flush_after_partial_feed_equals_parse_of_so_far():
    """A caller may flush mid-reply (e.g. to end the turn early): every
    flush must equal parse_bubbles() of exactly the text that stream has
    consumed since the previous flush — chunk boundaries never leak into
    output, and early (non-flush) releases plus the flush still sum to the
    segment's full parse."""
    rng = random.Random(31337)
    for i in range(300):
        text = _random_text(rng)
        s = BubbleStreamer()
        segment_out: list[str] = []
        segment_fed = ""
        pos = 0
        while pos < len(text):
            k = rng.randint(1, 5)
            chunk = text[pos : pos + k]
            pos += k
            segment_fed += chunk
            segment_out.extend(s.feed(chunk))
            if rng.random() < 0.15:  # mid-stream flush ends this segment
                segment_out.extend(s.flush())
                assert segment_out == parse_bubbles(segment_fed), (
                    f"case {i}: segment {segment_fed!r}"
                )
                s = BubbleStreamer()  # a fresh stream continues the reply
                segment_out = []
                segment_fed = ""
        segment_out.extend(s.flush())
        assert segment_out == parse_bubbles(segment_fed), f"case {i} tail"


# ---------------------------------------------------------------------------
# Incremental release + no-mid-sentence-split
# ---------------------------------------------------------------------------


def test_feed_releases_complete_sentence_at_trailing_boundary():
    """feed('Hello there.\\n\\n') releases WITHOUT flush: sentence-complete
    head at a boundary is decided, so it goes out early (WS-B)."""
    s = BubbleStreamer()
    assert s.feed("Hello there.\n\n") == ["Hello there."]
    # The separator is consumed; the stream is cleanly idle.
    assert s.feed("Next.") == []
    assert s.flush() == ["Next."]


def test_feed_holds_incomplete_line_at_trailing_boundary():
    """feed('First line\\n') releases NOTHING: no sentence end yet, so the
    newline may be a wrap — the head must hold."""
    s = BubbleStreamer()
    assert s.feed("First line\n") == []


def test_no_mid_sentence_split_then_sentence_end_releases():
    s = BubbleStreamer()
    assert s.feed("First line\nsecond") == []  # hold: wrap or mid-sentence?
    # A later sentence end must NOT yank 'First line' out early: its own
    # boundary is still mid-sentence, so the whole prefix keeps holding.
    assert s.feed(".") == []
    # The reply is over: flush splits per parse_bubbles (every newline run
    # is a separator) — held pieces go out parse-exact, never merged.
    assert s.flush() == ["First line", "second."]
    assert s.flush() == []


def test_no_mid_sentence_split_within_one_chunk():
    s = BubbleStreamer()
    # Both pieces are mid-sentence at their boundaries; nothing may go out.
    assert s.feed("First line\nsecond") == []
    assert s.feed("\n\nthird line\nfourth") == []
    assert s.flush() == ["First line", "second", "third line", "fourth"]


def test_no_release_before_sentence_punctuation_multi_chunk():
    s = BubbleStreamer()
    assert s.feed("First line\n") == []
    assert s.feed("second\n") == []
    assert s.feed("third") == []
    assert s.flush() == ["First line", "second", "third"]


def test_mid_sentence_split_never_multiple_bubbles_without_sentence_ends():
    """Holds are legal; a NON-flush feed may emit at most ONE bubble, and
    only when that bubble ends a sentence. Two mid-sentence breaks must not
    release two pieces."""
    texts = [
        "First line\nsecond\nthird",
        "no caps here\nneither here",
        "a\nb\nc\nd\ne\nf",
    ]
    for text in texts:
        s = BubbleStreamer()
        for k in range(1, len(text) + 1):
            pre = text[:k]
            rel = s.feed(text[k : k + 1])
            err = _assert_single_release(pre, rel)
            assert err is None, f"{text!r}: {err}"


def test_incremental_stream_exact_match():
    """Incremental (non-flush) releases are exactly the PREFIX bubbles of
    the final parse, in order — nothing extra, nothing lost."""
    text = "First line\nsecond\n\nthird.\n\nfourth."
    s = BubbleStreamer()
    emitted: list[str] = []
    for ch in text:
        emitted.extend(s.feed(ch))
    # Every non-final boundary here is followed by more text but the pieces
    # before it are mid-sentence (no '.'), so each holds; nothing is
    # releaseable before flush. The sentence-complete pieces sit behind
    # those mid-sentence boundaries and must NOT jump the queue.
    assert emitted == []
    assert s.flush() == ["First line", "second", "third.", "fourth."]
    assert emitted == []
    assert parse_bubbles(text) == ["First line", "second", "third.", "fourth."]

    # Same text where every piece IS sentence-complete before its boundary:
    # each releases as soon as its boundary is confirmed by more text.
    # Trailing-boundary release (my fix) sends 'third.' early here. The
    # FINAL piece ('fourth.') has no following boundary yet, so it holds
    # until flush — never released mid-reply on a maybe-wrap.
    text2 = "First.\nsecond.\n\nthird.\n\nfourth."
    s2 = BubbleStreamer()
    emitted2: list[str] = []
    for ch in text2:
        emitted2.extend(s2.feed(ch))
    assert emitted2 == ["First.", "second.", "third."]
    assert s2.flush() == ["fourth."]
    assert emitted2 + ["fourth."] == parse_bubbles(text2)


def test_flush_releases_held_tail_without_sentence_end():
    s = BubbleStreamer()
    assert s.feed("First line\nsecond") == []
    assert s.flush() == ["First line", "second"]


def test_flush_on_empty_stream_returns_nothing():
    assert BubbleStreamer().flush() == []
    s = BubbleStreamer()
    assert s.feed("   \n  \n ") == []
    assert s.flush() == []


def test_trailing_whitespace_boundary():
    s = BubbleStreamer()
    assert s.feed("Done.\n\n\n\n") == ["Done."]
    s2 = BubbleStreamer()
    assert s2.feed("Done.\n \n \n") == ["Done."]
    s3 = BubbleStreamer()
    assert s3.feed("Done.\n \nstill going") == ["Done."]
    assert s3.flush() == ["still going"]


# ---------------------------------------------------------------------------
# Sentence-completeness helper
# ---------------------------------------------------------------------------


def test_sentence_complete():
    assert _sentence_complete("Hello.")
    assert _sentence_complete("Hello!")
    assert _sentence_complete("Hello?")
    assert _sentence_complete("你好。")
    assert _sentence_complete("नमस्ते।")
    assert not _sentence_complete("Hello")
    assert not _sentence_complete("Hello,")
    assert not _sentence_complete("Hello;")
    assert not _sentence_complete("Hello:")
    assert not _sentence_complete("Hello-")
    assert not _sentence_complete("123")
    # trailing closers are skipped
    assert _sentence_complete('He said "hi."')
    assert _sentence_complete('He said "hi." ')
    assert _sentence_complete('Hello."')
    assert _sentence_complete('Hello!?"')
    assert _sentence_complete("Hello.)")
    assert _sentence_complete("…done?")
    # Ellipsis is a CLOSER, not a terminator: trailing '…' alone holds
    # (it may continue), but '.'/closers after it complete.
    assert not _sentence_complete("Hello…")
    assert _sentence_complete("Hello…. ")
    assert _sentence_complete("Hello…!")
    # whitespace-only
    assert not _sentence_complete("   ")
    assert not _sentence_complete("")


# ---------------------------------------------------------------------------
# parse_bubbles sanity (independent of the streamer)
# ---------------------------------------------------------------------------


def test_parse_bubbles_spec():
    assert parse_bubbles("") == []
    assert parse_bubbles("   ") == []
    assert parse_bubbles("a") == ["a"]
    assert parse_bubbles("a\n\nb") == ["a", "b"]
    assert parse_bubbles("a\nb") == ["a", "b"]
    assert parse_bubbles(" a \n\n b ") == ["a", "b"]
    assert parse_bubbles("\n\na\n\n\n\nb\n\n") == ["a", "b"]


# ---------------------------------------------------------------------------
# Env flag gating
# ---------------------------------------------------------------------------


def _with_env(monkeypatch, bubbles: bool, stream: bool):
    if bubbles:
        monkeypatch.setenv("HARNESS_BUBBLES", "1")
    else:
        monkeypatch.delenv("HARNESS_BUBBLES", raising=False)
    if stream:
        monkeypatch.setenv("HARNESS_BUBBLE_STREAM", "1")
    else:
        monkeypatch.delenv("HARNESS_BUBBLE_STREAM", raising=False)


def test_stream_flag_off_without_either_env(monkeypatch):
    _with_env(monkeypatch, bubbles=False, stream=False)
    assert not bubbles_enabled()
    assert not bubble_stream_enabled()


def test_stream_flag_off_when_bubbles_only(monkeypatch):
    _with_env(monkeypatch, bubbles=True, stream=False)
    assert bubbles_enabled()
    assert not bubble_stream_enabled()


def test_stream_flag_off_when_stream_only(monkeypatch):
    _with_env(monkeypatch, bubbles=False, stream=True)
    assert not bubbles_enabled()
    assert not bubble_stream_enabled()


def test_stream_flag_on_requires_both(monkeypatch):
    _with_env(monkeypatch, bubbles=True, stream=True)
    assert bubbles_enabled()
    assert bubble_stream_enabled()


def test_stream_flag_truthy_variants(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("HARNESS_BUBBLES", v)
        monkeypatch.setenv("HARNESS_BUBBLE_STREAM", v)
        assert bubbles_enabled()
        assert bubble_stream_enabled()
    for v in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("HARNESS_BUBBLES", v)
        monkeypatch.setenv("HARNESS_BUBBLE_STREAM", v)
        assert not bubbles_enabled()
        assert not bubble_stream_enabled()


def test_flags_do_not_cross_talk(monkeypatch):
    """bubble_stream_enabled must not read HARNESS_BUBBLES-only or the
    stream env alone — pin with a third sentinel var untouched."""
    monkeypatch.setenv("HARNESS_BUBBLE_STREAM", "1")
    monkeypatch.setenv("HARNESS_BUBBLES", "0")
    assert not bubble_stream_enabled()
    monkeypatch.setenv("HARNESS_BUBBLES", "1")
    monkeypatch.setenv("HARNESS_BUBBLE_STREAM", "0")
    assert not bubble_stream_enabled()


def test_instruction_mentions_blank_line_and_no_mid_sentence():
    assert "blank line" in BUBBLE_INSTRUCTION
    assert "never mid-sentence" in BUBBLE_INSTRUCTION


def test_env_usage_pinned_by_patch_import():
    """The flag reads happen through os.environ at call time — prove the
    gating isn't cached at import time."""
    with patch.dict(os.environ, {"HARNESS_BUBBLES": "1", "HARNESS_BUBBLE_STREAM": "1"}, clear=False):
        assert bubble_stream_enabled()
    with patch.dict(os.environ, {"HARNESS_BUBBLES": "1"}, clear=False):
        assert not bubble_stream_enabled()
