"""Endurecimiento del gasto incierto (ENGINE-6.1, hallazgo N6-02).

Claude Opus no encontró una reproducción actual, pero la estructura permitía esto: el kernel llama
al proveedor, el rol gasta, y **antes** de devolver el resultado salta un error técnico; el
reintento técnico del kernel volvía a llamar dentro de la misma reserva, así que un solo presupuesto
podía gastar dos veces sin que nadie lo supiera.

La política que se comprueba aquí es conservadora y explícita: una invocación que **pudo** gastar
modelo no se reintenta a ciegas. Los reintentos de transporte viven en el cliente del proveedor. La
única excepción es el fallo que la propia frontera declara como no facturable —un proveedor no
disponible: la petición no llegó a salir—, y un rol determinista conserva su reintento acotado
porque no hay gasto que duplicar.
"""

from __future__ import annotations

from pathlib import Path

from punto.audit.logger import AuditLogger
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.enums import TaskStatus
from punto.schemas.workflow import (
    ModelCallLimits,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowFailureCode,
)
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.errors import WorkflowProviderUnavailableError
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from workflow_support import all_stage_executors, make_request, role_sequence


def config_dir_of_repo() -> Path:
    """Directorio ``config/`` del repositorio, para el Policy Engine real."""
    return Path(__file__).resolve().parents[1] / "config"


