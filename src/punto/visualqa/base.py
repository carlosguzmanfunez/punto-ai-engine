"""Base del rol de Visual QA (ENGINE-5.3).

El contrato es provider-neutral: no menciona Claude. Recibe una tarea con la especificación
visual y los hechos técnicos, más las **imágenes** ya verificadas por PUNTO, y devuelve un
informe con veredicto calculado por el motor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from punto.providers.base import ImagePayload
from punto.schemas.visual import VisualQAReport, VisualQATask

#: Intentos de propuesta antes de rendirse.
DEFAULT_VISUAL_ATTEMPTS: Final[int] = 3

#: Llamadas al modelo en toda la evaluación (propuesta y reparaciones).
DEFAULT_VISUAL_MODEL_CALLS: Final[int] = 4

#: Presupuesto de tokens de entrada.
DEFAULT_VISUAL_INPUT_TOKENS: Final[int] = 400_000

#: Presupuesto de tokens de salida.
DEFAULT_VISUAL_OUTPUT_TOKENS: Final[int] = 120_000


@dataclass(frozen=True, slots=True)
class VisualQALimits:
    """Presupuesto acotado de una evaluación visual."""

    max_attempts: int = DEFAULT_VISUAL_ATTEMPTS
    max_model_calls: int = DEFAULT_VISUAL_MODEL_CALLS
    max_input_tokens: int = DEFAULT_VISUAL_INPUT_TOKENS
    max_output_tokens: int = DEFAULT_VISUAL_OUTPUT_TOKENS

    def __post_init__(self) -> None:
        """Rechaza presupuestos imposibles al construirlos."""
        if self.max_attempts <= 0:
            raise ValueError("max_attempts debe ser mayor que cero")
        if self.max_model_calls <= 0:
            raise ValueError("max_model_calls debe ser mayor que cero")
        if self.max_input_tokens <= 0:
            raise ValueError("max_input_tokens debe ser mayor que cero")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens debe ser mayor que cero")


class VisualQARunner(ABC):
    """Interfaz de Visual QA, independiente del proveedor."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Nombre del runner."""

    @property
    @abstractmethod
    def provider(self) -> str:
        """Proveedor del modelo visual."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Modelo visual."""

    @property
    @abstractmethod
    def prompt_version(self) -> str:
        """Versión del prompt visual en uso."""

    @property
    @abstractmethod
    def uses_ai(self) -> bool:
        """True si el runner consulta un modelo."""

    @property
    @abstractmethod
    def limits(self) -> VisualQALimits:
        """Presupuesto configurado."""

    @abstractmethod
    def evaluate(
        self, task: VisualQATask, screenshots: Mapping[str, ImagePayload]
    ) -> VisualQAReport:
        """Evalúa la interfaz y devuelve el informe.

        Args:
            task: Especificación visual más los hechos técnicos medidos.
            screenshots: Bytes verificados por nombre lógico. Nunca rutas: el modelo visual no
                recibe acceso a ningún sistema de archivos.

        Returns:
            El informe con el veredicto calculado por PUNTO.
        """


__all__ = [
    "DEFAULT_VISUAL_ATTEMPTS",
    "DEFAULT_VISUAL_INPUT_TOKENS",
    "DEFAULT_VISUAL_MODEL_CALLS",
    "DEFAULT_VISUAL_OUTPUT_TOKENS",
    "VisualQALimits",
    "VisualQARunner",
]
