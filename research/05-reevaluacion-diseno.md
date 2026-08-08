# Reevaluación de diseño — post Fase −1

**Fecha:** 2026-07-01
**Alcance:** viabilidad · selección de variables aleatorias · modelo de mensajes proactivos · simulación hormonal · arquitectura del arnés.
**Método:** re-derivación analítica sobre los documentos existentes (plan inicial, [DESIGN.md](../DESIGN.md), notas 01–03). Sin investigación nueva.

**Veredicto global:** el diseño se sostiene; no cambia el alcance ni el plan de fases. Cuatro correcciones estructurales (no cosméticas):

| Área | Veredicto | Cambio |
|---|---|---|
| Viabilidad | ✅ Alta, sin cambios | Reloj virtual pasa de implícito a requisito |
| Ánimo (variable aleatoria) | ⚠️ Corregir parametrización | Desacoplar media/ganancia del ciclo; beta-binomial; término AR(1) endógeno |
| Mensajes proactivos | ❌ Revertir recomendación de Fase −1 | Hawkes fuera del POC; renewal con hazard Weibull modulado |
| Simulación hormonal | ⚠️ Refinar | Jitter por ciclo; opción spline de anclas; el actuador es lenguaje |
| Arquitectura | ✅ Correcta, endurecer | Motor puro sembrado, trazabilidad append-only, un canal por proceso |

---

## 1. Viabilidad

Sin cambios de fondo: monolito Python + SQLite + asyncio + cliente OpenAI-compatible es todo tecnología aburrida (bien). Los dos riesgos técnicos reales:

1. **Régimen de parámetros del motor** — ya mitigado por la Fase 1 (simulación antes de cablear el LLM). La reevaluación amplía el barrido (ver §6).
2. **El juez como sensor ruidoso dentro de un lazo de realimentación** — nuevo énfasis, ver §2.3. Es el único lazo cerrado del sistema y nadie calibra el sensor en el plan original.

Requisito que asciende de "nice to have" a bloqueante: **reloj virtual**. El criterio de éxito global exige "una sesión de varios días (acelerada)" — eso es imposible de retrofitear si el motor, el scheduler y la persistencia leen tiempo real directamente. Se inyecta un `Clock` desde el día 0 (§5).

Esfuerzo estimado sin cambios: Fases 0–1 en días; POC completo en semanas a tiempo parcial.

---

## 2. Variables aleatorias del ánimo

### 2.1 Qué hace realmente la formulación original

Con neutro en valencia 0.5, `neutro_logit = logit(0.5) = 0`, y la fórmula del plan colapsa a:

```
arg = (logit(λ) + μ)·α
```

Tres observaciones:

**(a) α acopla ganancia y media.** El plan pide que α "amplifique swings y no sesgue". Eso solo es cierto en λ=0.5. Con λ=0.60 (`logit ≈ 0.405`), el ciclo hormonal solo (μ=0, A=0.25) mueve la media:

- fase alta: `arg = 0.405·1.25 ≈ 0.507 → p ≈ 0.624`
- fase baja: `arg = 0.405·0.75 ≈ 0.304 → p ≈ 0.575`

Es decir, α **también** desplaza el ánimo medio (~±0.25 pasos en N=10), sincronizado con el ciclo. Puede ser deseable (la fase afecta el nivel medio — plausible biológicamente), pero está **entrelazado** con la amplificación de reactividad y no se puede tunear por separado. Un solo knob, dos efectos.

**(b) La varianza binomial no es tuneable.** `Var[M] = N·p(1−p)` queda fija dada p (sd ≈ 1.5 pasos en p≈0.6). Dos personas con el mismo temperamento pero distinta volatilidad diaria no se pueden expresar: el único knob de varianza (N) es también el knob de resolución de la escala.

**(c) No hay autocorrelación endógena.** Dado p(t), los M(t) son ruido blanco alrededor de una media lenta; la única persistencia día-a-día viene de μ (eventos → juez). Los humanos muestran inercia de ánimo lag-1 ≈ 0.3–0.5 **sin causa externa** (literatura de inercia emocional). "Amanecí de malas otra vez, sin motivo" — el modelo original no puede producir rachas así, y eso es núcleo del objetivo "no robótico".

