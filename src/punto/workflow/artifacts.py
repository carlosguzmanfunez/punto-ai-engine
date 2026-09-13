"""Handoff estructurado y durable entre etapas del kernel (ENGINE-6.0, V60-04).

El problema que resuelve
------------------------
Hasta ahora la entrada de cada rol se construía con una *closure* del proceso
(``build_input``): el kernel tenía en memoria los objetos que la etapa anterior había producido y
los entregaba al rol siguiente. Eso funciona mientras el workflow vive entero en un proceso y se
rompe justo cuando más importa: **reanudar**. Un workflow reanudado en un proceso nuevo —tras una
caída, un reinicio o una reanudación explícita— no tiene esas variables, así que no puede
reconstruir lo que produjo la etapa anterior. El checkpoint guardaba el estado del workflow, pero
no el *handoff*.

La solución: referencias, no contenido
--------------------------------------
Este módulo persiste **referencias estructuradas**, no el contenido dentro del checkpoint:

- :class:`ArtifactStore` es el puerto del almacén estable (poner bytes, recuperar bytes). Es el
  único punto por el que entra o sale contenido, y por eso es inyectable.
- :class:`FileArtifactStore` es la implementación local y determinista: un fichero por artefacto
  bajo ``root/<workflow_id>/``, escrito de forma atómica y verificado por digest.
- :func:`record_stage` añade a ``run.stage_artifacts`` un :class:`~punto.schemas.workflow.
  StageArtifacts` con el resumen del rol, sus hallazgos reales y las referencias de lo que produjo.
- :class:`WorkflowContext` y :func:`build_role_context` reconstruyen, **solo** desde el run y el
  almacén, el texto acotado que recibe el rol siguiente.

Regla que no se rompe: **nada de secretos ni de contenido de los ficheros del proyecto**. Lo que
viaja son resúmenes acotados, identificadores, digests, tamaños y referencias a un almacén estable.
Ni credenciales, ni volcados de código, ni cadenas de razonamiento, ni rutas de workspace más allá
de las que el contrato del run ya declara. El contenido íntegro, si hace falta, se recupera del
almacén con su digest; nunca engorda el checkpoint.

Formato en disco
----------------
Para cada workflow, bajo ``root/<workflow_id>/``, un fichero por artefacto::

    <rol>-<paso>-<kind>-<n>.bin

- ``<rol>`` es el valor del rol (``DEVELOPER``), ``<paso>`` el índice del paso y ``<kind>`` el tipo
  declarado del artefacto. Los tres se validan contra ``[A-Za-z0-9_-]+`` antes de tocar el disco:
  un ``kind`` con ``/`` o con ``..`` no puede escapar del directorio del workflow.
- ``<n>`` es el ordinal del artefacto dentro de esa terna. Se calcula **leyendo el directorio**
  (mayor existente más uno), no desde memoria: dos procesos que suban artefactos del mismo paso no
  se pisan, y el resultado no depende del reloj ni de un contador de proceso.
- La referencia que se guarda en el run es ``<workflow_id>/<fichero>``, relativa a la raíz y con
  separador ``/``: el mismo checkpoint vale en cualquier sistema de ficheros.

Escritura atómica: se escribe en un temporal **del mismo directorio**, se vuelca a disco y se
publica con ``os.replace``. Un lector concurrente ve el fichero anterior o el nuevo, nunca uno a
medias.

Errores
-------
La distinción importa y es deliberada:

- **Entrada malformada** (``workflow_id`` no canónico, ``kind`` fuera del alfabeto permitido, paso
  negativo, ``payloads`` sin almacén): ``ValueError``. Es un defecto de quien llama, no un estado
  durable; se detecta antes de escribir nada.
- **Artefacto ausente o ilegible** (el run referencia algo que el almacén no tiene): subclase de
  :class:`~punto.workflow.errors.WorkflowError` con código ``WORKFLOW_RESUME_FAILED``. Es
  exactamente el fallo de reanudar en un proceso nuevo sin el contenido de la etapa anterior.
- **Digest o tamaño que no cuadran** (el contenido cambió bajo los pies): ``WorkflowError`` con
  código ``WORKFLOW_CHECKPOINT_INVALID``. Es corrupción o manipulación, y se detecta en vez de
  devolver bytes dudosos como si fueran los que el run declaró.

``WorkflowError`` es la API pública de errores del kernel: quien captura no compara cadenas.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol
from uuid import UUID

from punto.schemas.enums import TaskStatus
from punto.schemas.workflow import (
    MAX_CONTEXT_ENTRIES,
    MAX_WORKFLOW_ARTIFACTS,
    MAX_WORKFLOW_CONTEXT_CHARS,
    MAX_WORKFLOW_FINDINGS,
    MAX_WORKFLOW_SUMMARY_CHARS,
    ArtifactReference,
    RoleExecutionResult,
    RoleName,
    StageArtifacts,
    WorkflowRun,
)
from punto.workflow.errors import WorkflowCheckpointInvalidError, WorkflowResumeFailedError

#: Nombre del almacén local tal y como viaja en :class:`ArtifactReference`.
STORE_NAME: Final[str] = "file"
#: Tipo con el que se registran las referencias que el propio rol reporta como texto.
REPORTED_KIND: Final[str] = "reported"
#: Marcador del almacén para esas referencias reportadas: no son contenido del almacén local.
REPORTED_STORE: Final[str] = "role-report"
#: Sufijo (y extensión) de todo artefacto en disco.
_ARTIFACT_SUFFIX: Final[str] = ".bin"
#: Temporales de la escritura atómica: empiezan por punto y terminan en ``.tmp`` para que ningún
#: glob de artefactos los alcance.
_TEMP_PREFIX: Final[str] = ".artifact-"
_TEMP_SUFFIX: Final[str] = ".tmp"
#: Alfabeto permitido en los componentes del nombre de fichero. Sin separadores, sin ``.``.
_SAFE_COMPONENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")
#: Forma exacta de un nombre de artefacto: ``<rol>-<paso>-<kind>-<n>.bin``.
_ARTIFACT_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_-]+-\d+-[A-Za-z0-9_-]+-\d+\.bin$"
)
#: Cotas del contrato de :class:`ArtifactReference`, aplicadas aquí para no construirlo inválido.
_MAX_KIND_CHARS: Final[int] = 60
_MAX_LABEL_CHARS: Final[int] = 200
_MAX_REFERENCE_CHARS: Final[int] = 400
#: Marca de recorte. Es explícita: un texto recortado en silencio se leería como completo.
_TRUNCATION_MARKER: Final[str] = " …[recortado]"

#: Etapas cuyo resultado **necesita** cada rol. No es una preferencia estética: es la dependencia
#: real del trabajo (el Developer necesita el plan; el Reviewer necesita lo construido y lo
#: verificado). En el handoff, estas etapas van primero, de modo que si hay que recortar por
#: límite de caracteres lo que se pierde es lo menos relevante para el rol destino.
ROLE_HANDOFF_SOURCES: Final[Mapping[RoleName, tuple[RoleName, ...]]] = MappingProxyType(
    {
        RoleName.ARCHITECT: (),
        RoleName.PLANNER: (RoleName.ARCHITECT,),
        RoleName.DEVELOPER: (RoleName.ARCHITECT, RoleName.PLANNER),
        RoleName.QA: (RoleName.DEVELOPER, RoleName.PLANNER),
        RoleName.SECURITY: (RoleName.DEVELOPER, RoleName.ARCHITECT),
        RoleName.REVIEWER: (RoleName.DEVELOPER, RoleName.QA, RoleName.SECURITY),
        RoleName.CROSS_AUDIT: (RoleName.DEVELOPER, RoleName.QA, RoleName.SECURITY),
        RoleName.VISUAL_QA: (RoleName.DEVELOPER,),
    }
)


class ArtifactStore(Protocol):
    """Contrato del almacén estable de artefactos que consume el kernel.

    Se declara como ``Protocol`` y no como clase base por el mismo motivo que
    :class:`~punto.workflow.checkpoints.CheckpointStore`: el kernel depende de la **capacidad**
    (dejar bytes y recuperarlos) y no de esta implementación. Una prueba puede inyectar un doble en
    memoria y el kernel no cambia una línea.

    Aquí no se guardan secretos: lo que se sube es el contenido que una etapa produce y que la
    siguiente necesita, y lo que se persiste en el run es solo su referencia.
    """

    def put(
        self,
        *,
        workflow_id: UUID,
        role: RoleName,
        step_index: int,
        kind: str,
        label: str,
        data: bytes,
    ) -> ArtifactReference:
        """Guarda ``data`` y devuelve la referencia que lo localiza y lo verifica.

        Debe rechazar cualquier entrada que pudiera escapar del espacio del almacén y debe
        devolver una referencia con digest y tamaño reales: el digest es lo que después permite
        detectar manipulación.
        """
        ...

    def get(self, reference: ArtifactReference) -> bytes:
        """Devuelve el contenido de ``reference``, verificando su digest.

        Debe fallar —no devolver bytes dudosos— si el artefacto no está, si no se puede leer o si
        el contenido no coincide con el digest declarado.
        """
        ...


class FileArtifactStore:
    """Implementación en disco de :class:`ArtifactStore`, local y determinista.

    ``root/<workflow_id>/<rol>-<paso>-<kind>-<n>.bin``. Un workflow no ve los ficheros de otro y
    el ordinal se calcula leyendo el directorio, así que dos procesos que suban artefactos del
    mismo paso no se pisan.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """Directorio raíz donde se escriben los workflows."""
        return self._root

    def put(
        self,
        *,
        workflow_id: UUID,
        role: RoleName,
        step_index: int,
        kind: str,
        label: str,
        data: bytes,
    ) -> ArtifactReference:
        """Escribe ``data`` de forma atómica y devuelve su referencia verificable.

        Lanza ``ValueError`` —antes de tocar el disco— si el ``workflow_id`` no es un UUID
        canónico, si ``kind`` no pertenece a ``[A-Za-z0-9_-]+``, si excede la cota del contrato o
        si el paso es negativo: esos datos se convierten en nombres de ruta, y una ruta construida
        con basura es la forma clásica de salirse del directorio.
        """
        workflow_name = _safe_workflow_name(workflow_id)
        safe_role = _safe_component(role.value, "role")
        safe_kind = _safe_component(kind, "kind", max_chars=_MAX_KIND_CHARS)
        if step_index < 0:
            raise ValueError(f"step_index no puede ser negativo: {step_index}")
        directory = self._root / workflow_name
        directory.mkdir(parents=True, exist_ok=True)
        index = _next_artifact_index(directory, safe_role, step_index, safe_kind)
        filename = f"{safe_role}-{step_index}-{safe_kind}-{index}{_ARTIFACT_SUFFIX}"
        _atomic_write(directory / filename, data)
        return ArtifactReference(
            kind=kind,
            label=_bounded_text(label, _MAX_LABEL_CHARS),
            store=STORE_NAME,
            reference=f"{workflow_name}/{filename}",
            digest=hashlib.sha256(data).hexdigest(),
            bytes_written=len(data),
        )

    def get(self, reference: ArtifactReference) -> bytes:
        """Devuelve el contenido de ``reference``, comprobando digest y tamaño.

        Lanza :class:`~punto.workflow.errors.WorkflowError` si la referencia no es de este almacén,
        si el artefacto falta o no se puede leer (``WORKFLOW_RESUME_FAILED``: reanudar sin el
        contenido de la etapa anterior) o si el contenido no cuadra con lo declarado
        (``WORKFLOW_CHECKPOINT_INVALID``: corrupción o manipulación, nunca se devuelve).
        """
        path = self._resolve(reference)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise WorkflowResumeFailedError(
                f"el artefacto {reference.reference!r} no existe en {self._root}: "
                "la etapa anterior no dejó el contenido que el run referencia"
            ) from exc
        except OSError as exc:
            raise WorkflowResumeFailedError(
                f"no se pudo leer el artefacto {reference.reference!r} en {self._root}: {exc}"
            ) from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != reference.digest:
            raise WorkflowCheckpointInvalidError(
                f"el digest del artefacto {reference.reference!r} no coincide con el declarado "
                f"(esperado {reference.digest!r}, calculado {digest}): corrupción o manipulación"
            )
        if reference.bytes_written and len(data) != reference.bytes_written:
            raise WorkflowCheckpointInvalidError(
                f"el tamaño del artefacto {reference.reference!r} no coincide con el declarado "
                f"(esperado {reference.bytes_written}, leído {len(data)})"
            )
        return data

    def _resolve(self, reference: ArtifactReference) -> Path:
        """Traduce una referencia a una ruta **dentro** de la raíz del almacén.

        La referencia viene de un run que pudo persistirse en otra máquina o haberse manipulado,
        así que se reconstruye por partes y se valida cada una: almacén esperado, exactamente dos
        componentes, UUID canónico y nombre de artefacto con la forma exacta. Nada de ``..``, nada
        de rutas absolutas, nada de separadores del sistema.
        """
        if reference.store != STORE_NAME:
            raise WorkflowResumeFailedError(
                f"la referencia apunta al almacén {reference.store!r} y este almacén local es "
                f"{STORE_NAME!r}: no puede servir su contenido"
            )
        parts = reference.reference.split("/")
        if len(parts) != 2 or not all(parts):
            raise WorkflowCheckpointInvalidError(
                f"la referencia {reference.reference!r} no tiene la forma <workflow_id>/<artefacto>"
            )
        workflow_name, filename = parts
        try:
            workflow_name = _safe_workflow_name(workflow_name)
        except ValueError as exc:
            raise WorkflowCheckpointInvalidError(
                f"la referencia {reference.reference!r} no lleva un workflow_id utilizable: {exc}"
            ) from exc
        if not _ARTIFACT_NAME_RE.fullmatch(filename):
            raise WorkflowCheckpointInvalidError(
                f"la referencia {reference.reference!r} no lleva un nombre de artefacto válido"
            )
        return self._root / workflow_name / filename


