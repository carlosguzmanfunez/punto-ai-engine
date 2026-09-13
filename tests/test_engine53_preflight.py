"""Preflight de ENGINE-5.3: NF-01, NF-02 y NF-03.

Tres hallazgos no bloqueantes de la verificación de 5.2, cerrados antes de construir la capa
web porque los tres afectan a la frontera con el modelo:

- **NF-01**: un archivo obligatorio que **no existe** aparecía en el contexto visible como
  ``(no existe)`` y contaba como cubierto. Ahora falta, y sin excepción explícita de borrado la
  revisión se bloquea antes de llamar al modelo.
- **NF-02**: los caracteres de control se comprobaban **después** de ``strip()``, así que un
  tabulador o un salto de línea en el borde de la ruta desaparecía y la ruta pasaba como limpia.
  Ahora se comprueban sobre la cadena original.
- **NF-03**: el dialecto del proveedor tiene límites de complejidad documentados (24 opcionales,
  16 uniones). Un esquema que los supere se rechaza en PUNTO, no en la API.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ConfigDict, create_model

from engine5_support import build_security_project, make_review_task, review_payload
from engine52_support import (
    FakeAnthropicAPI,
    FakeEngine5Client,
    cross_audit_payload,
    cross_audit_workspace,
    make_client,
    make_cross_audit_task,
    make_qa_report,
    make_security_report,
    message_response,
)
from punto.crossaudit.claude import ClaudeCrossModelAuditRunner
from punto.model_context import (
    build_model_review_context,
    missing_paths,
    safe_path_label,
)
from punto.providers.json_schema import (
    MAX_OPTIONAL_PARAMETERS,
    MAX_UNION_PARAMETERS,
    SchemaValidationError,
    count_optional_parameters,
    count_union_parameters,
    prepare_json_schema,
    provider_schema_for,
    validate_complexity_limits,
)
from punto.qa.paths import PathKind, classify_path, normalize_relative_path
from punto.reviewer.deepseek import DeepSeekReviewerRunner
from punto.schemas.cross_audit import CrossAuditProposal, CrossAuditStatus
from punto.schemas.review import ReviewStatus

CONTROL_CHARACTERS: tuple[str, ...] = (
    "\x00",
    "\x01",
    "\t",
    "\n",
    "\v",
    "\f",
    "\r",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x1f",
    "\x7f",
)


def reviewer_runner_on(
    workspace: Path, **overrides: object
) -> tuple[DeepSeekReviewerRunner, FakeEngine5Client, object]:
    """Runner de revisión con cliente falso, sin red ni modelo real."""
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(),
        security_report=make_security_report(),
        **overrides,
    )
    client = FakeEngine5Client([json.dumps(review_payload())])
    return DeepSeekReviewerRunner(client=client), client, task  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# NF-02: caracteres de control antes de strip
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("character", CONTROL_CHARACTERS)
@pytest.mark.parametrize("position", ["inicio", "final", "medio"])
def test_control_characters_are_rejected_in_any_position(
    character: str, position: str
) -> None:
    """Un control en el borde no puede desaparecer con ``strip()``."""
    candidate = {
        "inicio": f"{character}src/app.py",
        "final": f"src/app.py{character}",
        "medio": f"src{character}/app.py",
    }[position]

    with pytest.raises(ValueError, match="control"):
        normalize_relative_path(candidate)


def test_ordinary_edge_whitespace_is_still_trimmed() -> None:
    """El espacio ordinario del borde sí se recorta: no es un carácter de control."""
    assert normalize_relative_path("  src/app.py  ") == "src/app.py"


def test_control_character_is_invalid_for_qa_policy() -> None:
    """La política de rutas de QA coincide: inválida, sin excepción."""
    for character in CONTROL_CHARACTERS:
        assert classify_path(f"src{character}app.py") is PathKind.INVALID


# ---------------------------------------------------------------------------
# NF-01: archivo obligatorio inexistente
# ---------------------------------------------------------------------------
def test_a_missing_required_file_makes_the_context_incomplete(tmp_path: Path) -> None:
    """Estar en el contexto como ``(no existe)`` no es haberlo revisado."""
    workspace = build_security_project(tmp_path, {"runner.py": "VALOR = 1\n"})

    context = build_model_review_context(workspace, ("runner.py", "fantasma.py"))

    assert "fantasma.py" in context.visible_paths, "el modelo debe saber que se declaró"
    assert context.absent_paths == ("fantasma.py",)
    assert "fantasma.py" in context.omitted_paths
    assert context.complete is False
    assert missing_paths(context, ("runner.py", "fantasma.py")) == ("fantasma.py",)
    assert missing_paths(context, ("runner.py",)) == ()
    assert "inexistente" in context.annotated_content()
    assert "no existen" in context.omission_detail()


def test_a_declared_deletion_is_the_only_exception(tmp_path: Path) -> None:
    """Una eliminación **explícita** exime; la ausencia sola no exime de nada."""
    workspace = build_security_project(tmp_path, {"runner.py": "VALOR = 1\n"})
    context = build_model_review_context(workspace, ("runner.py", "borrado.py"))

    assert missing_paths(context, ("runner.py", "borrado.py")) == ("borrado.py",)
    assert missing_paths(context, ("runner.py", "borrado.py"), deleted=("borrado.py",)) == ()
    # La exención es por ruta: no exime a las demás.
    assert missing_paths(
        context, ("runner.py", "borrado.py"), deleted=("otro.py",)
    ) == ("borrado.py",)


def test_reviewer_blocks_on_a_missing_changed_file(tmp_path: Path) -> None:
    """El Reviewer no puede aprobar un cambio cuyo archivo modificado no existe."""
    workspace = build_security_project(tmp_path, {"runner.py": "VALOR = 1\n"})
    runner, client, task = reviewer_runner_on(
        workspace, changed_files=("runner.py", "perdido.py")
    )

    report = runner.review(task)  # type: ignore[arg-type]

    assert report.status is ReviewStatus.BLOCKED
    assert client.calls == 0, "no se llama al modelo con el contexto incompleto"
    assert "perdido.py" in report.error


def test_reviewer_accepts_an_explicitly_deleted_file(tmp_path: Path) -> None:
    """Con la eliminación declarada, el archivo ausente deja de ser un hueco."""
    workspace = build_security_project(tmp_path, {"runner.py": "VALOR = 1\n"})
    runner, client, task = reviewer_runner_on(
        workspace,
        changed_files=("runner.py", "perdido.py"),
        deleted_files=("perdido.py",),
    )

    report = runner.review(task)  # type: ignore[arg-type]

    assert report.status is ReviewStatus.APPROVED
    assert client.calls >= 1


def test_cross_audit_blocks_on_a_missing_changed_file_without_calling_the_model(
    tmp_path: Path,
) -> None:
    """``changed_files`` con un archivo inexistente ⇒ BLOCKED y cero llamadas al modelo."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace, changed_files=("runner.py", "no-existe.py"))
    api = FakeAnthropicAPI([message_response(text=json.dumps(cross_audit_payload()))])
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    assert api.calls == 0
    assert "no-existe.py" in report.error
    assert "CONTEXT_LIMIT_EXCEEDED" in report.error


