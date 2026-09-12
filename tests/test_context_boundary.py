"""Frontera de contexto del modelo (ENGINE-5.1).

Dos defectos se cierran aquí, y los dos son de la misma naturaleza: el motor estaba dispuesto
a aceptar como revisado algo que el modelo **nunca vio**.

1. **Allowlist vacía = permitir todo.** Un plan podía apuntar a cualquier archivo existente del
   workspace cuando la tarea no declaraba ni un solo archivo revisable.
2. **Contexto declarado ≠ contexto enviado.** Los validadores aceptaban hallazgos sobre todo
   ``changed_files + context_files``, mientras el prompt solo llevaba los primeros 30.

La regla que se prueba, sin excepciones: *un hallazgo sobre un archivo que el agente no vio es
una invención*. Una allowlist vacía no autoriza nada, un archivo omitido no se marca como
revisado, y una revisión a la que le faltó un archivo modificado no se aprueba.

Todo es determinista: no hay modelo real ni sandbox.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine5_support import (
    CORRECTED_RUNNER,
    LEAKY_FILE,
    FakeEngine5Client,
    build_security_project,
    finding_payload,
    findings_payload,
    make_qa_report,
    make_review_task,
    make_security_report,
    make_security_task,
    payload,
    review_finding_payload,
    review_payload,
    security_plan_payload,
)
from punto.audit.logger import AuditLogger
from punto.model_context import (
    BLOCKED_CONTEXT_LIMIT,
    MAX_MODEL_VISIBLE_PATHS,
    MISSING_FILE_MARKER,
    build_model_review_context,
    missing_paths,
)
from punto.reviewer.deepseek import DeepSeekReviewerRunner
from punto.reviewer.validation import validate_review_proposal
from punto.schemas.audit import AuditEventType
from punto.schemas.review import (
    ReviewGateName,
    ReviewProposal,
    ReviewStatus,
)
from punto.schemas.security import (
    SecurityFindingSource,
    SecurityFindingsProposal,
    SecurityPlanProposal,
    SecurityStatus,
)
from punto.security.checks import DEFAULT_SECURITY_REGISTRY
from punto.security.deepseek import DeepSeekSecurityRunner
from punto.security.validation import (
    validate_findings,
    validate_security_plan,
)

#: Secreto real, para comprobar que nunca se lee lo que no está autorizado.
SECRET_VALUE = "sk-live-9f8e7d6c5b4a3210fedcba9876543210"


def many_files(
    tmp_path: Path, count: int, *, prefix: str = "mod"
) -> tuple[Path, tuple[str, ...]]:
    """Proyecto sintético con ``count`` archivos Python y sus rutas."""
    files = {
        f"{prefix}{index:02d}.py": f"VALOR_{index} = {index}\n"
        for index in range(1, count + 1)
    }
    workspace = build_security_project(tmp_path, files)
    return workspace, tuple(files)


def security_plan_with_targets(targets: tuple[str, ...]) -> SecurityPlanProposal:
    """Plan de seguridad que apunta a los objetivos indicados."""
    data = security_plan_payload()
    data["review_targets"] = [
        {"path": path, "areas": ["INJECTION"]} for path in targets
    ]
    return SecurityPlanProposal.model_validate(data)


# ---------------------------------------------------------------------------
# La frontera, como función pura
# ---------------------------------------------------------------------------
def test_context_builder_is_deterministic_and_declares_omissions(tmp_path: Path) -> None:
    """§9.8: mismo input, mismo contexto visible, y lo omitido se declara."""
    workspace, names = many_files(tmp_path, MAX_MODEL_VISIBLE_PATHS + 1)

    first = build_model_review_context(workspace, names)
    second = build_model_review_context(workspace, names)

    assert first == second
    assert first.visible_paths == names[:MAX_MODEL_VISIBLE_PATHS]
    assert first.omitted_paths == (names[-1],)
    assert first.complete is False
    assert first.line_count(names[-1]) is None
    assert "contexto parcial" in first.annotated_content()
    assert first.omission_detail()


def test_an_omitted_file_never_appears_in_the_content(tmp_path: Path) -> None:
    """Lo que no se envía no puede estar en el prompt: ni su nombre como bloque."""
    workspace, names = many_files(tmp_path, MAX_MODEL_VISIBLE_PATHS + 1)

    context = build_model_review_context(workspace, names)

    assert f"=== {names[-1]} ===" not in context.content
    assert all(f"=== {name} ===" in context.content for name in context.visible_paths)


def test_an_empty_list_authorizes_nothing(tmp_path: Path) -> None:
    """§9.7: sin rutas declaradas no hay contexto visible, y sin contexto no hay permiso."""
    workspace, _ = many_files(tmp_path, 1)

    context = build_model_review_context(workspace, ())

    assert context.visible_paths == ()
    assert context.visible_set == frozenset()
    assert context.content == ""
    assert context.complete is True  # no se declaró nada: no hay nada omitido
    assert context.annotated_content() == "(no se declararon archivos)"
    assert missing_paths(context, ("mod01.py",)) == ("mod01.py",)


def test_a_declared_but_absent_file_is_visible_without_lines(tmp_path: Path) -> None:
    """Un archivo declarado que no existe se declara: visible, pero sin una sola línea."""
    workspace, _ = many_files(tmp_path, 1)

    context = build_model_review_context(workspace, ("mod01.py", "fantasma.py"))

    assert context.visible_paths == ("mod01.py", "fantasma.py")
    assert context.line_count("mod01.py") == 2
    assert context.line_count("fantasma.py") == 0
    assert MISSING_FILE_MARKER in context.content


def test_missing_paths_reports_what_did_not_reach_the_model(tmp_path: Path) -> None:
    """La comprobación que impide aprobar una revisión parcial."""
    workspace, names = many_files(tmp_path, MAX_MODEL_VISIBLE_PATHS + 2)

    context = build_model_review_context(workspace, names)

    assert missing_paths(context, names) == names[MAX_MODEL_VISIBLE_PATHS:]
    assert missing_paths(context, names[:3]) == ()


# ---------------------------------------------------------------------------
# §8: allowlist vacía = no permitir nada
# ---------------------------------------------------------------------------
def test_security_plan_cannot_escape_an_empty_context(tmp_path: Path) -> None:
    """§8: sin contexto autorizado, un objetivo existente del workspace se rechaza."""
    workspace = build_security_project(tmp_path, {"src/secret.py": LEAKY_FILE})
    task = make_security_task(workspace, changed_files=(), context_files=())

    validation = validate_security_plan(
        security_plan_with_targets(("src/secret.py",)),
        task,
        registry=DEFAULT_SECURITY_REGISTRY,
        existing_paths=frozenset({"src/secret.py"}),
    )

    assert not validation.valid
    assert any("fuera del contexto autorizado" in item for item in validation.violations)


def test_security_with_an_empty_context_never_passes_and_never_reads(tmp_path: Path) -> None:
    """§8: el runner bloquea, no ejecuta ningún check y no toca el archivo.

    El modelo insiste tres veces en un objetivo que la tarea no autorizó. La auditoría termina
    ``BLOCKED``, ningún check llega a ejecutarse y el secreto no aparece en el informe.
    """
    workspace = build_security_project(tmp_path, {"src/secret.py": LEAKY_FILE})
    task = make_security_task(workspace, changed_files=(), context_files=())
    client = FakeEngine5Client([payload(security_plan_payload(path="src/secret.py"))])
    runner = DeepSeekSecurityRunner(client=client)  # type: ignore[arg-type]

    report = runner.evaluate(task)

    assert report.status is SecurityStatus.BLOCKED
    assert report.executed_checks == ()
    assert report.findings == ()
    assert report.model_visible_files == ()
    assert report.plan is None
    assert SECRET_VALUE not in report.model_dump_json()
    assert client.calls == 3


def test_security_never_opens_a_file_outside_the_authorized_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§8: la frontera se comprueba **antes** de abrir nada.

    No basta con que la auditoría no termine en PASS: el archivo no autorizado no puede
    llegar a leerse ni una sola vez, ni por el prompt ni por ``SecurityCheckContext``.
    """
    workspace = build_security_project(tmp_path, {"src/secret.py": LEAKY_FILE})
    task = make_security_task(workspace, changed_files=(), context_files=())
    opened: list[str] = []
    original = Path.read_text

    def tracked(self: Path, *args: object, **kwargs: object) -> str:
        opened.append(self.as_posix())
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", tracked)
    client = FakeEngine5Client([payload(security_plan_payload(path="src/secret.py"))])
    runner = DeepSeekSecurityRunner(client=client)  # type: ignore[arg-type]

    report = runner.evaluate(task)

    assert report.status is SecurityStatus.BLOCKED
    assert not [path for path in opened if path.endswith("secret.py")], opened