class WorkflowContext:
    """Vista de solo lectura sobre ``run.stage_artifacts``.

    Es lo que una etapa deja a las siguientes, leído del propio run: nada de variables de proceso,
    nada de estado global. Se construye con el run entero —el que se acaba de cargar de un
    checkpoint o de reconstruir desde JSON— y por eso funciona igual en el proceso que ejecutó la
    etapa anterior y en uno que acaba de arrancar.
    """

    def __init__(self, run: WorkflowRun) -> None:
        self._entries = tuple(run.stage_artifacts)

    def entries(self) -> tuple[StageArtifacts, ...]:
        """Todas las etapas registradas, en el orden en que ocurrieron."""
        return self._entries

    def for_role(self, role: RoleName) -> tuple[StageArtifacts, ...]:
        """Etapas registradas por un rol concreto, en orden cronológico."""
        return tuple(entry for entry in self._entries if entry.role is role)

    def latest(self, role: RoleName) -> StageArtifacts | None:
        """Última etapa registrada por un rol, o ``None`` si ese rol no ha dejado nada."""
        for entry in reversed(self._entries):
            if entry.role is role:
                return entry
        return None

    def references(self, kind: str) -> tuple[ArtifactReference, ...]:
        """Referencias de un tipo, en orden cronológico y sin duplicar ninguna etapa."""
        return tuple(
            reference
            for entry in self._entries
            for reference in entry.references
            if reference.kind == kind
        )

    def handoff_text(self, *, for_role: RoleName, limit: int = MAX_WORKFLOW_CONTEXT_CHARS) -> str:
        """Texto acotado y determinista con lo que necesita el rol indicado.

        Contiene **resúmenes y referencias** de las etapas anteriores: ni el contenido íntegro de
        un artefacto, ni credenciales, ni rutas del proyecto que el run no declarara ya. Las etapas
        que ese rol necesita (``ROLE_HANDOFF_SOURCES``) van primero, en orden cronológico, y
        después el resto: así, si el límite obliga a recortar, se pierde lo menos relevante.

        Devuelve cadena vacía si el run no tiene ninguna etapa registrada: no hay handoff que dar.
        """
        if not self._entries:
            return ""
        needed_roles = ROLE_HANDOFF_SOURCES.get(for_role, ())
        needed = [entry for entry in self._entries if entry.role in needed_roles]
        remaining = [entry for entry in self._entries if entry.role not in needed_roles]
        lines = [
            f"HANDOFF DE ETAPAS ANTERIORES — destino {for_role.value}, "
            f"{len(self._entries)} etapa(s) registrada(s)"
        ]
        lines.extend(
            _entry_lines(entry, needed=entry.role in needed_roles)
            for entry in (*needed, *remaining)
        )
        lines.append("FIN DEL HANDOFF")
        return _truncate("\n".join(lines), limit)


