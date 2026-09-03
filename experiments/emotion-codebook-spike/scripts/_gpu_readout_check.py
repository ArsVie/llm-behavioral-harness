"""Scratch GPU check of the stage_p2b lm_head readout line (the Qwen crash).

Loads the real model once, runs the exact readout statement from
p2_gemma_extract.stage_p2b (with the .cpu() fix) on a synthetic direction
vector, and asserts the result is a valid CPU numpy score vector.
"""
import sys
import time
from pathlib import Path

import numpy as np
import torch

SPIKE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SPIKE_ROOT))

from scripts.p2_gemma_extract import GemmaHarness  # noqa: E402

h = GemmaHarness()
rng = np.random.default_rng(0)
d = rng.normal(size=h.hidden).astype(np.float32)
d /= np.linalg.norm(d)

wu = h.model.lm_head.weight.detach().float()
t0 = time.perf_counter()
scores = torch.softmax(wu @ torch.from_numpy(d).to(wu.device), dim=-1).cpu().numpy()
dt = time.perf_counter() - t0
print(f"readout OK: scores shape={scores.shape} sum={scores.sum():.6f} "
      f"top={scores.max():.6f} time={dt:.2f}s")
assert scores.shape == (h.vocab,)
assert abs(scores.sum() - 1.0) < 1e-3
top = np.argsort(scores)[::-1]
print("top tokens:", [h.tok.decode([int(t)], skip_special_tokens=True) for t in top[:8]])
print("READOUT_CHECK PASS")
