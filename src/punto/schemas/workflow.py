"""Contratos del kernel de workflow autónomo (ENGINE-6.0).

Vocabulario primero, porque es lo que hace determinista lo demás:

- **PUNTO decide, el modelo propone.** El modelo puede devolver análisis, planes, código,
  hallazgos y recomendaciones; **no** puede escribir el estado del workflow, el nivel de autoridad,
  la decisión de un Human Gate, el presupuesto, la aprobación ni el permiso de despliegue. Esos
  valores viven aquí y los calcula el kernel.
- **Estados del ciclo de vida**: se reutiliza :class:`punto.schemas.enums.TaskStatus` en lugar de
  duplicar una enumeración idéntica. La tabla de transiciones del workflow es propia y más estricta
  (vive en ``punto.workflow.state_machine``), así que compartir el vocabulario no relaja nada.
- **Nada de secretos**: ni credenciales, ni volcados de código, ni cadenas de razonamiento. Los
  campos de texto están acotados y las colecciones también.

Todos los modelos son inmutables (``frozen``), rechazan campos desconocidos (``extra="forbid"``) y
se serializan de forma determinista: dos ejecuciones del mismo caso producen el mismo JSON.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.execution import ModelUsage
from punto.schemas.planning import SCHEMA_VERSION

#: Máximos del contrato. Acotan la memoria, los informes y el tamaño de un checkpoint.
MAX_WORKFLOW_STEPS: Final[int] = 64
MAX_WORKFLOW_ROLE_CALLS: Final[int] = 48
MAX_WORKFLOW_MODEL_CALLS: Final[int] = 96
MAX_WORKFLOW_TRANSITIONS: Final[int] = 128
MAX_WORKFLOW_FINDINGS: Final[int] = 120
MAX_WORKFLOW_STATE_VISITS: Final[int] = 8
MAX_WORKFLOW_TEXT_CHARS: Final[int] = 2_000
MAX_WORKFLOW_SUMMARY_CHARS: Final[int] = 600
MAX_WORKFLOW_CONTEXT_CHARS: Final[int] = 4_000
MAX_WORKFLOW_ARTIFACTS: Final[int] = 40
MAX_WORKFLOW_EVIDENCE: Final[int] = 40
#: Cotas de las colecciones del contrato. Son explícitas y pequeñas: el checkpoint de un workflow no
#: puede crecer sin límite porque un rol decida devolver más cosas.
MAX_ACCEPTANCE_CRITERIA: Final[int] = 20
MAX_CHANGED_FILES: Final[int] = 80
MAX_CONTEXT_ENTRIES: Final[int] = 24
MAX_EFFECT_RECORDS: Final[int] = 48
MAX_ROLES_EXECUTED: Final[int] = 12
MAX_ROLE_SUPPORT: Final[int] = 12


class RoleName(StrEnum):
    """Roles que el kernel puede invocar.

    Son los roles que **ya existen** en el motor: el kernel los orquesta, no los reescribe.
    """

    ARCHITECT = "ARCHITECT"
    PLANNER = "PLANNER"
    DEVELOPER = "DEVELOPER"
    QA = "QA"
    SECURITY = "SECURITY"
    REVIEWER = "REVIEWER"
    CROSS_AUDIT = "CROSS_AUDIT"
    VISUAL_QA = "VISUAL_QA"


class RoleStatus(StrEnum):
    """Resultado normalizado de una ejecución de rol."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    NEEDS_REPAIR = "NEEDS_REPAIR"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PENDING_CREDENTIALS = "PENDING_CREDENTIALS"


class WorkflowDecisionKind(StrEnum):
    """Decisión que PUNTO calcula **después** de cada resultado de rol.

    Nunca la escribe el modelo: es una función determinista del resultado, del estado y del
    presupuesto.
    """

    CONTINUE = "CONTINUE"
    READY_FOR_NEXT_STAGE = "READY_FOR_NEXT_STAGE"
    ENTER_REPAIR = "ENTER_REPAIR"
    BLOCK = "BLOCK"
    REQUEST_HUMAN = "REQUEST_HUMAN"
    FAIL = "FAIL"
    COMPLETE = "COMPLETE"


