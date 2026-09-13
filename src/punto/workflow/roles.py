"""Adaptación de los roles existentes al contrato del kernel (ENGINE-6.0).

Por qué existe esta capa
------------------------
El kernel no reescribe los roles: los **invoca**. Esta capa es la frontera que lo hace posible
sin tocar Architect, Planner, Developer, QA, Security, Reviewer, Cross-Audit ni Visual QA, y
tiene tres piezas:

- :class:`RoleExecutor`, el contrato que el kernel conoce: ejecutar una
  :class:`~punto.schemas.workflow.RoleExecutionRequest` y declarar qué capacidad cubre un rol.
- los ``normalize_*``, funciones **puras** que traducen el informe real de cada rol
  (``ArchitectureOutcome``, ``PlanningOutcome``, ``DeveloperExecutionResult``, ``QAReport``,
  ``SecurityReport``, ``ReviewReport``, ``CrossAuditReport``, ``VisualQAReport``) a
  :class:`~punto.schemas.workflow.RoleExecutionResult`. No llaman a ningún modelo, no tocan el
  disco y no deciden nada: leen el estado real y lo copian.
- tres adaptadores: :class:`CallableRoleExecutor` (una función), :class:`UnavailableRoleExecutor`
  (rol sin proveedor disponible) y :class:`CamusRoleExecutor` (CAMUS real).

Reglas de normalización, explícitas porque son el contrato
----------------------------------------------------------
**Estado.** Se lee el estado real del informe; nunca se inventa:

- ``PASS`` / ``APPROVED`` / ``SUCCESS`` → ``RoleStatus.COMPLETED``;
- ``FAIL`` / ``CHANGES_REQUESTED`` / ``FAILED`` / ``TIMEOUT`` → ``RoleStatus.NEEDS_REPAIR``
  (el rol emitió un veredicto negativo: hay algo que rehacer);
- ``BLOCKED`` → ``RoleStatus.BLOCKED``;
- cualquier otro estado, o ninguno → ``RoleStatus.FAILED`` con
  ``WorkflowFailureCode.WORKFLOW_ROLE_FAILED``.

Un estado desconocido **nunca** se traduce en ``COMPLETED``: no entender un veredicto no puede
convertirse en aprobarlo, y el detalle del fallo dice qué estado llegó.

**Hallazgos.** Se copian con su gravedad real (``FindingSeverity``), acotados a
``MAX_WORKFLOW_FINDINGS`` y con los textos recortados a ``MAX_WORKFLOW_TEXT_CHARS``. Una gravedad
que no se pueda leer se traduce a ``MEDIUM``: no se degrada a ``INFO`` porque eso minimizaría el
riesgo en silencio. El Developer no declara hallazgos ni gravedades, así que su resultado no
inventa ninguno: sus hechos viajan en ``artifacts``, ``summary`` y ``recommendation``.

**Valores neutros.** ``summary``, ``provider``, ``model``, ``usage``, ``attempts``,
``started_at`` y ``completed_at`` salen del informe cuando los declara. Cuando no: un resumen
``ROL: ESTADO``, cadena vacía, ``ModelUsage()`` vacío, el intento técnico real de la petición
(``request.attempt``) y la hora de normalización. Son valores declarados, no deducciones.

**Secretos.** Solo se copian campos de una lista cerrada. El resto del informe —credenciales,
notas privadas, volcados— no se lee, de modo que no puede acabar en un checkpoint, en un log ni
en un informe de auditoría.

Nada de fallback silencioso
---------------------------
Un rol sin proveedor no se sustituye por otro: se devuelve un resultado ``PROVIDER_UNAVAILABLE``
con el código ``WORKFLOW_PROVIDER_UNAVAILABLE`` y un detalle que dice **qué rol** y **qué falta**.
Los runners de CAMUS son *opt-in*: si el rol no está inyectado, su ``*NotConfiguredError`` se
traduce a ese mismo resultado en lugar de improvisar una evaluación.

Una etapa, una llamada
----------------------
Cada rol ejecuta **solo** su etapa: el ``ARCHITECT`` llama a ``camus.analyze_project`` y el
``PLANNER`` a ``camus.plan_project_from_architecture``, nunca a ``camus.plan_project`` —que es la
composición de las dos—. El Planner recibe el diseño del Architect reconstruido desde la
referencia durable de la petición; si esa referencia no resuelve a un diseño válido, la etapa
falla con ``WORKFLOW_ROLE_FAILED`` en vez de repetir el diseño por su cuenta.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol, cast, runtime_checkable

from punto.common import utc_now
from punto.schemas.enums import FindingSeverity
from punto.schemas.execution import ModelUsage
from punto.schemas.planning import ModelExecutionSummary
from punto.schemas.workflow import (
    MAX_WORKFLOW_ARTIFACTS,
    MAX_WORKFLOW_FINDINGS,
    MAX_WORKFLOW_SUMMARY_CHARS,
    MAX_WORKFLOW_TEXT_CHARS,
    ProviderCapability,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowFailureCode,
    WorkflowFinding,
)
from punto.tools.errors import (
    ArchitectRunnerNotConfiguredError,
    CrossAuditRunnerNotConfiguredError,
    DeveloperRunnerNotConfiguredError,
    PlannerRunnerNotConfiguredError,
    PlanningValidationError,
    QARunnerNotConfiguredError,
    ReviewerRunnerNotConfiguredError,
    SecurityRunnerNotConfiguredError,
    VisualQARunnerNotConfiguredError,
)
from punto.workflow.errors import WorkflowProviderUnavailableError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from punto.architect.base import ArchitectureOutcome
    from punto.developer.context import ExecutionContext
    from punto.orchestrator.camus import Camus
    from punto.providers.base import ImagePayload
    from punto.schemas.cross_audit import CrossAuditTask
    from punto.schemas.execution import DeveloperTask
    from punto.schemas.planning import ProjectIntent
    from punto.schemas.qa import QATask
    from punto.schemas.review import ReviewTask
    from punto.schemas.security import SecurityTask
    from punto.schemas.visual import VisualQATask
    from punto.workflow.providers import ProviderCapabilityRegistry

#: Espejos de los máximos de ``RoleExecutionResult`` que no se exportan como constante propia.
_MAX_PROVIDER_CHARS: Final[int] = 40
_MAX_MODEL_CHARS: Final[int] = 120
#: Espejo del máximo de ``WorkflowFinding.category``.
_MAX_CATEGORY_CHARS: Final[int] = 80

#: Estados reales que significan «el rol hizo su trabajo y es aceptable».
_COMPLETED_STATES: Final[frozenset[str]] = frozenset({"PASS", "APPROVED", "SUCCESS"})
#: Estados reales que significan «hay algo que rehacer».
_REPAIR_STATES: Final[frozenset[str]] = frozenset(
    {"FAIL", "CHANGES_REQUESTED", "FAILED", "TIMEOUT"}
)
#: Estados reales que significan «no se pudo trabajar».
_BLOCKED_STATES: Final[frozenset[str]] = frozenset({"BLOCKED"})

#: Texto con el que se sustituye un hallazgo que no trae ni título ni descripción.
_NO_DETAIL: Final[str] = "hallazgo sin detalle declarado"

#: Errores tipados de CAMUS que significan «este rol no está configurado».
#:
#: Se listan uno a uno porque no comparten una base común más allá de ``RuntimeError``: cada
#: familia de rol tiene la suya. Traducirlos a ``PROVIDER_UNAVAILABLE`` es lo que impide que un
#: rol ausente se disfrace de otro proveedor.
_NOT_CONFIGURED_ERRORS: Final[tuple[type[Exception], ...]] = (
    ArchitectRunnerNotConfiguredError,
    PlannerRunnerNotConfiguredError,
    DeveloperRunnerNotConfiguredError,
    QARunnerNotConfiguredError,
    SecurityRunnerNotConfiguredError,
    ReviewerRunnerNotConfiguredError,
    CrossAuditRunnerNotConfiguredError,
    VisualQARunnerNotConfiguredError,
)

#: Firma común de los normalizadores puros de rol.
_RoleNormalizer = Callable[[object, RoleExecutionRequest], RoleExecutionResult]


@runtime_checkable
class RoleExecutor(Protocol):
    """Contrato mínimo que el kernel conoce de un rol.

    Es deliberadamente estrecho: ejecutar y declarar capacidad. Todo lo demás —prompts,
    proveedores, sandbox— queda por debajo de esta frontera, que es justo lo que permite que el
    kernel no sepa si detrás hay DeepSeek, Anthropic o un doble.
    """

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Ejecuta el rol descrito por la petición y devuelve su resultado normalizado."""
        ...

    def capability(self, role: RoleName) -> ProviderCapability | None:
        """Capacidad declarada para un rol, o ``None`` si este ejecutor no la cubre."""
        ...


