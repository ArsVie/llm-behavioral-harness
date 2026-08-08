# Crítica del objetivo y de la implementación planeada

**Fecha:** 2026-07-01
**Alcance:** objetivo y plan, excluyendo consideraciones de bienestar del usuario. Complementa [05-reevaluacion-diseno.md](05-reevaluacion-diseno.md) (que revisó la matemática); esto revisa la **tesis y la asignación de esfuerzo**.

---

## A nivel de objetivo

### 1. El POC, tal como está especificado, no puede falsificar su propia hipótesis
La hipótesis implícita es: *inyectar estado estocástico produce comportamiento perceptiblemente más humano/atractivo*. Todos los criterios de aceptación validan **plomería** (trazas, gráficas, μ se mueve en la dirección correcta), ninguno valida el **efecto**. Falta el experimento de control: comparación ciega A/B entre arnés-encendido y persona plana (mismo LLM, mismo prompt de persona, sin motor). El paper de inspiración reportó efectos no significativos — hay riesgo real de que toda la maquinaria sea inaudible a través del modelo. Sin ablación, se puede construir todo y no aprender nada. **La ablación ciega debería ser EL criterio de la Fase 7**, no la trazabilidad.

### 2. "Model-agnóstico" es una sobre-afirmación
Agnóstico a la **API**, sí (OpenAI-compatible). Pero la presión de persona necesaria varía por modelo (un modelo pequeño local y un frontier RLHF-ed necesitan briefs de estado muy distintos para producir el mismo tono). En la práctica habrá perfiles de prompt por modelo. Reclamar portabilidad de comportamiento idéntico es falso; conviene acotar la claim.

### 3. El ciclo de ~28 días es inobservable en la ventana del POC
En uso vivo (tiempo real), una evaluación de 1–2 semanas muestrea media fase. Su efecto perceptible durante el POC es ≈0; su peso es fidelidad conceptual, no comportamiento observable. Mantenerlo (es barato — misma senoide), pero: para demos usar `L≈7` o reloj acelerado, y no asignarle peso evaluativo. El circadiano y el ánimo día-a-día son los que cargan la percepción.

---

## A nivel de implementación

### 4. La actuación es el eslabón débil (inversión: mucho generador, poco actuador)
Todo el motor desemboca en unas frases de brief en el prompt, compitiendo contra el prior RLHF de "asistente alegre y servicial". Resultado probable: caricatura — el modelo *anuncia* "hoy estoy algo decaída" (telling) en vez de mostrarlo. El mal humor humano se manifiesta sobre todo en canales **paralingüísticos**: respuestas más cortas, latencia mayor, menos iniciativa, desconectarse antes del tema, cerrar la conversación. El arnés **controla directamente** varios de esos canales y no los usa: el ánimo debería modular latencia de respuesta, presupuesto de longitud, tasa de iniciativa (ya lo hace), disposición a terminar la conversación, hábitos de puntuación/emoji. Son medibles y no pelean contra el modelo. Este es el cambio de mayor palanca de todo el documento.

### 5. ~~Inversión de esfuerzo: memoria vs. motor~~ — RETIRADA (2026-07-01)
Retirada tras aclaración del dueño del proyecto: la **varianza percibida del baseline emocional a escala día/mes es un objetivo declarado del POC**, y lograr cambios de ánimo sutiles con un motor robusto y complejo es deseado — el esfuerzo en el motor no está invertido, es el producto. Queda solo el residuo no polémico: la mecánica de memoria (cadencia de resumen, core facts, presupuesto de contexto) se especifica al llegar a la Fase 2, como el plan ya prevé.

