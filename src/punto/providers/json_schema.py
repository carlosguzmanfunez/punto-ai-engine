"""JSON Schema para Structured Outputs (ENGINE-5.2.1 y ENGINE-5.2.2).

Un proveedor con *structured outputs* no necesita que el prompt pida JSON: recibe el esquema y
lo cumple o falla. Pero el esquema que PUNTO genera a partir de un modelo Pydantic **no** es
directamente transportable, y enviarlo tal cual es una petición de HTTP 400 esperando a ocurrir:

- Pydantic emite ``$defs`` y ``$ref`` para los modelos anidados. Se **inlinean** aquí, de forma
  determinista, para enviar un esquema autocontenido;
- Pydantic emite restricciones que el dialecto del proveedor **no admite** (``minLength``,
  ``minimum``, ``maximum``, ...). Se retiran del esquema enviado y se anotan en la
  ``description`` del campo para que el modelo siga sabiendo qué se espera de él;
- el contrato de PUNTO exige objetos **cerrados** (``additionalProperties: false``) y exige que
  ningún keyword desconocido llegue al proveedor.

Dos esquemas, dos responsabilidades:

- **ORIGINAL** (``Model.model_json_schema()``) es el contrato de PUNTO. No se toca ni se
  sustituye: ``Model.model_validate(...)`` lo sigue aplicando **entero**, restricciones
  incluidas.
- **PROVIDER** es la versión compatible que viaja al proveedor. Suaviza restricciones para
  que la petición sea aceptada.

Structured Outputs reduce errores de formato; Pydantic decide la validez final. Retirar
``minLength`` del provider schema no relaja nada en PUNTO: el modelo sigue rechazando una
cadena vacía.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel

#: Keywords que el dialecto del proveedor admite y PUNTO transporta tal cual.
#:
#: ``minItems`` está aquí a propósito: el dialecto lo admite, pero **solo** con 0 o 1, y eso se
#: comprueba aparte. ``format`` también, con allowlist. Una lista negra improvisada destruiría
#: features válidas, así que la lista es de lo **permitido**.
SUPPORTED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "default",
        "description",
        "enum",
        "format",
        "items",
        "minItems",
        "properties",
        "required",
        "title",
        "type",
    }
)

#: Valores de ``minItems`` que el dialecto admite.
SUPPORTED_MIN_ITEMS: Final[frozenset[int]] = frozenset({0, 1})

#: Formatos documentados por el proveedor. Cualquier otro es un 400 esperando a ocurrir.
ALLOWED_FORMATS: Final[frozenset[str]] = frozenset(
    {
        "date",
        "date-time",
        "duration",
        "email",
        "hostname",
        "ipv4",
        "ipv6",
        "time",
        "uri",
        "uuid",
    }
)

#: Tipos admitidos. Un ``type`` fuera de esta lista no se puede transportar.
ALLOWED_TYPES: Final[frozenset[str]] = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)

#: Keywords que se resuelven o se descartan antes de validar: no deben llegar al proveedor.
RESOLVED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"$defs", "$id", "$ref", "$schema", "definitions"}
)

#: Restricciones que el dialecto **no** admite, con su traducción legible.
#:
#: No se borran en silencio: se retiran del provider schema y se añaden a la ``description``
#: del campo. La garantía no se pierde porque Pydantic siga validando el modelo original.
UNSUPPORTED_CONSTRAINTS: Final[Mapping[str, str]] = {
    "exclusiveMaximum": "debe ser menor que {value}",
    "exclusiveMinimum": "debe ser mayor que {value}",
    "maximum": "debe ser menor o igual que {value}",
    "maxItems": "debe contener como máximo {value} elemento(s)",
    "maxLength": "debe contener como máximo {value} carácter(es)",
    "maxProperties": "debe declarar como máximo {value} propiedad(es)",
    "minLength": "debe contener al menos {value} carácter(es)",
    "minProperties": "debe declarar al menos {value} propiedad(es)",
    "minimum": "debe ser mayor o igual que {value}",
    "multipleOf": "debe ser múltiplo de {value}",
    "pattern": "debe cumplir el patrón {value}",
    "uniqueItems": "no debe contener elementos duplicados",
}

#: Notas legibles, incluidas las de los valores que se retiran **condicionalmente**
#: (``minItems > 1``), que no están en :data:`UNSUPPORTED_CONSTRAINTS` porque 0 y 1 sí viajan.
CONSTRAINT_NOTES: Final[Mapping[str, str]] = {
    **UNSUPPORTED_CONSTRAINTS,
    "minItems": "debe contener al menos {value} elemento(s)",
}

#: Claves estructurales: no se pueden fusionar con conflicto desde un ``$ref`` con siblings.
STRUCTURAL_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "enum",
        "format",
        "items",
        "minItems",
        "pattern",
        "properties",
        "required",
        "type",
    }
)

#: Siblings de un ``$ref`` que solo anotan: se pueden heredar sin cambiar la semántica.
ANNOTATION_KEYWORDS: Final[frozenset[str]] = frozenset({"default", "description", "title"})

#: Claves que PUNTO reconoce pero no transporta, con su tratamiento explícito.
DROPPED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"$comment", "deprecated", "examples", "readOnly", "writeOnly"}
)

LOCAL_REF_PREFIX: Final[str] = "#/"

#: Máximo de parámetros **opcionales** que admite Structured Outputs.
#:
#: Se cuenta, sobre el provider schema ya transformado, cada propiedad que **no** figura en el
#: ``required`` de su objeto. Un esquema con más opcionales de los admitidos se rechaza en
#: PUNTO, antes de gastar una llamada HTTP en un 400.
MAX_OPTIONAL_PARAMETERS: Final[int] = 24

#: Máximo de parámetros **de unión** que admite Structured Outputs.
#:
#: Se cuenta cada nodo que declara ``anyOf``: una posición del esquema que puede tomar más de
#: una forma. Los ``anyOf`` anidados cuentan uno por nodo, que es como los ve el dialecto.
MAX_UNION_PARAMETERS: Final[int] = 16

#: Profundidad máxima de expansión de referencias, para no depender de la recursión de Python.
MAX_REF_DEPTH: Final[int] = 12


class SchemaValidationError(ValueError):
    """El esquema no cumple el contrato que PUNTO envía al proveedor."""


def prepare_json_schema(schema: Any) -> dict[str, Any]:
    """Devuelve el **provider schema**: autocontenido, transformado y validado.

    Raises:
        SchemaValidationError: si el esquema no es un objeto JSON, si tiene referencias
            cíclicas o en conflicto, si contiene un keyword que PUNTO no sabe transportar o si
            algún nodo incumple el contrato de PUNTO.
    """
    if not isinstance(schema, dict):
        raise SchemaValidationError(
            f"el esquema debe ser un objeto JSON, no {type(schema).__name__}"
        )
    defs = schema.get("$defs", schema.get("definitions", {}))
    if not isinstance(defs, dict):
        raise SchemaValidationError("'$defs' debe ser un objeto")

    resolved = _resolve(schema, defs, depth=0, path="")
    if not isinstance(resolved, dict):  # pragma: no cover - la raíz ya se comprobó
        raise SchemaValidationError("el esquema raíz dejó de ser un objeto")
    transformed = _transform(resolved, path="")
    if not isinstance(transformed, dict):  # pragma: no cover - defensivo
        raise SchemaValidationError("el esquema raíz dejó de ser un objeto")
    validate_provider_schema(transformed)
    validate_complexity_limits(transformed)
    return transformed


def count_optional_parameters(schema: Any) -> int:
    """Cuenta los parámetros opcionales del esquema.

    Un parámetro opcional es cada propiedad de un objeto que no figura en el ``required`` de ese
    mismo objeto. Es la definición que usa el dialecto: ``required`` y ``properties`` viven en el
    mismo nivel, así que el recuento es por objeto, no global.
    """
    total = 0
    for node in _walk_schema_nodes(schema):
        properties = node.get("properties")
        if not isinstance(properties, dict):
            continue
        required = node.get("required")
        required_names = set(required) if isinstance(required, list) else set()
        total += sum(1 for name in properties if name not in required_names)
    return total


def count_union_parameters(schema: Any) -> int:
    """Cuenta los parámetros de unión del esquema: cada nodo con ``anyOf`` es uno."""
    return sum(1 for node in _walk_schema_nodes(schema) if "anyOf" in node)


def validate_complexity_limits(schema: Any) -> None:
    """Exige los límites de complejidad documentados por el proveedor.

    Se comprueban **antes** de cualquier llamada HTTP: un esquema demasiado complejo no se
    descubre con un 400, se descubre aquí.

    Raises:
        SchemaValidationError: si se supera el máximo de opcionales o de uniones.
    """
    optional = count_optional_parameters(schema)
    if optional > MAX_OPTIONAL_PARAMETERS:
        raise SchemaValidationError(
            f"el esquema declara {optional} parámetros opcionales y el máximo es "
            f"{MAX_OPTIONAL_PARAMETERS}"
        )
    unions = count_union_parameters(schema)
    if unions > MAX_UNION_PARAMETERS:
        raise SchemaValidationError(
            f"el esquema declara {unions} parámetros de unión (anyOf) y el máximo es "
            f"{MAX_UNION_PARAMETERS}"
        )


def _walk_schema_nodes(node: Any) -> list[dict[str, Any]]:
    """Todos los nodos de esquema alcanzables, en orden determinista."""
    found: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            found.extend(_walk_schema_nodes(item))
        return found
    if not isinstance(node, dict):
        return found
    found.append(node)
    for keyword in ("properties",):
        value = node.get(keyword)
        if isinstance(value, dict):
            for child in value.values():
                found.extend(_walk_schema_nodes(child))
    for keyword in ("items", "additionalProperties"):
        value = node.get(keyword)
        if isinstance(value, dict):
            found.extend(_walk_schema_nodes(value))
    for keyword in ("anyOf", "allOf"):
        value = node.get(keyword)
        if isinstance(value, list):
            found.extend(_walk_schema_nodes(value))
    return found


def provider_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Provider schema de un modelo Pydantic.

    Deja explícita la separación: el modelo sigue validando con su contrato **original**; esto
    solo produce la versión que viaja al proveedor.
    """
    return prepare_json_schema(model.model_json_schema())


