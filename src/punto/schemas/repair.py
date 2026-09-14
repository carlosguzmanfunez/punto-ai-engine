"""Contratos del ciclo de reparación autónoma acotado (ENGINE-6.1).

ENGINE-6.0 llegaba a ``REPAIRING`` y se detenía con ``WORKFLOW_REPAIR_DEFERRED``. ENGINE-6.1 activa
el ciclo, y estos son sus contratos: qué defecto se repara, con qué diagnóstico, bajo qué plan y con
qué autorización.

Tres decisiones que conviene leer antes de tocar nada:

- **El modelo propone, PUNTO decide.** Una reparación puede venir de un diagnóstico de modelo
  (``RepairDiagnosis``), pero la ``RepairDecision`` —clasificación, autoridad, política, intentos
  permitidos y plan de verificación— la calcula el kernel con reglas deterministas. Un modelo no
  puede declarar reparable lo que la constitución reserva a un humano.
- **Nada se repara sin plan ni sin snapshot.** El ``RepairPlan`` es un contrato: fija qué archivos
  se pueden tocar, cuáles no, qué cambios se esperan y qué roles vuelven a verificar. El
  ``RepairSnapshot`` captura el estado previo para que un fallo local y reversible se pueda
  deshacer con hashes, no con confianza.
- **Un finding no se cierra porque alguien lo diga.** ``RESOLVED`` solo lo escribe el kernel a
  partir de una verificación nueva que ya no reproduce el defecto y con los gates exigidos en PASS.
  El fingerprint canónico (campos estructurados, nunca timestamps) es lo que permite detectar que
  el mismo defecto volvió y cortar el bucle por falta de progreso.
- **El encargo viaja como contexto, no como un segundo Developer.** ``RepairTask`` reúne plan,
  diagnóstico, defectos, snapshot y criterios para que el **mismo** ``DeveloperRunner`` de siempre
  repare con ``DeveloperTask.repair``. No hay un runner de reparación distinto: lo que cambia es el
  contexto con el que se ejecuta el Developer, y sus reglas duras son texto fijo.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.workflow import (
    MAX_WORKFLOW_SUMMARY_CHARS,
    MAX_WORKFLOW_TEXT_CHARS,
    ArtifactReference,
    RoleName,
)

#: Cotas explícitas: un ciclo de reparación no puede hacer crecer el checkpoint sin límite.
MAX_REPAIR_FINDINGS: Final[int] = 32
MAX_REPAIR_FILES: Final[int] = 24
MAX_REPAIR_CYCLES: Final[int] = 8
MAX_REPAIR_EVIDENCE: Final[int] = 16
MAX_REPAIR_SNAPSHOT_ENTRIES: Final[int] = 64
MAX_REPAIR_DIAGNOSIS_UNKNOWNS: Final[int] = 12
#: Cuántas veces puede repetirse **el mismo** intento sin progreso antes de cortar el bucle.
#: Explícito y pequeño a propósito: el bucle no es «prueba hasta que salga».
MAX_IDENTICAL_REPAIR_FAILURES: Final[int] = 2

#: Objetivo determinista del encargo de reparación que recibe el Developer.
#:
#: No se compone con el texto del diagnóstico ni con el resumen de los defectos: el objetivo es la
#: instrucción de trabajo —corregir lo declarado sin ampliar el alcance—, y los defectos concretos
#: viajan en ``findings``, que es dato estructurado y no prosa que un modelo pueda reinterpretar.
REPAIR_OBJECTIVE: Final[str] = (
    "corrige los defectos declarados sin ampliar el alcance (mínima modificación)"
)

#: Reglas duras del prompt de reparación, en texto fijo y numerado.
#:
#: Son constantes y no se derivan de ningún campo a propósito: una reparación que pudiera reescribir
#: sus propias reglas dejaría de ser una reparación acotada. El orden es el de la lista, de modo que
#: el mismo encargo produce siempre el mismo prompt y un fallo se puede reproducir palabra por
#: palabra. Cada regla existe porque su infracción ya está prohibida en otra capa (la constitución,
#: el Policy Engine, el ``RepairGuard`` o el kernel) y el prompt solo la declara antes de trabajar.
REPAIR_HARD_RULES: Final[tuple[str, ...]] = (
    "1. mínima modificación: toca solo lo imprescindible para corregir el defecto.",
    "2. arregla solo los findings declarados: nada fuera de esa lista.",
    "3. no amplíes el alcance: sin refactorizaciones, renombrados ni reorganizaciones.",
    "4. no toques archivos prohibidos: respeta forbidden_files y los globs autorizados.",
    "5. no debilites gates: no relajes validaciones, comprobaciones ni umbrales.",
    "6. no elimines pruebas ni añadas skip/xfail: una prueba que falla se arregla, no se silencia.",
    "7. no subas presupuestos: ni coste, ni tiempo, ni archivos, ni reparaciones.",
    "8. no toques config/constitution.yaml ni config/permissions.yaml: son reglas de autoridad.",
    "9. no autoapruebes: la verificación la repiten los roles del plan, no quien repara.",
)

#: Cabecera del bloque de reglas duras. Declara que las reglas no se negocian.
REPAIR_RULES_HEADER: Final[str] = "REGLAS DURAS DE LA REPARACIÓN (no negociables):"

#: Cabecera de las restricciones adicionales declaradas por el plan de reparación.
#: Son subordinadas a las reglas duras: nunca las sustituyen ni las recortan.
REPAIR_EXTRA_RULES_HEADER: Final[str] = "RESTRICCIONES ADICIONALES DEL PLAN DE REPARACIÓN:"


class Repairability(StrEnum):
    """Clasificación determinista de un defecto. La calcula PUNTO, nunca el modelo.

    El modelo puede **proponer** un diagnóstico; la clasificación final decide si el motor puede
    reparar solo, si hace falta una persona, si no hay nada que reparar o si lo que falta es
    evidencia.
    """

    AUTONOMOUS_REPAIRABLE = "AUTONOMOUS_REPAIRABLE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    NON_REPAIRABLE = "NON_REPAIRABLE"
    RETRYABLE_INFRASTRUCTURE = "RETRYABLE_INFRASTRUCTURE"
    BLOCKED_EVIDENCE = "BLOCKED_EVIDENCE"
    SECURITY_STOP = "SECURITY_STOP"

    @property
    def allows_autonomous_repair(self) -> bool:
        """True solo si el motor puede reparar sin intervención humana."""
        return self is Repairability.AUTONOMOUS_REPAIRABLE


class RepairFindingStatus(StrEnum):
    """Estado durable de un defecto detectado por una etapa verificadora.

    ``RESOLVED`` no lo escribe un modelo ni el rol que reparó: solo el kernel, después de una
    verificación nueva que ya no reproduce el defecto.
    """

    OPEN = "OPEN"
    IN_REPAIR = "IN_REPAIR"
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    WAIVED_BY_HUMAN = "WAIVED_BY_HUMAN"


class RepairConfidence(StrEnum):
    """Categoría de confianza de un diagnóstico. Es una categoría, no una probabilidad inventada."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RepairAttemptStatus(StrEnum):
    """Resultado de un intento de reparación."""

    STARTED = "STARTED"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    ROLLED_BACK = "ROLLED_BACK"