def record_stage(
    run: WorkflowRun,
    *,
    role: RoleName,
    stage: TaskStatus,
    step_index: int,
    result: RoleExecutionResult,
    store: ArtifactStore | None = None,
    payloads: Mapping[str, bytes] | None = None,
) -> WorkflowRun:
    """Devuelve ``run`` con la etapa registrada como handoff durable.

    No muta el run que recibe (los contratos son ``frozen``): devuelve una copia con el nuevo
    :class:`~punto.schemas.workflow.StageArtifacts` añadido al final de ``stage_artifacts``.

    Qué se copia
        El **resumen** del rol, sus **hallazgos reales** y las **referencias**. Las referencias
        tipadas que el rol declara en ``result.artifact_references`` se copian tal cual (con su
        almacén, digest y tamaño): son el handoff que un proceso nuevo resuelve. Las cadenas que el
        rol reporta en ``result.artifacts`` se registran como referencias de tipo
        :data:`REPORTED_KIND` (con el texto acotado como etiqueta y referencia): son punteros que
        el rol declara, no contenido.

    Qué se sube
        Si se pasan ``store`` y ``payloads`` (mapa ``kind -> bytes``), cada payload se sube al
        almacén y se guarda su :class:`ArtifactReference` con ``store``, ``reference``, digest
        sha256 y ``bytes_written``. Los tipos se recorren **ordenados** para que el resultado sea
        determinista aunque el mapa llegue en otro orden. El contenido **no** entra en el run.

    Cotas y validación
        Las referencias se acotan a ``MAX_WORKFLOW_ARTIFACTS`` y la lista de etapas del run a
        ``MAX_CONTEXT_ENTRIES`` (conservando las más recientes). Lanza ``ValueError`` si ``result``
        pertenece a otro rol —registrar una etapa con el rol equivocado corrompería el handoff— o
        si llegan ``payloads`` sin ``store``: sin almacén no hay dónde dejar el contenido, y
        ignorarlo en silencio sería perder trabajo sin decirlo.
    """
    if result.role is not role:
        raise ValueError(
            f"el resultado es del rol {result.role.value} y la etapa se registra como "
            f"{role.value}: el handoff no puede atribuirse a otro rol"
        )
    references = _payload_references(
        run, role=role, step_index=step_index, store=store, payloads=payloads
    )
    # Referencias **tipadas** primero: traen almacén, digest y tamaño, así que son las que un
    # proceso nuevo puede resolver sin ambigüedad. Las de texto quedan detrás.
    references.extend(result.artifact_references)
    references.extend(_reported_references(result))
    entry = StageArtifacts(
        role=role,
        stage=stage,
        step_index=step_index,
        summary=result.summary,
        references=tuple(references[:MAX_WORKFLOW_ARTIFACTS]),
        findings=result.findings[:MAX_WORKFLOW_FINDINGS],
    )
    kept = (*run.stage_artifacts, entry)[-MAX_CONTEXT_ENTRIES:]
    return run.model_copy(update={"stage_artifacts": kept})


