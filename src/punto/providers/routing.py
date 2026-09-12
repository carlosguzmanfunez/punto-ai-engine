"""Routing de modelos por rol, multi-proveedor (ENGINE-5.2).

El motor tiene un rol por responsabilidad y un proveedor por rol. Este módulo responde a una
sola pregunta: **quién** debe responder a cada rol, y lo responde de forma explícita y
determinista.

Lo que este módulo **no** hace, y es lo importante:

- **no** elige proveedor por disponibilidad. Una ruta dice ``anthropic`` y con eso se acaba la
  discusión: si Anthropic no está, el resultado es ``PROVIDER_UNAVAILABLE``, nunca «entonces
  DeepSeek». Un fallback silencioso convertiría la auditoría cruzada en una auditoría del mismo
  modelo, que es exactamente lo que deja de tener valor;
- **no** adivina el modelo. Cada rol tiene su variable de entorno; lo que no está configurado
  usa un valor por defecto declarado, y un nombre desconocido se acepta porque no se puede
  verificar sin credencial (``MODEL_AVAILABILITY_UNVERIFIED``).

DeepSeek sigue siendo el proveedor de los seis roles que ya existían. Anthropic entra con los
cuatro roles nuevos, y en esta fase solo ``CROSS_AUDITOR`` tiene runner real.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from punto.providers.anthropic import (
    AUDIT_MODEL_ENV,
    DEFAULT_AUDIT_MODEL,
    DEFAULT_VISUAL_MODEL,
    VISUAL_MODEL_ENV,
)
from punto.providers.base import (
    PROVIDER_ANTHROPIC,
    PROVIDER_DEEPSEEK,
    StructuredModelClient,
)
from punto.providers.deepseek import DEFAULT_MODEL as DEFAULT_DEEPSEEK_MODEL
from punto.tools.errors import ProviderRouteError

#: Proveedores que el motor conoce. Un nombre fuera de esta lista es un error de
#: configuración, no un proveedor nuevo.
KNOWN_PROVIDERS: Final[frozenset[str]] = frozenset({PROVIDER_DEEPSEEK, PROVIDER_ANTHROPIC})

#: Sufijo de la variable de entorno que fuerza el proveedor de un rol.
PROVIDER_ENV_SUFFIX: Final[str] = "_PROVIDER"


class ModelRole(StrEnum):
    """Rol del motor que necesita un modelo.

    Los seis primeros existen desde fases anteriores. Los cuatro últimos están **preparados**:
    su ruta ya está declarada y sus runners llegarán en ENGINE-5.3, salvo ``CROSS_AUDITOR``,
    que se implementa en esta fase.
    """

    ARCHITECT = "ARCHITECT"
    PLANNER = "PLANNER"
    DEVELOPER = "DEVELOPER"
    QA = "QA"
    SECURITY = "SECURITY"
    REVIEWER = "REVIEWER"
    CROSS_AUDITOR = "CROSS_AUDITOR"
    VISUAL_ARCHITECT = "VISUAL_ARCHITECT"
    FRONTEND_SPECIALIST = "FRONTEND_SPECIALIST"
    VISUAL_QA = "VISUAL_QA"


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """Proveedor y modelo asignados a un rol."""

    role: ModelRole
    provider: str
    model: str

    def __post_init__(self) -> None:
        """Rechaza rutas que no se pueden ejecutar."""
        if self.provider not in KNOWN_PROVIDERS:
            raise ProviderRouteError(
                f"proveedor desconocido {self.provider!r} para el rol {self.role.value}: "
                f"conocidos: {', '.join(sorted(KNOWN_PROVIDERS))}"
            )
        if not self.model.strip():
            raise ProviderRouteError(f"el rol {self.role.value} no declara modelo")


#: Variable de entorno canónica del modelo de cada rol.
#:
#: Los tres roles visuales comparten ``PUNTO_CLAUDE_VISUAL_MODEL`` a propósito: en esta fase
#: son el mismo tipo de trabajo (mira material visual) y separarlos en tres variables sería
#: configuración sin uso. ``CROSS_AUDITOR`` tiene la suya porque es un rol distinto.
ROLE_MODEL_ENV: Final[Mapping[ModelRole, str]] = {
    ModelRole.ARCHITECT: "PUNTO_ARCHITECT_MODEL",
    ModelRole.PLANNER: "PUNTO_PLANNER_MODEL",
    ModelRole.DEVELOPER: "PUNTO_DEVELOPER_MODEL",
    ModelRole.QA: "PUNTO_QA_MODEL",
    ModelRole.SECURITY: "PUNTO_SECURITY_MODEL",
    ModelRole.REVIEWER: "PUNTO_REVIEWER_MODEL",
    ModelRole.CROSS_AUDITOR: AUDIT_MODEL_ENV,
    ModelRole.VISUAL_ARCHITECT: VISUAL_MODEL_ENV,
    ModelRole.FRONTEND_SPECIALIST: VISUAL_MODEL_ENV,
    ModelRole.VISUAL_QA: VISUAL_MODEL_ENV,
}

#: Rutas por defecto: DeepSeek para los roles existentes, Anthropic para los nuevos.
DEFAULT_ROUTES: Final[tuple[ModelRoute, ...]] = (
    ModelRoute(ModelRole.ARCHITECT, PROVIDER_DEEPSEEK, DEFAULT_DEEPSEEK_MODEL),
    ModelRoute(ModelRole.PLANNER, PROVIDER_DEEPSEEK, DEFAULT_DEEPSEEK_MODEL),
    ModelRoute(ModelRole.DEVELOPER, PROVIDER_DEEPSEEK, DEFAULT_DEEPSEEK_MODEL),
    ModelRoute(ModelRole.QA, PROVIDER_DEEPSEEK, DEFAULT_DEEPSEEK_MODEL),
    ModelRoute(ModelRole.SECURITY, PROVIDER_DEEPSEEK, DEFAULT_DEEPSEEK_MODEL),
    ModelRoute(ModelRole.REVIEWER, PROVIDER_DEEPSEEK, DEFAULT_DEEPSEEK_MODEL),
    ModelRoute(ModelRole.CROSS_AUDITOR, PROVIDER_ANTHROPIC, DEFAULT_AUDIT_MODEL),
    ModelRoute(ModelRole.VISUAL_ARCHITECT, PROVIDER_ANTHROPIC, DEFAULT_VISUAL_MODEL),
    ModelRoute(ModelRole.FRONTEND_SPECIALIST, PROVIDER_ANTHROPIC, DEFAULT_VISUAL_MODEL),
    ModelRoute(ModelRole.VISUAL_QA, PROVIDER_ANTHROPIC, DEFAULT_VISUAL_MODEL),
)


@dataclass(frozen=True, slots=True)
class ModelRouter:
    """Conjunto de rutas, una por rol, consultable y determinista."""

    routes: tuple[ModelRoute, ...] = DEFAULT_ROUTES

    def __post_init__(self) -> None:
        """Exige exactamente una ruta por rol."""
        roles = [route.role for route in self.routes]
        if len(roles) != len(set(roles)):
            repeated = sorted({role.value for role in roles if roles.count(role) > 1})
            raise ProviderRouteError(f"hay roles con más de una ruta: {', '.join(repeated)}")
        missing = sorted(role.value for role in ModelRole if role not in set(roles))
        if missing:
            raise ProviderRouteError(f"faltan rutas para los roles: {', '.join(missing)}")

    @classmethod
    def from_environment(cls) -> ModelRouter:
        """Construye el router aplicando las sobreescrituras del entorno.

        Dos variables por rol, y ninguna más: ``PUNTO_<ROL>_PROVIDER`` para forzar el
        proveedor y la variable canónica de modelo. Nada de heurísticas ni de detección
        automática de proveedor disponible.
        """
        routes: list[ModelRoute] = []
        for default in DEFAULT_ROUTES:
            provider = (
                os.environ.get(f"PUNTO_{default.role.value}{PROVIDER_ENV_SUFFIX}", "").strip()
                or default.provider
            )
            model = (
                os.environ.get(ROLE_MODEL_ENV[default.role], "").strip() or default.model
            )
            routes.append(ModelRoute(default.role, provider, model))
        return cls(tuple(routes))

    # ------------------------------------------------------------------ consulta
    def route(self, role: ModelRole) -> ModelRoute:
        """Ruta de un rol.

        Raises:
            ProviderRouteError: si el rol no tiene ruta (no debería ocurrir: el router
                valida en construcción que estén todos).
        """
        for candidate in self.routes:
            if candidate.role is role:
                return candidate
        raise ProviderRouteError(f"el rol {role.value} no tiene ruta declarada")

    def roles_of(self, provider: str) -> tuple[ModelRole, ...]:
        """Roles asignados a un proveedor, en el orden declarado."""
        return tuple(route.role for route in self.routes if route.provider == provider)

    def providers(self) -> tuple[str, ...]:
        """Proveedores presentes en el router, sin repetir y en orden declarado."""
        seen: list[str] = []
        for route in self.routes:
            if route.provider not in seen:
                seen.append(route.provider)
        return tuple(seen)

    def is_cross_model(self, roles: Iterable[ModelRole]) -> bool:
        """True si los roles indicados se reparten entre **más de un** proveedor.

        Es la definición operativa de auditoría cruzada: si todos los roles los responde el
        mismo proveedor, la auditoría deja de ser cruzada y llamarla así sería un adorno.
        """
        return len({self.route(role).provider for role in roles}) > 1


def require_provider(
    route: ModelRoute,
    clients: Mapping[str, StructuredModelClient],
) -> StructuredModelClient:
    """Cliente del proveedor y modelo de la ruta, o error explícito.

    Es la frontera que impide el fallback silencioso: si la ruta pide un proveedor y no hay
    cliente para él, o el cliente disponible declara otro proveedor u otro modelo, se falla.
    **No** se devuelve «el que haya».

    Comprobar el modelo importa tanto como comprobar el proveedor: una ruta que dice
    ``claude-sonnet-4-5`` y un cliente configurado con otro modelo producirían un informe que
    declara algo distinto de lo que se ejecutó.

    Raises:
        ProviderRouteError: si no hay cliente del proveedor, si el cliente declara otro
            proveedor o si su modelo no es el de la ruta.
    """
    client = clients.get(route.provider)
    if client is None:
        raise ProviderRouteError(
            f"PROVIDER_UNAVAILABLE: el rol {route.role.value} pide el proveedor "
            f"{route.provider!r} y no hay cliente configurado para él. No se sustituye por "
            "otro proveedor."
        )
    if client.provider != route.provider:
        raise ProviderRouteError(
            f"PROVIDER_UNAVAILABLE: el rol {route.role.value} pide {route.provider!r} y el "
            f"cliente entregado es {client.provider!r}: no se sustituye por otro proveedor."
        )
    if client.model and client.model != route.model:
        raise ProviderRouteError(
            f"PROVIDER_MODEL_MISMATCH: el rol {route.role.value} declara el modelo "
            f"{route.model!r} y el cliente entregado usa {client.model!r}: la ruta y lo que se "
            "ejecuta no pueden divergir en silencio."
        )
    return client


__all__ = [
    "DEFAULT_ROUTES",
    "KNOWN_PROVIDERS",
    "PROVIDER_ENV_SUFFIX",
    "ROLE_MODEL_ENV",
    "ModelRole",
    "ModelRoute",
    "ModelRouter",
    "require_provider",
]
