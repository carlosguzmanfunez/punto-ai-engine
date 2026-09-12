"""Taxonomía de errores de la capa de ejecución (ENGINE-1).

Módulo **hoja**: no importa nada de ``punto``. Es la base de errores que
comparten el contexto de ejecución y todas las herramientas; al no tener
dependencias internas no puede formar parte de ningún ciclo de importación.

Todos los errores heredan de :class:`DeveloperExecutionError`, de modo que el
``DeveloperRunner`` pueda capturarlos de forma uniforme, auditarlos y traducirlos
a un ``DeveloperExecutionResult`` con estado ``BLOCKED`` o ``FAILED`` sin dejar
de distinguir la causa concreta.
"""

from __future__ import annotations


class DeveloperExecutionError(RuntimeError):
    """Base de todos los errores de la capa de ejecución controlada."""


class WorkspaceViolationError(DeveloperExecutionError):
    """Se intentó operar sobre una ruta fuera del workspace autorizado.

    Cubre ``..``, rutas absolutas externas y escapes por enlace (symlink o
    junction). La detección se basa en resolver la ruta real, nunca en comparar
    cadenas.
    """

    def __init__(self, candidate: str, workspace: str, detail: str = "") -> None:
        self.candidate = candidate
        self.workspace = workspace
        self.detail = detail
        suffix = f" ({detail})" if detail else ""
        super().__init__(
            f"Ruta fuera del workspace autorizado: {candidate!r} no está dentro de "
            f"{workspace!r}{suffix}"
        )


class ProtectedFileError(DeveloperExecutionError):
    """Se intentó modificar un archivo constitucionalmente protegido.

    Los archivos constitucionales son intocables con independencia del workspace:
    la protección no depende de dónde esté el repositorio.
    """

    def __init__(self, path: str, reason: str = "") -> None:
        self.path = path
        self.reason = reason
        suffix = f": {reason}" if reason else ""
        super().__init__(
            f"Archivo constitucionalmente protegido, modificación denegada: {path!r}{suffix}"
        )


class CommandNotAllowedError(DeveloperExecutionError):
    """El comando no está permitido por la política de shell (default deny)."""

    def __init__(self, executable: str, reason: str = "") -> None:
        self.executable = executable
        self.reason = reason
        suffix = f": {reason}" if reason else ""
        super().__init__(f"Comando no permitido: {executable!r}{suffix}")


class BranchPolicyViolationError(DeveloperExecutionError):
    """Se intentó escribir código directamente sobre una rama protegida."""

    def __init__(self, branch: str, required_prefix: str = "ai/") -> None:
        self.branch = branch
        self.required_prefix = required_prefix
        super().__init__(
            f"Escritura denegada en la rama protegida {branch!r}: el trabajo debe "
            f"realizarse en una rama {required_prefix}<task-id>-<slug>"
        )


class ExecutionLimitExceededError(DeveloperExecutionError):
    """Se superó un límite declarado de la ejecución."""

    def __init__(self, limit: str, observed: object, allowed: object) -> None:
        self.limit = limit
        self.observed = observed
        self.allowed = allowed
        super().__init__(
            f"Límite excedido ({limit}): observado {observed!r}, permitido {allowed!r}"
        )


class DeveloperRunnerNotConfiguredError(DeveloperExecutionError):
    """No hay ningún ``DeveloperRunner`` inyectado en el orquestador."""

    def __init__(self) -> None:
        super().__init__(
            "No hay DeveloperRunner configurado: inyecta uno en Camus("
            "developer_runner=...) para ejecutar tareas de desarrollo."
        )


# ---------------------------------------------------------------------------
# Frontera de confianza (ENGINE-1.R1)
# ---------------------------------------------------------------------------
class TrustBoundaryError(DeveloperExecutionError):
    """Base de los errores de la frontera entre ejecución confiable y no confiable."""


class UntrustedExecutionDeniedError(TrustBoundaryError):
    """Se intentó ejecutar trabajo no confiable por una vía que no lo admite.

    Es la denegación dura: un backend local confiable **nunca** ejecuta código
    originado por un modelo, aunque el comando esté en la allowlist
    (``python`` y ``pytest`` no son un sandbox).
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Ejecución no confiable denegada: {detail}")


class SandboxRequiredError(TrustBoundaryError):
    """El trabajo exige un backend aislado y el backend disponible no lo es.

    No existe degradación: si se requiere sandbox y no hay uno apto, se falla.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"SANDBOX_REQUIRED: {detail}")


