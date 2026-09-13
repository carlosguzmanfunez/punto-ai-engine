"""Pruebas del checkpointing local e idempotente del kernel (ENGINE-6.0 §17 a §20).

Se ejercita el comportamiento real: ficheros en disco, escritura atómica, detección de
corrupción, reanudación sin repetir etapas y la garantía de que un workflow no puede quedar
falsamente ``COMPLETED``. Todo con ``tmp_path``: ningún test toca el directorio del proyecto.

La carga es de confianza cero (hallazgo V60-06): ``load`` valida **siempre** la integridad de los
metadatos contra los bytes y la coherencia entre run y checkpoint, así que aquí se manipulan los
metadatos y el contenido uno a uno para comprobar que ninguna anomalía pasa en silencio y que
nunca se retrocede al checkpoint anterior.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from punto.common import utc_now
from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.planning import SCHEMA_VERSION
from punto.schemas.workflow import (
    ArtifactReference,
    EffectRecord,
    EffectStatus,
    RoleName,
    RoleStatus,
    StageArtifacts,
    WorkflowCheckpoint,
    WorkflowDecisionKind,
    WorkflowFailureCode,
    WorkflowFinding,
    WorkflowRequest,
    WorkflowResult,
    WorkflowRun,
    WorkflowStep,
)
from punto.workflow.checkpoints import (
    FileCheckpointStore,
    completed_idempotency_keys,
    next_pending_step,
    step_idempotency_key,
    validate_checkpoint,
)
from punto.workflow.errors import WorkflowCheckpointInvalidError, WorkflowResumeFailedError

#: Subdirectorio de checkpoints dentro del ``tmp_path`` de cada prueba.
CHECKPOINT_DIRNAME = "checkpoints"

#: Contexto normal y representativo de una etapa: sin secretos, como exige el contrato.
NORMAL_CONTEXT = "Contexto de la etapa: objetivo, criterios de aceptación y rutas del proyecto."

#: Acción canónica de la petición: el contrato exige declararla, no se inventa.
REQUEST_ACTION = "modify_file"

#: Cadenas que jamás deben aparecer en un fichero de checkpoint.
FORBIDDEN_IN_CHECKPOINTS = (
    "API_KEY",
    "SECRET",
    "api_key",
    "sk-",
    "Bearer",
    "password",
    "credential",
)


def make_run(
    *,
    workflow_id: UUID | None = None,
    revision: int = 0,
    status: TaskStatus = TaskStatus.NEW,
    steps: tuple[WorkflowStep, ...] = (),
    result: WorkflowResult | None = None,
    completed_at: datetime | None = None,
    context_summary: str = NORMAL_CONTEXT,
    schema_version: str = SCHEMA_VERSION,
    action: str = REQUEST_ACTION,
    project_path: str = "",
    request_fingerprint: str = "",
    stage_artifacts: tuple[StageArtifacts, ...] = (),
    effects: tuple[EffectRecord, ...] = (),
    policy_decision_id: UUID | None = None,
    effective_authority: AuthorityLevel | None = None,
    effective_risk: RiskLevel | None = None,
) -> WorkflowRun:
    """Run mínimo y válido para las pruebas de checkpointing, con el contrato completo."""
    identifier = workflow_id if workflow_id is not None else uuid4()
    return WorkflowRun(
        workflow_id=identifier,
        schema_version=schema_version,
        request=WorkflowRequest(
            task_id=uuid4(),
            project_id=uuid4(),
            objective="Implementar el checkpointing determinista del kernel",
            action=action,
            acceptance_criteria=("el run se reanuda sin repetir etapas",),
            context_summary=context_summary,
            project_path=project_path,
            idempotency_key=f"request:{identifier}",
        ),
        status=status,
        revision=revision,
        steps=steps,
        request_fingerprint=request_fingerprint,
        stage_artifacts=stage_artifacts,
        effects=effects,
        policy_decision_id=policy_decision_id,
        effective_authority=effective_authority,
        effective_risk=effective_risk,
        result=result,
        completed_at=completed_at,
    )


def make_step(
    workflow_id: UUID,
    index: int,
    role: RoleName,
    stage: TaskStatus,
    *,
    status: RoleStatus = RoleStatus.COMPLETED,
) -> WorkflowStep:
    """Paso de workflow con la clave de idempotencia que produce el contrato."""
    return WorkflowStep(
        index=index,
        role=role,
        stage=stage,
        status=status,
        idempotency_key=step_idempotency_key(workflow_id, index, role, stage),
        decision=WorkflowDecisionKind.CONTINUE,
    )


def make_stage_artifacts(step_index: int = 0) -> StageArtifacts:
    """Handoff estructurado de una etapa, con referencias y un hallazgo real."""
    return StageArtifacts(
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
        step_index=step_index,
        summary="La etapa deja el módulo listo para QA.",
        references=(
            ArtifactReference(
                kind="patch",
                label="diff del módulo",
                store="artifacts",
                reference="art-0001",
                digest="a" * 64,
                bytes_written=1_234,
            ),
            ArtifactReference(kind="log", label="salida de pruebas", reference="log-0001"),
        ),
        findings=(
            WorkflowFinding(
                role=RoleName.DEVELOPER,
                severity=FindingSeverity.LOW,
                category="STYLE",
                message="hallazgo no bloqueante de la etapa",
                evidence="evidencia de la etapa",
            ),
        ),
    )


def make_effect(step_index: int = 0) -> EffectRecord:
    """Efecto ya resuelto, con la intención durable que impide repetirlo a ciegas."""
    return EffectRecord(
        idempotency_key=f"effect:{step_index}",
        action=REQUEST_ACTION,
        role=RoleName.DEVELOPER,
        step_index=step_index,
        status=EffectStatus.APPLIED,
        reversible=True,
        detail="escritura aplicada una sola vez",
        resolved_at=utc_now(),
    )


def run_path(root: Path, workflow_id: UUID, sequence: int) -> Path:
    """Ruta del contenido de un checkpoint, tal como la fija el formato en disco."""
    return root / str(workflow_id) / f"{sequence:04d}.json"


def meta_path(root: Path, workflow_id: UUID, sequence: int) -> Path:
    """Ruta de los metadatos de un checkpoint."""
    return root / str(workflow_id) / "meta" / f"{sequence:04d}.json"


def serialize(run: WorkflowRun) -> bytes:
    """Bytes exactos que el store debe escribir para un run."""
    return (run.model_dump_json(indent=2) + "\n").encode("utf-8")


def serialize_meta(checkpoint: WorkflowCheckpoint) -> bytes:
    """Bytes exactos que el store debe escribir para unos metadatos."""
    return (checkpoint.model_dump_json(indent=2) + "\n").encode("utf-8")


def write_meta(
    root: Path,
    workflow_id: UUID,
    sequence: int,
    checkpoint: WorkflowCheckpoint,
    updates: dict[str, Any] | None = None,
) -> WorkflowCheckpoint:
    """Reescribe los metadatos en disco con los cambios pedidos, como haría una manipulación."""
    patched = checkpoint if updates is None else checkpoint.model_copy(update=updates)
    meta_path(root, workflow_id, sequence).write_bytes(serialize_meta(patched))
    return patched


# ---------------------------------------------------------------------------
# Persistencia: guardar, cargar y metadatos
# ---------------------------------------------------------------------------
def test_guardar_y_cargar_devuelve_exactamente_el_mismo_run(tmp_path: Path) -> None:
    """El ciclo guardar/cargar es una identidad: ni se pierde ni se inventa estado."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    run = make_run(
        workflow_id=workflow_id,
        revision=2,
        status=TaskStatus.QA,
        steps=(
            make_step(workflow_id, 0, RoleName.ARCHITECT, TaskStatus.ANALYZING),
            make_step(workflow_id, 1, RoleName.DEVELOPER, TaskStatus.IN_PROGRESS),
        ),
    )

    store.save(run)
    loaded = store.load(workflow_id)

    assert loaded.model_dump() == run.model_dump()
    assert loaded == run


