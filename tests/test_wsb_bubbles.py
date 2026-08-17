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
    assert stray == 0  # a doubled delimiter is ONE separator run, not a stray


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


def test_newline_delimiter_blank_line_run():
    # the model emits \\n\\n (blank lines) under the \\n instruction; a run of
    # newlines is ONE separator -> followed, no stray
    m = analyze_output("First part.\n\nSecond part.", DELIMITERS["newline"], "First part. Second part.")
    assert m["followed"] is True
    assert m["k"] == 2
    assert m["stray"] == 0


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


def test_reliability_gate_zero_stray_passes():
    # falsy-zero regression: stray_pct=0.0 must not be treated as missing
    from experiments.wsb_bubbles import reliability_gate
    assert reliability_gate(100.0, 0.0) == "PASS"
    assert reliability_gate(90.0, 4.9) == "PASS"
    assert reliability_gate(89.9, 0.0) == "FAIL"
    assert reliability_gate(100.0, 5.0) == "FAIL"
    assert reliability_gate(None, 0.0) == "FAIL"    # no data -> fail
    assert reliability_gate(100.0, None) == "FAIL"


def test_judge_assembly_counts_identical_and_complete():
    # regression: n_identical crashed on the assembled pairs list (no
    # 'identical' key there) — must count from pair_info; identical pairs
    # contribute diff 0.0 to n_pairs/mean
    from experiments.wsb_bubbles import _assemble_judge_results, sha1

    class _FakeCache:
        def __init__(self, data):
            self.data = data
        def get(self, key):
            return self.data.get(key)

    k_split = sha1("judge|split|newline|r1")
    k_unsplit = sha1("judge|unsplit|newline|r1")
    pair_info = {
        "newline": [
            {"ref": "r1", "dname": "newline", "k": 3, "identical": False,
             "calls": [("split", "x", k_split), ("unsplit", "y", k_unsplit)]},
            {"ref": "r2", "dname": "newline", "k": 1, "identical": True, "calls": []},
        ]
    }
    cache = _FakeCache({k_split: {"score": 8.0}, k_unsplit: {"score": 6.0}})
    res = _assemble_judge_results(pair_info, cache, ["newline"])
    r = res["newline"]
    assert r["n_identical"] == 1
    assert r["n_pairs"] == 2  # identical pair contributes diff 0.0 -> complete
    assert r["mean_diff"] == 1.0  # (8-6 + 0) / 2
    assert r["n_split_only"] == 1
    assert r["mean_diff_split_only"] == 2.0
    assert r["verdict_primary"] == "FAIL"  # n=2 -> no CI -> not passable
