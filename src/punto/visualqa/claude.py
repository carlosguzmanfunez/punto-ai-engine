"""Visual QA real sobre Anthropic/Claude (ENGINE-5.3).

Reutiliza **todo** lo aprobado en ENGINE-5.2 y no duplica nada: el mismo ``AnthropicClient``, el
mismo contrato multimodal, la misma lógica de provider schema y las mismas reglas de proveedor
caído, negativa y error. Lo único nuevo es la materia prima: capturas en lugar de archivos.

Ciclo:

1. PUNTO verifica las capturas que va a enviar (bytes, hash, media type y presupuesto de
   imágenes) y las convierte en ``ImagePayload``;
2. Claude propone una ``VisualQAProposal``; PUNTO la valida;
3. PUNTO evalúa los **gates** —proveedor, capturas, hechos técnicos y hallazgos—;
4. PUNTO calcula el veredicto. Claude **no** lo escribe y no puede anular un fallo determinista.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from punto.common import utc_now
from punto.providers.anthropic import AnthropicClient
from punto.providers.base import (
    PROVIDER_ANTHROPIC,
    ImageLimits,
    ImagePayload,
    ImageValidationError,
    ModelCompletion,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRefusalError,
)
from punto.providers.json_schema import SchemaValidationError, provider_schema_for
from punto.schemas.execution import ModelUsage
from punto.schemas.visual import (
    VisualQAGate,
    VisualQAProposal,
    VisualQAReport,
    VisualQAStatus,
    VisualQATask,
)
from punto.tools.errors import PlanningLimitExceededError, VisualQAError
from punto.visualqa.base import VisualQALimits, VisualQARunner
from punto.visualqa.gates import determine_visual_status, evaluate_visual_gates
from punto.visualqa.prompts import (
    VISUAL_FORMAT_REMINDER,
    VISUAL_PROMPT_VERSION,
    VISUAL_REPAIR_AFTER_PROVIDER_ERROR,
    VISUAL_REPAIR_AFTER_REJECTION,
    VISUAL_REPAIR_TEMPLATE,
    VISUAL_SYSTEM_PROMPT,
    VISUAL_USER_TEMPLATE,
)
from punto.visualqa.validation import validate_visual_proposal

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger

#: Motivos de bloqueo.
BLOCKED_VISUAL_ATTEMPTS: str = "MAX_VISUAL_ATTEMPTS_EXCEEDED"
BLOCKED_VISUAL_CALLS: str = "MAX_VISUAL_MODEL_CALLS_EXCEEDED"
BLOCKED_VISUAL_TOKENS: str = "MAX_VISUAL_TOKENS_EXCEEDED"
BLOCKED_VISUAL_PROVIDER_UNAVAILABLE: str = "PROVIDER_UNAVAILABLE"
BLOCKED_VISUAL_PROVIDER_REFUSAL: str = "PROVIDER_REFUSAL"
BLOCKED_VISUAL_PROVIDER_ERROR: str = "PROVIDER_ERROR"
BLOCKED_VISUAL_IMAGES: str = "VISUAL_IMAGE_BUDGET_EXCEEDED"
BLOCKED_VISUAL_SCHEMA: str = "VISUAL_SCHEMA_INVALID"


def _bullets(items: Sequence[str]) -> str:
    """Lista legible para el prompt, o un marcador explícito si está vacía."""
    if not items:
        return "- (ninguno)"
    return "\n".join(f"- {item}" for item in items)


class VisualQAProposalError(VisualQAError):
    """No se pudo obtener una propuesta visual válida."""

    def __init__(self, violations: tuple[str, ...]) -> None:
        self.violations = violations
        detail = "; ".join(violations[:3]) if violations else "sin detalle"
        super().__init__(f"{BLOCKED_VISUAL_ATTEMPTS}: {detail}")


class ClaudeVisualQARunner(VisualQARunner):
    """Visual QA real: propone Claude, decide PUNTO."""

    def __init__(
        self,
        *,
        client: AnthropicClient,
        audit: AuditLogger | None = None,
        limits: VisualQALimits | None = None,
        image_limits: ImageLimits | None = None,
    ) -> None:
        self._client = client
        self._audit = audit
        self._limits = limits or VisualQALimits()
        self._image_limits = image_limits or ImageLimits()

    # ------------------------------------------------------------------ estado
    @property
    def name(self) -> str:
        """Nombre del runner."""
        return "ClaudeVisualQARunner"

    @property
    def provider(self) -> str:
        """Proveedor del modelo visual."""
        return PROVIDER_ANTHROPIC

    @property
    def model(self) -> str:
        """Modelo visual configurado."""
        return self._client.model

    @property
    def prompt_version(self) -> str:
        """Versión del prompt visual en uso."""
        return VISUAL_PROMPT_VERSION

    @property
    def uses_ai(self) -> bool:
        """Siempre ``True``."""
        return True

    @property
    def limits(self) -> VisualQALimits:
        """Presupuesto configurado."""
        return self._limits

    @property
    def image_limits(self) -> ImageLimits:
        """Presupuesto de imágenes configurado."""
        return self._image_limits

    # --------------------------------------------------------------- evaluación
    def evaluate(
        self, task: VisualQATask, screenshots: Mapping[str, ImagePayload]
    ) -> VisualQAReport:
        """Evalúa la interfaz y calcula el veredicto.

        Nunca lanza por un fallo de la evaluación: lo traduce a un ``VisualQAReport``. Los gates
        se evalúan **siempre**, incluso si el proveedor falla: un Claude caído no convierte una
        página rota en un PASS.
        """
        started_at = utc_now()
        usage = ModelUsage()
        model_calls = 0
        attempts = 0
        proposal: VisualQAProposal | None = None
        violations: tuple[str, ...] = ()
        error = ""
        proposal_missing = False
        provider_responded = False

        required = tuple(
            artifact.logical_name for artifact in task.screenshots
        )
        sent = tuple(
            name for name in required if name in screenshots
        )

        self._audit_request_started(task, required)

        #: Capturas que de verdad se analizaron. Si el presupuesto de imágenes aborta antes de
        #: construir la petición, no se analizó ninguna: informar de nueve sería mentir.
        analyzed = sent
        try:
            self._assert_image_budget(required)
        except VisualQAError as exc:
            proposal_missing = True
            error = str(exc)
            analyzed = ()
        else:
            try:
                (
                    proposal,
                    attempts,
                    model_calls,
                    usage,
                    provider_responded,
                ) = self._obtain_proposal(task, screenshots, usage, model_calls)
            except PlanningLimitExceededError as exc:
                proposal_missing = True
                error = self._safe(str(exc))
            except ProviderAuthenticationError as exc:
                proposal_missing = True
                error = f"{BLOCKED_VISUAL_PROVIDER_UNAVAILABLE}: {self._safe(str(exc))}"
            except ProviderRefusalError as exc:
                proposal_missing = True
                error = f"{BLOCKED_VISUAL_PROVIDER_REFUSAL}: {self._safe(str(exc))}"
            except ProviderError as exc:
                proposal_missing = True
                error = f"{BLOCKED_VISUAL_PROVIDER_ERROR}: {self._safe(str(exc))}"
            except SchemaValidationError as exc:
                proposal_missing = True
                error = f"{BLOCKED_VISUAL_SCHEMA}: {self._safe(str(exc))}"
            except VisualQAProposalError as exc:
                proposal_missing = True
                violations = self._safe_violations(exc.violations)
                error = self._safe(str(exc))

        findings = () if proposal is None else proposal.findings
        blocking = sum(1 for finding in findings if finding.blocks)

        gates = evaluate_visual_gates(
            task.session,
            provider_responded=provider_responded,
            provider_detail=error if not provider_responded else "",
            required_screenshots=required,
            present_screenshots=sent,
            proposal_present=proposal is not None,
            blocking_findings=blocking,
            total_findings=len(findings),
        )
        status, reasons = determine_visual_status(gates)
        report = self._build_report(
            task=task,
            status=status,
            gates=gates,
            proposal=proposal,
            screenshots=analyzed,
            reasons=reasons,
            error=error if proposal_missing else "",
            violations=violations,
            model_calls=model_calls,
            attempts=attempts,
            usage=usage,
            started_at=started_at,
        )
        self._audit_completed(task, report)
        return report

    # ------------------------------------------------------------- presupuesto
    def _assert_image_budget(self, names: tuple[str, ...]) -> None:
        """Comprueba el presupuesto de imágenes **antes** de construir la petición.

        Raises:
            VisualQAError: si se piden más capturas de las que el contrato multimodal admite o
                si alguna supera el tamaño permitido.
        """
        if not names:
            raise VisualQAError(
                f"{BLOCKED_VISUAL_IMAGES}: la sesión no produjo ninguna captura que enviar"
            )
        if len(names) > self._image_limits.max_images:
            raise VisualQAError(
                f"{BLOCKED_VISUAL_IMAGES}: se exigen {len(names)} capturas y el contrato "
                f"multimodal admite {self._image_limits.max_images}: no se evalúa una parte "
                "haciéndola pasar por el todo"
            )

    # ------------------------------------------------------------------ propuesta
    def _obtain_proposal(
        self,
        task: VisualQATask,
        screenshots: Mapping[str, ImagePayload],
        usage: ModelUsage,
        model_calls: int,
    ) -> tuple[VisualQAProposal, int, int, ModelUsage, bool]:
        """Pide la propuesta visual y la valida, con reparación acotada."""
        images = self._collect_images(task, screenshots)
        names = frozenset(image.logical_name for image in images)
        check_kinds = frozenset(check.kind.value for check in task.session.checks)
        prompt = self._user_prompt(task, images)
        violations: tuple[str, ...] = ()

        for attempt in range(1, self._limits.max_attempts + 1):
            if model_calls >= self._limits.max_model_calls:
                raise PlanningLimitExceededError(
                    BLOCKED_VISUAL_CALLS, model_calls, self._limits.max_model_calls
                )
            completion = self._call_model(task, attempt, prompt, images)
            model_calls += 1
            usage = usage.merged(completion.usage)
            self._assert_token_budget(usage)

            try:
                payload = _parse_json(completion.content)
                proposal = VisualQAProposal.model_validate(payload)
            except (ValueError, ValidationError) as exc:
                violations = (
                    f"contrato incumplido: {type(exc).__name__}: {self._safe(str(exc))}",
                )
                self._audit_proposal_rejected(task, attempt, violations)
                prompt = self._repair_prompt(task, violations, _AFTER_REJECTION)
                continue

            self._audit_proposal_received(task, attempt, proposal)
            validation = validate_visual_proposal(
                proposal,
                task,
                screenshot_names=names,
                check_kinds=check_kinds,
            )
            if not validation.valid:
                violations = self._safe_violations(validation.violations)
                self._audit_proposal_rejected(task, attempt, violations)
                prompt = self._repair_prompt(
                    task,
                    violations,
                    _AFTER_REJECTION if violations else _AFTER_PROVIDER_ERROR,
                )
                continue

            self._audit_proposal_accepted(task, attempt, proposal)
            return proposal, attempt, model_calls, usage, True

        raise VisualQAProposalError(violations)

    def _collect_images(
        self, task: VisualQATask, screenshots: Mapping[str, ImagePayload]
    ) -> tuple[ImagePayload, ...]:
        """Reúne las imágenes en el orden de la sesión, sin aceptar rutas ni nombres libres."""
        images: list[ImagePayload] = []
        for artifact in task.screenshots:
            payload = screenshots.get(artifact.logical_name)
            if payload is None:
                continue
            if payload.logical_name != artifact.logical_name:
                raise ImageValidationError(
                    f"la imagen {payload.logical_name!r} no corresponde al artefacto "
                    f"{artifact.logical_name!r}"
                )
            if payload.size_bytes != artifact.bytes:
                raise ImageValidationError(
                    f"la imagen {artifact.logical_name!r} ocupa {payload.size_bytes} y el "
                    f"artefacto declara {artifact.bytes}"
                )
            images.append(payload)
        return tuple(images)

    # ------------------------------------------------------------------ modelo
    def _call_model(
        self,
        task: VisualQATask,
        attempt: int,
        prompt: str,
        images: tuple[ImagePayload, ...],
    ) -> ModelCompletion:
        """Llama al modelo visual con las capturas y el esquema real de la propuesta."""
        self._audit_model_started(task, attempt, prompt, len(images))
        try:
            completion = self._client.complete_multimodal_json(
                system_prompt=VISUAL_SYSTEM_PROMPT,
                user_prompt=prompt,
                images=images,
                limits=self._image_limits,
                json_schema=provider_schema_for(VisualQAProposal),
            )
        except ProviderError as exc:
            self._audit_model_failed(task, attempt, self._client.redact(str(exc)))
            raise
        self._audit_model_completed(task, attempt, completion)
        return completion

    def _assert_token_budget(self, usage: ModelUsage) -> None:
        """Comprueba el presupuesto acumulado de tokens."""
        if usage.prompt_tokens > self._limits.max_input_tokens:
            raise PlanningLimitExceededError(
                BLOCKED_VISUAL_TOKENS, usage.prompt_tokens, self._limits.max_input_tokens
            )
        if usage.completion_tokens > self._limits.max_output_tokens:
            raise PlanningLimitExceededError(
                BLOCKED_VISUAL_TOKENS,
                usage.completion_tokens,
                self._limits.max_output_tokens,
            )

    def _user_prompt(
        self, task: VisualQATask, images: tuple[ImagePayload, ...]
    ) -> str:
        """Petición visual: especificación, hechos medidos y el orden exacto de las capturas."""
        spec = task.spec
        session = task.session
        screenshot_lines = [
            f"- {image.logical_name} ({image.media_type}, {image.size_bytes} bytes)"
            for image in images
        ]
        return VISUAL_USER_TEMPLATE.format(
            format_reminder=VISUAL_FORMAT_REMINDER,
            objective=task.objective,
            acceptance_criteria=_bullets(task.acceptance_criteria),
            routes=_bullets(spec.routes),
            viewports=_bullets(
                tuple(f"{viewport.name.value} {viewport.width}x{viewport.height}"
                      for viewport in spec.viewports)
            ),
            required_elements=_bullets(
                tuple(f"{item.route}: {item.marker} ({item.description})"
                      for item in spec.required_elements)
            ),
            forbid_overflow="sí" if spec.forbid_horizontal_overflow else "no",
            responsive_expectations=_bullets(spec.responsive_expectations),
            accessibility_expectations=_bullets(spec.accessibility_expectations),
            content_expectations=_bullets(spec.content_expectations),
            visual_notes=_bullets(spec.visual_notes),
            technical_status=session.status.value,
            screenshots=_bullets(tuple(screenshot_lines)),
            checks=_bullets(
                tuple(
                    f"{check.kind.value}: {'PASS' if check.passed else 'FAIL'}"
                    f"{'' if check.ran else ' (no ejecutada)'} — {check.detail}"
                    for check in session.checks
                )
            ),
            findings=_bullets(
                tuple(
                    f"{finding.check.value} [{finding.severity.value}] {finding.route} "
                    f"{finding.message}"
                    for finding in session.findings
                )
            ),
            console_errors=_bullets(
                tuple(
                    message.text
                    for message in session.console
                    if message.level.lower() == "error"
                )
            ),
            page_errors=_bullets(session.page_errors),
            failed_resources=_bullets(
                tuple(
                    f"{resource.url} ({resource.reason})"
                    for resource in session.failed_resources
                )
            ),
            source_context=task.source_context.strip() or "(no disponible)",
            architecture_context=task.architecture_context.strip() or "(no disponible)",
        )

    def _repair_prompt(
        self, task: VisualQATask, violations: tuple[str, ...], situation: str
    ) -> str:
        """Petición de reparación, diciendo la verdad sobre el estado."""
        return VISUAL_REPAIR_TEMPLATE.format(
            situation=situation,
            violations="\n".join(f"- {item}" for item in violations) or "- sin detalle",
            format_reminder=VISUAL_FORMAT_REMINDER,
            objective=task.objective,
            routes=_bullets(task.spec.routes),
            viewports=_bullets(
                tuple(viewport.name.value for viewport in task.spec.viewports)
            ),
        )

    def _safe(self, text: str) -> str:
        """Sanea un texto antes de persistirlo o reportarlo."""
        return self._client.redact(text)

    def _safe_violations(self, violations: tuple[str, ...]) -> tuple[str, ...]:
        """Sanea una lista de violaciones para el informe y la auditoría."""
        return tuple(self._safe(item) for item in violations)

    # ------------------------------------------------------------------ salida
    def _build_report(
        self,
        *,
        task: VisualQATask,
        status: VisualQAStatus,
        gates: tuple[VisualQAGate, ...],
        proposal: VisualQAProposal | None,
        screenshots: tuple[str, ...],
        reasons: tuple[str, ...],
        error: str,
        violations: tuple[str, ...],
        model_calls: int,
        attempts: int,
        usage: ModelUsage,
        started_at: datetime,
    ) -> VisualQAReport:
        """Compone el informe de Visual QA."""
        findings = () if proposal is None else proposal.findings
        summary_parts = [
            f"Visual QA {status.value}: {len(gates)} gate(s)",
            f"{len(findings)} hallazgo(s) visual(es)",
            f"{len(screenshots)} captura(s) analizada(s)",
            f"provider={self.provider} model={self.model}",
        ]
        if reasons:
            summary_parts.append("; ".join(reasons))
        if error:
            summary_parts.append(error)
        return VisualQAReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=status,
            summary=" · ".join(summary_parts),
            gates=gates,
            findings=findings,
            proposal=proposal,
            layout_assessment="" if proposal is None else proposal.layout_assessment,
            responsiveness_assessment=(
                "" if proposal is None else proposal.responsiveness_assessment
            ),
            hierarchy_assessment="" if proposal is None else proposal.hierarchy_assessment,
            accessibility_assessment=(
                "" if proposal is None else proposal.accessibility_assessment
            ),
            recommendation_notes="" if proposal is None else proposal.recommendation_notes,
            screenshots_analyzed=screenshots,
            routes_analyzed=task.spec.routes,
            viewports_analyzed=tuple(
                viewport.name.value for viewport in task.spec.viewports
            ),
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            model_calls=model_calls,
            attempts=attempts,
            model_usage=usage,
            started_at=started_at,
            completed_at=utc_now(),
            error=error,
        )

    # --------------------------------------------------------------- auditoría
    def _audit_request_started(
        self, task: VisualQATask, required: tuple[str, ...]
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_visual_qa_request_started(
            project_id=task.project_id,
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            routes=list(task.spec.routes),
            viewports=[viewport.name.value for viewport in task.spec.viewports],
            screenshots=len(required),
        )

    def _audit_proposal_received(
        self, task: VisualQATask, attempt: int, proposal: VisualQAProposal
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_visual_qa_proposal_received(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            findings=len(proposal.findings),
        )

    def _audit_proposal_accepted(
        self, task: VisualQATask, attempt: int, proposal: VisualQAProposal
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_visual_qa_proposal_accepted(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            findings=len(proposal.findings),
        )

    def _audit_proposal_rejected(
        self, task: VisualQATask, attempt: int, violations: tuple[str, ...]
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_visual_qa_proposal_rejected(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            violations=violations,
        )

    def _audit_completed(self, task: VisualQATask, report: VisualQAReport) -> None:
        if self._audit is None:
            return
        for finding in report.findings:
            self._audit.log_visual_qa_finding_recorded(
                project_id=task.project_id,
                task_id=task.task_id,
                finding_id=finding.id,
                severity=finding.severity.value,
                category=finding.category.value,
                route=finding.route,
                viewport=finding.viewport,
            )
        if report.status is VisualQAStatus.BLOCKED:
            self._audit.log_visual_qa_blocked(
                project_id=task.project_id,
                task_id=task.task_id,
                reason=report.error or "; ".join(gate.detail for gate in report.gates),
                provider=self.provider,
                model=self.model,
            )
        self._audit.log_visual_qa_completed(
            project_id=task.project_id,
            task_id=task.task_id,
            status=report.status.value,
            provider=self.provider,
            model=self.model,
            findings=len(report.findings),
            blocking_findings=len(report.blocking_findings),
            gates=[gate.name.value for gate in report.gates],
            screenshots=len(report.screenshots_analyzed),
            attempts=report.attempts,
            total_tokens=report.model_usage.total_tokens,
        )

    def _audit_model_started(
        self, task: VisualQATask, attempt: int, prompt: str, images: int
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_started(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            prompt_chars=len(prompt) + len(VISUAL_SYSTEM_PROMPT),
            images=images,
        )

    def _audit_model_completed(
        self, task: VisualQATask, attempt: int, completion: ModelCompletion
    ) -> None:
        if self._audit is None:
            return
        usage = completion.usage
        self._audit.log_model_request_completed(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            latency_ms=completion.latency_ms,
            transport_retries=completion.transport_retries,
        )

    def _audit_model_failed(
        self, task: VisualQATask, attempt: int, detail: str
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_failed(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            error=detail,
        )


def _parse_json(content: str) -> dict[str, object]:
    """Convierte el texto del modelo en un objeto JSON.

    Raises:
        ValueError: si no se puede obtener un objeto JSON.
    """
    text = content.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("el JSON del modelo no es un objeto")
    return parsed


_AFTER_REJECTION = VISUAL_REPAIR_AFTER_REJECTION
_AFTER_PROVIDER_ERROR = VISUAL_REPAIR_AFTER_PROVIDER_ERROR


__all__ = [
    "BLOCKED_VISUAL_ATTEMPTS",
    "BLOCKED_VISUAL_CALLS",
    "BLOCKED_VISUAL_IMAGES",
    "BLOCKED_VISUAL_PROVIDER_ERROR",
    "BLOCKED_VISUAL_PROVIDER_REFUSAL",
    "BLOCKED_VISUAL_PROVIDER_UNAVAILABLE",
    "BLOCKED_VISUAL_SCHEMA",
    "BLOCKED_VISUAL_TOKENS",
    "ClaudeVisualQARunner",
    "VisualQAProposalError",
]
