"""Esquemas de ejecución del Developer Execution Layer (ENGINE-1).

Contratos de datos de la capa de ejecución controlada: la tarea estructurada que
recibe un ``DeveloperRunner``, las recetas deterministas que ejecuta el runner
local, y la evidencia estructurada que produce.

Sin IA: estos modelos describen trabajo **declarado**, no generado.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.schemas.enums import RiskLevel

#: Código de salida con el que se marca un comando que agotó su timeout. Es el
#: código convencional de ``timeout(1)``; el campo ``timed_out`` lo desambigua.
TIMEOUT_EXIT_CODE: int = 124

#: Código de salida usado cuando el ejecutable no pudo lanzarse.
SPAWN_FAILURE_EXIT_CODE: int = 127

#: Código de salida usado cuando un check fue denegado por la política de shell.
BLOCKED_EXIT_CODE: int = -1


class DeveloperRunStatus(StrEnum):
    """Estado final de una ejecución de desarrollo."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"


class FileOperation(StrEnum):
    """Operación aplicada a un archivo del workspace."""

    CREATED = "CREATED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"


class FileChange(BaseModel):
    """Evidencia de un cambio de archivo dentro del workspace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., description="Ruta relativa al workspace (posix, normalizada).")
    absolute_path: str = Field(..., description="Ruta absoluta resuelta dentro del workspace.")
    operation: FileOperation = Field(..., description="Operación aplicada.")
    bytes_written: int = Field(default=0, ge=0, description="Bytes escritos.")
    verified: bool = Field(
        default=False,
        description="True si la lectura posterior al write coincidió con el contenido esperado.",
    )
    detail: str = Field(default="", description="Detalle legible del cambio.")


class CommandRequest(BaseModel):
    """Comando estructurado: nunca una cadena de shell.

    Los argumentos se pasan como lista y se ejecutan con ``shell=False``, de modo
    que no existe interpretación de metacaracteres.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    executable: str = Field(..., min_length=1, description="Ejecutable (clave de la allowlist).")
    args: tuple[str, ...] = Field(default=(), description="Argumentos, ya tokenizados.")
    cwd: str = Field(
        default=".",
        min_length=1,
        description="Directorio de trabajo, relativo al workspace y siempre contenido en él.",
    )
    timeout_seconds: float | None = Field(
        default=None, gt=0.0, description="Timeout explícito; si es None se usa el del contexto."
    )


class CommandResult(BaseModel):
    """Resultado completo de un comando ejecutado.

    ``stderr`` se conserva siempre: nunca se descarta ni se oculta.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(default="", description="Nombre lógico del comando, si lo tiene.")
    command: str = Field(..., description="Ejecutable efectivamente lanzado.")
    declared_executable: str = Field(default="", description="Ejecutable declarado en la petición.")
    args: tuple[str, ...] = Field(default=(), description="Argumentos usados.")
    cwd: str = Field(..., description="Directorio de trabajo del proceso.")
    exit_code: int = Field(..., description="Código de salida.")
    stdout: str = Field(default="", description="Salida estándar capturada.")
    stderr: str = Field(default="", description="Salida de error capturada (nunca se oculta).")
    started_at: datetime = Field(default_factory=utc_now, description="Inicio (UTC).")
    completed_at: datetime = Field(default_factory=utc_now, description="Fin (UTC).")
    duration_ms: int = Field(default=0, ge=0, description="Duración en milisegundos.")
    timed_out: bool = Field(default=False, description="True si agotó el timeout.")

    @property
    def succeeded(self) -> bool:
        """True solo si terminó por sí solo con código 0."""
        return self.exit_code == 0 and not self.timed_out


class ValidationCheck(BaseModel):
    """Resultado de un check de validación individual."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., description="Nombre del check.")
    command: str = Field(..., description="Ejecutable del check.")
    args: tuple[str, ...] = Field(default=(), description="Argumentos del check.")
    exit_code: int = Field(..., description="Código de salida.")
    stdout: str = Field(default="", description="Salida estándar.")
    stderr: str = Field(default="", description="Salida de error.")
    passed: bool = Field(..., description="True solo si se ejecutó, no expiró y salió con 0.")
    timed_out: bool = Field(default=False, description="True si agotó el timeout.")
    blocked: bool = Field(
        default=False, description="True si la política de shell denegó el check."
    )
    duration_ms: int = Field(default=0, ge=0, description="Duración en milisegundos.")