def test_cross_audit_accepts_an_explicitly_deleted_changed_file(tmp_path: Path) -> None:
    """La eliminación declarada permite auditar el resto del cambio."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(
        workspace,
        changed_files=("runner.py", "eliminado.py"),
        deleted_files=("eliminado.py",),
    )
    api = FakeAnthropicAPI([message_response(text=json.dumps(cross_audit_payload()))])
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.PASS
    assert api.calls == 1
    # El archivo eliminado se declara (el modelo lo ve como inexistente) pero no bloquea.
    assert "eliminado.py" in report.omitted_paths
    assert "runner.py" in report.model_visible_files


def test_absent_auxiliary_file_is_declared_not_silent(tmp_path: Path) -> None:
    """Un auxiliar inexistente se declara omitido, sin bloquear por sí solo."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(
        workspace, context_files=("runner.py", "auxiliar-ausente.md")
    )
    api = FakeAnthropicAPI([message_response(text=json.dumps(cross_audit_payload()))])
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert "auxiliar-ausente.md" in report.omitted_paths
    assert report.status is CrossAuditStatus.PASS


# ---------------------------------------------------------------------------
# NF-03: límites de complejidad del dialecto
# ---------------------------------------------------------------------------
def test_the_real_schema_is_within_the_documented_limits() -> None:
    """El contrato de producción cabe en los límites del proveedor."""
    prepared = provider_schema_for(CrossAuditProposal)

    assert count_optional_parameters(prepared) <= MAX_OPTIONAL_PARAMETERS
    assert count_union_parameters(prepared) <= MAX_UNION_PARAMETERS
    validate_complexity_limits(prepared)


