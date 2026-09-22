"""Registra en PELL (misma cadena causal que active-evidence-recovery) el aprendizaje del
dedup de Human Gates por acción, no por texto exacto del motivo (VERIFIED).
"""

from __future__ import annotations

from pathlib import Path

# Mismo almacén que record_pell_active_evidence_recovery.py: es la continuación del mismo
# cierre causal (951f589 → este commit), no una auditoría independiente.
MEMORIA = Path(__file__).resolve().parent / "pell-active-evidence-recovery.jsonl"


def main() -> int:
    """Registra el aprendizaje y reporta el resultado."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    a = store.record(
        problem=(
            "una Task real terminó con dos Human Gates EVIDENCE_REQUIRED pendientes a la vez "
            "para la misma acción sin resolver, en vez de uno solo"
        ),
        context=(
            "la recuperación activa de evidencia (misma cadena) redacta un motivo de bloqueo "
            "distinto en cada intento (observación capturada, conteo de intento); la "
            "reutilización de gate pendiente exigía igualdad TEXTUAL del motivo, así que un "
            "segundo run bloqueado por la MISMA causa creaba un gate nuevo en vez de reusar el "
            "que ya seguía pendiente"
        ),
        attempts=(
            "aumentar la ventana de reintentos antes de bloquear (no ataca la causa: el "
            "problema es la comparación de identidad del gate, no el presupuesto de evidencia)",
        ),
        failure_reason=(
            "el motivo mostrado es redacción del bloqueo, no la identidad de la causa: dos "
            "bloqueos de la misma tarea+acción sin resolver son la MISMA decisión humana "
            "pendiente aunque el texto varíe"
        ),
        solution=(
            "reutilizar un gate pendiente por (tarea, acción), nunca por igualdad de texto del "
            "motivo; refrescar el motivo mostrado al del bloqueo más reciente para que no quede "
            "obsoleto. Para sanar el rastro que el defecto ya dejó (gates duplicados "
            "preexistentes), una reconciliación idempotente agrupa los PENDING de una tarea por "
            "acción y SUPERSEDE (nunca aprueba/rechaza) todos menos el más reciente, con causa "
            "explícita 'duplicate_pending_gate' e historial íntegro"
        ),
        procedure=(
            "la identidad de un Human Gate operativo es (tarea, acción sin resolver), no el "
            "texto de su motivo: un ciclo autónomo que redacta explicaciones ricas y variables "
            "por intento no debe multiplicar gates por eso",
            "una reconciliación de gates duplicados debe ser una superación (SUPERSEDED) "
            "explícita y auditada, nunca un borrado ni una aprobación automática",
            "verificar la corrección contra el estado real vivo (GET, sin volver a ejecutar la "
            "Task) antes de darla por cerrada: aquí el propio recargado en caliente del servidor "
            "sanó los dos gates duplicados reales sin ninguna acción manual",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_evidence_repair_loop.py::test_7/test_8 (reutilización pese a motivo "
            "distinto; dedup de dos PENDING preexistentes a uno)",
            "tests/test_effective_capabilities.py::"
            "test_el_gate_de_evidencia_es_unico_correcto_y_durable sigue en verde sin cambios",
            "verificación en vivo (solo lectura) sobre la Task real 0983a418: los dos gates "
            "duplicados reales quedaron reconciliados (uno SUPERSEDED con "
            "duplicate_pending_gate, uno PENDING) sin aprobar/rechazar ni volver a ejecutar",
        ),
        tags=(
            "type:gate-reconciliation",
            "trigger:duplicate-pending-human-gate",
            "component:api/console",
            "provenance:deterministic-test+live-read-verification",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
