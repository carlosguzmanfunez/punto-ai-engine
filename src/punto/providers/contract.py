"""Contrato normalizado de proveedor: petición, resultado, estados y salud (MULTI-PROVIDER v0).

ENGINE no habla con OpenAI, DeepSeek ni Anthropic: habla con este contrato. Los adaptadores traducen
su dialecto a estas estructuras, y el resto del motor no ve nunca una estructura de SDK.

La regla de autoridad vive aquí, escrita una sola vez:

    PROVIDER OUTPUT = UNTRUSTED EXTERNAL INTELLIGENCE

Un resultado de proveedor es **texto y datos**, no una autorización: no concede capacidades, no
amplía un ``ResourceSet``, no salta el Human Gate y no cambia políticas. Quien consuma un
``ProviderResult`` tiene que pasarlo por los contratos que ya existen; el contrato no ofrece ninguna
vía para hacer lo contrario.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final
from uuid import uuid4

from punto.providers.base import ImagePayload
from punto.schemas.execution import ModelUsage

#: Identificador del proveedor OpenAI en el contrato de PUNTO.
PROVIDER_OPENAI: Final[str] = "openai"

#: Cotas de una petición normalizada: una petición sin límites es una petición que nadie decidió.
MAX_INSTRUCTIONS_CHARS: Final[int] = 20_000
MAX_CONTEXT_CHARS: Final[int] = 200_000
MAX_METADATA_ITEMS: Final[int] = 16
MAX_METADATA_CHARS: Final[int] = 512


class ProviderRole(StrEnum):
    """Rol de orquestación que pide un modelo.

    Son los roles de esta fase. Los roles internos de los agentes de ENGINE-5.x siguen en
    :class:`punto.providers.routing.ModelRole`; este vocabulario es el de la orquestación
    multi-proveedor y su asignación se cambia sin tocar el motor.
    """

    ARCHITECT = "ARCHITECT"
    BUILDER = "BUILDER"
    VISUAL_QA = "VISUAL_QA"


class ProviderStatus(StrEnum):
    """Estado de una petición al proveedor."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class ProviderHealthStatus(StrEnum):
    """Estado de la comprobación de conexión de un proveedor."""

    CONNECTED = "CONNECTED"
    UNAVAILABLE = "UNAVAILABLE"
    AUTH_FAILED = "AUTH_FAILED"
    CONFIG_ERROR = "CONFIG_ERROR"


class ProviderErrorKind(StrEnum):
    """Fallo normalizado: lo que el motor puede auditar sin conocer el dialecto del proveedor."""

    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    AUTHENTICATION = "AUTHENTICATION"
    NETWORK = "NETWORK"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    REFUSAL = "REFUSAL"
    UNAVAILABLE = "UNAVAILABLE"
    CONFIG = "CONFIG"
    UNKNOWN = "UNKNOWN"


class ProviderContractError(ValueError):
    """La petición o el resultado no cumplen el contrato de PUNTO."""


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Petición normalizada: qué se pide, a qué rol y con qué material.

    ``attachments`` transporta imágenes cuyos bytes controla PUNTO (el mismo ``ImagePayload`` del
    contrato multimodal de ENGINE-5.2): el proveedor nunca recibe una ruta para leer por su cuenta.
    """

    role: ProviderRole
    instructions: str
    request_id: str = ""
    context: str = ""
    attachments: tuple[ImagePayload, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Valida la petición al construirla, no al gastarla."""
        if not self.instructions.strip():
            raise ProviderContractError("instructions no puede estar vacío")
        if len(self.instructions) > MAX_INSTRUCTIONS_CHARS:
            raise ProviderContractError(
                f"instructions ocupa {len(self.instructions)} caracteres y el máximo es "
                f"{MAX_INSTRUCTIONS_CHARS}"
            )
        if len(self.context) > MAX_CONTEXT_CHARS:
            raise ProviderContractError(
                f"context ocupa {len(self.context)} caracteres y el máximo es {MAX_CONTEXT_CHARS}"
            )
        if len(self.metadata) > MAX_METADATA_ITEMS:
            raise ProviderContractError(
                f"metadata tiene {len(self.metadata)} entradas y el máximo es {MAX_METADATA_ITEMS}"
            )
        for key, value in self.metadata.items():
            if len(str(value)) > MAX_METADATA_CHARS:
                raise ProviderContractError(
                    f"metadata[{key!r}] supera {MAX_METADATA_CHARS} caracteres"
                )

    @property
    def has_attachments(self) -> bool:
        """True si la petición lleva material visual."""
        return bool(self.attachments)


