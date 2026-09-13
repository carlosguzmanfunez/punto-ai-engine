"""Colecciones acotadas del contrato del workflow (ENGINE-6.0.1, V60-08).

Una ejecución autónoma no puede crecer sin límite: cada colección que entra o sale del kernel tiene
su cota declarada en el contrato, y el borde se prueba en los dos sentidos —el máximo cabe y el
máximo más uno se rechaza—. Sin esta prueba, subir un recorte en el kernel no se notaría hasta que
un checkpoint gigante reventara al guardarse.

Se cubren también los recortes del **camino de escritura** (``record_stage``), porque ahí es donde
el kernel decide cuánto guarda: referencias de más no pueden acabar en el checkpoint.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from punto.schemas.enums import TaskStatus
from punto.schemas.workflow import (
    MAX_ACCEPTANCE_CRITERIA,
    MAX_CHANGED_FILES,
    MAX_CONTEXT_ENTRIES,
    MAX_EFFECT_RECORDS,
    MAX_ROLE_SUPPORT,
    MAX_ROLES_EXECUTED,
    MAX_WORKFLOW_ARTIFACTS,
    MAX_WORKFLOW_CONTEXT_CHARS,
    MAX_WORKFLOW_FINDINGS,
    MAX_WORKFLOW_MODEL_CALLS,
    MAX_WORKFLOW_STATE_VISITS,
    MAX_WORKFLOW_STEPS,
    MAX_WORKFLOW_TEXT_CHARS,
    ArtifactReference,
    EffectRecord,
    ProviderCapability,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    StageArtifacts,
    WorkflowBudget,
    WorkflowFinding,
    WorkflowRequest,
    WorkflowResult,
    WorkflowRun,
)
from punto.workflow.artifacts import FileArtifactStore, record_stage
from workflow_support import make_request, make_run

#: Hallazgo mínimo y válido, para rellenar colecciones acotadas.
FINDING = WorkflowFinding(
    role=RoleName.QA,
    severity="LOW",
    category="CORRECTNESS",
    message="hallazgo sintético de cota",
)


def _reference(index: int) -> ArtifactReference:
    """Referencia distinta por índice, para que la colección no se deduplique."""
    return ArtifactReference(
        kind="k", label=f"etiqueta {index}", store="file", reference=f"r{index}"
    )


def _result(*, references: int = 0, artifacts: int = 0, findings: int = 0) -> RoleExecutionResult:
    """Resultado del rol con las colecciones del tamaño indicado."""
    return RoleExecutionResult(
        role=RoleName.QA,
        status=RoleStatus.COMPLETED,
        artifacts=tuple(f"artefacto-{index}" for index in range(artifacts)),
        artifact_references=tuple(_reference(index) for index in range(references)),
        findings=tuple(FINDING for _ in range(findings)),
    )


def test_the_bounds_are_coherent_with_the_role_catalogue() -> None:
    """Las cotas de roles no son decorativas: cubren a todos los roles del contrato.

    ``MAX_ROLES_EXECUTED`` y ``MAX_ROLE_SUPPORT`` valen más que el número de roles, así que el
    recorte no puede probarse «por arriba» con ``RoleName``: lo que se exige es que **quepan
    todos**.
    """
    assert len(RoleName) <= MAX_ROLES_EXECUTED
    assert len(RoleName) <= MAX_ROLE_SUPPORT
    assert all(
        bound > 0
        for bound in (
            MAX_ACCEPTANCE_CRITERIA,
            MAX_CHANGED_FILES,
            MAX_CONTEXT_ENTRIES,
            MAX_EFFECT_RECORDS,
            MAX_ROLES_EXECUTED,
            MAX_ROLE_SUPPORT,
            MAX_WORKFLOW_ARTIFACTS,
            MAX_WORKFLOW_FINDINGS,
            MAX_WORKFLOW_STEPS,
        )
    )


def test_acceptance_criteria_and_changed_files_are_bounded() -> None:
    """La petición acepta el máximo exacto y rechaza un elemento más."""
    request = make_request(
        acceptance_criteria=tuple(
            f"criterio {index}" for index in range(MAX_ACCEPTANCE_CRITERIA)
        ),
        changed_files=tuple(f"archivo_{index}.py" for index in range(MAX_CHANGED_FILES)),
    )
    assert len(request.acceptance_criteria) == MAX_ACCEPTANCE_CRITERIA
    assert len(request.changed_files) == MAX_CHANGED_FILES

    with pytest.raises(ValidationError):
        make_request(
            acceptance_criteria=tuple(
                f"criterio {index}" for index in range(MAX_ACCEPTANCE_CRITERIA + 1)
            )
        )
    with pytest.raises(ValidationError):
        make_request(
            changed_files=tuple(f"a{index}.py" for index in range(MAX_CHANGED_FILES + 1))
        )


def test_the_text_fields_of_a_request_are_bounded() -> None:
    """Los textos de la petición tienen tope: un contexto desmedido no entra al run."""
    assert WorkflowRequest(
        task_id=make_request().task_id,
        project_id=make_request().project_id,
        objective="o" * MAX_WORKFLOW_TEXT_CHARS,
        action="create_file",
        context_summary="c" * MAX_WORKFLOW_CONTEXT_CHARS,
        idempotency_key="clave-de-cota",
    )
    with pytest.raises(ValidationError):
        make_request(objective="o" * (MAX_WORKFLOW_TEXT_CHARS + 1))
    with pytest.raises(ValidationError):
        make_request(context_summary="c" * (MAX_WORKFLOW_CONTEXT_CHARS + 1))


def test_role_result_collections_are_bounded() -> None:
    """Los artefactos y los hallazgos de un rol tienen cota, en los dos bordes."""
    result = _result(
        references=MAX_WORKFLOW_ARTIFACTS,
        artifacts=MAX_WORKFLOW_ARTIFACTS,
        findings=MAX_WORKFLOW_FINDINGS,
    )
    assert len(result.artifacts) == MAX_WORKFLOW_ARTIFACTS
    assert len(result.artifact_references) == MAX_WORKFLOW_ARTIFACTS
    assert len(result.findings) == MAX_WORKFLOW_FINDINGS

    over = MAX_WORKFLOW_ARTIFACTS + 1
    with pytest.raises(ValidationError):
        _result(artifacts=over)
    with pytest.raises(ValidationError):
        _result(references=over)
    with pytest.raises(ValidationError):
        _result(findings=MAX_WORKFLOW_FINDINGS + 1)


def test_run_handoff_and_effect_collections_are_bounded() -> None:
    """El handoff durable y el libro de efectos no crecen por encima de su cota.

    Se construye el ``WorkflowRun`` directamente: ``model_copy`` **no** valida, así que una cota
    comprobada con una copia no probaría nada.
    """
    base = make_run()
    entries = tuple(
        StageArtifacts(role=RoleName.QA, stage=TaskStatus.QA, step_index=index)
        for index in range(MAX_CONTEXT_ENTRIES)
    )
    effects = tuple(
        EffectRecord(
            idempotency_key=f"clave-{index}",
            action="create_file",
            role=RoleName.DEVELOPER,
            step_index=index,
        )
        for index in range(MAX_EFFECT_RECORDS)
    )
    run = WorkflowRun(
        workflow_id=base.workflow_id,
        request=base.request,
        stage_artifacts=entries,
        effects=effects,
    )
    assert len(run.stage_artifacts) == MAX_CONTEXT_ENTRIES
    assert len(run.effects) == MAX_EFFECT_RECORDS

    with pytest.raises(ValidationError):
        WorkflowRun(
            workflow_id=base.workflow_id,
            request=base.request,
            stage_artifacts=(
                *entries,
                StageArtifacts(role=RoleName.QA, stage=TaskStatus.QA, step_index=99),
            ),
        )
    with pytest.raises(ValidationError):
        WorkflowRun(
            workflow_id=base.workflow_id,
            request=base.request,
            effects=(
                *effects,
                EffectRecord(
                    idempotency_key="clave-de-mas",
                    action="create_file",
                    role=RoleName.DEVELOPER,
                    step_index=99,
                ),
            ),
        )


def test_the_declared_capability_and_the_final_result_fit_their_bounds() -> None:
    """La capacidad declarada y el resultado final caben en sus cotas con todos los roles."""
    capability = ProviderCapability(
        provider="deepseek",
        role_support=tuple(RoleName),
    )
    result = WorkflowResult(status=TaskStatus.COMPLETED, roles_executed=tuple(RoleName))

    assert capability.supports(RoleName.VISUAL_QA)
    assert len(result.roles_executed) == len(RoleName)


def test_the_budget_rejects_values_outside_its_own_bounds() -> None:
    """Los topes del presupuesto son explícitos: ni cero pasos ni cotas desmedidas."""
    assert WorkflowBudget(max_steps=MAX_WORKFLOW_STEPS).max_steps == MAX_WORKFLOW_STEPS
    assert WorkflowBudget(max_model_calls=0).max_model_calls == 0
    assert (
        WorkflowBudget(max_state_visits=MAX_WORKFLOW_STATE_VISITS).max_state_visits
        == MAX_WORKFLOW_STATE_VISITS
    )

    with pytest.raises(ValidationError):
        WorkflowBudget(max_steps=0)
    with pytest.raises(ValidationError):
        WorkflowBudget(max_steps=MAX_WORKFLOW_STEPS + 1)
    with pytest.raises(ValidationError):
        WorkflowBudget(max_state_visits=MAX_WORKFLOW_STATE_VISITS + 1)
    with pytest.raises(ValidationError):
        WorkflowBudget(max_model_calls=MAX_WORKFLOW_MODEL_CALLS + 1)


def test_record_stage_trims_the_references_it_persists(tmp_path: Path) -> None:
    """El camino de escritura recorta: una etapa nunca guarda más referencias que la cota.

    El contrato ya impide pasar más de ``MAX_WORKFLOW_ARTIFACTS`` en una sola colección, así que el
    exceso solo puede venir de **sumar** fuentes: aquí, payloads del almacén más las referencias
    declaradas por el rol.
    """
    store = FileArtifactStore(tmp_path / "artifacts")
    run = make_run()
    payloads = {f"kind{index:02d}": b"contenido" for index in range(30)}
    reported = _result(artifacts=30)
    updated = record_stage(
        run,
        role=RoleName.QA,
        stage=TaskStatus.QA,
        step_index=0,
        result=reported,
        store=store,
        payloads=payloads,
    )

    assert len(updated.stage_artifacts) == 1
    entry = updated.stage_artifacts[0]
    assert len(entry.references) == MAX_WORKFLOW_ARTIFACTS
    assert sum(1 for item in entry.references if item.kind != "reported") == 30
    assert len(reported.artifacts) == 30


def test_record_stage_keeps_the_most_recent_stages_only() -> None:
    """La lista de etapas del run conserva las más recientes hasta su propia cota."""
    run = make_run()
    for index in range(MAX_CONTEXT_ENTRIES + 5):
        run = record_stage(
            run,
            role=RoleName.QA,
            stage=TaskStatus.QA,
            step_index=index,
            result=_result(artifacts=1),
        )

    assert len(run.stage_artifacts) == MAX_CONTEXT_ENTRIES
    assert run.stage_artifacts[-1].step_index == MAX_CONTEXT_ENTRIES + 4
    assert run.stage_artifacts[0].step_index == 5