@dataclass(frozen=True, slots=True)
class _RoleView:
    """Vista neutra de un informe real, antes de convertirse en ``RoleExecutionResult``."""

    status: RoleStatus
    summary: str = ""
    recommendation: str = ""
    provider: str = ""
    model: str = ""
    artifacts: tuple[str, ...] = ()
    findings: tuple[WorkflowFinding, ...] = ()
    usage: ModelUsage | None = None
    #: Intentos declarados por el informe. ``0`` significa «no los declara».
    attempts: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: WorkflowFailureCode | None = None
    error_detail: str = ""


class _InvalidRoleInputError(Exception):
    """La entrada que devolvió ``build_input`` no tiene la forma que el rol exige.

    Es interna: el adaptador la convierte en un resultado ``FAILED`` en vez de propagarla, para
    que un error de cableado del llamante no rompa el bucle del kernel con una excepción no
    tipada.
    """


class CallableRoleExecutor:
    """Adaptador genérico: envuelve una función que ya devuelve un resultado normalizado.

    Sirve para roles deterministas, dobles de prueba y cualquier implementación que no necesite
    conocer CAMUS. La capacidad es **declarada**, nunca deducida de la función: si no se declara,
    :meth:`capability` devuelve ``None`` y el kernel lo trata como un hueco, no como un proveedor.
    """

    def __init__(
        self,
        role: RoleName,
        run: Callable[[RoleExecutionRequest], RoleExecutionResult],
        capability_info: ProviderCapability | None = None,
    ) -> None:
        self._role = role
        self._run = run
        self._capability = capability_info

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Devuelve exactamente lo que produce la función envuelta."""
        return self._run(request)

    def capability(self, role: RoleName) -> ProviderCapability | None:
        """Capacidad declarada, solo para el rol que este ejecutor cubre."""
        if role is not self._role:
            return None
        return self._capability


class UnavailableRoleExecutor:
    """Rol sin proveedor disponible: lo declara y no improvisa nada.

    No hay aquí ningún camino que llame a un rol, porque no hay rol que llamar. Existe para que
    el kernel pueda cablear los ocho roles sin mentir sobre los que no tienen proveedor: el
    resultado dice **qué rol** falta, con el código del contrato y sin sustituirlo por otro.
    """

    def __init__(
        self,
        role: RoleName,
        detail: str,
        code: WorkflowFailureCode = WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
    ) -> None:
        self._role = role
        self._detail = detail
        self._code = code

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Devuelve ``PROVIDER_UNAVAILABLE`` sin ejecutar ni sustituir ningún rol."""
        return _failure(
            self._role,
            request,
            status=RoleStatus.PROVIDER_UNAVAILABLE,
            code=self._code,
            detail=self._detail,
        )

    def capability(self, role: RoleName) -> ProviderCapability | None:
        """``None``: no hay proveedor que declarar, y declararlo sería inventarlo."""
        return None


