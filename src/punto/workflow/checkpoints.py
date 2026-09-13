"""Checkpointing local, determinista e idempotente del kernel de workflow (ENGINE-6.0).

Este módulo es la frontera de **durabilidad** del kernel (§17 a §20). Resuelve un problema
concreto: que un workflow autónomo pueda reanudarse tras una caída **sin repetir etapas ya
ejecutadas** y **sin poder declararse terminado en falso**. No introduce infraestructura nueva:
escribe JSON en un directorio local, con escritura atómica, y detecta corrupción en lugar de
silenciarla.

Formato en disco
----------------
Para cada workflow, bajo ``root/<workflow_id>/``:

- ``<secuencia:04d>.json``: el :class:`~punto.schemas.workflow.WorkflowRun` serializado con
  ``model_dump_json(indent=2)`` más un salto de línea final. Es el contenido durable, y su
  sha256 es el ``digest`` del checkpoint.
- ``meta/<secuencia:04d>.json``: el :class:`~punto.schemas.workflow.WorkflowCheckpoint` con la
  misma convención de serialización. Es el **marcador de commit**: un ``WorkflowRun`` sin sus
  metadatos es el resto de una escritura interrumpida y **nunca** se lista ni se carga.

Solo se escribe lo que el contrato del run ya declara. Aquí no se añade nada: ni credenciales,
ni contenido de los ficheros del proyecto, ni rutas de workspace más allá de las que el propio
``WorkflowRun`` ya contiene. El checkpoint es un documento auditable, no un volcado del sistema.

Regla de secuencia
------------------
``sequence = max(run.revision, última_secuencia_confirmada + 1)``, con ``-1`` como «no hay
ninguna» (de modo que el primer checkpoint de un run en ``revision`` 0 obtiene la secuencia 0).

- Es **monótona estricta**: aunque el kernel vuelva a guardar el mismo ``revision`` —cosa que
  ocurre, por ejemplo, al re-guardar tras una reanudación sin transición nueva—, el checkpoint
  nuevo ocupa la secuencia siguiente en vez de pisar el anterior. La historia no se reescribe.
- Es **determinista**: dadas las mismas llamadas en el mismo orden sobre el mismo directorio,
  las secuencias son las mismas. Y nunca queda por debajo del ``revision`` del run, así que la
  secuencia y la revisión se pueden leer juntas sin ambigüedad.

Escritura atómica (consistencia ante caídas)
--------------------------------------------
Cada fichero se escribe primero en un temporal **del mismo directorio** y después se mueve con
``os.replace``, que en el mismo sistema de ficheros es atómico. Un lector concurrente ve el
fichero anterior o el nuevo, nunca uno a medias. Además se vuelca el contenido a disco antes de
publicarlo, para que una caída de la máquina no deje el nombre apuntando a bytes que aún no
estaban escritos. El orden —primero el contenido, después los metadatos— hace que un checkpoint
a medias sea, como mucho, un contenido huérfano e invisible: no hay forma de que se lea como
válido.

Orden de commit que este módulo exige (§20)
-------------------------------------------
El kernel persiste en **este** orden, y solo este es seguro: ejecuta el rol y persiste su
resultado → valida el resultado contra el contrato → aplica la transición (sube ``revision`` y
fija ``status``) → llama a :meth:`CheckpointStore.save`. Guardar **después** de aplicar la
transición es lo que garantiza que un checkpoint nunca describa un estado que el run no tenía.

La otra mitad de la garantía está en :meth:`FileCheckpointStore.save`, que **se niega** a
persistir un cierre falso: todo estado terminal exige ``completed_at``, y además ``COMPLETED``
y ``FAILED`` exigen ``result``, porque ambos **afirman un desenlace** y sin resultado esa
afirmación no está demostrada. (``CANCELLED`` no exige ``result``: una cancelación explícita no
produce resultado, solo una marca de cierre; es la única salida terminal legítima sin él.) Si el
kernel intentara cerrar sin resultado, el fallo ocurre en el punto de persistencia y queda como
error explícito en vez de como un éxito sin evidencia.

Idempotencia por etapa
----------------------
La clave de una etapa es ``f"{workflow_id}:{step_index}:{role.value}:{stage.value}"``: estable
entre procesos, entre máquinas y entre reanudaciones, porque solo depende de datos que están en
el propio run. Una etapa ya presente en ``run.steps`` está **confirmada** —su resultado se
persistió, su transición se aplicó y su checkpoint se guardó—, así que reanudar no la vuelve a
ejecutar. Eso es exactamente lo que hace segura la reanudación: el trabajo pagado no se repite.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

from pydantic import ValidationError

from punto.schemas.enums import TaskStatus
from punto.schemas.planning import SCHEMA_VERSION
from punto.schemas.workflow import (
    TERMINAL_WORKFLOW_STATUSES,
    RoleName,
    WorkflowCheckpoint,
    WorkflowRun,
)
from punto.workflow.errors import WorkflowCheckpointInvalidError, WorkflowResumeFailedError

#: Subdirectorio donde viven los metadatos (el marcador de commit de cada checkpoint).
_META_DIRNAME: Final[str] = "meta"
#: Sufijo de todo fichero de checkpoint.
_CHECKPOINT_SUFFIX: Final[str] = ".json"
#: Ancho mínimo del número de secuencia. Los nombres se ordenan por número, no por texto.
_SEQUENCE_WIDTH: Final[int] = 4
#: Prefijo y sufijo de los temporales de escritura atómica. Empiezan por punto para no
#: confundirse con un checkpoint, y terminan en ``.tmp`` para que ningún glob los alcance.
_TEMP_PREFIX: Final[str] = ".checkpoint-"
_TEMP_SUFFIX: Final[str] = ".tmp"
#: Estados terminales que, además de ``completed_at``, exigen ``result``: afirman un desenlace.
#: ``CANCELLED`` queda fuera a propósito: cancelar cierra el workflow sin producir resultado.
_RESULT_REQUIRED_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED}
)


class CheckpointStore(Protocol):
    """Contrato de persistencia de checkpoints que consume el kernel.

    Se declara como ``Protocol`` y no como clase base para que el kernel dependa de la
    **capacidad** (guardar, leer la última, cargar, listar) y no de esta implementación: una
    prueba puede inyectar un doble en memoria y el kernel no cambia una línea.
    """

    def save(self, run: WorkflowRun) -> WorkflowCheckpoint:
        """Persiste ``run`` y devuelve los metadatos del checkpoint escrito.

        Debe negarse a persistir un cierre falso: todo estado terminal exige ``completed_at``,
        y ``COMPLETED``/``FAILED`` exigen además ``result``. El checkpoint no puede afirmar un
        desenlace que el run no demuestra.
        """
        ...

    def latest(self, workflow_id: UUID) -> WorkflowCheckpoint | None:
        """Metadatos del checkpoint de secuencia mayor, o ``None`` si no hay ninguno.

        Solo lee los metadatos del último: un checkpoint anterior corrupto no puede impedir
        reanudar desde el más reciente.
        """
        ...

    def load(self, workflow_id: UUID) -> WorkflowRun:
        """Devuelve el run del último checkpoint, validando integridad y versión de esquema.

        La corrupción se **detecta y se reporta**; nunca se salta al checkpoint anterior en
        silencio, porque eso sería reanudar un estado que no es el que se guardó.
        """
        ...

    def list_checkpoints(self, workflow_id: UUID) -> tuple[WorkflowCheckpoint, ...]:
        """Metadatos de todos los checkpoints del workflow, ordenados por secuencia."""
        ...


def step_idempotency_key(
    workflow_id: UUID, step_index: int, role: RoleName, stage: TaskStatus
) -> str:
    """Clave estable de una etapa del workflow.

    Depende solo del workflow, del índice del paso, del rol y del estado que motivó el paso.
    Nada de tiempo, de identificadores aleatorios ni del contenido del resultado: la misma
    etapa debe producir la misma clave en otra máquina y en otra ejecución.
    """
    return f"{workflow_id}:{step_index}:{role.value}:{stage.value}"


def completed_idempotency_keys(run: WorkflowRun) -> frozenset[str]:
    """Claves de las etapas ya confirmadas del run.

    Un paso presente en ``run.steps`` está confirmado: el kernel solo lo añade después de
    persistir su resultado, aplicar la transición y guardar el checkpoint. Repetirlo en una
    reanudación duplicaría llamadas a modelos, gastaría presupuesto y podría repetir efectos
    laterales, así que cuenta como completado con independencia del resultado que el rol
    reportara. Cambiar de intento es una decisión del kernel (``attempt``), no una reanudación.
    """
    return frozenset(step.idempotency_key for step in run.steps)


def next_pending_step(run: WorkflowRun, keys: Sequence[str]) -> str | None:
    """Primera clave de ``keys`` que el run no da por completada, en orden.

    ``keys`` es el recorrido previsto de etapas (el que decide el kernel); ``run`` es lo que ya
    se hizo. Devuelve ``None`` solo si todas están completadas: ahí no queda trabajo pendiente
    que reanudar.
    """
    completed = completed_idempotency_keys(run)
    for key in keys:
        if key not in completed:
            return key
    return None


def validate_checkpoint(run: WorkflowRun, checkpoint: WorkflowCheckpoint) -> None:
    """Comprueba que ``checkpoint`` describe exactamente a ``run``.

    Se valida en la reanudación, antes de continuar: reanudar desde un checkpoint de otro
    workflow, de otra revisión o de otro estado produciría un estado imposible. Una revisión
    que retrocede significa que el checkpoint es más nuevo que el run; una que avanza, que el
    checkpoint ya está obsoleto. Ambos casos se rechazan en vez de elegir uno en silencio.
    """
    if checkpoint.workflow_id != run.workflow_id:
        raise WorkflowResumeFailedError(
            f"el checkpoint pertenece al workflow {checkpoint.workflow_id} "
            f"y el run es del workflow {run.workflow_id}"
        )
    if run.revision < checkpoint.revision:
        raise WorkflowResumeFailedError(
            f"la revisión del run ({run.revision}) retrocede respecto a la del checkpoint "
            f"({checkpoint.revision})"
        )
    if run.revision > checkpoint.revision:
        raise WorkflowResumeFailedError(
            f"la revisión del run ({run.revision}) no coincide con la del checkpoint "
            f"({checkpoint.revision}): el checkpoint está obsoleto"
        )
    if checkpoint.status != run.status:
        raise WorkflowResumeFailedError(
            f"el estado del run ({run.status.value}) no coincide con el del checkpoint "
            f"({checkpoint.status.value})"
        )


class FileCheckpointStore:
    """Implementación en disco de :class:`CheckpointStore`.

    Un workflow, un directorio. Dentro, un JSON por checkpoint y un subdirectorio ``meta`` con
    sus metadatos. Nada compartido entre workflows, así que no hay contención ni orden global
    que mantener.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """Directorio raíz donde se escriben los workflows."""
        return self._root

    def save(self, run: WorkflowRun) -> WorkflowCheckpoint:
        """Persiste ``run`` de forma atómica y devuelve sus metadatos.

        Lanza :class:`~punto.workflow.errors.WorkflowCheckpointInvalidError` si el run no es
        persistible (terminal sin ``completed_at``, o ``COMPLETED``/``FAILED`` sin ``result``)
        o si su ``workflow_id`` no sirve como nombre de carpeta. La validación ocurre **antes**
        de tocar el disco: un rechazo no deja restos.
        """
        _assert_persistable(run)
        directory = self._workflow_dir(run.workflow_id)
        meta_directory = directory / _META_DIRNAME
        meta_directory.mkdir(parents=True, exist_ok=True)
        sequence = _next_sequence(meta_directory, run.revision)
        payload = _serialize(run)
        checkpoint = WorkflowCheckpoint(
            workflow_id=run.workflow_id,
            sequence=sequence,
            status=run.status,
            revision=run.revision,
            digest=hashlib.sha256(payload).hexdigest(),
            bytes_written=len(payload),
        )
        # Contenido primero, metadatos después: los metadatos son el marcador de commit.
        _atomic_write(directory / _checkpoint_name(sequence), payload)
        _atomic_write(meta_directory / _checkpoint_name(sequence), _serialize(checkpoint))
        return checkpoint

    def latest(self, workflow_id: UUID) -> WorkflowCheckpoint | None:
        """Metadatos del último checkpoint, o ``None`` si el workflow no tiene ninguno."""
        meta_directory = self._workflow_dir(workflow_id) / _META_DIRNAME
        sequences = _committed_sequences(meta_directory)
        if not sequences:
            return None
        return _read_checkpoint(meta_directory / _checkpoint_name(max(sequences)))

    def load(self, workflow_id: UUID) -> WorkflowRun:
        """Carga el run del último checkpoint, verificando digest y ``schema_version``.

        Cualquier anomalía —fichero ausente, JSON ilegible, contenido que no es un
        ``WorkflowRun``, digest que no cuadra o esquema de otra versión— se convierte en
        :class:`~punto.workflow.errors.WorkflowCheckpointInvalidError`. Nunca se devuelve un
        run dudoso ni se retrocede al checkpoint anterior por conveniencia.
        """
        directory = self._workflow_dir(workflow_id)
        checkpoint = self.latest(workflow_id)
        if checkpoint is None:
            raise WorkflowCheckpointInvalidError(
                f"el workflow {workflow_id} no tiene ningún checkpoint en {directory}"
            )
        path = directory / _checkpoint_name(checkpoint.sequence)
        payload = _read_bytes(path)
        digest = hashlib.sha256(payload).hexdigest()
        if digest != checkpoint.digest:
            raise WorkflowCheckpointInvalidError(
                f"el digest del checkpoint {path} no coincide con su contenido "
                f"(esperado {checkpoint.digest}, calculado {digest}): corrupción o "
                "manipulación"
            )
        run = _parse_run(payload, path)
        if run.schema_version != SCHEMA_VERSION:
            raise WorkflowCheckpointInvalidError(
                f"el checkpoint {path} declara schema_version {run.schema_version!r} y este "
                f"motor escribe {SCHEMA_VERSION!r}"
            )
        if run.workflow_id != workflow_id:
            raise WorkflowCheckpointInvalidError(
                f"el checkpoint {path} contiene el workflow {run.workflow_id} en lugar de "
                f"{workflow_id}"
            )
        return run

    def list_checkpoints(self, workflow_id: UUID) -> tuple[WorkflowCheckpoint, ...]:
        """Metadatos de todos los checkpoints, en orden creciente de secuencia.

        Solo aparecen los checkpoints confirmados (con metadatos). Un contenido huérfano de una
        escritura interrumpida no se lista: no es un estado del que se pueda reanudar.
        """
        meta_directory = self._workflow_dir(workflow_id) / _META_DIRNAME
        return tuple(
            _read_checkpoint(meta_directory / _checkpoint_name(sequence))
            for sequence in _committed_sequences(meta_directory)
        )

    def _workflow_dir(self, workflow_id: UUID) -> Path:
        """Directorio del workflow, con el identificador saneado como nombre de carpeta."""
        return self._root / _safe_workflow_name(workflow_id)


