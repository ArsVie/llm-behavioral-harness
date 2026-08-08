# Fase 1 — Informe de validación del motor estocástico (W4.1)

**Fecha:** 2026-07-03
**Alcance:** motor estocástico aislado + simulación 90–120 días, **sin LLM** (objetivo de Fase 1 del plan).
**Insumos:** los 5 reportes de la Ola 3 — [w31-baseline](w31-baseline/reporte.md) · [w32-variantes](w32-variantes/reporte.md) · [w33-barrido](w33-barrido/reporte.md) · [w34-temporizacion](w34-temporizacion/reporte.md) · [w35-shocks](w35-shocks/reporte.md) — más una verificación adicional de W4.1 (criterio 4 bajo defaults afinados, abajo).
**Estado del código:** 213 tests verdes (`pytest` completo); todos los experimentos reproducibles con `python -m experiments.<id>` (semillas fijas documentadas en cada reporte).

---

## Resumen ejecutivo

El motor se sostiene. Con los **defaults de DESIGN.md** pasan 6 de 8 criterios; los dos que fallan — (4) varianza modulada por g y (6) autocorrelación lag-1 "humana" — comparten una sola causa raíz: el ruido binomial rápido (N=10, Var≈2.4/día) entierra la señal lenta de μ/η con σ_e=0.2. El barrido (W3.3) encontró el arreglo con un cambio mínimo: **`rho_e=0.7, sigma_e=0.45`** (todo lo demás igual), que mete la autocorrelación en el centro del rango objetivo (0.39–0.41) y levanta el ratio de varianza por g a 1.31 medio, sin saturar ni desestabilizar la media. La comparativa de variantes (W3.2) confirma con datos la elección de diseño: **DECOUPLED_OFFSETS**. La temporización (W3.4) pasa entera tal cual está configurada. El lazo score→μ (W3.5) revierte shocks como predice la teoría y la cota de estabilidad se confirma conservadora, con un hallazgo estructural a tener en cuenta para Fase 3 (sesgo positivo del lazo con `score_neutral=0`, ver riesgos).

**Recomendación:** congelar `MoodVariant.DECOUPLED_OFFSETS` + los defaults afinados de abajo como parámetros de arranque de Fase 2, y revisar en el checkpoint los dos puntos de decisión marcados (fuerza del efecto g; sesgo del lazo).

---

## Tabla de criterios (plan §validación + research/05 §6)

| # | Criterio | Umbral | Defaults DESIGN | Defaults afinados | Veredicto | Evidencia |
|---|---|---|---|---|---|---|
| 1 | Media de M estable ≈ N·σ(logit λ)=6.0, sin deriva | media ∈ [5.25, 6.75]; deriva < 1.0 | 5.82–6.33, deriva ≤ 0.98 (5/5) | media 5.77 (10 semillas) | **PASS** | [w31](w31-baseline/mean_M_across_seeds.png) |
| 2 | m/g ondas limpias de periodo ~L | autocorr m lag28 > 0.5; amps ≈ B, A ±30% | 0.90–1.00; amp(m)=0.150, amp(g−1)≈0.25 (5/5) | sin cambio (no depende de ρ_e/σ_e) | **PASS** | [w31](w31-baseline/mg_decoupled_offsets_s101.png) |
| 3 | Histograma de M sin saturación | frac(M∈{0,N}) < 10% | 0–1.1% | 3.0% | **PASS** | [w31](w31-baseline/mood_hist_decoupled_offsets_s101.png) |
| 4 | var(M) mayor en g-alta vs g-baja | ratio > 1 | **FAIL** — 0.926 medio, 3/5 | **PASS marginal** — 1.308 medio, 7/10 | **PASS marginal** | [w33 heatmap](w33-barrido/05_A_B_var_ratio.png) + §verificación abajo |
| 5 | μ cae con racha y revierte en ~1/(1−ρ) d; cota de estabilidad | caída < μ_pre−0.15; reversión ∈ [1,8] d; orden monotónico en k | caída 5/5; reversión 3–8 d (teórico 2.8); ρ↑⇒reversión↑ (3.4/5.2/12.6 d); cota separa k=0.40/0.47/0.60 monotónicamente | — | **PASS** (nota: umbral literal \|μ\|<0.6 falla por sesgo de λ, ver riesgo R1) | [w35](w35-shocks/01_shock_mu_t.png), [w35 cota](w35-shocks/04_k_comparison_mu_and_sat.png) |
| 6 | autocorr lag-1 de M ∈ [0.2, 0.5] | por semilla | **FAIL** — 0.113 medio, 0/5 | **PASS** — 0.411 medio, 9/10 (una semilla 0.549) | **PASS (afinados)** | [w33 heatmap](w33-barrido/01_rho_e_sigma_e_autocorr.png), [verificación](w33-barrido/08_verificacion_defaults_M_t.png) |
| 7 | Temporización: horario ⊂ envolvente; 0 en quiet hours; media ∈ [1,3]/d; hazard creciente; efecto de fase | ver reporte | 0 violaciones (5/5); 1.36–1.59/d (5/5); moda gaps 14.5 h ≫ 0; cv↓ con k_w (0.83→0.41); Spearman(mult, tasa)=0.87; cap ata 5.8% de días | n/a (TimingParams sin cambios) | **PASS** | [w34 horario](w34-temporizacion/hourly_events_baseline_agg_s1001-1002-1003-1004-1005.png), [fase](w34-temporizacion/phase_rate_vs_multiplier.png) |
| 8a | Variantes comparadas, diferencias documentadas | 3 contrastes estructurales | acoplamiento media-ganancia del ORIGINAL visible (Δmedia 0.40 vs −0.27); autocorr con η > sin η; efecto de B medible (corr 0.20 vs −0.11) | — | **PASS** | [w32](w32-variantes/metrics_barplot.png) |
| 8b | Barrido: región "humana" + defaults afinados verificados | región no vacía + verificación con semillas frescas | región en los 3 grids (5+2+1 celdas); propuesta verificada 4/4 métricas | — | **PASS** | [w33 heatmaps](w33-barrido/01_rho_e_sigma_e_autocorr.png) |
| 9 | Repetibilidad del juez | — | — | — | **Fase 3** (fuera de alcance, por plan) | — |

