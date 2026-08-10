# Amendment 2026-08-10 — fallback de modelo de generación (G3/G5)

**Decisión (autónoma, regla de escalamiento preregistrada del usuario):
cambiar el modelo de generación de deepseek-v4-flash a gpt-5.6-luna para G3/G5.**

## Evidencia
- deepseek-v4-flash devolvió 100% de respuestas vacías (200 + content vacío)
  desde 03:23 hasta el momento de este fallback (30 sondas
  consecutivas, ~1h). Las 4 células G3 lanzadas en ese periodo murieron
  con el mismo RuntimeError (4-7 intentos agotados).
- gpt-5.6-luna (mismo endpoint opencode-go, misma clave) respondió
  contenido normal durante el episodio (probes con finish/usage reales).
- Regla del usuario (it2): un modelo con tasa alta de vacíos es inapto
  para un corpus de 30 días — escalar a cambio de modelo antes de G5.

## Decisión
- Generación G3+G5: gpt-5.6-luna. Todas las células de la matriz usan el MISMO
  modelo (comparabilidad dentro del experimento intacta).
- Juces (G6): familias sin cambios (opencode-flash / opencode-luna); si
  flash sigue degradado en G6, la familia 1 se reevalúa con luna y se
  reporta (desacuerdo entre familias ya es el diseño).
- Reversible: si flash se recupera, re-correr G3/G5 con el modelo
  congelado es una decisión de revisión del orquestador.

## Estado preregistrado
- El manifiesto it3 (results/it3-g4-manifest-*.json) congela el modelo de
  generación; este amendment lo modifica ANTES de la generación de la
  matriz (G4 es gate de revisión — el orquestador revisa al despertar).