def _safe_workflow_name(workflow_id: UUID) -> str:
    """Nombre de carpeta derivado del ``workflow_id``.

    Se reconstruye el UUID a partir de su forma textual en vez de confiar en el valor recibido:
    así ni ``..`` ni un separador de ruta pueden llegar al sistema de ficheros, aunque el
    llamante pase algo que solo se parezca a un UUID.
    """
    raw = str(workflow_id)
    try:
        canonical = str(UUID(raw))
    except ValueError as exc:
        raise WorkflowCheckpointInvalidError(
            f"workflow_id no utilizable como nombre de carpeta: {raw!r}"
        ) from exc
    if canonical != raw:
        raise WorkflowCheckpointInvalidError(
            f"workflow_id no canónico: {raw!r} debería ser {canonical!r}"
        )
    return canonical


def _assert_persistable(run: WorkflowRun) -> None:
    """Rechaza runs que no se pueden persistir sin afirmar un cierre falso.

    Un estado terminal exige ``completed_at``, y ``COMPLETED``/``FAILED`` exigen además
    ``result``: son los dos estados que **afirman un desenlace**, y sin resultado esa
    afirmación no está demostrada. Sin esta comprobación, el kernel podría quedar falsamente
    ``COMPLETED`` y una reanudación posterior no tendría nada que reanudar. ``CANCELLED`` sí se
    admite sin ``result``: cancelar cierra el workflow sin producir un desenlace técnico.
    """
    if run.status not in TERMINAL_WORKFLOW_STATUSES:
        return
    if run.completed_at is None:
        raise WorkflowCheckpointInvalidError(
            f"no se puede guardar el run {run.workflow_id} en {run.status.value}: un estado "
            "terminal exige completed_at"
        )
    if run.status in _RESULT_REQUIRED_STATUSES and run.result is None:
        raise WorkflowCheckpointInvalidError(
            f"no se puede guardar el run {run.workflow_id} en {run.status.value} sin result: "
            "un estado terminal no puede afirmar un desenlace que el run no demuestra"
        )


