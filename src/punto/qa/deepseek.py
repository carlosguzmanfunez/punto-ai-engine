"""QA real sobre DeepSeek (ENGINE-4).

Implementa :class:`~punto.qa.base.QARunner` reutilizando el ``DeepSeekClient`` y toda
la maquinaria de ejecución ya existente: ``ShellRunner`` con backend inyectado,
``Validator`` y ``ContainerSandboxBackend`` verificado. **No** se duplica cliente HTTP
ni se inventa una vía de ejecución nueva.

Ciclo completo, con la validación de PUNTO en cada frontera:

1. QA propone un ``QAPlan`` (modelo).
2. PUNTO valida el plan entero y lo rechaza si incumple un invariante (§13).
3. PUNTO comprueba capacidades: si falta la que un check necesita, la evaluación es
   ``BLOCKED`` por hueco de capacidad, sin ejecutar nada en el host (§12).
4. PUNTO crea un overlay desechable, escribe las pruebas **validadas** y ejecuta los
   checks en el sandbox verificado (§9 y §10).
5. PUNTO clasifica los fallos: defecto del producto, prueba de QA rota, infraestructura
   o capacidad ausente (§15).
6. Solo si el fallo es de la prueba de QA, QA puede repararla dentro de su presupuesto
   —sin tocar la expectativa del producto— (§23).
7. PUNTO calcula el estado final a partir de la evidencia (§17).
"""

from __future__ import annotations

from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING

from punto.common import utc_now
from punto.developer.backend import ExecutionBackend, require_sandbox_backend
from punto.developer.context import ExecutionContext
from punto.providers.deepseek import (
    DeepSeekClient,
    DeepSeekError,
    ModelCompletion,
    parse_proposal_json,
)
from punto.qa.base import QALimits, QARunner
from punto.qa.capabilities import (
    blocking_capability_gaps,
    detect_qa_capability_gaps,
)
from punto.qa.checks import DEFAULT_CHECK_REGISTRY, ValidationCheckRegistry
from punto.qa.overlay import QAOverlay
from punto.qa.paths import normalize_relative_path
from punto.qa.prompts import (
    QA_FORMAT_REMINDER,
    QA_PROMPT_VERSION,
    QA_REPAIR_AFTER_PROVIDER_ERROR,
    QA_REPAIR_AFTER_REJECTION,
    QA_REPAIR_AFTER_TEST_FAILURE,
    QA_REPAIR_TEMPLATE,
    QA_SYSTEM_PROMPT,
    QA_USER_TEMPLATE,
)
from punto.qa.report import (
    QATestFileOutcome,
    build_report,
    case_failures_from_output,
    classify_check_failure,
)
from punto.qa.validation import validate_qa_plan
from punto.schemas.execution import (
    CommandSpec,
    ExecutionTrustLevel,
    ModelUsage,
    ValidationCheck,
    ValidationResult,
)
from punto.schemas.planning import CapabilityGap
from punto.schemas.qa import (
    MAX_EVIDENCE_CHARS,
    QAExecutedCheck,
    QAFailureCategory,
    QAPlan,
    QAPlanProposal,
    QAReport,
    QATask,
)
from punto.tools.errors import (
    DeveloperExecutionError,
    PlanningLimitExceededError,
    SandboxRequiredError,
    SandboxUnavailableError,
    UntrustedExecutionDeniedError,
)
from punto.tools.shell import ShellRunner
from punto.tools.validator import Validator

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger

#: Motivos de bloqueo de QA por límites.
BLOCKED_QA_ATTEMPTS: str = "MAX_QA_ATTEMPTS_EXCEEDED"
BLOCKED_QA_CALLS: str = "MAX_QA_MODEL_CALLS_EXCEEDED"
BLOCKED_QA_TOKENS: str = "MAX_QA_TOKENS_EXCEEDED"
BLOCKED_QA_CAPABILITY: str = "CAPABILITY_REQUIRED"
BLOCKED_QA_SANDBOX: str = "SANDBOX_REQUIRED"

#: Prefijo del check sintético que ejecuta un archivo de prueba concreto.
PER_FILE_CHECK_PREFIX: str = "pytest-file"


