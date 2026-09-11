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


__all__ = [
    "BranchPolicyViolationError",
    "CommandNotAllowedError",
    "DeveloperExecutionError",
    "DeveloperRunnerNotConfiguredError",
    "ExecutionLimitExceededError",
    "ProtectedFileError",
    "WorkspaceViolationError",
]
