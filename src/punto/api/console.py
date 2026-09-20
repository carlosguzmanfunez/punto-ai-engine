"""Consola humana local: crea tareas, sigue su etapa, atiende Human Gates y publica a producción.

Se registra **sobre la aplicación FastAPI que ya existe** (`punto.api.app`) y **reutiliza** la
maquinaria del motor, sin construir un sistema paralelo:

- la identidad de la tarea es la de la propia solicitud gobernada (``BuildRequest.request_id``), de
  modo que ``/audit/events?resource_id=<task_id>`` reconstruye **todo** lo que pasó con ella;
- el trabajo lo hace el ``DevelopmentCycle`` de siempre (PELL → plan → autoridad → proveedores →
  workspace → verificación → commit local);
- los Human Gates son los ``HumanGate`` del motor, con su ``HumanApprovalRequest`` ligada a la
  ``PolicyDecision`` que la originó;
- la publicación a producción la hace ``punto.publish.production`` y **exige** un gate aprobado
  (``HumanGate.assert_executable``): no existe otra noción de autorización.

Lo que esta capa **no** hace: no concede autoridad, no resuelve gates por su cuenta, no inventa
estados del motor, no publica sin aprobación humana y no devuelve secretos ni prompts completos.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any, Final
from uuid import UUID

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from punto.api.task_progress import TaskSignals, build_progress
from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.orchestrator.dev_cycle import DevelopmentCycle
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.policy.target_authority import (
    GIT_PUSH_MECHANISM,
    ReleaseContext,
    ReleaseDecision,
    evaluate_release,
)
from punto.providers.contract import ProviderRole
from punto.providers.secrets import redact_secret_text
from punto.publish.production import (
    GitPublisher,
    ProductionProbe,
    PublicationRecord,
    PublicationService,
    PublicationStage,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.build import BuildRequest
from punto.schemas.decision import ActionRequest
from punto.schemas.dev import DevelopmentResult
from punto.schemas.enums import ApprovalStatus, AuditResult, RiskLevel, TaskStatus
from punto.workspace.target import DevelopmentTarget, DevelopmentTargetError

__all__ = [
    "CONSOLE_HTML",
    "CONSOLE_PATH",
    "PRODUCTION_ACTION",
    "PUSH_AUTHORIZATION_ENV",
    "ConsoleDependencies",
    "ConsoleStage",
    "ConsoleTask",
    "register_human_console",
]

#: Ruta de la consola humana: la misma página del dashboard, ya convertida en consola.
CONSOLE_PATH: Final[str] = "/console"

#: Página servida por la consola: es la del dashboard, sin duplicar frontend.
CONSOLE_HTML: Final[Path] = Path(__file__).resolve().parent / "static" / "dashboard.html"

#: Acción del catálogo que representa publicar en producción. Está en nivel 3 y en
#: ``never_autonomous``: es la razón por la que esta cadena **exige** una persona.
PRODUCTION_ACTION: Final[str] = "deploy_production"

#: Variable de entorno con la que el operador autoriza el push a un remoto **no local**.
PUSH_AUTHORIZATION_ENV: Final[str] = "PUNTO_PRODUCTION_PUSH"

#: Tope de filas de verificación que se muestran a una persona.
MAX_VERIFICATION_ROWS: Final[int] = 20

#: Tope de piezas de evidencia (problemas, condiciones y recursos) que se muestran en un gate.
MAX_EVIDENCE_ROWS: Final[int] = 12

#: Tope del motivo del gate: el detalle real, acotado y ya redactado.
MAX_REASON_CHARS: Final[int] = 400

#: Motivos de parada del ciclo que son, de verdad, una petición de autoridad humana.
HUMAN_REQUIRED_KINDS: Final[frozenset[str]] = frozenset(
    {
        "HUMAN_GATE_REQUIRED",
        "PLAN_REQUIRES_HUMAN",
        "CHANGE_REQUIRES_HUMAN",
        "PLAN_OUTSIDE_AUTHORITY",
        "CHANGE_OUTSIDE_AUTHORITY",
    }
)

#: Etapas que cierran la tarea: su transición es la marca real de finalización.
TERMINAL_STAGES: Final[frozenset[str]] = frozenset(
    {
        "DEVELOPMENT_COMPLETED",
        "DEVELOPMENT_FAILED",
        "REJECTED",
        "PUBLICATION_FAILED",
        "DEPLOYMENT_NOT_VERIFIED",
        "PRODUCTION_VALIDATED",
    }
)


class ConsoleStage(StrEnum):
    """Etapa visible de una tarea de la consola (desarrollo; la publicación usa sus etapas)."""

    QUEUED = "QUEUED"
    DEVELOPING = "DEVELOPING"
    DEVELOPMENT_COMPLETED = "DEVELOPMENT_COMPLETED"
    DEVELOPMENT_FAILED = "DEVELOPMENT_FAILED"
    WAITING_HUMAN = "WAITING_HUMAN"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    REJECTED = "REJECTED"


class TaskCreateBody(BaseModel):
    """Cuerpo de ``POST /console/tasks``: la solicitud escrita por una persona."""

    model_config = {"extra": "forbid"}

    objective: str = Field(min_length=3, max_length=2_000)
    target_id: str = Field(min_length=1, max_length=80)
    acceptance_criteria: tuple[str, ...] = ()
    scope_paths: tuple[str, ...] = ("src",)
    context: str = Field(default="", max_length=2_000)
    run: bool = True


class GateDecisionBody(BaseModel):
    """Cuerpo de la resolución de un gate: quién decide y con qué nota."""

    model_config = {"extra": "forbid"}

    resolved_by: str = Field(default="humano-local", min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)


class ConsoleTask:
    """Tarea de la consola: índice sobre objetos reales del motor, no un duplicado de su estado."""

    def __init__(
        self,
        *,
        task_id: UUID,
        objective: str,
        target_id: str,
        acceptance_criteria: tuple[str, ...],
        scope_paths: tuple[str, ...],
        context: str,
    ) -> None:
        #: La identidad de la tarea **es** la de la solicitud gobernada: una sola
        #: traza de auditoría.
        self.task_id = task_id
        self.objective = objective
        self.target_id = target_id
        self.acceptance_criteria = acceptance_criteria
        self.scope_paths = scope_paths
        self.context = context
        self.stage: str = ConsoleStage.QUEUED.value
        self.created_at: datetime = utc_now()
        self.updated_at: datetime = self.created_at
        #: Momento real en que la tarea alcanzó una etapa terminal (``None`` mientras sigue viva).
        self.finished_at: datetime | None = None
        self.result: DevelopmentResult | None = None
        self.publication: PublicationRecord | None = None
        #: Decisión de autoridad de release evaluada (AP000-R01): AUTO, HUMAN_GATE o DENIED.
        self.release: ReleaseDecision | None = None
        #: Caché de la comprobación real de que el commit del ciclo existe en el repositorio.
        self.commit_presence: dict[str, bool] = {}
        self.gates: list[UUID] = []
        self.runs = 0
        self.notes: list[str] = []
        self._lock = Lock()

    @property
    def request_id(self) -> UUID:
        """Solicitud gobernada asociada (la misma identidad que la tarea)."""
        return self.task_id

    def set_stage(self, stage: ConsoleStage | PublicationStage, detail: str = "") -> None:
        """Cambia la etapa visible, dejando constancia del motivo si lo hay.

        La marca de finalización es la hora real de la transición terminal: si la tarea vuelve a
        avanzar (por ejemplo, el desarrollo completado pasa a publicarse), deja de estar finalizada.
        """
        with self._lock:
            self.stage = stage.value
            self.updated_at = utc_now()
            self.finished_at = self.updated_at if stage.value in TERMINAL_STAGES else None
            if detail:
                self.notes = [*self.notes[-4:], detail[:300]]

    def summary(self) -> dict[str, Any]:
        """Resumen del resultado de desarrollo, sin contenido de ficheros.

        Incluye la evidencia **real** de la decisión gobernada (problemas con su código y detalle,
        decisiones de autoridad con sus reglas y razones, y el alcance del plan). Es lo que permite
        que un Human Gate diga *qué* se autoriza y *por qué* PUNTO pide una persona: antes de esto
        el resultado llegaba sin nada de esa evidencia y el gate solo podía ser genérico.
        """
        result = self.result
        if result is None:
            return {}
        return {
            "status": result.status.value,
            "error_kind": _redacted(result.error_kind, 60),
            "error": _redacted(result.error, 300),
            "commit_sha": result.commit_sha,
            "branch": result.branch,
            "applied": [item.path for item in result.applied],
            "repair_rounds": result.repair_rounds,
            "structural_corrections": result.structural_corrections,
            "functional_chain_result": result.functional_chain_result,
            "verification": [
                {"name": item.name, "passed": item.passed, "exit_code": item.exit_code}
                for item in result.verification[:MAX_VERIFICATION_ROWS]
            ],
            "verification_failures": sum(1 for item in result.verification if not item.passed),
            "scope_expansions": len(result.scope_expansions),
            "authority_decisions": len(result.authority_decisions),
            "published": result.published,
            "duration_ms": result.duration_ms,
            "pell_status": result.pell_status,
            "plan_summary": _redacted(result.plan.summary if result.plan is not None else ""),
            "plan_issues": [
                {"code": item.code, "detail": _redacted(item.detail)}
                for item in result.plan_issues[:MAX_EVIDENCE_ROWS]
            ],
            "change_issues": [
                {"code": item.code, "detail": _redacted(item.detail)}
                for item in result.change_issues[:MAX_EVIDENCE_ROWS]
            ],
            "authority_evidence": [
                _decision_view(item) for item in _gate_decisions(result)[:MAX_EVIDENCE_ROWS]
            ],
            "scope_paths": list(_result_scope(result))[:MAX_VERIFICATION_ROWS],
            # AP000-OBS-02: evidencia de aceptación medida contra la superficie solicitada.
            "acceptance_result": result.acceptance_result,
            "acceptance": [item.model_dump() for item in result.acceptance[:MAX_EVIDENCE_ROWS]],
        }

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable para el dashboard."""
        return {
            "task_id": str(self.task_id),
            "request_id": str(self.request_id),
            "objective": self.objective,
            "target_id": self.target_id,
            "acceptance_criteria": list(self.acceptance_criteria),
            "scope_paths": list(self.scope_paths),
            "stage": self.stage,
            "publication_stage": (
                self.publication.stage.value if self.publication is not None else ""
            ),
            "runs": self.runs,
            "gates": [str(item) for item in self.gates],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at is not None else "",
            "development": self.summary(),
            "publication": self.publication.as_dict() if self.publication else None,
            "notes": list(self.notes),
        }


