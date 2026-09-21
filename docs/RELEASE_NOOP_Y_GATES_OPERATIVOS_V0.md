# Release de un no-op verificado + Human Gates operativos V0

## A · Artefacto publicable de un no-op

Causa: toda la cadena de release identificaba lo publicable por `result.commit_sha` (el commit que
produce el ciclo). Un no-op verificado (`ALREADY_SATISFIED`) no tiene commit propio, así que el
botón, `POST /production-gate` (409 «no tiene commit local»), la evaluación de autoridad
(`commit_from_governed_task = UNKNOWN`) y la publicación no tenían sobre qué actuar.

Corrección: una **única** fuente de identidad, `DevelopmentResult.publishable_artifact`:

| origen | cuándo |
|---|---|
| `cycle-commit` | el ciclo hizo commit |
| `verified-head` | no-op cuyo estado verificado es exactamente el HEAD (`NoOpEvidence.verified_sha`) |
| `legacy-baseline` | no-op persistido antes de existir `verified_sha`: solo su baseline declarado, y sin cambios aplicados |

El ciclo fija `verified_sha` solo si HEAD == baseline del ciclo == baseline del destino, no hay
rutas cambiadas ni sin confirmar (salvo las preexistentes) y ninguna es recurso del plan. Si no, deja
`artifact_issue` con la causa y **no hay artefacto** (fail closed, nunca un commit «probable»).

Release: `commit_from_governed_task` exige SHA idéntico al del resultado y, en un no-op, que el HEAD
actual del destino siga siendo ese SHA. La publicación y la aprobación del gate lo vuelven a
comprobar (409 sin registrar la decisión si divergió). El gate `deploy_production` es **idempotente**
y va ligado al SHA exacto; un gate pendiente de otro SHA pasa a `SUPERSEDED`.

## B · Historial frente a acciones pendientes

Nuevo estado terminal `SUPERSEDED` (ni aprobado ni rechazado). Transición: `HumanGate.supersede`
conserva la solicitud original y añade `superseded_by` (intento), `supersession_cause`, momento
(`resolved_at`) y actor (`punto-engine`), más el evento `HUMAN_GATE_SUPERSEDED`.

La obsolescencia la decide `punto.api.gate_reconciliation` **por tipo de gate** sobre el resultado
canónico (nunca por la etapa):

| gate | deja de ser accionable cuando |
|---|---|
| `PLAN_REQUIRES_HUMAN` / `PLAN_OUTSIDE_AUTHORITY` | un intento posterior validó el plan y `plan_apply` fue ALLOW |
| `CHANGE_*` / `HUMAN_GATE_REQUIRED` | un intento posterior completó sin volver a pedir persona |
| `EVIDENCE_REQUIRED` | evidencia posterior con criterios `SATISFIED` |
| `deploy_production` | el artefacto verificado cambió o ya no hay artefacto |

Tipo desconocido, gate del propio intento, tarea en ejecución o evidencia ausente ⇒ **sigue pendiente**.
Se reconcilia al cerrar un intento, al recuperar el estado y antes de aprobar/rechazar.

API: `GET /console/human-gates` devuelve `operational` (accionables, con botones) e `history`
(resto, sin acciones) además de `items`. `GET /console/tasks/{id}` añade `pending_gates` y
`next_human_action` (`decide_gate` | `request_production_gate` | `release_autonomous` | `none`).

Pruebas: `tests/test_release_chain_and_gate_reconciliation.py` (incluye el estado durable real de la
Task 2e7822a0 como fixture).