class CamusRoleExecutor:
    """Adaptador real sobre CAMUS: llama al método público del rol y normaliza su informe.

    Forma de ``build_input`` por rol, porque cada método de CAMUS recibe cosas distintas:

    - ``ARCHITECT``: un ``ProjectIntent``. Se ejecuta ``camus.analyze_project``, que hace
      **solo** el diseño y devuelve un ``ArchitectureOutcome``;
    - ``PLANNER``: la pareja ``(ProjectIntent, ArchitectureOutcome)``. Se ejecuta
      ``camus.plan_project_from_architecture``, que hace **solo** la planificación sobre el
      diseño del Architect. ``build_input`` es quien reconstruye ese diseño desde la referencia
      durable que viaja en ``RoleExecutionRequest.references`` (el ``reference`` apunta a un
      artefacto del almacén del motor): el adaptador no lo adivina ni vuelve a ejecutar al
      Architect para rellenar el hueco;
    - ``DEVELOPER``: la pareja ``(DeveloperTask, ExecutionContext)``;
    - ``QA``: un ``QATask``; ``SECURITY``: un ``SecurityTask``; ``REVIEWER``: un ``ReviewTask``;
      ``CROSS_AUDIT``: un ``CrossAuditTask``;
    - ``VISUAL_QA``: la pareja ``(VisualQATask, Mapping[str, ImagePayload])``.

    Ningún rol de planificación usa ``camus.plan_project``: ese método compone las dos etapas, y
    llamarlo desde los dos adaptadores ejecutaría Architect + Planner dos veces.

    Si se da ``registry``, la capacidad se consulta con ``require(role, provider)`` **antes** de
    llamar a CAMUS: un rol sin proveedor utilizable no llega a ejecutarse. Cualquier excepción que
    no sea un fallo tipado de configuración, de proveedor o de validación del diseño se propaga
    tal cual: ocultarla convertiría un defecto real en un resultado inventado.
    """

    def __init__(
        self,
        *,
        camus: Camus,
        role: RoleName,
        build_input: Callable[[RoleExecutionRequest], object],
        registry: ProviderCapabilityRegistry | None = None,
        provider: str | None = None,
    ) -> None:
        self._camus = camus
        self._role = role
        self._build_input = build_input
        self._registry = registry
        self._provider = provider
        self._handlers: Mapping[RoleName, Callable[[object], object]] = MappingProxyType(
            {
                RoleName.ARCHITECT: self._call_analyze_project,
                RoleName.PLANNER: self._call_plan_from_architecture,
                RoleName.DEVELOPER: self._call_developer,
                RoleName.QA: self._call_qa,
                RoleName.SECURITY: self._call_security,
                RoleName.REVIEWER: self._call_review,
                RoleName.CROSS_AUDIT: self._call_cross_audit,
                RoleName.VISUAL_QA: self._call_visual_qa,
            }
        )

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Comprueba la capacidad, delega en CAMUS y normaliza el informe resultante."""
        if request.role is not self._role:
            return _failure(
                self._role,
                request,
                status=RoleStatus.FAILED,
                code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED,
                detail=(
                    f"el adaptador de {self._role.value} recibió una petición para "
                    f"{request.role.value}: el ejecutor no ejecuta otro rol"
                ),
            )

        blocked = self._registry_failure(request)
        if blocked is not None:
            return blocked

        payload = self._build_input(request)
        try:
            produced = self._invoke(payload)
        except _NOT_CONFIGURED_ERRORS as error:
            return _unavailable(
                self._role,
                request,
                (
                    f"el rol {self._role.value} no tiene runner configurado en CAMUS "
                    f"({type(error).__name__}): falta inyectarlo. PUNTO no lo sustituye por otro "
                    "proveedor"
                ),
            )
        except WorkflowProviderUnavailableError as error:
            return _unavailable(
                self._role,
                request,
                f"el rol {self._role.value} no tiene proveedor disponible: {error}",
            )
        except PlanningValidationError as error:
            return _failure(
                self._role,
                request,
                status=RoleStatus.FAILED,
                code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED,
                detail=(
                    f"la entrada del rol {self._role.value} no es un diseño válido de PUNTO: "
                    f"{error}. Se rechaza en vez de volver a ejecutar al Architect"
                ),
            )
        except _InvalidRoleInputError as error:
            return _failure(
                self._role,
                request,
                status=RoleStatus.FAILED,
                code=WorkflowFailureCode.WORKFLOW_ROLE_FAILED,
                detail=f"entrada inválida para {self._role.value}: {error}",
            )
        return _NORMALIZERS[self._role](produced, request)

    def capability(self, role: RoleName) -> ProviderCapability | None:
        """Capacidad declarada por el registro, o ``None`` si el rol no se puede cubrir hoy."""
        if role is not self._role or self._registry is None:
            return None
        try:
            return self._registry.require(self._role, self._provider)
        except WorkflowProviderUnavailableError:
            return None

    # ------------------------------------------------------------------ interno
    def _registry_failure(self, request: RoleExecutionRequest) -> RoleExecutionResult | None:
        """Consulta la capacidad declarada **antes** de llamar a CAMUS.

        Un rol sin proveedor utilizable no debe llegar a ejecutarse: si llegara, el fallo se
        confundiría con un fallo del rol, que es justo lo que el encargo prohíbe.
        """
        if self._registry is None:
            return None
        try:
            capability = self._registry.require(self._role, self._provider)
        except WorkflowProviderUnavailableError as error:
            return _unavailable(self._role, request, str(error))
        if not capability.available or not capability.supports(self._role):
            return _unavailable(
                self._role,
                request,
                (
                    f"el rol {self._role.value} no tiene proveedor utilizable: "
                    f"proveedor={capability.provider!r}, available={capability.available}, "
                    f"credential_state={capability.credential_state.value}. No hay fallback"
                ),
            )
        return None

    def _invoke(self, payload: object) -> object:
        """Despacha al método público de CAMUS del rol; ninguno se improvisa."""
        handler = self._handlers.get(self._role)
        if handler is None:
            msg = f"no hay método de CAMUS declarado para el rol {self._role.value}"
            raise _InvalidRoleInputError(msg)
        return handler(payload)

    def _call_analyze_project(self, payload: object) -> object:
        """``analyze_project`` ejecuta **solo** al Architect y devuelve su informe."""
        return self._camus.analyze_project(cast("ProjectIntent", payload))

    def _call_plan_from_architecture(self, payload: object) -> object:
        """``plan_project_from_architecture`` ejecuta **solo** al Planner sobre el diseño.

        ``build_input`` debe devolver la pareja ``(ProjectIntent, ArchitectureOutcome)``, con el
        diseño del Architect reconstruido desde la referencia durable de la petición. Si el
        diseño no llega, se falla aquí: volver a ejecutar al Architect para rellenar el hueco
        sería exactamente la duplicación que este adaptador debe impedir.
        """
        intent, architecture = _pair(
            payload, RoleName.PLANNER, "ProjectIntent y ArchitectureOutcome"
        )
        if getattr(architecture, "proposal", None) is None:
            msg = (
                "falta el diseño del Architect: build_input debe reconstruirlo desde las "
                "referencias durables de la petición (RoleExecutionRequest.references). "
                "PUNTO no vuelve a ejecutar al Architect para suplirlo"
            )
            raise _InvalidRoleInputError(msg)
        return self._camus.plan_project_from_architecture(
            cast("ProjectIntent", intent), cast("ArchitectureOutcome", architecture)
        )

    def _call_developer(self, payload: object) -> object:
        """``execute_developer_task`` recibe la tarea de desarrollo y su contexto de ejecución."""
        task, context = _pair(payload, RoleName.DEVELOPER, "DeveloperTask y ExecutionContext")
        return self._camus.execute_developer_task(
            cast("DeveloperTask", task), cast("ExecutionContext", context)
        )

    def _call_qa(self, payload: object) -> object:
        """``qa_task`` delega en el ``QARunner`` inyectado."""
        return self._camus.qa_task(cast("QATask", payload))

    def _call_security(self, payload: object) -> object:
        """``security_task`` delega en el ``SecurityRunner`` inyectado."""
        return self._camus.security_task(cast("SecurityTask", payload))

    def _call_review(self, payload: object) -> object:
        """``review_task`` delega en el ``ReviewerRunner`` inyectado."""
        return self._camus.review_task(cast("ReviewTask", payload))

    def _call_cross_audit(self, payload: object) -> object:
        """``cross_audit`` delega en el ``CrossAuditRunner`` inyectado."""
        return self._camus.cross_audit(cast("CrossAuditTask", payload))

    def _call_visual_qa(self, payload: object) -> object:
        """``visual_qa`` recibe la tarea visual y las capturas ya verificadas por PUNTO."""
        task, screenshots = _pair(
            payload, RoleName.VISUAL_QA, "VisualQATask y las capturas verificadas"
        )
        return self._camus.visual_qa(
            cast("VisualQATask", task), cast("Mapping[str, ImagePayload]", screenshots)
        )


# ---------------------------------------------------------------------------
# Normalizadores puros
# ---------------------------------------------------------------------------
def normalize_architecture(outcome: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el resultado real del Architect.

    La entrada real es el ``ArchitectureOutcome`` que devuelve ``camus.analyze_project``. También
    se tolera —sin usarlo ya el adaptador— el ``ProjectPlanResult`` de ``camus.plan_project``, que
    lleva el mismo informe en ``architect``: leerlo no cuesta nada y evita que un llamante antiguo
    se quede sin normalizador.
    """
    status, error_code, status_detail = _map_status(outcome)
    summary = _model_summary(outcome, RoleName.ARCHITECT)
    return _result(
        RoleName.ARCHITECT,
        request,
        _RoleView(
            status=status,
            summary=_coalesce(_text(outcome, "summary"), _text(outcome, "error")),
            recommendation=_join(*_texts(outcome, "violations")),
            provider=_text(summary, "provider"),
            model=_text(summary, "model"),
            artifacts=_component_ids(outcome),
            usage=_as_usage(getattr(summary, "usage", None))
            or _as_usage(getattr(outcome, "model_usage", None)),
            attempts=_int(summary, "attempts_used"),
            started_at=_moment(outcome, "started_at"),
            completed_at=_moment(outcome, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(outcome, "error"), status_detail),
        ),
    )