class WorkflowFailureCode(StrEnum):
    """Códigos estables de fallo del kernel.

    Son la API pública de errores: el kernel no expone excepciones genéricas.
    """

    WORKFLOW_INVALID_TRANSITION = "WORKFLOW_INVALID_TRANSITION"
    WORKFLOW_TERMINAL = "WORKFLOW_TERMINAL"
    WORKFLOW_BUDGET_EXCEEDED = "WORKFLOW_BUDGET_EXCEEDED"
    WORKFLOW_LOOP_DETECTED = "WORKFLOW_LOOP_DETECTED"
    WORKFLOW_PROVIDER_UNAVAILABLE = "WORKFLOW_PROVIDER_UNAVAILABLE"
    WORKFLOW_ROLE_FAILED = "WORKFLOW_ROLE_FAILED"
    WORKFLOW_HUMAN_APPROVAL_REQUIRED = "WORKFLOW_HUMAN_APPROVAL_REQUIRED"
    WORKFLOW_CHECKPOINT_INVALID = "WORKFLOW_CHECKPOINT_INVALID"
    WORKFLOW_RESUME_FAILED = "WORKFLOW_RESUME_FAILED"
    WORKFLOW_INCOMPLETE_EVIDENCE = "WORKFLOW_INCOMPLETE_EVIDENCE"
    #: La autorización presentada para salir de un Human Gate no es válida, no corresponde a esta
    #: tarea/decisión o ya se consumió.
    WORKFLOW_APPROVAL_PROOF_INVALID = "WORKFLOW_APPROVAL_PROOF_INVALID"
    #: La misma clave de idempotencia llegó con contenido distinto: no es la misma petición.
    WORKFLOW_IDEMPOTENCY_CONFLICT = "WORKFLOW_IDEMPOTENCY_CONFLICT"
    #: El Policy Engine rechazó la acción de forma dura (default deny o archivo protegido).
    WORKFLOW_POLICY_REJECTED = "WORKFLOW_POLICY_REJECTED"
    #: Hay un efecto en vuelo cuyo resultado se desconoce: se bloquea para reconciliar, no se
    #: repite.
    WORKFLOW_EFFECT_RECONCILIATION_REQUIRED = "WORKFLOW_EFFECT_RECONCILIATION_REQUIRED"
    #: El workflow llegó a ``REPAIRING`` y se detiene ahí: el ciclo de reparación completo es
    #: ENGINE-6.1. Es un código propio para no disfrazar la pausa de otra cosa.
    WORKFLOW_REPAIR_DEFERRED = "WORKFLOW_REPAIR_DEFERRED"


class CredentialState(StrEnum):
    """Estado de la credencial de un proveedor, declarado y no supuesto."""

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    PENDING_CREDENTIALS = "PENDING_CREDENTIALS"