class ValidationResult(BaseModel):
    """Agregado de validación.

    ``passed`` es ``True`` únicamente si se declaró al menos un check **y** todos
    los checks se ejecutaron, no expiraron y salieron con código 0.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool = Field(..., description="Resultado agregado.")
    checks: tuple[ValidationCheck, ...] = Field(default=(), description="Checks ejecutados.")
    failed_checks: tuple[str, ...] = Field(
        default=(), description="Nombres de los checks que no pasaron."
    )
    duration_ms: int = Field(default=0, ge=0, description="Duración total en milisegundos.")

    @property
    def total(self) -> int:
        """Número de checks declarados."""
        return len(self.checks)


class DeveloperExecutionResult(BaseModel):
    """Evidencia estructurada de una ejecución de desarrollo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID = Field(..., description="Tarea ejecutada.")
    status: DeveloperRunStatus = Field(..., description="Estado final.")
    workspace: str = Field(..., description="Workspace raíz usado.")
    branch: str = Field(default="", description="Rama de trabajo.")
    files_changed: tuple[FileChange, ...] = Field(
        default=(), description="Archivos creados, modificados o eliminados."
    )
    commands_executed: tuple[CommandResult, ...] = Field(
        default=(), description="Comandos ejecutados."
    )
    validation: ValidationResult | None = Field(
        default=None, description="Resultado de validación, si se declararon checks."
    )
    commit_sha: str | None = Field(default=None, description="SHA del commit local creado.")
    started_at: datetime = Field(default_factory=utc_now, description="Inicio (UTC).")
    completed_at: datetime = Field(default_factory=utc_now, description="Fin (UTC).")
    error: str | None = Field(default=None, description="Error que impidió completar la tarea.")
    cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Coste de la ejecución. ENGINE-1 es siempre 0.0: no hay modelo externo.",
    )
    attempts_used: int = Field(default=1, ge=0, description="Intentos consumidos.")

    @property
    def succeeded(self) -> bool:
        """True si la ejecución terminó en SUCCESS."""
        return self.status is DeveloperRunStatus.SUCCESS


# ---------------------------------------------------------------------------
# Recetas deterministas (entrada del runner local)
# ---------------------------------------------------------------------------
class FileWrite(BaseModel):
    """Escritura declarada de un archivo del workspace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., min_length=1, description="Ruta relativa al workspace.")
    content: str = Field(default="", description="Contenido exacto a escribir.")


class TextReplacement(BaseModel):
    """Reemplazo textual declarado, con ancla literal única."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., min_length=1, description="Ruta relativa al workspace.")
    old: str = Field(..., min_length=1, description="Texto a localizar (debe aparecer una vez).")
    new: str = Field(..., description="Texto de reemplazo.")


class ContentAssertion(BaseModel):
    """Verificación determinista del contenido de un archivo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., min_length=1, description="Ruta relativa al workspace.")
    expected: str = Field(..., description="Contenido exacto esperado.")


class CommandSpec(BaseModel):
    """Check de validación declarado en la receta."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, description="Nombre del check.")
    executable: str = Field(..., min_length=1, description="Ejecutable (allowlist).")
    args: tuple[str, ...] = Field(default=(), description="Argumentos declarados.")
    timeout_seconds: float | None = Field(default=None, gt=0.0, description="Timeout del check.")


class DeveloperTask(BaseModel):
    """Tarea de desarrollo estructurada y determinista.

    No contiene texto generado por IA: declara exactamente qué archivos escribir,
    qué reemplazos aplicar, qué contenido verificar, qué checks ejecutar y con qué
    mensaje commitear. El ``action`` permite que CAMUS la someta al Policy Engine
    antes de delegar en el ``DeveloperRunner``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID = Field(..., description="Identificador de la tarea.")
    objective: str = Field(..., min_length=1, description="Objetivo legible.")
    slug: str = Field(default="task", min_length=1, description="Slug para el nombre de rama.")
    action: str = Field(
        default="modify_file",
        min_length=1,
        description="Acción del catálogo de autoridad usada para la evaluación de política.",
    )
    risk_level: RiskLevel = Field(default=RiskLevel.LOW, description="Riesgo declarado.")
    files: tuple[FileWrite, ...] = Field(default=(), description="Archivos a escribir.")
    replacements: tuple[TextReplacement, ...] = Field(
        default=(), description="Reemplazos textuales a aplicar."
    )
    assertions: tuple[ContentAssertion, ...] = Field(
        default=(), description="Contenido exacto a verificar tras escribir."
    )
    validations: tuple[CommandSpec, ...] = Field(default=(), description="Checks de validación.")
    commit_message: str = Field(..., min_length=1, description="Mensaje del commit local.")


__all__ = [
    "SPAWN_FAILURE_EXIT_CODE",
    "TIMEOUT_EXIT_CODE",
    "CommandRequest",
    "CommandResult",
    "CommandSpec",
    "ContentAssertion",
    "DeveloperExecutionResult",
    "DeveloperRunStatus",
    "DeveloperTask",
    "FileChange",
    "FileOperation",
    "FileWrite",
    "TextReplacement",
    "ValidationCheck",
    "ValidationResult",
]
