"""CRAP report: CC^2 * (1 - coverage)^3 + CC, per callable.

CRAP (Change Risk Anti-Patterns) is the metric that catches what neither
complexity nor coverage catches alone: a branchy function nobody tests.
Low complexity forgives thin coverage and full coverage forgives
complexity, so only the combination scores badly.

Per-callable coverage comes from coverage.json's executed/missing line sets
intersected with each callable's line span (radon's cyclomatic report).
radon reports a method twice (once under its class, once bare), so the
output lists some entries in pairs - the SCORE is what matters, not the
count.

Usage:

    .venv/bin/python -m pytest -q -m '' --cov --cov-report=json:cov.json
    .venv/bin/python scripts/crap_report.py cov.json

Exits 1 when any callable exceeds the threshold (default 25, override with
a second argument).
"""
import json, subprocess, sys

cov_path = sys.argv[1]
cov = json.load(open(cov_path))["files"]

cc_raw = subprocess.run(
    [".venv/bin/python", "-m", "radon", "cc", "-j",
     "engine", "harness", "sim"],
    capture_output=True, text=True).stdout
cc = json.loads(cc_raw)

def spans(path):
    items = cc.get(path)
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        out.append((it["name"], it["lineno"], it["endline"], it["complexity"]))
        for m in it.get("methods") or []:
            out.append((f"{it['name']}.{m['name']}", m["lineno"], m["endline"],
                        m["complexity"]))
    return out

rows = []
for path, entry in cov.items():
    executed = set(entry.get("executed_lines") or [])
    missing = set(entry.get("missing_lines") or [])
    for name, start, end, complexity in spans(path):
        rng = set(range(start, end + 1))
        ex, ms = len(executed & rng), len(missing & rng)
        total = ex + ms
        if total == 0:
            continue
        c = ex / total
        crap = complexity ** 2 * (1 - c) ** 3 + complexity
        rows.append((crap, complexity, c, path, name))

threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0
rows.sort(reverse=True)
over = [r for r in rows if r[0] > threshold]
print(f"callables measured: {len(rows)}")
print(f"CRAP > {threshold:g}: {len(over)}")
for crap, complexity, c, path, name in over:
    print(f"  CRAP {crap:7.1f}  CC {complexity:>3}  cov {c*100:5.1f}%  {path}::{name}")
print("\ntop 8 by CRAP:")
for crap, complexity, c, path, name in rows[:8]:
    print(f"  CRAP {crap:7.1f}  CC {complexity:>3}  cov {c*100:5.1f}%  {path}::{name}")

sys.exit(1 if over else 0)
