"""Semántica operacional de ``FileChangeProposal`` en la frontera del proveedor.

Pruebas de mutación del defecto causal: si se vuelve a validar ``source_path`` como una ruta
obligatoria antes de mirar la operación, CREATE/MODIFY/DELETE con ``""`` vuelven a fallar. Si se
relaja RENAME/MOVE, las pruebas de origen obligatorio fallan. Ninguna prueba depende de una Task,
un dominio de aplicación ni un proveedor concreto.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from punto.orchestrator.dev_cycle import BUILD_CONTRACT, DevelopmentCycle
from punto.orchestrator.proposal_boundary import normalize_change_shape
from punto.schemas.dev import FileChangeProposal


def _write(operation: str, *, source_path: str | None = None) -> dict[str, Any]:
    change: dict[str, Any] = {
        "path": "src/lib/catalog.ts",
        "operation": operation,
        "content": "export const catalog = ['one'];\n",
        "reason": "mantener el catálogo",
        "acceptance_criterion": "fuente canónica",
    }
    if source_path is not None:
        change["source_path"] = source_path
    return change


def _parse(payload: dict[str, Any]) -> tuple[tuple[FileChangeProposal, ...], Any]:
    """Invoca la frontera pura del ciclo sin montar proveedores ni workspace."""
    cycle = object.__new__(DevelopmentCycle)
    return cycle._proposals(payload)


@pytest.mark.parametrize("operation", ["CREATE", "MODIFY", "DELETE"])
def test_a_operaciones_sin_origen_aceptan_source_path_vacio_como_ausente(
    operation: str,
) -> None:
    """CREATE/MODIFY/DELETE usan ``path``; un opcional vacío no es una ruta a validar."""
    change = _write(operation, source_path="")
    if operation == "DELETE":
        change.pop("content")

    proposal = FileChangeProposal.model_validate(change)

    assert proposal.source_path is None


def test_b_modify_con_origen_no_vacio_se_rechaza_antes_de_apply() -> None:
    """Un origen real en MODIFY no se borra: contradice la operación y falla estructurado."""
    proposals, issue = _parse(
        {"changes": [_write("MODIFY", source_path="src/lib/other.ts")]}
    )

    assert proposals == ()
    assert issue is not None and issue.code == "CHANGE_SOURCE_PATH_FORBIDDEN"
    assert "MODIFY" in issue.detail and "omite el campo" in issue.detail


@pytest.mark.parametrize("operation", ["RENAME", "MOVE"])
@pytest.mark.parametrize("source_path", [None, "", "   "])
def test_c_rename_y_move_exigen_un_origen_real(
    operation: str, source_path: str | None
) -> None:
    """Una ausencia ambigua nunca se convierte en una ruta inventada."""
    change: dict[str, Any] = {
        "path": "src/lib/new.ts",
        "operation": operation,
        "reason": "mover el módulo",
    }
    if source_path is not None:
        change["source_path"] = source_path

    proposals, issue = _parse({"changes": [change]})

    assert proposals == ()
    assert issue is not None and issue.code == "CHANGE_SOURCE_PATH_REQUIRED"
    assert "no inventará una ruta" in issue.detail


def test_c2_delete_identifica_el_recurso_con_path_no_con_source_path() -> None:
    """DELETE no duplica la ruta: ``path`` es el fichero que se elimina."""
    proposal = FileChangeProposal.model_validate(
        {"path": "src/lib/obsolete.ts", "operation": "DELETE", "source_path": ""}
    )

    assert proposal.path == "src/lib/obsolete.ts" and proposal.source_path is None


def test_d_payload_reparable_se_normaliza_sin_mutar_el_original() -> None:
    """La reparación determinista solo elimina el opcional vacío y deja constancia."""
    payload = {"changes": [_write("MODIFY", source_path="  ")]}
    original = copy.deepcopy(payload)

    normalized, notes = normalize_change_shape(payload)

    assert payload == original
    assert "source_path" not in normalized["changes"][0]
    assert notes[0].location == "changes[0].source_path"
    assert notes[0].operation == "MODIFY"
    proposals, issue = _parse(dict(normalized))
    assert issue is None and len(proposals) == 1


def test_e_path_ambiguo_no_se_infiere() -> None:
    """Ni contenido, reason ni contexto autorizan a adivinar el destino."""
    payload = {"changes": [_write("CREATE")]}
    payload["changes"][0]["path"] = ""

    normalized, notes = normalize_change_shape(payload)
    proposals, issue = _parse(dict(normalized))

    assert notes == () and normalized["changes"][0]["path"] == ""
    assert proposals == ()
    assert issue is not None and issue.code == "CHANGE_PATH_REQUIRED"
    assert "no se puede inferir" in issue.detail


def test_f_move_valido_conserva_exactamente_el_origen() -> None:
    """La normalización no toca una ruta no vacía requerida por la operación."""
    payload = {
        "changes": [
            {
                "path": "src/new.ts",
                "operation": "MOVE",
                "source_path": "src/old.ts",
                "reason": "reorganizar",
            }
        ]
    }

    normalized, notes = normalize_change_shape(payload)
    proposals, issue = _parse(dict(normalized))

    assert normalized is payload and notes == () and issue is None
    assert proposals[0].source_path == "src/old.ts"


def test_h_contrato_declara_la_semantica_por_operacion() -> None:
    """Mutación contractual: el productor debe saber cuándo omitir y cuándo exigir el origen."""
    assert "source_path is REQUIRED and non-empty for RENAME/MOVE" in BUILD_CONTRACT
    assert "MUST be omitted for CREATE/MODIFY/DELETE" in BUILD_CONTRACT
    assert "DELETE identifies the file to remove with path" in BUILD_CONTRACT


def test_h_mutacion_schema_no_puede_exigir_source_path_a_create() -> None:
    """CREATE válido sin ``source_path`` fija el contrato del modelo, no solo la frontera."""
    proposal = FileChangeProposal.model_validate(_write("CREATE"))

    assert proposal.operation.value == "CREATE" and proposal.source_path is None
