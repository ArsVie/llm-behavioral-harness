"""C2 — Splitter offline de burbujas modulado por ánimo (mood-paced multi-bubble).

Experimento OFFLINE (plan advisor-orchestration-2026-08-15, Parte 3, sección C2):
el splitter NO se implementa en el runtime — solo se evalúa sobre corpus:

  - corpus vivo: resultados/live-companion/companion.db (SOLO LECTURA, modo
    ro; nunca se escribe). Extrae los turnos assistant y recomputa la
    directiva con harness.behavior.derive_behavior (canal expressiveness,
    solo lectura).
  - corpus sim: 30 réplicas generadas con FakeClient + RecordingSession
    (experiments.cvs_common.make_session), semilla 5001.
  - splitter: k burbujas ∝ expressiveness; cortes SOLO en límites de oración;
    gaps log-normal con media creciente en expressiveness (0.5 + 2.2·e s).

Criterios de éxito del plan:
  1. CERO splits a mitad de oración (chequeo mecánico, n previsto 114).
  2. naturalidad juzgada (LLM real) del renderizado dividido ≥ baseline sin
     dividir: n=30 pareado, media Δ ≥ 0 y CI 95% no por debajo de −0.05.
  3. Spearman ρ(gap, expressiveness) ≥ 0.5.

Solo añade archivos bajo experiments/ y results/ (límite duro Parte 3);
el DB vivo se abre en modo read-only con URI mode=ro.

Uso:
    .venv/bin/python -m experiments.c2_bubbles [--seed 5001] [--skip-judge]
    [--live-db RUTA] [--out-dir results/c2-bubbles]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.stats import bootstrap, spearmanr

from engine.types import DayRecord, MoodVariant, PersonaParams, TimingParams
from harness.behavior import derive_behavior
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

DEFAULT_LIVE_DB = (
    "/home/vruizes/.hermes/projects/llm-behavioral-harness/"
    "results/live-companion/companion.db"
)
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "results" / "c2-bubbles"

# --------------------------------------------------------------------------
# Corpus sim (30 réplicas) — texto scripteado estilo compañera (Ana), con
# 2-6 oraciones para que el splitter tenga límites reales que elegir.
# --------------------------------------------------------------------------

SIM_HOURS = (9.0, 12.0, 15.0, 18.0, 21.0, 23.0)  # horas locales por día (5 días)

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
    source: str  # "live" | "sim"
    user: str
    reply: str
    expressiveness: float
    t_h: float
    day: int
    ref: str

    @property
    def local_hour(self) -> float:
        return self.t_h % 24.0


@dataclass
class SplitRecord:
    exchange: Exchange
    bubbles: list[str] = field(default_factory=list)
    gaps_s: list[float] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)

    @property
    def k(self) -> int:
        return len(self.bubbles)

    @property
    def mean_gap_s(self) -> float:
        return float(np.mean(self.gaps_s)) if self.gaps_s else 0.0


# --------------------------------------------------------------------------
# Segmentación en oraciones (nunca corta a mitad de oración)
# --------------------------------------------------------------------------

_ABBREV_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|approx|fig|no|vol|e\.g|i\.e|"
    r"U\.S|U\.K|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.$",
    re.IGNORECASE,
)
_TERMINALS = ".!?…"
_CLOSERS = "\"'”’)]»"


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Span de cada oración (start, end) sobre el texto original.

    Solo se cierra oración tras puntuación terminal seguida de espacio/fin,
    protegiendo abreviaturas, decimales y comillas de cierre.
    """
    spans: list[tuple[int, int]] = []
    start, i, n = 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _TERMINALS:
            j = i
            while j < n and text[j] in _TERMINALS:
                j += 1
            while j < n and text[j] in _CLOSERS:
                j += 1
            if ch == "." and _ABBREV_RE.search(text[start:j].rstrip()):
                i = j
                continue
            if j < n and not text[j].isspace():
                i = j  # decimal/pegado → no es fin de oración
                continue
            spans.append((start, j))
            start = j
            i = j
            while i < n and text[i].isspace():
                i += 1
            continue
        i += 1
    if start < n:
        spans.append((start, n))
    return spans


# --------------------------------------------------------------------------
# Splitter conducido por expressiveness
# --------------------------------------------------------------------------

