"""Ciclo de desarrollo gobernado (PILOT-04).

Convierte una solicitud de trabajo en **cambios reales y verificados** sobre un repositorio destino,
con la autoridad repartida exactamente donde el motor la tiene:

```
BuildRequest
  -> frontera: destino registrado (configuración), baseline y rama de trabajo
  -> PELL: experiencia VERIFIED recuperada ANTES de planificar (y su influencia registrada)
  -> descubrimiento: inventario acotado del repositorio dentro del alcance
  -> ARCHITECT (ProviderRouter, rol ARCHITECT): plan normalizado
  -> validación del plan: PUNTO decide si se puede empezar a escribir
  -> BUILDER (ProviderRouter, rol BUILDER): cambios estructurados + peticiones de contexto
  -> validación de cada cambio: alcance, operación autorizada, política, huella y secretos
  -> checkpoint reversible ANTES de la primera escritura
  -> aplicación: escritura verificada por relectura (una sola puerta)
  -> verificación: comandos del catálogo del destino, con la evidencia del entorno
  -> reparación autónoma (rondas acotadas) con la evidencia del fallo
  -> si no se resuelve: rollback al estado capturado
  -> aprendizaje: experiencia VERIFIED con la evidencia del ciclo
  -> commit LOCAL con las rutas del ciclo (nunca los cambios preexistentes del usuario)
  -> DevelopmentResult con authority=LOCAL_APPLY_ONLY y published=False
```

Frontera de autoridad, explícita porque es lo que esta fase demuestra: el proveedor **propone**
(plan y cambios en JSON); PUNTO valida, aplica, verifica y confirma. El proveedor no ejecuta
nada: ni un comando, ni una escritura. Las órdenes de ejecución no salen de su texto —solo
puede **nombrar** una verificación del catálogo del destino—, y ninguna propuesta puede ampliar
el alcance, saltarse la política ni publicar nada.

Lo que este ciclo **no** hace, a propósito: publicar (``published=False`` siempre), desplegar, tocar
producción, ejecutar comandos arbitrarios, instalar dependencias, escribir fuera del alcance o
confirmar cambios que no sean suyos.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from punto.acceptance import (
    CapabilityRequirement,
    ClaimRecord,
    RequestReference,
    SemanticClaim,
    VisualCapability,
    VisualVerdict,
    capability_requirements,
    claims_result,
    extract_claims,
    ground_request,
    is_interaction_claim,
    verify_acceptance,
    verify_claims,
)
from punto.audit.logger import AuditLogger
from punto.cartography import find_department_datasets, renders_from_dataset
from punto.memory.experience import ExperienceResult, ExperienceStatus
from punto.memory.retrieval import (
    MemoryRetriever,
    PriorExperienceContext,
    RetrievalOutcome,
    RetrievalStatus,
    build_memory_query,
    render_experience_block,
)
from punto.memory.store import ExperienceStore
from punto.orchestrator.focused_resolution import (
    IMPLEMENTATION_PHASE,
    RESOLUTION_PHASE,
    UNCHANGED_BY_EVIDENCE,
    FailureMap,
    ResolutionState,
    causal_progress,
    declared_unchanged,
    escalation_resources,
    failure_map,
    normalize_path,
    resolution_block,
    resource_statuses,
)
from punto.orchestrator.proposal_boundary import length_limits_text, normalize_descriptive_fields
from punto.orchestrator.proposal_preflight import (
    ProposalPreflightResult,
    correction_feedback,
    issue_codes,
    proposal_preflight,
)
from punto.policy.config_loader import ConfigLoader
from punto.policy.envelope import (
    AUTONOMOUS_MAX_FILES,
    AdaptiveAuthorityEnvelope,
    EnvelopeOperation,
    Environment,
    OperationRisk,
    Provenance,
    VerificationStrength,
)
from punto.policy.policy_engine import PolicyEngine
from punto.providers.contract import (
    FailoverOutcome,
    ProviderRequest,
    ProviderResult,
    ProviderRole,
    ProviderStatus,
)
from punto.providers.registry import ProviderRegistry
from punto.providers.router import ProviderRouter
from punto.schemas.audit import AuditEventType
from punto.schemas.build import BuildRequest, BuildValidationIssue
from punto.schemas.dev import (
    MAX_PLAN_ITEMS,
    AcceptanceEvidence,
    AppliedChange,
    AuthorityDecisionRecord,
    BlockedEvidence,
    CapabilityEvidence,
    ChangeOperation,
    ClaimEvidence,
    CommandEvidence,
    ContextRequest,
    DevelopmentPlan,
    DevelopmentResult,
    DevelopmentStatus,
    ExpansionStatus,
    FileChangeProposal,
    PellInfluence,
    PlanRevisionRecord,
    PlanStatus,
    ProviderFailoverEvidence,
    RepositoryOperation,
    ScopeExpansionRecord,
    VisualEvidenceRecord,
    VisualShotEvidence,
)
from punto.schemas.enums import AuditResult
from punto.schemas.execution import CommandResult
from punto.schemas.repair import RepairSnapshot
from punto.security.deterministic import SECRET_PATTERNS
from punto.skills import SkillActivation
from punto.tools.errors import WorkspaceNotResolvedError, WorkspaceViolationError
from punto.visualqa.dev_evidence import (
    CaptureError,
    HeadlessBrowserCapture,
    ScreenshotCapture,
    assess_visual_claims,
)
from punto.visualqa.interaction import (
    BrowserInteraction,
    InteractionEvidence,
    InteractionRunner,
    assess_interaction_claims,
)
from punto.workflow.snapshots import FileRepairSnapshots
from punto.workspace.repository import (
    GovernedRepository,
    RepositoryDenied,
    RepositoryPolicy,
    SecretBoundaryViolation,
)
from punto.workspace.target import (
    DevelopmentTarget,
    DevelopmentTargetError,
    DevelopmentTargetRegistry,
)

#: Tope de tokens de salida por invocación, fijado por PUNTO.
#:
#: Se aplica a las dos invocaciones del ciclo. El plan cabe en poco, pero los cambios **repiten el
#: contenido** de los ficheros que tocan, y un modelo con razonamiento gasta parte del presupuesto
#: en pensar: con un tope corto la respuesta llega truncada (`finish_reason='length'`) y el ciclo se
#: queda sin cambios. El valor sale del que ya usa el motor para planificar.
DEFAULT_MAX_OUTPUT_TOKENS: Final[int] = 32_000

#: Ficheros que el descubrimiento inspecciona como candidatos, y cuántos entran al contexto.
MAX_DISCOVERY_FILES: Final[int] = 400
MAX_CONTEXT_FILES: Final[int] = 8

#: AP000-OBS-02: intenciones cuya superficie tiene que estar cubierta por el plan antes de escribir.
_ACCEPTANCE_PLAN_INTENTS: Final[frozenset[str]] = frozenset(
    {"REPLACE", "DELETE", "MODIFY", "CREATE"}
)

#: Tope de problemas de aceptación que se reportan como incidencias de una ronda.
_MAX_ACCEPTANCE_ISSUES: Final[int] = 8
MAX_CONTEXT_FILE_CHARS: Final[int] = 18_000
MAX_CONTEXT_TOTAL_CHARS: Final[int] = 60_000

#: Extensiones que el descubrimiento considera código o configuración relevante.
RELEVANT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".mjs",
        ".cjs",
        ".json",
        ".md",
        ".sql",
        ".yml",
        ".yaml",
        ".css",
    }
)

#: Cercas de código que el proveedor añade a veces alrededor del JSON.
_FENCE: Final[re.Pattern[str]] = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)

#: Campos del plan que son listas de textos (se normalizan antes de validar).
_TEXT_TUPLE_FIELDS: Final[frozenset[str]] = frozenset(
    {"risks", "acceptance_mapping", "verification_commands"}
)

#: Campos del plan que son listas de rutas (una ruta suelta es una lista de una).
_PATH_TUPLE_FIELDS: Final[frozenset[str]] = frozenset(
    {"files_to_read", "files_to_modify", "files_to_create"}
)

#: Palabras vacías para el ranking determinista de candidatos.
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "and", "for", "from", "with", "que", "los", "las", "del", "una", "uno", "por",
        "para", "con", "como", "mas", "más", "sin", "sobre", "the", "add", "new", "fix",
    }
)


class DevelopmentCycleError(RuntimeError):
    """El ciclo no se puede ejecutar con lo que se le ha dado."""


class _ApplyAborted(RuntimeError):
    """La frontera denegó una escritura a mitad de la aplicación.

    Es un control de flujo interno: el bucle lo captura, deshace lo aplicado y termina el ciclo con
    su estado. Nunca sale del ciclo como excepción.
    """

    def __init__(self, *, code: str, detail: str, applied: tuple[AppliedChange, ...]) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.applied = applied


@dataclass(frozen=True, slots=True)
class DevelopmentConfig:
    """Límites del ciclo: los fija PUNTO, no la solicitud.

    ``max_files_changed`` es un **presupuesto anti-runaway**, no la frontera de autoridad: la
    autoridad la decide el sobre adaptativo por riesgo efectivo. El número sigue existiendo para que
    ninguna tarea se convierta en un barrido del repositorio y para que el ciclo tenga un techo duro
    declarado.
    """

    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    max_context_files: int = MAX_CONTEXT_FILES
    max_context_file_chars: int = MAX_CONTEXT_FILE_CHARS
    max_context_total_chars: int = MAX_CONTEXT_TOTAL_CHARS
    max_files_changed: int = AUTONOMOUS_MAX_FILES
    max_repair_rounds: int = 3
    max_context_rounds: int = 2
    #: Rondas seguidas con el mismo fallo y la misma estrategia antes de declarar estancamiento.
    stagnation_limit: int = 2
    #: Correcciones **estructurales** admitidas por ciclo: una propuesta que PUNTO puede rechazar
    #: por un hecho medible (CREATE sobre algo que existe, MODIFY sobre algo que no está, cambios
    #: contradictorios o sin efecto) se corrige sin gastar una ronda funcional de reparación. El
    #: límite es pequeño y explícito: no hay reintentos ilimitados, y ``max_repair_rounds`` no se
    #: toca.
    max_structural_corrections: int = 2
    #: Techo acumulado de recursos distintos para toda la sesión (anti-fragmentación).
    session_ceiling: int = AUTONOMOUS_MAX_FILES * 3
    #: Permitir que el BUILDER pida ampliar alcance con evidencia causal.
    allow_scope_expansion: bool = True
    #: Exigir que el plan declare la cadena funcional que completa.
    require_functional_chain: bool = True
    #: Handoff causal plan → BUILDER: transporta la parte operativa del plan ya validado
    #: (fuente, consumidores, cadena funcional y qué demuestra cada criterio). Configurable para
    #: poder reproducir el control sin él.
    causal_handoff: bool = True
    #: Skill experimental del BUILDER (``id`` o ``id@version``), declarada por el operador.
    builder_skill: str = ""
    #: Skill experimental de **resolución** (EXPERIMENTO 03): se activa **solo** cuando existe un
    #: fallo real de verificación, nunca en el prompt del ARCHITECT ni en la implementación inicial.
    #: Así la variable medida es la resolución posterior al fallo, no la primera implementación.
    resolution_skill: str = ""
    #: Skill experimental del ARCHITECT (``id`` o ``id@version``), declarada por el operador.
    #:
    #: Vacío significa el comportamiento de siempre: sin skill, las instrucciones del ARCHITECT son
    #: exactamente las de antes. La activación es **explícita** (SKILL-LAYER-0): no hay selección
    #: automática todavía.
    architect_skill: str = ""


@dataclass(frozen=True, slots=True)
class ContextFile:
    """Fichero que entra al contexto gobernado: ruta relativa, huella y contenido."""

    path: str
    sha256: str
    content: str

    @property
    def chars(self) -> int:
        """Longitud del contenido entregado."""
        return len(self.content)


@dataclass(slots=True)
class DevelopmentCycle:
    """Ciclo de desarrollo gobernado, con todas sus dependencias inyectadas."""

    router: ProviderRouter
    targets: DevelopmentTargetRegistry
    config: DevelopmentConfig = field(default_factory=DevelopmentConfig)
    retriever: MemoryRetriever | None = None
    store: ExperienceStore | None = None
    audit: AuditLogger | None = None
    policy_engine: PolicyEngine | None = None
    actor: str = "punto-dev-cycle"
    #: Captura de la aplicación renderizada para la evidencia visual (opcional). Sin ella, o sin
    #: rutas visuales en el destino, no hay captura y el criterio sigue exigiendo evidencia.
    visual_capture: ScreenshotCapture | None = None
    #: Ejecutor de interacciones reales (hover) para criterios que una captura no demuestra.
    visual_interaction: InteractionRunner | None = None
    _snapshots: FileRepairSnapshots | None = field(default=None, init=False, repr=False)
    _checkpoint: RepairSnapshot | None = field(default=None, init=False, repr=False)
    _last_provider: str = field(default="", init=False, repr=False)
    _last_model: str = field(default="", init=False, repr=False)
    _envelope: AdaptiveAuthorityEnvelope | None = field(default=None, init=False, repr=False)
    #: Evidencia de PILOT-05: autoridad, riesgo, versiones del plan y expansiones del ciclo.
    _authority_decisions: list[AuthorityDecisionRecord] = field(
        default_factory=list, init=False, repr=False
    )
    _risk_envelopes: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _plan_versions: list[PlanRevisionRecord] = field(
        default_factory=list, init=False, repr=False
    )
    _scope_expansions: list[ScopeExpansionRecord] = field(
        default_factory=list, init=False, repr=False
    )
    #: PROVIDER FAILOVER: sustituciones de proveedor de la ejecución en curso.
    _failovers: list[ProviderFailoverEvidence] = field(
        default_factory=list, init=False, repr=False
    )
    #: Evidencia visual gobernada de la ejecución en curso.
    _visual_evidence: list[VisualEvidenceRecord] = field(
        default_factory=list, init=False, repr=False
    )
    _cumulative_resources: set[str] = field(default_factory=set, init=False, repr=False)
    _created_paths: set[str] = field(default_factory=set, init=False, repr=False)
    _final_plan: DevelopmentPlan | None = field(default=None, init=False, repr=False)
    _functional_chain_result: str = field(default="", init=False, repr=False)
    _last_root_cause: str = field(default="", init=False, repr=False)
    _last_risk: str = field(default="", init=False, repr=False)
    _skill_activations: dict[str, SkillActivation] = field(
        default_factory=dict, init=False, repr=False
    )
    #: Correcciones estructurales consumidas en esta ejecución (contabilidad separada de las rondas
    #: funcionales de reparación): es evidencia de por qué una propuesta no llegó a aplicarse.
    _structural_corrections: int = field(default=0, init=False, repr=False)
    #: AP000-OBS-02: superficies localizadas antes del build (precondición de aceptación).
    _grounding: tuple[RequestReference, ...] = field(default=(), init=False, repr=False)
    #: AP000-OBS-03: afirmaciones factuales/semánticas de la solicitud y su evidencia.
    _claims: tuple[SemanticClaim, ...] = field(default=(), init=False, repr=False)
    #: Ficheros candidatos del inventario, para buscar datasets entre ellos.
    _inventory_paths: tuple[str, ...] = field(default=(), init=False, repr=False)
    #: AP000-OBS-03-R1: capacidades efectivas exigidas por las afirmaciones, comprobadas al empezar.
    _capabilities: tuple[CapabilityRequirement, ...] = field(
        default=(), init=False, repr=False
    )
    #: Atestación humana explícita (nota de un gate resuelto) que puede demostrar la apariencia.
    _attestation: str = field(default="", init=False, repr=False)

    def _reset_run_state(self) -> None:
        """Deja limpio el estado de la ejecución: el mismo ciclo puede correr dos veces."""
        self._authority_decisions.clear()
        self._risk_envelopes.clear()
        self._plan_versions.clear()
        self._scope_expansions.clear()
        self._failovers.clear()
        self._visual_evidence.clear()
        self._cumulative_resources.clear()
        self._created_paths.clear()
        self._functional_chain_result = ""
        self._final_plan = None
        self._last_root_cause = ""
        self._last_risk = ""
        self._skill_activations.clear()
        self._structural_corrections = 0
        self._snapshots = None
        self._checkpoint = None
        #: AP000-OBS-02: superficies localizadas antes del build (precondición de aceptación).
        self._grounding = ()
        #: AP000-OBS-03: afirmaciones factuales/semánticas y su evidencia.
        self._claims = ()
        self._inventory_paths = ()
        #: AP000-OBS-03-R1: capacidades efectivas que exigen esas afirmaciones, comprobadas al
        #: empezar (antes de construir) y atestación humana aportada por una persona, si la hay.
        self._capabilities = ()
        self._attestation = ""

    @property
    def envelope(self) -> AdaptiveAuthorityEnvelope:
        """Sobre de autoridad adaptativo del ciclo, construido desde la constitución.

        Las rutas constitucionales **no** se inventan aquí: se leen de ``config/constitution.yaml``
        (``protected_files`` + ``additional_protected_paths``) y de ``config/permissions.yaml``
        (``self_elevation.targets``), que son las que ya declaran qué recursos cambian las reglas
        con las que PUNTO decide su propia autoridad.
        """
        if self._envelope is None:
            loader = ConfigLoader()
            declaration = loader.load("constitution")
            permissions = loader.load("permissions")
            declared: list[str] = []
            raw_protected = declaration.get("protected_files", [])
            if isinstance(raw_protected, list):
                for entry in raw_protected:
                    if isinstance(entry, dict) and "path" in entry:
                        declared.append(str(entry["path"]))
                    elif isinstance(entry, str):
                        declared.append(entry)
            additional = declaration.get("additional_protected_paths", [])
            if isinstance(additional, list):
                declared.extend(str(item) for item in additional)
            elevation = permissions.get("self_elevation", {})
            if isinstance(elevation, dict):
                targets = elevation.get("targets", [])
                if isinstance(targets, list):
                    declared.extend(str(item) for item in targets)
            self._envelope = AdaptiveAuthorityEnvelope(
                constitutional_paths=declared,
                max_files=self.config.max_files_changed,
                session_ceiling=self.config.session_ceiling,
            )
        return self._envelope

    # ------------------------------------------------------------------ público
    def run(self, request: BuildRequest, *, human_attestation: str = "") -> DevelopmentResult:
        """Ejecuta el ciclo completo sobre el destino de la solicitud.

        Args:
            human_attestation: Atestación **humana explícita** (la nota de un Human Gate resuelto
                por una persona) que puede demostrar un criterio de apariencia cuando ninguna ruta
                automática puede producir la imagen (AP000-OBS-03-R1). Sin ella, ese criterio queda
                ``NOT_VERIFIED`` y el desenlace es el gobernado; nunca se inventa la evidencia.
        """
        started = time.perf_counter()
        self._reset_run_state()
        self._attestation = human_attestation.strip()[:600]
        target = self._target_or_none(request)
        if target is None:
            return self._blocked(
                request,
                "TARGET_NOT_REGISTERED",
                "el destino no está registrado",
                started,
                rule=(
                    "el destino de una solicitud debe estar registrado en la configuración del "
                    "motor"
                ),
                resource=request.target_repository,
                remedy=(
                    "registra el destino en PUNTO_DEV_TARGETS o en la configuración local de "
                    "destinos y vuelve a lanzar la tarea"
                ),
            )
        self._log(
            AuditEventType.BUILD_REQUEST_ACCEPTED,
            "dev_request_accepted",
            request,
            {"target_id": target.target_id, "role": request.requested_role.value},
        )
        try:
            repository = self._open_repository(target, request)
        except (
            RepositoryDenied,
            WorkspaceNotResolvedError,
            WorkspaceViolationError,
            DevelopmentTargetError,
        ) as exc:
            # La frontera denegó el trabajo: el desenlace del intento es un bloqueo gobernado, con
            # su código, su regla, su recurso y su acción. No se propaga como excepción porque el
            # ciclo **sí** se ejecutó y su desenlace es información válida (y porque la tarea no
            # puede quedarse mostrando el resultado de un intento anterior).
            return self._blocked(
                request,
                str(getattr(exc, "code", "") or "BLOCKED"),
                str(exc),
                started,
                target,
                rule=str(getattr(exc, "rule", "")),
                resource=str(getattr(exc, "resource", "")),
                remedy=str(getattr(exc, "remedy", "")),
            )
        self._log(
            AuditEventType.BUILD_REQUEST_NORMALIZED,
            "dev_request_normalized",
            request,
            {
                "baseline": repository.baseline_sha,
                "branch": repository.branch,
                "preexisting_changes": len(repository.preexisting_paths()),
                "scope_roots": len(target.scope_roots),
            },
        )

        retrieval = self._retrieve(request)
        self._log(
            AuditEventType.DEV_PELL_RETRIEVED,
            "dev_pell_retrieved",
            request,
            {
                "status": retrieval.status.value,
                "verified": len(retrieval.context.verified),
                "failed": len(retrieval.context.failed),
                "experience_ids": [item.id for item in retrieval.context.verified],
            },
        )

        inventory = self._discover(repository, target, request, retrieval)
        self._log(
            AuditEventType.DEV_REPOSITORY_DISCOVERED,
            "dev_repository_discovered",
            request,
            {
                "candidates": inventory["candidates"],
                "selected": [item.path for item in inventory["selected"]],
                "selected_chars": sum(item.chars for item in inventory["selected"]),
                "pell_ranked": inventory["pell_ranked"],
                "unranked_alternative": inventory["without_pell"],
            },
        )

        # AP000-OBS-02: grounding determinista ANTES de construir. Si la solicitud se refiere a un
        # elemento existente, se localiza su superficie y esa precondición es la evidencia contra la
        # que se medirá después. Sin esto, una implementación relacionada en otra superficie podía
        # pasar por satisfacer la solicitud.
        self._grounding = self._ground(request, target, repository, inventory)
        # AP000-OBS-03: afirmaciones factuales/semánticas de la solicitud. Se extraen aquí para
        # que la verificación sepa **qué** hay que demostrar, no solo qué superficie tocar.
        self._inventory_paths = tuple(inventory["paths"])
        self._claims = extract_claims(request.objective, request.acceptance_criteria)
        # AP000-OBS-03-R1: **antes de planificar y construir** se comprueba qué capacidad efectiva
        # exige cada afirmación. Si la ruta activa no puede producir la evidencia, se sabe desde el
        # principio y queda escrito; el criterio no se convierte en VERIFIED por ello.
        self._capabilities = self._capability_preflight(request)

        plan, plan_issues = self._plan(
            request, target, inventory["selected"], retrieval, repository
        )
        if plan is None or plan_issues:
            self._log(
                AuditEventType.DEV_PLAN_REJECTED,
                "dev_plan_rejected",
                request,
                {
                    "issue_codes": [issue.code for issue in plan_issues],
                    "issue_details": [
                        f"{issue.code}: {issue.detail[:200]}" for issue in plan_issues
                    ],
                },
                AuditResult.FAILURE,
            )
            return self._result(
                request,
                target,
                repository,
                status=DevelopmentStatus.PLAN_REJECTED,
                plan=plan,
                plan_status=PlanStatus.REJECTED,
                plan_issues=plan_issues,
                retrieval=retrieval,
                started=started,
                provider=self._last_provider,
                model=self._last_model,
                # La causa **real** del rechazo viaja con el resultado: el código y el detalle del
                # primer problema son lo que una persona necesita para decidir en el Human Gate.
                # Antes se perdían aquí (el resultado salía sin ``error_kind``, sin decisiones de
                # autoridad y sin alcance), así que el gate solo podía decir que hacía falta
                # una persona. Esa evidencia existía: ahora viaja.
                error_kind=plan_issues[0].code if plan_issues else "PLAN_REJECTED",
                error=plan_issues[0].detail if plan_issues else "",
                initial_scope=_bounded(plan.touched_paths()) if plan is not None else (),
                final_scope=_bounded(plan.touched_paths()) if plan is not None else (),
                risk_envelopes=tuple(self._risk_envelopes),
                authority_decisions=tuple(self._authority_decisions),
            )
        self._final_plan = self._final_plan or plan

        outcome = self._build_and_apply(
            request=request,
            target=target,
            repository=repository,
            plan=plan,
            retrieval=retrieval,
            inventory=inventory,
        )
        return self._result(
            request,
            target,
            repository,
            status=outcome["status"],
            plan=outcome["plan"] or plan,
            plan_status=PlanStatus.VALID,
            plan_issues=(),
            change_issues=outcome["change_issues"],
            retrieval=retrieval,
            started=started,
            applied=outcome["applied"],
            verification=outcome["verification"],
            repair_rounds=outcome["repair_rounds"],
            structural_corrections=outcome["structural_corrections"],
            granted=outcome["granted"],
            denied=outcome["denied"],
            checkpoint_id=outcome["checkpoint_id"],
            rolled_back=outcome["rolled_back"],
            commit_sha=outcome["commit_sha"],
            influence=outcome["influence"],
            error_kind=outcome["error_kind"],
            error=outcome["error"],
            provider=outcome["provider"],
            model=outcome["model"],
            initial_scope=_bounded(plan.touched_paths()),
            final_scope=_bounded((outcome["plan"] or plan).touched_paths()),
            plan_versions=tuple(outcome["plan_versions"]),
            risk_envelopes=tuple(outcome["risk_envelopes"]),
            scope_expansions=tuple(outcome["scope_expansions"]),
            authority_decisions=tuple(outcome["authority_decisions"]),
            functional_chain_result=outcome["functional_chain_result"],
            acceptance=tuple(outcome["acceptance"]),
            acceptance_result=outcome["acceptance_result"],
            claims=tuple(outcome["claims"]),
            claims_result=outcome["claims_result"],
        )

    # ------------------------------------------------------------------ destinos
    def _target_or_none(self, request: BuildRequest) -> DevelopmentTarget | None:
        """Destino registrado de la solicitud, o ``None`` si no lo está."""
        try:
            return self.targets.get(request.target_repository)
        except DevelopmentTargetError:
            return None

    def _open_repository(
        self, target: DevelopmentTarget, request: BuildRequest
    ) -> GovernedRepository:
        """Prepara el repositorio gobernado en su rama de trabajo.

        La rama se prepara **antes** de construir la frontera definitiva, porque el motor no escribe
        código sobre ``main``: la comprobación de rama de :class:`ExecutionContext` es parte de la
        autoridad, no un trámite.

        Raises:
            RepositoryDenied: si la rama real no es la declarada o el baseline no cuadra.
        """
        branch = target.work_branch or f"ai/{request.request_id}-punto-dev"
        policy = RepositoryPolicy(
            allowed_operations=target.allowed_operations,
            allowed_commands=frozenset(
                {"git", "node", "npm", "npx", "mypy", "pytest", "python", "ruff"}
            ),
            allowed_command_lines=target.command_lines(),
            scope_roots=target.scope_roots,
            max_files_changed=target.max_files_changed,
            max_read_bytes=target.max_read_bytes,
            command_timeout_seconds=target.command_timeout_seconds,
        )
        bootstrap = GovernedRepository(
            root=target.repository,
            task_id=request.request_id,
            policy=policy,
            branch=branch,
            audit=None,
            actor=self.actor,
        )
        if bootstrap.current_branch() != branch:
            bootstrap.switch_to_work_branch(branch)
        repository = GovernedRepository(
            root=target.repository,
            task_id=request.request_id,
            policy=policy,
            branch=branch,
            audit=self.audit,
            policy_engine=self.policy_engine,
            actor=self.actor,
        )
        repository.verify_work_branch()
        if repository.baseline_sha != target.baseline_sha:
            raise RepositoryDenied(
                f"el destino está en {repository.baseline_sha[:12]}… y el baseline declarado es "
                f"{target.baseline_sha[:12]}…: el ciclo no empieza sobre un árbol que no es el "
                "acordado",
                rule=(
                    "el ciclo solo empieza sobre el commit acordado del destino "
                    "(baseline_sha de su configuración confiable)"
                ),
                resource=(
                    f"rama {branch} de {target.target_id} en {repository.baseline_sha[:12]}…"
                ),
                remedy=(
                    "actualiza baseline_sha del destino al commit real de su rama de trabajo "
                    "(git rev-parse HEAD) y vuelve a lanzar la tarea"
                ),
            )
        return repository

    def _ground(
        self,
        request: BuildRequest,
        target: DevelopmentTarget,
        repository: GovernedRepository,
        inventory: Mapping[str, Any],
    ) -> tuple[RequestReference, ...]:
        """Localiza las superficies a las que se refiere la solicitud (grounding determinista).

        Se registra la precondición completa —qué superficie, qué línea y qué fragmento— para poder
        medir después si el elemento cambió donde debía. Los ficheros con secretos o fuera de
        alcance los descarta la propia lectura gobernada del repositorio.
        """

        def read_text(path: str) -> str:
            return repository.read_text(path)

        try:
            references = ground_request(
                objective=request.objective,
                criteria=request.acceptance_criteria,
                files=tuple(inventory["paths"]),
                read_text=read_text,
            )
        except Exception:
            # El grounding nunca tumba el ciclo: si no se puede localizar nada, la aceptación queda
            # «no medida» (sin obligaciones inventadas) y la tarea sigue su camino normal.
            return ()
        localizadas = [item for item in references if item.measurable]
        if localizadas:
            self._log(
                AuditEventType.DEV_ACCEPTANCE_GROUNDED,
                "dev_acceptance_grounded",
                request,
                {
                    "target_id": target.target_id,
                    "references": len(references),
                    "measurable": len(localizadas),
                    "surfaces": [
                        {
                            "surface": f"{item.path}:{item.line}",
                            "intent": reference.intent,
                            "kind": reference.kind,
                            "score": item.score,
                        }
                        for reference in localizadas
                        for item in reference.surfaces
                    ][:12],
                },
            )
        return references

    def _acceptance_preflight(
        self, plan: DevelopmentPlan, request: BuildRequest
    ) -> tuple[BuildValidationIssue, ...]:
        """Comprueba que el plan trabaja sobre la superficie que la solicitud menciona.

        Si la solicitud pide reemplazar, eliminar, modificar o crear algo que PUNTO localizó en una
        superficie concreta y el plan no toca esa superficie, el plan se rechaza **antes de
        escribir**: es la diferencia entre «implementé algo relacionado» y «resolví lo pedido».
        """
        touched = set(plan.touched_paths())
        issues: list[BuildValidationIssue] = []
        for reference in self._grounding:
            if not reference.measurable or reference.literal:
                continue
            if reference.intent not in _ACCEPTANCE_PLAN_INTENTS:
                continue
            for surface in reference.surfaces:
                if surface.path in touched:
                    continue
                issues.append(
                    BuildValidationIssue(
                        code="PLAN_MISSES_REQUESTED_SURFACE",
                        detail=(
                            f"la solicitud se refiere a un elemento existente en "
                            f"{surface.path}:{surface.line} ({reference.kind}) y el plan no lo "
                            f"toca: {reference.sentence[:120]}"
                        )[:300],
                    )
                )
                break
        if len(issues) > _MAX_ACCEPTANCE_ISSUES:
            return tuple(issues[:_MAX_ACCEPTANCE_ISSUES])
        return tuple(issues)

    def _verify_acceptance(
        self, request: BuildRequest, repository: GovernedRepository
    ) -> tuple[bool, tuple[BuildValidationIssue, ...], tuple[AcceptanceEvidence, ...]]:
        """Mide las postcondiciones contra las superficies reales, después del build.

        Devuelve si la aceptación está superada, los problemas accionables (que entran en la cadena
        de reparación igual que un fallo de verificación) y la evidencia completa.
        """
        if not any(item.measurable for item in self._grounding):
            return True, (), ()
        records = verify_acceptance(
            self._grounding,
            read_text=repository.read_text,
            changed_paths=repository.changed_paths(),
            exists=repository.exists,
        )
        evidence = tuple(
            AcceptanceEvidence(
                sentence=item.sentence,
                intent=item.intent,
                kind=item.kind,
                surface=item.surface,
                precondition=item.precondition,
                postcondition=item.postcondition,
                result=item.result,
            )
            for item in records
        )
        failed = tuple(item for item in records if item.failed)
        issues = tuple(
            BuildValidationIssue(
                code="ACCEPTANCE_NOT_SATISFIED",
                detail=(
                    f"{item.intent} {item.kind} en {item.surface or 'superficie no localizada'}: "
                    f"{item.postcondition} ({item.precondition})"
                )[:300],
            )
            for item in failed
        )
        if failed:
            self._log(
                AuditEventType.DEV_ACCEPTANCE_FAILED,
                "dev_acceptance_failed",
                request,
                {
                    "failed": len(failed),
                    "measured": len(records),
                    "surfaces": [item.surface for item in failed][:8],
                    "details": [item.postcondition[:160] for item in failed][:8],
                },
                AuditResult.FAILURE,
            )
            return False, issues, evidence
        self._log(
            AuditEventType.DEV_ACCEPTANCE_VERIFIED,
            "dev_acceptance_verified",
            request,
            {
                "measured": len(records),
                "satisfied": sum(1 for item in records if item.satisfied),
                "not_measurable": sum(1 for item in records if item.result == "NOT_MEASURABLE"),
            },
        )
        return True, (), evidence

    def _visual_capability(self) -> VisualCapability:
        """Capacidad **efectiva** de la ruta asignada a VISUAL_QA para recibir imágenes.

        No se supone: la calcula la fuente canónica de capacidades efectivas
        (``punto.providers.effective``), que interseca lo que el proveedor declara con lo que su
        **transporte activo** ejecuta de verdad. Si el transporte no acepta imágenes —Claude Code en
        modo no interactivo, por ejemplo— la respuesta es «no disponible» (fail closed), el detalle
        dice por qué y el remedio qué corresponde, incluidas las rutas autorizadas que sí podrían.
        """
        from punto.providers.effective import visual_capability_for_role

        return visual_capability_for_role(router=self.router)

    def _capability_preflight(
        self, request: BuildRequest
    ) -> tuple[CapabilityRequirement, ...]:
        """Comprueba **antes de construir** qué capacidades exigen las afirmaciones de la solicitud.

        Es la comprobación que impide exigir una verificación imposible: si un criterio de
        apariencia exige imágenes y la ruta efectiva no las acepta, el ciclo lo sabe desde el
        principio y lo deja escrito (auditoría y resultado). No cambia el desenlace por sí sola —el
        criterio se mide después con la evidencia real, y sin ella el estado es
        ``EVIDENCE_REQUIRED``—, pero hace que ese estado sea explicable desde el primer paso y no
        una sorpresa al final.
        """
        if not self._claims:
            return ()
        visual = self._visual_capability()
        requisitos = capability_requirements(self._claims, visual=visual)
        self._log(
            AuditEventType.DEV_CAPABILITY_EVALUATED,
            "dev_capability_evaluated",
            request,
            {
                "capabilities": [item.as_dict() for item in requisitos],
                "visual": visual.as_dict(),
                "missing": [
                    item.capability for item in requisitos if not item.available and item.capability
                ],
            },
            AuditResult.SUCCESS
            if all(item.available for item in requisitos)
            else AuditResult.FAILURE,
        )
        return requisitos

    @staticmethod
    def _capability_evidence(
        requirements: Sequence[CapabilityRequirement],
    ) -> tuple[CapabilityEvidence, ...]:
        """Traduce los requisitos comprobados a la evidencia del resultado."""
        return tuple(
            CapabilityEvidence(
                kind=item.kind,
                capability=item.capability,
                available=item.available,
                criterion=item.criterion,
                detail=item.detail,
                remedy=item.remedy,
            )
            for item in requirements
        )

    def _dataset_candidates(
        self, repository: GovernedRepository, target: DevelopmentTarget
    ) -> tuple[str, ...]:
        """Ficheros donde puede vivir un dataset cartográfico: los cambiados y los del repositorio.

        Se recorren los ficheros de datos del destino (bounded, sin `.git`/`node_modules`/`.next`)
        porque el dataset puede estar fuera de las raíces de alcance del código: es un activo, no
        código que el ciclo vaya a escribir.
        """
        encontrados: list[str] = []
        for candidate in sorted(target.repository.rglob("*")):
            if len(encontrados) >= MAX_DISCOVERY_FILES:
                break
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(target.repository).as_posix()
            if relative.split("/", maxsplit=1)[0] in {
                ".git",
                ".next",
                "node_modules",
                ".vercel",
                ".punto-repair-snapshots",
            }:
                continue
            if candidate.suffix.lower() in {".geojson", ".json"}:
                encontrados.append(relative)
        return tuple(dict.fromkeys((*repository.changed_paths(), *encontrados)))

    def _verify_semantic_claims(
        self, request: BuildRequest, repository: GovernedRepository, target: DevelopmentTarget
    ) -> tuple[str, tuple[BuildValidationIssue, ...], tuple[ClaimRecord, ...]]:
        """Mide las afirmaciones factuales/semánticas con la evidencia disponible.

        Devuelve el resultado global (``SATISFIED`` / ``FAILED`` / ``EVIDENCE_REQUIRED`` /
        ``NONE``), los problemas reparables (``CLAIM_NOT_SATISFIED``) y la evidencia.
        """
        if not self._claims:
            return "NONE", (), ()
        candidatos = self._dataset_candidates(repository, target)
        datasets = find_department_datasets(candidatos, repository.read_text)
        rendered = (False, "no se modificó ningún fichero que use un dataset")
        if datasets:
            rendered = renders_from_dataset(
                repository.changed_paths(), repository.read_text, datasets[0].path
            )
        visual = self._visual_capability()
        verdicts = self._visual_verdicts(request, repository, target, visual)
        registros = verify_claims(
            self._claims,
            datasets=datasets,
            rendered=rendered,
            visual=visual,
            attestation=self._attestation,
            visual_verdicts=verdicts,
        )
        resultado = claims_result(registros)
        self._log(
            AuditEventType.DEV_CLAIMS_EVALUATED,
            "dev_claims_evaluated",
            request,
            {
                "result": resultado,
                "claims": [item.kind for item in registros],
                "results": [item.result for item in registros],
                "evidence": [item.evidence[:200] for item in registros],
                "datasets": [item.as_dict() for item in datasets[:2]],
                "visual_capability": visual.as_dict(),
            },
            AuditResult.SUCCESS if resultado in {"SATISFIED", "NONE"} else AuditResult.FAILURE,
        )
        issues = tuple(
            BuildValidationIssue(
                code="CLAIM_NOT_SATISFIED",
                detail=f"{item.kind}: {item.evidence}"[:300],
            )
            for item in registros
            if item.unsatisfied and item.required
        )
        return resultado, issues, registros

    def _visual_verdicts(
        self,
        request: BuildRequest,
        repository: GovernedRepository,
        target: DevelopmentTarget,
        visual: VisualCapability,
    ) -> dict[str, VisualVerdict]:
        """Produce evidencia visual real (capturas y/o interacciones) evaluada por VISUAL_QA.

        Solo con capacidad **efectiva** de imágenes y evidencia real: una captura de la app
        renderizada para los criterios estáticos, y una interacción real (hover con antes/después)
        para los de interacción. En cualquier otro caso el criterio sigue exigiendo evidencia
        (nunca se simula). Un fallo de captura, de interacción o de evaluación tampoco es un PASS.
        La evidencia queda ligada a la Task (``request_id``) y al cambio aplicado.
        """
        claims = [claim.sentence for claim in self._claims if claim.capability == "VISION"]
        if not claims or not visual.available:
            return {}
        interactive = [text for text in claims if is_interaction_claim(text)]
        static = [text for text in claims if text not in interactive]
        digest = self._applied_digest(repository)
        verdicts: dict[str, VisualVerdict] = {}
        if static:
            verdicts.update(self._static_verdicts(request, target, static, digest))
        if interactive:
            verdicts.update(self._interaction_verdicts(request, target, interactive, digest))
        return verdicts

    @staticmethod
    def _applied_digest(repository: GovernedRepository) -> str:
        """Huella de los ficheros aplicados sobre los que se toma la evidencia."""
        return hashlib.sha256(
            "\n".join(
                f"{path}:{sha}"
                for path, sha in sorted(
                    repository.file_hashes(repository.changed_paths()).items()
                )
            ).encode("utf-8")
        ).hexdigest()

    def _static_verdicts(
        self,
        request: BuildRequest,
        target: DevelopmentTarget,
        sentences: Sequence[str],
        digest: str,
    ) -> dict[str, VisualVerdict]:
        """Criterios de apariencia estática: captura real de la app renderizada + VISUAL_QA."""
        if self.visual_capture is None or not target.visual_routes:
            return {}
        try:
            shots = self.visual_capture.capture(target.visual_routes, target.visual_viewport)
        except CaptureError as error:
            self._log(
                AuditEventType.DEV_VISUAL_CAPTURED,
                "dev_visual_captured",
                request,
                {"captured": 0, "error": str(error)[:300]},
                AuditResult.FAILURE,
            )
            return {}
        self._log(
            AuditEventType.DEV_VISUAL_CAPTURED,
            "dev_visual_captured",
            request,
            {
                "captured": len(shots),
                "urls": [shot.url for shot in shots],
                "sha256": [shot.sha256 for shot in shots],
            },
        )
        assessment = assess_visual_claims(
            self.router,
            sentences,
            shots,
            request_id=str(request.request_id),
            max_output_tokens=self.config.max_output_tokens,
        )
        return self._register_assessment(request, sentences, assessment, digest=digest)

    def _interaction_verdicts(
        self,
        request: BuildRequest,
        target: DevelopmentTarget,
        sentences: Sequence[str],
        digest: str,
    ) -> dict[str, VisualVerdict]:
        """Criterios de interacción: hover real con antes/después, evaluado por VISUAL_QA.

        Sin interacción declarada por el destino, sin ejecutor o con una interacción que no se puede
        demostrar (elemento no localizado, hover no confirmado), cada criterio queda ``UNCLEAR`` con
        el motivo determinista y **no se llama a ningún proveedor**.
        """
        if self.visual_interaction is None or not target.visual_interactions:
            reason = (
                "el destino no declara una interacción (visual.interactions) que demuestre este "
                "criterio"
                if self.visual_interaction is not None
                else "no hay ejecutor de interacciones configurado"
            )
            return self._unproven(request, sentences, reason, digest=digest)
        evidences = [
            self.visual_interaction.run(spec, target.visual_viewport)
            for spec in target.visual_interactions
        ]
        self._log(
            AuditEventType.DEV_VISUAL_CAPTURED,
            "dev_visual_interaction",
            request,
            {
                "interactions": [item.name for item in evidences],
                "routes": [item.route for item in evidences],
                "targets": [item.target for item in evidences],
                "hover_applied": [item.hover_applied for item in evidences],
                "pixels_changed": [item.pixels_changed for item in evidences],
                "errors": [item.error for item in evidences if item.error],
            },
            AuditResult.SUCCESS
            if any(item.usable for item in evidences)
            else AuditResult.FAILURE,
        )
        assessment = assess_interaction_claims(
            self.router,
            sentences,
            evidences,
            request_id=str(request.request_id),
            max_output_tokens=self.config.max_output_tokens,
        )
        return self._register_assessment(
            request, sentences, assessment, digest=digest, evidences=evidences
        )

    def _unproven(
        self,
        request: BuildRequest,
        sentences: Sequence[str],
        reason: str,
        *,
        digest: str,
    ) -> dict[str, VisualVerdict]:
        """Deja constancia de que la interacción no es demostrable: ``UNCLEAR`` sin proveedor."""
        verdicts: dict[str, VisualVerdict] = {}
        for sentence in sentences:
            verdicts[sentence] = VisualVerdict(verdict="UNCLEAR", observation=reason[:300])
            self._visual_evidence.append(
                VisualEvidenceRecord(
                    request_id=str(request.request_id),
                    claim=sentence[:300],
                    verdict="UNCLEAR",
                    observation=reason[:300],
                    applied_digest=digest,
                    interaction="hover",
                )
            )
        return verdicts

    def _register_assessment(
        self,
        request: BuildRequest,
        sentences: Sequence[str],
        assessment: Any,
        *,
        digest: str,
        evidences: Sequence[InteractionEvidence] = (),
    ) -> dict[str, VisualVerdict]:
        """Audita la evaluación y traduce sus veredictos a evidencia persistida y a verificación."""
        self._log(
            AuditEventType.DEV_VISUAL_ASSESSED,
            "dev_visual_assessed",
            request,
            {
                "performed": assessment.performed,
                "provider": assessment.provider,
                "model": assessment.model,
                "transport": assessment.transport,
                "via_failover": assessment.via_failover,
                "verdicts": [item.verdict for item in assessment.verdicts],
                "interaction": bool(evidences),
                "error": assessment.error,
            },
            AuditResult.SUCCESS if assessment.performed else AuditResult.FAILURE,
        )
        if not assessment.performed:
            if evidences:
                return self._unproven(
                    request, sentences, assessment.error or "sin evaluación", digest=digest
                )
            return {}
        evidence_shots = tuple(
            VisualShotEvidence(
                url=shot.url[:300],
                viewport=f"{shot.viewport[0]}x{shot.viewport[1]}",
                sha256=shot.sha256,
                size_bytes=len(shot.data),
                phase=shot.phase,
            )
            for shot in assessment.shots
        )
        labels = tuple(
            f"{shot.url} {shot.phase} {shot.sha256[:12]}" for shot in assessment.shots
        )
        usable = [item for item in evidences if item.usable]
        first = usable[0] if usable else None
        verdicts: dict[str, VisualVerdict] = {}
        for index, sentence in enumerate(sentences, start=1):
            found = assessment.verdict_for(index)
            if found is None:
                continue
            verdicts[sentence] = VisualVerdict(
                verdict=found.verdict,
                observation=found.observation,
                provider=assessment.provider,
                model=assessment.model,
                transport=assessment.transport,
                screenshots=labels,
            )
            self._visual_evidence.append(
                VisualEvidenceRecord(
                    request_id=str(request.request_id),
                    claim=sentence[:300],
                    verdict=found.verdict,
                    observation=found.observation,
                    provider=assessment.provider,
                    model=assessment.model,
                    transport=assessment.transport,
                    via_failover=assessment.via_failover,
                    screenshots=evidence_shots,
                    applied_digest=digest,
                    interaction=f"hover:{first.name}" if first is not None else "",
                    route=first.route[:300] if first is not None else "",
                    target_element=first.target[:300] if first is not None else "",
                    hover_applied=any(item.hover_applied for item in usable),
                    pixels_changed=any(item.pixels_changed for item in usable),
                )
            )
        return verdicts

    @staticmethod
    def _claim_evidence(records: Sequence[ClaimRecord]) -> tuple[ClaimEvidence, ...]:
        """Traduce los registros de afirmaciones a la evidencia del resultado."""
        return tuple(
            ClaimEvidence(
                sentence=item.sentence,
                kind=item.kind,
                result=item.result,
                evidence=item.evidence,
                required=item.required,
                evidence_required=item.evidence_required[:300],
                capability=item.capability[:40],
                capability_available=item.capability_available,
                capability_detail=item.capability_detail[:300],
                remedy=item.remedy[:300],
            )
            for item in records
        )

    @staticmethod
    def _acceptance_result(evidence: Sequence[AcceptanceEvidence]) -> str:
        """Resultado global de aceptación a partir de la evidencia medida."""
        if not evidence or all(item.result == "NOT_MEASURABLE" for item in evidence):
            return "NOT_MEASURED"
        if any(item.result == "UNSATISFIED" for item in evidence):
            return "FAILED"
        return "SATISFIED"

    # -------------------------------------------------------------------- PELL
    def _retrieve(self, request: BuildRequest) -> RetrievalOutcome:
        """Recupera experiencia previa **antes** de planificar."""
        if self.retriever is None:
            return RetrievalOutcome(
                context=PriorExperienceContext(),
                status=RetrievalStatus.DISABLED,
                detail="sin recuperador configurado",
            )
        query = build_memory_query(
            objective=request.objective,
            action=request.requested_role.value,
            files=request.scope_paths,
            context=request.context,
        )
        return self.retriever.retrieve(query)

    @staticmethod
    def _pell_tokens(retrieval: RetrievalOutcome) -> tuple[str, ...]:
        """Tokens que la experiencia recuperada aporta para ordenar el descubrimiento."""
        tokens: list[str] = []
        for experience in retrieval.context.verified:
            text = " ".join(
                [experience.problem, experience.solution, *experience.tags, *experience.procedure]
            )
            for raw in re.findall(r"[a-z0-9_./-]{3,}", text.casefold()):
                if raw not in _STOPWORDS and raw not in tokens:
                    tokens.append(raw)
        return tuple(tokens[:60])

    # ------------------------------------------------------------ descubrimiento
    def _discover(
        self,
        repository: GovernedRepository,
        target: DevelopmentTarget,
        request: BuildRequest,
        retrieval: RetrievalOutcome,
    ) -> dict[str, Any]:
        """Inventario acotado del repositorio, ordenado con la experiencia recuperada."""
        candidates: list[str] = []
        for candidate in sorted(target.repository.rglob("*")):
            if len(candidates) >= MAX_DISCOVERY_FILES:
                break
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(target.repository).as_posix()
            if relative.split("/", maxsplit=1)[0] in {".git", ".next", "node_modules", ".vercel"}:
                continue
            if candidate.suffix.lower() not in RELEVANT_SUFFIXES:
                continue
            if target.scope_roots and not any(
                relative == root or relative.startswith(f"{root}/") for root in target.scope_roots
            ):
                continue
            candidates.append(relative)

        objective_tokens = self._tokens(request.objective)
        pell_tokens = self._pell_tokens(retrieval)
        ranked_pell = self._rank(candidates, (*objective_tokens, *pell_tokens))
        ranked_plain = self._rank(candidates, objective_tokens)

        selected: list[ContextFile] = []
        budget = self.config.max_context_total_chars
        for relative_path in ranked_pell[: self.config.max_context_files]:
            try:
                content = repository.read_text(relative_path)
            except RepositoryDenied:
                continue
            trimmed = content[: self.config.max_context_file_chars]
            if len(trimmed) > budget:
                break
            budget -= len(trimmed)
            selected.append(
                ContextFile(
                    path=relative_path,
                    sha256=repository.sha256(relative_path),
                    content=trimmed,
                )
            )
        return {
            "candidates": len(candidates),
            "paths": tuple(candidates),
            "selected": tuple(selected),
            "pell_ranked": tuple(ranked_pell[: self.config.max_context_files]),
            "without_pell": tuple(ranked_plain[: self.config.max_context_files]),
        }

    @staticmethod
    def _tokens(text: str) -> tuple[str, ...]:
        """Tokens significativos de un texto libre."""
        found: list[str] = []
        for raw in re.findall(r"[a-z0-9_./-]{3,}", text.casefold()):
            if raw in _STOPWORDS or raw in found:
                continue
            found.append(raw)
        return tuple(found)

    @classmethod
    def _rank(cls, candidates: Sequence[str], hints: Sequence[str]) -> list[str]:
        """Ordena candidatos por solapamiento de tokens, con orden determinista."""
        scored: list[tuple[int, str]] = []
        for path in candidates:
            lowered = path.casefold()
            score = sum(1 for hint in hints if hint and hint in lowered)
            scored.append((-score, path))
        scored.sort()
        return [path for _, path in scored]

    # -------------------------------------------------------------------- plan
    def _plan(
        self,
        request: BuildRequest,
        target: DevelopmentTarget,
        context_files: Sequence[ContextFile],
        retrieval: RetrievalOutcome,
        repository: GovernedRepository,
    ) -> tuple[DevelopmentPlan | None, tuple[BuildValidationIssue, ...]]:
        """Pide el plan al ARCHITECT y lo valida con las reglas de PUNTO.

        Se pide **una vez** y, si la respuesta no es un plan interpretable, se vuelve a pedir una
        sola vez explicando por qué se rechazó: un transporte puede ignorar el JSON Schema, así que
        el contrato también va escrito en el prompt, y una segunda respuesta con el motivo del
        rechazo delante es más útil que rendirse. El límite es una reintención, no un bucle.

        La validación incluye el **sobre agregado** del plan contra el catálogo de autoridad del
        motor: si el plan no cabe en la autoridad autónoma, se rechaza aquí, antes de escribir un
        solo fichero, en vez de descubrirlo al confirmar con el trabajo ya hecho.
        """
        issues: tuple[BuildValidationIssue, ...] = ()
        plan: DevelopmentPlan | None = None
        created_logged = False
        for attempt in range(2):
            prompt = self._plan_prompt(
                request, target, context_files, retrieval, rejection=issues, attempt=attempt
            )
            result = self._invoke(ProviderRole.ARCHITECT, request, prompt, PLAN_SCHEMA)
            if result.status is not ProviderStatus.SUCCESS:
                issues = (
                    BuildValidationIssue(
                        code="ARCHITECT_UNAVAILABLE",
                        detail=f"el ARCHITECT no respondió: {result.error or result.status.value}",
                    ),
                )
                return None, issues
            payload = _json_object(result.content)
            if payload is None:
                issues = (
                    BuildValidationIssue(
                        code="PLAN_NOT_JSON",
                        detail="el ARCHITECT no devolvió un plan interpretable",
                    ),
                )
                continue
            try:
                plan = DevelopmentPlan.model_validate(_plan_payload(payload))
            except Exception as exc:  # el contrato del plan es la primera validación
                issues = (BuildValidationIssue(code="PLAN_INVALID", detail=str(exc)[:300]),)
                continue
            if not created_logged:
                self._log(
                    AuditEventType.DEV_PLAN_CREATED,
                    "dev_plan_created",
                    request,
                    {
                        "touched": list(plan.touched_paths()),
                        "verification": list(plan.verification_commands),
                    },
                )
                created_logged = True
            issues = self._validate_plan(plan, target, request)
            issues = (*issues, *self._authority_review(plan, repository, request, phase="plan"))
            # AP000-OBS-02: el plan tiene que trabajar sobre la superficie que la solicitud
            # menciona, no solo sobre una relacionada. Se comprueba antes de escribir nada.
            issues = (*issues, *self._acceptance_preflight(plan, request))
            if not issues:
                self._log(
                    AuditEventType.DEV_PLAN_VALIDATED,
                    "dev_plan_validated",
                    request,
                    {
                        "touched": len(plan.touched_paths()),
                        "risks": len(plan.risks),
                        "functional_chain": len(plan.functional_chain),
                    },
                )
                self._final_plan = plan
                self._cumulative_resources.update(plan.touched_paths())
                self._plan_versions.append(
                    PlanRevisionRecord(
                        plan_version=1,
                        parent_version=0,
                        reason="plan inicial validado por PUNTO",
                        evidence=tuple(plan.acceptance_mapping),
                        added_resources=plan.touched_paths(),
                        risk_before="LOW",
                        risk_after=(
                            self._authority_decisions[-1].risk
                            if self._authority_decisions
                            else ""
                        ),
                        authority_result=(
                            self._authority_decisions[-1].outcome
                            if self._authority_decisions
                            else ""
                        ),
                    )
                )
                return plan, ()

        return plan, issues

    def _validate_plan(
        self, plan: DevelopmentPlan, target: DevelopmentTarget, request: BuildRequest
    ) -> tuple[BuildValidationIssue, ...]:
        """Reglas de PUNTO sobre el plan, antes de permitir una sola escritura."""
        issues: list[BuildValidationIssue] = []
        touched = plan.touched_paths()
        if not touched:
            issues.append(
                BuildValidationIssue(
                    code="PLAN_EMPTY", detail="el plan no declara ningún fichero que tocar"
                )
            )
        if len(touched) > min(self.config.max_files_changed, target.max_files_changed):
            issues.append(
                BuildValidationIssue(
                    code="PLAN_TOO_LARGE",
                    detail=f"el plan toca {len(touched)} ficheros y el tope es "
                    f"{min(self.config.max_files_changed, target.max_files_changed)}",
                )
            )
        known = set(target.command_names())
        for name in plan.verification_commands:
            if name not in known:
                issues.append(
                    BuildValidationIssue(
                        code="PLAN_UNKNOWN_VERIFICATION",
                        detail=f"la verificación {name!r} no está en el catálogo del destino",
                    )
                )
        for step in plan.functional_chain:
            if step.verification not in known:
                issues.append(
                    BuildValidationIssue(
                        code="PLAN_UNKNOWN_CHAIN_VERIFICATION",
                        detail=(
                            f"el eslabón {step.step!r} cita la verificación "
                            f"{step.verification!r}, que no está en el catálogo del destino"
                        ),
                    )
                )
        if not plan.verification_commands:
            issues.append(
                BuildValidationIssue(
                    code="PLAN_WITHOUT_VERIFICATION",
                    detail="un plan que no declara cómo se verifica no se aplica",
                )
            )
        plan_text = (
            plan.summary,
            *plan.risks,
            *plan.acceptance_mapping,
            *(step.description for step in plan.functional_chain),
        )
        if any(
            pattern.search(value)
            for value in plan_text
            for _name, pattern, _severity in SECRET_PATTERNS
        ):
            issues.append(
                BuildValidationIssue(
                    code="PLAN_SECRET_TEXT",
                    detail=(
                        "el texto del plan contiene algo con forma de credencial: el plan viaja al "
                        "BUILDER en el handoff causal, así que no cruza esta frontera"
                    ),
                )
            )
        if request.acceptance_criteria and not plan.acceptance_mapping:
            issues.append(
                BuildValidationIssue(
                    code="PLAN_WITHOUT_ACCEPTANCE",
                    detail="el plan no mapea ningún criterio de aceptación de la solicitud",
                )
            )
        if self.config.require_functional_chain and len(touched) >= 2 and not plan.functional_chain:
            issues.append(
                BuildValidationIssue(
                    code="PLAN_WITHOUT_FUNCTIONAL_CHAIN",
                    detail=(
                        "un plan que toca varios recursos debe declarar la cadena funcional que "
                        "completa (fuente canónica → consumidores → comportamiento → verificación)"
                    ),
                )
            )
        for path in (*plan.files_to_read, *touched):
            relative = path.replace("\\", "/")
            if target.scope_roots and not any(
                relative == root or relative.startswith(f"{root}/") for root in target.scope_roots
            ):
                issues.append(
                    BuildValidationIssue(
                        code="PLAN_OUT_OF_SCOPE", detail=f"{path!r} queda fuera del alcance"
                    )
                )
        return tuple(issues)

    def _envelope_issue(
        self, plan: DevelopmentPlan, repository: GovernedRepository
    ) -> BuildValidationIssue | None:
        """Comprueba el sobre agregado del plan contra la autoridad del motor.

        El ciclo escribe fichero a fichero, y cada escritura cabe por separado en la autoridad
        autónoma. La operación que **agrega** el trabajo es la confirmación (``COMMIT``), así que
        es ella la que se evalúa con el inventario completo del plan antes de tocar nada: el techo
        de archivos por nivel de autoridad es un techo duro, y un plan que no cabe se rechaza en la
        validación en vez de dejar el trabajo aplicado y sin poder confirmar.

        Returns:
            La incidencia si el catálogo de autoridad no admite el plan; ``None`` si cabe.
        """
        touched = plan.touched_paths()
        if not touched:
            return None
        try:
            repository.authorize(
                RepositoryOperation.COMMIT,
                paths=touched,
                description="validación del sobre agregado del plan antes de aplicar",
            )
        except RepositoryDenied as exc:
            reason = getattr(exc, "reason", "") or exc.detail
            return BuildValidationIssue(
                code="PLAN_OUTSIDE_AUTHORITY",
                detail=(
                    f"el plan toca {len(touched)} fichero(s) y la autoridad constitucional no "
                    f"alcanza para confirmarlos: {reason}"
                )[:300],
            )
        return None

    # --------------------------------------------------------- autoridad adaptativa
    def _plan_profile(
        self,
        plan: DevelopmentPlan,
        *,
        operation: EnvelopeOperation = EnvelopeOperation.PLAN_APPLY,
        resources: Sequence[str] | None = None,
    ) -> OperationRisk:
        """Perfil de riesgo del plan, con atributos que fija PUNTO a partir del plan validado.

        El proveedor no declara ninguno de estos atributos: el riesgo, la procedencia y la fuerza de
        la verificación los calcula el ciclo. Es lo que impide una escalada dirigida por prompt.
        """
        steps = len(plan.verification_commands)
        strength = (
            VerificationStrength.STRONG
            if steps >= 3
            else VerificationStrength.MODERATE
            if steps
            else VerificationStrength.NONE
        )
        return OperationRisk(
            operation=operation,
            resources=tuple(resources) if resources is not None else plan.touched_paths(),
            environment=Environment.LOCAL,
            reversible=True,
            verification_strength=strength,
            provenance=Provenance.PUNTO_POLICY,
            evidence=tuple(plan.acceptance_mapping) or (plan.summary,),
            description=plan.summary,
        )

    def _change_profile(
        self, proposal: FileChangeProposal, *, created_by_cycle: bool
    ) -> OperationRisk:
        """Perfil de riesgo de un cambio concreto, deducido de su operación y su ruta."""
        operation = {
            ChangeOperation.CREATE: EnvelopeOperation.CREATE,
            ChangeOperation.MODIFY: EnvelopeOperation.WRITE,
            ChangeOperation.DELETE: EnvelopeOperation.DELETE,
            ChangeOperation.RENAME: EnvelopeOperation.RENAME,
            ChangeOperation.MOVE: EnvelopeOperation.MOVE,
        }[proposal.operation]
        evidence = tuple(
            item for item in (proposal.reason, proposal.acceptance_criterion) if item
        )
        return OperationRisk(
            operation=operation,
            resources=tuple(
                item for item in (proposal.source_path, proposal.path) if item
            ),
            environment=Environment.LOCAL,
            reversible=True,
            verification_strength=VerificationStrength.MODERATE,
            destructive=proposal.operation is ChangeOperation.DELETE,
            created_by_cycle=created_by_cycle,
            provenance=Provenance.EVIDENCE if evidence else Provenance.PUNTO_POLICY,
            evidence=evidence,
            description=proposal.reason,
        )

    def _record_decision(
        self, decision: Any, request: BuildRequest, *, phase: str
    ) -> None:
        """Registra una decisión de autoridad en el resultado y en la auditoría."""
        profile: OperationRisk | None = decision.profile
        # Regla de esta frontera: **todo campo de evidencia acotado por esquema recibe una secuencia
        # acotada**. Una decisión sobre muchos recursos no puede reventar el registro con un
        # ``ValidationError``: la decisión y sus totales viajan en la auditoría, y aquí se acota lo
        # que el esquema admite (mismo defecto que en ``ScopeExpansionRecord``).
        record = AuthorityDecisionRecord(
            operation=profile.operation.value if profile is not None else "",
            outcome=decision.outcome.value,
            authority_class=decision.authority_class.value,
            risk=decision.risk.name,
            rules=_bounded(decision.rule_names),
            reasons=_bounded(decision.reasons),
            resources=_bounded(profile.resources if profile is not None else ()),
            required_evidence=_bounded(decision.required_evidence),
        )
        self._authority_decisions.append(record)
        if len(self._risk_envelopes) < 40:
            self._risk_envelopes.append({"phase": phase, **decision.as_dict()})
        self._log(
            AuditEventType.DEV_RISK_EVALUATED,
            "dev_risk_evaluated",
            request,
            {
                "phase": phase,
                "outcome": decision.outcome.value,
                "authority_class": decision.authority_class.value,
                "risk": decision.risk.name,
                "rules": list(decision.rule_names),
                "blast_radius": profile.blast_radius if profile is not None else 0,
                "resources_total": len(profile.resources) if profile is not None else 0,
                "required_evidence": list(decision.required_evidence),
            },
        )

    def _authority_review(
        self,
        plan: DevelopmentPlan,
        repository: GovernedRepository,
        request: BuildRequest,
        *,
        phase: str = "plan",
    ) -> tuple[BuildValidationIssue, ...]:
        """Revisa el plan completo contra el sobre adaptativo **y** contra la constitución.

        Se toman los dos veredictos y manda el más restrictivo: el sobre adaptativo añade reglas de
        riesgo, pero **nunca** relaja lo que el PolicyEngine deniega. El techo de archivos sigue
        existiendo como presupuesto anti-runaway, no como frontera de autoridad.
        """
        issues: list[BuildValidationIssue] = []
        decision = self.envelope.assess(self._plan_profile(plan))
        self._record_decision(decision, request, phase=phase)
        self._last_risk = decision.risk.name
        constitutional = self._envelope_issue(plan, repository)
        if constitutional is not None:
            issues.append(constitutional)
        if not decision.autonomous:
            code = (
                "PLAN_OUTSIDE_AUTHORITY"
                if decision.prohibited
                else "PLAN_REQUIRES_HUMAN"
            )
            issues.append(
                BuildValidationIssue(
                    code=code,
                    detail=(
                        f"el plan toca {len(plan.touched_paths())} recurso(s) y el sobre de "
                        f"autoridad devuelve {decision.outcome.value} "
                        f"({decision.authority_class.value}, riesgo {decision.risk.name}): "
                        + "; ".join(decision.reasons)[:200]
                    )[:300],
                )
            )
        return tuple(issues)

    def _handle_scope_expansion(
        self,
        *,
        request: BuildRequest,
        plan: DevelopmentPlan,
        repository: GovernedRepository,
        payload: Mapping[str, Any],
        round_index: int,
    ) -> tuple[DevelopmentPlan, str]:
        """Evalúa una ampliación de alcance pedida por el BUILDER con evidencia causal.

        La evidencia la produce una verificación real, no el proveedor: sin evidencia y sin relación
        declarada con el objetivo, la expansión se rechaza. Con ellas, el sobre compara el riesgo
        **acumulado** antes y después y decide: misma clase de riesgo ⇒ autónoma (plan v2);
        frontera protegida o techo de sesión ⇒ Human Gate; recurso constitucional ⇒ denegada.

        Returns:
            El plan vigente (v2 si se aprobó) y el desenlace: ``APPROVED``, ``DENIED`` o
            ``HUMAN_GATE``.
        """
        trigger = str(payload.get("trigger") or "evidencia de verificación")[:300]
        root_cause = str(payload.get("root_cause") or "")[:300]
        relationship = str(payload.get("relationship") or "")[:300]
        evidence = _text_tuple(payload.get("evidence"))
        requested = _text_tuple(payload.get("resources"))
        operations = _text_tuple(payload.get("operations")) or ("write",)
        self._log(
            AuditEventType.DEV_SCOPE_EXPANSION_REQUESTED,
            "dev_scope_expansion_requested",
            request,
            {
                "round": round_index,
                "trigger": trigger,
                "resources": list(requested),
                "evidence": list(evidence),
                "root_cause": root_cause,
            },
            AuditResult.FAILURE,
        )
        if not requested:
            self._log(
                AuditEventType.DEV_SCOPE_EXPANSION_DENIED,
                "dev_scope_expansion_denied",
                request,
                {"reason": "la petición no declara recursos nuevos"},
                AuditResult.FAILURE,
            )
            return plan, "DENIED"

        additions: dict[str, list[str]] = {"files_to_modify": [], "files_to_create": []}
        for item in requested:
            cleaned = item.replace("\\", "/")
            if cleaned in additions["files_to_create"] or cleaned in additions["files_to_modify"]:
                continue
            bucket = "files_to_create" if not repository.exists(cleaned) else "files_to_modify"
            additions[bucket].append(cleaned)
        requested_resources = (*plan.touched_paths(), *requested)
        profile = self._plan_profile(
            plan,
            operation=EnvelopeOperation.SCOPE_EXPANSION,
            resources=requested_resources,
        )
        decision = self.envelope.expansion(
            self._plan_profile(plan),
            profile,
            evidence=evidence,
            relationship=relationship,
            trigger=trigger,
            root_cause=root_cause,
            cumulative_resources=sorted(self._cumulative_resources),
        )
        status = (
            ExpansionStatus.AUTO_APPROVED
            if decision.approved
            else ExpansionStatus.HUMAN_GATE
            if decision.outcome.value == "REQUIRE_HUMAN"
            else ExpansionStatus.DENIED
        )
        # Los campos de evidencia del registro están acotados por esquema (``MAX_PLAN_ITEMS``). Una
        # petición grande no puede reventar la construcción del registro: se acota lo que se guarda
        # y **los totales reales viajan en el evento de auditoría**, que es donde vive la decisión.
        # El desenlace sigue siendo el que decidió el sobre (DENIED / HUMAN_GATE), no una excepción.
        acumulados = tuple(
            str(item) for item in decision.record.get("cumulative_resources", ())
        )
        declarados = payload.get("resources")
        total_declarados = len(declarados) if isinstance(declarados, list) else len(requested)
        total_pedidos = len(requested)
        total_acumulados = len(acumulados)
        record = ScopeExpansionRecord(
            trigger=trigger,
            evidence=evidence[:MAX_PLAN_ITEMS],
            root_cause=root_cause,
            new_resources=requested[:MAX_PLAN_ITEMS],
            operations=operations[:MAX_PLAN_ITEMS],
            relationship_to_original_objective=relationship,
            risk_before=decision.delta.previous_risk.name,
            risk_after=decision.delta.new_risk.name,
            authority_decision=decision.outcome.value,
            verification_required=tuple(
                str(item) for item in decision.record.get("verification_required", ())
            )[:MAX_PLAN_ITEMS],
            cumulative_resources=acumulados[:MAX_PLAN_ITEMS],
            status=status,
        )
        self._scope_expansions.append(record)
        self._record_decision(decision.decision, request, phase="scope_expansion")
        if not decision.approved:
            self._log(
                AuditEventType.DEV_SCOPE_EXPANSION_DENIED,
                "dev_scope_expansion_denied",
                request,
                {
                    "resources": list(requested),
                    "requested_declared": total_declarados,
                    "requested_total": total_pedidos,
                    "cumulative_total": total_acumulados,
                    "outcome": decision.outcome.value,
                    "reasons": list(decision.reasons),
                },
                AuditResult.FAILURE,
            )
            return plan, "HUMAN_GATE" if status is ExpansionStatus.HUMAN_GATE else "DENIED"

        revised = plan.model_copy(
            update={
                "files_to_create": (*plan.files_to_create, *additions["files_to_create"]),
                "files_to_modify": (*plan.files_to_modify, *additions["files_to_modify"]),
            }
        )
        version = len(self._plan_versions) + 1
        self._plan_versions.append(
            PlanRevisionRecord(
                plan_version=version,
                parent_version=version - 1,
                reason=trigger,
                evidence=evidence,
                added_resources=tuple(requested),
                removed_resources=(),
                changed_operations=operations,
                risk_before=decision.delta.previous_risk.name,
                risk_after=decision.delta.new_risk.name,
                authority_result=decision.outcome.value,
            )
        )
        self._cumulative_resources.update(requested)
        self._final_plan = revised
        self._log(
            AuditEventType.DEV_SCOPE_EXPANSION_APPROVED,
            "dev_scope_expansion_approved",
            request,
            {
                "plan_version": version,
                "resources": list(requested),
                "requested_declared": total_declarados,
                "requested_total": total_pedidos,
                "cumulative_total": total_acumulados,
                "risk_before": decision.delta.previous_risk.name,
                "risk_after": decision.delta.new_risk.name,
                "relationship": relationship,
            },
        )
        self._log(
            AuditEventType.DEV_PLAN_REVISED,
            "dev_plan_revised",
            request,
            {
                "plan_version": version,
                "parent_version": version - 1,
                "added_resources": list(requested),
                "touched": len(revised.touched_paths()),
            },
        )
        return revised, "APPROVED"

    def _verify_functional_chain(
        self,
        plan: DevelopmentPlan,
        verification: Sequence[CommandEvidence],
        request: BuildRequest,
    ) -> tuple[bool, tuple[BuildValidationIssue, ...]]:
        """Comprueba que cada eslabón de la cadena funcional quedó verificado de verdad."""
        if not plan.functional_chain:
            self._functional_chain_result = "NOT_DECLARED"
            return True, ()
        passed = {item.name for item in verification if item.passed}
        pending = [step for step in plan.functional_chain if step.verification not in passed]
        if pending:
            issues = tuple(
                BuildValidationIssue(
                    code="FUNCTIONAL_CHAIN_STEP_UNVERIFIED",
                    detail=(
                        f"el eslabón {step.step!r} depende de la verificación "
                        f"{step.verification!r}, que no pasó"
                    ),
                )
                for step in pending
            )
            self._functional_chain_result = "FAILED"
            return False, issues
        self._functional_chain_result = "VERIFIED"
        self._log(
            AuditEventType.DEV_FUNCTIONAL_CHAIN_VERIFIED,
            "dev_functional_chain_verified",
            request,
            {
                "steps": [step.step for step in plan.functional_chain],
                "verifications": sorted({step.verification for step in plan.functional_chain}),
            },
        )
        return True, ()

    # ------------------------------------------------------------ build + apply
    def _preflight_proposal(
        self,
        proposals: Sequence[FileChangeProposal],
        repository: GovernedRepository,
    ) -> ProposalPreflightResult | None:
        """Preflight estructural de la propuesta contra el estado real del workspace.

        Devuelve ``None`` si el estado no se puede leer: entonces sigue el camino de siempre, que ya
        reporta el motivo con su propio código. No aplica, no escribe y no concede nada.
        """
        try:
            return proposal_preflight(
                proposals, exists=repository.exists, read_text=repository.read_text
            )
        except (RepositoryDenied, SecretBoundaryViolation):
            return None

    def _record_resolution_progress(
        self,
        *,
        request: BuildRequest,
        rounds: int,
        state: ResolutionState,
        failure: FailureMap | None,
        plan: DevelopmentPlan,
        payload: Mapping[str, Any],
        touched_now: tuple[str, ...],
        strategy: tuple[str, ...],
        signature: str,
        previous_signature: str,
        still_failing: tuple[str, ...],
        escalated: tuple[str, ...] = (),
        passed: bool = False,
    ) -> None:
        """Registra qué hizo una ronda con el fallo: sin esto, «cambiar el parche» parece progreso.

        La ronda de implementación inicial no es una reparación, pero sus recursos cuentan como ya
        tocados: la brecha causal se mide contra **todo** lo intentado antes, no contra la ronda
        inmediatamente anterior. Si una reparación repite el mismo fallo sin abordar, explicar ni
        escalar ningún recurso relevante nuevo, se registra ``CAUSAL_STAGNATION`` y la ronda
        siguiente lo recibe por escrito: no se repite la estrategia en silencio.
        """
        if failure is None:
            state.touched.update(touched_now)
            return
        statuses = resource_statuses(
            resources=failure.paths,
            touched=touched_now,
            declared=dict(declared_unchanged(payload)),
            authorized=plan.touched_paths(),
            escalated=escalated,
        )
        if rounds == 0:
            state.touched.update(touched_now)
            return
        explained_now = {
            item.path: item.evidence
            for item in statuses
            if item.status == UNCHANGED_BY_EVIDENCE
        }
        record = causal_progress(
            round_index=rounds,
            failure_signature=signature,
            previous_failure_signature=previous_signature,
            strategy=strategy,
            touched=touched_now,
            failure_resources=failure.paths,
            previously_touched=state.touched,
            previously_explained=state.explained,
            explained_now=explained_now,
            still_failing=still_failing,
            escalated=escalated,
        )
        state.record(record, explained=explained_now)
        self._log(
            AuditEventType.DEV_CAUSAL_PROGRESS,
            "dev_causal_progress",
            request,
            {
                **record.as_dict(),
                # Lista plana y legible: los metadatos de auditoría se congelan, no se anidan.
                "resources_status": [
                    f"{item.path}={item.status}" for item in statuses
                ],
                "verification_result": "PASSED" if passed else "FAILED",
            },
            AuditResult.SUCCESS if passed else AuditResult.FAILURE,
        )
        if record.causal_stagnation:
            self._log(
                AuditEventType.DEV_CAUSAL_STAGNATION,
                "dev_causal_stagnation",
                request,
                {
                    "round": rounds,
                    "failure_signature": signature,
                    "strategy_signature": record.strategy_signature,
                    "causal_gap": list(record.causal_gap),
                    "repeated_failure_resources": list(record.repeated_failure_resources),
                },
                AuditResult.FAILURE,
            )

    def _build_and_apply(
        self,
        *,
        request: BuildRequest,
        target: DevelopmentTarget,
        repository: GovernedRepository,
        plan: DevelopmentPlan,
        retrieval: RetrievalOutcome,
        inventory: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Pide los cambios, los valida, crea el checkpoint, aplica y verifica."""
        influence: list[PellInfluence] = []
        pell_block = render_experience_block(retrieval.context)
        if retrieval.context.verified:
            influence.append(
                PellInfluence(
                    experience_id=retrieval.context.verified[0].id,
                    decision_point="selección de contexto del repositorio",
                    how_used="los tokens de la experiencia recuperada ordenan los candidatos",
                    observable_effect=(
                        "contexto elegido: "
                        + ", ".join(inventory["pell_ranked"][:3])
                        + " | sin la experiencia habría sido: "
                        + ", ".join(inventory["without_pell"][:3])
                    ),
                )
            )

        if self.config.causal_handoff:
            handoff = causal_handoff(plan)
            self._log(
                AuditEventType.DEV_CAUSAL_HANDOFF,
                "dev_causal_handoff",
                request,
                {
                    "present": True,
                    "chars": len(handoff),
                    "sha256": hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
                    "keys": sorted(json.loads(handoff)),
                },
            )

        context_files: list[ContextFile] = list(inventory["selected"])
        granted: list[str] = []
        denied: list[str] = []
        change_issues: tuple[BuildValidationIssue, ...] = ()
        applied: list[AppliedChange] = []
        verification: list[CommandEvidence] = []
        checkpoint: RepairSnapshot | None = None
        #: AP000-OBS-02: evidencia de aceptación de la última ronda evaluada.
        acceptance_issues: tuple[BuildValidationIssue, ...] = ()
        acceptance_evidence: tuple[AcceptanceEvidence, ...] = ()
        rounds = 0
        provider = ""
        model = ""
        failure_evidence = ""
        root_cause = ""
        previous_signature = ""
        previous_strategy: tuple[str, ...] = ()
        stagnation_streak = 0
        # EXPERIMENTO 03: estado acumulado de la resolución (qué se tocó, qué se explicó y si el
        # fallo dejó de avanzar). Es local a la ejecución: un ciclo nuevo empieza sin memoria.
        resolution = ResolutionState()

        # El techo de iteraciones es explícito: rondas funcionales de reparación + correcciones
        # estructurales + la implementación inicial. Ni bucle abierto ni rondas escondidas.
        max_iterations = (
            self.config.max_repair_rounds
            + 1
            + self.config.max_structural_corrections
        )
        # La corrección estructural se construye al final de una iteración y se entrega en la
        # siguiente invocación: se consume una sola vez (si no, se perdería antes de viajar).
        pending_feedback = ""
        for _ in range(max_iterations):
            # La skill de resolución solo actúa sobre un **fallo real ya medido**: sin verificación
            # fallida no hay nada que resolver, y la implementación inicial no la recibe.
            failed_now = tuple(item for item in verification if not item.passed)
            resolution_phase = bool(failed_now)
            current_failure = (
                failure_map(verification, target, plan) if resolution_phase else None
            )
            block = ""
            proposal_feedback = pending_feedback
            pending_feedback = ""
            if current_failure is not None:
                causal_gap = resolution.observe_failure(current_failure)
                block = resolution_block(
                    round_index=rounds,
                    failure=current_failure,
                    previous_patch=sorted(resolution.touched),
                    previous_strategy=(
                        resolution.records[-1].strategy_signature if resolution.records else ""
                    ),
                    causal_gap=causal_gap,
                    stagnation=resolution.stagnation,
                )
                # Los metadatos de auditoría se congelan (una lista se vuelve tupla y un objeto se
                # vuelve pares), así que la evidencia se registra en listas planas y legibles.
                self._log(
                    AuditEventType.DEV_RESOLUTION_INPUT,
                    "dev_resolution_input",
                    request,
                    {
                        "round": rounds,
                        "failed": list(current_failure.failed),
                        "resource_paths": list(current_failure.paths),
                        "resource_relations": [
                            f"{item.path}={item.relation}"
                            for item in current_failure.resources
                        ],
                        "unmapped": list(current_failure.unmapped),
                        "previous_patch": sorted(resolution.touched),
                        "causal_gap": list(causal_gap),
                        "causal_stagnation": resolution.stagnation,
                        "block_chars": len(block),
                    },
                    AuditResult.FAILURE,
                )
            prompt = self._build_prompt(
                request,
                target,
                plan,
                context_files,
                pell_block,
                failure_evidence,
                resolution=block,
                proposal_feedback=proposal_feedback,
            )
            result = self._invoke(
                ProviderRole.BUILDER,
                request,
                prompt,
                BUILD_SCHEMA,
                phase=RESOLUTION_PHASE if resolution_phase else IMPLEMENTATION_PHASE,
            )
            provider = result.provider or provider
            model = result.model or model
            if result.status is not ProviderStatus.SUCCESS:
                return self._outcome(
                    status=DevelopmentStatus.PROVIDER_FAILED,
                    error_kind=(
                        result.error_kind.value if result.error_kind else "PROVIDER_FAILED"
                    ),
                    error=result.error or result.status.value,
                    provider=provider,
                    model=model,
                    applied=applied,
                    verification=verification,
                    repair_rounds=rounds,
                    granted=granted,
                    denied=denied,
                    checkpoint=checkpoint,
                    influence=influence,
                )
            payload = _json_object(result.content)
            if payload is None:
                change_issues = (
                    BuildValidationIssue(
                        code="CHANGES_NOT_JSON",
                        detail="el BUILDER no devolvió cambios interpretables",
                    ),
                )
                failure_evidence = change_issues[0].detail
                continue

            # Frontera proveedor → propuesta: los campos **descriptivos** que exceden su límite se
            # ajustan de forma determinista y registrada (nunca path, contenido ni operación).
            payload, normalized = normalize_descriptive_fields(payload)
            if normalized:
                self._log(
                    AuditEventType.DEV_PROPOSAL_NORMALIZED,
                    "dev_proposal_normalized",
                    request,
                    {
                        "round": rounds,
                        "fields": [item.location for item in normalized],
                        "original_chars": [item.original_chars for item in normalized],
                        "kept_chars": [item.kept_chars for item in normalized],
                        "original_sha256": [item.original_sha256 for item in normalized],
                        "authority": "solo anotaciones: path, operación y contenido no se tocan",
                    },
                )
            # Causa raíz: en una reparación, sin hipótesis no se toca nada. Es la regla que impide
            # el «patch until green»: cada ronda explica qué falló, por qué y qué espera conseguir.
            root_cause, root_evidence, expected_effect = _root_cause(payload)
            if rounds > 0:
                if not root_cause:
                    change_issues = (
                        BuildValidationIssue(
                            code="CHANGE_WITHOUT_ROOT_CAUSE",
                            detail=(
                                "una reparación sin causa raíz es un parche a ciegas: declara "
                                "root_cause, evidence y expected_effect"
                            ),
                        ),
                    )
                    failure_evidence = change_issues[0].detail
                    if rounds >= self.config.max_repair_rounds:
                        # Nada de trabajo aplicado se queda sin verificar: si se corta aquí, se
                        # revierte lo que sí se aplicó en rondas anteriores.
                        rolled_back = self._rollback(
                            request, checkpoint, repository, tuple(applied)
                        )
                        return self._outcome(
                            status=DevelopmentStatus.CHANGE_REJECTED,
                            error_kind="CHANGE_WITHOUT_ROOT_CAUSE",
                            error=change_issues[0].detail,
                            provider=provider,
                            model=model,
                            applied=() if rolled_back else applied,
                            verification=verification,
                            repair_rounds=rounds,
                            granted=granted,
                            denied=denied,
                            checkpoint=checkpoint,
                            influence=influence,
                            change_issues=change_issues,
                            rolled_back=rolled_back,
                            acceptance=acceptance_evidence,
                            acceptance_result=self._acceptance_result(acceptance_evidence),
                        )
                    rounds += 1
                    continue
                self._log(
                    AuditEventType.DEV_ROOT_CAUSE_IDENTIFIED,
                    "dev_root_cause_identified",
                    request,
                    {
                        "round": rounds,
                        "root_cause": root_cause[:200],
                        "evidence": list(root_evidence)[:5],
                        "expected_effect": expected_effect[:200],
                    },
                )
                self._last_root_cause = root_cause

            # Expansión de alcance: solo con evidencia causal y dentro de la misma clase de riesgo.
            expansion = _scope_expansion(payload)
            expansion_status = ""
            if expansion is not None:
                plan, expansion_status = self._handle_scope_expansion(
                    request=request,
                    plan=plan,
                    repository=repository,
                    payload=expansion,
                    round_index=rounds,
                )
                if expansion_status == "HUMAN_GATE":
                    return self._outcome(
                        status=DevelopmentStatus.BLOCKED,
                        error_kind="HUMAN_GATE_REQUIRED",
                        error=(
                            "la ampliación de alcance cruza una frontera que exige autorización "
                            "humana: el ciclo se detiene sin tocar el recurso"
                        ),
                        provider=provider,
                        model=model,
                        applied=applied,
                        verification=verification,
                        repair_rounds=rounds,
                        granted=granted,
                        denied=denied,
                        checkpoint=checkpoint,
                        influence=influence,
                    )

            # Peticiones de contexto: se conceden o se deniegan, y se vuelve a pedir el trabajo.
            requests = _context_requests(payload)
            if requests and rounds < self.config.max_context_rounds:
                granted_now, denied_now = self._handle_context_requests(
                    request, repository, requests, context_files
                )
                granted.extend(granted_now)
                denied.extend(denied_now)
                if granted_now:
                    continue

            proposals, proposal_issue = self._proposals(payload)
            if proposal_issue is not None:
                change_issues = (proposal_issue,)
                failure_evidence = proposal_issue.detail
                continue

            # Preflight determinista de la propuesta: los hechos del workspace que PUNTO puede
            # medir se comprueban **antes** de validar y aplicar. Una inconsistencia estructural
            # se corrige sin consumir una ronda funcional de reparación; la atomicidad no cambia
            # (nada se aplica a medias) y la autoridad no se roza: si el preflight pasa, manda
            # ``_validate_changes``.
            if proposals and self._structural_corrections < self.config.max_structural_corrections:
                preflight = self._preflight_proposal(proposals, repository)
                if preflight is not None and not preflight.valid and preflight.correctable:
                    self._structural_corrections += 1
                    pending_feedback = correction_feedback(preflight)
                    self._log(
                        AuditEventType.DEV_PROPOSAL_PREFLIGHT_FAILED,
                        "dev_proposal_preflight_failed",
                        request,
                        {
                            "round": rounds,
                            "structural_correction": self._structural_corrections,
                            "max_structural_corrections": self.config.max_structural_corrections,
                            "issue_codes": list(issue_codes(preflight)),
                            "issues": [item.as_dict() for item in preflight.blocking[:5]],
                            "advisory_codes": [
                                item.code for item in preflight.advisory[:5]
                            ],
                            "repair_round_consumed": False,
                        },
                        AuditResult.FAILURE,
                    )
                    continue

            validated, issues = self._validate_changes(
                proposals=proposals,
                plan=plan,
                repository=repository,
                target=target,
                request=request,
            )
            change_issues = issues
            if issues or not validated:
                self._log(
                    AuditEventType.DEV_CHANGE_REJECTED,
                    "dev_change_rejected",
                    request,
                    {
                        "issue_codes": [issue.code for issue in issues],
                        "round": rounds,
                    },
                    AuditResult.FAILURE,
                )
                # Una operación que exige autoridad humana no se arregla reescribiéndola: insistir
                # con otra ronda gasta proveedor y presupuesto de reparación para acabar en el mismo
                # sitio. Se corta aquí, con su código, para que la persona decida (Human Gate).
                human_kind = next(
                    (
                        issue.code
                        for issue in issues
                        if issue.code in _HUMAN_REQUIRED_CHANGE_CODES
                    ),
                    "",
                )
                if human_kind or rounds >= self.config.max_repair_rounds:
                    return self._outcome(
                        status=DevelopmentStatus.CHANGE_REJECTED,
                        error_kind=human_kind or "CHANGE_REJECTED",
                        error="; ".join(issue.detail for issue in issues)[:500],
                        provider=provider,
                        model=model,
                        applied=applied,
                        verification=verification,
                        repair_rounds=rounds,
                        granted=granted,
                        denied=denied,
                        checkpoint=checkpoint,
                        influence=influence,
                        change_issues=issues,
                        acceptance=acceptance_evidence,
                        acceptance_result=self._acceptance_result(acceptance_evidence),
                    )
                # Un cambio rechazado es una corrección pendiente, no el final del ciclo: se le
                # devuelve al proveedor el motivo exacto y se le da otra ronda acotada.
                rounds += 1
                failure_evidence = "REJECTED CHANGES (fix these and answer again):\n" + "\n".join(
                    f"- {issue.code}: {issue.detail}" for issue in issues
                )
                self._log(
                    AuditEventType.DEV_REPAIR_STARTED,
                    "dev_change_repair_started",
                    request,
                    {
                        "round": rounds,
                        "issue_codes": [issue.code for issue in issues],
                    },
                    AuditResult.FAILURE,
                )
                continue

            if checkpoint is None:
                checkpoint = self._create_checkpoint(request, target, repository, validated)
            if checkpoint is None:
                return self._outcome(
                    status=DevelopmentStatus.BLOCKED,
                    error_kind="CHECKPOINT_FAILED",
                    error="no se pudo crear el checkpoint reversible: no se escribe nada",
                    provider=provider,
                    model=model,
                    applied=applied,
                    verification=verification,
                    repair_rounds=rounds,
                    granted=granted,
                    denied=denied,
                    influence=influence,
                )

            try:
                round_applied = self._apply(repository, validated, round_index=rounds)
            except _ApplyAborted as aborted:
                change_issues = (
                    BuildValidationIssue(
                        code=aborted.code, detail=f"{aborted.detail[:300]} (aplicación abortada)"
                    ),
                )
                self._log(
                    AuditEventType.DEV_CHANGE_REJECTED,
                    "dev_change_rejected",
                    request,
                    {"issue_codes": [aborted.code], "applied_before": len(aborted.applied)},
                    AuditResult.FAILURE,
                )
                rolled_back = self._rollback(
                    request, checkpoint, repository, aborted.applied
                )
                return self._outcome(
                    status=DevelopmentStatus.CHANGE_REJECTED,
                    error_kind=aborted.code,
                    error=aborted.detail,
                    provider=provider,
                    model=model,
                    applied=() if rolled_back else aborted.applied,
                    verification=verification,
                    repair_rounds=rounds,
                    granted=granted,
                    denied=denied,
                    checkpoint=checkpoint,
                    influence=influence,
                    change_issues=change_issues,
                    rolled_back=rolled_back,
                    acceptance=acceptance_evidence,
                    acceptance_result=self._acceptance_result(acceptance_evidence),
                )
            applied.extend(round_applied)
            if rounds > 0:
                self._log(
                    AuditEventType.DEV_REPAIR_COMPLETED,
                    "dev_repair_completed",
                    request,
                    {"round": rounds, "changes": len(validated)},
                )
            verification = self._verify(repository, target, plan, request)
            chain_ok, chain_issues = self._verify_functional_chain(plan, verification, request)
            # AP000-OBS-02: la aceptación se mide contra la superficie solicitada, después del
            # build y antes de dar la ronda por buena. Un fallo aquí no es VERIFIED: repara.
            acceptance_ok, acceptance_issues, acceptance_evidence = self._verify_acceptance(
                request, repository
            )
            # AP000-OBS-03: las afirmaciones factuales/semánticas se miden con evidencia real.
            # Un criterio requerido sin evidencia no se convierte en PASS: se para y se pide.
            claims_outcome, claim_issues, claim_records = self._verify_semantic_claims(
                request, repository, target
            )
            claim_evidence = self._claim_evidence(claim_records)
            if claims_outcome == "EVIDENCE_REQUIRED":
                rolled_back = False
                return self._outcome(
                    status=DevelopmentStatus.BLOCKED,
                    error_kind="EVIDENCE_REQUIRED",
                    error=(
                        "hay un criterio factual/semántico requerido que no se puede demostrar con "
                        "la evidencia disponible: "
                        + "; ".join(
                            item.evidence for item in claim_records if item.not_verified
                        )[:400]
                    ),
                    provider=provider,
                    model=model,
                    applied=applied,
                    verification=verification,
                    repair_rounds=rounds,
                    granted=granted,
                    denied=denied,
                    checkpoint=checkpoint,
                    influence=influence,
                    rolled_back=rolled_back,
                    change_issues=(),
                    acceptance=acceptance_evidence,
                    acceptance_result=self._acceptance_result(acceptance_evidence),
                    claims=claim_evidence,
                    claims_result=claims_outcome,
                )
            # El progreso causal se registra **antes** de decidir: la ronda que resuelve el fallo
            # también es evidencia (qué recurso lo resolvió), no solo la que vuelve a fallar.
            signature = _failure_signature(verification)
            strategy = tuple(
                f"{item.path}:{item.operation.value}" for item in round_applied
            )
            # Recursos que este parche tocó de verdad (incluido el origen de un RENAME/MOVE, que se
            # borra: contar solo el destino dejaría fuera un recurso modificado).
            touched_now = tuple(
                dict.fromkeys(
                    normalize_path(item)
                    for proposal in validated
                    for item in (proposal.path, proposal.source_path)
                    if item
                )
            )
            passed = (
                all(item.passed for item in verification)
                and chain_ok
                and acceptance_ok
                and claims_outcome in {"SATISFIED", "NONE"}
            )
            self._record_resolution_progress(
                request=request,
                rounds=rounds,
                state=resolution,
                failure=current_failure,
                plan=plan,
                payload=payload,
                touched_now=touched_now,
                strategy=strategy,
                signature=signature,
                previous_signature=previous_signature,
                still_failing=tuple(
                    item.name for item in verification if not item.passed
                ),
                # Ampliar el alcance con evidencia es una de las salidas legítimas de una
                # reparación: cuenta como progreso causal solo si PUNTO la aprobó en esta ronda.
                escalated=(
                    escalation_resources(payload)
                    if expansion_status == "APPROVED"
                    else ()
                ),
                passed=passed,
            )
            if passed:
                return self._outcome(
                    status=DevelopmentStatus.COMPLETED,
                    error_kind="",
                    error="",
                    provider=provider,
                    model=model,
                    applied=applied,
                    verification=verification,
                    repair_rounds=rounds,
                    granted=granted,
                    denied=denied,
                    checkpoint=checkpoint,
                    influence=influence,
                    change_issues=(),
                    acceptance=acceptance_evidence,
                    acceptance_result=self._acceptance_result(acceptance_evidence),
                    claims=claim_evidence,
                    claims_result=claims_outcome,
                )
            if chain_issues:
                change_issues = (*change_issues, *chain_issues)
            if acceptance_issues:
                change_issues = (*change_issues, *acceptance_issues)
            if claim_issues:
                change_issues = (*change_issues, *claim_issues)
            stagnated = (
                bool(previous_signature)
                and signature == previous_signature
                and strategy == previous_strategy
            )
            stagnation_streak = stagnation_streak + 1 if stagnated else 0
            self._log(
                AuditEventType.DEV_REPAIR_PROGRESS,
                "dev_repair_progress",
                request,
                {
                    "round": rounds,
                    "failure_signature": signature,
                    "hypothesis": root_cause[:200],
                    "strategy": list(strategy),
                    "changed_resources": [item.path for item in round_applied],
                    "result": "FAILED",
                    "progress": not stagnated,
                },
                AuditResult.FAILURE,
            )
            if stagnation_streak >= self.config.stagnation_limit:
                self._log(
                    AuditEventType.DEV_STAGNATION_DETECTED,
                    "dev_stagnation_detected",
                    request,
                    {
                        "round": rounds,
                        "failure_signature": signature,
                        "strategy": list(strategy),
                        "streak": stagnation_streak,
                    },
                    AuditResult.FAILURE,
                )
                rolled_back = self._rollback(request, checkpoint, repository, tuple(applied))
                return self._outcome(
                    status=DevelopmentStatus.BLOCKED,
                    error_kind="STAGNATION",
                    error=(
                        "la reparación repite el mismo fallo con la misma estrategia: se corta "
                        "antes de gastar rondas en balde"
                    ),
                    provider=provider,
                    model=model,
                    applied=() if rolled_back else applied,
                    verification=verification,
                    repair_rounds=rounds,
                    granted=granted,
                    denied=denied,
                    checkpoint=checkpoint,
                    influence=influence,
                    change_issues=change_issues,
                    rolled_back=rolled_back,
                )
            previous_signature, previous_strategy = signature, strategy
            failure_evidence = self._failure_evidence(verification)
            if chain_issues:
                failure_evidence += "\nFUNCTIONAL CHAIN INCOMPLETE:\n" + "\n".join(
                    f"- {issue.code}: {issue.detail}" for issue in chain_issues
                )
            if claim_issues:
                failure_evidence += (
                    "\nFACTUAL/SEMANTIC CLAIM NOT SATISFIED (the requested property is not proven "
                    "by the implementation):\n"
                    + "\n".join(f"- {issue.detail}" for issue in claim_issues)
                    + "\nProvide real evidence for the claim (an authoritative dataset that the "
                    "code actually uses), not an approximation."
                )
            if acceptance_issues:
                failure_evidence += (
                    "\nACCEPTANCE NOT SATISFIED (the requested element is still where it was, or "
                    "the surface the request names was not touched):\n"
                    + "\n".join(f"- {issue.detail}" for issue in acceptance_issues)
                    + "\nFix the surface that the request names; do not implement a related "
                    "feature somewhere else."
                )
            if stagnated:
                failure_evidence += (
                    "\nSTAGNATION: the same failure with the same strategy. Change your "
                    "hypothesis and your approach; do not repeat the previous patch."
                )
            if rounds >= self.config.max_repair_rounds:
                break
            rounds += 1
            self._log(
                AuditEventType.DEV_REPAIR_STARTED,
                "dev_repair_started",
                request,
                {
                    "round": rounds,
                    "failed": [item.name for item in verification if not item.passed],
                },
                AuditResult.FAILURE,
            )

        self._log(
            AuditEventType.DEV_REPAIR_EXHAUSTED,
            "dev_repair_exhausted",
            request,
            {"rounds": rounds, "failed": [item.name for item in verification if not item.passed]},
            AuditResult.FAILURE,
        )
        rolled_back = self._rollback(request, checkpoint, repository, tuple(applied))
        # AP000-OBS-02: si lo que sigue fallando es la aceptación (no las verificaciones), el código
        # lo dice: la implementación puede estar verde y no ser lo que la solicitud pedía.
        solo_aceptacion = bool(acceptance_issues) and all(item.passed for item in verification)
        return self._outcome(
            status=DevelopmentStatus.VERIFICATION_FAILED,
            error_kind="ACCEPTANCE_NOT_SATISFIED" if solo_aceptacion else "VERIFICATION_FAILED",
            error=(
                "el criterio de aceptación no quedó satisfecho en la superficie solicitada "
                "tras las rondas de reparación"
                if solo_aceptacion
                else "la verificación no pasó tras las rondas de reparación"
            ),
            provider=provider,
            model=model,
            applied=() if rolled_back else applied,
            verification=verification,
            repair_rounds=rounds,
            granted=granted,
            denied=denied,
            checkpoint=checkpoint,
            influence=influence,
            rolled_back=rolled_back,
            change_issues=change_issues,
            acceptance=acceptance_evidence,
            acceptance_result=self._acceptance_result(acceptance_evidence),
        )

    def _handle_context_requests(
        self,
        request: BuildRequest,
        repository: GovernedRepository,
        requests: Sequence[ContextRequest],
        context_files: list[ContextFile],
    ) -> tuple[list[str], list[str]]:
        """Concede o deniega peticiones de contexto, sin Human Gate si están dentro de autoridad."""
        granted: list[str] = []
        denied: list[str] = []
        seen = {item.path for item in context_files}
        for item in requests[: self.config.max_context_files]:
            try:
                content = repository.read_text(item.path)
            except RepositoryDenied as exc:
                denied.append(item.path)
                self._log(
                    AuditEventType.DEV_CONTEXT_DENIED,
                    "dev_context_denied",
                    request,
                    {
                        "path": item.path,
                        "code": getattr(exc, "code", "DENIED"),
                        "reason": item.reason[:200],
                    },
                    AuditResult.DENIED,
                )
                continue
            if item.path in seen:
                continue
            context_files.append(
                ContextFile(
                    path=item.path,
                    sha256=repository.sha256(item.path),
                    content=content[: self.config.max_context_file_chars],
                )
            )
            seen.add(item.path)
            granted.append(item.path)
            self._log(
                AuditEventType.DEV_CONTEXT_GRANTED,
                "dev_context_granted",
                request,
                {"path": item.path, "chars": len(content), "reason": item.reason[:200]},
            )
        return granted, denied

    # ---------------------------------------------------------------- validación
    def _proposals(
        self, payload: Mapping[str, Any]
    ) -> tuple[tuple[FileChangeProposal, ...], BuildValidationIssue | None]:
        """Interpreta la lista de cambios del BUILDER."""
        raw = payload.get("changes")
        if not isinstance(raw, list) or not raw:
            return (), BuildValidationIssue(
                code="CHANGES_EMPTY", detail="el BUILDER no propuso ningún cambio"
            )
        proposals: list[FileChangeProposal] = []
        for item in raw:
            if not isinstance(item, Mapping):
                return (), BuildValidationIssue(
                    code="CHANGE_MALFORMED", detail="un cambio no es un objeto"
                )
            try:
                proposals.append(FileChangeProposal.model_validate(dict(item)))
            except Exception as exc:
                return (), BuildValidationIssue(code="CHANGE_INVALID", detail=str(exc)[:300])
        return tuple(proposals), None

    def _validate_changes(
        self,
        *,
        proposals: Sequence[FileChangeProposal],
        plan: DevelopmentPlan,
        repository: GovernedRepository,
        target: DevelopmentTarget,
        request: BuildRequest,
    ) -> tuple[tuple[FileChangeProposal, ...], tuple[BuildValidationIssue, ...]]:
        """Valida cada cambio: alcance, operación, autoridad por riesgo, huella y secretos."""
        issues: list[BuildValidationIssue] = []
        validated: list[FileChangeProposal] = []
        declared = set(plan.touched_paths())
        seen: set[str] = set()
        for proposal in proposals:
            path = proposal.path.replace("\\", "/")
            if path in seen:
                issues.append(
                    BuildValidationIssue(
                        code="CHANGE_DUPLICATED",
                        detail=f"{path!r} aparece dos veces en la misma propuesta",
                    )
                )
                continue
            seen.add(path)
            missing = [
                item
                for item in (path, proposal.source_path)
                if item and item.replace("\\", "/") not in declared
            ]
            if missing:
                issues.append(
                    BuildValidationIssue(
                        code="CHANGE_NOT_IN_PLAN",
                        detail=(
                            f"{missing[0]!r} no está en el plan vigente: se aplica solo lo "
                            "declarado, o se amplía el alcance con evidencia causal"
                        ),
                    )
                )
                continue
            if (
                proposal.operation in (ChangeOperation.DELETE, ChangeOperation.RENAME,
                                       ChangeOperation.MOVE)
                and RepositoryOperation.DELETE not in target.allowed_operations
            ):
                issues.append(
                    BuildValidationIssue(
                        code="CHANGE_DELETE_NOT_AUTHORIZED",
                        detail=f"{path!r}: borrar no está autorizado en este destino",
                    )
                )
                continue
            try:
                current = repository.sha256(path)
                exists = repository.exists(path)
                if proposal.operation is ChangeOperation.CREATE and exists:
                    issues.append(
                        BuildValidationIssue(
                            code="CHANGE_ALREADY_EXISTS",
                            detail=(
                                f"{path!r} ya existe: para cambiarlo la operación es MODIFY, y un "
                                "CREATE sobre algo existente se rechaza antes de escribir"
                            ),
                        )
                    )
                    continue
                if proposal.operation is ChangeOperation.MODIFY and not exists:
                    issues.append(
                        BuildValidationIssue(
                            code="CHANGE_MISSING_FILE",
                            detail=f"{path!r} no existe: para crearlo la operación es CREATE",
                        )
                    )
                    continue
                if proposal.operation in (ChangeOperation.RENAME, ChangeOperation.MOVE):
                    source = (proposal.source_path or "").replace("\\", "/")
                    if not repository.exists(source):
                        issues.append(
                            BuildValidationIssue(
                                code="CHANGE_MISSING_SOURCE",
                                detail=f"{source!r} no existe: no hay nada que mover",
                            )
                        )
                        continue
                    if exists:
                        issues.append(
                            BuildValidationIssue(
                                code="CHANGE_ALREADY_EXISTS",
                                detail=(
                                    f"{path!r} ya existe: el destino del movimiento está ocupado"
                                ),
                            )
                        )
                        continue
                    source_sha = repository.sha256(source)
                    if proposal.expected_sha256 and proposal.expected_sha256 != source_sha:
                        issues.append(
                            BuildValidationIssue(
                                code="CHANGE_STALE",
                                detail=f"{source!r}: el fichero cambió desde que se leyó",
                            )
                        )
                        continue
                if proposal.expected_sha256 and proposal.expected_sha256 != current:
                    issues.append(
                        BuildValidationIssue(
                            code="CHANGE_STALE",
                            detail=f"{path!r}: el fichero cambió desde que se leyó",
                        )
                    )
                    continue
                if proposal.operation not in (
                    ChangeOperation.DELETE,
                    ChangeOperation.RENAME,
                    ChangeOperation.MOVE,
                ):
                    repository.assert_no_secrets_in_text(path, proposal.content or "")
            except SecretBoundaryViolation as exc:
                issues.append(
                    BuildValidationIssue(code="CHANGE_SECRET", detail=exc.detail[:300])
                )
                continue
            except RepositoryDenied as exc:
                issues.append(
                    BuildValidationIssue(
                        code=getattr(exc, "code", "CHANGE_DENIED"), detail=exc.detail[:300]
                    )
                )
                continue
            created = path in self._created_paths
            decision = self.envelope.assess(
                self._change_profile(proposal, created_by_cycle=created)
            )
            self._record_decision(decision, request, phase="change")
            if not decision.autonomous:
                issues.append(
                    BuildValidationIssue(
                        code=(
                            "CHANGE_OUTSIDE_AUTHORITY"
                            if decision.prohibited
                            else "CHANGE_REQUIRES_HUMAN"
                        ),
                        detail=(
                            f"{path!r}: el sobre de autoridad devuelve "
                            f"{decision.outcome.value} ({decision.authority_class.value}, riesgo "
                            f"{decision.risk.name}): " + "; ".join(decision.reasons)[:160]
                        )[:300],
                    )
                )
                continue
            validated.append(proposal)
            self._cumulative_resources.add(path)
            if proposal.operation is ChangeOperation.CREATE:
                self._created_paths.add(path)
            if proposal.operation in (ChangeOperation.DELETE, ChangeOperation.RENAME,
                                      ChangeOperation.MOVE):
                self._created_paths.discard((proposal.source_path or path).replace("\\", "/"))
        if not issues:
            self._log(
                AuditEventType.DEV_CHANGE_VALIDATED,
                "dev_change_validated",
                request,
                {"changes": len(validated), "paths": [item.path for item in validated]},
            )
        return tuple(validated), tuple(issues)

    # ------------------------------------------------------------------- apply
    def _create_checkpoint(
        self,
        request: BuildRequest,
        target: DevelopmentTarget,
        repository: GovernedRepository,
        validated: Sequence[FileChangeProposal],
    ) -> RepairSnapshot | None:
        """Crea el checkpoint reversible antes de la primera escritura.

        El checkpoint vive **en el propio workspace gobernado** (``.punto-repair-snapshots/``),
        que es como está diseñado el componente reutilizado: resuelve y restaura rutas relativas
        a su raíz. El directorio se autogitignora y la frontera de recursos lo tiene prohibido,
        así que no entra en el commit ni puede leerse ni escribirse desde el ciclo.
        """
        if self._snapshots is None:
            self._snapshots = FileRepairSnapshots(target.repository)
        # El checkpoint cubre **todo** lo que el ciclo puede cambiar, incluido el origen de un
        # RENAME/MOVE: si no, revertir un movimiento dejaría el fichero borrado.
        paths: list[str] = []
        for proposal in validated:
            candidates = (
                proposal.path.replace("\\", "/"),
                (proposal.source_path or "").replace("\\", "/"),
            )
            paths.extend(item for item in candidates if item)
        try:
            snapshot = self._snapshots.create(
                repair_id=request.request_id,
                cycle=1,
                paths=paths,
                workspace_path=str(target.repository),
            )
        except ValueError as exc:
            self._log(
                AuditEventType.DEV_CYCLE_BLOCKED,
                "dev_checkpoint_failed",
                request,
                {"detail": str(exc)[:300]},
                AuditResult.FAILURE,
            )
            return None
        self._log(
            AuditEventType.DEV_CHECKPOINT_CREATED,
            "dev_checkpoint_created",
            request,
            {
                "snapshot_id": str(snapshot.snapshot_id),
                "paths": len(snapshot.entries),
                "fingerprint": snapshot.workspace_fingerprint,
            },
        )
        self._checkpoint = snapshot
        return snapshot

    def _apply(
        self,
        repository: GovernedRepository,
        validated: Sequence[FileChangeProposal],
        *,
        round_index: int,
    ) -> tuple[AppliedChange, ...]:
        """Aplica los cambios validados por la única puerta de escritura.

        Una denegación de la frontera **no escapa** del ciclo: se registra, se deshace lo aplicado y
        el ciclo termina con su estado. Dejar salir la excepción convertiría una decisión de
        autoridad en una caída del motor.

        Raises:
            RepositoryDenied: propagada solo a través de :class:`_ApplyAborted` para que la maneje
                el bucle, nunca el llamante.
        """
        applied: list[AppliedChange] = []
        for proposal in validated:
            path = proposal.path.replace("\\", "/")
            try:
                if proposal.operation is ChangeOperation.DELETE:
                    # La huella del cambio es la del contenido **que se quitó**: es la evidencia,
                    # no un hueco. Un borrado verificado conserva lo que había.
                    digest = repository.sha256(path)
                    change = repository.delete_file(
                        path,
                        expected_sha256=proposal.expected_sha256,
                        reversible=self._checkpoint is not None,
                    )
                    applied.append(
                        AppliedChange(
                            path=path,
                            operation=ChangeOperation.DELETE,
                            bytes_written=0,
                            sha256=digest,
                            verified=change.verified,
                            round_index=round_index,
                        )
                    )
                    continue
                if proposal.operation in (ChangeOperation.RENAME, ChangeOperation.MOVE):
                    source = (proposal.source_path or "").replace("\\", "/")
                    source_digest = repository.sha256(source)
                    content = repository.read_text(source)
                    created = repository.write_text(
                        path,
                        content,
                        operation=ChangeOperation.CREATE,
                        expected_sha256=None,
                    )
                    removed = repository.delete_file(
                        source,
                        expected_sha256=proposal.expected_sha256,
                        reversible=self._checkpoint is not None,
                    )
                    applied.append(
                        AppliedChange(
                            path=path,
                            operation=proposal.operation,
                            bytes_written=created.bytes_written,
                            sha256=repository.sha256(path),
                            verified=created.verified and removed.verified,
                            round_index=round_index,
                        )
                    )
                    applied.append(
                        AppliedChange(
                            path=source,
                            operation=ChangeOperation.DELETE,
                            bytes_written=0,
                            sha256=source_digest,
                            verified=removed.verified,
                            round_index=round_index,
                        )
                    )
                    continue
                change = repository.write_text(
                    path,
                    proposal.content or "",
                    operation=proposal.operation,
                    expected_sha256=proposal.expected_sha256,
                )
                digest = repository.sha256(path)
                written = change.bytes_written
            except RepositoryDenied as exc:
                raise _ApplyAborted(
                    code=getattr(exc, "code", "CHANGE_DENIED"),
                    detail=exc.detail,
                    applied=tuple(applied),
                ) from exc
            applied.append(
                AppliedChange(
                    path=path,
                    operation=proposal.operation,
                    bytes_written=written,
                    sha256=digest,
                    verified=change.verified,
                    round_index=round_index,
                )
            )
        return tuple(applied)

    # -------------------------------------------------------------- verificación
    def _verify(
        self,
        repository: GovernedRepository,
        target: DevelopmentTarget,
        plan: DevelopmentPlan,
        request: BuildRequest,
    ) -> list[CommandEvidence]:
        """Ejecuta los comandos de verificación declarados y recoge la evidencia del entorno."""
        names = [name for name in plan.verification_commands if name in set(target.command_names())]
        self._log(
            AuditEventType.DEV_VERIFICATION_STARTED,
            "dev_verification_started",
            request,
            {"commands": names},
        )
        evidence: list[CommandEvidence] = []
        for name in names:
            command = target.command(name)
            result = repository.run(
                command.argv, name=name, timeout_seconds=command.timeout_seconds
            )
            evidence.append(_evidence(command.name, command.argv, result))
        self._log(
            AuditEventType.DEV_VERIFICATION_COMPLETED,
            "dev_verification_completed",
            request,
            {
                "passed": [item.name for item in evidence if item.passed],
                "failed": [item.name for item in evidence if not item.passed],
            },
            AuditResult.SUCCESS if all(item.passed for item in evidence) else AuditResult.FAILURE,
        )
        return evidence

    @staticmethod
    def _failure_evidence(verification: Sequence[CommandEvidence]) -> str:
        """Evidencia acotada del fallo, para que el BUILDER repare con hechos."""
        parts: list[str] = []
        for item in verification:
            if item.passed:
                continue
            parts.append(
                f"$ {' '.join(item.argv)}\nexit={item.exit_code} timed_out={item.timed_out}\n"
                f"{item.output_excerpt[-1500:]}"
            )
        return "\n\n".join(parts)[:4_000]

    # ---------------------------------------------------------------- rollback
    def _rollback(
        self,
        request: BuildRequest,
        checkpoint: RepairSnapshot | None,
        repository: GovernedRepository,
        applied: Sequence[AppliedChange],
    ) -> bool:
        """Devuelve el árbol al estado capturado, todo o nada."""
        if checkpoint is None or self._snapshots is None:
            return False
        expected = {
            entry.path: repository.sha256(entry.path) or "" for entry in checkpoint.entries
        }
        verdict = self._snapshots.rollback(snapshot=checkpoint, expected=expected)
        self._log(
            AuditEventType.DEV_ROLLBACK_COMPLETED,
            "dev_rollback_completed",
            request,
            {
                "rolled_back": verdict.rolled_back,
                "restored": len(verdict.restored_files),
                "code": "" if verdict.code is None else verdict.code.value,
                "detail": verdict.detail[:200],
                "changes_undone": len(applied),
            },
            AuditResult.SUCCESS if verdict.rolled_back else AuditResult.FAILURE,
        )
        return verdict.rolled_back

    # --------------------------------------------------------------- prompts
    def _plan_prompt(
        self,
        request: BuildRequest,
        target: DevelopmentTarget,
        context_files: Sequence[ContextFile],
        retrieval: RetrievalOutcome,
        *,
        rejection: Sequence[BuildValidationIssue] = (),
        attempt: int = 0,
    ) -> str:
        """Contexto gobernado del ARCHITECT, con el inventario y la experiencia previa.

        El contrato JSON va **escrito en el prompt** además de en el esquema: un transporte de
        suscripción puede ignorar el ``json_schema``, y entonces el único contrato que el modelo ve
        es este texto. Sin él, la respuesta llega sin las claves que PUNTO valida.
        """
        lines = [
            f"TARGET: {target.target_id}",
            f"OBJECTIVE: {request.objective}",
        ]
        if request.acceptance_criteria:
            lines.append("ACCEPTANCE CRITERIA: " + " | ".join(request.acceptance_criteria))
        if request.constraints:
            lines.append("CONSTRAINTS: " + " | ".join(request.constraints))
        lines.append(
            "VERIFICATION CATALOG (usa exactamente estos nombres): "
            + " | ".join(target.command_names())
        )
        lines.append(
            "ALLOWED PATHS: " + (" | ".join(target.scope_roots) or "(todo el repositorio)")
        )
        lines.append(
            "MAX FILES YOU MAY TOUCH: "
            f"{min(self.config.max_files_changed, target.max_files_changed)}"
        )
        # AP000-OBS-02: las superficies que PUNTO localizó para lo que la solicitud menciona. El
        # plan tiene que trabajar sobre ellas; si no, se rechaza antes de escribir nada.
        if self._grounding:
            lines.append(
                "GROUNDED SURFACES (existing elements the request refers to; your plan MUST touch "
                "them where the intent is REPLACE/DELETE/MODIFY/CREATE):"
            )
            for reference in self._grounding:
                if not reference.measurable:
                    continue
                for surface in reference.surfaces:
                    lines.append(
                        f"- {surface.path}:{surface.line} [{reference.intent} {reference.kind}] "
                        f"{surface.snippet[:120]}"
                    )
        for item in context_files:
            lines.append(f"\n===== {item.path} (sha256={item.sha256[:12]}) =====\n{item.content}")
        block = render_experience_block(retrieval.context)
        if block.strip():
            lines.append(block)
        if rejection:
            lines.append(
                "YOUR PREVIOUS ANSWER WAS REJECTED: "
                + " | ".join(f"{issue.code}: {issue.detail}" for issue in rejection)
                + ". Answer again with the exact JSON object."
            )
        if attempt > 0:
            lines.append("This is a retry: return ONLY the JSON object, with no prose around it.")
        lines.append(PLAN_CONTRACT)
        return "\n".join(lines)

    def _build_prompt(
        self,
        request: BuildRequest,
        target: DevelopmentTarget,
        plan: DevelopmentPlan,
        context_files: Sequence[ContextFile],
        pell_block: str,
        failure_evidence: str,
        *,
        resolution: str = "",
        proposal_feedback: str = "",
    ) -> str:
        """Contexto gobernado del BUILDER: plan validado, ficheros y evidencia del fallo.

        ``resolution`` es el bloque compacto que convierte un fallo medido en una reparación
        discriminante (qué recurso mide cada verificación fallida, qué tocó ya el parche anterior y
        qué brecha causal queda abierta). Va vacío en la implementación inicial: sin fallo real no
        hay resolución, y la skill asociada no se activa.

        ``proposal_feedback`` es la corrección estructural de una propuesta que PUNTO rechazó por un
        hecho medible (CREATE sobre algo que existe, MODIFY sobre algo que no está, cambios
        contradictorios o sin efecto). Es corto a propósito: la corrección tiene que ser barata.
        """
        lines = [
            f"TARGET: {target.target_id}",
            f"OBJECTIVE: {request.objective}",
            "VALIDATED PLAN: " + (plan.summary or "(sin resumen)"),
            "PLAN FILES TO MODIFY: " + (" | ".join(plan.files_to_modify) or "(ninguno)"),
            "PLAN FILES TO CREATE: " + (" | ".join(plan.files_to_create) or "(ninguno)"),
        ]
        if self.config.causal_handoff:
            lines.append(CAUSAL_HANDOFF_LABEL + causal_handoff(plan))
        if request.acceptance_criteria:
            lines.append("ACCEPTANCE CRITERIA: " + " | ".join(request.acceptance_criteria))
        for item in context_files:
            lines.append(f"\n===== {item.path} (sha256={item.sha256}) =====\n{item.content}")
        if pell_block.strip():
            lines.append(pell_block)
        if failure_evidence:
            lines.append("VERIFICATION FAILED AND MUST BE FIXED:\n" + failure_evidence)
        if resolution:
            lines.append(resolution)
        if proposal_feedback:
            lines.append(proposal_feedback)
        lines.append(
            "DELIVERABLE: the exact file changes as JSON. You write nothing yourself: PUNTO "
            "validates and applies them. Keep each change MINIMAL: modify only what the task "
            "needs and repeat the rest of the file unchanged. If you need another file, ask for "
            "it in context_requests with its path and a reason instead of inventing its content."
        )
        lines.append(BUILD_CONTRACT)
        return "\n".join(lines)

    def _skill_reference(self, role: ProviderRole, *, phase: str = "") -> str:
        """Skill declarada para un rol **y una fase**: la resolución tiene la suya, separada.

        Que la resolución sea una referencia distinta es lo que hace medible la variable: el
        ARCHITECT y la implementación inicial no reciben la skill de resolución, y durante la
        resolución no se activa ninguna otra.
        """
        if role is ProviderRole.ARCHITECT:
            return self.config.architect_skill
        if role is ProviderRole.BUILDER:
            if phase == RESOLUTION_PHASE:
                return self.config.resolution_skill
            return self.config.builder_skill
        return ""

    def _activation(
        self, role: ProviderRole, request: BuildRequest, *, phase: str = ""
    ) -> SkillActivation:
        """Constancia de la skill del rol en esa fase, activándola como mucho una vez.

        Sin skill declarada se conserva el comportamiento previo y **no se registra activación**:
        solo se audita una skill real. Una skill declarada que no valide **no se ignora**: se falla
        cerrado, porque ejecutar sin ella daría un resultado que no se podría atribuir al
        experimento. ``SKILL != AUTHORITY``: la skill es procedimiento, y nada de lo que diga cambia
        permisos, presupuestos ni verificaciones.
        """
        key = f"{role.value}:{phase or IMPLEMENTATION_PHASE}"
        cached = self._skill_activations.get(key)
        if cached is not None:
            return cached
        declared = self._skill_reference(role, phase=phase)
        if not declared.strip():
            activation = SkillActivation(instructions=WORKER_INSTRUCTIONS)
            self._skill_activations[key] = activation
            return activation
        from punto.skills import SkillValidationError, activate_skill

        try:
            activation = activate_skill(
                declared,
                role=role.value,
                base_instructions=WORKER_INSTRUCTIONS,
            )
        except SkillValidationError as exc:
            self._log(
                AuditEventType.DEV_SKILL_ACTIVATED,
                "dev_skill_activated",
                request,
                {
                    "role": role.value,
                    "phase": phase or IMPLEMENTATION_PHASE,
                    "skill_reference": declared,
                    "activated": False,
                    "detail": str(exc)[:300],
                },
                AuditResult.FAILURE,
            )
            raise DevelopmentCycleError(
                f"la skill declarada no se pudo activar: {exc}"
            ) from exc
        self._skill_activations[key] = activation
        self._log(
            AuditEventType.DEV_SKILL_ACTIVATED,
            "dev_skill_activated",
            request,
            {
                "role": role.value,
                "phase": phase or IMPLEMENTATION_PHASE,
                "skill_id": activation.skill_id,
                "skill_version": activation.skill_version,
                "skill_reference": activation.reference,
                "activated": activation.activated,
                "chars": activation.chars,
                "sha256": activation.sha256,
            },
        )
        return activation

    def _instructions_for(
        self, role: ProviderRole, request: BuildRequest, *, phase: str = ""
    ) -> str:
        """Instrucciones del rol: las de siempre, más la skill activada para ese rol y esa fase."""
        activation = self._activation(role, request, phase=phase)
        return activation.instructions or WORKER_INSTRUCTIONS

    def _invoke(
        self,
        role: ProviderRole,
        request: BuildRequest,
        prompt: str,
        schema: Mapping[str, Any],
        *,
        phase: str = IMPLEMENTATION_PHASE,
    ) -> ProviderResult:
        """Invoca a un rol por el router, con el JSON Schema declarado.

        Antes de invocar se registra **qué proveedor** atiende el rol según la configuración: es
        evidencia de la decisión, no autoridad, y deja el ciclo auditable por rol. La fase decide
        qué skill recibe el rol: la de resolución solo existe cuando hay un fallo que resolver.
        """
        try:
            selected = self.router.get_provider_for_role(role)
        except Exception:  # la ausencia de asignación la reporta el router al invocar
            selected = ""
        self._log(
            AuditEventType.BUILD_PROVIDER_SELECTED,
            "dev_provider_selected",
            request,
            {"role": role.value, "provider": selected, "fallback": False, "phase": phase},
        )
        provider_request = ProviderRequest(
            role=role,
            instructions=self._instructions_for(role, request, phase=phase),
            request_id=str(request.request_id),
            context=prompt,
            metadata={"target_id": request.target_repository, "phase": "PILOT-04"},
        )
        result = self.router.execute(
            role,
            provider_request,
            json_schema=schema,
            max_output_tokens=self.config.max_output_tokens,
        )
        self._last_provider = result.provider or self._last_provider
        self._last_model = result.model or self._last_model
        self._note_failover(role, request, result, phase=phase)
        return result

    def _note_failover(
        self, role: ProviderRole, request: BuildRequest, result: ProviderResult, *, phase: str
    ) -> None:
        """Deja constancia, en la traza de la Task, de una sustitución de proveedor.

        El router decide y ejecuta el failover; el ciclo solo lo **registra** (auditoría por
        ``request_id`` y evidencia persistida en el resultado). No cambia el flujo: lo que produjo
        el sustituto sigue siendo una propuesta que pasa por las mismas validaciones, autoridad y
        verificaciones que la del primario.
        """
        for record in result.failovers:
            evidence = ProviderFailoverEvidence(
                role=record.role.value,
                primary_provider=record.primary_provider,
                primary_model=record.primary_model,
                primary_error_kind=record.primary_error_kind,
                cause=record.cause,
                substitute_provider=record.substitute_provider,
                substitute_model=record.substitute_model,
                outcome=record.outcome.value,
                detail=record.detail[:300],
            )
            self._failovers.append(evidence)
            self._log(
                AuditEventType.BUILD_PROVIDER_SELECTED,
                "dev_provider_failover",
                request,
                {
                    "role": role.value,
                    "provider": record.substitute_provider or record.primary_provider,
                    "fallback": record.outcome is not FailoverOutcome.NO_COMPATIBLE_SUBSTITUTE,
                    "primary_provider": record.primary_provider,
                    "cause": record.cause,
                    "outcome": record.outcome.value,
                    "phase": phase,
                    "authority": "sin autoridad adicional: mismo rol, mismas reglas",
                },
                AuditResult.SUCCESS
                if record.outcome is FailoverOutcome.SUCCEEDED
                else AuditResult.FAILURE,
            )

    # ------------------------------------------------------------- resultado
    def _outcome(self, **kwargs: Any) -> dict[str, Any]:
        """Normaliza el diccionario interno que viaja entre las etapas del ciclo."""
        checkpoint = kwargs.get("checkpoint")
        return {
            "status": kwargs.get("status", DevelopmentStatus.BLOCKED),
            "applied": kwargs.get("applied", ()),
            "verification": kwargs.get("verification", ()),
            "repair_rounds": kwargs.get("repair_rounds", 0),
            # Contabilidad separada: correcciones estructurales de propuesta (no consumen ronda).
            "structural_corrections": kwargs.get(
                "structural_corrections", self._structural_corrections
            ),
            "granted": kwargs.get("granted", []),
            "denied": kwargs.get("denied", []),
            "checkpoint": checkpoint,
            "checkpoint_id": "" if checkpoint is None else str(checkpoint.snapshot_id),
            "rolled_back": kwargs.get("rolled_back", False),
            "commit_sha": kwargs.get("commit_sha", ""),
            "influence": kwargs.get("influence", []),
            "error_kind": kwargs.get("error_kind", ""),
            "error": kwargs.get("error", ""),
            "provider": kwargs.get("provider", ""),
            "model": kwargs.get("model", ""),
            "change_issues": kwargs.get("change_issues", ()),
            "authority_decisions": list(self._authority_decisions),
            "risk_envelopes": list(self._risk_envelopes),
            "plan_versions": list(self._plan_versions),
            "scope_expansions": list(self._scope_expansions),
            "functional_chain_result": self._functional_chain_result,
            "acceptance": kwargs.get("acceptance", ()),
            "acceptance_result": kwargs.get("acceptance_result", "NOT_MEASURED"),
            "claims": kwargs.get("claims", ()),
            "claims_result": kwargs.get("claims_result", "NONE"),
            "plan": self._final_plan,
        }

    def _result(
        self,
        request: BuildRequest,
        target: DevelopmentTarget,
        repository: GovernedRepository,
        *,
        status: DevelopmentStatus,
        plan: DevelopmentPlan | None,
        plan_status: PlanStatus,
        plan_issues: tuple[BuildValidationIssue, ...],
        retrieval: RetrievalOutcome,
        started: float,
        change_issues: tuple[BuildValidationIssue, ...] = (),
        applied: Sequence[AppliedChange] = (),
        verification: Sequence[CommandEvidence] = (),
        repair_rounds: int = 0,
        structural_corrections: int = 0,
        granted: Sequence[str] = (),
        denied: Sequence[str] = (),
        checkpoint_id: str = "",
        rolled_back: bool = False,
        commit_sha: str = "",
        influence: Sequence[PellInfluence] = (),
        error_kind: str = "",
        error: str = "",
        provider: str = "",
        model: str = "",
        initial_scope: Sequence[str] = (),
        final_scope: Sequence[str] = (),
        plan_versions: Sequence[PlanRevisionRecord] = (),
        risk_envelopes: Sequence[Mapping[str, Any]] = (),
        scope_expansions: Sequence[ScopeExpansionRecord] = (),
        authority_decisions: Sequence[AuthorityDecisionRecord] = (),
        functional_chain_result: str = "",
        acceptance: Sequence[AcceptanceEvidence] = (),
        acceptance_result: str = "NOT_MEASURED",
        claims: Sequence[ClaimEvidence] = (),
        claims_result: str = "NONE",
        capabilities: Sequence[CapabilityEvidence] | None = None,
    ) -> DevelopmentResult:
        """Cierra el ciclo: aprende (si procede), confirma lo suyo y publica el resultado."""
        final_status = status
        final_applied = tuple(applied)
        final_rolled_back = rolled_back
        final_commit = commit_sha
        final_influence = list(influence)

        if final_status is DevelopmentStatus.COMPLETED:
            learning = self._learn(request, target, plan, final_applied, verification)
            if learning is not None:
                final_influence.append(learning)
            if not final_rolled_back:
                try:
                    final_commit = repository.commit_local(
                        [item.path for item in final_applied],
                        f"feat(punto): {request.objective[:60]}",
                    )
                except RepositoryDenied as exc:
                    final_status = DevelopmentStatus.BLOCKED
                    error_kind = getattr(exc, "code", "COMMIT_DENIED")
                    error = exc.detail
                    self._log(
                        AuditEventType.DEV_CYCLE_BLOCKED,
                        "dev_commit_blocked",
                        request,
                        {"code": error_kind, "detail": error[:300]},
                        AuditResult.FAILURE,
                    )
                    rollback_done = self._rollback(
                        request, self._checkpoint, repository, final_applied
                    )
                    if rollback_done:
                        final_applied = ()
                        final_rolled_back = True

        result = DevelopmentResult(
            request_id=request.request_id,
            status=final_status,
            target_id=target.target_id,
            branch=repository.branch,
            plan=plan,
            plan_status=plan_status,
            plan_issues=plan_issues,
            change_issues=change_issues,
            applied=final_applied,
            verification=tuple(verification),
            repair_rounds=repair_rounds,
            structural_corrections=structural_corrections,
            context_requests_granted=tuple(granted),
            context_requests_denied=tuple(denied),
            checkpoint_id=checkpoint_id,
            rolled_back=final_rolled_back,
            commit_sha=final_commit,
            pell_status=retrieval.status.value,
            pell_influence=tuple(final_influence),
            provider=provider or self._last_provider,
            model=model or self._last_model,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_kind=error_kind,
            error=error,
            initial_scope=tuple(initial_scope),
            final_scope=tuple(final_scope),
            plan_versions=tuple(plan_versions),
            risk_envelopes=tuple(dict(item) for item in risk_envelopes),
            scope_expansions=tuple(scope_expansions),
            authority_decisions=tuple(authority_decisions),
            functional_chain_result=functional_chain_result,
            acceptance=tuple(acceptance),
            acceptance_result=acceptance_result,
            claims=tuple(claims),
            claims_result=claims_result,
            capabilities=(
                tuple(capabilities)
                if capabilities is not None
                else self._capability_evidence(self._capabilities)
            ),
            failovers=tuple(self._failovers),
            visual_evidence=tuple(self._visual_evidence),
        )
        self._log(
            AuditEventType.BUILD_CYCLE_COMPLETED,
            "dev_cycle_completed",
            request,
            {
                "status": result.status.value,
                "applied": len(result.applied),
                "commit_sha": result.commit_sha,
                "rolled_back": result.rolled_back,
                "repair_rounds": result.repair_rounds,
                "structural_corrections": result.structural_corrections,
                "verification_passed": [item.name for item in result.verification if item.passed],
                "authority": result.authority,
                "published": result.published,
            },
            AuditResult.SUCCESS if result.completed else AuditResult.FAILURE,
        )
        return result

    def _learn(
        self,
        request: BuildRequest,
        target: DevelopmentTarget,
        plan: DevelopmentPlan | None,
        applied: Sequence[AppliedChange],
        verification: Sequence[CommandEvidence],
    ) -> PellInfluence | None:
        """Registra el aprendizaje del ciclo y devuelve su influencia declarada.

        Solo entra a PELL una resolución **verificada** y con causa: la experiencia guarda el
        problema, las condiciones en las que se resolvió, el patrón de resolución, la evidencia y el
        resultado funcional. No se guarda «cambié el archivo X», y no se guarda nada de lo que el
        proveedor afirmó sin evidencia del entorno.
        """
        if self.store is None or not applied:
            return None
        rounds = sum(1 for item in applied if item.round_index > 0)
        evidence = [
            f"PILOT-05: {len(applied)} cambios aplicados y verificados en {target.target_id}",
            "verificación: "
            + ", ".join(f"{item.name}={item.exit_code}" for item in verification),
            "rutas: " + ", ".join(item.path for item in applied),
            f"causa raíz de la reparación: {self._last_root_cause or '(sin reparación)'}",
            f"cadena funcional: {self._functional_chain_result or 'NOT_DECLARED'}",
        ]
        conditions = (
            "condiciones: entorno local, alcance autorizado, verificación del entorno en verde, "
            f"reversible con checkpoint, sin secretos ni producción; riesgo {self._last_risk}; "
            f"plan v{len(self._plan_versions)} con "
            f"{len(self._final_plan.touched_paths()) if self._final_plan else 0} "
            "recursos; autoridad LOCAL_APPLY_ONLY sin publicación"
        )
        try:
            stored = self.store.record(
                problem=(
                    "completar una cadena funcional de desarrollo de bajo riesgo sin fronteras "
                    f"artificiales, en {target.target_id}"
                ),
                context=conditions,
                solution=(
                    "PUNTO evalúa el riesgo efectivo del plan y de cada cambio (operación, "
                    "recurso, alcance, reversibilidad, verificación, entorno, sensibilidad y "
                    "efectos externos) en vez de contar archivos; amplía el alcance solo con "
                    "evidencia causal y misma clase de riesgo; exige causa raíz en cada "
                    "reparación; corta el bucle si se estanca; verifica la cadena funcional "
                    "completa y confirma solo sus rutas"
                ),
                procedure=list(plan.touched_paths()) if plan else [],
                result=ExperienceResult.SUCCESS,
                verification=evidence,
                tags=[
                    "desarrollo",
                    "autoridad-adaptativa",
                    "riesgo",
                    "cadena-funcional",
                    "checkpoint",
                    "verificacion",
                ],
                status=ExperienceStatus.VERIFIED,
            )
        except Exception:  # la memoria no puede tumbar un ciclo que ya está verificado
            return None
        if rounds:
            self._log(
                AuditEventType.DEV_REPAIR_PROGRESS,
                "dev_learning_from_repair",
                request,
                {"experience_id": stored.id, "repaired_changes": rounds},
            )
        self._log(
            AuditEventType.DEV_PELL_INFLUENCE,
            "dev_pell_learned",
            request,
            {"experience_id": stored.id, "decision_point": "aprendizaje del ciclo"},
        )
        return PellInfluence(
            experience_id=stored.id,
            decision_point="aprendizaje posterior al ciclo",
            how_used="se registra la evidencia del ciclo como conocimiento reutilizable",
            observable_effect=(
                f"{len(applied)} cambios verificados quedan como experiencia VERIFIED"
            ),
        )

    def _blocked(
        self,
        request: BuildRequest,
        code: str,
        detail: str,
        started: float,
        target: DevelopmentTarget | None = None,
        *,
        rule: str = "",
        resource: str = "",
        remedy: str = "",
    ) -> DevelopmentResult:
        """Cierra el ciclo cuando la frontera impide empezar.

        La evidencia del bloqueo se escribe aquí, en el punto de decisión, con lo que la frontera
        declaró: es la que el dashboard muestra cuando una persona pulsa «Ver». Si la frontera no
        declara regla, recurso o acción, quedan vacíos: no se rellenan por suposición.
        """
        self._log(
            AuditEventType.DEV_CYCLE_BLOCKED,
            "dev_cycle_blocked",
            request,
            {
                "code": code,
                "detail": detail[:300],
                "rule": rule[:300],
                "resource": resource[:300],
            },
            AuditResult.FAILURE,
        )
        return DevelopmentResult(
            request_id=request.request_id,
            status=DevelopmentStatus.BLOCKED,
            target_id=target.target_id if target else request.target_repository,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_kind=code,
            error=detail[:1_000],
            blocked=BlockedEvidence(
                code=code[:60],
                detail=detail[:1_000],
                rule=rule[:300],
                resource=resource[:300],
                remedy=remedy[:300],
            ),
        )

    # ------------------------------------------------------------- auditoría
    def _log(
        self,
        event_type: AuditEventType,
        action: str,
        request: BuildRequest,
        metadata: Mapping[str, Any],
        result: AuditResult = AuditResult.SUCCESS,
    ) -> None:
        """Emite un evento del ciclo por ``request_id``."""
        if self.audit is None:
            return
        self.audit.log_dev_event(
            event_type,
            action,
            request_id=request.request_id,
            metadata=dict(metadata),
            result=result,
            actor=self.actor,
        )


