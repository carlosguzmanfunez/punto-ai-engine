"""Matriz determinista del bucle de reparación autónoma acotada (ENGINE-6.1).

Cada caso se ejecuta **a través del kernel real** —máquina de estados, presupuesto, checkpoints,
almacén de artefactos, guard y snapshots reales— y lo único doble es el ejecutor de cada rol, que
devuelve un resultado guionado sin proveedor ni red. Así se puede exigir el comportamiento exacto:
cuántas veces se invoca al Developer de reparación, cuántas veces vuelve a pasar QA, qué defectos
quedan resueltos y con qué código se bloquea cuando algo no se puede reparar.

Los casos están ordenados como la matriz del encargo: el ciclo feliz, la falta de progreso, los
límites del presupuesto, las fronteras de seguridad y de política, el alcance del plan (incluido el
debilitamiento de gates detectado en el diff) y la caída a mitad de mutación.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

import pytest

from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.execution import ModelUsage
from punto.schemas.policy import PolicyDecision, PolicyOutcome
from punto.schemas.repair import RepairCycleStatus, RepairFindingStatus
from punto.schemas.workflow import (
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowFailureCode,
    WorkflowFinding,
    WorkflowRequest,
)
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.errors import WorkflowPolicyRejectedError, WorkflowRoleFailedError
from punto.workflow.handoff import resolve_repair_snapshot
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import PolicyGate, WorkflowPolicy
from punto.workflow.snapshots import FileRepairSnapshots
from workflow_support import (
    FakeRoleExecutor,
    make_finding,
    make_request,
    offline_policy,
)

#: Contenido inicial del archivo que la reparación debe corregir. Se deja como texto legible porque
#: el guard juzga un diff real construido desde la copia de seguridad del snapshot.
BUGGY_SOURCE = "def clamp(value, upper):\n    return min(value, upper)\n"
FIXED_SOURCE = "def clamp(value, upper):\n    return max(0, min(value, upper))\n"
#: Archivo con una prueba: una reparación que la borre o la silencie tiene que ser rechazada.
TEST_SOURCE = "def test_clamp():\n    assert clamp(5, 10) == 0\n"
#: Archivo donde una reparación intentaría subir su propio presupuesto.
LIMITS_SOURCE = "max_repairs = 1\n"


class _SimulatedCrash(BaseException):
    """Caída de proceso simulada: no es un ``WorkflowError``, así que nadie la captura dentro."""


def role_result(
    role: RoleName,
    status: RoleStatus = RoleStatus.COMPLETED,
    *,
    findings: Sequence[WorkflowFinding] = (),
    summary: str = "rol completado",
    error_code: WorkflowFailureCode | None = None,
    error_detail: str = "",
    artifacts: Sequence[str] = (),
) -> RoleExecutionResult:
    """Resultado normalizado de un rol, como el que devolvería un ejecutor real.

    ``artifacts`` son los punteros que el rol declara (el nombre de su informe, por ejemplo): es la
    convención del motor para que un rol deje constancia sin publicar contenido, y es lo que la
    resolución de un defecto usa como evidencia durable.
    """
    stamp = utc_now()
    return RoleExecutionResult(
        role=role,
        status=status,
        summary=summary,
        findings=tuple(findings),
        artifacts=tuple(artifacts),
        model_calls=1,
        usage=ModelUsage(prompt_tokens=7, completion_tokens=1, total_tokens=8),
        attempts=1,
        started_at=stamp,
        completed_at=stamp,
        provider="fake",
        model="fake-model",
        error_code=error_code,
        error_detail=error_detail,
    )


class ScriptedExecutor(FakeRoleExecutor):
    """Ejecutor con guion por llamada: cada entrada del guion responde a una invocación.

    Agotado el guion, aprueba. Es el doble mínimo que permite describir «falla la primera vez y
    aprueba la segunda» sin tocar el kernel.
    """

    def __init__(self, role: RoleName, *outcomes: RoleExecutionResult) -> None:
        super().__init__(role)
        self._script = list(outcomes)

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        self.calls.append(request)
        if not self._script:
            # Un rol que aprueba deja su puntero de informe: es la evidencia con la que el kernel
            # puede declarar resuelto un defecto sin inventarse nada.
            return role_result(self.role, artifacts=(f"{self.role.value}-report",))
        return self._script.pop(0)


class SecurityExecutor(ScriptedExecutor):
    """Security con guion: cada llamada devuelve el resultado declarado para esa posición."""

    def __init__(self, *outcomes: RoleExecutionResult) -> None:
        super().__init__(RoleName.SECURITY, *outcomes)


class RepairingDeveloper(FakeRoleExecutor):
    """Developer que escribe **solo** en el paso de reparación y solo los archivos autorizados.

    El paso inicial de ``IN_PROGRESS`` no toca nada: el defecto ya está en el árbol. Escribir en el
    paso de reparación es lo que hace que el guard tenga cambios reales que juzgar, y es lo que
    distingue una reparación de un no-op.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        writes: Mapping[str, str],
        crash: bool = False,
        status: RoleStatus = RoleStatus.COMPLETED,
    ) -> None:
        super().__init__(RoleName.DEVELOPER)
        self._workspace = workspace
        self._writes = dict(writes)
        self._crash = crash
        self._repair_status = status

    @property
    def repair_calls(self) -> tuple[RoleExecutionRequest, ...]:
        """Invocaciones recibidas en el paso de reparación, que son las que se cuentan."""
        return tuple(call for call in self.calls if call.stage is TaskStatus.REPAIRING)

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        self.calls.append(request)
        if request.stage is not TaskStatus.REPAIRING:
            return role_result(RoleName.DEVELOPER)
        for relative, content in self._writes.items():
            target = self._workspace.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if self._crash:
            raise _SimulatedCrash("el proceso se cayó después de mutar el árbol")
        return role_result(
            RoleName.DEVELOPER,
            self._repair_status,
            summary="reparación aplicada",
        )