def build_role_context(
    run: WorkflowRun, *, role: RoleName, limit: int = MAX_WORKFLOW_CONTEXT_CHARS
) -> str:
    """Texto de handoff para ``role``, reconstruido **solo** desde el run.

    Es la entrada que un proceso nuevo puede construir sin ninguna variable del proceso que
    ejecutó las etapas anteriores: se apoya en ``run.stage_artifacts`` y, cuando hace falta el
    contenido, en las referencias al almacén estable. No consulta ni el reloj, ni el directorio de
    trabajo, ni el estado global del kernel.
    """
    return WorkflowContext(run).handoff_text(for_role=role, limit=limit)


def summarize_result(
    result: RoleExecutionResult, *, limit: int = MAX_WORKFLOW_SUMMARY_CHARS
) -> str:
    """Resumen acotado y determinista de un resultado de rol.

    Lleva lo que otra etapa necesita saber sin abrir nada: rol, estado, resumen, número y gravedad
    de los hallazgos, recomendación, código de error y cuántos artefactos reportó. **Nunca** el
    contenido de esos artefactos: para eso está la referencia y el digest.
    """
    parts = [f"{result.role.value}: {result.status.value}"]
    if result.summary:
        parts.append(result.summary)
    blocking = len(result.blocking_findings)
    parts.append(f"hallazgos: {len(result.findings)} (bloqueantes: {blocking})")
    if result.recommendation:
        parts.append(f"recomendación: {result.recommendation}")
    if result.error_code is not None:
        detail = f" {result.error_detail}" if result.error_detail else ""
        parts.append(f"error: {result.error_code.value}{detail}")
    if result.artifacts:
        parts.append(f"artefactos reportados: {len(result.artifacts)}")
    return _truncate(" — ".join(parts), limit)


