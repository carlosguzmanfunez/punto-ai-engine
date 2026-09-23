"""Snapshot durable en disco y rollback seguro de una reparación (ENGINE-6.1).

El problema que resuelve
------------------------
Una reparación autónoma muta el árbol de trabajo. Si después la verificación vuelve a reproducir el
defecto, hay que **deshacer** lo hecho, y «deshacer» tiene que significar exactamente eso: dejar los
bytes que había. Este módulo no confía en el control de versiones para eso —el workspace puede tener
trabajo sin confirmar que un ``checkout`` destruiría, y el motor no puede depender del estado de git
para reparar su propia mutación— ni en la memoria del proceso que aplicó el cambio, que es
precisamente lo que se pierde en la caída que este mecanismo existe para sobrevivir.

Dos piezas, con papeles distintos
---------------------------------
- El **registro** es :class:`~punto.schemas.repair.RepairSnapshot`, con su
  :class:`~punto.schemas.repair.RepairSnapshotEntry` por archivo: ruta, sha256, tamaño y si existía.
  Ese registro viaja dentro del checkpoint y no guarda ni un byte de contenido.
- Las **copias** son lo que hace real el rollback: :meth:`FileRepairSnapshots.create` escribe el
  contenido previo en ``<root>/.punto-repair-snapshots/<snapshot_id>/``. Sin esa copia el módulo
  podría demostrar que un archivo cambió, pero no devolverlo a su estado anterior: un hash no se
  restaura. La copia vive **fuera del control de versiones** —junto a ella se deja un ``.gitignore``
  con ``*`` para que eso sea un hecho y no una intención— y nunca captura un almacén de secretos: un
  ``.env`` o una clave privada copiados a un directorio del workspace serían un segundo sitio donde
  filtrar lo mismo.

Rollback: solo si se puede demostrar seguro
-------------------------------------------
:meth:`FileRepairSnapshots.rollback` es todo o nada. Antes de escribir un byte comprueba que la
reparación no pudo tener efectos externos y que **el estado actual de cada archivo es el que la
reparación dejó** (``expected``); si algo no cuadra devuelve
:class:`RollbackVerdict` con ``WORKFLOW_REPAIR_RECONCILIATION_REQUIRED`` y no toca nada. Restaurar a
ciegas sobre un árbol que ya cambió por otra vía —una persona, otra herramienta, un paso posterior—
destruiría trabajo que nadie pidió destruir; por eso la incertidumbre se declara, no se resuelve
adivinando. Cuando todo cuadra, los archivos que existían se reescriben byte a byte desde la copia y
los que no existían se borran.

Errores
-------
**Entrada malformada** —ruta vacía, absoluta, con ``..``, que resuelve fuera de la raíz, un almacén
de secretos, un directorio, más entradas de las que admite el contrato— es ``ValueError``: es un
defecto de quien llama y se detecta antes de tocar el disco. Un **veredicto** de rollback no lanza:
sale como :class:`RollbackVerdict`.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

from punto.common import basename_of, normalize_path
from punto.schemas.repair import (
    MAX_REPAIR_SNAPSHOT_ENTRIES,
    RepairSnapshot,
    RepairSnapshotEntry,
)
from punto.schemas.workflow import WorkflowFailureCode

#: Directorio, relativo a la raíz del workspace, donde viven las copias de seguridad. Empieza por
#: punto para quedar fuera de cualquier glob de paquetes y de la vista normal de un proyecto.
SNAPSHOT_DIR_NAME: Final[str] = ".punto-repair-snapshots"
#: Contenido del ``.gitignore`` de ese directorio. La copia lleva contenido real de archivos: que no
#: se versione no puede depender de que alguien se acuerde de excluirla.
_SNAPSHOT_GITIGNORE: Final[str] = (
    "# Copias de seguridad de reparaciones autónomas (ENGINE-6.1).\n"
    "# Contienen el contenido real de archivos previos a una mutación: no se versionan.\n"
    "*\n"
)
#: Nombres base que jamás se copian: son almacenes de secretos, y la copia es contenido en claro.
_SECRET_BASENAMES: Final[tuple[str, ...]] = (
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "secrets.yaml",
    "secrets.yml",
)
#: Prefijos de nombre base igual de explícitos (``.env``, ``.env.local``, ``.env.production``).
_SECRET_PREFIXES: Final[tuple[str, ...]] = (".env",)
#: Sufijos de material criptográfico.
_SECRET_SUFFIXES: Final[tuple[str, ...]] = (
    ".jks",
    ".kdbx",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
)
#: Cota de ``RepairSnapshotEntry.path``; se comprueba aquí para no construir un contrato inválido.
_MAX_ENTRY_PATH_CHARS: Final[int] = 400
#: Cota de ``RepairSnapshot.workspace_path``.
_MAX_WORKSPACE_PATH_CHARS: Final[int] = 400
#: Prefijo absoluto con unidad (``C:\...``): en Windows ``/x`` no es absoluto.
_WINDOWS_DRIVE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]:")
#: Temporales de la escritura atómica: empiezan por punto y terminan en ``.tmp``.
_TEMP_PREFIX: Final[str] = ".snapshot-"
_TEMP_SUFFIX: Final[str] = ".tmp"


@dataclass(frozen=True, slots=True)
class RollbackVerdict:
    """Resultado de un intento de rollback: hecho, o rechazado con su motivo.

    ``rolled_back=False`` significa que **no se tocó nada** salvo cuando ``restored_files`` no está
    vacío: ese caso es un rollback interrumpido a mitad de la escritura, y entonces el veredicto
    lleva el código de reconciliación y la lista de lo que sí se había restaurado, porque un fallo
    parcial contado como «no pasó nada» sería peor que el fallo.
    """

    rolled_back: bool
    code: WorkflowFailureCode | None = None
    detail: str = ""
    restored_files: tuple[str, ...] = ()


class FileRepairSnapshots:
    """Captura y restaura el estado de archivos locales de una reparación.

    La raíz se inyecta y no se lee del entorno: las pruebas usan un directorio temporal y el kernel
    el workspace real, y ninguna ruta de este módulo se construye con algo que no cuelgue de esa
    raíz. La instancia no guarda estado mutable: el registro durable es el
    :class:`~punto.schemas.repair.RepairSnapshot`, que viaja en el checkpoint, y las copias son
    archivos en disco que se pueden volver a leer en otro proceso.
    """

    __slots__ = ("_fence", "_root")

    def __init__(self, root: Path, *, fence: Callable[[], None] | None = None) -> None:
        self._root = Path(root)
        self._fence = fence

    @property
    def root(self) -> Path:
        """Raíz del workspace sobre la que se captura y se restaura."""
        return self._root

    def create(
        self,
        *,
        repair_id: UUID,
        cycle: int,
        paths: Sequence[str],
        workspace_path: str = "",
    ) -> RepairSnapshot:
        """Captura el estado previo de ``paths`` y deja la copia que permite deshacer.

        Para cada ruta guarda sha256, tamaño y si existía, y copia el contenido a
        ``<root>/.punto-repair-snapshots/<snapshot_id>/`` conservando la estructura de
        directorios. El orden es determinista —único y ordenado por ruta normalizada— para que dos
        capturas del mismo conjunto produzcan el mismo registro aunque el llamante las pase en
        otro orden.

        El archivo se lee **una sola vez**: el hash y la copia salen de los mismos bytes, así que
        un cambio del archivo a mitad de la captura no puede producir un registro que describa un
        estado y una copia que contenga otro.

        Args:
            repair_id: Reparación a la que pertenece el snapshot.
            cycle: Ciclo de reparación (el contrato exige 1 o más).
            paths: Rutas relativas a la raíz que se capturan antes de mutarlas.
            workspace_path: Etiqueta del workspace para el registro. Vacía significa «usa la raíz».

        Returns:
            El ``RepairSnapshot`` con sus entradas y su ``workspace_fingerprint`` calculado.

        Raises:
            ValueError: Si una ruta está vacía, es absoluta, contiene ``..``, sale de la raíz,
                apunta a un almacén de secretos, no es un archivo regular, o si hay más rutas de
                las que admite el contrato. En todos los casos se detecta antes de dejar una copia
                a medias.
        """
        targets = _unique_paths(paths)
        if len(targets) > MAX_REPAIR_SNAPSHOT_ENTRIES:
            raise ValueError(
                f"el snapshot pide {len(targets)} archivos y el contrato admite "
                f"{MAX_REPAIR_SNAPSHOT_ENTRIES}: un registro mayor no se podría volver a validar"
            )
        if self._fence is not None:
            self._fence()
        snapshot_id = uuid4()
        backup_root = self._snapshot_dir(snapshot_id)
        backup_root.mkdir(parents=True, exist_ok=True)
        _ensure_ignored(self._root / SNAPSHOT_DIR_NAME)
        entries: list[RepairSnapshotEntry] = []
        try:
            for path in targets:
                target = self._resolve(path)
                entry, data = self._capture(path, target)
                if data is not None:
                    backup = backup_root.joinpath(*path.split("/"))
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write(backup, data)
                entries.append(entry)
        except BaseException:
            # Una captura a medias no deja copias huérfanas: el snapshot no existe para nadie
            # todavía, así que borrar su directorio es la única limpieza honesta.
            shutil.rmtree(backup_root, ignore_errors=True)
            raise
        snapshot = RepairSnapshot(
            snapshot_id=snapshot_id,
            repair_id=repair_id,
            cycle=cycle,
            workspace_path=_workspace_label(workspace_path, self._root),
            entries=tuple(entries),
        )
        return snapshot.model_copy(
            update={"workspace_fingerprint": self.fingerprint(snapshot)}
        )

    def digest(self, path: str) -> str:
        """sha256 hex del archivo, o ``""`` si no existe.

        ``""`` es la representación de «no existe» en todo el módulo: así el hash de un archivo
        ausente y el de uno presente nunca se confunden, y el rollback compara el estado actual con
        el esperado sin una segunda consulta de existencia que podría desincronizarse.

        Raises:
            ValueError: Si la ruta sale de la raíz, no es un archivo regular o no se puede leer.
        """
        target = self._resolve(path)
        if not target.exists():
            return ""
        data = _read_regular(target, path)
        return hashlib.sha256(data).hexdigest()

    def fingerprint(self, snapshot: RepairSnapshot) -> str:
        """sha256 canónico de las entradas del snapshot.

        Es la huella que permite comparar dos capturas del mismo conjunto de archivos sin depender
        del orden en que llegaron ni del reloj: se ordenan las entradas por ruta normalizada y se
        hashea una línea por entrada con ruta, hash, tamaño y existencia. Un registro manipulado —o
        capturado en otro árbol— produce otra huella, y esa diferencia es lo que hace auditable el
        snapshot.
        """
        hasher = hashlib.sha256()
        for entry in sorted(snapshot.entries, key=lambda item: normalize_path(item.path)):
            line = (
                f"{normalize_path(entry.path)}\0{entry.sha256}\0{entry.bytes}\0"
                f"{int(entry.existed)}\n"
            )
            hasher.update(line.encode("utf-8"))
        return hasher.hexdigest()

    def verify(self, snapshot: RepairSnapshot) -> bool:
        """True si las entradas describen el estado **actual** del árbol.

        Es la comprobación que puede hacer un proceso nuevo tras una caída: releer cada archivo y
        comparar su hash con el capturado, y exigir que los declarados inexistentes sigan sin estar.
        Una entrada inservible —ruta que sale de la raíz, archivo ilegible— no se trata como
        excepción sino como un «no» verificado: la pregunta es si las entradas describen el estado
        actual, y con una entrada que no se puede leer la respuesta es que no se puede afirmar.
        """
        for entry in snapshot.entries:
            try:
                current = self.digest(entry.path)
            except ValueError:
                return False
            if entry.existed:
                if not current or current != entry.sha256:
                    return False
            elif current:
                return False
        return True

    def rollback(
        self,
        *,
        snapshot: RepairSnapshot,
        expected: Mapping[str, str],
        external_side_effects: bool = False,
    ) -> RollbackVerdict:
        """Restaura el estado previo **solo** cuando puede demostrarse seguro.

        Exige dos cosas y ninguna es negociable. Primera: ``external_side_effects=False``, porque
        deshacer archivos no deshace un despliegue, una publicación ni una llamada de red, y un
        rollback de archivos presentado como deshacerlo todo sería una mentira. Segunda: que el hash
        actual de **cada** archivo del snapshot sea el que la reparación dejó (``expected``), lo que
        demuestra que nadie más tocó el árbol desde entonces.

        Si algo no cuadra devuelve
        ``RollbackVerdict(rolled_back=False, code=WORKFLOW_REPAIR_RECONCILIATION_REQUIRED)`` **sin
        tocar nada**: restaurar sobre un árbol cambiado por otra vía destruiría trabajo ajeno. Si
        todo cuadra, los archivos que existían se reescriben byte a byte desde la copia y los que
        no existían se borran.

        Args:
            snapshot: Registro capturado antes de mutar.
            expected: Estado que la reparación dejó, por ruta (``""`` significa «no existe»). Debe
                cubrir todas las entradas: una clave ausente es «no consta», no «no existe», y con
                lo que no consta no se restaura.
            external_side_effects: ``True`` si la reparación pudo producir efectos fuera del árbol.

        Returns:
            ``RollbackVerdict`` con ``rolled_back=True`` y las rutas devueltas a su estado previo
            —incluidas las que se borraron porque no existían— o el rechazo con su motivo.
        """
        if external_side_effects:
            return RollbackVerdict(
                rolled_back=False,
                code=WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
                detail=(
                    "la reparación pudo producir efectos externos (red, publicación, despliegue): "
                    "deshacer archivos no deshace el efecto, así que hace falta reconciliar"
                ),
            )
        try:
            steps = self._plan_rollback(snapshot, expected)
        except ValueError as exc:
            return RollbackVerdict(
                rolled_back=False,
                code=WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
                detail=f"no se restauró nada: {exc}",
            )
        if self._fence is not None:
            self._fence()
        return self._apply_rollback(steps)

    def _resolve(self, path: str) -> Path:
        """Traduce una ruta relativa a una ruta **dentro** de la raíz, o lanza ``ValueError``.

        Se rechaza cualquier ruta absoluta, con ``..`` o que resuelva fuera de la raíz —un enlace
        simbólico que apunte fuera incluido—: restaurar bytes en una ruta que el snapshot no
        controla sería escribir fuera del workspace a partir de un checkpoint, que es entrada no
        confiable.
        """
        declared = _snapshot_path(path)
        candidate = self._root.joinpath(*declared.split("/"))
        root_resolved = self._root.resolve()
        resolved = candidate.resolve()
        if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
            raise ValueError(f"la ruta {path!r} sale de la raíz {self._root}")
        return candidate

    def _capture(self, path: str, target: Path) -> tuple[RepairSnapshotEntry, bytes | None]:
        """Lee el archivo una vez y devuelve su entrada y los bytes que hay que copiar.

        Devuelve ``None`` como bytes cuando el archivo no existía: eso no es un error, es un hecho
        que el snapshot tiene que recordar, porque en el rollback significa «bórralo» y sin esa
        entrada no se sabría si el archivo apareció por la reparación o ya estaba.
        """
        _reject_secret_path(path)
        if not target.exists():
            return RepairSnapshotEntry(path=path, sha256="", bytes=0, existed=False), None
        data = _read_regular(target, path)
        entry = RepairSnapshotEntry(
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data),
            existed=True,
        )
        return entry, data

    def _snapshot_dir(self, snapshot_id: UUID) -> Path:
        """Directorio de copias de un snapshot, dentro de la raíz y sin ambigüedad de nombre.

        El nombre se reconstruye desde el UUID canónico, así que no puede llevar separadores ni
        ``..`` aunque el registro viniera manipulado de un checkpoint.
        """
        return self._root / SNAPSHOT_DIR_NAME / str(snapshot_id)

    def _plan_rollback(
        self, snapshot: RepairSnapshot, expected: Mapping[str, str]
    ) -> tuple[tuple[str, bytes | None], ...]:
        """Valida el rollback completo **antes** de tocar un byte y devuelve qué hacer.

        Un paso por entrada: ``(ruta, bytes)`` para los archivos que existían —con el contenido
        exacto leído de la copia y verificado contra el hash capturado— y ``(ruta, None)`` para los
        que no existían y hay que borrar. Si algo no cuadra lanza ``ValueError`` y no se ha tocado
        nada: el rollback es todo o nada, y esa es la única forma de que «se deshizo» signifique
        algo.
        """
        expected_by_key = {normalize_path(key): value for key, value in expected.items()}
        backup_root = self._snapshot_dir(snapshot.snapshot_id)
        steps: list[tuple[str, bytes | None]] = []
        for entry in snapshot.entries:
            key = normalize_path(entry.path)
            if key not in expected_by_key:
                raise ValueError(
                    f"no consta el estado que la reparación dejó en {entry.path!r}: no se "
                    "restaura a ciegas"
                )
            current = self.digest(entry.path)
            if current != expected_by_key[key]:
                raise ValueError(
                    f"el estado actual de {entry.path!r} ({current or 'ausente'}) no es el que la "
                    f"reparación dejó ({expected_by_key[key] or 'ausente'})"
                )
            if not entry.existed:
                steps.append((entry.path, None))
                continue
            backup = backup_root.joinpath(*_snapshot_path(entry.path).split("/"))
            if not backup.is_file():
                raise ValueError(
                    f"falta la copia de seguridad de {entry.path!r} en {backup_root}: sin ella el "
                    "contenido previo no se puede reconstruir"
                )
            data = backup.read_bytes()
            if hashlib.sha256(data).hexdigest() != entry.sha256:
                raise ValueError(
                    f"la copia de seguridad de {entry.path!r} no coincide con el hash capturado"
                )
            steps.append((entry.path, data))
        return tuple(steps)

    def _apply_rollback(self, steps: tuple[tuple[str, bytes | None], ...]) -> RollbackVerdict:
        """Ejecuta los pasos ya validados y devuelve el veredicto.

        Un fallo de disco a mitad no se disfraza de rechazo limpio: se devuelve un veredicto con el
        código de reconciliación y la lista de lo que sí se restauró, porque el árbol está en un
        estado intermedio y quien lea el resultado tiene que saber exactamente cuál.
        """
        restored: list[str] = []
        for path, data in steps:
            target = self._resolve(path)
            try:
                if data is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write(target, data)
            except OSError as exc:
                return RollbackVerdict(
                    rolled_back=False,
                    code=WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
                    detail=(
                        f"el rollback se interrumpió restaurando {path!r}: {exc}; ya se habían "
                        f"restaurado {len(restored)} archivo(s)"
                    ),
                    restored_files=tuple(restored),
                )
            restored.append(path)
        if not restored:
            return RollbackVerdict(
                rolled_back=True,
                detail="el snapshot no tenía archivos que restaurar",
            )
        return RollbackVerdict(
            rolled_back=True,
            detail=(
                f"restaurados {len(restored)} archivo(s) al estado previo a la reparación: "
                + ", ".join(restored)
            ),
            restored_files=tuple(restored),
        )


def _unique_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """Rutas únicas del snapshot, en orden determinista.

    Se deduplica por ruta **normalizada** —``./src/a.py``, ``src/a.py`` y ``src\\a.py`` son el mismo
    archivo— y se ordena por esa misma forma: dos capturas del mismo conjunto producen el mismo
    registro aunque el llamante las pase en otro orden, y por tanto la misma huella. La ruta que se
    guarda conserva el **caso** que declaró el llamante: el registro tiene que poder señalar el
    archivo real, y normalizar a minúsculas lo cambiaría en un sistema sensible a mayúsculas.
    """
    declared: dict[str, str] = {}
    for path in paths:
        safe = _snapshot_path(path)
        declared.setdefault(normalize_path(safe), safe)
    return tuple(declared[key] for key in sorted(declared))


def _snapshot_path(path: str) -> str:
    """Ruta de una entrada del snapshot, validada y con separador ``/``.

    Se rechaza lo que podría salirse de la raíz —ruta vacía, absoluta (incluida una con unidad de
    Windows, que en Windows no lleva ``/`` inicial) o con ``..``— y también las rutas dentro del
    propio directorio de copias: capturar las copias de un snapshot dentro de otro sería crecer sin
    límite y no proteger nada.

    Raises:
        ValueError: Si la ruta no sirve como entrada de un snapshot.
    """
    stripped = path.strip().replace("\\", "/")
    parts = [part for part in stripped.split("/") if part not in ("", ".")]
    declared = "/".join(parts)
    if not declared:
        raise ValueError(f"ruta del snapshot no utilizable: {path!r}")
    if stripped.startswith("/") or _WINDOWS_DRIVE_RE.match(stripped):
        raise ValueError(f"ruta absoluta en el snapshot: {path!r}")
    if ".." in parts:
        raise ValueError(f"ruta con '..' en el snapshot: {path!r}")
    key = normalize_path(declared)
    if key == SNAPSHOT_DIR_NAME or key.startswith(f"{SNAPSHOT_DIR_NAME}/"):
        raise ValueError(
            f"{path!r} está en {SNAPSHOT_DIR_NAME}: el snapshot no captura sus propias copias"
        )
    if len(declared) > _MAX_ENTRY_PATH_CHARS:
        raise ValueError(
            f"la ruta del snapshot tiene {len(declared)} caracteres y el máximo es "
            f"{_MAX_ENTRY_PATH_CHARS}: {path!r}"
        )
    return declared


def _workspace_label(workspace_path: str, root: Path) -> str:
    """Etiqueta del workspace que viaja en el snapshot, acotada al contrato.

    ``RepairSnapshot.workspace_path`` es obligatorio y admite de 1 a 400 caracteres: si el llamante
    no da ninguno se usa la raíz en forma posix, que es lo que la captura describe. Se acota aquí
    porque un valor fuera de la cota haría imposible volver a validar el checkpoint que lo
    contiene.
    """
    text = workspace_path.strip() or root.as_posix() or "."
    return text[:_MAX_WORKSPACE_PATH_CHARS]


def _reject_secret_path(path: str) -> None:
    """Rechaza capturar un almacén de secretos: la copia es contenido en claro.

    El registro que viaja en el checkpoint solo lleva hashes y tamaños, pero la copia que hace
    posible el rollback lleva los bytes. Copiar un ``.env`` o una clave privada a un directorio
    del workspace sería crear un segundo sitio donde filtrar lo mismo, así que un archivo así no
    se captura: el motor no puede deshacerlo con este mecanismo y debe tratarlo como no reparable
    localmente.
    """
    name = basename_of(path)
    looks_secret = (
        name.startswith(_SECRET_PREFIXES)
        or name.endswith(_SECRET_SUFFIXES)
        or name in _SECRET_BASENAMES
    )
    if looks_secret:
        raise ValueError(
            f"{path!r} parece un almacén de secretos: no se copia a {SNAPSHOT_DIR_NAME}"
        )


def _ensure_ignored(directory: Path) -> None:
    """Deja un ``.gitignore`` que excluye todo el directorio de copias.

    Se escribe solo si falta: reescribirlo en cada snapshot cambiaría la fecha del archivo sin
    cambiar su contenido, y este módulo evita tocar el disco cuando no hay nada que cambiar.
    """
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        _atomic_write(gitignore, _SNAPSHOT_GITIGNORE.encode("utf-8"))


def _read_regular(target: Path, path: str) -> bytes:
    """Lee un archivo regular del árbol, o lanza ``ValueError`` explicando qué no sirve.

    Un directorio o un archivo ilegible no puede entrar en un snapshot: el registro declararía un
    hash que no se puede volver a comprobar y el rollback no tendría qué restaurar.
    """
    if not target.is_file():
        raise ValueError(f"{path!r} no es un archivo regular: no se puede capturar su contenido")
    try:
        return target.read_bytes()
    except OSError as exc:
        raise ValueError(f"no se pudo leer {path!r}: {exc}") from exc


def _atomic_write(path: Path, payload: bytes) -> None:
    """Publica ``payload`` en ``path`` de forma atómica.

    Temporal del **mismo** directorio (para que ``os.replace`` sea atómico), volcado a disco y
    renombrado. Si algo falla se borra el temporal: un rollback no puede dejar restos ni, mucho
    menos, un archivo restaurado a medias.
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
    "SNAPSHOT_DIR_NAME",
    "FileRepairSnapshots",
    "RollbackVerdict",
]
