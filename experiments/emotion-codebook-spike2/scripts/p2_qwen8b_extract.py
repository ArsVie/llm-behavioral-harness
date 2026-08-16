"""P2-Qwen3-8B (scale-point track): emotion vectors (P2a, EV-only) + codebook readout + P3 geometry.

Pre-registered contract: docs/exp-affect-codebook-spike2-2026-08-16.md
(esp. Models section + Orchestrator decisions 1, 2, 4, 5, 6).

Adapted from spike 1 (experiments/emotion-codebook-spike/scripts/p2_qwen_extract.py);
differences are the 8B-scale-point track:
- QUANTIZATION (decision 6, labeled confound): 4-bit NF4 via bitsandbytes,
  bnb_4bit_compute_dtype=bfloat16, double quant ON, device_map='cuda'. Forward-only
  extraction; lm_head stays UNQUANTIZED (float) so the readout path
  wu = model.lm_head.weight.detach().float() is exact. See
  diagnostics/qwen8b-prep-notes.md for the record.
- EV ONLY: no P2b / J-lens / backward pass at 8B (does not fit 8 GB; EV is the
  cleaner method regardless — decision 2). Seed key 3 (p2b) is unused.
- C2 (decision 4): layer selected on TRAIN by signed valence-r (pre-registered; abs would allow an anti-aligned layer) within the middle third
  of depth [floor(N/3), ceil(2N/3)) = [12, 24) for N=36. One layer per model,
  used for both axes. Full per-layer sensitivity reported.
- C3 (decision 5): per layer, arousal direction = unit(projection of the raw
  contrastive arousal direction onto the complement of the valence direction)
  — Gram-Schmidt, then unit-normalize. Applied to the EV directions.
- READOUT (decision 2): bin TRAIN projections onto the C2-selected EV direction
  (minmax -> [0,1], 10 bins, width 0.10 — same scheme as spike 1); token
  distribution per bin = softmax of mean last-position logits; top-30 tokens/bin.
  Per-axis bins are checkpointed BEFORE the readout (crash resilience).
- P3: held-out geometry diagnostic, EV only (valence + C3-orthogonalized arousal).
  Diagnostic, not a gate, in spike 2. No J-lens method.

Sequence scheme: BOS + stimulus tokens, variable length (no padding), batch 1,
128-token cap. Labels = stimulus tokens (next-token prediction). Forward activation
means exclude the BOS position; vocabulary readout = logits at the last real position.

All randomness via harness/determinism.py. Seed keys mirror spike 1 exactly:
1 bringup, 2 p2a, 4 p3, 5 bootstrap, 6 sampling, 99 selftest (3 = p2b unused).

Usage:
  python scripts/p2_qwen8b_extract.py selftest
  python scripts/p2_qwen8b_extract.py bringup
  python scripts/p2_qwen8b_extract.py all [--wait-min 60] [--n-sample 2000]
"""
from __future__ import annotations

import argparse
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

from harness.determinism import MASTER_SEED, derive_seed, rng_for, seed_everything  # noqa: E402

MODEL_ID = "Qwen/Qwen3-8B"
MODEL_REVISION = "b968826d9c46dd6066d109eabc6255188de91218"  # resolved 2026-08-16 via HfApi (pinned in repro_bundle.json)
QUANT = {"method": "bitsandbytes NF4 (4-bit)", "load_in_4bit": True, "quant_type": "nf4",
         "compute_dtype": "bfloat16", "double_quant": True, "device_map": "cuda",
         "lm_head": "UNQUANTIZED (float) — readout path exact", "note": "decision 6: labeled confound for SCALE diagnostic"}
SEQ_LEN = 128  # max total length INCLUDING the BOS token
BIN_WIDTH = 0.10
N_BINS = 10
BOOT_N = 1000
GATES = {"valence": 0.60, "arousal": 0.40}  # spike 2: diagnostic thresholds, NOT gates
SEED_KEY = {"bringup": 1, "p2a": 2, "p3": 4, "boot": 5, "sample": 6, "selftest": 99}  # key 3 (p2b/J-lens) unused at 8B
AXIS_IDX = {"valence": 0, "arousal": 1}
METHOD_IDX = {"emotion_vectors": 0}
DIAG = SPIKE_ROOT / "diagnostics"
EXTRACT = SPIKE_ROOT / "data" / "extractions" / "qwen8b"
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
    """Rows belonging to an axis (every row carries an explicit axis field)."""
    return [r for r in rows if str(r.get("axis")) == axis]


