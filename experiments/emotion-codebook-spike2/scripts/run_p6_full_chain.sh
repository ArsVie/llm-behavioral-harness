#!/usr/bin/env bash
# Spike-2 P6 full behavioral-eval chain (orchestrator-owned, serialized on GPU).
# Legs run sequentially so the GPU is never contended (gpu_clear lesson).
# Usage: bash scripts/run_p6_full_chain.sh > data/extractions/p6_chain.log 2>&1
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
K=30

echo "[chain] $(date -Is) start"

run() {
  echo "[chain] === $* ==="
  "$@" || { echo "[chain] FAILED: $*"; exit 1; }
}

# --- Generation: 3 actors x 2 variants, K per band, low,mid,high ---
run $PY scripts/p6_generate.py --actor qwen   --variant renderer --k $K --bands low,mid,high --device cuda --dtype bf16
run $PY scripts/p6_generate.py --actor qwen   --variant codebook --k $K --bands low,mid,high --device cuda --dtype bf16
run $PY scripts/p6_generate.py --actor gemma  --variant renderer --k $K --bands low,mid,high --device cuda --dtype bf16
run $PY scripts/p6_generate.py --actor gemma  --variant codebook --k $K --bands low,mid,high --device cuda --dtype bf16
run $PY scripts/p6_generate.py --actor qwen8b --variant renderer --k $K --bands low,mid,high --device cuda --dtype bf16
run $PY scripts/p6_generate.py --actor qwen8b --variant codebook --k $K --bands low,mid,high --device cuda --dtype bf16

# --- Judging: hosted (decision 10: contract line 80 sanctions the hosted
# --- API model; local base judges demonstrated non-functional as 3-way
# --- classifiers), paired G-BEH ---
run $PY scripts/p6_judge.py --actor qwen   --judge hosted --k $K --bands low,mid,high --paired
run $PY scripts/p6_judge.py --actor gemma  --judge hosted --k $K --bands low,mid,high --paired
run $PY scripts/p6_judge.py --actor qwen8b --judge hosted --k $K --bands low,mid,high --paired

# --- Stats + gate verdicts ---
run $PY scripts/p6_stats.py

echo "[chain] $(date -Is) DONE"
