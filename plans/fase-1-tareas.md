# Fase 1 — Plan de tareas por olas (subagentes en paralelo)

**Fecha:** 2026-07-03
**Objetivo de la fase:** motor estocástico aislado + simulación de 60–90 días con gráficas validadas, **sin LLM**. Especificación matemática en [DESIGN.md](../DESIGN.md); criterios ampliados en [research/05-reevaluacion-diseno.md](../research/05-reevaluacion-diseno.md) §6.
**Regla de las olas:** cero dependencias entre tareas de la misma ola. Dependencias solo entre olas (cada ola consume lo congelado por las anteriores).

## Contexto para una sesión nueva (leer antes de ejecutar)

1. **Orden de lectura:** [README](../README.md) → [DESIGN.md](../DESIGN.md) (especificación completa: ecuaciones del motor, tabla de **parámetros por defecto**, modelo de datos) → [research/05-reevaluacion-diseno.md](../research/05-reevaluacion-diseno.md) §6 (criterios de validación ampliados) → este plan. La crítica [research/06](../research/06-critica-objetivo-implementacion.md) da contexto de prioridades (tiers) pero no bloquea la Fase 1.
2. **Alcance acordado con el usuario:** PoC local mono-usuario, **sin** guardrails de seguridad/bienestar (archivados en `research/deferred/`); la complejidad del motor es intencional (la varianza emocional percibida día/mes es el objetivo); las formulaciones matemáticas son propias del usuario — el paper arXiv 2508.11829 es solo inspiración inicial.
3. **Entorno (quirks conocidos):** el proyecto vive en `\\wsl.localhost\ubuntu\home\vruizes\.hermes\projects\llm-behavioral-harness` (vista Windows, la que usan Read/Write/Edit/Glob) = `/home/vruizes/.hermes/projects/llm-behavioral-harness` (vista WSL). **La herramienta Bash mostró una vista desincronizada de este árbol en al menos una ocasión** (archivos escritos con Write no visibles vía Bash); para mover/copiar archivos usar PowerShell con la ruta `\\wsl.localhost\...`, y para contenido usar las herramientas de archivo. Dónde y cómo correr Python/pytest **no está verificado** — verificarlo y documentarlo en `CONVENTIONS.md` es entregable explícito de W0.1.
4. **Decisiones abiertas que W0.1 puede tomar sin consultar:** valores no fijados en DESIGN (p. ej. quiet hours por defecto, ~23:00–08:00; rangos exactos de días por fase del ciclo) — elegir razonablemente y documentar en `types.py`.

---

## Principios de paralelización

1. **Contract-first.** Los tipos, firmas y convenciones se congelan en la Ola 0 (`engine/types.py` + stubs). Toda tarea posterior codifica contra ese contrato sin ver el trabajo de las demás.
2. **Los módulos no se importan entre sí.** `mood` recibe `m`, `g` como floats — no importa `cycle`. `timing` recibe un modulador `Callable` — no importa `circadian`. La composición ocurre solo en los drivers de la Ola 2. Esto es lo que hace las olas paralelizables *y* es la arquitectura correcta (módulos testeables en aislamiento).
3. **Propiedad disjunta de archivos.** Cada tarea posee sus archivos (módulo + su test + su carpeta de resultados) y no toca los de nadie más. Los archivos compartidos (`types.py`, `conftest.py`, `pyproject.toml`) son de la Ola 0 y de solo-lectura después.
4. **Auto-verificación.** Cada tarea corre sus propios tests/figuras y reporta pass/fail — ningún agente necesita el output de otro agente de su misma ola.
5. **Reproducibilidad.** RNG por `numpy.random.SeedSequence` con spawn jerárquico (companion → día); toda figura/experimento fija y reporta su semilla.

---

## Estructura de archivos objetivo

