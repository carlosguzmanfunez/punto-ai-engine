"""Contrato del ciclo de desarrollo gobernado (PILOT-04).

Extiende el vocabulario de PILOT-03 en vez de reemplazarlo: la **solicitud** sigue siendo
:class:`~punto.schemas.build.BuildRequest` (objetivo, destino registrado, rol, límites y alcance
declarado), y lo que se añade aquí es lo que hace falta para **aplicar** cambios de verdad:

- :class:`DevelopmentPlan` — lo que PUNTO va a tocar, declarado **antes** de escribir;
- :class:`FileChangeProposal` — un cambio concreto con su operación, su precondición y su motivo;
- :class:`ContextRequest` — una petición de contexto del proveedor, con motivo, que PUNTO concede o
  deniega;
- :class:`DevelopmentResult` — el desenlace, con la autoridad limitada a lo local.

Frontera de autoridad de esta fase, escrita en el propio contrato: ``authority`` es
``LOCAL_APPLY_ONLY`` — PUNTO puede escribir y confirmar **dentro** del workspace gobernado y nada
más. ``published`` es siempre ``False``: publicar no es una operación de este ciclo, y
decirlo con un
campo evita que una fase posterior lo dé por supuesto.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from punto.common import utc_now
from punto.schemas.build import BuildValidationIssue  # el vocabulario de incidencias ya existe

#: Cotas del contrato: un plan es un plan, no un volcado del repositorio.
MAX_PLAN_ITEMS: Final[int] = 40
MAX_ITEM_CHARS: Final[int] = 300
MAX_PATH_CHARS: Final[int] = 400
MAX_CONTENT_CHARS: Final[int] = 200_000
MAX_REASON_CHARS: Final[int] = 400
MAX_RISKS: Final[int] = 10
MAX_CONTEXT_REQUESTS: Final[int] = 8
MAX_VERIFICATION_COMMANDS: Final[int] = 8

#: Caracteres de control que nunca forman parte de una ruta declarada.
_CONTROL_CHARS: Final[tuple[str, ...]] = (
    *(chr(code) for code in range(0x20)),
    "\x7f",
)


class RepositoryOperation(StrEnum):
    """Operaciones que la frontera de recursos puede autorizar, una a una.

    Se autorizan por separado a propósito: leer no implica escribir, ejecutar no implica confirmar.
    """

    READ = "READ"
    WRITE = "WRITE"
    CREATE = "CREATE"
    DELETE = "DELETE"
    EXECUTE = "EXECUTE"
    COMMIT = "COMMIT"


class ChangeOperation(StrEnum):
    """Operación de un cambio propuesto sobre un fichero.

    ``RENAME`` y ``MOVE`` son la misma primitiva local —cambiar un recurso de sitio dentro del
    proyecto— y se distinguen porque el proveedor declara su intención: un renombrado conserva el
    directorio y un movimiento lo cambia. Las dos son reversibles con el checkpoint del ciclo.
    """

    CREATE = "CREATE"
    MODIFY = "MODIFY"
    DELETE = "DELETE"
    RENAME = "RENAME"
    MOVE = "MOVE"


class ExpansionStatus(StrEnum):
    """Veredicto del sobre adaptativo sobre una expansión de alcance."""

    AUTO_APPROVED = "AUTO_APPROVED"
    HUMAN_GATE = "HUMAN_GATE_REQUIRED"
    DENIED = "DENIED"


class PlanStatus(StrEnum):
    """Veredicto de PUNTO sobre un plan."""

    VALID = "PLAN_VALID"
    REJECTED = "PLAN_REJECTED"


class DevelopmentStatus(StrEnum):
    """Desenlace del ciclo de desarrollo."""

    COMPLETED = "DEVELOPMENT_COMPLETED"          # cambios aplicados y verificados
    PLAN_REJECTED = "DEVELOPMENT_PLAN_REJECTED"  # el plan no superó la validación de PUNTO
    CHANGE_REJECTED = "DEVELOPMENT_CHANGE_REJECTED"
    VERIFICATION_FAILED = "DEVELOPMENT_VERIFICATION_FAILED"  # agotó las reparaciones
    ROLLED_BACK = "DEVELOPMENT_ROLLED_BACK"      # se revirtió a propósito
    PROVIDER_FAILED = "DEVELOPMENT_PROVIDER_FAILED"
    BLOCKED = "DEVELOPMENT_BLOCKED"              # la frontera denegó algo necesario


def _clean_path(value: str) -> str:
    """Normaliza y valida una ruta declarada: relativa, sin escapes y sin sorpresas.

    Raises:
        ValueError: si la ruta es absoluta, sube por ``..``, trae esquema URI, byte nulo o
            caracteres de control.
    """
    text = value.strip().replace("\\", "/")
    if not text:
        raise ValueError("la ruta no puede estar vacía")
    if len(text) > MAX_PATH_CHARS:
        raise ValueError(f"la ruta supera {MAX_PATH_CHARS} caracteres")
    if any(character in text for character in _CONTROL_CHARS):
        raise ValueError("la ruta contiene caracteres de control")
    if "://" in text or text.startswith("file:"):
        raise ValueError("la ruta no puede ser una URI")
    if text.startswith("/") or text.startswith("//") or PurePosixPath(text).is_absolute():
        raise ValueError("la ruta debe ser relativa al destino")
    parts = PurePosixPath(text).parts
    if any(part == ".." for part in parts):
        raise ValueError("la ruta no puede subir con '..'")
    normalized = PurePosixPath(*[part for part in parts if part not in ("", ".")]).as_posix()
    if not normalized or normalized == ".":
        raise ValueError("la ruta no apunta a ningún fichero")
    return normalized


class ContextRequest(BaseModel):
    """Petición de contexto del proveedor: qué fichero pide y por qué.

    No concede nada por sí misma: PUNTO valida la ruta, el alcance, la frontera de secretos y el
    presupuesto antes de entregarla.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(description="Ruta relativa del fichero pedido.")
    reason: str = Field(
        default="", max_length=MAX_REASON_CHARS, description="Para qué lo necesita."
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Acepta solo rutas declarables."""
        return _clean_path(value)


class FileChangeProposal(BaseModel):
    """Un cambio concreto propuesto por el BUILDER, listo para ser validado por PUNTO."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(description="Ruta relativa del fichero.")
    operation: ChangeOperation = Field(description="Crear, modificar, borrar, renombrar o mover.")
    source_path: str | None = Field(
        default=None,
        max_length=MAX_PATH_CHARS,
        description="Origen en RENAME/MOVE. Debe estar vacío en el resto de operaciones.",
    )
    content: str | None = Field(
        default=None,
        max_length=MAX_CONTENT_CHARS,
        description="Contenido exacto para CREATE/MODIFY. Nunca para DELETE/RENAME/MOVE.",
    )
    expected_sha256: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Huella del contenido que el proveedor leyó; evita escribir sobre algo "
            "distinto."
        ),
    )
    reason: str = Field(default="", max_length=MAX_REASON_CHARS)
    acceptance_criterion: str = Field(
        default="", max_length=MAX_ITEM_CHARS, description="Criterio del encargo que satisface."
    )

    @field_validator("path", "source_path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        """Acepta solo rutas declarables."""
        return None if value is None else _clean_path(value)

    @model_validator(mode="after")
    def validate_shape(self) -> FileChangeProposal:
        """Un borrado no lleva contenido y una escritura sí.

        Raises:
            ValueError: si la combinación de operación y contenido no tiene sentido.
        """
        if self.operation in (ChangeOperation.DELETE, ChangeOperation.RENAME, ChangeOperation.MOVE):
            if self.content is not None:
                raise ValueError(f"la operación {self.operation.value} no lleva contenido")
            if self.operation is not ChangeOperation.DELETE and not self.source_path:
                raise ValueError(f"la operación {self.operation.value} exige un origen")
            if self.operation is ChangeOperation.RENAME and self.source_path == self.path:
                raise ValueError("un renombrado no puede dejar el fichero donde estaba")
            return self
        if self.source_path is not None:
            raise ValueError(f"la operación {self.operation.value} no declara origen")
        if self.content is None:
            raise ValueError(f"la operación {self.operation.value} exige contenido")
        if not self.content.strip():
            raise ValueError("el contenido no puede estar vacío")
        return self


class DevelopmentPlan(BaseModel):
    """Plan normalizado que PUNTO valida antes de permitir una sola escritura."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(default="", max_length=MAX_ITEM_CHARS * 2)
    files_to_read: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    files_to_modify: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    files_to_create: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    files_to_delete: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_PLAN_ITEMS,
        description=(
            "Rutas que el plan declara borrar o mover. Un borrado de algo que ya existía exige "
            "autorización humana; el de algo que creó este mismo ciclo es revertir."
        ),
    )
    verification_commands: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_VERIFICATION_COMMANDS,
        description="Nombres del catálogo del destino.",
    )
    risks: tuple[str, ...] = Field(default=(), max_length=MAX_RISKS)
    acceptance_mapping: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    functional_chain: tuple[FunctionalChainStep, ...] = Field(
        default=(),
        max_length=MAX_PLAN_ITEMS,
        description="Cadena funcional que el plan completa, eslabón a eslabón.",
    )

    @field_validator("files_to_read", "files_to_modify", "files_to_create", "files_to_delete")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Normaliza y deduplica rutas declaradas."""
        cleaned: list[str] = []
        for item in value:
            path = _clean_path(item)
            if path not in cleaned:
                cleaned.append(path)
        return tuple(cleaned)

    @field_validator("verification_commands", "risks", "acceptance_mapping")
    @classmethod
    def validate_texts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Acota y deduplica textos cortos."""
        cleaned: list[str] = []
        for item in value:
            text = item.strip()
            if not text:
                continue
            if len(text) > MAX_ITEM_CHARS:
                raise ValueError(f"el elemento supera {MAX_ITEM_CHARS} caracteres")
            if text not in cleaned:
                cleaned.append(text)
        return tuple(cleaned)

    def touched_paths(self) -> tuple[str, ...]:
        """Rutas que el plan declara escribir o borrar, en orden estable."""
        return tuple(
            dict.fromkeys(
                (*self.files_to_modify, *self.files_to_create, *self.files_to_delete)
            )
        )


