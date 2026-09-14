"""Matriz de continuidad y cotas del bucle de reparación (ENGINE-6.1.1 — F611-07/08/09/11).

Cuatro casos que el encargo exige, todos **a través del kernel real** —máquina de estados,
presupuesto, checkpoints y artefactos en disco, guard y snapshots reales— y con el único doble en el
ejecutor de cada rol, que devuelve un resultado guionado sin proveedor ni red:

- **F611-07**: un ciclo a medias *sin* intención de efecto. El checkpoint queda con decisión,
  diagnóstico, plan y snapshot, y el proceso cae justo **antes** de ``EffectLedger.begin_intent``.
  Un proceso nuevo tiene que reconstruir el ciclo desde ese checkpoint, ejecutar la reparación
  **exactamente una vez** y no consumir un segundo presupuesto de reparación. Por eso
  ``_resume_repair`` distingue «plan persistido sin intención» (continuar) de «intención en vuelo»
  (reconciliar): la comprobación de efectos sin resolver va **antes** de reanudar el ciclo, de modo
  que la segunda situación bloquea y la primera continúa. Las dos mitades se comprueban: el ciclo
  sin intención se reanuda y repara una sola vez, y el ciclo con la intención ya durable bloquea
  para reconciliar sin repetir la mutación.
- **F611-08**: las colecciones del ciclo están acotadas. Se producen más defectos que
  ``MAX_REPAIR_FINDINGS_STORED`` y se agota la historia hasta ``MAX_REPAIR_HISTORY``, y se comprueba
  que la retención es determinista, que el checkpoint sigue siendo válido al recargarlo, que la
  colección queda ≤ límite y que no crece con reanudaciones repetidas.
- **F611-09**: un fallo de infraestructura (proveedor no disponible) no repara código por el camino
  normal: el Developer no se invoca, no se consume presupuesto de reparación y el bloqueo usa el
  código estable ``WORKFLOW_PROVIDER_UNAVAILABLE``. La rama de clasificación
  (``RETRYABLE_INFRASTRUCTURE``) queda cubierta **por contrato**, y la equivalencia de códigos se
  demuestra sin fabricar un estado que el motor no puede alcanzar.
- **F611-11**: el handoff durable del adaptador real cubre **todos** los roles, no solo los tres con
  constructor oficial. El caso fija el comportamiento que documenta el docstring de
  ``punto.workflow.roles``: ``build_input`` es el atajo explícito de los dobles, no un requisito de
  QA/Security/Reviewer/CrossAudit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

import pytest

from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.enums import FindingSeverity, TaskStatus
from punto.schemas.execution import ModelUsage
from punto.schemas.repair import Repairability, RepairCycleStatus, RepairFindingStatus
from punto.schemas.workflow import (
    MAX_REPAIR_FINDINGS_STORED,
    MAX_REPAIR_HISTORY,
    MAX_WORKFLOW_STATE_VISITS,
    EffectStatus,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowCheckpoint,
    WorkflowFailureCode,
    WorkflowFinding,
    WorkflowRequest,
    WorkflowRun,
)
from punto.tasks.manager import TaskManager
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import CheckpointStore, FileCheckpointStore
from punto.workflow.handoff import resolve_repair_plan
from punto.workflow.kernel import _BLOCKED_REPAIRABILITY_CODES, WorkflowKernel
from punto.workflow.repair import classify_repairability
from punto.workflow.roles import CamusRoleExecutor, RoleExecutor
from workflow_support import FakeRoleExecutor, make_finding, make_request, offline_policy

#: Contenido inicial del archivo que la reparación corrige, y su corrección. Se deja como texto
#: legible porque el guard juzga un diff real construido desde la copia de seguridad del snapshot.
BUGGY_SOURCE = "def clamp(value, upper):\n    return min(value, upper)\n"
FIXED_SOURCE = "def clamp(value, upper):\n    return max(0, min(value, upper))\n"
#: Único archivo autorizado por la petición de los casos.
TARGET = "src/module.py"
#: Estados de efecto que significan «se pidió y no consta si ocurrió».
UNRESOLVED_EFFECTS = (EffectStatus.IN_FLIGHT, EffectStatus.UNKNOWN)


class _SimulatedCrash(BaseException):
    """Caída de proceso simulada: no es un ``WorkflowError``, así que nadie la captura dentro."""


class CrashAfterCycleStore:
    """Checkpoint store en disco que cae justo **después** de una escritura del ciclo de reparación.

    Delega en un :class:`FileCheckpointStore` real —el contenido queda en disco, válido y recargable
    por un proceso nuevo— y, en el punto elegido, escribe el checkpoint y después cae. Los dos
    puntos son las dos mitades del mismo contrato de continuidad (F611-07):

    - ``with_effect_intent=False``: la escritura que deja el ciclo completo —decisión, diagnóstico,
      plan y snapshot— **sin** intención de efecto. Es el punto anterior a
      ``EffectLedger.begin_intent``: la mutación no se pidió, así que un proceso nuevo continúa;
    - ``with_effect_intent=True``: la escritura que ya dejó la intención ``IN_FLIGHT``. El estado de
      la mutación no consta, así que un proceso nuevo bloquea para reconciliar.
    """

    def __init__(self, root: Path, *, with_effect_intent: bool = False) -> None:
        self._inner = FileCheckpointStore(root)
        self._with_effect_intent = with_effect_intent
        self.crashed = False

    def save(self, run: WorkflowRun) -> WorkflowCheckpoint:
        """Persiste en disco y, en el punto de caída, falla *después* de haber persistido."""
        checkpoint = self._inner.save(run)
        if not self.crashed and self._is_crash_point(run):
            self.crashed = True
            raise _SimulatedCrash(
                "el proceso cae tras persistir el ciclo de reparación y su punto de continuidad"
            )
        return checkpoint

    def latest(self, workflow_id: UUID) -> WorkflowCheckpoint | None:
        """Metadatos del último checkpoint confirmado."""
        return self._inner.latest(workflow_id)

    def load(self, workflow_id: UUID) -> WorkflowRun:
        """Run del último checkpoint, validado por completo."""
        return self._inner.load(workflow_id)

    def list_checkpoints(self, workflow_id: UUID) -> tuple[WorkflowCheckpoint, ...]:
        """Metadatos de todos los checkpoints del workflow."""
        return self._inner.list_checkpoints(workflow_id)

    def _is_crash_point(self, run: WorkflowRun) -> bool:
        """True en la escritura del ciclo cuyo estado coincide con el punto pedido."""
        if run.active_repair_plan is None or run.active_repair_snapshot is None:
            return False
        return _has_unresolved_effect(run) is self._with_effect_intent


def _has_unresolved_effect(run: WorkflowRun) -> bool:
    """True si el libro de efectos tiene una intención cuyo resultado no consta.

    Es la pregunta que separa «no se llegó a pedir la mutación» de «se pidió y no consta»: la
    intención se persiste **antes** de invocar al Developer, así que una intención sin resolver
    significa que la mutación pudo ocurrir aunque el árbol no lo demuestre.
    """
    return any(record.status in UNRESOLVED_EFFECTS for record in run.effects)


def role_result(
    role: RoleName,
    status: RoleStatus = RoleStatus.COMPLETED,
    *,
    findings: Sequence[WorkflowFinding] = (),
    summary: str = "rol completado",
    error_code: WorkflowFailureCode | None = None,
    error_detail: str = "",
    artifacts: Sequence[str] = (),
    model_calls: int = 1,
) -> RoleExecutionResult:
    """Resultado normalizado de un rol, como el que devolvería un ejecutor real.

    ``artifacts`` son los punteros que el rol declara: es la convención del motor para que un rol
    deje constancia sin publicar contenido, y es lo que la resolución de un defecto usa como
    evidencia durable. Un rol sin proveedor declara cero llamadas de modelo (``model_calls=0``).
    """
    stamp = utc_now()
    tokens = 8 if model_calls else 0
    return RoleExecutionResult(
        role=role,
        status=status,
        summary=summary,
        findings=tuple(findings),
        artifacts=tuple(artifacts),
        model_calls=model_calls,
        usage=ModelUsage(
            prompt_tokens=max(0, tokens - 1),
            completion_tokens=1 if tokens else 0,
            total_tokens=tokens,
        ),
        attempts=1,
        started_at=stamp,
        completed_at=stamp,
        provider="fake",
        model="fake-model",
        error_code=error_code,
        error_detail=error_detail,
    )


class ScriptedExecutor(FakeRoleExecutor):
    """Ejecutor con guion por llamada: cada entrada responde a una invocación.

    Agotado el guion, aprueba y deja su puntero de informe, que es la evidencia con la que el kernel
    puede declarar resuelto un defecto sin inventarse nada.
    """

    def __init__(self, role: RoleName, *outcomes: RoleExecutionResult) -> None:
        super().__init__(role)
        self._script = list(outcomes)

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Devuelve el siguiente resultado del guion, o una aprobación con su informe."""
        self.calls.append(request)
        if not self._script:
            return role_result(self.role, artifacts=(f"{self.role.value}-report",))
        return self._script.pop(0)