```
llm-behavioral-harness/
├── pyproject.toml, README, CONVENTIONS.md      (Ola 0)
├── engine/
│   ├── types.py        (Ola 0 — CONGELADO)     dataclasses, enums, firmas
│   ├── rng.py          (Ola 0)                 SeedSequence, spawn por día
│   ├── mood.py         (W1.1)                  beta-binomial logit, μ, η, variantes
│   ├── cycle.py        (W1.2)                  m(t), g(t), redraw de L, fases
│   ├── circadian.py    (W1.3)                  c(h), energía, envolvente circ(t)
│   ├── timing.py       (W1.4)                  hazard Weibull + thinning
│   └── validation.py   (W1.7)                  validación de config + cota de estabilidad
├── sim/
│   ├── metrics.py      (W1.5)                  métricas de aceptación (arrays → floats)
│   ├── plots.py        (W1.6)                  figuras (SimResult → png)
│   ├── run_daily.py    (W2.1)                  bucle día-a-día + score sintético
│   └── run_events.py   (W2.2)                  stream de eventos de temporización
├── tests/
│   ├── conftest.py     (Ola 0)
│   └── test_<módulo>.py  (cada tarea el suyo)
├── experiments/        (W3.x — un script por experimento)
└── results/<experimento>/  (figuras + reporte.md por experimento)
```

---

## Contrato a congelar en la Ola 0 (resumen)

```python
# engine/types.py
class MoodVariant(Enum): ORIGINAL; DECOUPLED; DECOUPLED_OFFSETS
  # ORIGINAL:  arg = (logit λ + μ)·g          (plan §3, B≡0, η≡0, ν=∞)
  # DECOUPLED: arg = logit λ + g·(μ + η)      (B≡0)
  # DECOUPLED_OFFSETS: arg = logit λ + m + g·(μ + η)

@dataclass(frozen=True) class PersonaParams:   # N, lam, nu, k, rho, rho_e, sigma_e, B, A, sigma_eps, L_mean, L_sd, phi, score_neutral
@dataclass(frozen=True) class TimingParams:    # k_w, theta_h, peak_hour, diurnal_amp, quiet_hours, phase_multipliers, adj_bounds, min_gap_min, daily_cap
@dataclass class CycleState:                   # cycle_day, L_current
@dataclass class MoodState:                    # mu, eta
@dataclass class DayRecord:                    # t, m, g, arg, p, M, score, mu, eta, cycle_day, phase_label, seed
@dataclass class SimResult:                    # params, variant, records: list[DayRecord] (+ propiedades como arrays)

# Firmas clave (stubs con docstring en Ola 0):
cycle.step(state, params, rng)            -> (m: float, g: float, phase_label: str, state)
mood.step(state, params, m, g, variant, rng) -> (M: int, p: float, arg: float)
mood.update(state, params, score)         -> state          # μ ← ρμ + k(score − neutral)
mood.step_endogenous(state, params, rng)  -> state          # η AR(1)
circadian.c(h, params)                    -> float          # valencia intradía
circadian.energy(h, phase_label, params)  -> float
circadian.envelope(h, params)             -> float          # [0,1], 0 en quiet hours
timing.next_event(t_now, t_last_interaction, modulator: Callable[[float], float], params, rng) -> float
validation.check(persona, timing)         -> list[str]      # errores; incluye cota k < 2(1−ρ)/g_max (peor caso p(1−p)=0.25, g_max=1+A+3σ_ε)
```

Score sintético (cierra el lazo en simulación, driver W2.1): `score = clip(2·(M/N − 0.5) + Normal(0, 0.2), −1, 1)`, con override por guion (shocks).

---

## Ola 0 — Contratos + scaffolding (secuencial, 1 tarea)

| ID | Tarea | Entregable | Ejecutor |
|---|---|---|---|
| W0.1 | Scaffolding mínimo + contrato congelado | `pyproject.toml` (numpy/scipy/matplotlib/pytest), estructura de carpetas, `engine/types.py` completo, `engine/rng.py`, stubs con firmas + docstrings de todos los módulos, `tests/conftest.py`, `CONVENTIONS.md` (incluye **verificación del runtime**: qué herramienta ejecuta Python/pytest en este entorno WSL/Windows y cómo — se prueba y documenta aquí, des-riesga todas las olas) | Sesión principal (no subagente — el contrato define todo lo demás) |

> Nota de fases: W0.1 adelanta solo la porción de Fase 0 que la Fase 1 necesita (paquete + config numérica). Cliente LLM, CLI y SQLite siguen siendo Fase 0 y no se tocan aquí.

