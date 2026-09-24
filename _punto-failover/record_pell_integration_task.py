"""Registra el aprendizaje reusable y verificado de Fase 12 (Integration Task)."""

from __future__ import annotations

from pathlib import Path

MEMORY = Path(__file__).resolve().parent / "pell-integration-task.jsonl"


def main() -> int:
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORY)
    record = store.record(
        problem=(
            "outputs de Tasks paralelas debían combinarse en un resultado verificado sin merge "
            "directo al producto, sin tocar las Tasks fuente y sin tratar un merge textual limpio "
            "como compatibilidad"
        ),
        context=(
            "scheduler F11 con DAG operacional, TaskWorkspace por Task, EffectLedger, "
            "TakeoverChangeArtifact/GitTakeoverWorkspace de F10 y ResourceClaims de F4"
        ),
        attempts=(),
        failure_reason=(
            "guardar el tipo de Task en el overlay de scheduling se perdía: los coordinadores de "
            "espera reconstruyen ese overlay campo a campo; y leer outputs de workspaces vivos o "
            "aceptar un merge textual limpio no prueba ni procedencia ni compatibilidad"
        ),
        solution=(
            "la Integration Task es un TaskRecord real (kind=INTEGRATION en la Task, omitido al "
            "serializar cuando es DEVELOPMENT) que depende de sus fuentes y entra al mismo "
            "scheduler; consume commits inmutables base..commit, integra en su workspace propio "
            "desde la base segura con intent durable, y falla cerrado ante evidencia no VERIFIED, "
            "bases distintas, hunks incompatibles o superficies de contrato compartidas"
        ),
        procedure=(
            "poner atributos de identidad de la Task en el registro que los coordinadores copian "
            "entero, nunca en un overlay que reconstruyen",
            "leer outputs de objetos Git inmutables del destino, jamás del worktree vivo",
            "orden canónico por task_id y merge-file para hunks disjuntos: el orden de "
            "finalización no cambia el árbol resultante",
            "contrato primero: WRITE compartido sobre dimensiones de interfaz o cambios de varias "
            "fuentes en rutas de contrato son conflicto aunque Git combine limpio",
            "restore_base antes de aplicar e intent reversible por intento; un intent anterior "
            "sin resolver se sella FAILED y nunca se da por aplicado; resultado por huella",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_integration_task.py: 16 PASS (A-V)",
            "tests/test_multitask_phase12_minipilot.py: 4 PASS con DevelopmentCycles reales",
            "14/14 mutaciones CAUGHT en vivo, motivos verificados",
            "regresión causal 600 PASS (1 fallo ambiental preexistente en base); ruff/mypy strict",
        ),
        tags=(
            "type:integration-task-phase12",
            "trigger:parallel-task-outputs-need-verified-integration",
            "component:project/integration+scheduling/task_scheduler+takeover_resolution",
            "provenance:discriminants+real-cycle-minipilot+live-mutations+causal-regression",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {record.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
