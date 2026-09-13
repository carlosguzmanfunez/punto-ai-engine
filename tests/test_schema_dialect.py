"""Dialecto de JSON Schema para el proveedor (ENGINE-5.2.2, CA-01/CA-04/CA-05).

El esquema que produce Pydantic no es el esquema que el proveedor acepta. Enviarlo tal cual es
una petición de HTTP 400 esperando a ocurrir, y por eso aquí se comprueba, keyword a keyword:

- lo que el dialecto **no** admite se retira del provider schema y se anota en la descripción;
- lo que **sí** admite se conserva (una lista negra improvisada destruiría features válidas);
- los ``$ref`` se inlinean y sus siblings no pueden cambiar la estructura en silencio;
- y **todos** los nodos cumplen el contrato, no solo la raíz.

La garantía no se pierde al retirar una restricción: el modelo Pydantic original sigue
validando entero, y eso también se prueba aquí.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from punto.providers.json_schema import (
    SUPPORTED_KEYWORDS,
    UNSUPPORTED_CONSTRAINTS,
    SchemaValidationError,
    prepare_json_schema,
    provider_schema_for,
    validate_provider_schema,
)
from punto.schemas.cross_audit import CrossAuditProposal

#: Walker independiente: no usa el validador del módulo, para que el anti-regression test no
#: pueda pasar solo porque el validador y el transformador comparten el mismo error.
UNSUPPORTED_EVERYWHERE: tuple[str, ...] = tuple(UNSUPPORTED_CONSTRAINTS)


def assert_clean_everywhere(node: Any, path: str = "(raíz)") -> None:
    """Recorre el provider schema y exige que no quede nada fuera del dialecto.

    Solo desciende por posiciones que contienen **esquemas** (``properties.*``, ``items``,
    ``anyOf``, ``allOf``, ``additionalProperties``); el resto de valores son datos, no
    keywords, y tratarlos como tales daría falsos positivos.
    """
    if isinstance(node, list):
        for index, item in enumerate(node):
            assert_clean_everywhere(item, f"{path}[{index}]")
        return
    if not isinstance(node, dict):
        return
    for keyword, value in node.items():
        assert keyword not in UNSUPPORTED_EVERYWHERE, f"{path}: {keyword} no está admitido"
        assert keyword not in {"$ref", "$defs", "definitions"}, f"{path}: {keyword} residual"
        assert keyword in SUPPORTED_KEYWORDS, f"{path}: keyword inesperado {keyword!r}"
        if keyword == "properties" and isinstance(value, dict):
            for name, child in value.items():
                assert_clean_everywhere(child, f"{path}.properties.{name}")
        elif keyword in {"items", "additionalProperties"} and isinstance(value, dict):
            assert_clean_everywhere(value, f"{path}.{keyword}")
        elif keyword in {"anyOf", "allOf"} and isinstance(value, list):
            for index, branch in enumerate(value):
                assert_clean_everywhere(branch, f"{path}.{keyword}[{index}]")
    if node.get("type") == "object" and "properties" in node:
        assert node.get("additionalProperties") is False, f"{path}: objeto abierto"


class Annotated(BaseModel):
    """Modelo con restricciones que el dialecto del proveedor no admite."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identifier: str = Field(..., min_length=3, max_length=10, description="Identificador.")
    score: int = Field(..., ge=1, le=5, description="Puntuación.")
    tags: tuple[str, ...] = Field(..., max_length=3, description="Etiquetas.")
    optional_line: int | None = Field(default=None, ge=0, description="Línea opcional.")


