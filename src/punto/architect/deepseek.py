"""Architect real sobre DeepSeek (ENGINE-3).

Implementa :class:`~punto.architect.base.ArchitectRunner` reutilizando el
``DeepSeekClient`` de ENGINE-2: **no** se duplica cliente HTTP, ni autenticación, ni
reintentos de transporte.

Reparto de responsabilidades:

- el **modelo** propone la especificación, la arquitectura y el perfil de capacidades;
- **PUNTO** valida el contrato y los invariantes, y decide si la propuesta se acepta;
- un rechazo consume un intento de reparación; un fallo de red consume un reintento
  de transporte del cliente. Nunca se mezclan (§11);
- el **presupuesto de la petición** es vinculante: se ejecuta el mínimo entre los límites del
  runner y los de la petición, y el saldo de salida se reparte intento a intento, de modo que
  la autorización del workflow llega hasta la petición HTTP y no se queda en una comprobación
  posterior (hallazgos V605-01 y V605-02).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from punto.architect.base import (
    ArchitectLimits,
    ArchitectRequest,
    ArchitectRunner,
    ArchitectureOutcome,
)
from punto.architect.prompts import (
    ARCHITECT_FORMAT_REMINDER,
    ARCHITECT_PROMPT_VERSION,
    ARCHITECT_REPAIR_AFTER_PROVIDER_ERROR,
    ARCHITECT_REPAIR_AFTER_REJECTION,
    ARCHITECT_REPAIR_TEMPLATE,
    ARCHITECT_SYSTEM_PROMPT,
    ARCHITECT_USER_TEMPLATE,
)
from punto.planning.graph import (
    validate_architecture_plan,
    validate_capability_profile,
    validate_project_spec,
)
from punto.providers.base import accepts_output_budget
from punto.providers.deepseek import (
    DeepSeekClient,
    DeepSeekError,
    ModelCompletion,
    parse_proposal_json,
)
from punto.schemas.execution import ModelUsage
from punto.schemas.planning import (
    ArchitectureProposal,
    ModelExecutionSummary,
    ProjectIntent,
    ProjectPlanStatus,
)
from punto.tools.errors import PlanningLimitExceededError

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger

#: Motivos de bloqueo de la planificación por límites.
BLOCKED_ARCHITECT_ATTEMPTS: str = "MAX_ARCHITECT_ATTEMPTS_EXCEEDED"
BLOCKED_ARCHITECT_CALLS: str = "MAX_ARCHITECT_MODEL_CALLS_EXCEEDED"
BLOCKED_ARCHITECT_TOKENS: str = "MAX_ARCHITECT_TOKENS_EXCEEDED"


def _bullets(items: tuple[str, ...]) -> str:
    """Lista legible para el prompt, o un marcador explícito si está vacía."""
    if not items:
        return "(no declarado)"
    return "\n".join(f"- {item}" for item in items)


def _effective_limits(configured: ArchitectLimits, requested: ArchitectLimits) -> ArchitectLimits:
    """Combina el presupuesto del runner con el de la petición, campo por campo.

    La petición es **vinculante** (hallazgo V605-01): CAMUS calcula ahí la cota efectiva que el
    workflow autoriza, y el runner la ignoraba ejecutando su propio presupuesto, de modo que
    una autorización de una llamada acababa en cinco peticiones HTTP.

    La combinación es el **mínimo** de los dos, nunca la suma ni el máximo: la configuración
    del runner es un techo declarado y no puede ampliar lo que la petición autoriza. Si la
    petición pide menos, manda la petición; si pide más, manda el runner.
    """
    return ArchitectLimits(
        max_attempts=min(configured.max_attempts, requested.max_attempts),
        max_model_calls=min(configured.max_model_calls, requested.max_model_calls),
        max_input_tokens=min(configured.max_input_tokens, requested.max_input_tokens),
        max_output_tokens=min(configured.max_output_tokens, requested.max_output_tokens),
    )


class DeepSeekArchitectRunner(ArchitectRunner):
    """Architect que propone con DeepSeek y es validado por PUNTO."""

    def __init__(
        self,
        *,
        client: DeepSeekClient,
        audit: AuditLogger | None = None,
        limits: ArchitectLimits | None = None,
    ) -> None:
        self._client = client
        self._audit = audit
        self._limits = limits or ArchitectLimits()

    # ------------------------------------------------------------------ estado
    @property
    def name(self) -> str:
        """Nombre del runner."""
        return "DeepSeekArchitectRunner"

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
        return ARCHITECT_PROMPT_VERSION

    @property
    def uses_ai(self) -> bool:
        """Siempre ``True``: este runner consulta un modelo externo."""
        return True

    @property
    def limits(self) -> ArchitectLimits:
        """Presupuesto configurado (el runner puede declararlo, el modelo no)."""
        return self._limits

    # ------------------------------------------------------------------ diseño
    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Produce y valida la especificación, la arquitectura y el perfil.

        Los límites que gobiernan esta invocación son los efectivos —el mínimo entre los
        configurados del runner y los de la petición—, no los del runner a secas: la petición
        es la autorización del llamante y el runner no puede ampliarla.

        Nunca lanza por un fallo del trabajo: lo traduce a ``status`` con
        ``violations`` o ``error``.
        """
        limits = _effective_limits(self._limits, request.limits)
        usage = ModelUsage()
        model_calls = 0
        attempts_used = 0
        violations: tuple[str, ...] = ()
        error = ""
        status = ProjectPlanStatus.BLOCKED

        self._audit_request_started(request, limits.max_attempts)

        try:
            for attempt in range(1, limits.max_attempts + 1):
                attempts_used = attempt
                if model_calls >= limits.max_model_calls:
                    raise PlanningLimitExceededError(
                        BLOCKED_ARCHITECT_CALLS, model_calls, limits.max_model_calls
                    )

                prompt = (
                    self._initial_prompt(request.intent)
                    if attempt == 1
                    else self._repair_prompt(request.intent, violations, error)
                )

                # La autorización de salida de **esta** llamada es el saldo que queda tras lo
                # ya consumido: volver a enviar el total en cada intento multiplicaría el
                # gasto autorizado por el número de intentos.
                remaining_output = limits.max_output_tokens - usage.completion_tokens
                if remaining_output < 1:
                    raise PlanningLimitExceededError(
                        BLOCKED_ARCHITECT_TOKENS,
                        usage.completion_tokens,
                        limits.max_output_tokens,
                    )

                completion = self._call_model(request, attempt, prompt, remaining_output)
                model_calls += 1
                usage = usage.merged(completion.usage)
                self._assert_token_budget(usage, limits)

                proposal, violations = self._parse_and_validate(request, attempt, completion)
                if proposal is not None:
                    self._audit_plan_accepted(request, attempt, proposal, usage, model_calls)
                    return ArchitectureOutcome(
                        status=ProjectPlanStatus.PASS,
                        proposal=proposal,
                        summary=self._summary(model_calls, attempts_used, usage),
                    )

                error = "propuesta rechazada por invariantes deterministas"

            status = ProjectPlanStatus.BLOCKED
            error = (
                f"{BLOCKED_ARCHITECT_ATTEMPTS}: {attempts_used} intentos agotados sin "
                "una propuesta válida"
            )
        except PlanningLimitExceededError as exc:
            status = ProjectPlanStatus.BLOCKED
            error = str(exc)
        except DeepSeekError as exc:
            status = ProjectPlanStatus.FAILED
            error = self._client.redact(str(exc))
            violations = (error,)

        self._audit_plan_rejected(request, attempts_used, violations, error)
        return ArchitectureOutcome(
            status=status,
            summary=self._summary(model_calls, attempts_used, usage),
            violations=violations,
            error=error,
        )

    # --------------------------------------------------------------- internals
    def _summary(
        self, model_calls: int, attempts_used: int, usage: ModelUsage
    ) -> ModelExecutionSummary:
        """Resumen de consumo del rol, para la evidencia y la auditoría."""
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
        self,
        request: ArchitectRequest,
        attempt: int,
        prompt: str,
        max_output_tokens: int,
    ) -> ModelCompletion:
        """Llama al modelo y audita inicio, fin y fallo.

        ``max_output_tokens`` es el saldo de salida autorizado para esta invocación: viaja en
        la petición HTTP para que el proveedor no genere más de lo permitido, en vez de
        comprobarse después con el ``usage``, cuando el gasto ya ocurrió (hallazgo V605-02).
        Un cliente que no declare el parámetro —un doble de prueba anterior al hallazgo— se
        invoca como antes, sin el tope, y entonces el presupuesto se comprueba a posteriori.
        """
        self._audit_model_started(request, attempt, prompt)
        try:
            if accepts_output_budget(self._client):
                completion = self._client.complete_json(
                    system_prompt=ARCHITECT_SYSTEM_PROMPT,
                    user_prompt=prompt,
                    max_output_tokens=max_output_tokens,
                )
            else:
                completion = self._client.complete_json(
                    system_prompt=ARCHITECT_SYSTEM_PROMPT, user_prompt=prompt
                )
        except DeepSeekError as exc:
            self._audit_model_failed(request, attempt, self._client.redact(str(exc)))
            raise
        self._audit_model_completed(request, attempt, completion)
        return completion

    def _parse_and_validate(
        self, request: ArchitectRequest, attempt: int, completion: ModelCompletion
    ) -> tuple[ArchitectureProposal | None, tuple[str, ...]]:
        """Convierte la respuesta en propuesta y la somete a los invariantes.

        Returns:
            La propuesta válida, o ``None`` acompañada de todas las violaciones.
        """
        try:
            payload = parse_proposal_json(completion.content)
            proposal = ArchitectureProposal.model_validate(payload)
        except Exception as exc:  # se traduce a violaciones, nunca a excepción
            reason = f"contrato incumplido: {type(exc).__name__}: {exc}"
            self._audit_plan_rejected(request, attempt, (reason,), reason)
            return None, (reason,)

        self._audit_plan_received(request, attempt, proposal)

        violations = (
            validate_project_spec(proposal.project_spec)
            .merged(validate_architecture_plan(proposal.architecture))
            .merged(validate_capability_profile(proposal.capability_profile))
        )
        if not violations.valid:
            self._audit_plan_rejected(
                request, attempt, violations.violations, "invariantes incumplidos"
            )
            return None, violations.violations

        return proposal, ()

    def _assert_token_budget(self, usage: ModelUsage, limits: ArchitectLimits) -> None:
        """Comprueba el consumo acumulado contra los límites **efectivos** de la invocación."""
        if usage.prompt_tokens > limits.max_input_tokens:
            raise PlanningLimitExceededError(
                BLOCKED_ARCHITECT_TOKENS, usage.prompt_tokens, limits.max_input_tokens
            )
        if usage.completion_tokens > limits.max_output_tokens:
            raise PlanningLimitExceededError(
                BLOCKED_ARCHITECT_TOKENS, usage.completion_tokens, limits.max_output_tokens
            )

    def _initial_prompt(self, intent: ProjectIntent) -> str:
        """Petición de diseño inicial."""
        return ARCHITECT_USER_TEMPLATE.format(
            format_reminder=ARCHITECT_FORMAT_REMINDER,
            **self._intent_fields(intent),
        )

    def _repair_prompt(
        self, intent: ProjectIntent, violations: tuple[str, ...], error: str
    ) -> str:
        """Petición de reparación con las violaciones concretas."""
        situation = (
            ARCHITECT_REPAIR_AFTER_REJECTION
            if violations
            else ARCHITECT_REPAIR_AFTER_PROVIDER_ERROR
        )
        listed = violations or ((error or "sin detalle"),)
        return ARCHITECT_REPAIR_TEMPLATE.format(
            situation=situation,
            violations="\n".join(f"- {item}" for item in listed),
            format_reminder=ARCHITECT_FORMAT_REMINDER,
            **self._intent_fields(intent),
        )

    @staticmethod
    def _intent_fields(intent: ProjectIntent) -> dict[str, str]:
        """Campos de la intención tal como los espera la plantilla."""
        return {
            "name": intent.name,
            "description": intent.description,
            "business_goal": intent.business_goal or "(no declarado)",
            "target_users": _bullets(intent.target_users),
            "core_capabilities": _bullets(intent.core_capabilities),
            "constraints": _bullets(intent.constraints),
            "preferred_stack": _bullets(intent.preferred_stack),
            "deployment_preferences": _bullets(intent.deployment_preferences),
            "non_functional_requirements": _bullets(intent.non_functional_requirements),
            "known_integrations": _bullets(intent.known_integrations),
            "budget_constraints": _bullets(intent.budget_constraints),
            "human_notes": intent.human_notes or "(sin notas)",
        }

    # --------------------------------------------------------------- auditoría
    def _audit_request_started(self, request: ArchitectRequest, max_attempts: int) -> None:
        if self._audit is None:
            return
        self._audit.log_architect_request_started(
            project_id=request.project_id,
            project_name=request.intent.name,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            max_attempts=max_attempts,
        )

    def _audit_plan_received(
        self, request: ArchitectRequest, attempt: int, proposal: ArchitectureProposal
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_architect_plan_received(
            project_id=request.project_id,
            attempt=attempt,
            components=len(proposal.architecture.components),
            requirements=len(proposal.project_spec.requirement_ids),
            capabilities=len(proposal.capability_profile.entries()),
            open_questions=len(proposal.project_spec.open_questions),
        )

    def _audit_plan_accepted(
        self,
        request: ArchitectRequest,
        attempt: int,
        proposal: ArchitectureProposal,
        usage: ModelUsage,
        model_calls: int,
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_architect_plan_accepted(
            project_id=request.project_id,
            attempt=attempt,
            style=proposal.architecture.architecture_style,
            model_calls=model_calls,
            total_tokens=usage.total_tokens,
        )

    def _audit_plan_rejected(
        self,
        request: ArchitectRequest,
        attempt: int,
        violations: tuple[str, ...],
        detail: str,
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_architect_plan_rejected(
            project_id=request.project_id,
            attempt=attempt,
            violations=violations,
            detail=detail,
        )

    def _audit_model_started(
        self, request: ArchitectRequest, attempt: int, prompt: str
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
        self, request: ArchitectRequest, attempt: int, completion: ModelCompletion
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

    def _audit_model_failed(self, request: ArchitectRequest, attempt: int, detail: str) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_failed(
            task_id=request.project_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            error=detail,
        )


__all__ = [
    "BLOCKED_ARCHITECT_ATTEMPTS",
    "BLOCKED_ARCHITECT_CALLS",
    "BLOCKED_ARCHITECT_TOKENS",
    "DeepSeekArchitectRunner",
]