@dataclass
class ConsoleDependencies:
    """Piezas inyectables de la consola (pruebas y composición explícita)."""

    dev_cycle: DevelopmentCycle
    gates: HumanGate
    audit: AuditLogger
    policy: PolicyEngine
    targets: Mapping[str, DevelopmentTarget]
    publisher_factory: Callable[[DevelopmentTarget], PublicationService] | None = None
    run_inline: bool = False
    #: Entorno del que se lee el interlock de publicación (``PUNTO_PRODUCTION_PUSH``). Es una
    #: **lectura** de configuración del proceso, no el entorno de un comando hijo: la frontera de
    #: ejecución la construye ``build_sanitized_environment`` cuando de verdad se lanza un proceso.
    environ: Mapping[str, str] = field(default_factory=os.environ.copy)


def _publication_service(
    target: DevelopmentTarget, *, audit: AuditLogger, environ: Mapping[str, str]
) -> PublicationService:
    """Servicio de publicación del destino, con su interlock de push remoto."""
    allow_remote = str(environ.get(PUSH_AUTHORIZATION_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
    }
    return PublicationService(
        target_id=target.target_id,
        repository=target.repository,
        branch=target.production_branch,
        url=target.production_url,
        remote=target.publish_remote,
        marker=target.production_marker,
        publisher=GitPublisher(target.repository),
        probe=ProductionProbe(url=target.production_url, marker=target.production_marker),
        allow_remote_push=allow_remote,
        audit=audit,
    )


def register_human_console(
    application: FastAPI, dependencies: ConsoleDependencies | None = None
) -> None:
    """Registra la consola humana en la aplicación existente.

    Args:
        application: Aplicación FastAPI del motor (la misma del dashboard).
        dependencies: Piezas inyectables. Sin ellas se componen desde el motor real.
    """
    if getattr(application.state, "human_console", None) is not None:
        return
    if dependencies is None:
        dependencies = _composition_from_engine(application)
    application.state.human_console = dependencies

    tasks: dict[str, ConsoleTask] = {}
    executor = None if dependencies.run_inline else ThreadPoolExecutor(max_workers=2)

    # ------------------------------------------------------------------ página
    @application.get(CONSOLE_PATH, response_class=HTMLResponse, tags=["console"])
    def console_page() -> HTMLResponse:
        """Consola humana: tareas, Human Gates y publicación (misma página del dashboard)."""
        if not CONSOLE_HTML.is_file():
            return HTMLResponse("<!doctype html><p>consola no disponible</p>", status_code=500)
        return HTMLResponse(CONSOLE_HTML.read_text(encoding="utf-8"))

    @application.get("/console/targets", tags=["console"], summary="Destinos disponibles")
    def console_targets() -> dict[str, Any]:
        """Destinos declarados: dónde puede trabajar PUNTO y si son publicables.

        Se expone el **nombre humano** y la clave del destino, nunca la ruta del repositorio: el
        navegador elige una clave registrada y PUNTO resuelve a su repositorio declarado.
        """
        return {
            "targets": [
                {
                    "target_id": target.target_id,
                    "name": target.human_name,
                    "scope_roots": list(target.scope_roots),
                    "publishable": target.publishable,
                    "production_branch": target.production_branch,
                    "production_url": target.production_url,
                    "authority": target.authority.as_dict(),
                }
                for target in (dependencies.targets[key] for key in sorted(dependencies.targets))
            ]
        }

    # ------------------------------------------------------------------- tasks
    @application.post(
        "/console/tasks",
        tags=["console"],
        status_code=status.HTTP_201_CREATED,
        summary="Crear una tarea gobernada",
    )
    def create_console_task(body: TaskCreateBody) -> dict[str, Any]:
        """Crea la tarea y lanza el ciclo de desarrollo: una persona escribe, PUNTO ejecuta."""
        target = _target_or_400(dependencies, body.target_id)
        request = BuildRequest(
            objective=body.objective,
            target_repository=target.target_id,
            requested_role=ProviderRole.BUILDER,
            acceptance_criteria=tuple(body.acceptance_criteria),
            scope_paths=tuple(body.scope_paths),
            context=body.context,
        )
        task = ConsoleTask(
            task_id=request.request_id,
            objective=body.objective,
            target_id=target.target_id,
            acceptance_criteria=tuple(body.acceptance_criteria),
            scope_paths=tuple(body.scope_paths),
            context=body.context,
        )
        tasks[str(task.task_id)] = task
        dependencies.audit.log_dev_event(
            AuditEventType.CONSOLE_TASK_CREATED,
            "console_task_created",
            request_id=str(task.task_id),
            metadata={
                "target_id": target.target_id,
                "objective": body.objective[:200],
                "publishable": target.publishable,
                "authority": "la consola no concede autoridad: PUNTO gobierna",
            },
        )
        if body.run:
            _run_development(task, request, dependencies, executor)
        return _task_view(task, dependencies)

    @application.get("/console/tasks", tags=["console"], summary="Listar tareas")
    def list_console_tasks() -> dict[str, Any]:
        """Tareas de la consola, de la más nueva a la más antigua."""
        items = sorted(tasks.values(), key=lambda item: item.created_at, reverse=True)
        return {"total": len(items), "items": [_task_view(item, dependencies) for item in items]}

    @application.get("/console/tasks/{task_id}", tags=["console"], summary="Detalle de una tarea")
    def get_console_task(task_id: UUID) -> dict[str, Any]:
        """Detalle de una tarea: etapa, resultado, gates y publicación."""
        task = _task_or_404(tasks, task_id)
        return {
            **_task_view(task, dependencies),
            "gates_detail": [
                _gate_view(dependencies, approval_id, tasks) for approval_id in task.gates
            ],
        }

    @application.post(
        "/console/tasks/{task_id}/run",
        tags=["console"],
        summary="Reanudar el desarrollo de una tarea",
    )
    def run_console_task(task_id: UUID) -> dict[str, Any]:
        """Vuelve a ejecutar el ciclo sobre la misma solicitud (nunca si fue rechazada)."""
        task = _task_or_404(tasks, task_id)
        if task.stage == ConsoleStage.REJECTED.value:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="la tarea fue rechazada por una persona: no se reanuda",
            )
        target = _target_or_400(dependencies, task.target_id)
        request = BuildRequest(
            objective=task.objective,
            target_repository=target.target_id,
            requested_role=ProviderRole.BUILDER,
            acceptance_criteria=task.acceptance_criteria,
            scope_paths=task.scope_paths,
            context=task.context,
        )
        task.task_id = request.request_id
        tasks[str(task.task_id)] = task
        _run_development(task, request, dependencies, executor)
        return _task_view(task, dependencies)

    # -------------------------------------------------------------- human gates
    @application.get("/console/human-gates", tags=["console"], summary="Human Gates")
    def list_console_gates(pending_only: bool = False) -> dict[str, Any]:
        """Solicitudes de aprobación humana, con lo mínimo para decidir."""
        approvals = (
            dependencies.gates.list_pending() if pending_only else dependencies.gates.list_all()
        )
        return {
            "total": len(approvals),
            "pending": len(dependencies.gates.list_pending()),
            "items": [_gate_view(dependencies, approval.id, tasks) for approval in approvals],
        }

    @application.post(
        "/console/human-gates/{approval_id}/approve",
        tags=["console"],
        summary="Aprobar una solicitud humana",
    )
    def approve_console_gate(approval_id: UUID, body: GateDecisionBody) -> dict[str, Any]:
        """Aprueba el gate. Solo la aprobación de **publicación** desencadena la publicación."""
        approval = _gate_or_404(dependencies, approval_id)
        _resolve_gate(dependencies, approval_id, approved=True, body=body)
        task = tasks.get(str(approval.task_id))
        if _is_publication_gate(approval.action):
            if task is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail="la aprobación no corresponde a una tarea de esta consola",
                )
            target = _target_or_400(dependencies, task.target_id)
            _run_publication(task, target, dependencies, executor, approval_id)
            return _task_view(task, dependencies)
        if task is not None:
            task.set_stage(ConsoleStage.HUMAN_APPROVED, f"aprobado por {body.resolved_by}")
        return {
            "approval_id": str(approval_id),
            "status": ApprovalStatus.APPROVED.value,
            "task": _task_view(task, dependencies) if task is not None else None,
            "note": (
                "la aprobación autoriza esta operación; el ciclo sigue gobernado por PUNTO y no "
                "se publica nada en producción desde aquí"
            ),
        }

    @application.post(
        "/console/human-gates/{approval_id}/reject",
        tags=["console"],
        summary="Rechazar una solicitud humana",
    )
    def reject_console_gate(approval_id: UUID, body: GateDecisionBody) -> dict[str, Any]:
        """Rechaza el gate: la operación queda impedida y no se ejecuta nada."""
        approval = _gate_or_404(dependencies, approval_id)
        _resolve_gate(dependencies, approval_id, approved=False, body=body)
        task = tasks.get(str(approval.task_id))
        if task is not None:
            if _is_publication_gate(approval.action):
                task.publication = None
            task.set_stage(ConsoleStage.REJECTED, f"rechazado por {body.resolved_by}")
        return {
            "approval_id": str(approval_id),
            "status": ApprovalStatus.REJECTED.value,
            "task": _task_view(task, dependencies) if task is not None else None,
        }

    # --------------------------------------------------------------- producción
    @application.post(
        "/console/tasks/{task_id}/production-gate",
        tags=["console"],
        summary="Pedir el Human Gate de publicación",
    )
    def request_production_gate(task_id: UUID) -> dict[str, Any]:
        """Crea el Human Gate de publicación de una tarea ya desarrollada y verificada.

        La acción es ``deploy_production`` (nivel 3, ``never_autonomous``): la decisión de política
        es real y el gate queda ligado a ella por su identificador.
        """
        task = _task_or_404(tasks, task_id)
        target = _target_or_400(dependencies, task.target_id)
        result = task.result
        if result is None or result.status.value != "DEVELOPMENT_COMPLETED":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="la tarea no tiene un desarrollo completado y verificado que publicar",
            )
        if not result.commit_sha:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="la tarea no tiene commit local: no hay nada que publicar",
            )
        if not target.publishable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=(
                    "el destino no declara rama y URL de producción (production_branch, "
                    "production_url): PUNTO no adivina dónde vive producción"
                ),
            )
        decision = dependencies.policy.evaluate(
            ActionRequest(
                action=PRODUCTION_ACTION,
                technical=True,
                reversible=False,
                risk_level=RiskLevel.HIGH,
                production_impact=True,
                files_changed=[item.path for item in result.applied],
            )
        )
        dependencies.audit.log_policy_decision(decision)
        if not decision.requires_human:
            # Con el catálogo vigente no puede pasar; si pasara, publicar en
            # autonomía sería un fallo de la frontera: se falla cerrado.
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "la política no exigió persona para publicar en producción: "
                    "se falla cerrado"
                ),
            )
        approval = dependencies.gates.request(
            task_id=task.task_id,
            action=PRODUCTION_ACTION,
            risk=decision.effective_risk,
            reason=(
                f"publicar en producción de {target.target_id} el commit "
                f"{result.commit_sha[:12]} ya verificado en local"
            ),
            resume_status=TaskStatus.IN_PROGRESS,
            policy_outcome=decision.outcome.value,
            policy_decision_id=decision.id,
        )
        dependencies.audit.log_human_gate_created(
            approval_id=approval.id,
            task_id=task.task_id,
            action=approval.action,
            risk=approval.risk.name,
            reason=approval.reason,
        )
        if approval.id not in task.gates:
            task.gates.append(approval.id)
        task.publication = PublicationRecord(
            task_id=str(task.task_id),
            request_id=str(task.request_id),
            target_id=target.target_id,
            commit_sha=result.commit_sha,
            approval_id=str(approval.id),
        )
        task.publication.advance(
            PublicationStage.WAITING_PRODUCTION_APPROVAL,
            "gate de publicación pendiente de decisión humana",
        )
        task.set_stage(PublicationStage.WAITING_PRODUCTION_APPROVAL)
        return _task_view(task, dependencies)

    @application.post(
        "/console/tasks/{task_id}/publish",
        tags=["console"],
        summary="Publicar con el gate aprobado",
    )
    def publish_console_task(task_id: UUID) -> dict[str, Any]:
        """Publica el commit aprobado (si el gate está aprobado) y comprueba producción."""
        task = _task_or_404(tasks, task_id)
        target = _target_or_400(dependencies, task.target_id)
        publication = task.publication
        if publication is None or not publication.approval_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="no hay Human Gate de publicación: sin persona no se publica",
            )
        _run_publication(task, target, dependencies, executor, UUID(publication.approval_id))
        return _task_view(task, dependencies)

    @application.post(
        "/console/tasks/{task_id}/release",
        tags=["console"],
        summary="Ejecutar el release con la autoridad persistente del destino",
    )
    def release_console_task(task_id: UUID) -> dict[str, Any]:
        """Ejecuta la cadena de publicación si el sobre persistente del destino la autoriza.

        Es la ruta **autónoma** de AP000-R01: no concede autoridad —la lee de la configuración
        confiable del destino— y fracasa cerrado si la decisión no es ``AUTO``. Una desviación
        material (rama, destino, commit, verificación, QA, borrados, secretos, mecanismo o falta de
        dato) devuelve 409 con las condiciones que la bloquean, y la persona decide por el gate.
        """
        task = _task_or_404(tasks, task_id)
        target = _target_or_400(dependencies, task.target_id)
        decision = _evaluate_and_log(task, target, dependencies)
        if decision is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="la tarea no tiene resultado de desarrollo que liberar",
            )
        task.release = decision
        if not decision.autonomous:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "message": (
                        "la operación no está dentro de la autoridad persistente del destino o no "
                        "cumple sus condiciones: decide una persona en el Human Gate"
                    ),
                    "disposition": decision.disposition,
                    "reasons": list(decision.reasons),
                    "blockers": [item.name for item in decision.blockers],
                },
            )
        _run_publication(task, target, dependencies, executor, None, decision)
        return _task_view(task, dependencies)

    # ------------------------------------------------------------- ejecución
    def _run_development(
        task: ConsoleTask,
        request: BuildRequest,
        deps: ConsoleDependencies,
        pool: ThreadPoolExecutor | None,
    ) -> None:
        """Ejecuta el ciclo de desarrollo y refleja su resultado en la tarea."""
        task.set_stage(ConsoleStage.DEVELOPING)
        task.runs += 1

        def work() -> None:
            try:
                result = deps.dev_cycle.run(request)
            except Exception as exc:  # el ciclo no debe tumbar la consola
                task.set_stage(ConsoleStage.DEVELOPMENT_FAILED, f"el ciclo falló: {exc}")
                return
            task.result = result
            _reflect(task, result, deps)

        if pool is None:
            work()
        else:
            pool.submit(work)

    def _reflect(task: ConsoleTask, result: DevelopmentResult, deps: ConsoleDependencies) -> None:
        """Traduce el resultado del ciclo a la etapa visible y crea el gate si hace falta.

        AP000-R01: cuando el desarrollo termina bien se **evalúa** la autoridad persistente del
        destino. Si la operación está dentro de esa autoridad y todas las condiciones están
        demostradas, la publicación continúa sola (``AUTO``); si hay una desviación material o falta
        un dato, se queda con la decisión a la vista y el humano decide.
        """
        if result.status.value == "DEVELOPMENT_COMPLETED":
            task.set_stage(ConsoleStage.DEVELOPMENT_COMPLETED)
            target = deps.targets.get(task.target_id)
            release = _evaluate_and_log(task, target, deps)
            task.release = release
            if release is not None and release.autonomous and target is not None:
                _run_publication(task, target, deps, executor, None, release)
            return
        if result.error_kind in HUMAN_REQUIRED_KINDS or _issue_human_kind(result):
            human_kind = (
                result.error_kind
                if result.error_kind in HUMAN_REQUIRED_KINDS
                else _issue_human_kind(result)
            )
            decision = deps.policy.evaluate(
                ActionRequest(
                    action="modify_file",
                    technical=True,
                    reversible=True,
                    risk_level=RiskLevel.HIGH,
                    files_changed=list(result.final_scope),
                )
            )
            deps.audit.log_policy_decision(decision)
            approval = deps.gates.request(
                task_id=task.task_id,
                action=human_kind,
                risk=decision.effective_risk,
                reason=_gate_reason(human_kind, result, task.target_id),
                resume_status=TaskStatus.IN_PROGRESS,
                policy_outcome=decision.outcome.value,
                policy_decision_id=decision.id,
            )
            deps.audit.log_human_gate_created(
                approval_id=approval.id,
                task_id=task.task_id,
                action=approval.action,
                risk=approval.risk.name,
                reason=approval.reason,
            )
            if approval.id not in task.gates:
                task.gates.append(approval.id)
            task.set_stage(ConsoleStage.WAITING_HUMAN, human_kind)
            return
        task.set_stage(ConsoleStage.DEVELOPMENT_FAILED, result.error_kind or result.status.value)

    def _run_publication(
        task: ConsoleTask,
        target: DevelopmentTarget,
        deps: ConsoleDependencies,
        pool: ThreadPoolExecutor | None,
        approval_id: UUID | None,
        decision: ReleaseDecision | None = None,
    ) -> None:
        """Ejecuta la publicación gobernada del commit, por gate humano o por sobre del destino."""
        result = task.result
        if result is None or not result.commit_sha:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail="la tarea no tiene commit que publicar"
            )
        if task.publication is None:
            task.publication = PublicationRecord(
                task_id=str(task.task_id),
                request_id=str(task.request_id),
                target_id=target.target_id,
                commit_sha=result.commit_sha,
                approval_id="" if approval_id is None else str(approval_id),
            )
        service = (
            deps.publisher_factory(target)
            if deps.publisher_factory is not None
            else _publication_service(target, audit=deps.audit, environ=deps.environ)
        )
        task.set_stage(PublicationStage.PUBLISHING)

        def work() -> None:
            try:
                record = service.publish(
                    task_id=task.task_id,
                    request_id=str(task.request_id),
                    commit_sha=result.commit_sha,
                    approval_id=approval_id,
                    gate=deps.gates,
                    record=task.publication,
                    authority=decision,
                )
            except Exception as exc:  # la publicación nunca tumba la consola
                task.set_stage(PublicationStage.PUBLICATION_FAILED, f"publicación rechazada: {exc}")
                return
            task.publication = record
            task.set_stage(record.stage, record.error)

        if pool is None:
            work()
        else:
            pool.submit(work)

    # ------------------------------------------------------------- auxiliares
    def _gate_view(
        deps: ConsoleDependencies, approval_id: UUID, registry: Mapping[str, ConsoleTask]
    ) -> dict[str, Any]:
        """Vista de un gate: lo necesario para decidir **con la causa real delante**.

        Además del estado y el destino, se expone la evidencia gobernada del ciclo —qué operación
        quiere hacer PUNTO, qué recursos toca y qué condición concreta disparó el REQUIRE_HUMAN— y
        el alcance de la autorización: qué se autoriza al aprobar y qué no. Nada de eso se inventa
        aquí: sale del resultado real y de la decisión de autoridad que ya produjo el ciclo.
        """
        approval = deps.gates.get(approval_id)
        if approval is None:
            return {"approval_id": str(approval_id), "status": "NOT_FOUND"}
        task = registry.get(str(approval.task_id))
        target = deps.targets.get(task.target_id) if task is not None else None
        publication = _is_publication_gate(approval.action)
        return {
            "approval_id": str(approval.id),
            "kind": "publication" if publication else "development",
            "status": approval.status.value,
            "is_pending": approval.is_pending,
            "task_id": str(approval.task_id),
            "objective": task.objective[:160] if task is not None else "",
            "target_id": task.target_id if task is not None else "",
            "action": approval.action,
            "risk": approval.risk.name,
            "reason": approval.reason[:300],
            "policy_outcome": approval.policy_outcome or "",
            "policy_decision_id": (
                str(approval.policy_decision_id) if approval.policy_decision_id else ""
            ),
            "destination": {
                "kind": "production" if publication else "development",
                "target_id": target.target_id if target is not None else "",
                "repository": target.repository.name if target is not None else "",
                "production_branch": target.production_branch if target is not None else "",
                "production_url": target.production_url if target is not None else "",
                "commit_sha": (
                    task.result.commit_sha if task is not None and task.result is not None else ""
                ),
            },
            "verification": task.summary().get("verification", []) if task is not None else [],
            "evidence": _gate_evidence(approval, task, target, publication=publication),
            "requested_at": approval.requested_at.isoformat(),
            "resolved_at": approval.resolved_at.isoformat() if approval.resolved_at else "",
            "resolved_by": approval.resolved_by or "",
            "resolution_note": approval.resolution_note or "",
        }

    def _resolve_gate(
        deps: ConsoleDependencies,
        approval_id: UUID,
        *,
        approved: bool,
        body: GateDecisionBody,
    ) -> None:
        """Resuelve el gate con el ``HumanGate`` del motor y lo deja auditado."""
        approval = _gate_or_404(deps, approval_id)
        try:
            resolved = (
                deps.gates.approve(approval_id, resolved_by=body.resolved_by, note=body.note)
                if approved
                else deps.gates.reject(approval_id, resolved_by=body.resolved_by, note=body.note)
            )
        except Exception as exc:  # doble resolución o gate inexistente
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        deps.audit.log_human_gate_resolved(
            approval_id=resolved.id,
            task_id=approval.task_id,
            status=resolved.status.value,
            resolved_by=resolved.resolved_by or body.resolved_by,
        )


