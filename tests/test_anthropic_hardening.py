"""Motivos de parada y política de espera del cliente de Anthropic (ENGINE-5.2.2).

Dos cosas que un 200 puede traer y que no se pueden tratar como JSON normal:

- ``stop_reason="refusal"``: el modelo se negó a responder;
- ``stop_reason="model_context_window_exceeded"``: la salida no es utilizable porque se agotó
  la ventana de contexto.

Y una política explícita de espera: un ``retry-after`` válido manda; ausente o inválido usa el
backoff exponencial; por encima del presupuesto se falla en vez de dormir sin límite.

Todo con transporte falso: sin red, sin credencial y sin inventar respuestas.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from engine52_support import (
    FakeAnthropicAPI,
    cross_audit_payload,
    cross_audit_workspace,
    make_client,
    make_cross_audit_task,
    message_response,
)
from punto.crossaudit.claude import (
    BLOCKED_PROVIDER_REFUSAL,
    ClaudeCrossModelAuditRunner,
)
from punto.crossaudit.prompts import CROSS_AUDIT_SYSTEM_PROMPT
from punto.providers.anthropic import (
    MAX_RETRY_AFTER_SECONDS,
    RETRY_AFTER_HEADER,
    AnthropicClient,
    AnthropicConfig,
    AnthropicRateLimitError,
    AnthropicRefusalError,
    AnthropicTruncatedResponseError,
)
from punto.providers.base import ProviderRefusalError
from punto.schemas.cross_audit import CrossAuditStatus
from test_anthropic_client import FAKE_KEY, error_response

SYSTEM_PROMPT = "Responde con un objeto JSON."
USER_PROMPT = "Devuelve el resumen."

#: Texto de una respuesta que no es JSON: si se interpretara, fallaría como JSON roto.
REFUSAL_TEXT = "No puedo ayudar con esta petición."


def refusing_response(
    *, text: str = REFUSAL_TEXT, category: str = "cyber", message_id: str = "msg_refusal"
) -> httpx.Response:
    """200 con negativa, tal como la devuelve la API."""
    return httpx.Response(
        200,
        json={
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "refusal",
            "stop_details": {"category": category},
            "usage": {"input_tokens": 10, "output_tokens": 4},
        },
        headers={"request-id": "req-refusal-1"},
    )


def context_window_response(*, text: str = '{"summary": "cortado') -> httpx.Response:
    """200 con la salida cortada por la ventana de contexto."""
    return httpx.Response(
        200,
        json={
            "id": "msg_ctx",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "model_context_window_exceeded",
            "usage": {"input_tokens": 200_000, "output_tokens": 12},
        },
    )


# ---------------------------------------------------------------------------
# CA-03: negativa
# ---------------------------------------------------------------------------
def test_refusal_is_detected_before_reading_the_content() -> None:
    """La negativa se detecta antes de interpretar nada, no como JSON roto."""
    api = FakeAnthropicAPI([refusing_response()])
    client = make_client(api)

    with pytest.raises(AnthropicRefusalError) as caught:
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    message = str(caught.value)
    assert "provider=anthropic" in message
    assert "claude-opus-5" in message
    assert "refusal" in message
    assert "cyber" in message
    assert "req-refusal-1" in message
    # Solo metadata segura: ni la respuesta completa ni el texto del modelo.
    assert REFUSAL_TEXT not in message
    assert json.dumps({"content": REFUSAL_TEXT}) not in message


def test_refusal_is_a_provider_error_but_not_an_invalid_response() -> None:
    """El tipo del error distingue «no quiso» de «no se pudo interpretar»."""
    api = FakeAnthropicAPI([refusing_response()])
    client = make_client(api)

    with pytest.raises(ProviderRefusalError):
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)


def test_refusal_is_not_retried() -> None:
    """Repetir la misma petición que provocó la negativa no es reparar."""
    sleeps: list[float] = []
    api = FakeAnthropicAPI([refusing_response()])
    client = make_client(api, sleep=sleeps.append)

    with pytest.raises(AnthropicRefusalError):
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert api.calls == 1
    assert sleeps == []


def test_refusal_without_category_still_reports_safely() -> None:
    """Sin ``stop_details`` el error sigue siendo útil y sigue sin filtrar contenido."""
    response = httpx.Response(
        200,
        json={
            "id": "msg_2",
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": REFUSAL_TEXT}],
            "stop_reason": "refusal",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        },
    )
    client = make_client(FakeAnthropicAPI([response]))

    with pytest.raises(AnthropicRefusalError) as caught:
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert "categoría=''" in str(caught.value)
    assert REFUSAL_TEXT not in str(caught.value)


def test_cross_audit_maps_refusal_to_an_explicit_block(tmp_path) -> None:
    """En CrossAudit, una negativa es BLOCKED con causa diferenciada, no JSON inválido."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    api = FakeAnthropicAPI([refusing_response()])
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.BLOCKED
    assert BLOCKED_PROVIDER_REFUSAL in report.error
    assert "PROVIDER_ERROR" not in report.error
    assert "contrato incumplido" not in report.error
    # Sin reparación semántica: una sola llamada.
    assert api.calls == 1
    assert report.findings == ()


