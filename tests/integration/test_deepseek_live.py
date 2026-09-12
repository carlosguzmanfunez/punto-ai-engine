"""Live gate de DeepSeek (ENGINE-2 §22).

Este archivo **no** forma parte de la suite rápida: se ejecuta aparte y hace una
llamada **real** a la API de DeepSeek. Es obligatorio para cerrar ENGINE-2.

    pytest tests/integration/test_deepseek_live.py -q

Si la credencial no existe, el gate lo dice explícitamente y **falla**: ENGINE-2 no
se declara PASS sin una llamada real.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from punto.providers.deepseek import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DeepSeekClient,
    DeepSeekConfig,
    DeepSeekError,
    parse_proposal_json,
)
from punto.schemas.execution import DeveloperProposal

#: Mensaje exacto exigido por el mandato cuando falta la credencial.
CREDENTIAL_REQUIRED = "CREDENTIAL_REQUIRED: DEEPSEEK_API_KEY"

#: Instrucción mínima para el gate: una llamada real y estructurada.
OBJECTIVE = (
    "Devuelve una propuesta JSON con un único cambio CREATE sobre 'hello.py' cuyo "
    "contenido complete sea exactamente: print('PUNTO AI ENGINE')\n"
)


def api_key() -> str:
    """Credencial real desde el entorno."""
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def build_config() -> DeepSeekConfig:
    """Configuración real del cliente."""
    return DeepSeekConfig(
        api_key=api_key(),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
    )


@pytest.mark.integration
def test_deepseek_live_call_returns_a_valid_structured_proposal() -> None:
    """Llamada real: autenticación, JSON estructurado y esquema válido."""
    if not api_key():
        pytest.fail(
            f"{CREDENTIAL_REQUIRED}: no se puede ejecutar el live gate sin la "
            "credencial. ENGINE-2 no puede declararse PASS sin una llamada real."
        )

    config = build_config()
    with DeepSeekClient(config) as client:
        completion = client.complete_json(
            system_prompt=(
                "Eres el Developer de PUNTO AI ENGINE. Devuelve ÚNICAMENTE un objeto "
                "JSON con las claves: summary, changes, validation_notes, assumptions. "
                "No añadas texto fuera del JSON."
            ),
            user_prompt=OBJECTIVE,
        )

    # 1. El modelo real respondió.
    assert completion.content.strip()
    assert completion.model

    # 2. El contenido es JSON válido y cumple el esquema de propuesta.
    payload: dict[str, Any] = parse_proposal_json(completion.content)
    proposal = DeveloperProposal.model_validate(payload)

    # 3. La propuesta es utilizable.
    assert proposal.summary
    assert proposal.changes, "el modelo no propuso ningún cambio"
    assert proposal.changes[0].path == "hello.py"

    # 4. El consumo real se reporta.
    assert completion.usage.total_tokens > 0

    print(
        json.dumps(
            {
                "provider": "deepseek",
                "model": completion.model,
                "latency_ms": completion.latency_ms,
                "prompt_tokens": completion.usage.prompt_tokens,
                "completion_tokens": completion.usage.completion_tokens,
                "total_tokens": completion.usage.total_tokens,
                "changes": [change.path for change in proposal.changes],
            },
            indent=1,
        )
    )


@pytest.mark.integration
def test_deepseek_live_call_reports_errors_structurally() -> None:
    """Una credencial inválida produce un error estructurado, no un fallo opaco."""
    if not api_key():
        pytest.fail(f"{CREDENTIAL_REQUIRED}: no se puede ejecutar el live gate.")

    config = DeepSeekConfig(
        api_key="sk-invalid-credential-for-negative-test",
        base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
    )
    with DeepSeekClient(config) as client, pytest.raises(DeepSeekError) as caught:
        client.complete_json(system_prompt="s", user_prompt="u")

    # La credencial inválida no debe aparecer en el mensaje de error.
    assert "sk-invalid-credential-for-negative-test" not in str(caught.value)
