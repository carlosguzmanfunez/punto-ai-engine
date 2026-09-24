"""Registra en PELL el aprendizaje de Fase 6 (VERIFIED): DEPENDENCY antes que RESOURCE en un
mismo coordinador, y el "limpiar la espera" debe reconocer TODAS las causas de espera que
gobierna, no solo la primera que existió. Sin abrir auditoría aparte.
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
            "Fase 6 añade WAITING_DEPENDENCY como una SEGUNDA causa de espera gobernada por el "
            "mismo ResourceWaitCoordinator de Fase 5 (que hasta ahora solo conocía "
            "WAITING_RESOURCE). Al conectar 'dependencias satisfechas -> cae al bloque de "
            "recursos existente -> si no hay conflicto, _ready()', _ready() seguía decidiendo "
            "'¿había algo que limpiar?' mirando *solo* _resource_reason(task) (isinstance "
            "ResourceWaitReason) -- así que una Task que salía de WAITING_DEPENDENCY directa a "
            "READY (sin pasar nunca por WAITING_RESOURCE) veía previous=None, tomaba el atajo "
            "'nada que hacer' y se devolvía SIN CAMBIAR: outcome=READY pero "
            "state=WAITING_DEPENDENCY intacto -- un resultado inconsistente que ningún test de "
            "Fase 4/5 podía haber revelado, porque DependencyWaitReason no existía todavía"
        ),
        context=(
            "detectado por el propio discriminante F/G de Fase 6 (prerequisito satisfecho, sin "
            "conflicto de recursos, debía terminar en QUEUED) -- no por inspección de código: la "
            "aserción sobre evaluation.task.scheduling.state falló con exactamente ese patrón "
            "(READY con state todavía en WAITING_DEPENDENCY), confirmando que _ready() nunca se "
            "disparaba para esta Task"
        ),
        attempts=(),
        failure_reason=(
            "_ready() y _wait() ya tenían el patrón correcto de 'accessor específico por tipo' "
            "(_resource_reason) desde que solo existía una causa de espera; al añadir una "
            "segunda (DependencyWaitReason) sin auditar todos los puntos que decidían "
            "'¿hay algo que limpiar/reconciliar?' contra ESE accessor estrecho, la ruta que solo "
            "necesita saber '¿había CUALQUIER espera antes?' (_ready, la salida común de ambas) "
            "quedó ciega a la causa nueva"
        ),
        solution=(
            "_ready() pasa a usar _wait_reason(task) (accessor unificado: ResourceWaitReason O "
            "DependencyWaitReason, la que aplique) para decidir si hay algo que limpiar, y luego "
            "distingue por isinstance() solo para elegir qué par de eventos de auditoría emitir "
            "(RESOURCE_WAIT_* vs DEPENDENCY_WAIT_*) -- la decisión estructural (¿limpiar o no?) "
            "es unificada; el detalle de auditoría (¿cuál?) sigue siendo específico por tipo. "
            "_wait() y _wait_dependency() correctamente SIGUEN usando su accessor específico "
            "(_resource_reason / _dependency_reason respectivamente), porque su pregunta es "
            "distinta: no '¿había algo?' sino '¿el fingerprint de ESTA MISMA causa cambió?' -- "
            "transicionar de una causa a la otra es, correctamente, una espera nueva (waiting_"
            "since/generation reinician), nunca una continuación"
        ),
        procedure=(
            "cuando un mecanismo compartido gana una segunda variante de una misma familia "
            "(aquí: una segunda subclase de WaitingReason), cada método que antes decidía "
            "'¿hacer algo?' preguntando por el accessor específico de la ÚNICA variante que "
            "existía debe reauditarse uno por uno: los que preguntan '¿había ALGO (cualquier "
            "variante) antes?' necesitan el accessor unificado; los que preguntan '¿ESTA MISMA "
            "variante cambió?' deben seguir con el accessor específico -- confundir los dos roles "
            "en cualquier dirección produce, o bien ceguera silenciosa (este caso), o bien "
            "comparar fingerprints de dos causas distintas como si fueran la misma espera",
            "un dependent con dependencias pero SIN ResourceClaims declarados choca con una regla "
            "de Fase 5 ya deliberada y ya probada (test_w_insufficient_claims_are_neither_ready_"
            "nor_normal_wait): managed=True + resources=() es INSUFFICIENT_CLAIMS, nunca READY "
            "vacuo -- no es un hueco de Fase 6, es una Task de prueba incompleta; todo fixture de "
            "Fase 6 que espera llegar a READY tras satisfacer dependencias necesita declarar al "
            "menos un ResourceClaim propio, exactamente como ya lo exigía Fase 5",
            "la auto-dependencia queda rechazada por TRES caminos independientes que fallan "
            "cerrado de formas distintas pero igualmente seguras: el chequeo explícito "
            "(SELF_DEPENDENCY), MISSING_DEPENDENCY si la Task ni siquiera está en el universo "
            "conocido, o DEPENDENCY_CYCLE si el detector general la ve como un ciclo de longitud "
            "1 -- confirmado por mutación: desactivar el chequeo explícito con la Task presente "
            "en el universo degrada el código de error pero NO abre la puerta (sigue fallando "
            "cerrado, solo que como DEPENDENCY_CYCLE); redundancia deliberada, no duplicación",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_dependency_wait.py (18 discriminantes A-X del encargo de Fase 6) + "
            "tests/test_multitask_phase6_minipilot.py (2 pruebas, escenario literal "
            "contract:Property, ambos casos: sin conflicto -> READY, con conflicto -> "
            "WAITING_RESOURCE) en verde",
            "regresión (Fases 1-5, 140 pruebas): test_scheduler_phase1_contracts, "
            "test_resource_waiting, test_resource_claim_conflicts, "
            "test_multitask_phase5_minipilot, test_lease_ledger, test_lease_fencing, "
            "test_lease_enforcement, test_lease_restart, test_task_workspaces -- ninguna tocada, "
            "todas en verde tras el rewire de _evaluate()/_ready()",
            "mutación confirmada en vivo (revertida tras cada verificación): desactivar el gate "
            "de dependencias rompe A/O; desactivar el detector de ciclos rompe K/L; desactivar "
            "release_authority en _wait_dependency rompe S/T; desactivar el chequeo explícito de "
            "auto-dependencia con la Task presente en el universo degrada SELF_DEPENDENCY a "
            "DEPENDENCY_CYCLE (sigue fail-closed, confirma la redundancia deliberada) -- la "
            "propia F/G ya sirvió de mutación real: reveló _ready() ciego a DependencyWaitReason "
            "antes de escribir ninguna prueba adicional",
        ),
        tags=(
            "type:task-dependency-wait-phase6",
            "trigger:second-waiting-reason-variant-added-to-shared-clear-path",
            "component:scheduling/resource_waits+scheduling/task_dependencies+schemas/scheduling",
            "provenance:discriminant-test-caught-during-development+live-mutation-check",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