def test_empty_visible_context_denies_every_security_finding(tmp_path: Path) -> None:
    """§9.7: con contexto visible vacío ningún hallazgo del modelo señala un archivo."""
    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    task = make_security_task(workspace)
    proposal = SecurityFindingsProposal.model_validate(
        findings_payload((finding_payload(file="runner.py"),))
    )

    result = validate_findings(
        proposal,
        task,
        model_visible_paths=frozenset(),
        workspace_files=frozenset({"runner.py"}),
    )

    assert not result.valid
    assert result.findings == ()
    assert any("fuera del contexto visible" in item for item in result.violations)


# ---------------------------------------------------------------------------
# §9.1, §9.2 y §9.6: visibilidad en Security
# ---------------------------------------------------------------------------
def test_security_finding_on_an_invisible_file_is_rejected(tmp_path: Path) -> None:
    """§9.1: el archivo 31 está autorizado, pero no se envió: el hallazgo no vale."""
    workspace, names = many_files(tmp_path, MAX_MODEL_VISIBLE_PATHS + 1)
    task = make_security_task(workspace, changed_files=names, context_files=())
    context = build_model_review_context(workspace, task.reviewable_paths)

    assert names[-1] not in context.visible_set
    invisible = validate_findings(
        SecurityFindingsProposal.model_validate(
            findings_payload((finding_payload(file=names[-1], line=1),))
        ),
        task,
        model_visible_paths=context.visible_set,
        workspace_files=frozenset(names),
    )

    assert not invisible.valid
    assert invisible.findings == ()
    assert any("fuera del contexto visible" in item for item in invisible.violations)

    # El mismo hallazgo sobre un archivo que sí se envió es válido.
    visible = validate_findings(
        SecurityFindingsProposal.model_validate(
            findings_payload((finding_payload(file=names[0], line=1),))
        ),
        task,
        model_visible_paths=context.visible_set,
        workspace_files=frozenset(names),
    )

    assert visible.valid, visible.violations
    assert [finding.file for finding in visible.findings] == [names[0]]


