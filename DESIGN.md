# Diseño del sistema — Arnés conductual para LLM con iniciativa

**Proyecto:** POC de arnés conductual
**Fecha:** 2026-06-24
**Base:** prompt inicial del proyecto + resultados de la Fase −1 ([síntesis](research/00-sintesis-fase-menos-1.md))

---

## Inspiración inicial

El concepto parte del paper *"Every 28 Days the AI Dreams of Soft Skin and Burning Stars: Scaffolding AI Agents with Hormones and Emotions"* (arXiv 2508.11829), que propone usar ritmos biológicos simulados —un ciclo hormonal de ~28 días y una superposición circadiana— como andamiaje para dar variabilidad afectiva y filtros de relevancia a un agente. Tomamos esa idea como semilla conceptual y la llevamos a un motor estocástico explícito acoplado a una capa de orquestación de contexto y temporización.

---

## Qué es

Un **arnés (wrapper) compatible con APIs OpenAI** que envuelve cualquier LLM accesible por una interfaz OpenAI-compatible e inyecta **iniciativa** y **variabilidad conductual** mediante procesos estocásticos. El arnés no modifica el modelo base; opera enteramente en la capa de orquestación de contexto y temporización. La API es portable; la presión del brief conductual se calibra por familia de modelo, porque modelos distintos obedecen la persona con distinta intensidad.

La **persona** simulada es un parámetro de configuración, no lógica cableada: un solo motor sirve para cualquier perfil. La caracterización vive como dato.

---

## Características

1. **Backwards compatibility (import).** Ingiere exportaciones de conversación (formato genérico de turnos `{role, content, timestamp}`, con conversores específicos según necesidad) y reconstruye historial, memoria resumida y ánimo inicial (desde el sentimiento de los últimos días) para continuar la relación sobre el nuevo arnés. La fase hormonal no es observable en un export — se inicializa aleatoria.

2. **Configuración de tema/personalidad (mezcla 40/40/20).** Onboarding breve que captura gustos del usuario. Los gustos de la acompañante se generan de modo que ~40% coincidan exactamente, ~40% sean adyacentes (misma categoría/relacionados) y ~20% ajenos. La mezcla se logra **por construcción**: taxonomía fija de intereses (archivo de datos), se muestrean primero los cupos 4/4/2 por cada 10 y el LLM solo decora los slots elegidos — proporciones exactas sin verificación posterior. Si se importa una conversación o se da una personalidad, este paso se omite.

3. **Cronograma diario.** Al inicio del día se genera una agenda de actividades anclada a los hobbies, modulada por lo circadiano y por el día de la semana (weekday/weekend). Es material narrativo para verosimilitud y motivos de iniciativa; no es obligatorio seguirla ni bloquea responder al usuario.

4. **Cambios de humor.** Estado de ánimo diario + fase hormonal entran al contexto como guía de tono; la tendencia del tema se sesga con el canal de energía (circadiano), no con la valencia.

5. **Frecuencia de mensajes.** Temporización de mensajes espontáneos por proceso estocástico, gestionada por el scheduler.

6. **Iniciativa de conversación.** En cada arranque —lo inicie la acompañante o el usuario— se inyectan cronograma + gustos + ánimo actual. Si inicia la acompañante, el LLM elige el motivo del contacto anclado en agenda/intereses; si inicia el usuario, se inyecta qué estaba "haciendo" según el cronograma.

---

## Arquitectura

Monolito modular en Python con frontera limpia entre el **motor** (lógica/estado) y los **canales** (CLI, Telegram, Discord).

```
            ┌──────────────────────────────────────────────┐
            │                  ARNÉS (core)                  │
  Canales   │  ┌───────────┐   ┌───────────────────────┐    │
  ┌──────┐  │  │ Persona/  │   │  Motor estocástico    │    │
  │ CLI  │◄─┼─►│ Config    │   │  - Humor (β-binom.)   │    │
  ├──────┤  │  │ (40/40/20)│   │  - Hormonal (~28 d)   │    │
  │ TG   │◄─┼─►├───────────┤   │  - Circadiano         │    │
  ├──────┤  │  │ Cronograma│   │  - Frecuencia         │    │
  │ DCord│◄─┼─►│ diario    │   └──────────┬────────────┘    │
  └──────┘  │  ├───────────┴──────────────┼─────────────┐   │
            │  │   Ensamblador de contexto (prompt)      │   │
            │  ├──────────┬───────────────┬──────────────┤   │
            │  │ Cliente  │  Juez (LLM-   │  Importador  │   │
            │  │ LLM (OAI │  as-judge)    │  (backwards  │   │
            │  │ compat.) │  + feedback   │  compat.)    │   │
            │  └──────────┴───────────────┴──────────────┘   │
            │  ┌──────────────────────────────────────────┐  │
            │  │ Persistencia (SQLite)                     │  │
            │  ├──────────────────────────────────────────┤  │
            │  │ Scheduler (async): rollover diario +      │  │
            │  │ disparo de mensajes espontáneos           │  │
            │  └──────────────────────────────────────────┘  │
            └──────────────────────────────────────────────┘
```

