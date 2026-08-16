#!/usr/bin/env bash
# P6 hosted re-judge legs (decision 10) — FAIL LOUDLY on any leg.
# Generations are untouched; only the judge backend differs.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
V=.venv/bin/python
for actor in qwen gemma qwen8b; do
    echo "=== judge $actor (hosted) ==="
    $V scripts/p6_judge.py --actor "$actor" --judge hosted --k 30 --bands low,mid,high --paired \
        2>&1 | tee "data/extractions/p6_judge_hosted_${actor}.log"
done
echo "=== stats ==="
$V scripts/p6_stats.py 2>&1 | tail -50
echo "=== P6 hosted re-judge chain DONE ==="
