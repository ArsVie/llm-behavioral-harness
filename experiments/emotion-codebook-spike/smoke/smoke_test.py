"""P0 barrier smoke test: forward + backward (autograd) in 8 GB, bf16.

Pre-registered: 128-token sequence, gradient checkpointing enabled, layer
shard fallback if OOM. GGUF disqualified — autograd must work.

Usage:
    .venv/bin/python smoke/smoke_test.py Qwen/Qwen3-1.7B [--seq-len 128]
Output: diagnostics/smoke_<model>.json (peak MiB, wall time, PASS/FAIL).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness.determinism import (  # noqa: E402
    GPU_BUDGET_MIB,
    MASTER_SEED,
    ProvenanceRecorder,
    derive_seed,
    seed_everything,
)

SPIKE_ROOT = Path(__file__).resolve().parent.parent
DIAG_DIR = SPIKE_ROOT / "diagnostics"


def run_smoke(model_id: str, seq_len: int = 128) -> dict:
    seed = derive_seed(MASTER_SEED, 0)  # smoke-test run seed (deterministic)
    seed_everything(seed)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.deterministic = True

    t0 = time.perf_counter()
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(model_id)
    load_t0 = time.perf_counter()
    model = AutoModelForCausalLM.from_pretrained(
        model_id, dtype=torch.bfloat16, low_cpu_mem_usage=True
    )
    model.to("cuda")
    model.gradient_checkpointing_enable()
    model.train()
    load_s = time.perf_counter() - load_t0

    # Deterministic 128-token input (fixed text, then seeded padding to seq_len).
    text = "The quiet warmth of a slow evening settles over the small room."
    ids = tok(text, return_tensors="pt").input_ids
    pad = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id
    rng = torch.Generator(device="cuda").manual_seed(seed)
    extra = torch.randint(0, tok.vocab_size, (1, seq_len - ids.shape[1]), generator=rng, device="cuda")
    input_ids = torch.cat([ids.to("cuda"), extra], dim=1)[:, :seq_len]

    torch.cuda.reset_peak_memory_stats()
    torch.cuda.empty_cache()

    fwd_t0 = time.perf_counter()
    out = model(input_ids=input_ids, use_cache=False)
    logits = out.logits
    fwd_s = time.perf_counter() - fwd_t0
    peak_fwd = torch.cuda.max_memory_allocated()

    labels = input_ids[:, 1:].contiguous()
    loss = torch.nn.functional.cross_entropy(
        logits[:, :-1, :].reshape(-1, logits.size(-1)), labels.reshape(-1)
    )
    bwd_t0 = time.perf_counter()
    loss.backward()
    bwd_s = time.perf_counter() - bwd_t0
    peak = torch.cuda.max_memory_allocated()

    # Prove gradients actually flowed (autograd alive).
    grads = [p.grad for p in model.parameters() if p.grad is not None]
    n_grad = len(grads)
    grad_ok = n_grad > 0 and all(torch.isfinite(g).all() for g in grads)
    grad_norm = (
        float(torch.linalg.vector_norm(torch.stack([g.float().norm() for g in grads])))
        if grads
        else 0.0
    )

    total_s = time.perf_counter() - t0
    peak_mib = peak / (1024 * 1024)
    fit = peak_mib < GPU_BUDGET_MIB and grad_ok
    n_params = sum(p.numel() for p in model.parameters())
    result = {
        "model_id": model_id,
        "seed": seed,
        "seq_len": seq_len,
        "dtype": "bfloat16",
        "checkpointing": True,
        "n_params": n_params,
        "load_s": round(load_s, 2),
        "fwd_s": round(fwd_s, 3),
        "bwd_s": round(bwd_s, 3),
        "total_s": round(total_s, 2),
        "peak_alloc_mib": round(peak_mib, 1),
        "peak_fwd_mib": round(peak_fwd / (1024 * 1024), 1),
        "budget_mib": GPU_BUDGET_MIB,
        "n_grad_tensors": n_grad,
        "grad_finite": bool(grad_ok),
        "grad_norm": round(grad_norm, 4),
        "loss": float(loss.item()),
        "PASS": bool(fit),
    }
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model_id")
    ap.add_argument("--seq-len", type=int, default=128)
    args = ap.parse_args()

    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = run_smoke(args.model_id, args.seq_len)
    except torch.cuda.OutOfMemoryError:
        result = {"model_id": args.model_id, "PASS": False, "oom": True}
    name = args.model_id.replace("/", "-")
    out = DIAG_DIR / f"smoke_{name}.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