class DistinctDefectQA(FakeRoleExecutor):
    """QA que reproduce un defecto **distinto** en cada una de las primeras ``failures`` llamadas.

    Es lo que hace avanzar el bucle sin que la detección de falta de progreso lo corte: cada defecto
    tiene su propio fingerprint, así que cada ciclo es un plan distinto. Agotados los fallos, QA
    aprueba y deja su puntero de informe.
    """

    def __init__(self, *, failures: int) -> None:
        super().__init__(RoleName.QA)
        self._failures = failures
        self._seen = 0

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Falla con un defecto nuevo mientras queden fallos declarados; después aprueba."""
        self.calls.append(request)
        if self._seen >= self._failures:
            return role_result(RoleName.QA, artifacts=("qa-report-final",))
        index = self._seen
        self._seen += 1
        return role_result(
            RoleName.QA,
            RoleStatus.NEEDS_REPAIR,
            findings=(qa_finding(f"BUG-{index}"),),
            summary=f"el defecto {index} sigue ahí",
            artifacts=(f"qa-report-{index}",),
        )


class RepairingDeveloper(FakeRoleExecutor):
    """Developer que escribe **solo** en el paso de reparación y solo los archivos autorizados.

    El paso inicial de ``IN_PROGRESS`` no toca nada: el defecto ya está en el árbol. Escribir en el
    paso de reparación es lo que hace que el guard tenga cambios reales que juzgar.
    """

    def __init__(self, *, workspace: Path, writes: Mapping[str, str]) -> None:
        super().__init__(RoleName.DEVELOPER)
        self._workspace = workspace
        self._writes = dict(writes)

    @property
    def repair_calls(self) -> tuple[RoleExecutionRequest, ...]:
        """Invocaciones recibidas en el paso de reparación, que son las que se cuentan."""
        return tuple(call for call in self.calls if call.stage is TaskStatus.REPAIRING)

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Escribe lo autorizado en el paso de reparación; fuera de él, no toca el árbol."""
        self.calls.append(request)
        if request.stage is not TaskStatus.REPAIRING:
            return role_result(RoleName.DEVELOPER)
        for relative, content in self._writes.items():
            target = self._workspace.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return role_result(RoleName.DEVELOPER, summary="reparación aplicada")


