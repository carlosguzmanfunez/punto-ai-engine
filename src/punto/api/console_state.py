"""Estado durable de la consola humana: las tareas y los Human Gates sobreviven al proceso.

AP000-OBS-01. La consola guardaba sus tareas en un diccionario del proceso y el ``HumanGate`` del
motor sus solicitudes en memoria: un ``--reload`` de ``uvicorn``, un reinicio de PUNTO o un refresco
del navegador borraban el estado gobernado. Aquí vive el **mínimo** que evita eso, reutilizando el
mecanismo durable que PUNTO ya tiene —el mismo directorio local que la memoria PELL
(``.punto-memory/``, ignorado por Git) con escritura atómica por fichero temporal más
``os.replace``— sin infraestructura nueva: ni base de datos, ni Redis, ni servicio externo.

Tres reglas gobiernan este módulo:

- **Se persiste solo lo necesario para continuar**: identidad y solicitud de la tarea, su etapa, sus
  marcas de tiempo, sus referencias de evidencia (resultado del ciclo y expediente de publicación),
  los gates que le pertenecen y la decisión humana ya tomada. Nunca prompts, contenido de ficheros,
  credenciales ni tokens.
- **Se falla cerrado**: si el fichero no se puede leer, no valida contra el esquema, no cumple las
  reglas de coherencia de la consola o referencia algo que no existe, **no se recupera nada** —ni
  siquiera en parte— y la consola arranca vacía con el hecho auditado. Un estado a medias jamás se
  convierte por inferencia en una tarea ``DEVELOPMENT_COMPLETED`` ni en un gate ``APPROVED``.
- **La autoridad no se restaura de un fichero**: lo que se recupera es el estado gobernado y la
  decisión humana ya registrada. La autoridad de release se vuelve a evaluar en cada arranque contra
  la configuración del destino y el repositorio real, que es la única fuente de esa autoridad.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from punto.common import utc_now
from punto.providers.secrets import redact_secret_text
from punto.publish.production import PublicationRecord, PublicationRefused
from punto.schemas.dev import DevelopmentResult
from punto.schemas.enums import ApprovalStatus, RiskLevel
from punto.schemas.scheduling import TaskSchedulingRecord

__all__ = [
    "CONSOLE_STATE_ENV",
    "CONSOLE_STATE_SCHEMA_VERSION",
    "TASK_RELATION_KINDS",
    "ConsoleStateDocument",
    "ConsoleStateError",
    "ConsoleStateSnapshot",
    "ConsoleStateStatus",
    "ConsoleStateStore",
    "GateRecord",
    "StageRules",
    "TaskAttempt",
    "TaskRecord",
    "TaskRelation",
    "default_console_state_path",
    "migrate_console_state_v1",
    "publication_of",
]

#: Versión actual del documento persistido. Solo v1 tiene una migración explícita a este contrato;
#: cualquier otra versión se rechaza entera en vez de adivinar cómo leerla.
CONSOLE_STATE_SCHEMA_VERSION: Final[Literal[2]] = 2

#: Variable de entorno con la que se fija la ruta del estado (pruebas y despliegues locales).
CONSOLE_STATE_ENV: Final[str] = "PUNTO_CONSOLE_STATE_PATH"

#: Directorio local del motor (el mismo de la memoria PELL), ignorado por Git.
CONSOLE_STATE_DIR_NAME: Final[str] = ".punto-memory"

#: Nombre del documento de estado de la consola.
CONSOLE_STATE_FILE_NAME: Final[str] = "console-state.json"

#: Sufijo del expediente que conserva un estado que no se pudo interpretar (evidencia del rechazo).
CONSOLE_STATE_REJECTED_SUFFIX: Final[str] = ".rejected.json"

#: Tope de bytes que se aceptan al leer: un documento mayor no se interpreta, se rechaza.
MAX_CONSOLE_STATE_BYTES: Final[int] = 8_000_000

#: Topes de elementos persistidos: acotan el documento y delatan un fichero fabricado.
MAX_CONSOLE_TASKS: Final[int] = 200
MAX_CONSOLE_GATES: Final[int] = 500

#: Topes por campo: lo que la consola admite como máximo y un poco de holgura.
MAX_ITEMS: Final[int] = 200
MAX_NOTES: Final[int] = 20
MAX_ATTEMPTS: Final[int] = 20
MAX_TEXT_CHARS: Final[int] = 2_000
MAX_REASON_CHARS: Final[int] = 400


def default_console_state_path() -> Path:
    """Ruta del estado durable de la consola.

    Prioridad: ``PUNTO_CONSOLE_STATE_PATH`` y, si no, ``<cwd>/.punto-memory/console-state.json``:
    la misma convención local y no versionada que la memoria de PELL.
    """
    override = os.environ.get(CONSOLE_STATE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if os.environ.get("PYTEST_CURRENT_TEST"):
        # Aislamiento fixture ↔ estado operativo: una prueba jamás lee ni escribe el estado
        # durable real. Sin ruta explícita, falla en voz alta en vez de contaminarlo.
        raise ConsoleStateError(
            "STATE_ISOLATION",
            f"una prueba intentó usar el estado operativo por defecto: fija {CONSOLE_STATE_ENV}",
        )
    return Path.cwd() / CONSOLE_STATE_DIR_NAME / CONSOLE_STATE_FILE_NAME


class ConsoleStateError(RuntimeError):
    """El estado de la consola no se puede usar (corrupto, incompatible o con secretos).

    ``kind`` es un código estable para la auditoría (``STATE_CORRUPT``, ``STATE_VERSION``,
    ``STATE_SECRETS``, ``STATE_IO``) y ``detail`` el hecho concreto, ya redactado.
    """

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


class ConsoleStateStatus(StrEnum):
    """Desenlace de la carga del estado persistido."""

    #: No había nada persistido (primer arranque): no es un fallo, es un estado vacío legítimo.
    EMPTY = "EMPTY"
    #: El estado persistido se validó entero y se recupera.
    RECOVERED = "RECOVERED"
    #: Había estado y **no** es utilizable: se falla cerrado y la consola arranca vacía.
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class StageRules:
    """Coherencia que debe cumplir una etapa persistida para poder recuperarse.

    Las define la consola, que es quien conoce su vocabulario de etapas y sus transiciones reales;
    el almacén solo las aplica. Sin reglas, el almacén valida forma y referencias, nunca semántica
    de consola.
    """

    #: Etapas conocidas (consola y publicación). Una etapa fuera de aquí es un documento fabricado.
    known: frozenset[str] = frozenset()
    #: Etapas terminales: solo ellas pueden llevar marca de finalización.
    terminal: frozenset[str] = frozenset()
    #: Etapas que solo se alcanzan con un resultado real del ciclo detrás.
    requires_result: frozenset[str] = frozenset()
    #: Etapas que solo se alcanzan con un desarrollo **completado y verificado**.
    requires_completed_result: frozenset[str] = frozenset()
    #: Etapas que solo se alcanzan con un expediente de publicación abierto.
    requires_publication: frozenset[str] = frozenset()
    #: Etapas que solo se alcanzan con producción comprobada contra su marcador.
    requires_validated_production: frozenset[str] = frozenset()


class GateRecord(BaseModel):
    """Human Gate gobernado tal como se persiste (solicitud y decisión humana, si la hubo)."""

    model_config = ConfigDict(extra="forbid")

    approval_id: UUID
    task_id: UUID
    action: str = Field(min_length=1, max_length=120)
    risk: RiskLevel
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)
    status: ApprovalStatus
    requested_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = Field(default=None, max_length=80)
    resolution_note: str | None = Field(default=None, max_length=500)
    resume_status: str | None = Field(default=None, max_length=40)
    policy_outcome: str | None = Field(default=None, max_length=40)
    #: Decisión de política que originó la solicitud: el vínculo que hace la aprobación trazable.
    policy_decision_id: UUID | None = None
    #: Intento/evento posterior que volvió obsoleto el gate y razón causal (``SUPERSEDED``).
    superseded_by: str | None = Field(default=None, max_length=120)
    supersession_cause: str | None = Field(default=None, max_length=500)


class TaskAttempt(BaseModel):
    """Intento real de ejecución del ciclo sobre una tarea (uno por llamada al ciclo).

    Lo escribe la consola al terminar cada intento, con el desenlace **real** del ciclo. Existe para
    que un reintento sea visible: sin él, dos intentos con el mismo desenlace son indistinguibles y
    una persona no puede saber si la tarea se volvió a ejecutar o no.
    """

    model_config = ConfigDict(extra="forbid")

    run: int = Field(ge=1, le=1_000)
    started_at: datetime
    status: str = Field(min_length=1, max_length=40)
    error_kind: str = Field(default="", max_length=40)
    commit_sha: str = Field(default="", max_length=64)
    duration_ms: int | None = Field(default=None, ge=0)
    #: Proveedor que produjo la última respuesta del ciclo en este intento.
    provider: str = Field(default="", max_length=40)
    #: PROVIDER FAILOVER: resumen de las sustituciones del intento (``primario->sustituto:causa``).
    #: Vacío si el primario respondió. La identidad de la Task no cambia por un failover.
    failover: str = Field(default="", max_length=200)
    #: VISUAL_QA EFECTIVO: resumen de la evidencia visual del intento (proveedor/transporte).
    visual: str = Field(default="", max_length=120)
    #: ``ALREADY_SATISFIED`` si el intento se completó **sin cambios** porque el estado actual ya
    #: satisfacía la Task (verificado con la cadena completa); vacío si aplicó cambios.
    resolution: str = Field(default="", max_length=40)
    #: Cómo nació el intento: ``initial`` (primer intento), ``retry`` (reintento explícito) o
    #: ``continuation`` (una solicitud equivalente se absorbió en esta Task y la continuó).
    origin: str = Field(default="initial", max_length=20)


#: Relaciones estructurales entre Tasks. Son **hechos** que no se pueden derivar de otra cosa, así
#: que se persisten con la Task; el resto de relaciones del grafo son proyección del estado durable.
TASK_RELATION_KINDS: Final[frozenset[str]] = frozenset(
    {"supersedes", "superseded_by", "duplicate_of", "continuation_of", "retries"}
)


class TaskRelation(BaseModel):
    """Relación explícita de una Task con otra: quién la sustituye, de quién es duplicada, etc."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=20)
    task_id: UUID
    cause: str = Field(default="", max_length=120)
    at: datetime


