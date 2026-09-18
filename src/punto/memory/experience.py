"""Unidad de memoria práctica de experiencia (PELL-0).

Una experiencia es **conocimiento resumido y estructurado**: qué problema se intentó resolver,
qué se intentó antes y por qué no funcionó, qué corrección se aplicó, qué procedimiento resultó
reutilizable y con qué evidencia se comprobó.

Dos reglas gobiernan este módulo:

- **la memoria no es autoridad**: aquí no se ejecuta nada, no se conceden capabilities, no se salta
  un Human Gate y no se toca ningún ``ResourceSet``; solo se guarda y se recupera conocimiento;
- **sin evidencia no hay conocimiento confiable**: solo ``VERIFIED`` se considera reutilizable, y
  para llegar ahí hace falta evidencia declarada (tests, E2E, auditoría o resultado verificable).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from punto.common import utc_now

#: Versión del esquema de una experiencia. Un registro con otra versión no se interpreta: falla.
EXPERIENCE_SCHEMA_VERSION: Final[int] = 1

#: Cotas: la memoria guarda resúmenes, nunca volcados de stdout/stderr/prompts.
MAX_TEXT_CHARS: Final[int] = 2000
MAX_ITEMS: Final[int] = 24
MAX_ITEM_CHARS: Final[int] = 400

#: Palabras vacías que no aportan a la recuperación.
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "al", "algo", "con", "cosa", "de", "del", "el", "en", "es", "esa", "ese", "esta", "este",
        "la", "las", "lo", "los", "más", "no", "o", "para", "por", "que", "se", "sin", "su", "un",
        "una", "y", "ya", "the", "and", "for", "from", "not", "of", "on", "or", "to", "with",
    }
)

#: Formas que **nunca** se guardan en la memoria (credenciales y cabeceras de autorización).
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"sk-ant-(?:api|admin)[0-9]{2}-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-[a-f0-9]{24,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*\S{6,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb|redis|amqp)://[^:\s]+:[^@\s]+@"),
)


class ExperienceSchemaError(RuntimeError):
    """El registro no se puede interpretar con el esquema vigente de la memoria."""


class ExperienceSecretError(RuntimeError):
    """El texto contiene algo que la memoria no debe guardar nunca."""


class ExperienceStatus(StrEnum):
    """Estado del conocimiento. Solo ``VERIFIED`` es reutilizable con confianza."""

    CANDIDATE = "CANDIDATE"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"

    @property
    def reusable(self) -> bool:
        """``True`` solo si el conocimiento está demostrado."""
        return self is ExperienceStatus.VERIFIED


class ExperienceResult(StrEnum):
    """Resultado del intento que originó la experiencia."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


def normalize(text: str) -> str:
    """Texto comparable: minúsculas, sin acentos y con espacios colapsados."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", plain).strip()


def tokens(text: str) -> tuple[str, ...]:
    """Tokens significativos de un texto, sin repetir y en orden estable."""
    if not text:
        return ()
    found: list[str] = []
    for raw in re.findall(r"[a-z0-9+#_]+", normalize(text)):
        if len(raw) < 2 or raw in _STOPWORDS or raw in found:
            continue
        found.append(raw)
    return tuple(found)


def problem_fingerprint(problem: str) -> str:
    """Huella determinista del problema, para consolidar sin duplicar.

    La identidad del conocimiento es **el problema**: las etiquetas son metadatos que se fusionan
    cuando dos registros hablan del mismo problema.
    """
    return hashlib.sha256(normalize(problem).encode("utf-8")).hexdigest()[:16]


def assert_no_secrets(where: str, text: str) -> None:
    """Rechaza cualquier texto con forma de credencial o de cabecera de autorización.

    Raises:
        ExperienceSecretError: si el texto contiene algo que no debe persistirse.
    """
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise ExperienceSecretError(
                f"{where}: el texto contiene una credencial o una cabecera de autorización; la "
                "memoria guarda conocimiento resumido, nunca secretos"
            )


def _new_id() -> str:
    """Identidad corta y única de una experiencia."""
    return uuid4().hex[:16]


class ExperienceMemory(BaseModel):
    """Conocimiento reutilizable sobre cómo se resolvió un problema.

    Es un resumen estructurado: no guarda volcados, no guarda secretos y no ejecuta nada. Su estado
    ``VERIFIED`` significa «esto se comprobó con evidencia», no «esto está autorizado»: la autoridad
    sigue siendo del motor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=_new_id)
    created_at: datetime = Field(default_factory=utc_now)
    problem: str
    context: str = ""
    attempts: tuple[str, ...] = ()
    failure_reason: str = ""
    solution: str = ""
    procedure: tuple[str, ...] = ()
    result: ExperienceResult = ExperienceResult.SUCCESS
    verification: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    status: ExperienceStatus = ExperienceStatus.CANDIDATE
    schema_version: int = EXPERIENCE_SCHEMA_VERSION

    @field_validator("problem", "context", "failure_reason", "solution")
    @classmethod
    def _bounded_text(cls, value: str, info: object) -> str:
        """Acota el texto y rechaza secretos."""
        field = getattr(info, "field_name", "texto")
        text = " ".join(value.split())[:MAX_TEXT_CHARS]
        assert_no_secrets(str(field), text)
        return text

    @field_validator("attempts", "procedure", "verification", "tags")
    @classmethod
    def _bounded_items(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        """Acota cada elemento y rechaza secretos."""
        field = getattr(info, "field_name", "lista")
        items: list[str] = []
        for item in value[:MAX_ITEMS]:
            text = " ".join(str(item).split())[:MAX_ITEM_CHARS]
            assert_no_secrets(f"{field}", text)
            if text and text not in items:
                items.append(text)
        return tuple(items)

    @model_validator(mode="after")
    def _evidence_is_required(self) -> ExperienceMemory:
        """Sin evidencia no hay ``VERIFIED``; sin causa no hay ``FAILED`` utilizable.

        Una experiencia no se vuelve confiable porque alguien diga que funcionó: hace falta la
        evidencia declarada. Y un fracaso sin motivo no enseña nada, así que exige su causa.
        """
        if self.status is ExperienceStatus.VERIFIED and not self.verification:
            raise ValueError(
                "una experiencia VERIFIED exige evidencia (tests, E2E, auditoría o resultado "
                "verificable): la memoria no se cree nada por sí sola"
            )
        if self.status is ExperienceStatus.FAILED and not self.failure_reason:
            raise ValueError("una experiencia FAILED exige el motivo por el que no funcionó")
        if self.schema_version != EXPERIENCE_SCHEMA_VERSION:
            raise ExperienceSchemaError(
                f"esquema de experiencia desconocido: {self.schema_version} "
                f"(vigente: {EXPERIENCE_SCHEMA_VERSION})"
            )
        return self

    @property
    def fingerprint(self) -> str:
        """Huella del problema, que es la identidad del conocimiento."""
        return problem_fingerprint(self.problem)

    def as_json_line(self) -> str:
        """Línea JSON del registro, con las claves ordenadas (persistencia estable)."""
        return self.model_dump_json()

    def describe(self) -> str:
        """Resumen legible y acotado, para informes y búsquedas."""
        return f"[{self.status.value}] {self.problem}: {self.solution or self.failure_reason}"
