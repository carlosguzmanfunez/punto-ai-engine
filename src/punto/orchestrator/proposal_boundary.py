"""Frontera proveedor → propuesta de cambios: límites contractuales de los campos descriptivos.

Defecto que cierra (Task 2e7822a0, intento 9): el BUILDER devolvió un ``acceptance_criterion``
de más de 300 caracteres y ``FileChangeProposal`` rechazó **todo** el cambio (``CHANGE_INVALID``)
por una anotación. El defecto era predecible para cualquier proveedor por dos motivos:

1. el límite es un invariante deliberado (``MAX_ITEM_CHARS``: una anotación es una anotación, no
   un ensayo), pero el contrato que se le entrega al proveedor **nunca lo declaraba**; y
2. no puede viajar en el ``json_schema``: ``maxLength`` está en
   ``punto.providers.json_schema.UNSUPPORTED_CONSTRAINTS`` (los dialectos de proveedor no lo
   admiten), así que el único canal es el texto del contrato.

La corrección son dos cosas, y ninguna relaja una validación:

- ``length_limits_text`` deriva los límites **del propio modelo** (no de números copiados) para
  que el contrato del prompt no pueda divergir del contrato de validación;
- ``normalize_descriptive_fields`` ajusta, de forma determinista y **registrada**, únicamente
  los campos *descriptivos* (``reason`` y ``acceptance_criterion``): texto que solo se usa como
  evidencia legible y que no decide qué se escribe, dónde ni con qué autoridad.

Las rutas no se recortan ni se inventan. Hay una única normalización semántica adicional:
``source_path=""`` (o solo espacios) se elimina para CREATE/MODIFY/DELETE, operaciones en las que
el origen no aplica. Para RENAME/MOVE permanece intacto y se rechaza con causa estructurada porque
esas operaciones sí necesitan un origen real. Un valor no vacío nunca se modifica.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel

from punto.schemas.dev import ContextRequest, FileChangeProposal

__all__ = [
    "DESCRIPTIVE_FIELDS",
    "ChangeShapeNormalization",
    "Normalization",
    "field_limit",
    "fit_annotation",
    "length_limits_text",
    "normalize_change_shape",
    "normalize_descriptive_fields",
]

#: Campos descriptivos por lista de la respuesta del BUILDER: (modelo que los valida, campos).
#: Cerrada a propósito: añadir un campo aquí es decidir que su recorte no cambia lo que se escribe.
DESCRIPTIVE_FIELDS: Final[Mapping[str, tuple[type[BaseModel], tuple[str, ...]]]] = {
    "changes": (FileChangeProposal, ("reason", "acceptance_criterion")),
    "context_requests": (ContextRequest, ("reason",)),
}

#: Marca del recorte: ocupa un carácter dentro del límite, así que el resultado nunca lo excede.
ELLIPSIS: Final[str] = "…"

#: Mínimo de la anotación que debe conservarse para cortar en frontera de palabra (la mitad del
#: límite): un corte de palabra que dejara casi nada se sustituye por el corte duro.
_MIN_KEPT_RATIO: Final[float] = 0.5


@dataclass(frozen=True, slots=True)
class Normalization:
    """Constancia de un campo descriptivo ajustado: qué era, cuánto se conservó y su huella."""

    location: str
    original_chars: int
    kept_chars: int
    original_sha256: str

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable, sin el texto (la huella permite comprobarlo contra el original)."""
        return {
            "location": self.location,
            "original_chars": self.original_chars,
            "kept_chars": self.kept_chars,
            "original_sha256": self.original_sha256,
        }


@dataclass(frozen=True, slots=True)
class ChangeShapeNormalization:
    """Constancia de una ausencia opcional expresada como cadena vacía por el proveedor."""

    location: str
    operation: str
    action: str = "EMPTY_SOURCE_PATH_TO_ABSENT"

    def as_dict(self) -> dict[str, str]:
        """Vista serializable sin contenido ni rutas."""
        return {
            "location": self.location,
            "operation": self.operation,
            "action": self.action,
        }


