"""API compacta del directorio de casos: cargar, ejecutar y comparar (CASE DIRECTORY v0).

```
load_cases()   -> los casos válidos del directorio, o error si alguno es inválido
run_case(caso) -> PASS / FAIL / SKIP con los hechos observados y el motivo
run_cases(...) -> los casos que pasan el filtro, ejecutados en orden estable
```

Nada de esto sustituye a pytest: el runner solo conserva **escenarios del sistema** y los compara
con lo declarado. La ejecución del motor es la del repositorio.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from cases.loader import load_cases
from cases.model import (
    CONTAINMENT_KEYS,
    CaseCategory,
    CaseResult,
    CaseStatus,
    Comparison,
    EngineCase,
)
from cases.scenarios import SCENARIOS


def compare(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> Comparison:
    """Compara lo declarado con lo observado.

    Las claves ``*_contains`` exigen contención (el hecho observado tiene que incluir **todos** los
    valores declarados); el resto exige igualdad exacta. Que el escenario no haya observado un hecho
    declarado es un **error de infraestructura**, no una expectativa incumplida: el caso promete
    algo que su escenario no puede demostrar.
    """
    missing: list[str] = []
    mismatches: list[str] = []
    for key, want in expected.items():
        base = key.removesuffix("_contains") if key in CONTAINMENT_KEYS else key
        if base not in observed:
            missing.append(base)
            continue
        got = observed[base]
        if key in CONTAINMENT_KEYS:
            faltan = [item for item in want if item not in got]
            if faltan:
                mismatches.append(f"{key}: faltan {faltan!r} en {list(got)!r}")
        elif got != want:
            mismatches.append(f"{key}: esperado {want!r}, observado {got!r}")

    if missing:
        return Comparison(
            ok=False,
            reason=f"el escenario no observó: {', '.join(sorted(set(missing)))}",
            infrastructure=True,
        )
    if mismatches:
        return Comparison(ok=False, reason="; ".join(mismatches))
    return Comparison(ok=True, reason="todos los hechos declarados coinciden")


def run_case(
    case: EngineCase,
    *,
    measure_duration: bool = False,
    workspace_root: Path | None = None,
) -> CaseResult:
    """Ejecuta un caso y devuelve su resultado estructurado.

    Un escenario que no existe, que revienta o que no observa lo declarado termina en ``FAIL`` de
    infraestructura: **nunca** en ``PASS``.

    Args:
        case: Caso a ejecutar.
        measure_duration: Si se mide el tiempo de pared. Por defecto no: la duración no es
            determinista y no forma parte del contrato del caso.
        workspace_root: Directorio donde crear el temporal del caso; por defecto, el del sistema.
    """
    expected = dict(case.expected)
    scenario = SCENARIOS.get(case.case_input.scenario)
    if scenario is None:
        return CaseResult(
            case_id=case.case_id,
            status=CaseStatus.FAIL,
            expected=expected,
            observed={},
            reason=f"runner inexistente: {case.case_input.scenario!r}",
            infrastructure=True,
        )

    root = Path(tempfile.mkdtemp(prefix=f"{case.case_id}-", dir=workspace_root))
    start = time.perf_counter()
    observation = None
    failure: str | None = None
    try:
        observation = scenario(root, dict(case.case_input.params))
    except Exception as error:  # cualquier fallo del escenario es infraestructura, nunca PASS
        failure = f"el escenario falló: {type(error).__name__}: {error}"
    finally:
        shutil.rmtree(root, ignore_errors=True)
    duration = int((time.perf_counter() - start) * 1000) if measure_duration else None

    if failure is not None:
        return CaseResult(
            case_id=case.case_id,
            status=CaseStatus.FAIL,
            expected=expected,
            observed={},
            reason=failure,
            duration_ms=duration,
            infrastructure=True,
        )

    assert observation is not None
    if observation.skip_reason is not None and observation.skip_reason.strip():
        return CaseResult(
            case_id=case.case_id,
            status=CaseStatus.SKIP,
            expected=expected,
            observed=dict(observation.facts),
            reason=observation.skip_reason,
            duration_ms=duration,
        )

    comparison = compare(expected, observation.facts)
    return CaseResult(
        case_id=case.case_id,
        status=CaseStatus.PASS if comparison.ok else CaseStatus.FAIL,
        expected=expected,
        observed=dict(observation.facts),
        reason=observation.note if comparison.ok else comparison.reason,
        duration_ms=duration,
        infrastructure=comparison.infrastructure,
    )


def run_cases(
    cases: Sequence[EngineCase] | None = None,
    *,
    categories: Iterable[CaseCategory] | None = None,
    tags: Iterable[str] | None = None,
    case_ids: Iterable[str] | None = None,
    measure_duration: bool = False,
) -> tuple[CaseResult, ...]:
    """Ejecuta los casos seleccionados, en orden estable de ``case_id``.

    Args:
        cases: Casos a considerar; por defecto, los del directorio canónico.
        categories: Filtro por categoría (unión).
        tags: Filtro por etiqueta (unión).
        case_ids: Filtro por identificador exacto.
        measure_duration: Si se mide el tiempo de pared de cada caso.
    """
    selected = tuple(load_cases() if cases is None else cases)
    if categories is not None:
        wanted_categories = set(categories)
        selected = tuple(case for case in selected if case.category in wanted_categories)
    if tags is not None:
        wanted_tags = set(tags)
        selected = tuple(case for case in selected if wanted_tags & set(case.tags))
    if case_ids is not None:
        wanted_ids = set(case_ids)
        selected = tuple(case for case in selected if case.case_id in wanted_ids)
    return tuple(
        run_case(case, measure_duration=measure_duration)
        for case in sorted(selected, key=lambda case: case.case_id)
    )


__all__ = ["compare", "load_cases", "run_case", "run_cases"]
