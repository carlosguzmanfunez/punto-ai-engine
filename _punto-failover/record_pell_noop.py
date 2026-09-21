"""Registra en PELL el aprendizaje reutilizable de la reconciliación de un no-op (VERIFIED)."""

from __future__ import annotations

from pathlib import Path

MEMORIA = Path(__file__).resolve().parent / "pell-noop-reconciliation.jsonl"


def main() -> int:
    """Registra el aprendizaje y reporta el resultado."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    guardado = store.record(
        problem=(
            "un resultado vacío del productor (cero cambios) se interpreta como fallo aunque el "
            "estado actual ya satisfaga la tarea, o —peor— como éxito sin comprobarlo"
        ),
        context=(
            "El BUILDER devolvió cero cambios porque el objetivo ya estaba materializado; el ciclo "
            "lo trató como CHANGES_EMPTY y agotó rondas hasta VERIFICATION_FAILED sin medir nada. "
            "Aceptar la respuesta vacía sin medir habría sido igual de incorrecto."
        ),
        attempts=("reintentar pidiendo cambios al BUILDER",),
        failure_reason="la ausencia de cambios no decía nada del estado real",
        solution=(
            "reconciliar: medir el estado actual con la misma cadena que un cambio normal "
            "(extraída a un único método) y completar solo si todo pasa; registrar ALREADY_SATISFIED "
            "con la evidencia y sin commit vacío; cualquier FAIL/UNCLEAR mantiene el fallo cerrado"
        ),
        procedure=(
            "cero cambios nunca es éxito por sí solo ni fallo por sí solo: se mide",
            "reutilizar la misma medición que el flujo normal (una sola fuente de verdad)",
            "exigir al menos una verificación ejecutada y que los recursos del plan estén "
            "como el plan los describe",
            "no fabricar commits ni conceder autoridad de release a algo sin commit",
            "medir una vez por estado: repetir sobre lo mismo da el mismo veredicto",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_noop_reconciliation.py::test_1_cero_cambios_con_el_estado_ya_satisfecho_completa_como_no_op_verificado",
            "tests/test_noop_reconciliation.py::test_2_cero_cambios_con_el_estado_sin_satisfacer_falla_cerrado",
            "tests/test_noop_reconciliation.py::test_5_cero_cambios_con_el_criterio_visual_en_fail_o_unclear_no_completa",
            "audit:DEV_NOOP_RECONCILED",
        ),
        tags=(
            "type:idempotent-noop",
            "trigger:changes-empty",
            "component:orchestrator/dev_cycle",
            "provenance:deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {guardado.id} {guardado.status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