def _evidence(name: str, argv: Sequence[str], result: CommandResult) -> CommandEvidence:
    """Traduce un ``CommandResult`` a evidencia del ciclo."""
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return CommandEvidence(
        name=name,
        argv=tuple(argv),
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        output_excerpt=output[-4_000:],
        truncated=len(output) > 4_000,
        timed_out=result.timed_out,
        passed=result.exit_code == 0 and not result.timed_out,
    )


def _json_object(text: str) -> Mapping[str, Any] | None:
    """Extrae el primer objeto JSON de una respuesta del proveedor, o ``None``."""
    if not text or not text.strip():
        return None
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced is not None:
        candidate = fenced.group(1).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, Mapping) else None


def _plan_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normaliza la respuesta del ARCHITECT a los campos del contrato del plan.

    Un proveedor puede añadir campos de más (``notes``, ``explanation``…) y puede devolver una lista
    de textos como objetos de una sola clave (``[{"risk": "..."}]``) o un único texto suelto en vez
    de una lista. Eso es una variación de forma **benigna e interpretable**: se normaliza en vez
    de tirar el plan entero, porque rechazar un plan correcto por cómo empaqueta sus frases
    dejaría al ciclo sin trabajo y sin motivo. Lo que no se interpreta se descarta aquí y lo
    valida el contrato.
    """
    allowed = set(DevelopmentPlan.model_fields)
    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        if key in _TEXT_TUPLE_FIELDS or key in _PATH_TUPLE_FIELDS:
            normalized[key] = _as_text_tuple(value)
        else:
            normalized[key] = value
    return normalized


def _as_text_tuple(value: Any) -> tuple[str, ...]:
    """Convierte una lista (o un texto suelto) en una tupla de textos.

    Acepta ``"x"``, ``["x", "y"]`` y ``[{"risk": "x"}]``: en el último caso la clave es una
    etiqueta del proveedor y el valor es el texto que declara.
    """
    if value is None:
        return ()
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        return ()
    texts: list[str] = []
    for item in items:
        if isinstance(item, str):
            texts.append(item)
            continue
        if isinstance(item, Mapping):
            for candidate in item.values():
                if isinstance(candidate, str):
                    texts.append(candidate)
                    break
    return tuple(texts)


def _context_requests(payload: Mapping[str, Any]) -> tuple[ContextRequest, ...]:
    """Interpreta las peticiones de contexto del BUILDER, ignorando las malformadas."""
    raw = payload.get("context_requests")
    if not isinstance(raw, list):
        return ()
    requests: list[ContextRequest] = []
    for item in raw[:8]:
        if not isinstance(item, Mapping):
            continue
        try:
            requests.append(ContextRequest.model_validate(dict(item)))
        except Exception:
            continue
    return tuple(requests)


#: Contrato de los cambios, escrito en el prompt además de en el ``json_schema``.
BUILD_CONTRACT: Final[str] = (
    "DELIVERABLE: a JSON object with EXACTLY these keys:\n"
    '{"summary": "one sentence", '
    '"changes": [{"path": "relative/path", "operation": "CREATE|MODIFY|DELETE|RENAME|MOVE", '
    '"source_path": "origin for RENAME/MOVE", '
    '"content": "the FULL new file content", "reason": "why", '
    '"acceptance_criterion": "which criterion it satisfies"}], '
    '"context_requests": [{"path": "relative/path", "reason": "why you need it"}], '
    '"root_cause": "REQUIRED when fixing a failure: its cause, not the symptom", '
    '"evidence": ["the verification output that proves the cause"], '
    '"expected_effect": "what will change once the cause is fixed", '
    '"unchanged_resources": [{"path": "relative/path", '
    '"evidence": "why it needs no change"}], '
    '"scope_expansion": {"trigger": "what revealed the need", '
    '"evidence": ["environment evidence"], "root_cause": "why it belongs to this objective", '
    '"resources": ["relative/path"], "operations": ["MODIFY"], '
    '"relationship": "why this resource is part of the same functional chain"}}\n'
    "Rules: only paths declared in the validated plan; DELETE/RENAME/MOVE must not carry content; "
    "every change needs the full file content and a reason; do not touch files outside the plan; "
    "do not include credentials. Ask for scope_expansion with evidence when the objective "
    "genuinely requires another resource: PUNTO evaluates it and decides; it is not yours to "
    "grant. You do not apply anything: PUNTO validates and applies. "
    + length_limits_text()
)

#: Contrato del plan, escrito en el prompt además de en el ``json_schema``.
PLAN_CONTRACT: Final[str] = (
    "DELIVERABLE: a plan for a small, verifiable change, as a JSON object with EXACTLY these "
    "keys:\n"
    '{"summary": "one sentence", '
    '"files_to_read": ["relative/path"], '
    '"files_to_modify": ["relative/path"], '
    '"files_to_create": ["relative/path"], '
    '"files_to_delete": ["relative/path"], '
    '"verification_commands": ["<nombre del catálogo>"], '
    '"risks": ["risk as text"], '
    '"acceptance_mapping": ["which acceptance criterion this satisfies"], '
    '"functional_chain": [{"step": "canonical source|consumers|behaviour|tests|build", '
    '"description": "what this link does", "verification": "<catalog name>"}]}\n'
    "Rules: every key is mandatory (use [] for none); every path is relative to the repository "
    "root and inside the allowed paths; files_to_modify must be non-empty; verification_commands "
    "must name catalog entries, never a shell command; functional_chain is MANDATORY when the plan "
    "touches more than one resource, and every link must cite a catalog verification that proves "
    "There is no fixed file budget: the number of files is a signal, and the authority comes from "
    "the risk of the change. You have no authority to apply the plan."
)

#: Instrucciones del worker: su papel no confiable y el formato de su entrega.
WORKER_INSTRUCTIONS: Final[str] = (
    "Eres un worker de PUNTO AI ENGINE. Produces JSON estructural para revisión y aplicación por "
    "PUNTO.\nReglas que no puedes cambiar:\n"
    "- no tienes autoridad: no aplicas cambios, no ejecutas comandos y no apruebas nada;\n"
    "- no afirmes que algo se ha aplicado: PUNTO valida y aplica, tú propones;\n"
    "- no incluyas credenciales, tokens ni DSN en tu respuesta y no pidas ficheros de secretos;\n"
    "- no propongas rutas fuera del alcance declarado ni borrados que no hagan falta;\n"
    "- responde SOLO con el objeto JSON pedido, sin texto alrededor."
)

#: Esquema del plan que se pide al ARCHITECT.
PLAN_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "files_to_read": {"type": "array", "items": {"type": "string"}},
        "files_to_modify": {"type": "array", "items": {"type": "string"}},
        "files_to_create": {"type": "array", "items": {"type": "string"}},
        "files_to_delete": {"type": "array", "items": {"type": "string"}},
        "verification_commands": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "acceptance_mapping": {"type": "array", "items": {"type": "string"}},
        "functional_chain": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string"},
                    "description": {"type": "string"},
                    "verification": {"type": "string"},
                },
                "required": ["step", "verification"],
            },
        },
    },
    "required": ["summary", "files_to_modify", "verification_commands", "acceptance_mapping"],
}

#: Esquema de los cambios que se piden al BUILDER.
BUILD_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "operation": {
                        "type": "string",
                        "enum": ["CREATE", "MODIFY", "DELETE", "RENAME", "MOVE"],
                    },
                    "source_path": {"type": "string"},
                    "content": {"type": "string"},
                    "expected_sha256": {"type": "string"},
                    "reason": {"type": "string"},
                    "acceptance_criterion": {"type": "string"},
                },
                "required": ["path", "operation"],
            },
        },
        "context_requests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["path"],
            },
        },
        "root_cause": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "expected_effect": {"type": "string"},
        "unchanged_resources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["path", "evidence"],
            },
        },
        "scope_expansion": {
            "type": "object",
            "properties": {
                "trigger": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "root_cause": {"type": "string"},
                "resources": {"type": "array", "items": {"type": "string"}},
                "operations": {"type": "array", "items": {"type": "string"}},
                "relationship": {"type": "string"},
            },
            "required": ["resources", "relationship", "evidence"],
        },
        "notes": {"type": "string"},
    },
    "required": ["changes"],
}


def _text_tuple(value: Any, *, limit: int = 40) -> tuple[str, ...]:
    """Interpreta una lista de textos (o un texto suelto) sin inventar contenido."""
    if value is None:
        return ()
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        return ()
    texts: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            texts.append(item.strip()[:400])
        if len(texts) >= limit:
            break
    return tuple(texts)


def _root_cause(payload: Mapping[str, Any]) -> tuple[str, tuple[str, ...], str]:
    """Extrae la hipótesis de causa raíz de una respuesta del BUILDER.

    Sin hipótesis no hay reparación: es la diferencia entre corregir la causa y cambiar cosas hasta
    que el test deje de quejarse.
    """
    cause = payload.get("root_cause")
    if isinstance(cause, Mapping):
        cause = cause.get("cause") or cause.get("detail") or ""
    root = str(cause or "").strip()[:400]
    evidence = _text_tuple(payload.get("evidence"), limit=10)
    expected = payload.get("expected_effect")
    if isinstance(expected, Mapping):
        expected = expected.get("effect") or ""
    return root, evidence, str(expected or "").strip()[:400]


def _scope_expansion(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Interpreta una petición de ampliación de alcance, si el BUILDER la declara."""
    raw = payload.get("scope_expansion")
    if not isinstance(raw, Mapping):
        return None
    return raw


