"""P2-Gemma: emotion vectors (P2a), J-lens layer-sharded fit (P2b), H1 geometry (P3).

Adaptation of scripts/p2_qwen_extract.py to google/gemma-3-1b-pt @ pinned
revision fcf18a2a879aab110ca39f8bffbccd5d49d8eb29 (repro_bundle.json).
Adaptation notes: diagnostics/jlens-shim-gemma.md.

Pre-registered contract: docs/exp-affect-codebook-spike2-2026-08-16.md
(spike 2, behavioral re-gate; spike-1 contract:
docs/exp-affect-codebook-pipeline-2026-08-15.md).
Spike-2 machinery fixes (P2-machinery, 2026-08-16; see
diagnostics/spike2-machinery-notes.md):
- C1: J-lens polarity corrected — w = ybar - y (equivalently negate d).
- C2: single pre-registered layer rule — middle third of depth
  [floor(N/3), ceil(2N/3)) = [9,19) at 28 layers; layer selected on TRAIN by
  SIGNED train r within the band (not |r| argmax); identical in P2a/P2b/P3.
- C3: arousal direction per layer projected onto the valence-orthogonal
  complement (Gram-Schmidt, unit-normalized), EV and JL both.
- P2a: contrastive activation differences per layer per axis (forward hooks).
- P2b: J-lens directions via backward (gradient checkpointing, no parameter
  grads — graph leaf = embedding output), layer-sharded accumulation, binned
  vocabulary readout over [0,1] with bin width 0.10.
  Fallback (pre-registered): emotion-vectors + nearest-VAD-label lexical
  readout if J-lens cannot fit; recorded in jlens-shim-gemma.md.
- P3: H1 gate for the Gemma family ONLY — Pearson r on HELD-OUT stimuli,
  bootstrap 95% CI (>= 1000 resamples, seeded).

Stimulus protocol (P1, data/stimuli/README.md): rows carry axis, intensity
(= axis coordinate), v/a/d, contrast_group "<axis>:<split>:gNNNN:<side>".
Fitting passes use a SEEDED STRATIFIED SAMPLE of train (documented; the full
87k-row corpus is beyond the 8 GB GPU budget for backward passes); held-out
evaluation uses ALL held-out rows. Sample ids are recorded.

Sequence scheme (same as Qwen, commit f2d78b5): BOS + stimulus tokens,
variable length (no padding), batch 1, L <= 128. Labels = stimulus tokens
(next-token prediction from BOS context) so single-word NRC-VAD stimuli are
usable. Labeled positions 0..L-2; forward activation means exclude the BOS
position; vocabulary readout = logits at the last real position. Gemma's BOS
semantics: tokenizer does NOT add BOS with add_special_tokens=False, so BOS is
prepended explicitly (bos_token_id=2 '<bos>') — identical to the Qwen scheme;
verified by the bring-up analytic check (see jlens-shim-gemma.md).

All randomness via harness/determinism.py. Seed keys (int) are IDENTICAL to
the Qwen run so runs are comparable: 1 bringup, 2 p2a, 3 p2b, 4 p3, 5 bootstrap
(axis, method, layer), 6 sampling (axis), 99 selftest. 0 reserved (P0 smoke).

Usage:
  python scripts/p2_gemma_extract.py selftest
  python scripts/p2_gemma_extract.py bringup
  python scripts/p2_gemma_extract.py c1verify   (mini C1 sign gate; needs stimuli)
  python scripts/p2_gemma_extract.py all [--wait-min 60] [--n-sample 2000]
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

MODEL_ID = "google/gemma-3-1b-pt"
MODEL_REVISION = "fcf18a2a879aab110ca39f8bffbccd5d49d8eb29"
SEQ_LEN = 128  # max total length INCLUDING the BOS token
BIN_WIDTH = 0.10
N_BINS = 10
BOOT_N = 1000
GATES = {"valence": 0.60, "arousal": 0.40}
SEED_KEY = {"bringup": 1, "p2a": 2, "p2b": 3, "p3": 4, "boot": 5, "sample": 6, "c1": 7, "selftest": 99}
AXIS_IDX = {"valence": 0, "arousal": 1}
METHOD_IDX = {"emotion_vectors": 0, "jlens": 1}
SELFTEST_HIDDEN = 1152  # gemma-3-1b-pt hidden_size (config.json)
DIAG = SPIKE_ROOT / "diagnostics"
EXTRACT = SPIKE_ROOT / "data" / "extractions" / "gemma"
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
    """Seeded stratified sample over P1's intensity bins (width 0.1).

    Pre-registered efficiency decision (documented in jlens-shim-qwen.md): the
    full train corpus (87,278 rows) exceeds the 8 GB GPU budget for backward
    passes; directions/readouts are fitted on a stratified sample that keeps
    coverage across the [0,1] value range. Held-out evaluation uses ALL rows.
    """
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
# Model harness: forward activation capture + backward direction capture
# ---------------------------------------------------------------------------
class GemmaHarness:
    """Single model instance; hooks capture per-layer residual-stream states.

    Conventions (documented in diagnostics/jlens-shim-gemma.md):
    - layer i output = residual stream after layer i (input to layer i+1);
      the final RMSNorm output is the head input h_L.
    - Sequence = BOS + stimulus tokens, variable length L (no padding).
    - forward hooks: position-mean over CONTENT positions 1..L-1.
    - backward hooks: position-mean over LABELED positions 0..L-2, so the
      final-layer analytic check is exact.

    Gemma3 architecture notes (transformers 5.15.0):
    - Gemma3ForCausalLM.model IS the text backbone (Gemma3TextModel) with
      .layers / .embed_tokens / .norm directly (older transformers nested it
      under model.text_model — _resolve_backbone handles both).
    - embed_tokens is Gemma3TextScaledWordEmbedding (output scaled by
      sqrt(hidden_size)); irrelevant to the analytic check (graph leaf).
    - lm_head may be tied to embed_tokens (config.tie_word_embeddings absent
      in gemma-3-1b-pt; tied status is detected at load and recorded).
    - Tokenizer: gemma3_text maps to GemmaTokenizer (SentencePiece; the class
      historically named Gemma3Tokenizer) — BOS=2 '<bos>', EOS=1 '<eos>',
      PAD=0 '<pad>'; add_special_tokens=False emits content tokens only, so
      the Qwen BOS-prepend scheme applies unchanged.
    """

    def __init__(self) -> None:
        from transformers import AutoTokenizer, Gemma3ForCausalLM

        self.tok = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
        if not type(self.tok).__name__.startswith("Gemma"):
            raise RuntimeError(f"unexpected tokenizer class: {type(self.tok).__name__}")
        if self.tok.pad_token_id is None:
            self.tok.pad_token = self.tok.eos_token
        t0 = time.perf_counter()
        self.model = Gemma3ForCausalLM.from_pretrained(
            MODEL_ID, revision=MODEL_REVISION, dtype=torch.bfloat16, low_cpu_mem_usage=True
        )
        self.model.to("cuda")
        self.model.requires_grad_(False)  # no parameter grads anywhere (memory)
        self.model.eval()
        self.load_s = time.perf_counter() - t0
        self._backbone = self._resolve_backbone()
        self.n_layers = len(self._backbone.layers)
        cfg = self.model.config
        self.hidden = int(getattr(cfg, "hidden_size", None) or cfg.text_config.hidden_size)
        self.vocab = int(getattr(cfg, "vocab_size", None) or cfg.text_config.vocab_size)
        self.lm_tied = self.model.lm_head.weight.data_ptr() == self._backbone.embed_tokens.weight.data_ptr()

        self._x0: torch.Tensor | None = None
        self._collect_fwd = False
        self._collect_bwd = False
        self._pos_mask: torch.Tensor | None = None
        self.fwd_acts: dict = {}
        self.bwd_grads: dict = {}

        self._backbone.embed_tokens.register_forward_hook(lambda m, i, o: self._x0)
        for i, layer in enumerate(self._backbone.layers):
            layer.register_forward_hook(self._make_fwd_hook(str(i)))
            layer.register_full_backward_hook(self._make_bwd_hook(str(i)))
        self._backbone.norm.register_forward_hook(self._make_fwd_hook("norm"))
        self._backbone.norm.register_full_backward_hook(self._make_bwd_hook("norm"))

    def _resolve_backbone(self):
        """Gemma3ForCausalLM.model IS the text backbone (Gemma3TextModel) in
        transformers 5.15.0; older versions nested it one level deeper
        (model.model.text_model). Fail loudly if neither layout matches."""
        for cand in (self.model.model, getattr(self.model.model, "text_model", None)):
            if cand is not None and hasattr(cand, "layers") and hasattr(cand, "embed_tokens") and hasattr(cand, "norm"):
                return cand
        raise RuntimeError("cannot locate Gemma3 text backbone (unexpected model layout)")

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
        the last real position (bf16 CPU)."""
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

    def backward_directions(self, input_ids: torch.Tensor, attn: torch.Tensor, L: int):
        """One forward+backward of mean next-token CE over labeled positions
        0..L-2; per-layer position-mean grads (fp32 CPU) + analytic final-layer
        check. Graph leaf = embedding output (no parameter grads)."""
        self._pos_mask = torch.arange(0, L - 1, device=input_ids.device)  # labeled
        self._x0 = self._backbone.embed_tokens(input_ids).detach().requires_grad_(True)
        self._collect_fwd = False
        self._collect_bwd = True
        self.bwd_grads = {}
        stats = {}
        try:
            out = self.model(input_ids=input_ids, attention_mask=attn, use_cache=False)
            logits = out.logits
            labels = input_ids[:, 1:]  # stimulus tokens (next-token prediction)
            loss = torch.nn.functional.cross_entropy(
                logits[:, :-1].reshape(-1, self.vocab), labels.reshape(-1)
            )
            loss.backward()
            if "norm" in self.bwd_grads:
                z = logits[0, :-1].float()
                lab = labels[0]
                sm = torch.softmax(z, dim=-1)
                onehot = torch.zeros_like(sm)
                onehot.scatter_(1, lab.unsqueeze(1), 1.0)
                n_lab = lab.numel()
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
# Direction math (documented in jlens-shim-gemma.md; identical to Qwen)
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
    """P2b (C1-corrected): sum_i (mean(y) - y_i) * grad_i — value-centered
    J-lens direction with the pre-registered polarity fix (w = ybar - y;
    contract C1 — equivalently negate the spike-1 direction)."""
    ybar = float(ys.mean())
    return sum((ybar - y) * g for y, g in zip(ys, grads))