class RepairCycleStatus(StrEnum):
    """Estado de un ciclo de reparación, para la traza y el informe final."""

    DECIDED = "DECIDED"
    PLANNED = "PLANNED"
    SNAPSHOTTED = "SNAPSHOTTED"
    APPLIED = "APPLIED"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"
    NO_PROGRESS = "NO_PROGRESS"
    ROLLED_BACK = "ROLLED_BACK"
    BLOCKED = "BLOCKED"


class RepairFinding(BaseModel):
    """Defecto con identidad estable, seguido de forma durable por el workflow.

    El ``fingerprint`` se calcula con campos **estructurados** (etapa, código, categoría, archivos y
    evidencia normalizada) y nunca con timestamps: es lo que permite reconocer «este mismo defecto
    volvió» después de una reparación, y por tanto lo que hace posible detectar la falta de
    progreso.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: UUID = Field(default_factory=uuid4)
    fingerprint: str = Field(..., min_length=16, max_length=64)
    source_role: RoleName = Field(...)
    source_stage: TaskStatus = Field(...)
    source_step_index: int = Field(default=0, ge=0)
    source_artifact: ArtifactReference | None = Field(default=None)
    category: str = Field(default="", max_length=60)
    severity: FindingSeverity = Field(default=FindingSeverity.MEDIUM)
    code: str = Field(default="", max_length=60)
    summary: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    evidence: str = Field(default="", max_length=MAX_WORKFLOW_TEXT_CHARS)
    evidence_refs: tuple[ArtifactReference, ...] = Field(
        default=(), max_length=MAX_REPAIR_EVIDENCE
    )
    affected_files: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_FILES)
    acceptance_criteria: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_EVIDENCE)
    repairability: Repairability = Field(default=Repairability.NON_REPAIRABLE)
    status: RepairFindingStatus = Field(default=RepairFindingStatus.OPEN)
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    repair_attempts: int = Field(default=0, ge=0)
    cycle_introduced: int = Field(default=0, ge=0)
    resolved_at: datetime | None = Field(default=None)
    resolution_evidence: tuple[ArtifactReference, ...] = Field(
        default=(), max_length=MAX_REPAIR_EVIDENCE
    )

    @property
    def is_open(self) -> bool:
        """True si el defecto sigue pendiente de reparación."""
        return self.status in (RepairFindingStatus.OPEN, RepairFindingStatus.IN_REPAIR)


class RepairDiagnosis(BaseModel):
    """Conclusión técnica estructurada sobre un defecto. Sin razonamiento privado.

    Es una **propuesta**: describe causa raíz, evidencia, archivos sospechosos, restricciones y
    estrategia, con su categoría de confianza y lo que sigue sin saberse. Si la evidencia no
    alcanza, la clasificación es ``BLOCKED_EVIDENCE`` y no se repara nada: una reparación
    especulativa es peor que un bloqueo declarado.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    diagnosis_id: UUID = Field(default_factory=uuid4)
    finding_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_REPAIR_FINDINGS)
    root_cause_summary: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    evidence_refs: tuple[ArtifactReference, ...] = Field(
        default=(), max_length=MAX_REPAIR_EVIDENCE
    )
    suspected_files: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_FILES)
    constraints: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_DIAGNOSIS_UNKNOWNS)
    proposed_strategy: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    confidence: RepairConfidence = Field(default=RepairConfidence.LOW)
    unknowns: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_DIAGNOSIS_UNKNOWNS)
    #: ``False`` si la produjo PUNTO con reglas deterministas en vez de un modelo.
    model_proposed: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utc_now)


