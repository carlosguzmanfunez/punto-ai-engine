"""Reviewer real sobre DeepSeek (ENGINE-5).

Implementa :class:`~punto.reviewer.base.ReviewerRunner` reutilizando el ``DeepSeekClient``.

Ciclo:

1. El modelo propone una ``ReviewProposal`` (hallazgos y valoraciones); PUNTO la valida.
2. PUNTO evalúa los **gates** con los informes de QA y de Security (§19).
3. PUNTO calcula el veredicto a partir de los gates y de los hallazgos (§19 y §20).

El modelo puede aportar hallazgos; **no** puede aprobar. Si QA falló o Security falló, el
veredicto es ``CHANGES_REQUESTED`` por mucho que la propuesta diga que todo está perfecto,
y si alguno quedó ``BLOCKED``, el veredicto es ``BLOCKED``. Esa parte no se negocia ni se
promptea: es código.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from punto.common import utc_now
from punto.providers.deepseek import (
    DeepSeekClient,
    DeepSeekError,
    ModelCompletion,
    parse_proposal_json,
)
from punto.qa.paths import normalize_relative_path
from punto.reviewer.base import ReviewerLimits, ReviewerRunner
from punto.reviewer.gates import determine_review_status, evaluate_gates
from punto.reviewer.prompts import (
    REVIEW_FORMAT_REMINDER,
    REVIEW_REPAIR_AFTER_PROVIDER_ERROR,
    REVIEW_REPAIR_AFTER_REJECTION,
    REVIEW_REPAIR_TEMPLATE,
    REVIEW_USER_TEMPLATE,
    REVIEWER_PROMPT_VERSION,
    REVIEWER_SYSTEM_PROMPT,
)
from punto.reviewer.validation import validate_review_proposal
from punto.schemas.execution import ModelUsage
from punto.schemas.review import (
    ReviewGate,
    ReviewGateName,
    ReviewProposal,
    ReviewReport,
    ReviewStatus,
    ReviewTask,
)
from punto.tools.errors import PlanningLimitExceededError

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger

#: Motivos de bloqueo.
BLOCKED_REVIEW_ATTEMPTS: str = "MAX_REVIEW_ATTEMPTS_EXCEEDED"
BLOCKED_REVIEW_CALLS: str = "MAX_REVIEW_MODEL_CALLS_EXCEEDED"
BLOCKED_REVIEW_TOKENS: str = "MAX_REVIEW_TOKENS_EXCEEDED"

#: Máximo de caracteres de un archivo incluido en el contexto de revisión.
MAX_CONTEXT_FILE_CHARS: int = 40_000


def _bullets(items: tuple[str, ...]) -> str:
    """Lista legible para el prompt, o un marcador explícito si está vacía."""
    if not items:
        return "(no declarado)"
    return "\n".join(f"- {item}" for item in items)


class DeepSeekReviewerRunner(ReviewerRunner):
    """Reviewer real: propone con DeepSeek, decide PUNTO."""

    def __init__(
        self,
        *,
        client: DeepSeekClient,
        audit: AuditLogger | None = None,
        limits: ReviewerLimits | None = None,
    ) -> None:
        self._client = client
        self._audit = audit
        self._limits = limits or ReviewerLimits()

    # ------------------------------------------------------------------ estado
    @property
    def name(self) -> str:
        """Nombre del runner."""
        return "DeepSeekReviewerRunner"

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
        return REVIEWER_PROMPT_VERSION

    @property
    def uses_ai(self) -> bool:
        """Siempre ``True``."""
        return True

    @property
    def limits(self) -> ReviewerLimits:
        """Presupuesto configurado."""
        return self._limits

    # ---------------------------------------------------------------- revisión
    def review(self, task: ReviewTask) -> ReviewReport:
        """Revisa el trabajo y calcula el veredicto.

        Nunca lanza por un fallo de la revisión: lo traduce a un ``ReviewReport``.

        Los gates se evalúan **siempre**, incluso si el modelo falla: un proveedor caído no
        convierte un QA fallido en una aprobación. Sin propuesta, el veredicto se calcula
        igualmente y la ausencia de propuesta se registra como bloqueo de la revisión solo
        cuando no hay gates que la sostengan.
        """
        started_at = utc_now()
        usage = ModelUsage()
        model_calls = 0
        attempts = 0
        proposal: ReviewProposal | None = None
        violations: tuple[str, ...] = ()
        error = ""
        proposal_missing = False

        self._audit_request_started(task)

        try:
            proposal, attempts, model_calls, usage = self._obtain_proposal(
                task, usage, model_calls
            )
        except PlanningLimitExceededError as exc:
            proposal_missing = True
            error = str(exc)
        except DeepSeekError as exc:
            proposal_missing = True
            error = self._client.redact(str(exc))
        except (OSError, ValueError) as exc:
            proposal_missing = True
            error = str(exc)
        except ReviewerProposalError as exc:
            proposal_missing = True
            violations = exc.violations
            error = str(exc)

        findings = () if proposal is None else proposal.findings
        blocking = sum(1 for finding in findings if finding.blocks)

        gates = evaluate_gates(
            task, blocking_findings=blocking, total_findings=len(findings)
        )
        if proposal is None:
            # Sin propuesta no hay revisión: los gates de QA y Security siguen valiendo,
            # pero la aprobación es imposible porque nadie evaluó la calidad del cambio.
            gates = (
                gates[0],
                gates[1],
                ReviewGate(
                    name=ReviewGateName.REVIEW_FINDINGS,
                    passed=False,
                    blocking=True,
                    detail=(
                        "no se obtuvo una propuesta de revisión válida: "
                        f"{error or 'sin detalle'}"
                    ),
                ),
            )

        status, reasons = determine_review_status(gates)
        report = self._build_report(
            task=task,
            status=status,
            gates=gates,
            proposal=proposal,
            reasons=reasons,
            error=error if proposal_missing else "",
            violations=violations,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            model_calls=model_calls,
            attempts=attempts,
            usage=usage,
            started_at=started_at,
        )
        self._audit_completed(task, report)
        return report

    # -------------------------------------------------------------- propuesta
    def _obtain_proposal(
        self, task: ReviewTask, usage: ModelUsage, model_calls: int
    ) -> tuple[ReviewProposal, int, int, ModelUsage]:
        """Pide la propuesta de revisión y la valida, con reparación acotada."""
        existing = self._existing_paths(task)
        lines = self._file_lines(task)
        security_ids = (
            None
            if task.security_report is None
            else frozenset(finding.id for finding in task.security_report.findings)
        )
        prompt = self._user_prompt(task)
        violations: tuple[str, ...] = ()

        for attempt in range(1, self._limits.max_attempts + 1):
            if model_calls >= self._limits.max_model_calls:
                raise PlanningLimitExceededError(
                    BLOCKED_REVIEW_CALLS, model_calls, self._limits.max_model_calls
                )
            completion = self._call_model(task, attempt, prompt)
            model_calls += 1
            usage = usage.merged(completion.usage)
            self._assert_token_budget(usage)

            try:
                payload = parse_proposal_json(completion.content)
                proposal = ReviewProposal.model_validate(payload)
            except Exception as exc:  # se traduce a violaciones, nunca a excepción
                violations = (f"contrato incumplido: {type(exc).__name__}: {exc}",)
                self._audit_proposal_rejected(task, attempt, violations)
                prompt = self._repair_prompt(task, violations, _AFTER_REJECTION)
                continue

            self._audit_proposal_received(task, attempt, proposal)
            validation = validate_review_proposal(
                proposal,
                task,
                existing_paths=existing,
                security_finding_ids=security_ids,
                file_lines=lines,
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

        raise ReviewerProposalError(violations)

    # ------------------------------------------------------------------ modelo
    def _call_model(self, task: ReviewTask, attempt: int, prompt: str) -> ModelCompletion:
        """Llama al modelo y audita inicio, fin y fallo."""
        self._audit_model_started(task, attempt, prompt)
        try:
            completion = self._client.complete_json(
                system_prompt=REVIEWER_SYSTEM_PROMPT, user_prompt=prompt
            )
        except DeepSeekError as exc:
            self._audit_model_failed(task, attempt, self._client.redact(str(exc)))
            raise
        self._audit_model_completed(task, attempt, completion)
        return completion

    def _assert_token_budget(self, usage: ModelUsage) -> None:
        """Comprueba el presupuesto acumulado de tokens."""
        if usage.prompt_tokens > self._limits.max_input_tokens:
            raise PlanningLimitExceededError(
                BLOCKED_REVIEW_TOKENS, usage.prompt_tokens, self._limits.max_input_tokens
            )
        if usage.completion_tokens > self._limits.max_output_tokens:
            raise PlanningLimitExceededError(
                BLOCKED_REVIEW_TOKENS,
                usage.completion_tokens,
                self._limits.max_output_tokens,
            )

    def _user_prompt(self, task: ReviewTask) -> str:
        """Petición de revisión."""
        qa_status = "sin informe de QA"
        qa_summary = ""
        if task.qa_report is not None:
            qa_status = task.qa_report.status.value
            qa_summary = task.qa_report.summary
        security_status = "sin informe de seguridad"
        security_summary = ""
        security_findings = "(sin informe)"
        if task.security_report is not None:
            security_status = task.security_report.status.value
            security_summary = task.security_report.summary
            security_findings = _bullets(
                tuple(
                    f"{finding.id} [{finding.severity.value}] {finding.title}"
                    for finding in task.security_report.findings
                )
            )
        return REVIEW_USER_TEMPLATE.format(
            format_reminder=REVIEW_FORMAT_REMINDER,
            objective=task.objective,
            acceptance_criteria=_bullets(task.acceptance_criteria),
            changed_files=_bullets(task.changed_files),
            risk_level=task.risk_level.name,
            authority_level=task.authority_level.name,
            architecture_constraints=_bullets(task.architecture_constraints),
            qa_status=qa_status,
            qa_summary=qa_summary or "(sin resumen)",
            security_status=security_status,
            security_summary=security_summary or "(sin resumen)",
            security_findings=security_findings,
            project_spec_context=task.project_spec_context or "(no disponible)",
            architecture_context=task.architecture_context or "(no disponible)",
            diff_summary=task.diff_summary or self._diff_summary(task),
            review_content=self._review_content(task),
        )

    def _repair_prompt(
        self, task: ReviewTask, violations: tuple[str, ...], situation: str
    ) -> str:
        """Petición de reparación, diciendo la verdad sobre el estado."""
        return REVIEW_REPAIR_TEMPLATE.format(
            situation=situation,
            violations="\n".join(f"- {item}" for item in violations) or "- sin detalle",
            format_reminder=REVIEW_FORMAT_REMINDER,
            objective=task.objective,
            changed_files=_bullets(task.changed_files),
        )

    def _diff_summary(self, task: ReviewTask) -> str:
        """Resumen controlado del cambio: qué archivos y cuánto ocupan."""
        root = Path(task.workspace_path)
        lines: list[str] = []
        for relative in task.changed_files:
            try:
                normalized = normalize_relative_path(relative)
            except ValueError:
                continue
            path = root / normalized
            if not path.is_file():
                lines.append(f"- {normalized}: (no existe)")
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - depende del sistema de archivos
                lines.append(f"- {normalized}: (no legible)")
                continue
            lines.append(f"- {normalized}: {content.count(chr(10)) + 1} línea(s)")
        return "\n".join(lines) or "(sin archivos modificados)"

    def _review_content(self, task: ReviewTask) -> str:
        """Contenido de los archivos relevantes, acotado y sin silencios."""
        root = Path(task.workspace_path)
        wanted = tuple(dict.fromkeys((*task.changed_files, *task.context_files)))
        chunks: list[str] = []
        for relative in wanted[:30]:
            try:
                normalized = normalize_relative_path(relative)
            except ValueError:
                continue
            path = root / normalized
            if not path.is_file():
                chunks.append(f"=== {normalized} ===\n(no existe)")
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - depende del sistema de archivos
                chunks.append(f"=== {normalized} ===\n(no legible)")
                continue
            if len(content) > MAX_CONTEXT_FILE_CHARS:
                content = (
                    f"{content[:MAX_CONTEXT_FILE_CHARS]}\n"
                    f"…[recortado de {len(content)} caracteres]"
                )
            chunks.append(f"=== {normalized} ===\n{content}")
        return "\n\n".join(chunks) or "(no se declararon archivos)"

    def _existing_paths(self, task: ReviewTask) -> frozenset[str]:
        """Rutas que existen en el workspace."""
        root = Path(task.workspace_path)
        if not root.is_dir():
            return frozenset()
        found: set[str] = set()
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    found.add(path.relative_to(root).as_posix())
                except ValueError:  # pragma: no cover - defensivo
                    continue
        return frozenset(found)

    def _file_lines(self, task: ReviewTask) -> dict[str, int]:
        """Número de líneas de cada archivo revisable."""
        root = Path(task.workspace_path)
        lines: dict[str, int] = {}
        for relative in dict.fromkeys((*task.changed_files, *task.context_files)):
            try:
                normalized = normalize_relative_path(relative)
            except ValueError:
                continue
            path = root / normalized
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - depende del sistema de archivos
                continue
            lines[normalized] = content.count("\n") + 1
        return lines

    # ------------------------------------------------------------------ salida
    def _build_report(
        self,
        *,
        task: ReviewTask,
        status: ReviewStatus,
        gates: tuple[ReviewGate, ...],
        proposal: ReviewProposal | None,
        reasons: tuple[str, ...],
        error: str,
        violations: tuple[str, ...],
        provider: str,
        model: str,
        prompt_version: str,
        model_calls: int,
        attempts: int,
        usage: ModelUsage,
        started_at: datetime,
    ) -> ReviewReport:
        """Compone el informe de revisión."""
        findings = () if proposal is None else proposal.findings
        summary_parts = [
            f"Reviewer {status.value}: {len(gates)} gate(s)",
            f"{len(findings)} hallazgo(s) de revisión",
        ]
        if reasons:
            summary_parts.append("; ".join(reasons))
        if error:
            summary_parts.append(error)
        return ReviewReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=status,
            summary=" · ".join(summary_parts),
            gates=gates,
            findings=findings,
            proposal=proposal,
            architecture_assessment="" if proposal is None else proposal.architecture_assessment,
            maintainability_assessment=(
                "" if proposal is None else proposal.maintainability_assessment
            ),
            scope_assessment="" if proposal is None else proposal.scope_assessment,
            recommendation_notes="" if proposal is None else proposal.recommendation_notes,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            model_calls=model_calls,
            attempts=attempts,
            model_usage=usage,
            started_at=started_at,
            completed_at=utc_now(),
            error=error,
        )

    # --------------------------------------------------------------- auditoría
    def _audit_request_started(self, task: ReviewTask) -> None:
        if self._audit is None:
            return
        self._audit.log_review_request_started(
            project_id=task.project_id,
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            qa_status="" if task.qa_report is None else task.qa_report.status.value,
            security_status=(
                "" if task.security_report is None else task.security_report.status.value
            ),
        )

    def _audit_proposal_received(
        self, task: ReviewTask, attempt: int, proposal: ReviewProposal
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_review_proposal_received(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            findings=len(proposal.findings),
        )

    def _audit_proposal_accepted(
        self, task: ReviewTask, attempt: int, proposal: ReviewProposal
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_review_proposal_accepted(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            findings=len(proposal.findings),
            status="PENDING",
        )

    def _audit_proposal_rejected(
        self, task: ReviewTask, attempt: int, violations: tuple[str, ...]
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_review_proposal_rejected(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            violations=violations,
        )

    def _audit_completed(self, task: ReviewTask, report: ReviewReport) -> None:
        if self._audit is None:
            return
        for gate in report.gates:
            self._audit.log_review_gate_evaluated(
                project_id=task.project_id,
                task_id=task.task_id,
                gate=gate.name.value,
                passed=gate.passed,
                blocking=gate.blocking,
                detail=gate.detail,
            )
        for finding in report.findings:
            self._audit.log_review_finding_recorded(
                project_id=task.project_id,
                task_id=task.task_id,
                finding_id=finding.id,
                severity=finding.severity.value,
                category=finding.category.value,
            )
        self._audit.log_review_completed(
            project_id=task.project_id,
            task_id=task.task_id,
            status=report.status.value,
            findings=len(report.findings),
            blocking_findings=len(report.blocking_findings),
            gates=[gate.name.value for gate in report.gates],
            total_tokens=report.model_usage.total_tokens,
        )

    def _audit_model_started(self, task: ReviewTask, attempt: int, prompt: str) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_started(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            prompt_chars=len(prompt) + len(REVIEWER_SYSTEM_PROMPT),
        )

    def _audit_model_completed(
        self, task: ReviewTask, attempt: int, completion: ModelCompletion
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_completed(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
            total_tokens=completion.usage.total_tokens,
            latency_ms=completion.latency_ms,
            transport_retries=completion.transport_retries,
        )

    def _audit_model_failed(self, task: ReviewTask, attempt: int, detail: str) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_failed(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            error=detail,
        )


class ReviewerProposalError(RuntimeError):
    """No se pudo obtener una propuesta de revisión válida."""

    def __init__(self, violations: tuple[str, ...]) -> None:
        self.violations = violations
        detail = "; ".join(violations[:3]) if violations else "sin detalle"
        super().__init__(f"{BLOCKED_REVIEW_ATTEMPTS}: {detail}")


_AFTER_REJECTION = REVIEW_REPAIR_AFTER_REJECTION
_AFTER_PROVIDER_ERROR = REVIEW_REPAIR_AFTER_PROVIDER_ERROR


__all__ = [
    "BLOCKED_REVIEW_ATTEMPTS",
    "BLOCKED_REVIEW_CALLS",
    "BLOCKED_REVIEW_TOKENS",
    "MAX_CONTEXT_FILE_CHARS",
    "DeepSeekReviewerRunner",
    "ReviewerProposalError",
]