#: Códigos con los que el ciclo pide autoridad humana para un cambio. Insistir con otra ronda no
#: cambia una decisión de autoridad: solo gasta proveedor, así que el ciclo se detiene aquí.
_HUMAN_REQUIRED_CHANGE_CODES: Final[frozenset[str]] = frozenset(
    {"CHANGE_REQUIRES_HUMAN", "CHANGE_OUTSIDE_AUTHORITY"}
)


def _bounded(items: Any, *, limit: int = MAX_PLAN_ITEMS) -> tuple[str, ...]:
    """Secuencia acotada al tope del esquema, para campos de evidencia con ``max_length``.

    Existe porque un límite superado debe producir una **decisión gobernada** (DENIED, HUMAN_GATE,
    ChangeRejected), nunca un ``ValidationError``: el motor no usa excepciones como política. Los
    totales reales viajan en la auditoría, que no está acotada.
    """
    return tuple(str(item) for item in (items or ()))[:limit]


def _failure_signature(verification: Sequence[CommandEvidence]) -> str:
    """Firma estable de un fallo: qué verificaciones fallaron y con qué salida.

    Se normaliza la primera línea significativa de cada salida para que dos intentos con el mismo
    error produzcan la misma firma, y un error distinto no.
    """
    parts: list[str] = []
    for item in verification:
        if item.passed:
            continue
        excerpt = (item.output_excerpt or "").strip().splitlines()
        head = ""
        for line in excerpt:
            candidate = line.strip()
            if candidate:
                head = candidate[:120]
                break
        parts.append(f"{item.name}:{item.exit_code}:{head}")
    return "|".join(parts) or "no-failure"