class RepairPlan(BaseModel):
    """Contrato de una reparación: qué se puede tocar, qué se espera y quién lo verifica.

    El Developer de reparación no puede salirse de aquí: los ``target_files`` y sus globs son la
    autorización de escritura, y ``forbidden_files`` protege explícitamente lo que no se toca ni con
    una reparación (constitución, permisos, frontera de política, pruebas internas del Human Gate,
    límites de presupuesto, almacenes de secretos y gates de CI).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repair_id: UUID = Field(default_factory=uuid4)
    workflow_id: UUID = Field(...)
    cycle: int = Field(..., ge=1)
    finding_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_REPAIR_FINDINGS)
    diagnosis_id: UUID | None = Field(default=None)
    target_files: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_FILES)
    allowed_file_globs: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_FILES)
    forbidden_files: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_FILES)
    expected_changes: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_EVIDENCE)
    acceptance_criteria: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_EVIDENCE)
    verification_roles: tuple[RoleName, ...] = Field(default=(), max_length=8)
    risk: RiskLevel = Field(default=RiskLevel.LOW)
    authority: AuthorityLevel = Field(default=AuthorityLevel.LEVEL_0_AUTONOMOUS)
    policy_decision_id: UUID | None = Field(default=None)
    #: Llamadas de modelo y tokens que esta reparación puede gastar (autorización, no sugerencia).
    budget_model_calls: int = Field(default=0, ge=0)
    budget_total_tokens: int = Field(default=0, ge=0)
    idempotency_key: str = Field(..., min_length=1, max_length=120)
    plan_fingerprint: str = Field(..., min_length=16, max_length=64)
    created_at: datetime = Field(default_factory=utc_now)


class RepairDecision(BaseModel):
    """Decisión determinista del kernel sobre un conjunto de defectos.

    Incluye la clasificación, la autoridad y el riesgo **efectivos**, si hace falta una persona, el
    motivo, la decisión de política que la ampara, los intentos permitidos y el plan de verificación
    —la cadena de roles que hay que volver a pasar, calculada por PUNTO y no por un modelo—.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: UUID = Field(default_factory=uuid4)
    finding_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_REPAIR_FINDINGS)
    repairability: Repairability = Field(...)
    effective_risk: RiskLevel = Field(default=RiskLevel.LOW)
    effective_authority: AuthorityLevel = Field(default=AuthorityLevel.LEVEL_0_AUTONOMOUS)
    requires_human: bool = Field(default=False)
    reason: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    policy_decision_id: UUID | None = Field(default=None)
    max_allowed_attempts: int = Field(default=0, ge=0)
    verification_plan: tuple[RoleName, ...] = Field(default=(), max_length=8)
    origin_stage: TaskStatus = Field(default=TaskStatus.QA)
    restart_stage: TaskStatus = Field(default=TaskStatus.QA)
    created_at: datetime = Field(default_factory=utc_now)