def test_security_plan_beyond_the_model_budget_blocks(tmp_path: Path) -> None:
    """§9.2: un plan que no cabe en el contexto del modelo no finge haber revisado."""
    workspace, names = many_files(tmp_path, MAX_MODEL_VISIBLE_PATHS + 1)
    task = make_security_task(workspace, changed_files=names, context_files=())
    client = FakeEngine5Client([payload(security_plan_with_targets(names).model_dump(mode="json"))])
    runner = DeepSeekSecurityRunner(client=client)  # type: ignore[arg-type]

    report = runner.evaluate(task)

    assert report.status is SecurityStatus.BLOCKED
    assert BLOCKED_CONTEXT_LIMIT in report.error
    assert report.plan is not None
    assert report.model_visible_files == names[:MAX_MODEL_VISIBLE_PATHS]
    assert report.omitted_paths == (names[-1],)
    # Los checks deterministas siguen corriendo: su evidencia no depende del modelo.
    assert report.executed_checks
    # No se pidieron hallazgos sobre lo que no se envió.
    assert client.calls == 1


def test_security_sends_the_files_the_plan_targets(tmp_path: Path) -> None:
    """§9.2: si el plan apunta al archivo 31, se envía explícitamente y se puede auditar."""
    workspace, names = many_files(tmp_path, MAX_MODEL_VISIBLE_PATHS + 1)
    task = make_security_task(workspace, changed_files=names, context_files=())
    target = names[-1]
    client = FakeEngine5Client(
        [
            payload(security_plan_payload(path=target)),
            payload(
                findings_payload(
                    (finding_payload(file=target, line=1, severity="LOW", title="Observación"),)
                )
            ),
        ]
    )
    runner = DeepSeekSecurityRunner(client=client)  # type: ignore[arg-type]

    report = runner.evaluate(task)

    assert report.model_visible_files == (target,)
    assert report.omitted_paths == ()
    assert report.status is SecurityStatus.PASS
    assert [finding.file for finding in report.findings] == [target]
    assert f"=== {target} ===" in client.prompts[1]


