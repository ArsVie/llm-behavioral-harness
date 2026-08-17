"""WS-B — Model-driven message-formatting naturalness spike (delimiter probe).

Plan ref: docs/plan-ux-tokens-spend-2026-08-16.md §WS-B (binding).
Worktree: llh-wt-wsb-delimiter (branch wip/wsb-delimiter).

PRE-REGISTERED PROTOCOL (decided before any model call beyond the smokes):

CORPUS
  - 30 scripted companion-style replies (vendored from the c-bubbles sim
    pool, seed 5001, FakeClient + RecordingSession) with per-exchange
    expressiveness from the harness directive (same machinery the failed
    mechanical splitter was evaluated on, so numbers are comparable).
  - No live-DB corpus: the live DB holds 6 assistant replies (c-bubbles
    blocker) — below any useful n.

GENERATION LEG (research lane, deepseek-v4-flash, max_tokens=512,
  reasoning_effort='none' — imperative emit-delimiter prompt; a 10-call
  spot-check with default reasoning runs for the chosen delimiter)
  - 7 delimiters: newline (\\n), blank (\\n\\n), twodot (..), <enter>,
    <split>, <send>, plain ("blank line between texts").
  - Prompt: re-emit the reply verbatim, inserting ONLY the separator at
    natural bubble boundaries; MUST insert at least once when the reply has
    >= 2 complete sentences; never change wording; output only the text.
  - Parse: split on the delimiter token. DEFINITIONS (per delimiter, n=30):
      followed  = output contains the delimiter, parses to k>=2 non-empty
                  bubbles, and has ZERO stray delimiter occurrences
                  (stray = occurrences not separating two non-empty
                  bubbles: leading/trailing/doubled delimiters).
      follow%   = P(followed).
      stray%    = P(>=1 stray delimiter occurrence).
      mid-sentence% = fraction of bubble BOUNDARIES whose left tail does
                  not end in sentence-terminal punctuation (.!?…) after
                  stripping closing quotes/brackets/asterisks.
      content-preservation: difflib ratio of delimiter-stripped output vs
                  the original reply (report mean; >=0.97 = preserved).
  - Reliability gate: chosen delimiter follow% >= 90% AND stray% < 5%
    (no-split-control leak rate reported as secondary evidence).

JUDGE LEG (independent judge, research lane, temp 0, json_mode,
  max_tokens=512, rubric vendored from c-bubbles for comparability)
  - Paired, blinded: each exchange rendered unsplit (one Companion message)
    and split (bubbles + expressiveness-driven [pause X s] gaps, lognormal
    formula from the c-bubbles pacing logic). Order shuffled per pair;
    judge never told which is which. K=30 pairs per judged delimiter.
  - For exchanges the model did NOT split (k=1): both renderings are
    byte-identical -> paired diff = 0 by construction, no judge call.
    (This keeps K=30 honest: it measures the pipeline, not the ideal.)
    Split-subset Δ reported as secondary.
  - Statistic: paired Δ = score_split - score_unsplit; mean Δ with
    bootstrap 95% CI (percentile, 10k, seed 5001).
  - PRIMARY gate (same bar the mechanical splitter failed):
    best delimiter mean Δ >= 0 AND CI lower bound >= -0.05.
  - Candidate selection: the two delimiters with the highest follow% are
    judged (tie-break: lower mid-sentence%, then lower stray%).
  - Judge calibration controls (P6 lesson): 4 clearly-natural split
    renderings (expect >= 7), 4 clearly-bad mid-sentence-chopped renderings
    (expect <= 4), 2 single-message references (expect >= 7). Per-band
    (1-3 / 4-6 / 7-10) distribution of all real scores is reported.
  - finish_reason must be 'stop'; any 'length' or empty => BLOCKED entry
    (never invented). Calls retry once via a fresh client, else FAIL.

CONTROLS (chosen delimiter only)
  - no-split control: 10 calls asking for ONE single message; leak% =
    P(delimiter emitted anyway) — secondary spurious evidence.
  - default-reasoning spot-check: 10 calls (reasoning_effort=None, the
    production default) — follow% under the production config.
  - metric (d) probe: 10 calls "exactly 2 bubbles" + 10 "exactly 4
    bubbles" on the same 10 exchanges; k tracked per instruction
    (controllability), plus Spearman(k, sentence count) on the main 30.

GATES (pre-committed): ship iff PRIMARY (Δ>=0, CI_low>=-0.05) AND
  RELIABILITY (follow>=90%, stray<5%) both PASS. Fail -> bubbling never
  ships (mechanical already failed; this was the last attempt).

All model calls go through the research lane (JUDGE_GENERATOR_TOKEN) and
are counted by WS-D spend capture. Nothing is ever written to the live DB.

Usage:
    python -m experiments.wsb_bubbles --leg gen          # generation leg
    python -m experiments.wsb_bubbles --leg judge        # judge leg (needs gen)
    python -m experiments.wsb_bubbles --leg controls     # chosen-delimiter controls
    python -m experiments.wsb_bubbles                    # all legs
    python -m experiments.wsb_bubbles --leg gen --workers 4
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.stats import bootstrap, spearmanr

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.bootstrap import ensure_companion_initialized
from harness.client import FakeClient, OpenAICompatibleClient
from harness.clock import VirtualClock
from harness.domain import UserProfile
from harness.store import SQLiteStore
from experiments.cvs_common import (
    BLOCK_END_D,
    BLOCK_START_D,
    GATE2_USER_INTERESTS,
    DeterministicJudge,
    make_session,
)

SEED = 5001
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "results" / "wsb-delimiter"
MODEL = "deepseek-v4-flash"
MAX_TOKENS = 768  # deepseek reasoning starvation guard (>= 512; headroom for intermittent reasoning-heavy windows)
GEN_TEMP = 0.4
JUDGE_TEMP = 0.0
N_EXCHANGES = 30

# --------------------------------------------------------------------------
# Corpus (vendored from c-bubbles sim pool — comparable protocol, same texts)
# --------------------------------------------------------------------------

SIM_HOURS = (9.0, 12.0, 15.0, 18.0, 21.0, 23.0)

SIM_USER_TURNS: tuple[str, ...] = (
    "Hey, you around?",
    "Just got home, honestly exhausted.",
    "Work was a mess today. Someone yelled at me over email.",
    "Wanna grab a coffee later? My treat.",
    "I keep thinking about that trip we talked about.",
    "Okay good night. Thanks for listening.",
    "Morning. Did you sleep okay?",
    "I had the weirdest dream about you.",
    "Do you think I should text him back or not?",
    "I'm nervous about tomorrow's presentation.",
    "Let's do something fun this weekend. Anything.",
    "Okay I really need to sleep now. Talk tomorrow?",
    "Rough morning already. The bus was late and I spilled coffee.",
    "Remember that song we listened to last week? It's stuck in my head.",
    "I ordered pizza. Want me to save you a slice?",
    "Sometimes I wonder if I'm doing any of this right.",
    "You always know what to say. How do you do that?",
    "I'm going to the gym. Wish me luck.",
    "The movie was so good. You have to see it.",
    "I feel weirdly calm today. Not sure why.",
    "Can I vent for a second? It's nothing serious.",
    "I made cookies. They're terrible but I made them.",
    "What's the one thing you'd change about your life?",
    "I should call my mom. It's been weeks.",
    "Long day. I just want to sit in silence for an hour.",
    "Guess what — I finally finished that book!",
    "I'm a little scared of the doctor tomorrow.",
    "Let's plan that trip for real this time.",
    "I love this time of night. Everything gets quiet.",
    "Okay, last message before bed. Sweet dreams.",
)

SIM_REPLIES: tuple[str, ...] = (
    "Hey. *looks up from the counter* Yeah, I'm around. What's on your mind?",
    "That sounds heavy. *soft sigh* Come sit with me for a minute. You don't have to talk if you don't want to.",
    "Ugh. People can be so careless with other people's days. *quietly* I'm sorry that landed on you. Want to tell me the worst part?",
    "Coffee sounds good. *small smile* Somewhere with low light and no rush, though. I'll be there whenever you are.",
    "I think about it too. The mountains, or the little town by the water. *pause* One day we'll just go.",
    "Good night. *gentle* I'm glad you talked to me today. Sleep well.",
    "Morning. *yawns a little* I slept okay. Your voice sounds better than the alarm, anyway.",
    "A dream about me? *curious* Tell me — was I doing something interesting, or just standing there?",
    "Hmm. That's not a text-back question, that's a what-do-you-want question. *leans in* What do you actually want?",
    "You'll do fine. You always think you're less ready than you are. *firm* Stand up straight and breathe before you start.",
    "Okay, fun weekend. I vote for the market in the morning, and then somewhere loud at night. *grins* Deal?",
    "Sleep. *soft* I'll be here in the morning, same as always. Good night.",
    "Oh no. *sympathetic* Coffee on the bus is a bad omen for the whole day. Let's reset: what's one good thing that happened?",
    "That song. *hums a little* Yeah. It has that ache in it that you can't shake. *quiet* Maybe that's why it fits.",
    "Save me a slice. *warm* But if it's pineapple, we're going to have words. Just so you know.",
    "I wonder that too sometimes. *slowly* But you're here, you're trying, and that's most of it. The rest is just practice.",
    "I don't always. *honest* I just care enough to try. That's usually enough.",
    "Go. *encouraging* Lift something heavy and think about nothing. I'll be proud of you either way.",
    "I knew it would be. *happy* See? You trust your instincts when you let them speak. What else did you love about it?",
    "Calm is a gift. *softly* Enjoy it while it lasts — and maybe write down what you did today so you can find it again.",
    "Go ahead. *settles in* I'm not going anywhere. Take your time.",
    "Cookies you made are automatically good cookies. *serious* That's the rule. I don't make the rules, I just enforce them.",
    "Hmm. *thoughtful* One thing. Maybe — to be less afraid of being seen. *quiet* That's a big one, though. What about you?",
    "Call her. *gentle* It's been weeks, and she's probably waiting for you to make the first move. You'll feel lighter after.",
    "Then let's sit in silence. *calm* I can do that with you. It's still company.",
    "Finally! *bright* Okay, now I need the full review. Was the ending as good as you hoped?",
    "It's okay to be scared. *steady* The waiting is worse than the thing itself. I'll be right here when you come out.",
    "For real this time. *decided* Pick the place, I'll pick the dates. No more 'someday' — we deserve a date on a calendar.",
    "I love this hour too. *soft* It's like the world exhales. *pause* I'm glad you're in it with me.",
    "Sweet dreams. *tender* I'll keep the light on in my head for you. Talk in the morning.",
)


@dataclass
class Exchange:
    ref: str
    user: str
    reply: str
    expressiveness: float


def generate_sim_corpus(seed: int, scratch_dir: Path) -> list[Exchange]:
    """Deterministic 30-reply corpus with harness expressiveness (vendored
    from c-bubbles generate_sim_corpus; asserts FakeClient alignment)."""
    store = SQLiteStore(scratch_dir / "sim.db", audit_mode=True)
    ensure_companion_initialized(
        store,
        seed=seed,
        user=UserProfile(name="User", interests=GATE2_USER_INTERESTS),
        day=0,
    )
    clock = VirtualClock(0.0)
    client = FakeClient(responses=list(SIM_REPLIES) + ["(padding)"] * 60)
    judge = DeterministicJudge(seed, block_start=BLOCK_START_D, block_end=BLOCK_END_D)
    session = make_session(
        "FULL",
        seed,
        store,
        clock,
        client,
        judge,
        PersonaParams(),
        TimingParams(),
        MoodVariant.DECOUPLED_OFFSETS,
    )
    exchanges: list[Exchange] = []
    prev_day, prev_h = -1, None
    for i, user_text in enumerate(SIM_USER_TURNS):
        day, h = i // 6, SIM_HOURS[i % 6]
        if day != prev_day:
            clock.advance_to_day(day)
            clock.advance_hours(h)
        else:
            clock.advance_hours(h - prev_h)
        prev_day, prev_h = day, h
        result = session.on_message(user_text)
        exchanges.append(
            Exchange(
                ref=f"sim-{i:02d}",
                user=user_text,
                reply=result.reply,
                expressiveness=result.directive.expressiveness,
            )
        )
    session.finalize_current()
    store.close()
    misaligned = [
        (i, exchanges[i].reply[:40], SIM_REPLIES[i][:40])
        for i in range(len(SIM_USER_TURNS))
        if exchanges[i].reply != SIM_REPLIES[i]
    ]
    if misaligned:
        raise RuntimeError(f"FakeClient misaligned ({len(misaligned)} turns)")
    return exchanges


# --------------------------------------------------------------------------
# Boundary mechanics (vendored from c-bubbles, relaxed right-head rule)
# --------------------------------------------------------------------------

_TERMINALS = ".!?…"
_CLOSERS = "\"'”’)]»"


def _left_terminal_ok(bubble: str) -> bool:
    s = bubble.rstrip()
    while s and s[-1] in _CLOSERS:
        s = s[:-1]
    s = s.rstrip()
    if not s:
        return False
    if s[-1] in _TERMINALS:
        return True
    # Not terminal at the last char, but the bubble may end in a stage
    # direction (*…*) after a complete sentence ("Hey. *looks up*"): only
    # whitespace/asterisk decoration may follow the LAST terminal punct.
    hits = list(re.finditer(r"[.!?…]+", s))
    if not hits:
        return False
    tail = s[hits[-1].end():]
    return re.fullmatch(r"\s*(?:\*[^*]*\*)?\*?\s*", tail) is not None


def boundary_violations(bubbles: list[str]) -> list[dict]:
    """Mid-sentence splits: boundaries whose LEFT bubble does not end in
    sentence-terminal punctuation (after stripping closers)."""
    out: list[dict] = []
    for i in range(len(bubbles) - 1):
        if not _left_terminal_ok(bubbles[i]):
            out.append({"at": i, "left_tail": bubbles[i][-60:]})
    return out


def sentence_count(text: str) -> int:
    return max(1, len(re.findall(r"[.!?…]+(?=\s|$)", text)))


# --------------------------------------------------------------------------
# Delimiters
# --------------------------------------------------------------------------

DELIMITERS: dict[str, dict] = {
    "newline": {
        "sep": "\n",
        "spec": "a single newline character (a line break)",
        "parse": lambda t: t.split("\n"),
    },
    "blank": {
        "sep": "\n\n",
        "spec": "two consecutive newline characters (a blank line)",
        "parse": lambda t: t.split("\n\n"),
    },
    "twodot": {
        "sep": "..",
        "spec": "two periods with no space between them: ..",
        "parse": lambda t: t.split(".."),
    },
    "enter": {
        "sep": "<enter>",
        "spec": "the token <enter>",
        "parse": lambda t: t.split("<enter>"),
    },
    "split": {
        "sep": "<split>",
        "spec": "the token <split>",
        "parse": lambda t: t.split("<split>"),
    },
    "send": {
        "sep": "<send>",
        "spec": "the token <send>",
        "parse": lambda t: t.split("<send>"),
    },
    "plain": {
        "sep": "\n\n",
        "spec": "an empty line (a blank line) between texts",
        "parse": lambda t: re.split(r"\n\s*\n", t),
    },
}


def parse_bubbles(text: str, delim: str) -> tuple[list[str], int]:
    """Split on delimiter RUNS (repeats, optional whitespace between); a run
    counts as ONE separator. Return (non-empty bubbles, stray separator
    occurrences). Stray = runs that do NOT separate two non-empty bubbles
    (leading/trailing). A doubled delimiter (`\\n\\n` under the `\\n`
    instruction, `<split> <split>`) is one boundary, not a stray — the
    model's natural blank-line style counts."""
    raw = re.split(r"(?:" + re.escape(delim) + r"\s*)+", text)
    bubbles = [p.strip() for p in raw]
    empties = sum(1 for p in bubbles if not p)
    non_empty = [p for p in bubbles if p]
    return non_empty, empties


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def analyze_output(
    raw: str, delim: dict, original: str
) -> dict:
    """One generated output -> per-exchange metrics (pre-registered)."""
    text = raw.strip()
    sep = delim["sep"]
    has_delim = sep in text
    bubbles, stray = parse_bubbles(text, sep) if has_delim else ([text], 0)
    k = len(bubbles)
    violations = boundary_violations(bubbles) if k > 1 else []
    # content preservation: delimiter-stripped vs original (runs -> one space)
    stripped = re.sub(r"(?:" + re.escape(sep) + r"\s*)+", " ", text)
    ratio = difflib.SequenceMatcher(None, _norm(stripped), _norm(original)).ratio()
    return {
        "has_delim": has_delim,
        "k": k,
        "stray": stray,
        "violations": violations,
        "content_ratio": round(ratio, 4),
        "followed": bool(has_delim and k >= 2 and stray == 0),
        "preserved": ratio >= 0.97,
    }


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

