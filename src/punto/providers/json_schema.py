"""JSON Schema para Structured Outputs (ENGINE-5.2.1).

Un proveedor con *structured outputs* no necesita que el prompt pida JSON: recibe el esquema y
lo cumple o falla. Pero el esquema que PUNTO genera a partir de un modelo Pydantic no es
directamente transportable:

- Pydantic emite ``$defs`` y ``$ref`` para los modelos anidados (``CrossAuditFinding`` dentro de
  ``CrossAuditProposal``). Un ``$ref`` local obliga al proveedor a resolver referencias, y eso
  no está verificado en vivo: se **inlinea** aquí, de forma determinista, para enviar un esquema
  autocontenido;
- el contrato de PUNTO exige que el objeto raíz sea un objeto con ``properties``, ``required`` y
  ``additionalProperties: false``. Se comprueba **antes** de enviar nada, para que un esquema
  flojo falle en PUNTO y no en la API.

Reducir errores de formato no sustituye a la frontera final: el resultado sigue pasando por
``Pydantic.model_validate(...)``. Structured Outputs ayuda; Pydantic decide.
"""

from __future__ import annotations

from typing import Any, Final

#: Claves del dialecto que PUNTO sabe transformar.
DEFS_KEY: Final[str] = "$defs"
REF_KEY: Final[str] = "$ref"
LOCAL_REF_PREFIX: Final[str] = "#/"

#: Profundidad máxima de expansión de referencias, para no depender de la recursión de Python.
MAX_REF_DEPTH: Final[int] = 12


class SchemaValidationError(ValueError):
    """El esquema no cumple el contrato que PUNTO envía al proveedor."""


def prepare_json_schema(schema: Any) -> dict[str, Any]:
    """Devuelve un esquema autocontenido y validado, listo para enviar.

    Raises:
        SchemaValidationError: si el esquema no es un objeto JSON, si tiene referencias
            cíclicas o si la raíz no cumple el contrato de PUNTO.
    """
    if not isinstance(schema, dict):
        raise SchemaValidationError(
            f"el esquema debe ser un objeto JSON, no {type(schema).__name__}"
        )
    defs = schema.get(DEFS_KEY, {})
    if not isinstance(defs, dict):
        raise SchemaValidationError("'$defs' debe ser un objeto")

    with_defs = _resolve(schema, defs, depth=0)
    if not isinstance(with_defs, dict):  # pragma: no cover - la raíz ya se comprobó
        raise SchemaValidationError("el esquema raíz dejó de ser un objeto")
    prepared = {key: value for key, value in with_defs.items() if key != DEFS_KEY}
    validate_root_schema(prepared)
    return prepared


def validate_root_schema(schema: dict[str, Any]) -> None:
    """Comprueba que la raíz del esquema cumpla el contrato de PUNTO.

    Se exige, en la raíz: ``type: object``, ``properties`` no vacío, ``required`` con al menos
    un campo y ``additionalProperties: false``. Un modelo que no puede afirmar esto no debería
    enviarse como contrato estructurado.

    Raises:
        SchemaValidationError: si algo de lo anterior falta.
    """
    problems: list[str] = []
    if schema.get("type") != "object":
        problems.append("'type' debe ser 'object'")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        problems.append("'properties' debe ser un objeto con al menos un campo")
    required = schema.get("required")
    if not isinstance(required, list) or not required:
        problems.append("'required' debe listar al menos un campo")
    elif not all(isinstance(item, str) for item in required):
        problems.append("'required' debe contener solo nombres de campo")
    elif isinstance(properties, dict) and not set(required) <= set(properties):
        problems.append("'required' menciona campos que no están en 'properties'")
    if schema.get("additionalProperties") is not False:
        problems.append("'additionalProperties' debe ser false")
    if REF_KEY in schema or DEFS_KEY in schema:
        problems.append("el esquema enviado no puede contener '$ref' ni '$defs'")
    if problems:
        raise SchemaValidationError(
            "el esquema no cumple el contrato de PUNTO: " + "; ".join(problems)
        )


def _resolve(node: Any, defs: dict[str, Any], *, depth: int) -> Any:
    """Sustituye cada ``$ref`` local por su definición, con detección de ciclos."""
    if depth > MAX_REF_DEPTH:
        raise SchemaValidationError(
            "el esquema tiene referencias anidadas más profundas de lo que PUNTO expande "
            f"({MAX_REF_DEPTH})"
        )
    if isinstance(node, list):
        return [_resolve(item, defs, depth=depth) for item in node]
    if not isinstance(node, dict):
        return node

    reference = node.get(REF_KEY)
    if isinstance(reference, str):
        target = _lookup(reference, defs)
        if target is None:
            raise SchemaValidationError(f"referencia no resoluble en el esquema: {reference!r}")
        # Un ciclo no se puede inlinear sin inventar una estructura: se falla en vez de enviar
        # un esquema que el proveedor tendría que resolver por su cuenta.
        if _is_cyclic(reference, target, defs, seen=()):
            raise SchemaValidationError(
                f"el esquema tiene un ciclo de referencias en {reference!r}: PUNTO no lo inlinea"
            )
        merged = _resolve(target, defs, depth=depth + 1)
        if not isinstance(merged, dict):  # pragma: no cover - defensivo
            raise SchemaValidationError(f"la referencia {reference!r} no apunta a un objeto")
        extra = {key: value for key, value in node.items() if key != REF_KEY}
        return {**merged, **extra}

    return {
        key: _resolve(value, defs, depth=depth)
        for key, value in node.items()
        if key != DEFS_KEY
    }


def _lookup(reference: str, defs: dict[str, Any]) -> Any | None:
    """Resuelve una referencia local ``#/$defs/Nombre`` dentro de ``defs``."""
    if not reference.startswith(f"{LOCAL_REF_PREFIX}{DEFS_KEY}/"):
        return None
    name = reference[len(f"{LOCAL_REF_PREFIX}{DEFS_KEY}/") :]
    return defs.get(name)


def _is_cyclic(
    reference: str, target: Any, defs: dict[str, Any], *, seen: tuple[str, ...]
) -> bool:
    """True si resolver ``reference`` vuelve a pasar por una referencia ya vista."""
    if reference in seen:
        return True
    found: list[str] = []
    _collect_refs(target, found)
    return any(
        item == reference or _is_cyclic(item, _lookup(item, defs), defs, seen=(*seen, reference))
        for item in found
        if _lookup(item, defs) is not None
    )


def _collect_refs(node: Any, found: list[str]) -> None:
    """Recoge las referencias de un nodo, sin recorrer diccionarios anidados de más."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == REF_KEY and isinstance(value, str):
                found.append(value)
            else:
                _collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, found)


__all__ = [
    "DEFS_KEY",
    "LOCAL_REF_PREFIX",
    "MAX_REF_DEPTH",
    "REF_KEY",
    "SchemaValidationError",
    "prepare_json_schema",
    "validate_root_schema",
]