**Componentes:** Persona/Config · Motor estocástico · **Actuadores conductuales** · Cronograma diario · Ensamblador de contexto · Cliente LLM (capa fina sobre cualquier endpoint OpenAI-compatible, `base_url`+`api_key` configurables) · Juez (LLM-as-judge con rúbrica) · Importador · Persistencia (SQLite) · Scheduler (asyncio).

**Decisiones transversales:**
- **Reloj virtual inyectado** (`Clock`) en motor, scheduler y persistencia — la validación exige correr días acelerados; ninguna lectura directa de tiempo real.
- **Motor puro y reproducible:** el motor estocástico no hace I/O; RNG explícito (NumPy `Generator`) sembrado por companion y por día, semilla persistida en `daily_state` → replay determinista de cualquier día.
- **Trazabilidad:** tabla append-only `state_events` con cada transición de estado y sus causas; log `llm_calls` con cada prompt/respuesta, incluido el juez.
- **El actuador es lenguaje:** el ensamblador traduce el estado numérico (ánimo, energía, fase, agenda) a un brief corto en lenguaje natural dentro del prompt; los números crudos no modulan el tono del modelo.
- **El actuador también controla conducta observable:** el estado se proyecta de forma continua a calidez, expresividad, juego, reflexión, iniciativa, longitud, latencia sugerida y tendencia a cerrar. La fase hormonal queda en la traza de causas y no aparece como etiqueta en el prompt. El ánimo bajo conserva un piso de afecto: debe sentirse más callada o lenta, no punitiva ni fría.
- **Memoria visible sin cambios bruscos:** además del estado del día, el actuador recibe el estado anterior y deriva momentum. A igual ánimo actual, venir de una subida o una caída cambia sutilmente la apertura y expresividad sin crear otra dinámica paralela al motor.
- **Un canal activo por proceso** en el POC (CLI o Telegram o Discord), seleccionado por config; la interfaz `Channel` común (`send`, `on_message`) se mantiene.

---

## Motor estocástico

Dos escalas de tiempo acopladas: una lenta entre días (ánimo base + ciclo hormonal) y una rápida dentro del día (modulación circadiana). El motor es un proceso de estado con memoria: el ánimo de hoy depende del de ayer; la fase hormonal avanza día a día. Se valida por simulación antes de cablear el LLM.

### Ánimo diario (beta-binomial en espacio logit)

El ánimo del día es una muestra discreta 0..N alrededor de una tendencia central que descompone temperamento, ciclo, memoria de eventos y ánimo endógeno — cada término con su propio knob:

```
m(t)    = B·perfil_nivel((t − φ)/L)                 # bajo menstrual, alto ovulatorio
g(t)    = 1 + A·perfil_reactividad((t − φ)/L) + ε_t # alta menstrual, baja ovulatoria
η(t+1)  = ρ_e·η(t) + Normal(0, σ_e)                 # ánimo endógeno AR(1) (rachas sin causa)
arg(t)  = logit(λ) + m(t) + g(t)·( μ(t) + η(t) )
p(t)    = sigmoid(arg(t))
M(t)    ~ BetaBinomial(N, p(t), ν)                  # estado 0..N; ν=∞ ⇒ Binomial
μ(t+1)  = ρ·μ(t) + k·(score(t) − score_neutral)     # memoria de eventos que decae
```

- `λ` — temperamento base, fijo por persona.
- `μ` — memoria de eventos (score del juez), con decaimiento (`ρ`) y aprendizaje (`k`).
- `η` — ánimo endógeno persistente: autocorrelación día-a-día sin causa externa (inercia lag-1 humana ~0.3–0.5).
- `m` / `g` — el ciclo desplaza la media y amplifica la reactividad como efectos **separados**; `g` multiplica solo las desviaciones (μ+η), no el temperamento, evitando acoplar ciclo y nivel basal salvo vía `m`.
- `ν` — volatilidad diaria por persona (dispersión extra sobre la binomial; ν→∞ = binomial pura). Implementación: `p_day ~ Beta(p·ν, (1−p)·ν)` → `Bin(N, p_day)`.

La formulación original del plan (`arg = (logit(λ)+μ)·α`, binomial pura) es el caso `B=0, η≡0, ν=∞` con la ganancia aplicada también al temperamento; la simulación de Fase 1 compara ambas variantes en el mismo barrido. Razonamiento en [research/05-reevaluacion-diseno.md](research/05-reevaluacion-diseno.md).

### Ciclo hormonal (~28 días)