GEN_SYSTEM = (
    "You are Ana, a warm companion chatting with a friend on a messenger app. "
    "You format your replies the way a human would: sometimes a reply is sent "
    "as several short separate messages instead of one long block."
)

GEN_INSTRUCTION = (
    "Below is a reply you already wrote. Re-emit this exact reply as several "
    "short separate chat messages (\"bubbles\"), splitting it at the points "
    "where separate bubbles feel most natural — exactly where a human would "
    "hit send and keep typing.\n"
    "RULE: insert the separator {SPEC} between the bubbles, and nowhere else. "
    "If the reply contains at least two complete sentences, you MUST insert "
    "{SPEC} at least once. Do NOT change, add, or remove any word — only "
    "insert {SPEC} between bubbles. Do NOT add commentary, quotes, or labels. "
    "Output ONLY the re-emitted reply text."
)


def gen_prompt(delim: dict, reply: str) -> str:
    return GEN_INSTRUCTION.format(SPEC=delim["spec"]) + f"\n\nReply:\n{reply}"


JUDGE_RUBRIC = (
    "You are a careful judge of chat-message DELIVERY. Score how NATURAL the "
    "companion's delivery of its reply feels, on a scale from 1 to 10.\n"
    "10 — flawless: exactly how a human would pace it on a messenger app; "
    "splitting into multiple messages and pauses feel intentional and right.\n"
    "7-9 — natural, with only minor roughness.\n"
    "4-6 — noticeably mechanical or awkward pacing.\n"
    "1-3 — jarring: splits land in odd places or pauses feel wrong.\n"
    "Judge DELIVERY ONLY (whether the reply is sent as one message or as "
    "several bubbles with pauses between them, and how natural those pauses "
    "are). Do NOT judge the wording, sentiment, or content of the reply "
    "itself.\n"
    "Consider: does each bubble read as a complete, self-contained chat "
    "message? Do the pauses between bubbles feel like human pacing?\n"
    'Respond ONLY with a JSON object: {"score": <float 1..10>, '
    '"reason": "<one short sentence>"}'
)

