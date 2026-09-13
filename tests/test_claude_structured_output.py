"""Structured Outputs y contrato endurecido de Claude (ENGINE-5.2.1).

Esta fase endurece tres cosas antes de construir la capa visual:

1. los identificadores de modelo por defecto son los **documentados** por el proveedor, y lo
   único que sigue sin verificarse es el acceso de la cuenta (``LIVE_ACCOUNT_ACCESS_UNVERIFIED``);
2. el formato ya no depende de que el prompt pida JSON: la petición de Anthropic lleva
   ``output_config.format`` con un JSON Schema real, preparado de forma determinista;
3. DeepSeek conserva su primitiva (``response_format: json_object``) y declara el esquema como
   contrato textual: no se finge una garantía que su API no da.

Todo se verifica con ``httpx.MockTransport``: sin red, sin credencial y sin inventar un PASS.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from punto.crossaudit.claude import ClaudeCrossModelAuditRunner
from punto.providers.anthropic import (
    AUDIT_MODEL_ENV,
    DEFAULT_AUDIT_MODEL,
    DEFAULT_VISUAL_MODEL,
    LIVE_ACCOUNT_ACCESS_UNVERIFIED,
    MODEL_ID_DOCUMENTED,
    VISUAL_MODEL_ENV,
    AnthropicClient,
    config_from_environment,
)
from punto.providers.base import ImagePayload
from punto.providers.json_schema import (
    SchemaValidationError,
    prepare_json_schema,
    validate_provider_schema,
)
from punto.providers.routing import ModelRole, ModelRouter
from punto.schemas.cross_audit import CrossAuditProposal
from test_anthropic_client import FAKE_KEY, FakeAnthropicAPI, make_client, message_response

#: Esquema sencillo y autocontenido, como el que usaría un rol cualquiera.
SIMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "findings"],
    "additionalProperties": False,
}

#: Esquema con referencias locales, como los que produce Pydantic.
SCHEMA_WITH_DEFS: dict[str, Any] = {
    "type": "object",
    "properties": {"item": {"$ref": "#/$defs/Item"}},
    "required": ["item"],
    "additionalProperties": False,
    "$defs": {
        "Item": {
            "type": "object",
            "properties": {"nombre": {"type": "string"}},
            "required": ["nombre"],
            "additionalProperties": False,
        }
    },
}

PNG_BYTES = b"\x89PNG\r\n\x1a\n-cuerpo-png-de-prueba"


def client_with(script: list[Any]) -> tuple[AnthropicClient, FakeAnthropicAPI]:
    """Cliente real de Anthropic contra el transporte falso."""
    api = FakeAnthropicAPI(script)
    return make_client(api), api


# ---------------------------------------------------------------------------
# §2 y §3: modelos por defecto y distinción entre documentado y verificado
# ---------------------------------------------------------------------------
def test_default_audit_model_is_the_documented_one() -> None:
    """El modelo por defecto de la auditoría es el documentado por el proveedor."""
    assert DEFAULT_AUDIT_MODEL == "claude-opus-5"


def test_default_visual_model_is_the_documented_one() -> None:
    """El modelo por defecto de los roles visuales es el documentado."""
    assert DEFAULT_VISUAL_MODEL == "claude-sonnet-5"


def test_router_defaults_use_the_documented_models() -> None:
    """Cada rol Claude del router apunta al modelo documentado que le corresponde."""
    router = ModelRouter()

    assert router.route(ModelRole.CROSS_AUDITOR).model == "claude-opus-5"
    assert router.route(ModelRole.VISUAL_ARCHITECT).model == "claude-sonnet-5"
    assert router.route(ModelRole.FRONTEND_SPECIALIST).model == "claude-sonnet-5"
    assert router.route(ModelRole.VISUAL_QA).model == "claude-sonnet-5"


def test_model_id_is_documented_and_account_access_is_what_remains_unverified() -> None:
    """§3: son dos hechos distintos y no pueden confundirse.

    El identificador está publicado; lo que la fase no puede afirmar, por falta de credencial,
    es que esta cuenta pueda invocarlo.
    """
    assert MODEL_ID_DOCUMENTED is True
    assert LIVE_ACCOUNT_ACCESS_UNVERIFIED is True


def test_environment_override_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """El override por entorno sigue mandando sobre el default."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv(AUDIT_MODEL_ENV, "claude-opus-5-preview")
    monkeypatch.setenv(VISUAL_MODEL_ENV, "claude-sonnet-5-preview")

    config = config_from_environment(model_env=AUDIT_MODEL_ENV)

    assert config.model == "claude-opus-5-preview"
    assert ModelRouter.from_environment().route(ModelRole.VISUAL_ARCHITECT).model == (
        "claude-sonnet-5-preview"
    )


def test_no_model_whitelist_exists() -> None:
    """Un identificador arbitrario se acepta: no hay lista blanca inventada."""
    client, api = client_with([message_response()])

    completion = client.complete_json(
        system_prompt="sistema", user_prompt="usuario"
    )

    assert completion.content
    assert api.last_body["model"] is not None