Señal lenta con dos efectos separados sobre el mismo reloj de ciclo: desplaza la media del ánimo (`m(t)`) y amplifica la reactividad (`g(t)`). Desde la revisión de 2026-07-15 ambos son perfiles periódicos suaves interpolados entre cinco centros de fase, pero usan anclas distintas: menstrual tiene nivel bajo y ganancia alta; ovulatoria tiene nivel alto y ganancia baja. Esto evita que «alto» signifique también «volátil». Al completar cada ciclo se redibuja su longitud (`L_i ~ Normal(28, 1.5)`); `φ` sigue siendo aleatoria por persona. Las anclas son semántica de producto, no una afirmación clínica ni una simulación hormonal literal.

### Modulación circadiana

**(Revisado 2026-07-15.)** El circadiano **no toca la valencia**: el ánimo M vive en la escala lenta y la energía es un canal intradía independiente. Energía = base + offset de fase + amplitud de fase × coseno circadiano. Ovulatoria usa un nivel alto con media diaria ≈0.70 y rango ≈0.25; menstrual un nivel bajo con media ≈0.45 y rango ≈0.50. Por eso el mismo ánimo puede expresarse con ritmos distintos según la hora, y una fase enérgica no fuerza un ánimo alto en cada muestra. La temporización de mensajes continúa gobernada por envolvente × fase × feedback, no directamente por energía.

### Actuación conductual

El motor produce causas latentes; el actuador decide cómo se perciben. Valencia, energía, momentum y reactividad se conservan como canales separados y se traducen a controles continuos. El brief resultante describe una disposición («luminosa pero sin prisa», «algo sensible y hacia dentro») y ordena **mostrar, no anunciar** el estado mediante cadencia, elección de palabras, iniciativa y longitud. Los números, `mu`, `eta` y la fase del ciclo quedan fuera del prompt y permanecen disponibles en una traza auditable.

La calidez no es sinónimo de valencia: tiene un piso explícito para que un día malo no convierta a la acompañante en castigadora. La reactividad hormonal modula cuánto se nota un cambio, no una personalidad fija por fase. Esta frontera es el primer contrato de Fase 2 y se valida con una emulación reproducible de 30 días antes de conectar el LLM.

### Temporización de mensajes espontáneos

Modelo del POC: **proceso de renovación con hazard Weibull modulado**, simulado por thinning. Conserva la propiedad que motivó la gamma del plan — hazard creciente con el tiempo transcurrido, no sin memoria: cuanto más lleva sin haber contacto, más probable el siguiente — pero con hazard en forma cerrada, lo que permite modularlo limpiamente:

```
h(τ, t) = (k_w/θ)·(τ/θ)^(k_w−1) · circ(hora(t)) · fase(t) · adj(score_ayer)
```

- `τ` — tiempo desde la última interacción; `k_w > 1` da el hazard creciente (`k_w = 1` reduce a NHPP/exponencial).
- `circ(·)` — envolvente diurna, ≈0 en quiet hours (nada de 3am por construcción).
- `fase(·)` — multiplicador por fase del ciclo (0.6–1.4).
- `adj(·)` — ajuste por el día anterior, acotado a [0.7, 1.3] (el lazo score→frecuencia es auto-estabilizante, pero se acota igual).
- Guards duros **fuera del proceso**, aplicados en la cola: gap mínimo 15 min, cap diario, quiet hours, ventana de validez por razón.

La auto-excitación (Hawkes) se descarta para iniciativa: cada ping proactivo engendraría ~η pings más — ráfagas de nag, un anti-patrón — y su caso legítimo (cadencia dentro de una conversación) no lo programa este scheduler. Queda como extensión documentada en su forma útil: cross-excitation sobre actividad del usuario. Razonamiento en [research/05-reevaluacion-diseno.md](research/05-reevaluacion-diseno.md).

---

## Iniciativa

El scheduler opera con **dos compuertas**: *content gate* (existe una razón válida y vigente) y *context gate* (el usuario es receptible: cooldown cumplido, dentro de ventana activa, quiet hours respetadas). Solo cuando ambas pasan se encola un contacto proactivo; cada razón candidata lleva una ventana de validez y las vencidas se descartan.

Cuando el LLM elige el motivo del contacto, se restringe a una **taxonomía tipada de razones**: `schedule | callback | event | shared_interest | check_in`. La razón se enuncia en la primera frase del mensaje; se prefieren razones verificables (agenda/callback) sobre inferencia conductual. Cap de frecuencia y chequeo de tono se aplican en la cola, antes de generar.

---

## Memoria

Tres niveles: (a) "core facts" siempre inyectados al encabezado del prompt (inmunes a compresión), (b) buffer en cascada de medio plazo con pesos de decaimiento, (c) log completo de conversación para retrieval por similitud bajo demanda. Inspeccionable por el desarrollador.