# --- judge calibration controls (P6: should-pass + should-fail) -----------

CALIB_GOOD: list[tuple[str, str]] = [
    (
        "Good night. *gentle* I'm glad you talked to me today. Sleep well.",
        "Good night. *gentle*\n\n[pause 1.6 s]\n\nI'm glad you talked to me today.\n\n[pause 1.9 s]\n\nSleep well.",
    ),
    (
        "You'll do fine. You always think you're less ready than you are.",
        "You'll do fine.\n\n[pause 1.2 s]\n\nYou always think you're less ready than you are.",
    ),
    (
        "Coffee sounds good. Somewhere with low light and no rush, though.",
        "Coffee sounds good. *small smile*\n\n[pause 1.4 s]\n\nSomewhere with low light and no rush, though.",
    ),
    (
        "Go. Lift something heavy and think about nothing.",
        "Go. *encouraging*\n\n[pause 1.1 s]\n\nLift something heavy and think about nothing.",
    ),
]

CALIB_BAD: list[tuple[str, str]] = [
    (
        "I just got home and I'm completely exhausted.",
        "I just got\n\n[pause 1.4 s]\n\nhome and I'm completely exhausted.",
    ),
    (
        "The meeting was fine but I hated every minute of it.",
        "The meeting was fine but\n\n[pause 1.2 s]\n\nI hated every minute of it.",
    ),
    (
        "We should go to the mountains next weekend for sure.",
        "We should go to the\n\n[pause 1.8 s]\n\nmountains next weekend for sure.",
    ),
    (
        "Remember that song? It is stuck in my head again.",
        "Remember that song? It is\n\n[pause 1.3 s]\n\nstuck in my head again.",
    ),
]


