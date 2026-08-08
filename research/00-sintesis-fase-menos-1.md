# Fase −1 — Síntesis de investigación previa (prior-art)

**Proyecto:** Arnés conductual para LLM con iniciativa (POC)
**Fecha:** 2026-06-24
**Fuentes:** notas de investigación [01-products](01-products.md) · [02-research](02-research.md) · [03-initiative](03-initiative.md). Marco regulatorio archivado en [`deferred/04-regulatory.md`](deferred/04-regulatory.md) (fuera de alcance del POC).

Este documento es el **entregable de la Fase −1** del plan: tabla comparativa, decisiones de diseño que adoptamos/descartamos y los rangos de parámetros iniciales para el motor estocástico. Las notas 01–03 contienen el detalle y las citas.

---

## 1. Productos comparables (tabla)

| Producto | Memoria | Mensajes proactivos | Onboarding de persona | Salvaguardas |
|---|---|---|---|---|
| **Replika** | Memory Tab curada + ventana deslizante; recall ~80–85% al mes | Notificaciones de seguimiento; sin algoritmo de timing autónomo (parece scheduler simple) | Quiz de personalidad + rol de relación + backstory free-text; up/down-vote | Clasificador 5 niveles; botón crisis → hotline; gate 18+ (eludible, multado en IT) |
| **Character.AI** | Facts auto + Story Memory *pinned* (protegida de compresión) + ventana | Sin proactividad de origen documentada | User Persona ≤728 chars + "definition" largo | Pop-up crisis → 988; verificación de edad facial/ID; teen-restrictions Nov 2025 |
| **Chai** | Solo ventana ~20–40 msgs; memoria editable manual; resets molestos | Sin proactividad | UI de creación profunda; bots de comunidad | Moderación reactiva; eSafety (Oct 2025) halló fallos en redirección de crisis |
| **Kindroid** | **Memoria en cascada de 5 niveles** con decaimiento + recall por frase-clave (journal) | **"Advanced Proactivity"** (Ultra/MAX): msgs/voz/selfies; quiet hours; consciente de calendario | "Codex": 47 parámetros configurables + backstory | 3 "Red Lines" por escaneo automático; aviso antes de bloqueo |
| **Nomi** | Historia completa server-side; ventana expandida | **Frecuencia configurable** (5 niveles); contenido desde "lo que el AI está pensando/haciendo" | ~3 min: rol + 3–7 rasgos + backstory + intereses (solo icebreakers) | Históricamente débil (incidente suicidio, MIT 2025); update forzado Ene 2026 por ley NY |
| **Paradot** | "Memory-to-Understanding": captura hechos+emociones+opiniones; recall ~90% al mes | Re-engagement contextual documentado; mecanismo no público | **Encuesta de 23 preguntas** + sliders; primeras 72h "críticas" | Permisivo; documentación de crisis limitada; cubierto por ley NY |

### Lecturas clave
- **Solo 3 productos hacen proactividad real** (Kindroid, Nomi, Paradot) y **ninguno expone el estado interno** que la dispara — esa caja negra es justo nuestro diferenciador.
- **Onboarding estructurado > prompt libre.** Encuestas tipadas (Paradot 23-Q, Kindroid Codex) producen personas más predecibles y *testeables*. Casa con nuestra decisión de persona-como-config.
- **El "pin" de Character.AI** es la solución más barata al problema de compresión de contexto: memoria núcleo inmune a desalojo.

---

## 2. Decisiones de diseño (adoptar / descartar)

