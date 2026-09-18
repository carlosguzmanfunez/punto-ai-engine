"""PROVIDER CONFIGURATION DASHBOARD v0 — backend, registro y secreto.

La suite usa el mismo backend que sirve la página (`create_app`), con la configuración local y el
almacén de secretos redirigidos a un directorio temporal: nada toca el repositorio ni el HOME real.
El estado que se afirma es el estado real del motor (la máquina no tiene Codex instalado y Claude
Code no tiene sesión), así que varias comprobaciones afirman estados concretos **o** su alternativa
honesta, nunca un PASS inventado.

    pytest tests/test_provider_dashboard.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from punto.api.app import create_app
from punto.providers.contract import ProviderRole
from punto.providers.registry import (
    CAPABILITIES,
    CustomProviderSpec,
    InvalidProviderError,
    ProviderRegistry,
    UnknownProviderError,
)
from punto.providers.secrets import SECRETS_FILE_ENV, SecretStore, SecretStoreError
from punto.providers.settings import LOCAL_CONFIG_ENV

#: Credencial sintética con la marca de canario documentado del repositorio.
TEST_KEY = "sk-test-CANARY-0123456789abcdef"

#: Estados que la UI puede mostrar. Cualquier otro sería inventado.
STATUS_VOCABULARY = frozenset(
    {
        "CONNECTED",
        "NOT_CONFIGURED",
        "NOT_INSTALLED",
        "NOT_AUTHENTICATED",
        "UNAVAILABLE",
        "LIMIT_REACHED",
        "ERROR",
    }
)


@pytest.fixture
def dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, ProviderRegistry]:
    """Cliente del dashboard con configuración local y secretos en un temporal.

    El registro lo crea la propia aplicación (es el que sirve la página): aquí se recupera de su
    estado en vez de registrar un segundo juego de rutas.
    """
    monkeypatch.setenv(SECRETS_FILE_ENV, str(tmp_path / "secrets.json"))
    monkeypatch.setenv(LOCAL_CONFIG_ENV, str(tmp_path / "providers.local.yaml"))
    application = create_app(environment="test")
    registry: ProviderRegistry = application.state.provider_registry
    return TestClient(application), registry


def _providers(client: TestClient) -> list[dict[str, object]]:
    """Catálogo tal como lo ve la UI."""
    return list(client.get("/providers").json()["providers"])


def _provider(client: TestClient, name: str) -> dict[str, object]:
    """Ficha de un proveedor."""
    return dict(client.get(f"/providers/{name}").json())


# ---------------------------------------------------------------------------
# T1 — providers list
# ---------------------------------------------------------------------------
def test_t1_la_pagina_y_el_catalogo_cargan(dashboard: tuple[TestClient, ProviderRegistry]) -> None:
    """La página del dashboard se sirve y el catálogo trae los tres proveedores con su rol."""
    client, _ = dashboard

    page = client.get("/dashboard")
    catalogo = _providers(client)

    assert page.status_code == 200
    assert "Configuración" in page.text and "Proveedores" in page.text
    assert [item["provider"] for item in catalogo] == ["openai", "deepseek", "anthropic"]
    assert [item["display_name"] for item in catalogo] == [
        "OpenAI / Codex",
        "DeepSeek",
        "Anthropic / Claude",
    ]
    assert [item["roles"] for item in catalogo] == [["ARCHITECT"], ["BUILDER"], ["VISUAL_QA"]]
    assert client.get("/roles").json()["roles"] == {
        "ARCHITECT": "openai",
        "BUILDER": "deepseek",
        "VISUAL_QA": "anthropic",
    }


# ---------------------------------------------------------------------------
# T2 — estado real, no inventado
# ---------------------------------------------------------------------------
def test_t2_el_estado_es_el_del_backend(
    dashboard: tuple[TestClient, ProviderRegistry],
) -> None:
    """Cada proveedor declara un estado del vocabulario, coherente con su autenticación real."""
    client, _ = dashboard

    for item in _providers(client):
        assert item["status"] in STATUS_VOCABULARY, item
        auth = _provider(client, str(item["provider"]))["auth"]
        if auth == "NOT_INSTALLED":
            assert item["status"] == "NOT_INSTALLED"
        elif auth == "NOT_AUTHENTICATED":
            assert item["status"] == "NOT_AUTHENTICATED"
        elif auth == "AUTHENTICATED":
            assert item["status"] == "CONNECTED"


def test_t2b_sin_credencial_el_estado_no_es_conectado(
    dashboard: tuple[TestClient, ProviderRegistry],
) -> None:
    """Un proveedor de API sin clave no puede aparecer como conectado."""
    client, _ = dashboard

    client.post(
        "/providers/deepseek/transport", json={"transport": "existing", "auth_mode": "api_key"}
    )

    ficha = _provider(client, "deepseek")
    assert ficha["api_key_configured"] is False
    assert ficha["status"] in {"NOT_CONFIGURED", "NOT_AUTHENTICATED"}


# ---------------------------------------------------------------------------
# T3 — selección de transporte
# ---------------------------------------------------------------------------
def test_t3_cambio_de_transporte(
    dashboard: tuple[TestClient, ProviderRegistry],
) -> None:
    """El transporte se cambia por la API del dashboard y la configuración lo refleja."""
    client, registry = dashboard

    antes = _provider(client, "anthropic")
    assert antes["transport"] == "claude_code"

    respuesta = client.post(
        "/providers/anthropic/transport", json={"transport": "api", "auth_mode": "api_key"}
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["transport"] == "api"
    assert respuesta.json()["auth_mode"] == "api_key"
    assert _provider(client, "anthropic")["transport"] == "api"
    assert registry.settings().transport_of("anthropic") == "api"
    assert client.post(
        "/providers/anthropic/transport", json={"transport": "existing", "auth_mode": "api_key"}
    ).status_code == 400
    assert client.post(
        "/providers/anthropic/transport", json={"transport": "api", "auth_mode": "chatgpt"}
    ).status_code == 400


# ---------------------------------------------------------------------------
# T4 — selección de modelo
# ---------------------------------------------------------------------------
def test_t4_cambio_de_modelo(dashboard: tuple[TestClient, ProviderRegistry]) -> None:
    """El modelo es configuración: se cambia por la API y se persiste en la configuración local."""
    client, registry = dashboard

    respuesta = client.post("/providers/openai/model", json={"model": "gpt-5-codex-mini"})

    assert respuesta.status_code == 200
    assert respuesta.json()["model"] == "gpt-5-codex-mini"
    assert registry.settings().model_of("openai") == "gpt-5-codex-mini"
    assert client.post("/providers/openai/model", json={"model": "  "}).status_code == 400
    assert client.post("/providers/openai/model", json={"model": "dos palabras"}).status_code == 400


# ---------------------------------------------------------------------------
# T5 y T12 — asignación de roles y reflejo en el ProviderRouter
# ---------------------------------------------------------------------------
def test_t5_y_t12_el_cambio_de_rol_lo_ve_el_router(
    dashboard: tuple[TestClient, ProviderRegistry],
) -> None:
    """Asignar un rol por el dashboard cambia la asignación del `ProviderRouter` real."""
    client, registry = dashboard

    respuesta = client.post("/roles/BUILDER", json={"provider": "anthropic"})

    assert respuesta.status_code == 200
    assert respuesta.json()["roles"]["BUILDER"] == "anthropic"
    assert registry.roles()["BUILDER"] == "anthropic"
    assert registry.router_instance().get_provider_for_role(ProviderRole.BUILDER) == "anthropic"
    assert client.post("/roles/NOPE", json={"provider": "openai"}).status_code == 400
    assert client.post("/roles/BUILDER", json={"provider": "nope"}).status_code == 404
    assert _providers(client)[0]["roles"] == ["ARCHITECT"]


# ---------------------------------------------------------------------------
# T6, T14 — test connection y estado de autenticación visible
# ---------------------------------------------------------------------------
def test_t6_test_connection_usa_la_sonda_real(
    dashboard: tuple[TestClient, ProviderRegistry],
) -> None:
    """``Test connection`` devuelve un estado del vocabulario y su detalle real."""
    client, _ = dashboard

    codigo = client.post("/providers/openai/test", json={}).json()

    assert codigo["status"] in STATUS_VOCABULARY
    assert codigo["connected"] is (codigo["status"] == "CONNECTED")
    assert "detail" in codigo
    assert "sk-" not in json.dumps(codigo)


def test_t14_el_estado_de_codex_y_claude_es_visible_sin_credenciales(
    dashboard: tuple[TestClient, ProviderRegistry],
) -> None:
    """Codex y Claude Code muestran su estado de sesión, y su comando oficial, sin secretos."""
    client, _ = dashboard

    codex = client.post("/providers/openai/connect", json={}).json()
    claude = client.post("/providers/anthropic/connect", json={}).json()

    assert codex["command"] == "codex login"
    assert claude["command"] == "claude auth login"
    estados = {"NOT_INSTALLED", "NOT_AUTHENTICATED", "AUTHENTICATED", "UNAVAILABLE"}
    assert codex["auth_status"] in estados
    assert "no pide contraseñas" in codex["instructions"]
    assert TEST_KEY not in json.dumps([codex, claude])


# ---------------------------------------------------------------------------
# T7 — alta de proveedor nuevo
# ---------------------------------------------------------------------------
def test_t7_proveedor_nuevo_registrado(
    dashboard: tuple[TestClient, ProviderRegistry],
) -> None:
    """Un endpoint compatible se da de alta con sus capacidades y aparece en el catálogo."""
    client, registry = dashboard

    respuesta = client.post(
        "/providers/custom",
        json={
            "provider_id": "qwen_local",
            "display_name": "Qwen local",
            "base_url": "https://api.ejemplo.com/v1",
            "model": "qwen2.5-14b-instruct",
            "adapter_type": "openai_compatible",
            "capabilities": ["TEXT", "CODING"],
            "api_key": TEST_KEY,
        },
    )

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["provider"] == "qwen_local"
    assert cuerpo["builtin"] is False
    assert cuerpo["capabilities"] == ["TEXT", "CODING"]
    assert cuerpo["api_key_configured"] is True
    assert "qwen_local" in [item["provider"] for item in _providers(client)]
    assert registry.descriptor("qwen_local").model == "qwen2.5-14b-instruct"


def test_t7b_un_tipo_de_adaptador_desconocido_se_rechaza(
    dashboard: tuple[TestClient, ProviderRegistry],
) -> None:
    """El contrato es extensible, pero no acepta un adaptador que no existe."""
    client, _ = dashboard

    respuesta = client.post(
        "/providers/custom",
        json={
            "provider_id": "raro",
            "display_name": "Raro",
            "base_url": "https://api.ejemplo.com/v1",
            "model": "x",
            "adapter_type": "telepatia",
        },
    )

    assert respuesta.status_code == 400
    assert "adaptador" in respuesta.json()["detail"]
    with pytest.raises(InvalidProviderError):
        CustomProviderSpec(
            provider_id="otro",
            display_name="Otro",
            base_url="https://x/v1",
            model="m",
            capabilities=("MAGIC",),
        ).validate()


# ---------------------------------------------------------------------------
# T8, T9 — la clave es de solo escritura y nunca se filtra
# ---------------------------------------------------------------------------
def test_t8_la_api_key_es_de_solo_escritura(
    dashboard: tuple[TestClient, ProviderRegistry],
) -> None:
    """Guardar una clave solo devuelve ``api_key_configured``; el valor no vuelve nunca."""
    client, registry = dashboard

    respuesta = client.post("/providers/openai/api-key", json={"api_key": TEST_KEY})

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "provider": "openai",
        "api_key_configured": True,
        "status": respuesta.json()["status"],
    }
    assert TEST_KEY not in json.dumps(client.get("/providers").json())
    assert TEST_KEY not in json.dumps(_provider(client, "openai"))
    assert registry.secrets.api_key("openai") == TEST_KEY
    assert client.delete("/providers/openai/api-key").json()["api_key_configured"] is False
    assert client.post("/providers/openai/api-key", json={"api_key": "corta"}).status_code == 422


def test_t9_los_secretos_viven_fuera_del_repositorio(
    dashboard: tuple[TestClient, ProviderRegistry], tmp_path: Path
) -> None:
    """El almacén de secretos está fuera del árbol del repositorio y con permisos restrictivos."""
    client, registry = dashboard

    client.post("/providers/deepseek/api-key", json={"api_key": TEST_KEY})
    ruta = Path(registry.secrets.path)

    assert ruta.is_file()
    assert ruta == tmp_path / "secrets.json"
    repositorio = Path(__file__).resolve().parents[1]
    assert not ruta.resolve().is_relative_to(repositorio), (
        "el almacén de secretos nunca vive dentro del repositorio"
    )
    contenido = ruta.read_text(encoding="utf-8")
    assert TEST_KEY in contenido, "el almacén es el único sitio donde vive la clave"
    assert SecretStore(ruta).has_api_key("deepseek") is True
    assert client.get("/providers").json()["providers"][1]["api_key_configured"] is True


def test_t9b_un_error_del_proveedor_no_devuelve_la_clave(
    dashboard: tuple[TestClient, ProviderRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cualquier error que salga hacia el navegador va saneado."""
    client, _ = dashboard
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    client.post("/providers/openai/api-key", json={"api_key": TEST_KEY})

    respuesta = client.post("/providers/openai/test", json={})

    assert TEST_KEY not in json.dumps(respuesta.json())
    assert respuesta.status_code == 200