### 6. El lazo juez→μ→tono→juez: estabilidad y semántica
- **Estabilidad (doom loop):** mal día → μ baja → tono más seco → juez puntúa peor → μ más abajo. Linealizando: estable si `ρ + 2·k·g·p(1−p) < 1`. Con defaults: `0.7 + 0.15·2·1·0.24 ≈ 0.77` ✓ estable con margen; se desestabiliza hacia `k ≳ 0.6`. Convertirlo en restricción explícita del barrido de Fase 1 (`k_max ≈ 0.5·(1−ρ)/(g_max·p(1−p))`), y el test de shock del plan es de hecho el test de este lazo.
- **Semántica:** ¿"buen día" para quién? La rúbrica tiene que definir el constructo (calidad de la interacción desde la perspectiva de la acompañante ≠ satisfacción del usuario ≠ coherencia de persona). Sin esa decisión, μ mide una mezcla indefinida.
- **Circularidad:** un LLM juzgando a un LLM comparte sus sesgos. A escala de POC el evaluador humano barato existe: el propio usuario, con comparaciones ciegas (ver §1).

### 7. Afecto rápido: hueco intra-día entre sesiones
Por diseño, el ánimo solo cambia con el rollover (lag de un día). Dentro de una misma conversación el modelo reacciona solo (bien — división de trabajo: **el modelo pone la emoción rápida en-contexto, el arnés el estado lento**; conviene declararlo explícito en DESIGN). El hueco real: dos sesiones el mismo día — pelea por la mañana, conversación nueva por la noche con el mismo `M` muestreado en el rollover → "como si nada". Fix barato: nudge intradía sobre `arg_h` con el sentimiento de la última sesión del día, o aceptarlo y documentarlo.

### 8. Falta el usuario sintético — sin él, las Fases 3/4/7 no son ejecutables rápido
El reloj virtual acelera el tiempo, pero nadie conversa. Para correr 60–90 días end-to-end (juez, μ, iniciativa, cronogramas) hace falta un **usuario sintético** (LLM en rol de usuario, con guiones de días buenos/malos). Es la pieza de test harness que el plan implica ("sesión acelerada") pero nunca especifica. Sin ella, la validación de las fases 3–4 depende de semanas de uso real.

### 9. Vida ficticia sin continuidad ("vida de pez dorado")
El cronograma se genera fresco cada día desde persona+fase, sin memoria de los días anteriores → contradicciones visibles ("fui al dentista" dos veces por semana, proyectos que nunca avanzan). Hace falta un **estado de vida persistente** mínimo (arcos en curso: el curso que está tomando, la amiga del gimnasio) que alimente la generación del cronograma. Bonus: es la mejor fuente de razones `callback`/`event` para la iniciativa.

### 10. Semántica de sesión indefinida
Sin resolver: frontera del "día" vs conversaciones que cruzan medianoche; qué ventana exacta puntúa el juez; qué pasa con proactivos encolados si el canal no puede entregar (CLI cerrada = ¿qué significa un mensaje espontáneo?); modelo de presencia por canal. Las ventanas de validez por razón mitigan lo rancio, pero el concepto de **sesión** debe definirse antes de la Fase 3 porque el juez lo necesita como input.

### 11. Import: los hechos no son la voz
Reconstruir historial/memoria es lo fácil. Lo que el usuario nota al minuto de "continuar la relación" es la **voz** — y el plan no la aborda. El importador debería extraer una tarjeta de estilo (muletillas, longitud típica, uso de emoji, registro) + K extractos como few-shot. Sin eso, la continuidad tras el import fallará el criterio de éxito (c) aunque los datos estén perfectos.

### 12. Recortes de alcance defendibles
- **Discord es mal fit** para compañía íntima 1:1 (semántica de guild, presencia de terceros, proactivos en canal compartido). CLI + Telegram cubren el caso real — y *hermes* ya tiene el wiring de Telegram. Discord: recortar o dejar al final como stretch.
- **Taxonomía de razones vs. fuentes reales:** sin ingestión de contenido externo, `event` y `shared_interest` no tienen de dónde alimentarse salvo del propio cronograma. O se acotan las razones del POC a `schedule | callback | vida-propia (cronograma)`, o quedarán huecas.

