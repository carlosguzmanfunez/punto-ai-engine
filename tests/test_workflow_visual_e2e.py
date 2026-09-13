"""Workflow con verificación visual falsa (ENGINE-6.0, encargo 30).

La capa 5.3 entra como **capacidad opcional**: solo cuando la tarea o el perfil la exigen. Aquí se
comprueban las tres salidas que importan —aprobada, cambios pedidos y proveedor no disponible— y que
una tarea sin interfaz no arrastre una verificación visual que no tiene objeto.
"""

from __future__ import annotations

from pathlib import Path

from punto.schemas.enums import FindingSeverity, TaskStatus
from punto.schemas.workflow import RoleName, RoleStatus, WorkflowFailureCode
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.kernel import WorkflowKernel
from workflow_support import (
    FakeRoleExecutor,
    all_stage_executors,
    make_finding,
    make_request,
    role_sequence,
)

VISUAL_FINDING = make_finding(
    RoleName.VISUAL_QA,
    severity=FindingSeverity.HIGH,
    category="LAYOUT",
    message="el titular se sale del viewport en móvil",
)


def visual_kernel(
    store_root: Path, visual: FakeRoleExecutor
) -> tuple[WorkflowKernel, dict[RoleName, FakeRoleExecutor]]:
    """Kernel con una verificación visual guionada, sin auditoría cruzada."""
    executors = all_stage_executors(cross_audit_required=False, web_visual_required=True)
    executors[RoleName.VISUAL_QA] = visual
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(store_root),
    )
    return kernel, executors


def test_a_visual_task_runs_visual_qa_before_approval(tmp_path: Path) -> None:
    """Con perfil visual exigido, VisualQA entra en la etapa de revisión y pasa."""
    kernel, executors = visual_kernel(tmp_path, FakeRoleExecutor(RoleName.VISUAL_QA))

    run = kernel.run_all(make_request(web_visual_required=True, cross_audit_required=False))

    assert role_sequence(run) == (
        "ARCHITECT",
        "PLANNER",
        "DEVELOPER",
        "QA",
        "SECURITY",
        "REVIEWER",
        "VISUAL_QA",
    )
    assert run.status is TaskStatus.COMPLETED
    assert executors[RoleName.VISUAL_QA].calls


def test_a_non_visual_task_does_not_run_visual_qa(tmp_path: Path) -> None:
    """Sin perfil visual, la verificación visual no se exige ni se ejecuta."""
    kernel = WorkflowKernel(
        executors=dict(all_stage_executors(cross_audit_required=False)),
        store=FileCheckpointStore(tmp_path),
    )

    run = kernel.run_all(make_request(web_visual_required=False, cross_audit_required=False))

    assert RoleName.VISUAL_QA not in [step.role for step in run.steps]
    assert run.status is TaskStatus.COMPLETED


def test_visual_changes_requested_never_approves(tmp_path: Path) -> None:
    """Con cambios visuales pedidos, el workflow no aprueba ni completa."""
    kernel, _ = visual_kernel(
        tmp_path,
        FakeRoleExecutor(
            RoleName.VISUAL_QA,
            status=RoleStatus.NEEDS_REPAIR,
            findings=(VISUAL_FINDING,),
        ),
    )

    run = kernel.run_all(make_request(web_visual_required=True, cross_audit_required=False))

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_DEFERRED
    assert TaskStatus.APPROVED not in [t.to_status for t in run.transitions]
    assert TaskStatus.COMPLETED not in [t.to_status for t in run.transitions]


def test_visual_provider_unavailable_blocks_the_workflow(tmp_path: Path) -> None:
    """Sin proveedor visual, el workflow se bloquea: no se aprueba sin ver lo que se pidió ver."""
    kernel, _ = visual_kernel(
        tmp_path,
        FakeRoleExecutor(
            RoleName.VISUAL_QA,
            status=RoleStatus.PROVIDER_UNAVAILABLE,
            error_code=WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
            error_detail="sin credencial de Anthropic",
        ),
    )

    run = kernel.run_all(make_request(web_visual_required=True, cross_audit_required=False))

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    assert TaskStatus.COMPLETED not in [t.to_status for t in run.transitions]
