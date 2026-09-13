"""Live gates de Anthropic/Claude (ENGINE-5.2 §25).

Cinco puertas contra la API real:

A. **autenticación**: la credencial funciona y el modelo configurado responde;
B. **JSON estructurado**: la respuesta es un objeto JSON interpretable;
C. **credencial inválida**: una clave deliberadamente incorrecta se rechaza sin reintentos;
D. **multimodal**: texto + imagen llegan a la API y la respuesta se parsea;
E. **auditoría cruzada**: la cadena completa produce un informe con ``provider=anthropic``.

Si ``ANTHROPIC_API_KEY`` no existe, **no** se ejecuta esta suite y **no** se declara nada:
queda ``PENDING_API_KEY``. No se inventan resultados, no se simula que Claude respondió y no se
pide la credencial por el chat. La suite estándar no depende de estas pruebas: viven en
``tests/integration`` y el ``addopts`` del proyecto las ignora.

    pytest tests/integration/test_anthropic_live.py -q -s
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from engine52_support import cross_audit_workspace, make_cross_audit_task
from punto.crossaudit.claude import ClaudeCrossModelAuditRunner
from punto.providers.anthropic import (
    API_KEY_ENV,
    AUDIT_MODEL_ENV,
    LIVE_ACCOUNT_ACCESS_UNVERIFIED,
    MODEL_ID_DOCUMENTED,
    AnthropicAuthenticationError,
    AnthropicClient,
    AnthropicConfig,
    AnthropicError,
    config_from_environment,
)
from punto.providers.base import ImagePayload
from punto.schemas.cross_audit import CrossAuditStatus

pytestmark = pytest.mark.integration

#: Mensaje exacto exigido cuando falta la credencial.
CREDENTIAL_REQUIRED = f"CREDENTIAL_REQUIRED: {API_KEY_ENV}"

#: PNG 1x1 real: la API valida el contenido, no basta con bytes inventados.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
    "q842iQAAAABJRU5ErkJggg=="
)

#: Esquema real del gate B: structured outputs, no una instrucción textual.
STRUCTURED_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "findings"],
    "additionalProperties": False,
}

#: Esquema real del gate D: la respuesta debe ser un objeto verificable.
MULTIMODAL_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"image_received": {"type": "boolean"}},
    "required": ["image_received"],
    "additionalProperties": False,
}


def require_credential() -> None:
    """Falla con el mensaje exigido si no hay credencial."""
    if not os.environ.get(API_KEY_ENV, "").strip():
        pytest.fail(
            f"{CREDENTIAL_REQUIRED}: no se puede ejecutar el live gate de ENGINE-5.2 sin la "
            "credencial. La fase no puede declarar CLAUDE LIVE = PASS sin llamadas reales."
        )


@pytest.fixture(scope="module", autouse=True)
def gate() -> None:
    """Sin credencial la suite falla de forma explícita, no se salta en silencio."""
    require_credential()


@pytest.fixture(scope="module")
def client() -> object:
    """Cliente real, con el modelo configurado por entorno."""
    instance = AnthropicClient(config_from_environment(model_env=AUDIT_MODEL_ENV))
    try:
        yield instance
    finally:
        instance.close()


def test_live_a_authentication_works(client: AnthropicClient) -> None:
    """Gate A: la credencial autentica y el modelo configurado responde."""
    completion = client.complete_json(
        system_prompt="Responde solo con JSON.",
        user_prompt='Devuelve exactamente {"ok": true} y nada más.',
    )

    assert completion.content.strip()
    assert completion.provider == "anthropic"
    assert completion.model
    assert completion.usage.total_tokens > 0
    assert completion.latency_ms >= 0
    print(
        f"\nA. autenticación: model={completion.model} "
        f"tokens={completion.usage.total_tokens} stop={completion.stop_reason!r} "
        f"request_id={completion.request_id or '(sin id)'} "
        f"model_id_documented={MODEL_ID_DOCUMENTED} "
        f"live_account_access_unverified={LIVE_ACCOUNT_ACCESS_UNVERIFIED}"
    )


def test_live_b_structured_json_is_parseable(client: AnthropicClient) -> None:
    """Gate B: con esquema real, la respuesta es JSON parseable **sin** limpiar fences.

    El esquema viaja en ``output_config.format``: si el proveedor respeta structured outputs,
    el contenido es JSON válido tal cual. Quitarle las vallas al texto escondería que ese
    camino no funcionó, así que aquí no se limpia nada.
    """
    completion = client.complete_json(
        system_prompt="Respondes con un objeto JSON que cumple el esquema entregado.",
        user_prompt="Resume el cambio y lista los hallazgos (puede ser una lista vacía).",
        json_schema=STRUCTURED_SCHEMA,
    )
    payload = json.loads(completion.content)

    assert isinstance(payload, dict)
    assert isinstance(payload.get("summary"), str)
    assert isinstance(payload.get("findings"), list)
    assert set(payload) == {"summary", "findings"}, "el esquema prohíbe claves extra"
    print(f"\nB. structured: claves={sorted(payload)} stop={completion.stop_reason!r}")


def test_live_c_invalid_key_is_rejected_once() -> None:
    """Gate C: una credencial inválida se rechaza y no se reintenta."""
    config = AnthropicConfig(
        api_key="sk-ant-api03-CLAVE-INVALIDA-DEL-LIVE-GATE", transport_retries=3
    )
    calls = 0

    def counting_sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1

    client = AnthropicClient(config, sleep=counting_sleep)
    try:
        with pytest.raises(AnthropicAuthenticationError):
            client.complete_json(system_prompt="Responde JSON.", user_prompt='{"ok": true}')
    finally:
        client.close()

    assert calls == 0, "un 401/403 no debe provocar reintentos"
    print("\nC. credencial inválida: rechazada sin reintentos")


def test_live_d_multimodal_reaches_the_api(client: AnthropicClient) -> None:
    """Gate D: texto + imagen + esquema ⇒ objeto estructurado verificable.

    No se juzga nada visualmente: se comprueba un campo booleano que el esquema obliga a
    responder, de modo que el gate no dependa de una clasificación subjetiva.
    """
    image = ImagePayload(data=PNG_1X1, media_type="image/png", logical_name="pixel.png")
    completion = client.complete_multimodal_json(
        system_prompt="Respondes con un objeto JSON que cumple el esquema entregado.",
        user_prompt=(
            "Indica en image_received si has recibido una imagen en esta petición."
        ),
        images=(image,),
        json_schema=MULTIMODAL_SCHEMA,
    )
    payload = json.loads(completion.content)

    assert isinstance(payload, dict)
    assert payload.get("image_received") is True
    assert completion.usage.total_tokens > 0
    print(
        f"\nD. multimodal: image_received={payload.get('image_received')} "
        f"tokens={completion.usage.total_tokens} bytes={image.size_bytes}"
    )


def test_live_e_cross_audit_runs_with_claude(tmp_path: Path) -> None:
    """Gate E: la auditoría cruzada completa produce un informe de Anthropic."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    live_client = AnthropicClient(config_from_environment(model_env=AUDIT_MODEL_ENV))
    try:
        report = ClaudeCrossModelAuditRunner(client=live_client).audit(task)
    finally:
        live_client.close()

    assert report.provider == "anthropic"
    # El gate del fixture limpio exige PASS. Un BLOCKED o un CHANGES_REQUESTED aquí NO es un
    # resultado aceptable: significaría que el fixture no está limpio o que Claude encontró
    # algo real, y en ese caso hay que revisar el fixture, no relajar el gate.
    assert report.status is CrossAuditStatus.PASS, (
        f"status={report.status.value} error={report.error!r} "
        f"gates={[(g.name.value, g.passed, g.detail) for g in report.gates]} "
        f"findings={[(f.id, f.severity.value, f.category.value) for f in report.findings]}"
    )
    assert report.provider == "anthropic"
    assert report.model == live_client.model
    assert report.cross_model is True
    assert report.model_usage.total_tokens > 0
    print(
        f"\nE. cross audit: status={report.status.value} provider={report.provider} "
        f"model={report.model} findings={len(report.findings)} "
        f"cross_model={report.cross_model} tokens={report.model_usage.total_tokens}"
    )


def test_live_error_messages_never_leak_the_key(client: AnthropicClient) -> None:
    """Ningún error puede contener la credencial."""
    secret = os.environ.get(API_KEY_ENV, "")
    try:
        client.complete_json(system_prompt="", user_prompt="")
    except AnthropicError as exc:
        assert secret not in str(exc)
        assert client.redact(str(exc)) == str(exc)