# --------------------------------------------------------------------- auxiliares
def _task_or_404(tasks: Mapping[str, ConsoleTask], task_id: UUID) -> ConsoleTask:
    """Tarea de la consola o 404."""
    task = tasks.get(str(task_id))
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"tarea no encontrada: {task_id}")
    return task


def _gate_or_404(dependencies: ConsoleDependencies, approval_id: UUID) -> Any:
    """Gate existente o 404."""
    approval = dependencies.gates.get(approval_id)
    if approval is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Human Gate no encontrado: {approval_id}"
        )
    return approval


def _is_publication_gate(action: str) -> bool:
    """True si el gate autoriza publicar en producción."""
    return action == PRODUCTION_ACTION


def _task_view(task: ConsoleTask, dependencies: ConsoleDependencies) -> dict[str, Any]:
    """Vista de la tarea con su recorrido humano y su decisión de release."""
    target = dependencies.targets.get(task.target_id)
    return {
        **task.as_dict(),
        "progress": _progress(task, dependencies),
        "release": (
            task.release.as_dict()
            if task.release is not None
            else (_release_preview(task, target, dependencies))
        ),
    }


def _release_preview(
    task: ConsoleTask,
    target: DevelopmentTarget | None,
    dependencies: ConsoleDependencies,
) -> dict[str, Any] | None:
    """Decisión de release vigente para que la interfaz refleje lo que decidiría el motor.

    Es la **misma** función de evaluación del motor sobre las mismas señales reales; no ejecuta
    nada ni deja auditoría (la auditoría la escribe la evaluación que sí decide).
    """
    if task.result is None:
        return None
    return _release_decision(task, target, dependencies).as_dict()


