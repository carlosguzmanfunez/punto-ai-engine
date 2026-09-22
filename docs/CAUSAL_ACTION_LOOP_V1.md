# Causal action loop V1 (continuación de EVIDENCE_REPAIR_LOOP_V0)

`34b263d` demostró que el presupuesto/clasificación funcionaban, pero un retry `INCONCLUSIVE`
volvía a pedir la misma observación (misma página, mismo estado, mismo encuadre). Este cierre
convierte el retry en **recuperación activa**: actúa sobre la causa, no repite.

## `punto.orchestrator.evidence_recovery`

- `EvidenceGap` — hueco estructurado: `claim`, `evidence_class`, `signal` (`"unclear-evidence"`,
  determinista, no texto libre), `detail` (la observación real, acotada).
- `EvidenceAction` — acción concreta con su **materialidad**: una firma (`("viewport", w, h)`,
  `("interaction", (nombres...))`) que dice qué cambia de verdad respecto al intento anterior.
- `next_action(target, is_interaction_claim, tried)` — la siguiente acción **no probada aún**,
  derivada de lo que el destino ya declara (nunca de un proyecto):
  1. `EXPAND_FRAMING`: encuadre ampliado (×1.6, tope 4000px) — más contenido visible en una sola
     captura; cubre "fuera de viewport" / "requiere scroll" / "encuadre insuficiente".
  2. `RUN_INTERACTION`: evidencia de la interacción declarada del destino, si la hay y el criterio
     no era ya de interacción — cubre "estado/control no activado".
  3. `None`: no queda ninguna acción distinta → se detiene, aunque quede presupuesto.

## `DevelopmentCycle._evidence_recovery`

Por cada retry (solo si hay un criterio `INCONCLUSIVE` pendiente): diagnostica el hueco, pide la
siguiente acción, y si la hay, la **ejecuta de verdad** (`_verify_semantic_claims` con
`viewport_override` o `force_interaction`, nueva captura real, nuevo veredicto real). Si `next_action`
devuelve `None`, dejar de reintentar es la decisión correcta — la regla anti-repetición: un intento
que no cambia nada material no cuenta como recuperación.

`FAILED` no pasa por aquí (bucle de reparación ya existente, sin cambios). `CAPABILITY_UNAVAILABLE`
y `EVIDENCE_TECHNICAL_FAILURE` tampoco (nada que la acción pueda cambiar).

## Auditoría / durabilidad

Sin segunda fuente: cada intento sigue en `DEV_CLAIMS_EVALUATED` (ya existente), con
`evidence_action` (la acción de ESE intento, o `null` en la línea base) en su metadata. Reconstruye
`Criterion → EvidenceAttempt → EvidenceGap(implícito en la clasificación) → EvidenceAction →
Evidence → Evaluation` sin un almacén nuevo.

`BlockedEvidence.rule` distingue `evidence-strategies-exhausted` (se acabó la escalera) de
`evidence-budget-exhausted` (se acabó el presupuesto) y `capability-unavailable`.

Pruebas: `tests/test_evidence_repair_loop.py` (10, incluidas materialidad/anti-repetición/durabilidad).
