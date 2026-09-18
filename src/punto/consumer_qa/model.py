"""Modelo mínimo de un caso de QA de consumidor y de su resultado (QA CONSUMER v0).

Un caso declara **qué aplicación** se abre (``start_url``), **qué hace el usuario** (``steps``) y
**qué tiene que observar** (``expectations``). El vocabulario de pasos y de expectativas es cerrado:
un paso o una expectativa que este consumidor no sabe ejecutar invalida el caso en vez de aprobarlo.

QA Consumer **evalúa resultados**: no tiene autoridad sobre el motor, no modifica capacidades,
``ResourceSet``, Human Gate ni políticas, y no arregla nada. Solo dice ``PASS``, ``FAIL`` o ``SKIP``
y aporta evidencia.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Versión del esquema de caso que entiende este consumidor.
SCHEMA_VERSION = 1


class QAStatus(StrEnum):
    """Resultado de ejecutar un caso de consumidor."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


class QAStepKind(StrEnum):
    """Lo que un usuario puede hacer en la aplicación."""

    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SUBMIT = "submit"
    WAIT = "wait"
    ASSERT_VISIBLE = "assert_visible"


class QAExpectationKind(StrEnum):
    """Lo que el caso espera observar. Nada de estética: funcionalidad observable."""

    VISIBLE = "visible"
    TEXT_CONTAINS = "text_contains"
    URL_MATCHES = "url_matches"
    HTTP_OK = "http_ok"
    NO_CONSOLE_ERRORS = "no_console_errors"
    NO_JS_EXCEPTIONS = "no_js_exceptions"


#: Pasos cuyo destino es un selector CSS.
SELECTOR_STEPS: frozenset[QAStepKind] = frozenset(
    {
        QAStepKind.CLICK,
        QAStepKind.FILL,
        QAStepKind.SUBMIT,
        QAStepKind.WAIT,
        QAStepKind.ASSERT_VISIBLE,
    }
)

#: Expectativas que se comprueban **en el navegador** (las demás se comprueban con los hechos que
#: la sesión devolvió: URL final, código HTTP, consola y excepciones).
BROWSER_EXPECTATIONS: frozenset[QAExpectationKind] = frozenset(
    {QAExpectationKind.VISIBLE, QAExpectationKind.TEXT_CONTAINS}
)


class ConsumerQAError(Exception):
    """El caso o su ejecución no son válidos: nada puede salir ``PASS``."""


class QAStep(BaseModel):
    """Un paso del usuario: navegar, pulsar, rellenar, enviar, esperar o comprobar visibilidad."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: QAStepKind
    target: str = Field(default="", description="Selector CSS, o ruta si el paso es ``navigate``.")
    value: str = Field(default="", description="Texto del paso, solo para ``fill``.")

    @field_validator("target")
    @classmethod
    def _destino_obligatorio(cls, value: str) -> str:
        """Todo paso necesita un destino: un click sin selector no es un paso, es una intención."""
        if not value.strip():
            raise ValueError("target no puede estar vacío")
        return value

    def label(self) -> str:
        """Descripción determinista del paso, para la evidencia y el informe."""
        if self.kind is QAStepKind.FILL:
            return f"{self.kind.value} {self.target!r} value={self.value!r}"
        return f"{self.kind.value} {self.target!r}"


class QAExpectation(BaseModel):
    """Una expectativa: qué se espera observar y cómo se comprueba."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: QAExpectationKind
    target: str = Field(default="", description="Selector CSS, o texto/URL esperada.")
    value: str = Field(default="", description="Texto esperado, para ``text_contains``.")
    expected: str = Field(default="", description="Cómo se lee esta expectativa en el informe.")

    @field_validator("target")
    @classmethod
    def _objetivo_obligatorio(cls, value: str) -> str:
        """Sin objetivo no hay nada que comprobar."""
        if not value.strip():
            raise ValueError("target no puede estar vacío")
        return value

    def label(self) -> str:
        """Descripción determinista de la expectativa."""
        if self.expected:
            return self.expected
        if self.kind is QAExpectationKind.TEXT_CONTAINS:
            return f"{self.kind.value} {self.target!r} contiene {self.value!r}"
        return f"{self.kind.value} {self.target!r}"