class FunctionalChainStep(BaseModel):
    """Un eslabón de la cadena funcional que el objetivo exige completar.

    El plan no se valida solo porque «compile»: declara qué cadena funcional resuelve (fuente
    canónica → consumidores → comportamiento → pruebas → build) y con qué verificación del catálogo
    se comprueba cada eslabón. PUNTO exige que cada paso cite una verificación real del destino.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str = Field(min_length=1, max_length=MAX_ITEM_CHARS)
    description: str = Field(default="", max_length=MAX_ITEM_CHARS)
    verification: str = Field(
        min_length=1, max_length=MAX_ITEM_CHARS, description="Nombre del catálogo del destino."
    )


class ScopeExpansionRecord(BaseModel):
    """Registro de una ampliación de alcance, con la causa que la justifica.

    Es la pieza que separa **scope expansion** de **authority escalation**: sin evidencia causal y
    sin relación declarada con el objetivo original, no hay expansión posible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger: str = Field(default="evidencia de verificación", max_length=MAX_ITEM_CHARS)
    evidence: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    root_cause: str = Field(default="", max_length=MAX_ITEM_CHARS)
    new_resources: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    operations: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    relationship_to_original_objective: str = Field(default="", max_length=MAX_ITEM_CHARS)
    risk_before: str = Field(default="", max_length=40)
    risk_after: str = Field(default="", max_length=40)
    authority_decision: str = Field(default="", max_length=40)
    verification_required: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    cumulative_resources: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    status: ExpansionStatus = Field(default=ExpansionStatus.DENIED)