**(d) La dinámica de μ está bien.** Equilibrio bajo score constante s: `μ∞ = k·s/(1−ρ) = 0.5·s` (magnitud razonable: una racha perfecta mueve p de 0.60 a ~0.71). Half-life de un shock: `ln 0.5/ln 0.7 ≈ 1.9 días`; extinción total en ~una semana. Humano y tuneable.

### 2.2 Formulación revisada (generaliza, no reemplaza)

```
m(t)    = B·sin(2π·(t − φ)/L)                       # ciclo: desplazamiento de MEDIA
g(t)    = 1 + A·sin(2π·(t − φ)/L) + ε_t             # ciclo: GANANCIA de reactividad
η(t+1)  = ρ_e·η(t) + Normal(0, σ_e)                 # ánimo endógeno AR(1)
arg(t)  = logit(λ) + m(t) + g(t)·( μ(t) + η(t) )
p(t)    = sigmoid(arg(t))
M(t)    ~ BetaBinomial(N, p(t), ν)                  # ν=∞ ⇒ Binomial pura
μ(t+1)  = ρ·μ(t) + k·(score(t) − score_neutral)
```

Cada término hace **una** cosa:

| Término | Semántica | Knob |
|---|---|---|
| `logit(λ)` | temperamento estable | λ |
| `m(t)` | el ciclo desplaza el nivel medio | B |
| `g(t)` | el ciclo amplifica la reactividad — multiplica solo desviaciones (μ+η), no el temperamento | A |
| `η(t)` | rachas de ánimo sin causa externa | ρ_e, σ_e |
| beta-binomial | volatilidad diaria por persona | ν |
| `μ(t)` | memoria de eventos (juez) | k, ρ |

Chequeos de escala: sd estacionaria de η = `σ_e/√(1−ρ_e²) ≈ 0.23` con (0.5, 0.2) → ±0.5–1 paso típico, sutil pero persistente ✓. Beta-binomial con ν=4: multiplicador de varianza `1+(N−1)/(ν+1) = 2.8` → sd ×1.67 ✓ (implementación: `p_day ~ Beta(p·ν, (1−p)·ν)` y luego `Bin(N, p_day)`).

**La formulación original es un caso particular:** `B=0, η≡0, ν=∞`, con la ganancia aplicada también al temperamento. La Fase 1 corre el barrido comparando tres variantes — (i) original, (ii) desacoplada sin offsets (B=0), (iii) desacoplada completa — y elige por las gráficas, como el plan siempre quiso. Nada se decide por autoridad; se decide por simulación.

Intradía sin cambios: `arg_h = arg(t) + c(h)`, sin re-muestrear la binomial.

### 2.3 El juez como sensor ruidoso en el lazo

`μ ← score` es el único lazo cerrado del sistema, y los jueces LLM tienen sesgo (verbosidad, sicofancia hacia tono agradable) y varianza. Un juez que puntúa sistemáticamente +0.3 fija `μ∞ = +0.15` permanente: la acompañante deriva a "contenta" haga lo que haga el usuario. Mitigaciones concretas:

- **Calibrar `score_neutral` empíricamente**: media del juez sobre un set de conversaciones de referencia, no asumir 0.
- Rúbrica con escala anclada + salida JSON forzada + temperatura 0.
- **Test-retest en Fase 3**: puntuar la misma conversación N veces; si sd > ~0.2, promediar pasadas o bajar `k`.
- Winsorizar scores extremos.

### 2.4 Binomial vs PAD — decisión confirmada

Se mantiene el escalar discreto 0..N para el POC. Razones afiladas: (i) un escalar interpretable es trivialmente inyectable en prompt y auditable; (ii) el feedback del juez es un escalar — actualizar 3 dimensiones PAD desde un score de 1 dimensión es un problema de atribución que no queremos; (iii) el clamp de PAD que recomendaba la literatura queda implícito en la sigmoide (saturación = efectos techo/piso, deseable). Lo único que PAD aportaba de valor real se rescata barato: un **canal de energía** derivado (circadiano + fase) separado de la valencia, porque "cansada pero contenta" y "enérgica pero irritable" son estados humanos distintos que un solo escalar no expresa. Dos campos en el estado, cero dinámica nueva.