def _progress(task: ConsoleTask, dependencies: ConsoleDependencies) -> dict[str, Any]:
    """Proyecta la tarea en el recorrido humano: etapas reales, porcentaje y tiempo real.

    Todas las señales que se pasan ya existen: la etapa de la consola, el resultado real del ciclo,
    la etapa real de publicación, el estado real del ``HumanGate`` y los eventos de auditoría de la
    tarea (la misma traza que ``/audit/events?resource_id=<task_id>``).
    """
    target = dependencies.targets.get(task.target_id)
    result = task.result
    publication = task.publication
    return build_progress(
        TaskSignals(
            created_at=task.created_at,
            now=utc_now(),
            task_stage=task.stage,
            publication_stage=publication.stage.value if publication is not None else "",
            development_status=result.status.value if result is not None else "",
            development_error_kind=result.error_kind if result is not None else "",
            functional_chain_result=result.functional_chain_result if result is not None else "",
            applied_changes=len(result.applied) if result is not None else 0,
            events=tuple(
                (event.event_type.value, event.result is AuditResult.SUCCESS)
                for event in dependencies.audit.by_resource(task.task_id)
            ),
            publishable=target.publishable if target is not None else False,
            publication_gate_status=_publication_gate_status(publication, dependencies),
            finished_at=task.finished_at,
        )
    )


