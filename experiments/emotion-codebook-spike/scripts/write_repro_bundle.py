"""P0: write repro_bundle.json at the spike root (rerunnable, accumulates).

Sections: env, seeds, models, datasets, smoke. Future phases append their own
sections by calling ProvenanceRecorder.add(...) with the same file.
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path

SPIKE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SPIKE_ROOT))

from harness.determinism import (  # noqa: E402
    MASTER_SEED,
    DecodingConfig,
    ProvenanceRecorder,
    derive_seed,
)

RAW = SPIKE_ROOT / "datasets" / "raw"


def model_files(snapshot: Path) -> dict:
    files = {}
    for f in sorted(snapshot.iterdir()):
        if f.is_symlink():
            target = Path(f.resolve())
            files[f.name] = {
                "bytes": target.stat().st_size,
                "sha256": target.name if len(target.name) == 64 else None,
            }
    return files


def main() -> None:
    rec = ProvenanceRecorder(SPIKE_ROOT, master_seed=MASTER_SEED)

    import numpy as np
    import scipy
    import torch
    import transformers
    import huggingface_hub
    import datasets

    nvsmi = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    rec.add(
        "env",
        host=platform.node(),
        python=sys.version.split()[0],
        torch=torch.__version__,
        torch_cuda_build=torch.version.cuda,
        cuda_available=bool(torch.cuda.is_available()),
        gpu=nvsmi,
        transformers=transformers.__version__,
        datasets=datasets.__version__,
        numpy=np.__version__,
        scipy=scipy.__version__,
        huggingface_hub=huggingface_hub.__version__,
    )

    rec.add(
        "seeds",
        master_seed=MASTER_SEED,
        policy=(
            "per-run seeds = derive_seed(master, *key) via SeedSequence "
            "(mirrors engine/rng.py); seed_everything() before any sampling/"
            "generation; fixed-temperature DecodingConfig below."
        ),
        smoke_run_seed_qwen=derive_seed(MASTER_SEED, 0),
    )
    rec.add("decoding", **DecodingConfig().as_generation_kwargs())

    qwen_snap = Path(
        "/home/vruizes/.cache/huggingface/hub/models--Qwen--Qwen3-1.7B/snapshots/"
        "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
    )
    rec.add(
        "models.qwen3-1.7B",
        repo_id="Qwen/Qwen3-1.7B",
        revision="70d244cc86ccca08cf5af4e1e306ecf908b1ad5e",
        snapshot_path=str(qwen_snap),
        total_bytes=sum(f["bytes"] for f in model_files(qwen_snap).values()),
        files=model_files(qwen_snap),
    )
    rec.add(
        "models.gemma3-1b-pt",
        repo_id="google/gemma-3-1b-pt",
        revision="fcf18a2a879aab110ca39f8bffbccd5d49d8eb29",
        gated="manual",
        status="BLOCKED — no HF token on this machine (user must supply token/auth link)",
        note="google/gemma-3-1b-it is dcc83ea841ab6100d6b47a070329e1ba4cf78752 (also gated)",
        fallback_note=(
            "google/gemma-2-2b (sha c5ebcd40d208330abc697524c919956e692655cf) also gated — "
            "the token blocker applies to every Gemma model; size fallback question moot until token."
        ),
    )

    for name, fname, source, rev, rows in [
        ("nrc_vad", "NRC-VAD-Lexicon.zip", "https://saifmohammad.com/WebDocs/NRC-VAD-Lexicon.zip",
         "sha256 of zip (see file)", 19974),
        ("warriner2013", "Ratings_Warriner_et_al.csv",
         "https://github.com/JULIELab/XANEW (Warriner et al. 2013 secondary distribution)",
         "JULIELab/XANEW master", 13915),
        ("emobank", "emobank.csv", "https://github.com/JULIELab/EmoBank (corpus/emobank.csv)",
         "JULIELab/EmoBank master", 10062),
        ("goemotions_raw", "goemotions_train.parquet",
         "https://huggingface.co/datasets/google-research-datasets/go_emotions",
         "add492243ff905527e67aeb8b80c082af02207c3", 211225),
    ]:
        rec.add_file(f"datasets.{name}", RAW / fname, source=source, revision=rev, rows=rows)
    for split in ["train", "validation", "test"]:
        rec.add_file(
            f"datasets.goemotions_simplified",
            RAW / f"goemotions_simplified_{split}.parquet",
            source="https://huggingface.co/datasets/google-research-datasets/go_emotions",
            revision="add492243ff905527e67aeb8b80c082af02207c3",
            split=split,
        )

    smoke = json.loads((SPIKE_ROOT / "diagnostics" / "smoke_Qwen-Qwen3-1.7B.json").read_text())
    rec.add("smoke.qwen3-1.7B", **smoke)

    out = rec.write()
    print("wrote", out)


if __name__ == "__main__":
    main()