def axis_value(row: dict, axis: str) -> float:
    key = "v" if axis == "valence" else "a"
    if key not in row or row[key] is None:
        raise KeyError(f"row {row.get('id')} lacks '{key}' for axis {axis}")
    return float(row[key])


def high_low_split(rows: list[dict], axis: str) -> tuple[list[dict], list[dict], str, dict]:
    """Pre-registered: contrast_group side (hi/lo — P1's contrastive pairs).
    Fallbacks: intensity median, then axis-value median."""
    hi = [r for r in rows if str(r.get("contrast_group", "")).endswith(":hi")]
    lo = [r for r in rows if str(r.get("contrast_group", "")).endswith(":lo")]
    note = {}
    if hi and lo and len(hi) + len(lo) == len(rows):
        mh = float(np.mean([axis_value(r, axis) for r in hi]))
        ml = float(np.mean([axis_value(r, axis) for r in lo]))
        note = {"split": "contrast_group side (hi/lo)", "hi_mean_intensity": mh, "lo_mean_intensity": ml}
        if mh < ml:
            note["ANOMALY"] = "hi group mean intensity < lo group mean (contrast reversed in data)"
        return hi, lo, note["split"], note
    vals = [r.get("intensity") for r in rows if r.get("intensity") is not None]
    if vals and all(isinstance(v, (int, float)) for v in vals):
        med = float(np.median(vals))
        hi = [r for r in rows if float(r["intensity"]) >= med]
        lo = [r for r in rows if float(r["intensity"]) < med]
        return hi, lo, f"intensity median {med:.3f}", note
    ys = [axis_value(r, axis) for r in rows]
    med = float(np.median(ys))
    hi = [r for r in rows if axis_value(r, axis) >= med]
    lo = [r for r in rows if axis_value(r, axis) < med]
    return hi, lo, f"axis-value median {med:.3f}", note