def _publication_gate_status(
    publication: PublicationRecord | None, dependencies: ConsoleDependencies
) -> str:
    """Estado real del Human Gate de publicación de la tarea (vacío si no hay gate)."""
    if publication is None or not publication.approval_id:
        return ""
    approval = dependencies.gates.get(UUID(publication.approval_id))
    return approval.status.value if approval is not None else ""


def _commit_present(task: ConsoleTask, target: DevelopmentTarget | None, sha: str) -> bool | None:
    """True si el commit está en el repositorio del destino, con el runner saneado de publicación.

    ``None`` significa «no se pudo comprobar»: la condición quedará ``UNKNOWN`` y la decisión
    fallará cerrado en vez de suponer que el commit existe.
    """
    if not sha or target is None:
        return None
    cached = task.commit_presence.get(sha)
    if cached is not None:
        return cached
    try:
        present = GitPublisher(target.repository).has_commit(sha)
    except Exception:  # git ausente o repositorio ilegible: no se puede demostrar
        return None
    task.commit_presence[sha] = present
    return present


# ----------------------------------------------------- evidencia del Human Gate
def _redacted(text: object, limit: int = 300) -> str:
    """Texto acotado y **redactado** con el mecanismo del motor, listo para la interfaz.

    Es la única puerta por la que un texto que viene del ciclo (o del proveedor) llega a la
    persona: no se muestra ninguna credencial ni contenido de ficheros.
    """
    return redact_secret_text(str(text))[:limit]