def normalize_change_shape(
    payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], tuple[ChangeShapeNormalization, ...]]:
    """Normaliza solo ausencias inequívocas de ``source_path`` según la operación.

    CREATE, MODIFY y DELETE identifican su recurso mediante ``path`` y no tienen ruta de origen.
    Si un proveedor serializa el campo opcional como texto vacío, quitarlo conserva exactamente la
    misma semántica. RENAME/MOVE, operaciones desconocidas, rutas no vacías y formas no-objeto se
    dejan intactas para que la validación las rechace sin adivinar nada.
    """
    raw = payload.get("changes")
    if not isinstance(raw, list):
        return payload, ()
    notes: list[ChangeShapeNormalization] = []
    items: list[Any] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            items.append(item)
            continue
        operation = item.get("operation")
        source = item.get("source_path")
        if (
            operation in {"CREATE", "MODIFY", "DELETE"}
            and isinstance(source, str)
            and not source.strip()
        ):
            fixed = dict(item)
            fixed.pop("source_path", None)
            items.append(fixed)
            notes.append(
                ChangeShapeNormalization(
                    location=f"changes[{index}].source_path", operation=str(operation)
                )
            )
            continue
        items.append(item)
    if not notes:
        return payload, ()
    result = dict(payload)
    result["changes"] = items
    return result, tuple(notes)


def field_limit(model: type[BaseModel], name: str) -> int | None:
    """Límite ``max_length`` de un campo, leído del propio modelo (fuente única de verdad)."""
    for item in model.model_fields[name].metadata:
        limit = getattr(item, "max_length", None)
        if isinstance(limit, int):
            return limit
    return None


def fit_annotation(text: str, limit: int) -> str:
    """Ajusta un texto al límite conservando su **comienzo**, que es donde vive su identidad.

    Corta en frontera de palabra si eso no descarta más de la mitad y añade ``…``; el resultado
    nunca supera ``limit``. Un texto que ya cabe se devuelve tal cual.
    """
    if len(text) <= limit:
        return text
    head = text[: limit - len(ELLIPSIS)]
    boundary = head.rfind(" ")
    if boundary >= int(limit * _MIN_KEPT_RATIO):
        head = head[:boundary]
    return head.rstrip(" \t\r\n,;:.-—") + ELLIPSIS


def normalize_descriptive_fields(
    payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], tuple[Normalization, ...]]:
    """Ajusta los campos descriptivos que exceden su límite y devuelve la constancia de cada uno.

    Devuelve el mismo objeto si no hay nada que ajustar. Solo toca texto (``str``) dentro de las
    listas y campos de :data:`DESCRIPTIVE_FIELDS`; todo lo demás llega intacto al modelo, que sigue
    fallando cerrado ante cualquier otro defecto.
    """
    notes: list[Normalization] = []
    result: dict[str, Any] | None = None
    for key, (model, fields) in DESCRIPTIVE_FIELDS.items():
        raw = payload.get(key)
        if not isinstance(raw, list):
            continue
        items: list[Any] = []
        changed = False
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                items.append(item)
                continue
            fixed: dict[str, Any] | None = None
            for name in fields:
                value = item.get(name)
                limit = field_limit(model, name)
                if not isinstance(value, str) or limit is None or len(value) <= limit:
                    continue
                shortened = fit_annotation(value, limit)
                fixed = dict(item) if fixed is None else fixed
                fixed[name] = shortened
                notes.append(
                    Normalization(
                        location=f"{key}[{index}].{name}",
                        original_chars=len(value),
                        kept_chars=len(shortened),
                        original_sha256=hashlib.sha256(value.encode("utf-8")).hexdigest(),
                    )
                )
            items.append(item if fixed is None else fixed)
            changed = changed or fixed is not None
        if changed:
            result = dict(payload) if result is None else result
            result[key] = items
    return (payload if result is None else result), tuple(notes)


def length_limits_text() -> str:
    """Límites de los campos descriptivos, derivados de los modelos, para el contrato del prompt."""
    change = FileChangeProposal
    context = ContextRequest
    return (
        "Hard length limits (characters): "
        f"changes[].reason <= {field_limit(change, 'reason')}, "
        f"changes[].acceptance_criterion <= {field_limit(change, 'acceptance_criterion')} "
        "(a short label or the opening words of the criterion, never an essay), "
        f"context_requests[].reason <= {field_limit(context, 'reason')}. "
        "Stay within them. path, source_path, operation and content are never trimmed: a value "
        "that does not fit the contract there invalidates the whole change."
    )
