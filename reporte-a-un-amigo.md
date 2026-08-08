# Qué estoy montando (te lo cuento)

Estoy con una prueba de concepto medio rara y me apetecía contártela, a ver qué te parece.

## La idea en una frase

Un **arnés** que envuelve a cualquier LLM (de los que se hablan por API estilo OpenAI, sea remoto o local) y le mete **iniciativa** y **variabilidad de comportamiento**. O sea: en vez de un bot que solo responde cuando le escribes, uno que tiene "estados" propios —humor, energía según la hora, un ciclo lento de varias semanas— y que a veces te escribe él, por su cuenta, con un motivo.

Lo importante es que el arnés **no toca el modelo**. Todo pasa en la capa de orquestación: cómo se arma el contexto y cuándo se dispara un mensaje. El modelo de abajo es intercambiable.

Otra decisión que me gusta: la **persona** (el carácter, los gustos, los hobbies) es **configuración, no código**. Un solo motor sirve para cualquier perfil; el carácter es un dato que le pasas.

## Cómo funciona por dentro

Hay un **motor estocástico** que es el corazón. Maneja cuatro cosas con procesos aleatorios:

- **Humor del día** — un valor acotado que varía día a día, con algo de memoria: un buen día empuja el ánimo hacia arriba, uno malo hacia abajo, y sin estímulos vuelve poco a poco a su punto base.
- **Ciclo "hormonal" simulado** (~28 días) — una onda lenta que amplifica o calma los swings de humor. No es nada biológico de verdad; es una señal periódica que hace que en ciertas épocas las cosas se sientan más intensas.
- **Ritmo circadiano** — más social/enérgico de día, más reflexivo de noche. Sesga el tono y las ganas de iniciar conversación.
- **Frecuencia de mensajes espontáneos** — cada cuánto te escribe sin que le hables, modulado por la hora (nada de mensajes a las 3am) y por cómo fue el último día.

Encima de eso van seis features:

1. **Importar conversaciones viejas** para "continuar" una relación que venía de otro sitio.
2. **Configurar persona** mezclando gustos: ~40% coinciden contigo, ~40% son parecidos, ~20% ajenos (para que no sea un espejo).
3. **Cronograma diario** — cada día se inventa una agenda de actividades, atada a sus hobbies. No la sigue al pie de la letra; es material para dar verosimilitud y excusas para escribirte.
4. **Cambios de humor** — lo del motor de arriba, inyectado al contexto como guía de tono.
5. **Frecuencia de mensajes** — gestionada por un planificador.
6. **Iniciativa** — cuando arranca una charla (la inicie quien la inicie) se le inyecta qué "estaba haciendo" y cómo anda de ánimo; si la inicia ella, elige *por qué* te contacta.

La idea es validar primero el motor matemático solo, con simulaciones de un par de meses, antes de enchufar el LLM. Y hay un "juez" (otro LLM) que puntúa cada día de conversación y esa nota retroalimenta el humor del día siguiente.

Esto es un PoC local, para mí, nada distribuido ni público. CLI primero; luego, si tira, adaptadores para Telegram y Discord.

## Qué encontré investigando antes de programar

Me puse a mapear lo que ya existe para no reinventar y para fijar parámetros con algo de criterio. Resumen:

**Los productos del mercado** (Replika, Character.AI, Chai, Kindroid, Nomi, Paradot). Lo interesante:
- Solo tres hacen mensajes proactivos de verdad (Kindroid, Nomi, Paradot), y **ninguno te deja ver el estado interno** que los dispara. Es una caja negra. Justo ahí está lo que quiero hacer distinto: que el estado (humor, fase del ciclo, hora) sea **inspeccionable**.
- Para la memoria, lo más fino es lo de Kindroid (memoria en cascada que se va "olvidando" como la humana) y el truco de Character.AI de "fijar" recuerdos para que no se borren al comprimir el contexto.
- Para configurar el carácter, las encuestas estructuradas (Paradot pregunta 23 cosas al inicio) dan personas más predecibles que los prompts libres. Eso encaja con lo de "persona como config".

**El paper que me inspiró** ("Every 28 Days the AI Dreams of Soft Skin and Burning Stars", arXiv 2508.11829). Resulta que es más un experimento narrativo que un marco matemático: simula varias hormonas con ondas + ruido, mide emociones que *emergen* del texto, y los efectos en las tareas no son estadísticamente fuertes. O sea, la inspiración es buena (rítmos biológicos como filtro de relevancia), pero la matemática del motor la pongo yo. Para las ecuaciones de verdad tiré de literatura de agentes afectivos (modelo PAD, humor lento vs. emoción rápida) y de procesos de punto para la temporización (Poisson no homogéneo + Hawkes para que los mensajes tengan ritmo y ráfagas humanas, en vez de un timer robótico).

**Sobre la iniciativa** (que no resulte pesada): la clave es que cada mensaje espontáneo lleve un **motivo concreto** ("oye, lo que comentaste el martes…") en vez de un "hola, ¿cómo estás?" vacío, y que solo dispare si además es buen momento (no en mitad de algo, respetando horas tranquilas). Hay anti-patrones bien documentados que quiero evitar: culpar ("te extraño"), insistir, notificaciones huecas, optimizar para engagement. Eso lo pienso meter como reglas duras, no como sugerencias de estilo.

Y ya. Eso es lo que tengo en la cabeza. Si te pica la curiosidad te enseño las simulaciones cuando las tenga.