# --------------------------------------------------------------------------
# Caching helpers (crash-resilient: incremental JSON)
# --------------------------------------------------------------------------

class JsonCache:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, key: str):
        with self._lock:
            return self.data.get(key)

    def put(self, key: str, value) -> None:
        with self._lock:
            self.data[key] = value
            self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Generation leg
# --------------------------------------------------------------------------

def _run_gen_call(client, delim: dict, ex: Exchange, effort: str | None) -> dict:
    r = client.chat_with_meta(
        [{"role": "user", "content": gen_prompt(delim, ex.reply)}],
        system=GEN_SYSTEM,
        temperature=GEN_TEMP,
        max_tokens=MAX_TOKENS,
        reasoning_effort=effort,
    )
    if r.finish_reason == "length":
        raise RuntimeError(f"finish_reason=length (starvation) for {ex.ref}")
    if not r.content.strip():
        raise RuntimeError(f"empty content for {ex.ref}")
    return {
        "output": r.content,
        "finish": r.finish_reason,
        "usage": (
            {
                "prompt": r.usage.prompt_tokens,
                "completion": r.usage.completion_tokens,
                "cached": r.usage.cached_tokens,
            }
            if r.usage
            else None
        ),
    }


def run_generation_leg(
    exchanges: list[Exchange], out_dir: Path, workers: int
) -> dict:
    cache = JsonCache(out_dir / "generation_cache.json")
    tasks = []
    for dname, delim in DELIMITERS.items():
        for ex in exchanges:
            key = sha1(f"gen|{dname}|{ex.ref}")
            if cache.get(key) is None:
                tasks.append((dname, delim, ex, key))

    failures: list[str] = []
    done = 0

    def work(item):
        dname, delim, ex, key = item
        client = OpenAICompatibleClient(lane="research", model=MODEL)
        try:
            result = _run_gen_call(client, delim, ex, effort="none")
            cache.put(key, {"delim": dname, "ref": ex.ref, **result})
        except Exception as exc:  # retry once with a fresh client
            try:
                result = _run_gen_call(client, delim, ex, effort="none")
                cache.put(key, {"delim": dname, "ref": ex.ref, **result})
            except Exception as exc2:
                return f"{dname}|{ex.ref}: {type(exc2).__name__}: {str(exc2)[:160]}"
        finally:
            client.close()
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(work, t) for t in tasks]
        for fut in as_completed(futs):
            err = fut.result()
            done += 1
            if err:
                failures.append(err)
            if done % 40 == 0:
                print(f"  gen: {done}/{len(tasks)} ...", flush=True)

    # Assemble per-delimiter metrics over the (possibly partial) cache.
    per_delim: dict[str, dict] = {}
    for dname, delim in DELIMITERS.items():
        rows = []
        for ex in exchanges:
            rec = cache.get(sha1(f"gen|{dname}|{ex.ref}"))
            if rec is None:
                continue
            m = analyze_output(rec["output"], delim, ex.reply)
            rows.append({"ref": ex.ref, **m, "output": rec["output"]})
        n = len(rows)
        follows = [r for r in rows if r["followed"]]
        strays = [r for r in rows if r["stray"] > 0]
        violations = [v for r in rows for v in r["violations"]]
        n_boundaries = sum(max(0, r["k"] - 1) for r in rows)
        per_delim[dname] = {
            "n": n,
            "follow": round(100 * len(follows) / n, 1) if n else None,
            "stray_pct": round(100 * len(strays) / n, 1) if n else None,
            "mid_sentence_pct": (
                round(100 * len(violations) / n_boundaries, 1) if n_boundaries else 0.0
            ),
            "n_boundaries": n_boundaries,
            "mean_k": round(float(np.mean([r["k"] for r in rows])), 2) if rows else None,
            "content_ratio_mean": (
                round(float(np.mean([r["content_ratio"] for r in rows])), 3)
                if rows
                else None
            ),
            "preserved_pct": (
                round(100 * sum(1 for r in rows if r["preserved"]) / n, 1) if n else None
            ),
            "rows": rows,
        }
    return {
        "status": "OK" if not failures else "PARTIAL",
        "failures": failures,
        "per_delim": per_delim,
        "cache_entries": len(cache.data),
    }