def _result_scope(result: DevelopmentResult) -> tuple[str, ...]:
    """Recursos (rutas relativas) del alcance real del resultado: el final, o el inicial."""
    return tuple(result.final_scope or result.initial_scope)


def _gate_decisions(result: DevelopmentResult) -> tuple[Any, ...]:
    """Decisiones de autoridad que explican la parada: las que no fueron autónomas, o la última."""
    blocking = tuple(
        item
        for item in result.authority_decisions
        if item.outcome not in {"ALLOW", "ALLOW_WITH_REVIEW"}
    )
    if blocking:
        return blocking
    return tuple(result.authority_decisions[-1:]) if result.authority_decisions else ()


def _decision_view(decision: Any) -> dict[str, Any]:
    """Vista de una decisión de autoridad: sus reglas, sus razones y la evidencia que exige."""
    return {
        "operation": _redacted(decision.operation, 60),
        "outcome": _redacted(decision.outcome, 40),
        "authority_class": _redacted(decision.authority_class, 40),
        "risk": _redacted(decision.risk, 20),
        "rules": [_redacted(item, 120) for item in decision.rules[:MAX_EVIDENCE_ROWS]],
        "reasons": [_redacted(item, 200) for item in decision.reasons[:MAX_EVIDENCE_ROWS]],
        "resources": [_redacted(item, 200) for item in decision.resources[:MAX_EVIDENCE_ROWS]],
        "required_evidence": [
            _redacted(item, 200) for item in decision.required_evidence[:MAX_EVIDENCE_ROWS]
        ],
    }


