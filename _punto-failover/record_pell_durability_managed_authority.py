"""Registra el aprendizaje reusable y verificado de Fase 13R (durabilidad + autoridad managed)."""

from __future__ import annotations

from pathlib import Path

MEMORY = Path(__file__).resolve().parent / "pell-durability-managed-authority.jsonl"


def main() -> int:
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORY)
    record = store.record(
        problem=(
            "el documento durable de la consola podía quedarse con el intento anterior aunque el "
            "nuevo ya estuviera cerrado en memoria; y una solicitud equivalente podía absorberse "
            "en una Task managed=True y lanzar sobre ella el ciclo legacy de la consola"
        ),
        context=(
            "persist() de la consola: instantánea completa + ConsoleStateStore.save atómico "
            "(temporal + os.replace); handler de /run y worker del ThreadPoolExecutor persisten "
            "en paralelo; POST /console/tasks deduplica con find_equivalents"
        ),
        attempts=(),
        failure_reason=(
            "la instantánea se tomaba bajo state_lock pero save corría fuera: el handler (1 "
            "intento) terminaba de escribir después del worker (2 intentos) y nadie volvía a "
            "persistir (lost update trazado); la deduplicación no distinguía la autoridad "
            "operacional de la Task"
        ),
        solution=(
            "instantánea + escritura bajo la misma exclusión (las escrituras quedan en el orden de "
            "sus instantáneas, sin ledger ni reintentos, save sigue atómico y fail-closed); "
            "_canonical_equivalent excluye Tasks managed, una solicitud equivalente a una Task del "
            "scheduler se rechaza con 409 sin crear ni absorber, y rerun_block impide /run legacy"
        ),
        procedure=(
            "un escritor de instantáneas completas debe escribir bajo la misma exclusión con la "
            "que instantanea: si no, una instantánea vieja puede ganar la carrera",
            "probar la exclusión forzando la interleaving trazada con eventos acotados, tanto en "
            "la escritura como justo después de la instantánea",
            "un test de durabilidad espera al hecho durable; la memoria puede adelantarse a disco",
            "la autoridad managed se respeta en TODOS los caminos legacy: arranque, "
            "deduplicación de solicitudes y reejecución",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_console_persistence_order.py: 46 PASS (A, A2, B/C, D x40, E, F, G); sin "
            "fix: A rojo determinista y 23/40 lost updates en D",
            "test de carrera original 40/40 PASS (antes 6/40 fallos en base)",
            "tests/test_console_managed_authority.py H-L: 5 PASS, 4 rojos sin fix; K histórico "
            "igual",
            "4/4 mutaciones vivas CAUGHT sobre copia del árbol; cono causal 648 PASS; ruff; mypy "
            "strict",
        ),
        tags=(
            "type:durability-managed-authority-phase13r",
            "trigger:lost-durable-update-or-legacy-path-on-managed-task",
            "component:api/console.persist+create_console_task+rerun_block",
            "provenance:traced-interleaving+forced-order-discriminants+live-mutations",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {record.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