# ---------------------------------------------------------------------------
# T10 — proveedor inválido
# ---------------------------------------------------------------------------
def test_t10_proveedor_invalido_rechazado(
    dashboard: tuple[TestClient, ProviderRegistry],
) -> None:
    """Un proveedor que no está en el catálogo no se puede consultar, probar ni asignar."""
    client, registry = dashboard

    assert client.get("/providers/gemini").status_code == 404
    assert client.post("/providers/gemini/test", json={}).status_code == 404
    assert client.post("/providers/gemini/model", json={"model": "x"}).status_code == 404
    with pytest.raises(UnknownProviderError):
        registry.descriptor("gemini")
    with pytest.raises(InvalidProviderError):
        registry.register_custom(
            CustomProviderSpec(
                provider_id="openai", display_name="Otro OpenAI", base_url="https://x/v1", model="m"
            )
        )


# ---------------------------------------------------------------------------
# T11 — aviso de capacidad incompatible
# ---------------------------------------------------------------------------
def test_t11_aviso_de_capacidad_incompatible(
    dashboard: tuple[TestClient, ProviderRegistry],
) -> None:
    """Asignar a VISUAL_QA un proveedor sin VISION avisa, sin bloquear ni habilitar nada."""
    client, _ = dashboard
    client.post(
        "/providers/custom",
        json={
            "provider_id": "texto_puro",
            "display_name": "Texto puro",
            "base_url": "https://api.ejemplo.com/v1",
            "model": "modelo-texto",
            "capabilities": ["TEXT"],
        },
    )

    respuesta = client.post("/roles/VISUAL_QA", json={"provider": "texto_puro"})

    assert respuesta.status_code == 200
    assert respuesta.json()["roles"]["VISUAL_QA"] == "texto_puro"
    assert respuesta.json()["advisories"], "tiene que avisar de la capacidad que falta"
    assert "VISION" in respuesta.json()["advisories"][0]
    assert "no concede capacidades" in respuesta.json()["authority"]
    sin_aviso = client.post("/roles/VISUAL_QA", json={"provider": "anthropic"})
    assert sin_aviso.json()["advisories"] == []


