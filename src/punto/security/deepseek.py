"""Security Agent real sobre DeepSeek (ENGINE-5).

Implementa :class:`~punto.security.base.SecurityRunner` reutilizando el ``DeepSeekClient``
y los checks deterministas de PUNTO. **No** se duplica cliente HTTP ni se inventa una vía
de ejecución.

Ciclo, con la validación de PUNTO en cada frontera:

1. El modelo propone un ``SecurityPlan``; PUNTO lo valida (§4 y §5).
2. PUNTO ejecuta **siempre** los checks deterministas aplicables a los archivos revisados:
   evidencia que el modelo no puede fabricar ni omitir (§8 a §10).
3. PUNTO ejecuta los checks adicionales que el plan pidió; si alguno no está disponible, la
   auditoría es ``BLOCKED`` por capacidad, sin ejecutar nada en el host (§8).
4. El modelo propone hallazgos sobre el contexto autorizado; PUNTO los valida (§11 y §12).
5. PUNTO deduplica deterministas y del modelo conservando **todas** las fuentes.
6. PUNTO calcula el estado: ``HIGH``/``CRITICAL`` ⇒ ``FAIL`` (§7).

Un producto vulnerable **no** se repara aquí: se reporta.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from punto.common import utc_now
from punto.planning.capabilities import canonical_capability, capability_status
from punto.providers.deepseek import (
    DeepSeekClient,
    DeepSeekError,
    ModelCompletion,
    parse_proposal_json,
)
from punto.qa.paths import normalize_relative_path
from punto.schemas.execution import ModelUsage
from punto.schemas.planning import CapabilityGap, CapabilityKind
from punto.schemas.security import (
    SecurityCheckOutcome,
    SecurityFinding,
    SecurityFindingSource,
    SecurityFindingsProposal,
    SecurityPlan,
    SecurityPlanProposal,
    SecurityReport,
    SecurityTask,
)
from punto.security.base import SecurityLimits, SecurityRunner
from punto.security.checks import DEFAULT_SECURITY_REGISTRY, SecurityCheckRegistry
from punto.security.deterministic import SecurityCheckContext, SecurityCheckResult
from punto.security.prompts import (
    SECURITY_FINDINGS_FORMAT_REMINDER,
    SECURITY_FINDINGS_TEMPLATE,
    SECURITY_PLAN_FORMAT_REMINDER,
    SECURITY_PLAN_TEMPLATE,
    SECURITY_PROMPT_VERSION,
    SECURITY_REPAIR_AFTER_FINDINGS_REJECTION,
    SECURITY_REPAIR_AFTER_PLAN_REJECTION,
    SECURITY_REPAIR_AFTER_PROVIDER_ERROR,
    SECURITY_REPAIR_TEMPLATE,
    SECURITY_SYSTEM_PROMPT,
)
from punto.security.report import (
    build_security_report,
    deduplicate_findings,
    determine_security_status,
    summarize,
)
from punto.security.validation import validate_findings, validate_security_plan
from punto.tools.errors import PlanningLimitExceededError

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger

#: Motivos de bloqueo.
BLOCKED_SECURITY_ATTEMPTS: str = "MAX_SECURITY_ATTEMPTS_EXCEEDED"
BLOCKED_SECURITY_CALLS: str = "MAX_SECURITY_MODEL_CALLS_EXCEEDED"
BLOCKED_SECURITY_TOKENS: str = "MAX_SECURITY_TOKENS_EXCEEDED"
BLOCKED_SECURITY_CAPABILITY: str = "CAPABILITY_REQUIRED"

#: Máximo de caracteres de un archivo incluido en el contexto de revisión.
MAX_CONTEXT_FILE_CHARS: int = 40_000


def _bullets(items: tuple[str, ...]) -> str:
    """Lista legible para el prompt, o un marcador explícito si está vacía."""
    if not items:
        return "(no declarado)"
    return "\n".join(f"- {item}" for item in items)


class DeepSeekSecurityRunner(SecurityRunner):
    """Security independiente: propone con DeepSeek, comprueba PUNTO."""

    def __init__(
        self,
        *,
        client: DeepSeekClient,
        audit: AuditLogger | None = None,
        limits: SecurityLimits | None = None,
        registry: SecurityCheckRegistry | None = None,
    ) -> None:
        self._client = client
        self._audit = audit
        self._limits = limits or SecurityLimits()
        self._registry = registry or DEFAULT_SECURITY_REGISTRY

    # ------------------------------------------------------------------ estado
    @property
    def name(self) -> str:
        """Nombre del runner."""
        return "DeepSeekSecurityRunner"

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
        return SECURITY_PROMPT_VERSION

    @property
    def uses_ai(self) -> bool:
        """Siempre ``True``."""
        return True

    @property
    def limits(self) -> SecurityLimits:
        """Presupuesto configurado."""
        return self._limits

    @property
    def registry(self) -> SecurityCheckRegistry:
        """Registro de checks de seguridad."""
        return self._registry

    # -------------------------------------------------------------- evaluación
    def evaluate(self, task: SecurityTask) -> SecurityReport:
        """Audita el trabajo y devuelve la evidencia. Nunca lanza por un fallo de la tarea."""
        started_at = utc_now()
        usage = ModelUsage()
        model_calls = 0
        attempts = 0
        plan: SecurityPlan | None = None
        findings: tuple[SecurityFinding, ...] = ()
        checks: tuple[SecurityCheckOutcome, ...] = ()
        reviewed: tuple[str, ...] = ()
        gaps: tuple[CapabilityGap, ...] = ()
        evidence: list[str] = []
        error = ""
        planning_blocked = False
        capability_blocked = False

        self._audit_request_started(task)

        try:
            workspace = Path(task.workspace_path)
            existing = self._existing_paths(task)
            plan, attempts, model_calls, usage = self._obtain_plan(
                task, existing, usage, model_calls
            )
            missing = [
                name for name in plan.security_checks if not self._check_available(name)
            ]
            gaps = self._capability_gaps(task, missing)
            if missing:
                capability_blocked = True
                error = (
                    f"{BLOCKED_SECURITY_CAPABILITY}: los checks solicitados no están "
                    f"disponibles: {', '.join(missing)}"
                )
                evidence.append(error)
                for name in missing:
                    self._audit_blocked(task, name, "check registrado pero no disponible")
            else:
                checks, deterministic = self._run_checks(task, plan, workspace)
                reviewed = self._reviewed_files(plan, deterministic, existing)
                deterministic_findings = tuple(
                    finding for result in deterministic for finding in result.findings
                )
                (
                    model_findings,
                    findings_blocked,
                    findings_error,
                    model_calls,
                    usage,
                ) = self._obtain_findings(
                    task=task,
                    plan=plan,
                    check_results=deterministic,
                    usage=usage,
                    model_calls=model_calls,
                )
                if findings_blocked:
                    planning_blocked = True
                    error = findings_error
                    evidence.append(error)
                # Los deterministas van primero: son hechos reproducibles. La deduplicación
                # funde las coincidencias conservando **ambas** fuentes.
                findings = deduplicate_findings((*deterministic_findings, *model_findings))
                evidence.append(
                    f"{len(checks)} check(s) ejecutado(s), {len(reviewed)} archivo(s) "
                    f"revisado(s), {len(deterministic_findings)} hallazgo(s) determinista(s)"
                )
        except PlanningLimitExceededError as exc:
            planning_blocked = True
            error = str(exc)
            evidence.append(error)
        except DeepSeekError as exc:
            planning_blocked = True
            error = self._client.redact(str(exc))
            evidence.append(error)
        except (OSError, ValueError, KeyError) as exc:
            planning_blocked = True
            error = str(exc)
            evidence.append(error)

        status, reasons = determine_security_status(
            findings=findings,
            capability_blocked=capability_blocked,
            planning_blocked=planning_blocked,
            error=error,
        )
        report = build_security_report(
            task=task,
            status=status,
            summary=summarize(status, findings, checks, reasons),
            plan=plan,
            findings=findings,
            executed_checks=checks,
            reviewed_files=reviewed,
            capability_gaps=gaps,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            model_calls=model_calls,
            attempts=attempts,
            model_usage=usage,
            started_at=started_at,
            completed_at=utc_now(),
            error=error,
            extra_evidence=tuple(evidence),
        )
        self._audit_completed(task, report)
        return report

    # ------------------------------------------------------------------- plan
    def _obtain_plan(
        self,
        task: SecurityTask,
        existing: frozenset[str],
        usage: ModelUsage,
        model_calls: int,
    ) -> tuple[SecurityPlan, int, int, ModelUsage]:
        """Pide un plan de seguridad y lo valida, con reparación acotada."""
        violations: tuple[str, ...] = ()
        prompt = self._plan_prompt(task)

        for attempt in range(1, self._limits.max_attempts + 1):
            if model_calls >= self._limits.max_model_calls:
                raise PlanningLimitExceededError(
                    BLOCKED_SECURITY_CALLS, model_calls, self._limits.max_model_calls
                )
            completion = self._call_model(task, attempt, prompt, self._system_prompt())
            model_calls += 1
            usage = usage.merged(completion.usage)
            self._assert_token_budget(usage)

            try:
                payload = parse_proposal_json(completion.content)
                proposal = SecurityPlanProposal.model_validate(payload)
            except Exception as exc:  # se traduce a violaciones, nunca a excepción
                violations = (f"contrato incumplido: {type(exc).__name__}: {exc}",)
                prompt = self._repair_prompt(
                    task, violations, SECURITY_REPAIR_AFTER_PLAN_REJECTION, plan=True
                )
                self._audit_plan_rejected(task, attempt, violations)
                continue

            self._audit_plan_received(task, attempt, proposal)
            validation = validate_security_plan(
                proposal, task, registry=self._registry, existing_paths=existing
            )
            if not validation.valid:
                violations = validation.violations
                prompt = self._repair_prompt(
                    task,
                    violations,
                    SECURITY_REPAIR_AFTER_PLAN_REJECTION
                    if violations
                    else SECURITY_REPAIR_AFTER_PROVIDER_ERROR,
                    plan=True,
                )
                self._audit_plan_rejected(task, attempt, violations)
                continue

            plan = SecurityPlan(
                task_id=task.task_id,
                project_id=task.project_id,
                attempt=attempt,
                prompt_version=self.prompt_version,
                summary=proposal.summary,
                review_targets=proposal.review_targets,
                security_checks=proposal.security_checks,
                analysis_areas=proposal.analysis_areas,
                threats_considered=proposal.threats_considered,
                assumptions=proposal.assumptions,
            )
            self._audit_plan_accepted(task, attempt, plan)
            return plan, attempt, model_calls, usage

        raise PlanningLimitExceededError(
            BLOCKED_SECURITY_ATTEMPTS, self._limits.max_attempts, self._limits.max_attempts
        )

    # -------------------------------------------------------------- hallazgos
    def _obtain_findings(
        self,
        *,
        task: SecurityTask,
        plan: SecurityPlan,
        check_results: tuple[SecurityCheckResult, ...],
        usage: ModelUsage,
        model_calls: int,
    ) -> tuple[tuple[SecurityFinding, ...], bool, str, int, ModelUsage]:
        """Pide hallazgos al modelo y los valida, con reparación acotada."""
        violations: tuple[str, ...] = ()
        prompt = self._findings_prompt(task, plan, check_results)
        existing = self._existing_paths(task)
        file_lines = self._file_lines(task)

        for attempt in range(1, self._limits.max_attempts + 1):
            if model_calls >= self._limits.max_model_calls:
                raise PlanningLimitExceededError(
                    BLOCKED_SECURITY_CALLS, model_calls, self._limits.max_model_calls
                )
            completion = self._call_model(
                task, plan.attempt + attempt, prompt, self._system_prompt()
            )
            model_calls += 1
            usage = usage.merged(completion.usage)
            self._assert_token_budget(usage)

            try:
                payload = parse_proposal_json(completion.content)
                proposal = SecurityFindingsProposal.model_validate(payload)
            except Exception as exc:
                violations = (f"contrato incumplido: {type(exc).__name__}: {exc}",)
                prompt = self._repair_prompt(
                    task,
                    violations,
                    SECURITY_REPAIR_AFTER_FINDINGS_REJECTION,
                    plan=False,
                )
                continue

            validation = validate_findings(
                proposal,
                task,
                plan_paths=plan.target_paths,
                workspace_files=existing,
                file_lines=file_lines,
            )
            if validation.violations:
                violations = validation.violations
                prompt = self._repair_prompt(
                    task, violations, SECURITY_REPAIR_AFTER_FINDINGS_REJECTION, plan=False
                )
                if attempt >= self._limits.max_attempts:
                    return (), True, f"hallazgos inválidos: {'; '.join(violations[:3])}", (
                        model_calls
                    ), usage
                continue

            # Los hallazgos del modelo son del modelo: su fuente la fija PUNTO, no el JSON.
            model_findings = tuple(
                finding.model_copy(update={"sources": (SecurityFindingSource.MODEL_REVIEW,)})
                for finding in validation.findings
            )
            return model_findings, False, "", model_calls, usage

        return (), True, "no se pudieron obtener hallazgos válidos", model_calls, usage

    # ------------------------------------------------------------------ checks
    def _run_checks(
        self, task: SecurityTask, plan: SecurityPlan, workspace: Path
    ) -> tuple[tuple[SecurityCheckOutcome, ...], tuple[SecurityCheckResult, ...]]:
        """Ejecuta los deterministas aplicables y los que pidió el plan."""
        paths = tuple(dict.fromkeys((*plan.target_paths, *task.reviewable_paths)))
        context = SecurityCheckContext(
            workspace=workspace, paths=paths, developer_result=task.developer_result
        )

        requested = list(plan.security_checks)
        for name in self._registry.deterministic_names():
            if name not in requested:
                requested.append(name)

        outcomes: list[SecurityCheckOutcome] = []
        results: list[SecurityCheckResult] = []
        for name in requested:
            check = self._registry.get(name)
            if check is None or not check.available:
                outcomes.append(
                    SecurityCheckOutcome(
                        name=name,
                        ran=False,
                        deterministic=False,
                        detail="check registrado pero no disponible en PUNTO",
                    )
                )
                continue
            self._audit_check_started(task, name)
            result = self._registry.run(name, context)
            results.append(result)
            deterministic = check.deterministic
            outcome = SecurityCheckOutcome(
                name=name,
                ran=True,
                deterministic=deterministic,
                scanned_files=result.scanned,
                findings=len(result.findings),
                detail=result.skipped_reason or "; ".join(result.notes[:3]),
            )
            outcomes.append(outcome)
            self._audit_check_completed(task, outcome)

        return tuple(outcomes), tuple(results)

    def _check_available(self, name: str) -> bool:
        """True si el check está registrado y PUNTO puede ejecutarlo."""
        check = self._registry.get(name)
        return bool(check is not None and check.available)

    def _capability_gaps(
        self, task: SecurityTask, missing: list[str]
    ) -> tuple[CapabilityGap, ...]:
        """Huecos de capacidad de la auditoría: los checks ausentes y lo que exige la tarea."""
        gaps: list[CapabilityGap] = []
        for name in missing:
            check = self._registry.get(name)
            requires = () if check is None else check.requires
            for capability in requires:
                status, detail = capability_status(capability)
                gaps.append(
                    CapabilityGap(
                        capability=canonical_capability(capability),
                        kind=CapabilityKind.VALIDATOR,
                        status=status,
                        required_by=(f"check:{name}",),
                        detail=detail,
                    )
                )
        for capability in task.required_capabilities:
            status, detail = capability_status(capability)
            if status.is_gap:
                canonical = canonical_capability(capability)
                if any(gap.capability == canonical for gap in gaps):
                    continue
                gaps.append(
                    CapabilityGap(
                        capability=canonical,
                        kind=CapabilityKind.EXECUTION_PROFILE,
                        status=status,
                        required_by=("task",),
                        detail=detail,
                    )
                )
        return tuple(gaps)

    def _reviewed_files(
        self,
        plan: SecurityPlan,
        results: tuple[SecurityCheckResult, ...],
        existing: frozenset[str],
    ) -> tuple[str, ...]:
        """Archivos efectivamente revisados: objetivos válidos más los inspeccionados."""
        reviewed: list[str] = []
        for path in (*plan.target_paths, *(f for result in results for f in result.scanned)):
            try:
                relative = normalize_relative_path(path)
            except ValueError:
                continue
            if existing and relative not in existing:
                continue
            if relative not in reviewed:
                reviewed.append(relative)
        return tuple(reviewed)

    # ------------------------------------------------------------------ modelo
    def _call_model(
        self,
        task: SecurityTask,
        attempt: int,
        prompt: str,
        system_prompt: str,
    ) -> ModelCompletion:
        """Llama al modelo y audita inicio, fin y fallo."""
        self._audit_model_started(task, attempt, prompt, system_prompt)
        try:
            completion = self._client.complete_json(
                system_prompt=system_prompt, user_prompt=prompt
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
                BLOCKED_SECURITY_TOKENS, usage.prompt_tokens, self._limits.max_input_tokens
            )
        if usage.completion_tokens > self._limits.max_output_tokens:
            raise PlanningLimitExceededError(
                BLOCKED_SECURITY_TOKENS,
                usage.completion_tokens,
                self._limits.max_output_tokens,
            )

    @staticmethod
    def _system_prompt() -> str:
        """Prompt de sistema, compartido por el plan y los hallazgos."""
        return SECURITY_SYSTEM_PROMPT

    def _plan_prompt(self, task: SecurityTask) -> str:
        """Petición del plan de auditoría."""
        return SECURITY_PLAN_TEMPLATE.format(
            format_reminder=SECURITY_PLAN_FORMAT_REMINDER,
            **self._task_fields(task),
        )

    def _findings_prompt(
        self,
        task: SecurityTask,
        plan: SecurityPlan,
        results: tuple[SecurityCheckResult, ...],
    ) -> str:
        """Petición de hallazgos, con la evidencia determinista ya recogida."""
        check_lines: list[str] = []
        for result in results:
            if not result.findings:
                check_lines.append(f"- {result.name}: sin hallazgos")
                continue
            for finding in result.findings:
                location = finding.file or "(sin archivo)"
                line = f":{finding.line}" if finding.line else ""
                check_lines.append(
                    f"- {result.name}: {finding.severity.value} {location}{line} — {finding.title}"
                )
        fields = self._task_fields(task)
        return SECURITY_FINDINGS_TEMPLATE.format(
            format_reminder=SECURITY_FINDINGS_FORMAT_REMINDER,
            plan_summary=plan.summary,
            plan_targets=_bullets(plan.target_paths),
            plan_areas=_bullets(tuple(area.value for area in plan.analysis_areas)),
            plan_threats=_bullets(plan.threats_considered),
            check_results="\n".join(check_lines) or "(ningún check produjo evidencia)",
            review_content=fields["review_content"],
        )

    def _repair_prompt(
        self,
        task: SecurityTask,
        violations: tuple[str, ...],
        situation: str,
        *,
        plan: bool,
    ) -> str:
        """Petición de reparación, diciendo la verdad sobre el estado."""
        fields = self._task_fields(task)
        return SECURITY_REPAIR_TEMPLATE.format(
            situation=situation,
            violations="\n".join(f"- {item}" for item in violations) or "- sin detalle",
            format_reminder=(
                SECURITY_PLAN_FORMAT_REMINDER if plan else SECURITY_FINDINGS_FORMAT_REMINDER
            ),
            objective=fields["objective"],
            changed_files=fields["changed_files"],
            context_files=fields["context_files"],
        )

    def _task_fields(self, task: SecurityTask) -> dict[str, str]:
        """Campos de la tarea tal como los esperan las plantillas."""
        qa_status = "sin informe de QA"
        if task.qa_report is not None:
            qa_status = f"{task.qa_report.status.value} (CONTEXTO, no prueba de seguridad)"
        return {
            "objective": task.objective,
            "changed_files": _bullets(task.changed_files),
            "context_files": _bullets(task.context_files),
            "developer_claimed_pass": (
                "true (CONTEXTO, no prueba)"
                if task.developer_claimed_pass
                else "false o desconocido"
            ),
            "qa_status": qa_status,
            "acceptance_criteria": _bullets(task.acceptance_criteria),
            "project_spec_context": task.project_spec_context or "(no disponible)",
            "architecture_context": task.architecture_context or "(no disponible)",
            "capability_profile": _bullets(task.capability_profile),
            "available_checks": _bullets(self._registry.available_names()),
            "review_content": self._review_content(task),
        }

    def _review_content(self, task: SecurityTask) -> str:
        """Contenido de los archivos autorizados, acotado y sin silencios."""
        workspace = Path(task.workspace_path)
        chunks: list[str] = []
        for relative in task.reviewable_paths[:30]:
            try:
                normalized = normalize_relative_path(relative)
            except ValueError:
                continue
            path = workspace / normalized
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

    def _existing_paths(self, task: SecurityTask) -> frozenset[str]:
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

    def _file_lines(self, task: SecurityTask) -> dict[str, int]:
        """Número de líneas de cada archivo revisable."""
        root = Path(task.workspace_path)
        lines: dict[str, int] = {}
        for relative in task.reviewable_paths:
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

    # --------------------------------------------------------------- auditoría
    def _audit_request_started(self, task: SecurityTask) -> None:
        if self._audit is None:
            return
        self._audit.log_security_request_started(
            project_id=task.project_id,
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            changed_files=len(task.changed_files),
            developer_claimed_pass=task.developer_claimed_pass,
            qa_status="" if task.qa_report is None else task.qa_report.status.value,
        )

    def _audit_plan_received(
        self, task: SecurityTask, attempt: int, proposal: SecurityPlanProposal
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_security_plan_received(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            targets=len(proposal.review_targets),
            checks=len(proposal.security_checks),
            areas=len(proposal.analysis_areas),
            threats=len(proposal.threats_considered),
        )

    def _audit_plan_accepted(self, task: SecurityTask, attempt: int, plan: SecurityPlan) -> None:
        if self._audit is None:
            return
        self._audit.log_security_plan_accepted(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            targets=list(plan.target_paths),
            checks=list(plan.security_checks),
        )

    def _audit_plan_rejected(
        self, task: SecurityTask, attempt: int, violations: tuple[str, ...]
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_security_plan_rejected(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            violations=violations,
        )

    def _audit_check_started(self, task: SecurityTask, name: str) -> None:
        if self._audit is None:
            return
        self._audit.log_security_check_started(
            project_id=task.project_id, task_id=task.task_id, check=name
        )

    def _audit_check_completed(self, task: SecurityTask, outcome: SecurityCheckOutcome) -> None:
        if self._audit is None:
            return
        self._audit.log_security_check_completed(
            project_id=task.project_id,
            task_id=task.task_id,
            check=outcome.name,
            ran=outcome.ran,
            deterministic=outcome.deterministic,
            findings=outcome.findings,
            detail=outcome.detail,
        )

    def _audit_finding(self, task: SecurityTask, finding: SecurityFinding) -> None:
        if self._audit is None:
            return
        self._audit.log_security_finding_recorded(
            project_id=task.project_id,
            task_id=task.task_id,
            finding_id=finding.id,
            severity=finding.severity.value,
            category=finding.category.value,
            file=finding.file,
        )

    def _audit_blocked(self, task: SecurityTask, capability: str, detail: str) -> None:
        if self._audit is None:
            return
        self._audit.log_security_blocked(
            project_id=task.project_id,
            task_id=task.task_id,
            reason=BLOCKED_SECURITY_CAPABILITY,
            detail=f"{capability}: {detail}",
        )

    def _audit_completed(self, task: SecurityTask, report: SecurityReport) -> None:
        if self._audit is None:
            return
        for finding in report.findings:
            self._audit_finding(task, finding)
        self._audit.log_security_completed(
            project_id=task.project_id,
            task_id=task.task_id,
            status=report.status.value,
            findings=len(report.findings),
            blocking_findings=len(report.blocking_findings),
            highest_severity=(
                "" if report.highest_severity is None else report.highest_severity.value
            ),
            capability_gaps=len(report.capability_gaps),
            total_tokens=report.model_usage.total_tokens,
        )

    def _audit_model_started(
        self, task: SecurityTask, attempt: int, prompt: str, system_prompt: str
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_started(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            prompt_chars=len(prompt) + len(system_prompt),
        )

    def _audit_model_completed(
        self, task: SecurityTask, attempt: int, completion: ModelCompletion
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

    def _audit_model_failed(self, task: SecurityTask, attempt: int, detail: str) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_failed(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            error=detail,
        )


__all__ = [
    "BLOCKED_SECURITY_ATTEMPTS",
    "BLOCKED_SECURITY_CALLS",
    "BLOCKED_SECURITY_CAPABILITY",
    "BLOCKED_SECURITY_TOKENS",
    "MAX_CONTEXT_FILE_CHARS",
    "DeepSeekSecurityRunner",
]