def test_ida_y_vuelta_con_stage_artifacts_y_effects_vacios(tmp_path: Path) -> None:
    """El run nuevo con las colecciones nuevas vacías vuelve byte a byte igual."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    run = make_run(
        workflow_id=workflow_id,
        revision=4,
        status=TaskStatus.QA,
        steps=(make_step(workflow_id, 0, RoleName.DEVELOPER, TaskStatus.IN_PROGRESS),),
        request_fingerprint=hashlib.sha256(b"peticion").hexdigest(),
        stage_artifacts=(),
        effects=(),
    )
    assert run.stage_artifacts == ()
    assert run.effects == ()

    store.save(run)
    loaded = store.load(workflow_id)

    assert loaded.stage_artifacts == ()
    assert loaded.effects == ()
    assert loaded.model_dump() == run.model_dump()
    assert loaded == run


def test_ida_y_vuelta_con_stage_artifacts_y_effects_con_contenido(tmp_path: Path) -> None:
    """Handoff y efectos con contenido real sobreviven al checkpoint sin perder un campo."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    decision_id = uuid4()
    run = make_run(
        workflow_id=workflow_id,
        revision=6,
        status=TaskStatus.REVIEW,
        steps=(
            make_step(workflow_id, 0, RoleName.DEVELOPER, TaskStatus.IN_PROGRESS),
            make_step(workflow_id, 1, RoleName.QA, TaskStatus.QA),
        ),
        action="modify_file",
        project_path="src/punto",
        request_fingerprint="b" * 64,
        stage_artifacts=(make_stage_artifacts(),),
        effects=(make_effect(),),
        policy_decision_id=decision_id,
        effective_authority=AuthorityLevel.LEVEL_2_CAMUS,
        effective_risk=RiskLevel.MEDIUM,
    )

    checkpoint = store.save(run)
    loaded = store.load(workflow_id)

    assert loaded.model_dump() == run.model_dump()
    assert loaded == run
    assert loaded.stage_artifacts[0].references[0].digest == "a" * 64
    assert loaded.stage_artifacts[0].findings[0].severity is FindingSeverity.LOW
    assert loaded.effects[0].status is EffectStatus.APPLIED
    assert loaded.effects[0].resolved_at is not None
    assert loaded.policy_decision_id == decision_id
    assert loaded.effective_authority is AuthorityLevel.LEVEL_2_CAMUS
    assert loaded.effective_risk is RiskLevel.MEDIUM
    assert loaded.request.action == "modify_file"
    assert loaded.request.project_path == "src/punto"
    assert loaded.request_fingerprint == "b" * 64
    assert checkpoint.sequence == 6


