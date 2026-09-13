"""Auditoría cruzada de extremo a extremo con un transporte falso (ENGINE-5.2 §12 y §29).

No hay credencial de Anthropic ni llamada HTTP real: se ejercita el **cliente real** contra un
``httpx.MockTransport`` guionado. Eso permite comprobar lo que importa —contrato, validación,
gates, contexto y auditoría— sin inventar un PASS que nadie ha obtenido.

Los cinco live gates viven aparte, en ``tests/integration/test_anthropic_live.py``, y están
PENDING_API_KEY.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from engine52_support import (
    FakeAnthropicAPI,
    audit_runner_with,
    cross_audit_finding_payload,
    cross_audit_payload,
    cross_audit_workspace,
    make_cross_audit_task,
    make_qa_report,
    make_review_report,
    make_security_report,
    message_response,
)
from punto.audit.logger import AuditLogger
from punto.crossaudit.base import CrossAuditLimits, CrossAuditRunner
from punto.crossaudit.claude import (
    BLOCKED_CROSS_AUDIT_ATTEMPTS,
    BLOCKED_PROVIDER_ERROR,
    BLOCKED_PROVIDER_UNAVAILABLE,
    ClaudeCrossModelAuditRunner,
)
from punto.crossaudit.prompts import CROSS_AUDIT_SYSTEM_PROMPT
from punto.model_context import BLOCKED_CONTEXT_LIMIT
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.audit import AuditEventType
from punto.schemas.cross_audit import (
    CrossAuditGateName,
    CrossAuditStatus,
    CrossAuditTask,
)
from punto.schemas.qa import QAStatus
from punto.schemas.review import ReviewStatus
from punto.schemas.security import SecurityStatus
from punto.tasks.manager import TaskManager
from punto.tools.errors import CrossAuditRunnerNotConfiguredError
from test_anthropic_client import error_response, timing_out


def proposal_response(**overrides: Any) -> Any:
    """Respuesta 200 con una propuesta de auditoría serializada."""
    text = json.dumps(cross_audit_payload(**overrides))
    return message_response(text=text)


def make_runner(
    script: list[Any] | None = None,
    *,
    audit: AuditLogger | None = None,
    limits: CrossAuditLimits | None = None,
) -> tuple[ClaudeCrossModelAuditRunner, FakeAnthropicAPI]:
    """Runner contra el transporte falso, con logger opcional."""
    api = FakeAnthropicAPI(script)
    from engine52_support import make_client

    client = make_client(api)
    return (
        ClaudeCrossModelAuditRunner(client=client, audit=audit, limits=limits),
        api,
    )


# ---------------------------------------------------------------------------
# Contrato del runner
# ---------------------------------------------------------------------------
def test_runner_is_abstract() -> None:
    """§16: la interfaz no se puede instanciar sin implementar la auditoría."""
    with pytest.raises(TypeError):
        CrossAuditRunner()  # type: ignore[abstract]


def test_runner_declares_its_identity() -> None:
    """El runner declara quién es, con qué proveedor y con qué prompt."""
    runner, _ = audit_runner_with([proposal_response()])

    assert runner.name == "ClaudeCrossModelAuditRunner"
    assert runner.provider == "anthropic"
    assert runner.model.startswith("claude")
    assert runner.prompt_version
    assert runner.uses_ai is True
    assert runner.limits.max_attempts >= 1


def test_limits_reject_nonsense_values() -> None:
    """Un presupuesto inválido se rechaza al construirlo."""
    with pytest.raises(ValueError, match="max_attempts"):
        CrossAuditLimits(max_attempts=0)
    with pytest.raises(ValueError, match="max_model_calls"):
        CrossAuditLimits(max_model_calls=0)


# ---------------------------------------------------------------------------
# §29: el camino limpio
# ---------------------------------------------------------------------------
def test_clean_fake_claude_produces_pass(tmp_path: Path) -> None:
    """Todo verde + contexto completo + propuesta limpia ⇒ PASS."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    runner, api = make_runner([proposal_response()])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.PASS
    assert report.passed is True
    assert report.findings == ()
    assert all(gate.passed for gate in report.gates), [
        (gate.name.value, gate.detail) for gate in report.gates
    ]
    assert report.provider == "anthropic"
    assert report.model.startswith("claude")
    assert report.upstream_providers == ("deepseek",)
    assert report.cross_model is True
    assert report.model_calls == 1
    assert report.attempts == 1
    assert report.model_usage.total_tokens > 0
    assert api.calls == 1


