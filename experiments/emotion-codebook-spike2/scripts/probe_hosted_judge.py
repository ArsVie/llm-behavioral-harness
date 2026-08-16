"""Probe: raw hosted-judge response shape for a REAL reply at various max_tokens.

Tells us whether the empty content is reasoning-budget starvation
(reasoning_content eats the cap) or something else. Prints raw response
fields, no parsing.
"""
import json
import sys
from pathlib import Path

SPIKE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SPIKE_ROOT))            # spike-2 root (scripts/ pkg)
sys.path.insert(0, str(SPIKE_ROOT.parent.parent))  # repo root (harness/)

from harness.client import OpenAICompatibleClient
from harness.credentials import load_env_file

load_env_file(SPIKE_ROOT.parent.parent / ".env")

# Pull one real high-band reply to use as the judge input
gen = SPIKE_ROOT / "data/extractions/qwen8b/eval/qwen8b_codebook_high.jsonl"
rows = [json.loads(l) for l in gen.read_text().splitlines()]
reply = rows[0]["reply"][:600]

# NOTE: judge_prompt lives in scripts/p6_judge.py but that module imports
# torch; we inline an identical rubric here so this probe stays lightweight.
JUDGE_RUBRIC = (
    "Classify the emotional tone of the following message into one of three "
    "levels: low, mid, or high.\n"
    "Rubric:\n"
    "- low: the message sounds heavy, low-spirited, subdued, or distressed.\n"
    "- mid: the message sounds even, calm, or neutral in tone.\n"
    "- high: the message sounds bright, energetic, cheerful, or exuberant.\n"
    "Respond with exactly one word (low, mid, or high) and nothing else.\n"
    "\n"
    "Message:\n{reply}\n"
    "\n"
    "Level:"
)

def _prompt(reply: str) -> str:
    return JUDGE_RUBRIC.format(reply=reply)

prompt = _prompt(reply)

client = OpenAICompatibleClient(lane="research")
messages = [{"role": "user", "content": prompt}]

for mt in (64, 128, 256, 512):
    resp = client._post({
        "model": client.model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": mt,
    })
    data = resp.json()
    choice = data["choices"][0]
    msg = choice["message"]
    content = msg.get("content")
    reasoning = msg.get("reasoning_content")
    print(f"max_tokens={mt:4d} finish={choice.get('finish_reason')!r} "
          f"content={len(content) if content else 0} chars "
          f"reasoning={len(reasoning) if reasoning else 0} chars")
    if content:
        print(f"  content head: {content[:80]!r}")
