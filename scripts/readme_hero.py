"""Render the README hero: one moment of a simulated run, in the web chat.

    .venv/bin/python scripts/readme_hero.py            # docs/images/hero.png
    .venv/bin/python scripts/readme_hero.py --gif      # docs/images/chat.gif instead

The moment is from a committed simulated run (30 days, scripted user,
gpt-5.6-luna): she ends an evening chat, does her night reading, and the next
afternoon writes first because that agenda item finished. The script reads
the five messages and her state at that moment from the run database, serves
the run viewer's /chat page on a free loopback port with HARNESS_CHAT_DB
pointed at that run, and captures it with headless Chromium
(scripts/readme_capture.mjs). Nothing in the panel is typed in by hand:
mood, cycle day, phase, agenda items and the trigger come from the database.

The capture needs Node with playwright-core and a Chromium binary:
    PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core
    CHROMIUM=/path/to/chrome            (optional; playwright's own otherwise)
GIF output also needs ffmpeg on PATH.

The capture intercepts /api/chat/state so the page never talks to a running
bot, and blocks the local expression classifier: her face stays at rest.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
RUN = "results/it3-g5-matrix/no_timing_feedback/seed5002/cell_no_timing_feedback_seed5002.db"
RUN_LABEL = ("simulated 30-day run, scripted user, gpt-5.6-luna, seed 5002 "
             "(confirmatory matrix, timing-feedback ablation cell)")
#: The excerpt: his goodnight, hers, her unprompted message the next
#: afternoon, his hello that evening and her reply. Contiguous ids.
FIRST, UNPROMPTED, LAST = 52, 54, 56


def clock(t_h: float) -> str:
    """Virtual hours into the run as '1:32 PM'."""
    minutes = round((t_h % 24) * 60)
    h, m = divmod(minutes, 60)
    return f"{(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"


def day_of(t_h: float) -> int:
    return int(t_h // 24)


def load(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    msgs = q("SELECT id, role, proactive, t_h, intent_id, content FROM messages "
             "WHERE id BETWEEN ? AND ? ORDER BY id", FIRST, LAST)
    assert [m[0] for m in msgs] == list(range(FIRST, LAST + 1))
    assert all("—" not in m[5] and "–" not in m[5] for m in msgs)
    pro = next(m for m in msgs if m[0] == UNPROMPTED)
    assert pro[1] == "assistant" and pro[2] == 1, "the excerpt's centre must be proactive"
    t = pro[3]
    day = day_of(t)
    (reason, hook, source_id), = q(
        "SELECT reason, hook, source_id FROM proactive_intents WHERE id = ?", pro[4])
    (src_day, src_start, src_end, src_act), = q(
        "SELECT day, start_t_h, end_t_h, activity FROM agenda_items WHERE id = ?", source_id)
    (mood, cycle_day, phase), = q(
        "SELECT M, cycle_day, phase_label FROM daily_state WHERE day = ?", day)
    today = q("SELECT start_t_h, end_t_h, activity, status FROM agenda_items "
              "WHERE day = ? ORDER BY start_t_h", day)
    # Earlier tries on the same item that never reached him.
    held = q("SELECT created_t_h FROM proactive_intents WHERE source_id = ? AND "
             "status = 'suppressed' AND created_t_h < ? ORDER BY created_t_h", source_id, t)
    his_last = max(m[3] for m in msgs if m[1] == "user" and m[3] < t)
    con.close()
    return {"msgs": msgs, "t": t, "day": day, "reason": reason, "hook": hook,
            "source": (src_day, src_start, src_end, src_act), "mood": mood,
            "cycle_day": cycle_day, "phase": phase, "today": today,
            "held": [h[0] for h in held], "his_last": his_last}


PHASES = {"menstrual": "menstrual", "follicular": "follicular", "ovulatory": "ovulatory",
          "luteal_early": "early luteal", "luteal_late": "late luteal"}


def panel(s: dict) -> str:
    """Her state at the moment she wrote, as the card beside the chat."""
    e = html.escape
    dots = "".join(f'<i class="{"on" if i < s["mood"] else ""}"></i>' for i in range(10))
    sd, s0, s1, act = s["source"]
    rows = [f'<li class="d">Day {sd}</li>',
            f'<li><b>{clock(s0)}</b><span>{e(act)} '
            f'<span class="st done">done at {clock(s1)}</span></span></li>',
            f'<li class="d">Day {s["day"]}</li>']
    for start, _end, activity, status in s["today"]:
        if start > s["t"]:
            status = "later"
        word = {"completed": "done", "skipped": "skipped", "shifted": "moved",
                "later": "later"}.get(status, status)
        rows.append(f'<li><b>{clock(start)}</b><span>{e(activity)} '
                    f'<span class="st {word}">{word}</span></span></li>')
    held = ""
    if s["held"]:
        held = (f'<p class="why-sub">A first try at {clock(s["held"][0])} was held back; '
                f'this one fired at {clock(s["t"])}.</p>')
    gap = s["t"] - s["his_last"]
    return f"""