def _gate_reason(kind: str, result: DevelopmentResult, target_id: str) -> str:
    """Motivo del Human Gate con la **causa real** que PUNTO ya calculó.

    Un gate que solo dice «hace falta una persona» no es accionable: el motivo lleva el código del
    problema y el detalle que produjo el ciclo (reglas, clase de autoridad y riesgo incluidos), ya
    acotado y redactado. Si el ciclo no dejó detalle, se cae al texto genérico en vez de inventarlo.
    """
    cause = _human_cause(result)
    if cause["code"] and cause["detail"]:
        return f"{kind}: {cause['detail']}"[:MAX_REASON_CHARS]
    if cause["code"]:
        return f"{kind}: el ciclo se detuvo con {cause['code']} en {target_id}"[:MAX_REASON_CHARS]
    return (
        f"el ciclo se detuvo en {kind}: hace falta una persona antes de seguir con {target_id}"
    )[:MAX_REASON_CHARS]


def _human_cause(result: DevelopmentResult | None) -> dict[str, str]:
    """Causa real de la parada: el primer problema que PUNTO encontró, con su código y su detalle.

    El detalle lo compone el ciclo (``el plan toca N recurso(s) y el sobre de autoridad devuelve
    REQUIRE_HUMAN (HUMAN_GATE_REQUIRED, riesgo HIGH): recurso de clase desconocida: se falla
    cerrado``), no la interfaz: aquí solo se propaga.
    """
    if result is None:
        return {"code": "", "detail": ""}
    for issue in (*result.plan_issues, *result.change_issues):
        return {"code": _redacted(issue.code, 60), "detail": _redacted(issue.detail, 300)}
    return {"code": _redacted(result.error_kind, 60), "detail": _redacted(result.error, 300)}


def _planned_changes(result: DevelopmentResult | None) -> dict[str, list[str]]:
    """Lo que el plan declaraba hacer, recurso a recurso (nada se ha aplicado todavía)."""
    plan = result.plan if result is not None else None
    if plan is None:
        return {"modify": [], "create": [], "delete": []}
    return {
        "modify": [_redacted(item, 200) for item in plan.files_to_modify[:MAX_EVIDENCE_ROWS]],
        "create": [_redacted(item, 200) for item in plan.files_to_create[:MAX_EVIDENCE_ROWS]],
        "delete": [_redacted(item, 200) for item in plan.files_to_delete[:MAX_EVIDENCE_ROWS]],
    }