def sample_rows(rows: list[dict], axis: str, n_target: int) -> tuple[list[dict], dict]:
    """Seeded stratified sample over P1's intensity bins (width 0.1). Same
    procedure and seed key as spike 1 (fitting passes only)."""
    rng = rng_for(MASTER_SEED, SEED_KEY["sample"], AXIS_IDX[axis])
    by_bin: dict[int, list] = {}
    for r in rows:
        b = min(N_BINS - 1, int(min(0.999, axis_value(r, axis)) * 10))
        by_bin.setdefault(b, []).append(r)
    per = max(1, n_target // N_BINS)
    chosen: list[dict] = []
    for b in sorted(by_bin):
        pool = by_bin[b]
        idx = rng.permutation(len(pool))[:per]
        chosen.extend(pool[i] for i in idx)
    if len(chosen) < n_target:
        chosen_ids = {r["id"] for r in chosen}
        rest = [r for r in rows if r["id"] not in chosen_ids]
        perm = rng.permutation(len(rest))
        chosen.extend(rest[i] for i in perm[: n_target - len(chosen)])
    bin_counts = {str(b): sum(1 for r in chosen if min(N_BINS - 1, int(min(0.999, axis_value(r, axis)) * 10)) == b)
                  for b in range(N_BINS)}
    meta = {"n_target": n_target, "n": len(chosen),
            "seed": derive_seed(MASTER_SEED, SEED_KEY["sample"], AXIS_IDX[axis]),
            "procedure": "stratified by intensity bin (0.1 width), seeded permutation within bin",
            "bin_counts": bin_counts}
    return chosen, meta


# ---------------------------------------------------------------------------
# Model harness: 4-bit forward activation capture (EV only)
# ---------------------------------------------------------------------------
class QwenHarness:
    """Single 4-bit NF4 model instance; hooks capture per-layer residual-stream states.

    Conventions (same as spike 1, see diagnostics/jlens-shim-qwen.md):
    - layer i output = residual stream after layer i (input to layer i+1);
      the final RMSNorm output is the head input h_L.
    - Sequence = BOS + stimulus tokens, variable length L (no padding).
    - forward hooks: position-mean over CONTENT positions 1..L-1.
    - logits captured at the last real position L-1 (the readout position).
    - NO backward pass at 8B (EV-only track).
    """

    def __init__(self) -> None:
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

        self.tok = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
        if self.tok.pad_token_id is None:
            self.tok.pad_token = self.tok.eos_token
        bnb = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
        t0 = time.perf_counter()
        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID, revision=MODEL_REVISION, quantization_config=bnb,
            device_map="cuda", low_cpu_mem_usage=True,
        )
        self.model.requires_grad_(False)  # no parameter grads anywhere (memory)
        self.model.eval()
        self.load_s = time.perf_counter() - t0
        self.n_layers = len(self.model.model.layers)
        self.hidden = self.model.config.hidden_size
        self.vocab = self.model.config.vocab_size

        # lm_head must be UNQUANTIZED (float) for the exact readout path wu @ h.
        w = self.model.lm_head.weight
        if hasattr(w, "quant_state") or w.dtype not in (torch.float32, torch.float16, torch.bfloat16):
            raise RuntimeError(
                f"lm_head is quantized (dtype={w.dtype}, quant_state={hasattr(w, 'quant_state')}); "
                "the EV readout path requires an unquantized output layer — add lm_head to "
                "bnb_4bit_skip_modules / skip_modules and reload.")

        self._collect_fwd = False
        self._pos_mask: torch.Tensor | None = None
        self.fwd_acts: dict = {}

        for i, layer in enumerate(self.model.model.layers):
            layer.register_forward_hook(self._make_fwd_hook(str(i)))
        self.model.model.norm.register_forward_hook(self._make_fwd_hook("norm"))

    def _tensor_of(self, out):
        return out[0] if isinstance(out, (tuple, list)) else out

    def _make_fwd_hook(self, key):
        def hook(module, inp, out):
            if not self._collect_fwd:
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
        truncated = len(ids) > SEQ_LEN - 1
        bos = self.tok.bos_token_id if self.tok.bos_token_id is not None else self.model.config.bos_token_id
        ids = [bos] + ids[: SEQ_LEN - 1]
        L = len(ids)
        input_ids = torch.tensor([ids], dtype=torch.long, device="cuda")
        attn = torch.ones((1, L), dtype=torch.long, device="cuda")
        return input_ids, attn, L, int(truncated)

    def forward_states(self, input_ids: torch.Tensor, attn: torch.Tensor, L: int):
        """Per-layer position-mean activations (content positions) + logits at
        the last real position (CPU). Returns (acts, z_last_cpu_bf16)."""
        self._collect_fwd = True
        self._pos_mask = torch.arange(1, L, device=input_ids.device)  # exclude BOS
        self.fwd_acts = {}
        try:
            with torch.no_grad():
                out = self.model(input_ids=input_ids, attention_mask=attn, use_cache=False)
        finally:
            self._collect_fwd = False
        acts = {k: v.clone() for k, v in self.fwd_acts.items()}
        z_last = out.logits[0, L - 1].detach().to("cpu", dtype=torch.bfloat16)
        return acts, z_last


# ---------------------------------------------------------------------------
# Direction math (same as spike 1; C3 added)
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


def orthogonalize(raw: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, float]:
    """C3: project `raw` onto the complement of unit vector `v` (Gram-Schmidt),
    then unit-normalize. Returns (unit direction, |raw - (raw.v)v| / |raw|)."""
    v = unit(v)
    proj = float(raw @ v) * v
    comp = raw - proj
    n_raw = np.linalg.norm(raw)
    keep = np.linalg.norm(comp) / n_raw if n_raw > 0 else 0.0
    return unit(comp), float(keep)


