"""Registra en PELL el aprendizaje del MINI-PILOT 1 (VERIFIED): dos reconciliaciones de
reinicio independientes, ninguna hace el trabajo de la otra. Sin abrir auditoría aparte.
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
            "Multi-Task v0 (Fases 1-5) tiene DOS mecanismos de reconciliación tras un reinicio, "
            "cada uno cerrado y verificado por separado: "
            "ResourceWaitCoordinator.reconcile_persisted (Fase 5, reevalúa el overlay de "
            "scheduling -- WAITING_RESOURCE/QUEUED -- desde ConsoleStateStore) y "
            "LeaseLedger.reconcile (Fase 2A, expira por reloj y reporta leases ajenos vigentes "
            "como BUSY). Ninguna suite de fase individual los ejercita juntos contra un ledger y "
            "un TaskWorkspace reales en el mismo escenario de reinicio, así que no había "
            "evidencia directa de si uno delega en el otro o si ambos son necesarios"
        ),
        context=(
            "MINI-PILOT 1 monta el escenario real de reinicio (Task A bloqueando con un "
            "TaskWriterLease ACTIVE + workspace real; Task B en WAITING_RESOURCE) y, tras "
            "simular el reinicio con objetos completamente nuevos (ledger, manager, "
            "coordinator, identidad de executor nueva), llama primero a reconcile_persisted "
            "(para el overlay de B) y por separado a ledger.reconcile(holder) (para el lease "
            "de A)"
        ),
        attempts=(),
        failure_reason="",
        solution=(
            "confirmado empíricamente: reconcile_persisted NUNCA toca el LeaseLedger -- su "
            "llamada interna a _evaluate() no pasa task_token/provider_token (a diferencia de "
            "evaluate() cuando SÍ se le pasan explícitamente), así que un reinicio que solo "
            "llama a reconcile_persisted deja cualquier lease vigente-pero-huérfano "
            "exactamente como estaba, ni expirado ni reportado. Un recuperador de reinicio "
            "real necesita invocar los DOS mecanismos por separado: reconcile_persisted para "
            "el estado de espera/elegibilidad de cada Task, y ledger.reconcile(nuevo_holder) "
            "para descubrir leases que un proceso anterior dejó vigentes y que ahora son BUSY "
            "frente a la nueva identidad de proceso"
        ),
        procedure=(
            "cuando dos mecanismos de recuperación de reinicio pertenecen a fases distintas y "
            "cada una se prueba con fixtures ligeros propios, no asumir que uno delega en el "
            'otro solo porque comparten el mismo evento ("restart") -- verificarlo con un '
            "escenario real que use ambos objetos concretos (aquí: un LeaseLedger de verdad, "
            "no una versión sintética) y confirme explícitamente qué toca cada llamada y qué "
            "dejaría intacto si se omitiera",
            "una identidad de executor nueva tras un reinicio debe pasarse explícitamente "
            "(executor_id=uuid4()) -- holder_from_executor_ref memoiza una identidad por PID "
            "cuando no se le da una, así que dos holders 'distintos' construidos sin ese "
            "argumento en el mismo proceso de test colapsan silenciosamente en el mismo "
            "executor_id, lo que esconde exactamente la distinción de identidad que un "
            "reinicio necesita demostrar",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_multitask_phase5_minipilot.py (5 pruebas): conflicto físico real "
            "(2 TaskWriterLease + 2 TaskWorkspace + 2 worktrees Git aislados) -> "
            "WAITING_RESOURCE sin failure/Human Gate/Attempt/invocación de proveedor -> "
            "token viejo cercado -> A libera -> B reevaluada a QUEUED -> reevaluación "
            "repetida idempotente -> B readquiere (epoch nuevo, nunca reutiliza el viejo) y "
            "escribe de verdad; reinicio simulado con objetos 100% nuevos preserva "
            "WAITING_RESOURCE (misma fingerprint/blockers/workspace, sin duplicar Task ni "
            "worktree) y, por separado, ledger.reconcile confirma el lease de A todavía BUSY "
            "frente a la nueva identidad; recursos independientes nunca esperan; un conflicto "
            "lógico (contract:Property) se detecta pese al aislamiento físico completo; "
            "múltiples blockers exigen que TODOS dejen de bloquear antes de READY",
            "regresión (Fases 1-5, 120 pruebas): test_scheduler_phase1_contracts, "
            "test_lease_ledger, test_lease_fencing, test_lease_enforcement, "
            "test_lease_restart, test_task_workspaces, test_resource_claim_conflicts, "
            "test_resource_waiting, mas el propio mini-pilot -- todas en verde, ningún "
            "defecto causal real encontrado (los 3 fallos iniciales fueron errores del "
            "fixture del piloto, no del motor: preexisting_paths() sin fijar antes de "
            "escribir, executor_id memoizado por PID reutilizado entre holders, y el "
            "alcance de pytest.raises mal ubicado respecto a dónde el fence revalida "
            "realmente)",
        ),
        tags=(
            "type:multitask-minipilot-restart-reconciliation",
            "trigger:two-independent-restart-recovery-mechanisms",
            "component:scheduling/resource_waits+scheduling/leases+scheduling/workspaces",
            "provenance:real-git-worktree-integration-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