class ConsumerQACase(BaseModel):
    """Un escenario de consumidor: qué se abre, qué se hace y qué se espera."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    qa_id: str = Field(pattern=r"^QA-[0-9]{3}$")
    title: str = Field(min_length=1)
    start_url: str = Field(default="/", min_length=1)
    steps: tuple[QAStep, ...] = ()
    expectations: tuple[QAExpectation, ...] = Field(min_length=1)
    schema_version: int = SCHEMA_VERSION
    description: str = ""
    tags: tuple[str, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def _version_soportada(cls, value: int) -> int:
        """Rechaza una versión de esquema que este consumidor no entiende."""
        if value != SCHEMA_VERSION:
            raise ValueError(f"schema_version no soportada: {value} != {SCHEMA_VERSION}")
        return value

    @field_validator("start_url")
    @classmethod
    def _ruta_local(cls, value: str) -> str:
        """La aplicación se abre en una ruta local: una URL absoluta sería otro destino."""
        if "://" in value:
            raise ValueError("start_url tiene que ser una ruta local, no una URL absoluta")
        return value if value.startswith("/") else f"/{value}"


@dataclass(frozen=True, slots=True)
class QAActionRecord:
    """Acción que el navegador ejecutó (o no pudo ejecutar)."""

    kind: str
    target: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True si la acción llegó a ejecutarse."""
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable de la acción."""
        return {
            "kind": self.kind,
            "target": self.target,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class QAFailure:
    """Un fallo concreto: qué se esperaba, qué se observó y dónde."""

    step: str
    expected: str
    observed: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable del fallo."""
        return {
            "step": self.step,
            "expected": self.expected,
            "observed": self.observed,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class QAEvidence:
    """Evidencia de la sesión: navegador, hechos observados y captura conservada."""

    url: str = ""
    final_url: str = ""
    final_route: str = ""
    http_status: int | None = None
    browser: str = ""
    playwright_version: str = ""
    console_errors: tuple[str, ...] = ()
    console_warnings: int = 0
    page_errors: tuple[str, ...] = ()
    failed_resources: tuple[str, ...] = ()
    actions: tuple[QAActionRecord, ...] = ()
    screenshot_path: str = ""
    screenshot_sha256: str = ""
    screenshot_bytes: int = 0
    screenshot_note: str = ""
    route_mismatch: bool = False
    load_error: str = ""

    @property
    def has_screenshot(self) -> bool:
        """True si la sesión produjo una captura verificada."""
        return bool(self.screenshot_sha256)

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable de la evidencia, sin bytes de imagen."""
        return {
            "url": self.url,
            "final_url": self.final_url,
            "final_route": self.final_route,
            "http_status": self.http_status,
            "browser": self.browser,
            "playwright_version": self.playwright_version,
            "console_errors": list(self.console_errors),
            "console_warnings": self.console_warnings,
            "page_errors": list(self.page_errors),
            "failed_resources": list(self.failed_resources),
            "actions": [action.as_dict() for action in self.actions],
            "screenshot_path": self.screenshot_path,
            "screenshot_sha256": self.screenshot_sha256,
            "screenshot_bytes": self.screenshot_bytes,
            "screenshot_note": self.screenshot_note,
            "route_mismatch": self.route_mismatch,
            "load_error": self.load_error,
        }


@dataclass(frozen=True, slots=True)
class QAResult:
    """Resultado estructurado mínimo de un caso de consumidor."""

    qa_id: str
    status: QAStatus
    failures: tuple[QAFailure, ...] = ()
    evidence: QAEvidence | None = None
    reason: str = ""
    infrastructure: bool = False
    notes: tuple[str, ...] = field(default=())

    @property
    def passed(self) -> bool:
        """True solo si el caso terminó en ``PASS``."""
        return self.status is QAStatus.PASS

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable del resultado, con las claves en orden estable."""
        return {
            "qa_id": self.qa_id,
            "status": self.status.value,
            "failures": [failure.as_dict() for failure in self.failures],
            "evidence": None if self.evidence is None else self.evidence.as_dict(),
            "reason": self.reason,
            "infrastructure": self.infrastructure,
            "notes": list(self.notes),
        }


__all__ = [
    "BROWSER_EXPECTATIONS",
    "SCHEMA_VERSION",
    "SELECTOR_STEPS",
    "ConsumerQACase",
    "ConsumerQAError",
    "QAActionRecord",
    "QAEvidence",
    "QAExpectation",
    "QAExpectationKind",
    "QAFailure",
    "QAResult",
    "QAStatus",
    "QAStep",
    "QAStepKind",
]
