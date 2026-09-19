"""Contrato mínimo de una **solicitud de construcción gobernada** (PILOT-03).

Una solicitud es la intención de una persona expresada como datos: qué se quiere, sobre qué destino
declarado por PUNTO y con qué criterios. No es una orden de ejecución y no puede transportar
autoridad: el vocabulario es cerrado (``extra="forbid"``), el rol sale de una lista cerrada y el
destino tiene que ser un identificador **registrado en PUNTO**, nunca una ruta libre.

El resultado separa con nitidez dos cosas que no son lo mismo:

- lo que dijo el proveedor (``provider_status``, ``proposal``, ``usage``, ``error``), que es
  inteligencia externa **no confiable**;
- lo que decidió PUNTO (``status``, ``validation_status``, ``validation_issues``, ``authority``).

``authority`` es siempre ``PROPOSAL_ONLY`` y lo escribe PUNTO: el proveedor no puede declararlo ni
ampliarlo. Una propuesta aceptada significa «texto válido para revisión humana», nunca «cambio
aprobado».
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from punto.common import utc_now
from punto.memory.retrieval import RetrievalStatus
from punto.providers.contract import ProviderRole, ProviderStatus
from punto.schemas.execution import ModelUsage

#: Cotas de la solicitud. Todas explícitas: una solicitud no puede crecer sin límite ni colarse como
#: canal de datos.
MAX_OBJECTIVE_CHARS: Final[int] = 2000
MAX_CONTEXT_CHARS: Final[int] = 2000
MAX_ITEMS: Final[int] = 10
MAX_ITEM_CHARS: Final[int] = 300
MAX_SCOPE_PATHS: Final[int] = 20
MAX_TARGET_ID_CHARS: Final[int] = 80

#: Cota de la propuesta aceptada, además del tope de tokens de salida.
MAX_PROPOSAL_CHARS: Final[int] = 20_000

#: Caracteres de control prohibidos en cualquier texto de entrada (salvo tabulador y salto).
_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Esquema de URI: un destino de PUNTO no es una URL.
_URI_SCHEME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


class BuildAdmission(StrEnum):
    """Admisión de la solicitud en la frontera del ciclo."""

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ValidationVerdict(StrEnum):
    """Veredicto de PUNTO sobre la salida del proveedor."""

    NOT_RUN = "NOT_RUN"
    VALID = "VALID"
    INVALID = "INVALID"


class BuildRequestStatus(StrEnum):
    """Estado final del ciclo de construcción."""

    ACCEPTED = "PROPOSAL_ACCEPTED"
    REJECTED = "REQUEST_REJECTED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    INVALID_PROVIDER_OUTPUT = "INVALID_PROVIDER_OUTPUT"


class BuildValidationIssue(BaseModel):
    """Un problema concreto detectado por PUNTO: código estable y detalle acotado."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=60)
    detail: str = Field(default="", max_length=300)

    def as_text(self) -> str:
        """Texto legible del problema."""
        return f"{self.code}: {self.detail}" if self.detail else self.code


def _clean_text(value: str, *, field: str, limit: int, allow_empty: bool = False) -> str:
    """Normaliza un texto de entrada y rechaza lo que no puede viajar en una solicitud.

    Raise:
        ValueError: si queda vacío, pasa del límite o trae caracteres de control.
    """
    text = " ".join(value.split()) if field != "context" else value.strip()
    if _CONTROL_CHARS.search(text):
        raise ValueError(f"{field} contiene caracteres de control no permitidos")
    if not text and not allow_empty:
        raise ValueError(f"{field} no puede estar vacío")
    if len(text) > limit:
        raise ValueError(f"{field} supera el máximo de {limit} caracteres")
    return text


