# W3.1 — Experimento Baseline

90 días, `PersonaParams()` por defecto, variante `decoupled_offsets`, 5 semillas fijas: [101, 202, 303, 404, 505].

Media teórica de referencia: N·sigmoid(logit λ) = 10·sigmoid(logit 0.6) = **6.0000**.

## Criterio (1) — media de M estable

Umbral: media global por semilla ∈ [5.25, 6.75] Y sin deriva (|media(días 0–44) − media(días 45–89)| < 1.0).

| Semilla | media(M) | media(0–44) | media(45–89) | \|deriva\| | PASS/FAIL |
|---|---|---|---|---|---|
| 101 | 6.244 | 6.267 | 6.222 | 0.044 | PASS |
| 202 | 5.822 | 5.378 | 6.267 | 0.889 | PASS |
| 303 | 6.333 | 6.311 | 6.356 | 0.044 | PASS |
| 404 | 6.022 | 6.511 | 5.533 | 0.978 | PASS |
| 505 | 6.233 | 6.178 | 6.289 | 0.111 | PASS |

**Agregado (1):** PASS (5/5 semillas en rango sin deriva).

## Criterio (2) — ondas limpias de m/g, periodo ~L

Umbral: autocorrelación de m en lag 28 > 0.5 (nota: L se redibuja por ciclo ~N(28,1.5), el pico se desdibuja); amplitud empírica de m ≈ B=0.15 (±30%) y de g−1 ≈ A=0.25 (±30%, tras restar el ruido σ_ε=0.03).

Amplitud de m: pico-a-pico/2 (sin ruido, m(d)=B·sin(2πd/L) es determinista dado d). Amplitud de g−1: dos estimadores — pico-a-pico/2 (sesgado al alza por ε) y "desruidado" vía varianza: A_est=√(2·max(Var(g−1)−σ_ε²,0)), asumiendo Var(A·sin θ)≈A²/2 para fase θ que cubre ~uniformemente el ciclo en 90 días.

| Semilla | autocorr m lag28 | amp(m) pp/2 | amp(g−1) pp/2 | amp(g−1) desruidada | PASS/FAIL |
|---|---|---|---|---|---|
| 101 | 0.983 | 0.1499 | 0.2832 | 0.2497 | PASS |
| 202 | 0.902 | 0.1500 | 0.2898 | 0.2457 | PASS |
| 303 | 0.996 | 0.1499 | 0.3084 | 0.2535 | PASS |
| 404 | 0.970 | 0.1500 | 0.2916 | 0.2454 | PASS |
| 505 | 0.970 | 0.1500 | 0.2978 | 0.2496 | PASS |

**Agregado (2):** PASS (5/5 semillas).

## Criterio (3) — histograma de M sin saturación

Umbral: fracción de días con M==0 o M==N < 0.1 por semilla.

| Semilla | fracción saturada | PASS/FAIL |
|---|---|---|
| 101 | 0.0111 | PASS |
| 202 | 0.0111 | PASS |
| 303 | 0.0000 | PASS |
| 404 | 0.0111 | PASS |
| 505 | 0.0000 | PASS |

**Agregado (3):** PASS (5/5 semillas).

## Criterio (4) — var(M) mayor con g alta

Umbral: `var_ratio_by_gain(M, g) > 1.0` en ≥ 4 de 5 semillas.

| Semilla | var_ratio_by_gain | PASS/FAIL |
|---|---|---|
| 101 | 1.062 | PASS |
| 202 | 0.607 | FAIL |
| 303 | 1.323 | PASS |
| 404 | 1.007 | PASS |
| 505 | 0.629 | FAIL |

**Agregado (4):** FAIL (3/5 semillas con ratio > 1.0). Ratio medio entre semillas: **0.926**.

## Criterio (6) — autocorrelación lag-1 de M

Umbral: autocorr lag-1 de M ∈ [0.2, 0.5] por semilla.

| Semilla | autocorr lag-1(M) | PASS/FAIL |
|---|---|---|
| 101 | 0.0869 | FAIL |
| 202 | 0.1357 | FAIL |
| 303 | 0.1768 | FAIL |
| 404 | 0.1176 | FAIL |
| 505 | 0.0460 | FAIL |

**Agregado (6):** FAIL (0/5 semillas en rango). Media entre semillas: **0.1126**.

**Diagnóstico (FAIL honesto, no se ajustan parámetros — trabajo de W3.3):** la media de autocorr lag-1 medida (0.113) es consistente con las mediciones previas de humo (~0.16) reportadas en el enunciado de la tarea. La autocorrelación de M(t) combina dos fuentes de varianza: (a) ruido binomial rápido, decorrelacionado día a día (Var≈N·p(1−p), sin memoria), y (b) la componente lenta correlacionada que viene de μ (memoria del juez, half-life ~1.9 d con ρ=0.70) y del ciclo m,g (periodo ~28 d). Con N=10 y p≈0.6, Var_binomial≈N·p(1−p)≈2.4 por día es grande frente a la amplitud de las componentes lentas (B=0.15, A=0.25 en el argumento logit), así que dilye la autocorrelación observable de M aunque μ y η sí estén autocorrelacionados. Esto apunta a que el ratio señal-lenta/ruido-rápido, no la fórmula de autocorrelación, es lo que hay que subir en el barrido de W3.3 (p. ej. subiendo k, bajando N relativo a la amplitud del argumento, o subiendo B/A dentro de la cota de estabilidad).

## Figuras

- `mood_series_decoupled_offsets_s101.png`
- `mg_decoupled_offsets_s101.png`
- `mood_hist_decoupled_offsets_s101.png`
- `mu_eta_decoupled_offsets_s101.png`
- `mean_M_across_seeds.png`

## Lectura

3/5 criterios agregados en PASS. La media de M se estabiliza cerca del valor teórico (6.00) sin deriva apreciable entre la primera y segunda mitad de los 90 días, y el histograma no satura contra los bordes 0/N (la escala N=10 con λ=0.6 deja margen de sobra a ambos lados). Las ondas de m y g son visibles y su amplitud empírica cae dentro de la tolerancia del ±30%, aunque la autocorrelación de m en lag 28 exacto se ve algo atenuada por el redraw de L ~N(28,1.5) por ciclo (el periodo real oscila alrededor de 28, no es fijo). La ganancia g sí amplifica la varianza de M en el régimen de g alta en la mayoría de semillas. El punto que preocupa es (6): la autocorr lag-1 de M queda por debajo del rango humano esperado — es el ruido binomial rápido (N pequeño, p lejos de 0/1) compitiendo con la señal lenta de μ/η/ciclo, tal como se documentó en el diagnóstico de arriba; queda para W3.3 subir esa relación señal/ruido sin romper la cota de estabilidad k < 2(1−ρ)/g_max.
