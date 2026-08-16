"""Determinism + provenance utilities for the emotion-codebook spike (P0).

Pre-registered contract (docs/exp-affect-codebook-pipeline-2026-08-15.md):
- Master seed 20260815; per-run seeds derived deterministically from it.
- Fixed-seed + fixed-temperature decoding everywhere.
- Provenance recorder writes repro_bundle.json at the spike root.

RNG conventions mirror the frozen engine/rng.py (hierarchical SeedSequence,
never bare unseeded default_rng in production paths). This module is the ONLY
place allowed to touch global RNG state (it is the seeding utility).
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np

#: Master seed, pre-registered in the brief (P0).
MASTER_SEED: int = 20260815

#: GPU memory budget (MiB) — hard ceiling for the 8 GB barrier (nvidia-smi).
GPU_BUDGET_MIB: int = 8151


# ---------------------------------------------------------------------------
# Seeded RNG utilities
# ---------------------------------------------------------------------------

def derive_seed(master: int = MASTER_SEED, *key: int) -> int:
    """Deterministic per-run seed from (master, *key).

    Mirrors engine/rng.py: SeedSequence(master, spawn_key=key). Returns a
    stable 32-bit int usable with torch.manual_seed / random.seed.
    """
    ss = np.random.SeedSequence(master, spawn_key=key)
    return int(ss.generate_state(1)[0])


def rng_for(master: int = MASTER_SEED, *key: int) -> np.random.Generator:
    """Hierarchical Generator for (master, *key) — same style as engine/rng.py."""
    return np.random.default_rng(np.random.SeedSequence(master, spawn_key=key))


def seed_everything(seed: int) -> None:
    """Seed all global RNG state (torch, numpy, python) from one seed."""
    import torch

    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)


# ---------------------------------------------------------------------------
# Fixed-temperature decoding config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DecodingConfig:
    """Deterministic decoding defaults (pre-registered).

    Caller MUST call seed_everything(seed) immediately before generate();
    `seed` is recorded for provenance only.
    """

    temperature: float = 0.8
    top_p: float = 0.9
    top_k: int = 40
    do_sample: bool = True
    repetition_penalty: float = 1.0
    max_new_tokens: int = 64
    seed: int | None = None

    def as_generation_kwargs(self) -> dict[str, Any]:
        kw = asdict(self)
        kw.pop("seed", None)
        return kw


# ---------------------------------------------------------------------------
# Provenance recorder
# ---------------------------------------------------------------------------

def sha256_file(path: Path | str) -> str:
    """SHA-256 of a file, streamed (works for multi-GB model files)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ProvenanceRecorder:
    """Collects revisions/seeds/configs and writes repro_bundle.json."""

    def __init__(self, root: Path | str, master_seed: int = MASTER_SEED):
        self.root = Path(root)
        self.master_seed = master_seed
        self.data: dict[str, Any] = {
            "spike": "emotion-codebook-spike",
            "phase": "P0",
            "master_seed": master_seed,
            "sections": {},
        }

    def add(self, section: str, **entries: Any) -> None:
        self.data["sections"].setdefault(section, {}).update(entries)

    def add_file(self, section: str, path: Path | str, **extra: Any) -> None:
        p = Path(path)
        entry = {"path": str(p), "bytes": p.stat().st_size, "sha256": sha256_file(p)}
        entry.update(extra)
        self.data["sections"].setdefault(section, {}).update({p.name: entry})

    def write(self, filename: str = "repro_bundle.json") -> Path:
        out = self.root / filename
        out.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        return out