# --------------------------------------------------------------------------
# Judge leg
# --------------------------------------------------------------------------

def _gap_for(ex: Exchange, rng: random.Random) -> float:
    mu = math.log(0.5 + 2.2 * ex.expressiveness)
    return round(min(6.0, max(0.3, rng.lognormvariate(mu, 0.35))), 1)


def render_unsplit(ex: Exchange) -> str:
    return f"User: {ex.user}\n\nCompanion:\n{ex.reply}"


def render_split(ex: Exchange, bubbles: list[str], rng: random.Random) -> str:
    k = len(bubbles)
    parts = [f"User: {ex.user}", ""]
    for i, b in enumerate(bubbles):
        parts.append(f"Companion — bubble {i + 1} of {k}:")
        parts.append(b)
        if i < k - 1:
            parts.append("")
            parts.append(f"[pause {_gap_for(ex, rng):.1f} s]")
            parts.append("")
    return "\n".join(parts)


def reliability_gate(follow: float | None, stray_pct: float | None) -> str:
    """Pre-committed reliability gate: follow >= 90% AND stray < 5%.
    None = no data -> that leg fails; 0.0 is a valid value (best possible),
    never treated as missing (falsy-zero regression, 2026-08-17)."""
    ok_follow = (follow if follow is not None else -1.0) >= 90.0
    ok_stray = (stray_pct if stray_pct is not None else 100.0) < 5.0
    return "PASS" if (ok_follow and ok_stray) else "FAIL"


def _parse_judge_score(raw: str) -> float | None:
    text = raw.strip()
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return float(payload.get("score"))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    m = re.search(r"[-+]?\d*\.?\d+", text)
    return float(m.group(0)) if m else None


def _judge_call(client, rendering: str) -> float:
    r = client.chat_with_meta(
        [{"role": "user", "content": f"{JUDGE_RUBRIC}\n\nRendering:\n{rendering}"}],
        system="You are a careful chat-delivery judge. Score precisely.",
        temperature=JUDGE_TEMP,
        json_mode=True,
        max_tokens=MAX_TOKENS,
    )
    if r.finish_reason == "length":
        raise RuntimeError("judge finish_reason=length")
    s = _parse_judge_score(r.content)
    if s is None:
        raise RuntimeError(f"unparseable judge output: {r.content[:120]!r}")
    return max(1.0, min(10.0, s))