def _clean_path(value: str) -> str:
    """Normaliza una ruta de alcance y rechaza lo que no es una ruta relativa del destino.

    Raise:
        ValueError: si es absoluta, trae ``..``, es una URI o contiene caracteres de control.
    """
    text = value.strip().replace("\\", "/")
    if not text:
        raise ValueError("scope_paths no admite entradas vacías")
    if _CONTROL_CHARS.search(text):
        raise ValueError(f"scope_paths contiene caracteres de control: {text!r}")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"scope_paths tiene que ser relativo al destino: {text!r}")
    if _URI_SCHEME.match(text):
        raise ValueError(f"scope_paths no admite URI: {text!r}")
    if ".." in text.split("/"):
        raise ValueError(f"scope_paths no admite '..': {text!r}")
    return text.strip("/")


class BuildRequest(BaseModel):
    """Solicitud de construcción gobernada.

    ``target_repository`` es la **clave** de un destino registrado en PUNTO: la ruta real y las
    raíces permitidas viven en la configuración confiable del motor, nunca en la solicitud.
    ``scope_paths`` acota la propuesta y **no** concede permiso de lectura ni de escritura.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID = Field(default_factory=uuid4, description="Correlación de todo el ciclo.")
    objective: str = Field(description="Qué se quiere conseguir, en lenguaje declarativo.")
    target_repository: str = Field(description="Clave de un destino registrado en PUNTO.")
    requested_role: ProviderRole = Field(description="Rol que debe atender la solicitud.")
    constraints: tuple[str, ...] = Field(default=(), description="Restricciones declaradas.")
    acceptance_criteria: tuple[str, ...] = Field(default=(), description="Criterios verificables.")
    scope_paths: tuple[str, ...] = Field(
        default=(), description="Rutas del destino a las que se acota la propuesta."
    )
    context: str = Field(default="", description="Contexto declarado por quien pide.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")

    @field_validator("objective")
    @classmethod
    def _objetivo(cls, value: str) -> str:
        """El objetivo no puede estar vacío y no lleva caracteres de control."""
        return _clean_text(value, field="objective", limit=MAX_OBJECTIVE_CHARS)

    @field_validator("context")
    @classmethod
    def _contexto(cls, value: str) -> str:
        """El contexto es opcional y se acota."""
        return _clean_text(value, field="context", limit=MAX_CONTEXT_CHARS, allow_empty=True)

    @field_validator("target_repository")
    @classmethod
    def _destino(cls, value: str) -> str:
        """El destino es una clave, no una ruta ni una URL."""
        text = _clean_text(value, field="target_repository", limit=MAX_TARGET_ID_CHARS)
        if "/" in text or "\\" in text or _URI_SCHEME.match(text):
            raise ValueError(
                "target_repository es la clave de un destino registrado en PUNTO, no una ruta"
            )
        return text

    @field_validator("constraints", "acceptance_criteria")
    @classmethod
    def _listas(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Listas acotadas, sin vacíos y sin duplicados, conservando el orden."""
        if len(value) > MAX_ITEMS:
            raise ValueError(f"la lista admite como máximo {MAX_ITEMS} elementos")
        cleaned: list[str] = []
        for item in value:
            text = _clean_text(item, field="elemento de lista", limit=MAX_ITEM_CHARS)
            if text not in cleaned:
                cleaned.append(text)
        return tuple(cleaned)

    @field_validator("scope_paths")
    @classmethod
    def _rutas(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Rutas relativas y saneadas, sin duplicados."""
        if len(value) > MAX_SCOPE_PATHS:
            raise ValueError(f"scope_paths admite como máximo {MAX_SCOPE_PATHS} rutas")
        cleaned: list[str] = []
        for item in value:
            text = _clean_path(item)
            if text not in cleaned:
                cleaned.append(text)
        return tuple(cleaned)

    @model_validator(mode="after")
    def _sin_ordenes(self) -> BuildRequest:
        """Una solicitud no es una orden: se rechaza el objetivo que pide ejecución directa.

        No es un filtro semántico exhaustivo (eso sería frágil): es una frontera declarada contra el
        caso evidente de una intención que pide ejecutar en vez de proponer.
        """
        lowered = self.objective.casefold()
        markers = (
            "ejecuta ",
            "execute ",
            "run shell",
            "shell:",
            "apply the patch",
            "aplica el cambio",
        )
        for marker in markers:
            if marker in lowered:
                raise ValueError(
                    "la solicitud pide ejecución directa: PUNTO solo admite propuestas para revisar"
                )
        return self

    def as_public_dict(self) -> dict[str, object]:
        """Vista publicable de la solicitud (sin secretos: la solicitud no puede llevarlos)."""
        return {
            "request_id": str(self.request_id),
            "objective": self.objective,
            "target_repository": self.target_repository,
            "requested_role": self.requested_role.value,
            "constraints": list(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "scope_paths": list(self.scope_paths),
            "created_at": self.created_at.isoformat(),
        }


class BuildResult(BaseModel):
    """Resultado normalizado del ciclo: lo que dijo el proveedor y lo que decidió PUNTO."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    status: BuildRequestStatus
    role: ProviderRole
    provider: str = ""
    model: str = ""
    capability_declared: bool | None = Field(
        default=None, description="Si la tabla declarativa de capacidades reconoce la pareja."
    )
    provider_status: ProviderStatus | None = None
    proposal: str | None = Field(
        default=None, description="Texto inerte propuesto por el proveedor."
    )
    validation_status: ValidationVerdict = ValidationVerdict.NOT_RUN
    validation_issues: tuple[BuildValidationIssue, ...] = ()
    pell_status: RetrievalStatus = RetrievalStatus.DISABLED
    trusted_experience_ids: tuple[str, ...] = ()
    failed_experience_ids: tuple[str, ...] = ()
    usage: ModelUsage | None = Field(
        default=None,
        description=(
            "Consumo **reportado** por el proveedor. ``None`` significa que no lo reportó (por "
            "ejemplo, un transporte de suscripción): no es cero consumo y no se estima. La "
            "auditoría lo declara como USAGE_NOT_REPORTED."
        ),
    )
    duration_ms: int | None = None
    error_kind: str = ""
    error: str = ""
    authority: Literal["PROPOSAL_ONLY"] = "PROPOSAL_ONLY"

    @property
    def accepted(self) -> bool:
        """True solo si PUNTO aceptó la propuesta para revisión."""
        return self.status is BuildRequestStatus.ACCEPTED

    def as_public_dict(self) -> dict[str, object]:
        """Vista serializable, sin contexto interno ni metadatos crudos."""
        return {
            "request_id": str(self.request_id),
            "status": self.status.value,
            "role": self.role.value,
            "provider": self.provider,
            "model": self.model,
            "capability_declared": self.capability_declared,
            "provider_status": None if self.provider_status is None else self.provider_status.value,
            "proposal": self.proposal,
            "validation_status": self.validation_status.value,
            "validation_issues": [issue.as_text() for issue in self.validation_issues],
            "pell_status": self.pell_status.value,
            "trusted_experience_ids": list(self.trusted_experience_ids),
            "failed_experience_ids": list(self.failed_experience_ids),
            "usage": None if self.usage is None else self.usage.model_dump(mode="json"),
            "duration_ms": self.duration_ms,
            "error_kind": self.error_kind,
            "error": self.error,
            "authority": self.authority,
        }


__all__ = [
    "MAX_CONTEXT_CHARS",
    "MAX_ITEMS",
    "MAX_ITEM_CHARS",
    "MAX_OBJECTIVE_CHARS",
    "MAX_PROPOSAL_CHARS",
    "MAX_SCOPE_PATHS",
    "MAX_TARGET_ID_CHARS",
    "BuildAdmission",
    "BuildRequest",
    "BuildRequestStatus",
    "BuildResult",
    "BuildValidationIssue",
    "ValidationVerdict",
]
