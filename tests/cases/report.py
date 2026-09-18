"""Reporte compacto de una ejecución del directorio de casos (CASE DIRECTORY v0).

No hay dashboard: una tabla de texto para la terminal y un JSON estable para quien lo consuma. Las
duraciones no entran en el JSON determinista salvo que se hayan medido a propósito.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from cases.model import CaseResult, CaseStatus, EngineCase

#: Resultados registrados por la ejecución en curso, para el resumen de la terminal.
_RECORDED: list[CaseResult] = []


def record(result: CaseResult) -> None:
    """Registra un resultado para el reporte final de la sesión."""
    _RECORDED.append(result)


def recorded() -> tuple[CaseResult, ...]:
    """Resultados registrados hasta ahora, en orden de ejecución."""
    return tuple(_RECORDED)


def clear() -> None:
    """Olvida los resultados registrados (lo usa la propia suite de validación)."""
    _RECORDED.clear()


def render_table(results: Sequence[CaseResult], cases: Sequence[EngineCase]) -> str:
    """Tabla ``CASE | CATEGORÍA | RESULTADO`` con el resumen de la ejecución."""
    categories = {case.case_id: case.category.value for case in cases}
    titles = {case.case_id: case.title for case in cases}
    width = max([len("CASE")] + [len(result.case_id) for result in results])
    lines = [
        f"{'CASE'.ljust(width)} | {'CATEGORÍA'.ljust(20)} | RESULTADO | GARANTÍA",
        f"{'-' * width}-+-{'-' * 20}-+-----------+{'-' * 40}",
    ]
    for result in results:
        lines.append(
            f"{result.case_id.ljust(width)} | {categories.get(result.case_id, '?').ljust(20)} | "
            f"{result.status.value.ljust(9)} | {titles.get(result.case_id, '')}"
        )
    passed = sum(1 for result in results if result.status is CaseStatus.PASS)
    failed = sum(1 for result in results if result.status is CaseStatus.FAIL)
    skipped = sum(1 for result in results if result.status is CaseStatus.SKIP)
    lines.append(f"total: {len(results)} · PASS {passed} · FAIL {failed} · SKIP {skipped}")
    for result in results:
        if result.status is not CaseStatus.PASS:
            lines.append(f"{result.case_id} {result.status.value}: {result.reason}")
    return "\n".join(lines)


def render_json(results: Sequence[CaseResult]) -> str:
    """Reporte JSON estable, ordenado por claves, con los resultados tal cual."""
    payload = [result.as_dict() for result in sorted(results, key=lambda item: item.case_id)]
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


__all__ = ["clear", "record", "recorded", "render_json", "render_table"]