def _bullets(items: tuple[str, ...]) -> str:
    """Lista legible para el prompt, o un marcador explícito si está vacía."""
    if not items:
        return "(no declarado)"
    return "\n".join(f"- {item}" for item in items)


class DeepSeekQARunner(QARunner):
    """QA independiente: propone pruebas con DeepSeek, ejecuta PUNTO."""

    def __init__(
        self,
        *,
        client: DeepSeekClient,
        backend: ExecutionBackend | None = None,
        audit: AuditLogger | None = None,
        limits: QALimits | None = None,
        registry: ValidationCheckRegistry | None = None,
    ) -> None:
        self._client = client
        self._backend = backend
        self._audit = audit
        self._limits = limits or QALimits()
        self._registry = registry or DEFAULT_CHECK_REGISTRY

    # ------------------------------------------------------------------ estado
    @property
    def name(self) -> str:
        """Nombre del runner."""
        return "DeepSeekQARunner"

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
        return QA_PROMPT_VERSION

    @property
    def uses_ai(self) -> bool:
        """Siempre ``True``: este runner consulta un modelo externo."""
        return True

    @property
    def limits(self) -> QALimits:
        """Presupuesto configurado."""
        return self._limits

    @property
    def registry(self) -> ValidationCheckRegistry:
        """Registro de checks permitidos."""
        return self._registry

    # -------------------------------------------------------------- evaluación
    def evaluate(self, task: QATask) -> QAReport:
        """Evalúa el trabajo del Developer contra los criterios de aceptación.

        Nunca lanza por un fallo de la evaluación: lo traduce a un ``QAReport``.
        """
        started_at = utc_now()
        usage = ModelUsage()
        model_calls = 0
        attempts_used = 0
        test_repairs = 0
        plan: QAPlan | None = None
        evidence: list[str] = []
        error = ""
        planning_blocked = False
        capability_blocked = False
        gaps: tuple[CapabilityGap, ...] = ()
        executed: tuple[QAExecutedCheck, ...] = ()
        file_outcomes: tuple[QATestFileOutcome, ...] = ()
        case_outcomes: dict[str, QAFailureCategory | None] = {}
        raw_checks: tuple[ValidationCheck, ...] = ()
        workspace = ""

        self._audit_request_started(task)

        try:
            backend = self._resolve_backend(task)
            plan, attempts_used, model_calls, usage, evidence, gap_list = self._obtain_plan(
                task, usage, model_calls
            )
            gaps = gap_list
            if plan is None:
                planning_blocked = True
                error = evidence[-1] if evidence else "no se obtuvo un plan de QA válido"
            else:
                blocking = blocking_capability_gaps(
                    gaps, plan_checks=plan.checks, registry=self._registry
                )
                if blocking:
                    capability_blocked = True
                    error = (
                        f"{BLOCKED_QA_CAPABILITY}: faltan capacidades para ejecutar los "
                        f"checks elegidos: {', '.join(gap.capability for gap in blocking)}"
                    )
                    for gap in blocking:
                        self._audit_blocked(task, gap.capability, gap.detail)
                    evidence.append(error)
                else:
                    (
                        plan,
                        executed,
                        file_outcomes,
                        case_outcomes,
                        raw_checks,
                        workspace,
                        test_repairs,
                        model_calls,
                        usage,
                        execution_evidence,
                        repair_error,
                    ) = self._execute_with_repairs(
                        task=task,
                        plan=plan,
                        backend=backend,
                        usage=usage,
                        model_calls=model_calls,
                        test_repairs=test_repairs,
                    )
                    evidence.extend(execution_evidence)
                    if repair_error:
                        error = repair_error
        except (SandboxRequiredError, SandboxUnavailableError) as exc:
            planning_blocked = True
            error = f"{BLOCKED_QA_SANDBOX}: {exc}"
            evidence.append(error)
        except UntrustedExecutionDeniedError as exc:
            planning_blocked = True
            error = f"UNTRUSTED_EXECUTION_DENIED: {exc}"
            evidence.append(error)
        except PlanningLimitExceededError as exc:
            planning_blocked = True
            error = str(exc)
            evidence.append(error)
        except DeepSeekError as exc:
            planning_blocked = True
            error = self._client.redact(str(exc))
            evidence.append(error)
        except (DeveloperExecutionError, OSError, ValueError) as exc:
            planning_blocked = True
            error = str(exc)
            evidence.append(error)

        report = build_report(
            task=task,
            plan=plan,
            executed_checks=executed,
            file_outcomes=file_outcomes,
            case_outcomes=case_outcomes,
            raw_checks=raw_checks,
            capability_gaps=gaps,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            model_calls=model_calls,
            attempts=attempts_used,
            test_repairs=test_repairs,
            model_usage=usage,
            workspace=workspace,
            started_at=started_at,
            completed_at=utc_now(),
            planning_blocked=planning_blocked,
            capability_blocked=capability_blocked,
            error=error,
            extra_evidence=tuple(evidence),
        )
        self._audit_completed(task, report)
        return report

    # ---------------------------------------------------------------- backend
    def _resolve_backend(self, task: QATask) -> ExecutionBackend:
        """Exige un sandbox verificado: el código de QA es código de modelo."""
        del task
        return require_sandbox_backend(self._backend)

    # ------------------------------------------------------------------- plan
    def _obtain_plan(
        self,
        task: QATask,
        usage: ModelUsage,
        model_calls: int,
    ) -> tuple[QAPlan | None, int, int, ModelUsage, list[str], tuple[CapabilityGap, ...]]:
        """Pide un plan a QA y lo valida, con reparación acotada."""
        evidence: list[str] = []
        violations: tuple[str, ...] = ()
        gaps: tuple[CapabilityGap, ...] = ()
        proposal: QAPlanProposal | None = None

        for attempt in range(1, self._limits.max_attempts + 1):
            if model_calls >= self._limits.max_model_calls:
                raise PlanningLimitExceededError(
                    BLOCKED_QA_CALLS, model_calls, self._limits.max_model_calls
                )

            prompt = (
                self._initial_prompt(task)
                if attempt == 1
                else self._repair_prompt(
                    task,
                    violations,
                    evidence[-1] if evidence else "",
                    QA_REPAIR_AFTER_REJECTION
                    if violations
                    else QA_REPAIR_AFTER_PROVIDER_ERROR,
                )
            )
            completion = self._call_model(task, attempt, prompt)
            model_calls += 1
            usage = usage.merged(completion.usage)
            self._assert_token_budget(usage)

            try:
                payload = parse_proposal_json(completion.content)
                proposal = QAPlanProposal.model_validate(payload)
            except Exception as exc:  # se traduce a violaciones, nunca a excepción
                reason = f"contrato incumplido: {type(exc).__name__}: {exc}"
                violations = (reason,)
                evidence.append(reason)
                self._audit_plan_rejected(task, attempt, violations)
                continue

            self._audit_plan_received(task, attempt, proposal)

            existing = self._existing_paths(task)
            validation = validate_qa_plan(
                proposal,
                task,
                registry=self._registry,
                existing_paths=existing,
            )
            gaps = detect_qa_capability_gaps(task=task, proposal=proposal, registry=self._registry)

            if not validation.valid:
                violations = validation.violations
                evidence.append(
                    "plan rechazado: " + "; ".join(violations[:5])
                    + ("…" if len(violations) > 5 else "")
                )
                self._audit_plan_rejected(task, attempt, violations)
                continue

            plan = QAPlan(
                task_id=task.task_id,
                project_id=task.project_id,
                attempt=attempt,
                prompt_version=self.prompt_version,
                summary=proposal.summary,
                test_cases=proposal.test_cases,
                test_file_changes=proposal.test_file_changes,
                checks=proposal.checks,
                coverage_mapping=proposal.coverage_mapping,
                assumptions=proposal.assumptions,
            )
            self._audit_plan_accepted(task, attempt, plan)
            return plan, attempt, model_calls, usage, evidence, gaps

        raise PlanningLimitExceededError(
            BLOCKED_QA_ATTEMPTS, self._limits.max_attempts, self._limits.max_attempts
        )

    def _existing_paths(self, task: QATask) -> frozenset[str]:
        """Rutas que ya existen en el workspace candidato."""
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

    # -------------------------------------------------------------- ejecución
    def _execute_with_repairs(
        self,
        *,
        task: QATask,
        plan: QAPlan,
        backend: ExecutionBackend,
        usage: ModelUsage,
        model_calls: int,
        test_repairs: int,
    ) -> tuple[
        QAPlan,
        tuple[QAExecutedCheck, ...],
        tuple[QATestFileOutcome, ...],
        dict[str, QAFailureCategory | None],
        tuple[ValidationCheck, ...],
        str,
        int,
        int,
        ModelUsage,
        list[str],
        str,
    ]:
        """Ejecuta el plan en un overlay y repara **las pruebas de QA** si están rotas.

        Un fallo del producto no se repara aquí: se devuelve tal cual para que el
        veredicto sea ``FAIL``. Solo ``QA_TEST_FAILURE`` habilita otro intento.
        """
        evidence: list[str] = []
        workspace = ""

        for repair_round in range(self._limits.max_test_repairs + 1):
            overlay_root = Path(mkdtemp(prefix="punto-qa-overlay-"))
            workspace = str(overlay_root / "workspace")
            overlay = QAOverlay(source=Path(task.workspace_path), destination=Path(workspace))
            try:
                overlay.prepare()
                overlay.apply(plan.test_file_changes, test_only_paths=task.test_only_paths)
                evidence.append(
                    f"overlay efímero con {len(overlay.written)} archivo(s) de prueba: "
                    f"{', '.join(overlay.written)}"
                )
                executed, file_outcomes, case_outcomes, raw_checks = self._run_checks(
                    task=task, plan=plan, backend=backend, workspace=Path(workspace)
                )
            finally:
                overlay.destroy()

            broken_tests = [
                outcome
                for outcome in file_outcomes
                if outcome.failure is QAFailureCategory.QA_TEST_FAILURE
            ]
            product_failures = [
                outcome
                for outcome in file_outcomes
                if outcome.failure is QAFailureCategory.PRODUCT_FAILURE
            ]
            product_failed_cases = [
                case_id
                for case_id, failure in case_outcomes.items()
                if failure is QAFailureCategory.PRODUCT_FAILURE
            ]

            if product_failures or product_failed_cases:
                detail = ", ".join(
                    outcome.path for outcome in product_failures
                ) or ", ".join(product_failed_cases)
                evidence.append(f"fallo del producto detectado en: {detail}")
                return (
                    plan, executed, file_outcomes, case_outcomes, raw_checks, workspace,
                    test_repairs, model_calls, usage, evidence, "",
                )

            if not broken_tests or repair_round >= self._limits.max_test_repairs:
                if broken_tests:
                    evidence.append(
                        "pruebas de QA inválidas sin presupuesto de reparación: "
                        + ", ".join(outcome.path for outcome in broken_tests)
                    )
                return (
                    plan, executed, file_outcomes, case_outcomes, raw_checks, workspace,
                    test_repairs, model_calls, usage, evidence, "",
                )

            # Solo aquí QA toca SU prueba.
            detail = "; ".join(
                f"{outcome.path}: {outcome.output_excerpt[:300]}" for outcome in broken_tests
            )
            evidence.append(f"reparación de prueba de QA solicitada: {detail[:300]}")
            if model_calls >= self._limits.max_model_calls:
                return (
                    plan, executed, file_outcomes, case_outcomes, raw_checks, workspace,
                    test_repairs, model_calls, usage, evidence, BLOCKED_QA_CALLS,
                )

            repaired = self._repair_tests(task=task, plan=plan, detail=detail)
            if repaired is None:
                return (
                    plan, executed, file_outcomes, case_outcomes, raw_checks, workspace,
                    test_repairs, model_calls, usage, evidence,
                    "no se pudo reparar la prueba de QA",
                )
            plan, completion = repaired
            model_calls += 1
            usage = usage.merged(completion.usage)
            self._assert_token_budget(usage)
            test_repairs += 1
            evidence.append("plan de QA reparado y reejecutado")

        return (
            plan, (), (), {}, (), workspace, test_repairs, model_calls, usage, evidence,
            BLOCKED_QA_ATTEMPTS,
        )

    def _run_checks(
        self,
        *,
        task: QATask,
        plan: QAPlan,
        backend: ExecutionBackend,
        workspace: Path,
    ) -> tuple[
        tuple[QAExecutedCheck, ...],
        tuple[QATestFileOutcome, ...],
        dict[str, QAFailureCategory | None],
        tuple[ValidationCheck, ...],
    ]:
        """Ejecuta los checks del plan y una pasada por archivo de prueba de QA.

        La pasada por archivo permite atribuir cada fallo al **caso de prueba** que lo
        produjo, y con ello al criterio de aceptación concreto. Sin esa atribución, una
        sola aserción rota declararía incumplido todo el contrato.
        """
        context = ExecutionContext(
            task_id=task.task_id,
            workspace_path=Path(workspace),
            branch_name="qa/evaluation",
            trust_level=ExecutionTrustLevel.UNTRUSTED_MODEL,
            attempts_allowed=1,
        )
        shell = ShellRunner(context, backend=backend)
        validator = Validator(context, shell)

        specs: tuple[CommandSpec, ...] = self._registry.specs(plan.checks)
        self._audit_execution_started(task, plan, workspace=str(workspace))

        result: ValidationResult = validator.validate(
            specs, max_timeout_seconds=self._limits.check_timeout_seconds
        )
        raw_checks = tuple(result.checks)
        executed: list[QAExecutedCheck] = []
        for check in raw_checks:
            failure = classify_check_failure(check)
            executed.append(QAExecutedCheck.from_validation_check(check, failure=failure))
            self._audit_check_completed(task, check, failure)

        file_outcomes: list[QATestFileOutcome] = []
        case_outcomes: dict[str, QAFailureCategory | None] = {}
        runs_pytest = "pytest" in {name.lower() for name in plan.checks}
        for test_file in plan.test_file_changes:
            if not runs_pytest:
                break
            relative = normalize_relative_path(test_file.path)
            spec = CommandSpec(
                name=f"{PER_FILE_CHECK_PREFIX}:{relative}",
                executable="python",
                # ``-rA`` fuerza el resumen completo de resultados: sin él, un
                # ``addopts = "-q"`` del proyecto oculta las pruebas que pasaron y la
                # atribución por caso deja de ser fiable.
                args=("-m", "pytest", "-q", "-rA", relative),
                timeout_seconds=self._limits.check_timeout_seconds,
            )
            per_file = validator.validate(
                (spec,), max_timeout_seconds=self._limits.check_timeout_seconds
            )
            check = per_file.checks[0]
            failure = classify_check_failure(check)
            excerpt = "\n".join(part for part in (check.stdout, check.stderr) if part).strip()
            file_outcomes.append(
                QATestFileOutcome(
                    path=relative,
                    check_name=spec.name,
                    passed=check.passed,
                    failure=failure,
                    output_excerpt=excerpt[:MAX_EVIDENCE_CHARS],
                )
            )
            case_outcomes.update(
                case_failures_from_output(
                    excerpt,
                    test_file.test_case_ids,
                    file_failure=failure,
                )
            )
            executed.append(
                QAExecutedCheck(
                    name=spec.name,
                    command=spec.executable,
                    exit_code=check.exit_code,
                    passed=check.passed,
                    timed_out=check.timed_out,
                    blocked=check.blocked,
                    duration_ms=check.duration_ms,
                    output_excerpt=excerpt[:MAX_EVIDENCE_CHARS],
                    failure=failure,
                )
            )
            self._audit_check_completed(task, check, failure)

        return tuple(executed), tuple(file_outcomes), case_outcomes, raw_checks

    def _repair_tests(
        self, *, task: QATask, plan: QAPlan, detail: str
    ) -> tuple[QAPlan, ModelCompletion] | None:
        """Pide a QA que repare **sus propias pruebas**."""
        prompt = self._repair_prompt(
            task, tuple(plan.checks), detail, QA_REPAIR_AFTER_TEST_FAILURE
        )
        completion = self._call_model(task, plan.attempt + 1, prompt)
        try:
            payload = parse_proposal_json(completion.content)
            proposal = QAPlanProposal.model_validate(payload)
        except Exception:
            return None

        validation = validate_qa_plan(
            proposal,
            task,
            registry=self._registry,
            existing_paths=self._existing_paths(task),
        )
        if not validation.valid:
            self._audit_plan_rejected(task, plan.attempt + 1, validation.violations)
            return None
        self._audit_plan_accepted(task, plan.attempt + 1, plan)
        return (
            QAPlan(
                task_id=task.task_id,
                project_id=task.project_id,
                attempt=plan.attempt + 1,
                prompt_version=self.prompt_version,
                summary=proposal.summary,
                test_cases=proposal.test_cases,
                test_file_changes=proposal.test_file_changes,
                checks=proposal.checks,
                coverage_mapping=proposal.coverage_mapping,
                assumptions=proposal.assumptions,
            ),
            completion,
        )

    # ------------------------------------------------------------------ modelo
    def _call_model(self, task: QATask, attempt: int, prompt: str) -> ModelCompletion:
        """Llama al modelo y audita inicio, fin y fallo."""
        self._audit_model_started(task, attempt, prompt)
        try:
            completion = self._client.complete_json(
                system_prompt=QA_SYSTEM_PROMPT, user_prompt=prompt
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
                BLOCKED_QA_TOKENS, usage.prompt_tokens, self._limits.max_input_tokens
            )
        if usage.completion_tokens > self._limits.max_output_tokens:
            raise PlanningLimitExceededError(
                BLOCKED_QA_TOKENS, usage.completion_tokens, self._limits.max_output_tokens
            )

    def _initial_prompt(self, task: QATask) -> str:
        """Petición de evaluación inicial."""
        return QA_USER_TEMPLATE.format(
            format_reminder=QA_FORMAT_REMINDER,
            **self._task_fields(task),
        )

    def _repair_prompt(
        self, task: QATask, violations: tuple[str, ...], detail: str, situation: str
    ) -> str:
        """Petición de reparación, diciendo la verdad sobre el estado.

        La situación se recibe explícitamente en lugar de deducirse del texto: confundir
        «tu plan es inválido» con «tu prueba está rota» cambiaría lo que se le pide al
        modelo, y con ello la probabilidad de que «arregle» la expectativa.
        """
        listed = violations or (detail or "sin detalle",)
        numbered = tuple(
            f"{criterion_id}: {statement}"
            for criterion_id, statement in task.criteria_by_id.items()
        )
        return QA_REPAIR_TEMPLATE.format(
            situation=situation,
            evidence="\n".join(f"- {item}" for item in listed),
            format_reminder=QA_FORMAT_REMINDER,
            objective=task.objective,
            changed_files=_bullets(task.changed_files),
            developer_claimed_pass=(
                "true (CONTEXTO, no prueba)"
                if task.developer_claimed_pass
                else "false o desconocido"
            ),
            acceptance_criteria=_bullets(numbered),
            available_checks=_bullets(self._registry.available_names()),
        )

    def _task_fields(self, task: QATask) -> dict[str, str]:
        """Campos de la tarea tal como los espera la plantilla de evaluación."""
        numbered = tuple(
            f"{criterion_id}: {statement}"
            for criterion_id, statement in task.criteria_by_id.items()
        )
        developer_summary = "sin resultado del Developer"
        if task.developer_result is not None:
            result = task.developer_result
            changed = ", ".join(change.path for change in result.files_changed) or "ninguno"
            developer_summary = (
                f"status={result.status.value}, files_changed={changed}, "
                f"commit={result.commit_sha or 'sin commit'}"
            )
        return {
            "objective": task.objective,
            "changed_files": _bullets(task.changed_files),
            "context_files": _bullets(task.context_files),
            "validation_checks": _bullets(task.validation_checks),
            "developer_claimed_pass": (
                "true (CONTEXTO, no prueba: no lo uses como evidencia)"
                if task.developer_claimed_pass
                else "false o desconocido"
            ),
            "developer_summary": developer_summary,
            "acceptance_criteria": _bullets(numbered),
            "project_spec_context": task.project_spec_context or "(no disponible)",
            "architecture_context": task.architecture_context or "(no disponible)",
            "capability_profile": _bullets(task.capability_profile),
            "available_checks": _bullets(self._registry.available_names()),
            "changed_files_content": DeepSeekQARunner._files_content(task),
        }

    @staticmethod
    def _files_content(task: QATask) -> str:
        """Contenido de los archivos relevantes, acotado y sin silencios."""
        root = Path(task.workspace_path)
        wanted = tuple(dict.fromkeys((*task.changed_files, *task.context_files)))
        if not wanted:
            return "(no se declararon archivos)"
        chunks: list[str] = []
        for relative in wanted[:20]:
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
            if len(content) > 20_000:
                content = f"{content[:20_000]}\n…[recortado de {len(content)} caracteres]"
            chunks.append(f"=== {normalized} ===\n{content}")
        return "\n\n".join(chunks)

    # --------------------------------------------------------------- auditoría
    def _audit_request_started(self, task: QATask) -> None:
        if self._audit is None:
            return
        self._audit.log_qa_request_started(
            project_id=task.project_id,
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            acceptance_criteria=len(task.acceptance_criteria),
            developer_claimed_pass=task.developer_claimed_pass,
        )

    def _audit_plan_received(
        self, task: QATask, attempt: int, proposal: QAPlanProposal
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_qa_plan_received(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            test_cases=len(proposal.test_cases),
            test_files=len(proposal.test_file_changes),
            checks=len(proposal.checks),
            coverage=len(proposal.coverage_mapping),
        )

    def _audit_plan_accepted(self, task: QATask, attempt: int, plan: QAPlan) -> None:
        if self._audit is None:
            return
        self._audit.log_qa_plan_accepted(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            test_cases=len(plan.test_cases),
            checks=list(plan.checks),
        )

    def _audit_plan_rejected(
        self, task: QATask, attempt: int, violations: tuple[str, ...]
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_qa_plan_rejected(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt=attempt,
            violations=violations,
        )

    def _audit_execution_started(self, task: QATask, plan: QAPlan, *, workspace: str) -> None:
        if self._audit is None:
            return
        self._audit.log_qa_execution_started(
            project_id=task.project_id,
            task_id=task.task_id,
            workspace=workspace,
            checks=list(plan.checks),
            trust_level=ExecutionTrustLevel.UNTRUSTED_MODEL.value,
        )

    def _audit_check_completed(
        self,
        task: QATask,
        check: ValidationCheck,
        failure: object,
    ) -> None:
        if self._audit is None:
            return
        category = getattr(failure, "value", "") if failure is not None else ""
        self._audit.log_qa_check_completed(
            project_id=task.project_id,
            task_id=task.task_id,
            check=check.name,
            passed=check.passed,
            exit_code=check.exit_code,
            duration_ms=check.duration_ms,
            failure=category,
        )

    def _audit_blocked(self, task: QATask, capability: str, detail: str) -> None:
        if self._audit is None:
            return
        self._audit.log_qa_blocked(
            project_id=task.project_id,
            task_id=task.task_id,
            reason=BLOCKED_QA_CAPABILITY,
            detail=f"{capability}: {detail}",
        )

    def _audit_completed(self, task: QATask, report: QAReport) -> None:
        if self._audit is None:
            return
        self._audit.log_qa_completed(
            project_id=task.project_id,
            task_id=task.task_id,
            status=report.status.value,
            findings=len(report.findings),
            product_failures=len(report.product_failures),
            capability_gaps=len(report.capability_gaps),
            total_tokens=report.model_usage.total_tokens,
        )

    def _audit_model_started(self, task: QATask, attempt: int, prompt: str) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_started(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            prompt_chars=len(prompt),
        )

    def _audit_model_completed(
        self, task: QATask, attempt: int, completion: ModelCompletion
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

    def _audit_model_failed(self, task: QATask, attempt: int, detail: str) -> None:
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
    "BLOCKED_QA_ATTEMPTS",
    "BLOCKED_QA_CALLS",
    "BLOCKED_QA_CAPABILITY",
    "BLOCKED_QA_SANDBOX",
    "BLOCKED_QA_TOKENS",
    "PER_FILE_CHECK_PREFIX",
    "DeepSeekQARunner",
]
