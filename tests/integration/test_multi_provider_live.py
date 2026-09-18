"""Live gate de la orquestación multi-proveedor (MULTI-PROVIDER v0 §16).

Hace llamadas **reales** a OpenAI, DeepSeek y Anthropic a través del router y del circuito
``ARCHITECT → BUILDER → VISUAL_QA``. Sin credenciales legítimas en el entorno, el gate **falla** con
``CREDENTIAL_REQUIRED``: no se inventa un resultado ni se marca como verde lo que no se ejecutó.

    pytest tests/integration/test_multi_provider_live.py -q

Las credenciales se leen del entorno (``OPENAI_API_KEY``, ``DEEPSEEK_API_KEY``,
``ANTHROPIC_API_KEY``) y nunca se imprimen ni se escriben.
"""

from __future__ import annotations

import os

import pytest

from punto.providers.base import ImagePayload
from punto.providers.circuit import run_multi_provider_circuit
from punto.providers.contract import (
    ProviderHealthStatus,
    ProviderRole,
    ProviderStatus,
    make_request,
)
from punto.providers.router import load_default_router

pytestmark = pytest.mark.integration

#: Variables de entorno exigidas por el gate.
REQUIRED_ENV: tuple[str, ...] = ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY")

#: Objetivo del circuito vivo (§14).
PROJECT_GOAL = "Crear una pequeña página de listado inmobiliario."

#: PNG mínimo válido (1x1) para la evidencia visual del gate.
MINIMAL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100ffff03000006000557bfabd4000000"
    "0049454e44ae426082"
)


@pytest.fixture(scope="module", autouse=True)
def gate() -> None:
    """Sin las tres credenciales el gate falla de forma explícita, no se salta."""
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        pytest.fail(f"CREDENTIAL_REQUIRED: {', '.join(missing)}")


def test_live_health_de_los_tres_proveedores() -> None:
    """Cada proveedor responde a la comprobación barata de conexión."""
    router = load_default_router()

    salud = {name: router.test_connection(name) for name in router.providers()}

    for name, health in salud.items():
        assert health.status is ProviderHealthStatus.CONNECTED, (name, health.detail)
        assert health.model


def test_live_circuito_architect_builder_visual_qa() -> None:
    """El circuito completo responde en vivo y los tres resultados vuelven normalizados."""
    router = load_default_router()
    captura = ImagePayload(data=MINIMAL_PNG, media_type="image/png", logical_name="captura.png")

    outcome = run_multi_provider_circuit(
        PROJECT_GOAL,
        router,
        constraints=("una sola página", "sin base de datos"),
        visual_evidence=(captura,),
    )

    assert outcome.completed, outcome.as_dict()
    assert [step.result.provider for step in outcome.steps] == [
        "openai",
        "deepseek",
        "anthropic",
    ]
    assert all(step.result.status is ProviderStatus.SUCCESS for step in outcome.steps)
    assert all(step.result.content.strip() for step in outcome.steps)
    assert all(step.result.usage is not None for step in outcome.steps)


def test_live_un_rol_se_puede_reenrutar() -> None:
    """Cambiar el proveedor de un rol no requiere tocar ENGINE y el resultado lo declara."""
    router = load_default_router()
    router.assign_role(ProviderRole.BUILDER, "openai")

    result = router.execute(
        ProviderRole.BUILDER, make_request(ProviderRole.BUILDER, "propón un listado mínimo")
    )

    assert result.provider == "openai"
    assert result.status is ProviderStatus.SUCCESS, result.error
