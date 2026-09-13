"""Auditoría cruzada real sobre Anthropic/Claude (ENGINE-5.2).

Es el primer rol del motor que **no** habla con DeepSeek. Su valor depende de eso: si el
auditor usara el mismo proveedor que construyó y evaluó el trabajo, el informe podría seguir
llamándose «cruzado» pero ya no lo sería, y por eso el informe declara ``cross_model`` como un
hecho calculado.

Ciclo, con la validación de PUNTO en cada frontera:

1. PUNTO construye el contexto visible con :func:`build_model_review_context`, la misma
   frontera de ENGINE-5.1/5.1.1: enlaces que escapan no se leen y los archivos modificados que
   no quepan bloquean la auditoría;
2. Claude propone una ``CrossAuditProposal``; PUNTO la valida (contrato, evidencia, visibilidad
   y referencias);
3. PUNTO evalúa los **gates** con los informes de QA, Security y Reviewer;
4. PUNTO calcula el estado. Claude **no** escribe el veredicto y no puede anular un gate previo.

No ejecuta código, no modifica archivos, no hace commits y no repara el producto.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from punto.common import utc_now
from punto.crossaudit.base import CrossAuditLimits, CrossAuditRunner
from punto.crossaudit.gates import (
    determine_cross_audit_status,
    evaluate_cross_audit_gates,
)
from punto.crossaudit.prompts import (
    CROSS_AUDIT_FORMAT_REMINDER,
    CROSS_AUDIT_PROMPT_VERSION,
    CROSS_AUDIT_REPAIR_AFTER_PROVIDER_ERROR,
    CROSS_AUDIT_REPAIR_AFTER_REJECTION,
    CROSS_AUDIT_REPAIR_TEMPLATE,
    CROSS_AUDIT_SYSTEM_PROMPT,
    CROSS_AUDIT_USER_TEMPLATE,
)
from punto.crossaudit.validation import validate_cross_audit_proposal
from punto.model_context import (
    BLOCKED_CONTEXT_LIMIT,
    ModelReviewContext,
    build_model_review_context,
    missing_paths,
    resolve_within_workspace,
)
from punto.providers.anthropic import AnthropicClient
from punto.providers.base import (
    PROVIDER_ANTHROPIC,
    ModelCompletion,
    ProviderAuthenticationError,
    ProviderError,
)
from punto.schemas.cross_audit import (
    CrossAuditGate,
    CrossAuditProposal,
    CrossAuditReport,
    CrossAuditStatus,
    CrossAuditTask,
    compute_cross_model,
)
from punto.schemas.execution import ModelUsage
from punto.tools.errors import CrossAuditError, PlanningLimitExceededError

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger

#: Motivos de bloqueo.
BLOCKED_CROSS_AUDIT_ATTEMPTS: str = "MAX_CROSS_AUDIT_ATTEMPTS_EXCEEDED"
BLOCKED_CROSS_AUDIT_CALLS: str = "MAX_CROSS_AUDIT_MODEL_CALLS_EXCEEDED"
BLOCKED_CROSS_AUDIT_TOKENS: str = "MAX_CROSS_AUDIT_TOKENS_EXCEEDED"
BLOCKED_PROVIDER_UNAVAILABLE: str = "PROVIDER_UNAVAILABLE"
BLOCKED_PROVIDER_ERROR: str = "PROVIDER_ERROR"


def _bullets(items: tuple[str, ...]) -> str:
    """Lista legible para el prompt, o un marcador explícito si está vacía."""
    if not items:
        return "- (ninguno)"
    return "\n".join(f"- {item}" for item in items)


class CrossAuditProposalError(CrossAuditError):
    """No se pudo obtener una propuesta de auditoría válida."""

    def __init__(self, violations: tuple[str, ...]) -> None:
        self.violations = violations
        detail = "; ".join(violations[:3]) if violations else "sin detalle"
        super().__init__(f"{BLOCKED_CROSS_AUDIT_ATTEMPTS}: {detail}")


class ClaudeCrossModelAuditRunner(CrossAuditRunner):
    """Auditor cruzado real: propone Claude, decide PUNTO."""

    def __init__(
        self,
        *,
        client: AnthropicClient,
        audit: AuditLogger | None = None,
        limits: CrossAuditLimits | None = None,
    ) -> None:
        self._client = client
        self._audit = audit
        self._limits = limits or CrossAuditLimits()

    # ------------------------------------------------------------------ estado
    @property
    def name(self) -> str:
        """Nombre del runner."""
        return "ClaudeCrossModelAuditRunner"

    @property
    def provider(self) -> str:
        """Proveedor del auditor."""
        return PROVIDER_ANTHROPIC

    @property
    def model(self) -> str:
        """Modelo auditor configurado."""
        return self._client.model

    @property
    def prompt_version(self) -> str:
        """Versión del prompt de auditoría en uso."""
        return CROSS_AUDIT_PROMPT_VERSION

    @property
    def uses_ai(self) -> bool:
        """Siempre ``True``."""
        return True

    @property
    def limits(self) -> CrossAuditLimits:
        """Presupuesto configurado."""
        return self._limits

    # ------------------------------------------------------------------ auditoría
    def audit(self, task: CrossAuditTask) -> CrossAuditReport:
        """Audita el trabajo y devuelve el informe.

        Nunca lanza por un fallo de la tarea: lo traduce a un ``CrossAuditReport``. Los gates
        se evalúan **siempre**, incluso si el proveedor falla: un Claude caído no convierte un
        QA fallido en un PASS.
        """
        started_at = utc_now()
        usage = ModelUsage()
        model_calls = 0
        attempts = 0
        proposal: CrossAuditProposal | None = None
        violations: tuple[str, ...] = ()
        error = ""
        proposal_missing = False

        # Frontera de contexto (ENGINE-5.1/5.1.1): se reutiliza, no se reimplementa.
        context = build_model_review_context(task.workspace_path, task.reviewable_paths)
        uncovered = missing_paths(context, task.changed_files)

        self._audit_request_started(task)

        if uncovered:
            proposal_missing = True
            error = (
                f"{BLOCKED_CONTEXT_LIMIT}: {len(uncovered)} archivo(s) modificado(s) quedaron "
                f"fuera del contexto del auditor ({', '.join(uncovered[:5])}"
                f"{'…' if len(uncovered) > 5 else ''}): no se audita a medias"
            )
        else:
            try:
                proposal, attempts, model_calls, usage = self._obtain_proposal(
                    task, context, usage, model_calls
                )
            except PlanningLimitExceededError as exc:
                proposal_missing = True
                error = str(exc)
            except ProviderAuthenticationError as exc:
                proposal_missing = True
                error = f"{BLOCKED_PROVIDER_UNAVAILABLE}: {self._client.redact(str(exc))}"
            except ProviderError as exc:
                proposal_missing = True
                error = f"{BLOCKED_PROVIDER_ERROR}: {self._client.redact(str(exc))}"
            except CrossAuditProposalError as exc:
                proposal_missing = True
                violations = exc.violations
                error = str(exc)

        findings = () if proposal is None else proposal.findings
        blocking = sum(1 for finding in findings if finding.blocks)

        gates = evaluate_cross_audit_gates(
            task,
            context_complete=not uncovered,
            context_detail=self._context_detail(context, uncovered),
            proposal_present=proposal is not None,
            blocking_findings=blocking,
            total_findings=len(findings),
        )
        status, reasons = determine_cross_audit_status(gates)
        report = self._build_report(
            task=task,
            status=status,
            gates=gates,
            proposal=proposal,
            context=context,
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

    # ------------------------------------------------------------------ propuesta
    def _obtain_proposal(
        self,
        task: CrossAuditTask,
        context: ModelReviewContext,
        usage: ModelUsage,
        model_calls: int,
    ) -> tuple[CrossAuditProposal, int, int, ModelUsage]:
        """Pide la propuesta de auditoría y la valida, con reparación acotada."""
        existing = self._existing_paths(task)
        prompt = self._user_prompt(task, context)
        violations: tuple[str, ...] = ()

        for attempt in range(1, self._limits.max_attempts + 1):
            if model_calls >= self._limits.max_model_calls:
                raise PlanningLimitExceededError(
                    BLOCKED_CROSS_AUDIT_CALLS, model_calls, self._limits.max_model_calls
                )
            completion = self._call_model(task, attempt, prompt)
            model_calls += 1
            usage = usage.merged(completion.usage)
            self._assert_token_budget(usage)

            try:
                payload = _parse_json(completion.content)
                proposal = CrossAuditProposal.model_validate(payload)
            except Exception as exc:  # se traduce a violaciones, nunca a excepción
                violations = (f"contrato incumplido: {type(exc).__name__}: {exc}",)
                self._audit_proposal_rejected(task, attempt, violations)
                prompt = self._repair_prompt(task, violations, _AFTER_REJECTION)
                continue

            self._audit_proposal_received(task, attempt, proposal)
            validation = validate_cross_audit_proposal(
                proposal,
                task,
                model_visible_paths=context.visible_set,
                existing_paths=existing,
                qa_finding_ids=self._finding_ids(task, "qa"),
                security_finding_ids=self._finding_ids(task, "security"),
                review_finding_ids=self._finding_ids(task, "review"),
                file_lines=context.line_map(),
            )
            if not validation.valid:
                violations = validation.violations
                self._audit_proposal_rejected(task, attempt, violations)
                prompt = self._repair_prompt(
                    task,
                    violations,
                    _AFTER_REJECTION if violations else _AFTER_PROVIDER_ERROR,
                )
                continue

            self._audit_proposal_accepted(task, attempt, proposal)
            return proposal, attempt, model_calls, usage

        raise CrossAuditProposalError(violations)

    @staticmethod
    def _finding_ids(task: CrossAuditTask, source: str) -> frozenset[str] | None:
        """Identificadores de otro rol, o ``None`` si ese informe no existe.

        ``None`` y conjunto vacío no significan lo mismo: ``None`` es «no hay informe y no se
        puede comprobar la referencia»; un conjunto vacío es «el informe existe y no tiene
        hallazgos», así que cualquier referencia está colgando.
        """
        report = {
            "qa": task.qa_report,
            "security": task.security_report,
            "review": task.review_report,
        }[source]
        if report is None:
            return None
        return frozenset(finding.id for finding in report.findings)

    # ------------------------------------------------------------------ modelo
    def _call_model(
        self, task: CrossAuditTask, attempt: int, prompt: str
    ) -> ModelCompletion:
        """Llama al modelo y audita inicio, fin y fallo.

        La petición lleva el **esquema real** de :class:`CrossAuditProposal`: el formato no
        depende de que el prompt lo pida. El bucle de reparación semántica se conserva para lo
        que un esquema no puede cubrir —evidencia, visibilidad de archivos, referencias a
        hallazgos y los invariantes propios de PUNTO—, no para arreglar JSON roto.
        """
        self._audit_model_started(task, attempt, prompt)
        try:
            completion = self._client.complete_json(
                system_prompt=CROSS_AUDIT_SYSTEM_PROMPT,
                user_prompt=prompt,
                json_schema=CrossAuditProposal.model_json_schema(),
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
                BLOCKED_CROSS_AUDIT_TOKENS, usage.prompt_tokens, self._limits.max_input_tokens
            )
        if usage.completion_tokens > self._limits.max_output_tokens:
            raise PlanningLimitExceededError(
                BLOCKED_CROSS_AUDIT_TOKENS,
                usage.completion_tokens,
                self._limits.max_output_tokens,
            )

    def _user_prompt(self, task: CrossAuditTask, context: ModelReviewContext) -> str:
        """Petición de auditoría, con el contexto visible exacto."""
        qa = task.qa_report
        security = task.security_report
        review = task.review_report
        return CROSS_AUDIT_USER_TEMPLATE.format(
            format_reminder=CROSS_AUDIT_FORMAT_REMINDER,
            objective=task.objective,
            acceptance_criteria=_bullets(task.acceptance_criteria),
            changed_files=_bullets(task.changed_files),
            context_files=_bullets(task.context_files),
            risk_level=task.risk_level.name,
            authority_level=task.authority_level.name,
            diff_summary=task.diff_summary or "(no preparado)",
            project_spec_context=task.project_spec_context or "(no disponible)",
            architecture_context=task.architecture_context or "(no disponible)",
            developer_status=self._developer_status(task),
            qa_status="sin informe" if qa is None else qa.status.value,
            qa_summary="" if qa is None else qa.summary,
            qa_findings=_bullets(
                () if qa is None else tuple(_finding_line(f) for f in qa.findings)
            ),
            security_status="sin informe" if security is None else security.status.value,
            security_summary="" if security is None else security.summary,
            security_findings=_bullets(
                ()
                if security is None
                else tuple(_finding_line(f) for f in security.findings)
            ),
            review_status="sin informe" if review is None else review.status.value,
            review_summary="" if review is None else review.summary,
            review_findings=_bullets(
                () if review is None else tuple(_finding_line(f) for f in review.findings)
            ),
            review_content=context.annotated_content(),
        )

    def _repair_prompt(
        self, task: CrossAuditTask, violations: tuple[str, ...], situation: str
    ) -> str:
        """Petición de reparación, diciendo la verdad sobre el estado."""
        return CROSS_AUDIT_REPAIR_TEMPLATE.format(
            situation=situation,
            violations="\n".join(f"- {item}" for item in violations) or "- sin detalle",
            format_reminder=CROSS_AUDIT_FORMAT_REMINDER,
            objective=task.objective,
            changed_files=_bullets(task.changed_files),
        )

    @staticmethod
    def _developer_status(task: CrossAuditTask) -> str:
        """Estado del Developer, tal como consta en su evidencia."""
        result = task.developer_result
        if result is None:
            return "sin evidencia"
        return f"{result.status.value} (provider={result.provider or 'desconocido'})"

    @staticmethod
    def _context_detail(context: ModelReviewContext, uncovered: tuple[str, ...]) -> str:
        """Motivo del gate de contexto, con la verdad completa."""
        if uncovered:
            return (
                f"{BLOCKED_CONTEXT_LIMIT}: faltan {len(uncovered)} archivo(s) modificado(s): "
                f"{', '.join(uncovered[:5])}"
            )
        if context.omitted_paths:
            return (
                f"contexto parcial declarado: {len(context.omitted_paths)} archivo(s) "
                "auxiliar(es) no enviado(s) al modelo"
            )
        return "el contexto del modelo cubre todos los archivos declarados"

    def _existing_paths(self, task: CrossAuditTask) -> frozenset[str]:
        """Rutas que existen en el workspace y siguen dentro de él."""
        root = Path(task.workspace_path)
        if not root.is_dir():
            return frozenset()
        found: set[str] = set()
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:  # pragma: no cover - defensivo
                continue
            if resolve_within_workspace(root, relative) is None:
                continue
            found.add(relative)
        return frozenset(found)

    # ------------------------------------------------------------------ salida
    def _build_report(
        self,
        *,
        task: CrossAuditTask,
        status: CrossAuditStatus,
        gates: tuple[CrossAuditGate, ...],
        proposal: CrossAuditProposal | None,
        context: ModelReviewContext,
        reasons: tuple[str, ...],
        error: str,
        violations: tuple[str, ...],
        model_calls: int,
        attempts: int,
        usage: ModelUsage,
        started_at: datetime,
    ) -> CrossAuditReport:
        """Compone el informe de auditoría cruzada."""
        findings = () if proposal is None else proposal.findings
        upstream = task.upstream_providers
        summary_parts = [
            f"Auditoría cruzada {status.value}: {len(gates)} gate(s)",
            f"{len(findings)} hallazgo(s)",
            f"provider={self.provider} model={self.model}",
        ]
        if reasons:
            summary_parts.append("; ".join(reasons))
        if error:
            summary_parts.append(error)
        return CrossAuditReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=status,
            summary=" · ".join(summary_parts),
            gates=gates,
            findings=findings,
            proposal=proposal,
            architecture_assessment="" if proposal is None else proposal.architecture_assessment,
            qa_assessment="" if proposal is None else proposal.qa_assessment,
            security_assessment="" if proposal is None else proposal.security_assessment,
            maintainability_assessment=(
                "" if proposal is None else proposal.maintainability_assessment
            ),
            scope_assessment="" if proposal is None else proposal.scope_assessment,
            recommendation_notes="" if proposal is None else proposal.recommendation_notes,
            model_visible_files=context.visible_paths,
            omitted_paths=context.omitted_paths,
            provider=self.provider,
            model=self.model,
            upstream_providers=upstream,
            cross_model=compute_cross_model(self.provider, upstream),
            prompt_version=self.prompt_version,
            model_calls=model_calls,
            attempts=attempts,
            model_usage=usage,
            started_at=started_at,
            completed_at=utc_now(),
            error=error,
        )

    # --------------------------------------------------------------- auditoría
    def _audit_request_started(self, task: CrossAuditTask) -> None:
        if self._audit is None:
            return
        self._audit.log_cross_audit_request_started(
            project_id=task.project_id,
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            upstream_providers=list(task.upstream_providers),
        )

    def _audit_proposal_received(
        self, task: CrossAuditTask, attempt: int, proposal: CrossAuditProposal
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_cross_audit_proposal_received(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            findings=len(proposal.findings),
        )

    def _audit_proposal_accepted(
        self, task: CrossAuditTask, attempt: int, proposal: CrossAuditProposal
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_cross_audit_proposal_accepted(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            findings=len(proposal.findings),
        )

    def _audit_proposal_rejected(
        self, task: CrossAuditTask, attempt: int, violations: tuple[str, ...]
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_cross_audit_proposal_rejected(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            violations=violations,
        )

    def _audit_completed(self, task: CrossAuditTask, report: CrossAuditReport) -> None:
        if self._audit is None:
            return
        for finding in report.findings:
            self._audit.log_cross_audit_finding_recorded(
                project_id=task.project_id,
                task_id=task.task_id,
                finding_id=finding.id,
                severity=finding.severity.value,
                category=finding.category.value,
                file=finding.file,
            )
        if report.status is CrossAuditStatus.BLOCKED:
            self._audit.log_cross_audit_blocked(
                project_id=task.project_id,
                task_id=task.task_id,
                reason=report.error or "; ".join(gate.detail for gate in report.gates),
                provider=self.provider,
                model=self.model,
            )
        self._audit.log_cross_audit_completed(
            project_id=task.project_id,
            task_id=task.task_id,
            status=report.status.value,
            provider=self.provider,
            model=self.model,
            upstream_providers=list(report.upstream_providers),
            cross_model=report.cross_model,
            findings=len(report.findings),
            blocking_findings=len(report.blocking_findings),
            gates=[gate.name.value for gate in report.gates],
            attempts=report.attempts,
            total_tokens=report.model_usage.total_tokens,
        )

    def _audit_model_started(
        self, task: CrossAuditTask, attempt: int, prompt: str
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_started(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            prompt_chars=len(prompt) + len(CROSS_AUDIT_SYSTEM_PROMPT),
        )

    def _audit_model_completed(
        self, task: CrossAuditTask, attempt: int, completion: ModelCompletion
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

    def _audit_model_failed(self, task: CrossAuditTask, attempt: int, detail: str) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_failed(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            error=detail,
        )


def _finding_line(finding: object) -> str:
    """Línea legible de un hallazgo de otro rol, sin volcar su contenido entero."""
    identifier = getattr(finding, "id", "")
    severity = getattr(finding, "severity", "")
    title = getattr(finding, "title", "")
    severity_value = getattr(severity, "value", severity)
    file = getattr(finding, "file", "")
    location = f" {file}" if file else ""
    return f"{identifier} [{severity_value}]{location} {title}".strip()


def _parse_json(content: str) -> dict[str, object]:
    """Convierte el texto del modelo en un objeto JSON.

    Acepta contenido envuelto en un bloque de código, que es un formato que los modelos
    producen con frecuencia; el JSON resultante se valida igualmente con Pydantic.

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


_AFTER_REJECTION = CROSS_AUDIT_REPAIR_AFTER_REJECTION
_AFTER_PROVIDER_ERROR = CROSS_AUDIT_REPAIR_AFTER_PROVIDER_ERROR


__all__ = [
    "BLOCKED_CROSS_AUDIT_ATTEMPTS",
    "BLOCKED_CROSS_AUDIT_CALLS",
    "BLOCKED_CROSS_AUDIT_TOKENS",
    "BLOCKED_PROVIDER_ERROR",
    "BLOCKED_PROVIDER_UNAVAILABLE",
    "ClaudeCrossModelAuditRunner",
    "CrossAuditProposalError",
]