class RepairRejectingPolicy(WorkflowPolicy):
    """Política real que rechaza **solo** la evaluación del paso de reparación.

    Es la forma de ejercitar «la política rechaza la reparación» sin que el rechazo ocurra antes, al
    crear el workflow: el motor real decide en todo lo demás.
    """

    def evaluate_action(
        self, *, request: WorkflowRequest, role: RoleName, stage: TaskStatus
    ) -> PolicyGate:
        if stage is not TaskStatus.REPAIRING:
            return super().evaluate_action(request=request, role=role, stage=stage)
        decision = PolicyDecision(
            allowed=False,
            authority_level=AuthorityLevel.LEVEL_3_HUMAN,
            requires_human=True,
            reason="REJECT: la política de la prueba no autoriza reparar",
            outcome=PolicyOutcome.REJECT,
            effective_risk=RiskLevel.HIGH,
            action=request.action,
        )
        return PolicyGate(
            outcome=PolicyOutcome.REJECT,
            decision=decision,
            authority=AuthorityLevel.LEVEL_3_HUMAN,
            risk=RiskLevel.HIGH,
            requires_review=False,
            requires_human=True,
            allowed=False,
            reason="la política de la prueba rechaza la etapa de reparación",
        )


class HumanRequiringRepairPolicy(WorkflowPolicy):
    """Política real que exige aprobación humana **solo** para reparar."""

    def evaluate_action(
        self, *, request: WorkflowRequest, role: RoleName, stage: TaskStatus
    ) -> PolicyGate:
        if stage is not TaskStatus.REPAIRING:
            return super().evaluate_action(request=request, role=role, stage=stage)
        decision = PolicyDecision(
            allowed=False,
            authority_level=AuthorityLevel.LEVEL_3_HUMAN,
            requires_human=True,
            reason="REQUIRE_HUMAN: reparar exige una persona en esta política",
            outcome=PolicyOutcome.REQUIRE_HUMAN,
            effective_risk=RiskLevel.HIGH,
            action=request.action,
        )
        return PolicyGate(
            outcome=PolicyOutcome.REQUIRE_HUMAN,
            decision=decision,
            authority=AuthorityLevel.LEVEL_3_HUMAN,
            risk=RiskLevel.HIGH,
            requires_review=False,
            requires_human=True,
            allowed=False,
            reason="la política de la prueba exige una persona para reparar",
        )


def build_workspace(tmp_path: Path, files: Mapping[str, str]) -> Path:
    """Workspace sintético con los archivos indicados, fuera del repositorio."""
    workspace = tmp_path / "workspace"
    for relative, content in files.items():
        target = workspace.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return workspace


def build_kernel(
    tmp_path: Path,
    *,
    workspace: Path,
    executors: Mapping[RoleName, FakeRoleExecutor],
    policy: WorkflowPolicy | None = None,
) -> tuple[WorkflowKernel, AuditLogger]:
    """Kernel real con checkpoints y artefactos en disco y auditoría en memoria."""
    audit = AuditLogger()
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        audit=audit,
        policy=policy if policy is not None else offline_policy(),
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        workspace=workspace,
    )
    return kernel, audit


def failures(audit: AuditLogger, event_type: AuditEventType) -> tuple[str, ...]:
    """Códigos de fallo auditados de un tipo de evento, en orden."""
    return tuple(
        str(event.metadata_dict.get("code", "")) for event in audit.by_type(event_type)
    )


def digests(workspace: Path, paths: Sequence[str]) -> dict[str, str]:
    """sha256 de cada ruta, leído de disco (``""`` si no existe)."""
    return {path: FileRepairSnapshots(workspace).digest(path) for path in paths}


def repair_request(
    workspace: Path,
    *,
    key: str,
    changed_files: Sequence[str],
    max_repairs: int,
    cross_audit_required: bool = False,
) -> WorkflowRequest:
    """Petición de workflow del escenario, con su presupuesto de reparación."""
    return make_request(
        changed_files=tuple(changed_files),
        workspace_path=str(workspace),
        cross_audit_required=cross_audit_required,
        budget=WorkflowBudget(max_repairs=max_repairs),
        idempotency_key=key,
    )


def qa_finding(category: str = "CORRECTNESS") -> WorkflowFinding:
    """Hallazgo bloqueante de QA, con evidencia: sin evidencia no habría reparación que decidir."""
    return make_finding(
        RoleName.QA,
        severity=FindingSeverity.HIGH,
        category=category,
        message="el clamp no respeta el límite inferior",
        evidence="clamp(5, 10) devuelve 5 y debería devolver 0",
    )


def security_finding(severity: FindingSeverity, category: str) -> WorkflowFinding:
    """Hallazgo de Security con la gravedad y la categoría indicadas."""
    return make_finding(
        RoleName.SECURITY,
        severity=severity,
        category=category,
        message="el control de acceso no valida el rol",
        evidence="la ruta acepta una petición sin rol declarado",
    )