class SandboxUnavailableError(TrustBoundaryError):
    """No hay ninguna implementación real de sandbox disponible.

    Se declara explícitamente en lugar de simular aislamiento inexistente.
    """

    def __init__(self, detail: str = "no hay backend aislado implementado") -> None:
        self.detail = detail
        super().__init__(f"Sandbox no disponible: {detail}")


# ---------------------------------------------------------------------------
# Capa de planificación (ENGINE-3)
# ---------------------------------------------------------------------------
class PlanningError(RuntimeError):
    """Base de los errores de la capa de planificación.

    Deliberadamente **no** hereda de :class:`DeveloperExecutionError`: planificar no
    es ejecutar. Un plan inválido no es un fallo de ejecución y la auditoría debe
    poder distinguirlos.
    """


class PlanningValidationError(PlanningError):
    """El plan propuesto incumple un invariante determinista.

    Lleva la lista completa de violaciones: PUNTO rechaza el plan entero y devuelve
    todas las razones al modelo, no solo la primera.
    """

    def __init__(self, violations: tuple[str, ...] | list[str]) -> None:
        self.violations = tuple(violations)
        detail = "; ".join(self.violations) if self.violations else "sin detalle"
        super().__init__(f"Plan inválido ({len(self.violations)} violación/es): {detail}")


class PlanningCycleError(PlanningError):
    """El grafo de tareas contiene un ciclo de dependencias."""

    def __init__(self, cycle: str) -> None:
        self.cycle = cycle
        super().__init__(f"Ciclo de dependencias en el grafo de tareas: {cycle}")


class PlanningLimitExceededError(PlanningError):
    """Se superó un límite declarado de la planificación."""

    def __init__(self, limit: str, observed: object, allowed: object) -> None:
        self.limit = limit
        self.observed = observed
        self.allowed = allowed
        super().__init__(
            f"Límite de planificación excedido ({limit}): observado {observed!r}, "
            f"permitido {allowed!r}"
        )


class ArchitectRunnerNotConfiguredError(PlanningError):
    """No hay ningún ``ArchitectRunner`` inyectado en CAMUS."""

    def __init__(self) -> None:
        super().__init__(
            "No hay ArchitectRunner configurado: inyecta uno en Camus("
            "architect_runner=...) para planificar proyectos."
        )


class PlannerRunnerNotConfiguredError(PlanningError):
    """No hay ningún ``PlannerRunner`` inyectado en CAMUS."""

    def __init__(self) -> None:
        super().__init__(
            "No hay PlannerRunner configurado: inyecta uno en Camus("
            "planner_runner=...) para planificar proyectos."
        )


# ---------------------------------------------------------------------------
# QA independiente (ENGINE-4)
# ---------------------------------------------------------------------------
class QAError(RuntimeError):
    """Base de los errores del rol QA.

    Deliberadamente **no** hereda de ``DeveloperExecutionError``: evaluar no es
    ejecutar y la auditoría debe poder distinguirlo.
    """


class QAValidationError(QAError):
    """El plan de QA incumple un invariante determinista.

    Lleva la lista completa de violaciones: PUNTO rechaza el plan entero y devuelve
    todas las razones al modelo, no solo la primera.
    """

    def __init__(self, violations: tuple[str, ...] | list[str]) -> None:
        self.violations = tuple(violations)
        detail = "; ".join(self.violations) if self.violations else "sin detalle"
        super().__init__(
            f"Plan de QA inválido ({len(self.violations)} violación/es): {detail}"
        )


class QARunnerNotConfiguredError(QAError):
    """No hay ningún ``QARunner`` inyectado en CAMUS."""

    def __init__(self) -> None:
        super().__init__(
            "No hay QARunner configurado: inyecta uno en Camus("
            "qa_runner=...) para evaluar el trabajo del Developer."
        )


__all__ = [
    "ArchitectRunnerNotConfiguredError",
    "BranchPolicyViolationError",
    "CommandNotAllowedError",
    "DeveloperExecutionError",
    "DeveloperRunnerNotConfiguredError",
    "ExecutionLimitExceededError",
    "PlannerRunnerNotConfiguredError",
    "PlanningCycleError",
    "PlanningError",
    "PlanningLimitExceededError",
    "PlanningValidationError",
    "ProtectedFileError",
    "QAError",
    "QARunnerNotConfiguredError",
    "QAValidationError",
    "SandboxRequiredError",
    "SandboxUnavailableError",
    "TrustBoundaryError",
    "UntrustedExecutionDeniedError",
    "WorkspaceViolationError",
]