def qa_finding(category: str = "CORRECTNESS") -> WorkflowFinding:
    """Hallazgo bloqueante de QA con evidencia: sin evidencia no habría reparación que decidir."""
    return make_finding(
        RoleName.QA,
        severity=FindingSeverity.HIGH,
        category=category,
        message="el clamp no respeta el límite inferior",
        evidence="clamp(5, 10) devuelve 5 y debería devolver 0 en src/module.py",
    )


def distinct_findings(count: int) -> tuple[WorkflowFinding, ...]:
    """``count`` defectos bloqueantes distintos, todos situados en el archivo autorizado."""
    return tuple(
        make_finding(
            RoleName.QA,
            severity=FindingSeverity.HIGH,
            category=f"BUG-{index}",
            message=f"el clamp falla en el caso {index}",
            evidence=f"src/module.py: el caso {index} devuelve un valor fuera de rango",
        )
        for index in range(count)
    )


def build_workspace(root: Path, files: Mapping[str, str]) -> Path:
    """Workspace sintético con los archivos indicados, fuera del repositorio."""
    workspace = root / "workspace"
    for relative, content in files.items():
        target = workspace.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return workspace


def build_kernel(
    *,
    executors: Mapping[RoleName, RoleExecutor],
    store: CheckpointStore,
    artifacts_root: Path,
    workspace: Path,
) -> tuple[WorkflowKernel, AuditLogger]:
    """Kernel real: el almacén que se le pase —en disco— y artefactos en disco."""
    audit = AuditLogger()
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=store,
        audit=audit,
        policy=offline_policy(),
        artifacts=FileArtifactStore(artifacts_root),
        workspace=workspace,
    )
    return kernel, audit


def pipeline_executors(
    developer: RoleExecutor, qa: RoleExecutor
) -> dict[RoleName, RoleExecutor]:
    """Ejecutores de los roles del camino limpio sin auditoría cruzada, con Developer y QA dados."""
    return {
        RoleName.ARCHITECT: FakeRoleExecutor(RoleName.ARCHITECT),
        RoleName.PLANNER: FakeRoleExecutor(RoleName.PLANNER),
        RoleName.DEVELOPER: developer,
        RoleName.QA: qa,
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
    }


def repair_request(
    workspace: Path,
    *,
    key: str,
    changed_files: Sequence[str],
    max_repairs: int,
    max_state_visits: int = 4,
    max_failures: int = 3,
) -> WorkflowRequest:
    """Petición del escenario, con su presupuesto de reparación y sus topes de visitas y fallos."""
    return make_request(
        changed_files=tuple(changed_files),
        workspace_path=str(workspace),
        cross_audit_required=False,
        budget=WorkflowBudget(
            max_repairs=max_repairs,
            max_state_visits=max_state_visits,
            max_failures=max_failures,
        ),
        idempotency_key=key,
    )


def cycle_trace(run: WorkflowRun) -> tuple[tuple[int, str, str, int], ...]:
    """Traza comparable de la historia: ciclo, estado, huella del plan y defectos que cubre.

    Los identificadores de defecto son aleatorios por diseño, así que la identidad que se compara es
    la huella canónica del plan —calculada con los fingerprints de los defectos, que sí son
    deterministas— y no los UUID.
    """
    return tuple(
        (cycle.cycle, cycle.status.value, cycle.plan_fingerprint, len(cycle.findings_in))
        for cycle in run.repair_history
    )