# ---------------------------------------------------------------------------
# 1 - El ciclo feliz: QA falla, se repara y la verificación vuelve a pasar
# ---------------------------------------------------------------------------
def test_qa_failure_is_repaired_once_and_verified_again(tmp_path: Path) -> None:
    """QA FAIL → reparación → QA PASS: un ciclo, Developer una vez y QA dos veces.

    Es la prueba de que el bucle no es «volver a preguntar»: la mutación se aplica, la verificación
    vuelve a empezar por QA y las gates posteriores solo se ejecutan **después** del QA reparado.
    """
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="el clamp no cumple el límite inferior",
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, audit = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="ciclo-feliz", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.COMPLETED
    assert run.result is not None
    assert run.result.repair_cycles == 1
    assert len(developer.repair_calls) == 1, "el Developer de reparación se invoca una sola vez"
    assert len(executors[RoleName.QA].calls) == 2, "QA vuelve a pasar tras la reparación"
    assert [step.role for step in run.steps].count(RoleName.QA) == 2
    # La verificación posterior a la mutación es la que aprueba, y las gates independientes van
    # detrás de ella: ninguna se alcanza sin el QA reparado.
    repair_step = max(
        step.index for step in run.steps if step.stage is TaskStatus.REPAIRING
    )
    qa_indexes = [step.index for step in run.steps if step.role is RoleName.QA]
    security = next(step for step in run.steps if step.role is RoleName.SECURITY)
    reviewer = next(step for step in run.steps if step.role is RoleName.REVIEWER)
    assert min(qa_indexes) < repair_step < max(qa_indexes) < security.index < reviewer.index
    # El defecto se declara resuelto solo tras esa verificación.
    assert len(run.repair_findings) == 1
    finding = run.repair_findings[0]
    assert finding.status is RepairFindingStatus.RESOLVED
    assert finding.repair_attempts == 0, "un intento que funcionó no cuenta como fallo"
    assert finding.resolution_evidence, "resolver sin evidencia sería afirmar sin respaldo"
    assert run.result.resolved_findings == (finding.finding_id,)
    assert run.result.unresolved_findings == ()
    assert run.verification_restart_stage is None
    assert workspace.joinpath("src", "module.py").read_text(encoding="utf-8") == FIXED_SOURCE
    # Auditoría del ciclo, en su momento.
    by_type = {event.event_type for event in audit.events()}
    assert AuditEventType.WORKFLOW_REPAIR_DECIDED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_STARTED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_SNAPSHOT_CREATED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_APPLIED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_VERIFICATION_STARTED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_RESOLVED in by_type


def test_the_cycle_is_reconstructible_from_the_checkpoint(tmp_path: Path) -> None:
    """Un proceso nuevo reconstruye el ciclo desde el checkpoint, sin memoria del anterior."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="checkpoint", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)
    reloaded = FileCheckpointStore(tmp_path / "checkpoints").load(run.workflow_id)

    assert reloaded.status is TaskStatus.COMPLETED
    assert len(reloaded.repair_history) == 1
    cycle = reloaded.repair_history[0]
    assert cycle.cycle == 1
    assert cycle.status is RepairCycleStatus.RESOLVED
    assert cycle.origin_stage is TaskStatus.QA
    assert cycle.restart_stage is TaskStatus.QA
    assert len(cycle.findings_resolved) == 1
    assert len(reloaded.repair_findings) == 1
    assert reloaded.repair_findings[0].status is RepairFindingStatus.RESOLVED
    assert reloaded.usage.repairs == 1


# ---------------------------------------------------------------------------
# 2 - Falta de progreso: el mismo intento no se repite
# ---------------------------------------------------------------------------
def test_the_same_fingerprint_repeated_blocks_without_progress(tmp_path: Path) -> None:
    """El mismo plan que ya falló sin mover el defecto corta el bucle: no se repite idéntico.

    Con presupuesto de sobra para intentarlo tres veces, la tercera no llega a ejecutarse: dos
    intentos idénticos sin progreso son la prueba de que un tercero tampoco cambiaría nada.
    """
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        # QA falla siempre con el mismo defecto: el fingerprint es idéntico en los tres intentos.
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            *(
                role_result(
                    RoleName.QA,
                    RoleStatus.NEEDS_REPAIR,
                    findings=(qa_finding(),),
                    summary="el defecto sigue ahí",
                )
                for _ in range(3)
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, audit = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="sin-progreso", changed_files=("src/module.py",), max_repairs=3
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_NO_PROGRESS
    assert len(developer.repair_calls) == 2, "el tercer intento idéntico no se ejecuta"
    stalled = [
        cycle
        for cycle in run.repair_history
        if cycle.status is RepairCycleStatus.NO_PROGRESS
    ]
    assert len(stalled) == 2
    assert stalled[0].plan_fingerprint == stalled[1].plan_fingerprint
    assert audit.by_type(AuditEventType.WORKFLOW_REPAIR_NO_PROGRESS)


# ---------------------------------------------------------------------------
# 3 - Presupuesto de reparaciones
# ---------------------------------------------------------------------------
def test_without_repair_budget_the_workflow_defers(tmp_path: Path) -> None:
    """``max_repairs=0``: ni una reparación, y la pausa conserva el código de ENGINE-6.0."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="sin-presupuesto", changed_files=("src/module.py",), max_repairs=0
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_DEFERRED
    assert run.usage.repairs == 0
    assert run.repair_history == ()
    assert run.result is None
    assert not developer.repair_calls, "sin presupuesto no se repara nada"
    assert not run.repair_findings or all(
        finding.status is not RepairFindingStatus.RESOLVED
        for finding in run.repair_findings
    )
    assert workspace.joinpath("src", "module.py").read_text(encoding="utf-8") == BUGGY_SOURCE