def c2_band(n_layers: int) -> tuple[int, int]:
    """Pre-registered C2 band: middle third of depth [floor(N/3), ceil(2N/3)).
    Qwen3-1.7B (28) -> [9,19); Gemma-3-1B (26) -> [8,18) (orchestrator dec. 4)."""
    return (n_layers // 3, (2 * n_layers + 2) // 3)


def c2_select(layer_out: dict, n_layers: int, axis: str) -> str:
    """C2 rule: within the middle-third band, select the layer with the
    highest SIGNED train r for this axis (contract: valence-r, NOT |r|; same
    rule per axis; applied identically in P2a/P2b/P3)."""
    lo, hi = c2_band(n_layers)
    cands = [k for k in layer_out
             if k != "norm" and lo <= int(k) < hi
             and not np.isnan(layer_out[k]["train_r"])]
    if not cands:
        cands = [k for k in layer_out if k != "norm" and lo <= int(k) < hi]
    if not cands:
        raise RuntimeError(f"axis {axis}: no layers in C2 band [{lo},{hi})")
    return max(cands, key=lambda k: layer_out[k]["train_r"])


def orthogonalize(d_aro: np.ndarray, d_val: np.ndarray) -> np.ndarray:
    """C3: project arousal direction onto the valence-orthogonal complement,
    then unit-normalize (Gram-Schmidt; orchestrator decision 5).
    d_val must be a unit vector."""
    d_aro = d_aro - (d_aro @ d_val) * d_val
    return unit(d_aro)


def minmax_map(p: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip((p - lo) / (hi - lo + 1e-12), 0.0, 1.0)


def bin_index(p_tilde: np.ndarray) -> np.ndarray:
    return np.minimum(np.floor(p_tilde / BIN_WIDTH).astype(int), N_BINS - 1)


# ---------------------------------------------------------------------------
# Stage P2a — emotion vectors (forward only)
# ---------------------------------------------------------------------------
def stage_p2a(h: GemmaHarness, train_sample: dict[str, list[dict]], seed: int) -> dict:
    seed_everything(seed)
    out = {"model": MODEL_ID, "revision": MODEL_REVISION, "seed": seed,
           "stage": "P2a emotion vectors (forward hooks)", "axes": {}}
    directions: dict[str, dict[str, np.ndarray]] = {}
    for axis in AXES:
        rows = train_sample[axis]
        high, low, split_desc, split_note = high_low_split(rows, axis)
        hi_ids = {r["id"] for r in high}
        per_stim: list[dict] = []
        for r in rows:
            input_ids, attn, L, _ = h.tokenize(r["text"])
            if input_ids is None:
                continue
            acts, _ = h.forward_states(input_ids, attn, L)
            per_stim.append({"y": axis_value(r, axis), "acts": acts, "high": r["id"] in hi_ids})
        n_used = len(per_stim)
        if n_used == 0:
            raise RuntimeError(f"axis {axis}: no usable train stimuli (all empty texts?)")
        layers = sorted(per_stim[0]["acts"].keys())
        y_arr = np.array([s["y"] for s in per_stim])
        axes_out: dict = {"n": n_used,
                          "n_high": sum(s["high"] for s in per_stim),
                          "n_low": n_used - sum(s["high"] for s in per_stim),
                          "split": split_desc, "split_note": split_note, "layers": {}}
        directions[axis] = {}
        for Lk in layers:
            vecs = [s["acts"][Lk].numpy() for s in per_stim]
            d = contrastive_direction(
                [v for s, v in zip(per_stim, vecs) if s["high"]],
                [v for s, v in zip(per_stim, vecs) if not s["high"]],
            )
            c3_angle = None
            if axis == "arousal" and Lk in directions["valence"]:
                # C3: arousal in the valence-orthogonal complement, per layer.
                d_val_hat = directions["valence"][Lk]
                c3_angle = float(np.degrees(np.arccos(
                    np.clip((d @ d_val_hat) / (np.linalg.norm(d) + 1e-12), -1.0, 1.0))))
                d = orthogonalize(d, d_val_hat)
            dhat = unit(d)
            directions[axis][Lk] = dhat
            proj = np.array([float(v @ dhat) for v in vecs])
            tr = pearson(proj, y_arr)
            hi_v = [v for s, v in zip(per_stim, vecs) if s["high"]]
            lo_v = [v for s, v in zip(per_stim, vecs) if not s["high"]]
            sd_pooled = np.sqrt((np.var(np.stack(hi_v), axis=0).mean()
                                 + np.var(np.stack(lo_v), axis=0).mean()) / 2 + 1e-12)
            axes_out["layers"][Lk] = {
                "norm": float(np.linalg.norm(d)), "cohen_d": float(np.linalg.norm(d) / sd_pooled),
                "train_r": tr, "c3_angle_deg": c3_angle,
                "proj_high_mean": float(np.mean([v @ dhat for v in hi_v])),
                "proj_low_mean": float(np.mean([v @ dhat for v in lo_v])),
            }
        c2_lo, c2_hi = c2_band(h.n_layers)
        sel_c2 = c2_select(axes_out["layers"], h.n_layers, axis)
        axes_out["c2"] = {"rule": "middle third of depth; layer selected on TRAIN "
                                  "by SIGNED train r within band (contract C2, dec. 4)",
                          "band": [c2_lo, c2_hi], "n_layers": h.n_layers,
                          "selected_layer": sel_c2}
        axes_out["selected_layer_c2"] = sel_c2
        # legacy spike-1 diagnostic — reported, never used for selection
        axes_out["best_layer_by_abs_train_r"] = max(
            axes_out["layers"], key=lambda k: abs(axes_out["layers"][k]["train_r"]))
        axes_out["c3"] = {"applied": axis == "arousal",
                          "note": ("arousal = unit(proj(raw arousal, valence-orthogonal "
                                   "complement)), per layer (Gram-Schmidt, dec. 5)"
                                   if axis == "arousal"
                                   else "valence is the C3 reference axis (no orthogonalization)")}
        out["axes"][axis] = axes_out
    out["_directions"] = directions
    return out


# ---------------------------------------------------------------------------
# Stage P2b — J-lens (backward, layer-sharded accumulation + vocabulary readout)
# ---------------------------------------------------------------------------
def stage_p2b(h: GemmaHarness, train_sample: dict[str, list[dict]], seed: int) -> dict:
    seed_everything(seed)
    h.model.train()
    h.model.gradient_checkpointing_enable()
    torch.cuda.reset_peak_memory_stats()
    torch.cuda.empty_cache()

    keys = [str(i) for i in range(h.n_layers)] + ["norm"]
    axis_rows_map = {a: train_sample[a] for a in AXES}
    ybar = {a: float(np.mean([axis_value(r, a) for r in axis_rows_map[a]])) for a in AXES}
    seen: dict = {}
    for a in AXES:
        for r in axis_rows_map[a]:
            seen.setdefault(r["id"], r)
    membership = {a: {r["id"] for r in axis_rows_map[a]} for a in AXES}

    acc = {a: {k: np.zeros(h.hidden, dtype=np.float64) for k in keys} for a in AXES}
    analytic = None
    n_done = 0
    t0 = time.perf_counter()
    try:
        for r in seen.values():
            input_ids, attn, L, _ = h.tokenize(r["text"])
            if input_ids is None:
                continue
            grads, stats = h.backward_directions(input_ids, attn, L)
            if analytic is None and stats:
                analytic = stats
            for a in AXES:
                if r["id"] in membership[a]:
                    w = ybar[a] - axis_value(r, a)  # C1: corrected polarity (w = ybar - y)
                    for k in keys:
                        acc[a][k] += w * grads[k].numpy().astype(np.float64)
            n_done += 1
            if n_done % 25 == 0:
                torch.cuda.empty_cache()
            if n_done % 200 == 0:
                rate = n_done / (time.perf_counter() - t0)
                print(f"[P2b] {n_done} stimuli, {rate:.1f}/s, "
                      f"ETA { (len(seen) - n_done) / rate / 60:.1f} min", flush=True)
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
        if axis == "arousal":
            # C3: arousal in the valence-orthogonal complement, per layer.
            for k in keys:
                dirs[k] = orthogonalize(dirs[k], directions["valence"][k])
        directions[axis] = dirs
        y_arr = np.array([axis_value(r, axis) for r in rows])
        proj: dict[str, list] = {k: [] for k in keys}
        z_list: list = []
        for r in rows:
            input_ids, attn, L, _ = h.tokenize(r["text"])
            if input_ids is None:
                continue
            acts, z_last = h.forward_states(input_ids, attn, L)
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
        wu = h.model.lm_head.weight.detach().float()
        # NOTE: one-token divergence from p2_qwen_extract.py (documented in
        # jlens-shim-gemma.md §2.8): the Qwen line ends with `.numpy()` on the
        # CUDA product, which raises "can't convert cuda:0 device type tensor
        # to numpy" — observed live on the Qwen full run 2026-08-15 (crash at
        # the P2b valence readout, after ~3400 backward stimuli). The Gemma
        # adaptation keeps the device fix (.to(wu.device) before the matmul)
        # and adds .cpu() before .numpy() so the readout terminates.
        scores = torch.softmax(wu @ torch.from_numpy(dirs["norm"].astype(np.float32)).to(wu.device), dim=-1).cpu().numpy()
        dir_tokens = []
        for t in np.argsort(scores)[::-1]:
            s = toks.decode([int(t)], skip_special_tokens=True)
            if s.strip():
                dir_tokens.append([s, float(scores[t])])
            if len(dir_tokens) == 20:
                break
        c2_lo, c2_hi = c2_band(h.n_layers)
        sel_c2 = c2_select(layer_out, h.n_layers, axis)
        out["axes"][axis] = {
            "n": len(rows), "y_mean": ybar[axis],
            "direction_layers": layer_out,
            "c2": {"rule": "middle third of depth; layer selected on TRAIN by "
                           "SIGNED train r within band (contract C2, dec. 4)",
                   "band": [c2_lo, c2_hi], "n_layers": h.n_layers,
                   "selected_layer": sel_c2},
            "selected_layer_c2": sel_c2,
            "best_layer_by_abs_train_r": max(  # legacy spike-1 diagnostic, never used
                layer_out, key=lambda k: abs(layer_out[k]["train_r"])),
            "c3": {"applied": axis == "arousal",
                   "note": ("arousal = unit(proj(raw arousal, valence-orthogonal "
                            "complement)), per layer (Gram-Schmidt, dec. 5)"
                            if axis == "arousal"
                            else "valence is the C3 reference axis (no orthogonalization)")},
            "binning": {"bin_width": BIN_WIDTH, "n_bins": N_BINS, "p_min": pmin, "p_max": pmax,
                        "map": "minmax(train projection on d_norm) -> [0,1]",
                        "direction_layer": final_key,
                        "direction_train_r": layer_out[final_key]["train_r"]},
            "empty_bins": empty, "bins": bins_out,
            "readout": {"method": "softmax(mean last-pos logits, T=1.0)", "top_tokens_per_bin": 30},
            "jlens_direction_tokens": dir_tokens,
        }
        (EXTRACT / f"jlens_{axis}.json").write_text(json.dumps(
            {"model": MODEL_ID, "revision": MODEL_REVISION, "seed": seed, "axis": axis,
             "fallback": out.get("fallback"), "n_train": out["axes"][axis]["n"],
             "binning": out["axes"][axis]["binning"], "empty_bins": out["axes"][axis]["empty_bins"],
             "jlens_direction_tokens": dir_tokens, "bins": bins_out}, indent=2) + "\n")
        print(f"[P2b] checkpoint jlens_{axis}.json written", flush=True)
    out["peak_alloc_mib"] = round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1)
    out["_directions"] = directions
    return out


# ---------------------------------------------------------------------------
# Stage P3 — H1 geometry on held-out (Gemma-family gate; ALL held-out rows)
# ---------------------------------------------------------------------------
def stage_p3(h: GemmaHarness, held: list[dict], emo_vec: dict, jlens: dict, seed: int) -> dict:
    seed_everything(seed)
    h.model.eval()
    out: dict = {"model": MODEL_ID, "revision": MODEL_REVISION, "seed": seed,
                 "stage": "P3 H1 geometry (held-out)", "axes": {}, "n_heldout": len(held)}
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
        dirs_ev = emo_vec["_directions"][axis]
        dirs_jl = jlens["_directions"][axis]
        prof: dict = {}
        for method, dirs in (("emotion_vectors", dirs_ev), ("jlens", dirs_jl)):
            for Lk in layers:
                if Lk not in dirs:
                    continue
                dhat = dirs[Lk]
                proj = np.array([float(s["acts"][Lk].numpy() @ dhat) for s in per_stim])
                r = pearson(proj, y_arr)
                # NOTE: norm-layer spawn key must be NON-NEGATIVE — SeedSequence
                # rejects negative keys (ValueError: expected non-negative integer).
                # 10000 cannot collide with real layer indices (<= ~64).
                rng = rng_for(MASTER_SEED, SEED_KEY["boot"], AXIS_IDX[axis], METHOD_IDX[method],
                              int(Lk) if Lk != "norm" else 10000)
                lo, hi = bootstrap_ci(proj, y_arr, rng)
                prof.setdefault(Lk, {})[method] = {"r": r, "ci": [lo, hi], "n": n}
        axes_out: dict = {"n": n, "methods": {}, "layer_profile": prof}
        for method, dirs, chosen in (
            ("emotion_vectors", dirs_ev, emo_vec["axes"][axis]["selected_layer_c2"]),
            ("jlens", dirs_jl, jlens["axes"][axis]["selected_layer_c2"]),
        ):
            Lk = str(chosen) if str(chosen) in prof and method in prof[str(chosen)] else next(iter(prof))
            blk = dict(prof[Lk][method])
            blk["layer"] = Lk
            blk["train_selected_layer"] = str(chosen)
            axes_out["methods"][method] = blk
        prim = axes_out["methods"]["emotion_vectors"]
        ci_excl0 = prim["ci"][0] > 0 or prim["ci"][1] < 0
        axes_out["gate"] = {"r_threshold": GATES[axis], "ci_excludes_0": ci_excl0}
        axes_out["verdict"] = "PASS" if (prim["r"] >= GATES[axis] and ci_excl0) else "FAIL"
        out["axes"][axis] = axes_out
    return out


# ---------------------------------------------------------------------------
# C1 verify-gate (mini, pre-registered): corrected JL valence sign must match
# EV valence sign
# ---------------------------------------------------------------------------
def c1verify() -> None:
    """C1 verify-gate (mini run): ~60 train + ~60 held-out rows per axis,
    deterministic sampling via derive_seed(MASTER_SEED, SEED_KEY['c1'], axis).

    Fits EV directions (P2a) and JL directions (P2b, C1-corrected polarity)
    on the train sample, then correlates held-out projections vs held-out y
    per axis. Gate: corrected JL valence sign must match EV valence sign (C1).
    Arousal reported for completeness (not gating). Needs the spike2 stimulus
    corpus; fails cleanly if absent.
    """
    if not (STIM / "train.jsonl").exists() or not (STIM / "heldout.jsonl").exists():
        raise SystemExit(
            "c1verify needs spike2/data/stimuli/{train,heldout}.jsonl — not present; "
            "run P1 (scripts/build_stimuli.py) first.")
    DIAG.mkdir(parents=True, exist_ok=True)
    EXTRACT.mkdir(parents=True, exist_ok=True)
    tr, he = load_stimuli()
    n_train = n_held = 60
    train_sample: dict[str, list[dict]] = {}
    held_sample: dict[str, list[dict]] = {}
    meta: dict[str, dict] = {}
    for axis in AXES:
        rng = rng_for(MASTER_SEED, SEED_KEY["c1"], AXIS_IDX[axis])
        rows = axis_rows(tr, axis)
        idx = rng.permutation(len(rows))[:n_train]
        train_sample[axis] = [rows[i] for i in idx]
        hrows = axis_rows(he, axis)
        hidx = rng.permutation(len(hrows))[:n_held]
        held_sample[axis] = [hrows[i] for i in hidx]
        meta[axis] = {
            "n_train": len(train_sample[axis]), "n_heldout": len(held_sample[axis]),
            "seed": derive_seed(MASTER_SEED, SEED_KEY["c1"], AXIS_IDX[axis]),
            "ids_train": [r["id"] for r in train_sample[axis]],
            "ids_heldout": [r["id"] for r in held_sample[axis]],
        }
        print(f"[c1verify] {axis}: train n={meta[axis]['n_train']} "
              f"heldout n={meta[axis]['n_heldout']} seed={meta[axis]['seed']}", flush=True)

    gpu_clear()
    seed_a = derive_seed(MASTER_SEED, SEED_KEY["p2a"])
    seed_b = derive_seed(MASTER_SEED, SEED_KEY["p2b"])
    h = GemmaHarness()
    print(f"[c1verify] model loaded in {h.load_s:.1f}s", flush=True)
    emo = stage_p2a(h, train_sample, seed_a)   # EV directions (C2 layer rule)
    print("[c1verify] P2a (EV) done", flush=True)
    jl = stage_p2b(h, train_sample, seed_b)    # JL directions (C1 polarity + C2)
    print(f"[c1verify] P2b (JL) done, peak {jl.get('peak_alloc_mib')} MiB", flush=True)

    res: dict = {"model": MODEL_ID, "revision": MODEL_REVISION, "stage": "C1 verify (mini)",
                 "n_train": n_train, "n_heldout": n_held, "meta": meta, "axes": {}}
    for axis in AXES:
        per_stim = []
        for r in held_sample[axis]:
            input_ids, attn, L, _ = h.tokenize(r["text"])
            if input_ids is None:
                continue
            acts, _ = h.forward_states(input_ids, attn, L)
            per_stim.append({"y": axis_value(r, axis), "acts": acts})
        y_arr = np.array([s["y"] for s in per_stim])
        n = len(per_stim)

        def held_r(dirs: dict, Lk: str) -> float:
            dhat = dirs[Lk]
            proj = np.array([float(s["acts"][Lk].numpy() @ dhat) for s in per_stim])
            return pearson(proj, y_arr)

        ev_layer = emo["axes"][axis]["selected_layer_c2"]
        jl_layer = jl["axes"][axis]["selected_layer_c2"]
        ev_r = held_r(emo["_directions"][axis], ev_layer)
        jl_r = held_r(jl["_directions"][axis], jl_layer)
        jl_norm_r = held_r(jl["_directions"][axis], "norm")
        sgn = lambda r: "pos" if r >= 0 else "neg"  # noqa: E731
        res["axes"][axis] = {
            "n": n,
            "ev": {"layer": ev_layer, "r": ev_r, "sign": sgn(ev_r)},
            "jl": {"layer": jl_layer, "r": jl_r, "sign": sgn(jl_r)},
            "jl_norm": {"layer": "norm", "r": jl_norm_r, "sign": sgn(jl_norm_r)},
            "signs_match": (sgn(ev_r) == sgn(jl_r) == sgn(jl_norm_r)),
        }
        print(f"[c1verify] {axis}: n={n} EV r={ev_r:.3f} ({sgn(ev_r)}) "
              f"| JL r={jl_r:.3f} ({sgn(jl_r)}) "
              f"| JL(norm) r={jl_norm_r:.3f} ({sgn(jl_norm_r)}) "
              f"| match={res['axes'][axis]['signs_match']}", flush=True)

    # gate: valence (C1 pre-registered); arousal informational only
    val_ok = res["axes"]["valence"]["signs_match"]
    res["verdict"] = "PASS" if val_ok else "FAIL"
    res["note"] = ("C1 verify-gate: corrected JL valence sign must match EV valence sign "
                   "(pre-registered). Arousal reported for completeness, not gating.")
    (DIAG / "c1-verify-gemma.json").write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps({"c1verify": res["verdict"], "valence_signs_match": val_ok,
                      "axes": {a: res["axes"][a]["signs_match"] for a in AXES}}, indent=2))
    if not val_ok:
        raise SystemExit("C1 verify FAILED: corrected JL valence sign disagrees with EV sign")