def test_prompt_carries_the_production_system_prompt_and_context(tmp_path: Path) -> None:
    """§34: se envían el system prompt de producción y el contenido visible real."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    runner, api = make_runner([proposal_response()])

    runner.audit(task)

    body = api.last_body
    assert body["system"] == CROSS_AUDIT_SYSTEM_PROMPT
    text = body["messages"][0]["content"][0]["text"]
    assert "normalize_label" in text
    assert "PASS" in text
    assert "runner.py" in text


def test_cross_model_is_false_when_providers_match(tmp_path: Path) -> None:
    """§22: no se declara auditoría cruzada si todos los proveedores coinciden."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS).model_copy(
            update={"provider": "anthropic"}
        ),
        security_report=make_security_report(SecurityStatus.PASS).model_copy(
            update={"provider": "anthropic"}
        ),
        review_report=make_review_report(
            ReviewStatus.APPROVED, provider="anthropic"
        ),
    )
    runner, _ = make_runner([proposal_response()])

    report = runner.audit(task)

    assert report.upstream_providers == ("anthropic",)
    assert report.cross_model is False
    assert report.status is CrossAuditStatus.PASS


# ---------------------------------------------------------------------------
# Contrato de la propuesta
# ---------------------------------------------------------------------------
def test_status_in_the_proposal_is_rejected_and_repaired(tmp_path: Path) -> None:
    """§19: el modelo no escribe el veredicto; si lo intenta, se rechaza y se repara."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    bad = json.dumps(cross_audit_payload(extra={"status": "PASS"}))
    audit = AuditLogger()
    runner, api = make_runner(
        [message_response(text=bad), proposal_response()], audit=audit
    )

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.PASS
    assert api.calls == 2
    rejections = audit.by_type(AuditEventType.CROSS_AUDIT_PROPOSAL_REJECTED)
    assert len(rejections) == 1
    violations = dict(rejections[0].metadata)["violations"]
    assert any("status" in str(item) for item in violations)


def test_unknown_extra_key_is_rejected(tmp_path: Path) -> None:
    """El contrato es cerrado: una clave desconocida invalida la propuesta."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    bad = json.dumps(cross_audit_payload(extra={"veredicto": "ok"}))
    runner, api = make_runner([message_response(text=bad)])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    assert BLOCKED_CROSS_AUDIT_ATTEMPTS in report.error
    assert api.calls == runner.limits.max_attempts


def test_finding_on_an_invisible_file_is_rejected(tmp_path: Path) -> None:
    """Un hallazgo sobre un archivo que el auditor no recibió es una invención."""
    workspace = cross_audit_workspace(tmp_path)
    extra = {f"doc{index:02d}.md": f"# Documento {index}\n" for index in range(1, 36)}
    for name, content in extra.items():
        (workspace / name).write_text(content, encoding="utf-8")
    omitted = sorted(extra)[-1]
    task = make_cross_audit_task(
        workspace, context_files=("runner.py", *sorted(extra))
    )
    bad = proposal_response(findings=(cross_audit_finding_payload(file=omitted),))
    runner, api = make_runner([bad, proposal_response()])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.PASS
    assert report.findings == ()
    assert omitted in report.omitted_paths
    assert api.calls == 2


def test_finding_referencing_a_missing_qa_finding_is_rejected(tmp_path: Path) -> None:
    """Una referencia a un hallazgo que no existe es una invención con otro nombre."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    bad = proposal_response(
        findings=(cross_audit_finding_payload(references_qa_finding="QA-99"),)
    )
    runner, api = make_runner([bad])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    assert "QA-99" in report.error or "no existe" in report.error
    assert api.calls == runner.limits.max_attempts


def test_a_finding_without_a_useful_assessment_is_rejected(tmp_path: Path) -> None:
    """Una auditoría tiene que decir algo sobre cada dimensión que evalúa."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    payload = cross_audit_payload(extra={"qa_assessment": "ok"})
    runner, api = make_runner([message_response(text=json.dumps(payload))])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    assert "qa_assessment" in report.error
    assert api.calls == runner.limits.max_attempts