class EffectStatus(StrEnum):
    """Estado de un efecto con efectos secundarios.

    ``IN_FLIGHT`` significa «se pidió y no sabemos si ocurrió»: es el estado que impide repetir un
    efecto a ciegas tras una caída.
    """

    IN_FLIGHT = "IN_FLIGHT"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class WorkflowBudget(BaseModel):
    """Presupuesto explícito de un workflow. Todos los límites, con valor por defecto acotado."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_steps: int = Field(default=32, ge=1, le=MAX_WORKFLOW_STEPS)
    max_role_calls: int = Field(default=24, ge=1, le=MAX_WORKFLOW_ROLE_CALLS)
    max_model_calls: int = Field(default=48, ge=0, le=MAX_WORKFLOW_MODEL_CALLS)
    max_repairs: int = Field(
        default=0,
        ge=0,
        le=8,
        description=(
            "Reparaciones permitidas. Por defecto 0: ENGINE-6.0 llega a REPAIRING pero no ejecuta "
            "el ciclo de reparación, que es ENGINE-6.1."
        ),
    )
    max_total_tokens: int = Field(default=200_000, ge=0)
    max_wall_time_seconds: float = Field(default=3_600.0, gt=0)
    max_failures: int = Field(default=3, ge=0, le=16)
    #: Protección de bucles, calculada por PUNTO.
    max_state_visits: int = Field(default=4, ge=1, le=MAX_WORKFLOW_STATE_VISITS)
    max_transitions: int = Field(default=48, ge=1, le=MAX_WORKFLOW_TRANSITIONS)


class WorkflowUsage(BaseModel):
    """Presupuesto consumido. Lo actualiza PUNTO, nunca el modelo.

    Separa lo **gastado** de lo **reservado** (hallazgo V604-01): una llamada ya iniciada se reserva
    antes de salir y solo se convierte en consumo real cuando el resultado se conoce. Si el proceso
    muere en medio, la reserva sigue contando —``max_total_tokens`` es un tope de
    ``total_tokens + tokens_reserved``— de modo que una llamada perdida no reaparece como cero
    consumo y el workflow no puede rebasar su presupuesto por un crash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: int = Field(default=0, ge=0)
    role_calls: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    #: Llamadas de modelo reservadas y aún no convertidas en consumo real.
    model_calls_reserved: int = Field(default=0, ge=0)
    repairs: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    #: Tokens de entrada y salida reservados y aún no convertidos en consumo real.
    tokens_reserved: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    transitions: int = Field(default=0, ge=0)
    wall_time_seconds: float = Field(default=0.0, ge=0.0)
    #: Veces que se ha entrado en cada estado, para detectar bucles.
    state_visits: tuple[tuple[str, int], ...] = Field(default=())

    @property
    def tokens_committed(self) -> int:
        """Tokens comprometidos: los gastados más los reservados que aún no se han liquidado."""
        return self.total_tokens + self.tokens_reserved

    @property
    def model_calls_committed(self) -> int:
        """Llamadas de modelo comprometidas: las gastadas más las reservadas sin liquidar."""
        return self.model_calls + self.model_calls_reserved

    def visit_count(self, status: TaskStatus) -> int:
        """Veces que se ha entrado en un estado."""
        for name, count in self.state_visits:
            if name == status.value:
                return count
        return 0

    def with_visit(self, status: TaskStatus) -> WorkflowUsage:
        """Devuelve el consumo con una visita más al estado indicado."""
        current = dict(self.state_visits)
        current[status.value] = current.get(status.value, 0) + 1
        ordered = tuple(sorted(current.items()))
        return self.model_copy(update={"state_visits": ordered})


class WorkflowFinding(BaseModel):
    """Hallazgo normalizado de un rol, con gravedad comparable entre roles."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: RoleName = Field(..., description="Rol que lo encontró.")
    severity: FindingSeverity = Field(..., description="Gravedad.")
    category: str = Field(default="", max_length=80, description="Categoría declarada por el rol.")
    message: str = Field(..., min_length=1, max_length=MAX_WORKFLOW_TEXT_CHARS)
    evidence: str = Field(default="", max_length=MAX_WORKFLOW_TEXT_CHARS)

    @property
    def blocks_approval(self) -> bool:
        """True si la gravedad impide aprobar el trabajo."""
        return self.severity.blocks_approval


class ProviderCapability(BaseModel):
    """Lo que un proveedor **puede** hacer, declarado por PUNTO.

    CAMUS consulta esta tabla; no la supone. Un rol no se ejecuta «porque debería funcionar»: se
    ejecuta si hay capacidad declarada y disponible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(..., min_length=1, max_length=40)
    model: str = Field(default="", max_length=120)
    role_support: tuple[RoleName, ...] = Field(
        default=(), max_length=MAX_ROLE_SUPPORT, description="Roles que puede cubrir."
    )
    vision: bool = Field(default=False, description="True si acepta imágenes.")
    structured_output: bool = Field(default=False, description="True si respeta un JSON Schema.")
    available: bool = Field(default=False, description="True si se puede invocar ahora mismo.")
    credential_state: CredentialState = Field(default=CredentialState.PENDING_CREDENTIALS)
    live_verified: bool = Field(
        default=False, description="True solo si una ejecución real lo demostró."
    )
    notes: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)

    def supports(self, role: RoleName) -> bool:
        """True si el proveedor declara cubrir el rol."""
        return role in self.role_support