def test_el_checkpoint_declara_el_digest_y_el_tamano_reales(tmp_path: Path) -> None:
    """El digest y ``bytes_written`` se calculan sobre los bytes escritos, no sobre una idea."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run()
    checkpoint = store.save(run)

    payload = run_path(store.root, run.workflow_id, checkpoint.sequence).read_bytes()

    assert payload == serialize(run)
    assert payload.endswith(b"\n")
    assert checkpoint.digest == hashlib.sha256(payload).hexdigest()
    assert checkpoint.bytes_written == len(payload)
    assert checkpoint.bytes_written == run_path(
        store.root, run.workflow_id, checkpoint.sequence
    ).stat().st_size
    assert checkpoint.revision == run.revision
    assert checkpoint.status == run.status


def test_la_secuencia_crece_con_la_revision_y_latest_devuelve_la_ultima(tmp_path: Path) -> None:
    """Regla elegida: ``sequence = max(revision, última + 1)``, monótona y determinista."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()

    sequences = [
        store.save(
            make_run(
                workflow_id=workflow_id,
                revision=revision,
                steps=(
                    make_step(workflow_id, revision, RoleName.DEVELOPER, TaskStatus.IN_PROGRESS),
                ),
            )
        ).sequence
        for revision in range(4)
    ]

    assert sequences == [0, 1, 2, 3]
    checkpoints = store.list_checkpoints(workflow_id)
    assert [checkpoint.sequence for checkpoint in checkpoints] == [0, 1, 2, 3]
    assert [checkpoint.revision for checkpoint in checkpoints] == [0, 1, 2, 3]
    latest = store.latest(workflow_id)
    assert latest is not None
    assert latest.sequence == 3
    assert store.load(workflow_id).revision == 3


def test_la_secuencia_avanza_aunque_la_revision_no_cambie(tmp_path: Path) -> None:
    """Dos guardados de la misma revisión no se pisan: la historia no se reescribe."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    first = make_run(workflow_id=workflow_id, revision=2)
    second = make_run(
        workflow_id=workflow_id,
        revision=2,
        context_summary="Segundo guardado con la misma revisión del run.",
    )

    assert store.save(first).sequence == 2
    assert store.save(second).sequence == 3

    assert [checkpoint.sequence for checkpoint in store.list_checkpoints(workflow_id)] == [2, 3]
    assert store.load(workflow_id).model_dump() == second.model_dump()


def test_los_metadatos_se_escriben_junto_al_contenido(tmp_path: Path) -> None:
    """Los metadatos viven en ``meta/`` con la misma secuencia que el contenido."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run()
    checkpoint = store.save(run)

    path = meta_path(store.root, run.workflow_id, checkpoint.sequence)

    assert path.is_file()
    assert store.latest(run.workflow_id) == checkpoint


# ---------------------------------------------------------------------------
# Integridad del contenido: corrupción, esquema y ausencias
# ---------------------------------------------------------------------------
def test_un_byte_alterado_invalida_el_checkpoint(tmp_path: Path) -> None:
    """Corrupción silenciosa detectada por el digest: ``latest`` la ve, ``load`` la rechaza."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run()
    checkpoint = store.save(run)
    path = run_path(store.root, run.workflow_id, checkpoint.sequence)
    original = path.read_bytes()
    altered = original.replace(b"checkpointing determinista", b"checkpointing determinist@")
    assert altered != original
    path.write_bytes(altered)

    assert store.latest(run.workflow_id) == checkpoint
    with pytest.raises(WorkflowCheckpointInvalidError, match="digest"):
        store.load(run.workflow_id)


def test_un_checkpoint_truncado_invalida_el_checkpoint(tmp_path: Path) -> None:
    """Un fichero truncado es indistinguible de una caída a medias: se rechaza."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run()
    checkpoint = store.save(run)
    path = run_path(store.root, run.workflow_id, checkpoint.sequence)
    path.write_bytes(path.read_bytes()[:-40])

    assert store.latest(run.workflow_id) == checkpoint
    with pytest.raises(WorkflowCheckpointInvalidError, match="digest"):
        store.load(run.workflow_id)