# ---------------------------------------------------------------------------
# GPU etiquette / stimulus waiting
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
# Synthetic self-test (no GPU, no model)
# ---------------------------------------------------------------------------
def selftest() -> None:
    seed = derive_seed(MASTER_SEED, SEED_KEY["selftest"])
    rng = np.random.default_rng(seed)
    n = 120
    y = rng.uniform(0, 1, n)
    D = SELFTEST_HIDDEN  # gemma-3-1b-pt hidden_size
    d_val = rng.normal(size=D)
    d_val /= np.linalg.norm(d_val)
    raw = rng.normal(size=D)
    d_aro = raw - (raw @ d_val) * d_val          # true arousal: orthogonal to valence
    d_aro /= np.linalg.norm(d_aro)
    d_obs = d_aro + 0.5 * d_val                  # entangled observation direction (C3 target)
    d_obs /= np.linalg.norm(d_obs)
    acts_val = np.stack([0.5 * np.ones(D) + yi * 4.0 * d_val + rng.normal(0, 0.05, D) for yi in y])
    acts_aro = np.stack([0.5 * np.ones(D) + yi * 4.0 * d_obs + rng.normal(0, 0.05, D) for yi in y])
    # gradients ANTI-correlate with the feature direction (real-model
    # relationship: dL/dh opposes the feature; this is why C1's w = ybar - y
    # is the correct polarity — the spike-1 sign would recover -d_val).
    grads_val = -acts_val + rng.normal(0, 0.02, acts_val.shape)
    grads_aro = -acts_aro + rng.normal(0, 0.02, acts_aro.shape)
    hi = y >= np.median(y)

    d_ev_val = unit(contrastive_direction(list(acts_val[hi]), list(acts_val[~hi])))
    cos_ev_val = float(d_ev_val @ d_val)
    assert cos_ev_val > 0.9, f"P2a valence recovery failed: cos={cos_ev_val}"
    d_ev_aro_raw = contrastive_direction(list(acts_aro[hi]), list(acts_aro[~hi]))
    cos_ev_aro_raw = float(unit(d_ev_aro_raw) @ d_aro)
    d_ev_aro = orthogonalize(d_ev_aro_raw, d_ev_val)   # C3 (EV)
    cos_ev_aro = float(d_ev_aro @ d_aro)
    assert cos_ev_aro > 0.9, f"C3 EV arousal recovery failed: cos={cos_ev_aro}"

    d_jl_val = unit(jlens_direction(list(grads_val), y))  # C1-corrected polarity
    cos_jl_val = float(d_jl_val @ d_val)
    assert cos_jl_val > 0.9, f"C1/P2b valence recovery failed: cos={cos_jl_val}"
    d_old = unit(sum((yi - y.mean()) * g for yi, g in zip(y, grads_val)))  # spike-1 sign
    cos_old = float(d_old @ d_val)
    assert cos_old < -0.9, f"C1 counter-check failed: spike-1 sign cos={cos_old} (expected ~-1)"
    d_jl_aro = orthogonalize(jlens_direction(list(grads_aro), y), d_jl_val)  # C3 (JL)
    cos_jl_aro = float(d_jl_aro @ d_aro)
    assert cos_jl_aro > 0.9, f"C3 JL arousal recovery failed: cos={cos_jl_aro}"

    proj = acts_val @ d_ev_val
    r = pearson(proj, y)
    boot_rng = rng_for(MASTER_SEED, SEED_KEY["boot"], 0, 0, 0)
    lo, hi = bootstrap_ci(proj, y, boot_rng)
    assert abs(r) > 0.8, f"selftest r too low: {r}"
    assert lo <= r <= hi, "CI does not contain r"
    pt = minmax_map(proj, proj.min(), proj.max())
    b = bin_index(pt)
    assert set(np.unique(b)) <= set(range(10))
    print(json.dumps({"selftest": "PASS",
                      "cos_ev_val": round(cos_ev_val, 4),
                      "cos_ev_aro": round(cos_ev_aro, 4), "cos_ev_aro_raw": round(cos_ev_aro_raw, 4),
                      "cos_jl_val": round(cos_jl_val, 4),
                      "cos_jl_aro": round(cos_jl_aro, 4),
                      "c1_counter_spike1_sign_cos": round(cos_old, 4),
                      "r": round(r, 4), "ci": [round(lo, 4), round(hi, 4)]}, indent=2))


