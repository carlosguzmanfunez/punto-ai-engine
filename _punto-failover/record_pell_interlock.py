"""Registra en PELL el aprendizaje reutilizable del interlock de re-ejecución (VERIFIED).

Mismas reglas que ``record_pell_failover.py``: solo lo demostrado con pruebas deterministas y solo lo
reutilizable. Escribe ``pell-console-interlock.jsonl`` (no toca la memoria por defecto del motor).
"""

from __future__ import annotations

from pathlib import Path

MEMORIA = Path(__file__).resolve().parent / "pell-console-interlock.jsonl"


def main() -> int:
    """Registra el aprendizaje y reporta el resultado."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    guardado = store.record(
        problem=(
            "un interlock de ejecución que se libera después de que el resultado ya es visible da "
            "rechazos espurios a quien reacciona a ese resultado"
        ),
        context=(
            "Interlock por Task para POST /run: el intento se cerraba (visible en el historial) y el "
            "finally liberaba el interlock después de persistir. Un cliente que esperaba el intento "
            "cerrado y reanudaba de inmediato recibía un 409 durante unos milisegundos."
        ),
        attempts=("liberar solo en el finally, después de persistir",),
        failure_reason="la ventana entre «resultado observable» e «interlock libre» no era vacía",
        solution=(
            "liberar el interlock en el mismo punto en que el resultado pasa a ser observable y dejar "
            "el finally como red de seguridad idempotente; la exclusión sigue siendo una toma atómica "
            "(comprobar y tomar bajo un lock), nunca un botón deshabilitado"
        ),
        procedure=(
            "tomar el interlock antes de mutar cualquier estado (etapa, contadores, intento)",
            "liberarlo al cerrar el intento y de nuevo en finally (idempotente)",
            "mantenerlo solo en memoria: un reinicio no deja bloqueos huérfanos",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_console_rerun_interlock.py::test_2b_solicitudes_casi_simultaneas_admiten_exactamente_una",
            "tests/test_console_workspace_process.py::test_el_resultado_del_ultimo_intento_sobrevive_al_reinicio",
            "tests/test_console_rerun_interlock.py::test_5_el_interlock_no_se_persiste_y_un_reinicio_deja_la_task_libre",
        ),
        tags=(
            "type:concurrency-interlock",
            "trigger:duplicate-rerun-request",
            "component:api/console",
            "provenance:deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {guardado.id} {guardado.status.value}: {guardado.problem[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