# ---------------------------------------------------------------------------
# Interno
# ---------------------------------------------------------------------------
def _payload_references(
    run: WorkflowRun,
    *,
    role: RoleName,
    step_index: int,
    store: ArtifactStore | None,
    payloads: Mapping[str, bytes] | None,
) -> list[ArtifactReference]:
    """Sube los payloads al almacén y devuelve sus referencias, en orden determinista.

    La etiqueta de cada referencia es el propio ``kind``: el mapa solo declara contenido por tipo,
    y inventar una etiqueta a partir de los bytes sería meter contenido en el checkpoint.
    """
    if not payloads:
        return []
    if store is None:
        raise ValueError(
            "se pasaron payloads sin store: sin almacén estable no hay dónde dejar el contenido "
            "ni referencia que guardar"
        )
    return [
        store.put(
            workflow_id=run.workflow_id,
            role=role,
            step_index=step_index,
            kind=kind,
            label=kind,
            data=payloads[kind],
        )
        for kind in sorted(payloads)
    ]


def _reported_references(result: RoleExecutionResult) -> list[ArtifactReference]:
    """Convierte las cadenas que el rol reporta en referencias acotadas.

    Son punteros declarados por el rol (un nombre de informe, una ruta relativa ya declarada en el
    contrato), nunca contenido leído del proyecto. Se acotan a la longitud del contrato para no
    construir un :class:`ArtifactReference` inválido.
    """
    references: list[ArtifactReference] = []
    for reported in result.artifacts:
        text = reported.strip()
        if not text:
            continue
        references.append(
            ArtifactReference(
                kind=REPORTED_KIND,
                label=_bounded_text(text, _MAX_LABEL_CHARS),
                store=REPORTED_STORE,
                reference=_bounded_text(text, _MAX_REFERENCE_CHARS),
            )
        )
    return references


