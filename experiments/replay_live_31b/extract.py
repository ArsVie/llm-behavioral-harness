"""Extract replay scenarios from the live companion DB (read-only) for the 31B replay.

Frozen pre-registration: results/replay-live-31b-2026-10-03/README.md.

Scenarios: five live chat calls (ids 188, 162, 144, 122, 107) replayed on gemma-4-31B.
Variants (bearing-block surgery, blank-line blocks inside STATE UPDATE rows):
  A   = full context, verbatim
  B   = remove the LAST BEARING block (the latest behavioral/affective card)
  B2  = remove ALL BEARING blocks
  C   = B2 + valence steering (applied in the Modal run)
  D   = A + valence steering (applied in the Modal run)

Local rendering map (recorded OpenAI stream -> local chat template):
  system -> system verbatim; user -> user verbatim; assistant -> assistant (content);
  assistant rows that carry only tool_calls and empty content are dropped;
  tool rows -> system rows verbatim (the harness text stays out of user slots).

Run: .venv/bin/python -m experiments.replay_live_31b.extract
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DB = ROOT / "results" / "live-companion" / "companion.db"
OUT = ROOT / "results" / "replay-live-31b-2026-10-03" / "raw"

SCENARIOS = [188, 162, 144, 122, 107]
BEARING_RE = re.compile(r"^(BEHAVIORAL BEARING|AFFECTIVE BEARING):")


def render_stream(msgs: list[dict]) -> list[dict]:
    """Recorded OpenAI stream -> local-renderable message list."""
    out: list[dict] = []
    for m in msgs:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "assistant":
            if m.get("tool_calls") and not content.strip():
                continue  # pure tool mechanics; the visible effect follows in the stream
            out.append({"role": "assistant", "content": content})
        elif role == "tool":
            out.append({"role": "system", "content": content})
        elif role in ("system", "user"):
            out.append({"role": role, "content": content})
        else:
            raise ValueError(f"unexpected role {role!r}")
    return out


def bearing_positions(msgs: list[dict]) -> list[tuple[int, int]]:
    """(row_idx, block_idx) of every BEARING block in system rows."""
    pos = []
    for i, m in enumerate(msgs):
        if m["role"] != "system":
            continue
        for j, block in enumerate(m["content"].split("\n\n")):
            if BEARING_RE.match(block.strip()):
                pos.append((i, j))
    return pos


def drop_blocks(msgs: list[dict], drops: set[tuple[int, int]]) -> list[dict]:
    out = []
    for i, m in enumerate(msgs):
        if m["role"] != "system" or not any(d[0] == i for d in drops):
            out.append(dict(m))
            continue
        blocks = m["content"].split("\n\n")
        kept = [b for b in blocks if b.strip()]
        kept = [b for j, b in enumerate(blocks) if b.strip() and (i, j) not in drops]
        # drop the row when only the header block remains
        if not any(b.strip() != "STATE UPDATE:" for b in kept):
            continue
        out.append({"role": "system", "content": "\n\n".join(kept)})
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()
    scenarios = []
    for sid in SCENARIOS:
        row = cur.execute(
            "SELECT id, day, t_h, role, response, repro_json FROM llm_calls WHERE id=?",
            (sid,),
        ).fetchone()
        assert row and row[3] == "chat", f"call {sid} is not a chat call"
        id_, day, t_h, _, response, rj = row
        d = json.loads(rj)
        persona = d["system"]
        stream = render_stream(d["messages"])
        bpos = bearing_positions(stream)
        assert bpos, f"call {sid} has no bearing blocks"
        variants = {
            "A": drop_blocks(stream, set()),
            "B": drop_blocks(stream, {bpos[-1]}),
            "B2": drop_blocks(stream, set(bpos)),
        }
        scenarios.append({
            "call_id": id_,
            "day": day,
            "t_h": t_h,
            "temperature_recorded": d.get("temperature"),
            "persona": persona,
            "variants": variants,
            "recorded_response": response,
            "meta": {
                "n_messages_raw": len(d["messages"]),
                "n_messages_rendered": len(stream),
                "n_bearing_blocks": len(bpos),
                "n_bearing_blocks_removed_B": 1,
                "n_bearing_blocks_removed_B2": len(bpos),
                "last_user": next((m["content"] for m in reversed(stream) if m["role"] == "user"), None),
            },
        })
        print(f"[extract] call {sid}: rendered={len(stream)} msgs, bearings={len(bpos)}, "
              f"A={len(variants['A'])} B={len(variants['B'])} B2={len(variants['B2'])}")
    con.close()
    (OUT / "scenarios.json").write_text(json.dumps(scenarios, indent=1) + "\n")
    print(f"[extract] wrote {OUT / 'scenarios.json'}")


if __name__ == "__main__":
    main()