# ---------------------------------------------------------------------------
# F611-07 — ciclo persistido sin intención de efecto
# ---------------------------------------------------------------------------
def test_a_cycle_persisted_without_effect_intent_is_resumed_and_repaired_once(
    tmp_path: Path,
) -> None:
    """F611-07: caída tras persistir el ciclo y antes de la intención → una sola reparación.

    El checkpoint queda con decisión, diagnóstico, plan y snapshot, y el proceso cae justo antes de
    ``EffectLedger.begin_intent``. Un proceso nuevo reconstruye el ciclo desde ese checkpoint —el
    árbol verifica contra el snapshot, así que la mutación todavía no ocurrió— y ejecuta el
    Developer de reparación **exactamente una vez**, sin bloquear como efecto ``UNKNOWN`` y sin
    consumir un segundo presupuesto de reparación.
    """
    workspace = build_workspace(tmp_path, {TARGET: BUGGY_SOURCE})
    checkpoint_root = tmp_path / "checkpoints"
    artifacts_root = tmp_path / "artifacts"
    developer = RepairingDeveloper(workspace=workspace, writes={TARGET: FIXED_SOURCE})
    kernel, _ = build_kernel(
        executors=pipeline_executors(
            developer,
            ScriptedExecutor(
                RoleName.QA,
                role_result(
                    RoleName.QA,
                    RoleStatus.NEEDS_REPAIR,
                    findings=(qa_finding(),),
                    summary="el clamp no cumple el límite inferior",
                    artifacts=("qa-report",),
                ),
            ),
        ),
        store=CrashAfterCycleStore(checkpoint_root),
        artifacts_root=artifacts_root,
        workspace=workspace,
    )
    request = repair_request(
        workspace, key="f611-07", changed_files=(TARGET,), max_repairs=1
    )

    with pytest.raises(_SimulatedCrash):
        kernel.run_all(request)

    workflow_id = kernel.workflow_id_for(request)
    durable = FileCheckpointStore(checkpoint_root).load(workflow_id)

    # El ciclo quedó entero y durable, y el presupuesto de reparación se consumió una sola vez.
    assert durable.status is TaskStatus.REPAIRING
    assert durable.active_repair_decision is not None
    assert durable.active_repair_diagnosis is not None
    assert durable.active_repair_plan is not None
    assert durable.active_repair_snapshot is not None
    assert durable.usage.repairs == 1
    assert not developer.repair_calls, "la caída ocurre antes de invocar al Developer de reparación"
    assert workspace.joinpath(*TARGET.split("/")).read_text(encoding="utf-8") == BUGGY_SOURCE
    # La caída es **antes** de la intención: no hay ningún efecto sin resolver que reconciliar.
    assert [record for record in durable.effects if record.status in UNRESOLVED_EFFECTS] == []
    # El handoff del ciclo ya está publicado: el plan que el checkpoint cita existe en el almacén y
    # apunta al diagnóstico que el propio ciclo declara.
    assert durable.active_repair_plan.diagnosis_id is not None
    references = tuple(
        reference for entry in durable.stage_artifacts for reference in entry.references
    )
    published = resolve_repair_plan(FileArtifactStore(artifacts_root), references)
    assert published is not None, "el plan del ciclo viaja publicado antes de mutar"
    assert published.repair_id == durable.active_repair_plan.repair_id
    assert published.diagnosis_id == durable.active_repair_diagnosis.diagnosis_id

    # Proceso nuevo: mismo disco, kernel y ejecutores nuevos.
    fresh_developer = RepairingDeveloper(workspace=workspace, writes={TARGET: FIXED_SOURCE})
    resumed_kernel, _ = build_kernel(
        executors=pipeline_executors(fresh_developer, ScriptedExecutor(RoleName.QA)),
        store=FileCheckpointStore(checkpoint_root),
        artifacts_root=artifacts_root,
        workspace=workspace,
    )

    resumed = resumed_kernel.resume(workflow_id)

    assert resumed.failure is None, "el ciclo persistido sin intención se continúa, no se bloquea"
    assert resumed.status is TaskStatus.COMPLETED
    assert len(fresh_developer.repair_calls) == 1, "la reparación se ejecuta exactamente una vez"
    assert resumed.usage.repairs == 1, "el ciclo ya consumido no se cobra dos veces"
    assert all(record.status is not EffectStatus.UNKNOWN for record in resumed.effects)
    assert len(resumed.repair_history) == 1
    assert resumed.repair_history[0].status is RepairCycleStatus.RESOLVED
    assert len(resumed.repair_findings) == 1
    assert resumed.repair_findings[0].status is RepairFindingStatus.RESOLVED
    assert resumed.result is not None
    assert resumed.result.repair_cycles == 1
    assert workspace.joinpath(*TARGET.split("/")).read_text(encoding="utf-8") == FIXED_SOURCE


