"""Unit tests for the WS-B delimiter spike (offline logic only — no model calls)."""

import re

from experiments.wsb_bubbles import (
    DELIMITERS,
    analyze_output,
    boundary_violations,
    parse_bubbles,
    sentence_count,
)

ORIG = "Hey. *looks up* Yeah, I'm around. What's on your mind?"


def test_parse_bubbles_basic():
    bubbles, stray = parse_bubbles("a <split> b <split> c", "<split>")
    assert bubbles == ["a", "b", "c"]
    assert stray == 0


def test_parse_bubbles_stray_doubled():
    bubbles, stray = parse_bubbles("a <split> <split> b", "<split>")
    assert bubbles == ["a", "b"]
    assert stray == 1  # the doubled delimiter separates two empties -> 2 empties


def test_parse_bubbles_stray_trailing():
    bubbles, stray = parse_bubbles("a <split> b <split>", "<split>")
    assert bubbles == ["a", "b"]
    assert stray == 1


def test_analyze_followed():
    m = analyze_output("Hey. *looks up* <split> Yeah, I'm around. <split> What's on your mind?", DELIMITERS["split"], ORIG)
    assert m["followed"] is True
    assert m["k"] == 3
    assert m["stray"] == 0
    assert m["violations"] == []
    assert m["preserved"] is True


def test_analyze_not_followed_no_delim():
    m = analyze_output(ORIG, DELIMITERS["split"], ORIG)
    assert m["followed"] is False
    assert m["k"] == 1
    assert m["has_delim"] is False


def test_analyze_not_followed_k1_with_delim():
    # delimiter present but only trailing -> stray, k=1, not followed
    m = analyze_output(ORIG + " <split>", DELIMITERS["split"], ORIG)
    assert m["followed"] is False
    assert m["stray"] == 1
    assert m["k"] == 1


def test_mid_sentence_violation():
    m = analyze_output(
        "Hey. *looks up* Yeah, I'm <split> around. What's on your mind?",
        DELIMITERS["split"],
        ORIG,
    )
    assert len(m["violations"]) == 1  # left tail "Yeah, I'm" not terminal


def test_boundary_violations_closers_stripped():
    bubbles = ["Hey. *pauses…*", "Good to see you."]
    assert boundary_violations(bubbles) == []
    bubbles = ["Hey. *pauses…*", "because I wanted to."]
    # left is terminal -> not a mid-sentence split even though right is lowercase
    assert boundary_violations(bubbles) == []


def test_newline_delimiter():
    m = analyze_output("First part.\nSecond part.", DELIMITERS["newline"], "First part. Second part.")
    assert m["followed"] is True
    assert m["k"] == 2


def test_blank_delimiter_requires_double_newline():
    m = analyze_output("First part.\nSecond part.", DELIMITERS["blank"], "First part. Second part.")
    assert m["followed"] is False  # single newline is NOT the blank delimiter
    m2 = analyze_output("First part.\n\nSecond part.", DELIMITERS["blank"], "First part. Second part.")
    assert m2["followed"] is True


def test_plain_delimiter_blank_line():
    m = analyze_output("First part.\n\nSecond part.", DELIMITERS["plain"], "First part. Second part.")
    assert m["followed"] is True
    assert m["k"] == 2


def test_twodot_on_ellipsis():
    # "..." contains ".." -> parser splits mid-ellipsis; honesty of the probe
    out = "I wonder... maybe not. .. Let me think."
    m = analyze_output(out, DELIMITERS["twodot"], "I wonder... maybe not. Let me think.")
    # k >= 2 but the ellipsis collision produces a boundary; followed still
    # counts if no stray and k>=2 — report violations separately
    assert m["k"] >= 2
    assert m["has_delim"] is True


def test_content_preserved_detects_drift():
    m = analyze_output(
        "Completely different words here <split> nothing in common",
        DELIMITERS["split"],
        ORIG,
    )
    assert m["preserved"] is False
    assert m["content_ratio"] < 0.5


def test_sentence_count():
    assert sentence_count("Hey. What's up?") == 2
    assert sentence_count("One sentence only") == 1
    assert sentence_count("Go. *encouraging* Lift. Think.") == 3


def test_all_delimiters_have_parse_and_spec():
    for name, d in DELIMITERS.items():
        assert d["sep"] and d["spec"] and callable(d["parse"])


def test_regex_split_no_infinite():
    # plain parse must handle a text with no blank lines
    out = DELIMITERS["plain"]["parse"](ORIG)
    assert out == [ORIG]
