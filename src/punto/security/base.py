"""Interfaz abstracta del SecurityRunner (ENGINE-5).

Security responde una pregunta distinta a la de QA: no *¿funciona?*, sino *¿es seguro
usarlo?*. Busca vulnerabilidades, exposiciones y configuraciones inseguras.

El contrato es provider-agnostic. La primera implementación es
``DeepSeekSecurityRunner``; otro proveedor puede añadirse sin tocar esta interfaz, CAMUS
ni los esquemas.

Separación que esta interfaz hace cumplir por construcción:

- Security **no** modifica nada: ni el producto, ni las pruebas, ni los informes de otros;
- Security **no** declara el estado final: lo calcula PUNTO a partir de la evidencia;
- el PASS de Developer y de QA es **contexto**: que algo funcione no lo hace seguro.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from punto.schemas.security import SecurityReport, SecurityTask


@dataclass(frozen=True, slots=True)
class SecurityLimits:
    """Presupuesto de una auditoría de seguridad.

    Se separan, como en los demás roles, los reintentos de transporte del proveedor (los
    aplica el cliente HTTP) de los intentos de Security (los consume un plan o unos
    hallazgos estructuralmente inválidos).
    """

    max_attempts: int = 3
    max_model_calls: int = 6
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


class SecurityRunner(ABC):
    """Contrato de auditoría de seguridad independiente."""

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
    def evaluate(self, task: SecurityTask) -> SecurityReport:
        """Audita el trabajo contra el contexto autorizado.

        Nunca lanza por un fallo de la auditoría: lo traduce a un ``SecurityReport`` con
        estado ``FAIL`` o ``BLOCKED`` acompañado de evidencia. Sí puede lanzar por un uso
        incorrecto de la interfaz.
        """
        raise NotImplementedError


__all__ = ["SecurityLimits", "SecurityRunner"]
