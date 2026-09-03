"""P2-Gemma-PREP: pin the resolved gemma revision in repro_bundle.json.

Mirrors the qwen model entry shape (repo_id/revision/snapshot_path/
total_bytes/files) while preserving the P0-era gated/note fields.
SHA-256 only the weight file (mirrors the qwen entry, which hashes the
safetensors + tokenizer.json only).
"""
import hashlib
import json
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent.parent / "repro_bundle.json"
REV = "fcf18a2a879aab110ca39f8bffbccd5d49d8eb29"
SNAP = Path.home() / ".cache/huggingface/hub/models--google--gemma-3-1b-pt/snapshots" / REV


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


files = {}
for name in sorted(p.name for p in SNAP.iterdir() if p.is_file()):
    p = SNAP / name
    files[name] = {"bytes": p.stat().st_size,
                   "sha256": sha256(p) if name == "model.safetensors" else None}

total = sum(e["bytes"] for e in files.values())

bundle = json.loads(BUNDLE.read_text())
bundle["sections"]["models.gemma3-1b-pt"] = {
    "repo_id": "google/gemma-3-1b-pt",
    "revision": REV,
    "snapshot_path": str(SNAP),
    "total_bytes": total,
    "files": files,
    "gated": "manual",
    "note": "google/gemma-3-1b-it is dcc83ea841ab6100d6b47a070329e1ba4cf78752 (also gated)",
    "fallback_note": ("google/gemma-2-2b (sha c5ebcd40d208330abc697524c919956e692655cf) also "
                      "gated \u2014 the token blocker applies to every Gemma model; size "
                      "fallback question moot until token."),
    "status": ("PINNED + VERIFIED (P2-Gemma-PREP 2026-08-15): weights downloaded, "
               "CPU analytic check + GPU bringup PASS"),
}
BUNDLE.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
print("updated:", BUNDLE)
print("model.safetensors sha256:", files["model.safetensors"]["sha256"])
print("total_bytes:", total)