def test_deterministic_finding_survives_outside_the_model_context(tmp_path: Path) -> None:
    """§9.6: la evidencia determinista no necesita que el modelo haya visto el archivo.

    El archivo con el secreto está autorizado pero no es objetivo del plan, así que no llega
    al modelo. El check lo inspecciona igual y el hallazgo se conserva como
    ``DETERMINISTIC_CHECK``.
    """
    workspace, names = many_files(tmp_path, MAX_MODEL_VISIBLE_PATHS)
    leaky = "zz_leaky.py"
    (workspace / leaky).write_text(LEAKY_FILE, encoding="utf-8")
    task = make_security_task(workspace, changed_files=(names[0],), context_files=(leaky,))
    client = FakeEngine5Client(
        [payload(security_plan_payload(path=names[0])), payload(findings_payload())]
    )
    runner = DeepSeekSecurityRunner(client=client)  # type: ignore[arg-type]

    report = runner.evaluate(task)

    assert leaky not in report.model_visible_files
    assert report.status is SecurityStatus.FAIL
    assert {
        finding.source for finding in report.findings if finding.file == leaky
    } == {SecurityFindingSource.DETERMINISTIC_CHECK}


# ---------------------------------------------------------------------------
# §9.3, §9.4 y §9.5: visibilidad en el Reviewer
# ---------------------------------------------------------------------------
def test_reviewer_with_an_incomplete_change_cannot_approve(tmp_path: Path) -> None:
    """§9.3: 31 archivos modificados y solo 30 enviados ⇒ BLOCKED, nunca APPROVED."""
    workspace, names = many_files(tmp_path, MAX_MODEL_VISIBLE_PATHS + 1, prefix="rev")
    task = make_review_task(
        workspace,
        changed_files=names,
        context_files=(),
        qa_report=make_qa_report(),
        security_report=make_security_report(),
    )
    client = FakeEngine5Client([payload(review_payload())])
    runner = DeepSeekReviewerRunner(client=client)  # type: ignore[arg-type]

    report = runner.review(task)

    assert report.status is ReviewStatus.BLOCKED
    assert report.approved is False
    assert BLOCKED_CONTEXT_LIMIT in report.error
    assert report.omitted_files == (names[-1],)
    assert len(report.model_visible_files) == MAX_MODEL_VISIBLE_PATHS
    # Los gates de QA y Security seguían en verde: el bloqueo es por contexto incompleto.
    assert report.gate(ReviewGateName.QA) is not None
    assert report.gate(ReviewGateName.QA).passed is True  # type: ignore[union-attr]
    findings_gate = report.gate(ReviewGateName.REVIEW_FINDINGS)
    assert findings_gate is not None and findings_gate.blocking is True
    # No se pidió una revisión a medias.
    assert client.calls == 0


def test_review_finding_on_an_invisible_file_is_rejected(tmp_path: Path) -> None:
    """§9.4 y §9.5: solo se admite lo visible; el archivo visible sí vale."""
    workspace = build_security_project(
        tmp_path, {"runner.py": CORRECTED_RUNNER, "otro.py": "x = 1\n"}
    )
    task = make_review_task(workspace)
    existing = frozenset({"runner.py", "otro.py"})

    undeclared = validate_review_proposal(
        ReviewProposal.model_validate(
            review_payload((review_finding_payload(file="otro.py"),))
        ),
        task,
        model_visible_paths=frozenset({"runner.py"}),
        existing_paths=existing,
    )
    accepted = validate_review_proposal(
        ReviewProposal.model_validate(
            review_payload((review_finding_payload(file="runner.py"),))
        ),
        task,
        model_visible_paths=frozenset({"runner.py"}),
        existing_paths=existing,
    )

    assert not undeclared.valid
    assert any("fuera del contexto" in item for item in undeclared.violations)
    assert accepted.valid, accepted.violations