### Adoptamos
1. **Estado conductual inspeccionable y model-agnóstico** como objeto de primera clase (fase circadiana + ánimo + fase de ciclo). Es el hueco competitivo; ningún producto lo expone.
2. **Dinámica de dos velocidades (PAD)** — *mood* lento (horas/días, lo fija la fase hormonal) + *emoción* rápida (por turno, decae al baseline). Es el consenso de la literatura afectiva (Sentipolis). Encaja con la Sección 3.5 del plan (escala lenta entre días + rápida intradía).
3. **Memoria en tres niveles:** (a) "core facts" siempre inyectados (estilo pin), (b) buffer en cascada con decaimiento (estilo Kindroid), (c) log completo para retrieval. Inspeccionable por el desarrollador.
4. **Onboarding por esquema tipado** (estilo encuesta) que mapea a parámetros, no prompts free-text. Refuerza la mezcla 40/40/20 verificable.
5. **Scheduler de dos compuertas** para iniciativa: *content gate* (¿existe razón válida y vigente?) + *context gate* (¿usuario receptivo? cooldown, quiet hours). Modelo readiness/termination de ProActor.
6. **Taxonomía tipada de razones** para mensajes proactivos: `schedule | callback | event | shared_interest | check_in`. `check_in` es el de menor fundamento → menor frecuencia.
7. **Clamp de PAD a rangos moderados** (±0.6–0.8): arXiv 2604.00005 muestra curvas en U invertida — los extremos degradan la calidad de las respuestas.

### Adoptamos para temporización (refina la Sección 3.4 del plan)
8. **Modelo recomendado: NHPP + Hawkes** (envolvente diurna sinusoidal + auto-excitación con `η<1`). Da ritmo día/noche **y** ráfagas de conversación. La **Gamma del plan se mantiene como variante simple** del POC (k<1 bursty, k>1 metronómico), documentando que no modela la cadena causal "respuesta-dispara-respuesta" que sí da Hawkes. **[Superado en la reevaluación 2026-07-01: la auto-excitación sobre pings propios genera ráfagas de nag; el POC usa renewal con hazard Weibull modulado — ver [05-reevaluacion-diseno.md](05-reevaluacion-diseno.md) §3.]**

### Descartamos / matizamos
- **Atribuir el modelo binomial/gamma al paper** — no procede; son elección de diseño propia. Mantenemos la binomial (varianza acotada y controlable) y la validamos por simulación.
- **Modelo de comunidad abierta (Chai)** sin gate de moderación previo — fuera de alcance del POC y riesgoso.
- **7 hormonas explícitas** (paper) — para el POC basta **1 señal de ciclo** (amplitud) como dice el plan; dejamos la descomposición multi-hormona como extensión.
- **Proactividad por timer simple (Replika)** — insuficiente; usamos estado interno + dos compuertas.

---

## 3. Reglas de iniciativa no intrusiva (checklist operativo)

> **Alcance:** esto es **calidad de producto** (que la iniciativa no resulte molesta), no cumplimiento regulatorio. El marco de bienestar/regulatorio queda **fuera de alcance del POC** (local, mono-usuario, no distribuido ni público) y se archiva en [`deferred/04-regulatory.md`](deferred/04-regulatory.md) por si alguna vez se publica.

De la nota 03 (ProActor, JITAI, "Computers as Bad Social Actors"):

- **Dos compuertas:** razón válida y vigente **Y** usuario receptivo (breakpoint, ventana activa, cooldown).
- **Toda razón con ventana de validez** — las vencidas se descartan, no se difieren.
- **Mínimo de profundidad de relación** antes de iniciar (Meta exige ≥5 mensajes previos del usuario).
- **Razón explícita en la primera frase**; preferir razones verificables (agenda/callback) sobre inferencia conductual ("pareces estresado" → vigilancia).
- **Anti-patrones como restricciones duras** (no estilo): nada de pseudo-notificaciones, culpa ("te extraño"), pasivo-agresividad, "mothering", nagging, engagement-maxxing, triggers opacos. El tono se chequea **antes** de enviar; el cap de frecuencia se aplica en la cola, no en la generación.
- **Empezar conservador** (p.ej. máx. 1 contacto proactivo/día en ventana activa) y aprender de engage/dismiss/ignore.

---

## 4. Rangos de parámetros iniciales para el motor (Fase 1)

