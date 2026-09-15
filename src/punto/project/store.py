"""Almacén durable del agregado ``ProjectRun`` (ENGINE-6.2).

Es el homólogo de ``punto.workflow.checkpoints`` un nivel más arriba: donde el kernel de workflow
persiste ``WorkflowRun``, el kernel de proyecto persiste ``ProjectRun``. El problema es el mismo
—una ejecución autónoma tiene que poder reanudarse tras una caída **sin repetir nodos ya pagados** y
**sin poder declararse terminada en falso**— y la respuesta también: JSON en un directorio local,
escritura atómica y detección de corrupción en vez de reparación silenciosa.

Por qué un almacén propio y no reutilizar el de checkpoints: el agregado es otro (``ProjectRun``
tiene nodos, presupuesto y revisiones de workspace; ``WorkflowRun`` tiene pasos y efectos), la
política de acotado es otra (aquí se conservan los últimos ``MAX_PROJECT_SNAPSHOTS`` snapshots, no
todos) y, sobre todo, un proyecto y su child workflow son **dos verdades distintas** que se
referencian por identificador. Compartir almacén acoplaría dos ciclos de vida que el contrato quiere
separados; imitar el patrón cuesta menos que unificarlos mal.

Formato en disco
----------------
Para cada proyecto, bajo ``root/<project_run_id>/``:

- ``<secuencia:04d>.json``: el ``ProjectRun`` serializado con ``run.model_dump_json()``. Es el
  contenido durable, y su sha256 es el ``digest`` del snapshot.
- ``meta/<secuencia:04d>.json``: el :class:`ProjectSnapshot` con la misma convención de
  serialización. Es el **marcador de commit**: un payload sin sus metadatos es el resto de una
  escritura interrumpida y **nunca** se lista ni se carga.

Regla de secuencia
------------------
``secuencia = última_confirmada + 1``, empezando en 1. Es **monótona estricta**: cada ``save``
ocupa una secuencia nueva y **nunca** pisa la anterior. Es deliberado y es lo que el kernel
necesita: guarda tras cada hito, de modo que dos guardados del mismo ``run.revision`` —cosa que
ocurre al re-guardar tras una reanudación sin transición nueva— tienen que quedar los dos. La
historia del proyecto no se reescribe; si se pudiera pisar, un hito confirmado desaparecería del
disco y la auditoría del proyecto perdería justo el tramo que se quiere explicar.

Por eso la secuencia **no** se deriva de ``run.revision`` (a diferencia de los checkpoints del
workflow, donde ``sequence = max(revision, última + 1)``): aquí la revisión la lleva el agregado y
la secuencia la lleva el almacén, y confundirlas haría que el almacén pudiera perder un snapshot.
Dadas las mismas llamadas en el mismo orden sobre el mismo directorio, las secuencias son las
mismas; la secuencia depende solo de lo que hay en disco, nunca del reloj ni del azar.

Escritura atómica (consistencia ante caídas)
--------------------------------------------
Cada fichero se escribe primero en un temporal **del mismo directorio** y después se publica con
``os.replace``, que en el mismo sistema de ficheros es atómico: un lector concurrente ve el fichero
anterior o el nuevo, nunca uno a medias. El contenido se vuelca a disco (``fsync``) antes de
publicarlo. Y el orden es siempre **payload primero, metadatos después**: un proceso que muere a
mitad deja, como mucho, un payload huérfano e invisible, jamás un ``meta`` que apunte a un payload
que no existe.

Acotado
-------
Se conservan los ``MAX_PROJECT_SNAPSHOTS`` snapshots **más recientes** de cada proyecto; al
superarlos se eliminan los más antiguos, empezando por sus metadatos (descommit) y siguiendo por su
payload. El orden importa: borrar primero el marcador de commit hace que una caída a mitad del
recorte deje un payload huérfano —invisible— en vez de un meta roto. El último confirmado no se
toca nunca, porque el recorte siempre conserva la cola de la lista ordenada.

Verificación al leer
--------------------
Leer es una operación de **confianza cero**. ``load`` comprueba, en este orden: que la secuencia
declarada por los metadatos sea la que impone el nombre del fichero; que el sha256 de los bytes
reales coincida con el ``digest`` del snapshot; que el payload exista (si el meta apunta a una
secuencia cuyo fichero no está, **falla**, no retrocede al anterior); que el contenido sea un
``ProjectRun``; y que el run y el snapshot digan lo mismo (mismo proyecto, mismo estado, mismos
nodos completados). Cualquier anomalía es :class:`ProjectStoreError`: la corrupción se detecta y se
reporta, nunca se repara en silencio, porque reparar sería reanudar un estado que nadie guardó.

``latest`` verifica además el payload del snapshot que devuelve. Se separa aquí del
``FileCheckpointStore`` del workflow (que solo lee metadatos) porque el kernel decide con ese
snapshot si puede continuar: devolver unas coordenadas que luego no se pueden cargar convertiría la
detección de corrupción en un fallo tardío, en mitad de la reanudación.

Qué **no** hace este módulo
---------------------------
No juzga el estado del proyecto: no exige ``completed_at`` en los estados terminales ni rechaza un
cierre sin resultado. Esa autoridad es del ``ProjectStateMachine``, y una segunda opinión aquí
crearía dos verdades sobre lo mismo. El almacén guarda lo que el kernel decide y lo devuelve
íntegro; la coherencia del ciclo de vida se valida donde se decide, no donde se escribe.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from punto.common import utc_now
from punto.schemas.project import ProjectRun, ProjectState

#: Snapshots conservados por proyecto. Es una cota de disco, no de corrección: los más recientes
#: son los que permiten reanudar y explicar qué pasó, y un proyecto que se reanuda muchas veces no
#: puede crecer sin límite.
MAX_PROJECT_SNAPSHOTS: Final[int] = 32

#: Subdirectorio donde viven los metadatos (el marcador de commit de cada snapshot).
_META_DIRNAME: Final[str] = "meta"
#: Sufijo de todo fichero de snapshot.
_SNAPSHOT_SUFFIX: Final[str] = ".json"
#: Ancho mínimo del número de secuencia. Los nombres se ordenan por número, no por texto.
_SEQUENCE_WIDTH: Final[int] = 4
#: Prefijo y sufijo de los temporales de escritura atómica. Empiezan por punto para no confundirse
#: con un snapshot y terminan en ``.tmp`` para que ningún glob ni recuento los alcance.
_TEMP_PREFIX: Final[str] = ".snapshot-"
_TEMP_SUFFIX: Final[str] = ".tmp"


class ProjectStoreError(RuntimeError):
    """El almacén del proyecto no puede leer o escribir un estado válido."""


class ProjectSnapshot(BaseModel):
    """Metadatos del snapshot confirmado de un proyecto.

    Es el marcador de commit: dice **qué** secuencia está confirmada, **de qué** proyecto, en qué
    estado quedó y con qué ``digest`` se pueden verificar sus bytes. No lleva el ``ProjectRun``: el
    payload vive en su propio fichero y este objeto solo lo describe, de modo que un listado de
    snapshots no obliga a interpretar el estado completo de cada uno.

    ``nodes_completed`` se copia de ``run.usage.nodes_completed`` para poder responder «cuánto
    avanzó» sin cargar el agregado, y al leer se comprueba que siga coincidiendo con el payload:
    si no coincide, los metadatos describen a otro run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(..., ge=0)
    project_run_id: UUID = Field(...)
    status: ProjectState = Field(...)
    nodes_completed: int = Field(default=0, ge=0)
    digest: str = Field(default="", max_length=64)
    created_at: datetime = Field(default_factory=utc_now)


