"""API FastAPI mínima del núcleo constitucional (ENGINE-0).

Superficie pública deliberadamente reducida. ENGINE-0 **no** expone ninguna ruta
capaz de manipular directamente el estado de una tarea ni de resolver un Human
Gate: el estado solo cambia a través del dominio (``Camus`` + ``PolicyEngine`` +
``HumanGate``). En particular, *no existe* un endpoint genérico de transiciones.

Endpoints:

- ``GET  /health``             estado del motor y versión.
- ``GET  /engine``             información ampliada e integridad constitucional.
- ``POST /tasks``              crea una tarea y la procesa con CAMUS.
- ``GET  /tasks``              lista las tareas en memoria.
- ``GET  /tasks/{task_id}``     detalle de una tarea (solo lectura).
- ``GET  /human-gate``         introspección de solicitudes de aprobación.
- ``GET  /human-gate/{id}``     detalle de una solicitud (solo lectura).
- ``POST /policy/evaluate``    evalúa una acción sin crear tarea (solo lectura).
- ``GET  /policy/authority``   catálogo de autoridad activo (solo lectura).
- ``GET  /audit/events``       eventos de auditoría (solo lectura).

Invariante de la superficie HTTP: ninguna ruta puede sacar una tarea de
``HUMAN_APPROVAL`` sin una ``HumanApprovalRequest`` en estado ``APPROVED``. La
única vía autorizada es ``Camus.resume()``, que valida el gate y recupera la
``PolicyDecision`` vinculada a esa solicitud concreta.

Persistencia exclusivamente en memoria. Sin integraciones externas, sin IA.

Códigos de error:

- ``404`` tarea o solicitud de aprobación inexistente.
- ``405`` método no permitido sobre una ruta existente (p. ej. ``POST`` sobre
  ``/tasks/{task_id}``): no hay ruta de mutación que atender.
- ``409`` conflicto de dominio.
- ``422`` petición inválida (validación de esquema).
- ``503`` configuración del motor no disponible.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from punto._version import ENGINE_NAME, ENGINE_PHASE, ENGINE_VERSION
from punto.api.runtime_routes import (
    RuntimeFactory,
    register_runtime,
    runtime_enabled,
    runtime_lifespan,
)
from punto.audit.logger import AuditLogger
from punto.orchestrator.build_cycle import (
    BuildCycle,
    BuildCycleError,
    default_build_cycle,
)
from punto.orchestrator.camus import Camus, RequestOverrides
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import InvalidTransitionError, StateMachine
from punto.policy.config_loader import ConfigError
from punto.policy.human_gate import (
    HumanGate,
    HumanGateError,
    HumanGateNotApprovedError,
    HumanGateNotFoundError,
)
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.audit import AuditEvent
from punto.schemas.build import BuildRequest, BuildResult
from punto.schemas.decision import ActionRequest, HumanApprovalRequest
from punto.schemas.enums import AuthorityLevel, RiskLevel, TaskPriority, TaskStatus
from punto.schemas.policy import PolicyDecision
from punto.schemas.task import Task
from punto.tasks.manager import TaskManager, TaskNotFoundError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from punto.runtime.assembly import MultiTaskRuntime


# ============================================================================
# Contenedor del motor
# ============================================================================
class Engine:
    """Contenedor de composición del motor: un ensamblado explícito y único.

    Todas las dependencias se inyectan por constructor. No hay estado global
    oculto ni singletons implícitos: el motor se construye una vez en el arranque
    de la API y se comparte entre peticiones.
    """

    def __init__(
        self, *, environment: str = "local", build_cycle: BuildCycle | None = None
    ) -> None:
        self.environment = environment
        self.audit = AuditLogger()
        self.state_machine = StateMachine()
        self.task_manager = TaskManager(state_machine=self.state_machine, audit=self.audit)
        self.policy_engine = PolicyEngine.from_config(environment=environment)
        self.human_gate = HumanGate()
        self.planner = Planner()
        self.camus = Camus(
            task_manager=self.task_manager,
            policy_engine=self.policy_engine,
            human_gate=self.human_gate,
            audit=self.audit,
            state_machine=self.state_machine,
            planner=self.planner,
        )
        # El ciclo de construcción gobernada se compone en el primer uso, salvo que se inyecte uno
        # ya montado (pruebas y composición explícita): un motor sin destinos ni proveedores
        # configurados puede arrancar y responder al resto de la API, y el ciclo dice lo que le
        # falta cuando alguien pide trabajo de verdad.
        self._build_cycle: BuildCycle | None = build_cycle
        if build_cycle is not None:
            # El ciclo comparte el registro de auditoría del motor aunque venga montado desde fuera:
            # así la reconstrucción de un ciclo por ``request_id`` es **una sola** consulta
            # (``/audit/events?resource_id=...``) y no depende de dónde se compuso el ciclo.
            build_cycle.audit = self.audit
        self.build_results: dict[str, BuildResult] = {}

    def build_cycle(self) -> BuildCycle:
        """Ciclo de construcción gobernada, compuesto con la configuración vigente.

        Raises:
            BuildCycleError: si la configuración de destinos no se puede leer.
        """
        if self._build_cycle is None:
            self._build_cycle = default_build_cycle(audit=self.audit)
        return self._build_cycle

    def run_build_request(self, request: BuildRequest) -> BuildResult:
        """Ejecuta una solicitud y conserva su resultado para poder consultarlo después.

        El resultado se guarda por ``request_id``: es la misma clave con la que se puede
        reconstruir el ciclo entero desde ``/audit/events``.
        """
        result = self.build_cycle().run(request)
        self.build_results[str(request.request_id)] = result
        return result

    def build_targets(self) -> tuple[str, ...]:
        """Claves de destino registradas en la configuración vigente."""
        return tuple(sorted(self.build_cycle().config.targets))

    def health(self) -> dict[str, str]:
        """Información de salud del motor."""
        return {
            "status": "ok",
            "engine": ENGINE_NAME,
            "version": ENGINE_VERSION,
        }

    def engine_info(self) -> dict[str, Any]:
        """Información ampliada del motor."""
        return {
            "engine": ENGINE_NAME,
            "version": ENGINE_VERSION,
            "phase": ENGINE_PHASE,
            "environment": self.environment,
            "tasks_in_memory": self.task_manager.count(),
            "audit_events": self.audit.count(),
            "policy_decisions": len(self.policy_engine.decisions),
            "pending_human_gates": len(self.human_gate.list_pending()),
            "integrity": self.integrity_report(),
        }

    def integrity_report(self) -> dict[str, Any]:
        """Estado verificable de las garantías constitucionales."""
        protected = self.policy_engine.protected_paths
        return {
            "protected_files": list(protected),
            "default_deny": True,
            "llm_enabled": False,
            "external_integrations": [],
            "active_environment": self.environment,
            "constitutional_floor": True,
        }

    def reload_policy(self) -> None:
        """Recarga la configuración de política desde disco (uso explícito)."""
        self.policy_engine = PolicyEngine.from_config(environment=self.environment)


# ============================================================================
# Esquemas de la API
# ============================================================================
class TaskCreateRequest(BaseModel):
    """Cuerpo de ``POST /tasks``."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(
        ...,
        min_length=1,
        description="Objetivo de la tarea en lenguaje natural.",
        examples=["Crear el módulo de facturación"],
    )
    action: str = Field(
        ...,
        min_length=1,
        description="Acción canónica a ejecutar (clave del catálogo de autoridad).",
        examples=["create_file"],
    )
    description: str = Field(default="", description="Descripción ampliada del objetivo.")
    priority: TaskPriority = Field(default=TaskPriority.NORMAL, description="Prioridad.")
    risk_level: RiskLevel = Field(
        default=RiskLevel.LOW, description="Riesgo declarado por quien solicita."
    )
    technical: bool = Field(default=True, description="True si la acción es técnica.")
    reversible: bool = Field(default=True, description="True si la acción es reversible.")
    production_impact: bool = Field(default=False, description="True si afecta producción.")
    legal_impact: bool = Field(default=False, description="True si tiene impacto legal.")
    business_impact: bool = Field(default=False, description="True si altera el negocio.")
    estimated_cost: float = Field(default=0.0, ge=0.0, description="Costo estimado (USD).")
    estimated_minutes: float = Field(default=0.0, ge=0.0, description="Tiempo estimado (minutos).")
    files_changed: list[str] = Field(
        default_factory=list, description="Rutas afectadas por la acción."
    )
    max_cost_usd: float | None = Field(
        default=None, ge=0.0, description="Presupuesto máximo de la tarea (USD)."
    )
    max_execution_minutes: float | None = Field(
        default=None, ge=0.0, description="Tiempo máximo de la tarea (minutos)."
    )
    max_files_changed: int | None = Field(
        default=None, ge=0, description="Máximo de archivos modificables."
    )
    max_attempts: int | None = Field(default=None, ge=0, description="Intentos máximos permitidos.")
    project_id: UUID | None = Field(default=None, description="Proyecto de la tarea.")
    parent_task_id: UUID | None = Field(default=None, description="Tarea padre, si aplica.")


