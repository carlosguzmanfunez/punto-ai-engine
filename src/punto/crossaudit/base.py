"""Contrato del auditor cruzado (ENGINE-5.2).

El auditor cruzado es provider-agnostic: el contrato no menciona Anthropic ni Claude. Lo que
hace la implementación concreta es hablar con **su** proveedor, y el nombre del proveedor
viaja en el informe para que nadie tenga que suponerlo.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

from punto.schemas.cross_audit import CrossAuditReport, CrossAuditTask

#: Máximo de intentos de propuesta.
DEFAULT_CROSS_AUDIT_ATTEMPTS: Final[int] = 3

#: Máximo de llamadas al modelo en toda la auditoría.
DEFAULT_CROSS_AUDIT_MODEL_CALLS: Final[int] = 4

#: Presupuesto de tokens de entrada.
DEFAULT_CROSS_AUDIT_INPUT_TOKENS: Final[int] = 400_000

#: Presupuesto de tokens de salida.
DEFAULT_CROSS_AUDIT_OUTPUT_TOKENS: Final[int] = 120_000


@dataclass(frozen=True, slots=True)
class CrossAuditLimits:
    """Presupuesto acotado de una auditoría cruzada."""

    max_attempts: int = DEFAULT_CROSS_AUDIT_ATTEMPTS
    max_model_calls: int = DEFAULT_CROSS_AUDIT_MODEL_CALLS
    max_input_tokens: int = DEFAULT_CROSS_AUDIT_INPUT_TOKENS
    max_output_tokens: int = DEFAULT_CROSS_AUDIT_OUTPUT_TOKENS

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


class CrossAuditRunner(ABC):
    """Interfaz de la auditoría cruzada, independiente del proveedor."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Nombre del runner."""

    @property
    @abstractmethod
    def provider(self) -> str:
        """Proveedor del modelo auditor."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Modelo auditor."""

    @property
    @abstractmethod
    def prompt_version(self) -> str:
        """Versión del prompt de auditoría en uso."""

    @property
    @abstractmethod
    def uses_ai(self) -> bool:
        """True si el runner consulta un modelo."""

    @property
    @abstractmethod
    def limits(self) -> CrossAuditLimits:
        """Presupuesto configurado."""

    @abstractmethod
    def audit(self, task: CrossAuditTask) -> CrossAuditReport:
        """Audita el trabajo y devuelve el informe. Nunca lanza por un fallo de la tarea."""


__all__ = [
    "DEFAULT_CROSS_AUDIT_ATTEMPTS",
    "DEFAULT_CROSS_AUDIT_INPUT_TOKENS",
    "DEFAULT_CROSS_AUDIT_MODEL_CALLS",
    "DEFAULT_CROSS_AUDIT_OUTPUT_TOKENS",
    "CrossAuditLimits",
    "CrossAuditRunner",
]