def validate_provider_schema(schema: Any) -> None:
    """Recorre **todos** los nodos y exige el contrato de PUNTO.

    No basta con mirar la raíz: un ``minLength`` escondido en el ``items`` de un array anidado
    rompería la petición igual que uno en la raíz. Se comprueba, en cada nodo:

    - que no queden ``$ref``/``$defs`` ni restricciones no admitidas;
    - que ningún keyword sea desconocido para PUNTO;
    - que los objetos con ``properties`` sean cerrados (``additionalProperties: false``);
    - que los arrays declaren ``items`` y las uniones sean listas no vacías de esquemas;
    - que ``required`` no mencione campos inexistentes.

    Raises:
        SchemaValidationError: con la ruta exacta del nodo que incumple.
    """
    _validate_node(schema, path="", root=True)


def _validate_node(node: Any, *, path: str, root: bool = False) -> None:
    """Valida un nodo y desciende por sus hijos."""
    if not isinstance(node, dict):
        return
    location = path or "(raíz)"

    for keyword in node:
        if keyword in RESOLVED_KEYWORDS:
            raise SchemaValidationError(f"{location}: no puede contener {keyword!r}")
        if keyword in UNSUPPORTED_CONSTRAINTS:
            raise SchemaValidationError(
                f"{location}: la restricción {keyword!r} no está admitida por el dialecto del "
                "proveedor"
            )
        if keyword in DROPPED_KEYWORDS or keyword in SUPPORTED_KEYWORDS:
            continue
        raise SchemaValidationError(
            f"{location}: keyword {keyword!r} que PUNTO no sabe transportar"
        )

    node_type = node.get("type")
    if node_type is not None and (not isinstance(node_type, str) or node_type not in ALLOWED_TYPES):
        raise SchemaValidationError(
            f"{location}: 'type' debe ser uno de {', '.join(sorted(ALLOWED_TYPES))}, "
            f"no {node_type!r}"
        )

    # El proveedor solo admite el objeto **cerrado**. Un esquema de mapa
    # (``additionalProperties: {...}``) cambiaría la semántica del modelo si se transformara,
    # así que se rechaza antes de llamar a la API.
    if "additionalProperties" in node and node["additionalProperties"] is not False:
        raise SchemaValidationError(
            f"{location}: 'additionalProperties' debe ser exactamente false; "
            f"{node['additionalProperties']!r} no está admitido por el dialecto"
        )

    properties = node.get("properties")
    if properties is not None:
        if node_type != "object":
            raise SchemaValidationError(f"{location}: 'properties' exige type='object'")
        if not isinstance(properties, dict) or not properties:
            raise SchemaValidationError(f"{location}: 'properties' debe ser un objeto no vacío")
        required = node.get("required")
        if not isinstance(required, list) or not required:
            raise SchemaValidationError(
                f"{location}: 'required' debe listar al menos un campo"
            )
        if not all(isinstance(item, str) for item in required):
            raise SchemaValidationError(
                f"{location}: 'required' debe contener solo nombres de campo"
            )
        unknown = sorted(set(required) - set(properties))
        if unknown:
            raise SchemaValidationError(
                f"{location}: 'required' menciona campos inexistentes: {', '.join(unknown)}"
            )
        for name, child in properties.items():
            _validate_node(child, path=f"{path}.properties.{name}" if path else name)

    if node_type == "object" and node.get("additionalProperties") is not False:
        raise SchemaValidationError(
            f"{location}: un objeto debe declarar 'additionalProperties: false'"
        )

    if node_type == "array":
        items = node.get("items")
        if not isinstance(items, dict):
            # No basta con que exista la clave: el dialecto exige un **esquema** de items.
            raise SchemaValidationError(
                f"{location}: 'items' debe ser un esquema de objeto, no {type(items).__name__}"
            )
        _validate_node(items, path=f"{path}.items".lstrip("."))

    if "minItems" in node:
        value = node["minItems"]
        unsupported = (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value not in SUPPORTED_MIN_ITEMS
        )
        if unsupported:
            raise SchemaValidationError(
                f"{location}: 'minItems' solo admite {sorted(SUPPORTED_MIN_ITEMS)}, no {value!r}"
            )

    if "format" in node:
        value = node["format"]
        if not isinstance(value, str) or value not in ALLOWED_FORMATS:
            raise SchemaValidationError(
                f"{location}: 'format' debe ser uno de {', '.join(sorted(ALLOWED_FORMATS))}, "
                f"no {value!r}"
            )

    if "enum" in node:
        values = node["enum"]
        if not isinstance(values, list) or not values:
            raise SchemaValidationError(f"{location}: 'enum' debe ser una lista no vacía")
        for item in values:
            if not _is_scalar(item):
                raise SchemaValidationError(
                    f"{location}: 'enum' solo admite valores escalares; {type(item).__name__} "
                    "no lo es"
                )

    if "const" in node and not _is_scalar(node["const"]):
        raise SchemaValidationError(
            f"{location}: 'const' solo admite valores escalares"
        )

    for keyword in ("anyOf", "allOf"):
        branches = node.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, list) or not branches:
            raise SchemaValidationError(f"{location}: {keyword!r} debe ser una lista no vacía")
        for index, branch in enumerate(branches):
            if not isinstance(branch, dict):
                raise SchemaValidationError(
                    f"{location}: {keyword!r}[{index}] debe ser un esquema"
                )
            _validate_node(branch, path=f"{path}.{keyword}[{index}]".lstrip("."))

    if root and node_type != "object":
        raise SchemaValidationError("(raíz): 'type' debe ser 'object'")


