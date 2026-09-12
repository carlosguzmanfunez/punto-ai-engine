"""Interfaz abstracta del QARunner (ENGINE-4).

QA responde una sola pregunta: *¿la implementación cumple los criterios de
aceptación?* Y la responde con **evidencia**, no con opinión.

El contrato es provider-agnostic: no menciona ningún proveedor de modelo. La primera
implementación es ``DeepSeekQARunner``; otro proveedor puede añadirse sin tocar esta
interfaz, CAMUS ni los esquemas.

Separación de roles que esta interfaz hace cumplir por construcción:

- QA **no** modifica código de producción: solo puede añadir archivos de prueba;
- QA **no** corrige el producto ni hace commits;
- QA **no** declara PASS: el estado lo calcula PUNTO a partir de la ejecución;
- QA **no** redefine los criterios de aceptación: son el contrato recibido.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from punto.schemas.qa import QAReport, QATask


@dataclass(frozen=True, slots=True)
class QALimits:
    """Presupuesto de una evaluación de QA.

    Igual que en los demás roles, se separan dos cosas que no son lo mismo:

    - **reintentos del proveedor**: los aplica el cliente HTTP ante fallos transitorios
      de red; no consumen intentos de QA;
    - **intentos de QA**: los consume el plan cuando incumple un invariante y las
      pruebas de QA cuando están mal construidas (``QA_TEST_FAILURE``).
    """

    max_attempts: int = 3
    max_model_calls: int = 6
    max_test_repairs: int = 2
    max_input_tokens: int = 400_000
    max_output_tokens: int = 120_000
    check_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        """Valida que los límites sean positivos."""
        if self.max_attempts < 1:
            raise ValueError("max_attempts debe ser al menos 1")
        if self.max_model_calls < 1:
            raise ValueError("max_model_calls debe ser al menos 1")
        if self.max_test_repairs < 0:
            raise ValueError("max_test_repairs no puede ser negativo")
        if self.max_input_tokens < 1 or self.max_output_tokens < 1:
            raise ValueError("los límites de tokens deben ser positivos")
        if self.check_timeout_seconds <= 0:
            raise ValueError("check_timeout_seconds debe ser mayor que cero")


class QARunner(ABC):
    """Contrato de evaluación independiente de un resultado de desarrollo."""

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
    def evaluate(self, task: QATask) -> QAReport:
        """Evalúa el trabajo del Developer contra los criterios de aceptación.

        Nunca lanza por un fallo de la evaluación: lo traduce a un ``QAReport`` con
        estado ``FAIL`` o ``BLOCKED`` acompañado de evidencia. Sí puede lanzar por un
        uso incorrecto de la interfaz.
        """
        raise NotImplementedError


__all__ = ["QALimits", "QARunner"]
