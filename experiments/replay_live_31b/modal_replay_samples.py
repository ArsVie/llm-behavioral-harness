"""Sampling-variance extension: A / B2 / C42 at multiple temperatures, 4 samples per cell.

Answers the main run's biggest limitation (single greedy sample per cell): how large is the
sampling spread relative to the card effect and the dose effect? Seeds are fixed per sample
(MASTER_SEED + call*10 + sample) and paired across conditions.

Temps run sequentially in ONE container (avoids the Modal concurrent-commit race on the
shared output volume). Live run parity was temp 0.8; the recommended lane temp is 0.95.
Run: modal run --detach experiments/replay_live_31b/modal_replay_samples.py --temps 0.95,1.3
"""
from __future__ import annotations

from pathlib import Path

import modal

APP_NAME = "companion-replay-samples"
HF_HOME = "/models/hf"
REPO = Path(__file__).resolve().parent.parent.parent
SCEN = REPO / "results" / "replay-live-31b-2026-10-03" / "raw" / "scenarios.json"
DIRS = Path("/home/vruizes/.hermes/projects/emotion-geometry/diagnostics/p2-directions-chat-gemma-4-31b.npz")

MODEL_ID = "google/gemma-4-31B-it"
MODEL_REV = "842da3794eaa0b77d5f08bae87a17459d91ff475"
HL = 36
S42 = 42.4
N_SAMPLES = 4
DEFAULT_TEMPS = "0.95,1.3"
MASTER_SEED = 20261001
CONDS = [("A", "A", False), ("B2", "B2", False), ("C42", "B2", True)]
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
def replay_samples(temps: str = DEFAULT_TEMPS) -> str:
    import json
    import time

    import numpy as np
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    temp_list = [float(t) for t in temps.split(",")]
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
    print(f"[samples] model loaded in {time.time() - t0:.0f}s", flush=True)

    z = np.load("/in/dirs.npz")
    d_val = z[f"L{HL}"][3].astype(np.float64)
    delta = (1.0 * S42) * torch.tensor(d_val, dtype=torch.float32, device="cuda")
    scenarios = json.loads(Path("/in/scenarios.json").read_text())

    class SteerHook:
        def __init__(self, layer_mod, d):
            self.layer_mod, self.delta, self.h = layer_mod, d, None

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

    from contextlib import nullcontext

    n_total = 0
    for temp in temp_list:
        results = []
        for s in scenarios:
            for cname, vname, steer in CONDS:
                msgs = [{"role": "system", "content": s["persona"]}] + s["variants"][vname]
                text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
                enc = tok(text, return_tensors="pt").to("cuda")
                n_in = enc["input_ids"].shape[1]
                for si in range(N_SAMPLES):
                    seed = MASTER_SEED + s["call_id"] * 10 + si
                    torch.manual_seed(seed)
                    ctx = SteerHook(layers[HL - 1], delta) if steer else nullcontext()
                    tg = time.time()
                    with ctx:
                        gen = model.generate(**enc, max_new_tokens=MAX_NEW, do_sample=True,
                                             temperature=temp)
                    out = tok.decode(gen[0][n_in:], skip_special_tokens=True).strip()
                    results.append({"call_id": s["call_id"], "condition": cname, "sample": si,
                                    "seed": seed, "steer": steer, "n_in": n_in, "text": out,
                                    "seconds": round(time.time() - tg, 1)})
                    print(f"[samples t{temp:g}] call {s['call_id']} {cname} s{si}: {out[:70]!r}",
                          flush=True)
                    torch.cuda.empty_cache()
        payload = {"model": MODEL_ID, "revision": MODEL_REV, "hl": HL, "s42": S42,
                   "temperature": temp, "n_samples": N_SAMPLES, "master_seed": MASTER_SEED,
                   "conditions": [c[0] for c in CONDS], "results": results}
        Path(f"/out/generations-samples-t{temp:g}.json").write_text(json.dumps(payload, indent=1) + "\n")
        n_total += len(results)
        print(f"[samples] temp {temp:g} saved ({len(results)} gens) at {time.time() - t0:.0f}s",
              flush=True)
    out_volume.commit()
    return f"done: {n_total} generations in {time.time() - t0:.0f}s"


@app.local_entrypoint()
def main(temps: str = DEFAULT_TEMPS):
    print(f"[local] launching sampling-variance replay (temps={temps})")
    print(replay_samples.remote(temps=temps))