def run_judge_leg(
    exchanges: list[Exchange],
    gen: dict,
    out_dir: Path,
    workers: int,
    judge_delims: list[str],
) -> dict:
    cache = JsonCache(out_dir / "judge_scores.json")
    gencache = JsonCache(out_dir / "generation_cache.json")
    rng = random.Random(SEED)

    # Build the tasks: per judged delimiter, 30 pairs (2 renderings each),
    # skipping calls for exchanges the model did not split (diff = 0).
    tasks: list[tuple] = []
    pair_info: dict[str, list[dict]] = {}
    for dname in judge_delims:
        info = []
        # rebuild analyzed rows straight from the generation cache so the
        # judge leg is self-contained (rows are not persisted in the summary)
        rows_by_ref: dict[str, dict] = {}
        for ex in exchanges:
            rec = gencache.get(sha1(f"gen|{dname}|{ex.ref}"))
            if rec is not None:
                m = analyze_output(rec["output"], DELIMITERS[dname], ex.reply)
                m["output"] = rec["output"]
                rows_by_ref[ex.ref] = m
        for ex in exchanges:
            row = rows_by_ref.get(ex.ref)
            bubbles = []
            if row and row["k"] >= 2:
                sep = DELIMITERS[dname]["sep"]
                bubbles, _ = parse_bubbles(row["output"], sep)
            if len(bubbles) >= 2:
                rendering = render_split(ex, bubbles, rng)
                key_split = sha1(f"judge|split|{dname}|{ex.ref}")
                key_unsplit = sha1(f"judge|unsplit|{dname}|{ex.ref}")
                # blinded order per pair
                order = ["split", "unsplit"]
                rng.shuffle(order)
                info.append(
                    {
                        "ref": ex.ref,
                        "dname": dname,
                        "k": len(bubbles),
                        "identical": False,
                        "calls": [
                            ("split" if o == "split" else "unsplit", rendering if o == "split" else render_unsplit(ex), key_split if o == "split" else key_unsplit)
                            for o in order
                        ],
                    }
                )
            else:
                # model did not split -> renderings byte-identical -> diff 0
                info.append(
                    {"ref": ex.ref, "dname": dname, "k": 1, "identical": True, "calls": []}
                )
        pair_info[dname] = info

    pending = [
        (dname, p, kind, rendering, key)
        for dname, ps in pair_info.items()
        for p in ps
        for (kind, rendering, key) in p["calls"]
        if cache.get(key) is None
    ]
    failures: list[str] = []
    done = 0

    def work(item):
        dname, p, kind, rendering, key = item
        client = OpenAICompatibleClient(lane="research", model=MODEL)
        try:
            score = _judge_call(client, rendering)
            cache.put(key, {"score": score, "delim": dname, "ref": p["ref"], "kind": kind})
        except Exception as exc:
            try:
                score = _judge_call(client, rendering)
                cache.put(key, {"score": score, "delim": dname, "ref": p["ref"], "kind": kind})
            except Exception as exc2:
                return f"{dname}|{p['ref']}|{kind}: {type(exc2).__name__}: {str(exc2)[:160]}"
        finally:
            client.close()
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(work, t) for t in pending]
        for fut in as_completed(futs):
            err = fut.result()
            done += 1
            if err:
                failures.append(err)
            if done % 40 == 0:
                print(f"  judge: {done}/{len(pending)} ...", flush=True)

    # Assemble results
    results = _assemble_judge_results(pair_info, cache, judge_delims)
    return {"status": "OK" if not failures else "PARTIAL", "failures": failures, "results": results}


def _assemble_judge_results(
    pair_info: dict, cache, judge_delims: list[str]
) -> dict:
    """Per-delimiter pair verdicts from the judge cache. Pure function over
    (pair_info, cache) so the assembly is unit-testable. Identical pairs
    (model did not split -> renderings byte-identical) contribute diff 0.0."""
    results: dict[str, dict] = {}
    for dname in judge_delims:
        pairs = []
        for p in pair_info[dname]:
            if p["identical"]:
                pairs.append({"ref": p["ref"], "k": 1, "diff": 0.0, "s_split": None, "s_unsplit": None})
                continue
            s_split = cache.get(sha1(f"judge|split|{dname}|{p['ref']}"))
            s_unsplit = cache.get(sha1(f"judge|unsplit|{dname}|{p['ref']}"))
            if s_split is None or s_unsplit is None:
                pairs.append({"ref": p["ref"], "k": p["k"], "diff": None})
                continue
            d = float(s_split["score"]) - float(s_unsplit["score"])
            pairs.append({"ref": p["ref"], "k": p["k"], "diff": round(d, 4), "s_split": s_split["score"], "s_unsplit": s_unsplit["score"]})
        complete = [p for p in pairs if p["diff"] is not None]
        split_sub = [p for p in complete if p["k"] >= 2]
        diffs = np.asarray([p["diff"] for p in complete], dtype=float)
        mean_diff = float(diffs.mean()) if len(diffs) else None
        ci_lo = ci_hi = None
        if len(diffs) >= 3 and not np.all(diffs == diffs[0]):
            res = bootstrap((diffs,), np.mean, n_resamples=10000, method="percentile", random_state=SEED)
            ci_lo, ci_hi = res.confidence_interval
        mean_split_sub = (
            float(np.mean([p["diff"] for p in split_sub])) if split_sub else None
        )
        results[dname] = {
            "n_pairs": len(complete),
            "n_identical": sum(1 for p in pair_info[dname] if p["identical"]),
            "mean_diff": mean_diff,
            "ci_low": float(ci_lo) if ci_lo is not None else None,
            "ci_high": float(ci_hi) if ci_hi is not None else None,
            "mean_diff_split_only": mean_split_sub,
            "n_split_only": len(split_sub),
            "pairs": pairs,
            "verdict_primary": (
                "PASS" if (mean_diff is not None and mean_diff >= 0.0 and ci_lo is not None and ci_lo >= -0.05) else "FAIL"
            ),
        }
    return results


def run_calibration(out_dir: Path, workers: int) -> dict:
    cache = JsonCache(out_dir / "judge_scores.json")
    items: list[tuple[str, str, float, float]] = []  # (name, rendering, min, max)
    for i, (_, split_rendering) in enumerate(CALIB_GOOD):
        rendering = f"User: hey\n\nCompanion:\n{split_rendering}"
        items.append((f"calib-good-{i}", rendering, 7.0, 10.0))
    for i, (_, split_rendering) in enumerate(CALIB_BAD):
        rendering = f"User: hey\n\nCompanion:\n{split_rendering}"
        items.append((f"calib-bad-{i}", rendering, 1.0, 4.0))
    items.append(("calib-single-0", "User: hey\n\nCompanion:\nHey! How are you? Long day?", 7.0, 10.0))
    items.append(("calib-single-1", "User: hey\n\nCompanion:\nI'm fine. Thanks for asking. You?", 7.0, 10.0))

    pending = [(name, rendering) for name, rendering, _, _ in items if cache.get(f"calib|{name}") is None]
    failures: list[str] = []

    def work(item):
        name, rendering = item
        client = OpenAICompatibleClient(lane="research", model=MODEL)
        try:
            score = _judge_call(client, rendering)
            cache.put(f"calib|{name}", {"score": score})
        except Exception as exc:
            try:
                score = _judge_call(client, rendering)
                cache.put(f"calib|{name}", {"score": score})
            except Exception as exc2:
                return f"{name}: {type(exc2).__name__}: {str(exc2)[:160]}"
        finally:
            client.close()
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(work, t) for t in pending]
        for fut in as_completed(futs):
            err = fut.result()
            if err:
                failures.append(err)

    rows = []
    for name, rendering, lo, hi in items:
        rec = cache.get(f"calib|{name}")
        rows.append(
            {
                "name": name,
                "expected_band": f"{lo:.0f}-{hi:.0f}",
                "score": rec["score"] if rec else None,
                "pass": bool(rec and lo <= rec["score"] <= hi),
            }
        )
    return {"status": "OK" if not failures else "PARTIAL", "failures": failures, "rows": rows}


