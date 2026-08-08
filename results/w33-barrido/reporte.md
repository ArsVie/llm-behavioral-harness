# W3.3 — Barrido de parámetros (criterio 8b)

Variante fija: `decoupled_offsets`. Horizonte: 90 días. Semillas de barrido: `[11, 22, 33, 44, 55]` (métricas promediadas entre las 5 por celda). Semillas de verificación (frescas): `[66, 77, 88, 99, 110]`.

Criterio (8b) — región humana: media(M) ∈ [5.25, 6.75], sd(M) ∈ [1.2, 2.8], autocorr_lag1 ∈ [0.2, 0.5], fracción_saturada < 0.1.

## 1. Grid rho_e x sigma_e (autocorrelación endógena)

![autocorr](01_rho_e_sigma_e_autocorr.png)

![sd](02_rho_e_sigma_e_sd.png)

Celdas dentro de la región humana: **5** de 16. Recorrido de autocorr_lag1: [0.083, 0.511]; recorrido de sd(M): [1.56, 2.40].

Celdas humanas (rho_e, sigma_e) → métricas:

- rho_e=0.5, sigma_e=0.45: media=6.15 sd=1.99 ac1=0.227 sat=0.033
- rho_e=0.7, sigma_e=0.3: media=6.21 sd=1.87 ac1=0.203 sat=0.031
- rho_e=0.7, sigma_e=0.45: media=6.04 sd=2.17 ac1=0.391 sat=0.044
- rho_e=0.85, sigma_e=0.2: media=6.23 sd=1.80 ac1=0.261 sat=0.024
- rho_e=0.85, sigma_e=0.3: media=5.99 sd=2.08 ac1=0.407 sat=0.036

Lectura: el humo previo con defaults (rho_e=0.5, sigma_e=0.2) dio autocorr ≈ 0.16, bajo el objetivo. Subir rho_e (más memoria del AR(1) de η) empuja autocorr_lag1 hacia arriba sin cambiar la sd estacionaria de η (σ_e/√(1−ρ_e²)) tanto como subir σ_e directamente; sigma_e alto con rho_e alto simultáneamente infla sd(M) y puede acercarse a saturación en las colas de p(t).

## 2. Grid k x rho (memoria de eventos)

![autocorr](03_k_rho_autocorr.png)

![sd](04_k_rho_sd.png)

Celdas **inestables por diseño** (violan k < 2(1−rho)/g_max):

- k=0.3, rho=0.85: k: violates stability bound k < 2(1−rho)/g_max (0.3 >= 0.223881, g_max=1.34)
- k=0.44, rho=0.85: k: violates stability bound k < 2(1−rho)/g_max (0.44 >= 0.223881, g_max=1.34)

Celdas dentro de la región humana: **2** de 10 celdas estables (de 12 totales).

Celdas humanas (k, rho) → métricas:

- k=0.3, rho=0.5: media=6.47 sd=1.67 ac1=0.222 sat=0.027
- k=0.44, rho=0.5: media=6.70 sd=1.69 ac1=0.275 sat=0.036

Lectura: k y rho controlan la memoria del lazo juez→μ, no la autocorrelación endógena de η — su efecto sobre autocorr_lag1 de M es más débil e indirecto (vía la varianza que añaden a p(t) día a día); rho alto con k cerca de la cota de estabilidad es donde más sube sd(M).

## 3. Grid A x B (ciclo)

![var_ratio](05_A_B_var_ratio.png)

![amplitude](06_A_B_amplitude.png)

Celdas dentro de la región humana: **1** de 9. var_ratio_by_gain crece con A (ganancia amplifica la reactividad); la amplitud del ciclo en M crece con B (desplazamiento de media m(t)) y es ~0 cuando B=0 por construcción.

Celdas humanas (A, B) → métricas:

- A=0.4, B=0.3: media=6.36 sd=1.72 ac1=0.201 sat=0.020 var_ratio=0.86 amplitud=1.17

## 4. Barrido 1D nu (defaults, sobredispersión beta-binomial)

![nu](07_nu_1d.png)

| nu | media(M) | sd(M) | autocorr_lag1 | sat_frac |
|---|---|---|---|---|
| inf | 6.38 | 1.66 | 0.110 | 0.018 |
| 8 | 6.33 | 2.17 | 0.046 | 0.078 |
| 4 | 6.34 | 2.56 | 0.079 | 0.131 |

Lectura: yendo de nu=inf a nu=4, autocorr_lag1 **bajó** (0.110 → 0.079) y sd(M) **subió** (1.66 → 2.56), consistente con que la sobredispersión beta-binomial añade varianza blanca (ruido no autocorrelacionado) por encima del binomial puro.

## Defaults afinados propuestos

A partir del grid 1 (única fuente de autocorrelación endógena pura), se elige el punto que acerca autocorr_lag1 al centro del rango objetivo [0.2, 0.5] sin salir de sd(M) ≤ 2.8 ni saturar. Todo lo demás queda en el default de `PersonaParams()`.

```python
PersonaParams(
    N=10,
    lam=0.6,
    nu=inf,
    k=0.15,
    rho=0.7,
    rho_e=0.7,  # <- afinado
    sigma_e=0.45,  # <- afinado
    B=0.15,
    A=0.25,
    sigma_eps=0.03,
    L_mean=28.0,
    L_sd=1.5,
    phi=0.0,
    score_neutral=0.0,
)
```

Justificación: (1) rho_e=0.7 y sigma_e=0.45 colocan la autocorr_lag1 de M en el rango objetivo — el default previo (rho_e=0.5, sigma_e=0.2) daba ≈0.16 en el humo, por debajo del piso 0.2. (2) el resto de los parámetros (k, rho, A, B, nu, N, lam) se dejan sin tocar porque los grids 2–4 muestran que su efecto sobre autocorr_lag1 es más débil o va en la dirección equivocada (nu finito lo baja, no lo sube) frente al que ofrece rho_e/sigma_e. (3) se verifica con 5 semillas frescas para descartar sobreajuste a las semillas del barrido.

### Verificación (semillas frescas)

Semillas: `[66, 77, 88, 99, 110]`.

| métrica | valor | rango objetivo | cumple |
|---|---|---|---|
| media(M) | 5.8267 | (5.25, 6.75) | PASS |
| sd(M) | 2.0704 | (1.2, 2.8) | PASS |
| autocorr_lag1 | 0.3934 | (0.2, 0.5) | PASS |
| sat_frac | 0.0267 | < 0.1 | PASS |

![verificación](08_verificacion_defaults_M_t.png)

## Veredicto (8b): **PASS**

Existe una región no vacía que cumple los 4 umbrales del criterio (8b) (grid 1: 5 celdas, grid 2: 2 celdas, grid 3: 1 celdas), y la propuesta de defaults afinados se verificó con 5 semillas frescas (PASS). PASS de (8b) = región no vacía + propuesta verificada.
