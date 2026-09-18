# PELL-0 — Memoria práctica de experiencia

PUNTO empieza a **recordar cómo resolvió problemas** para no repetir investigación, errores ni ciclos
ya resueltos. Es memoria, no autoridad: la recuperación se hace con etiquetas y palabras (determinista,
sin embeddings ni servicios externos) y el almacén es un fichero JSONL local que sobrevive reinicios.

```
problema → intento → fallo → causa → corrección → éxito verificado → procedimiento guardado
        → memoria persistente → problema similar futuro → consulta → conocimiento recuperado
```

## Qué recuerda

Una experiencia (`punto.memory.ExperienceMemory`) es conocimiento **resumido y estructurado**:

| Campo | Qué guarda |
| --- | --- |
| `problem` / `context` | qué se intentó resolver y en qué contexto aplica |
| `attempts` / `failure_reason` | qué se intentó antes y por qué no funcionó |
| `solution` / `procedure` | la corrección aplicada y el procedimiento reutilizable, paso a paso |
| `result` / `verification` | el resultado y la evidencia que lo demuestra |
| `tags` | etiquetas para la recuperación |
| `status` / `schema_version` | estado del conocimiento y versión del esquema |

No se guardan volcados de `stdout`/`stderr`/prompts: los campos se acotan y se rechaza cualquier texto
con forma de credencial o cabecera de autorización (`ExperienceSecretError`).

## Cómo registra un fracaso

```python
store.record(
    problem="cerrar F633 usando clasificación semántica amplia",
    attempts=("ampliar el vocabulario", "añadir marcas de producto"),
    failure_reason="las paráfrasis arquitectónicas seguían clasificándose como tácticas",
    result=ExperienceResult.FAILED,
    status=ExperienceStatus.FAILED,
    tags=("f633", "clasificador"),
)
```

`FAILED` **se conserva a propósito**: evita repetir caminos que ya sabemos que no funcionan. Un fracaso
sin causa se rechaza: no enseña nada.

## Cómo registra un éxito

```python
candidata = store.record(problem=..., solution=..., procedure=..., tags=...)
store.verify(candidata.id, evidence=("test inline", "test de bloque", "E2E", "control positivo"))
```

## Cómo verifica

Sin evidencia no hay conocimiento confiable: `CANDIDATE` es lo registrado y aún no demostrado,
`VERIFIED` exige evidencia declarada (tests, E2E, auditoría o resultado verificable) y solo `VERIFIED`
se considera reutilizable. `SUPERSEDED` conserva historia válida que fue sustituida.

## Cómo busca

```python
store.search("dependencia Go no detectada")            # problema nuevo
store.search("go.mod", statuses=(ExperienceStatus.VERIFIED,))
store.search(problema, context="proyecto Go", tags=("containment",))
```

Puntuación determinista: etiquetas (peso 2) y palabras del problema, del contexto, de la solución y del
procedimiento (peso 1). Se devuelven **primero las `VERIFIED`** —conocimiento demostrado—, después los
`FAILED` relevantes —qué caminos no repetir— y al final las `SUPERSEDED`. Sin coincidencia real no se
devuelve nada: la memoria no adivina.

## Cómo consolida

La identidad del conocimiento es el problema normalizado (huella `sha256` corta). Al registrar:

- misma huella y mismo estado ⇒ **fusión** en el registro existente (procedimiento, intentos,
  verificación y etiquetas se unen; se conserva la fecha más antigua);
- misma huella con estado distinto ⇒ se conservan **ambas** experiencias, porque «esto falló» y «esto
  funcionó» son dos hechos distintos y los dos sirven.

No hay clustering ni umbrales difusos: es una huella y una unión de conjuntos.

## Cómo se recuperará el conocimiento después

La API es pequeña a propósito: `record`, `search`, `verify`, `mark_failed`, `update_status`, `get`,
`list`. En PELL-0 se usa desde código y desde las pruebas; más adelante se conectará al razonamiento
de los agentes (planner y developer) para **consultar** antes de resolver.

## Invariante: MEMORIA ≠ AUTORIDAD

Una experiencia `VERIFIED` **jamás** puede ampliar capabilities, saltar el Human Gate, modificar un
`ResourceSet`, reconciliar una violación de arquitectura, cambiar políticas ni ejecutar código por sí
sola. El paquete no importa ninguna pieza de autoridad del motor y su API pública no tiene verbos de
ejecución; la memoria **aconseja**, el motor decide.

## Estados

`CANDIDATE` · `VERIFIED` · `FAILED` · `SUPERSEDED` (solo `VERIFIED` es reutilizable con confianza).

## Persistencia

JSONL local (por defecto `./.punto-memory/experiences.jsonl`, configurable con `PUNTO_PELL_PATH`), con
escritura atómica y lectura estricta: un registro de otra versión o una línea ilegible **fallan
claramente**, no se interpretan como memoria vacía.
