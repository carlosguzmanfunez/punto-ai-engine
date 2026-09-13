"""Pruebas del checkpointing local e idempotente del kernel (ENGINE-6.0 §17 a §20).

Se ejercita el comportamiento real: ficheros en disco, escritura atómica, detección de
corrupción, reanudación sin repetir etapas y la garantía de que un workflow no puede quedar
falsamente ``COMPLETED``. Todo con ``tmp_path``: ningún test toca el directorio del proyecto.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from punto.common import utc_now
from punto.schemas.enums import TaskStatus
from punto.schemas.planning import SCHEMA_VERSION
from punto.schemas.workflow import (
    RoleName,
    RoleStatus,
    WorkflowDecisionKind,
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
) -> WorkflowRun:
    """Run mínimo y válido para las pruebas de checkpointing."""
    identifier = workflow_id if workflow_id is not None else uuid4()
    return WorkflowRun(
        workflow_id=identifier,
        schema_version=schema_version,
        request=WorkflowRequest(
            task_id=uuid4(),
            project_id=uuid4(),
            objective="Implementar el checkpointing determinista del kernel",
            acceptance_criteria=("el run se reanuda sin repetir etapas",),
            context_summary=context_summary,
            idempotency_key=f"request:{identifier}",
        ),
        status=status,
        revision=revision,
        steps=steps,
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


def run_path(root: Path, workflow_id: UUID, sequence: int) -> Path:
    """Ruta del contenido de un checkpoint, tal como la fija el formato en disco."""
    return root / str(workflow_id) / f"{sequence:04d}.json"


def meta_path(root: Path, workflow_id: UUID, sequence: int) -> Path:
    """Ruta de los metadatos de un checkpoint."""
    return root / str(workflow_id) / "meta" / f"{sequence:04d}.json"


def serialize(run: WorkflowRun) -> bytes:
    """Bytes exactos que el store debe escribir para un run."""
    return (run.model_dump_json(indent=2) + "\n").encode("utf-8")


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
# Integridad: corrupción, esquema y ausencias
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
    meta = meta_path(store.root, run.workflow_id, checkpoint.sequence)
    meta.write_bytes(
        (
            checkpoint.model_copy(
                update={
                    "digest": hashlib.sha256(payload).hexdigest(),
                    "bytes_written": len(payload),
                }
            ).model_dump_json(indent=2)
            + "\n"
        ).encode("utf-8")
    )

    with pytest.raises(WorkflowCheckpointInvalidError, match="WorkflowRun"):
        store.load(run.workflow_id)


def test_schema_version_distinta_falla_al_cargar(tmp_path: Path) -> None:
    """Un checkpoint de otra versión de esquema se rechaza de forma explícita al reanudar."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(schema_version="0.0.1")
    store.save(run)

    with pytest.raises(WorkflowCheckpointInvalidError, match="schema_version"):
        store.load(run.workflow_id)


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
    content = run_path(store.root, workflow_id, 1).read_bytes()
    assert content == serialize(second)
    assert b"Contenido final tras el segundo guardado." in content
    assert store.load(workflow_id).model_dump() == second.model_dump()


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
# Reanudación: coherencia entre run y checkpoint
# ---------------------------------------------------------------------------
def test_validate_checkpoint_acepta_el_checkpoint_correspondiente(tmp_path: Path) -> None:
    """El caso normal no levanta nada: el checkpoint describe al run."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(status=TaskStatus.QA, revision=5)
    checkpoint = store.save(run)

    validate_checkpoint(run, checkpoint)


def test_validate_checkpoint_detecta_otro_workflow(tmp_path: Path) -> None:
    """Reanudar un workflow con el checkpoint de otro mezclaría dos estados distintos."""
    store = FileCheckpointStore(tmp_path / CHECKPOINT_DIRNAME)
    run = make_run(status=TaskStatus.QA, revision=5)
    checkpoint = store.save(run)
    other = make_run(workflow_id=uuid4(), status=TaskStatus.QA, revision=5)

    with pytest.raises(WorkflowResumeFailedError, match="workflow"):
        validate_checkpoint(other, checkpoint)


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
