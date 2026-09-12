"""Pruebas del routing de modelos por rol (ENGINE-5.2).

El routing responde a una sola pregunta —quién debe responder a cada rol— y lo hace de forma
explícita y determinista. Aquí se comprueba, sin red y sin credenciales reales:

- que los diez roles exigidos existan y que cada uno tenga exactamente una ruta;
- que DeepSeek siga siendo el proveedor de los seis roles originales y Anthropic el de los
  cuatro roles nuevos;
- que ``ModelRoute`` rechace una ruta que no se puede ejecutar;
- que ``from_environment`` sea la única puerta de configuración, y que una variable con un
  proveedor inventado falle en vez de degradarse;
- que **no** haya fallback silencioso: ``require_provider`` falla con
  ``PROVIDER_UNAVAILABLE`` si la ruta pide un proveedor que no está, o si el cliente
  entregado declara otro proveedor.
"""

from __future__ import annotations

import pytest

from punto.providers.anthropic import (
    AUDIT_MODEL_ENV,
    DEFAULT_AUDIT_MODEL,
    DEFAULT_VISUAL_MODEL,
    VISUAL_MODEL_ENV,
)
from punto.providers.base import (
    PROVIDER_ANTHROPIC,
    PROVIDER_DEEPSEEK,
    ModelCompletion,
    StructuredModelClient,
)
from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig
from punto.providers.routing import (
    DEFAULT_ROUTES,
    KNOWN_PROVIDERS,
    PROVIDER_ENV_SUFFIX,
    ROLE_MODEL_ENV,
    ModelRole,
    ModelRoute,
    ModelRouter,
    require_provider,
)
from punto.schemas.execution import ModelUsage
from punto.tools.errors import ProviderRouteError

#: Valores exigidos, en el orden en que los declara el enum: los seis originales primero.
REQUIRED_ROLE_VALUES: tuple[str, ...] = (
    "ARCHITECT",
    "PLANNER",
    "DEVELOPER",
    "QA",
    "SECURITY",
    "REVIEWER",
    "CROSS_AUDITOR",
    "VISUAL_ARCHITECT",
    "FRONTEND_SPECIALIST",
    "VISUAL_QA",
)

#: Roles que siguen respondiendo con DeepSeek.
DEEPSEEK_ROLES: tuple[ModelRole, ...] = (
    ModelRole.ARCHITECT,
    ModelRole.PLANNER,
    ModelRole.DEVELOPER,
    ModelRole.QA,
    ModelRole.SECURITY,
    ModelRole.REVIEWER,
)

#: Roles que responden con Anthropic/Claude.
CLAUDE_ROLES: tuple[ModelRole, ...] = (
    ModelRole.CROSS_AUDITOR,
    ModelRole.VISUAL_ARCHITECT,
    ModelRole.FRONTEND_SPECIALIST,
    ModelRole.VISUAL_QA,
)

#: Mezcla de proveedores usada para comprobar la auditoría cruzada.
MIXED_ROLES: tuple[ModelRole, ...] = (ModelRole.REVIEWER, ModelRole.CROSS_AUDITOR)

#: Credencial ficticia: nunca una clave real, ni siquiera en las pruebas.
FAKE_API_KEY = "sk-test"


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
class _ClientDouble(StructuredModelClient):
    """Doble mínimo del contrato, con proveedor y modelo declarados.

    No habla con ningún proveedor: sirve para comprobar la frontera de ``require_provider``
    sin red y sin depender de la credencial real de nadie.
    """

    def __init__(self, provider: str, model: str = "modelo-de-prueba") -> None:
        self._provider = provider
        self._model = model

    @property
    def provider(self) -> str:
        """Proveedor que declara el doble."""
        return self._provider

    @property
    def model(self) -> str:
        """Modelo que declara el doble."""
        return self._model

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> ModelCompletion:
        """Devuelve una respuesta fija: estas pruebas no salen a la red."""
        return ModelCompletion(
            content='{"ok": true}',
            model=self._model,
            usage=ModelUsage(),
            latency_ms=0,
            provider=self._provider,
        )

    def redact(self, text: str) -> str:
        """El doble no conoce ninguna credencial que redactar."""
        return text

    def close(self) -> None:
        """El doble no tiene recursos que liberar."""


def _routing_environment_variables() -> tuple[str, ...]:
    """Todas las variables de entorno que consulta el routing de modelos."""
    models = tuple(ROLE_MODEL_ENV.values())
    providers = tuple(f"PUNTO_{role.value}{PROVIDER_ENV_SUFFIX}" for role in ModelRole)
    return models + providers