**Gate → Ola 1:** `pytest` corre (aunque sea colección vacía), `import engine.types` funciona, CONVENTIONS.md dice cómo ejecutar.

---

## Ola 1 — Módulos del motor (7 tareas paralelas)

Cada tarea: implementa su módulo contra `types.py`, escribe su archivo de test, corre pytest de sus archivos, reporta. No lee ni escribe archivos de otras tareas.

| ID | Módulo (archivos propios) | Contenido | Tests de aceptación de la tarea | Modelo |
|---|---|---|---|---|
| W1.1 | `engine/mood.py`, `tests/test_mood.py` | Las 3 variantes de `arg`; beta-binomial con caso especial `ν=∞` → binomial exacta (`p_day ~ Beta(pν,(1−p)ν)` → `Bin(N,p_day)`); μ y η | ν=∞ reproduce binomial (test estadístico); recursión de μ vs forma cerrada `μ∞=k·s/(1−ρ)`; sd estacionaria de η ≈ `σ_e/√(1−ρ_e²)`; las 3 variantes coinciden cuando B=0, η≡0, ν=∞ y g≡1 | sonnet |
| W1.2 | `engine/cycle.py`, `tests/test_cycle.py` | m(t), g(t) senoidales sobre reloj de ciclo; redraw `L_i~N(28,1.5)` al completar ciclo; etiquetas de fase (5 fases por rangos de día) | Media/amplitud de m,g correctas; L redibujada con stats correctos; fase correcta en fronteras; periodicidad ~L (autocorrelación) | haiku |
| W1.3 | `engine/circadian.py`, `tests/test_circadian.py` | `c(h)` coseno (pico 14:00, ±0.25), canal energía (circadiano+fase), `envelope(h)` con quiet hours = 0 y transición suave | Valores en horas ancla; envelope=0 en quiet hours; continuidad; energía difiere por fase | haiku |
| W1.4 | `engine/timing.py`, `tests/test_timing.py` | Hazard `h(τ,t)=(k_w/θ)(τ/θ)^{k_w−1}·modulator(t)`; muestreo del siguiente evento por **thinning** con cota superior correcta | Con modulador≡1: gaps ~ Weibull (KS test); `k_w=1` → exponencial (memoryless); hazard creciente para k_w>1 (gaps con moda>0); con modulador escalón: cero eventos donde modulator=0; tasa escala con el multiplicador | sonnet |
| W1.5 | `sim/metrics.py`, `tests/test_metrics.py` | Funciones puras array→float: media/sd de M, autocorr lag-1, ratio de varianza g-alta vs g-baja, tiempo de reversión tras shock, stats de gaps (media diaria, moda, burstiness), histograma horario vs envolvente | Cada métrica verificada sobre series sintéticas con valor conocido | sonnet |
| W1.6 | `sim/plots.py`, `tests/test_plots.py` | Figuras estándar desde `SimResult`/arrays: serie M(t) con banda, m/g(t), histograma de M, μ/η(t), histograma horario de eventos, comparativa por variante. Estilo único, semilla en el título | Smoke: genera png sin error desde fixtures sintéticos; nombres de archivo deterministas | haiku |
| W1.7 | `engine/validation.py`, `tests/test_validation.py` | Validación de `PersonaParams`/`TimingParams`: rangos, **cota de estabilidad** `k < 2(1−ρ)/g_max` con `g_max=1+A+3σ_ε`, `adj_bounds⊂[0.5,1.5]`, quiet hours coherentes | Configs válidas pasan; cada violación produce su error; frontera de la cota exacta | haiku |

**Gate → Ola 2:** pytest completo verde; revisión rápida de firmas contra el contrato (sesión principal).

---

## Ola 2 — Drivers de simulación (2 tareas paralelas)

| ID | Archivos propios | Contenido | Aceptación de la tarea | Modelo |
|---|---|---|---|---|
| W2.1 | `sim/run_daily.py`, `tests/test_run_daily.py` | Bucle día-a-día 60–90 días componiendo cycle+mood(+c para arg_h de referencia); score sintético con override por guion (rachas negativas programables); produce `SimResult`; CLI mínima (`--days --seed --variant --params yaml`) | Corre 90 días determinista con semilla fija; los guiones de shock aparecen en los records; smoke de integración | sonnet |
| W2.2 | `sim/run_events.py`, `tests/test_run_events.py` | Stream de eventos continuo componiendo timing+envelope circadiano+multiplicadores de fase+adj(score); respeta min_gap y daily_cap (guards en cola); produce timestamps | Corre 90 días simulados; determinista; guards verificados (ningún gap<min, ningún día>cap) | sonnet |