class PlanRevisionRecord(BaseModel):
    """Versión del plan con el delta que la produjo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_version: int = Field(ge=1)
    parent_version: int = Field(default=0, ge=0)
    reason: str = Field(default="", max_length=MAX_ITEM_CHARS)
    evidence: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    added_resources: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    removed_resources: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    changed_operations: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    risk_before: str = Field(default="", max_length=40)
    risk_after: str = Field(default="", max_length=40)
    authority_result: str = Field(default="", max_length=40)


class AuthorityDecisionRecord(BaseModel):
    """Decisión de autoridad tomada durante el ciclo, con las reglas que la sostienen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str = Field(max_length=60)
    outcome: str = Field(max_length=40)
    authority_class: str = Field(max_length=40)
    risk: str = Field(max_length=20)
    rules: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    reasons: tuple[str, ...] = Field(default=(), max_length=MAX_RISKS)
    resources: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    required_evidence: tuple[str, ...] = Field(default=(), max_length=MAX_RISKS)


class AppliedChange(BaseModel):
    """Cambio que PUNTO aplicó de verdad, con la evidencia de que quedó escrito."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    operation: ChangeOperation
    bytes_written: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    verified: bool = Field(description="True si la relectura coincide con lo escrito.")
    round_index: int = Field(
        default=0, ge=0, description="Ronda de reparación en la que se aplicó."
    )


class CommandEvidence(BaseModel):
    """Evidencia de un comando de verificación ejecutado por PUNTO."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    argv: tuple[str, ...]
    exit_code: int
    duration_ms: int = Field(ge=0)
    output_excerpt: str = Field(default="", max_length=4_000)
    truncated: bool = False
    timed_out: bool = False
    passed: bool = False


