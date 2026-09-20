"""Preflight determinista de una **propuesta de reparación** (RESOLUTION PIPELINE HARDENING).

PUNTO no debe gastar una ronda funcional de reparación —ni una verificación— en descubrir una
inconsistencia que puede **medir**: ``CREATE`` sobre algo que ya existe, ``MODIFY``/``DELETE`` sobre
algo que no está, dos operaciones contradictorias sobre el mismo recurso, un cambio que dejaría el
árbol exactamente igual. Esta capa comprueba esa frontera **antes** de validar y aplicar, y devuelve
un feedback estructurado y mínimo para que el proveedor corrija barato.

Qué **no** hace, a propósito:

- no toca la autoridad: el plan, el sobre de riesgo y ``PolicyEngine`` siguen decidiendo en
  ``_validate_changes``, que sigue siendo el validador que gobierna la aplicación;
- no cambia la atomicidad: una propuesta inválida sigue sin aplicarse a medias;
- no transforma operaciones por su cuenta (``CREATE`` → ``MODIFY``): PUNTO **sabe** si el fichero
  existe, pero la equivalencia semántica la decide quien propone, con el estado real delante;
- no concede autoridad ni presupuesto: solo describe hechos del workspace.

Los códigos reutilizan el vocabulario del validador existente (``CHANGE_ALREADY_EXISTS``,
``CHANGE_MISSING_FILE``, ``CHANGE_MISSING_SOURCE``, ``CHANGE_DUPLICATED``) y añaden los que faltaban
en esta frontera (``CHANGE_CONFLICTING``, ``CHANGE_ALREADY_APPLIED``, ``CHANGE_WITHOUT_EFFECT``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from punto.schemas.dev import ChangeOperation, FileChangeProposal

__all__ = [
    "CHANGE_ALREADY_APPLIED",
    "CHANGE_ALREADY_EXISTS",
    "CHANGE_CONFLICTING",
    "CHANGE_DUPLICATED",
    "CHANGE_MISSING_FILE",
    "CHANGE_MISSING_SOURCE",
    "CHANGE_WITHOUT_EFFECT",
    "MAX_FEEDBACK_ISSUES",
    "PROPOSAL_FEEDBACK_LABEL",
    "NormalizedChange",
    "ProposalIssue",
    "ProposalPreflightResult",
    "correction_feedback",
    "issue_codes",
    "normalize_changes",
    "proposal_preflight",
]

#: Códigos de la frontera estructural.
CHANGE_ALREADY_EXISTS: Final[str] = "CHANGE_ALREADY_EXISTS"
CHANGE_MISSING_FILE: Final[str] = "CHANGE_MISSING_FILE"
CHANGE_MISSING_SOURCE: Final[str] = "CHANGE_MISSING_SOURCE"
CHANGE_DUPLICATED: Final[str] = "CHANGE_DUPLICATED"
CHANGE_CONFLICTING: Final[str] = "CHANGE_CONFLICTING"
CHANGE_ALREADY_APPLIED: Final[str] = "CHANGE_ALREADY_APPLIED"
CHANGE_WITHOUT_EFFECT: Final[str] = "CHANGE_WITHOUT_EFFECT"

#: Etiqueta del bloque de corrección estructural en el prompt del BUILDER.
PROPOSAL_FEEDBACK_LABEL: Final[str] = (
    "PROPOSAL PREFLIGHT FAILED (structural facts only; nothing was applied and no repair attempt "
    "was consumed):"
)

#: Reglas que acompañan al feedback: son la corrección barata, no un ensayo.
PROPOSAL_FEEDBACK_RULES: Final[str] = (
    "RULES: choose the operation that matches the workspace state (CREATE only for a file that "
    "does not exist, MODIFY for one that does); one operation per path; do not re-send a change "
    "whose content is already in the file. Re-send the whole proposal corrected."
)

#: Cota del feedback: la corrección es barata, no un volcado.
MAX_FEEDBACK_ISSUES: Final[int] = 5


@dataclass(frozen=True, slots=True)
class NormalizedChange:
    """Cambio ya normalizado: ruta con separadores POSIX y operación del contrato."""

    path: str
    operation: str
    source_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable, sin contenido de ficheros."""
        data: dict[str, Any] = {"path": self.path, "operation": self.operation}
        if self.source_path:
            data["source_path"] = self.source_path
        return data


