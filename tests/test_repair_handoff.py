"""Handoff durable de la reparación (ENGINE-6.1): códecs y fallos tipados.

Lo que se fija aquí, y por qué:

- **ida y vuelta completa** de los cuatro sobres de reparación (plan, diagnóstico, defectos y
  snapshot): el encargo que el kernel publica tiene que reconstruirse **igual** en el proceso que
  ejecuta la reparación, porque entre uno y otro no hay memoria compartida, solo el almacén;
- **nada se entrega a medias**: un artefacto que la referencia declara y el almacén no tiene falla
  con el código estable de reanudación, unos bytes manipulados fallan como checkpoint inválido y un
  sobre que declara otro tipo no se interpreta «lo mejor posible»;
- **sin bytes binarios**: el contenido del almacén es un objeto JSON legible —el registro
  estructurado del encargo—, nunca un binario codificado;
- **el rol importa**: los cuatro códecs se publican con la petición del paso ``DEVELOPER``, que
  es la etapa que repara, y publicarlos con otro rol se rechaza antes de escribir nada.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.repair import (
    RepairDiagnosis,
    RepairFinding,
    RepairPlan,
    RepairSnapshot,
    RepairSnapshotEntry,
)
from punto.schemas.workflow import (
    ArtifactReference,
    RoleExecutionRequest,
    RoleName,
    WorkflowFailureCode,
)
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.errors import WorkflowCheckpointInvalidError, WorkflowResumeFailedError
from punto.workflow.handoff import (
    REPAIR_DIAGNOSIS_KIND,
    REPAIR_FINDINGS_KIND,
    REPAIR_PLAN_KIND,
    REPAIR_SNAPSHOT_KIND,
    publish_repair_diagnosis,
    publish_repair_findings,
    publish_repair_plan,
    publish_repair_snapshot,
    resolve_repair_diagnosis,
    resolve_repair_findings,
    resolve_repair_plan,
    resolve_repair_snapshot,
)
from punto.workflow.repair import build_repair_plan, finding_fingerprint

#: Identidad fija del caso: los artefactos se publican y se resuelven con la misma petición.
_WORKFLOW_ID = UUID("11111111-2222-4333-8444-555555555555")
_TASK_ID = UUID("66666666-7777-4888-8999-000000000000")
_PROJECT_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


def _request(role: RoleName = RoleName.DEVELOPER) -> RoleExecutionRequest:
    """Petición del paso que publica y resuelve los artefactos de reparación."""
    return RoleExecutionRequest(
        workflow_id=_WORKFLOW_ID,
        step_index=4,
        role=role,
        stage=TaskStatus.REPAIRING,
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        objective="reparar los defectos declarados",
        idempotency_key="paso-de-reparacion",
    )


def _store(tmp_path: Path) -> FileArtifactStore:
    """Almacén de artefactos en disco, aislado por prueba."""
    return FileArtifactStore(tmp_path / "artefactos")


def _finding(categoria: str = "CORRECTNESS") -> RepairFinding:
    """Defecto sintético con fingerprint canónico, como los que produce el ciclo."""
    return RepairFinding(
        fingerprint=finding_fingerprint(
            source_role=RoleName.QA,
            code="QA_FALLO",
            category=categoria,
            affected_files=("src/app.py",),
            evidence="la prueba falla",
            acceptance=("la prueba pasa",),
        ),
        source_role=RoleName.QA,
        source_stage=TaskStatus.QA,
        source_step_index=2,
        category=categoria,
        severity=FindingSeverity.HIGH,
        code="QA_FALLO",
        summary="la prueba de normalización falla",
        evidence="assert 1 == 2",
        affected_files=("src/app.py",),
        acceptance_criteria=("la prueba pasa",),
    )


def _plan(*findings: RepairFinding, diagnosis_id: UUID | None = None) -> RepairPlan:
    """Plan de reparación construido por la función de dominio, con su fingerprint real."""
    return build_repair_plan(
        workflow_id=_WORKFLOW_ID,
        cycle=1,
        findings=findings,
        diagnosis_id=diagnosis_id,
        target_files=("src/app.py",),
        allowed_file_globs=("src/**",),
        expected_changes=("la prueba de normalización pasa",),
        acceptance_criteria=("la prueba pasa",),
        verification_roles=(RoleName.QA, RoleName.SECURITY),
        risk=RiskLevel.LOW,
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        policy_decision_id=None,
        budget_model_calls=2,
        budget_total_tokens=4_000,
        idempotency_key="reparacion-ciclo-1",
        strategy="corregir la comparación",
    )


def _diagnosis(finding: RepairFinding) -> RepairDiagnosis:
    """Diagnóstico estructurado del defecto, sin razonamiento privado."""
    return RepairDiagnosis(
        finding_ids=(finding.finding_id,),
        root_cause_summary="la comparación usa el operador equivocado",
        suspected_files=("src/app.py",),
        constraints=("no tocar config/constitution.yaml",),
        proposed_strategy="cambiar el operador y no tocar nada más",
        unknowns=("ninguna",),
    )


def _snapshot(plan: RepairPlan, workspace: Path) -> RepairSnapshot:
    """Estado previo del archivo que la reparación puede tocar."""
    return RepairSnapshot(
        repair_id=plan.repair_id,
        cycle=plan.cycle,
        workspace_path=str(workspace),
        entries=(
            RepairSnapshotEntry(path="src/app.py", sha256="a" * 64, bytes=120),
            RepairSnapshotEntry(path="src/nuevo.py", existed=False),
        ),
    )


def test_el_plan_de_reparacion_vuelve_igual_del_almacen(tmp_path: Path) -> None:
    """El contrato de la reparación sobrevive al viaje por el almacén, campo a campo."""
    store = _store(tmp_path)
    request = _request()
    plan = _plan(_finding())

    reference = publish_repair_plan(store, request=request, plan=plan)

    assert reference.kind == REPAIR_PLAN_KIND
    assert reference.digest and reference.bytes_written > 0
    assert resolve_repair_plan(store, (reference,)) == plan


def test_el_diagnostico_vuelve_igual_del_almacen(tmp_path: Path) -> None:
    """El diagnóstico es dato estructurado: se reconstruye idéntico, no como prosa."""
    store = _store(tmp_path)
    request = _request()
    diagnosis = _diagnosis(_finding())

    reference = publish_repair_diagnosis(store, request=request, diagnosis=diagnosis)

    assert reference.kind == REPAIR_DIAGNOSIS_KIND
    assert resolve_repair_diagnosis(store, (reference,)) == diagnosis


def test_los_defectos_vuelven_iguales_del_almacen(tmp_path: Path) -> None:
    """La lista de defectos se reconstruye entera y en el mismo orden."""
    store = _store(tmp_path)
    request = _request()
    findings = (_finding("CORRECTNESS"), _finding("ROBUSTNESS"))

    reference = publish_repair_findings(store, request=request, findings=findings)

    assert reference.kind == REPAIR_FINDINGS_KIND
    assert resolve_repair_findings(store, (reference,)) == findings


def test_el_snapshot_vuelve_igual_del_almacen(tmp_path: Path) -> None:
    """El estado previo se reconstruye con sus hashes: sin ellos no hay rollback fundado."""
    store = _store(tmp_path)
    request = _request()
    snapshot = _snapshot(_plan(_finding()), tmp_path)

    reference = publish_repair_snapshot(store, request=request, snapshot=snapshot)

    assert reference.kind == REPAIR_SNAPSHOT_KIND
    assert resolve_repair_snapshot(store, (reference,)) == snapshot


def test_sin_referencia_del_tipo_este_paso_no_repara(tmp_path: Path) -> None:
    """Sin referencias del tipo no hay reparación: ``None`` y tupla vacía, nunca un invento."""
    store = _store(tmp_path)

    assert resolve_repair_plan(store, ()) is None
    assert resolve_repair_diagnosis(store, ()) is None
    assert resolve_repair_findings(store, ()) == ()
    assert resolve_repair_snapshot(store, ()) is None


def test_otro_tipo_de_referencia_no_se_confunde_con_la_reparacion(tmp_path: Path) -> None:
    """Una referencia de otro ``kind`` se ignora: resolver es buscar el tipo, no el primero."""
    store = _store(tmp_path)
    request = _request()
    findings = (_finding(),)
    reference = publish_repair_findings(store, request=request, findings=findings)

    assert resolve_repair_plan(store, (reference,)) is None
    assert resolve_repair_findings(store, (reference,)) == findings


def test_un_artefacto_que_falta_falla_con_el_codigo_de_reanudacion(tmp_path: Path) -> None:
    """La referencia está y el contenido no: es un fallo de reanudación, no un ``None``."""
    store = _store(tmp_path)
    reference = publish_repair_plan(store, request=_request(), plan=_plan(_finding()))
    (store.root / reference.reference).unlink()

    with pytest.raises(WorkflowResumeFailedError) as fallo:
        resolve_repair_plan(store, (reference,))

    assert fallo.value.code is WorkflowFailureCode.WORKFLOW_RESUME_FAILED


def test_un_digest_que_no_cuadra_falla_como_checkpoint_invalido(tmp_path: Path) -> None:
    """Unos bytes que no son los declarados son corrupción: no se devuelven como si lo fueran."""
    store = _store(tmp_path)
    reference = publish_repair_snapshot(
        store, request=_request(), snapshot=_snapshot(_plan(_finding()), tmp_path)
    )
    (store.root / reference.reference).write_bytes(b'{"kind":"REPAIR_SNAPSHOT"}')

    with pytest.raises(WorkflowCheckpointInvalidError) as fallo:
        resolve_repair_snapshot(store, (reference,))

    assert fallo.value.code is WorkflowFailureCode.WORKFLOW_CHECKPOINT_INVALID


def test_un_sobre_de_otro_tipo_no_se_interpreta(tmp_path: Path) -> None:
    """Una referencia que dice ``REPAIR_PLAN`` y apunta a defectos se rechaza con su motivo."""
    store = _store(tmp_path)
    findings = (_finding(),)
    published = publish_repair_findings(store, request=_request(), findings=findings)
    impostor = ArtifactReference(
        kind=REPAIR_PLAN_KIND,
        label="contrato de reparación",
        store=published.store,
        reference=published.reference,
        digest=published.digest,
        bytes_written=published.bytes_written,
    )

    with pytest.raises(WorkflowResumeFailedError) as fallo:
        resolve_repair_plan(store, (impostor,))

    assert "no coinciden" in (fallo.value.detail or str(fallo.value))


def test_el_sobre_de_reparacion_es_json_sin_binarios(tmp_path: Path) -> None:
    """Lo que se guarda es el registro estructurado: JSON legible, sin bytes binarios."""
    store = _store(tmp_path)
    request = _request()
    plan = _plan(_finding())
    diagnosis = _diagnosis(_finding())
    snapshot = _snapshot(plan, tmp_path)

    references = (
        publish_repair_plan(store, request=request, plan=plan),
        publish_repair_diagnosis(store, request=request, diagnosis=diagnosis),
        publish_repair_findings(store, request=request, findings=(_finding(),)),
        publish_repair_snapshot(store, request=request, snapshot=snapshot),
    )

    for reference in references:
        sobre = json.loads(store.get(reference).decode("utf-8"))
        assert isinstance(sobre, dict), reference.kind
        assert sobre["kind"] == reference.kind
        assert sobre["schema_version"], reference.kind


def test_ningun_codec_publica_con_otro_rol(tmp_path: Path) -> None:
    """Publicar el encargo con el rol equivocado se rechaza antes de escribir nada."""
    store = _store(tmp_path)
    request = _request(RoleName.QA)
    plan = _plan(_finding())
    diagnosis = _diagnosis(_finding())
    snapshot = _snapshot(plan, tmp_path)

    with pytest.raises(ValueError, match="DEVELOPER"):
        publish_repair_plan(store, request=request, plan=plan)
    with pytest.raises(ValueError, match="DEVELOPER"):
        publish_repair_diagnosis(store, request=request, diagnosis=diagnosis)
    with pytest.raises(ValueError, match="DEVELOPER"):
        publish_repair_findings(store, request=request, findings=(_finding(),))
    with pytest.raises(ValueError, match="DEVELOPER"):
        publish_repair_snapshot(store, request=request, snapshot=snapshot)


def test_los_defectos_publicados_no_llevan_secretos(tmp_path: Path) -> None:
    """La evidencia de un defecto pasa por la redacción del códec antes de viajar."""
    store = _store(tmp_path)
    finding = _finding().model_copy(
        update={"evidence": "curl -H 'Authorization: Bearer sk-abcdef123456' http://x"}
    )

    reference = publish_repair_findings(store, request=_request(), findings=(finding,))

    sobre = store.get(reference).decode("utf-8")
    assert "sk-abcdef123456" not in sobre
    assert "credencial omitida" in sobre
    assert resolve_repair_findings(store, (reference,))[0].fingerprint == finding.fingerprint


def test_una_referencia_inexistente_del_tipo_correcto_no_es_un_artefacto(tmp_path: Path) -> None:
    """Una referencia bien formada pero fabricada no resuelve: el almacén la verifica y falla."""
    store = _store(tmp_path)
    inventada = ArtifactReference(
        kind=REPAIR_DIAGNOSIS_KIND,
        label="diagnóstico de reparación",
        store="file",
        reference=f"{_WORKFLOW_ID}/DEVELOPER-4-{REPAIR_DIAGNOSIS_KIND}-9.bin",
    )

    with pytest.raises(WorkflowResumeFailedError):
        resolve_repair_diagnosis(store, (inventada,))


def test_el_plan_de_reparacion_publicado_declara_su_ciclo(tmp_path: Path) -> None:
    """El plan reconstruido conserva la identidad del ciclo: no es un plan de otro reintento."""
    store = _store(tmp_path)
    plan = _plan(_finding(), diagnosis_id=uuid4())

    reference = publish_repair_plan(store, request=_request(), plan=plan)
    reconstruido = resolve_repair_plan(store, (reference,))

    assert reconstruido is not None
    assert reconstruido.plan_fingerprint == plan.plan_fingerprint
    assert reconstruido.idempotency_key == "reparacion-ciclo-1"
    assert reconstruido.diagnosis_id == plan.diagnosis_id
