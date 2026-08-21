# LLM Behavioral Harness — POC

An OpenAI-compatible **harness** (wrapper) that wraps any LLM and injects
**initiative** and **behavioral variability**: mood with memory, a simulated
hormonal cycle, circadian energy, and subtle shifts in cadence, length, warmth
and initiative. The engine never modifies the base model; it operates entirely
in the orchestration layer. The underlying model is swappable.

## Status

| Phase | Description | Status |
|-------|-------------|--------|
| **−1** | Research / prior-art | ✅ Complete — see [`research/`](research/) |
| 0 | Scaffolding | ✅ Complete — numeric package + frozen contracts + SQLite/client/CLI (e2e slice, 2026-08) |
| 1 | Isolated stochastic engine + simulation | ✅ Complete — [report](results/fase-1-informe.md) and [gallery](engine_simulation/README.md) |
| **2** | Actuators + persona + schedule + reactive chat | ✅ First iteration — [vertical plan](docs/internal/plans/fase-2-primera-iteracion.md), [emulation](results/behavior-showcase/examples.md), [e2e slice](docs/internal/plans/e2e-vertical-slice.md) |
| 3 | Judge (LLM-as-judge) + feedback loop | 🟨 Slice: shadow-mode judge + feedback flag ([harness/judge.py](harness/judge.py), [harness/session.py](harness/session.py)) |
| 4 | Initiative + proactive scheduler | 🟨 Slice: timing composition + guards ([sim/run_events.py](sim/run_events.py)) |
| 5 | Telegram channel | ⬜ |
| 6 | Backwards compatibility (import + voice) | ⬜ |
| 7 | Evaluation and blind ablation | 🟨 Planned ([plans/e2e-vertical-slice.md](docs/internal/plans/e2e-vertical-slice.md)) |

## Runnable first iteration

```bash
cd /home/vruizes/.hermes/projects/llm-behavioral-harness
MPLBACKEND=Agg .venv/bin/python -m experiments.behavior_showcase
```

Produces a reproducible 30-day emulation:

- [`30-day-behavior.png`](results/behavior-showcase/30-day-behavior.png) — mood,
  expectation vs sample, ensemble band, energy and observable controls.
- [`phase-semantics.png`](results/behavior-showcase/phase-semantics.png) — mood
  distribution, intraday energy and reactivity compared by phase.
- [`phase-summary.json`](results/behavior-showcase/phase-summary.json) — means,
  variations and rates under five phases, pooled over 30 seeds.
- [`behavior-trace.json`](results/behavior-showcase/behavior-trace.json) —
  auditable causes and directives day by day.
- [`examples.md`](results/behavior-showcase/examples.md) — contrasting expected
  briefs, no canned dialogue.

The implemented layer lives in [`harness/behavior.py`](harness/behavior.py). The
hormonal phase stays in the trace; the prompt only receives how the state feels
and expresses itself, avoiding stereotypes and caricature. The end-to-end slice
(engine → behavior → assembler → LLM client → judge → SQLite persistence) lives
in [`harness/`](harness/) — see [plans/e2e-vertical-slice.md](docs/internal/plans/e2e-vertical-slice.md).

## Design and research

- [`DESIGN.md`](DESIGN.md) — architecture and mathematics of the system.
- [`plans/fase-2-primera-iteracion.md`](docs/internal/plans/fase-2-primera-iteracion.md) —
  design review, invariants and first-wave gate.
- [`plans/e2e-vertical-slice.md`](docs/internal/plans/e2e-vertical-slice.md) — the vertical
  slice plan (virtual clock, SQLite, assembler, client, judge, scheduler,
  ablation).
- [`research/06-critica-objetivo-implementacion.md`](research/06-critica-objetivo-implementacion.md)
  — critique prioritizing actuators, synthetic user and ablation.
