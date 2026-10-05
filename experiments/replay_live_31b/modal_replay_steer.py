"""Steering-validation battery: does steering ±dominance / ±intensity / ±arousal move the
corresponding TEXT readouts? The dominance and intensity directions come from the 500-word
ridge fit (p1f-dirs-gemma-4-31b-w500.npz); arousal from the corrected PC2 direction (hs35).

Conditions (B2 context, temp 0.95, 3 samples, seeds MASTER_SEED+call*10+si):
  B2    baseline
  Dp42 / Dm42   dominance + / -   (s = 42.4, layers[35])
  Ip42 / Im42   intensity + / -   (s = 42.4, layers[35])
  Ap42 / Am42   arousal   + / -   (s = 42.4, layers[34])
Readouts (analyze_steer.py): NRC valence/arousal/dominance of replies, assertiveness vs
hedging markers, rep3, sprite tag, hearts.

Runs in its own container + volume. Run: modal run --detach experiments/replay_live_31b/modal_replay_steer.py
"""
from __future__ import annotations

from pathlib import Path

import modal

APP_NAME = "companion-replay-steer"
HF_HOME = "/models/hf"
REPO = Path(__file__).resolve().parent.parent.parent
SCEN = REPO / "results" / "replay-live-31b-2026-10-03" / "raw" / "scenarios.json"
DIRS_W500 = Path("/home/vruizes/.hermes/projects/emotion-geometry/diagnostics/p1f-dirs-gemma-4-31b-w500.npz")
DIRS_COMPOSE = Path("/home/vruizes/.hermes/cache/scratch/compose-dirs.npz")

MODEL_ID = "google/gemma-4-31B-it"
MODEL_REV = "842da3794eaa0b77d5f08bae87a17459d91ff475"
S42 = 42.4
N_SAMPLES = 3
TEMP = 0.95
MASTER_SEED = 20261001
DIR_LAYER = {"dominance": 35, "intensity": 35, "arousal": 34}
CONDS = [("B2", None, 1.0),
         ("Dp42", "dominance", +1.0), ("Dm42", "dominance", -1.0),
         ("Ip42", "intensity", +1.0), ("Im42", "intensity", -1.0),
         ("Ap42", "arousal", +1.0), ("Am42", "arousal", -1.0)]
MAX_NEW = 200

app = modal.App(APP_NAME)
models_volume = modal.Volume.from_name("emotion-models")
out_volume = modal.Volume.from_name("companion-replay-steer", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("torch==2.11.0", index_url="https://download.pytorch.org/whl/cu128")
    .pip_install("transformers==5.15.0", "bitsandbytes==0.50.1", "accelerate==1.14.0",
                 "huggingface_hub==1.27.0", "numpy==2.5.2", "hf_transfer")
    .env({"HF_HOME": HF_HOME, "TOKENIZERS_PARALLELISM": "false",
          "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"})
    .add_local_file(str(SCEN), "/in/scenarios.json")
    .add_local_file(str(DIRS_W500), "/in/dirs-w500.npz")
    .add_local_file(str(DIRS_COMPOSE), "/in/dirs-compose.npz")
)


@app.function(
    image=image, gpu="L40S", cpu=4.0, memory=65536,
    volumes={"/models": models_volume, "/out": out_volume},
    secrets=[modal.Secret.from_name("huggingface")],
    timeout=3600, retries=1, max_containers=1,
)
def replay_steer() -> str:
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
    print(f"[steer] model loaded in {time.time() - t0:.0f}s", flush=True)

    zw = np.load("/in/dirs-w500.npz")
    zc = np.load("/in/dirs-compose.npz")
    raw_dirs = {
        "dominance": zw["d"].astype(np.float64),
        "intensity": zw["int"].astype(np.float64),
        "arousal": zc["arousal"].astype(np.float64),
    }
    scenarios = json.loads(Path("/in/scenarios.json").read_text())

    class Steer:
        def __init__(self, layer_mod, delta):
            self.mod, self.delta = layer_mod, delta
            self.handle = None

        def __enter__(self):
            def fn(module, inp, out):
                h = out[0] if isinstance(out, tuple) else out
                h = h + self.delta.to(h.dtype)
                return (h,) + out[1:] if isinstance(out, tuple) else h
            self.handle = self.mod.register_forward_hook(fn)
            return self

        def __exit__(self, *a):
            self.handle.remove()
            return False

    from contextlib import nullcontext

    results = []

    def build_payload(complete: bool) -> dict:
        return {"model": MODEL_ID, "revision": MODEL_REV, "s42": S42, "temp": TEMP,
                "n_samples": N_SAMPLES, "master_seed": MASTER_SEED,
                "dir_layer": DIR_LAYER,
                "dirs": {"dominance": "p1f-dirs-gemma-4-31b-w500.npz:d",
                         "intensity": "p1f-dirs-gemma-4-31b-w500.npz:int",
                         "arousal": "compose-dirs.npz:arousal(hs35)"},
                "conditions": [c[0] for c in CONDS],
                "complete": complete, "results": results}

    for s in scenarios:
        for cname, dname, sign in CONDS:
            msgs = [{"role": "system", "content": s["persona"]}] + s["variants"]["B2"]
            text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
            enc = tok(text, return_tensors="pt").to("cuda")
            n_in = enc["input_ids"].shape[1]
            for si in range(N_SAMPLES):
                seed = MASTER_SEED + s["call_id"] * 10 + si
                torch.manual_seed(seed)
                if dname is None:
                    ctx = nullcontext()
                else:
                    vec = torch.tensor(raw_dirs[dname] * (S42 * sign), dtype=torch.float32,
                                       device="cuda")
                    ctx = Steer(layers[DIR_LAYER[dname]], vec)
                tg = time.time()
                with ctx:
                    gen = model.generate(**enc, max_new_tokens=MAX_NEW, do_sample=True,
                                         temperature=TEMP)
                out = tok.decode(gen[0][n_in:], skip_special_tokens=True).strip()
                results.append({"call_id": s["call_id"], "condition": cname, "dir": dname,
                                "sign": sign, "sample": si, "seed": seed, "n_in": n_in,
                                "text": out, "seconds": round(time.time() - tg, 1)})
                print(f"[steer] call {s['call_id']} {cname} s{si}: {out[:70]!r}", flush=True)
                torch.cuda.empty_cache()
        Path("/out/generations-steer.json").write_text(json.dumps(build_payload(False), indent=1) + "\n")
        out_volume.commit()
        print(f"[steer] checkpoint: call {s['call_id']} done ({len(results)} gens)", flush=True)
    Path("/out/generations-steer.json").write_text(json.dumps(build_payload(True), indent=1) + "\n")
    out_volume.commit()
    print(f"[steer] saved ({len(results)} gens) in {time.time() - t0:.0f}s", flush=True)
    return f"done: {len(results)} generations in {time.time() - t0:.0f}s"


@app.local_entrypoint()
def main():
    print("[local] launching steering battery (B2 / D± / I± / A± @ 0.95)")
    fc = replay_steer.spawn()
    print(f"[local] spawned (call id: {fc.object_id}); safe to disconnect")
