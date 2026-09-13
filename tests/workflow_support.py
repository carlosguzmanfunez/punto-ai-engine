"""Soportes de prueba del kernel de workflow (ENGINE-6.0).

Dos piezas:

1. constructores deterministas de ``WorkflowRequest`` y ``WorkflowRun``, para no repetir campos en
   cada prueba;
2. un ejecutor de rol **falso** que satisface el puerto ``RoleExecutor`` sin proveedor ni red, con
   registro de llamadas y guiones (fallar, bloquear, pedir cambios, no aplicar, lanzar un error
   técnico).

Nada de este módulo se usa en producción.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from punto.common import utc_now
from punto.policy.human_gate import HumanApprovalProof, HumanGate
from punto.policy.policy_engine import PolicyConfigBundle, PolicyEngine
from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.execution import ModelUsage
from punto.schemas.workflow import (
    ProviderCapability,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowFinding,
    WorkflowRequest,
    WorkflowRun,
)
from punto.workflow.policy import WorkflowPolicy


def make_finding(
    role: RoleName = RoleName.QA,
    *,
    severity: FindingSeverity = FindingSeverity.MEDIUM,
    category: str = "CORRECTNESS",
    message: str = "detalle sintético",
    evidence: str = "evidencia sintética",
) -> WorkflowFinding:
    """Hallazgo normalizado con la gravedad indicada."""
    return WorkflowFinding(
        role=role,
        severity=severity,
        category=category,
        message=message,
        evidence=evidence,
    )


def make_request(
    *,
    task_id: UUID | None = None,
    project_id: UUID | None = None,
    idempotency_key: str = "wf-test",
    action: str = "create_file",
    risk: RiskLevel = RiskLevel.LOW,
    authority: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
    web_visual_required: bool = False,
    cross_audit_required: bool = True,
    **overrides: Any,
) -> WorkflowRequest:
    """Petición de workflow lista para el kernel.

    La acción por defecto es autónoma y reversible (``create_file``): si una prueba quiere un
    Human Gate, declara una acción L3 real o sube el riesgo declarado.
    """
    base: dict[str, Any] = {
        "task_id": task_id or uuid4(),
        "project_id": project_id or uuid4(),
        "objective": "implementar una función pura de normalización",
        "action": action,
        "acceptance_criteria": ("normaliza etiquetas",),
        "workspace_path": ".",
        "changed_files": ("runner.py",),
        "context_summary": "proyecto sintético de prueba",
        "web_visual_required": web_visual_required,
        "cross_audit_required": cross_audit_required,
        "risk": risk,
        "authority": authority,
        "idempotency_key": idempotency_key,
    }
    base.update(overrides)
    return WorkflowRequest(**base)


def make_policy(engine: PolicyEngine, gate: HumanGate) -> WorkflowPolicy:
    """Frontera de política real (Policy Engine + Human Gate del repositorio)."""
    return WorkflowPolicy(engine=engine, gate=gate)


@lru_cache(maxsize=1)
def _config_bundle() -> PolicyConfigBundle:
    """Configuración de política del repositorio, leída una sola vez por proceso de prueba."""
    from punto.policy.config_loader import ConfigLoader

    loader = ConfigLoader(Path(__file__).resolve().parents[1] / "config")
    return PolicyConfigBundle.from_loader(loader)


def offline_policy() -> WorkflowPolicy:
    """Frontera de política **real** para las pruebas offline.

    El kernel exige una política (hallazgo V602-02): no existe el camino «sin política». Las pruebas
    usan el Policy Engine real leído de ``config/`` con un Human Gate nuevo en cada llamada, de modo
    que nunca comparten estado de aprobaciones entre casos.
    """
    return WorkflowPolicy(engine=PolicyEngine.from_bundle(_config_bundle()), gate=HumanGate())


def approve_human_gate(
    human_gate: HumanGate, run: WorkflowRun, *, resolved_by: str = "humano-de-prueba"
) -> HumanApprovalProof:
    """Aprueba el Human Gate del workflow y emite la autorización de reanudación.

    Es el único camino real: la prueba la emite ``HumanGate.authorize_resume``, nunca el kernel ni
    la prueba a mano.
    """
    if run.human_gate is None or run.human_gate.approval_id is None:
        raise AssertionError("el workflow no tiene un Human Gate con solicitud registrada")
    human_gate.approve(run.human_gate.approval_id, resolved_by=resolved_by)
    return human_gate.authorize_resume(run.human_gate.approval_id, task_id=run.task_id)



def make_run(
    request: WorkflowRequest | None = None,
    *,
    status: TaskStatus = TaskStatus.NEW,
    started_seconds_ago: float = 0.0,
) -> WorkflowRun:
    """Workflow en el estado indicado, opcionalmente con el reloj ya avanzado."""
    resolved = request or make_request()
    run = WorkflowRun(workflow_id=uuid4(), request=resolved, status=status)
    run = run.model_copy(update={"usage": run.usage.with_visit(status)})
    if started_seconds_ago:
        run = run.model_copy(
            update={"started_at": utc_now() - timedelta(seconds=started_seconds_ago)}
        )
    return run


class FakeRoleExecutor:
    """Ejecutor de rol falso: registra llamadas y sigue el guion que se le dé.

    Satisface el puerto ``RoleExecutor`` sin proveedor, sin red y sin reloj propio.
    """

    def __init__(
        self,
        role: RoleName,
        *,
        status: RoleStatus = RoleStatus.COMPLETED,
        findings: tuple[WorkflowFinding, ...] = (),
        summary: str = "rol completado",
        error_code: Any = None,
        error_detail: str = "",
        raise_error: Exception | None = None,
        raise_times: int = 0,
        provider: str = "fake",
        model: str = "fake-model",
        tokens: int = 7,
        model_calls: int = 1,
        capability_info: ProviderCapability | None = None,
    ) -> None:
        self.role = role
        self.status = status
        self.findings = findings
        self.summary = summary
        self.error_code = error_code
        self.error_detail = error_detail
        self.raise_error = raise_error
        self.raise_times = raise_times
        self.provider = provider
        self.model = model
        self.tokens = tokens
        self.model_calls = model_calls
        self.capability_info = capability_info
        self.calls: list[RoleExecutionRequest] = []

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Devuelve el resultado del guion, o lanza el error técnico configurado."""
        self.calls.append(request)
        if self.raise_error is not None and len(self.calls) <= self.raise_times:
            raise self.raise_error
        started = utc_now()
        return RoleExecutionResult(
            role=self.role,
            status=self.status,
            summary=self.summary,
            findings=self.findings,
            provider=self.provider,
            model=self.model,
            attempts=len(self.calls),
            started_at=started,
            completed_at=started,
            model_calls=self.model_calls,
            usage=ModelUsage(
                prompt_tokens=self.tokens,
                completion_tokens=1,
                total_tokens=self.tokens + 1,
            ),
            error_code=self.error_code,
            error_detail=self.error_detail,
        )

    def capability(self, role: RoleName) -> ProviderCapability | None:
        """Capacidad declarada del ejecutor falso, si la hay."""
        return self.capability_info if role is self.role else None