def _entry_lines(entry: StageArtifacts, *, needed: bool) -> str:
    """Bloque de texto de una etapa: cabecera, resumen, hallazgos y referencias."""
    header = f"- paso {entry.step_index} | {entry.role.value} | {entry.stage.value}"
    if needed:
        header = f"{header} [contexto requerido por el rol destino]"
    lines = [header, f"  resumen: {entry.summary or '(sin resumen)'}"]
    if entry.findings:
        blocking = sum(1 for finding in entry.findings if finding.blocks_approval)
        lines.append(f"  hallazgos: {len(entry.findings)} (bloqueantes: {blocking})")
    if not entry.references:
        lines.append("  referencias: (ninguna)")
    for reference in entry.references:
        lines.append(
            f"  referencia: kind={reference.kind} label={reference.label!r} "
            f"store={reference.store!r} digest={reference.digest or '(sin digest)'} "
            f"bytes={reference.bytes_written} ref={reference.reference!r}"
        )
    return "\n".join(lines)


def _safe_workflow_name(workflow_id: UUID | str) -> str:
    """Nombre de carpeta derivado del ``workflow_id``, o ``ValueError`` si no sirve.

    Se reconstruye el UUID a partir de su forma textual en vez de confiar en el valor recibido:
    así ni ``..`` ni un separador de ruta pueden llegar al sistema de ficheros, aunque el llamante
    pase algo que solo se parezca a un UUID.
    """
    raw = str(workflow_id)
    try:
        canonical = str(UUID(raw))
    except ValueError as exc:
        raise ValueError(f"workflow_id no utilizable como nombre de carpeta: {raw!r}") from exc
    if canonical != raw:
        raise ValueError(f"workflow_id no canónico: {raw!r} debería ser {canonical!r}")
    return canonical


