"""Verify hosted-judge outputs are non-degenerate (not another collapse)."""
import json
import collections
from pathlib import Path

SPIKE = Path(__file__).resolve().parent.parent

for actor in ("qwen", "gemma", "qwen8b"):
    print(f"===== {actor} =====")
    for variant in ("renderer", "codebook"):
        print(f"== {variant} ==")
        for band in ("low", "mid", "high"):
            p = SPIKE / "data/extractions" / actor / "eval" / \
                f"{actor}_judged_hosted_{variant}_{band}.jsonl"
            rows = [json.loads(l) for l in p.read_text().splitlines()]
            dist = collections.Counter(r["level"] for r in rows)
            n_corr = sum(r["correct"] for r in rows)
            print(f"  {band}: n={len(rows)} levels={dict(dist)} "
                  f"correct={n_corr}/{len(rows)}")

print("\n== raw judge output samples (qwen8b codebook high) ==")
p = SPIKE / "data/extractions/qwen8b/eval/qwen8b_judged_hosted_codebook_high.jsonl"
for r in [json.loads(l) for l in p.read_text().splitlines()][:5]:
    print(f"  level={r['level']!r} raw={r['raw_judge_output'][:50]!r} "
          f"correct={r['correct']}")

print("\n== raw judge output samples (qwen8b renderer high) ==")
p = SPIKE / "data/extractions/qwen8b/eval/qwen8b_judged_hosted_renderer_high.jsonl"
for r in [json.loads(l) for l in p.read_text().splitlines()][:5]:
    print(f"  level={r['level']!r} raw={r['raw_judge_output'][:50]!r} "
          f"correct={r['correct']}")