def test_a_cycle_with_the_effect_intent_already_durable_blocks_for_reconciliation(
    tmp_path: Path,
) -> None:
    """F611-07: con la intención ya persistida, un proceso nuevo **no** repite la mutación.

    Es la otra mitad de la distinción que ``_resume_repair`` necesita: aquí el checkpoint trae la
    intención del efecto ``IN_FLIGHT`` sin resolución, así que el estado de la mutación no consta.
    La reanudación bloquea para reconciliar —no ejecuta al Developer, no vuelve a cobrar el ciclo y
    no toca el árbol—, que es exactamente lo contrario de lo que hace el caso anterior con el ciclo
    persistido **sin** intención.
    """
    workspace = build_workspace(tmp_path, {TARGET: BUGGY_SOURCE})
    checkpoint_root = tmp_path / "checkpoints"
    artifacts_root = tmp_path / "artifacts"
    developer = RepairingDeveloper(workspace=workspace, writes={TARGET: FIXED_SOURCE})
    kernel, _ = build_kernel(
        executors=pipeline_executors(
            developer,
            ScriptedExecutor(
                RoleName.QA,
                role_result(
                    RoleName.QA,
                    RoleStatus.NEEDS_REPAIR,
                    findings=(qa_finding(),),
                    summary="el clamp no cumple el límite inferior",
                    artifacts=("qa-report",),
                ),
            ),
        ),
        store=CrashAfterCycleStore(checkpoint_root, with_effect_intent=True),
        artifacts_root=artifacts_root,
        workspace=workspace,
    )
    request = repair_request(
        workspace, key="f611-07-intencion", changed_files=(TARGET,), max_repairs=1
    )

    with pytest.raises(_SimulatedCrash):
        kernel.run_all(request)

    workflow_id = kernel.workflow_id_for(request)
    durable = FileCheckpointStore(checkpoint_root).load(workflow_id)

    # La intención quedó durable y la mutación no ocurrió: el Developer no llegó a invocarse.
    assert durable.status is TaskStatus.REPAIRING
    assert durable.usage.repairs == 1
    assert any(record.status is EffectStatus.IN_FLIGHT for record in durable.effects)
    assert not developer.repair_calls
    assert workspace.joinpath(*TARGET.split("/")).read_text(encoding="utf-8") == BUGGY_SOURCE

    fresh_developer = RepairingDeveloper(workspace=workspace, writes={TARGET: FIXED_SOURCE})
    fresh_qa = ScriptedExecutor(RoleName.QA)
    resumed_kernel, _ = build_kernel(
        executors=pipeline_executors(fresh_developer, fresh_qa),
        store=FileCheckpointStore(checkpoint_root),
        artifacts_root=artifacts_root,
        workspace=workspace,
    )

    resumed = resumed_kernel.resume(workflow_id)

    assert resumed.status is TaskStatus.BLOCKED
    assert resumed.failure is not None
    assert resumed.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED
    assert not fresh_developer.repair_calls, "una intención sin resolver no se repite a ciegas"
    assert not fresh_qa.calls
    assert resumed.usage.repairs == 1, "el ciclo no se cobra dos veces por bloquear"
    assert workspace.joinpath(*TARGET.split("/")).read_text(encoding="utf-8") == BUGGY_SOURCE


# ---------------------------------------------------------------------------
# F611-08 — colecciones acotadas
# ---------------------------------------------------------------------------
def drive_findings_flood(root: Path, *, reported: int) -> WorkflowRun:
    """Conduce el caso de avalancha de defectos sobre un árbol y un almacén propios.

    Un solo informe de QA trae ``reported`` defectos bloqueantes distintos, más de los que el
    contrato guarda: la reparación se decide, se ejecuta y la verificación vuelve a pasar, de modo
    que el checkpoint final conserva la colección ya recortada por el kernel.
    """
    workspace = build_workspace(root, {TARGET: BUGGY_SOURCE})
    developer = RepairingDeveloper(workspace=workspace, writes={TARGET: FIXED_SOURCE})
    kernel, _ = build_kernel(
        executors=pipeline_executors(
            developer,
            ScriptedExecutor(
                RoleName.QA,
                role_result(
                    RoleName.QA,
                    RoleStatus.NEEDS_REPAIR,
                    findings=distinct_findings(reported),
                    summary=f"{reported} defectos bloqueantes en el mismo informe",
                    artifacts=("qa-report",),
                ),
            ),
        ),
        store=FileCheckpointStore(root / "checkpoints"),
        artifacts_root=root / "artifacts",
        workspace=workspace,
    )
    request = repair_request(
        workspace, key=f"f611-08-{root.name}", changed_files=(TARGET,), max_repairs=1
    )
    return kernel.run_all(request)