class AcceptanceEvidence(BaseModel):
    """Evidencia de aceptación de una referencia de la solicitud (AP000-OBS-02).

    Registra la precondición (dónde estaba el elemento que la solicitud mencionaba), la
    postcondición medida sobre esa misma superficie y el resultado. Es la prueba de que un criterio
    determinista se midió **contra la superficie solicitada**, no contra una implementación
    relacionada en otra parte.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sentence: str = Field(default="", max_length=300)
    intent: str = Field(default="", max_length=20)
    kind: str = Field(default="", max_length=20)
    surface: str = Field(default="", max_length=300)
    precondition: str = Field(default="", max_length=600)
    postcondition: str = Field(default="", max_length=600)
    #: ``SATISFIED`` | ``UNSATISFIED`` | ``NOT_MEASURABLE``.
    result: str = Field(default="", max_length=20)


class ClaimEvidence(BaseModel):
    """Evidencia de una afirmación factual/semántica de la solicitud (AP000-OBS-03).

    Una propiedad como «el mapa representa Honduras» o «integrado visualmente» no se demuestra con
    la presencia de algo: exige evidencia (dataset real, imagen o atestación humana). Este registro
    dice qué se afirmó, con qué evidencia y con qué resultado, incluido ``NOT_VERIFIED`` cuando no
    hay forma de demostrarlo.

    AP000-OBS-03-R1 añade **con qué capacidad**: qué capacidad efectiva exigía la demostración, si
    estaba disponible en la ruta activa, el detalle real cuando no lo estaba (transporte) y qué
    corresponde hacer. Así el motivo de un ``EVIDENCE_REQUIRED`` viaja en el resultado gobernado y
    una persona puede decidir sin adivinar.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sentence: str = Field(default="", max_length=300)
    kind: str = Field(default="", max_length=40)
    result: str = Field(default="", max_length=20)
    evidence: str = Field(default="", max_length=600)
    required: bool = True
    #: Evidencia que la solicitud exige para ese criterio (imagen o atestación humana, por ejemplo).
    evidence_required: str = Field(default="", max_length=300)
    #: Capacidad efectiva que exigía la evidencia (``VISION`` para la apariencia; vacía si no
    #: exigía ninguna).
    capability: str = Field(default="", max_length=40)
    #: True si esa capacidad estaba disponible en la ruta efectiva (o no hacía falta).
    capability_available: bool = True
    #: Detalle real de la capacidad cuando no está disponible (transporte activo y motivo).
    capability_detail: str = Field(default="", max_length=300)
    #: Qué corresponde hacer para obtener la evidencia que falta.
    remedy: str = Field(default="", max_length=300)