---

## Ranking por impacto/esfuerzo (2026-07-01, excluye §5 retirada)

Los de esfuerzo ≈0 no compiten por agenda — son decisiones/ediciones de config que se hacen de pasada; el orden dentro de cada tier es por impacto.

### Tier A — gratis (esfuerzo ≈0, hacer de pasada en la próxima edición de DESIGN)
| # | Ítem | Impacto | Por qué |
|---|---|---|---|
| A1 | Perfil de observación del ciclo: `L≈7` para demo en vivo + aceleración con reloj virtual (§3) | Medio-alto | El componente **mensual** del objetivo es invisible en la ventana del POC sin esto; `L` ya es parámetro |
| A2 | Acotar razones del POC a `schedule \| callback \| vida-propia` (§12b) | Medio | Evita proactivos huecos — el fallo más visible de la iniciativa; `event`/`shared_interest` no tienen fuente sin ingestión |
| A3 | Recortar Discord; CLI + Telegram (§12a) | Medio | Elimina trabajo de Fase 5 y semántica rara de guild; *hermes* ya trae Telegram |
| A4 | Cota de estabilidad del lazo: `ρ + 2k·g·p(1−p) < 1` en validación de config (§6a) | Medio-bajo | Seguro contra doom-loop; defaults ya estables (0.77), es una desigualdad en el validador |
| A5 | Acotar claim "model-agnóstico" → "API-agnóstico, con perfil de prompt por modelo" (§2) | Bajo | Honestidad documental, una frase |

### Tier B — alta palanca (esfuerzo bajo-medio, impacto alto)
| # | Ítem | Impacto | Esfuerzo | Por qué |
|---|---|---|---|---|
| B1 | **Actuadores conductuales del ánimo**: latencia de respuesta, presupuesto de longitud, disposición a cerrar, hábitos de puntuación (§4) | Muy alto | Horas–1 día | Convierte la varianza *generada* en varianza *percibida* — sirve directamente al objetivo declarado; canales que no pelean contra el prior RLHF |
| B2 | **Usuario sintético** (LLM en rol de usuario con guiones de días buenos/malos) (§8) | Alto | 1–2 días | Único camino a observar el ciclo mensual y el lazo del juez en tiempo acelerado; desbloquea B3 y da dientes a A1 |
| B3 | **Ablación ciega arnés-on/off** como criterio de Fase 7 (§1) | Muy alto | Bajo (dado B2) | Mide literalmente "percibida"; convierte el POC en experimento. Sin esto se construye todo y no se aprende nada |
| B4 | **Semántica de sesión**: frontera de día, ventana del juez, entrega por canal (§10) | Medio-alto | Bajo (decisiones + reglas) | Prerequisito de corrección de Fases 3–4, no mejora opcional |
| B5 | **Constructo de la rúbrica del juez** (§6b) | Medio | Bajo | Define qué significa μ; sin ello el lazo mide una mezcla indefinida |

### Tier C — valiosos, diferidos a su fase
| # | Ítem | Impacto | Esfuerzo | Cuándo |
|---|---|---|---|---|
| C1 | **Estado de vida persistente** (arcos en curso → cronograma) (§9) | Alto | Medio | Fase 2 — continuidad narrativa + fuente de callbacks + arco mensual perceptible que complementa el ciclo |
| C2 | **Nudge intradía** desde el sentimiento de la última sesión (§7) | Medio-bajo | Bajo | Fase 2–3 — caso dos-sesiones/día |
| C3 | **Tarjeta de estilo + few-shot en el import** (§11) | Alto para la Fase 6 | Medio | Fase 6 — los hechos no son la voz |

**Cadena de dependencias:** B2 (usuario sintético) habilita B3 (ablación) y hace observable A1 (ciclo mensual acelerado). B1 es independiente y es la mayor palanca absoluta.
