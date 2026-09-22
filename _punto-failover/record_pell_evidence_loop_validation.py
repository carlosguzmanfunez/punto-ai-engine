"""Registra en PELL (misma cadena causal) el aprendizaje de la validación end-to-end del
AUTONOMOUS EVIDENCE + REPAIR LOOP v0 (VERIFIED).
"""

from __future__ import annotations

from pathlib import Path

# Mismo almacén que record_pell_active_evidence_recovery.py y
# record_pell_gate_reuse_by_action.py: continuación de la misma cadena, no una auditoría aparte.
MEMORIA = Path(__file__).resolve().parent / "pell-active-evidence-recovery.jsonl"


def main() -> int:
    """Registra el aprendizaje y reporta el resultado."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    a = store.record(
        problem=(
            "faltaba demostrar, con una prueba real de extremo a extremo, que un criterio visual "
            "FAILED (no INCONCLUSIVE) se repara de forma autónoma en la MISMA Task y el MISMO "
            "ciclo, sin pedir una persona solo para empezar a corregir algo ya autorizado"
        ),
        context=(
            "el bucle de reparación (``change_issues`` → ronda de reparación → reverificación) ya "
            "existía en el motor antes de esta cadena; lo que no estaba demostrado explícitamente "
            "es que un criterio semántico/visual FAILED entra por ese MISMO camino general — sin "
            "mecanismo nuevo, sin Human Gate para 'autorizar' la reparación — y que el resultado "
            "sale con la evidencia REAL reevaluada, no una suposición"
        ),
        attempts=(),
        failure_reason="",
        solution=(
            "no hizo falta ningún cambio de arquitectura: ``claim_issues`` de un criterio FAILED ya "
            "se suma a ``change_issues`` como cualquier otra verificación fallida (dev_cycle.py, "
            "la ronda de reparación existente), así que la reparación automática, la reverificación "
            "y la reevidencia ya ocurrían dentro del mismo ciclo. Lo que hacía falta era la prueba "
            "que lo demuestra con proveedores reales encadenados (FAIL → reparación real con causa "
            "raíz declarada → PASS) y una prueba de que el presupuesto/gate de evidencia sobrevive "
            "a un reinicio real del proceso (mismo fichero de estado durable)"
        ),
        procedure=(
            "antes de añadir un mecanismo nuevo para 'FAILED debe reparar solo', comprobar si el "
            "camino general ya existente (verificación fallida → ronda de reparación) ya lo cubre: "
            "una clasificación de evidencia nueva no necesita su propio bucle de reparación si ya "
            "alimenta la señal genérica que el motor ya sabe reparar",
            "una reparación automática dentro de la autoridad ya concedida exige causa raíz "
            "declarada (root_cause/evidence/expected_effect) igual que cualquier otra reparación: "
            "no es una excepción para criterios visuales",
            "validar la supervivencia a un reinicio montando la consola dos veces sobre el MISMO "
            "fichero de estado durable (la fixture ya lo aísla por prueba), no simulando el estado "
            "a mano",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_evidence_repair_loop.py::test_10 (reinicio conserva evidence_attempts y "
            "el gate único) y test_11 (FAIL real → reparación real con causa raíz → PASS real, "
            "sin gate, con el grafo Evidence→Evaluation→Repair→Verification→Evidence reconstruible "
            "desde DEV_CLAIMS_EVALUATED/DEV_REPAIR_STARTED)",
            "regresión relacionada: 178 pruebas en verde (evidence_repair_loop, "
            "effective_capabilities, release_chain_and_gate_reconciliation, human_console, "
            "task_consolidation_and_graph, visual_interaction, noop_reconciliation)",
            "verificación en vivo (solo lectura) sobre la Task real 0983a418: el gate de evidencia "
            "único permanece correctamente vigente pese a un fallo externo posterior (RATE_LIMIT) "
            "no relacionado con el criterio — el predicado de reconciliación no lo superó por eso, "
            "que es el comportamiento correcto (fail-safe: solo SATISFIED supera un gate de "
            "evidencia)",
        ),
        tags=(
            "type:evidence-repair-loop-validation",
            "trigger:failed-claim-must-autorepair-same-cycle",
            "component:orchestrator/dev_cycle+api/console",
            "provenance:deterministic-test+live-read-verification",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