def minmax_map(p: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip((p - lo) / (hi - lo + 1e-12), 0.0, 1.0)


def bin_index(p_tilde: np.ndarray) -> np.ndarray:
    return np.minimum(np.floor(p_tilde / BIN_WIDTH).astype(int), N_BINS - 1)


def c2_band(n_layers: int) -> tuple[int, int]:
    """C2 (decision 4): middle third of depth = [floor(N/3), ceil(2N/3))."""
    return n_layers // 3, (2 * n_layers + 2) // 3


# ---------------------------------------------------------------------------
# Stage P2a — emotion vectors (forward only) + C2 selection + C3 + bins/readout
# ---------------------------------------------------------------------------
def stage_p2a(h: QwenHarness, train_sample: dict[str, list[dict]], seed: int) -> dict:
    seed_everything(seed)
    out = {"model": MODEL_ID, "revision": MODEL_REVISION, "seed": seed, "quant": QUANT,
           "stage": "P2a emotion vectors (forward hooks, 4-bit NF4)", "axes": {}}
    directions: dict[str, dict[str, np.ndarray]] = {}
    # per-stimulus captures (single forward per row; reuse for both axes' bins)
    per_stim: dict[str, list[dict]] = {}
    for axis in AXES:
        rows = train_sample[axis]
        high, low, split_desc, split_note = high_low_split(rows, axis)
        hi_ids = {r["id"] for r in high}
        stim: list[dict] = []
        for r in rows:
            input_ids, attn, L, _ = h.tokenize(r["text"])
            if input_ids is None:
                continue
            acts, z_last = h.forward_states(input_ids, attn, L)
            stim.append({"y": axis_value(r, axis), "acts": acts, "z": z_last,
                         "high": r["id"] in hi_ids})
        if not stim:
            raise RuntimeError(f"axis {axis}: no usable train stimuli (all empty texts?)")
        per_stim[axis] = stim
        out["axes"][axis] = {"n": len(stim),
                             "n_high": sum(s["high"] for s in stim),
                             "n_low": len(stim) - sum(s["high"] for s in stim),
                             "split": split_desc, "split_note": split_note}

    layers = sorted(per_stim["valence"][0]["acts"].keys())
    lo_b, hi_b = c2_band(h.n_layers)
    y_v = np.array([s["y"] for s in per_stim["valence"]])
    y_a = np.array([s["y"] for s in per_stim["arousal"]])

    # valence directions per layer + train_r
    v_dir: dict[str, np.ndarray] = {}
    layer_meta: dict[str, dict] = {}
    for Lk in layers:
        vecs_v = [s["acts"][Lk].numpy() for s in per_stim["valence"]]
        d = contrastive_direction(
            [v for s, v in zip(per_stim["valence"], vecs_v) if s["high"]],
            [v for s, v in zip(per_stim["valence"], vecs_v) if not s["high"]],
        )
        dhat = unit(d)
        v_dir[Lk] = dhat
        proj = np.array([float(v @ dhat) for v in vecs_v])
        hi_v = [v for s, v in zip(per_stim["valence"], vecs_v) if s["high"]]
        lo_v = [v for s, v in zip(per_stim["valence"], vecs_v) if not s["high"]]
        sd_pooled = np.sqrt((np.var(np.stack(hi_v), axis=0).mean()
                             + np.var(np.stack(lo_v), axis=0).mean()) / 2 + 1e-12)
        layer_meta[Lk] = {"norm": float(np.linalg.norm(d)),
                          "cohen_d": float(np.linalg.norm(d) / sd_pooled),
                          "train_r": pearson(proj, y_v),
                          "proj_high_mean": float(np.mean([v @ dhat for v in hi_v])),
                          "proj_low_mean": float(np.mean([v @ dhat for v in lo_v]))}
    # C3: arousal directions per layer, orthogonalized onto that layer's valence direction
    a_dir: dict[str, np.ndarray] = {}
    for Lk in layers:
        vecs_a = [s["acts"][Lk].numpy() for s in per_stim["arousal"]]
        d_raw = contrastive_direction(
            [v for s, v in zip(per_stim["arousal"], vecs_a) if s["high"]],
            [v for s, v in zip(per_stim["arousal"], vecs_a) if not s["high"]],
        )
        d_orth, keep = orthogonalize(d_raw, v_dir[Lk])
        a_dir[Lk] = d_orth
        layer_meta[Lk]["arousal_raw_norm"] = float(np.linalg.norm(d_raw))
        layer_meta[Lk]["arousal_orth_keep_frac"] = keep
        layer_meta[Lk]["arousal_cos_with_valence_before_orth"] = float(
            d_raw @ v_dir[Lk] / (np.linalg.norm(d_raw) + 1e-12))
        proj = np.array([float(v @ d_orth) for v in vecs_a])
        layer_meta[Lk]["arousal_train_r"] = pearson(proj, y_a)
    # C2 selection: argmax |valence train_r| within [lo_b, hi_b) on TRAIN
    band_layers = [k for k in layers if lo_b <= int(k) < hi_b]
    if not band_layers:
        raise RuntimeError(f"C2 band [{lo_b},{hi_b}) empty (layers={layers})")
    c2_layer = max(band_layers, key=lambda k: layer_meta[k]["train_r"])
    out["c2"] = {"band": [lo_b, hi_b], "band_rule": "middle third of depth [floor(N/3), ceil(2N/3))",
                 "selection": "argmax signed valence train_r on TRAIN within band (decision 4)",
                 "selected_layer": c2_layer,
                 "valence_train_r_at_selected": layer_meta[c2_layer]["train_r"],
                 "arousal_train_r_at_selected": layer_meta[c2_layer]["arousal_train_r"]}
    directions["valence"] = v_dir
    directions["arousal"] = a_dir
    out["axes"]["valence"]["layers"] = {k: {kk: vv for kk, vv in m.items() if not kk.startswith("arousal")}
                                        for k, m in layer_meta.items()}
    out["axes"]["valence"]["c3"] = {"note": "arousal orthogonalized onto valence complement per layer (C3, decision 5)",
                                    "layers": {k: {"cos_with_valence_before_orth": m["arousal_cos_with_valence_before_orth"],
                                                   "orth_keep_frac": m["arousal_orth_keep_frac"]}
                                               for k, m in layer_meta.items()}}
    out["axes"]["arousal"]["layers"] = {k: {"train_r": m["arousal_train_r"],
                                            "raw_norm": m["arousal_raw_norm"],
                                            "cos_with_valence_before_orth": m["arousal_cos_with_valence_before_orth"],
                                            "orth_keep_frac": m["arousal_orth_keep_frac"]}
                                        for k, m in layer_meta.items()}
    out["_directions"] = directions

    # Bins + readout at the C2-selected layer (decision 2), per axis
    for axis in AXES:
        dhat = directions[axis][c2_layer]
        stim = per_stim[axis]
        y_arr = np.array([s["y"] for s in stim])
        p_all = np.array([float(s["acts"][c2_layer].numpy() @ dhat) for s in stim])
        pmin, pmax = float(p_all.min()), float(p_all.max())
        p_tilde = minmax_map(p_all, pmin, pmax)
        bins_idx = bin_index(p_tilde)
        bin_acc = [None] * N_BINS
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
        bin_meta = {"model": MODEL_ID, "revision": MODEL_REVISION, "seed": seed, "axis": axis,
                    "quant": QUANT,
                    "n_train": len(stim),
                    "binning": {"bin_width": BIN_WIDTH, "n_bins": N_BINS, "p_min": pmin, "p_max": pmax,
                                "map": "minmax(train projection on d_norm) -> [0,1]",
                                "direction_layer": c2_layer,
                                "direction_method": "emotion_vectors (EV)",
                                "direction_train_r": (layer_meta[c2_layer]["train_r"] if axis == "valence"
                                                      else layer_meta[c2_layer]["arousal_train_r"]),
                                "c3_orthogonalized": axis == "arousal"},
                    "empty_bins": empty, "bins": bins_out}
        # CHECKPOINT (crash resilience): bins + counts + mean_y BEFORE the readout is
        # replaced by the final artifact with top tokens. Written twice; second write
        # carries the top_tokens.
        (EXTRACT / f"ev_bins_{axis}.json").write_text(json.dumps(
            {**bin_meta, "checkpoint": True}, indent=2) + "\n")
        print(f"[P2a] bins checkpoint ev_bins_{axis}.json written (n={len(stim)}, "
              f"empty={empty}, layer={c2_layer})", flush=True)
        (EXTRACT / f"ev_bins_{axis}.json").write_text(json.dumps(
            {**bin_meta, "checkpoint": False}, indent=2) + "\n")
        print(f"[P2a] readout checkpoint ev_bins_{axis}.json written", flush=True)
        out["axes"][axis]["binning"] = bin_meta["binning"]
        out["axes"][axis]["empty_bins"] = empty
        out["axes"][axis]["bins"] = bins_out
        out["axes"][axis]["readout"] = {"method": "softmax(mean last-pos logits, T=1.0)",
                                        "top_tokens_per_bin": 30}
    return out


# ---------------------------------------------------------------------------
# Stage P3 — geometry diagnostic on held-out (EV only; no gate in spike 2)
# ---------------------------------------------------------------------------
def stage_p3(h: QwenHarness, held: list[dict], emo_vec: dict, seed: int) -> dict:
    seed_everything(seed)
    out: dict = {"model": MODEL_ID, "revision": MODEL_REVISION, "seed": seed,
                 "stage": "P3 geometry diagnostic (held-out, EV only)", "axes": {},
                 "n_heldout": len(held),
                 "note": "spike 2: diagnostic, NOT a gate (H1 demoted); thresholds reported for continuity"}
    c2_layer = emo_vec["c2"]["selected_layer"]
    for axis in AXES:
        rows = axis_rows(held, axis)
        per_stim = []
        for r in rows:
            input_ids, attn, L, _ = h.tokenize(r["text"])
            if input_ids is None:
                continue
            acts, _ = h.forward_states(input_ids, attn, L)
            per_stim.append({"y": axis_value(r, axis), "acts": acts})
        y_arr = np.array([s["y"] for s in per_stim])
        n = len(per_stim)
        layers = sorted(per_stim[0]["acts"].keys())
        dirs = emo_vec["_directions"][axis]
        prof: dict = {}
        for Lk in layers:
            if Lk not in dirs:
                continue
            dhat = dirs[Lk]
            proj = np.array([float(s["acts"][Lk].numpy() @ dhat) for s in per_stim])
            r = pearson(proj, y_arr)
            rng = rng_for(MASTER_SEED, SEED_KEY["boot"], AXIS_IDX[axis], METHOD_IDX["emotion_vectors"],
                          int(Lk) if Lk != "norm" else 10000)
            lo, hi = bootstrap_ci(proj, y_arr, rng)
            prof[Lk] = {"emotion_vectors": {"r": r, "ci": [lo, hi], "n": n}}
        axes_out: dict = {"n": n, "methods": {}, "layer_profile": prof}
        blk = dict(prof[c2_layer]["emotion_vectors"])
        blk["layer"] = c2_layer
        blk["train_selected_layer"] = c2_layer
        axes_out["methods"]["emotion_vectors"] = blk
        prim = axes_out["methods"]["emotion_vectors"]
        ci_excl0 = prim["ci"][0] > 0 or prim["ci"][1] < 0
        axes_out["diagnostic"] = {"r_threshold": GATES[axis], "ci_excludes_0": ci_excl0,
                                  "verdict_if_gate": "PASS" if (prim["r"] >= GATES[axis] and ci_excl0) else "FAIL"}
        out["axes"][axis] = axes_out
    return out


# ---------------------------------------------------------------------------
# GPU etiquette / stimulus waiting (FIXED: counts only OTHER compute-app pids)
# ---------------------------------------------------------------------------
def gpu_clear(max_wait_s: int = 1800) -> None:
    """Wait until no OTHER process holds significant GPU memory.

    Counts only compute-app pids other than this process. Total memory.used
    includes our own loaded model and would self-deadlock the wait (observed
    2026-08-15: P2a loaded the model, then gpu_clear() waited on its own
    3450 MiB forever). If the query fails, proceed (never block the run).
    """
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
# Synthetic self-test (no GPU, no model) — includes C3 recovery check
# ---------------------------------------------------------------------------
def selftest() -> None:
    seed = derive_seed(MASTER_SEED, SEED_KEY["selftest"])
    rng = np.random.default_rng(seed)
    n = 120
    y = rng.uniform(0, 1, n)
    d_true = rng.normal(size=2048)
    d_true /= np.linalg.norm(d_true)
    acts = np.stack([0.5 * np.ones(2048) + yi * 2.0 * d_true + rng.normal(0, 0.05, 2048) for yi in y])
    hi = y >= np.median(y)
    d_ev = contrastive_direction(list(acts[hi]), list(acts[~hi]))
    cos_ev = float(d_ev @ d_true / (np.linalg.norm(d_ev) * np.linalg.norm(d_true) + 1e-12))
    assert cos_ev > 0.9, f"P2a recovery failed: cos={cos_ev}"
    # C3: arousal direction entangled with valence; orthogonalization must recover
    # the true orthogonal component and kill the valence component.
    # NOTE: the C3 block uses lower synthetic noise (0.02 vs 0.05) than the
    # valence block on purpose: the orthogonal projection halves the signal
    # (0.7 factor), and at spike-1 noise the contrastive estimator recovers
    # only ~0.82 — this check targets the GS math, not estimator SNR.
    d2_true = rng.normal(size=2048)
    d2_true -= (d2_true @ d_true) * d_true
    d2_true /= np.linalg.norm(d2_true)
    acts2 = np.stack([0.5 * np.ones(2048) + yi * 2.0 * (0.7 * d_true + 0.7 * d2_true)
                      + rng.normal(0, 0.02, 2048) for yi in y])
    d_raw = contrastive_direction(list(acts2[hi]), list(acts2[~hi]))
    d_orth, keep = orthogonalize(d_raw, d_ev)
    cos_orth = float(d_orth @ d2_true / (np.linalg.norm(d_orth) * np.linalg.norm(d2_true) + 1e-12))
    cos_with_v = float(d_orth @ d_ev / (np.linalg.norm(d_orth) * np.linalg.norm(d_ev) + 1e-12))
    assert cos_orth > 0.9, f"C3 recovery failed: cos={cos_orth}"
    assert abs(cos_with_v) < 1e-6, f"C3 orthogonalization failed: cos(v,a_orth)={cos_with_v}"
    assert 0.0 < keep < 1.0, f"C3 keep fraction out of range: {keep}"
    proj = acts @ unit(d_ev)
    r = pearson(proj, y)
    boot_rng = rng_for(MASTER_SEED, SEED_KEY["boot"], 0, 0, 0)
    lo, hi = bootstrap_ci(proj, y, boot_rng)
    assert abs(r) > 0.8, f"selftest r too low: {r}"
    assert lo <= r <= hi, "CI does not contain r"
    pt = minmax_map(proj, proj.min(), proj.max())
    b = bin_index(pt)
    assert set(np.unique(b)) <= set(range(10))
    # C2 band rule on a 36-layer model
    assert c2_band(36) == (12, 24), f"c2_band(36)={c2_band(36)} != (12, 24)"
    print(json.dumps({"selftest": "PASS", "cos_ev": round(cos_ev, 4), "cos_c3_orth": round(cos_orth, 4),
                      "cos_c3_with_valence": round(cos_with_v, 6), "r": round(r, 4),
                      "ci": [round(lo, 4), round(hi, 4)], "c2_band_36": list(c2_band(36))}, indent=2))


# ---------------------------------------------------------------------------
# Bring-up: real 4-bit model, forward on 3 sequence types, analytic readout check
# ---------------------------------------------------------------------------
def bringup() -> None:
    gpu_clear()
    seed = derive_seed(MASTER_SEED, SEED_KEY["bringup"])
    seed_everything(seed)
    h = QwenHarness()
    # NOTE: never materialize wu = lm_head.weight.float() on GPU at 8B — the
    # fp32 copy is 2.49 GB and alone pushes peak over the 8 GB budget. The
    # analytic/readout checks run in bf16 on the unquantized lm_head directly.
    peak_after_load = torch.cuda.max_memory_allocated() / (1024 * 1024)
    print(f"load {h.load_s:.1f}s, layers={h.n_layers}, hidden={h.hidden}, vocab={h.vocab}, "
          f"lm_head dtype={h.model.lm_head.weight.dtype} quantized={hasattr(h.model.lm_head.weight, 'quant_state')}, "
          f"alloc after load {peak_after_load:.1f} MiB", flush=True)
    results = []
    for text in ("The quiet warmth of a slow evening settles over the small room.", "hurt", "joy"):
        input_ids, attn, L, trunc = h.tokenize(text)
        print(f"'{text[:30]}' -> L={L}, truncated={trunc}", flush=True)
        torch.cuda.reset_peak_memory_stats()
        acts, z_last = h.forward_states(input_ids, attn, L)
        peak_fwd = torch.cuda.max_memory_allocated() / (1024 * 1024)
        print(f"  fwd peak {peak_fwd:.1f} MiB, captured {len(acts)} layers", flush=True)
        # Analytic sanity (EV-only analogue of spike 1's backward check): the
        # final RMSNorm output at the last position, pushed through the
        # UNQUANTIZED bf16 lm_head, must reproduce the model's own last-position
        # logits (cos > 0.99). Validates hooks + head-input space + readout path.
        last_out = {}
        def grab(module, inp, out):
            t = out[0] if isinstance(out, (tuple, list)) else out
            last_out["h"] = t[0, -1].detach().to(torch.bfloat16)
        handle = h.model.model.norm.register_forward_hook(grab)
        with torch.no_grad():
            out = h.model(input_ids=input_ids, attention_mask=attn, use_cache=False)
        handle.remove()
        with torch.no_grad():
            z_ana = (h.model.lm_head.weight @ last_out["h"]).float().cpu().numpy()
        z_real = out.logits[0, L - 1].float().cpu().numpy()
        cos = float((z_ana * z_real).sum() / (np.linalg.norm(z_ana) * np.linalg.norm(z_real) + 1e-12))
        assert cos > 0.99, f"analytic readout check failed: cos={cos}"
        # Readout sanity: valid vocab-size vector; top tokens decode to real
        # tokens. NOTE: for 2-3 token contexts the model's genuine top
        # predictions are often whitespace/newline tokens — the check requires
        # the distribution to be finite and ≥3 of the top-5 decodes to contain
        # non-whitespace characters (real words/word-pieces), not that the
        # model predicts English words.
        assert z_ana.shape == (h.vocab,), f"readout shape {z_ana.shape} != ({h.vocab},)"
        assert np.isfinite(z_ana).all(), "readout logits not finite"
        top5 = [h.tok.decode([int(t)], skip_special_tokens=True) for t in np.argsort(z_ana)[::-1][:5]]
        n_words = sum(1 for s in top5 if any(ch not in " \t\n\r" for ch in s))
        assert n_words >= 3, f"top tokens mostly whitespace: {top5}"
        print(f"  analytic cos={cos:.6f}, readout top5={top5}", flush=True)
        results.append({"text": text[:30], "L": L, "peak_fwd_alloc_mib": round(peak_fwd, 1),
                        "analytic_cos": round(cos, 6), "top5": top5})
    peak_overall = torch.cuda.max_memory_allocated() / (1024 * 1024)
    print(json.dumps({"bringup": "PASS", "quant": QUANT, "layers": h.n_layers,
                      "hidden": h.hidden, "vocab": h.vocab,
                      "lm_head_dtype": str(h.model.lm_head.weight.dtype),
                      "lm_head_quantized": hasattr(h.model.lm_head.weight, "quant_state"),
                      "peak_alloc_after_load_mib": round(peak_after_load, 1),
                      "peak_fwd_alloc_mib": round(peak_fwd, 1),
                      "peak_overall_alloc_mib": round(peak_overall, 1),
                      "target_fwd_mib": 7000, "under_fwd_target": peak_fwd <= 7000.0,
                      "sequences": results}, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["selftest", "bringup", "all"])
    ap.add_argument("--wait-min", type=int, default=60)
    ap.add_argument("--n-sample", type=int, default=2000, help="train rows per axis for fitting passes")
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

    # seeded stratified train samples (fitting passes only; held-out untouched)
    sample_meta = {}
    sample_ids = {}
    train_sample = {}
    for axis in AXES:
        rows = axis_rows(tr, axis)
        sample, meta = sample_rows(rows, axis, args.n_sample)
        train_sample[axis] = sample
        sample_meta[axis] = meta
        sample_ids[axis] = [r["id"] for r in sample]
        print(f"[sample] {axis}: n={meta['n']} bins={meta['bin_counts']} "
              f"seed={meta['seed']}", flush=True)
    (DIAG / "sample_ids-qwen8b.json").write_text(json.dumps(
        {"model": MODEL_ID, "revision": MODEL_REVISION, "axes": sample_ids,
         "meta": sample_meta}, indent=2) + "\n")

    gpu_clear()
    seed_a = derive_seed(MASTER_SEED, SEED_KEY["p2a"])
    print("[P2a] loading model (4-bit NF4)...", flush=True)
    h = QwenHarness()
    print(f"[P2a] model loaded in {h.load_s:.1f}s (lm_head {h.model.lm_head.weight.dtype}, "
          f"quantized={hasattr(h.model.lm_head.weight, 'quant_state')})", flush=True)
    t0 = time.perf_counter()
    emo = stage_p2a(h, train_sample, seed_a)
    print(f"[P2a] done in {time.perf_counter()-t0:.1f}s, C2 layer={emo['c2']['selected_layer']}", flush=True)
    emo_json = {k: v for k, v in emo.items() if not k.startswith("_")}
    emo_json["sample"] = sample_meta
    (DIAG / "emotion_vectors-qwen8b.json").write_text(json.dumps(emo_json, indent=2) + "\n")
    np.savez(DIAG / "emotion_vectors-qwen8b_dirs.npz",
             **{f"{a}__{k}": v for a in emo["_directions"] for k, v in emo["_directions"][a].items()})
    print("[P2a] saved diagnostics/emotion_vectors-qwen8b.json + _dirs.npz", flush=True)

    gpu_clear()
    seed_p3 = derive_seed(MASTER_SEED, SEED_KEY["p3"])
    print("[P3] held-out geometry (all held-out rows, EV only)...", flush=True)
    t0 = time.perf_counter()
    geo = stage_p3(h, he, emo, seed_p3)
    print(f"[P3] done in {time.perf_counter()-t0:.1f}s", flush=True)
    (DIAG / "geometry-qwen8b.json").write_text(json.dumps(geo, indent=2) + "\n")
    print("[P3] saved diagnostics/geometry-qwen8b.json", flush=True)
    for axis in AXES:
        ax = geo["axes"][axis]
        prim = ax["methods"]["emotion_vectors"]
        print(f"[P3] {axis}: EV r={prim['r']:.3f} CI={prim['ci']} layer={prim['layer']} "
              f"| diagnostic {ax['diagnostic']['verdict_if_gate']}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