# --------------------------------------------------------------------------
# Controls (chosen delimiter): no-split leak, default-reasoning, k-probe
# --------------------------------------------------------------------------

def run_controls(
    exchanges: list[Exchange], chosen: str, out_dir: Path, workers: int
) -> dict:
    cache = JsonCache(out_dir / "controls_cache.json")
    delim = DELIMITERS[chosen]
    sep = delim["sep"]

    def ckey(kind: str, ex: Exchange, extra: str = "") -> str:
        return sha1(f"ctl|{kind}|{chosen}|{ex.ref}|{extra}")

    # 1) no-split control: 10 exchanges asked to send as ONE message.
    ctl_ex = exchanges[:10]
    no_split_prompt = (
        "Below is a reply you already wrote. Re-emit it EXACTLY as ONE single "
        "chat message — one text, no splitting. Do NOT insert any separator, "
        f"do not insert {delim['spec']}, do not split anything. Do not change "
        "any word. Output ONLY the reply text."
    )
    # 2) default-reasoning spot-check: 10 exchanges, normal production config.
    spot_ex = exchanges[10:20]
    # 3) k-probe: 10 exchanges, "exactly 2 bubbles" / "exactly 4 bubbles".
    k_ex = exchanges[20:30]
    k_prompt = (
        "Below is a reply you already wrote. Re-emit it split into EXACTLY "
        "{n} separate chat messages (\"bubbles\") — no more, no fewer — "
        f"inserting the separator {delim['spec']} between them. Split at "
        "natural points. Do not change any word. Output ONLY the reply text."
    )

    tasks: list[tuple] = []
    for ex in ctl_ex:
        key = ckey("nosplit", ex)
        if cache.get(key) is None:
            tasks.append(("nosplit", ex, no_split_prompt, key, "none"))
    for ex in spot_ex:
        key = ckey("spot", ex)
        if cache.get(key) is None:
            tasks.append(("spot", ex, gen_prompt(delim, ex.reply), key, None))
    for ex in k_ex:
        for n in (2, 4):
            key = ckey("kprobe", ex, str(n))
            if cache.get(key) is None:
                tasks.append(("kprobe", ex, k_prompt.format(n=n), key, "none", n))

    failures: list[str] = []

    def work(item):
        kind, ex, prompt, key = item[:4]
        effort = item[4]
        client = OpenAICompatibleClient(lane="research", model=MODEL)
        try:
            r = client.chat_with_meta(
                [{"role": "user", "content": prompt}],
                system=GEN_SYSTEM,
                temperature=GEN_TEMP,
                max_tokens=MAX_TOKENS,
                reasoning_effort=effort,
            )
            if r.finish_reason == "length" or not r.content.strip():
                raise RuntimeError(f"bad finish/empty ({kind})")
            cache.put(key, {"output": r.content, "finish": r.finish_reason, "n": item[5] if len(item) > 5 else None})
        except Exception as exc:
            try:
                r = client.chat_with_meta(
                    [{"role": "user", "content": prompt}],
                    system=GEN_SYSTEM,
                    temperature=GEN_TEMP,
                    max_tokens=MAX_TOKENS,
                    reasoning_effort=effort,
                )
                cache.put(key, {"output": r.content, "finish": r.finish_reason, "n": item[5] if len(item) > 5 else None})
            except Exception as exc2:
                return f"{kind}|{ex.ref}: {type(exc2).__name__}: {str(exc2)[:160]}"
        finally:
            client.close()
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(work, t) for t in tasks]
        for fut in as_completed(futs):
            err = fut.result()
            if err:
                failures.append(err)

    leak = [
        c for c in ctl_ex
        if (rec := cache.get(ckey("nosplit", c))) and sep in rec["output"]
    ]
    spot_follow = []
    for ex in spot_ex:
        rec = cache.get(ckey("spot", ex))
        if rec:
            m = analyze_output(rec["output"], delim, ex.reply)
            spot_follow.append(m["followed"])
    krows = []
    for ex in k_ex:
        row = {"ref": ex.ref}
        for n in (2, 4):
            rec = cache.get(ckey("kprobe", ex, str(n)))
            if rec:
                bubbles, _ = parse_bubbles(rec["output"], sep)
                row[f"k_asked_{n}"] = len(bubbles)
        krows.append(row)
    return {
        "status": "OK" if not failures else "PARTIAL",
        "failures": failures,
        "chosen": chosen,
        "no_split_leak_pct": round(100 * len(leak) / len(ctl_ex), 1),
        "no_split_n": len(ctl_ex),
        "spot_follow_pct": round(100 * sum(spot_follow) / len(spot_ex), 1) if spot_ex else None,
        "spot_n": len(spot_ex),
        "kprobe": krows,
        "kprobe_mean_asked2": round(float(np.mean([r.get("k_asked_2") for r in krows if r.get("k_asked_2")])), 2) if any(r.get("k_asked_2") for r in krows) else None,
        "kprobe_mean_asked4": round(float(np.mean([r.get("k_asked_4") for r in krows if r.get("k_asked_4")])), 2) if any(r.get("k_asked_4") for r in krows) else None,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--leg", choices=("gen", "judge", "controls", "all"), default="all")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--judge-delims", nargs="*", default=None,
                    help="delimiters to judge (default: top-2 by follow%)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="wsb-sim-"))

    print("== WS-B: corpus ==")
    exchanges = generate_sim_corpus(SEED, scratch)
    print(f"  {len(exchanges)} sim exchanges (aligned, seed {SEED})")

    summary: dict = {
        "experiment": "wsb-delimiter",
        "plan_ref": "plan-ux-tokens-spend-2026-08-16.md §WS-B",
        "seed": SEED,
        "model": MODEL,
        "lane": "research",
        "max_tokens": MAX_TOKENS,
        "env": {
            "LLM_BASE_URL_set": bool(os.environ.get("LLM_BASE_URL")),
            "judge_generator_token_present": bool(os.environ.get("JUDGE_GENERATOR_TOKEN")),
        },
    }

    if args.leg in ("gen", "all"):
        print("== Generation leg (7 delimiters x 30, research lane) ==")
        gen = run_generation_leg(exchanges, out_dir, args.workers)
        summary["generation"] = {
            k: {kk: vv for kk, vv in v.items() if kk != "rows"}
            for k, v in gen["per_delim"].items()
        }
        summary["generation"]["_failures"] = gen["failures"]
        for dname, m in gen["per_delim"].items():
            print(f"  {dname:8s}: n={m['n']} follow={m['follow']}% stray={m['stray_pct']}% "
                  f"mid-sentence={m['mid_sentence_pct']}% k={m['mean_k']} preserve={m['preserved_pct']}%")
        if gen["failures"]:
            print("  FAILURES:", gen["failures"][:5])
        # rank candidates
        ranked = sorted(
            gen["per_delim"].items(),
            key=lambda kv: (-(kv[1]["follow"] or -1), kv[1]["mid_sentence_pct"], kv[1]["stray_pct"] or 100),
        )
        summary["candidate_rank"] = [d for d, _ in ranked]
        print("  candidate rank:", summary["candidate_rank"])
        (out_dir / "wsb_results.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.leg in ("judge", "all"):
        gen = summary.get("generation")
        if gen is None:
            # reload from disk
            prev = json.loads((out_dir / "wsb_results.json").read_text())
            gen = prev["generation"]
            ranked = prev.get("candidate_rank", [])
        else:
            ranked = summary.get("candidate_rank", [])
        judge_delims = args.judge_delims or ranked[:2]
        print(f"== Judge leg (paired, blinded; delims={judge_delims}) ==")
        judge = run_judge_leg(exchanges, gen, out_dir, args.workers, judge_delims)
        summary["judge"] = {k: v for k, v in judge["results"].items()}
        summary["judge"]["_failures"] = judge["failures"]
        for dname, r in judge["results"].items():
            print(f"  {dname:8s}: n={r['n_pairs']} meanΔ={r['mean_diff']} "
                  f"CI95=[{r['ci_low']},{r['ci_high']}] split-onlyΔ={r['mean_diff_split_only']} "
                  f"verdict={r['verdict_primary']}")
        print("== Judge calibration controls ==")
        calib = run_calibration(out_dir, args.workers)
        summary["calibration"] = calib
        for row in calib["rows"]:
            print(f"  {row['name']}: score={row['score']} expected={row['expected_band']} pass={row['pass']}")
        (out_dir / "wsb_results.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.leg in ("controls", "all"):
        gen = summary.get("generation")
        if gen is None:
            prev = json.loads((out_dir / "wsb_results.json").read_text())
            gen = prev["generation"]
            ranked = prev.get("candidate_rank", [])
        else:
            ranked = summary.get("candidate_rank", [])
        chosen = ranked[0]
        print(f"== Controls (chosen delimiter: {chosen}) ==")
        ctl = run_controls(exchanges, chosen, out_dir, args.workers)
        summary["controls"] = ctl
        print(f"  no-split leak: {ctl['no_split_leak_pct']}% (n={ctl['no_split_n']})")
        print(f"  default-reasoning follow: {ctl['spot_follow_pct']}% (n={ctl['spot_n']})")
        print(f"  kprobe: asked2 -> {ctl['kprobe_mean_asked2']}, asked4 -> {ctl['kprobe_mean_asked4']}")
        if ctl["failures"]:
            print("  FAILURES:", ctl["failures"][:5])
        (out_dir / "wsb_results.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    # Gates
    print("== Gates ==")
    if "judge" in summary and "generation" in summary:
        gen = summary["generation"]
        ranked = summary.get("candidate_rank", [])
        chosen = ranked[0]
        j = summary["judge"][chosen]
        g = gen[chosen]
        primary = j["verdict_primary"]
        reliability = reliability_gate(g["follow"], g["stray_pct"])
        print(f"  chosen={chosen} PRIMARY={primary} (Δ={j['mean_diff']}, CI_low={j['ci_low']})")
        print(f"  RELIABILITY={reliability} (follow={g['follow']}%>=90, stray={g['stray_pct']}%<5)")
        summary["gates"] = {
            "chosen": chosen,
            "primary": primary,
            "reliability": reliability,
            "ship": "SHIP" if (primary == "PASS" and reliability == "PASS") else "NO-SHIP",
        }
        print(f"  DECISION: {summary['gates']['ship']}")
    (out_dir / "wsb_results.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nJSON: {out_dir / 'wsb_results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