def normalize_planning(outcome: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el resultado real del Planner.

    La entrada real es el ``PlanningOutcome`` que devuelve
    ``camus.plan_project_from_architecture``. Igual que el Architect, también tolera el
    ``ProjectPlanResult`` de ``camus.plan_project``; en ese caso el resumen está en ``planner``.
    """
    status, error_code, status_detail = _map_status(outcome)
    summary = _model_summary(outcome, RoleName.PLANNER)
    return _result(
        RoleName.PLANNER,
        request,
        _RoleView(
            status=status,
            summary=_coalesce(_text(outcome, "summary"), _text(outcome, "error")),
            recommendation=_join(*_texts(outcome, "violations")),
            provider=_text(summary, "provider"),
            model=_text(summary, "model"),
            artifacts=_planned_task_ids(outcome),
            usage=_as_usage(getattr(summary, "usage", None))
            or _as_usage(getattr(outcome, "model_usage", None)),
            attempts=_int(summary, "attempts_used"),
            started_at=_moment(outcome, "started_at"),
            completed_at=_moment(outcome, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(outcome, "error"), status_detail),
        ),
    )


def normalize_developer(result: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el ``DeveloperExecutionResult`` real.

    El Developer no declara hallazgos ni gravedades, así que este normalizador **no** fabrica
    ninguno: copia el estado, los archivos cambiados, los checks fallidos y el consumo real.
    """
    status, error_code, status_detail = _map_status(result)
    validation = _nested(result, "validation")
    return _result(
        RoleName.DEVELOPER,
        request,
        _RoleView(
            status=status,
            summary=_text_or_error(result),
            recommendation=_join(*_texts(validation, "failed_checks")),
            provider=_text(result, "provider"),
            model=_text(result, "model"),
            artifacts=_attribute_texts(result, "files_changed", "path"),
            usage=_as_usage(getattr(result, "usage", None)),
            attempts=_int(result, "attempts_used"),
            started_at=_moment(result, "started_at"),
            completed_at=_moment(result, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(result, "error"), status_detail),
        ),
    )


def normalize_qa(report: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el ``QAReport`` real."""
    status, error_code, status_detail = _map_status(report)
    return _result(
        RoleName.QA,
        request,
        _RoleView(
            status=status,
            summary=_text_or_error(report),
            recommendation=_join(
                *(_text(item, "repair_hint") for item in _sequence(report, "findings"))
            ),
            provider=_text(report, "provider"),
            model=_text(report, "model"),
            artifacts=(
                *_attribute_texts(report, "executed_checks", "name"),
                *_attribute_texts(report, "test_cases", "id"),
            ),
            findings=_findings(
                report, RoleName.QA, ("file", "acceptance_criterion", "repair_hint")
            ),
            usage=_as_usage(getattr(report, "model_usage", None)),
            attempts=_int(report, "attempts"),
            started_at=_moment(report, "started_at"),
            completed_at=_moment(report, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(report, "error"), status_detail),
        ),
    )


def normalize_security(report: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el ``SecurityReport`` real.

    Conserva la gravedad de cada hallazgo tal como la calculó PUNTO: un ``HIGH`` o ``CRITICAL``
    llega al kernel como hallazgo bloqueante y ninguna normalización lo rebaja.
    """
    status, error_code, status_detail = _map_status(report)
    return _result(
        RoleName.SECURITY,
        request,
        _RoleView(
            status=status,
            summary=_text_or_error(report),
            recommendation=_join(
                *(_text(item, "recommendation") for item in _sequence(report, "findings"))
            ),
            provider=_text(report, "provider"),
            model=_text(report, "model"),
            artifacts=_texts(report, "reviewed_files"),
            findings=_findings(
                report, RoleName.SECURITY, ("impact", "file", "acceptance_criterion")
            ),
            usage=_as_usage(getattr(report, "model_usage", None)),
            attempts=_int(report, "attempts"),
            started_at=_moment(report, "started_at"),
            completed_at=_moment(report, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(report, "error"), status_detail),
        ),
    )


def normalize_review(report: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el ``ReviewReport`` real (veredicto y gates calculados por PUNTO)."""
    status, error_code, status_detail = _map_status(report)
    return _result(
        RoleName.REVIEWER,
        request,
        _RoleView(
            status=status,
            summary=_text_or_error(report),
            recommendation=_text(report, "recommendation_notes"),
            provider=_text(report, "provider"),
            model=_text(report, "model"),
            artifacts=_texts(report, "model_visible_files"),
            findings=_findings(report, RoleName.REVIEWER, ("file", "recommendation")),
            usage=_as_usage(getattr(report, "model_usage", None)),
            attempts=_int(report, "attempts"),
            started_at=_moment(report, "started_at"),
            completed_at=_moment(report, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(report, "error"), status_detail),
        ),
    )


def normalize_cross_audit(report: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el ``CrossAuditReport`` real de la auditoría cruzada."""
    status, error_code, status_detail = _map_status(report)
    return _result(
        RoleName.CROSS_AUDIT,
        request,
        _RoleView(
            status=status,
            summary=_text_or_error(report),
            recommendation=_text(report, "recommendation_notes"),
            provider=_text(report, "provider"),
            model=_text(report, "model"),
            artifacts=_texts(report, "model_visible_files"),
            findings=_findings(report, RoleName.CROSS_AUDIT, ("file", "recommendation")),
            usage=_as_usage(getattr(report, "model_usage", None)),
            attempts=_int(report, "attempts"),
            started_at=_moment(report, "started_at"),
            completed_at=_moment(report, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(report, "error"), status_detail),
        ),
    )


def normalize_visual_qa(report: object, request: RoleExecutionRequest) -> RoleExecutionResult:
    """Normaliza el ``VisualQAReport`` real."""
    status, error_code, status_detail = _map_status(report)
    return _result(
        RoleName.VISUAL_QA,
        request,
        _RoleView(
            status=status,
            summary=_text_or_error(report),
            recommendation=_text(report, "recommendation_notes"),
            provider=_text(report, "provider"),
            model=_text(report, "model"),
            artifacts=(
                *_texts(report, "routes_analyzed"),
                *_texts(report, "viewports_analyzed"),
                *_texts(report, "screenshots_analyzed"),
            ),
            findings=_findings(report, RoleName.VISUAL_QA, ("route", "viewport", "recommendation")),
            usage=_as_usage(getattr(report, "model_usage", None)),
            attempts=_int(report, "attempts"),
            started_at=_moment(report, "started_at"),
            completed_at=_moment(report, "completed_at"),
            error_code=error_code,
            error_detail=_coalesce(_text(report, "error"), status_detail),
        ),
    )


#: Normalizador puro de cada rol. Es la tabla que usa :class:`CamusRoleExecutor`.
_NORMALIZERS: Final[Mapping[RoleName, _RoleNormalizer]] = MappingProxyType(
    {
        RoleName.ARCHITECT: normalize_architecture,
        RoleName.PLANNER: normalize_planning,
        RoleName.DEVELOPER: normalize_developer,
        RoleName.QA: normalize_qa,
        RoleName.SECURITY: normalize_security,
        RoleName.REVIEWER: normalize_review,
        RoleName.CROSS_AUDIT: normalize_cross_audit,
        RoleName.VISUAL_QA: normalize_visual_qa,
    }
)


# ---------------------------------------------------------------------------
# Construcción de resultados
# ---------------------------------------------------------------------------
def _result(role: RoleName, request: RoleExecutionRequest, view: _RoleView) -> RoleExecutionResult:
    """Construye el resultado normalizado, aplicando acotado y valores neutros documentados."""
    started_at = view.started_at or utc_now()
    return RoleExecutionResult(
        role=role,
        status=view.status,
        summary=_excerpt(
            view.summary or f"{role.value}: {view.status.value}", MAX_WORKFLOW_SUMMARY_CHARS
        ),
        artifacts=_artifacts(view.artifacts),
        findings=view.findings[:MAX_WORKFLOW_FINDINGS],
        recommendation=_excerpt(view.recommendation, MAX_WORKFLOW_TEXT_CHARS),
        usage=view.usage if view.usage is not None else ModelUsage(),
        provider=_excerpt(view.provider, _MAX_PROVIDER_CHARS),
        model=_excerpt(view.model, _MAX_MODEL_CHARS),
        attempts=view.attempts if view.attempts >= 1 else request.attempt,
        started_at=started_at,
        completed_at=view.completed_at or started_at,
        error_code=view.error_code,
        error_detail=_excerpt(view.error_detail, MAX_WORKFLOW_TEXT_CHARS),
    )


def _failure(
    role: RoleName,
    request: RoleExecutionRequest,
    *,
    status: RoleStatus,
    code: WorkflowFailureCode,
    detail: str,
) -> RoleExecutionResult:
    """Resultado de fallo sin trabajo producido: el rol no llegó a ejecutarse."""
    return _result(role, request, _RoleView(status=status, error_code=code, error_detail=detail))


def _unavailable(
    role: RoleName, request: RoleExecutionRequest, detail: str
) -> RoleExecutionResult:
    """Resultado de proveedor no disponible, con el rol y lo que falta en el detalle."""
    return _failure(
        role,
        request,
        status=RoleStatus.PROVIDER_UNAVAILABLE,
        code=WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
        detail=detail,
    )


def _pair(payload: object, role: RoleName, expectation: str) -> tuple[object, object]:
    """Descompone la pareja que ``build_input`` debe devolver para los roles de dos argumentos."""
    if isinstance(payload, tuple) and len(payload) == 2:
        return payload[0], payload[1]
    msg = (
        f"build_input debe devolver {expectation} para el rol {role.value}; "
        f"se recibió {type(payload).__name__}"
    )
    raise _InvalidRoleInputError(msg)


# ---------------------------------------------------------------------------
# Lectura defensiva del informe real
# ---------------------------------------------------------------------------
def _state_value(report: object) -> str:
    """Estado real del informe en mayúsculas, o cadena vacía si no lo declara."""
    raw = getattr(report, "status", None)
    value = getattr(raw, "value", raw)
    return value.strip().upper() if isinstance(value, str) else ""


def _map_status(report: object) -> tuple[RoleStatus, WorkflowFailureCode | None, str]:
    """Traduce el estado real del informe a estado normalizado, código y detalle.

    El detalle solo se rellena cuando el estado **no** es reconocible: en ese caso es la
    explicación de por qué el kernel no puede tratarlo como éxito.
    """
    state = _state_value(report)
    if state in _COMPLETED_STATES:
        return RoleStatus.COMPLETED, None, ""
    if state in _REPAIR_STATES:
        return RoleStatus.NEEDS_REPAIR, None, ""
    if state in _BLOCKED_STATES:
        return RoleStatus.BLOCKED, None, ""
    detail = (
        f"estado {state!r} no reconocido por el kernel: no se trata como éxito"
        if state
        else "el informe no declara estado: no se trata como éxito"
    )
    return RoleStatus.FAILED, WorkflowFailureCode.WORKFLOW_ROLE_FAILED, detail


def _text(report: object, field: str) -> str:
    """Campo de texto del informe, o cadena vacía si falta o no es texto."""
    raw = getattr(report, field, None)
    return raw if isinstance(raw, str) else ""


def _text_or_error(report: object) -> str:
    """Resumen textual del informe: el ``summary`` real o, si no lo hay, su error."""
    return _coalesce(_text(report, "summary"), _text(report, "error"))


def _int(report: object, field: str) -> int:
    """Campo entero del informe, o ``0`` si falta (``0`` significa «no lo declara»)."""
    raw = getattr(report, field, None)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return 0


def _moment(report: object, field: str) -> datetime | None:
    """Marca temporal del informe, o ``None`` si no la declara."""
    raw = getattr(report, field, None)
    return raw if isinstance(raw, datetime) else None


def _as_usage(value: object) -> ModelUsage | None:
    """Consumo de tokens, solo si el valor es realmente un ``ModelUsage``."""
    return value if isinstance(value, ModelUsage) else None


def _sequence(report: object, field: str) -> tuple[object, ...]:
    """Elementos de una colección del informe, o tupla vacía si falta."""
    raw = getattr(report, field, None)
    if isinstance(raw, (list, tuple)):
        return tuple(raw)
    return ()


def _texts(report: object, field: str) -> tuple[str, ...]:
    """Textos de una colección de cadenas del informe, en orden."""
    return tuple(item for item in _sequence(report, field) if isinstance(item, str) and item)


def _attribute_texts(report: object | None, field: str, attribute: str) -> tuple[str, ...]:
    """Atributo de texto de cada elemento de una colección del informe, en orden."""
    return tuple(
        value
        for item in _sequence(report, field)
        if isinstance(value := getattr(item, attribute, None), str) and value
    )


def _nested(report: object, *fields: str) -> object | None:
    """Atributo anidado del informe, o ``None`` en cuanto falta un eslabón."""
    current: object | None = report
    for field in fields:
        if current is None:
            return None
        current = getattr(current, field, None)
    return current


def _model_summary(report: object, role: RoleName) -> ModelExecutionSummary | None:
    """Resumen de ejecución del rol, si el informe lo declara.

    ``ArchitectureOutcome`` y ``PlanningOutcome`` —lo que devuelven ``analyze_project`` y
    ``plan_project_from_architecture``— lo llevan en ``summary``; el ``ProjectPlanResult`` de
    ``camus.plan_project`` lo separa en ``architect`` y ``planner``. Se aceptan los dos.
    """
    direct = getattr(report, "summary", None)
    if isinstance(direct, ModelExecutionSummary):
        return direct
    field = "architect" if role is RoleName.ARCHITECT else "planner"
    nested = getattr(report, field, None)
    if isinstance(nested, ModelExecutionSummary):
        return nested
    return None


def _component_ids(outcome: object) -> tuple[str, ...]:
    """Componentes del plan de arquitectura, como referencias de artefacto."""
    architecture = _nested(outcome, "proposal", "architecture")
    if architecture is None:
        architecture = _nested(outcome, "architecture")
    return _attribute_texts(architecture, "components", "id")


def _planned_task_ids(outcome: object) -> tuple[str, ...]:
    """Tareas del roadmap, como referencias de artefacto."""
    return _attribute_texts(_nested(outcome, "roadmap"), "tasks", "id")


def _enum_text(value: object) -> str:
    """Texto de un valor de enumeración (su ``value``) o de una cadena ya lista."""
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else ""


def _severity(value: object) -> FindingSeverity:
    """Gravedad real del hallazgo; ``MEDIUM`` si el informe no declara una reconocible.

    No se degrada a ``INFO``: una gravedad ilegible no es una gravedad benigna, y tratar una
    como la otra minimizaría el riesgo sin decirlo.
    """
    raw = getattr(value, "value", value)
    if isinstance(raw, str):
        try:
            return FindingSeverity(raw.strip().upper())
        except ValueError:
            return FindingSeverity.MEDIUM
    return FindingSeverity.MEDIUM


def _findings(
    report: object, role: RoleName, extras: tuple[str, ...] = ()
) -> tuple[WorkflowFinding, ...]:
    """Copia los hallazgos del informe con su gravedad real, acotados y recortados.

    ``extras`` son campos propios del rol (impacto, archivo, ruta visual…) que se anexan a la
    evidencia para no perder contexto al normalizar, siempre acotados.
    """
    collected: list[WorkflowFinding] = []
    for item in _sequence(report, "findings")[:MAX_WORKFLOW_FINDINGS]:
        reference = _join(*(_text(item, field) for field in extras))
        collected.append(
            WorkflowFinding(
                role=role,
                severity=_severity(getattr(item, "severity", None)),
                category=_excerpt(
                    _enum_text(getattr(item, "category", None)), _MAX_CATEGORY_CHARS
                ),
                message=_excerpt(
                    _coalesce(
                        _join(_text(item, "title"), _text(item, "description")), _NO_DETAIL
                    ),
                    MAX_WORKFLOW_TEXT_CHARS,
                ),
                evidence=_excerpt(
                    _join(_text(item, "evidence"), reference), MAX_WORKFLOW_TEXT_CHARS
                ),
            )
        )
    return tuple(collected)


# ---------------------------------------------------------------------------
# Utilidades puras
# ---------------------------------------------------------------------------
def _excerpt(value: object, limit: int) -> str:
    """Recorta un texto al límite indicado, sin inventar contenido.

    Un valor que no sea texto se traduce a cadena vacía: preferimos un hueco declarado a un
    ``str()`` de un objeto que nadie quiso convertir en prosa.
    """
    if limit < 1 or not isinstance(value, str):
        return ""
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _coalesce(*values: str) -> str:
    """Primer valor no vacío, o cadena vacía si no hay ninguno."""
    for value in values:
        if value:
            return value
    return ""


def _join(*parts: str) -> str:
    """Une los textos no vacíos con ``"; "``, en orden y sin duplicar separadores."""
    return "; ".join(part for part in parts if part)


def _artifacts(values: tuple[str, ...]) -> tuple[str, ...]:
    """Acota y deduplica las referencias de artefacto, conservando el orden."""
    bounded: list[str] = []
    for value in values:
        text = _excerpt(value, MAX_WORKFLOW_TEXT_CHARS)
        if text and text not in bounded:
            bounded.append(text)
        if len(bounded) == MAX_WORKFLOW_ARTIFACTS:
            break
    return tuple(bounded)


__all__ = [
    "CallableRoleExecutor",
    "CamusRoleExecutor",
    "RoleExecutor",
    "UnavailableRoleExecutor",
    "normalize_architecture",
    "normalize_cross_audit",
    "normalize_developer",
    "normalize_planning",
    "normalize_qa",
    "normalize_review",
    "normalize_security",
    "normalize_visual_qa",
]