_BUBBLE_TARGET_SLOPE = 0.22  # 1 + floor((e − 0.15)/0.22) → e∈[0.12,0.95] ⇒ k∈[1,4]
_JITTER_PROB = 0.30          # con prob. 0.3 una burbuja extra si hay oraciones
_GAP_BASE_S = 0.5
_GAP_SLOPE_S = 2.2
_GAP_SIGMA = 0.35
_GAP_MIN_S, _GAP_MAX_S = 0.3, 6.0
_MAX_BUBBLES = 4


def split_reply(text: str, expressiveness: float, rng: random.Random) -> tuple[list[str], list[float]]:
    """Divide una réplica en burbujas + gaps (en segundos) modulados por ánimo.

    El número de burbujas y el gap crecen con expressiveness; los cortes solo
    caen entre oraciones (nunca a mitad).
    """
    spans = sentence_spans(text)
    n_sent = len(spans)
    if n_sent <= 1:
        return [text.strip()], []

    target = 1 + int((expressiveness - 0.15) / _BUBBLE_TARGET_SLOPE)
    target = max(1, min(_MAX_BUBBLES, target))
    if rng.random() < _JITTER_PROB and target < _MAX_BUBBLES:
        target += 1
    k = min(target, n_sent)

    cuts = sorted({int(round(n_sent * j / k)) for j in range(1, k)})
    cuts = [c for c in cuts if 0 < c < n_sent]
    if not cuts:
        return [text.strip()], []

    bubbles: list[str] = []
    prev = 0
    for c in cuts:
        bubbles.append(text[spans[prev][0] : spans[c - 1][1]].strip())
        prev = c
    bubbles.append(text[spans[prev][0] : spans[-1][1]].strip())

    gap_mu = math.log(_GAP_BASE_S + _GAP_SLOPE_S * expressiveness)
    gaps = [
        round(min(_GAP_MAX_S, max(_GAP_MIN_S, rng.lognormvariate(gap_mu, _GAP_SIGMA))), 1)
        for _ in range(len(bubbles) - 1)
    ]
    return bubbles, gaps


# --------------------------------------------------------------------------
# Chequeo mecánico independiente (valida las cadenas, no el bookkeeping)
# --------------------------------------------------------------------------

_LEFT_TERMINALS = ".!?…"
_RIGHT_OK_STARTS = set('"\'“”‘([{*—–…')


def _left_terminal_ok(bubble: str) -> bool:
    s = bubble.rstrip()
    while s and s[-1] in _CLOSERS + "*":
        s = s[:-1]
    s = s.rstrip()
    return bool(s) and s[-1] in _LEFT_TERMINALS


def _right_start_ok(bubble: str) -> bool:
    s = bubble.lstrip()
    if not s:
        return False
    ch = s[0]
    return ch.isupper() or ch.isdigit() or ch in _RIGHT_OK_STARTS


def check_boundaries(bubbles: list[str]) -> list[dict]:
    """Devuelve lista (vacía si OK) de cortes a mitad de oración."""
    violations: list[dict] = []
    for i in range(len(bubbles) - 1):
        a, b = bubbles[i], bubbles[i + 1]
        if not (_left_terminal_ok(a) and _right_start_ok(b)):
            violations.append(
                {"at": i, "left_tail": a[-80:], "right_head": b[:80]}
            )
    return violations


# --------------------------------------------------------------------------
# Corpus vivo (SOLO LECTURA)
# --------------------------------------------------------------------------


def load_live_corpus(db_path: str) -> list[Exchange]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    msgs = [dict(r) for r in con.execute(
        "SELECT id, role, content, t_h, day, conversation_id FROM messages ORDER BY id"
    )]
    state_rows = [dict(r) for r in con.execute("SELECT * FROM daily_state ORDER BY day")]
    con.close()

    state_by_day: dict[int, dict] = {r["day"]: r for r in state_rows}
    fallback_state = state_rows[-1] if state_rows else None

    exchanges: list[Exchange] = []
    for m in msgs:
        if m["role"] != "assistant":
            continue
        prev_user = next(
            (
                x["content"]
                for x in msgs
                if x["role"] == "user"
                and x["conversation_id"] == m["conversation_id"]
                and x["id"] < m["id"]
            ),
            None,
        )
        st = state_by_day.get(m["day"], fallback_state)
        if st is None:
            raise RuntimeError("companion.db sin daily_state — no se puede derivar expressiveness")
        record = DayRecord(
            t=st["day"],
            m=st["m_level"],
            g=st["g"],
            arg=st["arg"],
            p=st["p"],
            M=int(st["M"]),
            score=float(st["score"] or 0.0),
            mu=st["mu"],
            eta=st["eta"],
            cycle_day=st["cycle_day"],
            phase_label=st["phase_label"],
            seed=st["seed"],
        )
        directive = derive_behavior(record, TimingParams(), hour=m["t_h"] % 24.0)
        exchanges.append(
            Exchange(
                source="live",
                user=prev_user or "",
                reply=m["content"],
                expressiveness=directive.expressiveness,
                t_h=m["t_h"],
                day=m["day"],
                ref=f"live-msg-{m['id']}",
            )
        )
    return exchanges


