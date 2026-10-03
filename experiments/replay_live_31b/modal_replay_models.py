"""Cross-model extension: same five frozen live calls on the rest of the gemma-4 family.

Conditions A / B2 / C (each model's own chat-format valence direction + own disp scale
from the P2d native battery). One L40S container, models loaded sequentially.
Run: modal run --detach experiments/replay_live_31b/modal_replay_models.py
"""
from __future__ import annotations

from pathlib import Path

import modal

APP_NAME = "companion-replay-models"
HF_HOME = "/models/hf"
REPO = Path(__file__).resolve().parent.parent.parent
SCEN = REPO / "results" / "replay-live-31b-2026-10-03" / "raw" / "scenarios.json"
EGD = Path("/home/vruizes/.hermes/projects/emotion-geometry/diagnostics")

MODELS = {
    "gemma-4-e2b": {"hf": "google/gemma-4-E2B-it", "rev": "3e22461f65e89153144f8adb70e3b8c2cc9845a7",
                    "hl": 19, "s": 4.649819374084473, "dirs": EGD / "p2-directions-chat-gemma-4-e2b.npz"},
    "gemma-4-e4b": {"hf": "google/gemma-4-E4B-it", "rev": "ee0ef6023621cff504d758262d4e04895a5af4a2",
                    "hl": 14, "s": 8.954622268676758, "dirs": EGD / "p2-directions-chat-gemma-4-e4b.npz"},
    "gemma-4-12b": {"hf": "google/gemma-4-12B-it", "rev": "707f0a3b8a3c7ad586ed01e27eafbad8a27dd0f7",
                    "hl": 32, "s": 4.637953758239746, "dirs": EGD / "p2-directions-chat-gemma-4-12b.npz"},
}
CONDS = [("A", "A", False), ("B2", "B2", False), ("C", "B2", True)]
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
)
for name, spec in MODELS.items():
    image = image.add_local_file(str(spec["dirs"]), f"/in/{name}.npz")


@app.function(
    image=image, gpu="L40S", cpu=4.0, memory=65536,
    volumes={"/models": models_volume, "/out": out_volume},
    secrets=[modal.Secret.from_name("huggingface")],
    timeout=3600, retries=0, max_containers=1,
)
def replay_models() -> str:
    import gc
    import json
    import time

    import numpy as np
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    scenarios = json.loads(Path("/in/scenarios.json").read_text())
    results = []
    t0 = time.time()
    for name, spec in MODELS.items():
        t1 = time.time()
        tok = AutoTokenizer.from_pretrained(spec["hf"], revision=spec["rev"])
        model = AutoModelForCausalLM.from_pretrained(
            spec["hf"], revision=spec["rev"],
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
        z = np.load(f"/in/{name}.npz")
        d_val = z[f"L{spec['hl']}"][3].astype(np.float64)
        delta = (1.0 * spec["s"]) * torch.tensor(d_val, dtype=torch.float32, device="cuda")
        print(f"[xmodels] {name}: loaded in {time.time() - t1:.0f}s, layers={len(layers)}", flush=True)

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

        for s in scenarios:
            for cname, vname, steer in CONDS:
                msgs = [{"role": "system", "content": s["persona"]}] + s["variants"][vname]
                text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
                enc = tok(text, return_tensors="pt").to("cuda")
                n_in = enc["input_ids"].shape[1]
                ctx = SteerHook(layers[spec["hl"] - 1], delta) if steer else nullcontext()
                tg = time.time()
                with ctx:
                    gen = model.generate(**enc, max_new_tokens=MAX_NEW, do_sample=False)
                out = tok.decode(gen[0][n_in:], skip_special_tokens=True).strip()
                results.append({"model": name, "call_id": s["call_id"], "condition": cname,
                                "steer": steer, "n_in": n_in, "text": out,
                                "seconds": round(time.time() - tg, 1)})
                print(f"[xmodels] {name} call {s['call_id']} {cname}: {n_in} in -> {out[:80]!r}", flush=True)
        del model, tok
        gc.collect()
        torch.cuda.empty_cache()
    payload = {"models": {k: {"hf": v["hf"], "rev": v["rev"], "hl": v["hl"], "s": v["s"]}
                          for k, v in MODELS.items()},
               "conditions": [c[0] for c in CONDS], "results": results}
    Path("/out/generations-xmodels.json").write_text(json.dumps(payload, indent=1) + "\n")
    out_volume.commit()
    print(f"[xmodels] saved ({len(results)} gens) in {time.time() - t0:.0f}s", flush=True)
    return f"done: {len(results)} generations in {time.time() - t0:.0f}s"


@app.local_entrypoint()
def main():
    print("[local] launching cross-model replay (e2b/e4b/12b)")
    print(replay_models.remote())