---

## Verificación adicional W4.1 — criterio (4) bajo defaults afinados

W3.3 verificó su propuesta contra 4 métricas (media, sd, autocorr, saturación) pero no contra el ratio de varianza por g. Se corrió aparte: `PersonaParams(rho_e=0.7, sigma_e=0.45)`, DECOUPLED_OFFSETS, 90 días, 10 semillas `[66,77,88,99,110,101,202,303,404,505]`:

```
var_ratio  medio = 1.308   (> 1 en 7/10 semillas; rango 0.43–2.34)
ac1        medio = 0.411   (en [0.2,0.5]: 9/10)
mean(M)    medio = 5.77    sd(M) medio = 2.13    saturación media = 3.0%
```

Lectura: al triplicar la sd estacionaria de η (0.23 → 0.63), g tiene más desviación que amplificar y el efecto pasa de invisible (0.93) a presente (1.31). Sigue siendo **marginal a 90 días por semilla** (3/10 semillas quedan < 1 por azar): la huella existe en agregado pero no está garantizada en una ventana corta. Si se quiere perceptible por ciclo individual, el heatmap [05_A_B_var_ratio](w33-barrido/05_A_B_var_ratio.png) indica que hay que subir A (0.25 → 0.4), al costo de subir g_max y bajar la cota admisible de k (0.448 → 0.404 con A=0.4). **Decisión para el checkpoint.**

---

## Elección de variante

**`MoodVariant.DECOUPLED_OFFSETS`** — confirmada por datos (W3.2), no solo por diseño:

- ORIGINAL acopla nivel y reactividad en un solo término (`(logit λ + μ)·g`): Δmedia por g de +0.40 pasos sin knob para apagarla, y sin η no hay rachas endógenas (autocorr 0.097, la más baja). Se descarta.
- DECOUPLED pierde el desplazamiento de media por fase (corr(MA7(M), m) = −0.11 ≈ ruido): el ciclo solo modula varianza, invisible como "estado de ánimo por fase".
- DECOUPLED_OFFSETS compra los tres knobs ortogonales (B nivel, A reactividad, ρ_e/σ_e rachas) al costo de un parámetro extra; todos dejan huella medible por separado.

---

## Defaults afinados propuestos (arranque de Fase 2)

```python
PersonaParams(
    N=10, lam=0.60, nu=math.inf,
    k=0.15, rho=0.70,
    rho_e=0.70,     # ← afinado (era 0.50)
    sigma_e=0.45,   # ← afinado (era 0.20)
    B=0.15, A=0.25, sigma_eps=0.03,
    L_mean=28.0, L_sd=1.5, phi=0.0,   # φ se sortea por companion en producción
    score_neutral=0.0,                # ← recalibrar en Fase 3 (ver R1)
)
TimingParams()  # sin cambios: validada entera en W3.4
```

Métricas con esta configuración (10 semillas): media 5.77 · sd 2.13 · autocorr 0.41 · saturación 3.0% · var_ratio 1.31. La media queda ~0.2 pasos bajo el 6.0 teórico — el ruido lento mayor interactúa con la concavidad de la sigmoide sobre p>0.5; cosmético y dentro del rango del criterio (1).

---

## Riesgos abiertos

