# Galería de simulaciones — efectos diarios del motor

30 días · variante `decoupled_offsets` · semilla **3001** compartida entre los 6 escenarios (las diferencias vienen de los overrides, no del azar). Persona base = `PersonaParams()` (defaults adoptados en Fase 1).

## Figuras

| Figura | Qué muestra | Qué mirar |
|---|---|---|
| `00_comparativa.png` | Small multiples 2×3 de M(t) para los 6 escenarios, mismo eje y | Contraste rápido de dispersión y nivel medio entre escenarios |
| `01_baseline.png` | Todos los efectos activos: ciclo m/g + rachas endógenas η + memoria de eventos μ | Línea de base con la que comparar los demás escenarios |
| `02_solo_ciclo.png` | σ_e=0, k=0 ⇒ η≡0 y μ≡0: solo queda la onda hormonal m/g | Periodicidad ~28 días pura, sin ruido de rachas ni memoria |
| `03_solo_endogeno.png` | B=0, A=0, σ_ε=0, k=0: solo quedan las rachas endógenas η | Deriva tipo "amanecí así, sin motivo", sin periodicidad del ciclo |
| `04_racha_negativa.png` | Defaults + shocks días 10–14 = −1.0 (vía μ) | Profundidad de la caída de μ durante la racha y velocidad de recuperación al soltar |
| `05_alta_volatilidad.png` | ν=4.0: sobredispersión beta-binomial | M(t) más errático día a día que el baseline, banda de referencia más ancha |
| `06_ciclo_fuerte.png` | A=0.4, B=0.3: variante "fase perceptible" (riesgo R2, results/fase-1-informe.md) | Oscilación de m/g y su arrastre sobre M(t) mucho más visible en un solo ciclo |
| `07_intradia.png` | Efecto circadiano (rápido) sobre el baseline: heatmap p_h(d,h) y curvas de energía por fase | Pico diario de probabilidad de mensaje alrededor de `peak_hour`, y cómo el offset de energía por fase desplaza cada curva |

## Regenerar

```powershell
wsl.exe -d Ubuntu -- bash -lc 'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && MPLBACKEND=Agg .venv/bin/python -m experiments.engine_simulation'
```

Semilla compartida: **3001** · variante: `decoupled_offsets` · días: 30

### Persona base y overrides por escenario

Persona base = `PersonaParams()` (defaults): lam=0.6, nu=inf, k=0.15, rho=0.7, rho_e=0.7, sigma_e=0.45, B=0.15, A=0.25, sigma_eps=0.03.

> Nota: las figuras 00–12 se generaron con B=0.15 (el default de entonces). El 2026-07-03 se adoptó **B=0.5** tras el barrido de la figura 12 (las figuras 13–15 ya lo usan), y después **k=0.18, ρ=0.85** tras la figura 15 (que compara ese régimen — "lenta" — contra el anterior). Defaults vigentes: ver `engine/types.py`.

| Escenario | Overrides (dataclasses.replace) | Shocks |
|---|---|---|
| `01_baseline` (baseline) | — | — |
| `02_solo_ciclo` (solo ciclo hormonal) | sigma_e=0.0, k=0.0 | — |
| `03_solo_endogeno` (solo rachas endogenas) | B=0.0, A=0.0, sigma_eps=0.0, k=0.0 | — |
| `04_racha_negativa` (racha negativa (shocks 10-14)) | — | días 10–14 = -1.0 |
| `05_alta_volatilidad` (alta volatilidad (nu=4.0)) | nu=4.0 | — |
| `06_ciclo_fuerte` (ciclo fuerte (A=0.4, B=0.3)) | A=0.4, B=0.3 | — |

## Lecturas adicionales

Con B=0.15 (default) el ciclo hormonal mueve el ánimo real N·p(t) solo ≈0.36 pasos (sensibilidad local N·p·(1−p)≈2.4 pasos/logit) contra un ruido de muestreo binomial de sd≈1.55 pasos: invisible mirando solo los puntos M(t) del dado diario. Estas dos figuras separan la señal del ruido de muestreo.