@dataclass(frozen=True, slots=True)
class ProposalIssue:
    """Inconsistencia estructural de una propuesta, con el hecho que la sostiene."""

    code: str
    path: str
    operation: str
    actual_state: str
    expected_state: str
    correctable: bool = True
    blocking: bool = True
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable y compacta."""
        return {
            "code": self.code,
            "path": self.path,
            "operation": self.operation,
            "actual_state": self.actual_state,
            "expected_state": self.expected_state,
            "correctable": self.correctable,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class ProposalPreflightResult:
    """Resultado del preflight: la propuesta normalizada y lo que impide aplicarla."""

    changes: tuple[NormalizedChange, ...] = ()
    issues: tuple[ProposalIssue, ...] = ()

    @property
    def valid(self) -> bool:
        """True si nada **bloquea** la aplicación (los avisos no bloquean)."""
        return not any(item.blocking for item in self.issues)

    @property
    def blocking(self) -> tuple[ProposalIssue, ...]:
        """Inconsistencias que impiden aplicar la propuesta."""
        return tuple(item for item in self.issues if item.blocking)

    @property
    def advisory(self) -> tuple[ProposalIssue, ...]:
        """Avisos que no impiden aplicar: se registran, no se cobran como corrección."""
        return tuple(item for item in self.issues if not item.blocking)

    @property
    def correctable(self) -> bool:
        """True si todo lo que bloquea se corrige con el estado real delante."""
        return bool(self.blocking) and all(item.correctable for item in self.blocking)

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "valid": self.valid,
            "correctable": self.correctable,
            "changes": [item.as_dict() for item in self.changes],
            "issues": [item.as_dict() for item in self.issues],
        }


def _normalize(path: str) -> str:
    """Ruta con separadores POSIX, sin espacios sobrantes."""
    return str(path or "").strip().replace("\\", "/")


def normalize_changes(proposals: Sequence[FileChangeProposal]) -> tuple[NormalizedChange, ...]:
    """Forma normalizada de la propuesta, en el orden en que llegó."""
    return tuple(
        NormalizedChange(
            path=_normalize(item.path),
            operation=item.operation.value,
            source_path=_normalize(item.source_path or ""),
        )
        for item in proposals
    )


def _content_sha(proposal: FileChangeProposal) -> str:
    """Huella del contenido propuesto, normalizado, o vacío si la operación no lleva contenido."""
    if not proposal.content:
        return ""
    return hashlib.sha256(_normaliza(proposal.content).encode("utf-8")).hexdigest()


def _normaliza(texto: str) -> str:
    """Texto comparable: finales de línea unificados.

    El workspace puede devolver ``\\r\\n`` (Windows) mientras lo que propone el proveedor llega con
    ``\\n``: comparar bytes crudos daría falsos negativos en la detección de «ya aplicado».
    """
    return texto.replace("\r\n", "\n").replace("\r", "\n")


def proposal_preflight(
    proposals: Sequence[FileChangeProposal],
    *,
    exists: Callable[[str], bool],
    read_text: Callable[[str], str],
) -> ProposalPreflightResult:
    """Comprueba la coherencia estructural de la propuesta contra el estado real del workspace.

    ``exists`` y ``read_text`` son la única fuente de verdad del workspace (el repositorio
    gobernado): el preflight no adivina ni transforma, solo mide y describe. Nunca aplica nada.
    """
    changes = normalize_changes(proposals)
    issues: list[ProposalIssue] = []
    if not changes:
        return ProposalPreflightResult(changes=(), issues=())

    # 1) Coherencia interna de la propuesta: duplicados y contradicciones sobre el mismo recurso.
    por_ruta: dict[str, list[NormalizedChange]] = {}
    for change in changes:
        por_ruta.setdefault(change.path, []).append(change)
    for path, grupo in por_ruta.items():
        if len(grupo) < 2:
            continue
        operaciones = {item.operation for item in grupo}
        if len(operaciones) > 1:
            issues.append(
                ProposalIssue(
                    code=CHANGE_CONFLICTING,
                    path=path,
                    operation="|".join(sorted(operaciones)),
                    actual_state=",".join(sorted(operaciones)),
                    expected_state="una sola operación por ruta",
                    evidence="la propuesta pide operaciones contradictorias sobre el mismo recurso",
                )
            )
            continue
        if len({item.source_path for item in grupo}) == 1:
            issues.append(
                ProposalIssue(
                    code=CHANGE_DUPLICATED,
                    path=path,
                    operation=grupo[0].operation,
                    actual_state=f"{len(grupo)} cambios idénticos",
                    expected_state="un cambio por ruta y operación",
                    evidence="el mismo cambio aparece más de una vez en la propuesta",
                )
            )

    # 2) Coherencia con el workspace: la operación tiene que encajar con lo que hay.
    no_ops: list[str] = []
    for proposal, change in zip(proposals, changes, strict=True):
        path = change.path
        operation = change.operation
        presente = exists(path)
        if operation == ChangeOperation.CREATE.value and presente:
            issues.append(
                ProposalIssue(
                    code=CHANGE_ALREADY_EXISTS,
                    path=path,
                    operation=operation,
                    actual_state="EXISTS",
                    expected_state="MISSING",
                    evidence="CREATE sobre un fichero que ya existe: la operación es MODIFY",
                )
            )
            continue
        if operation == ChangeOperation.MODIFY.value and not presente:
            issues.append(
                ProposalIssue(
                    code=CHANGE_MISSING_FILE,
                    path=path,
                    operation=operation,
                    actual_state="MISSING",
                    expected_state="EXISTS",
                    evidence="MODIFY sobre un fichero que no existe: la operación es CREATE",
                )
            )
            continue
        if operation == ChangeOperation.DELETE.value and not presente:
            issues.append(
                ProposalIssue(
                    code=CHANGE_MISSING_FILE,
                    path=path,
                    operation=operation,
                    actual_state="MISSING",
                    expected_state="EXISTS",
                    evidence="DELETE sobre un fichero que no existe: no hay nada que borrar",
                )
            )
            continue
        if operation in (ChangeOperation.RENAME.value, ChangeOperation.MOVE.value):
            origen = change.source_path
            if not exists(origen):
                issues.append(
                    ProposalIssue(
                        code=CHANGE_MISSING_SOURCE,
                        path=path,
                        operation=operation,
                        actual_state="MISSING",
                        expected_state="EXISTS",
                        evidence=f"el origen {origen!r} no existe: no hay nada que mover",
                    )
                )
                continue
            if presente:
                issues.append(
                    ProposalIssue(
                        code=CHANGE_ALREADY_EXISTS,
                        path=path,
                        operation=operation,
                        actual_state="EXISTS",
                        expected_state="MISSING",
                        evidence="el destino del movimiento está ocupado",
                    )
                )
                continue
        # Aviso: el cambio dejaría el fichero exactamente como está (trabajo repetido, no error).
        # Si el cambio declara ``expected_sha256`` está afirmando una versión del fichero, y esa
        # afirmación la juzga el validador (``CHANGE_STALE``): el preflight no la tapa.
        propuesto = _content_sha(proposal) if not proposal.expected_sha256 else ""
        if propuesto and presente:
            actual = _normaliza(read_text(path))
            if propuesto == hashlib.sha256(actual.encode("utf-8")).hexdigest():
                no_ops.append(path)
                issues.append(
                    ProposalIssue(
                        code=CHANGE_ALREADY_APPLIED,
                        path=path,
                        operation=operation,
                        actual_state="ALREADY_APPLIED",
                        expected_state="cambio con efecto",
                        blocking=False,
                        evidence="el contenido propuesto ya está en el fichero",
                    )
                )

    # 3) Una propuesta que no cambia nada no merece una ronda de verificación.
    if changes and not any(item.blocking for item in issues) and len(no_ops) == len(changes):
        issues.append(
            ProposalIssue(
                code=CHANGE_WITHOUT_EFFECT,
                path=",".join(no_ops[:MAX_FEEDBACK_ISSUES]),
                operation="",
                actual_state="ALREADY_APPLIED",
                expected_state="algún cambio con efecto",
                evidence="todos los cambios de la propuesta dejarían el árbol igual",
            )
        )
    return ProposalPreflightResult(changes=changes, issues=tuple(issues))


def correction_feedback(result: ProposalPreflightResult) -> str:
    """Bloque compacto de corrección: los hechos, no el prompt entero.

    Solo viaja lo que hace falta para corregir: código, ruta, operación propuesta y estado real. Ni
    logs, ni PELL, ni auditoría, ni ficheros no relacionados.
    """
    if result.valid:
        return ""
    payload = {
        "proposal_preflight": "FAILED",
        "issues": [item.as_dict() for item in result.blocking[:MAX_FEEDBACK_ISSUES]],
    }
    avisos = result.advisory[:MAX_FEEDBACK_ISSUES]
    if avisos:
        payload["advisory"] = [item.as_dict() for item in avisos]
    return (
        f"{PROPOSAL_FEEDBACK_LABEL}\n"
        f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True)}\n"
        f"{PROPOSAL_FEEDBACK_RULES}"
    )


def issue_codes(result: ProposalPreflightResult) -> tuple[str, ...]:
    """Códigos de los bloqueos, sin repetir y en orden."""
    return tuple(dict.fromkeys(item.code for item in result.blocking))