def test_un_contenido_que_no_es_un_run_valido_falla(tmp_path: Path) -> None:
    """Aunque el digest cuadre, un contenido ajeno al contrato no se interpreta como run."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run()
    checkpoint = store.save(run)
    path = run_path(store.root, run.workflow_id, checkpoint.sequence)
    payload = b'{"no": "es un workflow"}'
    path.write_bytes(payload)
    write_meta(
        store.root,
        run.workflow_id,
        checkpoint.sequence,
        checkpoint,
        {"digest": hashlib.sha256(payload).hexdigest(), "bytes_written": len(payload)},
    )

    with pytest.raises(WorkflowCheckpointInvalidError, match="WorkflowRun"):
        store.load(run.workflow_id)


def test_schema_version_distinta_falla_al_cargar(tmp_path: Path) -> None:
    """Un checkpoint de otra versión de esquema se rechaza de forma explícita al cargar."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(schema_version="0.0.1")
    store.save(run)

    with pytest.raises(WorkflowCheckpointInvalidError, match="schema_version") as excinfo:
        store.load(run.workflow_id)
    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_CHECKPOINT_INVALID


def test_cargar_un_workflow_sin_checkpoints_falla(tmp_path: Path) -> None:
    """No hay estado que reanudar: se dice con un error, no devolviendo un run inventado."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)

    with pytest.raises(WorkflowCheckpointInvalidError, match="no tiene ningún checkpoint"):
        store.load(uuid4())


def test_latest_de_un_workflow_sin_checkpoints_es_none(tmp_path: Path) -> None:
    """La ausencia de checkpoints es un caso normal, no un error: ``latest`` devuelve ``None``."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)

    assert store.latest(uuid4()) is None
    assert store.list_checkpoints(uuid4()) == ()


def test_cargar_falla_si_desaparece_el_fichero_del_run(tmp_path: Path) -> None:
    """Metadatos sin contenido no son un checkpoint válido: la falta se reporta."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run()
    checkpoint = store.save(run)
    run_path(store.root, run.workflow_id, checkpoint.sequence).unlink()

    assert store.latest(run.workflow_id) == checkpoint
    with pytest.raises(WorkflowCheckpointInvalidError, match="no se pudo leer"):
        store.load(run.workflow_id)


def test_un_contenido_sin_metadatos_no_es_un_checkpoint(tmp_path: Path) -> None:
    """Una caída entre el contenido y los metadatos deja un huérfano que nadie puede leer.

    El contenido solo se publica antes que sus metadatos a propósito: si el proceso muere en
    medio, el resultado es un fichero que no se lista ni se carga, nunca un checkpoint dudoso.
    """
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run()
    checkpoint = store.save(run)
    meta_path(store.root, run.workflow_id, checkpoint.sequence).unlink()

    assert store.latest(run.workflow_id) is None
    assert store.list_checkpoints(run.workflow_id) == ()
    with pytest.raises(WorkflowCheckpointInvalidError, match="no tiene ningún checkpoint"):
        store.load(run.workflow_id)

    assert store.save(run).sequence == checkpoint.sequence


# ---------------------------------------------------------------------------
# Manipulación de metadatos (V60-06): nada pasa en silencio
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("updates", "esperado"),
    [
        pytest.param({"status": TaskStatus.REVIEW}, "estado", id="status-cambiado"),
        pytest.param({"revision": 4}, "retrocede", id="revision-adelantada"),
        pytest.param({"revision": 2}, "obsoleto", id="revision-retrocedida"),
        pytest.param({"sequence": 4}, "secuencia", id="sequence-cambiada"),
        pytest.param({"bytes_written": 999_999}, "bytes_written", id="bytes-written-cambiado"),
        pytest.param({"digest": "0" * 64}, "digest", id="digest-alterado"),
        pytest.param(
            {"workflow_id": UUID("00000000-0000-0000-0000-0000000000ff")},
            "workflow",
            id="workflow-cambiado",
        ),
    ],
)
def test_metadatos_manipulados_fallan_al_cargar(
    tmp_path: Path, updates: dict[str, Any], esperado: str
) -> None:
    """Cada campo de los metadatos se comprueba: manipularlo es un error explícito, no un aviso."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    run = make_run(workflow_id=workflow_id, revision=3, status=TaskStatus.QA)
    checkpoint = store.save(run)
    write_meta(store.root, workflow_id, checkpoint.sequence, checkpoint, updates)

    with pytest.raises(WorkflowCheckpointInvalidError, match=esperado) as excinfo:
        store.load(workflow_id)

    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_CHECKPOINT_INVALID