def test_cross_audit_refusal_never_falls_back_to_another_provider(tmp_path) -> None:
    """La negativa no autoriza a responder con DeepSeek."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    api = FakeAnthropicAPI([refusing_response()])
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert report.provider == "anthropic"
    assert api.calls == 1, "no se reintenta ni se cambia de proveedor"


# ---------------------------------------------------------------------------
# §12: ventana de contexto
# ---------------------------------------------------------------------------
def test_context_window_exceeded_is_truncation_not_broken_json() -> None:
    """Se clasifica como truncamiento/capacidad, no como JSON roto."""
    client = make_client(FakeAnthropicAPI([context_window_response()]))

    with pytest.raises(AnthropicTruncatedResponseError) as caught:
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    message = str(caught.value)
    assert "ventana de contexto" in message
    assert "model_context_window_exceeded" in message
    assert "provider=anthropic" in message
    assert "max_tokens" not in message, "no es el presupuesto de salida"


def test_context_window_is_not_retried() -> None:
    """Reintentar con el mismo contexto daría el mismo corte."""
    sleeps: list[float] = []
    api = FakeAnthropicAPI([context_window_response()])
    client = make_client(api, sleep=sleeps.append)

    with pytest.raises(AnthropicTruncatedResponseError):
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert api.calls == 1
    assert sleeps == []


def test_max_tokens_truncation_is_unchanged() -> None:
    """La detección de ``max_tokens`` sigue funcionando como antes."""
    client = make_client(
        FakeAnthropicAPI([message_response(text='{"a": ', stop_reason="max_tokens")])
    )

    with pytest.raises(AnthropicTruncatedResponseError, match="max_tokens"):
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)


# ---------------------------------------------------------------------------
# CA-08: retry-after
# ---------------------------------------------------------------------------
def rate_limited(retry_after: str | None) -> httpx.Response:
    """429 con o sin cabecera ``retry-after``."""
    headers = {} if retry_after is None else {RETRY_AFTER_HEADER: retry_after}
    return httpx.Response(
        429,
        json={"type": "error", "error": {"type": "rate_limit_error", "message": "lento"}},
        headers=headers,
    )


def test_valid_retry_after_is_respected() -> None:
    """Un ``retry-after`` entero y razonable manda sobre el backoff."""
    sleeps: list[float] = []
    api = FakeAnthropicAPI([rate_limited("7"), message_response()])
    client = make_client(api, sleep=sleeps.append)

    completion = client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert completion.transport_retries == 1
    assert api.calls == 2
    assert sleeps == [7.0]


def test_absent_retry_after_uses_exponential_backoff() -> None:
    """Sin cabecera se usa el backoff de siempre."""
    sleeps: list[float] = []
    api = FakeAnthropicAPI([rate_limited(None), message_response()])
    client = make_client(api, sleep=sleeps.append)

    client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert sleeps == [0.5]


@pytest.mark.parametrize("value", ["", "   ", "tomorrow", "-3", "NaN", "inf"])
def test_invalid_retry_after_uses_exponential_backoff(value: str) -> None:
    """Un valor ilegible no se obedece: se cae al backoff exponencial."""
    sleeps: list[float] = []
    api = FakeAnthropicAPI([rate_limited(value), message_response()])
    client = make_client(api, sleep=sleeps.append)

    client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert sleeps == [0.5]


def test_over_budget_retry_after_fails_without_sleeping() -> None:
    """Un header enorme se rechaza: no puede bloquear el proceso."""
    sleeps: list[float] = []
    api = FakeAnthropicAPI([rate_limited("3600")])
    client = make_client(api, sleep=sleeps.append)

    with pytest.raises(AnthropicRateLimitError) as caught:
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert sleeps == []
    assert api.calls == 1
    message = str(caught.value)
    assert "3600" in message
    assert f"{MAX_RETRY_AFTER_SECONDS:.0f}s" in message


def test_retry_after_at_the_budget_is_obeyed() -> None:
    """El límite exacto sigue siendo aceptable."""
    sleeps: list[float] = []
    api = FakeAnthropicAPI([rate_limited(str(int(MAX_RETRY_AFTER_SECONDS))), message_response()])
    client = make_client(api, sleep=sleeps.append)

    client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert sleeps == [MAX_RETRY_AFTER_SECONDS]


def test_retry_after_on_server_error_also_applies() -> None:
    """Un 503 con ``retry-after`` se espera igual, y sigue siendo error de servidor."""
    sleeps: list[float] = []
    limited = httpx.Response(
        503,
        json={"type": "error", "error": {"type": "overloaded_error", "message": "carga"}},
        headers={RETRY_AFTER_HEADER: "2"},
    )
    api = FakeAnthropicAPI([limited, message_response()])
    client = make_client(api, sleep=sleeps.append)

    client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert sleeps == [2.0]


def test_sensitive_headers_never_leak_into_errors() -> None:
    """No se filtran cabeceras: ni la credencial ni un canario ajeno."""
    canary = "sk-ant-api03-HEADER-CANARY-4f8a9bc7d6e5"
    limited = httpx.Response(
        429,
        json={"type": "error", "error": {"type": "rate_limit_error", "message": "lento"}},
        headers={RETRY_AFTER_HEADER: "9999", "x-secret": canary, "authorization": canary},
    )
    client = make_client(FakeAnthropicAPI([limited]))

    with pytest.raises(AnthropicRateLimitError) as caught:
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert canary not in str(caught.value)
    assert "x-secret" not in str(caught.value)


def test_rate_limit_after_exhausted_retries_still_raises() -> None:
    """Agotados los reintentos con espera válida, el error sigue siendo de límite."""
    sleeps: list[float] = []
    api = FakeAnthropicAPI([rate_limited("1")])
    client = make_client(api, sleep=sleeps.append)

    with pytest.raises(AnthropicRateLimitError):
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert api.calls == 3
    assert sleeps == [1.0, 1.0]


def test_stop_reasons_do_not_disturb_a_normal_response() -> None:
    """Una respuesta normal sigue su camino: nada de falsos positivos."""
    client = make_client(FakeAnthropicAPI([message_response()]))

    completion = client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert completion.content
    assert completion.stop_reason == "end_turn"


def test_prompt_and_task_are_untouched_by_the_hardening(tmp_path) -> None:
    """El endurecimiento no cambia prompts ni el camino feliz de la auditoría."""
    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    api = FakeAnthropicAPI([message_response(text=json.dumps(cross_audit_payload()))])
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert report.status is CrossAuditStatus.PASS
    assert api.last_body["system"] == CROSS_AUDIT_SYSTEM_PROMPT


def test_client_can_still_be_configured_with_a_fake_key() -> None:
    """La credencial ficticia sigue valiendo para pruebas: nada se filtra por el cambio."""
    config = AnthropicConfig(api_key=FAKE_KEY)
    client: AnthropicClient = AnthropicClient(config, sleep=lambda _seconds: None)
    try:
        assert client.redact(f"clave={FAKE_KEY}") == "clave=***REDACTED***"
    finally:
        client.close()


def test_error_response_helper_is_still_valid() -> None:
    """El helper de errores del transporte falso sigue siendo el mismo."""
    response: Any = error_response(500, "boom")

    assert response.status_code == 500