def test_counting_matches_the_documented_semantics() -> None:
    """Los opcionales se cuentan por objeto, contra su propio ``required``."""
    schema = {
        "type": "object",
        "properties": {
            "obligatorio": {"type": "string"},
            "opcional_uno": {"type": "string"},
            "opcional_dos": {"type": "string"},
            "anidado": {
                "type": "object",
                "properties": {
                    "obligatorio_hijo": {"type": "string"},
                    "opcional_hijo": {"type": "string"},
                },
                "required": ["obligatorio_hijo"],
                "additionalProperties": False,
            },
        },
        "required": ["obligatorio", "anidado"],
        "additionalProperties": False,
    }

    # Dos opcionales en la raíz más uno en el objeto anidado.
    assert count_optional_parameters(schema) == 3
    assert count_union_parameters(schema) == 0


def test_too_many_optional_parameters_is_rejected() -> None:
    """25 opcionales superan el máximo documentado."""
    schema = {
        "type": "object",
        "properties": {f"campo_{index}": {"type": "string"} for index in range(26)},
        "required": ["campo_0"],
        "additionalProperties": False,
    }

    assert count_optional_parameters(schema) == 25
    with pytest.raises(SchemaValidationError, match="opcionales"):
        prepare_json_schema(schema)


def test_optional_parameters_at_the_limit_are_accepted() -> None:
    """Exactamente 24 opcionales pasan."""
    schema = {
        "type": "object",
        "properties": {f"campo_{index}": {"type": "string"} for index in range(25)},
        "required": ["campo_0"],
        "additionalProperties": False,
    }

    assert count_optional_parameters(schema) == MAX_OPTIONAL_PARAMETERS
    prepare_json_schema(schema)


def test_too_many_union_parameters_is_rejected() -> None:
    """17 uniones superan el máximo documentado."""
    schema = {
        "type": "object",
        "properties": {
            f"campo_{index}": {"anyOf": [{"type": "string"}, {"type": "null"}]}
            for index in range(17)
        },
        "required": ["campo_0"],
        "additionalProperties": False,
    }

    assert count_union_parameters(schema) == 17
    with pytest.raises(SchemaValidationError, match="unión"):
        prepare_json_schema(schema)


def test_union_parameters_at_the_limit_are_accepted() -> None:
    """Exactamente 16 uniones pasan."""
    schema = {
        "type": "object",
        "properties": {
            f"campo_{index}": {"anyOf": [{"type": "string"}, {"type": "null"}]}
            for index in range(MAX_UNION_PARAMETERS)
        },
        "required": ["campo_0"],
        "additionalProperties": False,
    }

    assert count_union_parameters(schema) == MAX_UNION_PARAMETERS
    prepare_json_schema(schema)


def test_a_pydantic_model_over_the_limit_fails_before_http() -> None:
    """Un modelo Pydantic con demasiados opcionales no llega a la API."""
    over_limit = create_model(
        "OverLimit",
        __config__=ConfigDict(frozen=True, extra="forbid"),
        ancla=(str, ...),
        **{f"campo_{index}": (str | None, None) for index in range(30)},  # type: ignore[call-overload]
    )

    with pytest.raises(SchemaValidationError, match="opcionales"):
        provider_schema_for(over_limit)


def test_safe_path_label_is_still_used_for_reports() -> None:
    """La etiqueta saneada sigue evitando inyectar controles en un informe."""
    assert safe_path_label("a\x00b") == "a\\x00b"