class ModelCallLimits(BaseModel):
    """Cota de llamadas y de tokens **declarada** por el runner de un rol.

    La consulta el presupuesto del workflow para no autorizar una ejecución que podría gastar más de
    lo permitido: es la única forma de conocer el máximo real del proveedor sin depender de su
    implementación (hallazgos V603-01 y V604-01).

    ``uses_ai`` distingue un runner con modelo de uno determinista, y una cota en ``None`` significa
    **desconocida**, no «sin límite»: para un runner con IA, una cota desconocida es motivo de
    bloqueo, porque PUNTO no puede garantizar que no se rebase el presupuesto. El valor por defecto
    es ``True``: declarar una cota sin decir que no se usa IA es declarar un runner **con** modelo,
    y el silencio nunca habilita el camino sin saldo que el hallazgo V605-05 abre a quien lo
    declara explícitamente.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: True si el runner puede llamar a un modelo (y por tanto consume presupuesto de modelo).
    uses_ai: bool = Field(default=True)
    #: ``None`` = el runner no declara cota de llamadas.
    max_model_calls: int | None = Field(default=None, ge=1)
    #: ``None`` = el runner no declara cota de tokens de entrada.
    max_input_tokens: int | None = Field(default=None, ge=1)
    #: ``None`` = el runner no declara cota de tokens de salida.
    max_output_tokens: int | None = Field(default=None, ge=1)

    @property
    def known(self) -> bool:
        """True si el runner declara las tres cotas: sin ellas no hay gasto autónomo seguro."""
        return (
            self.max_model_calls is not None
            and self.max_input_tokens is not None
            and self.max_output_tokens is not None
        )


class BudgetAllowance(BaseModel):
    """Lo que el rol **puede** gastar en modelo, calculado por el kernel antes de ejecutarlo.

    Es la frontera que convierte ``max_model_calls`` y ``max_total_tokens`` en un límite real
    (hallazgo V602-04): sin ella el kernel solo se enteraba del gasto **después** de la llamada, y
    un ``max_model_calls=0`` todavía permitía una invocación real al proveedor. El rol recibe el
    saldo y no puede rebasarlo: si el saldo es cero, no llama al modelo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_calls_remaining: int = Field(default=0, ge=0)
    tokens_remaining: int = Field(default=0, ge=0)
    wall_time_seconds_remaining: float = Field(default=0.0, ge=0.0)