class RepairSnapshotEntry(BaseModel):
    """Un archivo capturado antes de mutar: su hash y su tamaño, o su ausencia declarada."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., min_length=1, max_length=400)
    sha256: str = Field(default="", max_length=64)
    bytes: int = Field(default=0, ge=0)
    existed: bool = Field(default=True)


class RepairSnapshot(BaseModel):
    """Estado previo a una reparación, durable y verificable.

    Sirve para auditoría, para recuperación tras una caída y para **deshacer** una reparación local
    y reversible sin adivinar: si el hash actual de un archivo no es el que la reparación dejó, el
    rollback no se hace a ciegas.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: UUID = Field(default_factory=uuid4)
    repair_id: UUID = Field(...)
    cycle: int = Field(..., ge=1)
    workspace_path: str = Field(..., min_length=1, max_length=400)
    entries: tuple[RepairSnapshotEntry, ...] = Field(
        default=(), max_length=MAX_REPAIR_SNAPSHOT_ENTRIES
    )
    workspace_fingerprint: str = Field(default="", max_length=64)
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def files(self) -> int:
        """Archivos capturados (existentes o declarados ausentes)."""
        return len(self.entries)


class RepairAttempt(BaseModel):
    """Un intento de reparación, con su snapshot y los archivos que cambió."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repair_id: UUID = Field(...)
    cycle: int = Field(..., ge=1)
    attempt: int = Field(..., ge=1)
    status: RepairAttemptStatus = Field(default=RepairAttemptStatus.STARTED)
    snapshot_id: UUID | None = Field(default=None)
    changed_files: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_FILES)
    detail: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = Field(default=None)


class RepairCycle(BaseModel):
    """Resumen durable de un ciclo: de dónde salió, a dónde vuelve y cómo acabó."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cycle: int = Field(..., ge=1)
    repair_id: UUID = Field(...)
    decision_id: UUID | None = Field(default=None)
    plan_id: UUID | None = Field(default=None)
    snapshot_id: UUID | None = Field(default=None)
    origin_stage: TaskStatus = Field(default=TaskStatus.QA)
    restart_stage: TaskStatus = Field(default=TaskStatus.QA)
    status: RepairCycleStatus = Field(default=RepairCycleStatus.DECIDED)
    findings_in: tuple[UUID, ...] = Field(default=(), max_length=MAX_REPAIR_FINDINGS)
    findings_resolved: tuple[UUID, ...] = Field(default=(), max_length=MAX_REPAIR_FINDINGS)
    findings_open: tuple[UUID, ...] = Field(default=(), max_length=MAX_REPAIR_FINDINGS)
    plan_fingerprint: str = Field(default="", max_length=64)
    detail: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = Field(default=None)