def make_request(
    role: ProviderRole,
    instructions: str,
    *,
    context: str = "",
    attachments: tuple[ImagePayload, ...] = (),
    metadata: Mapping[str, str] | None = None,
    request_id: str = "",
) -> ProviderRequest:
    """Construye una petición normalizada, con identificador propio si no se indica otro."""
    return ProviderRequest(
        role=role,
        instructions=instructions,
        request_id=request_id or uuid4().hex,
        context=context,
        attachments=attachments,
        metadata=dict(metadata or {}),
    )


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Resultado normalizado: el único formato en el que ENGINE ve lo que dijo un proveedor.

    ``content`` es **inteligencia externa no confiable**. ``structured_output`` es el mismo
    contenido interpretado como JSON cuando lo era; tampoco concede nada por sí mismo.
    """

    request_id: str
    provider: str
    model: str
    status: ProviderStatus
    role: ProviderRole | None = None
    content: str = ""
    structured_output: Mapping[str, Any] | None = None
    usage: ModelUsage | None = None
    error: str = ""
    error_kind: ProviderErrorKind | None = None
    duration_ms: int = 0
    finish_reason: str = ""
    transport_retries: int = 0

    @property
    def ok(self) -> bool:
        """True solo si el proveedor respondió con éxito."""
        return self.status is ProviderStatus.SUCCESS

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable, sin credenciales ni bytes de imagen."""
        return {
            "request_id": self.request_id,
            "role": None if self.role is None else self.role.value,
            "provider": self.provider,
            "model": self.model,
            "status": self.status.value,
            "content": self.content,
            "structured_output": (
                None if self.structured_output is None else dict(self.structured_output)
            ),
            "usage": None if self.usage is None else self.usage.model_dump(mode="json"),
            "error": self.error,
            "error_kind": None if self.error_kind is None else self.error_kind.value,
            "duration_ms": self.duration_ms,
            "finish_reason": self.finish_reason,
            "transport_retries": self.transport_retries,
            "trusted": False,
        }


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Resultado de la comprobación mínima de conexión de un proveedor."""

    provider: str
    status: ProviderHealthStatus
    model: str = ""
    detail: str = ""

    @property
    def usable(self) -> bool:
        """True solo si el proveedor está conectado y puede usarse."""
        return self.status is ProviderHealthStatus.CONNECTED

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable de la salud del proveedor."""
        return {
            "provider": self.provider,
            "status": self.status.value,
            "model": self.model,
            "detail": self.detail,
        }


def parse_structured_output(content: str) -> Mapping[str, Any] | None:
    """Interpreta el contenido como JSON si lo es; si no, devuelve ``None``.

    No valida esquemas ni inventa contenido: un texto que no es JSON se queda como texto.
    """
    text = content.strip()
    if not text or not text.startswith("{"):
        return None
    try:
        loaded = json.loads(text)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


__all__ = [
    "MAX_CONTEXT_CHARS",
    "MAX_INSTRUCTIONS_CHARS",
    "MAX_METADATA_CHARS",
    "MAX_METADATA_ITEMS",
    "PROVIDER_OPENAI",
    "ProviderContractError",
    "ProviderErrorKind",
    "ProviderHealth",
    "ProviderHealthStatus",
    "ProviderRequest",
    "ProviderResult",
    "ProviderRole",
    "ProviderStatus",
    "make_request",
    "parse_structured_output",
]
