"""Esquemas de ejecución del Developer Execution Layer (ENGINE-1).

Contratos de datos de la capa de ejecución controlada: la tarea estructurada que
recibe un ``DeveloperRunner``, las recetas deterministas que ejecuta el runner
local, y la evidencia estructurada que produce.

Sin IA: estos modelos describen trabajo **declarado**, no generado.

Contexto de reparación (ENGINE-6.1.1)
-------------------------------------
``DeveloperTask.repair`` es opcional y por defecto ``None``: cuando viaja, la tarea es una
**reparación** y el runner recibe el encargo completo (plan, diagnóstico, defectos, snapshot y
criterios) dentro de la misma tarea. No hay un segundo Developer ni una segunda interfaz de runner:
lo que cambia es el contexto, no el contrato de ejecución.

Su tipo se enlaza en la **primera construcción** (:meth:`DeveloperTask.__init__`) y no al importar
este módulo, y el motivo es un ciclo de importación real: ``punto.schemas.repair`` importa
``punto.schemas.workflow``, que importa **este** módulo (``ModelUsage``). Importarlo aquí arriba
rompería la carga de cualquiera de los tres, en cualquiera de los dos órdenes posibles. El enlace
diferido conserva el contrato exacto —``RepairTask | None``, validado por pydantic— sin cerrar el
ciclo. Límite declarado: una tarea no se reconstruye desde JSON en PUNTO (el encargo viaja por los
artefactos durables del handoff, no dentro de la tarea), así que la única vía de construirla es el
constructor, que es exactamente la que enlaza el tipo.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.schemas.enums import RiskLevel

if TYPE_CHECKING:
    from punto.schemas.repair import RepairTask

#: ``True`` cuando el campo ``repair`` ya está enlazado con su tipo real. Evita repetir el enlace.
_REPAIR_FIELD_BOUND: bool = False

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


class ExecutionTrustLevel(StrEnum):
    """Nivel de confianza del código que se va a ejecutar.

    La distinción es la frontera de seguridad de ENGINE-1.R1:

    - ``TRUSTED_LOCAL``: trabajo determinista declarado por nosotros. Puede
      ejecutarse en el host.
    - ``UNTRUSTED_MODEL``: trabajo originado por un modelo externo. **Nunca**
      puede ejecutarse en el host: exige un backend con aislamiento real.
    """

    TRUSTED_LOCAL = "TRUSTED_LOCAL"
    UNTRUSTED_MODEL = "UNTRUSTED_MODEL"


class SandboxCapabilities(BaseModel):
    """Aislamientos que un backend debe garantizar para admitir trabajo no confiable.

    Para ``UNTRUSTED_MODEL`` **los cuatro** deben ser ``True``. Si falta uno, la
    ejecución no está autorizada: no se degrada a ejecución local.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    filesystem_isolated: bool = Field(
        default=False, description="El código no puede leer ni escribir fuera de su zona."
    )
    environment_isolated: bool = Field(
        default=False, description="El código no ve variables de entorno del host."
    )
    network_isolated: bool = Field(
        default=False, description="El código no puede abrir sockets ni alcanzar la red."
    )
    process_isolated: bool = Field(
        default=False, description="El código no puede afectar ni observar otros procesos."
    )

    def satisfies_untrusted(self) -> bool:
        """True solo si los cuatro aislamientos están garantizados."""
        return (
            self.filesystem_isolated
            and self.environment_isolated
            and self.network_isolated
            and self.process_isolated
        )

    @property
    def missing(self) -> tuple[str, ...]:
        """Nombres de los aislamientos que faltan."""
        candidates = (
            ("filesystem_isolated", self.filesystem_isolated),
            ("environment_isolated", self.environment_isolated),
            ("network_isolated", self.network_isolated),
            ("process_isolated", self.process_isolated),
        )
        return tuple(name for name, present in candidates if not present)


#: Capacidades honestas de un backend que **no** es un sandbox.
NO_SANDBOX_CAPABILITIES: SandboxCapabilities = SandboxCapabilities()

