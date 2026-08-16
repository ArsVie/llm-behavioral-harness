"""P2-EVBINS: EV-projection per-bin lexical distributions for the small models.

Contract: docs/exp-affect-codebook-spike2-2026-08-16.md — Orchestrator decision
2: the codebook readout for ALL actors is EV-projection bins + softmax
last-position logits per bin. The Qwen3-8B track already produced these
(data/extractions/qwen8b/ev_bins_{valence,arousal}.json); this script produces
SCHEMA-IDENTICAL files for Qwen3-1.7B and Gemma-3-1B so P4 can build all three
codebooks with the identical readout method.

RE-DERIVED (labeled): directions, C2 layer selections, and train samples are NOT
recomputed — they are loaded from the recorded P2a diagnostics
(diagnostics/emotion_vectors-{model}.json + _dirs.npz) and the recorded sample
ids (diagnostics/sample_ids-{model}.json, the same ids P2a/P2b extracted on).
Only the forward-only readout pass is new: for each sampled train stimulus,
project the position-mean activation at the recorded C2-selected layer onto the
recorded EV direction (per axis; arousal direction is the recorded C3-
orthogonalized one), minmax-map the train projections to [0,1], bin with width
0.10 (10 bins), and per bin average softmax(last-position logits); save top-30
non-whitespace tokens per bin. Determinism: seed_everything(derive_seed(
MASTER_SEED, SEED_KEY['p2a'])) before the pass, bf16, batch 1, 128-token cap,
.cpu() before .numpy(), checkpoint-per-axis before the readout write.

Schema reference: the Qwen3-8B ev_bins files (same top-level keys, same binning
keys, same per-bin fields and types; 'quant' mirrors the 8B key set with bf16
values since the small models are unquantized — decision 6 applies to the 8B
scale point only).

Usage:
  python scripts/evbins_build.py qwen
  python scripts/evbins_build.py gemma
  python scripts/evbins_build.py verify    (schema check vs qwen8b, no GPU)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import torch

SPIKE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SPIKE_ROOT))

from harness.determinism import MASTER_SEED, derive_seed, seed_everything  # noqa: E402

BIN_WIDTH = 0.10
N_BINS = 10
AXES = ("valence", "arousal")
MODELS = ("qwen", "gemma")

# Mirrors the 8B ev_bins 'quant' key set (schema-identical); values are bf16
# full precision — the small-model track is unquantized (decision 6's 4-bit
# caveat applies to the 8B scale point only).
QUANT = {"method": "bf16 full precision (no quantization)", "load_in_4bit": False,
         "quant_type": None, "compute_dtype": "bfloat16", "double_quant": False,
         "device_map": "cuda", "lm_head": "bf16 (native)",
         "note": "small-model track (decision 6 applies to the 8B scale point only)"}

torch.backends.cuda.matmul.allow_tf32 = False
torch.backends.cudnn.deterministic = True


# ---------------------------------------------------------------------------
# P2 module constants (single source of truth: the extraction scripts)
# ---------------------------------------------------------------------------
def p2_module(model: str):
    """Load the model's P2 extraction script as a module (constants + math)."""
    path = Path(__file__).resolve().parent / f"p2_{model}_extract.py"
    spec = importlib.util.spec_from_file_location(f"p2_{model}_extract", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Forward-only harness (same conventions as the P2 harnesses)
# ---------------------------------------------------------------------------
class FwdHarness:
    """Single bf16 model instance; hooks capture per-layer residual-stream
    states (position-mean over content positions 1..L-1) and last-position
    logits. Identical sequence scheme to the P2 harnesses: BOS + content
    tokens, variable length <= SEQ_LEN, batch 1, no padding."""

    def __init__(self, model: str) -> None:
        p2 = p2_module(model)
        from transformers import AutoModelForCausalLM, AutoTokenizer

        cls = AutoModelForCausalLM
        if model == "gemma":
            from transformers import Gemma3ForCausalLM
            cls = Gemma3ForCausalLM  # mirror p2_gemma_extract.py

        self.seq_len = p2.SEQ_LEN  # 128-token cap (incl. BOS), same as extraction
        self.tok = AutoTokenizer.from_pretrained(p2.MODEL_ID, revision=p2.MODEL_REVISION)
        if self.tok.pad_token_id is None:
            self.tok.pad_token = self.tok.eos_token
        t0 = time.perf_counter()
        self.model = cls.from_pretrained(
            p2.MODEL_ID, revision=p2.MODEL_REVISION,
            dtype=torch.bfloat16, low_cpu_mem_usage=True,
        )
        self.model.to("cuda")
        self.model.requires_grad_(False)
        self.model.eval()
        self.load_s = time.perf_counter() - t0
        self.backbone = self.model.model
        if not (hasattr(self.backbone, "layers") and hasattr(self.backbone, "norm")):
            self.backbone = self.backbone.text_model  # older gemma3 layout
        self.n_layers = len(self.backbone.layers)
        cfg = self.model.config
        self.hidden = int(getattr(cfg, "hidden_size", None) or cfg.text_config.hidden_size)
        self.vocab = int(getattr(cfg, "vocab_size", None) or cfg.text_config.vocab_size)

        self._collect = False
        self._pos_mask: torch.Tensor | None = None
        self.fwd_acts: dict = {}
        for i, layer in enumerate(self.backbone.layers):
            layer.register_forward_hook(self._make_fwd_hook(str(i)))
        self.backbone.norm.register_forward_hook(self._make_fwd_hook("norm"))

    def _tensor_of(self, out):
        return out[0] if isinstance(out, (tuple, list)) else out

    def _make_fwd_hook(self, key):
        def hook(module, inp, out):
            if not self._collect:
                return
            h = self._tensor_of(out)
            # .cpu() BEFORE .numpy() (never .numpy() on a CUDA tensor)
            self.fwd_acts[key] = h[0][self._pos_mask].float().mean(dim=0).detach().cpu()
        return hook

    def tokenize(self, text: str):
        """BOS + content tokens, variable length L <= SEQ_LEN. Returns
        (input_ids [1,L], attn [1,L], L, truncated)."""
        ids = self.tok(text, add_special_tokens=False).input_ids
        if not ids:
            return None, None, 0, 0
        truncated = len(ids) > self.seq_len - 1
        bos = self.tok.bos_token_id if self.tok.bos_token_id is not None else self.model.config.bos_token_id
        ids = [bos] + ids[: self.seq_len - 1]
        L = len(ids)
        input_ids = torch.tensor([ids], dtype=torch.long, device="cuda")
        attn = torch.ones((1, L), dtype=torch.long, device="cuda")
        return input_ids, attn, L, int(truncated)

    def forward_states(self, input_ids: torch.Tensor, attn: torch.Tensor, L: int):
        """Per-layer position-mean activations (content positions) + logits at
        the last real position (CPU). Returns (acts, z_last_cpu_bf16)."""
        self._collect = True
        self._pos_mask = torch.arange(1, L, device=input_ids.device)  # exclude BOS
        self.fwd_acts = {}
        try:
            with torch.no_grad():
                out = self.model(input_ids=input_ids, attention_mask=attn, use_cache=False)
        finally:
            self._collect = False
        acts = {k: v.clone() for k, v in self.fwd_acts.items()}
        z_last = out.logits[0, L - 1].detach().to("cpu", dtype=torch.bfloat16)
        return acts, z_last


# ---------------------------------------------------------------------------
# GPU etiquette (FIXED: counts only OTHER compute-app pids — same as P2)
# ---------------------------------------------------------------------------
def gpu_clear(max_wait_s: int = 1800) -> None:
    """Wait until no OTHER process holds significant GPU memory.

    Counts only compute-app pids other than this process (total memory.used
    includes our own loaded model and would self-deadlock the wait). If the
    query fails, proceed (never block the run)."""
    own = {os.getpid()}
    waited = 0
    while waited < max_wait_s:
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-compute-apps=pid,used_memory",
                 "--format=csv,noheader,nounits"]
            ).decode().strip()
        except Exception:
            return
        other = 0
        for line in out.splitlines():
            parts = line.replace(",", " ").split()
            if len(parts) >= 2:
                try:
                    pid = int(parts[0])
                except ValueError:
                    continue
                if pid in own:
                    continue
                try:
                    other += int(parts[1])
                except ValueError:
                    pass
        if other < 1024:
            return
        print(f"[gpu] another process holds {other} MiB — waiting 60 s", flush=True)
        time.sleep(60)
        waited += 60
    print("[gpu] still busy after wait — proceeding anyway (logged)", flush=True)


