"""Workflow con verificación visual falsa (ENGINE-6.0, encargo 30).

La capa 5.3 entra como **capacidad opcional**: solo cuando la tarea o el perfil la exigen. Aquí se
comprueban las tres salidas que importan —aprobada, cambios pedidos y proveedor no disponible— y que
una tarea sin interfaz no arrastre una verificación visual que no tiene objeto.

ENGINE-6.0.1 (V60-09) añade la otra mitad: la aplicabilidad la decide el **perfil web determinista**
del proyecto, no el llamante. Un ``web_visual_required=False`` no puede anular una necesidad
objetiva, y un proyecto sin interfaz no se bloquea por una verificación que no tiene objeto.
"""

from __future__ import annotations

import json
from pathlib import Path

from punto.schemas.enums import FindingSeverity, TaskStatus
from punto.schemas.workflow import RoleName, RoleStatus, WorkflowFailureCode
from punto.web.detection import detect_web_project
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.pipeline import visual_qa_required
from workflow_support import (
    FakeRoleExecutor,
    all_stage_executors,
    make_finding,
    make_request,
    offline_policy,
    role_sequence,
)

VISUAL_FINDING = make_finding(
    RoleName.VISUAL_QA,
    severity=FindingSeverity.HIGH,
    category="LAYOUT",
    message="el titular se sale del viewport en móvil",
)


def build_web_project(root: Path) -> Path:
    """Proyecto web mínimo pero real: manifiesto con Next.js y scripts declarados."""
    project = root / "webapp"
    project.mkdir(parents=True, exist_ok=True)
    (project / "package.json").write_text(
        json.dumps(
            {
                "name": "webapp-sintetica",
                "scripts": {"build": "next build", "test": "vitest run"},
                "dependencies": {"next": "14.0.0", "react": "18.2.0"},
            }
        ),
        encoding="utf-8",
    )
    return project


def build_non_web_project(root: Path) -> Path:
    """Proyecto sin interfaz: solo código de servidor."""
    project = root / "engine"
    project.mkdir(parents=True, exist_ok=True)
    (project / "main.py").write_text("print('sin interfaz')\n", encoding="utf-8")
    return project


def visual_kernel(
    store_root: Path, visual: FakeRoleExecutor
) -> tuple[WorkflowKernel, dict[RoleName, FakeRoleExecutor]]:
    """Kernel con una verificación visual guionada, sin auditoría cruzada."""
    executors = all_stage_executors(cross_audit_required=False, web_visual_required=True)
    executors[RoleName.VISUAL_QA] = visual
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(store_root),
        policy=offline_policy(),
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
        policy=offline_policy(),
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


# ---------------------------------------------------------------------------
# V60-09 - Aplicabilidad por perfil web determinista
# ---------------------------------------------------------------------------
def test_a_web_profile_makes_visual_qa_mandatory_even_with_the_flag_false(
    tmp_path: Path,
) -> None:
    """V60-09: un proyecto web exige verificación visual aunque el llamante no la pida."""
    project = build_web_project(tmp_path)
    request = make_request(
        web_visual_required=False,
        cross_audit_required=False,
        workspace_path=str(tmp_path),
        project_path=project.name,
    )

    assert detect_web_project(project).framework.value == "NEXTJS"
    assert visual_qa_required(request) is True

    executors = all_stage_executors(cross_audit_required=False, web_visual_required=True)
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(tmp_path / "cp"),
        policy=offline_policy(),
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.COMPLETED
    assert role_sequence(run)[-1] == "VISUAL_QA"
    assert executors[RoleName.VISUAL_QA].calls


def test_a_project_without_a_web_profile_does_not_run_visual_qa(tmp_path: Path) -> None:
    """Un proyecto sin interfaz no arrastra la verificación visual: no tiene objeto."""
    project = build_non_web_project(tmp_path)
    request = make_request(
        web_visual_required=False,
        cross_audit_required=False,
        workspace_path=str(tmp_path),
        project_path=project.name,
    )

    assert visual_qa_required(request) is False

    executors = all_stage_executors(cross_audit_required=False, web_visual_required=True)
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(tmp_path / "cp"),
        policy=offline_policy(),
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.COMPLETED
    assert RoleName.VISUAL_QA not in [step.role for step in run.steps]
    assert not executors[RoleName.VISUAL_QA].calls


def test_the_declared_flag_still_requests_visual_qa_without_a_web_profile(
    tmp_path: Path,
) -> None:
    """La señal declarada suma: pedirla explícitamente la exige aunque el perfil no la vea."""
    project = build_non_web_project(tmp_path)
    request = make_request(
        web_visual_required=True,
        cross_audit_required=False,
        workspace_path=str(tmp_path),
        project_path=project.name,
    )

    assert visual_qa_required(request) is True


def test_an_unreadable_project_does_not_invent_a_visual_requirement(tmp_path: Path) -> None:
    """Un proyecto ilegible no se declara web por si acaso: no se sabe, y eso se respeta."""
    request = make_request(
        web_visual_required=False,
        cross_audit_required=False,
        workspace_path=str(tmp_path),
        project_path="no-existe",
    )

    assert visual_qa_required(request) is False


def test_the_web_profile_is_deterministic(tmp_path: Path) -> None:
    """El mismo proyecto produce el mismo veredicto de aplicabilidad, sin depender del llamante."""
    project = build_web_project(tmp_path)
    first = make_request(
        web_visual_required=False,
        cross_audit_required=False,
        workspace_path=str(tmp_path),
        project_path=project.name,
    )
    second = make_request(
        web_visual_required=False,
        cross_audit_required=False,
        workspace_path=str(tmp_path),
        project_path=project.name,
    )

    assert visual_qa_required(first) is True
    assert visual_qa_required(second) is True
    assert detect_web_project(project).model_dump() == detect_web_project(project).model_dump()