def test_metadatos_obsoletos_copiados_a_una_secuencia_nueva_fallan(tmp_path: Path) -> None:
    """Copiar los metadatos viejos a la secuencia nueva no los convierte en los de ese contenido."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    first = store.save(make_run(workflow_id=workflow_id, revision=0))
    store.save(
        make_run(
            workflow_id=workflow_id,
            revision=1,
            context_summary="Segundo guardado, con contenido distinto del primero.",
        )
    )
    # Los metadatos del primer checkpoint, tal cual, sobre el nombre de la secuencia nueva.
    meta_path(store.root, workflow_id, 1).write_bytes(
        meta_path(store.root, workflow_id, 0).read_bytes()
    )

    assert first.sequence == 0
    with pytest.raises(WorkflowCheckpointInvalidError, match="secuencia"):
        store.load(workflow_id)
    with pytest.raises(WorkflowCheckpointInvalidError, match="secuencia"):
        store.latest(workflow_id)


def test_metadatos_obsoletos_con_la_secuencia_retocada_fallan_por_el_digest(tmp_path: Path) -> None:
    """Retocar el ``sequence`` no basta: ``digest`` y ``bytes_written`` siguen siendo los viejos."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    first = store.save(make_run(workflow_id=workflow_id, revision=0))
    store.save(
        make_run(
            workflow_id=workflow_id,
            revision=1,
            context_summary="Segundo guardado, con contenido distinto del primero.",
        )
    )
    stale = first.model_copy(update={"sequence": 1})
    meta_path(store.root, workflow_id, 1).write_bytes(serialize_meta(stale))

    with pytest.raises(WorkflowCheckpointInvalidError, match="digest") as excinfo:
        store.load(workflow_id)

    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_CHECKPOINT_INVALID


def test_load_valida_la_coherencia_aunque_no_sea_una_reanudacion(tmp_path: Path) -> None:
    """Digest y tamaño pueden cuadrar y el checkpoint seguir siendo inválido: describen otro estado.

    Es el corazón de V60-06: ``load`` no se limita a comprobar la integridad de los bytes, también
    comprueba que los metadatos digan lo mismo que el run que se va a devolver.
    """
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    stored = make_run(workflow_id=workflow_id, revision=5, status=TaskStatus.QA)
    checkpoint = store.save(stored)
    other = make_run(
        workflow_id=workflow_id,
        revision=6,
        status=TaskStatus.REVIEW,
        context_summary="Contenido de otro estado, con metadatos del anterior.",
    )
    payload = serialize(other)
    run_path(store.root, workflow_id, checkpoint.sequence).write_bytes(payload)
    write_meta(
        store.root,
        workflow_id,
        checkpoint.sequence,
        checkpoint,
        {"digest": hashlib.sha256(payload).hexdigest(), "bytes_written": len(payload)},
    )

    with pytest.raises(WorkflowCheckpointInvalidError, match="no describe al run") as excinfo:
        store.load(workflow_id)

    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_CHECKPOINT_INVALID


def test_load_falla_si_el_ultimo_checkpoint_esta_corrupto_y_no_retrocede(tmp_path: Path) -> None:
    """El penúltimo sigue en disco, pero ``load`` no baja a él: reanudaría otro estado."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    store.save(make_run(workflow_id=workflow_id, revision=0))
    second = store.save(
        make_run(
            workflow_id=workflow_id,
            revision=1,
            context_summary="Segundo guardado, que se corrompe a continuación.",
        )
    )
    path = run_path(store.root, workflow_id, second.sequence)
    path.write_bytes(path.read_bytes()[:-40])

    assert [checkpoint.sequence for checkpoint in store.list_checkpoints(workflow_id)] == [0, 1]
    assert store.latest(workflow_id) == second
    with pytest.raises(WorkflowCheckpointInvalidError, match="digest") as excinfo:
        store.load(workflow_id)

    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_CHECKPOINT_INVALID


def test_load_detecta_un_directorio_copiado_de_otro_workflow(tmp_path: Path) -> None:
    """Un directorio entero copiado a otro workflow describe al run equivocado: se rechaza."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    store.save(make_run(workflow_id=workflow_id, revision=1))
    other = uuid4()
    shutil.copytree(store.root / str(workflow_id), store.root / str(other))

    with pytest.raises(WorkflowCheckpointInvalidError, match="workflow"):
        store.load(other)


