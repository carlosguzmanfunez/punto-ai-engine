"""Identidad y equivalencia de Tasks: una sola Task canónica por trabajo equivalente activo.

Dos preguntas generales (nada de proyectos ni ids concretos):

1. **¿Esta Task pertenece de verdad a este destino?** Todo destino tiene una identidad canónica
   (``TargetIdentity``: repositorio, rama de trabajo, rama de producción). Una Task guarda la huella
   de la identidad con la que nació; si difiere de la vigente —o, en una Task anterior a la huella,
   si su resultado se produjo en otra rama— su estado no es de este destino (un fixture, otro
   repositorio, una configuración cambiada): no es operativa y no se ejecuta.

2. **¿Dos solicitudes son el mismo trabajo?** No por igualdad de texto: por destino + objetivo
   normalizado + alcance funcional compatible + criterios compatibles. Si lo son, la segunda es una
   continuación de la primera (mismo mecanismo de reintento), no una Task nueva.

Es puro: decide, no escribe. La consola aplica el veredicto y deja el rastro durable.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any, Final

from punto.workspace.target import DevelopmentTarget

__all__ = [
    "ACTIVE_LINEAGE",
    "CAUSE_DUPLICATE",
    "CAUSE_IDENTITY",
    "SUPERSEDED_LINEAGE",
    "canonical_rank",
    "criteria_compatible",
    "find_equivalents",
    "identity_conflict",
    "normalize_tokens",
    "objectives_equivalent",
    "pick_canonical",
    "scopes_compatible",
    "signature_equivalent",
]

ACTIVE_LINEAGE: Final[str] = "ACTIVE"
SUPERSEDED_LINEAGE: Final[str] = "SUPERSEDED"
CAUSE_DUPLICATE: Final[str] = "duplicate_objective"
CAUSE_IDENTITY: Final[str] = "identity_mismatch"

#: Similitud mínima (Jaccard sobre tokens normalizados) para considerar el mismo objetivo.
OBJECTIVE_THRESHOLD: Final[float] = 0.75
#: Similitud mínima entre criterios de aceptación (si ambos los declaran).
CRITERIA_THRESHOLD: Final[float] = 0.5

_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al", "en", "y", "o",
        "a", "que", "se", "por", "para", "con", "su", "sus", "lo", "es", "e", "u", "the", "of",
        "to", "and", "in", "for", "an", "on", "is",
    }
)  # fmt: skip

#: Etapas en las que el trabajo sigue vivo (una petición equivalente lo continúa).
CONTINUABLE_STAGES: Final[frozenset[str]] = frozenset(
    {
        "QUEUED",
        "DEVELOPING",
        "WAITING_HUMAN",
        "HUMAN_APPROVED",
        "DEVELOPMENT_FAILED",
        "DEVELOPMENT_COMPLETED",
        "WAITING_PRODUCTION_APPROVAL",
        "PUBLISHING",
        "DEPLOYMENT_VERIFICATION",
        "PUBLICATION_FAILED",
        "DEPLOYMENT_NOT_VERIFIED",
        "BLOCKED_NOT_PUBLISHABLE",
    }
)

_PROGRESS: Final[dict[str, int]] = {
    "DEVELOPING": 6,
    "PUBLISHING": 6,
    "DEPLOYMENT_VERIFICATION": 6,
    "WAITING_PRODUCTION_APPROVAL": 5,
    "DEVELOPMENT_COMPLETED": 5,
    "WAITING_HUMAN": 4,
    "HUMAN_APPROVED": 4,
    "PUBLICATION_FAILED": 3,
    "DEPLOYMENT_NOT_VERIFIED": 3,
    "DEVELOPMENT_FAILED": 2,
    "QUEUED": 1,
}


# --------------------------------------------------------------------------- normalización
def normalize_tokens(text: str) -> frozenset[str]:
    """Tokens comparables de un texto: sin acentos, minúsculas, sin puntuación ni vacías."""
    folded = unicodedata.normalize("NFKD", str(text))
    plain = "".join(char for char in folded if not unicodedata.combining(char)).casefold()
    tokens: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", plain):
        if raw in _STOPWORDS or len(raw) < 2:
            continue
        # Plural regular: «tipos»/«tipo», «departamentos»/«departamento».
        tokens.add(raw[:-1] if len(raw) > 4 and raw.endswith("s") else raw)
    return frozenset(tokens)


def _jaccard(first: frozenset[str], second: frozenset[str]) -> float:
    if not first and not second:
        return 1.0
    union = first | second
    return len(first & second) / len(union) if union else 0.0


def objectives_equivalent(first: str, second: str) -> bool:
    """Mismo objetivo tras normalizar (no por igualdad literal)."""
    return _jaccard(normalize_tokens(first), normalize_tokens(second)) >= OBJECTIVE_THRESHOLD


def _normal_paths(paths: Iterable[str]) -> frozenset[str]:
    return frozenset(
        item.replace("\\", "/").strip().strip("/").casefold() for item in paths if item.strip()
    )


def scopes_compatible(first: Sequence[str], second: Sequence[str]) -> bool:
    """Alcance funcional compatible: alguno vacío (todo el destino), o rutas que se contienen."""
    a, b = _normal_paths(first), _normal_paths(second)
    if not a or not b:
        return True
    return any(
        left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")
        for left in a
        for right in b
    )


def criteria_compatible(first: Sequence[str], second: Sequence[str]) -> bool:
    """Criterios de aceptación compatibles: alguno sin criterios, o suficientemente parecidos."""
    if not first or not second:
        return True
    return (
        _jaccard(normalize_tokens(" ".join(first)), normalize_tokens(" ".join(second)))
        >= CRITERIA_THRESHOLD
    )


def signature_equivalent(
    first: Any, second: Any
) -> bool:  # first/second: objetos con objective, target_id, scope_paths, acceptance_criteria
    """Mismo trabajo: mismo destino, mismo objetivo normalizado, alcance y criterios compatibles."""
    return (
        first.target_id == second.target_id
        and objectives_equivalent(first.objective, second.objective)
        and scopes_compatible(first.scope_paths, second.scope_paths)
        and criteria_compatible(first.acceptance_criteria, second.acceptance_criteria)
    )


def find_equivalents(candidate: Any, tasks: Iterable[Any]) -> tuple[Any, ...]:
    """Tareas activas y operativas del mismo destino que son el mismo trabajo que ``candidate``."""
    return tuple(
        task
        for task in tasks
        if task is not candidate
        and getattr(task, "lineage_status", ACTIVE_LINEAGE) == ACTIVE_LINEAGE
        and task.stage in CONTINUABLE_STAGES
        and signature_equivalent(candidate, task)
    )


# ----------------------------------------------------------------------------- canónica
def canonical_rank(task: Any) -> tuple[Any, ...]:
    """Clave de ordenación (mayor = más canónica): estado real, intentos, evidencia y cronología.

    Manda lo que **de verdad ocurrió**: el trabajo más avanzado, con más intentos y evidencia, y a
    igualdad la más antigua (la solicitud original) y la de actividad más reciente.
    """
    result = getattr(task, "result", None)
    evidence = (
        int(result is not None)
        + int(bool(result and result.plan is not None))
        + int(bool(result and result.verification))
    )
    gates = len(getattr(task, "gates", ()) or ())
    return (
        _PROGRESS.get(task.stage, 0),
        len(task.attempts),
        evidence,
        gates,
        -task.created_at.timestamp(),
        task.updated_at.timestamp(),
    )


def pick_canonical(tasks: Sequence[Any]) -> Any:
    """La Task canónica de un grupo de equivalentes."""
    return max(tasks, key=canonical_rank)


# ------------------------------------------------------------------------------ identidad
def identity_conflict(
    *,
    fingerprint: str,
    work_branch: str,
    production_branch: str,
    result_branch: str,
    target: DevelopmentTarget,
) -> str:
    """Motivo por el que una Task no pertenece a la identidad vigente del destino, o ``""``.

    Con huella registrada se compara la huella (y se dice qué rama difiere). Sin huella (Task
    anterior a la huella) solo hay una prueba objetiva: la rama en la que se produjo su resultado
    frente a la rama de trabajo canónica del destino; sin resultado no hay nada que contradiga.
    """
    current = target.identity
    if fingerprint:
        if fingerprint == current.fingerprint:
            return ""
        diffs: list[str] = []
        if work_branch and work_branch != current.work_branch:
            diffs.append(
                f"rama de trabajo registrada {work_branch!r} ≠ canónica {current.work_branch!r}"
            )
        if production_branch and production_branch != current.production_branch:
            diffs.append(
                f"rama de producción registrada {production_branch!r} ≠ canónica "
                f"{current.production_branch!r}"
            )
        return "la identidad del destino no es la registrada" + (
            ": " + "; ".join(diffs) if diffs else " (repositorio o remoto distintos)"
        )
    if result_branch and current.work_branch and result_branch != current.work_branch:
        return (
            f"la rama del resultado {result_branch!r} no es la rama de trabajo canónica "
            f"{current.work_branch!r} del destino"
        )
    return ""