class RoleExecutionRequest(BaseModel):
    """Lo que el kernel le pide a un rol. Sin secretos y con el contexto acotado."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: UUID = Field(..., description="Workflow al que pertenece el paso.")
    step_index: int = Field(..., ge=0, description="Índice del paso.")
    role: RoleName = Field(..., description="Rol a ejecutar.")
    stage: TaskStatus = Field(..., description="Estado del workflow que motiva el paso.")
    task_id: UUID = Field(..., description="Tarea del motor asociada.")
    project_id: UUID = Field(..., description="Proyecto asociado.")
    objective: str = Field(..., min_length=1, max_length=MAX_WORKFLOW_TEXT_CHARS)
    acceptance_criteria: tuple[str, ...] = Field(default=(), max_length=MAX_ACCEPTANCE_CRITERIA)
    workspace_path: str = Field(default="", max_length=MAX_WORKFLOW_TEXT_CHARS)
    changed_files: tuple[str, ...] = Field(default=(), max_length=MAX_CHANGED_FILES)
    context_summary: str = Field(default="", max_length=MAX_WORKFLOW_CONTEXT_CHARS)
    #: Referencias a artefactos de etapas anteriores. El siguiente rol reconstruye su entrada desde
    #: el checkpoint y los almacenes del motor, no desde variables de un proceso anterior.
    references: tuple[ArtifactReference, ...] = Field(
        default=(), max_length=MAX_WORKFLOW_ARTIFACTS
    )
    #: Saldo de gasto en modelo autorizado para este intento. ``None`` significa «el kernel no
    #: declaró saldo»: un ejecutor real no puede inventarse uno y, por defecto, no gasta.
    budget_allowance: BudgetAllowance | None = Field(default=None)
    attempt: int = Field(default=1, ge=1, description="Intento técnico, no reparación.")
    idempotency_key: str = Field(..., min_length=1, max_length=120)


class RoleExecutionResult(BaseModel):
    """Resultado **normalizado** de cualquier rol.

    Es la frontera por la que el kernel deja de mirar hacia dentro de cada rol: da igual si por
    debajo hay DeepSeek, Anthropic o un transporte falso.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: RoleName = Field(..., description="Rol ejecutado.")
    status: RoleStatus = Field(..., description="Resultado normalizado.")
    summary: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    artifacts: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_WORKFLOW_ARTIFACTS,
        description=f"Referencias acotadas (máx. {MAX_WORKFLOW_ARTIFACTS}).",
    )
    #: Referencias **tipadas** a artefactos que el rol ya dejó en un almacén estable (con su
    #: ``store``, su digest y su tamaño). Son el handoff durable: viajan al checkpoint y un proceso
    #: nuevo las resuelve sin volver a ejecutar la etapa que las produjo. ``artifacts`` sigue siendo
    #: el sitio de los punteros declarados como texto (ids de componentes, nombres de informe).
    artifact_references: tuple[ArtifactReference, ...] = Field(
        default=(),
        max_length=MAX_WORKFLOW_ARTIFACTS,
        description=f"Artefactos durables referenciados (máx. {MAX_WORKFLOW_ARTIFACTS}).",
    )
    findings: tuple[WorkflowFinding, ...] = Field(
        default=(),
        max_length=MAX_WORKFLOW_FINDINGS,
        description=f"Hallazgos normalizados (máx. {MAX_WORKFLOW_FINDINGS}).",
    )
    recommendation: str = Field(default="", max_length=MAX_WORKFLOW_TEXT_CHARS)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    #: Llamadas reales al modelo que hizo el rol. No se infieren de los tokens: un rol puede llamar
    #: tres veces gastando pocos tokens, o una sola gastando muchos.
    model_calls: int = Field(default=0, ge=0, le=64)
    provider: str = Field(default="", max_length=40)
    model: str = Field(default="", max_length=120)
    attempts: int = Field(default=1, ge=1)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime = Field(default_factory=utc_now)
    error_code: WorkflowFailureCode | None = Field(default=None)
    error_detail: str = Field(default="", max_length=MAX_WORKFLOW_TEXT_CHARS)

    @property
    def blocking_findings(self) -> tuple[WorkflowFinding, ...]:
        """Hallazgos que impiden aprobar."""
        return tuple(finding for finding in self.findings if finding.blocks_approval)

    @property
    def succeeded(self) -> bool:
        """True solo si el rol terminó y no dejó hallazgos bloqueantes."""
        return self.status is RoleStatus.COMPLETED and not self.blocking_findings


class WorkflowStep(BaseModel):
    """Un paso del workflow: una invocación de rol, con su decisión calculada."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(..., ge=0)
    role: RoleName = Field(...)
    stage: TaskStatus = Field(..., description="Estado en el que se ejecutó el rol.")
    status: RoleStatus = Field(...)
    attempt: int = Field(default=1, ge=1)
    idempotency_key: str = Field(..., min_length=1, max_length=120)
    decision: WorkflowDecisionKind = Field(...)
    summary: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    findings: int = Field(default=0, ge=0)
    blocking_findings: int = Field(default=0, ge=0)
    provider: str = Field(default="", max_length=40)
    model: str = Field(default="", max_length=120)
    total_tokens: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime = Field(default_factory=utc_now)
    error_code: WorkflowFailureCode | None = Field(default=None)
    error_detail: str = Field(default="", max_length=MAX_WORKFLOW_TEXT_CHARS)


class WorkflowTransition(BaseModel):
    """Transición aplicada, con su motivo. Es la traza auditable del workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(..., ge=0)
    from_status: TaskStatus = Field(...)
    to_status: TaskStatus = Field(...)
    decision: WorkflowDecisionKind = Field(...)
    reason: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    authority: AuthorityLevel = Field(default=AuthorityLevel.LEVEL_0_AUTONOMOUS)
    step_index: int | None = Field(default=None, ge=0)
    created_at: datetime = Field(default_factory=utc_now)


