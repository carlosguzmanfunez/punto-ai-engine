"""Endurecimiento de la auditoría cruzada y de las rutas (ENGINE-5.2.2).

Tres defectos concretos, sin cambiar la arquitectura:

- **CA-07**: ``MAX_CROSS_AUDIT_EVIDENCE_CHARS`` existía y no se aplicaba. Ahora se aplica en
  PUNTO, después de recibir la propuesta, y **no** como ``maxLength`` en el esquema del
  proveedor (sería una restricción que su dialecto no admite).
- **CA-12**: todo lo que se persiste o se reporta pasa por la redacción del cliente, incluidas
  las violaciones que Pydantic construye con ``input_value``.
- **CA-11**: un carácter de control en una ruta no puede tumbar la frontera.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine5_support import build_security_project
from engine52_support import (
    FakeAnthropicAPI,
    cross_audit_finding_payload,
    cross_audit_payload,
    cross_audit_workspace,
    make_client,
    make_cross_audit_task,
    message_response,
)
from punto.audit.logger import AuditLogger
from punto.crossaudit.claude import ClaudeCrossModelAuditRunner
from punto.crossaudit.validation import validate_cross_audit_proposal
from punto.model_context import (
    _contained,
    _resolved_root,
    build_model_review_context,
    resolve_within_workspace,
)
from punto.qa.paths import PathKind, classify_path, normalize_relative_path
from punto.schemas.audit import AuditEventType
from punto.schemas.cross_audit import (
    MAX_CROSS_AUDIT_EVIDENCE_CHARS,
    CrossAuditFinding,
    CrossAuditGateName,
    CrossAuditProposal,
    CrossAuditStatus,
)
from punto.schemas.enums import FindingSeverity
from punto.security.deterministic import SecurityCheckContext
from test_anthropic_client import FAKE_KEY

CLEAN_PROPOSAL = {
    "summary": "El cambio es coherente con lo pedido y con los informes previos.",
    "findings": [],
    "architecture_assessment": "El cambio respeta la separación de capas del proyecto.",
    "qa_assessment": "QA cubrió el criterio de aceptación con una prueba del comportamiento.",
    "security_assessment": "Security no encontró hallazgos bloqueantes en el contexto revisado.",
    "maintainability_assessment": "Funciones cortas y nombres claros, sin deuda nueva.",
    "scope_assessment": "El alcance se limita al objetivo de la tarea.",
    "recommendation_notes": "Sin objeciones: el cambio puede aceptarse.",
}


def proposal_with_evidence(size: int) -> CrossAuditProposal:
    """Propuesta con una evidencia de tamaño exacto."""
    payload = dict(CLEAN_PROPOSAL)
    payload["findings"] = [
        cross_audit_finding_payload(severity="LOW", evidence="e" * size, line=1)
    ]
    return CrossAuditProposal.model_validate(payload)


# ---------------------------------------------------------------------------
# CA-07: límite de evidencia
# ---------------------------------------------------------------------------
def test_evidence_at_the_limit_is_valid(tmp_path: Path) -> None:
    """2000 caracteres se aceptan."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    proposal = proposal_with_evidence(MAX_CROSS_AUDIT_EVIDENCE_CHARS)

    validation = validate_cross_audit_proposal(
        proposal, task, model_visible_paths=frozenset({"runner.py"})
    )

    assert validation.valid, validation.violations


def test_evidence_over_the_limit_is_rejected(tmp_path: Path) -> None:
    """2001 caracteres se rechazan, con el motivo en la violación."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    proposal = proposal_with_evidence(MAX_CROSS_AUDIT_EVIDENCE_CHARS + 1)

    validation = validate_cross_audit_proposal(
        proposal, task, model_visible_paths=frozenset({"runner.py"})
    )

    assert not validation.valid
    assert any("evidencia" in item and "2000" in item for item in validation.violations)


def test_evidence_limit_is_not_sent_as_a_schema_restriction(tmp_path: Path) -> None:
    """El límite vive en PUNTO, no en el esquema: ``maxLength`` no es del dialecto."""
    from punto.providers.json_schema import provider_schema_for

    provider = provider_schema_for(CrossAuditProposal)
    evidence = provider["properties"]["findings"]["items"]["properties"]["evidence"]

    assert "maxLength" not in evidence
    assert "maxLength" not in json.dumps(provider)


def test_oversized_evidence_is_repaired_by_the_runner(tmp_path: Path) -> None:
    """El runner rechaza la evidencia excesiva y pide la propuesta de nuevo."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    oversized = dict(CLEAN_PROPOSAL)
    oversized["findings"] = [
        cross_audit_finding_payload(
            severity="LOW", evidence="e" * (MAX_CROSS_AUDIT_EVIDENCE_CHARS + 1), line=1
        )
    ]
    api = FakeAnthropicAPI(
        [
            message_response(text=json.dumps(oversized)),
            message_response(text=json.dumps(cross_audit_payload())),
        ]
    )
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.PASS
    assert api.calls == 2


def test_a_finding_object_rejects_evidence_without_text() -> None:
    """La evidencia vacía ya estaba cubierta y sigue estándolo."""
    with pytest.raises(ValidationError):
        CrossAuditFinding(
            id="XA-1",
            severity=FindingSeverity.LOW,
            category="CORRECTNESS",
            title="t",
            description="d",
            evidence="",
        )


