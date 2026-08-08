# LLM Behavioral Harness — POC

Arnés (wrapper) compatible con APIs OpenAI que envuelve un LLM e inyecta
**iniciativa** y **variabilidad conductual**: ánimo con memoria, ciclo hormonal
simulado, energía circadiana y cambios sutiles en cadencia, longitud, calidez e
iniciativa. El motor no modifica el modelo base; opera en la orquestación.

## Estado

| Fase | Descripción | Estado |
|------|-------------|--------|
| **−1** | Investigación / prior-art | ✅ Completa — ver [`research/`](research/) |
| 0 | Scaffolding | 🟨 Parcial — paquete numérico y contratos listos; cliente/SQLite/CLI pendientes |
| 1 | Motor estocástico aislado + simulación | ✅ Completa — [informe](results/fase-1-informe.md) y [galería](engine_simulation/README.md) |
| **2** | Actuadores + persona + cronograma + chat reactivo | 🟨 Primera iteración — [plan vertical](plans/fase-2-primera-iteracion.md) y [emulación](results/behavior-showcase/examples.md) |
| 3 | Juez (LLM-as-judge) + bucle de feedback | ⬜ |
| 4 | Iniciativa + scheduler proactivo | ⬜ |
| 5 | Canal Telegram | ⬜ |
| 6 | Backwards compatibility (import + voz) | ⬜ |
| 7 | Evaluación y ablación ciega | ⬜ |

## Primera iteración ejecutable

```powershell
wsl.exe -d Ubuntu -- bash -lc 'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && MPLBACKEND=Agg .venv/bin/python -m experiments.behavior_showcase'
```

Produce una emulación reproducible de 30 días:

- [`30-day-behavior.png`](results/behavior-showcase/30-day-behavior.png) — ánimo,
  expectativa frente a muestra, banda ensemble, energía y controles observables.
- [`phase-semantics.png`](results/behavior-showcase/phase-semantics.png) — distribución
  de ánimo, energía intradía y reactividad comparadas por fase.
- [`phase-summary.json`](results/behavior-showcase/phase-summary.json) — medias,
  variaciones y tasas bajo cinco fases, agregadas sobre 30 semillas.
- [`behavior-trace.json`](results/behavior-showcase/behavior-trace.json) — causas y
  directivas auditables día por día.
- [`examples.md`](results/behavior-showcase/examples.md) — briefs contrastantes
  esperados, sin diálogo enlatado.

La capa implementada vive en [`harness/behavior.py`](harness/behavior.py). La fase
hormonal permanece en la traza; el prompt solo recibe cómo se siente y se expresa
el estado, evitando estereotipos y caricatura.

## Diseño e investigación

- [`DESIGN.md`](DESIGN.md) — arquitectura y matemática del sistema.
- [`plans/fase-2-primera-iteracion.md`](plans/fase-2-primera-iteracion.md) — revisión
  de diseño, invariantes y gate de la primera ola.
- [`research/06-critica-objetivo-implementacion.md`](research/06-critica-objetivo-implementacion.md)
  — crítica que prioriza actuadores, usuario sintético y ablación.