<section class="card hero-state">
  <div class="hs-kicker">Her state at {clock(s["t"])}, day {s["day"]}</div>
  <div class="hs-row"><span class="hs-k">Mood today</span>
    <span class="hs-mood">{dots}</span><span class="hs-v">{s["mood"]} / 10</span></div>
  <div class="hs-row"><span class="hs-k">Cycle</span>
    <span class="hs-v">day {s["cycle_day"]:.0f}, {PHASES.get(s["phase"], s["phase"])} phase</span></div>
  <div class="hs-k hs-block">Her agenda</div>
  <ul class="hs-agenda">{''.join(rows)}</ul>
  <div class="hs-k hs-block">Why she wrote first</div>
  <p class="why"><span class="hook">{e(s["hook"])}</span></p>
  <p class="why">An item on her own agenda ended, so the engine raised a reason
    to reach out. He had been quiet for {gap:.1f} hours.</p>
  {held}
  <p class="hs-foot">The model never sees the mood score or the cycle day: the
    engine turns her state into plain-language briefs, and the model writes
    the words.</p>
</section>"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gif", action="store_true", help="render docs/images/chat.gif instead of the hero")
    args = ap.parse_args()
    db = ROOT / RUN
    s = load(db)
    # A time label wherever the chat page would put one: before the first
    # line and after 30 quiet minutes (chat.js GAP_MIN).
    labels, prev = {}, None
    for mid, _role, _pro, t_h, _iid, _c in s["msgs"]:
        if prev is None or t_h - prev > 0.5:
            labels[mid] = f"Day {day_of(t_h)}, {clock(t_h)}"
        prev = t_h
    last_t = s["msgs"][-1][3]
    h, m = divmod(round((last_t % 24) * 60) + 2, 60)
    config = {
        "ids": [m_[0] for m_ in s["msgs"]], "unprompted": UNPROMPTED,
        "labels": {str(k): v for k, v in labels.items()},
        "panel": panel(s), "clock": [h, m], "footer": f"Excerpt, verbatim: {RUN_LABEL}.",
        "out": str(OUT / "hero.png"),
        "gif_dir": str(ROOT / "docs" / "images" / ".gif-frames") if args.gif else None,
    }
    port = free_port()
    env = dict(os.environ, HARNESS_CHAT_DB=str(db))
    server = subprocess.Popen([sys.executable, "-m", "observability", "--port", str(port)],
                              cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        config["url"] = f"http://127.0.0.1:{port}"
        subprocess.run(["node", str(ROOT / "scripts" / "readme_capture.mjs")],
                       input=json.dumps(config), text=True, check=True)
    finally:
        server.terminate()
        server.wait()
    if args.gif:
        frames = Path(config["gif_dir"])
        palette = frames / "palette.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "10", "-i",
                        str(frames / "f%04d.png"), "-vf",
                        "scale=760:-1:flags=lanczos,palettegen=max_colors=96", str(palette)],
                       check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "10", "-i",
                        str(frames / "f%04d.png"), "-i", str(palette), "-lavfi",
                        "scale=760:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4",
                        "-loop", "0", str(OUT / "chat.gif")], check=True)
        for f in frames.iterdir():
            f.unlink()
        frames.rmdir()


if __name__ == "__main__":
    main()