---

## 3. Modelo de mensajes proactivos

### 3.1 Hawkes: recomendación de Fase −1 revertida

La auto-excitación de Hawkes sobre los **mensajes propios** significa, literalmente, que cada ping proactivo engendra en expectativa η ≈ 0.44 pings más. Eso es un generador de ráfagas de nag — exactamente el anti-patrón que la nota 03 prohíbe como restricción dura. El caso legítimo de Hawkes (cadencia dentro de una conversación: respuesta-dispara-respuesta) **no lo programa este scheduler** — las respuestas reactivas son inmediatas, no agendadas. Conclusión: Hawkes sale del camino del POC. Queda como extensión documentada en su forma útil: **cross-excitation sobre actividad del usuario** (usuario activo hoy → tasa levemente mayor), que es otra cosa.

### 3.2 Gamma: la intuición del plan era correcta, la mecánica no

El plan eligió Gamma por no-memorylessness, y esa propiedad es exactamente la deseable: **hazard creciente** con el tiempo transcurrido — "cuanto más llevamos sin hablar, más ganas de escribirte". Pero un renewal Gamma homogéneo no integra la envolvente diurna limpiamente (habría que rechazar muestras contra pesos circadianos, un kludge), y el hazard de la Gamma no tiene forma cerrada (gamma incompleta), lo que complica la modulación.

### 3.3 Modelo del POC: renewal con hazard Weibull modulado

La Weibull con k_w>1 tiene la misma propiedad de envejecimiento con hazard en forma cerrada:

```
h(τ, t) = (k_w/θ)·(τ/θ)^(k_w−1) · circ(hora(t)) · fase(t) · adj(score_ayer)
```

- `τ` = tiempo desde la última interacción; `k_w > 1` ⇒ hazard creciente. **k_w = 1 reduce a exponencial/NHPP**, así que el modelo contiene al NHPP como caso particular.
- `circ(·)` — envolvente diurna, ≈0 en quiet hours (nada de 3am por construcción, no por parche).
- `fase(·)` — multiplicadores por fase del ciclo (0.6–1.4, de la nota 02).
- `adj(·)` — ajuste por el día anterior, **acotado a [0.7, 1.3]**: el lazo score→frecuencia es auto-estabilizante (buen día → más pings → si molestan → peor score → menos pings), pero se acota igual para impedir deriva.
- Simulación por **thinning** contra una cota superior del hazard. ~30 líneas de NumPy.
- **Guards duros fuera del proceso** (en la cola, no en la generación): gap mínimo 15 min, cap diario, quiet hours, ventana de validez por razón.

Punto de partida: `k_w = 2.0`, `θ ≈ 13.5 h` (media Weibull = θ·Γ(1.5) ≈ 12 h base; con la envolvente diurna matando la masa nocturna queda ~1–2 contactos/día, dentro del objetivo). La Fase 1 valida el histograma horario y la media.

---

## 4. Simulación hormonal

1. **Separación media/ganancia** — es el cambio de §2.2: `m(t)` y `g(t)` sobre el mismo reloj de ciclo, tuneables por separado.
2. **La curva real no es senoidal.** Las anclas por fase de la nota 02 (menstrual −0.3 · folicular +0.1 · ovulatoria +0.4 · luteal-temprana +0.1 · luteal-tardía −0.2) son asimétricas: pico ovulatorio estrecho, declive luteal gradual. La senoide es la aproximación de arranque correcta; si la simulación muestra que la asimetría importa, se sustituye por una **spline de 5 anclas** (o dos armónicos) — cambio local, misma interfaz.
3. **Jitter por ciclo, no por persona.** La irregularidad biológica realista está **entre** ciclos: al completar un ciclo se redibuja `L_i ~ Normal(28, 1.5)` (contador `cycle_day` que se resetea). Más realista que un L fijo con jitter único, e igual de trivial.
4. **El actuador es lenguaje.** El hallazgo replicable del paper de inspiración es que las emociones emergen de **descripciones en lenguaje natural** del estado, no de números en el prompt. El ensamblador traduce el estado numérico (M, energía, fase, agenda) a un brief corto en lenguaje natural; los números crudos no modulan el tono del modelo. Esto es una pieza de diseño de primera clase, no un detalle de prompt.
5. **Ritmo semanal.** Weekday/weekend entra al generador del cronograma. Barato y aumenta mucho la verosimilitud (nadie "va a la oficina" un domingo).
6. El estado expone `cycle_day` y `cycle_phase` (etiqueta) para el ensamblador y los multiplicadores de tasa.