Síntesis de la nota 02. Punto de partida a **afinar por simulación** (criterio de aceptación de Fase 1). Nótese que el plan usa una **binomial en espacio logit**; aquí damos también los parámetros PAD/timing equivalentes recomendados.

### 4.1 Ánimo (modelo del plan, espacio logit + binomial)
| Parámetro | Símbolo | Valor inicial | Nota |
|---|---|---|---|
| Pasos de escala | `N` | 10 | estados 0..10 |
| Temperamento base (valencia) | `λ` | 0.60 | levemente positivo |
| Neutro | `score_neutral` | 0.0 | score en [−1,1] |
| Aprendizaje | `k` | 0.15 | peso del día anterior |
| Decaimiento | `ρ` | 0.70 | memoria ~3–4 días (≈ 1/(1−ρ)) |
| Clamp valencia/arousal/dominancia | — | ±0.80 / ±0.70 / ±0.60 | evita extremos (2604.00005) |

### 4.2 Ciclo hormonal (~28 d)
| Parámetro | Símbolo | Valor inicial | Nota |
|---|---|---|---|
| Largo de ciclo | `L` | 28 (±2–3 jitter) | estándar; jitter por instancia |
| Amplitud | `A` | 0.25 | fuerza del swing |
| Fase | `φ` | aleatoria | por persona |
| Ruido | `σ_ε` | 0.03 | pequeño |

Offsets de valencia por fase (de la nota 02, si se quiere granularidad por fase en lugar de una sola senoidal): menstrual −0.3 · folicular +0.1 · ovulatoria +0.4 · luteal-temprana +0.1 · luteal-tardía −0.2. Multiplicadores de **tasa de mensajes** por fase: 0.60 / 1.00 / 1.40 / 1.10 / 0.80.

### 4.3 Circadiano
| Parámetro | Valor inicial | Nota |
|---|---|---|
| Amplitud arousal | ±0.25 | `cos(2π(h−14)/24)`, pico ~14:00 |
| Boost mañana / penal. noche | +0.15 / −0.10 | 6–11h / 23–4h |

### 4.4 Temporización de mensajes espontáneos
| Parámetro | Valor inicial | Nota |
|---|---|---|
| Modelo | `NHPP + Hawkes` (recom.) / `Gamma` (simple POC) | — |
| Tasa base `λ_mean` | 0.08 msg/h (~2/día a solas) | envolvente NHPP |
| Amplitud diurna `A` | 0.65 | pico `t_peak`=14:00 |
| Hawkes `α` / `β` | 0.35 / 0.80 /h | half-life ~52 min |
| Branching `η=α/β` | 0.44 | **estable (<1)**; rango sano 0.3–0.7 |
| Gamma bursty / regular | `k`=0.6 / `k`=3.0 | CV=1.29 / 0.58 |
| Gaps mín/máx | 15 min / 48 h | nunca <15 min; al menos 1 contacto/2 días |

### 4.5 Dinámica de dos velocidades (si se adopta PAD)
| Parámetro | Valor inicial | Nota |
|---|---|---|
| Decaimiento emoción rápida | ~0.30/turno | half-life ~2.3 turnos |
| Decaimiento mood lento | ~0.02/turno | half-life ~35 turnos (~1 día) |
| Peso emoción → mood | 0.25 | — |
| Peso hormonal → mood | 0.10 | — |

---

## 5. Próximo paso

Con esto cerrado, el plan recomienda: **Fase 0 (scaffolding)** y **Fase 1 (motor estocástico aislado + simulación de 60–90 días con gráficas validadas)**, revisando juntos los parámetros antes de cablear el LLM. El esqueleto NumPy de la Sección 3.5 del plan es el punto de arranque de la Fase 1.

Antes de la Fase 1 conviene una decisión de diseño: **¿binomial en logit (plan) o PAD continuo (literatura)?** Ambos son compatibles con la dinámica de dos velocidades; la binomial es más simple y aporta varianza acotada gratis, PAD es más rico y mejor citado. Recomendación: **binomial para el POC**, dejando PAD como extensión documentada.