class HumanGateRequest(BaseModel):
    """Solicitud de aprobación humana. Sin secretos, sin volcados y sin razonamiento interno."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    workflow_id: UUID = Field(...)
    task_id: UUID = Field(...)
    reason_code: WorkflowFailureCode = Field(...)
    requested_action: str = Field(..., min_length=1, max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    risk: RiskLevel = Field(...)
    authority_required: AuthorityLevel = Field(...)
    current_state: TaskStatus = Field(...)
    proposed_next_state: TaskStatus = Field(...)
    context_summary: str = Field(default="", max_length=MAX_WORKFLOW_CONTEXT_CHARS)
    #: Referencias a la autorización **real** del Policy Engine / Human Gate. El kernel no sustituye
    #: ninguno de los dos: los referencia. Sin decisión de política no hay aprobación que valga.
    policy_decision_id: UUID | None = Field(default=None)
    policy_outcome: str = Field(default="", max_length=40)
    approval_id: UUID | None = Field(
        default=None, description="Solicitud del HumanGate real que se está pidiendo aprobar."
    )
    human_gate_resume_status: TaskStatus = Field(
        default=TaskStatus.IN_PROGRESS,
        description=(
            "Estado de reanudación registrado en el HumanGate. Debe pertenecer a los estados "
            "reanudables del motor; no es el destino del workflow, que va en "
            "``proposed_next_state``."
        ),
    )
    created_at: datetime = Field(default_factory=utc_now)


class ArtifactReference(BaseModel):
    """Referencia **segura** a un artefacto de una etapa.

    No guarda el contenido: guarda cómo encontrarlo y cómo comprobar que es el mismo (etiqueta,
    digest, tamaño y referencia al almacén estable del motor). Así el checkpoint no crece con
    código privado ni con volcados, y a la vez la etapa siguiente puede reconstruir su entrada en
    un proceso nuevo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(..., min_length=1, max_length=60, description="Tipo del artefacto.")
    label: str = Field(default="", max_length=200, description="Nombre legible y acotado.")
    store: str = Field(default="", max_length=80, description="Almacén estable donde vive.")
    reference: str = Field(default="", max_length=400, description="Identificador en ese almacén.")
    digest: str = Field(
        default="", max_length=64, description="sha256 del contenido, si se conoce."
    )
    bytes_written: int = Field(default=0, ge=0)


class StageArtifacts(BaseModel):
    """Lo que una etapa deja para las siguientes, en forma estructurada y acotada."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: RoleName = Field(...)
    stage: TaskStatus = Field(...)
    step_index: int = Field(..., ge=0)
    summary: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    references: tuple[ArtifactReference, ...] = Field(
        default=(), max_length=MAX_WORKFLOW_ARTIFACTS
    )
    findings: tuple[WorkflowFinding, ...] = Field(
        default=(), max_length=MAX_WORKFLOW_FINDINGS
    )
    recorded_at: datetime = Field(default_factory=utc_now)


class EffectRecord(BaseModel):
    """Efecto con efectos secundarios, con su intención durable y su estado.

    El kernel apunta la **intención** antes de ejecutar un efecto y la resuelve después. Si el
    proceso muere en medio, el registro queda ``IN_FLIGHT`` y una reanudación no repite el efecto a
    ciegas: bloquea para reconciliar.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: str = Field(..., min_length=1, max_length=120)
    action: str = Field(..., min_length=1, max_length=120)
    role: RoleName = Field(...)
    step_index: int = Field(..., ge=0)
    status: EffectStatus = Field(default=EffectStatus.IN_FLIGHT)
    reversible: bool = Field(default=True, description="False si repetirlo sería irreversible.")
    detail: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = Field(default=None)


class WorkflowFailure(BaseModel):
    """Fallo del kernel con código estable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: WorkflowFailureCode = Field(...)
    detail: str = Field(default="", max_length=MAX_WORKFLOW_TEXT_CHARS)
    step_index: int | None = Field(default=None, ge=0)
    role: RoleName | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)


class WorkflowResult(BaseModel):
    """Resultado final de un workflow. Solo PUNTO lo escribe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: TaskStatus = Field(...)
    summary: str = Field(default="", max_length=MAX_WORKFLOW_SUMMARY_CHARS)
    roles_executed: tuple[RoleName, ...] = Field(default=(), max_length=MAX_ROLES_EXECUTED)
    findings: tuple[WorkflowFinding, ...] = Field(
        default=(),
        max_length=MAX_WORKFLOW_FINDINGS,
        description="Hallazgos reales de los roles: no se pierden al cerrar el workflow.",
    )
    evidence: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_WORKFLOW_EVIDENCE,
        description=f"Evidencia acotada (máx. {MAX_WORKFLOW_EVIDENCE}).",
    )
    completed_at: datetime = Field(default_factory=utc_now)

    @property
    def blocking_findings(self) -> tuple[WorkflowFinding, ...]:
        """Hallazgos bloqueantes del conjunto."""
        return tuple(finding for finding in self.findings if finding.blocks_approval)


