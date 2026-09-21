# VISUAL_QA efectivo con OpenAI/Codex

Cierra `EVIDENCE_REQUIRED` por «no hay capacidad de QA visual con imágenes»: Anthropic **declara**
`VISION`, pero su transporte activo (`claude --print`) es texto y no la ejecuta. Codex, sí.

## 1. Capacidad real (prueba real, no supuesta)

Codex CLI 0.155 con la sesión ChatGPT ya configurada, `codex exec --image <FICHERO>`:

| Prueba | Resultado |
| --- | --- |
| Control sin imagen | `NO_IMAGE` |
| Escena de colores generada | acertó izquierda/derecha/franja/cuadrado |
| Captura real de navegador (Chrome headless) | título, departamento, botón, color y un código aleatorio (`QX-98305`) que solo estaba en los píxeles |
| Cadena completa de PUNTO (gate vivo) | `PASS` al código real, `FAIL` a uno falso, `UNCLEAR` a un hover |

Gate reproducible: `tests/integration/test_codex_vision_live.py` (falla con la causa si falta Codex con
sesión, navegador o `--image`; no hay `skip` ni PASS simulado).

## 2. Causa

1. `CodexTransport` declaraba `supports_images=False` y rechazaba adjuntos: se escribió cuando
   `codex exec` era solo texto.
2. El catálogo no declaraba `VISION` para OpenAI (la capacidad efectiva es declarada ∩ transporte).
3. VISUAL_QA solo se resolvía por asignación (`anthropic`), sin selección por capacidad efectiva.
4. **Aunque hubiera capacidad, el ciclo no producía evidencia**: `_visual_record` devolvía
   `NOT_VERIFIED` («no se aportó ninguna imagen»). No existía captura ni evaluación en el ciclo.

## 3. Arquitectura

- **Transporte** (`transports/codex.py`): imágenes acreditadas solo si `codex exec --help` anuncia
  `--image`. Los adjuntos se validan (límites multimodales), viajan como ficheros temporales privados
  (`--image`, antes de `--model`) y se borran al terminar. Sandbox `read-only` intacto.
- **Catálogo**: `VISION` declarada para OpenAI; la efectividad la decide el transporte.
- **Selección por capacidad efectiva** (`providers/router.py`, `failover.py`): `resolve_route` juzga al
  asignado y a los sustitutos declarados con el evaluador del registro (conexión + capacidad efectiva).
  Con política de failover para VISUAL_QA y una petición con imágenes, un asignado sin capacidad
  efectiva **no se ejecuta**: se pasa al sustituto (`CAPABILITY_MISSING`). Sin ruta → fallo cerrado.
- **Configuración**: `failover.roles.VISUAL_QA: [openai]` en `config/providers.yaml`. El asignado
  (`anthropic`) no cambia. Claude BUILDER sigue text-only y sin herramientas.
- **Evidencia** (`visualqa/dev_evidence.py`): el ciclo captura la app **renderizada** (Chrome/Edge
  headless, solo URLs de bucle local declaradas en el destino: `visual: {routes, viewport}`), la evalúa
  la ruta efectiva de VISUAL_QA y valida el veredicto con un contrato cerrado
  (`PASS`/`FAIL`/`UNCLEAR`; todo lo dudoso es `UNCLEAR`).
- **Verificación**: `PASS` → criterio `SATISFIED`; `FAIL` → `UNSATISFIED` (reparable); `UNCLEAR` o sin
  captura/ruta → `NOT_VERIFIED` → `EVIDENCE_REQUIRED`.
- **Gobernanza**: `DevelopmentResult.visual_evidence` (persistido con la Task): criterio, capturas con
  huella, proveedor/modelo/transporte efectivos, veredicto, `request_id` (id de la Task) y
  `applied_digest` del cambio. `TaskAttempt.visual` lo enlaza con el intento. Auditoría
  `DEV_VISUAL_CAPTURED` / `DEV_VISUAL_ASSESSED`.

## 4. Límites (no se relajan)

- Una captura estática solo demuestra lo que **se ve**. Un criterio de interacción (pasar el cursor)
  sale `UNCLEAR` y sigue exigiendo evidencia; capturar estados de interacción es trabajo aparte.
- La captura es del servidor de bucle local que el destino declare (p. ej. `next dev`); PUNTO no lo
  arranca. Sin `visual.routes` en el destino no hay captura.
- Sin autoridad nueva: el veredicto no aprueba gates, no escribe y no publica.