Ambas dependen de la Ola 1 completa; entre sí, nada.

**Gate → Ola 3:** ambos drivers corren end-to-end con params por defecto.

---

## Ola 3 — Experimentos de validación (5 tareas paralelas)

Cada tarea: un script en `experiments/`, resultados y figuras en `results/<id>/`, y un `reporte.md` corto con pass/fail por criterio. Semillas fijas y múltiples (≥5) donde haya stats.

| ID | Experimento (resultados propios) | Valida (criterio) | Modelo |
|---|---|---|---|
| W3.1 | **Baseline**: 90 días, params por defecto, variante DECOUPLED_OFFSETS | (1) media de M estable ≈ `N·sigmoid(logit λ)` con desviaciones acotadas; (2) m/g ondas limpias de periodo ~L; (3) histograma de M sin saturación; (4) var(M) mayor en g-alta vs g-baja; (6) autocorr lag-1 ∈ [0.2, 0.5] | sonnet |
| W3.2 | **Comparativa de variantes**: ORIGINAL vs DECOUPLED vs DECOUPLED_OFFSETS, mismas semillas | (8a) diferencias documentadas: acoplamiento media-ganancia del ORIGINAL visible; autocorr sin/con η; recomendación razonada de variante | sonnet |
| W3.3 | **Barrido de parámetros**: grid sobre A, k, ρ, B, ρ_e, σ_e, ν (respetando la cota de estabilidad); heatmaps de métricas | (8b) región de régimen "humano" (media estable + varianza viva + autocorr en rango); propuesta de defaults afinados | sonnet |
| W3.4 | **Validación de temporización**: histograma horario, gaps, tasas por fase, k_w ∈ {1, 1.5, 2, 3} | (7) horario dentro de la envolvente; 0 eventos en quiet hours; media diaria ∈ [1,3]; hazard creciente visible (moda de gaps > 0 para k_w>1); efecto de multiplicadores de fase medible | sonnet |
| W3.5 | **Shocks y estabilidad del lazo**: rachas negativas programadas; k cerca de la cota | (5) μ cae y revierte en ~`1/(1−ρ)` días; verificación empírica de la cota de estabilidad (estable justo debajo, oscila/diverge por encima) | sonnet |

Sin dependencias entre experimentos: cada uno importa la infraestructura de Olas 1–2 y escribe solo en su carpeta.

**Gate → Ola 4:** los 5 reportes existen con figuras.

---

## Ola 4 — Síntesis y aceptación (secuencial, 1 tarea)

| ID | Tarea | Entregable | Ejecutor |
|---|---|---|---|
| W4.1 | Agregar los 5 reportes, tabla de criterios 1–8 con pass/fail y figura de evidencia, elección razonada de variante + defaults afinados, riesgos abiertos | `results/fase-1-informe.md` — insumo para la revisión conjunta de parámetros antes de cablear el LLM (checkpoint del plan) | Sesión principal (requiere contexto completo y presenta al usuario) |

El criterio (9) del plan ampliado — repetibilidad del juez — es de Fase 3 y queda fuera.

---

## Mecánica de ejecución

- **Una llamada batch de agentes por ola** (todas las tareas de la ola en un solo mensaje → corren concurrentes). Prompt por tarea: leer `types.py` + `CONVENTIONS.md` + su fila de este plan; archivos que posee; correr sus tests; reportar pass/fail + resumen.
- **Aislamiento:** no hace falta worktree (propiedad de archivos disjunta); opcional si se prefiere.
- **Gates:** entre olas, la sesión principal corre pytest completo y verifica el contrato — es el único punto de sincronización.
- **Recuento:** 16 tareas, 5 olas. Camino crítico ≈ W0 + max(W1) + max(W2) + max(W3) + W4; las olas 1 y 3 dominan y van en paralelo puro.
