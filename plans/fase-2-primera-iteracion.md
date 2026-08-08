# Fase 2 — primera iteración vertical del arnés

**Fecha:** 2026-07-15  
**Objetivo:** convertir el motor validado de Fase 1 en comportamiento observable,
trazable y sutil antes de conectar un LLM real.

## Invariantes del producto

1. El motor complejo de regulación emocional se conserva. `m`, `g`, `mu`,
   `eta`, el estado anterior y la energía circadiana siguen siendo causas
   distintas y auditables.
2. Valencia y energía son canales ortogonales. Deben existir estados como
   «contenta pero cansada» e «irritable pero activa».
3. La fase hormonal es causa latente, no una etiqueta de actuación. Se registra
   en la traza, pero el prompt recibe fenomenología y nunca estereotipos de fase.
4. El ánimo bajo no apaga el afecto. Reduce juego, velocidad o iniciativa antes
   de convertir a la acompañante en fría o castigadora.
5. El estado se muestra mediante cadencia, longitud, iniciativa, expresividad y
   cierre; no mediante declaraciones como «hoy mi ánimo es bajo».

## Revisión del diseño anterior

- **Se mantiene:** motor `DECOUPLED_OFFSETS`, memoria mensual `mu`, rachas
  endógenas `eta`, ciclo `m/g`, energía circadiana, RNG reproducible y traza.
- **Se corrige:** «model-agnostic» pasa a significar API compatible; la presión
  del brief deberá tener perfiles por familia de modelo cuando se conecte el LLM.
- **Se concreta:** el afecto rápido queda en la conversación del modelo; el arnés
  conserva estado lento y momentum entre días/sesiones.
- **Se adelanta:** la capa actuadora (antes implícita en el ensamblador) se vuelve
  un contrato probado antes del cliente LLM, porque es el puente entre varianza
  matemática y humanidad percibida.
- **Se difiere:** persona completa, vida ficticia persistente, cronograma, SQLite,
  usuario sintético, juez, scheduler real, Telegram e importación de voz.

## Ola P2.1 — actuadores conductuales (implementada)

Archivos propios:

- `harness/behavior.py`
- `tests/test_behavior.py`

Entrada: `DayRecord`, `TimingParams`, hora local y `DayRecord` anterior opcional.

Salida: `BehaviorDirective` con canales continuos:

- valencia, energía, momentum y reactividad;
- calidez, expresividad, juego y reflexión;
- iniciativa, escala de longitud, latencia sugerida y tendencia a cerrar;
- brief corto para prompt;
- traza separada con fase, ganancia hormonal, `mu`, `eta` y delta de ánimo.

Criterios de aceptación:

- ánimo bajo conserva un piso de calidez;
- energía no se confunde con valencia;
- el estado anterior cambia momentum sin reescribir el ánimo actual;
- `g` cambia reactividad de forma sutil sin sesgar la calidez base;
- el brief no contiene números ni nombres hormonales;
- toda salida es determinista para la misma entrada.

## Ola P2.2 — emulación de treinta días (implementada)

Archivos propios:

- `experiments/behavior_showcase.py`
- `tests/test_behavior_showcase.py`
- `results/behavior-showcase/30-day-behavior.png`
- `results/behavior-showcase/behavior-trace.json`
- `results/behavior-showcase/examples.md`

La emulación compone el motor existente con energía a las 09:00, 14:00 y 20:00,
y deriva los actuadores para una conversación vespertina. El gráfico presenta en
un mismo eje temporal ánimo, energía, textura afectiva y controles observables.
El JSON permite auditar causas; Markdown enseña briefs de días contrastantes.

## Gate de esta iteración

1. Tests nuevos pasan y la suite completa de Fase 1 no regresa.
2. El paquete editable importa `harness` y el experimento corre desde CLI.
3. Los artefactos de treinta días son reproducibles con semilla fija.
4. La visualización muestra variación continua, no saltos de personalidad.
5. Ningún brief revela fase hormonal, números internos ni ordena «actuar triste».

## Siguiente ola propuesta

Conectar esta directiva a un ensamblador de prompt mínimo y a un cliente
OpenAI-compatible inyectable. La prueba decisiva de esa ola será una matriz de
respuestas para los mismos mensajes bajo estados contrastantes, seguida por una
ablación ciega arnés encendido/apagado. Persona, cronograma y vida persistente
entran después de demostrar que el actuador es audible sin volverse caricatura.