def _safe_component(value: str, field_name: str, *, max_chars: int | None = None) -> str:
    """Componente de nombre de fichero saneado, o ``ValueError`` si no lo es."""
    too_long = max_chars is not None and len(value) > max_chars
    if not value or too_long or not _SAFE_COMPONENT_RE.fullmatch(value):
        raise ValueError(
            f"{field_name} no utilizable como componente de un nombre de artefacto: {value!r}"
        )
    return value


def _next_artifact_index(directory: Path, role: str, step_index: int, kind: str) -> int:
    """Ordinal libre para la terna ``rol-paso-kind``, leído del propio directorio.

    No hay contador en memoria a propósito: el ordinal debe ser el mismo lo calcule quien lo
    calcule, y dos procesos que suban artefactos del mismo paso no pueden pisarse el uno al otro.
    """
    prefix = f"{role}-{step_index}-{kind}-"
    used: list[int] = []
    for entry in directory.iterdir():
        if not entry.is_file() or entry.suffix != _ARTIFACT_SUFFIX:
            continue
        if not entry.name.startswith(prefix):
            continue
        ordinal = entry.stem[len(prefix) :]
        if ordinal.isdigit():
            used.append(int(ordinal))
    return max(used, default=-1) + 1


def _bounded_text(text: str, max_chars: int) -> str:
    """Recorta ``text`` a ``max_chars`` sin adornos: para campos que el contrato ya acota."""
    return text if len(text) <= max_chars else text[:max_chars]


def _truncate(text: str, limit: int) -> str:
    """Recorta ``text`` a ``limit`` caracteres dejando una marca explícita de recorte.

    La marca importa: un texto recortado en silencio se leería como el texto completo. Si el
    límite es tan pequeño que no cabe ni la marca, se devuelve el prefijo sin ella y nunca más
    caracteres de los pedidos.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_MARKER):
        return text[:limit]
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _atomic_write(path: Path, payload: bytes) -> None:
    """Publica ``payload`` en ``path`` de forma atómica.

    Temporal del **mismo** directorio (para que ``os.replace`` sea atómico), volcado a disco y
    renombrado. Si algo falla, el temporal se borra: el almacén no acumula restos y nadie puede
    leer un artefacto a medias.
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
    "REPORTED_KIND",
    "REPORTED_STORE",
    "ROLE_HANDOFF_SOURCES",
    "STORE_NAME",
    "ArtifactStore",
    "FileArtifactStore",
    "WorkflowContext",
    "build_role_context",
    "record_stage",
    "summarize_result",
]
