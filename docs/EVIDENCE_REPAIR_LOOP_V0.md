# Autonomous evidence + repair loop V0

General para cualquier destino/criterio (nada hardcodeado de un proyecto).

## Clasificación (`punto.acceptance.EvidenceClass`)

Fijada **en el punto donde se produce el registro** (`_visual_record` / `_cartographic_record`),
nunca inferida después por texto:

| Clase | Cuándo |
|---|---|
| `SATISFIED` | la evidencia demuestra el criterio |
| `FAILED` | la evidencia demuestra que **no** se cumple (`FAIL`, dataset no usado) |
| `INCONCLUSIVE` | un revisor con capacidad efectiva vio la evidencia y no pudo decidir (`UNCLEAR`) |
| `EVIDENCE_TECHNICAL_FAILURE` | la herramienta falló antes de llegar a un revisor (selector no localiza el elemento, interacción no demostrable) |
| `CAPABILITY_UNAVAILABLE` | ninguna ruta autorizada y efectiva puede producir la evidencia |

`RETRYABLE_EVIDENCE_CLASSES = {INCONCLUSIVE}`. `EVIDENCE_TECHNICAL_FAILURE` se produce **sin**
llamar a ningún proveedor (un selector que no localiza nada da el mismo resultado determinista cada
vez); `CAPABILITY_UNAVAILABLE` no tiene ruta que reintentar. Solo `INCONCLUSIVE` —un juicio real de
un revisor con capacidad— tiene sentido reintentar.

## Presupuesto (`DevelopmentConfig.max_evidence_attempts`, por defecto 2)

Distinto de `max_repair_rounds`: no consume una ronda del BUILDER, porque no hace falta cambiar
código para reintentar evidencia. `DevelopmentCycle._evidence_recovery` re-invoca **solo**
`_verify_semantic_claims` (nueva captura real + veredicto real), llevando la observación del
intento anterior como guía al siguiente (`assess_visual_claims(..., guidance=...)`): la estrategia
se deriva del propio criterio, no de una regla por proyecto.

## Flujo

```
_evaluate_state → EVIDENCE_REQUIRED?
  no  → sigue el flujo normal (COMPLETED / repair loop si FAILED)
  sí  → _evidence_recovery (hasta max_evidence_attempts, solo si hay clase INCONCLUSIVE pendiente)
          SATISFIED/FAILED tras recuperar → sigue el flujo normal (sin gate)
          EVIDENCE_REQUIRED tras agotar presupuesto → BLOCKED con BlockedEvidence rico
```

`FAILED` nunca pasó por aquí: ya alimentaba el bucle de reparación existente (`claim_issues` →
`change_issues` → siguiente ronda del BUILDER), sin cambios — es la misma arquitectura de siempre.

## Auditoría del bloqueo

`BlockedEvidence` (ya existente, reutilizada): `rule` (`evidence-budget-exhausted` /
`capability-unavailable`), `resource` (criterio pendiente), `remedy`, `detail` (clasificación +
traza de intentos). La traza completa y durable es el evento `DEV_CLAIMS_EVALUATED`, uno por
intento (nada nuevo: reutiliza la auditoría que ya existía).

## Reconciliación del gate

Sin cambios: `gate_reconciliation._assess_evidence` ya superaba un `EVIDENCE_REQUIRED` cuando un
intento posterior medía `claims_result == SATISFIED`. Con la recuperación autónoma, la mayoría de
los `EVIDENCE_REQUIRED` se resuelven **dentro del mismo intento** y nunca llegan a crear gate.

## Failover de capacidad

Sin cambios: `_visual_capability()` ya resuelve la ruta efectiva por capacidad (router + failover);
si el primario no tiene VISION, el sustituto configurado la aporta. `CAPABILITY_UNAVAILABLE` solo
ocurre cuando ninguna ruta autorizada la tiene.

Pruebas: `tests/test_evidence_repair_loop.py`.