# ---------------------------------------------------------------------------
# Build: forward-only readout pass -> ev_bins_{axis}.json (RE-DERIVED sources)
# ---------------------------------------------------------------------------
def build(model: str) -> None:
    if model not in MODELS:
        raise SystemExit(f"unknown model '{model}' (choose from {MODELS})")
    p2 = p2_module(model)
    diag, extract = p2.DIAG, p2.EXTRACT
    extract.mkdir(parents=True, exist_ok=True)

    # --- RE-DERIVED inputs: recorded P2a diagnostics + recorded sample ids ---
    ev_path = diag / f"emotion_vectors-{model}.json"
    dirs_path = diag / f"emotion_vectors-{model}_dirs.npz"
    sid_path = diag / f"sample_ids-{model}.json"
    for p in (ev_path, dirs_path, sid_path, p2.STIM / "train.jsonl"):
        if not p.exists():
            raise SystemExit(f"missing input {p} — run P2a/P1 first")
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    dirs = np.load(dirs_path)
    sample_ids = json.loads(sid_path.read_text(encoding="utf-8"))

    train_by_id: dict[str, dict] = {}
    for line in (p2.STIM / "train.jsonl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            r = json.loads(line)
            train_by_id[str(r["id"])] = r
    rows_by_axis: dict[str, list[dict]] = {}
    for ax in AXES:
        ids = [str(i) for i in sample_ids["axes"][ax]]
        missing = [i for i in ids if i not in train_by_id]
        if missing:
            raise SystemExit(f"{model} {ax}: {len(missing)} sample ids missing from train.jsonl")
        rows_by_axis[ax] = [train_by_id[i] for i in ids]
        print(f"[evbins] {model} {ax}: sample n={len(rows_by_axis[ax])} (recorded ids)", flush=True)

    # --- recorded C2 layers + EV directions (per axis, as P2a selected them) ---
    layers: dict[str, str] = {}
    train_r: dict[str, float] = {}
    dhat: dict[str, np.ndarray] = {}
    for ax in AXES:
        ax_ev = ev["axes"][ax]
        layer = str(ax_ev["selected_layer_c2"])
        layers[ax] = layer
        train_r[ax] = float(ax_ev["layers"][layer]["train_r"])
        key = f"{ax}__{layer}"
        if key not in dirs.files:
            raise SystemExit(f"{model}: direction key '{key}' missing from {dirs_path.name}")
        dhat[ax] = np.asarray(dirs[key], dtype=np.float32)
        print(f"[evbins] {model} {ax}: C2 layer={layer} (recorded), "
              f"train_r={train_r[ax]:.4f} (recorded), dir dim={dhat[ax].shape}", flush=True)

    seed = derive_seed(MASTER_SEED, p2.SEED_KEY["p2a"])
    print(f"[evbins] {model}: seed={seed} (derive_seed(MASTER_SEED, p2a))", flush=True)
    seed_everything(seed)

    gpu_clear()
    print(f"[evbins] {model}: loading model (bf16)...", flush=True)
    h = FwdHarness(model)
    print(f"[evbins] {model}: loaded in {h.load_s:.1f}s, layers={h.n_layers}, "
          f"hidden={h.hidden}, vocab={h.vocab}", flush=True)
    lo, hi = p2.c2_band(h.n_layers)
    for ax in AXES:
        assert lo <= int(layers[ax]) < hi, f"{model} {ax}: C2 layer {layers[ax]} outside band [{lo},{hi})"
    print(f"[evbins] {model}: C2 band [{lo},{hi}) — recorded layers in band", flush=True)

    # --- forward pass (one per unique stimulus; both axes share the pass) ---
    seen: dict[str, dict] = {}
    membership: dict[str, set] = {ax: set() for ax in AXES}
    for ax in AXES:
        for r in rows_by_axis[ax]:
            seen.setdefault(r["id"], r)
            membership[ax].add(r["id"])
    per_stim: dict[str, list] = {ax: [] for ax in AXES}
    t0 = time.perf_counter()
    for n_done, (rid, r) in enumerate(seen.items(), start=1):
        input_ids, attn, L, _ = h.tokenize(r["text"])
        if input_ids is None:
            continue
        acts, z_last = h.forward_states(input_ids, attn, L)
        for ax in AXES:
            if rid in membership[ax]:
                per_stim[ax].append({
                    "y": p2.axis_value(r, ax),
                    "p": float(acts[layers[ax]].numpy() @ dhat[ax]),
                    "z": z_last,
                })
        if n_done % 400 == 0:
            rate = n_done / (time.perf_counter() - t0)
            print(f"[evbins] {model}: {n_done}/{len(seen)} stimuli, {rate:.1f}/s, "
                  f"ETA {(len(seen) - n_done) / rate / 60:.1f} min", flush=True)
    print(f"[evbins] {model}: forward pass done in {time.perf_counter()-t0:.1f}s "
          f"({len(seen)} stimuli)", flush=True)

    # --- per-axis binning + softmax readout (identical to the 8B readout) ---
    for ax in AXES:
        stim = per_stim[ax]
        y_arr = np.array([s["y"] for s in stim])
        p_all = np.array([s["p"] for s in stim])
        pmin, pmax = float(p_all.min()), float(p_all.max())
        p_tilde = p2.minmax_map(p_all, pmin, pmax)
        bins_idx = p2.bin_index(p_tilde)
        bin_acc: list = [None] * N_BINS
        bin_n = np.zeros(N_BINS, int)
        bin_y = np.zeros(N_BINS)
        for s, b in zip(stim, bins_idx):
            sm = torch.softmax(s["z"].float(), dim=-1).numpy()  # z already on CPU
            bin_acc[b] = sm if bin_acc[b] is None else bin_acc[b] + sm
            bin_n[b] += 1
            bin_y[b] += s["y"]
        empty = [b for b in range(N_BINS) if bin_n[b] == 0]
        bins_out = []
        for b in range(N_BINS):
            if bin_n[b] == 0:
                bins_out.append({"bin": b, "center": (b + 0.5) * BIN_WIDTH, "n": 0,
                                 "mean_y": None, "top_tokens": [], "fallback": None})
                continue
            dist = bin_acc[b] / bin_n[b]
            top = np.argsort(dist)[::-1]
            top_tokens = []
            for t in top:
                s = h.tok.decode([int(t)], skip_special_tokens=True)
                if s.strip():
                    top_tokens.append([s, float(dist[t])])
                if len(top_tokens) == 30:
                    break
            bins_out.append({"bin": b, "center": (b + 0.5) * BIN_WIDTH, "n": int(bin_n[b]),
                             "mean_y": float(bin_y[b] / bin_n[b]), "top_tokens": top_tokens,
                             "fallback": None})
        for b in empty:
            near = min([i for i in range(N_BINS) if bin_n[i] > 0], key=lambda i: abs(i - b))
            bins_out[b]["fallback"] = f"copied from bin {near} (n={int(bin_n[near])})"
            bins_out[b]["top_tokens"] = bins_out[near]["top_tokens"]
            bins_out[b]["mean_y"] = bins_out[near]["mean_y"]
        bin_meta = {"model": p2.MODEL_ID, "revision": p2.MODEL_REVISION, "seed": seed,
                    "axis": ax, "quant": QUANT, "n_train": len(stim),
                    "binning": {"bin_width": BIN_WIDTH, "n_bins": N_BINS,
                                "p_min": pmin, "p_max": pmax,
                                "map": "minmax(train projection on d_norm) -> [0,1]",
                                "direction_layer": layers[ax],
                                "direction_method": "emotion_vectors (EV)",
                                "direction_train_r": train_r[ax],
                                "c3_orthogonalized": ax == "arousal"},
                    "empty_bins": empty, "bins": bins_out}
        # CHECKPOINT (crash resilience): bins + counts + mean_y BEFORE the
        # final artifact with top_tokens (same two-write pattern as the 8B).
        (extract / f"ev_bins_{ax}.json").write_text(json.dumps(
            {**bin_meta, "checkpoint": True}, indent=2) + "\n")
        print(f"[evbins] {model} {ax}: checkpoint ev_bins_{ax}.json written "
              f"(n={len(stim)}, empty={empty}, layer={layers[ax]})", flush=True)
        (extract / f"ev_bins_{ax}.json").write_text(json.dumps(
            {**bin_meta, "checkpoint": False}, indent=2) + "\n")
        print(f"[evbins] {model} {ax}: final ev_bins_{ax}.json written", flush=True)
    print(f"[evbins] {model}: DONE", flush=True)


# ---------------------------------------------------------------------------
# Verify: schema equivalence vs the Qwen3-8B ev_bins files (no GPU)
# ---------------------------------------------------------------------------
def verify() -> None:
    ref = {}
    for ax in AXES:
        ref[ax] = json.loads(
            (SPIKE_ROOT / "data" / "extractions" / "qwen8b" / f"ev_bins_{ax}.json")
            .read_text(encoding="utf-8"))
    failures: list[str] = []
    for model in MODELS:
        for ax in AXES:
            path = SPIKE_ROOT / "data" / "extractions" / model / f"ev_bins_{ax}.json"
            d = json.loads(path.read_text(encoding="utf-8"))
            r = ref[ax]
            tag = f"{model}/{ax}"
            if sorted(d.keys()) != sorted(r.keys()):
                failures.append(f"{tag}: top-level keys differ "
                                f"{sorted(r.keys())} vs {sorted(d.keys())}")
            if sorted(d["binning"].keys()) != sorted(r["binning"].keys()):
                failures.append(f"{tag}: binning keys differ")
            if sorted(d["quant"].keys()) != sorted(r["quant"].keys()):
                failures.append(f"{tag}: quant keys differ")
            if d["checkpoint"] is not False:
                failures.append(f"{tag}: checkpoint != False")
            if len(d["bins"]) != len(r["bins"]):
                failures.append(f"{tag}: bins len {len(d['bins'])} != {len(r['bins'])}")
            for b in d["bins"]:
                if sorted(b.keys()) != sorted(r["bins"][0].keys()):
                    failures.append(f"{tag}: bin field keys differ {sorted(b.keys())}")
                    break
                if not isinstance(b["bin"], int) or not isinstance(b["center"], float) \
                        or not isinstance(b["n"], int) or not isinstance(b["mean_y"], float) \
                        or not (b["fallback"] is None or isinstance(b["fallback"], str)):
                    failures.append(f"{tag}: bin {b['bin']} field types off")
                if b["n"] == 0:
                    failures.append(f"{tag}: bin {b['bin']} EMPTY (n=0)")
                if len(b["top_tokens"]) != 30:
                    failures.append(f"{tag}: bin {b['bin']} has {len(b['top_tokens'])} top tokens")
                for tok, prob in b["top_tokens"]:
                    if not isinstance(tok, str) or not isinstance(prob, float) \
                            or not (0.0 <= prob <= 1.0):
                        failures.append(f"{tag}: bin {b['bin']} malformed top token")
                        break
            for b in d["bins"]:
                if b["n"] > 0 and b["fallback"] is not None:
                    failures.append(f"{tag}: bin {b['bin']} has fallback but n>0")
            if d["empty_bins"] != []:
                failures.append(f"{tag}: empty_bins {d['empty_bins']} (reference has none)")
    if failures:
        print("VERIFY FAIL:")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("VERIFY PASS: schema identical to qwen8b for all 4 files "
          "(top-level keys, binning keys, quant keys, bin fields/types, "
          "10 non-empty bins, 30 [str,float] top tokens per bin)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=list(MODELS) + ["verify"])
    args = ap.parse_args()
    if args.stage == "verify":
        verify()
        return
    build(args.stage)


if __name__ == "__main__":
    main()