class TaskRecord(BaseModel):
    """Tarea gobernada tal como se persiste: lo necesario para continuar y rendir cuentas."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    objective: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    target_id: str = Field(min_length=1, max_length=80)
    acceptance_criteria: tuple[str, ...] = Field(default=(), max_length=MAX_ITEMS)
    scope_paths: tuple[str, ...] = Field(default=(), max_length=MAX_ITEMS)
    context: str = Field(default="", max_length=MAX_TEXT_CHARS)
    stage: str = Field(min_length=1, max_length=40)
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    runs: int = Field(default=0, ge=0, le=1_000)
    notes: tuple[str, ...] = Field(default=(), max_length=MAX_NOTES)
    #: Historial de intentos reales del ciclo, acotado: es la auditoría de lo que ya se intentó.
    attempts: tuple[TaskAttempt, ...] = Field(default=(), max_length=MAX_ATTEMPTS)
    gate_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_ITEMS)
    #: Resultado real del ciclo. Es la evidencia que permite continuar sin repetir etapas; se
    #: valida con el propio contrato del motor al leerlo, así que un documento manipulado no pasa.
    result: DevelopmentResult | None = None
    #: Expediente de publicación (``PublicationRecord.as_dict``) tal cual se emitió.
    publication: dict[str, Any] | None = None
    #: Identidad canónica del destino con la que nació la Task (huella + ramas, sin rutas ni
    #: credenciales). Vacía en Tasks anteriores a la huella.
    target_identity: str = Field(default="", max_length=64)
    target_work_branch: str = Field(default="", max_length=120)
    target_production_branch: str = Field(default="", max_length=120)
    #: ``ACTIVE`` o ``SUPERSEDED``: separa el flujo operativo del historial. Una Task superada
    #: conserva íntegros su historial, sus intentos y sus gates; solo deja de ser operativa.
    lineage_status: str = Field(default="ACTIVE", max_length=20)
    superseded_by: UUID | None = None
    supersession_cause: str = Field(default="", max_length=120)
    superseded_at: datetime | None = None
    relations: tuple[TaskRelation, ...] = Field(default=(), max_length=MAX_ITEMS)
    #: Overlay durable de scheduling. En Fase 1 queda ``managed=False``: persistir el contrato no
    #: afirma que ya exista un scheduler, un lease o un executor vivo.
    scheduling: TaskSchedulingRecord


class ConsoleStateDocument(BaseModel):
    """Documento persistido: versión de esquema, tareas y gates, con su momento de escritura."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    written_at: datetime
    source: str = Field(default="punto-console", max_length=80)
    tasks: tuple[TaskRecord, ...] = ()
    gates: tuple[GateRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ConsoleStateSnapshot:
    """Lo que la consola puede recuperar de un documento persistido (o nada, si se rechaza)."""

    status: ConsoleStateStatus
    detail: str
    tasks: tuple[TaskRecord, ...] = ()
    gates: tuple[GateRecord, ...] = ()
    migrated_from: int | None = None

    @property
    def recovered(self) -> bool:
        """True solo si el documento se validó entero y hay estado que recuperar."""
        return self.status is ConsoleStateStatus.RECOVERED

    def as_metadata(self) -> dict[str, Any]:
        """Metadatos de auditoría: qué se recuperó, sin contenido de la solicitud."""
        return {
            "status": self.status.value,
            "tasks": len(self.tasks),
            "gates": len(self.gates),
            "pending_gates": sum(
                1 for gate in self.gates if gate.status is ApprovalStatus.PENDING
            ),
            "migrated_from": self.migrated_from,
            "detail": self.detail[:300],
        }


def migrate_console_state_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Migra exactamente v1 → v2 sin inferir ejecución, ownership ni leases.

    Cada Task anterior recibe el overlay neutral ``managed=False``. Una sección ``scheduling`` en
    un documento que todavía se declara v1 es ambigua/fabricada y se rechaza: no se mezclan
    contratos de versiones distintas.
    """
    migrated = dict(payload)
    tasks = migrated.get("tasks")
    if isinstance(tasks, list):
        migrated_tasks: list[Any] = []
        neutral = TaskSchedulingRecord().model_dump(mode="json")
        for item in tasks:
            if not isinstance(item, Mapping):
                migrated_tasks.append(item)
                continue
            if "scheduling" in item:
                raise ConsoleStateError(
                    "STATE_MIGRATION",
                    "un documento v1 no puede declarar la sección scheduling de v2",
                )
            migrated_task = dict(item)
            migrated_task["scheduling"] = neutral.copy()
            migrated_tasks.append(migrated_task)
        migrated["tasks"] = migrated_tasks
    migrated["schema_version"] = CONSOLE_STATE_SCHEMA_VERSION
    return migrated


def publication_of(task: TaskRecord) -> PublicationRecord | None:
    """Expediente de publicación de una tarea persistida, reconstruido con el contrato del motor.

    Raises:
        ConsoleStateError: si el expediente persistido no cumple el contrato (``STATE_CORRUPT``).
    """
    if task.publication is None:
        return None
    try:
        return PublicationRecord.from_dict(task.publication)
    except PublicationRefused as exc:
        raise ConsoleStateError(
            "STATE_CORRUPT",
            f"el expediente de publicación de {task.task_id} no cumple el contrato: {exc}",
        ) from exc


def integrity_problems(
    document: ConsoleStateDocument, rules: StageRules, *, source: str
) -> tuple[str, ...]:
    """Problemas de coherencia del documento: si hay alguno, no se recupera **nada**.

    Comprueba referencias cruzadas (cada gate pertenece a una tarea persistida, cada gate citado
    existe) y las reglas de etapa que declara la consola. Es el filtro que impide que un fichero
    manipulado o a medias se convierta en una tarea verificada o en un gate aprobado.
    """
    problems: list[str] = []
    if len(document.tasks) > MAX_CONSOLE_TASKS:
        problems.append(f"hay más de {MAX_CONSOLE_TASKS} tareas persistidas")
    if len(document.gates) > MAX_CONSOLE_GATES:
        problems.append(f"hay más de {MAX_CONSOLE_GATES} gates persistidos")

    tasks = {task.task_id: task for task in document.tasks}
    if len(tasks) != len(document.tasks):
        problems.append("hay identificadores de tarea repetidos")
    gates = {gate.approval_id: gate for gate in document.gates}
    if len(gates) != len(document.gates):
        problems.append("hay identificadores de gate repetidos")

    for gate in document.gates:
        if gate.task_id not in tasks:
            problems.append(
                f"el gate {gate.approval_id} apunta a una tarea que no está: {gate.task_id}"
            )
        problems.extend(_gate_problems(gate))

    for task in document.tasks:
        problems.extend(_task_problems(task, gates, rules))
        for reference in (task.superseded_by, *(item.task_id for item in task.relations)):
            if reference is not None and reference not in tasks:
                problems.append(
                    f"la tarea {task.task_id} referencia una tarea que no está: {reference}"
                )

    problems.extend(_source_problems(document, source))
    return tuple(problems)


def _source_problems(document: ConsoleStateDocument, source: str) -> list[str]:
    """El documento tiene que venir de donde dice venir: no se acepta estado de otro origen."""
    if document.source != source:
        return [f"el documento declara un origen inesperado: {document.source!r}"]
    return []


def _gate_problems(gate: GateRecord) -> list[str]:
    """Coherencia de un gate: una decisión existe con su actor y su momento, o no existe."""
    problems: list[str] = []
    if gate.resolved_at is not None and gate.resolved_at < gate.requested_at:
        problems.append(f"el gate {gate.approval_id} se resolvió antes de pedirse")
    if gate.status is ApprovalStatus.PENDING:
        if gate.resolved_at is not None or gate.resolved_by is not None:
            problems.append(f"el gate {gate.approval_id} está pendiente y ya lleva resolución")
    elif gate.resolved_at is None:
        problems.append(f"el gate {gate.approval_id} está resuelto y no tiene momento de decisión")
    return problems


def _task_problems(
    task: TaskRecord, gates: Mapping[UUID, GateRecord], rules: StageRules
) -> list[str]:
    """Coherencia de una tarea: etapa conocida, marcas de tiempo ordenadas y evidencia presente."""
    problems: list[str] = []
    for gate_id in task.gate_ids:
        if gate_id not in gates:
            problems.append(f"la tarea {task.task_id} cita un gate que no está: {gate_id}")
    if task.lineage_status not in {"ACTIVE", "SUPERSEDED"}:
        problems.append(f"la tarea {task.task_id} tiene un estado de linaje desconocido")
    if task.lineage_status == "SUPERSEDED" and task.superseded_at is None:
        problems.append(f"la tarea {task.task_id} está superada y no tiene momento de sustitución")
    if task.superseded_by is not None and task.superseded_by == task.task_id:
        problems.append(f"la tarea {task.task_id} se declara superada por sí misma")
    for relation in task.relations:
        if relation.kind not in TASK_RELATION_KINDS:
            problems.append(f"la tarea {task.task_id} declara una relación desconocida")
    if task.updated_at < task.created_at:
        problems.append(f"la tarea {task.task_id} se actualizó antes de crearse")
    if task.finished_at is not None and task.finished_at < task.created_at:
        problems.append(f"la tarea {task.task_id} terminó antes de crearse")
    if rules.known and task.stage not in rules.known:
        problems.append(f"la tarea {task.task_id} tiene una etapa desconocida: {task.stage!r}")
    if task.finished_at is not None and rules.terminal and task.stage not in rules.terminal:
        problems.append(f"la tarea {task.task_id} está finalizada en una etapa no terminal")
    if task.stage in rules.requires_result and task.result is None:
        problems.append(f"la tarea {task.task_id} está en {task.stage} sin resultado del ciclo")
    if task.stage in rules.requires_completed_result and (
        task.result is None or not task.result.completed
    ):
        problems.append(
            f"la tarea {task.task_id} está en {task.stage} sin un desarrollo completado"
        )
    problems.extend(_publication_problems(task, rules))
    return problems


def _publication_problems(task: TaskRecord, rules: StageRules) -> list[str]:
    """Coherencia entre la etapa y el expediente de publicación persistido."""
    problems: list[str] = []
    if task.stage in rules.requires_publication and task.publication is None:
        problems.append(
            f"la tarea {task.task_id} está en {task.stage} sin expediente de publicación"
        )
        return problems
    if task.publication is None:
        return problems
    try:
        record = publication_of(task)
    except ConsoleStateError as exc:
        problems.append(exc.detail)
        return problems
    if record is None:
        return problems
    if record.task_id != str(task.task_id):
        problems.append(f"el expediente de publicación de {task.task_id} es de otra tarea")
    if task.stage in rules.requires_validated_production and (
        record.production is None or not record.production.validated
    ):
        problems.append(
            f"la tarea {task.task_id} está en {task.stage} sin producción comprobada"
        )
    return problems


class ConsoleStateStore:
    """Almacén local y durable del estado gobernado de la consola (tareas y Human Gates)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else default_console_state_path()

    @property
    def path(self) -> Path:
        """Fichero donde vive el estado de la consola."""
        return self._path

    # ------------------------------------------------------------------ lectura
    def load(
        self, *, rules: StageRules | None = None, source: str = "punto-console"
    ) -> ConsoleStateSnapshot:
        """Lee y valida el estado persistido.

        Returns:
            La instantánea: vacía si no había nada, recuperable si el documento es coherente y
            **rechazada sin recuperar nada** si el documento no se puede interpretar.

        Un rechazo nunca deja el estado a medias: la consola arranca con el registro vacío y el
        hecho queda auditado. La evidencia del rechazo se conserva en un expediente aparte.
        """
        effective = rules if rules is not None else StageRules()
        if not self._path.is_file():
            return ConsoleStateSnapshot(ConsoleStateStatus.EMPTY, "no había estado persistido")
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return self._rejected(f"no se pudo leer el estado persistido: {type(exc).__name__}")
        if len(raw.encode("utf-8")) > MAX_CONSOLE_STATE_BYTES:
            return self._rejected("el estado persistido supera el tope de lectura")
        try:
            decoded: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._rejected(f"el estado persistido no valida como JSON: {exc.msg}")
        if not isinstance(decoded, Mapping):
            return self._rejected("el estado persistido no es un documento JSON")
        version = decoded.get("schema_version")
        if type(version) is not int:
            return self._rejected("la versión del estado persistido no es un entero")
        migrated_from: int | None = None
        if version == 1:
            try:
                decoded = migrate_console_state_v1(decoded)
            except ConsoleStateError as exc:
                return self._rejected(f"la migración del estado falló: {exc.detail}")
            migrated_from = 1
        elif version != CONSOLE_STATE_SCHEMA_VERSION:
            return self._rejected(
                "la versión del estado persistido no es compatible: "
                f"{version} != {CONSOLE_STATE_SCHEMA_VERSION}"
            )
        try:
            document = ConsoleStateDocument.model_validate(decoded)
        except ValidationError as exc:
            return self._rejected(f"el estado persistido no valida: {_first_error(exc)}")
        problems = integrity_problems(document, effective, source=source)
        if problems:
            return self._rejected("el estado persistido es incoherente: " + "; ".join(problems[:5]))
        return ConsoleStateSnapshot(
            ConsoleStateStatus.RECOVERED,
            f"estado recuperado de {self._path.name}",
            tasks=document.tasks,
            gates=document.gates,
            migrated_from=migrated_from,
        )

    def _rejected(self, detail: str) -> ConsoleStateSnapshot:
        """Falla cerrado: conserva la evidencia y devuelve una instantánea sin estado."""
        self._quarantine()
        return ConsoleStateSnapshot(ConsoleStateStatus.REJECTED, detail[:500])

    def _quarantine(self) -> None:
        """Copia el documento ilegible a un expediente aparte, sin romper nada si no se puede."""
        target = self._path.with_name(self._path.name + CONSOLE_STATE_REJECTED_SUFFIX)
        try:
            target.write_text(self._path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            return

    # ----------------------------------------------------------------- escritura
    def save(self, *, tasks: Iterable[TaskRecord], gates: Iterable[GateRecord]) -> Path:
        """Escribe el estado de forma atómica, tras comprobar que no lleva credenciales.

        Returns:
            La ruta escrita.

        Raises:
            ConsoleStateError: ``STATE_SECRETS`` si el documento contiene algo con forma de
                credencial (entonces **no** se escribe nada) o ``STATE_IO`` si la escritura falla.
        """
        document = ConsoleStateDocument(
            schema_version=CONSOLE_STATE_SCHEMA_VERSION,
            written_at=utc_now(),
            tasks=tuple(tasks),
            gates=tuple(gates),
        )
        payload = document.model_dump_json()
        if redact_secret_text(payload) != payload:
            raise ConsoleStateError(
                "STATE_SECRETS",
                "el estado contiene algo con forma de credencial: no se persiste",
            )
        self._write_atomic(payload)
        return self._path

    def _write_atomic(self, payload: str) -> None:
        """Escribe el documento con temporal + ``os.replace`` (el patrón durable del motor)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self._path)
            except OSError:
                Path(temporary).unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise ConsoleStateError(
                "STATE_IO", f"no se pudo escribir el estado de la consola: {type(exc).__name__}"
            ) from exc

def _first_error(error: ValidationError) -> str:
    """Primer problema de validación, en una línea y sin el volcado del documento."""
    problems = error.errors()
    if not problems:
        return "error de validación sin detalle"
    first = problems[0]
    location = ".".join(str(item) for item in first.get("loc", ()))
    return f"{location}: {first.get('msg', '')}"[:300]