def _release_decision(
    task: ConsoleTask,
    target: DevelopmentTarget | None,
    deps: ConsoleDependencies,
    *,
    audit_policy: bool = False,
) -> ReleaseDecision:
    """Decisión de release con las señales reales de la tarea y del destino (AP000-R01).

    La política se evalúa con el ``PolicyEngine`` real sobre ``deploy_production``; la autoridad
    persistente del destino y las condiciones verificables deciden si esa operación continúa sola.
    """
    result = task.result
    commit_sha = result.commit_sha if result is not None else ""
    policy_decision = deps.policy.evaluate(
        ActionRequest(
            action=PRODUCTION_ACTION,
            technical=True,
            reversible=False,
            risk_level=RiskLevel.HIGH,
            production_impact=True,
            files_changed=[item.path for item in result.applied] if result is not None else [],
        )
    )
    if audit_policy:
        deps.audit.log_policy_decision(policy_decision)
    return evaluate_release(
        ReleaseContext(
            task_id=str(task.task_id),
            target=target,
            policy_decision=policy_decision,
            result=result,
            commit_sha=commit_sha,
            branch=result.branch if result is not None else "",
            repository=target.repository if target is not None else None,
            destination_branch=target.production_branch if target is not None else "",
            destination_url=target.production_url if target is not None else "",
            destination_remote=target.publish_remote if target is not None else "",
            mechanism=GIT_PUSH_MECHANISM,
            commit_present=_commit_present(task, target, commit_sha),
        )
    )


def _evaluate_and_log(
    task: ConsoleTask,
    target: DevelopmentTarget | None,
    deps: ConsoleDependencies,
) -> ReleaseDecision | None:
    """Evalúa la autoridad de release y deja la decisión auditada con todas sus condiciones."""
    if task.result is None:
        return None
    decision = _release_decision(task, target, deps, audit_policy=True)
    deps.audit.log_dev_event(
        AuditEventType.RELEASE_AUTHORITY_EVALUATED,
        "release_authority_evaluated",
        request_id=str(task.task_id),
        metadata={
            "target_id": task.target_id,
            "disposition": decision.disposition,
            "operation": decision.operation,
            "policy_outcome": decision.policy_outcome,
            "risk": decision.risk,
            "authorized_operations": list(decision.authorized_operations),
            "conditions": [
                {"name": item.name, "state": item.state} for item in decision.conditions
            ],
            "reasons": list(decision.reasons)[:6],
        },
    )
    return decision


def _gate_evidence(
    approval: Any,
    task: ConsoleTask | None,
    target: DevelopmentTarget | None,
    *,
    publication: bool,
) -> dict[str, Any]:
    """Evidencia mínima y gobernada para que una persona sepa qué autoriza y por qué.

    Responde, con datos reales del ciclo: qué operación quiere hacer PUNTO, qué recursos afecta, qué
    condición concreta disparó el REQUIRE_HUMAN/HIGH, qué autoriza aprobar y qué no autoriza.
    """
    result = task.result if task is not None else None
    destination = target.human_name if target is not None else ""
    decisions = _gate_decisions(result)[:3] if result is not None else ()
    scope = list(_result_scope(result)) if result is not None else []
    return {
        "risk": approval.risk.name,
        "policy_outcome": approval.policy_outcome or "",
        "action": approval.action,
        "operation": {
            "name": _redacted(decisions[0].operation, 40) if decisions else "",
            "summary": (
                _redacted(result.plan.summary, 300)
                if result is not None and result.plan is not None
                else ""
            ),
            "planned": _planned_changes(result),
        },
        "resources": {
            "paths": [_redacted(item, 200) for item in scope[:MAX_EVIDENCE_ROWS]],
            "total": len(scope),
        },
        "cause": _human_cause(result),
        "conditions": [_decision_view(item) for item in decisions],
        "authorizes": (
            f"Publicar en producción el commit aprobado de {destination or 'este destino'} y "
            "comprobar que sirve lo esperado."
            if publication
            else (
                f"Que PUNTO continúe **esta** operación en "
                f"{destination or 'el destino autorizado'}: el plan declarado se aplicará en el "
                "alcance permitido y se verificará con el catálogo del destino. Nada más."
            )
        ),
        "does_not_authorize": (
            [
                "Nada más que ese commit en la rama de producción declarada.",
                "Autoridad nueva: la aprobación no amplía permisos, alcance ni reglas.",
                "Publicar en otro destino ni por otra vía.",
            ]
            if publication
            else [
                "Publicar en producción: eso exige su propio Human Gate de publicación.",
                "Autoridad nueva: la aprobación no amplía permisos, alcance ni reglas de PUNTO.",
                "Operar sobre otro destino: el gate está ligado a esta tarea y a este destino.",
                "Saltar la verificación: el ciclo sigue verificando y puede fallar igualmente.",
            ]
        ),
    }


def _issue_human_kind(result: DevelopmentResult) -> str:
    """Primer código del ciclo que pide autoridad humana, si lo hay.

    El ciclo puede parar con un ``error_kind`` genérico (``CHANGE_REJECTED``) llevando el motivo
    real en sus incidencias: mirar solo el ``error_kind`` perdería la petición de persona.
    """
    for issue in (*result.change_issues, *result.plan_issues):
        if issue.code in HUMAN_REQUIRED_KINDS:
            return issue.code
    return ""


def _target_or_400(dependencies: ConsoleDependencies, target_id: str) -> DevelopmentTarget:
    """Destino registrado o 400."""
    target = dependencies.targets.get(target_id)
    if target is None:
        known = ", ".join(sorted(dependencies.targets)) or "ninguno"
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"destino {target_id!r} no registrado (registrados: {known})",
        )
    return target


def _composition_from_engine(application: FastAPI) -> ConsoleDependencies:
    """Compone la consola desde el motor real de la aplicación.

    La auditoría, los gates y la política son los del motor; el ciclo de desarrollo
    se compone con su fábrica de siempre, y los destinos se leen de la configuración
    vigente.
    """
    from punto.api.app import get_engine
    from punto.orchestrator.dev_cycle import default_development_cycle
    from punto.workspace.target import DevelopmentTargetRegistry

    engine = get_engine(application)
    try:
        registry = DevelopmentTargetRegistry.from_environment()
    except DevelopmentTargetError:
        registry = DevelopmentTargetRegistry({})
    cycle = default_development_cycle(audit=engine.audit)
    return ConsoleDependencies(
        dev_cycle=cycle,
        gates=engine.human_gate,
        audit=engine.audit,
        policy=engine.policy_engine,
        targets=dict(registry.targets),
    )
