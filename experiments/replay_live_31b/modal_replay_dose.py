"""Dose-response extension: valence steering on the frozen live calls across scales.

Endpoints from the main runs: s=14.12 (coherent, inert) and s=73.2 (degenerate).
Intermediate grid: {21.2, 28.2, 42.4, 56.5} (1.5x .. 4x disp). Condition C only
(B2 + steer). One L40S container, single model load.
Run: modal run --detach experiments/replay_live_31b/modal_replay_dose.py
"""
from __future__ import annotations

from pathlib import Path

import modal

APP_NAME = "companion-replay-dose"
HF_HOME = "/models/hf"
REPO = Path(__file__).resolve().parent.parent.parent
SCEN = REPO / "results" / "replay-live-31b-2026-10-03" / "raw" / "scenarios.json"
DIRS = Path("/home/vruizes/.hermes/projects/emotion-geometry/diagnostics/p2-directions-chat-gemma-4-31b.npz")

MODEL_ID = "google/gemma-4-31B-it"
MODEL_REV = "842da3794eaa0b77d5f08bae87a17459d91ff475"
HL = 36
DOSES = [21.2, 28.2, 42.4, 56.5]
MAX_NEW = 200

app = modal.App(APP_NAME)
models_volume = modal.Volume.from_name("emotion-models")
out_volume = modal.Volume.from_name("companion-replay", create_if_missing=True)

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
    timeout=3600, retries=0, max_containers=1,
)
def replay_dose() -> str:
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
    print(f"[dose] model loaded in {time.time() - t0:.0f}s", flush=True)

    z = np.load("/in/dirs.npz")
    d_val = z[f"L{HL}"][3].astype(np.float64)
    scenarios = json.loads(Path("/in/scenarios.json").read_text())

    class SteerHook:
        def __init__(self, layer_mod, delta):
            self.layer_mod, self.delta, self.h = layer_mod, delta, None

        def __enter__(self):
            self.h = self.layer_mod.register_forward_hook(self._fn)
            return self

        def _fn(self, module, inp, out):
            if isinstance(out, tuple):
                return (out[0] + self.delta.to(out[0].dtype),) + out[1:]
            return out + self.delta.to(out.dtype)

        def __exit__(self, *a):
            if self.h is not None:
                self.h.remove()
            return False

    results = []
    for s_scale in DOSES:
        delta = (1.0 * s_scale) * torch.tensor(d_val, dtype=torch.float32, device="cuda")
        for s in scenarios:
            msgs = [{"role": "system", "content": s["persona"]}] + s["variants"]["B2"]
            text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
            enc = tok(text, return_tensors="pt").to("cuda")
            n_in = enc["input_ids"].shape[1]
            tg = time.time()
            with SteerHook(layers[HL - 1], delta):
                gen = model.generate(**enc, max_new_tokens=MAX_NEW, do_sample=False)
            out = tok.decode(gen[0][n_in:], skip_special_tokens=True).strip()
            results.append({"scale_s": s_scale, "call_id": s["call_id"], "condition": "C",
                            "steer": True, "n_in": n_in, "text": out,
                            "seconds": round(time.time() - tg, 1)})
            print(f"[dose] s={s_scale} call {s['call_id']}: {out[:90]!r}", flush=True)
            torch.cuda.empty_cache()
    payload = {"model": MODEL_ID, "revision": MODEL_REV, "hl": HL, "doses": DOSES, "results": results}
    Path("/out/generations-dose.json").write_text(json.dumps(payload, indent=1) + "\n")
    out_volume.commit()
    print(f"[dose] saved ({len(results)} gens) in {time.time() - t0:.0f}s", flush=True)
    return f"done: {len(results)} generations in {time.time() - t0:.0f}s"


@app.local_entrypoint()
def main():
    print("[local] launching dose-response replay")
    print(replay_dose.remote())