# ---------------------------------------------------------------------------
# Bring-up: real model, one sequence, hooks + backward + analytic check
# ---------------------------------------------------------------------------
def bringup() -> None:
    gpu_clear()
    seed = derive_seed(MASTER_SEED, SEED_KEY["bringup"])
    seed_everything(seed)
    h = GemmaHarness()
    print(f"load {h.load_s:.1f}s, layers={h.n_layers}, hidden={h.hidden}, vocab={h.vocab}, "
          f"lm_tied={h.lm_tied}, tokenizer={type(h.tok).__name__}", flush=True)
    lo, hi = c2_band(h.n_layers)
    print(f"C2 band [{lo},{hi})", flush=True)
    assert (lo, hi) == (8, 18), f"Gemma C2 band must be [8,18), got [{lo},{hi})"
    assert h.n_layers == 26, f"Gemma-3-1B must have 26 layers, got {h.n_layers}"
    # Sequence types: multi-token sentence, 2-token, 1-token (single-word NRC-VAD style).
    for text in ("The quiet warmth of a slow evening settles over the small room.",
                 "hurt joy", "hurt", "joy"):
        input_ids, attn, L, trunc = h.tokenize(text)
        print(f"'{text[:30]}' -> L={L} (content={L-1}), truncated={trunc}", flush=True)
        torch.cuda.reset_peak_memory_stats()
        acts, z_last = h.forward_states(input_ids, attn, L)
        print(f"  fwd peak {torch.cuda.max_memory_allocated()/1e6:.0f} MiB, captured {len(acts)} layers", flush=True)
        h.model.train()
        h.model.gradient_checkpointing_enable()
        torch.cuda.reset_peak_memory_stats()
        grads, stats = h.backward_directions(input_ids, attn, L)
        peak = torch.cuda.max_memory_allocated() / (1024 * 1024)
        print(f"  bwd peak {peak:.1f} MiB, captured {len(grads)} layers, "
              f"analytic cos={stats.get('cos')}, rel_diff={stats.get('rel_diff')}", flush=True)
        assert len(grads) == h.n_layers + 1, f"missing layer grads: {len(grads)}"
        assert stats.get("cos", 0) > 0.99, "analytic check failed"
        assert peak <= 7000.0, f"bwd peak {peak:.1f} MiB exceeds the 7000 MiB bringup budget (8151 total)"
        h.model.eval()
    print(json.dumps({"bringup": "PASS", "arch": {"layers": h.n_layers, "hidden": h.hidden,
                                                  "vocab": h.vocab}, "lm_tied": h.lm_tied}))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["selftest", "bringup", "c1verify", "all"])
    ap.add_argument("--wait-min", type=int, default=60)
    ap.add_argument("--n-sample", type=int, default=2000, help="train rows per axis for fitting passes")
    args = ap.parse_args()

    if args.stage == "selftest":
        selftest()
        return
    if args.stage == "bringup":
        bringup()
        return
    if args.stage == "c1verify":
        c1verify()
        return

    DIAG.mkdir(parents=True, exist_ok=True)
    EXTRACT.mkdir(parents=True, exist_ok=True)
    if not (STIM / "train.jsonl").exists() or not (STIM / "heldout.jsonl").exists():
        raise SystemExit(
            "spike2/data/stimuli/{train,heldout}.jsonl not present — run P1 "
            "(scripts/build_stimuli.py) first; selftest/bringup do not need stimuli.")
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
    (DIAG / "sample_ids-gemma.json").write_text(json.dumps(
        {"model": MODEL_ID, "revision": MODEL_REVISION, "axes": sample_ids,
         "meta": sample_meta}, indent=2) + "\n")

    gpu_clear()
    seed_a = derive_seed(MASTER_SEED, SEED_KEY["p2a"])
    print("[P2a] loading model...", flush=True)
    h = GemmaHarness()
    print(f"[P2a] model loaded in {h.load_s:.1f}s", flush=True)
    t0 = time.perf_counter()
    emo = stage_p2a(h, train_sample, seed_a)
    print(f"[P2a] done in {time.perf_counter()-t0:.1f}s", flush=True)
    emo_json = {k: v for k, v in emo.items() if not k.startswith("_")}
    emo_json["sample"] = sample_meta
    (DIAG / "emotion_vectors-gemma.json").write_text(json.dumps(emo_json, indent=2) + "\n")
    np.savez(DIAG / "emotion_vectors-gemma_dirs.npz",
             **{f"{a}__{k}": v for a in emo["_directions"] for k, v in emo["_directions"][a].items()})
    print("[P2a] saved diagnostics/emotion_vectors-gemma.json + _dirs.npz", flush=True)

    gpu_clear()
    seed_b = derive_seed(MASTER_SEED, SEED_KEY["p2b"])
    print("[P2b] backward pass (J-lens)...", flush=True)
    t0 = time.perf_counter()
    jl = stage_p2b(h, train_sample, seed_b)
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
    np.savez(DIAG / "jlens-gemma_dirs.npz",
             **{f"{a}__{k}": v for a in jl["_directions"] for k, v in jl["_directions"][a].items()})
    print("[P2b] saved data/extractions/gemma/jlens_{valence,arousal}.json + dirs npz", flush=True)

    gpu_clear()
    seed_p3 = derive_seed(MASTER_SEED, SEED_KEY["p3"])
    print("[P3] held-out geometry (all held-out rows)...", flush=True)
    t0 = time.perf_counter()
    geo = stage_p3(h, he, emo, jl, seed_p3)
    print(f"[P3] done in {time.perf_counter()-t0:.1f}s", flush=True)
    (DIAG / "geometry-gemma.json").write_text(json.dumps(geo, indent=2) + "\n")
    print("[P3] saved diagnostics/geometry-gemma.json", flush=True)
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
