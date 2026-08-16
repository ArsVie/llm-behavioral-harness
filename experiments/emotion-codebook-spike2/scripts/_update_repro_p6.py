"""One-off: append the P6 machinery section to repro_bundle.json."""
import json
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "repro_bundle.json"
b = json.loads(p.read_text(encoding="utf-8"))
b["sections"]["p6_behavioral_eval"] = {
    "note": ("P6 machinery (2026-08-16): scripts p6_common/p6_prompts/"
             "p6_generate/p6_judge/p6_stats; G-ABS/G-BEH per "
             "docs/exp-affect-codebook-spike2-2026-08-16.md"),
    "master_seed": 20260815,
    "seed_keys": {"gen": 10, "judge": 11, "boot": 12},
    "bands": {
        "low": {"m": 1, "valence": -0.80, "codebook_value": 0.10},
        "mid": {"m": 6, "valence": 0.20, "codebook_value": 0.60},
        "high": {"m": 9, "valence": 0.80, "codebook_value": 0.90},
    },
    "energy_fixed": 0.55,
    "decoding": {
        "temperature": 0.8, "top_p": 0.9, "top_k": 40, "do_sample": True,
        "max_new_tokens": 128, "repetition_penalty": 1.0,
    },
    "judge_rule": ("cross-family (decision 3): qwen/qwen8b -> gemma; "
                   "gemma -> qwen; never actor==judge"),
    "judge_decoding": "greedy do_sample=False",
    "bootstrap": {"n": 10000, "method": "percentile 2.5/97.5, seeded"},
    "token_renderer": ("P6 token-to-prose renderer v1 (top-k=10, filtered, "
                       "deduped, lowercase-folded, space-joined)"),
}
p.write_text(json.dumps(b, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("repro_bundle updated; sections:", sorted(b["sections"]))