# ---------------------------------------------------------------------------
# Escritura atómica
# ---------------------------------------------------------------------------
def test_dos_guardados_seguidos_dejan_el_ultimo_y_ningun_temporal(tmp_path: Path) -> None:
    """El directorio queda exacto: contenido y metadatos nuevos, ningún resto temporal."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    first = make_run(workflow_id=workflow_id, revision=0)
    second = make_run(
        workflow_id=workflow_id,
        revision=1,
        context_summary="Contenido final tras el segundo guardado.",
    )

    store.save(first)
    store.save(second)

    directory = store.root / str(workflow_id)
    written = sorted(
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    )
    assert written == ["0000.json", "0001.json", "meta/0000.json", "meta/0001.json"]
    strays = [
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and (path.name.startswith(".") or path.suffix == ".tmp")
    ]
    assert strays == []
    content = run_path(store.root, workflow_id, 1).read_bytes()
    assert content == serialize(second)
    assert b"Contenido final tras el segundo guardado." in content
    assert store.load(workflow_id).model_dump() == second.model_dump()


def test_un_guardado_rechazado_no_deja_restos(tmp_path: Path) -> None:
    """La validación ocurre antes de tocar el disco: un rechazo no deja temporales ni carpetas."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    rejected = make_run(
        workflow_id=workflow_id,
        revision=1,
        status=TaskStatus.COMPLETED,
        completed_at=utc_now(),
    )

    with pytest.raises(WorkflowCheckpointInvalidError, match="sin result"):
        store.save(rejected)

    assert store.latest(workflow_id) is None
    assert not (store.root / str(workflow_id)).exists()


# ---------------------------------------------------------------------------
# Idempotencia por etapa
# ---------------------------------------------------------------------------
def test_la_clave_de_idempotencia_es_estable() -> None:
    """Dos llamadas con los mismos datos producen la misma clave, con el formato documentado."""
    workflow_id = uuid4()

    first = step_idempotency_key(workflow_id, 3, RoleName.QA, TaskStatus.QA)
    second = step_idempotency_key(workflow_id, 3, RoleName.QA, TaskStatus.QA)

    assert first == second
    assert first == f"{workflow_id}:3:QA:QA"


def test_la_clave_de_idempotencia_distingue_paso_rol_etapa_y_workflow() -> None:
    """La clave identifica la etapa completa: cualquier componente distinto da otra clave."""
    workflow_id = uuid4()

    keys = {
        step_idempotency_key(workflow_id, 0, RoleName.DEVELOPER, TaskStatus.IN_PROGRESS),
        step_idempotency_key(workflow_id, 1, RoleName.DEVELOPER, TaskStatus.IN_PROGRESS),
        step_idempotency_key(workflow_id, 0, RoleName.QA, TaskStatus.IN_PROGRESS),
        step_idempotency_key(workflow_id, 0, RoleName.DEVELOPER, TaskStatus.QA),
        step_idempotency_key(uuid4(), 0, RoleName.DEVELOPER, TaskStatus.IN_PROGRESS),
    }

    assert len(keys) == 5


def test_completed_idempotency_keys_refleja_los_pasos_del_run() -> None:
    """Lo confirmado es exactamente lo que hay en ``run.steps``."""
    workflow_id = uuid4()
    steps = (
        make_step(workflow_id, 0, RoleName.ARCHITECT, TaskStatus.ANALYZING),
        make_step(workflow_id, 1, RoleName.PLANNER, TaskStatus.PLANNING),
    )
    run = make_run(workflow_id=workflow_id, revision=2, steps=steps)

    assert completed_idempotency_keys(run) == frozenset(
        step.idempotency_key for step in steps
    )
    assert completed_idempotency_keys(make_run(workflow_id=workflow_id)) == frozenset()


def test_un_paso_registrado_cuenta_como_confirmado_aunque_el_rol_fallara() -> None:
    """Un paso solo entra en el run tras el commit: repetirlo duplicaría efectos y gasto."""
    workflow_id = uuid4()
    failed = make_step(
        workflow_id,
        0,
        RoleName.DEVELOPER,
        TaskStatus.IN_PROGRESS,
        status=RoleStatus.FAILED,
    )
    run = make_run(workflow_id=workflow_id, revision=1, steps=(failed,))

    assert completed_idempotency_keys(run) == frozenset({failed.idempotency_key})


def test_next_pending_step_devuelve_el_primero_no_completado() -> None:
    """Al reanudar se retoma la primera etapa que no está confirmada, y en orden."""
    workflow_id = uuid4()
    keys = [
        step_idempotency_key(workflow_id, index, RoleName.DEVELOPER, TaskStatus.IN_PROGRESS)
        for index in range(4)
    ]
    run = make_run(
        workflow_id=workflow_id,
        revision=2,
        steps=(
            make_step(workflow_id, 0, RoleName.DEVELOPER, TaskStatus.IN_PROGRESS),
            make_step(workflow_id, 1, RoleName.DEVELOPER, TaskStatus.IN_PROGRESS),
        ),
    )

    assert next_pending_step(run, keys) == keys[2]
    assert next_pending_step(run, keys[2:]) == keys[2]
    assert next_pending_step(run, []) is None