def executor_map(
    *roles: RoleName,
    status: RoleStatus = RoleStatus.COMPLETED,
    findings: tuple[WorkflowFinding, ...] = (),
) -> dict[RoleName, FakeRoleExecutor]:
    """Un ejecutor falso por rol, todos con el mismo comportamiento."""
    return {
        role: FakeRoleExecutor(role, status=status, findings=findings) for role in roles
    }


def all_stage_executors(
    *, web_visual_required: bool = False, cross_audit_required: bool = True
) -> dict[RoleName, FakeRoleExecutor]:
    """Ejecutores para todos los roles del camino limpio, según lo que exija la petición."""
    roles = [
        RoleName.ARCHITECT,
        RoleName.PLANNER,
        RoleName.DEVELOPER,
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
    ]
    if cross_audit_required:
        roles.append(RoleName.CROSS_AUDIT)
    if web_visual_required:
        roles.append(RoleName.VISUAL_QA)
    return {role: FakeRoleExecutor(role) for role in roles}


def state_sequence(run: WorkflowRun) -> tuple[str, ...]:
    """Secuencia de estados por la que pasó el workflow, empezando por ``NEW``."""
    sequence = ["NEW"]
    sequence.extend(transition.to_status.value for transition in run.transitions)
    return tuple(sequence)


def role_sequence(run: WorkflowRun) -> tuple[str, ...]:
    """Roles ejecutados, en orden."""
    return tuple(step.role.value for step in run.steps)


def decision_sequence(run: WorkflowRun) -> tuple[str, ...]:
    """Decisiones calculadas, en orden."""
    return tuple(step.decision.value for step in run.steps)


def clock_after(seconds: float) -> Callable[[], datetime]:
    """Reloj fijo desplazado ``seconds`` hacia el futuro: sirve para agotar el tiempo máximo."""
    frozen = utc_now() + timedelta(seconds=seconds)
    return lambda: frozen


__all__ = [
    "FakeRoleExecutor",
    "all_stage_executors",
    "approve_human_gate",
    "clock_after",
    "decision_sequence",
    "executor_map",
    "make_finding",
    "make_policy",
    "make_request",
    "make_run",
    "offline_policy",
    "role_sequence",
    "state_sequence",
]
