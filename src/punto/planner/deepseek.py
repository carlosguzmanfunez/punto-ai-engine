"""Planner real sobre DeepSeek (ENGINE-3).

Implementa :class:`~punto.planner.base.PlannerRunner` reutilizando el
``DeepSeekClient`` de ENGINE-2.

El Planner **propone** milestones, epics y tareas. PUNTO hace el resto, de forma
determinista:

1. **cablea** las relaciones que el modelo no declara: ``Epic.task_ids`` sale de
   ``task.epic_id`` y ``Milestone.epic_ids`` sale de ``epic.milestone_id``; así una
   referencia inventada no puede colarse en el plan;
2. construye el :class:`~punto.schemas.planning.TaskGraph`;
3. valida roadmap y grafo (invariantes, DAG, ciclos, capacidades declaradas);
4. si algo falla, devuelve las violaciones al modelo como evidencia y consume un
   intento de reparación.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from punto.planner.base import (
    PlannerLimits,
    PlannerRequest,
    PlannerRunner,
    PlanningOutcome,
)
from punto.planner.prompts import (
    PLANNER_FORMAT_REMINDER,
    PLANNER_PROMPT_VERSION,
    PLANNER_REPAIR_AFTER_PROVIDER_ERROR,
    PLANNER_REPAIR_AFTER_REJECTION,
    PLANNER_REPAIR_TEMPLATE,
    PLANNER_SYSTEM_PROMPT,
    PLANNER_USER_TEMPLATE,
)
from punto.planning.graph import (
    planning_safe_text,
    validate_roadmap,
    validate_task_graph,
)
from punto.providers.deepseek import (
    DeepSeekClient,
    DeepSeekError,
    ModelCompletion,
    parse_proposal_json,
)
from punto.schemas.execution import ModelUsage
from punto.schemas.planning import (
    Epic,
    Milestone,
    ModelExecutionSummary,
    PlannerProposal,
    ProjectPlanStatus,
    Roadmap,
    TaskGraph,
)
from punto.tools.errors import PlanningLimitExceededError

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger

#: Motivos de bloqueo de la planificación de trabajo por límites.
BLOCKED_PLANNER_ATTEMPTS: str = "MAX_PLANNER_ATTEMPTS_EXCEEDED"
BLOCKED_PLANNER_CALLS: str = "MAX_PLANNER_MODEL_CALLS_EXCEEDED"
BLOCKED_PLANNER_TOKENS: str = "MAX_PLANNER_TOKENS_EXCEEDED"


def _bullets(items: tuple[str, ...]) -> str:
    """Lista legible para el prompt, o un marcador explícito si está vacía."""
    if not items:
        return "(no declarado)"
    return "\n".join(f"- {item}" for item in items)


class DeepSeekPlannerRunner(PlannerRunner):
    """Planner que propone con DeepSeek y es validado por PUNTO."""

    def __init__(
        self,
        *,
        client: DeepSeekClient,
        audit: AuditLogger | None = None,
        limits: PlannerLimits | None = None,
    ) -> None:
        self._client = client
        self._audit = audit
        self._limits = limits or PlannerLimits()

    # ------------------------------------------------------------------ estado
    @property
    def name(self) -> str:
        """Nombre del runner."""
        return "DeepSeekPlannerRunner"

    @property
    def provider(self) -> str:
        """Proveedor del modelo."""
        return "deepseek"

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return self._client.model

    @property
    def prompt_version(self) -> str:
        """Versión del prompt de sistema en uso."""
        return PLANNER_PROMPT_VERSION

    @property
    def uses_ai(self) -> bool:
        """Siempre ``True``: este runner consulta un modelo externo."""
        return True

    @property
    def limits(self) -> PlannerLimits:
        """Presupuesto configurado."""
        return self._limits

    # ----------------------------------------------------------- planificación
    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Produce y valida el roadmap y el grafo de tareas.

        Nunca lanza por un fallo del trabajo: lo traduce a ``status`` con
        ``violations`` o ``error``.
        """
        usage = ModelUsage()
        model_calls = 0
        attempts_used = 0
        violations: tuple[str, ...] = ()
        error = ""
        status = ProjectPlanStatus.BLOCKED

        self._audit_request_started(request)

        try:
            for attempt in range(1, self._limits.max_attempts + 1):
                attempts_used = attempt
                if model_calls >= self._limits.max_model_calls:
                    raise PlanningLimitExceededError(
                        BLOCKED_PLANNER_CALLS, model_calls, self._limits.max_model_calls
                    )

                prompt = (
                    self._initial_prompt(request)
                    if attempt == 1
                    else self._repair_prompt(request, violations, error)
                )

                completion = self._call_model(request, attempt, prompt)
                model_calls += 1
                usage = usage.merged(completion.usage)
                self._assert_token_budget(usage)

                roadmap, graph, violations = self._parse_and_validate(request, attempt, completion)
                if roadmap is not None and graph is not None:
                    self._audit_graph_accepted(request, attempt, graph, usage, model_calls)
                    return PlanningOutcome(
                        status=ProjectPlanStatus.PASS,
                        roadmap=roadmap,
                        task_graph=graph,
                        summary=self._summary(model_calls, attempts_used, usage),
                    )

                error = "roadmap rechazado por invariantes deterministas"

            status = ProjectPlanStatus.BLOCKED
            error = (
                f"{BLOCKED_PLANNER_ATTEMPTS}: {attempts_used} intentos agotados sin "
                "un roadmap válido"
            )
        except PlanningLimitExceededError as exc:
            status = ProjectPlanStatus.BLOCKED
            error = str(exc)
        except DeepSeekError as exc:
            status = ProjectPlanStatus.FAILED
            error = self._client.redact(str(exc))
            violations = (error,)

        self._audit_graph_rejected(request, attempts_used, violations, error)
        return PlanningOutcome(
            status=status,
            summary=self._summary(model_calls, attempts_used, usage),
            violations=violations,
            error=error,
        )

    # --------------------------------------------------------------- internals
    def _summary(
        self, model_calls: int, attempts_used: int, usage: ModelUsage
    ) -> ModelExecutionSummary:
        """Resumen de consumo del rol."""
        return ModelExecutionSummary(
            runner=self.name,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            model_calls=model_calls,
            attempts_used=attempts_used,
            usage=usage,
        )

    def _call_model(
        self, request: PlannerRequest, attempt: int, prompt: str
    ) -> ModelCompletion:
        """Llama al modelo y audita inicio, fin y fallo."""
        self._audit_model_started(request, attempt, prompt)
        try:
            completion = self._client.complete_json(
                system_prompt=PLANNER_SYSTEM_PROMPT, user_prompt=prompt
            )
        except DeepSeekError as exc:
            self._audit_model_failed(request, attempt, self._client.redact(str(exc)))
            raise
        self._audit_model_completed(request, attempt, completion)
        return completion

    def _parse_and_validate(
        self, request: PlannerRequest, attempt: int, completion: ModelCompletion
    ) -> tuple[Roadmap | None, TaskGraph | None, tuple[str, ...]]:
        """Convierte la respuesta en roadmap validado y grafo de tareas."""
        try:
            payload = parse_proposal_json(completion.content)
            proposal = PlannerProposal.model_validate(payload)
        except Exception as exc:  # se traduce a violaciones, nunca a excepción
            reason = f"contrato incumplido: {type(exc).__name__}: {exc}"
            self._audit_graph_rejected(request, attempt, (reason,), reason)
            return None, None, (reason,)

        self._audit_roadmap_received(request, attempt, proposal)

        roadmap = _assemble_roadmap(proposal)
        graph = TaskGraph(project_name=roadmap.project_name, tasks=roadmap.tasks)

        violations = validate_roadmap(
            roadmap, capability_profile=request.capability_profile
        ).merged(validate_task_graph(graph, roadmap=roadmap))
        if not violations.valid:
            self._audit_graph_rejected(
                request, attempt, violations.violations, "invariantes incumplidos"
            )
            return None, None, violations.violations

        return roadmap, graph, ()

    def _assert_token_budget(self, usage: ModelUsage) -> None:
        """Comprueba el presupuesto acumulado de tokens."""
        if usage.prompt_tokens > self._limits.max_input_tokens:
            raise PlanningLimitExceededError(
                BLOCKED_PLANNER_TOKENS, usage.prompt_tokens, self._limits.max_input_tokens
            )
        if usage.completion_tokens > self._limits.max_output_tokens:
            raise PlanningLimitExceededError(
                BLOCKED_PLANNER_TOKENS, usage.completion_tokens, self._limits.max_output_tokens
            )

    def _initial_prompt(self, request: PlannerRequest) -> str:
        """Petición de planificación inicial."""
        return PLANNER_USER_TEMPLATE.format(
            format_reminder=PLANNER_FORMAT_REMINDER,
            **self._spec_fields(request),
        )

    def _repair_prompt(
        self, request: PlannerRequest, violations: tuple[str, ...], error: str
    ) -> str:
        """Petición de reparación con las violaciones concretas."""
        situation = (
            PLANNER_REPAIR_AFTER_REJECTION
            if violations
            else PLANNER_REPAIR_AFTER_PROVIDER_ERROR
        )
        listed = violations or ((error or "sin detalle"),)
        return PLANNER_REPAIR_TEMPLATE.format(
            situation=situation,
            violations="\n".join(f"- {item}" for item in listed),
            format_reminder=PLANNER_FORMAT_REMINDER,
            **self._spec_fields(request),
        )

    @staticmethod
    def _spec_fields(request: PlannerRequest) -> dict[str, str]:
        """Campos de especificación, arquitectura y capacidades para la plantilla."""
        spec = request.project_spec
        architecture = request.architecture
        profile = request.capability_profile

        requirements = tuple(
            f"{item.id} [{item.priority.value}] {item.statement}"
            for item in spec.functional_requirements
        )
        non_functional = tuple(
            f"{item.id} [{item.priority.value}] {item.statement}"
            for item in spec.non_functional_requirements
        )
        components = tuple(
            f"{item.id} ({item.kind.value}) {item.name}: {item.responsibility}"
            for item in architecture.components
        )
        data_stores = tuple(
            f"{item.id} {item.engine}: {item.purpose}" for item in architecture.data_stores
        )
        interfaces = tuple(
            f"{item.id} ({item.kind.value}) {item.name}: {item.description}"
            for item in architecture.interfaces
        )
        choices = tuple(
            f"{item.topic}: {item.choice}" for item in architecture.technology_choices
        )
        # El vocabulario se lista SIN prefijo de familia: mostrar «LANGUAGE: typescript»
        # invitaba al modelo a copiar el prefijo en "required_capabilities", y PUNTO lo
        # rechazaba. El prefijo era una trampa del prompt, no un error del modelo.
        capability_profile = "\n".join(f"- {name}" for _kind, name in profile.entries())
        safe = planning_safe_text
        return {
            "project_name": safe(spec.project_name),
            "problem_statement": safe(spec.problem_statement),
            "product_goals": _bullets(spec.product_goals),
            "target_users": _bullets(spec.target_users),
            "functional_requirements": _bullets(requirements),
            "non_functional_requirements": _bullets(non_functional),
            "constraints": _bullets(spec.constraints),
            "out_of_scope": _bullets(spec.out_of_scope),
            "success_criteria": _bullets(spec.success_criteria),
            "architecture_style": safe(architecture.architecture_style),
            "components": _bullets(components),
            "data_stores": _bullets(data_stores),
            "interfaces": _bullets(interfaces),
            "deployment_topology": safe(architecture.deployment_topology),
            "testing_strategy": _bullets(architecture.testing_strategy),
            "technology_choices": _bullets(choices),
            "capability_profile": capability_profile or "  (sin capacidades declaradas)",
        }

    # --------------------------------------------------------------- auditoría
    def _audit_request_started(self, request: PlannerRequest) -> None:
        if self._audit is None:
            return
        self._audit.log_planner_request_started(
            project_id=request.project_id,
            project_name=request.project_spec.project_name,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            max_attempts=self._limits.max_attempts,
        )

    def _audit_roadmap_received(
        self, request: PlannerRequest, attempt: int, proposal: PlannerProposal
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_roadmap_received(
            project_id=request.project_id,
            attempt=attempt,
            milestones=len(proposal.milestones),
            epics=len(proposal.epics),
            tasks=len(proposal.tasks),
        )

    def _audit_graph_accepted(
        self,
        request: PlannerRequest,
        attempt: int,
        graph: TaskGraph,
        usage: ModelUsage,
        model_calls: int,
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_task_graph_accepted(
            project_id=request.project_id,
            attempt=attempt,
            tasks=len(graph.tasks),
            ready_tasks=len(graph.ready_tasks()),
            model_calls=model_calls,
            total_tokens=usage.total_tokens,
        )

    def _audit_graph_rejected(
        self,
        request: PlannerRequest,
        attempt: int,
        violations: tuple[str, ...],
        detail: str,
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_task_graph_rejected(
            project_id=request.project_id,
            attempt=attempt,
            violations=violations,
            detail=detail,
        )

    def _audit_model_started(
        self, request: PlannerRequest, attempt: int, prompt: str
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_started(
            task_id=request.project_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            prompt_chars=len(prompt),
        )

    def _audit_model_completed(
        self, request: PlannerRequest, attempt: int, completion: ModelCompletion
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_completed(
            task_id=request.project_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
            total_tokens=completion.usage.total_tokens,
            latency_ms=completion.latency_ms,
            transport_retries=completion.transport_retries,
        )

    def _audit_model_failed(self, request: PlannerRequest, attempt: int, detail: str) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_failed(
            task_id=request.project_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            error=detail,
        )


def _assemble_roadmap(proposal: PlannerProposal) -> Roadmap:
    """Cablea el roadmap: el modelo propone tareas y epics, PUNTO deriva los enlaces.

    ``Epic.task_ids`` y ``Milestone.epic_ids`` se calculan aquí desde ``epic_id`` y
    ``milestone_id``. El modelo no puede declarar una relación que no exista.
    """
    epics: list[Epic] = []
    for epic in proposal.epics:
        task_ids = tuple(task.id for task in proposal.tasks if task.epic_id == epic.id)
        epics.append(epic.model_copy(update={"task_ids": task_ids}))

    milestones: list[Milestone] = []
    for milestone in proposal.milestones:
        epic_ids = tuple(epic.id for epic in epics if epic.milestone_id == milestone.id)
        milestones.append(milestone.model_copy(update={"epic_ids": epic_ids}))

    return Roadmap(
        project_name=proposal.project_name,
        milestones=tuple(milestones),
        epics=tuple(epics),
        tasks=proposal.tasks,
    )


__all__ = [
    "BLOCKED_PLANNER_ATTEMPTS",
    "BLOCKED_PLANNER_CALLS",
    "BLOCKED_PLANNER_TOKENS",
    "DeepSeekPlannerRunner",
]