class Nested(BaseModel):
    """Modelo anidado, para comprobar que la transformación es recursiva."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=2, description="Nombre.")
    inner: Annotated = Field(..., description="Detalle anidado.")
    others: tuple[Annotated, ...] = Field(default=(), description="Más detalles.")


# ---------------------------------------------------------------------------
# Transformación de restricciones no admitidas
# ---------------------------------------------------------------------------
def test_min_length_is_removed_and_annotated() -> None:
    """``minLength`` no viaja al proveedor, pero queda contado en la descripción."""
    prepared = prepare_json_schema(Annotated.model_json_schema())

    identifier = prepared["properties"]["identifier"]
    assert "minLength" not in identifier
    assert "maxLength" not in identifier
    assert "al menos 3 carácter" in identifier["description"]
    assert "como máximo 10 carácter" in identifier["description"]


def test_minimum_is_removed_including_inside_any_of() -> None:
    """Una restricción escondida en una rama de ``anyOf`` se trata igual que en la raíz."""
    prepared = prepare_json_schema(Annotated.model_json_schema())

    score = prepared["properties"]["score"]
    assert "minimum" not in score and "maximum" not in score
    assert "mayor o igual que 1" in score["description"]

    line = prepared["properties"]["optional_line"]
    assert "anyOf" in line
    integer_branch = line["anyOf"][0]
    assert "minimum" not in integer_branch
    assert "mayor o igual que 0" in integer_branch["description"]


def test_nested_restrictions_are_transformed_recursively() -> None:
    """La transformación baja por objetos, arrays y uniones anidados."""
    prepared = prepare_json_schema(Nested.model_json_schema())

    inner = prepared["properties"]["inner"]["properties"]
    assert "minLength" not in inner["identifier"]
    assert "minimum" not in inner["score"]

    items = prepared["properties"]["others"]["items"]
    assert items["type"] == "object"
    assert "minLength" not in items["properties"]["identifier"]
    assert "name" in prepared["properties"]
    assert "minLength" not in prepared["properties"]["name"]

    assert_clean_everywhere(prepared)


def test_supported_features_are_preserved() -> None:
    """Lo que el dialecto admite no se destruye por precaución."""

    class Mixed(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")

        kind: str = Field(..., pattern="^[a-z]+$", description="Tipo.")
        level: int = Field(..., ge=0, description="Nivel.")
        items: tuple[str, ...] = Field(..., min_length=1, description="Elementos.")

    prepared = prepare_json_schema(Mixed.model_json_schema())

    assert prepared["properties"]["kind"]["pattern"] == "^[a-z]+$"
    assert prepared["properties"]["items"]["minItems"] == 1
    assert prepared["properties"]["items"]["type"] == "array"
    assert prepared["additionalProperties"] is False
    assert prepared["required"] == ["kind", "level", "items"]
    assert prepared["type"] == "object"


def test_enum_and_const_survive_the_transformation() -> None:
    """``enum`` y ``const`` son parte del dialecto y llegan intactos."""
    prepared = prepare_json_schema(CrossAuditProposal.model_json_schema())

    severity = prepared["properties"]["findings"]["items"]["properties"]["severity"]
    assert severity["type"] == "string"
    assert "CRITICAL" in severity["enum"]


def test_default_is_preserved() -> None:
    """``default`` es anotación admitida: se conserva."""
    prepared = prepare_json_schema(Annotated.model_json_schema())

    assert prepared["properties"]["optional_line"]["default"] is None


# ---------------------------------------------------------------------------
# Lo que PUNTO no sabe transportar
# ---------------------------------------------------------------------------
def test_unknown_keyword_is_rejected() -> None:
    """Un keyword que PUNTO no sabe transportar no puede llegar al proveedor en silencio."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
        "additionalProperties": False,
        "propertyNames": {"pattern": "^[a-z]$"},
    }

    with pytest.raises(SchemaValidationError, match="no sabe transportar"):
        prepare_json_schema(schema)


def test_unknown_keyword_is_rejected_when_nested() -> None:
    """El keyword inesperado puede estar en un nodo interno: la inspección es recursiva."""
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"inner": {"type": "string", "contentMediaType": "text/plain"}},
                "required": ["inner"],
                "additionalProperties": False,
            }
        },
        "required": ["outer"],
        "additionalProperties": False,
    }

    with pytest.raises(SchemaValidationError, match="no sabe transportar"):
        prepare_json_schema(schema)


def test_recursive_validation_requires_closed_objects() -> None:
    """Un objeto anidado con ``properties`` y sin cerrar rompe el contrato."""
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"inner": {"type": "string"}},
                "required": ["inner"],
            }
        },
        "required": ["outer"],
        "additionalProperties": False,
    }

    with pytest.raises(SchemaValidationError, match="additionalProperties"):
        validate_provider_schema(schema)