def test_one_failed_repair_exhausts_the_budget_and_rolls_back(tmp_path: Path) -> None:
    """``max_repairs=1``: exactamente un ciclo; si no arregla el defecto, se agota y se deshace.

    El rollback es local y demostrable: dos archivos autorizados, el estado actual es el que dejó la
    reparación y el snapshot tiene su copia, así que restaurar deja los hashes idénticos a los
    capturados. Sin esa demostración no se restauraría nada.
    """
    workspace = build_workspace(
        tmp_path, {"src/module.py": BUGGY_SOURCE, "src/other.py": "VALUE = 1\n"}
    )
    developer = RepairingDeveloper(
        workspace=workspace,
        writes={"src/module.py": FIXED_SOURCE, "src/other.py": "VALUE = 2\n"},
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            *(
                role_result(
                    RoleName.QA,
                    RoleStatus.NEEDS_REPAIR,
                    findings=(qa_finding(),),
                    summary="el defecto sigue ahí",
                )
                for _ in range(2)
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, audit = build_kernel(tmp_path, workspace=workspace, executors=executors)
    paths = ("src/module.py", "src/other.py")
    request = repair_request(
        workspace, key="agotado", changed_files=paths, max_repairs=1
    )
    before = digests(workspace, paths)

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_BUDGET_EXHAUSTED
    assert len(developer.repair_calls) == 1, "exactamente un ciclo"
    assert len(run.repair_history) == 1
    # El ciclo queda como intento **sin progreso** —es lo que corta la repetición del mismo plan— y
    # el rollback se declara en su detalle y en su propio evento de auditoría.
    assert run.repair_history[0].status is RepairCycleStatus.NO_PROGRESS
    assert "revertida" in run.repair_history[0].detail
    assert run.usage.repairs == 1
    assert digests(workspace, paths) == before, "los hashes vuelven a ser los del snapshot"
    assert workspace.joinpath("src", "module.py").read_text(encoding="utf-8") == BUGGY_SOURCE
    assert workspace.joinpath("src", "other.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert audit.by_type(AuditEventType.WORKFLOW_REPAIR_ROLLED_BACK)
    assert failures(audit, AuditEventType.WORKFLOW_REPAIR_BUDGET_EXHAUSTED)
    assert all(
        finding.status is RepairFindingStatus.UNRESOLVED for finding in run.repair_findings
    )


def test_the_rollback_restores_exactly_the_snapshot_hashes(tmp_path: Path) -> None:
    """El rollback devuelve el árbol al estado que el snapshot publicó, hash a hash."""
    workspace = build_workspace(
        tmp_path, {"src/module.py": BUGGY_SOURCE, "src/other.py": "VALUE = 1\n"}
    )
    store = FileArtifactStore(tmp_path / "artifacts")
    developer = RepairingDeveloper(
        workspace=workspace,
        writes={"src/module.py": FIXED_SOURCE, "src/other.py": "VALUE = 2\n"},
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            *(
                role_result(
                    RoleName.QA,
                    RoleStatus.NEEDS_REPAIR,
                    findings=(qa_finding(),),
                    summary="el defecto sigue ahí",
                )
                for _ in range(2)
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    audit = AuditLogger()
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        audit=audit,
        policy=offline_policy(),
        artifacts=store,
        workspace=workspace,
    )
    paths = ("src/module.py", "src/other.py")
    request = repair_request(
        workspace, key="rollback", changed_files=paths, max_repairs=1
    )

    run = kernel.run_all(request)

    published = resolve_repair_snapshot(
        store, tuple(run.request.evidence_references) + tuple(
            reference
            for entry in run.stage_artifacts
            for reference in entry.references
        )
    )
    assert published is not None, "el snapshot del ciclo viaja publicado en el almacén"
    expected = {entry.path: entry.sha256 for entry in published.entries if entry.existed}
    assert expected == digests(workspace, paths)


# ---------------------------------------------------------------------------
# 4 - Fronteras de seguridad y de política
# ---------------------------------------------------------------------------
def test_a_security_high_finding_stops_the_loop_before_the_developer(tmp_path: Path) -> None:
    """Security HIGH es una frontera, no una tarea: no se repara código por ella."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: FakeRoleExecutor(RoleName.QA),
        RoleName.SECURITY: SecurityExecutor(
            role_result(
                RoleName.SECURITY,
                RoleStatus.NEEDS_REPAIR,
                findings=(security_finding(FindingSeverity.HIGH, "AUTHZ"),),
                summary="hallazgo de seguridad alto",
            )
        ),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="security-alto", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED
    assert not developer.repair_calls, "el Developer no toca código por un SECURITY_STOP"
    assert run.repair_history == ()
    assert workspace.joinpath("src", "module.py").read_text(encoding="utf-8") == BUGGY_SOURCE
    decision = run.repair_findings[0]
    assert decision.source_role is RoleName.SECURITY


def test_a_security_medium_finding_is_repaired_when_policy_allows(tmp_path: Path) -> None:
    """Security MEDIUM con política que permite es reparable: el bucle no se detiene en todo."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: FakeRoleExecutor(RoleName.QA),
        RoleName.SECURITY: SecurityExecutor(
            role_result(
                RoleName.SECURITY,
                RoleStatus.NEEDS_REPAIR,
                findings=(security_finding(FindingSeverity.MEDIUM, "AUTHZ"),),
                summary="hallazgo de seguridad medio",
            )
        ),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="security-medio", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.COMPLETED
    assert len(developer.repair_calls) == 1
    assert len(executors[RoleName.SECURITY].calls) == 2
    assert run.repair_history[0].status is RepairCycleStatus.RESOLVED


def test_a_policy_reject_blocks_the_repair_without_touching_the_workspace(
    tmp_path: Path,
) -> None:
    """Si la política rechaza la reparación, se bloquea sin mutar nada."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    policy = RepairRejectingPolicy(
        engine=PolicyEngine.from_config(Path(__file__).resolve().parents[1] / "config"),
        gate=HumanGate(),
    )
    kernel, _ = build_kernel(
        tmp_path, workspace=workspace, executors=executors, policy=policy
    )
    request = repair_request(
        workspace, key="politica-rechaza", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_POLICY_REJECTED
    assert not developer.repair_calls
    assert run.usage.repairs == 0
    assert workspace.joinpath("src", "module.py").read_text(encoding="utf-8") == BUGGY_SOURCE


# ---------------------------------------------------------------------------
# 5 - Alcance del plan: archivos protegidos y debilitamiento de gates
# ---------------------------------------------------------------------------
def test_a_protected_target_file_is_a_scope_violation(tmp_path: Path) -> None:
    """Un plan que pretenda escribir en un árbol protegido se rechaza antes de mutar.

    Se usa ``src/punto/audit/logger.py`` —árbol protegido por ``PROTECTED_PATHS``— y no
    ``config/constitution.yaml`` porque la política rechaza el workflow **al crearlo** si la
    declara la constitución entre sus archivos: el guard nunca llegaría a juzgarlo. La segunda mitad
    de la prueba fija precisamente eso, para que el motivo de la elección quede comprobado.
    """
    workspace = build_workspace(
        tmp_path, {"src/punto/audit/logger.py": "# auditoría real\n"}
    )
    developer = RepairingDeveloper(
        workspace=workspace,
        writes={"src/punto/audit/logger.py": "# auditoría manipulada\n"},
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace,
        key="archivo-protegido",
        changed_files=("src/punto/audit/logger.py",),
        max_repairs=1,
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_SCOPE_VIOLATION
    assert not developer.repair_calls
    # El intento queda registrado como ciclo bloqueado: la traza conserva que se intentó y por qué
    # no se pudo, sin haber mutado nada.
    assert len(run.repair_history) == 1
    assert run.repair_history[0].status is RepairCycleStatus.BLOCKED
    assert (
        workspace.joinpath("src", "punto", "audit", "logger.py").read_text(encoding="utf-8")
        == "# auditoría real\n"
    )


def test_the_constitution_cannot_even_be_declared_as_a_changed_file(tmp_path: Path) -> None:
    """Motivo del caso anterior: declarar la constitución ni siquiera crea el workflow."""
    workspace = build_workspace(tmp_path, {"config/constitution.yaml": "version: '0.1.0'\n"})
    executor = FakeRoleExecutor(RoleName.ARCHITECT)
    kernel, _ = build_kernel(
        tmp_path, workspace=workspace, executors={RoleName.ARCHITECT: executor}
    )
    request = repair_request(
        workspace,
        key="constitucion",
        changed_files=("config/constitution.yaml",),
        max_repairs=1,
    )

    with pytest.raises(WorkflowPolicyRejectedError):
        kernel.run_all(request)
    assert not executor.calls


def test_removing_a_test_in_the_repair_is_rejected(tmp_path: Path) -> None:
    """Una reparación que borra una prueba no arregla nada: se rechaza y se deshace."""
    workspace = build_workspace(tmp_path, {"tests/test_clamp.py": TEST_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"tests/test_clamp.py": "def helper():\n    return 1\n"}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, audit = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="borra-prueba", changed_files=("tests/test_clamp.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_SCOPE_VIOLATION
    assert "bypass de pruebas" in run.failure.detail
    assert failures(audit, AuditEventType.WORKFLOW_REPAIR_FAILED)
    # El intento no se acepta y el guard demuestra que se puede deshacer: se deshace.
    assert (
        workspace.joinpath("tests", "test_clamp.py").read_text(encoding="utf-8") == TEST_SOURCE
    )
    assert run.repair_history[0].status is RepairCycleStatus.FAILED
    assert "revertida" in run.repair_history[0].detail


def test_adding_a_skip_in_the_repair_is_rejected(tmp_path: Path) -> None:
    """Silenciar una prueba con ``skip`` es el mismo bypass con otro nombre."""
    workspace = build_workspace(tmp_path, {"tests/test_clamp.py": TEST_SOURCE})
    silenced = (
        "import pytest\n\n\n"
        "@pytest.mark.skip(reason='para que pase')\n"
        "def test_clamp():\n"
        "    assert clamp(5, 10) == 0\n"
    )
    developer = RepairingDeveloper(
        workspace=workspace, writes={"tests/test_clamp.py": silenced}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="anade-skip", changed_files=("tests/test_clamp.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_SCOPE_VIOLATION
    assert "skip" in run.failure.detail
    assert (
        workspace.joinpath("tests", "test_clamp.py").read_text(encoding="utf-8") == TEST_SOURCE
    )


def test_raising_max_repairs_in_the_repair_is_rejected(tmp_path: Path) -> None:
    """Subir el propio presupuesto en el diff es debilitar el gate que lo limita."""
    workspace = build_workspace(tmp_path, {"src/limits.py": LIMITS_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/limits.py": "max_repairs = 8\n"}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="sube-presupuesto", changed_files=("src/limits.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_SCOPE_VIOLATION
    assert "max_repairs" in run.failure.detail
    assert workspace.joinpath("src", "limits.py").read_text(encoding="utf-8") == LIMITS_SOURCE


# ---------------------------------------------------------------------------
# 6 - Defecto nuevo introducido por la reparación
# ---------------------------------------------------------------------------
def test_a_new_defect_introduced_by_the_repair_opens_another_cycle(tmp_path: Path) -> None:
    """Un defecto nuevo tiene identidad propia, ciclo propio y el workflow no cierra antes."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    new_defect = security_finding(FindingSeverity.MEDIUM, "AUTHZ")
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: SecurityExecutor(
            role_result(
                RoleName.SECURITY,
                RoleStatus.NEEDS_REPAIR,
                findings=(new_defect,),
                summary="la reparación dejó un control de acceso sin validar",
            )
        ),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="defecto-nuevo", changed_files=("src/module.py",), max_repairs=2
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.COMPLETED
    assert len(developer.repair_calls) == 2, "el defecto nuevo abre su propio ciclo"
    assert len(run.repair_findings) == 2
    identifiers = {finding.finding_id for finding in run.repair_findings}
    assert len(identifiers) == 2, "cada defecto tiene su propio identificador"
    assert {finding.source_role for finding in run.repair_findings} == {
        RoleName.QA,
        RoleName.SECURITY,
    }
    assert run.repair_history[0].status is RepairCycleStatus.FAILED, (
        "el primer ciclo introdujo un defecto nuevo: eso es un intento fallido, no un éxito"
    )
    assert run.repair_history[1].status is RepairCycleStatus.RESOLVED
    assert all(
        finding.status is RepairFindingStatus.RESOLVED for finding in run.repair_findings
    )
    assert run.result is not None
    assert run.result.repair_cycles == 2


# ---------------------------------------------------------------------------
# 7 - Caída tras la mutación y antes del checkpoint
# ---------------------------------------------------------------------------
def test_a_crash_after_the_mutation_does_not_repeat_it(tmp_path: Path) -> None:
    """Una caída después de mutar no repite la mutación: se bloquea para reconciliar.

    El contador del Developer no aumenta al reanudar, porque la intención del efecto quedó apuntada
    ``IN_FLIGHT`` y el kernel no vuelve a invocar al rol a ciegas.
    """
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}, crash=True
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="caida", changed_files=("src/module.py",), max_repairs=2
    )

    with pytest.raises(_SimulatedCrash):
        kernel.run_all(request)

    assert len(developer.repair_calls) == 1
    assert workspace.joinpath("src", "module.py").read_text(encoding="utf-8") == FIXED_SOURCE

    # Un proceso nuevo, con el mismo almacén y el mismo workspace, reanuda el workflow.
    fresh_executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: RepairingDeveloper(
            workspace=workspace, writes={"src/module.py": "OTRA COSA\n"}
        ),
        RoleName.QA: FakeRoleExecutor(RoleName.QA),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    resumed_kernel, _ = build_kernel(
        tmp_path, workspace=workspace, executors=fresh_executors
    )

    resumed = resumed_kernel.run_all(request)

    assert resumed.status is TaskStatus.BLOCKED
    assert resumed.failure is not None
    assert (
        resumed.failure.code
        is WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED
    )
    assert not fresh_executors[RoleName.DEVELOPER].calls, "no se repite la mutación"
    assert workspace.joinpath("src", "module.py").read_text(encoding="utf-8") == FIXED_SOURCE


# ---------------------------------------------------------------------------
# 8 - Idempotencia del workflow reparado
# ---------------------------------------------------------------------------
def test_the_repaired_workflow_is_idempotent_and_keeps_its_history(tmp_path: Path) -> None:
    """Repetir la misma petición devuelve el workflow reparado, no uno nuevo."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="idempotente", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)
    again = kernel.run_all(request)

    assert again.workflow_id == run.workflow_id
    assert again.model_dump() == run.model_dump()
    assert len(developer.repair_calls) == 1


def test_the_snapshot_digests_are_not_left_behind_after_the_cycle(tmp_path: Path) -> None:
    """El estado aplicado solo vive mientras el ciclo está abierto: al cerrar no queda suelto."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="estado-limpio", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.active_repair_plan is None
    assert run.active_repair_snapshot is None
    assert run.active_repair_decision is None
    assert run.active_repair_cycle == 0
    assert run.active_repair_id is None
    assert run.repair_applied_digests == ()
    assert run.usage.repairs == 1


def test_the_repair_findings_survive_without_the_workflow(tmp_path: Path) -> None:
    """Los defectos y su resolución quedan en el resultado final, no solo en el checkpoint."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="informe", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.result is not None
    assert run.result.human_gates == 0
    assert run.result.repair_cycles == 1
    assert run.result.repair_history
    assert "RESOLVED" in run.result.repair_history[0]
    assert run.result.resolved_findings == (run.repair_findings[0].finding_id,)
    assert run.result.known_budget_overrun_model_calls == 0
    assert run.result.known_budget_overrun_tokens == 0


def test_the_workspace_fingerprint_of_the_snapshot_is_stable(tmp_path: Path) -> None:
    """El snapshot publicado describe el estado previo con hashes, no con confianza."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    original = workspace.joinpath("src", "module.py").read_bytes()
    expected = hashlib.sha256(original).hexdigest()

    snapshot = FileRepairSnapshots(workspace).create(
        repair_id=uuid4(), cycle=1, paths=("src/module.py",)
    )

    assert snapshot.entries[0].sha256 == expected
    assert snapshot.workspace_fingerprint
    assert FileRepairSnapshots(workspace).verify(snapshot) is True


def test_the_kernel_refuses_to_repair_without_a_workspace(tmp_path: Path) -> None:
    """Sin workspace no se repara: mutar un árbol desconocido es lo que el snapshot impide."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        audit=AuditLogger(),
        policy=offline_policy(),
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
    )
    request = make_request(
        changed_files=("src/module.py",),
        workspace_path="",
        cross_audit_required=False,
        budget=WorkflowBudget(max_repairs=1),
        idempotency_key="sin-workspace",
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED
    assert not developer.repair_calls


def test_a_repair_without_an_artifact_store_is_refused(tmp_path: Path) -> None:
    """Sin almacén de artefactos el contexto no puede viajar al Developer: no se repara a ciegas."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        audit=AuditLogger(),
        policy=offline_policy(),
        workspace=workspace,
    )
    request = repair_request(
        workspace, key="sin-almacen", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED
    assert not developer.repair_calls
    assert run.repair_history[0].status is RepairCycleStatus.BLOCKED


def test_an_ineffective_repair_is_rejected_as_a_scope_violation(tmp_path: Path) -> None:
    """Una «reparación» que no cambia ningún archivo no es una reparación: se rechaza."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(workspace=workspace, writes={})
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="no-op", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_SCOPE_VIOLATION
    assert len(developer.repair_calls) == 1


def test_the_incomplete_evidence_classification_blocks_with_its_own_code(
    tmp_path: Path,
) -> None:
    """Un defecto sin evidencia ni resumen no se repara: especular está prohibido."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    empty = make_finding(
        RoleName.QA,
        severity=FindingSeverity.HIGH,
        category="CORRECTNESS",
        message="x",
        evidence="",
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(empty,),
                summary="",
                error_detail="",
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="sin-evidencia", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE
    assert not developer.repair_calls


def test_the_provider_unavailable_code_blocks_an_infrastructure_defect(
    tmp_path: Path,
) -> None:
    """Un defecto del entorno se declara infraestructura: no se repara código por un timeout."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    infrastructure = make_finding(
        RoleName.QA,
        severity=FindingSeverity.HIGH,
        category="ENVIRONMENT",
        message="el proveedor no respondió al ejecutar la comprobación",
        evidence="timeout tras 3 intentos de transporte",
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(infrastructure,),
                summary="la verificación no pudo completarse",
                error_code=WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
                error_detail="proveedor no disponible",
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="infraestructura", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    assert not developer.repair_calls


def test_a_non_repairable_category_blocks_with_its_own_code(tmp_path: Path) -> None:
    """Un defecto de una categoría reservada no lo repara el motor, ni con presupuesto."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    reserved = make_finding(
        RoleName.REVIEWER,
        severity=FindingSeverity.HIGH,
        category="SECRETS",
        message="hay una credencial en el repositorio",
        evidence="aparece una clave en el informe",
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: FakeRoleExecutor(RoleName.QA),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: ScriptedExecutor(
            RoleName.REVIEWER,
            role_result(
                RoleName.REVIEWER,
                RoleStatus.NEEDS_REPAIR,
                findings=(reserved,),
                summary="cambios pedidos",
            ),
        ),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="no-reparable", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED
    assert not developer.repair_calls


def test_the_repair_failure_of_the_role_blocks_with_the_role_code(tmp_path: Path) -> None:
    """Si el Developer de reparación falla, el ciclo no se acepta y el código es el del rol."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace,
        writes={"src/module.py": FIXED_SOURCE},
        status=RoleStatus.FAILED,
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, audit = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="rol-falla", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_ROLE_FAILED
    assert run.repair_history[0].status is RepairCycleStatus.FAILED
    assert failures(audit, AuditEventType.WORKFLOW_REPAIR_FAILED)


def test_the_repair_loop_never_runs_without_a_human_gate_for_policy_human_required(
    tmp_path: Path,
) -> None:
    """Una política que exige humano abre un gate real y **no** repara sola."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    policy = HumanRequiringRepairPolicy(
        engine=PolicyEngine.from_config(Path(__file__).resolve().parents[1] / "config"),
        gate=HumanGate(),
    )
    kernel, _ = build_kernel(
        tmp_path, workspace=workspace, executors=executors, policy=policy
    )
    request = repair_request(
        workspace, key="gate-humano", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.HUMAN_APPROVAL
    assert run.human_gate is not None
    assert run.human_gate.human_gate_resume_status is TaskStatus.QA
    assert run.human_gate.current_state is TaskStatus.REPAIRING
    assert not developer.repair_calls, "el kernel no se aprueba a sí mismo"
    assert workspace.joinpath("src", "module.py").read_text(encoding="utf-8") == BUGGY_SOURCE
    assert run.result is None


def test_a_policy_failure_is_reported_without_leaking_the_reason(tmp_path: Path) -> None:
    """El detalle auditado de un fallo de reparación es un código, no razonamiento interno."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, audit = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="auditoria", changed_files=("src/module.py",), max_repairs=1
    )

    kernel.run_all(request)

    payload = " ".join(
        f"{event.action} {event.metadata}" for event in audit.events()
    )
    assert "sk-" not in payload
    assert "API_KEY" not in payload


def test_the_kernel_reports_a_missing_developer_as_a_provider_failure(tmp_path: Path) -> None:
    """Sin ejecutor de Developer, el ciclo no muta nada y el fallo es de proveedor, no de código."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.QA: FakeRoleExecutor(RoleName.QA),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="sin-developer", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    # El Developer falta ya en la etapa inicial, así que el workflow se bloquea antes de reparar.
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE


def test_a_repair_error_from_the_role_leaves_the_run_for_reconciliation(tmp_path: Path) -> None:
    """Un fallo técnico del Developer con el efecto en vuelo no se reintenta: se reconcilia."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = _ExplodingRepairDeveloper(workspace=workspace, writes={})
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="error-reparacion", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert (
        run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED
    )
    assert len(developer.repair_calls) == 1
    assert run.effects[-1].status.value == "UNKNOWN"


class _ExplodingRepairDeveloper(RepairingDeveloper):
    """Developer de reparación que muta el árbol y **después** falla con un error del motor.

    Es el caso que la frontera de efectos existe para cubrir: el rol pudo aplicar el cambio y la
    llamada falló, así que el resultado es incierto y el kernel no reintenta a ciegas.
    """

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        if request.stage is not TaskStatus.REPAIRING:
            return super().execute(request)
        self.calls.append(request)
        target = self._workspace.joinpath("src", "module.py")
        target.write_text(FIXED_SOURCE, encoding="utf-8")
        raise WorkflowRoleFailedError("el proveedor se cayó después de escribir")


def test_a_reproduced_defect_prevents_the_close(tmp_path: Path) -> None:
    """Un defecto que la verificación **vuelve a reproducir** impide cerrar el workflow.

    El invariante «``COMPLETED`` es imposible con defectos bloqueantes abiertos» se cumple en la
    frontera de cierre: al llegar a ``APPROVED`` el kernel resuelve los defectos que la cadena ya no
    reproduce —con la evidencia de las verificaciones— y **no** resuelve los que sí reprodujo: esos
    quedan abiertos y bloquean el cierre. Se comprueba sobre un run aprobado con un ciclo en
    verificación y el defecto reabierto: es el único estado determinista en el que el kernel no
    puede cerrar, porque el camino normal reabre el ciclo antes de aprobar.
    """
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="sin-cerrar", changed_files=("src/module.py",), max_repairs=1
    )
    run = kernel.run_all(request)
    # Se simula el estado en el que la verificación reprodujo el defecto y aun así se llegó a la
    # frontera de aprobación: el ciclo sigue abierto y el defecto está reabierto.
    unresolved = run.model_copy(
        update={
            "status": TaskStatus.APPROVED,
            "active_repair_cycle": 1,
            "active_repair_plan": run.active_repair_plan,
            "repair_history": tuple(
                cycle.model_copy(update={"status": RepairCycleStatus.VERIFYING})
                for cycle in run.repair_history
            ),
            "repair_findings": tuple(
                finding.model_copy(update={"status": RepairFindingStatus.OPEN})
                for finding in run.repair_findings
            ),
        }
    )

    blocked = kernel._advance(unresolved)

    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.failure is not None
    assert blocked.failure.code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert "sin resolver" in blocked.failure.detail


def test_the_repair_domain_is_used_through_the_kernel_with_real_findings(tmp_path: Path) -> None:
    """Los defectos del run son los del dominio de reparación, con su fingerprint canónico."""
    workspace = build_workspace(tmp_path, {"src/module.py": BUGGY_SOURCE})
    developer = RepairingDeveloper(
        workspace=workspace, writes={"src/module.py": FIXED_SOURCE}
    )
    executors: dict[RoleName, FakeRoleExecutor] = {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: ScriptedExecutor(
            RoleName.QA,
            role_result(
                RoleName.QA,
                RoleStatus.NEEDS_REPAIR,
                findings=(qa_finding(),),
                summary="defecto detectado",
                artifacts=("qa-report",),
            ),
        ),
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }
    kernel, _ = build_kernel(tmp_path, workspace=workspace, executors=executors)
    request = repair_request(
        workspace, key="fingerprint", changed_files=("src/module.py",), max_repairs=1
    )

    run = kernel.run_all(request)

    finding = run.repair_findings[0]
    assert finding.fingerprint
    assert len(finding.fingerprint) == 64
    assert finding.source_role is RoleName.QA
    assert finding.source_stage is TaskStatus.QA
    assert finding.affected_files == ("src/module.py",)
    assert finding.acceptance_criteria, "los criterios de la petición viajan al defecto"
    assert finding.category == "CORRECTNESS"
    assert finding.severity is FindingSeverity.HIGH