def test_next_pending_step_devuelve_none_si_estan_todas() -> None:
    """Si todo está confirmado no queda trabajo: ``None``, y la reanudación no repite nada."""
    workflow_id = uuid4()
    keys = [
        step_idempotency_key(workflow_id, index, RoleName.QA, TaskStatus.QA) for index in range(3)
    ]
    run = make_run(
        workflow_id=workflow_id,
        revision=3,
        steps=tuple(
            make_step(workflow_id, index, RoleName.QA, TaskStatus.QA) for index in range(3)
        ),
    )

    assert next_pending_step(run, keys) is None


def test_dos_runs_con_los_mismos_pasos_comparten_claves_de_idempotencia() -> None:
    """La clave no depende del momento ni del contenido: sobrevive entre ejecuciones."""
    workflow_id = uuid4()
    step = make_step(workflow_id, 0, RoleName.DEVELOPER, TaskStatus.IN_PROGRESS)
    first = make_run(workflow_id=workflow_id, revision=1, steps=(step,))
    second = make_run(workflow_id=workflow_id, revision=1, steps=(step,))

    assert completed_idempotency_keys(first) == completed_idempotency_keys(second)
    assert step.idempotency_key in completed_idempotency_keys(first)


# ---------------------------------------------------------------------------
# Cierres falsos: un workflow no puede quedar COMPLETED sin demostrarlo
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED])
def test_un_run_terminal_sin_resultado_no_se_guarda(tmp_path: Path, status: TaskStatus) -> None:
    """``COMPLETED`` y ``FAILED`` afirman un desenlace: sin ``result`` serían un cierre falso."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(status=status, revision=1, completed_at=utc_now())

    with pytest.raises(WorkflowCheckpointInvalidError, match="sin result"):
        store.save(run)

    assert not (store.root / str(run.workflow_id)).exists()
    assert store.latest(run.workflow_id) is None


@pytest.mark.parametrize(
    "status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
)
def test_un_run_terminal_sin_completed_at_no_se_guarda(
    tmp_path: Path, status: TaskStatus
) -> None:
    """Un terminal sin marca de cierre no es un cierre: falta cuándo terminó."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(
        status=status,
        revision=1,
        result=WorkflowResult(status=status, summary="Desenlace sin marca de cierre"),
    )

    with pytest.raises(WorkflowCheckpointInvalidError, match="completed_at"):
        store.save(run)


def test_un_run_terminal_con_resultado_y_completed_at_se_guarda(
    tmp_path: Path,
) -> None:
    """Con resultado y marca de cierre, el terminal es legítimo y se persiste."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(
        status=TaskStatus.COMPLETED,
        revision=7,
        result=WorkflowResult(status=TaskStatus.COMPLETED, summary="Cierre con evidencia"),
        completed_at=utc_now(),
    )

    checkpoint = store.save(run)

    assert checkpoint.sequence == 7
    assert checkpoint.status == TaskStatus.COMPLETED
    assert store.load(run.workflow_id).model_dump() == run.model_dump()


def test_una_cancelacion_explicita_sin_resultado_se_guarda(tmp_path: Path) -> None:
    """``CANCELLED`` cierra sin desenlace técnico: exigirle ``result`` rompería la cancelación."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(status=TaskStatus.CANCELLED, revision=3, completed_at=utc_now())

    checkpoint = store.save(run)

    assert checkpoint.status == TaskStatus.CANCELLED
    assert store.load(run.workflow_id).status == TaskStatus.CANCELLED


def test_un_run_no_terminal_se_guarda_sin_resultado(tmp_path: Path) -> None:
    """La exigencia es solo para los terminales: el trabajo en curso no tiene resultado aún."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(status=TaskStatus.IN_PROGRESS, revision=4)

    assert store.save(run).status == TaskStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# validate_checkpoint: coherencia entre run y checkpoint
# ---------------------------------------------------------------------------
def test_validate_checkpoint_acepta_el_checkpoint_correspondiente(tmp_path: Path) -> None:
    """El caso normal no levanta nada: el checkpoint describe al run."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(status=TaskStatus.QA, revision=5)
    checkpoint = store.save(run)

    validate_checkpoint(run, checkpoint)
    validate_checkpoint(
        run,
        checkpoint,
        expected_sequence=checkpoint.sequence,
        durable_bytes=serialize(run),
    )


