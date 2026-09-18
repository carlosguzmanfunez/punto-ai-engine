# CASE DIRECTORY v0 — casos canónicos reejecutables

Un **caso** conserva una situación conocida del motor y la garantía que esa situación debe seguir
respetando. Sirve para responder una sola pregunta:

> Ante esta situación conocida, ¿el motor sigue comportándose correctamente?

No es un framework de pruebas: el directorio **reutiliza** pytest, los montajes durables y los
contratos que el repositorio ya tiene. No hay proveedor real, ni red, ni reloj, ni azar.

## Qué es un caso

Un fichero JSON en `tests/cases/data/`, con el esquema mínimo:

```json
{
  "case_id": "CASE-004",
  "title": "Un require de go.mod en una línea no produce contención falsa",
  "category": "RESOURCE_CONTAINMENT",
  "description": "Garantía (AUD-T-01): ...",
  "input": {"scenario": "go_mod_dependency_unproven", "params": {"variant": "una-linea"}},
  "expected": {"node_status": "BLOCKED", "resource_tokens_contains": ["package:..."]},
  "tags": ["go.mod", "parser", "aud-t-01"],
  "source": "tests/test_project_replan_go_mod_containment.py::test_e2e_...",
  "schema_version": 1,
  "related_failure": "AUD-T-01",
  "related_memory": "problem: reparar la contención ..."
}
```

Cada caso responde a cuatro preguntas: **qué situación** reproduce (`description`), **qué entra**
(`input`), **qué se espera** (`expected`) y **qué garantía** protege (`title` + `description`).
`related_failure` y `related_memory` son trazabilidad opcional: nada depende de que exista memoria
previa para que un caso se ejecute.

Categorías: `AUTHORITY`, `RESOURCE_CONTAINMENT`, `HUMAN_GATE`, `REPLAN`, `MEMORY`, `FAIL_CLOSED`,
`CONSUMER_QA` (ejecuta QA Consumer contra una aplicación real; ver
[QA_CONSUMER_V0.md](QA_CONSUMER_V0.md)) y `PROVIDER` (la salida de un proveedor de modelos no
concede autoridad; ver [MULTI_PROVIDER_V0.md](MULTI_PROVIDER_V0.md)).

El vocabulario de `expected` es **cerrado**. Las claves `*_contains` exigen contención; el resto,
igualdad exacta. Una clave que el runner no conoce invalida el caso.

## Dónde vive

```
tests/cases/
  model.py                 modelo de caso, categorías y vocabulario cerrado de `expected`
  loader.py                carga fail-closed de `data/`
  scenarios.py             escenarios sobre los caminos reales del motor
  runner.py                load_cases / run_case / run_cases y la comparación
  report.py                tabla compacta y reporte JSON
  conftest.py              resumen de la sesión y opción --case-json
  test_case_directory.py   los 14 casos y la validez del directorio
  data/CASE-0NN.json       los casos
```

## Cómo se ejecutan todos

```bash
.venv\Scripts\python.exe -m pytest tests/cases -q
```

Ese es el comando único. Cada caso canónico es un test parametrizado: si el motor se comporta
distinto de lo declarado, el caso aparece como `FAIL` y la suite falla. Al final de la sesión se
imprime la tabla `CASE | CATEGORÍA | RESULTADO | GARANTÍA`.

Para el reporte estructurado:

```bash
.venv\Scripts\python.exe -m pytest tests/cases -q --case-json casos.json
```

Filtros desde Python (misma API que usa la suite):

```python
from cases import load_cases, run_cases
from cases.model import CaseCategory

run_cases(load_cases(), categories={CaseCategory.MEMORY})
run_cases(load_cases(), tags={"go.mod"})
```

## Cómo añadir un caso

1. Elige o escribe un **escenario** en `scenarios.py` que conduzca el motor de verdad y devuelva
   `Observation(facts=...)`. Un escenario que solo simula la garantía no vale.
2. Añade el fichero `data/CASE-0NN.json` con el nombre exacto de su `case_id`.
3. Ejecuta el comando único y **comprueba que el caso pasa por la razón correcta**: si has tenido que
   ajustar `expected` a lo observado, asegúrate de que lo observado es la garantía, no otra cosa.

## Cómo se interpretan los resultados

- `PASS`: todos los hechos declarados coinciden con lo observado.
- `FAIL`: algo no coincide. El motivo dice qué clave cambió, con lo esperado y lo observado.
- `SKIP`: no se pudo ejecutar por una razón **explícita y verificable** (por ejemplo, un servicio
  externo que no está). Una razón en blanco no salta el caso. Ningún caso canónico usa `SKIP`.
- **Error de infraestructura**: runner inexistente, esquema inválido, `case_id` duplicado, escenario
  que revienta o expectativa que el escenario nunca observa. Siempre termina en `FAIL`:
  «no pude ejecutarlo» **nunca** es `PASS`.

## Diferencia con pytest y con PELL

| | Qué responde |
| --- | --- |
| **pytest** | ¿está bien implementado? Pruebas unitarias y contractuales del código. |
| **Case Directory** | ¿sigue el sistema comportándose bien en una situación conocida? Escenarios reejecutables. |
| **PELL** | ¿qué aprendimos? Memoria de experiencia: consejo, nunca autoridad. |

El directorio no sustituye a pytest ni duplica sus fixtures, su descubrimiento o sus aserciones: los
usa. Y no sustituye a PELL: un caso demuestra un comportamiento que debe seguir funcionando; PELL
recuerda lo que pasó la última vez.
