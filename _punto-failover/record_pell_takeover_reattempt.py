"""Registra en PELL el aprendizaje del TAKEOVER REAL DEL INTENTO 10 (VERIFIED). Misma cadena
causal que BUILDER TAKEOVER (3c48db6) y QUALITY TAKEOVER POLICY (0a963c0); sin abrir auditoría
aparte.
"""

from __future__ import annotations

from pathlib import Path

MEMORIA = Path(__file__).resolve().parent / "pell-active-evidence-recovery.jsonl"


def main() -> int:
    """Registra el aprendizaje y reporta el resultado."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    a = store.record(
        problem=(
            "con TAKEOVER ya separado de FAILOVER y priorizando Codex (0a963c0), la Task real "
            "0983a418 volvió a cerrar su intento 10 en CHANGES_EMPTY / "
            "DEVELOPMENT_VERIFICATION_FAILED, pese a que el takeover SÍ se disparó y SÍ seleccionó "
            "al candidato correcto (Codex/openai): Codex hizo un cambio real (SALVAGE, 4 ficheros) "
            "que no bastó para pasar typecheck/property-types, la ronda siguiente volvió a caer "
            "por failover operativo en el mismo proveedor que ya había dado CHANGES_EMPTY antes, y "
            "ese segundo CHANGES_EMPTY cerró el ciclo en vez de ofrecer una segunda oportunidad"
        ),
        context=(
            "reconstruido por auditoría durable (/audit/events?resource=<task>), sin asumir nada: "
            "DeepSeek (primario nominal de BUILDER) estuvo CREDITS_EXHAUSTED todo el ciclo, así "
            "que quien respondía siempre era Anthropic vía failover OPERATIVO; el TAKEOVER de "
            "CALIDAD sí excluyó correctamente a 'anthropic' (no a 'deepseek', el rol nominal) y "
            "seleccionó 'openai' según la política declarada. La causa no estaba en la selección "
            "del candidato (eso ya funcionaba) sino en tres defectos posteriores que impedían "
            "aprovechar un takeover que sí había producido progreso real"
        ),
        attempts=(
            "revisar solo la política de selección de candidato (TakeoverPolicy/providers.yaml): "
            "descartado por evidencia directa de auditoría — el candidato elegido (openai) ya era "
            "el correcto según la política declarada; el defecto estaba río abajo, en qué se hace "
            "con su resultado",
        ),
        failure_reason=(
            "tres defectos combinados, cada uno necesario para reproducir el fallo real: "
            "(1) builder_tried se llenaba con CUALQUIER proveedor que respondiera, no solo con "
            "quien dejaba CHANGES_EMPTY — un proveedor que sí propuso un cambio real (aunque "
            "insuficiente) quedaba excluido para siempre de futuras recuperaciones, como si su "
            "intento real fuera indistinguible de una respuesta vacía; "
            "(2) max_builder_takeovers por defecto era 1 — un ciclo real puede legítimamente "
            "producir más de un CHANGES_EMPTY distinto (el mismo proveedor operativo puede volver "
            "a fallar en calidad tras la ronda de otro) y el presupuesto no dejaba margen; "
            "(3) el gate de causa raíz (ad05280, `if rounds > 0: if not root_cause: ...`) corría "
            "ANTES de que _proposals() determinara CHANGES_EMPTY y sin eximir una respuesta "
            "genuinamente vacía, interceptándola como CHANGE_WITHOUT_ROOT_CAUSE antes de que "
            "pudiera llegar siquiera a la reconciliación/TAKEOVER; "
            "(4) el gate 'accionable' que decide si un CHANGES_EMPTY merece TAKEOVER solo miraba "
            "ClaimRecords semánticos (STRUCTURAL_CONSISTENCY, etc.), nunca verificaciones de "
            "COMANDO (typecheck, property-types) — un ciclo donde el claim semántico ya quedó "
            "SATISFIED pero una verificación de comando sigue fallando no activaba un segundo "
            "TAKEOVER aunque el propio `gap` (`_noop_gap`, ya computado para negar el cierre en "
            "no-op) ya demostraba una causa concreta y describible"
        ),
        solution=(
            "cuatro correcciones puntuales, todas en dev_cycle.py: "
            "(1) builder_tried.add(result.provider) se movió de justo-después-de-_invoke() a "
            "justo-después-de-_proposals(), condicionado a `proposal_issue.code == 'CHANGES_EMPTY'` "
            "— un proveedor que propuso algo real, aunque PUNTO lo rechace o no baste, sigue "
            "siendo candidato legítimo para el SIGUIENTE takeover; "
            "(2) DevelopmentConfig.max_builder_takeovers pasa de 1 a 2 por defecto; "
            "(3) el gate de causa raíz gana una condición `and not changes_empty` (el mismo "
            "predicado que usa _proposals: `not isinstance(raw, list) or not raw`), calculada "
            "antes del gate para no reordenar el resto del bloque; "
            "(4) `accionable` pasa de `any(claim.unsatisfied and claim.remedy for claim in "
            "state.claim_records)` a `bool(gap)` — como el bloque solo se alcanza tras el `if not "
            "gap: return COMPLETED` de más arriba, `gap` no vacío YA es la prueba de que existe "
            "una causa concreta (verificación de comando fallida, cadena funcional, aceptación, o "
            "claim semántico), sin restringir el gate a una sola de esas cuatro fuentes"
        ),
        procedure=(
            "un conjunto de exclusión ('quién ya lo intentó y no cuenta más') debe alcanzar "
            "EXACTAMENTE la condición que lo motiva, nunca 'cualquiera que respondió' — si la "
            "condición real es 'respondió con éxito pero no produjo nada' (CHANGES_EMPTY), añadir "
            "al conjunto en cualquier otro punto del flujo (p. ej. justo tras invocar al "
            "proveedor, antes de saber si produjo algo) sobregeneraliza la exclusión y descarta "
            "candidatos que sí estaban actuando de buena fe",
            "un presupuesto de sub-mecanismo (aquí, cuántas veces se puede pedir un sustituto "
            "dentro del mismo ciclo) debe dimensionarse para el número de eventos DISTINTOS que "
            "razonablemente pueden ocurrir, no para 'al menos uno' — un ciclo real con failover "
            "operativo de fondo puede producir el mismo tipo de evento de calidad más de una vez "
            "por causas independientes",
            "un gate que exige justificación (aquí: causa raíz) para una reparación debe eximir "
            "explícitamente el caso donde NO hay reparación que justificar (una respuesta vacía) "
            "— si el gate corre antes de que el camino 'vacío' se determine y no lo exime, "
            "intercepta ese camino entero con un error genérico que oculta la clasificación real",
            "cuando ya existe una función que calcula 'la razón concreta por la que este estado "
            "no se puede declarar satisfecho' (aquí, _noop_gap para el cierre en no-op), un gate "
            "hermano que decide 'si vale la pena intentar recuperar este estado' debe reutilizar "
            "esa misma señal en vez de re-derivar una versión más estrecha con una sola de sus "
            "fuentes — la señal completa ya existe, calculada, en el mismo punto del código",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_takeover_reattempt.py (5 pruebas A-D + J, reproduce el intento 10 real "
            "con un 'sustituto' que repara dos veces — una insuficiente que rompe una "
            "verificación de comando, otra completa): un intento real insuficiente no excluye al "
            "proveedor de un segundo takeover; dos eventos DEV_BUILDER_TAKEOVER distintos quedan "
            "auditados con el mismo excluded=[primario]; el presupuesto por defecto es 2; un "
            "CHANGES_EMPTY genuino (el sustituto también responde vacío) sigue excluyendo y el "
            "ciclo falla explícito, no silenciosamente; mutación ancla: volver a excluir por "
            "cualquier respuesta rompe la reparación en dos pasos",
            "tests/test_structural_repair_evidence.py (46 pruebas, incluye el ajuste del default "
            "de max_builder_takeovers en el harness compartido de 1 a 2) en verde",
            "regresión: test_quality_takeover_policy, test_evidence_modality_routing, "
            "test_evidence_repair_loop, test_provider_failover, test_architect_failover, "
            "test_dev_cycle, test_proposal_preflight, test_file_change_proposal_semantics en "
            "verde junto con los ficheros anteriores",
            "ruff format + ruff check y mypy --strict limpios sobre dev_cycle.py",
        ),
        tags=(
            "type:takeover-reattempt-and-root-cause-gate",
            "trigger:changes-empty-after-real-but-insufficient-recovery-attempt",
            "component:orchestrator/dev_cycle",
            "provenance:live-audit-reconstruction+deterministic-test+mutation-check",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