# ---------------------------------------------------------------------------
# Hallazgos y veredicto
# ---------------------------------------------------------------------------
def test_high_finding_requests_changes(tmp_path: Path) -> None:
    """§29: gates verdes con hallazgo HIGH ⇒ CHANGES_REQUESTED."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    proposal = proposal_response(
        findings=(
            cross_audit_finding_payload(
                severity="HIGH", category="REGRESSION_RISK", line=4
            ),
        )
    )
    runner, _ = make_runner([proposal])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.CHANGES_REQUESTED
    assert report.blocking_findings
    findings_gate = report.gate(CrossAuditGateName.FINDINGS)
    assert findings_gate is not None and findings_gate.passed is False


def test_medium_finding_keeps_pass(tmp_path: Path) -> None:
    """Un hallazgo MEDIUM se informa y no impide el PASS."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    proposal = proposal_response(
        findings=(cross_audit_finding_payload(severity="MEDIUM", line=4),)
    )
    runner, _ = make_runner([proposal])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.PASS
    assert len(report.findings) == 1
    assert report.blocking_findings == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"qa_report": make_qa_report(QAStatus.FAIL)},
        {"security_report": make_security_report(SecurityStatus.FAIL)},
        {"review_report": make_review_report(ReviewStatus.CHANGES_REQUESTED)},
    ],
)
def test_upstream_failure_never_passes(tmp_path: Path, overrides: dict[str, Any]) -> None:
    """§29: un gate previo no superado impide el PASS por muy limpia que sea la auditoría."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace, **overrides)
    runner, _ = make_runner([proposal_response()])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.CHANGES_REQUESTED


def test_reviewer_blocked_blocks_the_audit(tmp_path: Path) -> None:
    """§29: un Reviewer BLOCKED bloquea la auditoría, no la aprueba."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(
        workspace, review_report=make_review_report(ReviewStatus.BLOCKED)
    )
    runner, _ = make_runner([proposal_response()])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    review_gate = report.gate(CrossAuditGateName.REVIEW)
    assert review_gate is not None and review_gate.blocking is True


def test_missing_review_report_blocks(tmp_path: Path) -> None:
    """Sin Reviewer no hay auditoría cruzada: el gate es preceptivo."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace, review_report=None)
    runner, _ = make_runner([proposal_response()])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    assert report.approved is False if hasattr(report, "approved") else True


# ---------------------------------------------------------------------------
# Proveedor y contexto
# ---------------------------------------------------------------------------
def test_authentication_error_blocks_as_provider_unavailable(tmp_path: Path) -> None:
    """§15: sin credencial válida, PROVIDER_UNAVAILABLE. Nunca otro proveedor."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    runner, api = make_runner([error_response(401, "invalid x-api-key")])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    assert BLOCKED_PROVIDER_UNAVAILABLE in report.error
    assert api.calls == 1
    assert report.provider == "anthropic"


def test_provider_server_error_blocks_after_retries(tmp_path: Path) -> None:
    """Un 5xx se reintenta de forma acotada y termina en BLOCKED."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    runner, api = make_runner([error_response(500, "internal error")])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    assert BLOCKED_PROVIDER_ERROR in report.error
    assert api.calls == 3


def test_timeout_blocks_as_provider_error(tmp_path: Path) -> None:
    """Un timeout agotado también es un fallo de proveedor, no un PASS."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    runner, api = make_runner([timing_out])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    assert BLOCKED_PROVIDER_ERROR in report.error
    assert api.calls == 3


def test_truncated_response_blocks_and_does_not_leak(tmp_path: Path) -> None:
    """Un truncamiento se detecta antes de interpretar el JSON y no filtra contenido."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    partial = '{"summary": "auditoría cortada a medias", "findings": [{"id": "XA-1"'
    runner, api = make_runner(
        [message_response(text=partial, stop_reason="max_tokens")]
    )

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    assert BLOCKED_PROVIDER_ERROR in report.error
    assert "max_tokens" in report.error
    assert partial not in report.error
    assert api.calls == 1


def test_incomplete_context_blocks_without_calling_the_model(tmp_path: Path) -> None:
    """§18: si un archivo modificado no cabe, se bloquea sin consultar al auditor."""
    workspace = cross_audit_workspace(tmp_path)
    outside = tmp_path / "exterior"
    outside.mkdir(exist_ok=True)
    (outside / "secreto.txt").write_text("CONTENIDO_EXTERNO\n", encoding="utf-8")
    junction = workspace / "escape"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if created.returncode != 0:
        pytest.fail(f"no se pudo crear el junction: {created.stdout}{created.stderr}")
    declared = "escape/secreto.txt"
    task = make_cross_audit_task(
        workspace, changed_files=(declared,), context_files=(declared,)
    )
    runner, api = make_runner([proposal_response()])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    assert BLOCKED_CONTEXT_LIMIT in report.error
    assert report.model_visible_files == ()
    assert declared in report.omitted_paths
    assert api.calls == 0
    assert "CONTENIDO_EXTERNO" not in report.model_dump_json()


def test_context_uses_the_shared_boundary(tmp_path: Path) -> None:
    """§18: el contexto visible es el que calcula el módulo compartido, no otro."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    runner, _ = make_runner([proposal_response()])

    report = runner.audit(task)

    assert report.model_visible_files == ("runner.py", "tests/test_runner_developer.py")
    assert report.omitted_paths == ()