class CapabilityEvidence(BaseModel):
    """Capacidad efectiva que exigía una afirmación de la solicitud, comprobada antes de construir.

    AP000-OBS-03-R1: dice qué capacidad hacía falta, si la ruta activa la tiene **de verdad** (no
    solo en el catálogo), el detalle real cuando no la tiene y qué corresponde hacer. Es la prueba
    de que PUNTO comprobó la capacidad antes de asignar el trabajo, y la explicación de un
    ``EVIDENCE_REQUIRED``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(default="", max_length=40)
    capability: str = Field(default="", max_length=40)
    available: bool = True
    criterion: str = Field(default="", max_length=300)
    detail: str = Field(default="", max_length=300)
    remedy: str = Field(default="", max_length=300)


class ProviderFailoverEvidence(BaseModel):
    """Sustitución de proveedor que hubo que hacer durante el ciclo (PROVIDER FAILOVER).

    Es la constancia **persistida con la Task** de que el primario de un rol no estaba operativo,
    por qué, quién lo sustituyó y cómo acabó. No concede nada: el sustituto hizo el mismo trabajo
    del mismo rol bajo las mismas reglas, y todo lo que produjo pasó por las mismas validaciones.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str = Field(default="", max_length=40)
    primary_provider: str = Field(default="", max_length=40)
    primary_model: str = Field(default="", max_length=120)
    primary_error_kind: str = Field(default="", max_length=40)
    cause: str = Field(default="", max_length=40)
    substitute_provider: str = Field(default="", max_length=40)
    substitute_model: str = Field(default="", max_length=120)
    #: ``SUCCEEDED`` | ``FAILED`` | ``NO_COMPATIBLE_SUBSTITUTE``.
    outcome: str = Field(default="", max_length=40)
    detail: str = Field(default="", max_length=300)


class VisualShotEvidence(BaseModel):
    """Captura real entregada a VISUAL_QA: de qué URL, a qué tamaño y su huella exacta."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(default="", max_length=300)
    viewport: str = Field(default="", max_length=20)
    sha256: str = Field(default="", max_length=64)
    size_bytes: int = Field(default=0, ge=0)
    #: ``static`` | ``before`` | ``after`` (antes y después de una interacción real).
    phase: str = Field(default="static", max_length=10)


class VisualEvidenceRecord(BaseModel):
    """Evidencia visual **gobernada** de un criterio de apariencia, ligada a la Task.

    Dice qué criterio, qué capturas reales del estado renderizado (con huella), quién las evaluó
    (proveedor, modelo y transporte **efectivos**), qué veredicto dio y sobre qué cambio se tomó
    (``applied_digest``). ``request_id`` es la identidad de la Task. El veredicto es evidencia, no
    autoridad: no aprueba nada por sí mismo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(default="", max_length=64)
    claim: str = Field(default="", max_length=300)
    #: ``PASS`` | ``FAIL`` | ``UNCLEAR``.
    verdict: str = Field(default="", max_length=20)
    observation: str = Field(default="", max_length=300)
    provider: str = Field(default="", max_length=40)
    model: str = Field(default="", max_length=120)
    transport: str = Field(default="", max_length=40)
    via_failover: bool = False
    screenshots: tuple[VisualShotEvidence, ...] = Field(default=(), max_length=8)
    #: Huella de los ficheros aplicados sobre los que se tomó la captura.
    applied_digest: str = Field(default="", max_length=64)
    #: Interacción real ejecutada (``hover:<nombre>``), vacío para una captura estática.
    interaction: str = Field(default="", max_length=80)
    route: str = Field(default="", max_length=300)
    #: Elemento objetivo de la interacción (descripción determinista del elemento localizado).
    target_element: str = Field(default="", max_length=300)
    #: El navegador confirmó el elemento en ``:hover`` tras el movimiento real.
    hover_applied: bool = False
    #: El estado posterior difiere byte a byte del inicial.
    pixels_changed: bool = False
    captured_at: datetime = Field(default_factory=utc_now)


class PellInfluence(BaseModel):
    """Cómo una experiencia recuperada cambió una decisión del ciclo, con efecto observable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experience_id: str = Field(min_length=1, max_length=64)
    decision_point: str = Field(min_length=1, max_length=MAX_ITEM_CHARS)
    how_used: str = Field(min_length=1, max_length=MAX_ITEM_CHARS)
    observable_effect: str = Field(min_length=1, max_length=MAX_ITEM_CHARS)


class BlockedEvidence(BaseModel):
    """Evidencia gobernada de un bloqueo: qué lo decidió, sobre qué y qué corresponde hacer.

    La escribe **el punto de decisión** —la frontera que denegó la operación—, no la interfaz: el
    dashboard solo la muestra. ``rule``, ``resource`` y ``remedy`` quedan vacíos cuando esa frontera
    no los declara, y entonces la interfaz dice que no están registrados en vez de inventarlos.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=60)
    detail: str = Field(default="", max_length=1_000)
    #: Regla concreta que produjo el bloqueo (quién lo decidió y con qué criterio).
    rule: str = Field(default="", max_length=300)
    #: Recurso sobre el que se decidió: destino, rama, ruta o comando según el caso.
    resource: str = Field(default="", max_length=300)
    #: Qué corresponde hacer para levantar el bloqueo, según la regla que lo produjo.
    remedy: str = Field(default="", max_length=300)


