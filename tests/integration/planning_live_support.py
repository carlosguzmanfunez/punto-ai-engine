"""Soporte de los live gates de planificación (ENGINE-3 §22 a §24).

Los gates de ENGINE-3 hablan con DeepSeek **real** pero, a diferencia de los de
ENGINE-2, **no** necesitan sandbox: planificar no ejecuta nada. Esa es una propiedad
del diseño, no una comodidad de la prueba.

Si falta la credencial, el gate lo dice con el mensaje exacto y **falla**: no se salta
en silencio y no se declara PASS sin una llamada real.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from punto.providers.deepseek import (
    API_KEY_ENV,
    ARCHITECT_MODEL_ENV,
    PLANNER_MODEL_ENV,
    DeepSeekClient,
    config_from_environment,
)
from punto.schemas.planning import ProjectIntent

#: Mensaje exacto exigido por el mandato cuando falta la credencial.
CREDENTIAL_REQUIRED = f"CREDENTIAL_REQUIRED: {API_KEY_ENV}"

#: Intención del gate vivo (§22): un producto real, sin imponerle el stack.
DENTALFLOW_INTENT = ProjectIntent(
    name="DentalFlow",
    description=(
        "Plataforma web para que clínicas dentales administren pacientes, citas, "
        "tratamientos y facturación."
    ),
    business_goal="Reducir las citas perdidas y ordenar la facturación de la clínica.",
    target_users=("Recepcionista", "Odontólogo", "Administrador de la clínica"),
)


def require_credential() -> None:
    """Falla con el mensaje exigido si no hay credencial."""
    if not os.environ.get(API_KEY_ENV, "").strip():
        pytest.fail(
            f"{CREDENTIAL_REQUIRED}: no se puede ejecutar el live gate sin la "
            "credencial. ENGINE-3 no puede declararse PASS sin llamadas reales."
        )


def architect_client() -> DeepSeekClient:
    """Cliente real del Architect, con su modelo configurable por entorno."""
    return DeepSeekClient(config_from_environment(model_env=ARCHITECT_MODEL_ENV))


def planner_client() -> DeepSeekClient:
    """Cliente real del Planner, con su modelo configurable por entorno."""
    return DeepSeekClient(config_from_environment(model_env=PLANNER_MODEL_ENV))


def evidence(**fields: Any) -> str:
    """Evidencia legible de una llamada real, sin credenciales ni prompts."""
    lines = [f"{key}: {value}" for key, value in fields.items()]
    return "\n".join(lines)


__all__ = [
    "CREDENTIAL_REQUIRED",
    "DENTALFLOW_INTENT",
    "architect_client",
    "evidence",
    "planner_client",
    "require_credential",
]
