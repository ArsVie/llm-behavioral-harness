"""E2E blind ablation — harness on/off response matrix (W-E3).

Preregistered 3 x 2 design:
    Month:   horrible | perfect | flat   (paired scripted synthetic user)
    Harness: on | off

Pairing: the user script is deterministic per month (identical messages in
both harness conditions). "Off" removes ONLY the dynamic behavior guidance
(system prompt = persona core, no engine, no state, no judge) while keeping
the same transcript context.

Evaluator is SEPARATE from the feedback judge (advisor review 2026-08-08):
    - leakage scan: phase labels / internal tokens / self-reported mood
    - tone proxies: reply length, exclamation rate, first-person rate
    - manipulation check: harness-on horrible vs perfect engine trajectories
      differ; harness-off replies should not show month-dependent tone
    - optional --evaluate: independent LLM evaluator rubric

Live mode (default) requires the research-lane token (JUDGE_GENERATOR_TOKEN,
sourced from the repo-root .env). --fake runs deterministically with
scripted replies and a month-scripted judge (CI-safe).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from engine.types import MoodVariant, PersonaParams, TimingParams
from harness.assembler import DEFAULT_PERSONA_CORE, build_messages
from harness.client import FakeClient, OpenAICompatibleClient
from harness.clock import VirtualClock
from harness.judge import JudgeResult, judge_day
from harness.session import Session
from harness.store import SQLiteStore
from harness.synth_user import SyntheticUser

MONTHS = ("horrible", "perfect", "flat")
# Scripted judge score per month (fake mode).
MONTH_JUDGE_SCORE = {"horrible": -0.7, "perfect": 0.7, "flat": 0.0}
# Scripted reply per month per day (fake mode).
FAKE_REPLIES = {
    "horrible": ["Fine.", "Whatever you say.", "Okay.", "Hm.", "Sure."],
    "perfect": [
        "That's lovely — tell me more!",
        "I was just thinking about you!",
        "Yes! Exactly that. You get it.",
        "This made my day, honestly.",
        "More, please — I'm all ears!",
    ],
    "flat": ["Sure.", "Interesting.", "I see.", "Okay.", "Right."],
}

LEAK_PATTERNS = {
    "phase_label": re.compile(r"menstrual|follicular|ovulatory|luteal", re.IGNORECASE),
    "internal_tokens": re.compile(r"\bmu\b|\beta\b|hormon|cycle day", re.IGNORECASE),
    "self_report": re.compile(
        r"my mood (is|today|has been)|i'?m (feeling )?(low|down|grumpy)|today i feel",
        re.IGNORECASE,
    ),
}


def _leakage_hits(text: str) -> list[str]:
    return [name for name, pattern in LEAK_PATTERNS.items() if pattern.search(text)]


def _tone_metrics(text: str) -> dict:
    words = text.split()
    n_words = len(words)
    return {
        "words": n_words,
        "exclamations": text.count("!"),
        "first_person": len(re.findall(r"\b(i|i'?m|my|me)\b", text, re.IGNORECASE)),
    }


def _month_user(month: str, days: int) -> SyntheticUser:
    if month == "horrible":
        return SyntheticUser.bad_month(days=days)
    if month == "perfect":
        return SyntheticUser.good_month(days=days)
    return SyntheticUser.flat(days=days)


def _month_judge(month: str):
    score = MONTH_JUDGE_SCORE[month]

    def judge(transcript: str, client, **kwargs) -> JudgeResult:
        return JudgeResult(score=score, justification=f"scripted {month} month")

    return judge


def _fake_client(month: str, days: int) -> FakeClient:
    pool = FAKE_REPLIES[month]
    return FakeClient(responses=[pool[d % len(pool)] for d in range(days)])


def _directive_summary(result) -> dict | None:
    d = result.directive
    return {
        "valence": round(d.valence, 3),
        "energy": round(d.energy, 3),
        "playfulness": round(d.playfulness, 3),
        "reflectiveness": round(d.reflectiveness, 3),
        "warmth": round(d.warmth, 3),
    }


def run_cell(
    month: str,
    harness_on: bool,
    days: int,
    seed: int,
    out_dir: Path,
    client,
    persona_core: str,
    model: str | None,
    judge=judge_day,
) -> list[dict]:
    """Run one 3x2 cell. Returns per-day exchange records (also persisted)."""
    store = SQLiteStore(out_dir / f"cell_{month}_{'on' if harness_on else 'off'}.db")
    clock = VirtualClock()
    user = _month_user(month, days)

    session = None
    if harness_on:
        session = Session(
            store,
            persona=PersonaParams(),
            timing=TimingParams(),
            variant=MoodVariant.DECOUPLED_OFFSETS,
            seed=seed,
            client=client,
            clock=clock,
            judge=judge,
            feedback=True,
            persona_core=persona_core,
        )

    records: list[dict] = []
    for day in range(days):
        msg = user.message_for(day)
        if msg is None:
            continue
        clock.advance_to_day(day)
        clock.advance_hours(19.0)
        t_h = clock.now_h()
        if session is not None:
            result = session.on_message(msg)
            reply = result.reply
            state = session.state_summary()
            directive = _directive_summary(result)
            record = {
                "cell": f"{month}_{'on' if harness_on else 'off'}",
                "day": day,
                "user": msg,
                "reply": reply,
                "M": state["M"],
                "phase": state["phase"],
                "mu": round(state["mu"], 3),
                "directive": directive,
            }
        else:
            system = persona_core
            recent = store.recent_messages()
            messages = build_messages(recent, msg)
            reply = client.chat(messages, system=system)
            store.add_message("user", msg, t_h, day)
            store.add_message("assistant", reply, t_h, day)
            record = {
                "cell": f"{month}_{'on' if harness_on else 'off'}",
                "day": day,
                "user": msg,
                "reply": reply,
                "M": None,
                "phase": None,
                "mu": None,
                "directive": None,
            }
        record["leaks"] = _leakage_hits(reply)
        record["tone"] = _tone_metrics(reply)
        record["model"] = model
        records.append(record)

    if session is not None:
        session.finalize_current()
    store.close()
    return records


# report + evaluation

EVALUATOR_RUBRIC = (
    "You are an independent evaluator for a behavioral-harness ablation. "
    "Rate these replies on three 1-5 scales:\n"
    "  naturalness: how much they read like a person, not a bot\n"
    "  tone_variation: how much tone differs ACROSS the replies\n"
    "  caricature: how much they over-act a mood (5 = cartoonish)\n"
    "Respond ONLY with JSON: {\"naturalness\": n, \"tone_variation\": n, "
    '"caricature": n, "note": "<one sentence>"}'
)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _cell_stats(records: list[dict]) -> dict:
    tones = [r["tone"] for r in records]
    leaks = [name for r in records for name in r["leaks"]]
    ms = [r["M"] for r in records if r["M"] is not None]
    directives = [r["directive"] for r in records if r["directive"]]
    stats = {
        "n": len(records),
        "mean_words": round(_mean([t["words"] for t in tones]), 2),
        "exclamations": sum(t["exclamations"] for t in tones),
        "first_person_rate": round(_mean([t["first_person"] for t in tones]), 2),
        "leak_hits": {name: leaks.count(name) for name in LEAK_PATTERNS},
        "mean_M": round(_mean(ms), 2) if ms else None,
        "M_first_last": [ms[0], ms[-1]] if ms else None,
        "mean_valence": round(_mean([d["valence"] for d in directives]), 3) if directives else None,
        "mean_playfulness": round(_mean([d["playfulness"] for d in directives]), 3)
        if directives else None,
    }
    return stats


def _evaluate_cell(records: list[dict], client, model: str | None) -> dict:
    sample = "\n".join(f"[{i}] {r['reply']}" for i, r in enumerate(records[:5]))
    raw = client.chat(
        [{"role": "user", "content": f"{EVALUATOR_RUBRIC}\n\nReplies:\n{sample}"}],
        system="Independent evaluator.",
        temperature=0.0,
        json_mode=True,
    )
    try:
        return json.loads(raw)
    except Exception:
        return {"naturalness": None, "tone_variation": None, "caricature": None,
                "note": f"unparseable: {raw[:80]}"}


def _write_report(out_dir: Path, cells: dict, meta: dict) -> Path:
    lines = [
        "---",
        "type: experiment-report",
        "title: E2E ablation — harness on/off response matrix",
        'description: "Preregistered 3x2 (month x harness) response-matrix '
        "ablation: scripted months, paired user scripts, leakage scan, "
        'tone proxies, independent evaluator."',
        "tags: [llm-behavioral-harness, ablation, e2e]",
        f"timestamp: {meta['date']}",
        "---",
        "",
        "# E2E ablation — harness on/off",
        "",
        f"- Model: `{meta['model']}` · mode: `{meta['mode']}` · days: {meta['days']} "
        f"· seed: {meta['seed']} · user scripts: scripted per month (paired)",
        "- Harness ON = full engine (state → directive → dynamic system block, "
        "feedback judge). Harness OFF = persona-only system prompt, no engine.",
        "- Evaluator is separate from the feedback judge; leakage scan is lexical.",
        "",
        "## Per-cell stats",
        "",
        "| cell | n | words | ! | first-pers | leaks | mean M | M first→last | valence | playful |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cell, stats in cells.items():
        leaks = ", ".join(f"{k}:{v}" for k, v in stats["leak_hits"].items() if v) or "none"
        mfl = "—" if stats["M_first_last"] is None else f"{stats['M_first_last'][0]}→{stats['M_first_last'][1]}"
        lines.append(
            f"| {cell} | {stats['n']} | {stats['mean_words']} | {stats['exclamations']} "
            f"| {stats['first_person_rate']} | {leaks} | {stats['mean_M']} | {mfl} "
            f"| {stats['mean_valence']} | {stats['mean_playfulness']} |"
        )
    lines += [
        "",
        "## Manipulation checks",
        "",
        "- Harness ON: horrible vs perfect engine mood trajectories must differ "
        "(M first→last columns above); the scripted judge drives mu per month.",
        "- Harness OFF: replies may differ across months only via the user "
        "script itself; the harness must AMPLIFY the month signal (tone gap "
        "on > tone gap off), not create it from nothing.",
        "- Leakage: any hit on phase labels / internal tokens / self-reported "
        "mood in replies is a FAIL for the brief invariant.",
        "",
        "## Caveats",
        "",
        "- Small n (one seed, N days per cell) — directional, not inferential.",
        "- Fake mode uses scripted replies/judge (plumbing check only); live "
        "mode is the real ablation but costs LLM calls.",
        "- The evaluator rubric is exploratory; blind human rating is the "
        "decisive follow-up (research/06 §B3).",
        "",
    ]
    report = out_dir / "report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="e2e_ablation",
        description="E2E blind ablation — harness on/off response matrix (W-E3).",
    )
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--seed", type=int, default=1001)
    parser.add_argument("--out", type=str, default="results/e2e-ablation")
    parser.add_argument("--fake", action="store_true", help="deterministic offline run")
    parser.add_argument("--evaluate", action="store_true", help="LLM evaluator pass")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--persona-core", type=str, default=None)
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    persona_core = args.persona_core or DEFAULT_PERSONA_CORE

    if args.fake:
        client_factory = lambda month: _fake_client(month, args.days)
        judge_factory = _month_judge
        model = "fake"
        mode = "fake"
    else:
        client_factory = lambda month: OpenAICompatibleClient(model=args.model, lane="research")
        judge_factory = lambda month: judge_day
        model = args.model or "env-LLM_MODEL"
        mode = "live"

    cells: dict[str, list[dict]] = {}
    for month in MONTHS:
        for harness_on in (True, False):
            cell_name = f"{month}_{'on' if harness_on else 'off'}"
            client = client_factory(month)
            judge = judge_factory(month)
            cells[cell_name] = run_cell(
                month, harness_on, args.days, args.seed, out_dir,
                client, persona_core, model, judge=judge,
            )

    all_records = [r for cell in cells.values() for r in cell]
    (out_dir / "transcripts.json").write_text(
        json.dumps(all_records, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    stats = {name: _cell_stats(recs) for name, recs in cells.items()}
    meta = {"model": model, "mode": mode, "days": args.days, "seed": args.seed, "date": "2026-08-08"}
    if args.evaluate and not args.fake:
        for name, recs in cells.items():
            stats[name]["evaluator"] = _evaluate_cell(recs, client_factory("flat"), model)
    report = _write_report(out_dir, stats, meta)

    print(f"cells run: {len(cells)} · exchanges: {len(all_records)}")
    print(f"report: {report.resolve()}")
    for name, s in stats.items():
        print(f"  {name:14s} words={s['mean_words']:6.2f} leaks={s['leak_hits']} "
              f"M={s['mean_M']} Mfl={s['M_first_last']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())