# --------------------------------------------------------------------------
# Corpus sim (FakeClient + RecordingSession)
# --------------------------------------------------------------------------


def generate_sim_corpus(seed: int, scratch_dir: Path) -> list[Exchange]:
    store = SQLiteStore(scratch_dir / "sim.db", audit_mode=True)
    ensure_companion_initialized(
        store,
        seed=seed,
        user=UserProfile(name="User", interests=GATE2_USER_INTERESTS),
        day=0,
    )
    clock = VirtualClock(0.0)
    # Cola con padding: absorbe llamadas extra del harness (memoria, etc.)
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
                source="sim",
                user=user_text,
                reply=result.reply,
                expressiveness=result.directive.expressiveness,
                t_h=clock.now_h(),
                day=day,
                ref=f"sim-{i:02d}",
            )
        )
    session.finalize_current()
    store.close()

    # Verificación de alineación: la réplica i debe ser SIM_REPLIES[i]
    misaligned = [
        (i, exchanges[i].reply[:40], SIM_REPLIES[i][:40])
        for i in range(len(SIM_USER_TURNS))
        if exchanges[i].reply != SIM_REPLIES[i]
    ]
    if misaligned:
        raise RuntimeError(f"FakeClient desalineado ({len(misaligned)} turnos): {misaligned[:3]}")
    return exchanges


# --------------------------------------------------------------------------
# Legs de evaluación
# --------------------------------------------------------------------------


def spearman_with_ci(x: list[float], y: list[float], seed: int) -> dict:
    a, b = np.asarray(x, dtype=float), np.asarray(y, dtype=float)

    def _rho(u, v):
        return spearmanr(u, v).statistic

    rho = _rho(a, b)
    if len(a) < 3 or np.all(a == a[0]) or np.all(b == b[0]):
        return {"rho": float(rho), "ci_low": float("nan"), "ci_high": float("nan"), "n": int(len(a))}
    res = bootstrap(
        (a, b),
        _rho,
        n_resamples=10000,
        method="percentile",
        random_state=seed,
        paired=True,
    )
    lo, hi = res.confidence_interval
    return {"rho": float(rho), "ci_low": float(lo), "ci_high": float(hi), "n": int(len(a))}


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


def _render_unsplit(ex: Exchange) -> str:
    return f"User: {ex.user}\n\nCompanion:\n{ex.reply}"


def _render_split(ex: Exchange, rec: SplitRecord) -> str:
    parts = [f"User: {ex.user}", ""]
    for i, (bubble, gap) in enumerate(zip(rec.bubbles, rec.gaps_s + [None])):
        parts.append(f"Companion — bubble {i + 1} of {rec.k}:")
        parts.append(bubble)
        if gap is not None:
            parts.append("")
            parts.append(f"[pause {gap:.1f} s]")
            parts.append("")
    return "\n".join(parts)


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