def _serialize(model: WorkflowRun | WorkflowCheckpoint) -> bytes:
    """Bytes exactos que se escriben en disco.

    Se centraliza aquí porque el ``digest`` y ``bytes_written`` se calculan sobre **estos**
    bytes: cualquier otra ruta de serialización invalidaría los metadatos.
    """
    return (model.model_dump_json(indent=2) + "\n").encode("utf-8")


def _checkpoint_name(sequence: int) -> str:
    """Nombre de fichero de una secuencia."""
    return f"{sequence:0{_SEQUENCE_WIDTH}d}{_CHECKPOINT_SUFFIX}"


def _next_sequence(meta_directory: Path, revision: int) -> int:
    """Secuencia del checkpoint que se va a escribir.

    ``max(revision, última + 1)``: nunca por debajo de la revisión del run y siempre por encima
    del último checkpoint confirmado, de modo que dos guardados del mismo ``revision`` no se
    pisan.
    """
    sequences = _committed_sequences(meta_directory)
    last = max(sequences, default=-1)
    return max(revision, last + 1)


def _committed_sequences(meta_directory: Path) -> tuple[int, ...]:
    """Secuencias de los checkpoints confirmados, ordenadas de menor a mayor.

    Solo cuenta ficheros ``<número>.json``. Un nombre que no sea un número no es un checkpoint
    de este formato, así que se ignora; los temporales de la escritura atómica nunca encajan.
    """
    if not meta_directory.is_dir():
        return ()
    sequences = [
        int(entry.stem)
        for entry in meta_directory.iterdir()
        if entry.is_file() and entry.suffix == _CHECKPOINT_SUFFIX and entry.stem.isdigit()
    ]
    return tuple(sorted(sequences))