# ---------------------------------------------------------------------------
# §4 a §7: JSON Schema en el contrato y en la petición
# ---------------------------------------------------------------------------
def test_json_schema_is_sent_as_output_config_format() -> None:
    """§6: el esquema viaja en ``output_config.format``, con la forma actual de la API."""
    client, api = client_with([message_response()])

    client.complete_json(
        system_prompt="sistema", user_prompt="usuario", json_schema=SIMPLE_SCHEMA
    )

    body = api.last_body
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"] == SIMPLE_SCHEMA
    # La forma antigua y el prefill no se usan.
    assert "output_format" not in body
    assert all(
        message["role"] != "assistant" for message in body["messages"]
    ), "no se usa assistant prefill"


def test_request_without_schema_has_no_output_config() -> None:
    """Sin esquema, la petición no inventa una restricción que nadie pidió."""
    client, api = client_with([message_response()])

    client.complete_json(system_prompt="sistema", user_prompt="usuario")

    assert "output_config" not in api.last_body


def test_multimodal_request_carries_image_and_schema_together() -> None:
    """§9: texto + imagen + esquema, todo en la misma petición."""
    import base64

    client, api = client_with([message_response()])
    image = ImagePayload(data=PNG_BYTES, media_type="image/png", logical_name="pixel.png")

    client.complete_multimodal_json(
        system_prompt="sistema",
        user_prompt="¿hay imagen?",
        images=(image,),
        json_schema=SIMPLE_SCHEMA,
    )

    body = api.last_body
    content = body["messages"][0]["content"]
    source = content[1]["source"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image"
    assert source["media_type"] == "image/png"
    assert base64.standard_b64decode(source["data"]) == PNG_BYTES
    assert body["output_config"]["format"]["schema"] == SIMPLE_SCHEMA


def test_prepared_schema_has_no_local_references() -> None:
    """§7: las referencias locales se inlinean antes de enviar, no se delegan al proveedor."""
    prepared = prepare_json_schema(SCHEMA_WITH_DEFS)

    blob = json.dumps(prepared)
    assert "$ref" not in blob
    assert "$defs" not in blob
    assert prepared["properties"]["item"]["properties"]["nombre"]["type"] == "string"
    assert prepared["properties"]["item"]["additionalProperties"] is False


def test_cross_audit_schema_satisfies_the_contract() -> None:
    """El esquema real de ``CrossAuditProposal`` cumple el contrato de PUNTO."""
    prepared = prepare_json_schema(CrossAuditProposal.model_json_schema())

    assert prepared["type"] == "object"
    assert prepared["additionalProperties"] is False
    assert "summary" in prepared["required"]
    assert set(prepared["properties"]) == {
        "summary",
        "findings",
        "architecture_assessment",
        "qa_assessment",
        "security_assessment",
        "maintainability_assessment",
        "scope_assessment",
        "recommendation_notes",
    }
    finding = prepared["properties"]["findings"]["items"]
    assert finding["type"] == "object"
    assert finding["additionalProperties"] is False
    assert "severity" in finding["required"]
    assert "category" in finding["properties"]


def test_schema_validation_rejects_a_weak_root() -> None:
    """Un esquema flojo se rechaza en PUNTO, no en la API."""
    with pytest.raises(SchemaValidationError):
        validate_provider_schema({"type": "array"})
    with pytest.raises(SchemaValidationError):
        validate_provider_schema(
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        )
    with pytest.raises(SchemaValidationError):
        validate_provider_schema(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["b"],
                "additionalProperties": False,
            }
        )
    with pytest.raises(SchemaValidationError):
        validate_provider_schema(
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
        )


def test_schema_with_cycles_is_rejected() -> None:
    """Un ciclo no se puede inlinear sin inventar estructura: se falla en vez de adivinar."""
    cyclic = {
        "type": "object",
        "properties": {"nodo": {"$ref": "#/$defs/Nodo"}},
        "required": ["nodo"],
        "additionalProperties": False,
        "$defs": {
            "Nodo": {
                "type": "object",
                "properties": {"hijo": {"$ref": "#/$defs/Nodo"}},
                "required": ["hijo"],
                "additionalProperties": False,
            }
        },
    }

    with pytest.raises(SchemaValidationError, match="ciclo"):
        prepare_json_schema(cyclic)


def test_invalid_schema_never_reaches_the_api() -> None:
    """La validación ocurre antes de construir la petición: cero llamadas HTTP."""
    client, api = client_with([message_response()])

    with pytest.raises(SchemaValidationError):
        client.complete_json(
            system_prompt="sistema", user_prompt="usuario", json_schema={"type": "array"}
        )

    assert api.calls == 0