class ProjectStore(Protocol):
    """Contrato de persistencia del agregado ``ProjectRun``.

    Se declara como ``Protocol`` y no como clase base para que el kernel dependa de la
    **capacidad** —guardar, leer el último, cargar, listar— y no de una implementación concreta:
    una prueba puede inyectar el doble en memoria y el kernel no cambia una línea.
    """

    def save(self, run: ProjectRun) -> ProjectSnapshot:
        """Persiste ``run`` como una secuencia nueva y devuelve el snapshot escrito.

        Nunca pisa un snapshot anterior: cada llamada ocupa la secuencia siguiente, y un fallo a
        mitad no puede publicar metadatos que apunten a un payload inexistente.
        """
        ...

    def latest(self, project_run_id: UUID) -> ProjectSnapshot | None:
        """Snapshot confirmado de secuencia mayor, o ``None`` si el proyecto no tiene ninguno.

        Verifica el payload de ese snapshot: devolver coordenadas que luego no se pueden cargar
        convertiría la detección de corrupción en un fallo tardío.
        """
        ...

    def load(self, project_run_id: UUID) -> ProjectRun:
        """Devuelve el run del último snapshot, validándolo **siempre** y por completo.

        Comprueba la integridad del payload contra su ``digest`` y la coherencia entre run y
        snapshot. La corrupción se **detecta y se reporta**; nunca se salta al snapshot anterior
        en silencio, porque eso sería reanudar un estado que no es el que se guardó.
        """
        ...

    def list_snapshots(self, project_run_id: UUID) -> tuple[ProjectSnapshot, ...]:
        """Snapshots confirmados del proyecto, ordenados por secuencia ascendente."""
        ...


