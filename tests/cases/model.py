"""Modelo mínimo de un caso canónico del directorio de casos (CASE DIRECTORY v0).

Un caso declara **qué situación** se reproduce (``description``), **qué entra** (``case_input``),
**qué se espera** (``expected``) y **qué garantía** protege (``title`` + ``description``). El
vocabulario de ``expected`` es **cerrado**: una clave que el runner no sabe comparar invalida el
caso en vez de aprobarlo, y un caso inválido nunca puede aparecer como ``PASS``.

El campo ``input`` del JSON se expone en Python como ``case_input`` porque ``input`` sombrea un
builtin y el lint del repositorio lo prohíbe; el nombre del fichero de datos no cambia.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Versión del esquema que entiende este runner. Un caso con otra versión es inválido.
SCHEMA_VERSION = 1

#: Vocabulario cerrado de ``expected``: lo que el runner sabe comparar contra los hechos.
EXPECTATION_KEYS: frozenset[str] = frozenset(
    {
        "accepted_revision_advanced",
        "active_generation_index",
        "audit_events_contains",
        "authority_unchanged",
        "autonomous",
        "change_class",
        "classification_category",
        "classification_eligibility",
        "containment_compatibility",
        "context_markers_contains",
        "expanded_dimensions_contains",
        "expanded_resources_contains",
        "failure_detail_contains",
        "gate_required",
        "generations",
        "memory_in_context",
        "node_failure_code",
        "node_status",
        "project_failure_code",
        "project_status",
        "provider_name",
        "provider_status",
        "qa_browser_contains",
        "qa_evidence_screenshot",
        "qa_failures",
        "qa_http_status",
        "qa_status",
        "replanner_calls",
        "replans_accepted",
        "resource_tokens_contains",
        "retrieval_status",
    }
)

#: Claves de contención: el hecho observado tiene que contener **todos** los valores declarados.
CONTAINMENT_KEYS: frozenset[str] = frozenset(
    {
        "audit_events_contains",
        "context_markers_contains",
        "expanded_dimensions_contains",
        "expanded_resources_contains",
        "failure_detail_contains",
        "resource_tokens_contains",
        "qa_browser_contains",
    }
)

#: Estados posibles de un caso. ``SKIP`` exige una razón explícita y verificable.
class CaseStatus(StrEnum):
    """Resultado de ejecutar un caso."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


class CaseCategory(StrEnum):
    """Categorías pequeñas y explícitas: la garantía que el caso protege."""

    AUTHORITY = "AUTHORITY"
    RESOURCE_CONTAINMENT = "RESOURCE_CONTAINMENT"
    HUMAN_GATE = "HUMAN_GATE"
    REPLAN = "REPLAN"
    MEMORY = "MEMORY"
    FAIL_CLOSED = "FAIL_CLOSED"
    CONSUMER_QA = "CONSUMER_QA"
    PROVIDER = "PROVIDER"


class CaseDirectoryError(Exception):
    """El directorio de casos es inválido: no se ejecuta nada y nada puede salir ``PASS``."""


class CaseInput(BaseModel):
    """Qué entra en el caso: el escenario registrado y sus parámetros declarativos."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class EngineCase(BaseModel):
    """Un caso canónico: situación, entrada, expectativa y trazabilidad a su origen."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    case_id: str = Field(pattern=r"^CASE-[0-9]{3}$")
    title: str = Field(min_length=1)
    category: CaseCategory
    description: str = Field(min_length=1)
    case_input: CaseInput = Field(alias="input")
    expected: dict[str, Any]
    tags: tuple[str, ...] = Field(min_length=1)
    source: str = Field(min_length=1)
    schema_version: int = SCHEMA_VERSION
    related_failure: str | None = None
    related_memory: str | None = None

    @field_validator("expected")
    @classmethod
    def _expected_es_conocido(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Rechaza una expectativa vacía o con claves fuera del vocabulario cerrado."""
        if not value:
            raise ValueError("expected no puede estar vacío")
        unknown = sorted(set(value) - EXPECTATION_KEYS)
        if unknown:
            raise ValueError(f"expected desconocido: {unknown}")
        return value

    @field_validator("schema_version")
    @classmethod
    def _version_soportada(cls, value: int) -> int:
        """Rechaza una versión de esquema que este runner no entiende."""
        if value != SCHEMA_VERSION:
            raise ValueError(f"schema_version no soportada: {value} != {SCHEMA_VERSION}")
        return value

    @field_validator("tags")
    @classmethod
    def _etiquetas_no_vacias(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Rechaza etiquetas vacías: los filtros se escriben con ellas."""
        if any(not tag.strip() for tag in value):
            raise ValueError("las etiquetas no pueden estar vacías")
        return value


@dataclass(frozen=True)
class Observation:
    """Hechos que el escenario observó en el motor, más su nota y su posible motivo de ``SKIP``."""

    facts: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    skip_reason: str | None = None


@dataclass(frozen=True)
class Comparison:
    """Resultado de comparar ``expected`` contra lo observado."""

    ok: bool
    reason: str
    infrastructure: bool = False


@dataclass(frozen=True)
class CaseResult:
    """Resultado estructurado mínimo de ejecutar un caso."""

    case_id: str
    status: CaseStatus
    expected: dict[str, Any]
    observed: dict[str, Any]
    reason: str
    duration_ms: int | None = None
    infrastructure: bool = False

    @property
    def passed(self) -> bool:
        """``True`` solo si el caso terminó en ``PASS``."""
        return self.status is CaseStatus.PASS

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable del resultado, con las claves en orden estable."""
        return {
            "case_id": self.case_id,
            "status": self.status.value,
            "expected": self.expected,
            "observed": self.observed,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
            "infrastructure": self.infrastructure,
        }


__all__ = [
    "CONTAINMENT_KEYS",
    "EXPECTATION_KEYS",
    "SCHEMA_VERSION",
    "CaseCategory",
    "CaseDirectoryError",
    "CaseInput",
    "CaseResult",
    "CaseStatus",
    "Comparison",
    "EngineCase",
    "Observation",
]