| Figura | Qué muestra | Cómo leerla |
|---|---|---|
| `10_barrido_B.png` | 4 paneles (B ∈ {0.15, 0.30, 0.50, 0.65}, resto de la persona = defaults): M(t) (dado diario, gris), N·p(t) ± σ_binom (ánimo real, azul) y MA7(M) (media móvil 7 días, naranja discontinua) | Compara la amplitud teórica del título de cada panel (≈2.4·B pasos) contra el ruido de muestreo sd≈1.55 pasos: recién con B≈0.5–0.65 la onda se distingue a simple vista en N·p(t) y, más suavizada aún, en MA7(M) |
| `11_lectura_suavizada.png` | Los mismos 6 escenarios de la galería principal, pero releídos con N·p(t) (ánimo real) y MA7(M) (media móvil) superpuestos sobre M(t) (dado diario, gris) | Compara qué sobrevive al promediar: en `02_solo_ciclo` y `06_ciclo_fuerte` la onda hormonal emerge con claridad en N·p(t); en `04_racha_negativa` la caída y recuperación de la racha se ve mucho más nítida en MA7(M) que en el M(t) crudo; en `05_alta_volatilidad` el suavizado reduce el aspecto errático pero no cambia la tendencia central |
| `12_barrido_B_30seeds.png` | El mismo barrido de B ∈ {0.15, 0.30, 0.50, 0.65} que `10_barrido_B.png`, pero promediado entre 30 semillas (4001–4030) en vez de mostrar una sola: media entre semillas de M(t) (naranja, ± sem sombreado), media entre semillas de N·p(t) (azul) y la onda teórica pura N·sigmoid(logit(0.6)+B·sin(2πt/28)) (negro punteado) | Al promediar 30 semillas el ruido de muestreo binomial y las rachas endógenas de η se cancelan en gran parte, dejando ver la onda hormonal incluso para B pequeño; compara la amplitud pico-valle medida (título de cada panel) contra la de la onda teórica para ver cuánto de la señal restante viene de μ/η residual |
| `13_dias_buenos_malos.png` | 3 paneles apilados (una sola semilla, 3001): "siempre buenos" (shock=+1.0 todos los días), baseline (score endógeno, sin shocks) y "siempre malos" (shock=−1.0 todos los días); M(t) crudo (gris), N·p(t) ± sd binomial (verde/azul/rojo) y μ(t) en eje secundario con la línea de equilibrio teórico μ∞=±0.5 | Compara el μ(t) final medido (título de cada panel) contra el equilibrio teórico μ∞=k·(s−score_neutral)/(1−ρ)=±0.5; con ρ=0.70 la vida media de μ es ≈1.9 días, así que el equilibrio se alcanza en ≈5–7 días |
| `14_dias_buenos_malos_promedio.png` | Media entre 30 semillas (4001–4030) de M(t) para los 3 regímenes en un solo eje (verde/azul/rojo, ± sem sombreado), con las curvas de referencia N·sigmoid(logit(0.6)+B·sin(2πt/28)+μ∞) punteadas (μ∞∈{+0.5, 0, −0.5}); panel inferior: media entre semillas de μ(t) por régimen con las asíntotas ±0.5 | Muestra cuánto separa en pasos de M un régimen de "siempre buenos" de uno de "siempre malos" una vez que μ converge, y en cuántos días se abre esa separación desde el arranque compartido en μ=0 |
| `15_mes_perfecto_horrible.png` | Tres parametrizaciones de la memoria de eventos — actual (k=0.15, ρ=0.70, μ∞=±0.5), media (k=0.25, ρ=0.80, μ∞=±1.25) y lenta (k=0.18, ρ=0.85, μ∞=±1.20), todas dentro de la cota de estabilidad — bajo mes perfecto (+1) y mes horrible (−1), 30 semillas × 30 días; banda = p10–p90 de los días, zonas objetivo 7–10 y 0–4 sombreadas | Con μ∞=±0.5 (actual) el mes perfecto se queda en ~6.4 (50% de días ≥7); con μ∞≈±1.2–1.25 el mes perfecto vive en ~7.5–7.6 (72–75% de días ≥7) y el horrible en ~3.2–3.3 (73–77% de días ≤4) — el techo del trato es el knob k/(1−ρ), no una limitación estructural (regenerar: `experiments.engine_simulation_meses`) |

### Regenerar

```powershell
wsl.exe -d Ubuntu -- bash -lc 'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && MPLBACKEND=Agg .venv/bin/python -m experiments.engine_simulation_lecturas'
```