class FileProjectStore:
    """Almacén en disco, por proyecto, con escritura atómica y digest verificado.

    Un proyecto, un directorio. Dentro, un JSON por snapshot y un subdirectorio ``meta`` con sus
    marcadores de commit. Nada compartido entre proyectos: no hay contención ni orden global que
    mantener, y el estado de un proyecto no puede filtrarse al de otro.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """Directorio raíz donde se escribe un subdirectorio por proyecto."""
        return self._root

    def save(self, run: ProjectRun) -> ProjectSnapshot:
        """Persiste ``run`` de forma atómica y devuelve sus metadatos.

        La identidad del proyecto se valida **antes** de tocar el disco, así que un
        ``project_run_id`` que no sirva como nombre de carpeta no deja restos. Después se escribe
        el payload y, solo cuando está publicado, su marcador de commit.

        Raises:
            ProjectStoreError: la identidad no es un nombre de carpeta seguro, o la escritura
                falló. Un fallo deja como mucho un payload huérfano, nunca un meta roto.
        """
        directory = self._project_dir(run.project_run_id)
        meta_directory = directory / _META_DIRNAME
        sequence = _next_sequence(meta_directory)
        payload = _serialize(run)
        snapshot = ProjectSnapshot(
            sequence=sequence,
            project_run_id=run.project_run_id,
            status=run.status,
            nodes_completed=run.usage.nodes_completed,
            digest=hashlib.sha256(payload).hexdigest(),
        )
        try:
            meta_directory.mkdir(parents=True, exist_ok=True)
            # Contenido primero, metadatos después: los metadatos son el marcador de commit.
            _atomic_write(directory / _snapshot_name(sequence), payload)
            _atomic_write(meta_directory / _snapshot_name(sequence), _serialize(snapshot))
        except OSError as exc:
            raise ProjectStoreError(
                f"no se pudo escribir el snapshot {sequence} del proyecto "
                f"{run.project_run_id}: {exc}"
            ) from exc
        _prune(directory, meta_directory)
        return snapshot

    def latest(self, project_run_id: UUID) -> ProjectSnapshot | None:
        """Snapshot confirmado de secuencia mayor, o ``None`` si el proyecto no tiene ninguno.

        «Último» es la secuencia mayor **con marcador de commit**. Sus metadatos y su payload se
        verifican antes de devolverlos: si el meta apunta a un fichero que no existe —proceso
        muerto a mitad, o borrado a mano— se falla en vez de devolver una posición irrecuperable.

        Raises:
            ProjectStoreError: los metadatos no corresponden a su nombre, faltan los bytes del
                payload, el digest no cuadra, o la identidad no es un nombre de carpeta seguro.
        """
        found = self._latest(project_run_id)
        return None if found is None else found[0]

    def load(self, project_run_id: UUID) -> ProjectRun:
        """Carga el run del último snapshot y lo valida **siempre** y por completo.

        La secuencia sale del nombre del fichero de metadatos —el marcador de commit—, nunca del
        contenido: si el último snapshot está corrupto o sus metadatos no le corresponden, se falla
        en vez de bajar al penúltimo en silencio.

        Raises:
            ProjectStoreError: no hay ningún snapshot, falta el payload, el digest no cuadra, el
                contenido no es un ``ProjectRun``, o el run y los metadatos no dicen lo mismo.
        """
        found = self._latest(project_run_id)
        if found is None:
            raise ProjectStoreError(
                f"el proyecto {project_run_id} no tiene ningún snapshot en "
                f"{self._project_dir(project_run_id)}"
            )
        snapshot, payload = found
        path = self._project_dir(project_run_id) / _snapshot_name(snapshot.sequence)
        run = _parse_run(payload, path)
        if run.project_run_id != project_run_id:
            raise ProjectStoreError(
                f"{path} contiene el proyecto {run.project_run_id} en lugar de {project_run_id}: "
                "los metadatos describen a otro run"
            )
        if run.status != snapshot.status or run.usage.nodes_completed != snapshot.nodes_completed:
            raise ProjectStoreError(
                f"{path} no coincide con su snapshot: el payload declara {run.status.value} con "
                f"{run.usage.nodes_completed} nodos completados y el snapshot "
                f"{snapshot.status.value} con {snapshot.nodes_completed}"
            )
        return run

    def list_snapshots(self, project_run_id: UUID) -> tuple[ProjectSnapshot, ...]:
        """Snapshots confirmados del proyecto, en orden creciente de secuencia.

        Solo aparecen los confirmados (con marcador de commit). Un payload huérfano de una escritura
        interrumpida no se lista: no es un estado del que se pueda reanudar. Listar no verifica los
        bytes —es un inventario, no una carga—: la garantía la da ``load``.

        Raises:
            ProjectStoreError: unos metadatos no declaran la secuencia que les da su nombre, o la
                identidad no es un nombre de carpeta seguro.
        """
        meta_directory = self._project_dir(project_run_id) / _META_DIRNAME
        snapshots = []
        for sequence in _committed_sequences(meta_directory):
            path = meta_directory / _snapshot_name(sequence)
            snapshot = _read_snapshot(path)
            _assert_sequence_matches_name(snapshot, sequence, path)
            snapshots.append(snapshot)
        return tuple(snapshots)

    def _project_dir(self, project_run_id: UUID) -> Path:
        """Directorio del proyecto, con el identificador saneado como nombre de carpeta."""
        return self._root / _safe_project_name(project_run_id)

    def _latest(self, project_run_id: UUID) -> tuple[ProjectSnapshot, bytes] | None:
        """Snapshot confirmado de secuencia mayor y sus bytes verificados."""
        directory = self._project_dir(project_run_id)
        meta_directory = directory / _META_DIRNAME
        sequences = _committed_sequences(meta_directory)
        if not sequences:
            return None
        sequence = max(sequences)
        meta_path = meta_directory / _snapshot_name(sequence)
        snapshot = _read_snapshot(meta_path)
        _assert_sequence_matches_name(snapshot, sequence, meta_path)
        if snapshot.project_run_id != project_run_id:
            raise ProjectStoreError(
                f"el snapshot {meta_path} es del proyecto {snapshot.project_run_id} y no de "
                f"{project_run_id}: los metadatos describen a otro run"
            )
        payload_path = directory / _snapshot_name(sequence)
        payload = _read_bytes(payload_path)
        _assert_digest(payload, snapshot, payload_path)
        return snapshot, payload


class InMemoryProjectStore:
    """Almacén en memoria con el mismo contrato, para pruebas y para usos efímeros.

    Reproduce la misma regla de secuencia, el mismo acotado y el mismo fallo cerrado que el almacén
    en disco, pero no puede corromperse a sí mismo: no hay bytes que manipular, así que aquí no hay
    verificación de digest que hacer. Los ``ProjectRun`` son inmutables por contrato, de modo que
    guardar la referencia no expone el estado a mutaciones posteriores del llamante.
    """

    def __init__(self) -> None:
        self._snapshots: dict[UUID, list[tuple[ProjectSnapshot, ProjectRun]]] = {}

    def save(self, run: ProjectRun) -> ProjectSnapshot:
        """Persiste ``run`` como una secuencia nueva y devuelve el snapshot escrito."""
        entries = self._snapshots.setdefault(run.project_run_id, [])
        sequence = (entries[-1][0].sequence + 1) if entries else 1
        digest = hashlib.sha256(_serialize(run)).hexdigest()
        snapshot = ProjectSnapshot(
            sequence=sequence,
            project_run_id=run.project_run_id,
            status=run.status,
            nodes_completed=run.usage.nodes_completed,
            digest=digest,
        )
        entries.append((snapshot, run))
        del entries[:-MAX_PROJECT_SNAPSHOTS]
        return snapshot

    def latest(self, project_run_id: UUID) -> ProjectSnapshot | None:
        """Snapshot confirmado de secuencia mayor, o ``None`` si el proyecto no tiene ninguno."""
        entries = self._snapshots.get(project_run_id)
        return None if not entries else entries[-1][0]

    def load(self, project_run_id: UUID) -> ProjectRun:
        """Devuelve el run del último snapshot.

        Raises:
            ProjectStoreError: el proyecto no tiene ningún snapshot guardado.
        """
        entries = self._snapshots.get(project_run_id)
        if not entries:
            raise ProjectStoreError(f"el proyecto {project_run_id} no tiene ningún snapshot")
        return entries[-1][1]

    def list_snapshots(self, project_run_id: UUID) -> tuple[ProjectSnapshot, ...]:
        """Snapshots confirmados del proyecto, en orden creciente de secuencia."""
        entries = self._snapshots.get(project_run_id)
        return () if not entries else tuple(snapshot for snapshot, _ in entries)


def _safe_project_name(project_run_id: UUID) -> str:
    """Nombre de carpeta derivado del ``project_run_id``.

    Se reconstruye el UUID a partir de su forma textual en vez de confiar en el valor recibido: así
    ni ``..`` ni un separador de ruta pueden llegar al sistema de ficheros, aunque el llamante pase
    algo que solo se parezca a un UUID. Un almacén que escribe donde le digan es un almacén que
    puede escribir fuera de ``root``.
    """
    raw = str(project_run_id)
    try:
        canonical = str(UUID(raw))
    except ValueError as exc:
        raise ProjectStoreError(
            f"project_run_id no utilizable como nombre de carpeta: {raw!r}"
        ) from exc
    if canonical != raw:
        raise ProjectStoreError(
            f"project_run_id no canónico: {raw!r} debería ser {canonical!r}"
        )
    return canonical


def _serialize(model: ProjectRun | ProjectSnapshot) -> bytes:
    """Bytes exactos que se escriben en disco.

    Se centraliza aquí porque el ``digest`` se calcula sobre **estos** bytes y se verifica contra
    ellos: cualquier otra ruta de serialización invalidaría los metadatos.
    """
    return model.model_dump_json().encode("utf-8")


def _snapshot_name(sequence: int) -> str:
    """Nombre de fichero de una secuencia."""
    return f"{sequence:0{_SEQUENCE_WIDTH}d}{_SNAPSHOT_SUFFIX}"


def _next_sequence(meta_directory: Path) -> int:
    """Secuencia del snapshot que se va a escribir.

    ``última confirmada + 1``, empezando en 1: dos guardados del mismo ``run.revision`` ocupan
    secuencias distintas en vez de pisarse, y el almacén nunca reescribe la historia.
    """
    return max(_committed_sequences(meta_directory), default=0) + 1


def _committed_sequences(meta_directory: Path) -> tuple[int, ...]:
    """Secuencias de los snapshots confirmados, ordenadas de menor a mayor.

    Solo cuenta ficheros ``<número>.json``. Un nombre que no sea un número no es un snapshot de
    este formato y se ignora; los temporales de la escritura atómica nunca encajan.
    """
    if not meta_directory.is_dir():
        return ()
    sequences = [
        int(entry.stem)
        for entry in meta_directory.iterdir()
        if entry.is_file() and entry.suffix == _SNAPSHOT_SUFFIX and entry.stem.isdigit()
    ]
    return tuple(sorted(sequences))


def _prune(directory: Path, meta_directory: Path) -> None:
    """Borra los snapshots más antiguos hasta respetar ``MAX_PROJECT_SNAPSHOTS``.

    La lista llega ordenada de menor a mayor, así que ``[:-MAX]`` es exactamente «todo menos los
    más recientes»: el último confirmado —el que se acaba de escribir— no se toca nunca. Se
    descommitea antes de borrar el payload, para que una caída a mitad del recorte deje un fichero
    huérfano e invisible en vez de un meta que apunte a un payload inexistente.
    """
    for sequence in _committed_sequences(meta_directory)[:-MAX_PROJECT_SNAPSHOTS]:
        name = _snapshot_name(sequence)
        try:
            (meta_directory / name).unlink(missing_ok=True)
            (directory / name).unlink(missing_ok=True)
        except OSError as exc:
            raise ProjectStoreError(
                f"no se pudo recortar el snapshot {sequence} de {directory}: {exc}"
            ) from exc


def _assert_sequence_matches_name(
    snapshot: ProjectSnapshot, expected_sequence: int, path: Path
) -> None:
    """El nombre del fichero y la secuencia declarada tienen que ser la misma cosa.

    La secuencia viaja **dentro** de los metadatos, así que puede copiarse de un fichero a otro: si
    la que declaran no es la que les da el nombre, describen a otro snapshot y no al que se está
    leyendo.
    """
    if snapshot.sequence == expected_sequence:
        return
    raise ProjectStoreError(
        f"el snapshot {path} declara la secuencia {snapshot.sequence} y el fichero es de la "
        f"secuencia {expected_sequence}: los metadatos no son de este snapshot"
    )


def _assert_digest(payload: bytes, snapshot: ProjectSnapshot, path: Path) -> None:
    """El ``digest`` del snapshot tiene que describir **estos** bytes.

    Es lo que distingue un estado real de unos bytes manipulados o corrompidos. Se comprueba
    **antes** de interpretar el contenido: unos bytes cuyo digest no cuadra no llegan a convertirse
    en estado del proyecto.
    """
    digest = hashlib.sha256(payload).hexdigest()
    if digest == snapshot.digest:
        return
    raise ProjectStoreError(
        f"el payload {path} no tiene el digest de su snapshot (esperado {snapshot.digest}, "
        f"calculado {digest}): corrupción o manipulación"
    )


def _read_bytes(path: Path) -> bytes:
    """Lee un fichero del almacén, traduciendo cualquier fallo de E/S a error de dominio.

    Un payload que no está —borrado, o nunca escrito porque el proceso murió antes— tiene que
    fallar como almacén inválido, no como ``FileNotFoundError`` perdido en mitad de una reanudación.
    """
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProjectStoreError(f"no se pudo leer el fichero {path}: {exc}") from exc


def _read_snapshot(path: Path) -> ProjectSnapshot:
    """Lee y valida los metadatos de un snapshot."""
    payload = _read_bytes(path)
    try:
        return ProjectSnapshot.model_validate_json(payload)
    except ValidationError as exc:
        raise ProjectStoreError(
            f"los metadatos del snapshot {path} no son válidos: {exc}"
        ) from exc


def _parse_run(payload: bytes, path: Path) -> ProjectRun:
    """Interpreta el payload de un snapshot como ``ProjectRun``."""
    try:
        return ProjectRun.model_validate_json(payload)
    except ValidationError as exc:
        raise ProjectStoreError(
            f"el snapshot {path} no contiene un ProjectRun válido: {exc}"
        ) from exc


def _atomic_write(path: Path, payload: bytes) -> None:
    """Publica ``payload`` en ``path`` de forma atómica.

    Se escribe en un temporal del **mismo** directorio (para que ``os.replace`` sea atómico: mismo
    sistema de ficheros), se vuelca a disco y se renombra. Si algo falla, el temporal se borra: el
    directorio no acumula restos y nadie puede leer un fichero a medias.
    """
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX)
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
    "MAX_PROJECT_SNAPSHOTS",
    "FileProjectStore",
    "InMemoryProjectStore",
    "ProjectSnapshot",
    "ProjectStore",
    "ProjectStoreError",
]
