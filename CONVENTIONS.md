---
type: conventions
title: Repository conventions — Phase 1 (frozen in Wave 0 / W0.1; revised 2026-08-08)
description: Operating rules for every task (human or subagent) in this repo — working environment (native WSL), git and Conventional Commits, frozen files, wave ownership, code conventions, tests, and experiments.
tags: [conventions, repo, phase-1, wsl]
timestamp: 2026-08-08
---

# CONVENTIONS — Phase 1 (frozen in Wave 0 / W0.1; revised 2026-08-08)

Operating rules for every task (human or subagent) in this repo.
Read together with `engine/types.py` (frozen contract) before writing code.

## 1. Working environment (native WSL)

The project lives on the native WSL filesystem and is worked on from
**native WSL** (Hermes agent): `/home/vruizes/.hermes/projects/llm-behavioral-harness`
is the real path, and direct bash/python works fine. The old warning about a
desynced Bash tool view applied to a Windows-side harness view and is
obsolete. (For reference, the Windows UNC view is
`\\wsl.localhost\ubuntu\home\vruizes\.hermes\projects\llm-behavioral-harness`.)

**Version control:** the repo is under git (`main` branch). Commits follow
**Conventional Commits** (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, ...).

## 2. Verified runtime (W0.1, 2026-07-03)

- WSL distro **Ubuntu** · Python **3.12.3** (`/usr/bin/python3`) · `uv 0.x` in `~/.local/bin`.
- Project venv: `.venv` (created with `uv venv`), dependencies installed
  with `uv pip install -e ".[dev]"` → numpy 2.5.0, scipy 1.18.0,
  matplotlib 3.11.0, pyyaml 6.0.3, pytest 9.1.1. Package installed editable
  (`import engine`, `import sim` work from any cwd).

### Canonical commands (native bash)

Full suite:

```bash
cd /home/vruizes/.hermes/projects/llm-behavioral-harness && MPLBACKEND=Agg .venv/bin/python -m pytest
```

A single test file (the norm for a wave task):

```bash
cd /home/vruizes/.hermes/projects/llm-behavioral-harness && MPLBACKEND=Agg .venv/bin/python -m pytest tests/test_mood.py -q
```

Run a module/script:

```bash
cd /home/vruizes/.hermes/projects/llm-behavioral-harness && MPLBACKEND=Agg .venv/bin/python -m sim.run_daily --days 90 --seed 12345
```

Reinstall deps (only if `pyproject.toml` changes):

```bash
cd /home/vruizes/.hermes/projects/llm-behavioral-harness && uv pip install --python .venv/bin/python -e ".[dev]"
```

## 3. File ownership (wave rule)

- **Frozen after Wave 0 (read-only):** `engine/types.py`, `engine/rng.py`,
  `tests/conftest.py`, `pyproject.toml`, this file.
- Each task owns **its module + its test** (e.g. W1.1 → `engine/mood.py` +
  `tests/test_mood.py`) and, in Wave 3, **its folder** `results/<experiment>/`.
  Nobody touches another task's files, not even to "fix" something: if a
  foreign file looks wrong, it is reported in the task's final summary.
- The stubs already define signature + semantics; implement EXACTLY those
  signatures (additional private helpers in the file itself are allowed).

## 4. Code conventions

- Python 3.11+; type hints in public signatures; **docstrings in English
  going forward** (the old "docstrings in Spanish" rule is superseded),
  identifiers in English (as in the stubs).
- `engine/` is **pure**: no I/O, no reads of the real clock
  (`time`, `datetime.now` are forbidden — the clock is virtual), no global
  state. All randomness enters through an injected `numpy.random.Generator`.
- RNG: only via `engine/rng.py` (hierarchical SeedSequence). Never
  `np.random.seed` nor an unseeded `default_rng()` in production code.
- States (`MoodState`, `CycleState`) are not mutated: steps return new
  instances.
- Matplotlib only in `sim/plots.py` and experiments, always Agg backend
  (`matplotlib.use("Agg")` before importing pyplot).
- Time: whole days for the slow scale; absolute float hours for the fast
  scale (t_h=0 ⇒ day 0, 00:00; local hour = t_h % 24). See `types.py`.

## 5. Tests

- Plain pytest (no extra plugins). Statistical tests use a fixed seed
  and tolerances documented in the test itself (generous: they check shape,
  not decimals — e.g. KS with α=0.01, means with ±3·sem).
- Each task runs **only its own** test file and reports pass/fail; the full
  suite is run by the main session at each wave gate.
- Experiment figures fix the seed and write it in the title and in the
  report name.

## 6. Experiments (Wave 3)

- One script per experiment in `experiments/` (`w31_baseline.py`, ...),
  runnable with `.venv/bin/python -m experiments.<name>` or as a script.
- Outputs ONLY in the own `results/<id>/` folder: `*.png` + **`report.md`**
  (new reports are named `report.md` in English; old ones keep `reporte.md`)
  with (a) seeds used, (b) evaluated criterion/criteria with a numeric
  threshold, (c) pass/fail verdict per criterion, (d) 2–5 lines of reading.