class RepairTask(BaseModel):
    """Encargo de reparación que recibe el Developer real.

    Es el **contexto de reparación**: no crea un segundo Developer ni un runner aparte, viaja dentro
    de la tarea de desarrollo que el mismo ``DeveloperRunner`` de siempre ya sabe ejecutar
    (``DeveloperTask.repair``). Reúne lo que la reparación necesita para no improvisar: el contrato
    (``plan``: qué se puede tocar y qué se espera), el diagnóstico y los defectos que la motivan, el
    snapshot previo para poder deshacerla y los criterios con los que se volverá a verificar.

    Dos decisiones que conviene leer antes de tocar nada:

    - **el plan manda en la autorización**: ``target_files``, ``allowed_file_globs`` y
      ``forbidden_files`` se copian del ``RepairPlan``, que es el contrato del ciclo. Este modelo no
      los recalcula ni los amplía; el adaptador los copia tal cual a la tarea del Developer.
    - **las reglas duras son texto fijo**: :meth:`prompt_constraints` devuelve siempre
      :data:`REPAIR_HARD_RULES`. ``constraints`` solo puede **añadir** restricciones del plan, y
      viajan bajo una cabecera que las declara subordinadas: un campo libre no puede reescribir la
      regla que prohíbe tocar la constitución.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repair_id: UUID = Field(...)
    workflow_id: UUID = Field(...)
    task_id: UUID = Field(...)
    cycle: int = Field(..., ge=1)
    project_id: UUID = Field(...)
    objective: str = Field(..., min_length=1, max_length=MAX_WORKFLOW_TEXT_CHARS)
    plan: RepairPlan = Field(...)
    diagnosis: RepairDiagnosis | None = Field(default=None)
    findings: tuple[RepairFinding, ...] = Field(default=(), max_length=MAX_REPAIR_FINDINGS)
    target_files: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_FILES)
    allowed_file_globs: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_FILES)
    forbidden_files: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_FILES)
    snapshot_id: UUID | None = Field(default=None)
    acceptance_criteria: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_EVIDENCE)
    verification_roles: tuple[RoleName, ...] = Field(default=(), max_length=8)
    #: Restricciones adicionales del plan. Se **añaden** a las reglas duras, nunca las recortan.
    constraints: tuple[str, ...] = Field(default=(), max_length=MAX_REPAIR_DIAGNOSIS_UNKNOWNS)
    idempotency_key: str = Field(..., min_length=1, max_length=120)
    workspace_path: str = Field(default="")

    def prompt_constraints(self) -> str:
        """Devuelve el bloque de reglas duras del prompt, en texto estable y sin razonamiento.

        El texto es determinista: la misma tarea produce siempre el mismo bloque, sin depender del
        reloj, del azar ni del proceso que lo pide, de modo que un fallo se puede reproducir y
        auditar palabra por palabra. Contiene **siempre** :data:`REPAIR_HARD_RULES` completas; si la
        tarea declara ``constraints``, se añaden al final bajo
        :data:`REPAIR_EXTRA_RULES_HEADER`, que las declara subordinadas. Nunca se incluye aquí el
        diagnóstico ni la evidencia: el prompt declara límites, no una narración de por qué se
        repara.
        """
        lines = [REPAIR_RULES_HEADER, *REPAIR_HARD_RULES]
        if self.constraints:
            lines.append(REPAIR_EXTRA_RULES_HEADER)
            lines.extend(f"- {item}" for item in self.constraints)
        return "\n".join(lines)


__all__ = [
    "MAX_IDENTICAL_REPAIR_FAILURES",
    "MAX_REPAIR_CYCLES",
    "MAX_REPAIR_DIAGNOSIS_UNKNOWNS",
    "MAX_REPAIR_EVIDENCE",
    "MAX_REPAIR_FILES",
    "MAX_REPAIR_FINDINGS",
    "MAX_REPAIR_SNAPSHOT_ENTRIES",
    "REPAIR_EXTRA_RULES_HEADER",
    "REPAIR_HARD_RULES",
    "REPAIR_OBJECTIVE",
    "REPAIR_RULES_HEADER",
    "RepairAttempt",
    "RepairAttemptStatus",
    "RepairConfidence",
    "RepairCycle",
    "RepairCycleStatus",
    "RepairDecision",
    "RepairDiagnosis",
    "RepairFinding",
    "RepairFindingStatus",
    "RepairPlan",
    "RepairSnapshot",
    "RepairSnapshotEntry",
    "RepairTask",
    "Repairability",
]