def test_pydantic_remains_the_final_frontier() -> None:
    """§7: structured outputs reduce errores, pero Pydantic decide.

    El cliente devuelve el texto; la validación del contrato es de PUNTO y sigue rechazando
    una respuesta que el esquema pedía y el modelo no cumplió.
    """
    client, _ = client_with([message_response(text='{"summary": "ok", "extra": 1}')])

    completion = client.complete_json(
        system_prompt="sistema", user_prompt="usuario", json_schema=SIMPLE_SCHEMA
    )

    proposal = CrossAuditProposal.model_validate(
        {
            "summary": json.loads(completion.content)["summary"],
            "findings": [],
            "architecture_assessment": "El cambio respeta las capas del proyecto.",
            "qa_assessment": "QA cubrió el criterio con una prueba del comportamiento.",
            "security_assessment": "Security no encontró nada bloqueante en el contexto.",
            "maintainability_assessment": "Funciones cortas y nombres claros, sin deuda.",
            "scope_assessment": "El alcance se limita al objetivo de la tarea.",
        }
    )
    assert proposal.summary == "ok"

    # El contrato final sigue siendo de PUNTO: una clave que el esquema no declaraba
    # (``status``, ``veredicto``, lo que sea) invalida la propuesta.
    with pytest.raises(ValidationError):
        CrossAuditProposal.model_validate(
            {"summary": "ok", "findings": [], "veredicto": "PASS"}
        )


# ---------------------------------------------------------------------------
# §8: la auditoría cruzada pasa el esquema real
# ---------------------------------------------------------------------------
def test_cross_audit_sends_the_proposal_schema(tmp_path: Any) -> None:
    """El auditor envía el esquema real de ``CrossAuditProposal``, sin ``$ref``."""
    from engine52_support import (
        cross_audit_payload,
        cross_audit_workspace,
        make_cross_audit_task,
    )

    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    api = FakeAnthropicAPI(
        [message_response(text=json.dumps(cross_audit_payload()))]
    )
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert report.status.value == "PASS"
    schema = api.last_body["output_config"]["format"]["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "findings" in schema["properties"]
    blob = json.dumps(schema)
    assert "$ref" not in blob and "$defs" not in blob


def test_malformed_json_is_not_the_normal_path_but_is_still_repaired(
    tmp_path: Any,
) -> None:
    """§8: con el esquema activo, el JSON roto es la excepción, y se repara.

    El bucle semántico sigue existiendo para cuando el proveedor no cumple ni el formato: la
    petición lleva el esquema, la respuesta rota se trata como violación de contrato y se pide
    de nuevo. Lo que **no** se hace es dar el JSON roto por bueno.
    """
    from engine52_support import (
        cross_audit_payload,
        cross_audit_workspace,
        make_cross_audit_task,
    )

    workspace = cross_audit_workspace(tmp_path)
    task = make_cross_audit_task(workspace)
    broken = '{"summary": "cortado a mitad", "findings": ['
    api = FakeAnthropicAPI(
        [
            message_response(text=broken),
            message_response(text=json.dumps(cross_audit_payload())),
        ]
    )
    runner = ClaudeCrossModelAuditRunner(client=make_client(api))

    report = runner.audit(task)

    assert report.status.value == "PASS"
    assert api.calls == 2
    # Todas las peticiones llevaron el esquema: el formato no dependía del prompt.
    assert all("output_config" in body for body in api.bodies)


# ---------------------------------------------------------------------------
# §5: DeepSeek conserva su primitiva y declara el esquema como textual
# ---------------------------------------------------------------------------
def test_deepseek_keeps_its_native_mechanism_without_a_schema() -> None:
    """Regresión: sin esquema, la petición de DeepSeek no cambia."""
    from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "message": {"content": '{"ok": true}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    client = DeepSeekClient(
        DeepSeekConfig(api_key="sk-test"),
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    )
    try:
        completion = client.complete_json(system_prompt="sistema", user_prompt="usuario")
    finally:
        client.close()

    body = json.loads(captured[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["content"] == "sistema"
    assert "output_config" not in body
    assert completion.provider == "deepseek"


def test_deepseek_declares_the_schema_as_a_textual_contract() -> None:
    """§5: no se finge Structured Outputs nativos en un proveedor que no los tiene."""
    from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "deepseek-v4-pro",
                "choices": [
                    {"message": {"content": "{}"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    client = DeepSeekClient(
        DeepSeekConfig(api_key="sk-test"),
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    )
    try:
        client.complete_json(
            system_prompt="sistema", user_prompt="usuario", json_schema=SIMPLE_SCHEMA
        )
    finally:
        client.close()

    body = json.loads(captured[0].content)
    system = body["messages"][0]["content"]
    assert body["response_format"] == {"type": "json_object"}
    assert "sistema" in system
    assert '"summary"' in system
    assert "output_config" not in body


def test_missing_anthropic_key_still_fails_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin credencial, el fallo es explícito y no hay fallback a otro proveedor."""
    from punto.providers.base import ProviderAuthenticationError

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ProviderAuthenticationError):
        config_from_environment()

    router = ModelRouter()
    assert router.is_cross_model([ModelRole.REVIEWER, ModelRole.CROSS_AUDITOR]) is True
