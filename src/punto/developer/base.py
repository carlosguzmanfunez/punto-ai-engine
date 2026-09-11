"""Interfaz abstracta del DeveloperRunner.

Frontera entre CAMUS y la ejecución real. CAMUS **nunca** ejecuta ``subprocess``,
ni escrituras de archivos, ni Git, ni shell: delega en un ``DeveloperRunner``.

El contrato es deliberadamente **provider-agnostic**: no menciona ni conoce
ningún proveedor de modelo. ENGINE-1 implementa ``LocalDeveloperRunner``
(determinista, sin IA); ENGINE-2 podrá añadir ``DeepSeekDeveloperRunner`` sin
tocar el núcleo ni esta interfaz.

Un ``DeveloperRunner`` **no** decide autoridad. Aplica los límites técnicos que le
fija el ``ExecutionContext`` y rechaza lo que los viole, pero jamás eleva
permisos: la jerarquía es Policy Engine -> CAMUS -> DeveloperRunner.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from punto.developer.context import ExecutionContext
    from punto.schemas.execution import DeveloperExecutionResult, DeveloperTask


class DeveloperRunner(ABC):
    """Contrato de ejecución de una tarea de desarrollo estructurada."""

    @property
    def name(self) -> str:
        """Nombre del runner, para auditoría y evidencia."""
        return type(self).__name__

    @property
    def generates_code_with_ai(self) -> bool:
        """True si el runner genera código mediante un modelo externo.

        ``LocalDeveloperRunner`` devuelve ``False``: aplica recetas declaradas.
        Un futuro ``DeepSeekDeveloperRunner`` devolverá ``True``.
        """
        return False

    @abstractmethod
    def execute(
        self, task: DeveloperTask, context: ExecutionContext
    ) -> DeveloperExecutionResult:
        """Ejecuta ``task`` dentro del workspace de ``context``.

        Args:
            task: Tarea estructurada (archivos, reemplazos, asserts, checks, commit).
            context: Contexto que delimita workspace, rama, comandos y límites.

        Returns:
            La evidencia estructurada de la ejecución. Nunca lanza por un fallo
            de la tarea: los fallos se expresan como ``status`` acompañado de
            ``error``. Sí puede lanzar por un uso incorrecto de la interfaz.
        """
        raise NotImplementedError


__all__ = ["DeveloperRunner"]
