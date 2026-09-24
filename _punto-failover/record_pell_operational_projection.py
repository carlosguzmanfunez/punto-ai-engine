"""Registra el aprendizaje reusable y verificado de Fase 13 (proyección operacional)."""

from __future__ import annotations

from pathlib import Path

MEMORY = Path(__file__).resolve().parent / "pell-operational-projection.jsonl"


def main() -> int:
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORY)
    record = store.record(
        problem=(
            "el estado operacional Multi-Task (activas, esperas y su causa, bloqueos, "
            "dependencias, Integration Tasks, capacidad) no era visible en API/dashboard, y el "
            "panel de providers repetía avisos de capacidad sin distinguir configurada de efectiva"
        ),
        context=(
            "TwoTaskScheduler F11 persistiendo TaskRecord+TaskSchedulingRecord con WaitReasons "
            "tipadas en el documento de la consola; Integration Task F12 (kind en la Task); "
            "effective_capabilities_table; página única dashboard.html"
        ),
        attempts=(),
        failure_reason=(
            "la consola reconstruía ConsoleTask campo a campo y omitía kind: una Integration Task "
            "recuperada se re-persistía como DEVELOPMENT sin error de validación"
        ),
        solution=(
            "proyección pura sobre los TaskRecord durables (GET /console/operations relee el "
            "documento en cada consulta, load(quarantine=False), sin escribir nada): estado real, "
            "etiqueta derivada no persistida, resumen de espera más el WaitReason estructurado, "
            "aristas solo desde hechos durables, orden y huella deterministas; capacidades "
            "limitadas contadas por capacidad solo en providers CONNECTED sin tocar su estado"
        ),
        procedure=(
            "el grafo operacional es proyección, no autoridad: el scheduler jamás lo importa",
            "cada causa de espera se deriva del WaitReason durable; sin waiting no hay causa",
            "BUSY es WAITING_PROVIDER, nunca failure ni indisponibilidad del provider",
            "capacidad configurada != efectiva; una limitación no cambia CONNECTED",
            "el resumen compacto no borra el detalle causal: siempre acompaña el motivo completo",
            "todo round-trip campo a campo de un registro durable debe copiar cada campo de "
            "identidad (kind): probarlo con restore->persist",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_operational_projection.py: 21 PASS (A-V + regresión kind + linaje)",
            "tests/test_multitask_phase13_minipilot.py: 4/4 PASS sobre scheduler F11 real, "
            "consola real y página real en Node",
            "tests/test_multitask_phase13_mutations.py: 12/12 mutaciones CAUGHT, motivo verificado",
            "cono causal 334 PASS (1 carrera preexistente en base, misma tasa); ruff; mypy strict",
        ),
        tags=(
            "type:operational-projection-phase13",
            "trigger:multitask-state-not-observable",
            "component:api/operational_projection+api/console+providers/effective+dashboard",
            "provenance:discriminants+real-scheduler-minipilot+live-mutations+causal-regression",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {record.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