class WorkflowRequest(BaseModel):
    """Intención que entra al kernel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID = Field(...)
    project_id: UUID = Field(...)
    objective: str = Field(..., min_length=1, max_length=MAX_WORKFLOW_TEXT_CHARS)
    #: Acción canónica del catálogo de autoridad. Es lo que evalúa el Policy Engine: sin acción
    #: declarada no se inventa una, y una acción desconocida cae en *default deny*.
    action: str = Field(..., min_length=1, max_length=120)
    acceptance_criteria: tuple[str, ...] = Field(
        default=(), max_length=MAX_ACCEPTANCE_CRITERIA
    )
    workspace_path: str = Field(default="", max_length=MAX_WORKFLOW_TEXT_CHARS)
    changed_files: tuple[str, ...] = Field(default=(), max_length=MAX_CHANGED_FILES)
    context_summary: str = Field(default="", max_length=MAX_WORKFLOW_CONTEXT_CHARS)
    #: Referencias durables que el *composition root* declara para el workflow: evidencia que ya
    #: existe en un almacén estable antes de la primera etapa (por ejemplo, el informe técnico de la
    #: sesión web medido en un navegador real). Viajan al checkpoint y se entregan a cada rol junto
    #: con las de las etapas anteriores, de modo que ninguna etapa depende de memoria efímera
    #: (hallazgo V603-04).
    evidence_references: tuple[ArtifactReference, ...] = Field(
        default=(), max_length=MAX_WORKFLOW_ARTIFACTS
    )
    #: Verificación visual declarada por el llamante. Es una **señal a favor**: si el perfil web del
    #: proyecto la exige, se ejecuta aunque esto venga en ``False``.
    web_visual_required: bool = Field(default=False)
    #: Ruta del proyecto en el workspace, para que el perfil web pueda decidir aplicabilidad.
    project_path: str = Field(default="", max_length=MAX_WORKFLOW_TEXT_CHARS)
    #: Auditoría cruzada: verificación independiente en la frontera de aprobación.
    cross_audit_required: bool = Field(default=True)
    #: Riesgo y autoridad **declarados** por el llamante. El kernel usa los efectivos que calcula el
    #: Policy Engine: declarar menos no rebaja una acción L3.
    risk: RiskLevel = Field(default=RiskLevel.LOW)
    authority: AuthorityLevel = Field(default=AuthorityLevel.LEVEL_0_AUTONOMOUS)
    budget: WorkflowBudget = Field(default_factory=WorkflowBudget)
    #: Clave de idempotencia de la petición: repetirla no duplica efectos.
    idempotency_key: str = Field(..., min_length=1, max_length=120)
    requested_by: str = Field(default="CAMUS", max_length=80)
    created_at: datetime = Field(default_factory=utc_now)


class WorkflowRun(BaseModel):
    """Estado completo de una ejecución. Es lo que se guarda en cada checkpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    schema_version: str = Field(default=SCHEMA_VERSION)
    workflow_id: UUID = Field(...)
    request: WorkflowRequest = Field(...)
    #: Huella canónica de la petición: distingue dos peticiones con la misma clave de idempotencia
    #: pero contenido distinto (eso es un conflicto, no una repetición).
    request_fingerprint: str = Field(default="", max_length=64)
    status: TaskStatus = Field(default=TaskStatus.NEW)
    revision: int = Field(default=0, ge=0, description="Sube en cada transición aplicada.")
    steps: tuple[WorkflowStep, ...] = Field(default=(), max_length=MAX_WORKFLOW_STEPS)
    transitions: tuple[WorkflowTransition, ...] = Field(
        default=(), max_length=MAX_WORKFLOW_TRANSITIONS
    )
    usage: WorkflowUsage = Field(default_factory=WorkflowUsage)
    #: Lo que cada etapa deja para las siguientes: handoff estructurado y durable.
    stage_artifacts: tuple[StageArtifacts, ...] = Field(
        default=(), max_length=MAX_CONTEXT_ENTRIES
    )
    #: Intenciones y resultados de efectos con efectos secundarios, para no repetirlos a ciegas.
    effects: tuple[EffectRecord, ...] = Field(default=(), max_length=MAX_EFFECT_RECORDS)
    #: Decisión de política vigente, si la hay: es la autoridad efectiva del workflow.
    policy_decision_id: UUID | None = Field(default=None)
    effective_authority: AuthorityLevel | None = Field(default=None)
    effective_risk: RiskLevel | None = Field(default=None)
    human_gate: HumanGateRequest | None = Field(default=None)
    human_gate_approved: bool = Field(
        default=False,
        description=(
            "True solo si una reanudación explícita aprobó el Human Gate. El kernel no puede "
            "aprobarse a sí mismo: este campo lo escribe la operación de reanudación, no un paso."
        ),
    )
    failure: WorkflowFailure | None = Field(default=None)
    result: WorkflowResult | None = Field(default=None)
    started_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = Field(default=None)

    @property
    def task_id(self) -> UUID:
        """Tarea asociada."""
        return self.request.task_id

    @property
    def project_id(self) -> UUID:
        """Proyecto asociado."""
        return self.request.project_id

    @property
    def is_terminal(self) -> bool:
        """True si el workflow ya no puede avanzar sin una operación explícita nueva."""
        return self.status in TERMINAL_WORKFLOW_STATUSES

    def last_step(self) -> WorkflowStep | None:
        """Último paso ejecutado, si lo hay."""
        return self.steps[-1] if self.steps else None


