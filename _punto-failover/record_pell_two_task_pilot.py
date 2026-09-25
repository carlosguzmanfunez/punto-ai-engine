"""Registra el aprendizaje reusable y verificado del piloto E2E de Fase 14 (dos Tasks reales)."""

from __future__ import annotations

from pathlib import Path

MEMORY = Path(__file__).resolve().parent / "pell-two-task-pilot.jsonl"


def main() -> int:
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORY)
    record = store.record(
        problem=(
            "con consola y scheduler escribiendo el mismo documento durable, una escritura de la "
            "consola regresaba una Task managed (COMPLETED -> RUNNING, sin resultado) y el restart "
            "del scheduler re-ejecutaba su DevelopmentCycle; a la inversa, el scheduler borraba "
            "Tasks y gates creados por la consola después de su arranque"
        ),
        context=(
            "ConsoleStateStore único (instantánea completa, temporal + os.replace); la consola y "
            "el TwoTaskScheduler cargan el documento al arrancar y persisten instantáneas "
            "completas desde su memoria; el restart requeuea un RUNNING huérfano si su intención "
            "de despacho no está IN_FLIGHT"
        ),
        attempts=(),
        failure_reason=(
            "dos escritores de instantáneas completas sobre un documento: cada uno reescribía los "
            "registros del OTRO desde la copia tomada al arrancar (lost update entre procesos). "
            "El exclusivo F13R (instantánea+escritura bajo el mismo lock) no lo cubre: ordena "
            "escrituras de UN escritor, no las copias viejas de otro. Y el restart trataba un "
            "RUNNING cuyo ciclo del intento vigente ya constaba APPLIED como si nunca hubiera "
            "corrido: la regresión durable se convertía en doble ejecución"
        ),
        solution=(
            "ConsoleStateStore.save_owned: cada escritor reescribe SOLO lo que gobierna "
            "(scheduler: managed=True; consola: el resto y los gates) y conserva lo ajeno tal "
            "como está en disco, releído bajo una exclusión por documento; y el scheduler lleva a "
            "DISPATCH_RECONCILIATION_REQUIRED un RUNNING huérfano cuyo intento vigente ya es "
            "APPLIED, en vez de requeuearlo"
        ),
        procedure=(
            "si varios componentes persisten instantáneas completas del mismo documento, "
            "particionar por propiedad: nadie escribe desde memoria registros cuya verdad es de "
            "otro",
            "el orden de escrituras de un escritor (F13R) no protege contra la copia vieja de otro "
            "escritor: probar ambos sentidos con el otro escritor vivo",
            "un efecto APPLIED sin desenlace durable es tan ambiguo como uno IN_FLIGHT: "
            "reconciliación explícita, nunca repetición a ciegas",
            "un piloto E2E encuentra interacciones que los mini-pilotos por fase no ven: montar la "
            "consola MIENTRAS el scheduler corre y reiniciar después",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_multitask_phase14_pilot.py: 12 PASS + 1 xfail estricto (GAP F14-G1); "
            "escenarios 9 y 10 rojos sin el fix",
            "tests/test_multitask_phase14_discriminants.py: 13/13 fallos inyectados CAUGHT "
            "(10 exigidos + 7b/10b/10c del fix) y defensa en profundidad IN_FLIGHT",
            "cono causal 67 módulos: 1429 PASS, 1 xfail G1; 2 fallos preexistentes idénticos "
            "en base (test_qa_service_dependency, entorno Docker); ruff; mypy strict",
        ),
        tags=(
            "type:durability-multi-writer-phase14",
            "trigger:two-full-snapshot-writers-same-document",
            "component:api/console_state.save_owned+console.persist+scheduler._persist",
            "component:scheduling/task_scheduler._reconcile_orphans",
            "provenance:e2e-pilot+fault-injection",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {record.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