def test_more_defects_than_the_cap_keep_the_newest_and_the_checkpoint_stays_valid(
    tmp_path: Path,
) -> None:
    """F611-08: con más defectos que la cota, la colección se recorta y el checkpoint sigue válido.

    Un informe de QA con ``MAX_REPAIR_FINDINGS_STORED + 8`` defectos distintos produce más entradas
    de las que el contrato admite. La retención es explícita y determinista: se conservan las **más
    recientes** —en el orden en que se conocieron— y se descarta la traza más antigua, que ya no
    decide nada. La colección queda en la cota, el checkpoint se vuelve a cargar entero y dos
    ejecuciones idénticas retienen exactamente lo mismo.
    """
    reported = MAX_REPAIR_FINDINGS_STORED + 8
    run = drive_findings_flood(tmp_path / "primera", reported=reported)
    repeated = drive_findings_flood(tmp_path / "segunda", reported=reported)

    assert run.status is TaskStatus.COMPLETED
    assert run.usage.repairs == 1
    assert len(run.repair_findings) == MAX_REPAIR_FINDINGS_STORED
    # Política de retención, explícita: `[-MAX_REPAIR_FINDINGS_STORED:]` conserva las últimas
    # entradas conocidas y descarta las más antiguas, sin reordenar lo que queda.
    dropped = reported - MAX_REPAIR_FINDINGS_STORED
    retained = tuple(finding.category for finding in run.repair_findings)
    assert retained == tuple(f"BUG-{index}" for index in range(dropped, reported))
    assert all(finding.status is RepairFindingStatus.RESOLVED for finding in run.repair_findings)
    # La historia no se dispara por tener muchos defectos: sigue siendo un ciclo.
    assert len(run.repair_history) == 1 <= MAX_REPAIR_HISTORY
    assert len(run.repair_history[0].findings_in) == MAX_REPAIR_FINDINGS_STORED
    # Retención determinista: los mismos hechos producen la misma colección retenida.
    assert tuple(finding.category for finding in repeated.repair_findings) == retained
    # El checkpoint con la colección recortada se recarga **entero** y valida.
    reloaded = FileCheckpointStore(tmp_path / "primera" / "checkpoints").load(run.workflow_id)
    assert reloaded.model_dump() == run.model_dump()
    assert len(reloaded.repair_findings) == MAX_REPAIR_FINDINGS_STORED


def drive_to_the_history_cap(root: Path) -> WorkflowRun:
    """Conduce el workflow hasta la cota de historia: ``MAX_REPAIR_HISTORY`` ciclos registrados.

    Cada QA reproduce un defecto **distinto**, así que ningún plan se repite (la detección de falta
    de progreso no corta) y el bucle gasta los ocho ciclos que el presupuesto declara. El octavo
    intento ya no puede volver a ``QA`` —``max_state_visits`` está en su máximo— y el workflow
    cierra bloqueado con el ciclo registrado, que es la frontera que impide que la historia crezca.
    """
    workspace = build_workspace(root, {TARGET: BUGGY_SOURCE})
    developer = RepairingDeveloper(workspace=workspace, writes={TARGET: FIXED_SOURCE})
    kernel, _ = build_kernel(
        executors=pipeline_executors(
            developer, DistinctDefectQA(failures=MAX_REPAIR_HISTORY)
        ),
        store=FileCheckpointStore(root / "checkpoints"),
        artifacts_root=root / "artifacts",
        workspace=workspace,
    )
    request = repair_request(
        workspace,
        key=f"f611-08-historia-{root.name}",
        changed_files=(TARGET,),
        max_repairs=MAX_REPAIR_HISTORY,
        max_state_visits=MAX_WORKFLOW_STATE_VISITS,
        # Ocho ciclos fallidos son ocho fallos de rol: el tope por defecto (3) es un presupuesto
        # distinto del de reparaciones y aquí se declara holgado para que el caso mida la cota de la
        # **historia**, no la de fallos.
        max_failures=16,
    )
    return kernel.run_all(request)