# ---------------------------------------------------------------------------
# T13 — el dashboard no concede autoridad
# ---------------------------------------------------------------------------
def test_t13_el_dashboard_no_concede_autoridad(
    dashboard: tuple[TestClient, ProviderRegistry],
) -> None:
    """Configurar proveedores no crea aprobaciones ni cambia el Human Gate."""
    client, registry = dashboard

    client.post("/providers/anthropic/transport", json={"transport": "api", "auth_mode": "api_key"})
    client.post("/roles/ARCHITECT", json={"provider": "deepseek"})
    ficha = _provider(client, "openai")

    assert "authority" not in ficha
    assert "capabilities" in ficha
    assert set(ficha["capabilities"]) <= set(CAPABILITIES)
    assert registry.router_instance().providers() == ("openai", "deepseek", "anthropic")
    assert client.get("/openapi.json").status_code == 200
    rutas = client.get("/openapi.json").json()["paths"]
    assert "/providers" in rutas and "/roles" in rutas
    propias = [ruta for ruta in rutas if ruta.startswith(("/providers", "/roles", "/dashboard"))]
    assert propias, "el dashboard tiene que exponer sus rutas"
    assert not any("approve" in ruta or "gate" in ruta for ruta in propias), (
        "el dashboard de configuración no expone ni sustituye el Human Gate"
    )