@pytest.fixture(autouse=True)
def _isolated_routing_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aísla cada prueba del entorno real de la máquina.

    Sin esto, una variable ``PUNTO_*`` dejada por el operador haría que las pruebas
    dependieran del estado de la máquina en vez del código.
    """
    for name in _routing_environment_variables():
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
def test_model_role_declares_exactly_the_ten_required_values() -> None:
    assert len(ModelRole) == 10
    assert tuple(role.value for role in ModelRole) == REQUIRED_ROLE_VALUES


def test_model_role_is_a_string_enum_with_stable_names() -> None:
    assert ModelRole.ARCHITECT == "ARCHITECT"
    assert ModelRole.CROSS_AUDITOR == "CROSS_AUDITOR"
    assert ModelRole.VISUAL_QA == "VISUAL_QA"


# ---------------------------------------------------------------------------
# Rutas por defecto
# ---------------------------------------------------------------------------
def test_default_routes_assign_deepseek_to_the_six_original_roles() -> None:
    routes = {route.role: route for route in DEFAULT_ROUTES}

    assert [routes[role].provider for role in DEEPSEEK_ROLES] == ["deepseek"] * 6


def test_default_routes_assign_anthropic_to_the_four_claude_roles() -> None:
    routes = {route.role: route for route in DEFAULT_ROUTES}

    assert [routes[role].provider for role in CLAUDE_ROLES] == ["anthropic"] * 4


def test_default_routes_cover_every_role_exactly_once() -> None:
    roles = [route.role for route in DEFAULT_ROUTES]

    assert len(roles) == len(set(roles))
    assert set(roles) == set(ModelRole)
    assert len(DEFAULT_ROUTES) == 10


def test_default_routes_never_declare_an_empty_model() -> None:
    assert all(route.model.strip() for route in DEFAULT_ROUTES)


def test_default_routes_use_the_declared_default_models() -> None:
    routes = {route.role: route for route in DEFAULT_ROUTES}

    assert routes[ModelRole.CROSS_AUDITOR].model == DEFAULT_AUDIT_MODEL
    assert routes[ModelRole.VISUAL_ARCHITECT].model == DEFAULT_VISUAL_MODEL
    assert routes[ModelRole.FRONTEND_SPECIALIST].model == DEFAULT_VISUAL_MODEL
    assert routes[ModelRole.VISUAL_QA].model == DEFAULT_VISUAL_MODEL


# ---------------------------------------------------------------------------
# ModelRoute: una ruta que no se puede ejecutar no se construye
# ---------------------------------------------------------------------------
def test_model_route_rejects_an_unknown_provider() -> None:
    with pytest.raises(ProviderRouteError):
        ModelRoute(ModelRole.QA, "openai", "modelo-x")


@pytest.mark.parametrize("model", ["", "   "])
def test_model_route_rejects_an_empty_model(model: str) -> None:
    with pytest.raises(ProviderRouteError):
        ModelRoute(ModelRole.QA, PROVIDER_DEEPSEEK, model)


def test_model_route_accepts_a_known_provider_with_a_model() -> None:
    route = ModelRoute(ModelRole.CROSS_AUDITOR, PROVIDER_ANTHROPIC, "claude-de-prueba")

    assert route.role is ModelRole.CROSS_AUDITOR
    assert route.provider == PROVIDER_ANTHROPIC
    assert route.model == "claude-de-prueba"


# ---------------------------------------------------------------------------
# ModelRouter: consulta determinista
# ---------------------------------------------------------------------------
def test_router_without_arguments_exposes_the_default_routes() -> None:
    assert ModelRouter().routes == DEFAULT_ROUTES


def test_router_route_returns_the_route_of_each_role() -> None:
    router = ModelRouter()

    for role in ModelRole:
        assert router.route(role).role is role


def test_router_roles_of_anthropic_are_exactly_the_claude_roles() -> None:
    router = ModelRouter()

    assert router.roles_of(PROVIDER_ANTHROPIC) == CLAUDE_ROLES
    assert router.roles_of(PROVIDER_DEEPSEEK) == DEEPSEEK_ROLES


def test_router_providers_are_deepseek_then_anthropic() -> None:
    router = ModelRouter()

    assert router.providers() == ("deepseek", "anthropic")
    assert frozenset(router.providers()) == KNOWN_PROVIDERS


def test_router_rejects_a_repeated_role() -> None:
    with pytest.raises(ProviderRouteError):
        ModelRouter((*DEFAULT_ROUTES, DEFAULT_ROUTES[0]))


def test_router_rejects_a_missing_role() -> None:
    with pytest.raises(ProviderRouteError):
        ModelRouter(DEFAULT_ROUTES[:-1])


def test_is_cross_model_is_false_when_all_roles_share_a_provider() -> None:
    router = ModelRouter()

    assert router.is_cross_model(DEEPSEEK_ROLES) is False
    assert router.is_cross_model(CLAUDE_ROLES) is False


def test_is_cross_model_is_true_when_the_roles_mix_providers() -> None:
    assert ModelRouter().is_cross_model(MIXED_ROLES) is True


# ---------------------------------------------------------------------------
# from_environment: la configuración explícita, sin heurísticas
# ---------------------------------------------------------------------------
def test_from_environment_without_variables_matches_the_defaults() -> None:
    assert ModelRouter.from_environment().routes == DEFAULT_ROUTES


def test_from_environment_applies_the_audit_model_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert AUDIT_MODEL_ENV == "PUNTO_CLAUDE_AUDIT_MODEL"
    monkeypatch.setenv(AUDIT_MODEL_ENV, "modelo-x")

    router = ModelRouter.from_environment()

    assert router.route(ModelRole.CROSS_AUDITOR).model == "modelo-x"
    assert router.route(ModelRole.CROSS_AUDITOR).provider == PROVIDER_ANTHROPIC
    # Los roles visuales usan su propia variable: una sobreescritura no contamina a la otra.
    assert router.route(ModelRole.VISUAL_QA).model == DEFAULT_VISUAL_MODEL


def test_from_environment_applies_the_role_provider_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = f"PUNTO_{ModelRole.CROSS_AUDITOR.value}{PROVIDER_ENV_SUFFIX}"
    assert name == "PUNTO_CROSS_AUDITOR_PROVIDER"
    monkeypatch.setenv(name, PROVIDER_DEEPSEEK)

    router = ModelRouter.from_environment()

    assert router.route(ModelRole.CROSS_AUDITOR).provider == PROVIDER_DEEPSEEK
    assert router.roles_of(PROVIDER_DEEPSEEK) == (*DEEPSEEK_ROLES, ModelRole.CROSS_AUDITOR)
    assert router.is_cross_model(MIXED_ROLES) is False


def test_from_environment_uses_the_canonical_model_variable_of_each_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ROLE_MODEL_ENV[ModelRole.QA] == "PUNTO_QA_MODEL"
    assert ROLE_MODEL_ENV[ModelRole.VISUAL_ARCHITECT] == VISUAL_MODEL_ENV
    monkeypatch.setenv(ROLE_MODEL_ENV[ModelRole.QA], "deepseek-de-prueba")

    router = ModelRouter.from_environment()

    assert router.route(ModelRole.QA).provider == PROVIDER_DEEPSEEK
    assert router.route(ModelRole.QA).model == "deepseek-de-prueba"


def test_from_environment_rejects_an_unknown_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUNTO_QA_PROVIDER", "openai")

    with pytest.raises(ProviderRouteError):
        ModelRouter.from_environment()


def test_from_environment_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AUDIT_MODEL_ENV, "modelo-x")
    monkeypatch.setenv("PUNTO_QA_PROVIDER", PROVIDER_DEEPSEEK)

    builds = {ModelRouter.from_environment().routes for _ in range(3)}

    assert len(builds) == 1


# ---------------------------------------------------------------------------
# require_provider: la frontera que impide el fallback silencioso
# ---------------------------------------------------------------------------
def test_require_provider_fails_without_a_client_for_the_route() -> None:
    route = ModelRouter().route(ModelRole.CROSS_AUDITOR)
    clients = {PROVIDER_DEEPSEEK: _ClientDouble(PROVIDER_DEEPSEEK)}

    with pytest.raises(ProviderRouteError, match="PROVIDER_UNAVAILABLE"):
        require_provider(route, clients)


def test_require_provider_returns_the_client_of_the_route() -> None:
    route = ModelRouter().route(ModelRole.CROSS_AUDITOR)
    client = _ClientDouble(PROVIDER_ANTHROPIC, route.model)

    assert require_provider(route, {PROVIDER_ANTHROPIC: client}) is client


def test_require_provider_rejects_a_client_with_another_model() -> None:
    """La ruta y lo que se ejecuta no pueden divergir en silencio."""
    route = ModelRouter().route(ModelRole.CROSS_AUDITOR)
    other = _ClientDouble(PROVIDER_ANTHROPIC, "otro-modelo")

    with pytest.raises(ProviderRouteError, match="PROVIDER_MODEL_MISMATCH"):
        require_provider(route, {PROVIDER_ANTHROPIC: other})


def test_require_provider_rejects_a_client_from_another_provider() -> None:
    route = ModelRouter().route(ModelRole.CROSS_AUDITOR)
    impostor = _ClientDouble(PROVIDER_DEEPSEEK)

    with pytest.raises(ProviderRouteError, match="PROVIDER_UNAVAILABLE"):
        require_provider(route, {PROVIDER_ANTHROPIC: impostor})

def test_require_provider_accepts_a_real_deepseek_client_for_a_deepseek_route() -> None:
    config = DeepSeekConfig(api_key=FAKE_API_KEY)

    with DeepSeekClient(config) as client:
        route = ModelRouter().route(ModelRole.QA)

        assert client.provider == PROVIDER_DEEPSEEK
        assert require_provider(route, {"deepseek": client}) is client