class WorkflowCheckpoint(BaseModel):
    """Metadatos de un checkpoint persistido.

    El contenido del workflow se guarda serializado junto a estos metadatos; aquí solo viaja lo
    necesario para reanudar, auditar y **detectar corrupción**: el digest del contenido.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    workflow_id: UUID = Field(...)
    sequence: int = Field(..., ge=0, description="Orden del checkpoint dentro del workflow.")
    status: TaskStatus = Field(...)
    revision: int = Field(..., ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    digest: str = Field(..., min_length=64, max_length=64, description="sha256 del contenido.")
    bytes_written: int = Field(default=0, ge=0)


#: Estados de los que no se sale sin una operación explícita de un workflow nuevo.
TERMINAL_WORKFLOW_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

#: Estados que pausan el workflow pero pueden reanudarse.
PAUSED_WORKFLOW_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.BLOCKED, TaskStatus.HUMAN_APPROVAL}
)


__all__ = [
    "MAX_ACCEPTANCE_CRITERIA",
    "MAX_CHANGED_FILES",
    "MAX_CONTEXT_ENTRIES",
    "MAX_EFFECT_RECORDS",
    "MAX_ROLES_EXECUTED",
    "MAX_ROLE_SUPPORT",
    "MAX_WORKFLOW_ARTIFACTS",
    "MAX_WORKFLOW_CONTEXT_CHARS",
    "MAX_WORKFLOW_EVIDENCE",
    "MAX_WORKFLOW_FINDINGS",
    "MAX_WORKFLOW_MODEL_CALLS",
    "MAX_WORKFLOW_ROLE_CALLS",
    "MAX_WORKFLOW_STATE_VISITS",
    "MAX_WORKFLOW_STEPS",
    "MAX_WORKFLOW_SUMMARY_CHARS",
    "MAX_WORKFLOW_TEXT_CHARS",
    "MAX_WORKFLOW_TRANSITIONS",
    "PAUSED_WORKFLOW_STATUSES",
    "TERMINAL_WORKFLOW_STATUSES",
    "ArtifactReference",
    "CredentialState",
    "EffectRecord",
    "EffectStatus",
    "HumanGateRequest",
    "ProviderCapability",
    "RoleExecutionRequest",
    "RoleExecutionResult",
    "RoleName",
    "RoleStatus",
    "StageArtifacts",
    "WorkflowBudget",
    "WorkflowCheckpoint",
    "WorkflowDecisionKind",
    "WorkflowFailure",
    "WorkflowFailureCode",
    "WorkflowFinding",
    "WorkflowRequest",
    "WorkflowResult",
    "WorkflowRun",
    "WorkflowStep",
    "WorkflowTransition",
    "WorkflowUsage",
]
