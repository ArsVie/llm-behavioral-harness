"""P6 generation — actor replies for the behavioral eval (G-ABS / G-BEH).

For each requested band, generate K replies with the actor model under the
assembled variant prompt (renderer or codebook affect-bearing; identical
scaffold, only the affect slot differs). One actor per process — the
orchestrator serializes actors on the GPU.

Determinism: every sample is seeded with derive_seed(MASTER_SEED, 10,
actor_idx, band_idx, i) — the seed does NOT depend on the variant, so the
renderer and codebook runs at the same band share the same sampling stream
(paired contexts/levels per G-BEH). Decoding = the pre-registered
DecodingConfig (temperature 0.8, top_p 0.9, top_k 40, do_sample).

Protocol: raw continuation on base models (same convention as the p2
extraction scripts — BOS + text, no chat template): the assembled system
prompt + USER_LINE + COMPANION_PREFIX, then 128 new tokens.

Checkpointing: one JSONL per (actor, variant, band):
  data/extractions/<actor>/eval/<actor>_<variant>_<band>.jsonl
rows carry {id, band, valence, prompt_hash, reply} + provenance. A band file
with exactly K rows is treated as complete and skipped (idempotent resume).

Loaders are reused EXACTLY from the p2 extraction scripts (p2_qwen_extract,
p2_gemma_extract, p2_qwen8b_extract): bf16 for qwen/gemma, BitsAndBytes NF4
4-bit for qwen8b (lm_head unquantized check included).

Usage (orchestrator, one actor per process):
  python scripts/p6_generate.py --actor qwen --variant renderer --k 30 --bands low,mid,high
  python scripts/p6_generate.py --actor qwen --variant codebook --k 30 --bands low,mid,high
  python scripts/p6_generate.py --actor gemma --variant codebook --k 30
  python scripts/p6_generate.py --actor qwen8b --variant renderer --k 30   # GPU only
  # CPU selftest (plumbing proof, tiny):
  python scripts/p6_generate.py --actor qwen --variant renderer --k 2 --device cpu
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import torch

from p6_common import (
    BANDS,
    BAND_ORDER,
    COMPANION_PREFIX,
    MODELS,
    SEED_KEY,
    SPIKE_ROOT,
    USER_LINE,
    build_codebook_prompt,
    build_renderer_prompt,
    derive_seed,
    eval_dir,
    gen_path,
    load_jsonl,
    model_input,
    prompt_hash,
    seed_everything,
    write_jsonl,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)  # unbuffered stdout

MAX_NEW_TOKENS = 128  # task: 128-token reply per sample
SEQ_CAP = 4096  # safety cap on the full input sequence (prompt is ~2.6k chars)


# ---------------------------------------------------------------------------
# Loaders — reused EXACTLY from the p2 extraction scripts (pinned revisions).
# ---------------------------------------------------------------------------
def load_model(actor: str, device: str, dtype: str):
    """Returns (model, tokenizer, load_s). Loader code mirrors the p2
    extraction scripts verbatim (bf16 / NF4-4bit patterns)."""
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if actor == "qwen8b" and device == "cpu":
        raise SystemExit("qwen8b requires --device cuda (NF4 loader is CUDA-only)")
    from transformers import BitsAndBytesConfig  # noqa: F401  (import order as p2)

    info = MODELS[actor]
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
        w = model.lm_head.weight
        if hasattr(w, "quant_state") or w.dtype not in (
            torch.float32, torch.float16, torch.bfloat16,
        ):
            raise RuntimeError("lm_head is quantized; NF4 readout path requires "
                               "an unquantized output layer")
    else:
        if actor == "gemma":
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


def _encode(tok, text: str, device: str):
    """BOS + content tokens (extraction convention), capped at SEQ_CAP."""
    ids = tok(text, add_special_tokens=False).input_ids
    bos = tok.bos_token_id if tok.bos_token_id is not None else None
    if bos is not None:
        ids = [bos] + ids
    ids = ids[:SEQ_CAP]
    input_ids = torch.tensor([ids], dtype=torch.long, device=device)
    attn = torch.ones((1, len(ids)), dtype=torch.long, device=device)
    return input_ids, attn


def generate_one(model, tok, prompt_text: str, seed: int, device: str,
                 max_new_tokens: int) -> str:
    """One seeded reply: seed_everything immediately before generate (the
    pre-registered decoding contract)."""
    seed_everything(seed)
    input_ids, attn = _encode(tok, prompt_text, device)
    out = model.generate(
        input_ids,
        attention_mask=attn,
        do_sample=True,
        temperature=0.8,
        top_p=0.9,
        top_k=40,
        repetition_penalty=1.0,
        max_new_tokens=max_new_tokens,
        pad_token_id=tok.pad_token_id,
        eos_token_id=tok.eos_token_id,
    )
    new = out[0][input_ids.shape[1]:]
    return tok.decode(new, skip_special_tokens=True).strip()


def run_band(model, tok, actor: str, variant: str, band: str, k: int,
             device: str, max_new_tokens: int) -> list[dict]:
    """Generate K replies for one band; deterministic seeds shared across
    variants (paired sampling streams)."""
    actor_idx = tuple(MODELS).index(actor)
    band_idx = BAND_ORDER.index(band)
    info = MODELS[actor]
    if variant == "renderer":
        prompt, meta = build_renderer_prompt(band)
    else:
        prompt, meta = build_codebook_prompt(actor, band)
    full = model_input(prompt)
    phash = prompt_hash(prompt)
    print(f"[{actor}/{variant}/{band}] prompt_len={len(prompt)} "
          f"prompt_hash={phash[:16]}…", flush=True)
    rows: list[dict] = []
    for i in range(k):
        seed = derive_seed(SEED_KEY["gen"], actor_idx, band_idx, i)
        t0 = time.perf_counter()
        reply = generate_one(model, tok, full, seed, device, max_new_tokens)
        dt = time.perf_counter() - t0
        rows.append({
            "id": f"{actor}-{variant}-{band}-{i:03d}",
            "band": band,
            "valence": meta.get("valence_rendered", meta.get("valence_requested")),
            "variant": variant,
            "actor": actor,
            "model": info["id"],
            "model_revision": info["revision"],
            "prompt_hash": phash,
            "seed": seed,
            "temperature": 0.8,
            "top_p": 0.9,
            "top_k": 40,
            "max_new_tokens": max_new_tokens,
            "reply": reply,
        })
        print(f"  [{band} {i+1}/{k}] seed={seed} {dt:.1f}s "
              f"reply_len={len(reply)}", flush=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--actor", required=True, choices=tuple(MODELS))
    ap.add_argument("--variant", required=True, choices=("renderer", "codebook"))
    ap.add_argument("--k", type=int, default=30, help="replies per band")
    ap.add_argument("--bands", default=",".join(BAND_ORDER),
                    help="comma-separated band list (default low,mid,high)")
    ap.add_argument("--device", default="cuda", choices=("cuda", "cpu"))
    ap.add_argument("--dtype", default="bf16", choices=("bf16", "float32"))
    ap.add_argument("--max-new-tokens", type=int, default=MAX_NEW_TOKENS)
    ap.add_argument("--force", action="store_true",
                    help="regenerate bands even if their checkpoint is complete")
    args = ap.parse_args()

    bands = [b.strip() for b in args.bands.split(",") if b.strip()]
    for b in bands:
        if b not in BANDS:
            raise SystemExit(f"unknown band {b!r}; expected one of {BAND_ORDER}")

    model, tok, load_s = load_model(args.actor, args.device, args.dtype)
    print(f"[p6-generate] actor={args.actor} variant={args.variant} "
          f"device={args.device} dtype={args.dtype} load={load_s:.1f}s "
          f"k={args.k} bands={bands}", flush=True)

    for band in bands:
        out_path = gen_path(args.actor, args.variant, band)
        existing = load_jsonl(out_path)
        if not args.force and len(existing) == args.k:
            print(f"[{band}] checkpoint complete ({len(existing)} rows) — skip",
                  flush=True)
            continue
        rows = run_band(model, tok, args.actor, args.variant, band, args.k,
                        args.device, args.max_new_tokens)
        write_jsonl(out_path, rows)
        print(f"[{band}] wrote {len(rows)} rows -> {out_path.relative_to(SPIKE_ROOT)}",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
