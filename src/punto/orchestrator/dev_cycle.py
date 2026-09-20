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

import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from punto.audit.logger import AuditLogger
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
from punto.policy.policy_engine import PolicyEngine
from punto.providers.contract import (
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
    AppliedChange,
    ChangeOperation,
    CommandEvidence,
    ContextRequest,
    DevelopmentPlan,
    DevelopmentResult,
    DevelopmentStatus,
    FileChangeProposal,
    PellInfluence,
    PlanStatus,
    RepositoryOperation,
)
from punto.schemas.enums import AuditResult
from punto.schemas.execution import CommandResult
from punto.schemas.repair import RepairSnapshot
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
    """Límites del ciclo: los fija PUNTO, no la solicitud."""

    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    max_context_files: int = MAX_CONTEXT_FILES
    max_context_file_chars: int = MAX_CONTEXT_FILE_CHARS
    max_context_total_chars: int = MAX_CONTEXT_TOTAL_CHARS
    max_files_changed: int = 12
    max_repair_rounds: int = 3
    max_context_rounds: int = 2


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
    _snapshots: FileRepairSnapshots | None = field(default=None, init=False, repr=False)
    _checkpoint: RepairSnapshot | None = field(default=None, init=False, repr=False)
    _last_provider: str = field(default="", init=False, repr=False)
    _last_model: str = field(default="", init=False, repr=False)

    # ------------------------------------------------------------------ público
    def run(self, request: BuildRequest) -> DevelopmentResult:
        """Ejecuta el ciclo completo sobre el destino de la solicitud."""
        started = time.perf_counter()
        target = self._target_or_none(request)
        if target is None:
            return self._blocked(
                request, "TARGET_NOT_REGISTERED", "el destino no está registrado", started
            )
        self._log(
            AuditEventType.BUILD_REQUEST_ACCEPTED,
            "dev_request_accepted",
            request,
            {"target_id": target.target_id, "role": request.requested_role.value},
        )
        try:
            repository = self._open_repository(target, request)
        except (RepositoryDenied, DevelopmentTargetError) as exc:
            return self._blocked(
                request, getattr(exc, "code", "BLOCKED"), str(exc), started, target
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
            )
        self._log(
            AuditEventType.DEV_PLAN_CREATED,
            "dev_plan_created",
            request,
            {
                "touched": list(plan.touched_paths()),
                "verification": list(plan.verification_commands),
            },
        )
        self._log(
            AuditEventType.DEV_PLAN_VALIDATED,
            "dev_plan_validated",
            request,
            {"touched": len(plan.touched_paths()), "risks": len(plan.risks)},
        )

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
            plan=plan,
            plan_status=PlanStatus.VALID,
            plan_issues=(),
            change_issues=outcome["change_issues"],
            retrieval=retrieval,
            started=started,
            applied=outcome["applied"],
            verification=outcome["verification"],
            repair_rounds=outcome["repair_rounds"],
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
                "acordado"
            )
        return repository

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
            issues = self._validate_plan(plan, target, request)
            envelope = self._envelope_issue(plan, repository)
            if envelope is not None:
                issues = (*issues, envelope)
            if not issues:
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
        if not plan.verification_commands:
            issues.append(
                BuildValidationIssue(
                    code="PLAN_WITHOUT_VERIFICATION",
                    detail="un plan que no declara cómo se verifica no se aplica",
                )
            )
        if request.acceptance_criteria and not plan.acceptance_mapping:
            issues.append(
                BuildValidationIssue(
                    code="PLAN_WITHOUT_ACCEPTANCE",
                    detail="el plan no mapea ningún criterio de aceptación de la solicitud",
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
                    f"el plan toca {len(touched)} fichero(s) y la autoridad del motor no "
                    f"alcanza para confirmarlos: {reason}"
                )[:300],
            )
        return None

    # ------------------------------------------------------------ build + apply
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

        context_files: list[ContextFile] = list(inventory["selected"])
        granted: list[str] = []
        denied: list[str] = []
        change_issues: tuple[BuildValidationIssue, ...] = ()
        applied: list[AppliedChange] = []
        verification: list[CommandEvidence] = []
        checkpoint: RepairSnapshot | None = None
        rounds = 0
        provider = ""
        model = ""
        failure_evidence = ""

        for _ in range(self.config.max_repair_rounds + 1):
            prompt = self._build_prompt(
                request, target, plan, context_files, pell_block, failure_evidence
            )
            result = self._invoke(ProviderRole.BUILDER, request, prompt, BUILD_SCHEMA)
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
                if rounds >= self.config.max_repair_rounds:
                    return self._outcome(
                        status=DevelopmentStatus.CHANGE_REJECTED,
                        error_kind="CHANGE_REJECTED",
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
                applied.extend(self._apply(repository, validated, round_index=rounds))
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
                )
            if rounds > 0:
                self._log(
                    AuditEventType.DEV_REPAIR_COMPLETED,
                    "dev_repair_completed",
                    request,
                    {"round": rounds, "changes": len(validated)},
                )
            verification = self._verify(repository, target, plan, request)
            if all(item.passed for item in verification):
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
                )
            failure_evidence = self._failure_evidence(verification)
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
        return self._outcome(
            status=DevelopmentStatus.VERIFICATION_FAILED,
            error_kind="VERIFICATION_FAILED",
            error="la verificación no pasó tras las rondas de reparación",
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
        """Valida cada cambio: alcance, operación, política, huella y secretos."""
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
            if path not in declared:
                issues.append(
                    BuildValidationIssue(
                        code="CHANGE_NOT_IN_PLAN",
                        detail=f"{path!r} no está en el plan validado: no se aplica",
                    )
                )
                continue
            if (
                proposal.operation is ChangeOperation.DELETE
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
                if proposal.expected_sha256 and proposal.expected_sha256 != current:
                    issues.append(
                        BuildValidationIssue(
                            code="CHANGE_STALE",
                            detail=f"{path!r}: el fichero cambió desde que se leyó",
                        )
                    )
                    continue
                if proposal.operation is not ChangeOperation.DELETE:
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
            validated.append(proposal)
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
        paths = [item.path.replace("\\", "/") for item in validated]
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
                    change = repository.delete_file(path, expected_sha256=proposal.expected_sha256)
                    digest = ""
                    written = 0
                else:
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
    ) -> str:
        """Contexto gobernado del BUILDER: plan validado, ficheros y evidencia del fallo."""
        lines = [
            f"TARGET: {target.target_id}",
            f"OBJECTIVE: {request.objective}",
            "VALIDATED PLAN: " + (plan.summary or "(sin resumen)"),
            "PLAN FILES TO MODIFY: " + (" | ".join(plan.files_to_modify) or "(ninguno)"),
            "PLAN FILES TO CREATE: " + (" | ".join(plan.files_to_create) or "(ninguno)"),
        ]
        if request.acceptance_criteria:
            lines.append("ACCEPTANCE CRITERIA: " + " | ".join(request.acceptance_criteria))
        for item in context_files:
            lines.append(f"\n===== {item.path} (sha256={item.sha256}) =====\n{item.content}")
        if pell_block.strip():
            lines.append(pell_block)
        if failure_evidence:
            lines.append("VERIFICATION FAILED AND MUST BE FIXED:\n" + failure_evidence)
        lines.append(
            "DELIVERABLE: the exact file changes as JSON. You write nothing yourself: PUNTO "
            "validates and applies them. Keep each change MINIMAL: modify only what the task "
            "needs and repeat the rest of the file unchanged. If you need another file, ask for "
            "it in context_requests with its path and a reason instead of inventing its content."
        )
        lines.append(BUILD_CONTRACT)
        return "\n".join(lines)

    def _invoke(
        self,
        role: ProviderRole,
        request: BuildRequest,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> ProviderResult:
        """Invoca a un rol por el router, con el JSON Schema declarado.

        Antes de invocar se registra **qué proveedor** atiende el rol según la configuración: es
        evidencia de la decisión, no autoridad, y deja el ciclo auditable por rol.
        """
        try:
            selected = self.router.get_provider_for_role(role)
        except Exception:  # la ausencia de asignación la reporta el router al invocar
            selected = ""
        self._log(
            AuditEventType.BUILD_PROVIDER_SELECTED,
            "dev_provider_selected",
            request,
            {"role": role.value, "provider": selected, "fallback": False},
        )
        provider_request = ProviderRequest(
            role=role,
            instructions=WORKER_INSTRUCTIONS,
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
        return result

    # ------------------------------------------------------------- resultado
    def _outcome(self, **kwargs: Any) -> dict[str, Any]:
        """Normaliza el diccionario interno que viaja entre las etapas del ciclo."""
        checkpoint = kwargs.get("checkpoint")
        return {
            "status": kwargs.get("status", DevelopmentStatus.BLOCKED),
            "applied": kwargs.get("applied", ()),
            "verification": kwargs.get("verification", ()),
            "repair_rounds": kwargs.get("repair_rounds", 0),
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
        """Registra el aprendizaje del ciclo y devuelve su influencia declarada."""
        if self.store is None or not applied:
            return None
        evidence = [
            f"PILOT-04: {len(applied)} cambios aplicados y verificados en {target.target_id}",
            "verificación: "
            + ", ".join(f"{item.name}={item.exit_code}" for item in verification),
            "rutas: " + ", ".join(item.path for item in applied),
        ]
        try:
            stored = self.store.record(
                problem=f"aplicar cambios reales de forma gobernada en {target.target_id}",
                context="PUNTO AI ENGINE, PILOT-04",
                solution=(
                    "el proveedor propone un plan y cambios estructurados; PUNTO valida alcance, "
                    "operación, política, huella y secretos, crea un checkpoint reversible antes "
                    "de escribir, aplica por una sola puerta, verifica con comandos del catálogo "
                    "y confirma solo sus propias rutas"
                ),
                procedure=list(plan.files_to_modify) if plan else [],
                result=ExperienceResult.SUCCESS,
                verification=evidence,
                tags=["desarrollo", "aplicacion", "gobernado", "checkpoint", "verificacion"],
                status=ExperienceStatus.VERIFIED,
            )
        except Exception:  # la memoria no puede tumbar un ciclo que ya está verificado
            return None
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
    ) -> DevelopmentResult:
        """Cierra el ciclo cuando la frontera impide empezar."""
        self._log(
            AuditEventType.DEV_CYCLE_BLOCKED,
            "dev_cycle_blocked",
            request,
            {"code": code, "detail": detail[:300]},
            AuditResult.FAILURE,
        )
        return DevelopmentResult(
            request_id=request.request_id,
            status=DevelopmentStatus.BLOCKED,
            target_id=target.target_id if target else request.target_repository,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_kind=code,
            error=detail[:1_000],
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
    '"changes": [{"path": "relative/path", "operation": "CREATE|MODIFY|DELETE", '
    '"content": "the FULL new file content", "reason": "why", '
    '"acceptance_criterion": "which criterion it satisfies"}], '
    '"context_requests": [{"path": "relative/path", "reason": "why you need it"}]}\n'
    "Rules: only paths declared in the validated plan; DELETE must not carry content; every change "
    "needs the full file content and a reason; do not touch files outside the plan; do not include "
    "credentials. You do not apply anything: PUNTO validates and applies."
)

#: Contrato del plan, escrito en el prompt además de en el ``json_schema``.
PLAN_CONTRACT: Final[str] = (
    "DELIVERABLE: a plan for a small, verifiable change, as a JSON object with EXACTLY these "
    "keys:\n"
    '{"summary": "one sentence", '
    '"files_to_read": ["relative/path"], '
    '"files_to_modify": ["relative/path"], '
    '"files_to_create": ["relative/path"], '
    '"verification_commands": ["<nombre del catálogo>"], '
    '"risks": ["risk as text"], '
    '"acceptance_mapping": ["which acceptance criterion this satisfies"]}\n'
    "Rules: every key is mandatory (use [] for none); every path is relative to the repository "
    "root and inside the allowed paths; files_to_modify must be non-empty; verification_commands "
    "must name catalog entries, never a shell command. You have no authority to apply the plan."
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
        "verification_commands": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "acceptance_mapping": {"type": "array", "items": {"type": "string"}},
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
                    "operation": {"type": "string", "enum": ["CREATE", "MODIFY", "DELETE"]},
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
        "notes": {"type": "string"},
    },
    "required": ["changes"],
}


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
