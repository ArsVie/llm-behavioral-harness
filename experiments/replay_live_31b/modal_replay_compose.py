"""Composition / interference extension: steer MULTIPLE directions at once.

The live-wiring blocker: every experiment so far steered ONE direction. The engine needs
valence + energy(arousal) + warmth(love) simultaneously. Do composed vectors stay additive,
saturate, or interfere?

Conditions (all on B2 context, temp 0.8, 3 samples, seeds MASTER_SEED+call*10+si):
  B2     baseline (no steering)
  V42    valence only          (P2 chat L36 dir, layers[35])
  A42    arousal only          (P1 balanced suppressed PC2, hs35, layers[34])
  L42    love only             (P2 chat L36 dir, layers[35])
  VA42   valence + arousal
  VAL42  valence + arousal + love
Each active direction at s=42.4 (the proven single-direction dose). Measures per axis:
NRC valence, NRC arousal, hearts; interference = combo effect vs single effect.

Runs in its own container + volume. Run: modal run --detach experiments/replay_live_31b/modal_replay_compose.py
"""
from __future__ import annotations

from pathlib import Path

import modal

APP_NAME = "companion-replay-compose"
HF_HOME = "/models/hf"
REPO = Path(__file__).resolve().parent.parent.parent
SCEN = REPO / "results" / "replay-live-31b-2026-10-03" / "raw" / "scenarios.json"
DIRS = Path("/home/vruizes/.hermes/cache/scratch/compose-dirs.npz")

MODEL_ID = "google/gemma-4-31B-it"
MODEL_REV = "842da3794eaa0b77d5f08bae87a17459d91ff475"
S42 = 42.4
N_SAMPLES = 3
TEMP = 0.8
MASTER_SEED = 20261001
# direction -> layer index in model.layers (0-based); output of layers[i] == hs_{i+1}
DIR_LAYER = {"valence": 35, "love": 35, "arousal": 34}
CONDS = [("B2", []), ("V42", ["valence"]), ("A42", ["arousal"]), ("L42", ["love"]),
         ("VA42", ["valence", "arousal"]), ("VAL42", ["valence", "arousal", "love"])]
MAX_NEW = 200

app = modal.App(APP_NAME)
models_volume = modal.Volume.from_name("emotion-models")
out_volume = modal.Volume.from_name("companion-replay-compose", create_if_missing=True)

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
    .add_local_file(str(DIRS), "/in/compose-dirs.npz")
)


@app.function(
    image=image, gpu="L40S", cpu=4.0, memory=65536,
    volumes={"/models": models_volume, "/out": out_volume},
    secrets=[modal.Secret.from_name("huggingface")],
    timeout=3600, retries=1, max_containers=1,
)
def replay_compose() -> str:
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
    print(f"[compose] model loaded in {time.time() - t0:.0f}s", flush=True)

    z = np.load("/in/compose-dirs.npz")
    vecs = {k: torch.tensor(z[k].astype(np.float64) * S42, dtype=torch.float32, device="cuda")
            for k in ("valence", "arousal", "love")}
    scenarios = json.loads(Path("/in/scenarios.json").read_text())

    class MultiSteer:
        """One forward hook per involved layer; combined delta per layer."""

        def __init__(self, layer_deltas):
            self.handles = []
            self.layer_deltas = layer_deltas  # list[(layer_mod, delta_tensor)]

        def __enter__(self):
            for mod, delta in self.layer_deltas:
                self.handles.append(mod.register_forward_hook(self._make(delta)))
            return self

        @staticmethod
        def _make(delta):
            def fn(module, inp, out):
                h = out[0] if isinstance(out, tuple) else out
                h = h + delta.to(h.dtype)
                return (h,) + out[1:] if isinstance(out, tuple) else h
            return fn

        def __exit__(self, *a):
            for h in self.handles:
                h.remove()
            return False

    from contextlib import nullcontext

    results = []

    def build_payload(complete: bool) -> dict:
        return {"model": MODEL_ID, "revision": MODEL_REV, "s42": S42, "temp": TEMP,
                "n_samples": N_SAMPLES, "master_seed": MASTER_SEED,
                "dir_layer": DIR_LAYER, "conditions": [c[0] for c in CONDS],
                "complete": complete, "results": results}

    for s in scenarios:
        for cname, active in CONDS:
            msgs = [{"role": "system", "content": s["persona"]}] + s["variants"]["B2"]
            text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
            enc = tok(text, return_tensors="pt").to("cuda")
            n_in = enc["input_ids"].shape[1]
            for si in range(N_SAMPLES):
                seed = MASTER_SEED + s["call_id"] * 10 + si
                torch.manual_seed(seed)
                if active:
                    per_layer = {}
                    for k in active:
                        per_layer.setdefault(DIR_LAYER[k], []).append(vecs[k])
                    ld = [(layers[li], torch.stack(vs).sum(0)) for li, vs in per_layer.items()]
                    ctx = MultiSteer(ld)
                else:
                    ctx = nullcontext()
                tg = time.time()
                with ctx:
                    gen = model.generate(**enc, max_new_tokens=MAX_NEW, do_sample=True,
                                         temperature=TEMP)
                out = tok.decode(gen[0][n_in:], skip_special_tokens=True).strip()
                results.append({"call_id": s["call_id"], "condition": cname, "active": active,
                                "sample": si, "seed": seed, "n_in": n_in, "text": out,
                                "seconds": round(time.time() - tg, 1)})
                print(f"[compose] call {s['call_id']} {cname} s{si}: {out[:70]!r}", flush=True)
                torch.cuda.empty_cache()
        Path("/out/generations-compose.json").write_text(json.dumps(build_payload(False), indent=1) + "\n")
        out_volume.commit()
        print(f"[compose] checkpoint: call {s['call_id']} done ({len(results)} gens)", flush=True)
    Path("/out/generations-compose.json").write_text(json.dumps(build_payload(True), indent=1) + "\n")
    out_volume.commit()
    print(f"[compose] saved ({len(results)} gens) in {time.time() - t0:.0f}s", flush=True)
    return f"done: {len(results)} generations in {time.time() - t0:.0f}s"


@app.local_entrypoint()
def main():
    print("[local] launching composition/interference replay (B2/V42/A42/L42/VA42/VAL42 @ 0.8)")
    fc = replay_compose.spawn()
    print(f"[local] spawned (call id: {fc.object_id}); safe to disconnect")
