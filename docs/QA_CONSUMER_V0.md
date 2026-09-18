# QA CONSUMER v0 — la aplicación, vista por un usuario real

PUNTO ya comprueba código, tests, tipos, lint, autoridad, regresiones y casos contractuales. Esta
capa comprueba otra cosa:

> ¿La aplicación realmente abre, funciona y puede utilizarse?

```
APP -> INICIAR -> ABRIR EN NAVEGADOR -> OBSERVAR -> INTERACTUAR -> DETECTAR FALLOS
    -> CAPTURAR EVIDENCIA -> PASS / FAIL
```

## Qué verifica

Funcionalidad **observable**, en un navegador real (Chromium dentro del sandbox web que ya existía
para ENGINE-5.3):

| Qué detecta | Cómo |
| --- | --- |
| la página no carga | error de navegación o tiempo agotado |
| HTTP/error de navegación relevante | código de estado observado |
| excepción de JavaScript no controlada | `pageerror` del navegador |
| error grave de consola | mensajes de nivel `error` (los avisos **no** fallan) |
| elemento obligatorio ausente | `assert_visible` sobre el selector exigido |
| interacción principal que no funciona | `assert_visible`/`assert_text` después del click |
| formulario que no se puede completar | `fill` + `submit` y estado esperado |
| navegación/ruta rota | URL final observada + HTTP |
| pantalla esencial vacía | visibilidad del elemento principal |
| resultado esperado que nunca aparece | texto esperado dentro del elemento |

No evalúa estética: nada de «esta página es bonita», «este diseño es moderno» ni comparación de
píxeles. Eso sería QA visual, otra capa.

## Cómo se define un escenario

Un caso declara lo mínimo: dónde se abre la aplicación, qué hace el usuario y qué se espera.

```python
from punto.consumer_qa import ConsumerQACase, QAExpectation, QAExpectationKind, QAStep, QAStepKind

case = ConsumerQACase(
    qa_id="QA-003",
    title="El usuario pulsa la acción principal y ve el resultado",
    start_url="/",
    steps=(QAStep(kind=QAStepKind.CLICK, target="#ping"),),
    expectations=(
        QAExpectation(kind=QAExpectationKind.VISIBLE, target="#pong", expected="resultado visible"),
    ),
)
```

Pasos: `navigate`, `click`, `fill`, `submit`, `wait`, `assert_visible`.
Expectativas: `visible`, `text_contains`, `url_matches`, `http_ok`, `no_console_errors`,
`no_js_exceptions`. El vocabulario es cerrado: un paso o una expectativa que el consumidor no sabe
ejecutar invalida el caso, no lo aprueba.

Los cinco escenarios canónicos viven en `punto.consumer_qa.cases` (`QA-001` carga, `QA-002`
navegación, `QA-003` interacción, `QA-004` formulario y `QA-005` experiencia rota) y están escritos
contra la aplicación de referencia `fixtures/consumer-qa-app`.

El viewport por defecto es el primero del contrato web (móvil, 390×844); otro tamaño se pide con el
argumento `viewport=` de `run_consumer_qa`.

## Cómo se ejecuta

```bash
.venv\Scripts\python.exe -m pytest tests/consumer_qa -q
```

Ese es el comando único. Arranca la aplicación de referencia dentro del sandbox, la abre con
Chromium, interactúa, compara y conserva la captura. El directorio de casos sigue funcionando con
`pytest tests/cases`, que incluye `CASE-015` (categoría `CONSUMER_QA`) y ejecuta un caso real.

Desde Python:

```python
from punto.consumer_qa import QATarget, case_by_id, run_consumer_qa

target = QATarget(
    workspace=app.parent,
    project_relative="app",
    preview_argv=(("python3", "-m", "http.server", "4173", "--bind", "0.0.0.0"),),
)
result = run_consumer_qa(case_by_id("QA-001"), target, evidence_dir=Path("evidencia"))
```

## Qué significa PASS / FAIL / SKIP

- `PASS`: la aplicación se abrió, respondió y cumplió todo lo declarado.
- `FAIL`: algo no se cumplió. El resultado trae `failures` con **qué se esperaba**, **qué se observó**
  y **dónde** (el paso o la expectativa exacta) y la evidencia de la sesión.
- `SKIP`: solo si quien llama declara explícitamente que una dependencia ambiental está permitida
  (`allow_environmental_skip=True`, por ejemplo la ausencia del sandbox de navegador). Por defecto
  **no** existe: un error de infraestructura es `FAIL`.

Nunca: «no pude ejecutarlo → PASS». Sin navegador disponible, con la aplicación que no arranca, con
la evidencia que no cuadra o con un caso inválido, el resultado es `FAIL`.

## Qué evidencia produce

Ante `FAIL` y ante `PASS`, la sesión deja: URL pedida y URL final, código HTTP, navegador y versión
de Playwright, acciones ejecutadas con su resultado, errores de consola, avisos contados,
excepciones, recursos que no cargaron y **una captura** verificada por hash.

- En `PASS` es la captura del estado final: la prueba de lo que se vio.
- En `FAIL` es la captura del punto de fallo, porque la interacción se detiene en la primera acción
  que no puede completarse.

Si la captura no se puede conservar, el resultado lo dice (`screenshot_note`) en vez de fingir que no
hubo ninguna.

## Relación con PELL

Un `FAIL` deja la información que PELL necesita para aprender del fallo, sin construir otro bucle:
`failure_as_experience(result)` describe `problem`, paso fallido, `observed`, `expected` y evidencia,
y `record_failure(store, result)` la registra como experiencia `FAILED` con la interfaz de PELL-0/1.

## Qué NO evalúa

Estética, diseño, color, tipografía, comparación de píxeles, puntuaciones de UX, accesibilidad
completa (solo lo que el navegador reporta de forma gruesa), matrices responsive completas,
multi-navegador, nube, dashboards, auto-arreglo y QA Consumer v1.
