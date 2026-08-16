"""P2-Qwen: emotion vectors (P2a), J-lens layer-sharded fit (P2b), H1 geometry (P3).

Pre-registered contract: docs/exp-affect-codebook-pipeline-2026-08-15.md
- P2a: contrastive activation differences per layer per axis (forward hooks).
- P2b: J-lens directions via backward (gradient checkpointing, no parameter
  grads — graph leaf = embedding output), layer-sharded accumulation, binned
  vocabulary readout over [0,1] with bin width 0.10.
  Fallback (pre-registered): emotion-vectors + nearest-VAD-label lexical
  readout if J-lens cannot fit; recorded in jlens-shim-qwen.md.
- P3: H1 gate for the Qwen family ONLY — Pearson r on HELD-OUT stimuli,
  bootstrap 95% CI (>= 1000 resamples, seeded).

All randomness via harness/determinism.py (derive_seed/seed_everything/rng_for).
Model: Qwen/Qwen3-1.7B @ pinned revision (repro_bundle.json).

Seed keys (int, documented): 1 bringup, 2 p2a, 3 p2b, 4 p3, 5 bootstrap
(axis_idx, method_idx, layer), 99 selftest. 0 is reserved (P0 smoke).

Usage:
  python scripts/p2_qwen_extract.py selftest          # synthetic math self-test
  python scripts/p2_qwen_extract.py bringup           # 1 real seq fwd+bwd (GPU)
  python scripts/p2_qwen_extract.py all [--wait-min 60]  # full pipeline
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import torch

SPIKE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SPIKE_ROOT))

from harness.determinism import MASTER_SEED, derive_seed, rng_for, seed_everything  # noqa: E402

MODEL_ID = "Qwen/Qwen3-1.7B"
MODEL_REVISION = "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
SEQ_LEN = 128
BIN_WIDTH = 0.10
N_BINS = 10
BOOT_N = 1000
GATES = {"valence": 0.60, "arousal": 0.40}
SEED_KEY = {"bringup": 1, "p2a": 2, "p2b": 3, "p3": 4, "boot": 5, "selftest": 99}
AXIS_IDX = {"valence": 0, "arousal": 1}
METHOD_IDX = {"emotion_vectors": 0, "jlens": 1}
DIAG = SPIKE_ROOT / "diagnostics"
EXTRACT = SPIKE_ROOT / "data" / "extractions" / "qwen"
STIM = SPIKE_ROOT / "data" / "stimuli"
AXES = ("valence", "arousal")

torch.backends.cuda.matmul.allow_tf32 = False
torch.backends.cudnn.deterministic = True


# ---------------------------------------------------------------------------
# Stimuli (read-only — never modify data/stimuli)
# ---------------------------------------------------------------------------
def stimuli_ready() -> bool:
    return (STIM / "READY").exists()


def load_stimuli() -> tuple[list[dict], list[dict]]:
    train, held = [], []
    for fname, out in (("train.jsonl", train), ("heldout.jsonl", held)):
        for line in (STIM / fname).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return train, held


def schema_report(rows: list[dict]) -> dict:
    keys = sorted({k for r in rows for k in r})
    axes = sorted({str(r.get("axis")) for r in rows})
    cg = sorted({str(r.get("contrast_group")) for r in rows})
    return {"n": len(rows), "keys": keys, "axis_values": axes, "contrast_groups": cg}


def axis_rows(rows: list[dict], axis: str) -> list[dict]:
    """Rows belonging to an axis. If no row carries an explicit axis field,
    every row belongs to every axis (schema fallback, documented)."""
    explicit = [r for r in rows if r.get("axis") is not None]
    if explicit:
        return [r for r in rows if str(r.get("axis")) == axis]
    return rows


def axis_value(row: dict, axis: str) -> float:
    key = "v" if axis == "valence" else "a"
    if key not in row or row[key] is None:
        raise KeyError(f"row {row.get('id')} lacks '{key}' for axis {axis}")
    return float(row[key])


def high_low_split(rows: list[dict], axis: str) -> tuple[list[dict], list[dict], str]:
    """Pre-registered: contrast_group (2 distinct values) > intensity median
    > axis-value median."""
    cgs = {str(r.get("contrast_group")) for r in rows if r.get("contrast_group") is not None}
    if len(cgs) == 2:
        groups = sorted(cgs)
        y0 = np.mean([axis_value(r, axis) for r in rows if str(r.get("contrast_group")) == groups[0]])
        y1 = np.mean([axis_value(r, axis) for r in rows if str(r.get("contrast_group")) == groups[1]])
        high_g, low_g = (groups[0], groups[1]) if y0 >= y1 else (groups[1], groups[0])
        high = [r for r in rows if str(r.get("contrast_group")) == high_g]
        low = [r for r in rows if str(r.get("contrast_group")) == low_g]
        return high, low, f"contrast_group({high_g} vs {low_g})"
    vals = [r.get("intensity") for r in rows if r.get("intensity") is not None]
    if vals and all(isinstance(v, (int, float)) for v in vals):
        med = float(np.median(vals))
        high = [r for r in rows if float(r["intensity"]) >= med]
        low = [r for r in rows if float(r["intensity"]) < med]
        return high, low, f"intensity median {med:.3f}"
    ys = [axis_value(r, axis) for r in rows]
    med = float(np.median(ys))
    high = [r for r in rows if axis_value(r, axis) >= med]
    low = [r for r in rows if axis_value(r, axis) < med]
    return high, low, f"axis-value median {med:.3f}"


# ---------------------------------------------------------------------------
# Model harness: forward activation capture + backward direction capture
# ---------------------------------------------------------------------------
class QwenHarness:
    """Single model instance; hooks capture per-layer residual-stream states.

    Conventions (documented in diagnostics/jlens-shim-qwen.md):
    - layer i output = residual stream after layer i (input to layer i+1);
      the final RMSNorm output is the head input h_L.
    - forward hooks: position-mean over ALL real tokens.
    - backward hooks: position-mean over next-token-LABELED positions
      (0..n_real-2) so the final-layer analytic check is exact.
    """

    def __init__(self) -> None:
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.tok = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
        if self.tok.pad_token_id is None:
            self.tok.pad_token = self.tok.eos_token
        t0 = time.perf_counter()
        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID, revision=MODEL_REVISION, dtype=torch.bfloat16, low_cpu_mem_usage=True
        )
        self.model.to("cuda")
        self.model.requires_grad_(False)  # no parameter grads anywhere (memory)
        self.model.eval()
        self.load_s = time.perf_counter() - t0
        self.n_layers = len(self.model.model.layers)
        self.hidden = self.model.config.hidden_size
        self.vocab = self.model.config.vocab_size

        self._x0: torch.Tensor | None = None
        self._collect_fwd = False
        self._collect_bwd = False
        self._pos_mask: torch.Tensor | None = None
        self.fwd_acts: dict = {}
        self.bwd_grads: dict = {}

        self.model.model.embed_tokens.register_forward_hook(lambda m, i, o: self._x0)
        for i, layer in enumerate(self.model.model.layers):
            layer.register_forward_hook(self._make_fwd_hook(str(i)))
            layer.register_full_backward_hook(self._make_bwd_hook(str(i)))
        self.model.model.norm.register_forward_hook(self._make_fwd_hook("norm"))
        self.model.model.norm.register_full_backward_hook(self._make_bwd_hook("norm"))

    def _tensor_of(self, out):
        return out[0] if isinstance(out, (tuple, list)) else out

    def _make_fwd_hook(self, key):
        def hook(module, inp, out):
            if not self._collect_fwd:
                return
            h = self._tensor_of(out)
            self.fwd_acts[key] = h[0][self._pos_mask].float().mean(dim=0).detach().cpu()
        return hook

    def _make_bwd_hook(self, key):
        def hook(module, grad_in, grad_out):
            if not self._collect_bwd:
                return
            g = grad_out[0]
            self.bwd_grads[key] = g[0][self._pos_mask].float().mean(dim=0).detach().cpu()
        return hook

    def tokenize(self, text: str):
        ids = self.tok(text, add_special_tokens=False).input_ids
        if len(ids) < 2:
            return None, None, len(ids), 0
        truncated = len(ids) > SEQ_LEN
        ids = ids[:SEQ_LEN]
        n_real = len(ids)
        ids = ids + [self.tok.pad_token_id] * (SEQ_LEN - n_real)
        input_ids = torch.tensor([ids], dtype=torch.long, device="cuda")
        attn = torch.tensor([[1] * n_real + [0] * (SEQ_LEN - n_real)], dtype=torch.long, device="cuda")
        return input_ids, attn, n_real, int(truncated)

    def forward_states(self, input_ids: torch.Tensor, attn: torch.Tensor, n_real: int):
        """Per-layer position-mean activations + last-real-position logits (bf16 CPU)."""
        self._collect_fwd = True
        self._pos_mask = attn[0].bool()
        self.fwd_acts = {}
        try:
            with torch.no_grad():
                out = self.model(input_ids=input_ids, attention_mask=attn, use_cache=False)
        finally:
            self._collect_fwd = False
        acts = {k: v.clone() for k, v in self.fwd_acts.items()}
        z_last = out.logits[0, n_real - 1].detach().to("cpu", dtype=torch.bfloat16)
        return acts, z_last

    def backward_directions(self, input_ids: torch.Tensor, attn: torch.Tensor, n_real: int):
        """One forward+backward of mean next-token CE; per-layer position-mean
        grads w.r.t. the layer-output residual stream (fp32 CPU) + analytic
        final-layer check stats. Graph leaf = embedding output (no param grads)."""
        self._pos_mask = attn[0].bool().clone()
        self._pos_mask[n_real - 1] = False  # labeled positions 0..n_real-2
        self._x0 = self.model.model.embed_tokens(input_ids).detach().requires_grad_(True)
        self._collect_fwd = False
        self._collect_bwd = True
        self.bwd_grads = {}
        stats = {}
        try:
            out = self.model(input_ids=input_ids, attention_mask=attn, use_cache=False)
            logits = out.logits
            labels = input_ids[:, 1:].clone()
            labels[attn[:, 1:] == 0] = -100
            loss = torch.nn.functional.cross_entropy(
                logits[:, :-1].reshape(-1, self.vocab), labels.reshape(-1)
            )
            loss.backward()
            if "norm" in self.bwd_grads:
                z = logits[0, :-1].float()
                lab = labels[0]
                mask = lab != -100
                sm = torch.softmax(z[mask], dim=-1)
                onehot = torch.zeros_like(sm)
                onehot.scatter_(1, lab[mask].unsqueeze(1), 1.0)
                n_lab = int(mask.sum())
                # cross_entropy(reduction='mean') scales per-position gradients
                # by 1/n_labeled — included so the check is scale-exact.
                ana = ((sm - onehot) @ self.model.lm_head.weight.float()).mean(dim=0) / n_lab
                auto = self.bwd_grads["norm"].to(ana.device)
                denom = ana.norm() * auto.norm() + 1e-12
                stats = {
                    "cos": float(((ana * auto).sum() / denom).detach()),
                    "rel_diff": float(((ana - auto).norm() / (ana.norm() + 1e-12)).detach()),
                }
        finally:
            self._collect_bwd = False
            self._x0 = None
        return {k: v.clone() for k, v in self.bwd_grads.items()}, stats


# ---------------------------------------------------------------------------
# Direction math (documented in jlens-shim-qwen.md)
# ---------------------------------------------------------------------------
def unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def pearson(x: np.ndarray, y: np.ndarray) -> float:
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def bootstrap_ci(x: np.ndarray, y: np.ndarray, rng: np.random.Generator, n: int = BOOT_N):
    rs = np.empty(n)
    idx = np.arange(len(x))
    for k in range(n):
        s = rng.choice(idx, size=len(x), replace=True)
        rs[k] = pearson(x[s], y[s])
    return float(np.percentile(rs, 2.5)), float(np.percentile(rs, 97.5))


def contrastive_direction(high: list[np.ndarray], low: list[np.ndarray]) -> np.ndarray:
    """P2a: mean(high) - mean(low) over per-stimulus position-mean activations."""
    return np.mean(np.stack(high), axis=0) - np.mean(np.stack(low), axis=0)


def jlens_direction(grads: list[np.ndarray], ys: np.ndarray) -> np.ndarray:
    """P2b: sum_i (y_i - mean(y)) * grad_i — value-centered J-lens direction."""
    yc = ys - ys.mean()
    return sum(w * g for w, g in zip(yc, grads))


def minmax_map(p: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip((p - lo) / (hi - lo + 1e-12), 0.0, 1.0)


def bin_index(p_tilde: np.ndarray) -> np.ndarray:
    return np.minimum(np.floor(p_tilde / BIN_WIDTH).astype(int), N_BINS - 1)


# ---------------------------------------------------------------------------
# Stage P2a — emotion vectors (forward only)
# ---------------------------------------------------------------------------
def stage_p2a(h: QwenHarness, train: list[dict], seed: int) -> dict:
    seed_everything(seed)
    out = {"model": MODEL_ID, "revision": MODEL_REVISION, "seed": seed,
           "stage": "P2a emotion vectors (forward hooks)", "axes": {}}
    directions: dict[str, dict[str, np.ndarray]] = {}
    for axis in AXES:
        rows = axis_rows(train, axis)
        high, low, split_desc = high_low_split(rows, axis)
        hi_ids = {r["id"] for r in high}
        per_stim: list[dict] = []
        for r in rows:
            input_ids, attn, n_real, _ = h.tokenize(r["text"])
            if input_ids is None:
                continue
            acts, _ = h.forward_states(input_ids, attn, n_real)
            per_stim.append({"y": axis_value(r, axis), "acts": acts, "high": r["id"] in hi_ids})
        n_used = len(per_stim)
        if n_used == 0:
            raise RuntimeError(f"axis {axis}: no usable train stimuli (all <2 tokens?)")
        layers = sorted(per_stim[0]["acts"].keys())
        y_arr = np.array([s["y"] for s in per_stim])
        axes_out: dict = {"n": n_used,
                          "n_high": sum(s["high"] for s in per_stim),
                          "n_low": n_used - sum(s["high"] for s in per_stim),
                          "split": split_desc, "layers": {}}
        directions[axis] = {}
        for L in layers:
            vecs = [s["acts"][L].numpy() for s in per_stim]
            d = contrastive_direction(
                [v for s, v in zip(per_stim, vecs) if s["high"]],
                [v for s, v in zip(per_stim, vecs) if not s["high"]],
            )
            dhat = unit(d)
            directions[axis][L] = dhat
            proj = np.array([float(v @ dhat) for v in vecs])
            tr = pearson(proj, y_arr)
            hi_v = [v for s, v in zip(per_stim, vecs) if s["high"]]
            lo_v = [v for s, v in zip(per_stim, vecs) if not s["high"]]
            sd_pooled = np.sqrt((np.var(np.stack(hi_v), axis=0).mean()
                                 + np.var(np.stack(lo_v), axis=0).mean()) / 2 + 1e-12)
            axes_out["layers"][L] = {
                "norm": float(np.linalg.norm(d)), "cohen_d": float(np.linalg.norm(d) / sd_pooled),
                "train_r": tr,
                "proj_high_mean": float(np.mean([v @ dhat for v in hi_v])),
                "proj_low_mean": float(np.mean([v @ dhat for v in lo_v])),
            }
        best = max(axes_out["layers"], key=lambda k: abs(axes_out["layers"][k]["train_r"]))
        axes_out["best_layer_by_abs_train_r"] = best
        out["axes"][axis] = axes_out
    out["_directions"] = directions
    return out


# ---------------------------------------------------------------------------
# Stage P2b — J-lens (backward, layer-sharded accumulation + vocabulary readout)
# ---------------------------------------------------------------------------
def stage_p2b(h: QwenHarness, train: list[dict], seed: int) -> dict:
    seed_everything(seed)
    h.model.train()
    h.model.gradient_checkpointing_enable()
    torch.cuda.reset_peak_memory_stats()
    torch.cuda.empty_cache()

    keys = [str(i) for i in range(h.n_layers)] + ["norm"]
    axis_rows_map = {a: axis_rows(train, a) for a in AXES}
    ybar = {a: float(np.mean([axis_value(r, a) for r in axis_rows_map[a]])) for a in AXES}
    seen: dict = {}
    for a in AXES:
        for r in axis_rows_map[a]:
            seen.setdefault(r["id"], r)
    membership = {a: {r["id"] for r in axis_rows_map[a]} for a in AXES}

    acc = {a: {k: np.zeros(h.hidden, dtype=np.float64) for k in keys} for a in AXES}
    analytic = None
    n_done = 0
    try:
        for r in seen.values():
            input_ids, attn, n_real, _ = h.tokenize(r["text"])
            if input_ids is None:
                continue
            grads, stats = h.backward_directions(input_ids, attn, n_real)
            if analytic is None and stats:
                analytic = stats
            for a in AXES:
                if r["id"] in membership[a]:
                    w = axis_value(r, a) - ybar[a]
                    for k in keys:
                        acc[a][k] += w * grads[k].numpy().astype(np.float64)
            n_done += 1
            if n_done % 25 == 0:
                torch.cuda.empty_cache()
    except torch.cuda.OutOfMemoryError:
        return {"fallback": True, "reason": "torch.cuda.OutOfMemoryError in backward pass",
                "n_stimuli_done": n_done}

    out: dict = {"model": MODEL_ID, "revision": MODEL_REVISION, "seed": seed,
                 "stage": "P2b J-lens (backward, sharded)", "fallback": False,
                 "axes": {}, "peak_alloc_mib": None, "n_stimuli": n_done,
                 "analytic_check": analytic}
    h.model.eval()
    directions: dict[str, dict[str, np.ndarray]] = {}
    for axis in AXES:
        rows = axis_rows_map[axis]
        dirs = {k: unit(acc[axis][k]) for k in keys}
        directions[axis] = dirs
        y_arr = np.array([axis_value(r, axis) for r in rows])
        # one forward pass per stimulus: projections (all layers) + z_last
        proj: dict[str, list] = {k: [] for k in keys}
        z_list: list = []
        for r in rows:
            input_ids, attn, n_real, _ = h.tokenize(r["text"])
            if input_ids is None:
                continue
            acts, z_last = h.forward_states(input_ids, attn, n_real)
            z_list.append(z_last)
            for k in keys:
                proj[k].append(float(acts[k].numpy() @ dirs[k]) if k in acts else float("nan"))
        layer_out: dict = {}
        for k in keys:
            ok = ~np.isnan(proj[k])
            layer_out[k] = {"train_r": pearson(np.array(proj[k])[ok], y_arr[ok]) if ok.sum() > 2 else float("nan")}
        final_key = "norm"  # head-input space: the space W_U maps to logits
        p_all = np.array([v for v in proj[final_key] if not np.isnan(v)])
        pmin, pmax = float(p_all.min()), float(p_all.max())
        p_tilde = minmax_map(p_all, pmin, pmax)
        bins_idx = bin_index(p_tilde)
        bin_acc = [None] * N_BINS
        bin_n = np.zeros(N_BINS, int)
        bin_y = np.zeros(N_BINS)
        for r, b, zv in zip(rows, bins_idx, z_list):
            sm = torch.softmax(zv.float(), dim=-1).numpy()
            bin_acc[b] = sm if bin_acc[b] is None else bin_acc[b] + sm
            bin_n[b] += 1
            bin_y[b] += axis_value(r, axis)
        toks = h.tok
        bins_out = []
        for b in range(N_BINS):
            if bin_n[b] == 0:
                bins_out.append({"bin": b, "center": (b + 0.5) * BIN_WIDTH, "n": 0,
                                 "mean_y": None, "top_tokens": [], "fallback": "nearest non-empty bin"})
                continue
            dist = bin_acc[b] / bin_n[b]
            top = np.argsort(dist)[::-1]
            top_tokens = []
            for t in top:
                s = toks.decode([int(t)], skip_special_tokens=True)
                if s.strip():
                    top_tokens.append([s, float(dist[t])])
                if len(top_tokens) == 30:
                    break
            bins_out.append({"bin": b, "center": (b + 0.5) * BIN_WIDTH, "n": int(bin_n[b]),
                             "mean_y": float(bin_y[b] / bin_n[b]), "top_tokens": top_tokens,
                             "fallback": None})
        empty = [b for b in range(N_BINS) if bin_n[b] == 0]
        for b in empty:
            near = min([i for i in range(N_BINS) if bin_n[i] > 0], key=lambda i: abs(i - b))
            bins_out[b]["fallback"] = f"copied from bin {near} (n={int(bin_n[near])})"
            bins_out[b]["top_tokens"] = bins_out[near]["top_tokens"]
            bins_out[b]["mean_y"] = bins_out[near]["mean_y"]
        # axis-level J-lens token scores: softmax(W_U @ d_norm)
        wu = h.model.lm_head.weight.detach().float()
        scores = torch.softmax(wu @ torch.from_numpy(dirs["norm"].astype(np.float32)), dim=-1).numpy()
        dir_tokens = []
        for t in np.argsort(scores)[::-1]:
            s = toks.decode([int(t)], skip_special_tokens=True)
            if s.strip():
                dir_tokens.append([s, float(scores[t])])
            if len(dir_tokens) == 20:
                break
        best = max(layer_out, key=lambda k: abs(layer_out[k]["train_r"]))
        out["axes"][axis] = {
            "n": len(rows), "y_mean": ybar[axis],
            "direction_layers": layer_out, "best_layer_by_abs_train_r": best,
            "binning": {"bin_width": BIN_WIDTH, "n_bins": N_BINS, "p_min": pmin, "p_max": pmax,
                        "map": "minmax(train projection on d_norm) -> [0,1]",
                        "direction_layer": final_key,
                        "direction_train_r": layer_out[final_key]["train_r"]},
            "empty_bins": empty, "bins": bins_out,
            "readout": {"method": "softmax(mean last-pos logits, T=1.0)", "top_tokens_per_bin": 30},
            "jlens_direction_tokens": dir_tokens,
        }
    out["peak_alloc_mib"] = round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1)
    out["_directions"] = directions
    return out


# ---------------------------------------------------------------------------
# Stage P3 — H1 geometry on held-out (Qwen-family gate)
# ---------------------------------------------------------------------------
def stage_p3(h: QwenHarness, held: list[dict], emo_vec: dict, jlens: dict, seed: int) -> dict:
    seed_everything(seed)
    h.model.eval()
    out: dict = {"model": MODEL_ID, "revision": MODEL_REVISION, "seed": seed,
                 "stage": "P3 H1 geometry (held-out)", "axes": {}, "n_heldout": len(held)}
    for axis in AXES:
        rows = axis_rows(held, axis)
        per_stim = []
        for r in rows:
            input_ids, attn, n_real, _ = h.tokenize(r["text"])
            if input_ids is None:
                continue
            acts, _ = h.forward_states(input_ids, attn, n_real)
            per_stim.append({"y": axis_value(r, axis), "acts": acts})
        y_arr = np.array([s["y"] for s in per_stim])
        n = len(per_stim)
        layers = sorted(per_stim[0]["acts"].keys())
        dirs_ev = emo_vec["_directions"][axis]
        dirs_jl = jlens["_directions"][axis]
        prof: dict = {}
        for method, dirs in (("emotion_vectors", dirs_ev), ("jlens", dirs_jl)):
            for L in layers:
                if L not in dirs:
                    continue
                dhat = dirs[L]
                proj = np.array([float(s["acts"][L].numpy() @ dhat) for s in per_stim])
                r = pearson(proj, y_arr)
                rng = rng_for(MASTER_SEED, SEED_KEY["boot"], AXIS_IDX[axis], METHOD_IDX[method], int(L) if L != "norm" else -1)
                lo, hi = bootstrap_ci(proj, y_arr, rng)
                prof.setdefault(L, {})[method] = {"r": r, "ci": [lo, hi], "n": n}
        axes_out: dict = {"n": n, "methods": {}, "layer_profile": prof}
        for method, dirs, chosen in (
            ("emotion_vectors", dirs_ev, emo_vec["axes"][axis]["best_layer_by_abs_train_r"]),
            ("jlens", dirs_jl, jlens["axes"][axis]["best_layer_by_abs_train_r"]),
        ):
            L = str(chosen) if str(chosen) in prof and method in prof[str(chosen)] else next(iter(prof))
            blk = dict(prof[L][method])
            blk["layer"] = L
            blk["train_selected_layer"] = str(chosen)
            axes_out["methods"][method] = blk
        prim = axes_out["methods"]["emotion_vectors"]
        ci_excl0 = prim["ci"][0] > 0 or prim["ci"][1] < 0
        axes_out["gate"] = {"r_threshold": GATES[axis], "ci_excludes_0": ci_excl0}
        axes_out["verdict"] = "PASS" if (prim["r"] >= GATES[axis] and ci_excl0) else "FAIL"
        out["axes"][axis] = axes_out
    return out


# ---------------------------------------------------------------------------
# GPU etiquette / stimulus waiting
# ---------------------------------------------------------------------------
def gpu_clear(max_wait_s: int = 1800) -> None:
    waited = 0
    while waited < max_wait_s:
        try:
            used = int(subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
            ).decode().strip())
        except Exception:
            return
        if used < 1024:
            return
        print(f"[gpu] another process holds {used} MiB — waiting 60 s", flush=True)
        time.sleep(60)
        waited += 60
    print("[gpu] still busy after wait — proceeding anyway (logged)", flush=True)


def wait_for_stimuli(max_wait_min: int) -> None:
    waited = 0
    while not stimuli_ready():
        if waited >= max_wait_min * 60:
            raise SystemExit(f"stimuli not READY after {max_wait_min} min — aborting")
        print(f"[stimuli] not READY — waited {waited // 60} min, sleeping 60 s", flush=True)
        time.sleep(60)
        waited += 60
    print("[stimuli] READY marker present", flush=True)


# ---------------------------------------------------------------------------
# Synthetic self-test (no GPU, no model)
# ---------------------------------------------------------------------------
def selftest() -> None:
    seed = derive_seed(MASTER_SEED, SEED_KEY["selftest"])
    rng = np.random.default_rng(seed)
    n = 120
    y = rng.uniform(0, 1, n)
    d_true = rng.normal(size=2048)
    d_true /= np.linalg.norm(d_true)
    acts = np.stack([0.5 * np.ones(2048) + yi * 2.0 * d_true + rng.normal(0, 0.05, 2048) for yi in y])
    grads = acts + rng.normal(0, 0.02, acts.shape)
    hi = y >= np.median(y)
    d_ev = contrastive_direction(list(acts[hi]), list(acts[~hi]))
    cos_ev = float(d_ev @ d_true / (np.linalg.norm(d_ev) * np.linalg.norm(d_true) + 1e-12))
    assert cos_ev > 0.9, f"P2a recovery failed: cos={cos_ev}"
    d_jl = jlens_direction(list(grads), y)
    cos_jl = float(d_jl @ d_true / (np.linalg.norm(d_jl) * np.linalg.norm(d_true) + 1e-12))
    assert cos_jl > 0.9, f"P2b recovery failed: cos={cos_jl}"
    proj = acts @ unit(d_ev)
    r = pearson(proj, y)
    boot_rng = rng_for(MASTER_SEED, SEED_KEY["boot"], 0, 0, 0)
    lo, hi = bootstrap_ci(proj, y, boot_rng)
    assert abs(r) > 0.8, f"selftest r too low: {r}"
    assert lo <= r <= hi, "CI does not contain r"
    pt = minmax_map(proj, proj.min(), proj.max())
    b = bin_index(pt)
    assert set(np.unique(b)) <= set(range(10))
    print(json.dumps({"selftest": "PASS", "cos_ev": round(cos_ev, 4), "cos_jl": round(cos_jl, 4),
                      "r": round(r, 4), "ci": [round(lo, 4), round(hi, 4)]}, indent=2))


# ---------------------------------------------------------------------------
# Bring-up: real model, one sequence, hooks + backward + analytic check
# ---------------------------------------------------------------------------
def bringup() -> None:
    gpu_clear()
    seed = derive_seed(MASTER_SEED, SEED_KEY["bringup"])
    seed_everything(seed)
    h = QwenHarness()
    print(f"load {h.load_s:.1f}s, layers={h.n_layers}, hidden={h.hidden}, vocab={h.vocab}", flush=True)
    text = "The quiet warmth of a slow evening settles over the small room."
    input_ids, attn, n_real, trunc = h.tokenize(text)
    print(f"tokens: n_real={n_real}, truncated={trunc}", flush=True)
    torch.cuda.reset_peak_memory_stats()
    acts, z_last = h.forward_states(input_ids, attn, n_real)
    print(f"fwd peak {torch.cuda.max_memory_allocated()/1e6:.0f} MiB, captured {len(acts)} layers", flush=True)
    h.model.train()
    h.model.gradient_checkpointing_enable()
    torch.cuda.reset_peak_memory_stats()
    grads, stats = h.backward_directions(input_ids, attn, n_real)
    peak = torch.cuda.max_memory_allocated() / (1024 * 1024)
    print(f"bwd peak {peak:.1f} MiB, captured {len(grads)} layers", flush=True)
    print(f"analytic check: cos={stats.get('cos')}, rel_diff={stats.get('rel_diff')}", flush=True)
    assert len(grads) == h.n_layers + 1, f"missing layer grads: {len(grads)}"
    assert stats.get("cos", 0) > 0.99, "analytic check failed"
    print(json.dumps({"bringup": "PASS", "bwd_peak_mib": round(peak, 1),
                      "analytic_cos": stats.get("cos"), "analytic_rel_diff": stats.get("rel_diff")}))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["selftest", "bringup", "all"])
    ap.add_argument("--wait-min", type=int, default=60)
    args = ap.parse_args()

    if args.stage == "selftest":
        selftest()
        return
    if args.stage == "bringup":
        bringup()
        return

    DIAG.mkdir(parents=True, exist_ok=True)
    EXTRACT.mkdir(parents=True, exist_ok=True)
    if stimuli_ready():
        tr, he = load_stimuli()
        print("train:", json.dumps(schema_report(tr)), flush=True)
        print("heldout:", json.dumps(schema_report(he)), flush=True)
    else:
        print("[stimuli] not READY — waiting (bounded)", flush=True)
        wait_for_stimuli(args.wait_min)
        tr, he = load_stimuli()
        print("train:", json.dumps(schema_report(tr)), flush=True)
        print("heldout:", json.dumps(schema_report(he)), flush=True)

    gpu_clear()
    seed_a = derive_seed(MASTER_SEED, SEED_KEY["p2a"])
    print("[P2a] loading model...", flush=True)
    h = QwenHarness()
    print(f"[P2a] model loaded in {h.load_s:.1f}s", flush=True)
    t0 = time.perf_counter()
    emo = stage_p2a(h, tr, seed_a)
    print(f"[P2a] done in {time.perf_counter()-t0:.1f}s", flush=True)
    emo_json = {k: v for k, v in emo.items() if not k.startswith("_")}
    (DIAG / "emotion_vectors-qwen.json").write_text(json.dumps(emo_json, indent=2) + "\n")
    np.savez(DIAG / "emotion_vectors-qwen_dirs.npz",
             **{f"{a}__{k}": v for a in emo["_directions"] for k, v in emo["_directions"][a].items()})
    print("[P2a] saved diagnostics/emotion_vectors-qwen.json + _dirs.npz", flush=True)

    gpu_clear()
    seed_b = derive_seed(MASTER_SEED, SEED_KEY["p2b"])
    print("[P2b] backward pass (J-lens)...", flush=True)
    t0 = time.perf_counter()
    jl = stage_p2b(h, tr, seed_b)
    print(f"[P2b] done in {time.perf_counter()-t0:.1f}s, peak {jl.get('peak_alloc_mib')} MiB, "
          f"fallback={jl.get('fallback')}", flush=True)
    for axis in AXES:
        ax = jl["axes"][axis]
        (EXTRACT / f"jlens_{axis}.json").write_text(json.dumps(
            {"model": MODEL_ID, "revision": MODEL_REVISION, "seed": seed_b, "axis": axis,
             "fallback": jl.get("fallback"), "n_train": ax["n"],
             "binning": ax["binning"], "empty_bins": ax["empty_bins"],
             "jlens_direction_tokens": ax["jlens_direction_tokens"], "bins": ax["bins"]},
            indent=2) + "\n")
    np.savez(DIAG / "jlens-qwen_dirs.npz",
             **{f"{a}__{k}": v for a in jl["_directions"] for k, v in jl["_directions"][a].items()})
    print("[P2b] saved data/extractions/qwen/jlens_{valence,arousal}.json + dirs npz", flush=True)

    gpu_clear()
    seed_p3 = derive_seed(MASTER_SEED, SEED_KEY["p3"])
    print("[P3] held-out geometry...", flush=True)
    t0 = time.perf_counter()
    geo = stage_p3(h, he, emo, jl, seed_p3)
    print(f"[P3] done in {time.perf_counter()-t0:.1f}s", flush=True)
    (DIAG / "geometry-qwen.json").write_text(json.dumps(geo, indent=2) + "\n")
    print("[P3] saved diagnostics/geometry-qwen.json", flush=True)
    for axis in AXES:
        ax = geo["axes"][axis]
        prim = ax["methods"]["emotion_vectors"]
        jlb = ax["methods"]["jlens"]
        print(f"[P3] {axis}: EV r={prim['r']:.3f} CI={prim['ci']} layer={prim['layer']} "
              f"| JL r={jlb['r']:.3f} CI={jlb['ci']} layer={jlb['layer']} "
              f"| VERDICT {ax['verdict']}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
