# CONVENTIONS — Fase 1 (congelado en Ola 0 / W0.1)

Reglas operativas para toda tarea (humana o subagente) de este repo.
Leer junto con `engine/types.py` (contrato congelado) antes de escribir código.

## 1. Rutas duales (Windows ↔ WSL)

El proyecto vive en el filesystem de WSL Ubuntu y se ve desde dos lados:

| Vista | Ruta | La usan |
|---|---|---|
| Windows (UNC) | `\\wsl.localhost\ubuntu\home\vruizes\.hermes\projects\llm-behavioral-harness` | Read / Write / Edit / Glob / Grep |
| WSL (nativa) | `/home/vruizes/.hermes/projects/llm-behavioral-harness` | Python, pytest, uv (vía `wsl.exe`) |

**Reglas duras:**
- **NO usar la herramienta Bash sobre este árbol** — mostró una vista
  desincronizada (archivos escritos con Write no visibles). Ejecutar siempre
  con la herramienta **PowerShell** invocando `wsl.exe`.
- Contenido de archivos: herramientas Read/Write/Edit con la ruta UNC.
- Mover/copiar/crear directorios: PowerShell con ruta UNC, o `wsl.exe ... mkdir/cp`.

## 2. Runtime verificado (W0.1, 2026-07-03)

- WSL distro **Ubuntu** · Python **3.12.3** (`/usr/bin/python3`) · `uv 0.x` en `~/.local/bin`.
- Venv del proyecto: `.venv` (creado con `uv venv`), dependencias instaladas
  con `uv pip install -e ".[dev]"` → numpy 2.5.0, scipy 1.18.0,
  matplotlib 3.11.0, pyyaml 6.0.3, pytest 9.1.1. Paquete instalado editable
  (`import engine`, `import sim` funcionan desde cualquier cwd).

### Comandos canónicos (copiar/pegar en la herramienta PowerShell)

> **Usar comillas SIMPLES en el string de PowerShell** — con dobles,
> PowerShell interpola `$VAR` antes de llegar a bash (verificado: `$HOME` se
> corrompió en la prueba de W0.1).

Suite completa:

```powershell
wsl.exe -d Ubuntu -- bash -lc 'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && MPLBACKEND=Agg .venv/bin/python -m pytest'
```

Un solo archivo de tests (lo normal para una tarea de ola):

```powershell
wsl.exe -d Ubuntu -- bash -lc 'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && MPLBACKEND=Agg .venv/bin/python -m pytest tests/test_mood.py -q'
```

Ejecutar un módulo/script:

```powershell
wsl.exe -d Ubuntu -- bash -lc 'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && MPLBACKEND=Agg .venv/bin/python -m sim.run_daily --days 90 --seed 12345'
```

Reinstalar deps (solo si cambia `pyproject.toml`):

```powershell
wsl.exe -d Ubuntu -- bash -lc 'cd /home/vruizes/.hermes/projects/llm-behavioral-harness && uv pip install --python .venv/bin/python -e ".[dev]"'
```

## 3. Propiedad de archivos (regla de las olas)

- **Congelados tras Ola 0 (solo lectura):** `engine/types.py`, `engine/rng.py`,
  `tests/conftest.py`, `pyproject.toml`, este archivo.
- Cada tarea posee **su módulo + su test** (p. ej. W1.1 → `engine/mood.py` +
  `tests/test_mood.py`) y, en Ola 3, **su carpeta** `results/<experimento>/`.
  Nadie toca archivos de otra tarea, ni siquiera para "arreglar" algo: si un
  archivo ajeno parece mal, se reporta en el resumen final de la tarea.
- Los stubs ya definen firma + semántica; implementar EXACTAMENTE esas firmas
  (se permiten helpers privados adicionales en el propio archivo).

## 4. Convenciones de código

- Python 3.11+; type hints en firmas públicas; docstrings en español,
  identificadores en inglés (como los stubs).
- `engine/` es **puro**: sin I/O, sin lectura de reloj real
  (`time`, `datetime.now` prohibidos — el reloj es virtual), sin estado
  global. Todo azar entra por un `numpy.random.Generator` inyectado.
- RNG: solo vía `engine/rng.py` (SeedSequence jerárquico). Nunca
  `np.random.seed` ni `default_rng()` sin semilla en código de producción.
- Estados (`MoodState`, `CycleState`) no se mutan: los pasos devuelven
  instancias nuevas.
- Matplotlib solo en `sim/plots.py` y experimentos, siempre backend Agg
  (`matplotlib.use("Agg")` antes de importar pyplot).
- Tiempo: días enteros para la escala lenta; horas absolutas float para la
  rápida (t_h=0 ⇒ día 0, 00:00; hora local = t_h % 24). Ver `types.py`.

## 5. Tests

- pytest plano (sin plugins extra). Los tests estadísticos usan semilla fija
  y tolerancias documentadas en el propio test (generosas: verifican forma,
  no décimas — p. ej. KS con α=0.01, medias con ±3·sem).
- Cada tarea corre **solo su archivo** de tests y reporta pass/fail; la suite
  completa la corre la sesión principal en el gate de cada ola.
- Las figuras de experimentos fijan semilla y la escriben en el título y en
  el nombre del reporte.

## 6. Experimentos (Ola 3)

- Un script por experimento en `experiments/` (`w31_baseline.py`, ...),
  ejecutable con `.venv/bin/python -m experiments.<nombre>` o como script.
- Salidas SOLO en `results/<id>/` propio: `*.png` + `reporte.md` con
  (a) semillas usadas, (b) criterio(s) evaluado(s) con umbral numérico,
  (c) veredicto pass/fail por criterio, (d) 2–5 líneas de lectura.
