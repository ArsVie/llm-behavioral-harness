"""Shock-variant extension: WHEN does the steering act? Constant vs prefill-only vs
last-position vs decaying. Base condition C42 (B2 context + valence steer at s=42.4), temp 0.95,
4 samples per cell, same seeds as the other sample runs.

Variants:
  C42       - constant (hook active on every forward, all positions)  [reference]
  C42pre    - prefill only (steer the initial context pass, then off)
  C42last   - last position only (only the newest token each forward)
  C42decay  - constant during prefill, then 0.95^step on each generated token
Baseline B2 (no steering) is included for a self-contained table.

Runs in its own container + volume (parallel with the temp sweep; avoids the Modal
concurrent-commit race on the shared output volume).
Run: modal run --detach experiments/replay_live_31b/modal_replay_shock.py
"""
from __future__ import annotations

from pathlib import Path

import modal

APP_NAME = "companion-replay-shock"
HF_HOME = "/models/hf"
REPO = Path(__file__).resolve().parent.parent.parent
SCEN = REPO / "results" / "replay-live-31b-2026-10-03" / "raw" / "scenarios.json"
DIRS = Path("/home/vruizes/.hermes/projects/emotion-geometry/diagnostics/p2-directions-chat-gemma-4-31b.npz")

MODEL_ID = "google/gemma-4-31B-it"
MODEL_REV = "842da3794eaa0b77d5f08bae87a17459d91ff475"
HL = 36
S42 = 42.4
TEMP = 0.95
N_SAMPLES = 4
DECAY = 0.95
MASTER_SEED = 20261001
# (name, variant, steer?)
CONDS = [("B2", "B2", None), ("C42", "B2", "const"), ("C42pre", "B2", "prefill"),
         ("C42last", "B2", "last"), ("C42decay", "B2", "decay")]
MAX_NEW = 200

app = modal.App(APP_NAME)
models_volume = modal.Volume.from_name("emotion-models")
out_volume = modal.Volume.from_name("companion-replay-shock", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("torch==2.11.0", index_url="https://download.pytorch.org/whl/cu128")
    .pip_install(
        "transformers==5.15.0",
        "bitsandbytes==0.50.1",
        "accelerate==1.14.0",
        "huggingface_hub==1.27.0",
        "numpy==2.5.2",
        "hf_transfer",
    )
    .env({"HF_HOME": HF_HOME, "TOKENIZERS_PARALLELISM": "false",
          "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"})
    .add_local_file(str(SCEN), "/in/scenarios.json")
    .add_local_file(str(DIRS), "/in/dirs.npz")
)


@app.function(
    image=image, gpu="L40S", cpu=4.0, memory=65536,
    volumes={"/models": models_volume, "/out": out_volume},
    secrets=[modal.Secret.from_name("huggingface")],
    timeout=3600, retries=1, max_containers=1,
)
def replay_shock() -> str:
    import json
    import time

    import numpy as np
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    t0 = time.time()
    tok = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REV)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID, revision=MODEL_REV,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True),
        device_map={"": "cuda:0"}, low_cpu_mem_usage=True,
    )
    model.eval()
    layers = None
    for path in ("model.layers", "model.language_model.layers", "language_model.model.layers",
                 "language_model.layers"):
        try:
            obj = model
            for part in path.split("."):
                obj = getattr(obj, part)
            layers = list(obj)
            break
        except AttributeError:
            continue
    if layers is None:
        raise RuntimeError("decoder layers not found")
    print(f"[shock] model loaded in {time.time() - t0:.0f}s", flush=True)

    z = np.load("/in/dirs.npz")
    d_val = z[f"L{HL}"][3].astype(np.float64)
    delta = (1.0 * S42) * torch.tensor(d_val, dtype=torch.float32, device="cuda")
    scenarios = json.loads(Path("/in/scenarios.json").read_text())

    class SteerHook:
        """mode: const | prefill | last | decay"""

        def __init__(self, layer_mod, d, mode):
            self.layer_mod, self.delta, self.mode, self.h, self.step = layer_mod, d, mode, None, 0

        def __enter__(self):
            self.h = self.layer_mod.register_forward_hook(self._fn)
            return self

        def _fn(self, module, inp, out):
            h = out[0] if isinstance(out, tuple) else out
            prefill = h.shape[1] > 1
            if self.mode == "const":
                h = h + self.delta.to(h.dtype)
            elif self.mode == "prefill":
                if prefill:
                    h = h + self.delta.to(h.dtype)
            elif self.mode == "last":
                h = h.clone()
                h[:, -1:, :] = h[:, -1:, :] + self.delta.to(h.dtype)
            elif self.mode == "decay":
                if prefill:
                    h = h + self.delta.to(h.dtype)
                else:
                    h = h + (self.delta * (DECAY ** self.step)).to(h.dtype)
                    self.step += 1
            if isinstance(out, tuple):
                return (h,) + out[1:]
            return h

        def __exit__(self, *a):
            if self.h is not None:
                self.h.remove()
            return False

    from contextlib import nullcontext

    results = []

    def build_payload(complete: bool) -> dict:
        return {"model": MODEL_ID, "revision": MODEL_REV, "hl": HL, "s42": S42, "temp": TEMP,
                "decay": DECAY, "n_samples": N_SAMPLES, "master_seed": MASTER_SEED,
                "conditions": [c[0] for c in CONDS], "complete": complete, "results": results}

    for s in scenarios:
        for cname, vname, mode in CONDS:
            msgs = [{"role": "system", "content": s["persona"]}] + s["variants"][vname]
            text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
            enc = tok(text, return_tensors="pt").to("cuda")
            n_in = enc["input_ids"].shape[1]
            for si in range(N_SAMPLES):
                seed = MASTER_SEED + s["call_id"] * 10 + si
                torch.manual_seed(seed)
                ctx = SteerHook(layers[HL - 1], delta, mode) if mode else nullcontext()
                tg = time.time()
                with ctx:
                    gen = model.generate(**enc, max_new_tokens=MAX_NEW, do_sample=True,
                                         temperature=TEMP)
                out = tok.decode(gen[0][n_in:], skip_special_tokens=True).strip()
                results.append({"call_id": s["call_id"], "condition": cname, "variant": mode,
                                "sample": si, "seed": seed, "n_in": n_in, "text": out,
                                "seconds": round(time.time() - tg, 1)})
                print(f"[shock] call {s['call_id']} {cname} s{si}: {out[:70]!r}", flush=True)
                torch.cuda.empty_cache()
        Path("/out/generations-shock.json").write_text(json.dumps(build_payload(False), indent=1) + "\n")
        out_volume.commit()
        print(f"[shock] checkpoint: call {s['call_id']} done ({len(results)} gens)", flush=True)
    Path("/out/generations-shock.json").write_text(json.dumps(build_payload(True), indent=1) + "\n")
    out_volume.commit()
    print(f"[shock] saved ({len(results)} gens) in {time.time() - t0:.0f}s", flush=True)
    return f"done: {len(results)} generations in {time.time() - t0:.0f}s"


@app.local_entrypoint()
def main():
    print("[local] launching shock-variant replay (B2 / C42 const / prefill / last / decay @ 0.95)")
    fc = replay_shock.spawn()  # fire-and-forget: no client stream to lose mid-run
    print(f"[local] spawned (call id: {fc.object_id}); safe to disconnect")