class PolicyEvaluateRequest(BaseModel):
    """Cuerpo de ``POST /policy/evaluate``."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., min_length=1, description="Acción a evaluar.")
    technical: bool = Field(default=True)
    reversible: bool = Field(default=True)
    risk_level: RiskLevel = Field(default=RiskLevel.LOW)
    production_impact: bool = Field(default=False)
    legal_impact: bool = Field(default=False)
    business_impact: bool = Field(default=False)
    estimated_cost: float = Field(default=0.0, ge=0.0)
    estimated_minutes: float = Field(default=0.0, ge=0.0)
    files_changed: list[str] = Field(default_factory=list)


# ============================================================================
# Aplicación
# ============================================================================
def create_app(
    *,
    environment: str = "local",
    build_cycle: BuildCycle | None = None,
    runtime: RuntimeFactory | None = None,
) -> FastAPI:
    """Construye la aplicación FastAPI con el motor ensamblado.

    Args:
        runtime: Fábrica del runtime Multi-Task del proceso (Fase 15). Sin ella, la aplicación por
            defecto lo compone desde la configuración vigente solo si ``PUNTO_MULTITASK_RUNTIME``
            lo activa. El ``lifespan`` lo arranca (reconciliando antes de admitir) y lo detiene.
    """
    application = FastAPI(
        lifespan=runtime_lifespan,
        title=ENGINE_NAME,
        version=ENGINE_VERSION,
        description=(
            "Núcleo constitucional determinista (Base Constitucional V0.1). "
            "Sin IA, sin integraciones externas, sin persistencia externa."
        ),
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    try:
        engine = Engine(environment=environment, build_cycle=build_cycle)
    except ConfigError as exc:
        message = str(exc)

        @application.get("/health", tags=["engine"])
        def _unconfigured_health() -> JSONResponse:  # pragma: no cover - arranque roto
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "error", "engine": ENGINE_NAME, "detail": message},
            )

        # El dashboard de proveedores **no** depende del motor de tareas: se registra igual, para
        # que la configuración de proveedores siga siendo administrable aunque falte la
        # configuración constitucional.
        from punto.api.dashboard import register_dashboard

        register_dashboard(application)
        return application

    application.state.engine = engine
    _register_routes(application, engine)
    # PROVIDER DASHBOARD v0: la pagina de configuracion de proveedores y sus endpoints se registran
    # sobre esta misma aplicacion (no hay un segundo backend).
    from punto.api.dashboard import register_dashboard

    register_dashboard(application)
    # CONSOLA HUMANA LOCAL: la misma pagina pasa a ser consola de tareas, Human Gates y publicacion
    # a produccion. Reutiliza el ciclo, los gates, la politica y la auditoria del motor.
    from punto.api.console import register_human_console

    register_human_console(application)
    # RUNTIME MULTI-TASK (Fase 15): UN owner por proceso, arrancado por el lifespan de esta misma
    # aplicación; la consola y la proyección leen el mismo documento durable que él escribe.
    register_runtime(application, runtime if runtime is not None else _configured_runtime(engine))
    return application


def _configured_runtime(engine: Engine) -> RuntimeFactory | None:
    """Runtime productivo con la configuración vigente, si el entorno lo activa."""
    if not runtime_enabled(os.environ):
        return None

    def factory() -> MultiTaskRuntime:
        from punto.runtime.assembly import production_runtime

        return production_runtime(audit=engine.audit)

    return factory


def get_engine(application: FastAPI) -> Engine:
    """Devuelve el motor asociado a una aplicación."""
    engine: Engine = application.state.engine
    return engine


def _register_routes(application: FastAPI, engine: Engine) -> None:
    """Registra todos los endpoints sobre la aplicación."""

    # ------------------------------------------------------------------ health
    @application.get("/health", tags=["engine"], summary="Estado del motor")
    def health() -> dict[str, str]:
        """Devuelve el estado del motor y su versión."""
        return engine.health()

    @application.get("/engine", tags=["engine"], summary="Información del motor")
    def engine_info() -> dict[str, Any]:
        """Información ampliada: entorno, contadores e integridad constitucional."""
        return engine.engine_info()

    # ------------------------------------------------------------------- tasks
    @application.post(
        "/tasks",
        tags=["tasks"],
        summary="Crear y procesar una tarea",
        status_code=status.HTTP_201_CREATED,
    )
    def create_task(payload: TaskCreateRequest) -> dict[str, Any]:
        """Crea una tarea y la procesa con CAMUS.

        La respuesta incluye la decisión de política aplicada. Si la tarea queda
        bloqueada o a la espera de aprobación humana, el campo ``outcome`` lo
        indica de forma explícita (no se convierte en error HTTP, porque la
        tarea se creó correctamente y su estado es información válida).
        """
        overrides = RequestOverrides(
            risk_level=payload.risk_level,
            technical=payload.technical,
            reversible=payload.reversible,
            production_impact=payload.production_impact,
            legal_impact=payload.legal_impact,
            business_impact=payload.business_impact,
            estimated_cost=payload.estimated_cost,
            estimated_minutes=payload.estimated_minutes,
            files_changed=tuple(payload.files_changed),
            priority=payload.priority,
            description=payload.description,
        )
        result = engine.camus.process_request(
            objective=payload.objective,
            action=payload.action,
            overrides=overrides,
            project_id=payload.project_id,
            parent_task_id=payload.parent_task_id,
            max_cost_usd=payload.max_cost_usd,
            max_execution_minutes=payload.max_execution_minutes,
            max_files_changed=payload.max_files_changed,
            max_attempts=payload.max_attempts,
        )
        return {
            "id": str(result.task.id),
            "status": result.task.status.value,
            "outcome": result.outcome.value,
            "action": result.decision.action,
            "allowed": result.decision.allowed,
            "requires_review": result.decision.requires_review,
            "requires_human": result.decision.requires_human,
            "authority_level": result.decision.authority_level.name,
            "effective_risk": result.decision.effective_risk.name,
            "reason": result.decision.reason,
            "blocked_reason": (
                result.blocked_reason.value if result.blocked_reason is not None else None
            ),
            "human_approval_id": (
                str(result.human_approval.id) if result.human_approval is not None else None
            ),
            "detail": result.detail,
            "created_at": result.task.created_at.isoformat(),
            "task": result.task.model_dump(mode="json"),
        }

    @application.get("/tasks", tags=["tasks"], summary="Listar tareas")
    def list_tasks(
        task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
        project_id: Annotated[UUID | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        """Lista las tareas almacenadas en memoria."""
        tasks = engine.task_manager.list_tasks(status=task_status, project_id=project_id)
        selected = tasks[:limit]
        return {
            "total": len(tasks),
            "returned": len(selected),
            "items": [_task_summary(task) for task in selected],
        }

    @application.get("/tasks/{task_id}", tags=["tasks"], summary="Detalle de una tarea")
    def get_task(task_id: UUID) -> dict[str, Any]:
        """Devuelve el detalle de una tarea.

        Solo lectura. No existe un endpoint público de transición: el estado de
        una tarea únicamente cambia a través del dominio (CAMUS), de modo que
        ninguna ruta HTTP puede sacar una tarea de ``HUMAN_APPROVAL`` sin una
        aprobación humana válida.
        """
        task = engine.task_manager.get_task(task_id)
        return {
            **_task_summary(task),
            "task": task.model_dump(mode="json"),
            "allowed_transitions": sorted(
                state.value for state in engine.task_manager.allowed_transitions(task.id)
            ),
        }

    # ---------------------------------------------------------- build requests
    @application.get(
        "/build-targets",
        tags=["build"],
        summary="Destinos de construcción registrados",
    )
    def list_build_targets() -> dict[str, Any]:
        """Destinos que PUNTO acepta como destino de una solicitud de construcción.

        La ruta real del repositorio **no** se publica: la solicitud nombra una clave registrada y
        el motor resuelve la ruta por su cuenta. Publicar esta lista no autoriza a nadie a pedir
        trabajo: la admisión se decide por solicitud.
        """
        cycle = engine.build_cycle()
        return {
            "targets": [
                {
                    "target_id": target.target_id,
                    "scope_roots": list(target.scope_roots),
                }
                for target in (cycle.config.targets[key] for key in sorted(cycle.config.targets))
            ],
            "max_output_tokens": cycle.config.max_output_tokens,
            "authority": "PROPOSAL_ONLY",
        }

    @application.post(
        "/build-requests",
        tags=["build"],
        summary="Solicitar una construcción gobernada",
        status_code=status.HTTP_201_CREATED,
    )
    def create_build_request(payload: BuildRequest) -> dict[str, Any]:
        """Admite una solicitud, la ejecuta por el ciclo gobernado y devuelve el resultado.

        PUNTO no aplica nada: el proveedor produce una propuesta, PUNTO la valida con sus propias
        comprobaciones y el resultado sale con ``authority: PROPOSAL_ONLY``. Un fallo del proveedor
        **no** es un error HTTP: es un estado del resultado (``PROVIDER_FAILED``), porque el ciclo
        sí se ejecutó y su desenlace es información válida.
        """
        result = engine.run_build_request(payload)
        return {
            **result.as_public_dict(),
            "audit_resource": str(result.request_id),
            "applied": False,
        }

    @application.get(
        "/build-requests/{request_id}",
        tags=["build"],
        summary="Resultado de una solicitud de construcción",
    )
    def get_build_request(request_id: UUID) -> dict[str, Any]:
        """Devuelve el resultado normalizado de una solicitud ya ejecutada."""
        stored = engine.build_results.get(str(request_id))
        if stored is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Solicitud de construcción no encontrada: {request_id}",
            )
        return {
            **stored.as_public_dict(),
            "audit_resource": str(stored.request_id),
            "applied": False,
        }

    # -------------------------------------------------------------- human gate
    @application.get("/human-gate", tags=["human-gate"], summary="Listar Human Gates")
    def list_human_gates(
        pending_only: Annotated[bool, Query()] = False,
    ) -> dict[str, Any]:
        """Lista solicitudes de aprobación humana."""
        approvals = (
            engine.human_gate.list_pending() if pending_only else engine.human_gate.list_all()
        )
        return {
            "total": len(approvals),
            "items": [_approval_payload(approval) for approval in approvals],
        }

    @application.get(
        "/human-gate/{approval_id}",
        tags=["human-gate"],
        summary="Detalle de un Human Gate",
    )
    def get_human_gate(approval_id: UUID) -> dict[str, Any]:
        """Devuelve el detalle de una solicitud de aprobación humana.

        Solo lectura. La resolución del gate **no** se expone por HTTP en
        ENGINE-0: se realiza exclusivamente a través de la lógica de dominio
        ``Camus.resume()``, que exige que la solicitud esté ``APPROVED`` y
        recupera la ``PolicyDecision`` vinculada a *esa* solicitud.
        """
        approval = engine.human_gate.get(approval_id)
        if approval is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Solicitud de aprobación no encontrada: {approval_id}",
            )
        return _approval_payload(approval)

    # ------------------------------------------------------------------ policy
    @application.post("/policy/evaluate", tags=["policy"], summary="Evaluar una acción")
    def evaluate_policy(payload: PolicyEvaluateRequest) -> dict[str, Any]:
        """Evalúa una acción sin crear tarea ni ejecutar nada."""
        request = ActionRequest(
            action=payload.action,
            technical=payload.technical,
            reversible=payload.reversible,
            risk_level=payload.risk_level,
            production_impact=payload.production_impact,
            legal_impact=payload.legal_impact,
            business_impact=payload.business_impact,
            estimated_cost=payload.estimated_cost,
            estimated_minutes=payload.estimated_minutes,
            files_changed=list(payload.files_changed),
        )
        decision = engine.policy_engine.evaluate(request)
        engine.audit.log_policy_decision(decision)
        return _decision_payload(decision)

    @application.get(
        "/policy/authority",
        tags=["policy"],
        summary="Catálogo de autoridad",
    )
    def authority_catalog() -> dict[str, Any]:
        """Devuelve el catálogo de autoridad activo por nivel."""
        catalog = engine.policy_engine.catalog
        levels: dict[str, list[str]] = {}
        for raw_level in range(4):
            levels[str(raw_level)] = list(catalog.actions_by_level(AuthorityLevel(raw_level)))
        return {
            "default_deny": True,
            "actions": list(catalog.all_actions()),
            "levels": levels,
            "protected_files": list(engine.policy_engine.protected_paths),
        }

    # ------------------------------------------------------------------- audit
    @application.get("/audit/events", tags=["audit"], summary="Eventos de auditoría")
    def audit_events(
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        resource_id: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        """Lista los eventos de auditoría en memoria."""
        events: Sequence[AuditEvent] = (
            engine.audit.by_resource(resource_id) if resource_id else engine.audit.events()
        )
        selected = list(events)[-limit:]
        return {
            "total": len(events),
            "returned": len(selected),
            "items": [_audit_payload(event) for event in selected],
        }

    # ------------------------------------------------------------- excepciones
    @application.exception_handler(TaskNotFoundError)
    def _task_not_found(_request: Request, exc: TaskNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": f"Tarea no encontrada: {exc.task_id}", "task_id": exc.task_id},
        )

    @application.exception_handler(InvalidTransitionError)
    def _invalid_transition(_request: Request, exc: InvalidTransitionError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": str(exc),
                "current": exc.current.value,
                "target": exc.target.value,
            },
        )

    @application.exception_handler(HumanGateNotFoundError)
    def _gate_missing(_request: Request, exc: HumanGateNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(exc), "error": "human_gate_not_found"},
        )

    @application.exception_handler(HumanGateNotApprovedError)
    def _gate_not_approved(_request: Request, exc: HumanGateNotApprovedError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "error": "human_gate_not_approved"},
        )

    @application.exception_handler(HumanGateError)
    def _gate_error(_request: Request, exc: HumanGateError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "error": "human_gate_error"},
        )

    @application.exception_handler(BuildCycleError)
    def _build_rejected(_request: Request, exc: BuildCycleError) -> JSONResponse:
        """Una solicitud que la frontera rechaza no se ejecuta: se declara rechazada.

        El rechazo se registra en la auditoría por ``request_id`` (evento
        ``BUILD_REQUEST_REJECTED``) y no se invoca a ningún proveedor.
        """
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": str(exc),
                "error": "build_request_rejected",
                "status": "REQUEST_REJECTED",
                "authority": "PROPOSAL_ONLY",
                "provider_invoked": False,
            },
        )

    @application.exception_handler(ValueError)
    def _value_error(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": str(exc), "error": "invalid_request"},
        )


# ============================================================================
# Serializadores internos
# ============================================================================
def _task_summary(task: Task) -> dict[str, Any]:
    """Resumen serializable de una tarea."""
    return {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "title": task.title,
        "status": task.status.value,
        "priority": task.priority.value,
        "risk_level": task.risk_level.name,
        "authority_level": task.authority_level.name,
        "assigned_agent": task.assigned_agent,
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "current_cost_usd": task.current_cost_usd,
        "max_cost_usd": task.max_cost_usd,
        "max_execution_minutes": task.max_execution_minutes,
        "max_files_changed": task.max_files_changed,
        "blocked_reason": task.blocked_reason.value if task.blocked_reason else None,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


def _decision_payload(decision: PolicyDecision) -> dict[str, Any]:
    """Representación serializable de una decisión de política."""
    return {
        "allowed": decision.allowed,
        "authority_level": decision.authority_level.name,
        "requires_review": decision.requires_review,
        "requires_human": decision.requires_human,
        "reason": decision.reason,
        "outcome": decision.outcome.value,
        "effective_risk": decision.effective_risk.name,
        "action": decision.action,
        "reasons": list(decision.reasons),
        "protected_files": list(decision.protected_files),
    }


def _approval_payload(approval: HumanApprovalRequest) -> dict[str, Any]:
    """Representación serializable de una solicitud de Human Gate."""
    return {
        "id": str(approval.id),
        "task_id": str(approval.task_id),
        "action": approval.action,
        "risk": approval.risk.name,
        "reason": approval.reason,
        "status": approval.status.value,
        "resume_status": approval.resume_status,
        "policy_outcome": approval.policy_outcome,
        "policy_decision_id": (
            str(approval.policy_decision_id) if approval.policy_decision_id else None
        ),
        "requested_at": approval.requested_at.isoformat(),
        "resolved_at": approval.resolved_at.isoformat() if approval.resolved_at else None,
        "resolved_by": approval.resolved_by,
        "resolution_note": approval.resolution_note,
        "is_pending": approval.is_pending,
    }


def _audit_payload(event: AuditEvent) -> dict[str, Any]:
    """Representación serializable de un evento de auditoría."""
    return {
        "id": str(event.id),
        "timestamp": event.timestamp.isoformat(),
        "event_type": event.event_type.value,
        "actor": event.actor,
        "action": event.action,
        "resource": event.resource,
        "resource_id": event.resource_id,
        "result": event.result.value,
        "metadata": {
            key: (list(value) if isinstance(value, tuple) else value)
            for key, value in event.metadata
        },
    }


#: Aplicación ASGI por defecto usada por ``uvicorn punto.api.app:app``.
app = create_app()


__all__ = [
    "Engine",
    "PolicyEvaluateRequest",
    "TaskCreateRequest",
    "app",
    "create_app",
    "get_engine",
]