class DevelopmentResult(BaseModel):
    """Desenlace del ciclo de desarrollo, con la autoridad explícita y acotada."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    status: DevelopmentStatus
    target_id: str = Field(default="", max_length=80)
    branch: str = Field(default="", max_length=120)
    plan: DevelopmentPlan | None = None
    plan_status: PlanStatus = PlanStatus.REJECTED
    plan_issues: tuple[BuildValidationIssue, ...] = ()
    change_issues: tuple[BuildValidationIssue, ...] = ()
    applied: tuple[AppliedChange, ...] = ()
    verification: tuple[CommandEvidence, ...] = ()
    repair_rounds: int = Field(default=0, ge=0)
    #: Correcciones estructurales de propuesta: inconsistencias medibles (CREATE sobre lo que
    #: existe, MODIFY sobre lo que no está, cambios contradictorios o sin efecto) que PUNTO
    #: detecta antes de aplicar y que **no** consumen una ronda funcional de reparación.
    #: Contabilidad separada de ``repair_rounds``, con su propio techo explícito.
    structural_corrections: int = Field(default=0, ge=0)
    context_requests_granted: tuple[str, ...] = ()
    context_requests_denied: tuple[str, ...] = ()
    checkpoint_id: str = Field(default="", max_length=64)
    rolled_back: bool = False
    commit_sha: str = Field(default="", max_length=64)
    pell_status: str = Field(default="DISABLED", max_length=20)
    pell_influence: tuple[PellInfluence, ...] = ()
    #: PILOT-05: alcance inicial y final, versiones del plan, sobres de riesgo, expansiones y
    #: decisiones de autoridad. Es la evidencia que hace auditable la autonomía adaptativa.
    initial_scope: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    final_scope: tuple[str, ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    plan_versions: tuple[PlanRevisionRecord, ...] = ()
    risk_envelopes: tuple[dict[str, Any], ...] = Field(default=(), max_length=MAX_PLAN_ITEMS)
    scope_expansions: tuple[ScopeExpansionRecord, ...] = ()
    authority_decisions: tuple[AuthorityDecisionRecord, ...] = ()
    #: AP000-OBS-02: evidencia de aceptación medida contra las superficies solicitadas. Si alguna
    #: referencia medible queda ``UNSATISFIED``, el ciclo **no** puede declarar VERIFIED.
    acceptance: tuple[AcceptanceEvidence, ...] = ()
    #: ``SATISFIED`` | ``FAILED`` | ``NOT_MEASURED`` (sin referencias medibles).
    acceptance_result: str = Field(default="NOT_MEASURED", max_length=20)
    #: AP000-OBS-03: afirmaciones factuales/semánticas y su evidencia. Un criterio requerido que
    #: queda ``NOT_VERIFIED`` impide declarar el desarrollo completado.
    claims: tuple[ClaimEvidence, ...] = ()
    #: ``SATISFIED`` | ``FAILED`` | ``EVIDENCE_REQUIRED`` | ``NONE``.
    claims_result: str = Field(default="NONE", max_length=20)
    #: AP000-OBS-03-R1: capacidades efectivas que exigían las afirmaciones, comprobadas al empezar.
    capabilities: tuple[CapabilityEvidence, ...] = ()
    #: PROVIDER FAILOVER: sustituciones de proveedor de este ciclo (vacío si nadie falló).
    failovers: tuple[ProviderFailoverEvidence, ...] = Field(default=(), max_length=64)
    #: Evidencia visual gobernada (capturas reales + veredicto de VISUAL_QA) de este ciclo.
    visual_evidence: tuple[VisualEvidenceRecord, ...] = Field(default=(), max_length=32)
    functional_chain_result: str = Field(default="", max_length=40)
    provider: str = Field(default="", max_length=40)
    model: str = Field(default="", max_length=120)
    duration_ms: int | None = Field(default=None, ge=0)
    error_kind: str = Field(default="", max_length=40)
    error: str = Field(default="", max_length=1_000)
    #: AP000-OBS-04: evidencia estructurada del bloqueo (código, causa, regla, recurso y acción).
    #: La frontera que deniega la escribe aquí para que la persona pueda verla desde el dashboard.
    blocked: BlockedEvidence | None = None
    authority: Literal["LOCAL_APPLY_ONLY"] = "LOCAL_APPLY_ONLY"
    published: bool = Field(
        default=False,
        description="Publicar no es una operación de este ciclo: siempre False y comprobado.",
    )
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def completed(self) -> bool:
        """True solo si los cambios quedaron aplicados y verificados."""
        return self.status is DevelopmentStatus.COMPLETED

    def as_public_dict(self) -> dict[str, object]:
        """Vista serializable, sin contexto interno ni contenido de los ficheros."""
        return {
            "request_id": str(self.request_id),
            "status": self.status.value,
            "target_id": self.target_id,
            "branch": self.branch,
            "plan_status": self.plan_status.value,
            "plan_issues": [issue.as_text() for issue in self.plan_issues],
            "change_issues": [issue.as_text() for issue in self.change_issues],
            "applied": [
                {
                    "path": change.path,
                    "operation": change.operation.value,
                    "bytes": change.bytes_written,
                    "sha256": change.sha256,
                    "verified": change.verified,
                }
                for change in self.applied
            ],
            "verification": [
                {
                    "name": evidence.name,
                    "argv": list(evidence.argv),
                    "exit_code": evidence.exit_code,
                    "passed": evidence.passed,
                    "duration_ms": evidence.duration_ms,
                    "truncated": evidence.truncated,
                }
                for evidence in self.verification
            ],
            "repair_rounds": self.repair_rounds,
            "context_requests_granted": list(self.context_requests_granted),
            "context_requests_denied": list(self.context_requests_denied),
            "checkpoint_id": self.checkpoint_id,
            "rolled_back": self.rolled_back,
            "commit_sha": self.commit_sha,
            "pell_status": self.pell_status,
            "pell_influence": [
                {
                    "experience_id": influence.experience_id,
                    "decision_point": influence.decision_point,
                    "how_used": influence.how_used,
                    "observable_effect": influence.observable_effect,
                }
                for influence in self.pell_influence
            ],
            "provider": self.provider,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "error_kind": self.error_kind,
            "error": self.error,
            "authority": self.authority,
            "published": self.published,
            "initial_scope": list(self.initial_scope),
            "final_scope": list(self.final_scope),
            "plan_versions": [item.model_dump(mode="json") for item in self.plan_versions],
            "risk_envelopes": [dict(item) for item in self.risk_envelopes],
            "scope_expansions": [
                item.model_dump(mode="json") for item in self.scope_expansions
            ],
            "authority_decisions": [
                item.model_dump(mode="json") for item in self.authority_decisions
            ],
            "functional_chain_result": self.functional_chain_result,
            "failovers": [item.model_dump(mode="json") for item in self.failovers],
            "visual_evidence": [item.model_dump(mode="json") for item in self.visual_evidence],
        }


__all__ = [
    "MAX_CONTENT_CHARS",
    "MAX_CONTEXT_REQUESTS",
    "MAX_ITEM_CHARS",
    "MAX_PATH_CHARS",
    "MAX_PLAN_ITEMS",
    "AppliedChange",
    "AuthorityDecisionRecord",
    "BlockedEvidence",
    "CapabilityEvidence",
    "ChangeOperation",
    "CommandEvidence",
    "ContextRequest",
    "DevelopmentPlan",
    "DevelopmentResult",
    "DevelopmentStatus",
    "ExpansionStatus",
    "FileChangeProposal",
    "FunctionalChainStep",
    "PellInfluence",
    "PlanRevisionRecord",
    "PlanStatus",
    "ProviderFailoverEvidence",
    "RepositoryOperation",
    "ScopeExpansionRecord",
    "VisualEvidenceRecord",
    "VisualShotEvidence",
]