def _read_bytes(path: Path) -> bytes:
    """Lee el contenido de un checkpoint, traduciendo cualquier fallo de E/S a error de dominio."""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise WorkflowCheckpointInvalidError(
            f"no se pudo leer el checkpoint {path}: {exc}"
        ) from exc


def _read_checkpoint(path: Path) -> WorkflowCheckpoint:
    """Lee y valida los metadatos de un checkpoint."""
    payload = _read_bytes(path)
    try:
        return WorkflowCheckpoint.model_validate_json(payload)
    except ValidationError as exc:
        raise WorkflowCheckpointInvalidError(
            f"los metadatos del checkpoint {path} no son válidos: {exc}"
        ) from exc


def _parse_run(payload: bytes, path: Path) -> WorkflowRun:
    """Interpreta el contenido de un checkpoint como ``WorkflowRun``."""
    try:
        return WorkflowRun.model_validate_json(payload)
    except ValidationError as exc:
        raise WorkflowCheckpointInvalidError(
            f"el checkpoint {path} no contiene un WorkflowRun válido: {exc}"
        ) from exc


def _atomic_write(path: Path, payload: bytes) -> None:
    """Publica ``payload`` en ``path`` de forma atómica.

    Se escribe en un temporal del **mismo** directorio (para que ``os.replace`` sea atómico:
    mismo sistema de ficheros), se vuelca a disco y se renombra. Si algo falla, el temporal se
    borra: el directorio no acumula restos y nadie puede leer un fichero a medias.
    """
    handle, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


__all__ = [
    "CheckpointStore",
    "FileCheckpointStore",
    "completed_idempotency_keys",
    "next_pending_step",
    "step_idempotency_key",
    "validate_checkpoint",
]
