"""Registra en PELL el aprendizaje de propagación de evidencia accionable al BUILDER (VERIFIED).

Misma cadena causal que EVIDENCE MODALITY ROUTING; sin abrir auditoría aparte.
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
            "un criterio STRUCTURAL_CONSISTENCY=FAILED (correcto, real, con duplicación real en "
            "el repositorio) entraba en el bucle de reparación ya existente (correcto), pero la "
            "ronda de reparación terminaba en CHANGES_EMPTY repetido hasta agotar el presupuesto "
            "y cerrar en VERIFICATION_FAILED — como si el BUILDER no tuviera nada que corregir"
        ),
        context=(
            "la evidencia SÍ llegaba al BUILDER (claim_issues alimenta failure_evidence, que sí "
            "entra en el prompt), pero en forma incompleta y activamente engañosa: (1) "
            "ClaimRecord.remedy — el cambio concreto que cierra el hueco, ya calculado por el "
            "verificador — se descartaba antes de construir el BuildValidationIssue "
            "(solo viajaba f'{kind}: {evidence}', nunca el remedio); (2) el texto fijo que "
            "acompañaba a todo CLAIM_NOT_SATISFIED decía, sin condición, 'aporta un dataset "
            "autoritativo' — una instrucción escrita para el caso cartográfico que, para "
            "cualquier otro tipo de criterio (estructural incluido), le dice al BUILDER que "
            "traiga algo que no viene al caso"
        ),
        attempts=(),
        failure_reason="",
        solution=(
            "propagar el remedio ya calculado por el verificador (item.remedy) en el detail de "
            "cada CLAIM_NOT_SATISFIED, y sustituir la instrucción fija (específica de dataset) "
            "por una que remite al REMEDY de cada línea sin presuponer de qué trata el defecto — "
            "cambio general en la construcción del issue y del prompt, no específico de ningún "
            "ClaimKind"
        ),
        procedure=(
            "cuando un verificador calcula un campo de remedio/instrucción concreta pero el "
            "consumidor solo propaga la evidencia (el hecho medido), revisar si ese remedio se "
            "pierde antes de llegar a quien tiene que actuar — la evidencia por sí sola dice QUÉ "
            "está mal, pero el actor necesita también QUÉ HACER",
            "un texto de instrucción fijo compartido entre varios tipos de fallo (aquí: "
            "cartográfico y estructural comparten el mismo mensaje CLAIM_NOT_SATISFIED) no puede "
            "presuponer detalles de UNO solo de esos tipos (aquí: 'dataset'); o se generaliza el "
            "texto o cada tipo aporta su propio remedio explícito y el texto solo lo referencia",
            "antes de aceptar 'CHANGES_EMPTY repetido = nada que reparar', comprobar qué prompt "
            "recibió de verdad el BUILDER (un doble que registra el prompt, no solo el "
            "resultado) — un CHANGES_EMPTY ante evidencia genuinamente accionable es una señal de "
            "que la evidencia no llegó bien, no de que no había nada que hacer",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_structural_repair_evidence.py (7 pruebas): el prompt de reparación "
            "contiene REMEDY y los ficheros implicados (y ya no 'authoritative dataset'); "
            "reparación real produce SATISFIED; un BUILDER que sigue sin actuar ante evidencia "
            "accionable NO se acepta como reparado; sin duplicación real es SATISFIED de entrada "
            "sin ninguna reparación; misma Task/mismo ciclo; reinicio conserva el resultado; "
            "mutación documental sobre la construcción del detail",
            "confirmado por reversión temporal: test_1 falla sin el fix (el prompt no contiene "
            "REMEDY) y pasa con él",
            "regresión relacionada (evidence_modality_routing, evidence_repair_loop, dev_cycle, "
            "acceptance, visual_interaction, visual_qa_effective, noop_reconciliation, "
            "human_console, effective_capabilities, repair_guard, repair_domain) en verde",
        ),
        tags=(
            "type:repair-evidence-propagation",
            "trigger:builder-returns-changes-empty-despite-actionable-failed-claim",
            "component:orchestrator/dev_cycle",
            "provenance:deterministic-test+prompt-inspection",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