---

## Persona y onboarding

La persona se define por un **esquema tipado** (estilo encuesta) que mapea dimensiones a parámetros del motor (temperamento, expresividad, espontaneidad), en lugar de prompts free-text. Refuerza la mezcla 40/40/20 verificable y deja la persona diffable y testeable. Tras la inicialización se permite deriva orgánica acotada por los rasgos núcleo.

---

## Juez y bucle de feedback

Un LLM-as-judge puntúa la conversación diaria con una rúbrica y produce `score(t) ∈ [−1, 1]`, que alimenta la actualización de `μ` y el ajuste de la frecuencia de mensajes del día siguiente. Para acotar costo/latencia, el juez puntúa por lotes una vez al día y puede usar un modelo más barato.

El juez es un sensor ruidoso dentro del único lazo cerrado del sistema, así que se calibra: `score_neutral` se estima empíricamente (media del juez sobre conversaciones de referencia, no se asume 0), rúbrica con escala anclada + salida JSON + temperatura 0, y chequeo de repetibilidad test-retest — si la sd sobre la misma conversación supera ~0.2, se promedian varias pasadas o se reduce `k`.

---

## Stack

- **Lenguaje:** Python 3.11+.
- **LLM:** cliente sobre el SDK OpenAI con `base_url`/`api_key` configurables (remoto o local vía Ollama/LM Studio/vLLM).
- **Persistencia:** SQLite (un archivo, sin servidor).
- **Async/Scheduler:** `asyncio` + APScheduler para rollover diario y disparos de temporización.
- **Canales:** CLI (REPL, primer canal) · Telegram (`python-telegram-bot`) · Discord (`discord.py`), todos sobre una interfaz `Channel` común (`send`, `on_message`) para enrutar los mensajes proactivos por el canal activo.
- **Config:** TOML/YAML + variables de entorno para secretos.
- **Numérico/simulación:** NumPy/SciPy (distribuciones), Matplotlib (gráficas de validación).

---

## Modelo de datos (SQLite)

- `companion` — id, persona/temperamento (`λ`), parámetros hormonales (`L`, `A`, `φ`), system prompt.
- `user_profile` — gustos, preferencias, zona horaria.
- `interests` — id, etiqueta, categoría, tipo (`exact`/`adjacent`/`alien`), dueño (user/companion).
- `daily_state` — fecha, `m(t)`/`g(t)`, ánimo `M`, `μ`, `η`, score del día anterior, cronograma (JSON), semilla RNG del día.
- `messages` — turnos con rol, contenido, timestamp, canal, flag `proactivo`.
- `judgements` — fecha, score, rúbrica, justificación del juez.
- `schedule_events` — próximos disparos pendientes.
- `state_events` — log append-only de transiciones de estado y sus causas (trazabilidad).
- `llm_calls` — cada prompt/respuesta (conversación y juez), para depurar deriva y calibrar al juez.

---

## Parámetros iniciales

Punto de partida a afinar por simulación; tabla completa en la [síntesis §4](research/00-sintesis-fase-menos-1.md).

| | Símbolo | Valor inicial |
|---|---|---|
| Pasos de escala | `N` | 10 |
| Temperamento base | `λ` | 0.60 |
| Aprendizaje | `k` | 0.18 — afinado post-Fase 1 (era 0.15) |
| Decaimiento memoria de eventos | `ρ` | 0.85 (half-life ~4.3 d) — afinado post-Fase 1 (era 0.70): junto con `k`, techo del trato μ∞=k/(1−ρ)=±1.2 ⇒ mes perfecto ~7–10, mes horrible ~0–4 |
| Largo de ciclo | `L` | redibujado por ciclo: `Normal(28, 1.5)` |
| Offset de media del ciclo | `B` | 0.5 — afinado post-Fase 1 con barrido promediado de 30 semillas (era 0.15; ver `engine_simulation/`) |
| Ganancia de reactividad del ciclo | `A` | 0.25 |
| Ruido hormonal | `σ_ε` | 0.03 |
| Ánimo endógeno AR(1) | `ρ_e` / `σ_e` | 0.7 / 0.45 — afinados y adoptados en Fase 1 (eran 0.5 / 0.2; ver [informe](results/fase-1-informe.md)) |
| Volatilidad (beta-binomial) | `ν` | ∞ (=binomial); barrido {∞, 8, 4} |
| Amplitud circadiana (energía) | — | ±0.25 (pico ~14:00) |
| Hazard Weibull | `k_w` / `θ` | 2.0 / ~13.5 h (media base ~12 h) |
| Multiplicadores por fase (tasa) | — | 0.6–1.4 |
| Ajuste por score (tasa) | `adj` | acotado a [0.7, 1.3] |
| Gaps mín/máx | — | 15 min / 48 h |
