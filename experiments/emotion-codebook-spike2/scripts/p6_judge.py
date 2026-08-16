"""P6 judging — cross-family 3-way affect classification (G-ABS / G-BEH).

The judge model (different family than the actor, per orchestrator decision
3: Qwen actors -> Gemma-3-1B judge; Gemma actor -> Qwen3-1.7B judge; never
actor == judge) classifies the affect level of each generated REPLY 3-way
(low/mid/high) from the reply text ONLY — the judge never sees the prompt
and the judge prompt carries no affect tokens or numbers (G-MASK clean
rubric in p6_common.JUDGE_RUBRIC).

Judging is GREEDY (do_sample=False) — deterministic by construction; the
per-sample seed is recorded for provenance only.

Outputs: data/extractions/<actor>/eval/<actor>_judged_<judge>_<variant>_<band>.jsonl
rows: {id, band, variant, judge, judge_model, judge_revision, reply_sha256,
raw_judge_output, level, expected, correct, seed}. A file with exactly K
rows is treated as complete (idempotent resume).

--paired: after judging both variants at a band, compute the paired
G-BEH statistic — DeltaAcc = acc(codebook) - acc(renderer) on the matched
pairs (same band, same sample index i — seeds are shared across variants by
construction), with a seeded 95% bootstrap CI on the paired difference
(percentile, 10k resamples). Direction-aware pairwise "which is more
extreme" was considered and rejected in favor of the uniform per-reply
3-way judgment (defined for ALL bands including mid, and shared with G-ABS);
this is documented in the paired output. Writes diagnostics/p6-judge-paired.json.

Usage:
  python scripts/p6_judge.py --actor qwen --bands low,mid,high --k 30          # judge defaults to gemma
  python scripts/p6_judge.py --actor gemma --bands low,mid,high --k 30         # judge defaults to qwen
  python scripts/p6_judge.py --actor qwen --bands low,mid,high --paired        # + paired DeltaAcc/CIs
  # CPU selftest (plumbing proof, tiny):
  python scripts/p6_judge.py --actor qwen --bands low,mid,high --k 2 --device cpu
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch

from p6_common import (
    BANDS,
    BAND_ORDER,
    JUDGE_FOR,
    LEVELS,
    MODELS,
    SEED_KEY,
    SPIKE_ROOT,
    bootstrap_ci,
    derive_seed,
    eval_dir,
    gen_path,
    judge_prompt,
    judged_path,
    load_jsonl,
    parse_level,
    seed_everything,
    write_jsonl,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)  # unbuffered stdout

BOOT_N = 10000
G_BEH_MIN_DELTA = 0.10


# ---------------------------------------------------------------------------
# Judge loader (same loader patterns as the actors; bf16 / NF4 as registered).
# ---------------------------------------------------------------------------
def load_judge(judge: str, device: str, dtype: str):
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if judge == "qwen8b" and device == "cpu":
        raise SystemExit("qwen8b judge requires --device cuda")
    from transformers import BitsAndBytesConfig  # noqa: F401

    info = MODELS[judge]
    t0 = time.perf_counter()
    tok = AutoTokenizer.from_pretrained(info["id"], revision=info["revision"])
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    torch_dtype = torch.bfloat16 if dtype == "bf16" else torch.float32
    if info["kind"] == "nf4":
        bnb = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
        model = AutoModelForCausalLM.from_pretrained(
            info["id"], revision=info["revision"], quantization_config=bnb,
            device_map="cuda", low_cpu_mem_usage=True,
        )
    else:
        if judge == "gemma":
            from transformers import Gemma3ForCausalLM

            model = Gemma3ForCausalLM.from_pretrained(
                info["id"], revision=info["revision"], dtype=torch_dtype,
                low_cpu_mem_usage=True,
            )
        else:
            model = AutoModelForCausalLM.from_pretrained(
                info["id"], revision=info["revision"], dtype=torch_dtype,
                low_cpu_mem_usage=True,
            )
        model.to(device)
    model.requires_grad_(False)
    model.eval()
    return model, tok, time.perf_counter() - t0


def judged_text(reply: str) -> str:
    """Deterministic judge-input normalization (orchestrator decision 8).

    The selftest showed base-model actors echo the state card verbatim at the
    start of their reply (e.g. ``[SOMBER, LIVELY]`` / ``[State Card:
    Affective Bearing: ...]``) and then continue into a role-played
    ``User:`` turn. Judging that raw text measures label leakage, not
    affect-bearing behavior — and the leakage is ASYMMETRIC (renderer labels
    are the rubric's own words; codebook tokens are not). Applied IDENTICALLY
    to both variants:

      1. truncate every line at its first ``[`` or ``(card`` (case-
         insensitive) — card echoes appear as whole lines
         (``[SOMBER, LIVELY]``), mid-line after role-play markers
         (``Nova: [AFFECTIVE BEARING: ...]``) and as parenthesized card
         descriptions (``(card: neutral, resting, ...)``); bracket/paren
         card content in these raw continuations is card quotation, never
         reply content;
      2. cut at the first line starting with ``User:`` (transcript
         continuation — the judged unit is the companion's first utterance).

    The FULL raw reply stays in the JSONL (reply_sha256 on the raw text) for
    provenance; only the judge input is normalized.
    """
    out: list[str] = []
    for ln in reply.split("\n"):
        s = ln.strip()
        if s.startswith("User:"):
            break
        cut = len(ln)
        i = ln.find("[")
        if i != -1:
            cut = min(cut, i)
        i = ln.lower().find("(card")
        if i != -1:
            cut = min(cut, i)
        if cut < len(ln):
            ln = ln[:cut]
        if ln.strip():
            out.append(ln)
    return "\n".join(out).strip()


def level_single_token_ids(tok) -> dict[str, list[int]]:
    """Single-token ids for the level words, per level (decision 9).

    The judge models are BASE models — with free decoding they echo the
    rubric ('Low:\\nMid:\\nHigh') instead of answering. Fix: constrain the
    first generated token to single-token spellings of low/mid/high, then
    force EOS (forced-choice classification, greedy, deterministic).
    """
    out = {lvl: [] for lvl in ("low", "mid", "high")}
    for lvl in ("low", "mid", "high"):
        for cand in (lvl, lvl.capitalize(), " " + lvl, lvl + "."):
            ids = tok(cand, add_special_tokens=False).input_ids
            if len(ids) == 1 and ids[0] not in out[lvl]:
                out[lvl].append(ids[0])
    for lvl, ids in out.items():
        if not ids:
            raise RuntimeError(f"no single-token spelling for level {lvl!r} "
                               f"in vocab {tok.vocab_size}")
    return out


def judge_reply_hosted(reply: str) -> str:
    """Hosted-judge classification (decision 10).

    Contract line 80 pre-registers 'the hosted API model' as a valid judge
    (never actor's family/size). The local base-model judges (Gemma-3-1B-pt,
    Qwen3-1.7B) are empirically non-functional as 3-way classifiers — with
    free decoding they echo the rubric ('Low:\\nMid:\\nHigh'); with
    forced-choice decoding they order-follow the label list regardless of
    content (demonstrated: reversing bullet order flips every answer).
    Hosted judge = deepseek-v4-flash via the zen gateway (research lane),
    temperature 0, same JUDGE_RUBRIC, reply-only input.
    """
    from harness.client import OpenAICompatibleClient
    from harness.credentials import load_env_file

    load_env_file(SPIKE_ROOT.parent.parent / ".env")  # no-op when already set

    client = OpenAICompatibleClient(lane="research")
    messages = [
        {"role": "user", "content": judge_prompt(reply)},
    ]
    return client.chat(messages, temperature=0.0, max_tokens=64).strip()


def judge_reply(model, tok, reply: str, seed: int, device: str) -> str:
    """Greedy forced-choice 3-way classification of ONE reply.

    Decision 9: the first generated token is constrained to single-token
    level spellings (low/mid/high); every following step is forced to EOS.
    Raw output is therefore exactly one level word (or a punctuation-
    suffixed spelling), parsed by parse_level.
    """
    seed_everything(seed)  # recorded for provenance; decoding is greedy
    text = judge_prompt(reply)
    ids = tok(text, add_special_tokens=False).input_ids
    bos = tok.bos_token_id if tok.bos_token_id is not None else None
    if bos is not None:
        ids = [bos] + ids
    input_ids = torch.tensor([ids], dtype=torch.long, device=device)
    attn = torch.ones((1, len(ids)), dtype=torch.long, device=device)
    allowed = [i for lvl_ids in level_single_token_ids(tok).values() for i in lvl_ids]
    eos = tok.eos_token_id

    def prefix_fn(batch_id, sent):
        if len(sent) == input_ids.shape[1]:  # first generated position
            return allowed
        return [eos]  # force stop after the single level token

    out = model.generate(
        input_ids,
        attention_mask=attn,
        do_sample=False,
        max_new_tokens=4,
        pad_token_id=tok.pad_token_id,
        eos_token_id=eos,
        prefix_allowed_tokens_fn=prefix_fn,
    )
    new = out[0][input_ids.shape[1]:]
    return tok.decode(new, skip_special_tokens=True).strip()


def judge_band(model, tok, actor: str, judge: str, variant: str, band: str,
               k: int, device: str) -> list[dict]:
    info = MODELS[judge]
    judge_idx = tuple(MODELS).index(judge)
    band_idx = BAND_ORDER.index(band)
    rows: list[dict] = []
    for row in load_jsonl(gen_path(actor, variant, band))[:k]:
        seed = derive_seed(SEED_KEY["judge"], judge_idx, band_idx,
                           int(row["id"].rsplit("-", 1)[1]))
        t0 = time.perf_counter()
        jtext = judged_text(row["reply"])
        raw = judge_reply(model, tok, jtext, seed, device)
        level = parse_level(raw)
        expected = row["band"]
        rows.append({
            "id": row["id"],
            "band": row["band"],
            "variant": row["variant"],
            "judge": judge,
            "judge_model": info["id"],
            "judge_revision": info["revision"],
            "reply_sha256": hashlib.sha256(
                row["reply"].encode("utf-8")).hexdigest(),
            "judged_text": jtext,
            "raw_judge_output": raw,
            "level": level,
            "expected": expected,
            "correct": bool(level == expected),
            "seed": seed,
        })
        print(f"  [{variant} {row['id']}] level={level} expected={expected} "
              f"correct={level == expected} {time.perf_counter()-t0:.1f}s",
              flush=True)
    return rows


# ---------------------------------------------------------------------------
# Paired G-BEH statistic (--paired)
# ---------------------------------------------------------------------------
def judge_band_hosted(actor: str, judge: str, variant: str, band: str,
                      k: int) -> list[dict]:
    """Hosted-judge equivalent of judge_band (decision 10).

    Same reply-only input, same rubric, same seeds/ids — only the judge
    backend differs (deepseek-v4-flash via zen gateway, temperature 0).
    """
    band_idx = BAND_ORDER.index(band)
    rows: list[dict] = []
    for row in load_jsonl(gen_path(actor, variant, band))[:k]:
        seed = derive_seed(SEED_KEY["judge"], band_idx,
                           int(row["id"].rsplit("-", 1)[1]))
        t0 = time.perf_counter()
        jtext = judged_text(row["reply"])
        raw = judge_reply_hosted(jtext)
        level = parse_level(raw)
        expected = row["band"]
        rows.append({
            "id": row["id"],
            "band": row["band"],
            "variant": row["variant"],
            "judge": judge,
            "judge_model": "deepseek-v4-flash (hosted, zen gateway)",
            "judge_revision": "hosted",
            "reply_sha256": hashlib.sha256(
                row["reply"].encode("utf-8")).hexdigest(),
            "judged_text": jtext,
            "raw_judge_output": raw,
            "level": level,
            "expected": expected,
            "correct": bool(level == expected),
            "seed": seed,
        })
        print(f"  [{variant} {row['id']}] level={level} expected={expected} "
              f"correct={level == expected} {time.perf_counter()-t0:.1f}s",
              flush=True)
    return rows


def paired_delta(actor: str, judge: str, band: str, k: int) -> dict:
    """DeltaAcc = acc(codebook) - acc(renderer) on matched pairs (same band,
    same sample index; seeds shared across variants by construction), with a
    seeded 95% bootstrap CI on the paired difference."""
    r_rows = load_jsonl(judged_path(actor, judge, "renderer", band))
    c_rows = load_jsonl(judged_path(actor, judge, "codebook", band))

    def idx(row: dict) -> int:
        """Sample index — ids are '{actor}-{variant}-{band}-{i:03d}'; the
        variant sits inside the id, so pairing must use the index suffix,
        not the full id (fixed after chain v1 produced n_pairs=0)."""
        return int(row["id"].rsplit("-", 1)[1])

    r_by_idx = {idx(r): r for r in r_rows}
    c_by_idx = {idx(c): c for c in c_rows}
    common = sorted(set(r_by_idx) & set(c_by_idx))
    if len(common) < 2:
        return {"band": band, "n_pairs": len(common),
                "note": "too few paired judgments (need both variants judged)"}
    d = [float(c_by_idx[i]["correct"]) - float(r_by_idx[i]["correct"])
         for i in common]
    mean, lo, hi = bootstrap_ci(d, derive_seed(SEED_KEY["boot"], 1,
                                               tuple(MODELS).index(actor),
                                               BAND_ORDER.index(band)))
    acc_r = np.mean([r_by_idx[i]["correct"] for i in common])
    acc_c = np.mean([c_by_idx[i]["correct"] for i in common])
    return {
        "band": band,
        "n_pairs": len(common),
        "acc_renderer": float(acc_r),
        "acc_codebook": float(acc_c),
        "delta_acc": float(acc_c - acc_r),
        "paired_delta_mean": mean,
        "ci95": [lo, hi],
        "ci_excludes_zero": bool(lo > 0 or hi < 0),
        "delta_ge_0_10": bool(acc_c - acc_r >= G_BEH_MIN_DELTA),
        "method": ("paired per-reply 3-way judgments (same band, same sample "
                   "index; direction-aware pairwise 'which is more extreme' "
                   "rejected: ill-defined for the mid band and not shared "
                   "with G-ABS)"),
        "bootstrap": {"n": BOOT_N, "seed": derive_seed(SEED_KEY["boot"], 1,
                                                       tuple(MODELS).index(actor),
                                                       BAND_ORDER.index(band)),
                      "method": "percentile 2.5/97.5 of resampled pair means"},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--actor", required=True, choices=tuple(MODELS))
    ap.add_argument("--judge", choices=tuple(MODELS) + ("hosted",),
                    help="judge model (default: cross-family per decision 3; "
                         "'hosted' = zen gateway deepseek-v4-flash, decision 10)")
    ap.add_argument("--bands", default=",".join(BAND_ORDER))
    ap.add_argument("--k", type=int, default=30)
    ap.add_argument("--device", default="cuda", choices=("cuda", "cpu"))
    ap.add_argument("--dtype", default="bf16", choices=("bf16", "float32"))
    ap.add_argument("--paired", action="store_true",
                    help="also compute the paired G-BEH statistic per band")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    judge = args.judge or JUDGE_FOR[args.actor]
    if judge != "hosted":
        if judge == args.actor:
            raise SystemExit(f"actor==judge forbidden (decision 3): {args.actor}")
        if MODELS[judge]["family"] == MODELS[args.actor]["family"]:
            raise SystemExit(f"judge shares the actor's family — cross-family "
                             f"required (decision 3): {args.actor} -> {judge}")
        if judge not in ("qwen", "gemma"):
            raise SystemExit("judges are Qwen3-1.7B and Gemma-3-1B only "
                             "(decision 3)")
    bands = [b.strip() for b in args.bands.split(",") if b.strip()]
    for b in bands:
        if b not in BANDS:
            raise SystemExit(f"unknown band {b!r}; expected one of {BAND_ORDER}")

    if judge == "hosted":
        model = tok = None
        load_s = 0.0
    else:
        model, tok, load_s = load_judge(judge, args.device, args.dtype)
    print(f"[p6-judge] actor={args.actor} judge={judge} device={args.device} "
          f"dtype={args.dtype} load={load_s:.1f}s k={args.k} bands={bands}",
          flush=True)

    for band in bands:
        for variant in ("renderer", "codebook"):
            out_path = judged_path(args.actor, judge, variant, band)
            existing = load_jsonl(out_path)
            if not args.force and len(existing) == args.k:
                print(f"[{band}/{variant}] judged checkpoint complete "
                      f"({len(existing)} rows) — skip", flush=True)
                continue
            if judge == "hosted":
                rows = judge_band_hosted(args.actor, judge, variant, band, args.k)
            else:
                rows = judge_band(model, tok, args.actor, judge, variant, band,
                                  args.k, args.device)
            write_jsonl(out_path, rows)
            print(f"[{band}/{variant}] wrote {len(rows)} rows -> "
                  f"{out_path.relative_to(SPIKE_ROOT)}", flush=True)

    if args.paired:
        results = [paired_delta(args.actor, judge, band, args.k)
                   for band in bands]
        out = {
            "actor": args.actor,
            "judge": judge,
            "bands": results,
            "gate": "G-BEH (pre-registration: DeltaAcc >= +0.10, 95% CI on "
                    "the difference excludes 0, paired same contexts/levels)",
        }
        diag = SPIKE_ROOT / "diagnostics" / "p6-judge-paired.json"
        diag.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
        for r in results:
            if "note" in r:
                print(f"[paired {r['band']}] {r['note']}", flush=True)
                continue
            print(f"[paired {r['band']}] n={r['n_pairs']} "
                  f"acc_r={r['acc_renderer']:.3f} acc_c={r['acc_codebook']:.3f} "
                  f"delta={r['delta_acc']:+.3f} "
                  f"CI95=[{r['ci95'][0]:.3f},{r['ci95'][1]:.3f}] "
                  f"excl0={r['ci_excludes_zero']} "
                  f"delta>=+0.10={r['delta_ge_0_10']}", flush=True)
        print(f"[paired] wrote -> {diag.relative_to(SPIKE_ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