def test_review_finding_on_a_declared_but_omitted_file_is_rejected(tmp_path: Path) -> None:
    """§9.4: declarado pero no enviado tampoco vale: el motivo es la visibilidad.

    Es la diferencia que importa. Un archivo puede estar en ``context_files`` y no haber
    llegado nunca al modelo; citarlo entonces sería inventar la revisión.
    """
    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    task = make_review_task(
        workspace, context_files=("runner.py", "omitido.py")
    )
    (workspace / "omitido.py").write_text("x = 1\n", encoding="utf-8")

    omitted = validate_review_proposal(
        ReviewProposal.model_validate(
            review_payload((review_finding_payload(file="omitido.py"),))
        ),
        task,
        model_visible_paths=frozenset({"runner.py"}),
        existing_paths=frozenset({"runner.py", "omitido.py"}),
    )

    assert not omitted.valid
    assert any("fuera del contexto visible" in item for item in omitted.violations)


def test_reviewer_repairs_a_finding_on_an_omitted_context_file(tmp_path: Path) -> None:
    """§9.4: un hallazgo sobre un archivo auxiliar omitido se rechaza y se pide de nuevo.

    Los ``context_files`` auxiliares no bloquean la revisión, pero **no** se pueden citar como
    si se hubieran leído: el rechazo queda auditado y el modelo tiene que corregirlo.
    """
    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    # 35 auxiliares + 1 modificado: el modificado entra siempre, los auxiliares se recortan.
    extra = {
        f"doc{index:02d}.md": f"# Documento {index}\n" for index in range(1, 36)
    }
    for name, content in extra.items():
        (workspace / name).write_text(content, encoding="utf-8")
    omitted = sorted(extra)[-1]
    task = make_review_task(
        workspace,
        changed_files=("runner.py",),
        context_files=tuple(sorted(extra)),
        qa_report=make_qa_report(),
        security_report=make_security_report(),
    )
    bad = review_payload((review_finding_payload(file=omitted),))
    client = FakeEngine5Client([json.dumps(bad), json.dumps(review_payload())])
    audit = AuditLogger()
    runner = DeepSeekReviewerRunner(client=client, audit=audit)  # type: ignore[arg-type]

    report = runner.review(task)

    assert omitted in report.omitted_files
    assert "runner.py" in report.model_visible_files
    assert report.status is ReviewStatus.APPROVED
    assert report.findings == ()
    assert client.calls == 2

    rejections = audit.by_type(AuditEventType.REVIEW_PROPOSAL_REJECTED)
    assert rejections
    violations = dict(rejections[0].metadata).get("violations", ())
    assert any("fuera del contexto visible" in str(item) for item in violations)


def test_empty_visible_context_denies_every_review_finding(tmp_path: Path) -> None:
    """§9.7: sin contexto visible, un hallazgo de revisión sobre un archivo no vale."""
    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    task = make_review_task(workspace)

    rejected = validate_review_proposal(
        ReviewProposal.model_validate(
            review_payload((review_finding_payload(file="runner.py"),))
        ),
        task,
        model_visible_paths=frozenset(),
        existing_paths=frozenset({"runner.py"}),
    )

    assert not rejected.valid
    assert any("fuera del contexto visible" in item for item in rejected.violations)


def test_the_boundary_verdict_is_repeatable(tmp_path: Path) -> None:
    """§9.8: el mismo input da el mismo contexto visible y el mismo veredicto."""
    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    task = make_security_task(workspace)

    def evaluate() -> tuple[str, tuple[str, ...]]:
        client = FakeEngine5Client(
            [payload(security_plan_payload()), payload(findings_payload())]
        )
        report = DeepSeekSecurityRunner(client=client).evaluate(task)  # type: ignore[arg-type]
        return report.status.value, report.model_visible_files

    assert evaluate() == evaluate() == ("PASS", ("runner.py",))


@pytest.mark.parametrize("count", [1, MAX_MODEL_VISIBLE_PATHS, MAX_MODEL_VISIBLE_PATHS + 1])
def test_visible_and_omitted_partition_the_declared_paths(tmp_path: Path, count: int) -> None:
    """Lo visible y lo omitido son una partición exacta: nada se pierde por el camino."""
    workspace, names = many_files(tmp_path, count, prefix=f"part{count}")

    context = build_model_review_context(workspace, names)

    assert set(context.visible_paths) | set(context.omitted_paths) == set(names)
    assert not set(context.visible_paths) & set(context.omitted_paths)
    assert len(context.visible_paths) == min(count, MAX_MODEL_VISIBLE_PATHS)
