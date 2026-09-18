"""Carga y validación fail-closed de los casos del directorio (CASE DIRECTORY v0).

El cargador no tolera un directorio a medias: si un solo fichero es inválido —JSON ilegible,
esquema roto, ``expected`` desconocido, ``case_id`` duplicado o escenario inexistente— lanza
:class:`CaseDirectoryError` con **todos** los problemas juntos. Nunca devuelve una lista parcial:
un caso inválido no puede llegar a ejecutarse y, por tanto, nunca puede aparecer como ``PASS``.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from cases.model import CaseDirectoryError, EngineCase
from cases.scenarios import SCENARIOS

#: Directorio canónico de los datos de caso.
DEFAULT_CASES_DIR: Path = Path(__file__).resolve().parent / "data"


def _problems_of_validation(error: ValidationError) -> str:
    """Resumen legible de los errores de esquema de un caso."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()
    )


def load_cases(directory: Path | None = None) -> tuple[EngineCase, ...]:
    """Carga todos los casos del directorio, o falla si alguno es inválido.

    Args:
        directory: Directorio de ficheros ``*.json``; por defecto, ``tests/cases/data``.

    Raises:
        CaseDirectoryError: Si falta el directorio, está vacío, un fichero no se puede leer, su
            esquema es inválido, repite un ``case_id``, no se llama como su identificador o
            declara un escenario que no existe en el registro del runner.
    """
    root = DEFAULT_CASES_DIR if directory is None else Path(directory)
    if not root.is_dir():
        raise CaseDirectoryError(f"no existe el directorio de casos: {root}")

    paths = sorted(root.glob("*.json"))
    if not paths:
        raise CaseDirectoryError(f"el directorio de casos está vacío: {root}")

    cases: list[EngineCase] = []
    origins: dict[str, str] = {}
    problems: list[str] = []
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            problems.append(f"{path.name}: JSON ilegible ({error})")
            continue
        try:
            case = EngineCase.model_validate(raw)
        except ValidationError as error:
            problems.append(f"{path.name}: esquema inválido ({_problems_of_validation(error)})")
            continue
        if path.stem != case.case_id:
            problems.append(f"{path.name}: el fichero debe llamarse {case.case_id}.json")
        if case.case_input.scenario not in SCENARIOS:
            problems.append(
                f"{path.name}: runner inexistente {case.case_input.scenario!r} "
                f"(registrados: {sorted(SCENARIOS)})"
            )
        previous = origins.get(case.case_id)
        if previous is not None:
            problems.append(f"case_id duplicado: {case.case_id} en {previous} y {path.name}")
        origins[case.case_id] = path.name
        cases.append(case)

    if problems:
        detail = "\n  - ".join(problems)
        raise CaseDirectoryError(f"directorio de casos inválido ({root}):\n  - {detail}")
    return tuple(sorted(cases, key=lambda case: case.case_id))


__all__ = ["DEFAULT_CASES_DIR", "load_cases"]