def _is_scalar(value: Any) -> bool:
    """True si el valor es un escalar JSON: cadena, número, booleano o nulo."""
    return value is None or isinstance(value, str | int | float | bool)


def _transform(node: Any, *, path: str) -> Any:
    """Retira del provider schema lo que el dialecto no admite y lo anota."""
    if isinstance(node, list):
        return [_transform(item, path=path) for item in node]
    if not isinstance(node, dict):
        return node

    result: dict[str, Any] = {}
    notes: list[str] = []
    for keyword, value in node.items():
        if keyword in RESOLVED_KEYWORDS:
            continue
        if keyword in DROPPED_KEYWORDS:
            continue
        if keyword == "minItems":
            result[keyword], note = _transform_min_items(value)
            if note:
                notes.append(note)
        elif keyword in UNSUPPORTED_CONSTRAINTS:
            notes.append(_describe(keyword, value))
        elif keyword == "properties" and isinstance(value, dict):
            result[keyword] = {
                name: _transform(child, path=f"{path}.properties.{name}".lstrip("."))
                for name, child in value.items()
            }
        elif keyword == "items" and isinstance(value, dict):
            result[keyword] = _transform(value, path=f"{path}.items".lstrip("."))
        elif keyword in ("anyOf", "allOf") and isinstance(value, list):
            result[keyword] = [
                _transform(branch, path=f"{path}.{keyword}[{index}]".lstrip("."))
                for index, branch in enumerate(value)
            ]
        else:
            # ``additionalProperties`` cae aquí a propósito cuando no es ``False``: se conserva
            # tal cual para que la validación lo **rechace**, en vez de transformarlo y cambiar
            # en silencio la semántica del modelo.
            result[keyword] = value

    if notes:
        # La restricción retirada se cuenta en la descripción: es información para el modelo,
        # nunca enforcement. La garantía sigue en el modelo Pydantic original.
        suffix = " ".join(f"({note}.)" for note in notes)
        description = str(result.get("description", "")).strip()
        result["description"] = f"{description} {suffix}".strip()
    return result