def test_the_repair_history_stops_at_the_cap_and_a_resume_does_not_grow_it(
    tmp_path: Path,
) -> None:
    """F611-08: la historia queda en la cota exacta y una reanudación posterior no la hace crecer.

    Con ``max_repairs = MAX_REPAIR_HISTORY`` y defectos siempre distintos, el bucle abre ocho
    ciclos, uno por presupuesto; el octavo no puede volver a ``QA`` y cierra bloqueado. La historia
    queda en la cota —nunca por encima—, se recarga entera y, al reanudar, el presupuesto de
    reparación ya consumido impide abrir un noveno: la colección no crece y el checkpoint sigue
    siendo válido.
    """
    first = drive_to_the_history_cap(tmp_path / "primera")
    second = drive_to_the_history_cap(tmp_path / "segunda")

    assert first.status is TaskStatus.BLOCKED
    assert first.failure is not None
    assert first.failure.code is WorkflowFailureCode.WORKFLOW_LOOP_DETECTED
    assert len(first.repair_history) == MAX_REPAIR_HISTORY
    assert first.usage.repairs == MAX_REPAIR_HISTORY
    # Retención en orden: los ciclos que no movieron el defecto quedan ``FAILED`` y el vigente —el
    # último, el que el bucle estaba cerrando— es siempre el que sobrevive, aquí ``BLOCKED``.
    assert [cycle.status for cycle in first.repair_history] == [
        *[RepairCycleStatus.FAILED] * (MAX_REPAIR_HISTORY - 1),
        RepairCycleStatus.BLOCKED,
    ]
    # Retención determinista de la historia: dos ejecuciones idénticas dejan la misma traza.
    assert cycle_trace(first) == cycle_trace(second)
    # El checkpoint con la colección en la cota se recarga entero y valida.
    reloaded = FileCheckpointStore(tmp_path / "primera" / "checkpoints").load(first.workflow_id)
    assert reloaded.model_dump() == first.model_dump()
    assert len(reloaded.repair_history) == MAX_REPAIR_HISTORY

    # Reanudar no abre un noveno ciclo: no queda presupuesto de reparación y nada crece.
    workspace = tmp_path / "primera" / "workspace"
    fresh_qa = ScriptedExecutor(RoleName.QA)
    resumed_kernel, _ = build_kernel(
        executors=pipeline_executors(
            RepairingDeveloper(workspace=workspace, writes={TARGET: FIXED_SOURCE}), fresh_qa
        ),
        store=FileCheckpointStore(tmp_path / "primera" / "checkpoints"),
        artifacts_root=tmp_path / "primera" / "artifacts",
        workspace=workspace,
    )

    resumed = resumed_kernel.resume(first.workflow_id)

    assert resumed.status is TaskStatus.BLOCKED
    assert resumed.failure is not None
    assert resumed.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_BUDGET_EXHAUSTED
    assert not fresh_qa.calls, "sin presupuesto no se vuelve a invocar a ningún rol de verificación"
    assert len(resumed.repair_history) == MAX_REPAIR_HISTORY
    assert resumed.usage.repairs == MAX_REPAIR_HISTORY
    after = FileCheckpointStore(tmp_path / "primera" / "checkpoints").load(first.workflow_id)
    assert len(after.repair_history) == MAX_REPAIR_HISTORY