# ---------------------------------------------------------------------------
# Auditoría y CAMUS
# ---------------------------------------------------------------------------
def test_audit_events_are_recorded(tmp_path: Path) -> None:
    """§23: el ciclo queda auditado, sin credenciales ni código en los eventos."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    audit = AuditLogger()
    proposal = proposal_response(
        findings=(cross_audit_finding_payload(severity="LOW", line=4),)
    )
    runner, _ = make_runner([proposal], audit=audit)

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.PASS
    for event_type in (
        AuditEventType.CROSS_AUDIT_REQUEST_STARTED,
        AuditEventType.CROSS_AUDIT_PROPOSAL_RECEIVED,
        AuditEventType.CROSS_AUDIT_PROPOSAL_ACCEPTED,
        AuditEventType.CROSS_AUDIT_FINDING_RECORDED,
        AuditEventType.CROSS_AUDIT_COMPLETED,
    ):
        assert audit.by_type(event_type), event_type

    completed = audit.by_type(AuditEventType.CROSS_AUDIT_COMPLETED)[0]
    metadata = dict(completed.metadata)
    assert metadata["provider"] == "anthropic"
    assert tuple(metadata["upstream_providers"]) == ("deepseek",)
    assert metadata["cross_model"] is True
    assert "sk-ant" not in json.dumps(metadata)
    assert "normalize_label" not in json.dumps(metadata)


def test_blocked_audit_is_recorded_as_blocked(tmp_path: Path) -> None:
    """Un bloqueo también queda en el registro, con su motivo."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace, review_report=None)
    audit = AuditLogger()
    runner, _ = make_runner([proposal_response()], audit=audit)

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    blocked = audit.by_type(AuditEventType.CROSS_AUDIT_BLOCKED)
    assert blocked


def test_camus_delegates_the_cross_audit(
    tmp_path: Path,
    task_manager: TaskManager,
    policy_engine: PolicyEngine,
    human_gate: HumanGate,
) -> None:
    """CAMUS delega la auditoría cruzada en el runner inyectado."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    audit = AuditLogger()
    runner, _ = make_runner(
        [
            proposal_response(
                findings=(cross_audit_finding_payload(severity="LOW", line=4),)
            )
        ]
    )

    camus = Camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        planner=Planner(),
        cross_audit_runner=runner,
    )
    report = camus.cross_audit(task)

    assert report.status is CrossAuditStatus.PASS
    assert camus.cross_audit_runner is runner
    # CAMUS registra los hallazgos del rol; el runner registra su propio ciclo.
    assert audit.by_type(AuditEventType.CROSS_AUDIT_FINDING_RECORDED)


def test_camus_without_cross_audit_fails_explicitly(
    tmp_path: Path,
    task_manager: TaskManager,
    policy_engine: PolicyEngine,
    human_gate: HumanGate,
) -> None:
    """Sin auditor inyectado, CAMUS falla: no improvisa ni usa otro proveedor."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    camus = Camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=AuditLogger(),
        planner=Planner(),
    )

    with pytest.raises(CrossAuditRunnerNotConfiguredError):
        camus.cross_audit(task)


# ---------------------------------------------------------------------------
# §30: generalidad
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("app.py", "def saludar() -> str:\n    return 'hola'\n"),
        ("src/page.ts", "export default function Page() { return null }\n"),
        ("cli.py", "print('uso: mi-cli')\n"),
    ],
)
def test_cross_audit_works_for_any_project_nature(
    tmp_path: Path, name: str, content: str
) -> None:
    """§30: Python, Next.js/TypeScript y CLI se auditan con el mismo código."""
    workspace = tmp_path / "workspace"
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    task = make_cross_audit_task(
        workspace, changed_files=(name,), context_files=(name,)
    )
    runner, _ = make_runner([proposal_response()])

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.PASS
    assert report.model_visible_files == (name,)


def test_audit_is_deterministic(tmp_path: Path) -> None:
    """El mismo input da el mismo veredicto y el mismo contexto visible."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)

    def audit_once() -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        runner, _ = make_runner([proposal_response()])
        report = runner.audit(task)
        return (
            report.status.value,
            report.model_visible_files,
            tuple(gate.name.value for gate in report.gates),
        )

    assert audit_once() == audit_once()


def test_task_upstream_providers_ignores_missing_reports(tmp_path: Path) -> None:
    """Un rol que no participó no puede figurar como proveedor previo."""
    workspace = cross_audit_workspace(tmp_path)
    task: CrossAuditTask = make_cross_audit_task(
        workspace, developer_result=None, review_report=None
    )

    assert task.upstream_providers == ("deepseek",)