def test_recursive_validation_requires_array_items() -> None:
    """Un array sin ``items`` no dice qué contiene."""
    schema = {
        "type": "object",
        "properties": {"lista": {"type": "array"}},
        "required": ["lista"],
        "additionalProperties": False,
    }

    with pytest.raises(SchemaValidationError, match="items"):
        validate_provider_schema(schema)


def test_recursive_validation_rejects_empty_unions() -> None:
    """Una unión vacía no es un contrato."""
    schema = {
        "type": "object",
        "properties": {"valor": {"anyOf": []}},
        "required": ["valor"],
        "additionalProperties": False,
    }

    with pytest.raises(SchemaValidationError, match="lista no vacía"):
        validate_provider_schema(schema)


def test_additional_properties_as_schema_is_allowed() -> None:
    """``additionalProperties`` con esquema (diccionarios) es válido y se inspecciona."""
    schema = {
        "type": "object",
        "properties": {"mapa": {"type": "object", "additionalProperties": {"type": "integer"}}},
        "required": ["mapa"],
        "additionalProperties": False,
    }

    validate_provider_schema(schema)


# ---------------------------------------------------------------------------
# $ref y siblings
# ---------------------------------------------------------------------------
def test_ref_siblings_annotations_are_merged() -> None:
    """``title``, ``description`` y ``default`` acompañan al destino sin romperlo."""
    schema = {
        "type": "object",
        "properties": {
            "item": {
                "$ref": "#/$defs/Item",
                "title": "Item anotado",
                "description": "Descripción del sibling.",
                "default": None,
            }
        },
        "required": ["item"],
        "additionalProperties": False,
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {"nombre": {"type": "string"}},
                "required": ["nombre"],
                "additionalProperties": False,
                "description": "Descripción del destino.",
            }
        },
    }

    prepared = prepare_json_schema(schema)
    item = prepared["properties"]["item"]

    assert item["title"] == "Item anotado"
    assert item["description"] == "Descripción del sibling."
    assert item["properties"]["nombre"]["type"] == "string"
    assert "default" in item


def test_ref_sibling_structural_conflict_is_rejected() -> None:
    """Un sibling que contradice la estructura del destino no se resuelve en silencio."""
    schema = {
        "type": "object",
        "properties": {"item": {"$ref": "#/$defs/Item", "type": "string"}},
        "required": ["item"],
        "additionalProperties": False,
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {"nombre": {"type": "string"}},
                "required": ["nombre"],
                "additionalProperties": False,
            }
        },
    }

    with pytest.raises(SchemaValidationError, match="conflicto estructural"):
        prepare_json_schema(schema)


def test_ref_sibling_adding_structure_is_rejected() -> None:
    """Añadir estructura desde el sibling tampoco se fusiona a ciegas."""
    schema = {
        "type": "object",
        "properties": {"item": {"$ref": "#/$defs/Item", "enum": ["a", "b"]}},
        "required": ["item"],
        "additionalProperties": False,
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {"nombre": {"type": "string"}},
                "required": ["nombre"],
                "additionalProperties": False,
            }
        },
    }

    with pytest.raises(SchemaValidationError, match="añade estructura"):
        prepare_json_schema(schema)


def test_identical_structural_sibling_is_tolerated() -> None:
    """Un sibling estructural idéntico al destino no es un conflicto."""
    schema = {
        "type": "object",
        "properties": {"item": {"$ref": "#/$defs/Item", "type": "object"}},
        "required": ["item"],
        "additionalProperties": False,
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {"nombre": {"type": "string"}},
                "required": ["nombre"],
                "additionalProperties": False,
            }
        },
    }

    prepared = prepare_json_schema(schema)

    assert prepared["properties"]["item"]["type"] == "object"


def test_cycles_are_rejected() -> None:
    """Un ciclo de referencias no se inlinea: se falla en vez de adivinar."""
    cyclic = {
        "type": "object",
        "properties": {"nodo": {"$ref": "#/$defs/Nodo"}},
        "required": ["nodo"],
        "additionalProperties": False,
        "$defs": {
            "Nodo": {
                "type": "object",
                "properties": {"hijo": {"$ref": "#/$defs/Nodo"}},
                "required": ["hijo"],
                "additionalProperties": False,
            }
        },
    }

    with pytest.raises(SchemaValidationError, match="ciclo"):
        prepare_json_schema(cyclic)


