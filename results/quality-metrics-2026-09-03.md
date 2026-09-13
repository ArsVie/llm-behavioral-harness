# Quality metrics — historical baseline (2026-09-03)

This is a dated measurement record, not the current status. The repository was
refactored after this snapshot; rerun the commands below before using its
structural counts as a current claim.

Scope: `engine/`, `harness/`, `sim/`, `behavioral_signature/`. Every number
below was measured, not estimated; the command that produced it is given so
it can be re-run.

## Where the thresholds stand

| Target | Status | Measured |
|---|---|---|
| Cyclomatic complexity < 22 | **PASS** | 0 of 1,228 callables over |
| Cognitive complexity < 22 | **PASS** | 0 of ~1,230 functions over |
| Halstead difficulty < 80 | **PASS** | max 14.3 (`runtime.py`, file total) |
| Test coverage ≥ 95% | **PASS** | 95.38%, gated at 95 |
| CRAP < 25 | **PASS** | 0 over; worst is 21.2 |
| Dead code = 0 | **PASS** | vulture, confidence 80: none |
| `any` / `unknown` = 0 | **PASS (n/a)** | no TypeScript; 6 `typing.Any`, all duck-typing seams |
| Lines of code per file < 500 | **FAIL** | 12 core files over |
| Surviving mutants = 0 | **FAIL** | 98 survive in `engine/` alone |

## Commands

```bash
# complexity
.venv/bin/complexipy engine harness sim behavioral_signature -mx 22
.venv/bin/python -m radon cc engine harness sim behavioral_signature -n C -s
.venv/bin/python -m radon hal -f engine harness sim behavioral_signature

# dead code
.venv/bin/python -m vulture engine harness sim behavioral_signature --min-confidence 80

# coverage (whole suite: the default one skips the 30-day matrix)
.venv/bin/python -m pytest -q -m '' --cov --cov-report=term-missing

# CRAP (needs a coverage json first)
.venv/bin/python -m pytest -q -m '' --cov --cov-report=json:cov.json
.venv/bin/python scripts/crap_report.py cov.json

# mutation (engine only — see pyproject [tool.mutmut])
.venv/bin/mutmut run && .venv/bin/mutmut results
```

## The two that fail, and why they are not quick

### Lines of code per file (12 over)

```
2524  harness/session.py      921  harness/memory.py       603  harness/channels/telegram.py
2157  harness/store.py        738  harness/assembler.py    561  harness/scheduler.py
1121  harness/tools.py        655  harness/client.py       533  harness/domain.py
1112  harness/runtime.py      654  harness/life.py         507  harness/summarization.py
```

Note the direction of travel: the complexity work made these files LONGER,
not shorter (`runtime.py` 986 → 1112, `session.py` 2427 → 2524), because
extracted helpers each carry their own docstring. Complexity and file length
pull against each other at this stage; only real module splits fix the
second one.

That means the two god objects, which is the decomposition
the 2026-08-28 code-quality review deliberately parked:

- `session.py` → the negotiation state machine, the steering/decision
  execution block, and the conversation lifecycle as three modules. Every
  decision id (`neg-<item_id>-inform`, `neg-<item_id>-decide-<n>`) and the
  `negotiation_state` JSON snapshot are the replay contract, so the parity
  tests are the gate.
- `store.py` → schema + migration chain into `store_schema.py`. The
  migration order and DDL strings must stay byte-identical.

Each needs its own pass with the full suite plus the `-m slow` ablation gate
between steps. Not something to fold into another change.

### Surviving mutants (98 in `engine/`)

Baseline, `engine/` only: **555 mutants, 454 killed, 98 survived, 3 timeouts
— 81.8% killed.**

| module | survivors |
|---|---|
| `cycle.py` | 30 |
| `validation.py` | 24 |
| `circadian.py` | 22 |
| `mood.py` | 13 |
| `timing.py` | 8 |
| `rng.py` | 1 |

One methodological warning worth keeping. A first run with only the
per-module unit tests selected reported 112 survivors including 9 in
`rng.py` — among them replacing `day_rng`'s entire
`SeedSequence(master_seed, spawn_key=(DAILY_STREAM, day))` with `None`,
i.e. making the day RNG non-deterministic. That reads as "replay is
unpinned", which would be alarming and is FALSE: the tests that pin it live
in `test_session.py` and `test_adversarial_restart.py`, which were not in
the selection. Widening it killed 8 of those 9. A mutation score is only as
honest as its test selection — the selection now in `pyproject.toml`
includes the replay tests, and any future widening to `harness/` must do
the same.

The remaining 98 have not been triaged. Expect a meaningful fraction to be
equivalent mutants (semantically identical, unkillable in principle) —
notably boundary flips like `envelope`'s `quiet_ini > quiet_fin` →
`>=`, which only differ when the quiet window has zero width. The honest
next step is to triage them into "real gap", "equivalent", and "don't care",
not to chase the number to zero.

`engine/` was chosen first on purpose: it is the frozen contract layer, so a
genuinely unpinned law there would invalidate every experiment downstream.
`harness/` is ~10× the code with much slower tests per mutant and needs its
own budget.