- **R1 — Sesgo positivo estructural del lazo (para Fase 3).** Con `score_neutral=0` y λ=0.6, el score sintético hereda la media de M (E[score]≈+0.2) y μ deriva a un equilibrio positivo; cerca o sobre la cota, el runaway es asimétrico hacia arriba con probabilidad ~1 (hallazgo de W3.5, 5/5 semillas y 3/3 valores de k). No es un bug del motor — es exactamente el fenómeno "juez descentrado" que DESIGN ya obliga a calibrar en Fase 3 (`score_neutral` empírico). Para simulaciones futuras con lazo centrado: `score_neutral ≈ 2·(λ−0.5)`.
- **R2 — Criterio (4) marginal.** El efecto de g sobre la varianza es real pero débil a 90 días (7/10 semillas). Subir A=0.4 lo haría robusto al costo de apretar la cota de estabilidad. Decisión de producto: ¿debe la "fase reactiva" ser perceptible en un solo ciclo, o basta en agregado?
- **R3 — Potencia estadística n=5 (W3.2).** Los tres contrastes de variantes se confirmaron direccionalmente, pero con sd entre semillas del orden de la media (intervalos solapados en el barplot). Las magnitudes citadas tienen incertidumbre alta; las direcciones son consistentes con la estructura de las fórmulas.
- **R4 — Interacción k_w bajo × max_gap.** Con k_w=1 (hazard plano) el guard de 48 h domina la distribución de gaps (pico espurio en ~47.5 h, W3.4 §2). Irrelevante con el default k_w=2; revisar si algún tuning futuro baja k_w hacia 1.
- **R5 — daily_cap.** Ata el 5.8% de los días con defaults (máx 10% por semilla) — aceptable, pero es un recorte real de la cola alta de la tasa; documentado por si la Fase 4 (scheduler real) observa menos "días intensos" de los esperados.

---

## Próximo paso (checkpoint del plan)

Este informe es el insumo de la **revisión conjunta de parámetros antes de cablear el LLM**. Decisiones a tomar en el checkpoint: (a) adoptar los defaults afinados tal cual, o subir también A (R2); (b) `score_neutral` en simulación (R1); (c) dar por congelado el contrato de `engine/types.py` para Fase 2 (cliente LLM + SQLite + CLI del resto de Fase 0, luego persona + cronograma + chat reactivo). El criterio (9) — repetibilidad del juez — queda programado para Fase 3, como manda el plan.

---

## Resolución del checkpoint (2026-07-03, decisión del usuario)

- **(a) Defaults afinados ADOPTADOS tal cual** (`rho_e=0.7`, `sigma_e=0.45`; A se queda en 0.25). Aplicado en `engine/types.py` y reflejado en la tabla de DESIGN.md; suite completa verde tras el cambio (213 tests).
- **(b) R1 resuelto por diseño:** el sesgo ligeramente positivo del lazo se considera **deseable** — `score_neutral` se mantiene en 0.0 a propósito. La calibración empírica del juez en Fase 3 sigue vigente, pero su objetivo pasa a ser controlar la magnitud del sesgo, no eliminarlo.
- **Galería de referencia:** simulaciones de 30 días de los ciclos emocionales bajo distintos efectos diarios (baseline, solo ciclo, solo endógeno, racha negativa, alta volatilidad, ciclo fuerte, efecto intradía) en [`engine_simulation/`](../engine_simulation/README.md), generadas con `python -m experiments.engine_simulation` (semilla 3001).
- **(3ª iteración, mismo día) Memoria de eventos a escala de mes y circadiano solo-energía.** (a) `k=0.18, ρ=0.85` adoptados (eran 0.15/0.70): el techo del trato sube a μ∞=±1.2 — un mes perfecto vive en ~7.5 de media (72% de días ≥7) y uno horrible en ~3.3 (73% de días ≤4), contra 6.4/4.6 con los valores previos ([15_mes_perfecto_horrible.png](../engine_simulation/15_mes_perfecto_horrible.png)); dentro de la cota de estabilidad (0.18 < 0.224) y con half-life de 4.3 días — los días sueltos pesan poco, las rachas se acumulan. Se prefirió subir ρ y no k para no amplificar el ruido diario del juez. (b) **El circadiano deja de tocar la valencia**: `arg_h = arg + c(h)` descartado; la señal intradía se expresa solo por el canal de energía (DESIGN §Modulación circadiana revisado; `circadian.c` queda como utilidad). (c) La temporización sigue con envolvente × fase × adj — la energía **no** será el control único de la frecuencia; unificación tasa←energía diferida a experimento A/B de Fase 4. Tres tests que asumían los defaults viejos se fijaron a parámetros explícitos; suite verde (213).
- **(2ª iteración, mismo día) `B` sube de 0.15 a 0.5.** El análisis de los datos puntuales mostró que con B=0.15 el ciclo mueve el ánimo real solo ±0.36 pasos contra un ruido de muestreo de sd≈1.55 — invisible incluso en la media móvil semanal. El barrido de B promediado sobre 30 semillas (`engine_simulation/12_barrido_B_30seeds.png`) mostró escala monotónica (corr con la onda teórica: 0.54 → 0.77 → 0.87 → 0.93 para B = 0.15/0.3/0.5/0.65) y que **B=0.5 es el mínimo donde el arco mensual es legible en el comportamiento observable**; además el lazo positivo (score→μ) amplifica la onda (~3.3 pasos pico-valle medidos vs 2.4 teóricos). B no entra en la cota de estabilidad (solo A vía g_max). Adoptado en `engine/types.py` y DESIGN.md.