def test_unresolvable_reference_is_rejected() -> None:
    """Una referencia que no existe no puede quedar colgando."""
    schema = {
        "type": "object",
        "properties": {"item": {"$ref": "#/$defs/NoExiste"}},
        "required": ["item"],
        "additionalProperties": False,
        "$defs": {},
    }

    with pytest.raises(SchemaValidationError, match="no resoluble"):
        prepare_json_schema(schema)


def test_depth_beyond_the_limit_is_rejected() -> None:
    """Una cadena de referencias absurdamente profunda se rechaza, no se expande sin fin."""
    defs: dict[str, Any] = {
        f"N{index}": {
            "type": "object",
            "properties": {"hijo": {"$ref": f"#/$defs/N{index + 1}"}},
            "required": ["hijo"],
            "additionalProperties": False,
        }
        for index in range(20)
    }
    defs["N20"] = {
        "type": "object",
        "properties": {"hoja": {"type": "string"}},
        "required": ["hoja"],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {"raiz": {"$ref": "#/$defs/N0"}},
        "required": ["raiz"],
        "additionalProperties": False,
        "$defs": defs,
    }

    with pytest.raises(SchemaValidationError, match="profund"):
        prepare_json_schema(schema)


# ---------------------------------------------------------------------------
# Anti-regresión del dialecto (§8) y frontera final de Pydantic
# ---------------------------------------------------------------------------
def test_cross_audit_provider_schema_is_clean_everywhere() -> None:
    """El schema real de producción no contiene nada fuera del dialecto, en ningún nodo."""
    prepared = provider_schema_for(CrossAuditProposal)

    assert_clean_everywhere(prepared)
    assert prepared["type"] == "object"
    assert set(prepared["properties"]) == {
        "summary",
        "findings",
        "architecture_assessment",
        "qa_assessment",
        "security_assessment",
        "maintainability_assessment",
        "scope_assessment",
        "recommendation_notes",
    }
    finding = prepared["properties"]["findings"]["items"]
    assert finding["type"] == "object"
    assert finding["additionalProperties"] is False
    assert "severity" in finding["required"]
    assert "category" in finding["properties"]
    # Serializable y sin claves colgando.
    assert json.loads(json.dumps(prepared)) == prepared


def test_original_schema_is_never_substituted() -> None:
    """El contrato original mantiene sus restricciones; solo el provider schema las suaviza."""
    original = CrossAuditProposal.model_json_schema()

    provider = provider_schema_for(CrossAuditProposal)
    again = CrossAuditProposal.model_json_schema()

    assert "minLength" in json.dumps(original)
    assert "minLength" not in json.dumps(provider)
    assert json.dumps(again) == json.dumps(original), "el modelo no se modificó"


def test_pydantic_still_enforces_what_the_provider_schema_dropped() -> None:
    """Retirar ``minLength`` del provider schema no relaja nada en PUNTO."""

    class Strict(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")

        identifier: str = Field(..., min_length=3, description="Identificador.")

    provider = prepare_json_schema(Strict.model_json_schema())
    assert "minLength" not in json.dumps(provider)

    with pytest.raises(ValidationError):
        Strict(identifier="ab")
    assert Strict(identifier="abc").identifier == "abc"


def test_nested_min_length_of_cross_audit_is_dropped_but_enforced() -> None:
    """El hallazgo anidado también pierde ``minLength`` en el provider schema."""
    provider = provider_schema_for(CrossAuditProposal)
    finding = provider["properties"]["findings"]["items"]

    assert "minLength" not in json.dumps(finding)

    with pytest.raises(ValidationError):
        CrossAuditProposal.model_validate(
            {
                "summary": "a" * 1,
                "findings": [
                    {
                        "id": "",
                        "severity": "HIGH",
                        "category": "CORRECTNESS",
                        "title": "t",
                        "description": "d",
                        "evidence": "e",
                    }
                ],
                "architecture_assessment": "El cambio respeta las capas del proyecto.",
                "qa_assessment": "QA cubrió el criterio con una prueba del comportamiento.",
                "security_assessment": "Security no encontró nada bloqueante en el contexto.",
                "maintainability_assessment": "Funciones cortas y nombres claros, sin deuda.",
                "scope_assessment": "El alcance se limita al objetivo de la tarea.",
            }
        )
