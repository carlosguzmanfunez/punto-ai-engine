"""Interfaz abstracta del ReviewerRunner (ENGINE-5).

El Reviewer cierra la cadena: evalúa la calidad global del cambio y decide si técnicamente
está listo para aceptarse. Es el único rol que emite un veredicto de aprobación, y por eso
es donde más importa que el modelo no pueda decidirlo solo.

Separación que esta interfaz hace cumplir por construcción:

- el Reviewer **no** ejecuta código: QA y Security ya lo hicieron;
- el Reviewer **no** modifica los informes de QA ni de Security: los lee como gates;
- el veredicto lo calcula PUNTO a partir de esos gates y de sus propios hallazgos.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from punto.schemas.review import ReviewReport, ReviewTask


@dataclass(frozen=True, slots=True)
class ReviewerLimits:
    """Presupuesto de una revisión.

    Los intentos solo se consumen por una propuesta estructuralmente inválida: **nunca**
    por un hallazgo grave del producto, que se reporta en lugar de repararse.
    """

    max_attempts: int = 3
    max_model_calls: int = 4
    max_input_tokens: int = 400_000
    max_output_tokens: int = 120_000

    def __post_init__(self) -> None:
        """Valida que los límites sean positivos."""
        if self.max_attempts < 1:
            raise ValueError("max_attempts debe ser al menos 1")
        if self.max_model_calls < 1:
            raise ValueError("max_model_calls debe ser al menos 1")
        if self.max_input_tokens < 1 or self.max_output_tokens < 1:
            raise ValueError("los límites de tokens deben ser positivos")


class ReviewerRunner(ABC):
    """Contrato de revisión independiente de un cambio."""

    @property
    def name(self) -> str:
        """Nombre del runner, para auditoría y evidencia."""
        return type(self).__name__

    @property
    def provider(self) -> str:
        """Proveedor del modelo. Cadena vacía si el runner es determinista."""
        return ""

    @property
    def model(self) -> str:
        """Modelo en uso. Cadena vacía si el runner es determinista."""
        return ""

    @property
    def prompt_version(self) -> str:
        """Versión del prompt de sistema. Cadena vacía si no hay prompt."""
        return ""

    @property
    def uses_ai(self) -> bool:
        """True si el runner consulta un modelo externo."""
        return False

    @abstractmethod
    def review(self, task: ReviewTask) -> ReviewReport:
        """Revisa el trabajo y devuelve el veredicto con su evidencia.

        Nunca lanza por un fallo de la revisión: lo traduce a un ``ReviewReport`` con
        estado ``CHANGES_REQUESTED`` o ``BLOCKED``. Sí puede lanzar por un uso incorrecto
        de la interfaz.
        """
        raise NotImplementedError


__all__ = ["ReviewerLimits", "ReviewerRunner"]