class CountingFailingExecutor:
    """Ejecutor que cuenta cada invocación y falla las primeras ``failures`` veces.

    Declara si puede gastar modelo: es la diferencia entre un rol determinista —que conserva el
    reintento técnico acotado— y una invocación de IA, que no se reintenta a ciegas.
    """

    def __init__(
        self,
        role: RoleName,
        *,
        uses_ai: bool,
        error: Exception,
        failures: int = 1,
    ) -> None:
        self.role = role
        self.uses_ai = uses_ai
        self.error = error
        self.failures = failures
        self.calls: list[RoleExecutionRequest] = []

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Cuenta la invocación y falla, o devuelve un resultado correcto."""
        self.calls.append(request)
        if len(self.calls) <= self.failures:
            raise self.error
        return RoleExecutionResult(
            role=self.role,
            status=RoleStatus.COMPLETED,
            summary="rol completado tras el reintento",
            model_calls=1 if self.uses_ai else 0,
        )

    def capability(self, role: RoleName) -> None:
        """``None``: este doble no declara proveedor."""
        return None

    def model_limits(self, role: RoleName) -> ModelCallLimits | None:
        """Cota declarada: determinista si ``uses_ai`` es falso; con modelo y una llamada si no."""
        if role is not self.role:
            return None
        if not self.uses_ai:
            return ModelCallLimits(uses_ai=False)
        return ModelCallLimits(
            uses_ai=True, max_model_calls=2, max_input_tokens=500, max_output_tokens=500
        )


def kernel_with(
    tmp_path: Path, executor: CountingFailingExecutor, *, max_model_calls: int = 8
) -> tuple[WorkflowKernel, CountingFailingExecutor]:
    """Kernel real con el ejecutor del caso en el Architect."""
    executors: dict[RoleName, object] = dict(all_stage_executors(cross_audit_required=False))
    executors[RoleName.ARCHITECT] = executor
    kernel = WorkflowKernel(
        executors=executors,
        store=FileCheckpointStore(tmp_path / "cp"),
        audit=AuditLogger(),
        policy=WorkflowPolicy(
            engine=PolicyEngine.from_config(config_dir_of_repo()), gate=HumanGate()
        ),
    )
    return kernel, executor


def test_an_ai_invocation_is_not_retried_after_a_technical_error(tmp_path: Path) -> None:
    """N6-02 (matriz 26): el proveedor se llama **una** vez y nunca se reintenta a ciegas.

    El error es un timeout del proveedor: pudo salir la petición y pudo facturarse, así que el gasto
    es incierto y el workflow se bloquea declarándolo, con la reserva comprometida.
    """
    executor = CountingFailingExecutor(
        RoleName.ARCHITECT,
        uses_ai=True,
        error=WorkflowProviderUnavailableError("timeout tras enviar la petición"),
        failures=1,
    )
    # El error de este caso dice «proveedor no disponible», que la frontera declara no facturable;
    # para forzar el caso facturable se usa un error de rol con código distinto.
    executor.error = _billable_error()
    kernel, _ = kernel_with(tmp_path, executor)

    run = kernel.run_all(make_request(cross_audit_required=False, budget=WorkflowBudget()))

    assert len(executor.calls) == 1, "una invocación con gasto posible no se reintenta"
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_MODEL_SPEND_RECONCILIATION_REQUIRED
    assert RoleName.PLANNER.value not in role_sequence(run), "no se avanza de etapa"


def test_a_non_billable_failure_may_be_retried(tmp_path: Path) -> None:
    """La excepción documentada: si la petición no llegó a salir, reintentar no duplica gasto."""
    executor = CountingFailingExecutor(
        RoleName.ARCHITECT,
        uses_ai=True,
        error=WorkflowProviderUnavailableError("no se pudo ni abrir la conexión"),
        failures=1,
    )
    kernel, _ = kernel_with(tmp_path, executor)

    run = kernel.run_all(make_request(cross_audit_required=False, budget=WorkflowBudget()))

    assert len(executor.calls) == 2, "un fallo no facturable conserva el reintento acotado"
    assert run.status is not TaskStatus.FAILED


def test_a_deterministic_role_keeps_its_bounded_retry(tmp_path: Path) -> None:
    """N6-02 (matriz 27): un rol determinista sí reintenta, porque no hay gasto de modelo."""
    executor = CountingFailingExecutor(
        RoleName.ARCHITECT,
        uses_ai=False,
        error=WorkflowProviderUnavailableError("tropiezo determinista"),
        failures=1,
    )
    kernel, _ = kernel_with(tmp_path, executor)

    run = kernel.run_all(make_request(cross_audit_required=False, budget=WorkflowBudget()))

    assert len(executor.calls) == 2, "el reintento técnico acotado sigue disponible"
    assert run.status is not TaskStatus.FAILED


def test_the_unknown_spend_stays_reserved(tmp_path: Path) -> None:
    """La reserva del intento fallido no se libera: es lo que impide que el gasto se evapore."""
    executor = CountingFailingExecutor(
        RoleName.ARCHITECT, uses_ai=True, error=_billable_error(), failures=1
    )
    kernel, _ = kernel_with(tmp_path, executor)

    run = kernel.run_all(make_request(cross_audit_required=False, budget=WorkflowBudget()))

    assert run.usage.model_calls_reserved >= 1, "el gasto incierto sigue comprometido"
    assert run.usage.known_budget_overrun_model_calls == 0, "no se inventa un sobregasto conocido"
    durable = FileCheckpointStore(tmp_path / "cp").load(run.workflow_id)
    assert durable.usage.model_calls_reserved >= 1


def test_a_provider_without_credentials_never_burns_a_model_call(tmp_path: Path) -> None:
    """Un rol sin credencial no gasta: el código lo dice y no se cuenta ningún consumo."""

    class PendingCredentials:
        """Rol sin credencial: declara el estado y **no** reporta consumo de modelo."""

        role = RoleName.ARCHITECT

        def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
            """Devuelve el resultado sin credencial, con consumo cero."""
            return RoleExecutionResult(
                role=self.role,
                status=RoleStatus.PENDING_CREDENTIALS,
                summary="sin credencial",
                error_code=WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
                error_detail="CREDENTIAL_REQUIRED",
            )

        def capability(self, role: RoleName) -> None:
            """``None``: no hay proveedor que declarar."""
            return None

        def model_limits(self, role: RoleName) -> ModelCallLimits | None:
            """Cota declarada: una llamada autorizada que no se llega a gastar."""
            if role is not self.role:
                return None
            return ModelCallLimits(
                uses_ai=True, max_model_calls=1, max_input_tokens=500, max_output_tokens=500
            )

    executors: dict[RoleName, object] = dict(all_stage_executors(cross_audit_required=False))
    executors[RoleName.ARCHITECT] = PendingCredentials()
    kernel = WorkflowKernel(
        executors=executors,
        store=FileCheckpointStore(tmp_path / "cp"),
        audit=AuditLogger(),
        policy=WorkflowPolicy(
            engine=PolicyEngine.from_config(config_dir_of_repo()), gate=HumanGate()
        ),
    )

    run = kernel.run_all(make_request(cross_audit_required=False, budget=WorkflowBudget()))

    assert run.usage.model_calls == 0
    assert run.usage.total_tokens == 0


def _billable_error() -> Exception:
    """Error técnico facturable: el fallo pudo ocurrir **después** de enviar la petición.

    Se usa el error de rol del kernel, cuyo código no es «proveedor no disponible»: la frontera no
    puede demostrar que la petición no salió, así que la política conservadora aplica.
    """
    from punto.workflow.errors import WorkflowRoleFailedError

    return WorkflowRoleFailedError("la respuesta del proveedor llegó ilegible")
