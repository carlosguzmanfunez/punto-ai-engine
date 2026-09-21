"""Registra en PELL dos aprendizajes causales reutilizables (VERIFIED, con prueba determinista).

Mismas reglas que ``record_pell_failover.py``. Escribe ``pell-release-gates.jsonl``.
"""

from __future__ import annotations

from pathlib import Path

MEMORIA = Path(__file__).resolve().parent / "pell-release-gates.jsonl"


def main() -> int:
    """Registra los aprendizajes y reporta el resultado."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    a = store.record(
        problem=(
            "una cadena de release identifica lo publicable por «el commit que produjo el ciclo» y "
            "un no-op verificado (sin commit propio) queda sin artefacto aunque exista un HEAD "
            "exactamente verificado"
        ),
        context=(
            "Task ALREADY_SATISFIED: DEVELOPMENT_COMPLETED sin commit. El botón, /production-gate, "
            "la evaluación de autoridad (commit_from_governed_task=UNKNOWN) y la publicación "
            "exigían result.commit_sha; el dashboard mostraba «Aprobación de producción» sin nada "
            "que pudiera abrirla."
        ),
        attempts=("fabricar un commit vacío para tener SHA",),
        failure_reason="un commit vacío inventa un artefacto que nadie verificó",
        solution=(
            "una sola fuente de identidad del artefacto (DevelopmentResult.publishable_artifact: "
            "commit del ciclo | verified-head del no-op | legacy-baseline) que el ciclo fija solo "
            "si HEAD==baseline==baseline del destino y no hay cambios sin confirmar ni plan sobre "
            "ficheros sin confirmar; release exige SHA idéntico y HEAD actual; falla cerrado"
        ),
        procedure=(
            "buscar todos los sitios que dan por hecho que «artefacto == commit del ciclo»",
            "centralizar la identidad en el resultado; nunca inferir el SHA en el consumidor",
            "en un no-op comprobar HEAD vs baseline vs destino y el árbol antes de ofrecer SHA",
            "volver a comprobar HEAD al publicar: si divergió, no publicar (sin resolver el gate)",
            "reproducir el defecto sobre el código anterior (409) antes de arreglar",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_release_chain_and_gate_reconciliation.py::test_b_ y test_c1/c2/c4",
            "mutaciones (sha publicable, comprobación de HEAD en release y en publicación) "
            "detectadas",
        ),
        tags=(
            "type:artifact-identity",
            "trigger:noop-has-no-commit-for-release",
            "component:schemas/dev+target_authority+console",
            "provenance:deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    b = store.record(
        problem=(
            "los Human Gates pendientes de intentos anteriores siguen como acciones humanas aunque "
            "la evidencia posterior demostró que su condición desapareció, y contaminan el tablero"
        ),
        context=(
            "La Task tenía PLAN_REQUIRES_HUMAN y EVIDENCE_REQUIRED PENDING; el intento 13 validó el "
            "plan (plan_apply=ALLOW) y obtuvo la evidencia (criterios SATISFIED). Aprobar/rechazar "
            "no era correcto: nadie decidió nada."
        ),
        attempts=("marcarlos APPROVED/REJECTED o borrarlos", "resolver por cambio de etapa"),
        failure_reason="reescribe la historia o resuelve por una señal que no es la condición",
        solution=(
            "estado terminal propio SUPERSEDED (ni aprobado ni rechazado) con constancia "
            "(intento posterior, causa, momento) y evento de auditoría; predicado por tipo de gate "
            "sobre el resultado canónico; tipo desconocido o evidencia ausente ⇒ sigue pendiente; "
            "reconciliar al terminar el intento, al recuperar y antes de decidir; el tablero "
            "operativo lista solo lo accionable y el historial va aparte sin acciones"
        ),
        procedure=(
            "separar historial (inmutable) de acciones pendientes (proyección del estado canónico)",
            "derivar la obsolescencia de la condición concreta del gate, no de la etapa",
            "fail-safe: sin predicado o sin evidencia posterior el gate permanece pendiente",
            "idempotente: un reinicio no resucita ni duplica; solo PENDING puede superarse",
            "una decisión humana sobre un gate superado se rechaza (409) sin tocar su estado",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_release_chain_and_gate_reconciliation.py::test_d/e/f/g/i/j",
            "estado durable real de la Task: 2 gates PENDING -> SUPERSEDED (intento 13), "
            "historial y auditoría conservados",
            "mutaciones (evidencia, plan, gate del propio intento, aprobar histórico) detectadas",
        ),
        tags=(
            "type:gate-projection",
            "trigger:stale-pending-gates",
            "component:api/gate_reconciliation+human_gate",
            "provenance:deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id} / {b.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