#: Capacidades exigidas a un backend que pretenda admitir trabajo no confiable.
FULL_SANDBOX_CAPABILITIES: SandboxCapabilities = SandboxCapabilities(
    filesystem_isolated=True,
    environment_isolated=True,
    network_isolated=True,
    process_isolated=True,
)


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


class DeveloperInvocationLimits(BaseModel):
    """Autorización de modelo de **una** invocación del Developer (ENGINE-6.1.3, F613-01).

    El presupuesto del workflow ya era una frontera pre-gasto para el Architect y el Planner, y para
    el Developer solo llegaba hasta la reserva del kernel: su bucle de llamadas ejecutaba la
    configuración del runner, así que una autorización de una llamada podía acabar en varias. Esta
    autorización viaja con la invocación —dentro del ``ExecutionContext``— y es un **techo**:

    - el runner toma el mínimo entre su propia configuración, esta autorización y el presupuesto del
      ``RepairPlan`` cuando el paso repara; ninguna de las tres amplía a otra;
    - se aplica al Developer **normal** y al de reparación, porque el proveedor es el mismo;
    - el conteo es **acumulado** en la invocación, y el tope de tokens es de totales (entrada más
      salida), igual que en el resto del motor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Llamadas de modelo autorizadas para toda la invocación (cero significa «ninguna»).
    max_model_calls: int = Field(..., ge=0)
    #: Tokens totales autorizados (entrada más salida) para toda la invocación.
    max_total_tokens: int | None = Field(default=None, ge=1)
    #: Tope de salida autorizado por llamada, si la autorización lo declara.
    max_output_tokens: int | None = Field(default=None, ge=1)
    #: Procedencia de la autorización, para la traza (``"workflow"``, ``"repair_plan"``…).
    source: str = Field(default="workflow", max_length=40)


class ModelUsage(BaseModel):
    """Consumo real de tokens reportado por el proveedor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0)
    prompt_cache_miss_tokens: int | None = Field(default=None, ge=0)

    def merged(self, other: ModelUsage) -> ModelUsage:
        """Suma acumulativa de dos consumos."""
        return ModelUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            prompt_cache_hit_tokens=_add_optional(
                self.prompt_cache_hit_tokens, other.prompt_cache_hit_tokens
            ),
            prompt_cache_miss_tokens=_add_optional(
                self.prompt_cache_miss_tokens, other.prompt_cache_miss_tokens
            ),
        )