def run_judge_leg(
    pairs: list[tuple[Exchange, SplitRecord]],
    seed: int,
    cache_path: Path,
    client,
) -> dict:
    """N=30 pareado: naturalidad del renderizado dividido vs sin dividir."""
    cache: dict[str, float] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            cache = {}

    def key(kind: str, ex: Exchange) -> str:
        import hashlib
        return hashlib.sha1(f"{kind}|{ex.user}|{ex.reply}".encode()).hexdigest()[:16]

    rng = random.Random(seed)
    results: list[dict] = []
    failures: list[str] = []

    def score(kind: str, ex: Exchange, rendering: str) -> float | None:
        k = key(kind, ex)
        if k in cache:
            return cache[k]
        try:
            raw = client.chat(
                [{"role": "user", "content": f"{JUDGE_RUBRIC}\n\nRendering:\n{rendering}"}],
                system="You are a careful chat-delivery judge. Score precisely.",
                temperature=0.0,
                json_mode=True,
            )
            s = _parse_judge_score(raw)
        except Exception as exc:  # red/API: el leg queda BLOCKED, no inventado
            failures.append(f"{ex.ref}/{kind}: {type(exc).__name__}: {exc}")
            return None
        if s is None:
            failures.append(f"{ex.ref}/{kind}: unparseable judge output: {raw[:120]!r}")
            return None
        s = max(1.0, min(10.0, s))
        cache[k] = s
        cache_path.write_text(json.dumps(cache, indent=1))  # incrementa: sobrevive cortes
        return s

    for ex, rec in pairs:
        # Orden ciego por pareja (el juez no sabe qué es cada renderizado).
        items = [
            ("split", _render_split(ex, rec)),
            ("unsplit", _render_unsplit(ex)),
        ]
        rng.shuffle(items)
        scores: dict[str, float | None] = {}
        for kind, rendering in items:
            scores[kind] = score(kind, ex, rendering)
        results.append(
            {
                "ref": ex.ref,
                "k": rec.k,
                "mean_gap_s": rec.mean_gap_s,
                "expressiveness": round(ex.expressiveness, 4),
                "score_split": scores["split"],
                "score_unsplit": scores["unsplit"],
                "diff": (
                    round(scores["split"] - scores["unsplit"], 4)
                    if scores["split"] is not None and scores["unsplit"] is not None
                    else None
                ),
            }
        )

    cache_path.write_text(json.dumps(cache, indent=1))

    complete = [r for r in results if r["diff"] is not None]
    if not complete:
        return {"status": "BLOCKED", "n": 0, "failures": failures, "pairs": results}

    diffs = np.asarray([r["diff"] for r in complete], dtype=float)
    mean_diff = float(diffs.mean())
    res = bootstrap((diffs,), np.mean, n_resamples=10000, method="percentile",
                    random_state=seed)
    lo, hi = res.confidence_interval
    verdict = "PASS" if (mean_diff >= 0.0 and lo >= -0.05) else "FAIL"
    if failures:
        status = "PARTIAL" if len(complete) == len(results) else "BLOCKED"
    else:
        status = "OK"
    return {
        "status": status,
        "n": int(len(complete)),
        "mean_diff": mean_diff,
        "ci_low": float(lo),
        "ci_high": float(hi),
        "verdict": verdict,
        "failures": failures,
        "pairs": results,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=5001)
    ap.add_argument("--live-db", default=os.environ.get("C2_LIVE_DB", DEFAULT_LIVE_DB))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--skip-judge", action="store_true", help="omite el leg del juez LLM")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="c2-bubbles-sim-"))
    rng = random.Random(args.seed)

    print("== C2: corpus ==")
    live = load_live_corpus(args.live_db)
    print(f"  live (companion.db, read-only): {len(live)} réplicas assistant")
    sim = generate_sim_corpus(args.seed, scratch)
    print(f"  sim (FakeClient + RecordingSession): {len(sim)} réplicas")
    corpus = live + sim

    print("== C2: splitter ==")
    records: list[SplitRecord] = []
    n_boundaries = 0
    for ex in corpus:
        bubbles, gaps = split_reply(ex.reply, ex.expressiveness, rng)
        rec = SplitRecord(exchange=ex, bubbles=bubbles, gaps_s=gaps)
        rec.violations = check_boundaries(rec.bubbles)
        records.append(rec)
        n_boundaries += rec.k - 1

    total_violations = sum(len(r.violations) for r in records)
    print(f"  réplicas: {len(corpus)}  cortes: {n_boundaries}  "
          f"violaciones mid-sentence: {total_violations}")
    mech = {
        "n_replies": len(corpus),
        "n_live": len(live),
        "n_sim": len(sim),
        "n_boundaries": n_boundaries,
        "mid_sentence_violations": total_violations,
        "verdict": "PASS" if total_violations == 0 else "FAIL",
    }

    print("== C2: ρ(gap, expressiveness) ==")
    gaps_all = [r.mean_gap_s for r in records]
    es_all = [r.exchange.expressiveness for r in records]
    rho_all = spearman_with_ci(gaps_all, es_all, args.seed)
    split_records = [r for r in records if r.k > 1]
    rho_split = spearman_with_ci(
        [r.mean_gap_s for r in split_records],
        [r.exchange.expressiveness for r in split_records],
        args.seed,
    )
    print(f"  todos (n={rho_all['n']}): ρ={rho_all['rho']:.3f} "
          f"CI95=[{rho_all['ci_low']:.3f},{rho_all['ci_high']:.3f}]")
    print(f"  solo divididas (n={rho_split['n']}): ρ={rho_split['rho']:.3f} "
          f"CI95=[{rho_split['ci_low']:.3f},{rho_split['ci_high']:.3f}]")
    rho_leg = {
        "all": rho_all,
        "split_only": rho_split,
        "verdict": "PASS" if rho_all["rho"] >= 0.5 else "FAIL",
    }

    print("== C2: leg del juez ==")
    judge_leg: dict = {}
    if args.skip_judge:
        print("  --skip-judge: leg omitido")
        judge_leg = {"status": "SKIPPED"}
    elif not os.environ.get("LLM_API_KEY"):
        print("  LLM_API_KEY no resuelve → leg BLOCKED (nunca se inventan números)")
        judge_leg = {"status": "BLOCKED", "reason": "LLM_API_KEY not set"}
    else:
        client = OpenAICompatibleClient()
        try:
            judge_leg = run_judge_leg(
                [(r.exchange, r) for r in records if r.exchange.source == "sim"],
                args.seed,
                out_dir / "judge_scores.json",
                client,
            )
        finally:
            client.close()
        print(f"  {judge_leg.get('status')}: n={judge_leg.get('n')} "
              f"meanΔ={judge_leg.get('mean_diff')} "
              f"CI95=[{judge_leg.get('ci_low')},{judge_leg.get('ci_high')}] "
              f"verdict={judge_leg.get('verdict')}")
        if judge_leg.get("failures"):
            print("  fallos:", judge_leg["failures"][:5])

    summary = {
        "experiment": "c2-bubbles",
        "plan_ref": "advisor-orchestration-2026-08-15.md Part 3 C2",
        "seed": args.seed,
        "live_db": args.live_db,
        "env": {
            "LLM_BASE_URL": os.environ.get("LLM_BASE_URL", ""),
            "LLM_API_KEY_set": bool(os.environ.get("LLM_API_KEY")),
            "judge_model": getattr(client, "model", "n/a") if not args.skip_judge and os.environ.get("LLM_API_KEY") else "n/a",
        },
        "splitter": {
            "bubble_target": f"1+floor((e-0.15)/{_BUBBLE_TARGET_SLOPE}) cap {_MAX_BUBBLES}",
            "jitter_prob": _JITTER_PROB,
            "gap_s": f"lognormal(mu=ln({_GAP_BASE_S}+{_GAP_SLOPE_S}*e), sigma={_GAP_SIGMA}) clipped [{_GAP_MIN_S},{_GAP_MAX_S}]",
        },
        "corpus": {
            "live": [{"ref": e.ref, "user": e.user, "reply": e.reply,
                      "expressiveness": round(e.expressiveness, 4),
                      "t_h": e.t_h, "day": e.day} for e in live],
            "sim": [{"ref": e.ref, "user": e.user, "reply": e.reply,
                     "expressiveness": round(e.expressiveness, 4),
                     "t_h": e.t_h, "day": e.day} for e in sim],
        },
        "splits": [
            {
                "ref": r.exchange.ref,
                "k": r.k,
                "gaps_s": r.gaps_s,
                "mean_gap_s": r.mean_gap_s,
                "expressiveness": round(r.exchange.expressiveness, 4),
                "bubbles": r.bubbles,
                "violations": r.violations,
            }
            for r in records
        ],
        "mechanical": mech,
        "rho_leg": rho_leg,
        "judge_leg": judge_leg,
    }
    (out_dir / "c2_bubbles_results.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    print("== Verdictos ==")
    print(f"  1. mecánico (0 splits mid-sentence): {mech['verdict']} "
          f"(n={mech['n_replies']}, cortes={mech['n_boundaries']}, violaciones={mech['mid_sentence_violations']})")
    print(f"  2. juez naturalidad pareado: {judge_leg.get('verdict', judge_leg.get('status'))} "
          f"(n={judge_leg.get('n')}, meanΔ={judge_leg.get('mean_diff')}, "
          f"CI95=[{judge_leg.get('ci_low')},{judge_leg.get('ci_high')}])")
    print(f"  3. ρ(gap, e): {rho_leg['verdict']} (ρ={rho_all['rho']:.3f}, n={rho_all['n']})")
    print(f"\nJSON: {out_dir / 'c2_bubbles_results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
