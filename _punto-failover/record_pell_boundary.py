"""Registra en PELL el aprendizaje reutilizable de la frontera proveedor → propuesta (VERIFIED).

Mismas reglas que ``record_pell_failover.py``: solo lo demostrado con pruebas deterministas y solo lo
reutilizable. Escribe ``pell-proposal-boundary.jsonl`` (no toca la memoria por defecto del motor).
"""

from __future__ import annotations

from pathlib import Path

MEMORIA = Path(__file__).resolve().parent / "pell-proposal-boundary.jsonl"


def main() -> int:
    """Registra el aprendizaje y reporta el resultado."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    guardado = store.record(
        problem=(
            "un límite contractual que el proveedor no conoce invalida una respuesta semánticamente "
            "válida por una simple anotación demasiado larga"
        ),
        context=(
            "FileChangeProposal limita acceptance_criterion (300) y reason (400). El contrato del "
            "prompt no lo decía y maxLength no puede viajar en el json_schema (el dialecto de "
            "proveedor no lo admite), así que un proveedor capaz podía exceder el límite y todo el "
            "cambio caía con CHANGE_INVALID."
        ),
        attempts=(),
        failure_reason="el límite existía solo en la validación, nunca en lo que ve el productor",
        solution=(
            "declarar los límites en el contrato del prompt derivándolos del propio modelo, y "
            "ajustar de forma determinista y registrada (huella y longitudes) solo los campos "
            "descriptivos; nunca path, contenido ni operación, que siguen fallando cerrado"
        ),
        procedure=(
            "cuando un límite no pueda viajar en el schema, escribirlo en el contrato de texto",
            "derivar el número del modelo, no copiarlo, para que contrato y validación no diverjan",
            "normalizar solo texto que no decide qué se escribe; conservar el comienzo y auditar",
            "todo lo que decide qué se escribe falla cerrado sin normalizar",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_proposal_boundary.py::test_2_un_criterio_mayor_de_300_se_ajusta_y_el_modelo_lo_acepta",
            "tests/test_proposal_boundary.py::test_5_cualquier_otro_defecto_sigue_fallando_cerrado",
            "tests/test_proposal_boundary.py::test_8_el_ciclo_completa_con_un_criterio_largo_y_lo_deja_registrado",
            "audit:DEV_PROPOSAL_NORMALIZED",
        ),
        tags=(
            "type:provider-boundary",
            "trigger:change-invalid-string-too-long",
            "component:orchestrator/proposal_boundary",
            "provenance:deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {guardado.id} {guardado.status.value}: {guardado.problem[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