def test_validate_checkpoint_detecta_otro_workflow(tmp_path: Path) -> None:
    """Reanudar un workflow con el checkpoint de otro mezclaría dos estados distintos."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(status=TaskStatus.QA, revision=5)
    checkpoint = store.save(run)
    other = make_run(workflow_id=uuid4(), status=TaskStatus.QA, revision=5)

    with pytest.raises(WorkflowResumeFailedError, match="workflow") as excinfo:
        validate_checkpoint(other, checkpoint)

    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_RESUME_FAILED


def test_validate_checkpoint_detecta_revision_que_retrocede(tmp_path: Path) -> None:
    """Un run más antiguo que su checkpoint significaría reanudar hacia atrás."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    checkpoint = store.save(make_run(workflow_id=workflow_id, revision=3))
    older = make_run(workflow_id=workflow_id, revision=2)

    with pytest.raises(WorkflowResumeFailedError, match="retrocede"):
        validate_checkpoint(older, checkpoint)


def test_validate_checkpoint_detecta_un_checkpoint_obsoleto(tmp_path: Path) -> None:
    """Un checkpoint por detrás del run ya no lo describe: se rechaza en vez de elegir uno."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    checkpoint = store.save(make_run(workflow_id=workflow_id, revision=3))
    newer = make_run(workflow_id=workflow_id, revision=4)

    with pytest.raises(WorkflowResumeFailedError, match="obsoleto"):
        validate_checkpoint(newer, checkpoint)


def test_validate_checkpoint_detecta_estado_distinto(tmp_path: Path) -> None:
    """Misma revisión pero otro estado es un estado imposible: la reanudación se niega."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    checkpoint = store.save(make_run(workflow_id=workflow_id, status=TaskStatus.QA, revision=5))
    other_status = make_run(
        workflow_id=workflow_id, status=TaskStatus.REVIEW, revision=5
    )

    with pytest.raises(WorkflowResumeFailedError, match="estado"):
        validate_checkpoint(other_status, checkpoint)


def test_validate_checkpoint_detecta_schema_version_distinta(tmp_path: Path) -> None:
    """Un documento de otra versión de esquema no se interpreta: es un checkpoint inválido."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(status=TaskStatus.QA, revision=5, schema_version="9.9.9")
    checkpoint = store.save(run)

    with pytest.raises(WorkflowCheckpointInvalidError, match="schema_version") as excinfo:
        validate_checkpoint(run, checkpoint)

    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_CHECKPOINT_INVALID


def test_validate_checkpoint_detecta_una_secuencia_que_no_es_la_del_fichero(
    tmp_path: Path,
) -> None:
    """La secuencia declarada tiene que ser la que impone el nombre del fichero."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(status=TaskStatus.QA, revision=5)
    checkpoint = store.save(run)

    with pytest.raises(WorkflowCheckpointInvalidError, match="secuencia"):
        validate_checkpoint(run, checkpoint, expected_sequence=checkpoint.sequence + 1)


def test_validate_checkpoint_detecta_metadatos_que_no_corresponden_a_los_bytes(
    tmp_path: Path,
) -> None:
    """``digest`` y ``bytes_written`` se comprueban contra los bytes, no contra su declaración."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(status=TaskStatus.QA, revision=5)
    checkpoint = store.save(run)
    payload = serialize(run)

    with pytest.raises(WorkflowCheckpointInvalidError, match="digest"):
        validate_checkpoint(run, checkpoint, durable_bytes=payload + b" ")

    stale_size = checkpoint.model_copy(update={"bytes_written": len(payload) + 1})
    with pytest.raises(WorkflowCheckpointInvalidError, match="bytes_written"):
        validate_checkpoint(run, stale_size, durable_bytes=payload)


# ---------------------------------------------------------------------------
# Nada de secretos en el checkpoint
# ---------------------------------------------------------------------------
def test_ningun_fichero_contiene_credenciales(tmp_path: Path) -> None:
    """El checkpoint solo lleva el run: ni claves, ni tokens, ni material de credenciales."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    workflow_id = uuid4()
    run = make_run(
        workflow_id=workflow_id,
        revision=1,
        status=TaskStatus.IN_PROGRESS,
        steps=(make_step(workflow_id, 0, RoleName.DEVELOPER, TaskStatus.IN_PROGRESS),),
        stage_artifacts=(make_stage_artifacts(),),
        effects=(make_effect(),),
    )
    checkpoint = store.save(run)

    written = sorted(path for path in store.root.rglob("*") if path.is_file())
    assert len(written) == 2
    contents: dict[str, str] = {}
    for path in written:
        text = path.read_text(encoding="utf-8")
        contents[path.relative_to(store.root).as_posix()] = text
        for needle in FORBIDDEN_IN_CHECKPOINTS:
            assert needle not in text, f"{path} contiene {needle!r}"

    name = f"{checkpoint.sequence:04d}.json"
    assert contents[f"{workflow_id}/{name}"] == serialize(run).decode("utf-8")
    assert "Contexto de la etapa" in contents[f"{workflow_id}/{name}"]
    assert contents[f"{workflow_id}/meta/{name}"].startswith("{\n")
