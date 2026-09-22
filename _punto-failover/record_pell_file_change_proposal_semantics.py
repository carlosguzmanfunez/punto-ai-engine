"""Registra el aprendizaje verificado de semántica operacional de FileChangeProposal."""

from __future__ import annotations

from pathlib import Path

MEMORIA = Path(__file__).resolve().parent / "pell-active-evidence-recovery.jsonl"


def main() -> int:
    """Guarda únicamente la regla causal general demostrada por pruebas."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    experience = store.record(
        problem=(
            "un FileChangeProposal semánticamente válido podía morir como CHANGE_INVALID cuando "
            "un proveedor de salida estructurada materializaba source_path opcional como cadena "
            "vacía, antes de que el schema juzgara si la operación necesitaba un origen"
        ),
        context=(
            "la validación compartida de path/source_path intentaba convertir source_path='' en "
            "una ruta y fallaba primero; el contrato además mostraba source_path en el objeto "
            "genérico sin declarar que solo aplica a RENAME/MOVE. El parser colapsaba la causa "
            "en CHANGE_INVALID y el agotamiento del ciclo podía reemplazarla por el genérico "
            "VERIFICATION_FAILED"
        ),
        attempts=(),
        failure_reason=(
            "el carácter opcional de source_path se validaba sintácticamente antes que su "
            "semántica por operación, y la causa estructurada no sobrevivía al cierre del ciclo"
        ),
        solution=(
            "declarar en el contrato que source_path se omite para CREATE/MODIFY/DELETE y es no "
            "vacío para RENAME/MOVE; normalizar determinista y auditadamente el vacío a ausencia "
            "solo donde no aplica; rechazar path/origen realmente ausentes con códigos causales "
            "antes de apply y devolverlos al mismo ciclo con presupuesto estructural acotado"
        ),
        procedure=(
            "validar los campos condicionales después de conocer el discriminante de operación, "
            "no como si fueran obligatorios para todas las variantes",
            "normalizar solo representaciones equivalentes sin ambigüedad; una ruta requerida "
            "ausente nunca se inventa a partir del contenido, motivo o contexto",
            "hacer que contrato, schema, parser, feedback y resultado terminal conserven la misma "
            "semántica y el mismo código causal",
            "probar cada operación y una mutación de ambas direcciones: no exigir origen donde no "
            "aplica y no aceptarlo vacío donde sí aplica",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_file_change_proposal_semantics.py: CREATE/MODIFY/DELETE, RENAME/MOVE, "
            "payload reparable, payload ambiguo y contrato",
            "tests/test_proposal_preflight.py: feedback estructurado al mismo ciclo y cierre "
            "causal "
            "al agotarse",
            "tests/test_structural_repair_evidence.py: quality takeover con MODIFY/source_path='' "
            "normalizado -> cambio material -> verificación -> nueva evidencia estructural",
            "regresión relacionada: 242 pruebas en verde; ruff y mypy --strict en verde",
        ),
        tags=(
            "type:provider-boundary",
            "trigger:empty-optional-source-path",
            "component:schema+proposal-boundary+dev-cycle",
            "provenance:raw-provider-payload+deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {experience.id} {experience.status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