def _transform_min_items(value: Any) -> tuple[int, str]:
    """Aplica la regla de ``minItems``: 0 y 1 viajan, más de 1 se retira, lo ilegible falla.

    Raises:
        SchemaValidationError: si el valor no es un entero no negativo.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchemaValidationError(
            f"'minItems' debe ser un entero no negativo, no {value!r}"
        )
    if value in SUPPORTED_MIN_ITEMS:
        return value, ""
    return 0, _describe("minItems", value)


def _describe(keyword: str, value: Any) -> str:
    """Traducción legible de una restricción retirada."""
    template = CONSTRAINT_NOTES[keyword]
    if keyword == "uniqueItems":
        return template
    rendered = value if isinstance(value, int | float) else str(value)
    return template.format(value=rendered)


def _resolve(node: Any, defs: dict[str, Any], *, depth: int, path: str) -> Any:
    """Sustituye cada ``$ref`` local por su definición, con detección de ciclos."""
    if depth > MAX_REF_DEPTH:
        raise SchemaValidationError(
            "el esquema tiene referencias anidadas más profundas de lo que PUNTO expande "
            f"({MAX_REF_DEPTH})"
        )
    if isinstance(node, list):
        return [_resolve(item, defs, depth=depth, path=path) for item in node]
    if not isinstance(node, dict):
        return node

    reference = node.get("$ref")
    if isinstance(reference, str):
        target = _lookup(reference, defs)
        if target is None:
            raise SchemaValidationError(f"referencia no resoluble en el esquema: {reference!r}")
        if _is_cyclic(reference, target, defs, seen=()):
            raise SchemaValidationError(
                f"el esquema tiene un ciclo de referencias en {reference!r}: PUNTO no lo inlinea"
            )
        resolved = _resolve(target, defs, depth=depth + 1, path=path)
        if not isinstance(resolved, dict):  # pragma: no cover - defensivo
            raise SchemaValidationError(f"la referencia {reference!r} no apunta a un objeto")
        return _merge_siblings(resolved, node, reference=reference, path=path)

    return {
        keyword: _resolve(value, defs, depth=depth, path=f"{path}.{keyword}".lstrip("."))
        for keyword, value in node.items()
        if keyword not in RESOLVED_KEYWORDS
    }


def _merge_siblings(
    target: dict[str, Any], node: dict[str, Any], *, reference: str, path: str
) -> dict[str, Any]:
    """Fusiona los siblings de un ``$ref`` sin sobrescribir estructura en silencio.

    Los siblings puramente anotativos (``title``, ``description``, ``default``) se heredan: son
    información, no restricciones. Un sibling **estructural** que difiera del destino es un
    conflicto y se rechaza: elegir uno de los dos en silencio cambiaría el contrato.
    """
    merged = dict(target)
    for keyword, value in node.items():
        if keyword == "$ref":
            continue
        if keyword in ANNOTATION_KEYWORDS:
            merged[keyword] = value
            continue
        if keyword in DROPPED_KEYWORDS:
            continue
        if keyword in RESOLVED_KEYWORDS:
            raise SchemaValidationError(
                f"{path or '(raíz)'}: el sibling {keyword!r} del $ref {reference!r} no se puede "
                "resolver sin ambigüedad"
            )
        if keyword in merged and merged[keyword] != value:
            raise SchemaValidationError(
                f"{path or '(raíz)'}: el sibling {keyword!r} del $ref {reference!r} entra en "
                "conflicto estructural con el destino"
            )
        if keyword not in merged:
            raise SchemaValidationError(
                f"{path or '(raíz)'}: el sibling {keyword!r} del $ref {reference!r} añade "
                "estructura que PUNTO no puede fusionar sin ambigüedad"
            )
        merged[keyword] = value
    return merged


def _lookup(reference: str, defs: dict[str, Any]) -> Any | None:
    """Resuelve una referencia local ``#/$defs/Nombre`` dentro de ``defs``."""
    for prefix in (f"{LOCAL_REF_PREFIX}$defs/", f"{LOCAL_REF_PREFIX}definitions/"):
        if reference.startswith(prefix):
            return defs.get(reference[len(prefix) :])
    return None


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
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                _collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, found)


__all__ = [
    "ALLOWED_FORMATS",
    "ALLOWED_TYPES",
    "ANNOTATION_KEYWORDS",
    "CONSTRAINT_NOTES",
    "DROPPED_KEYWORDS",
    "LOCAL_REF_PREFIX",
    "MAX_OPTIONAL_PARAMETERS",
    "MAX_REF_DEPTH",
    "MAX_UNION_PARAMETERS",
    "RESOLVED_KEYWORDS",
    "STRUCTURAL_KEYWORDS",
    "SUPPORTED_KEYWORDS",
    "SUPPORTED_MIN_ITEMS",
    "UNSUPPORTED_CONSTRAINTS",
    "SchemaValidationError",
    "count_optional_parameters",
    "count_union_parameters",
    "prepare_json_schema",
    "provider_schema_for",
    "validate_complexity_limits",
    "validate_provider_schema",
]