# ---------------------------------------------------------------------------
# Registro y secretos, a nivel de componente
# ---------------------------------------------------------------------------
def test_el_registro_valida_su_configuracion(tmp_path: Path) -> None:
    """Transporte inexistente, capacidad inventada y clave corta se rechazan con su motivo."""
    registry = ProviderRegistry(
        secrets=SecretStore(tmp_path / "secrets.json"), local_path=tmp_path / "local.yaml"
    )

    with pytest.raises(InvalidProviderError):
        registry.set_transport("openai", "telepatia")
    with pytest.raises(InvalidProviderError):
        registry.set_model("openai", "")
    with pytest.raises(SecretStoreError):
        registry.set_api_key("openai", "corta")
    with pytest.raises(InvalidProviderError):
        registry.remove_custom("openai")
    assert registry.descriptor("openai").transport == "codex"
    assert registry.descriptor("openai").adapter_type == "openai_compatible"


def test_la_pagina_no_contiene_secretos_ni_estado_hardcodeado() -> None:
    """La página no trae ningún estado pintado: el estado lo pone la API en el DOM."""
    from punto.api.dashboard import DASHBOARD_HTML

    texto = DASHBOARD_HTML.read_text(encoding="utf-8")

    assert 'class="dot CONNECTED"' not in texto, "ningún punto verde escrito a mano"
    assert "state.providers.map(card)" in texto, "las tarjetas se pintan desde el catálogo real"
    assert 'id="status-openai"' not in texto
    assert "sk-" not in texto
    assert "/providers" in texto and "/roles" in texto