---

## 5. Arquitectura

La frontera motor/canales y el monolito modular se confirman. Endurecimientos:

1. **Reloj virtual inyectado** (`Clock`) en motor, scheduler y persistencia. Ninguna lectura directa de tiempo real. Requisito del criterio de éxito (días acelerados), no optimización.
2. **Motor puro y reproducible:** el motor estocástico es un módulo sin I/O con RNG explícito (NumPy `Generator`), sembrado por companion y por día; la semilla se persiste en `daily_state`. Permite replay determinista de cualquier día ("¿por qué estaba gruñona el martes?") y hace la Fase 1 y los tests unitarios triviales.
3. **Trazabilidad append-only:** tabla `state_events` con cada transición de estado y sus causas (el criterio de éxito (a) exige "variación de ánimo trazable a las variables" — eso es un log de eventos, no un UPDATE sobre `daily_state`). Y tabla `llm_calls` con cada prompt/respuesta, incluido el juez — imprescindible para depurar deriva de persona y para juzgar al juez.
4. **Un canal activo por proceso** en el POC. `python-telegram-bot` y `discord.py` son asyncio-nativos y APScheduler puede compartir loop, pero no hay razón para pagar esa complejidad ahora; la interfaz `Channel` común ya deja la puerta abierta.
5. **Import: la fase hormonal no es reconstruible.** φ no es observable en un export de conversación; pretender inferirla es falsa precisión. Se inicializa aleatoria; lo que sí se reconstruye: historial, memoria resumida, y ánimo inicial desde el sentimiento de los últimos días.
6. **40/40/20 por construcción, no por verificación.** Taxonomía fija de intereses como archivo de datos; se muestrean los **cupos** primero (4/4/2 por cada 10) y el LLM solo decora los slots elegidos. Las proporciones quedan exactas por construcción — desaparece el riesgo "generación LLM se desvía → re-muestrear" del plan (§9).

---

## 6. Validación de Fase 1 — criterios ampliados

A los 5 criterios del plan se añaden:

6. **Autocorrelación lag-1 de M** en rango objetivo 0.2–0.5 (valida η; el modelo original da ~0 salvo vía scores).
7. **Histograma horario de disparos proactivos** dentro de la envolvente circadiana; cero eventos en quiet hours; media diaria en [1, 3]; hazard creciente visible (distribución de gaps con moda > 0).
8. Barrido comparando las tres variantes del modelo de ánimo (§2.2) además de A, k, ρ; se añaden B, ρ_e, σ_e, ν al barrido.
9. (Fase 3) **Repetibilidad del juez**: sd test-retest sobre la misma conversación < 0.2, y `score_neutral` calibrado empíricamente.

---

## 7. Cambios aplicados

- [DESIGN.md](../DESIGN.md): fórmula del ánimo (§2.2), modelo de temporización (§3.3), ciclo con jitter por ciclo y m/g (§4), decisiones transversales de arquitectura (§5), modelo de datos (`state_events`, `llm_calls`, semilla RNG, η), tabla de parámetros.
- [00-sintesis-fase-menos-1.md](00-sintesis-fase-menos-1.md): nota de supersesión en la recomendación NHPP+Hawkes.
- Los valores siguen siendo priors a validar en Fase 1; esta reevaluación cambia **estructura** (descomposición y knobs ortogonales), no certeza sobre los números.
