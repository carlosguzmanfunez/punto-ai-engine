"""Pruebas del handoff estructurado y durable entre etapas (ENGINE-6.0, V60-04).

Lo que se demuestra aquí es el problema exacto que motivó el módulo: un workflow **reanudado en un
proceso nuevo** puede reconstruir su contexto sin ninguna variable del proceso que ejecutó las
etapas anteriores. Para que la prueba signifique algo, el run no se reutiliza por referencia: se
serializa con ``model_dump_json``, se vuelve a cargar con ``model_validate_json`` y se comprueba
que el texto de handoff es idéntico.

Todo con ``tmp_path``: ningún test escribe en el directorio del proyecto.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from punto.schemas.enums import FindingSeverity, TaskStatus
from punto.schemas.workflow import (
    MAX_CONTEXT_ENTRIES,
    MAX_WORKFLOW_ARTIFACTS,
    MAX_WORKFLOW_CONTEXT_CHARS,
    MAX_WORKFLOW_SUMMARY_CHARS,
    ArtifactReference,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowFinding,
    WorkflowRequest,
    WorkflowRun,
)
from punto.workflow.artifacts import (
    REPORTED_KIND,
    REPORTED_STORE,
    STORE_NAME,
    FileArtifactStore,
    WorkflowContext,
    build_role_context,
    record_stage,
    summarize_result,
)
from punto.workflow.errors import (
    WorkflowCheckpointInvalidError,
    WorkflowError,
    WorkflowResumeFailedError,
)

#: Subdirectorio del almacén dentro del ``tmp_path`` de cada prueba.
ARTIFACT_DIRNAME = "artifacts"

#: Marca que identifica el contenido íntegro de un payload grande. Si aparece en el contexto, el
#: handoff está arrastrando contenido en vez de una referencia.
BIG_PAYLOAD_MARKER = "MARCADOR-DEL-PAYLOAD-INTEGRO"


def make_run() -> WorkflowRun:
    """Run mínimo y válido para las pruebas de handoff.

    Se construye aquí, y no en un soporte compartido, para que la prueba no dependa de nada más
    que del contrato congelado: lo que se ejercita es la reconstrucción desde el propio run.
    """
    return WorkflowRun(
        workflow_id=uuid4(),
        request=WorkflowRequest(
            task_id=uuid4(),
            project_id=uuid4(),
            objective="implementar el handoff estructurado entre etapas",
            action="workflow_artifact_handoff",
            acceptance_criteria=("el handoff se reconstruye en un proceso nuevo",),
            workspace_path=".",
            context_summary="proyecto sintético de prueba",
            idempotency_key="wf-artifacts-test",
        ),
    )


def make_finding(
    role: RoleName,
    *,
    severity: FindingSeverity = FindingSeverity.MEDIUM,
    message: str = "detalle sintético",
) -> WorkflowFinding:
    """Hallazgo normalizado con la gravedad indicada."""
    return WorkflowFinding(
        role=role,
        severity=severity,
        category="CORRECTNESS",
        message=message,
        evidence="evidencia sintética",
    )


def make_store(tmp_path: Path) -> FileArtifactStore:
    """Almacén local aislado en el ``tmp_path`` de la prueba."""
    return FileArtifactStore(tmp_path / ARTIFACT_DIRNAME)


def make_result(
    role: RoleName = RoleName.DEVELOPER,
    *,
    status: RoleStatus = RoleStatus.COMPLETED,
    summary: str = "resumen sintético del rol",
    findings: tuple[WorkflowFinding, ...] = (),
    artifacts: tuple[str, ...] = (),
) -> RoleExecutionResult:
    """Resultado de rol mínimo, con lo que el handoff debe copiar."""
    return RoleExecutionResult(
        role=role,
        status=status,
        summary=summary,
        findings=findings,
        artifacts=artifacts,
    )


def stored_path(tmp_path: Path, reference: ArtifactReference) -> Path:
    """Ruta en disco del artefacto referenciado (la referencia es relativa a la raíz)."""
    return tmp_path / ARTIFACT_DIRNAME / reference.reference


# ---------------------------------------------------------------------------
# record_stage
# ---------------------------------------------------------------------------
def test_record_stage_sin_payloads_copia_resumen_hallazgos_y_referencias() -> None:
    run = make_run()
    findings = (
        make_finding(RoleName.DEVELOPER, severity=FindingSeverity.MEDIUM),
        make_finding(
            RoleName.DEVELOPER,
            severity=FindingSeverity.HIGH,
            message="falta el caso límite",
        ),
    )
    result = make_result(
        RoleName.DEVELOPER,
        summary="implementado el normalizador",
        findings=findings,
        artifacts=("informe.md", "diff.patch"),
    )

    updated = record_stage(
        run,
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
        step_index=4,
        result=result,
    )

    # El run de entrada es inmutable: no se toca, se copia.
    assert run.stage_artifacts == ()
    assert updated is not run
    assert len(updated.stage_artifacts) == 1

    entry = updated.stage_artifacts[0]
    assert entry.role is RoleName.DEVELOPER
    assert entry.stage is TaskStatus.IN_PROGRESS
    assert entry.step_index == 4
    assert entry.summary == "implementado el normalizador"
    assert entry.findings == findings

    # Las cadenas que el rol reporta son referencias acotadas, no contenido.
    assert [reference.kind for reference in entry.references] == [
        REPORTED_KIND,
        REPORTED_KIND,
    ]
    assert [reference.reference for reference in entry.references] == [
        "informe.md",
        "diff.patch",
    ]
    assert {reference.store for reference in entry.references} == {REPORTED_STORE}
    assert {reference.digest for reference in entry.references} == {""}


def test_record_stage_con_payloads_sube_al_almacen_y_guarda_referencias(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    run = make_run()
    payloads = {"plan": b"plan de la etapa", "code": b"print('hola')\n"}

    updated = record_stage(
        run,
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
        step_index=2,
        result=make_result(RoleName.DEVELOPER, artifacts=("informe.md",)),
        store=store,
        payloads=payloads,
    )

    entry = updated.stage_artifacts[0]
    # Orden determinista por tipo, y luego las referencias que reporta el rol.
    assert [reference.kind for reference in entry.references] == [
        "code",
        "plan",
        REPORTED_KIND,
    ]
    for reference in entry.references[:2]:
        expected = payloads[reference.kind]
        assert reference.store == STORE_NAME
        assert reference.label == reference.kind
        assert reference.digest == hashlib.sha256(expected).hexdigest()
        assert reference.bytes_written == len(expected)
        # Ida y vuelta real por el almacén: el contenido se recupera desde el fichero.
        assert store.get(reference) == expected
        assert stored_path(tmp_path, reference).is_file()

    # Nada de contenido dentro del run: solo referencias.
    serialized = updated.model_dump_json()
    assert "print('hola')" not in serialized
    assert "plan de la etapa" not in serialized
    assert entry.references[0].digest in serialized

    # Con almacén pero sin payloads no se sube nada y no es un error.
    without_payloads = record_stage(
        run,
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
        step_index=2,
        result=make_result(RoleName.DEVELOPER),
        store=store,
    )
    assert without_payloads.stage_artifacts[0].references == ()


def test_las_etapas_del_run_se_acotan_a_max_context_entries() -> None:
    run = make_run()
    for step_index in range(MAX_CONTEXT_ENTRIES + 6):
        run = record_stage(
            run,
            role=RoleName.DEVELOPER,
            stage=TaskStatus.IN_PROGRESS,
            step_index=step_index,
            result=make_result(RoleName.DEVELOPER, summary=f"paso {step_index}"),
        )
    assert len(run.stage_artifacts) == MAX_CONTEXT_ENTRIES
    # Se conservan las más recientes: el handoff mira hacia delante, no hacia el origen.
    assert run.stage_artifacts[-1].step_index == MAX_CONTEXT_ENTRIES + 5
    assert run.stage_artifacts[0].step_index == 6


def test_record_stage_acota_las_referencias_y_pone_los_payloads_primero(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    run = make_run()
    reported = tuple(f"informe-{index}.md" for index in range(MAX_WORKFLOW_ARTIFACTS))

    updated = record_stage(
        run,
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
        step_index=1,
        result=make_result(RoleName.DEVELOPER, artifacts=reported),
        store=store,
        payloads={"plan": b"plan", "code": b"codigo"},
    )

    references = updated.stage_artifacts[0].references
    assert len(references) == MAX_WORKFLOW_ARTIFACTS
    assert [reference.kind for reference in references[:2]] == ["code", "plan"]
    assert all(reference.kind == REPORTED_KIND for reference in references[2:])


# ---------------------------------------------------------------------------
# FileArtifactStore
# ---------------------------------------------------------------------------
def test_ida_y_vuelta_por_file_artifact_store_verifica_el_digest(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    run = make_run()
    data = b"contenido del artefacto de la etapa anterior\n"

    reference = store.put(
        workflow_id=run.workflow_id,
        role=RoleName.ARCHITECT,
        step_index=1,
        kind="plan",
        label="Plan inicial",
        data=data,
    )

    assert reference.store == STORE_NAME
    assert reference.kind == "plan"
    assert reference.label == "Plan inicial"
    assert reference.reference == f"{run.workflow_id}/ARCHITECT-1-plan-0.bin"
    assert reference.digest == hashlib.sha256(data).hexdigest()
    assert reference.bytes_written == len(data)
    assert store.root == tmp_path / ARTIFACT_DIRNAME

    assert store.get(reference) == data

    # Un segundo artefacto del mismo paso no pisa al primero: ordinal siguiente y ambos legibles.
    second = store.put(
        workflow_id=run.workflow_id,
        role=RoleName.ARCHITECT,
        step_index=1,
        kind="plan",
        label="Plan revisado",
        data=b"segundo",
    )
    assert second.reference == f"{run.workflow_id}/ARCHITECT-1-plan-1.bin"
    assert store.get(reference) == data
    assert store.get(second) == b"segundo"


def test_digest_manipulado_levanta_workflow_error(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    run = make_run()
    reference = store.put(
        workflow_id=run.workflow_id,
        role=RoleName.DEVELOPER,
        step_index=3,
        kind="code",
        label="codigo",
        data=b"contenido original",
    )

    # 1) El fichero cambia bajo los pies: el digest calculado ya no es el declarado.
    stored_path(tmp_path, reference).write_bytes(b"contenido manipulado")
    with pytest.raises(WorkflowCheckpointInvalidError):
        store.get(reference)

    # 2) La referencia miente sobre el digest, aunque el contenido sea el original.
    stored_path(tmp_path, reference).write_bytes(b"contenido original")
    falsified = reference.model_copy(update={"digest": "0" * 64})
    with pytest.raises(WorkflowError):
        store.get(falsified)

    # 3) El tamaño declarado tampoco se acepta como decorativo.
    wrong_size = reference.model_copy(update={"bytes_written": reference.bytes_written + 1})
    with pytest.raises(WorkflowCheckpointInvalidError):
        store.get(wrong_size)


def test_artefacto_ausente_o_referencia_ajena_levanta_workflow_error(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    run = make_run()
    reference = store.put(
        workflow_id=run.workflow_id,
        role=RoleName.QA,
        step_index=1,
        kind="report",
        label="informe",
        data=b"qa",
    )
    stored_path(tmp_path, reference).unlink()

    with pytest.raises(WorkflowResumeFailedError):
        store.get(reference)

    # Una referencia de otro almacén no se puede servir desde aquí.
    foreign = reference.model_copy(update={"store": "otro-almacen"})
    with pytest.raises(WorkflowResumeFailedError):
        store.get(foreign)

    # Una referencia con pinta de escaparse de la raíz se rechaza antes de leer.
    escaping = reference.model_copy(update={"reference": f"{run.workflow_id}/../../escape.bin"})
    with pytest.raises(WorkflowCheckpointInvalidError):
        store.get(escaping)


def test_rutas_saneadas_rechazan_workflow_id_kind_y_paso_invalidos(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    role = RoleName.DEVELOPER

    for kind in ("../../etc/passwd", "con espacio", "con/slash", ""):
        with pytest.raises(ValueError):
            store.put(
                workflow_id=make_run().workflow_id,
                role=role,
                step_index=0,
                kind=kind,
                label="x",
                data=b"x",
            )

    with pytest.raises(ValueError):
        store.put(
            workflow_id=make_run().workflow_id,
            role=role,
            step_index=-1,
            kind="plan",
            label="x",
            data=b"x",
        )

    # El ``workflow_id`` se valida aunque llegue como texto: se salta la anotación a propósito
    # para comprobar que el saneado no confía en el tipo declarado.
    with pytest.raises(ValueError):
        store.put(
            workflow_id="../../escape",  # type: ignore[arg-type]
            role=role,
            step_index=0,
            kind="plan",
            label="x",
            data=b"x",
        )

    # Ningún rechazo llegó a crear el directorio del almacén.
    assert not (tmp_path / ARTIFACT_DIRNAME).exists()


# ---------------------------------------------------------------------------
# WorkflowContext
# ---------------------------------------------------------------------------
def test_workflow_context_for_role_latest_y_references(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    run = make_run()
    run = record_stage(
        run,
        role=RoleName.ARCHITECT,
        stage=TaskStatus.ANALYZING,
        step_index=1,
        result=make_result(RoleName.ARCHITECT, summary="análisis inicial"),
        store=store,
        payloads={"plan": b"analisis"},
    )
    run = record_stage(
        run,
        role=RoleName.PLANNER,
        stage=TaskStatus.PLANNING,
        step_index=2,
        result=make_result(RoleName.PLANNER, summary="plan de la tarea"),
        store=store,
        payloads={"plan": b"plan"},
    )
    run = record_stage(
        run,
        role=RoleName.ARCHITECT,
        stage=TaskStatus.ANALYZING,
        step_index=3,
        result=make_result(RoleName.ARCHITECT, summary="reanálisis"),
        store=store,
        payloads={"plan": b"reanalisis"},
    )

    context = WorkflowContext(run)

    assert context.entries() == run.stage_artifacts
    assert [entry.step_index for entry in context.for_role(RoleName.ARCHITECT)] == [1, 3]
    assert [entry.step_index for entry in context.for_role(RoleName.PLANNER)] == [2]
    assert context.for_role(RoleName.SECURITY) == ()

    latest = context.latest(RoleName.ARCHITECT)
    assert latest is not None
    assert latest.step_index == 3
    assert latest.summary == "reanálisis"
    planner = context.latest(RoleName.PLANNER)
    assert planner is not None
    assert planner.step_index == 2
    assert context.latest(RoleName.SECURITY) is None

    references = context.references("plan")
    assert [reference.digest for reference in references] == [
        hashlib.sha256(b"analisis").hexdigest(),
        hashlib.sha256(b"plan").hexdigest(),
        hashlib.sha256(b"reanalisis").hexdigest(),
    ]
    assert context.references("code") == ()


def test_handoff_text_sin_etapas_es_vacio_y_respeta_el_limite(tmp_path: Path) -> None:
    run = make_run()
    assert WorkflowContext(run).handoff_text(for_role=RoleName.DEVELOPER) == ""

    store = make_store(tmp_path)
    recorded = record_stage(
        run,
        role=RoleName.ARCHITECT,
        stage=TaskStatus.ANALYZING,
        step_index=1,
        result=make_result(RoleName.ARCHITECT, summary="a" * MAX_WORKFLOW_SUMMARY_CHARS),
        store=store,
        payloads={"plan": b"contenido"},
    )

    text = WorkflowContext(recorded).handoff_text(for_role=RoleName.DEVELOPER, limit=120)
    assert 0 < len(text) <= 120
    # El recorte es explícito: un texto recortado en silencio se leería como completo.
    assert text.endswith("[recortado]")
    assert "a" * MAX_WORKFLOW_SUMMARY_CHARS not in text


# ---------------------------------------------------------------------------
# Reconstrucción en un proceso nuevo
# ---------------------------------------------------------------------------
def test_build_role_context_identico_tras_reconstruir_el_run_desde_json(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    run = make_run()
    run = record_stage(
        run,
        role=RoleName.ARCHITECT,
        stage=TaskStatus.ANALYZING,
        step_index=1,
        result=make_result(RoleName.ARCHITECT, summary="análisis de la arquitectura"),
        store=store,
        payloads={"plan": b"plan del arquitecto"},
    )
    run = record_stage(
        run,
        role=RoleName.PLANNER,
        stage=TaskStatus.PLANNING,
        step_index=2,
        result=make_result(RoleName.PLANNER, summary="plan de ejecución"),
        store=store,
        payloads={"plan": b"plan del planner"},
    )

    before = build_role_context(run, role=RoleName.DEVELOPER)
    assert before != ""
    assert "ARCHITECT" in before
    assert "PLANNER" in before
    assert "análisis de la arquitectura" in before
    assert "plan de ejecución" in before

    # Proceso nuevo: nada de variables en memoria, solo el JSON del checkpoint.
    serialized = run.model_dump_json()
    assert "plan del arquitecto" not in serialized
    reloaded = WorkflowRun.model_validate_json(serialized)

    after = build_role_context(reloaded, role=RoleName.DEVELOPER)
    assert after == before

    # Y el contenido íntegro se recupera solo con las referencias del run reconstruido.
    fresh_store = FileArtifactStore(tmp_path / ARTIFACT_DIRNAME)
    recovered = [
        fresh_store.get(reference)
        for entry in WorkflowContext(reloaded).entries()
        for reference in entry.references
    ]
    assert recovered == [b"plan del arquitecto", b"plan del planner"]


def test_el_contexto_no_contiene_el_payload_integro_solo_su_resumen_y_referencia(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    run = make_run()
    big = ((BIG_PAYLOAD_MARKER + "-" + "x" * 997 + "\n") * 20).encode("utf-8")
    summary = "resumen acotado del artefacto grande"

    recorded = record_stage(
        run,
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
        step_index=1,
        result=make_result(
            RoleName.DEVELOPER,
            summary=summary,
            artifacts=("diff.patch",),
        ),
        store=store,
        payloads={"code": big},
    )

    context = build_role_context(recorded, role=RoleName.QA)

    assert summary in context
    assert "diff.patch" in context
    assert hashlib.sha256(big).hexdigest() in context
    assert BIG_PAYLOAD_MARKER not in context
    assert len(context) <= MAX_WORKFLOW_CONTEXT_CHARS
    assert len(big) > MAX_WORKFLOW_CONTEXT_CHARS

    # El checkpoint tampoco lo lleva, y el contenido sigue disponible por referencia.
    assert BIG_PAYLOAD_MARKER not in recorded.model_dump_json()
    reference = WorkflowContext(recorded).references("code")[0]
    assert store.get(reference) == big


# ---------------------------------------------------------------------------
# summarize_result y validaciones de entrada
# ---------------------------------------------------------------------------
def test_summarize_result_resume_y_acota() -> None:
    result = make_result(
        RoleName.REVIEWER,
        status=RoleStatus.NEEDS_REPAIR,
        summary="falta validar el caso límite",
        findings=(
            make_finding(RoleName.REVIEWER, severity=FindingSeverity.HIGH),
            make_finding(RoleName.REVIEWER, severity=FindingSeverity.LOW),
        ),
        artifacts=("informe.md",),
    )

    text = summarize_result(result)

    assert "REVIEWER" in text
    assert "NEEDS_REPAIR" in text
    assert "falta validar el caso límite" in text
    assert "hallazgos: 2 (bloqueantes: 1)" in text
    assert "artefactos reportados: 1" in text
    assert len(text) <= MAX_WORKFLOW_SUMMARY_CHARS

    long_result = make_result(summary="s" * MAX_WORKFLOW_SUMMARY_CHARS)
    assert len(summarize_result(long_result, limit=80)) <= 80
    assert summarize_result(long_result, limit=0) == ""


def test_payloads_sin_store_y_rol_incorrecto_levantan_value_error(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    run = make_run()
    result = make_result(RoleName.DEVELOPER)

    with pytest.raises(ValueError):
        record_stage(
            run,
            role=RoleName.DEVELOPER,
            stage=TaskStatus.IN_PROGRESS,
            step_index=1,
            result=result,
            payloads={"code": b"x"},
        )

    with pytest.raises(ValueError):
        record_stage(
            run,
            role=RoleName.QA,
            stage=TaskStatus.QA,
            step_index=1,
            result=result,
            store=store,
        )

    # Un mapa vacío no es un payload: no hay nada que subir ni error que dar.
    updated = record_stage(
        run,
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
        step_index=1,
        result=result,
        payloads={},
    )
    assert len(updated.stage_artifacts) == 1