# ---------------------------------------------------------------------------
# F611-09 — RETRYABLE_INFRASTRUCTURE por el camino normal
# ---------------------------------------------------------------------------
def test_a_provider_unavailable_verification_blocks_before_any_repair(tmp_path: Path) -> None:
    """F611-09: un proveedor no disponible bloquea antes de decidir reparación; no se toca código.

    El resultado es el realista de un rol que no pudo hablar con su proveedor: estado
    ``PROVIDER_UNAVAILABLE``, el código estable ``WORKFLOW_PROVIDER_UNAVAILABLE`` y ni un hallazgo
    que capturar. La decisión posterior al rol bloquea ahí mismo, así que el ciclo no llega a
    existir: sin plan, sin snapshot, sin invocación del Developer y sin presupuesto de reparación
    consumido.
    """
    workspace = build_workspace(tmp_path, {TARGET: BUGGY_SOURCE})
    developer = RepairingDeveloper(workspace=workspace, writes={TARGET: FIXED_SOURCE})
    kernel, audit = build_kernel(
        executors=pipeline_executors(
            developer,
            ScriptedExecutor(
                RoleName.QA,
                role_result(
                    RoleName.QA,
                    RoleStatus.PROVIDER_UNAVAILABLE,
                    summary="el proveedor de QA no responde",
                    error_code=WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
                    error_detail="timeout de conexión: no llegó ninguna respuesta del proveedor",
                    model_calls=0,
                ),
            ),
        ),
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        artifacts_root=tmp_path / "artifacts",
        workspace=workspace,
    )
    request = repair_request(
        workspace, key="f611-09-proveedor", changed_files=(TARGET,), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    assert not developer.repair_calls, "el Developer no toca código por un fallo de infraestructura"
    assert run.usage.repairs == 0, "no se consume presupuesto de reparación"
    assert run.repair_history == ()
    assert run.repair_findings == ()
    assert run.active_repair_decision is None
    assert run.active_repair_plan is None
    assert run.active_repair_snapshot is None
    assert workspace.joinpath(*TARGET.split("/")).read_text(encoding="utf-8") == BUGGY_SOURCE
    assert audit.events(), "el bloqueo queda auditado"


def test_an_infrastructure_defect_is_blocked_by_the_normal_path_before_classification(
    tmp_path: Path,
) -> None:
    """F611-09: el defecto de infraestructura se bloquea **antes** de que el bucle lo clasifique.

    Un rol verificador que declara el código de proveedor no disponible y además trae un hallazgo
    bloqueante deja el defecto capturado y abierto —el material que la clasificación
    ``RETRYABLE_INFRASTRUCTURE`` necesitaría— pero ``decide_after_role`` bloquea con
    ``WORKFLOW_PROVIDER_UNAVAILABLE`` antes de que exista un ciclo. La rama de clasificación queda
    cubierta **por contrato**: se demuestra que, si se alcanzara, bloquearía con el mismo código
    estable, y que la única regla que la separaría de una reparación autónoma es el indicador de
    infraestructura que el kernel deriva del paso que reportó el defecto.
    """
    workspace = build_workspace(tmp_path, {TARGET: BUGGY_SOURCE})
    developer = RepairingDeveloper(workspace=workspace, writes={TARGET: FIXED_SOURCE})
    kernel, _ = build_kernel(
        executors=pipeline_executors(
            developer,
            ScriptedExecutor(
                RoleName.QA,
                role_result(
                    RoleName.QA,
                    RoleStatus.NEEDS_REPAIR,
                    findings=(qa_finding(),),
                    summary="el entorno no permitió verificar: el proveedor no respondió",
                    artifacts=("qa-report",),
                    error_code=WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
                    error_detail="el proveedor de QA cayó a mitad de la verificación",
                    model_calls=0,
                ),
            ),
        ),
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        artifacts_root=tmp_path / "artifacts",
        workspace=workspace,
    )
    request = repair_request(
        workspace, key="f611-09-infraestructura", changed_files=(TARGET,), max_repairs=1
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    # El defecto existe y sigue abierto: el material de la clasificación estaba ahí.
    assert len(run.repair_findings) == 1
    defect = run.repair_findings[0]
    assert defect.status is RepairFindingStatus.OPEN
    assert defect.code == WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE.value
    # ... y aun así no hubo ciclo: ni plan, ni snapshot, ni Developer, ni presupuesto gastado.
    assert not developer.repair_calls
    assert run.usage.repairs == 0
    assert run.repair_history == ()
    assert run.active_repair_decision is None
    assert workspace.joinpath(*TARGET.split("/")).read_text(encoding="utf-8") == BUGGY_SOURCE

    # Cobertura por contrato de la rama de clasificación: el mismo código estable, por las dos vías.
    assert (
        _BLOCKED_REPAIRABILITY_CODES[Repairability.RETRYABLE_INFRASTRUCTURE]
        is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    )
    assert run.failure.code is _BLOCKED_REPAIRABILITY_CODES[
        Repairability.RETRYABLE_INFRASTRUCTURE
    ]
    assert (
        classify_repairability(
            finding=defect,
            request=run.request,
            policy_allows=True,
            infrastructure=True,
        )
        is Repairability.RETRYABLE_INFRASTRUCTURE
    )
    # El código normalizado del defecto lleva el prefijo ``WORKFLOW_``, así que la regla por token
    # de ``INFRASTRUCTURE_CODES`` no lo reconoce: lo que lo declara infraestructura es el paso que
    # lo reportó (``_is_infrastructure``), que el kernel solo calcula con un ciclo ya abierto.
    assert (
        classify_repairability(
            finding=defect,
            request=run.request,
            policy_allows=True,
            infrastructure=False,
        )
        is Repairability.AUTONOMOUS_REPAIRABLE
    )


# ---------------------------------------------------------------------------
# F611-11 — el handoff durable es de todos los roles
# ---------------------------------------------------------------------------
def test_a_verification_role_uses_the_durable_handoff_without_build_input(
    tmp_path: Path, config_dir: Path
) -> None:
    """F611-11: QA no exige ``build_input``; sin él usa el handoff durable y, si falta, bloquea.

    Es el comportamiento real que documenta el docstring de ``punto.workflow.roles``: el adaptador
    real reconstruye la entrada de **todos** los roles del pipeline desde el almacén de artefactos,
    y ``build_input`` es solo el atajo explícito de los dobles. Sin almacén y sin *closure* la etapa
    falla como defecto de cableado; con almacén pero sin el handoff durable se declara evidencia
    incompleta (``BLOCKED``) en vez de inventar la entrada del rol.
    """
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    camus = Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
        policy_engine=PolicyEngine.from_config(config_dir),
        human_gate=HumanGate(),
        audit=AuditLogger(),
        planner=Planner(),
    )
    request = RoleExecutionRequest(
        workflow_id=UUID("11111111-1111-4111-8111-111111111111"),
        step_index=0,
        role=RoleName.QA,
        stage=TaskStatus.QA,
        task_id=UUID("22222222-2222-4222-8222-222222222222"),
        project_id=UUID("33333333-3333-4333-8333-333333333333"),
        objective="verificar el cambio",
        workspace_path=str(tmp_path / "workspace"),
        idempotency_key="f611-11-sin-handoff",
    )

    durable = CamusRoleExecutor(camus=camus, role=RoleName.QA, artifacts=artifacts)
    without_store = CamusRoleExecutor(camus=camus, role=RoleName.QA)

    # Sin almacén: defecto de cableado, no «le falta un build_input».
    wired = without_store.execute(request)
    assert wired.status is RoleStatus.FAILED
    assert wired.error_code is WorkflowFailureCode.WORKFLOW_ROLE_FAILED
    assert "artifacts" in wired.error_detail

    # Con almacén y sin el plan durable: evidencia incompleta, que el kernel vuelve ``BLOCKED``.
    result = durable.execute(request)
    assert result.status is RoleStatus.BLOCKED
    assert result.error_code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert "falta el plan durable" in result.error_detail