#: Etiqueta del handoff causal en el prompt del BUILDER: dice de dónde sale y qué hacer con él.
CAUSAL_HANDOFF_LABEL: Final[str] = (
    "CAUSAL HANDOFF (del plan ya validado por PUNTO; úsalo, no lo rederives): "
)


def causal_handoff(plan: DevelopmentPlan) -> str:
    """Handoff causal compacto del plan al BUILDER, en una línea JSON determinista.

    Transporta **solo** la parte operativa que hoy se perdía: el objetivo, los recursos del plan, la
    cadena funcional con su verificación por eslabón, qué observación demuestra cada criterio y las
    verificaciones del catálogo. No incluye riesgos, razonamiento, autoridad, PELL ni auditoría: es
    información para implementar, no un plan paralelo ni un ensayo.
    """
    payload = {
        "goal": plan.summary,
        "resources": list(plan.touched_paths()),
        "chain": [
            {"step": step.step, "verification": step.verification}
            for step in plan.functional_chain
        ],
        "done": list(plan.acceptance_mapping),
        "verify": list(plan.verification_commands),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def default_development_cycle(
    *,
    audit: AuditLogger | None = None,
    environ: Mapping[str, str] | None = None,
) -> DevelopmentCycle:
    """Compone el ciclo con los componentes que ya existen, sin construir ninguno nuevo."""
    registry = ProviderRegistry()
    return DevelopmentCycle(
        router=registry.router_instance(),
        targets=DevelopmentTargetRegistry.from_environment(environ),
        retriever=MemoryRetriever(ExperienceStore()),
        store=ExperienceStore(),
        audit=audit,
        policy_engine=PolicyEngine.from_config(),
        visual_capture=HeadlessBrowserCapture(),
        visual_interaction=BrowserInteraction(),
    )


__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "MAX_CONTEXT_FILES",
    "RELEVANT_SUFFIXES",
    "DevelopmentConfig",
    "DevelopmentCycle",
    "DevelopmentCycleError",
    "default_development_cycle",
]