def _add_optional(left: int | None, right: int | None) -> int | None:
    """Suma dos contadores opcionales, devolviendo ``None`` si ambos faltan."""
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


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

    # --- ENGINE-2: proveedor de modelo ---
    provider: str = Field(
        default="", description="Proveedor del modelo usado (vacío si no hubo modelo)."
    )
    model: str = Field(default="", description="Modelo usado (vacío si no hubo modelo).")
    model_calls: int = Field(
        default=0, ge=0, description="Llamadas al modelo realizadas (incluye reparaciones)."
    )
    usage: ModelUsage = Field(
        default_factory=ModelUsage, description="Consumo acumulado de tokens."
    )
    rolled_back: bool = Field(
        default=False,
        description="True si el workspace se restauró al estado base tras un fallo.",
    )

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
    """Tarea de desarrollo estructurada.

    En ENGINE-1 la receta es determinista (``files``, ``replacements``). En
    ENGINE-2 el ``DeveloperRunner`` puede generar los cambios con un modelo: la
    tarea declara entonces el **contexto** que se le entrega y PUNTO decide qué se
    escribe y qué se ejecuta.
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

    # --- ENGINE-2: contexto controlado para un runner que genera código ---
    acceptance_criteria: tuple[str, ...] = Field(
        default=(), description="Criterios de aceptación que la propuesta debe cumplir."
    )
    context_files: tuple[str, ...] = Field(
        default=(),
        description=(
            "Archivos del workspace que se entregan al modelo como contexto. "
            "Ninguno más se envía: el repositorio completo nunca se vuelca."
        ),
    )
    allowed_files: tuple[str, ...] = Field(
        default=(),
        description=(
            "Rutas que el modelo puede proponer modificar. Una propuesta que toque "
            "cualquier otra ruta se rechaza. Si está vacío, se derivan de "
            "``context_files``."
        ),
    )
    max_context_bytes: int = Field(
        default=200_000, gt=0, description="Límite de bytes de contexto enviado al modelo."
    )

    # --- ENGINE-6.1: contexto de reparación del ciclo autónomo acotado ---
    repair: RepairTask | None = Field(
        default=None,
        description=(
            "Encargo de reparación (``RepairTask``) cuando esta ejecución repara defectos "
            "declarados. ``None`` en el trabajo normal del Developer."
        ),
    )

    def __init__(self, **data: Any) -> None:
        """Enlaza el tipo del contexto de reparación y construye la tarea.

        El enlace es diferido por el ciclo de importación documentado en el módulo: la primera vez
        que se construye una tarea —siempre después de que los módulos estén cargados— el campo
        ``repair`` queda resuelto contra ``RepairTask`` y pydantic lo valida como cualquier otro.
        Las siguientes construcciones no pagan nada: el enlace se hace una sola vez.
        """
        _bind_repair_field()
        super().__init__(**data)


def _bind_repair_field() -> None:
    """Resuelve el campo ``repair`` de :class:`DeveloperTask` contra su contrato real.

    Se llama en la primera construcción. Falla si ``punto.schemas.repair`` no se puede importar,
    que ya no es un ciclo de importación sino un defecto de instalación: preferimos un error
    explícito a una tarea sin el contrato de reparación.
    """
    global _REPAIR_FIELD_BOUND
    if _REPAIR_FIELD_BOUND:
        return
    from punto.schemas.repair import RepairTask

    DeveloperTask.model_rebuild(_types_namespace={"RepairTask": RepairTask})
    _REPAIR_FIELD_BOUND = True


# ---------------------------------------------------------------------------
# ENGINE-2: propuesta del modelo
# ---------------------------------------------------------------------------
class ProposalOperation(StrEnum):
    """Operación que el modelo puede proponer sobre un archivo.

    ENGINE-2 V1 no admite borrado: un modelo no debe poder eliminar archivos.
    """

    CREATE = "CREATE"
    REPLACE = "REPLACE"


class ProposedFileChange(BaseModel):
    """Cambio concreto propuesto por el modelo sobre un archivo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., min_length=1, description="Ruta relativa al workspace.")
    operation: ProposalOperation = Field(..., description="CREATE o REPLACE.")
    content: str = Field(default="", description="Contenido COMPLETO tras el cambio.")


class DeveloperProposal(BaseModel):
    """Propuesta estructurada que devuelve el modelo.

    El modelo **no** ejecuta nada: propone. PUNTO valida la propuesta completa
    antes de aplicar nada.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(..., min_length=1, description="Resumen del cambio propuesto.")
    changes: tuple[ProposedFileChange, ...] = Field(
        default=(), description="Cambios propuestos."
    )
    validation_notes: tuple[str, ...] = Field(
        default=(), description="Notas del modelo sobre la validación."
    )
    assumptions: tuple[str, ...] = Field(
        default=(), description="Supuestos declarados por el modelo."
    )


__all__ = [
    "BLOCKED_EXIT_CODE",
    "FULL_SANDBOX_CAPABILITIES",
    "NO_SANDBOX_CAPABILITIES",
    "SPAWN_FAILURE_EXIT_CODE",
    "TIMEOUT_EXIT_CODE",
    "CommandRequest",
    "CommandResult",
    "CommandSpec",
    "ContentAssertion",
    "DeveloperExecutionResult",
    "DeveloperProposal",
    "DeveloperRunStatus",
    "DeveloperTask",
    "ExecutionTrustLevel",
    "FileChange",
    "FileOperation",
    "FileWrite",
    "ModelUsage",
    "ProposalOperation",
    "ProposedFileChange",
    "SandboxCapabilities",
    "TextReplacement",
    "ValidationCheck",
    "ValidationResult",
]