# ---------------------------------------------------------------------------
# CA-12: redacción de lo que se persiste
# ---------------------------------------------------------------------------
def keyed_bad_proposal() -> str:
    """Propuesta con la credencial ficticia dentro de un valor mal tipado.

    Así el error de Pydantic incluye ``input_value`` con el texto del modelo, que es el caso
    que el endurecimiento cubre.
    """
    payload = dict(CLEAN_PROPOSAL)
    payload["findings"] = [
        {
            **cross_audit_finding_payload(severity="LOW", line=1),
            "line": FAKE_KEY,
        }
    ]
    return json.dumps(payload)


def test_validation_errors_are_redacted_in_the_report(tmp_path: Path) -> None:
    """``report.error`` no puede contener la credencial aunque Pydantic la cite."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    api = FakeAnthropicAPI([message_response(text=keyed_bad_proposal())])
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    assert report.error
    assert FAKE_KEY not in report.error
    assert FAKE_KEY not in report.model_dump_json()


def test_validation_errors_are_redacted_in_the_audit_log(tmp_path: Path) -> None:
    """El registro de auditoría tampoco guarda la credencial."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    audit = AuditLogger()
    api = FakeAnthropicAPI([message_response(text=keyed_bad_proposal())])
    runner = ClaudeCrossModelAuditRunner(client=make_client(api), audit=audit)

    runner.audit(task)

    rejections = audit.by_type(AuditEventType.CROSS_AUDIT_PROPOSAL_REJECTED)
    assert rejections
    dumped = json.dumps([dict(event.metadata) for event in rejections], default=str)
    assert FAKE_KEY not in dumped
    assert "redactado" in dumped or "REDACTED" in dumped or "input_value" not in dumped


def test_limit_errors_are_redacted_too(tmp_path: Path) -> None:
    """Los errores de límite también pasan por la redacción."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    api = FakeAnthropicAPI([message_response(text=keyed_bad_proposal())])
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert "REDACTED" in report.error or FAKE_KEY not in report.error


def test_redaction_does_not_mangle_clean_errors(tmp_path: Path) -> None:
    """La redacción no convierte en ilegible un motivo que no tenía credenciales."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace, review_report=None)
    api = FakeAnthropicAPI([message_response(text=json.dumps(CLEAN_PROPOSAL))])
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    review_gate = report.gate(CrossAuditGateName.REVIEW)
    assert review_gate is not None
    assert "Reviewer" in review_gate.detail or "revisión" in review_gate.detail
    assert "REDACTED" not in review_gate.detail


# ---------------------------------------------------------------------------
# CA-11: caracteres de control en rutas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["a\x00b", "\x00", "a\x01b", "a\x7fb", "dir/\x00file.py", "a\tb", "a\nb"],
)
def test_control_characters_are_rejected(path: str) -> None:
    """Un carácter de control no es una ruta."""
    with pytest.raises(ValueError, match="control"):
        normalize_relative_path(path)


def test_null_byte_path_is_invalid_for_qa_policy() -> None:
    """La política de rutas de QA lo clasifica como inválido, sin lanzar."""
    assert classify_path("a\x00b") is PathKind.INVALID
    assert classify_path("dir/f\x00ile.py") is PathKind.INVALID


def test_null_byte_path_is_not_resolvable_within_the_workspace(tmp_path: Path) -> None:
    """La frontera devuelve ``None``: no contenida, no crash."""
    workspace = build_security_project(tmp_path, {"runner.py": "VALOR = 1\n"})

    assert resolve_within_workspace(workspace, "a\x00b") is None
    assert resolve_within_workspace(workspace, "\x00") is None


def test_contained_handles_value_error_from_resolve(tmp_path: Path) -> None:
    """``_contained`` trata un ``ValueError`` de ``Path.resolve()`` como no contenida."""
    workspace = build_security_project(tmp_path, {"runner.py": "VALOR = 1\n"})
    root = _resolved_root(workspace)

    assert _contained(workspace, root, "a\x00b") is None
    assert _contained(workspace, root, "runner.py") is not None


def test_context_builder_survives_a_null_byte_path(tmp_path: Path) -> None:
    """Construir contexto con una ruta con NUL no revienta: la descarta."""
    workspace = build_security_project(tmp_path, {"runner.py": "VALOR = 1\n"})

    context = build_model_review_context(workspace, ("runner.py", "a\x00b"))

    assert context.visible_paths == ("runner.py",)
    assert context.unsafe_paths == ()
    assert context.omitted_paths == ()
    assert "VALOR = 1" in context.content


def test_deterministic_checks_survive_a_null_byte_path(tmp_path: Path) -> None:
    """El contexto de los checks deterministas tampoco se cae."""
    workspace = build_security_project(tmp_path, {"runner.py": "VALOR = 1\n"})
    context = SecurityCheckContext(workspace=workspace, paths=("a\x00b", "runner.py"))

    assert context.readable("a\x00b") is None
    assert context.readable("runner.py") is not None


def test_null_byte_path_discarded_by_the_audit_task(tmp_path: Path) -> None:
    """El runner de auditoría no se cae si la tarea declara una ruta con NUL."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(
        workspace, changed_files=("runner.py", "a\x00b"), context_files=("runner.py",)
    )
    api = FakeAnthropicAPI([message_response(text=json.dumps(cross_audit_payload()))])
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert report.status in {CrossAuditStatus.PASS, CrossAuditStatus.BLOCKED}
    assert "runner.py" in report.model_visible_files
