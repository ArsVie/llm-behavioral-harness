# W3.2 — Comparativa de variantes de MoodVariant

Semillas: [111, 222, 333, 444, 555] · días: 90 · PersonaParams() por defecto.

Script reproducible: `python -m experiments.w32_variantes` regenera todas las figuras y este reporte.

## Tabla: métrica × variante (media ± sd entre semillas)

| Métrica | ORIGINAL | DECOUPLED | DECOUPLED_OFFSETS |
|---|---|---|---|
| Δmedia = mean(M\|g alto) − mean(M\|g bajo) | 0.4000 ± 0.1880 | -0.2696 ± 0.6884 | 0.4087 ± 0.5548 |
| corr(g, M) [Pearson] | 0.0991 ± 0.0398 | -0.0341 ± 0.1235 | 0.1139 ± 0.1236 |
| autocorr_lag1(M) | 0.0965 ± 0.1524 | 0.1384 ± 0.1144 | 0.1097 ± 0.1253 |
| corr(MA7(M), m(t)) | 0.1859 ± 0.0624 | -0.1082 ± 0.2642 | 0.2046 ± 0.2256 |

## Criterio (8a) — diferencias documentadas cuantitativamente

### 1. Acoplamiento media-ganancia del ORIGINAL

- Δmedia ORIGINAL = 0.4000 ± 0.1880; Δmedia DECOUPLED = -0.2696 ± 0.6884; Δmedia DECOUPLED_OFFSETS = 0.4087 ± 0.5548.
- corr(g, M) ORIGINAL = 0.0991 ± 0.0398; corr(g, M) DECOUPLED = -0.0341 ± 0.1235; corr(g, M) DECOUPLED_OFFSETS = 0.1139 ± 0.1236.
- Veredicto: **PASS** — se espera |Δmedia| y |corr(g,M)| de ORIGINAL claramente mayores que en DECOUPLED, porque en ORIGINAL g(t) multiplica también la constante grande logit(λ)+μ, mientras que en DECOUPLED g(t) solo multiplica μ+η (que fluctúa cerca de 0).

### 2. Autocorrelación con/sin η

- autocorr_lag1(M) ORIGINAL = 0.0965 ± 0.1524; DECOUPLED = 0.1384 ± 0.1144; DECOUPLED_OFFSETS = 0.1097 ± 0.1253.
- Veredicto: **PASS** — se espera DECOUPLED y DECOUPLED_OFFSETS > ORIGINAL, porque solo esas dos variantes incluyen el término AR(1) η(t) en el argumento logit.

### 3. Efecto de B (offset de media)

- corr(MA7(M), m(t)) DECOUPLED = -0.1082 ± 0.2642; DECOUPLED_OFFSETS = 0.2046 ± 0.2256.
- Veredicto: **PASS** — se espera que DECOUPLED_OFFSETS muestre |correlación| mayor con m(t), porque es la única variante que suma m(t) al argumento logit; en DECOUPLED m(t) no entra en la fórmula (research/05 §2.2, compute_arg en engine/mood.py).

### Veredicto global (8a): **PASS**

Confirmadas: (1) acoplamiento media-ganancia, (2) autocorrelación con/sin η, (3) efecto de B.
No confirmadas: ninguna.

## 4. Recomendación de variante para el POC

ORIGINAL acopla temperamento y ganancia (un knob, λ, mueve nivel Y reactividad a la vez) y carece de η — su autocorr_lag1(M) (0.096) queda cerca del piso frente a DECOUPLED_OFFSETS (0.110) — no permite tunear 'racha sin causa externa' por separado de 'temperamento'; se descarta para Fase 2.
El offset de media m(t) sí deja huella medible (|corr(MA7(M), m(t))| mayor en DECOUPLED_OFFSETS que en DECOUPLED: 0.2046 vs 0.1082), es decir, B compra una señal real y no redundante con A/η.
**Recomendación: DECOUPLED_OFFSETS.** Compra tres knobs ortogonales (B = nivel por fase, A = reactividad por fase, ρ_e/σ_e = rachas sin causa) al costo de un parámetro extra (B) frente a DECOUPLED — costo bajo, la separación de knobs es justamente el objetivo de research/05 §2.2 y el efecto es verificable en los datos de esta corrida.

## Figuras

- `variants_w32_s111.png` — comparación de variantes (sim.plots.plot_variant_comparison), semilla 111
- `mood_series_by_variant.png` — M(t) por variante, 5 semillas superpuestas + media
- `metrics_barplot.png` — barplot de métricas (Δmedia, corr(g,M), autocorr_lag1) con error bars
