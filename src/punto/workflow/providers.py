"""Capacidades de proveedor declaradas para el kernel (ENGINE-6.0).

Porqué existe esta tabla y porqué no basta con tener clientes de proveedor: el kernel tiene
que poder responder **antes** de intentar nada si un rol se puede ejecutar, con qué proveedor
y por qué no. La alternativa —intentar, fallar y ver qué pasa— convierte cada rol en una
sorpresa y hace imposible que el workflow sea determinista.

Reglas que este módulo hace cumplir:

- **Nada de fallback silencioso.** :meth:`ProviderCapabilityRegistry.require` devuelve la
  capacidad pedida o falla; nunca devuelve «otro proveedor que sí está». Si el rol pedía
  DeepSeek y DeepSeek no tiene credencial, la respuesta es un error explícito, no Claude.
- **La credencial se mira, no se lee.** Solo se consulta la **presencia** de
  ``DEEPSEEK_API_KEY``, ``OPENAI_API_KEY`` y ``ANTHROPIC_API_KEY``: su valor no se copia, no se
  guarda, no se compara y no se registra en ninguna parte. Por eso ningún
  :class:`ProviderCapability` construido aquí puede contener un secreto, y hay una prueba que
  lo fija con un valor canario.
- **La disponibilidad se declara, no se supone.** ``available`` describe lo que PUNTO sabe hoy
  —hay credencial y el proveedor declara roles—, y ``live_verified`` sigue siendo ``False``
  para todos: en esta fase **no** hay ninguna ejecución real verificada, y afirmar lo contrario
  sería inventar evidencia.

El doble de prueba (``fake``) existe para que el kernel y sus pruebas puedan recorrer el
workflow completo sin llamar a nadie. Nunca se elige de forma implícita: sustituir un
proveedor real por un doble sería exactamente el fallback silencioso que esta fase prohíbe.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Final

from punto.providers.base import PROVIDER_ANTHROPIC, PROVIDER_DEEPSEEK
from punto.schemas.workflow import CredentialState, ProviderCapability, RoleName
from punto.workflow.errors import WorkflowProviderUnavailableError

#: Variables de entorno cuya **presencia** acredita a cada proveedor real. El nombre es lo
#: único que se consulta; el valor nunca sale de la expresión que lo comprueba.
DEEPSEEK_API_KEY_ENV: Final[str] = "DEEPSEEK_API_KEY"
ANTHROPIC_API_KEY_ENV: Final[str] = "ANTHROPIC_API_KEY"
OPENAI_API_KEY_ENV: Final[str] = "OPENAI_API_KEY"

#: Proveedor declarado pero sin roles: se declara para que su ausencia de soporte sea un dato
#: explícito y no un olvido.
PROVIDER_OPENAI: Final[str] = "openai"

#: Doble de prueba. Solo se usa si el llamante lo nombra: ver la nota del módulo.
PROVIDER_FAKE: Final[str] = "fake"

#: Roles de ingeniería: los que el motor ya ejecuta con DeepSeek.
_ENGINEERING_ROLES: Final[tuple[RoleName, ...]] = (
    RoleName.ARCHITECT,
    RoleName.PLANNER,
    RoleName.DEVELOPER,
    RoleName.QA,
    RoleName.SECURITY,
    RoleName.REVIEWER,
)

#: Roles que exigen visión o auditoría independiente: los cubre Anthropic.
_VISUAL_ROLES: Final[tuple[RoleName, ...]] = (RoleName.CROSS_AUDIT, RoleName.VISUAL_QA)

_ALL_ROLES: Final[tuple[RoleName, ...]] = _ENGINEERING_ROLES + _VISUAL_ROLES

#: Credencial que acredita a cada proveedor real.
_CREDENTIAL_ENV: Final[Mapping[str, str]] = {
    PROVIDER_DEEPSEEK: DEEPSEEK_API_KEY_ENV,
    PROVIDER_ANTHROPIC: ANTHROPIC_API_KEY_ENV,
    PROVIDER_OPENAI: OPENAI_API_KEY_ENV,
}


class ProviderCapabilityRegistry:
    """Tabla consultable de capacidades declaradas, en orden de declaración.

    El orden importa y por eso se conserva tal cual: cuando varios proveedores cubren el mismo
    rol, :meth:`require` devuelve el primero que esté disponible, y eso tiene que ser
    reproducible y no depender de cómo itere un diccionario.
    """

    def __init__(self, capabilities: Sequence[ProviderCapability]) -> None:
        self._capabilities: tuple[ProviderCapability, ...] = tuple(capabilities)

    @property
    def capabilities(self) -> tuple[ProviderCapability, ...]:
        """Todas las capacidades declaradas, en el orden en que se dieron."""
        return self._capabilities

    def get(self, provider: str) -> ProviderCapability | None:
        """Capacidad declarada de un proveedor concreto, si está declarado."""
        for capability in self._capabilities:
            if capability.provider == provider:
                return capability
        return None

    def for_role(self, role: RoleName) -> tuple[ProviderCapability, ...]:
        """Capacidades que **declaran** cubrir el rol, sin filtrar por disponibilidad.

        No filtra a propósito: quien pregunta qué proveedores cubren un rol necesita ver
        también los que hoy no pueden —sin credencial o no disponibles— para poder explicar el
        hueco en lugar de esconderlo.
        """
        return tuple(
            capability for capability in self._capabilities if capability.supports(role)
        )

    def require(self, role: RoleName, provider: str | None = None) -> ProviderCapability:
        """Devuelve la capacidad que puede ejecutar el rol, o falla explicando por qué no.

        Con ``provider`` concreto, la capacidad se busca por nombre y tiene que cubrir el rol,
        estar disponible y tener credencial presente. Sin ``provider``, se toma la primera
        capacidad declarada para el rol que cumpla las tres condiciones.

        Nunca se devuelve un proveedor distinto del pedido ni se recurre al doble de prueba de
        forma implícita: un proveedor real ausente es un hueco que se reporta, no una
        sustitución.

        Args:
            role: Rol que se quiere ejecutar.
            provider: Proveedor exigido, si el llamante lo conoce. ``None`` deja que PUNTO
                elija el primero declarado y utilizable.

        Returns:
            La capacidad declarada que cubre el rol y se puede invocar.

        Raises:
            WorkflowProviderUnavailableError: si no hay ninguna capacidad que pueda ejecutar el
                rol, distinguiendo «sin credencial» de «declarado no disponible» y nombrando
                siempre el rol y el proveedor.
        """
        if provider is not None:
            declared = self.get(provider)
            if declared is None:
                raise WorkflowProviderUnavailableError(
                    f"el proveedor {provider!r} no está declarado y el rol {role.value} no se "
                    "ejecuta con un proveedor distinto: no hay fallback"
                )
            return _usable(declared, role)

        candidates = tuple(
            capability
            for capability in self.for_role(role)
            if capability.provider != PROVIDER_FAKE
        )
        for capability in candidates:
            if capability.available and capability.credential_state is CredentialState.PRESENT:
                return capability
        raise WorkflowProviderUnavailableError(_unavailable_detail(role, candidates))


def credential_state_from_environment(
    env: Mapping[str, str] | None = None,
) -> tuple[ProviderCapability, ...]:
    """Declara las capacidades del motor con el estado de credencial leído del entorno.

    Solo se comprueba **si** la variable de cada proveedor está declarada y no vacía. El valor
    no se copia, no se guarda, no se compara con nada y no aparece en el detalle de ningún
    error: un ``ProviderCapability`` que llevara la credencial dentro acabaría en un log, en un
    checkpoint o en un informe de auditoría.

    ``fake`` se declara siempre disponible y con credencial presente porque no llama a nadie;
    ``live_verified`` se queda en ``False`` en todas, incluidas las reales: que exista una
    credencial no es prueba de que una ejecución real haya funcionado.

    Args:
        env: Entorno a consultar. Si es ``None`` se usa ``os.environ``. En pruebas se pasa un
            mapping explícito para no depender —ni hablar— del entorno real.

    Returns:
        Las cuatro capacidades declaradas, en orden determinista: DeepSeek, Anthropic, OpenAI
        y el doble de prueba.
    """
    source = os.environ if env is None else env
    deepseek_state = _credential_state(source, PROVIDER_DEEPSEEK)
    anthropic_state = _credential_state(source, PROVIDER_ANTHROPIC)
    openai_state = _credential_state(source, PROVIDER_OPENAI)
    return (
        ProviderCapability(
            provider=PROVIDER_DEEPSEEK,
            role_support=_ENGINEERING_ROLES,
            structured_output=True,
            available=deepseek_state is CredentialState.PRESENT,
            credential_state=deepseek_state,
        ),
        ProviderCapability(
            provider=PROVIDER_ANTHROPIC,
            role_support=_VISUAL_ROLES,
            vision=True,
            structured_output=True,
            available=anthropic_state is CredentialState.PRESENT,
            credential_state=anthropic_state,
        ),
        ProviderCapability(
            provider=PROVIDER_OPENAI,
            role_support=(),
            # Sin roles declarados no está disponible para el workflow, tenga o no credencial:
            # ``available`` dice si se puede usar **para algo**, no si el proveedor responde.
            available=False,
            credential_state=openai_state,
        ),
        ProviderCapability(
            provider=PROVIDER_FAKE,
            role_support=_ALL_ROLES,
            vision=True,
            structured_output=True,
            available=True,
            credential_state=CredentialState.PRESENT,
        ),
    )


def default_capabilities(
    env: Mapping[str, str] | None = None,
) -> ProviderCapabilityRegistry:
    """Registro por defecto del kernel, construido desde el entorno declarado.

    Es el único camino por el que el kernel obtiene sus capacidades, y por eso el ``env`` es un
    parámetro: una prueba puede fijar el entorno sin tocar el del proceso.

    Args:
        env: Entorno a consultar. Si es ``None`` se usa ``os.environ``.

    Returns:
        Registro consultable con las capacidades declaradas.
    """
    return ProviderCapabilityRegistry(credential_state_from_environment(env))


def _credential_state(env: Mapping[str, str], provider: str) -> CredentialState:
    """Estado de credencial de un proveedor real según la presencia de su variable.

    Se consulta la veracidad de la variable —declarada y no vacía— y nada más. Una variable
    ausente o vacía deja al proveedor en ``PENDING_CREDENTIALS``: no es un fallo, es trabajo
    pendiente de configuración, y el workflow lo reporta como tal.
    """
    key = _CREDENTIAL_ENV[provider]
    if env.get(key):
        return CredentialState.PRESENT
    return CredentialState.PENDING_CREDENTIALS


def _usable(capability: ProviderCapability, role: RoleName) -> ProviderCapability:
    """Devuelve la capacidad si puede ejecutar el rol; si no, falla enumerando los motivos.

    Los motivos se acumulan en vez de informar solo del primero: saber que un proveedor no
    cubre el rol *y además* no tiene credencial evita una ida y vuelta de configuración.
    """
    reasons = _refusal_reasons(capability, role)
    if not reasons:
        return capability
    raise WorkflowProviderUnavailableError(
        f"el proveedor {capability.provider!r} no puede cubrir el rol {role.value}: "
        f"{'; '.join(reasons)}. PUNTO no sustituye el proveedor: no hay fallback"
    )


def _refusal_reasons(capability: ProviderCapability, role: RoleName) -> list[str]:
    """Motivos concretos por los que una capacidad declarada no puede ejecutar el rol."""
    reasons: list[str] = []
    if not capability.supports(role):
        reasons.append(f"no declara el rol {role.value}")
    if capability.credential_state is not CredentialState.PRESENT:
        reasons.append(f"sin credencial (credential_state={capability.credential_state.value})")
    if not capability.available:
        reasons.append("declarado no disponible (available=False)")
    return reasons


def _unavailable_detail(
    role: RoleName, considered: Sequence[ProviderCapability]
) -> str:
    """Detalle del fallo cuando ningún proveedor implícito puede ejecutar el rol.

    Distingue los tres casos que un humano necesita distinguir: nadie declara el rol, quien lo
    declara no puede usarlo (y por qué), o solo lo declara el doble de prueba.
    """
    if not considered:
        return (
            f"ningún proveedor declara cubrir el rol {role.value} y el rol no se ejecuta con un "
            f"proveedor distinto: el doble de prueba {PROVIDER_FAKE!r} solo se usa si se nombra "
            "explícitamente"
        )
    reasons = "; ".join(
        f"{capability.provider!r}: {', '.join(_refusal_reasons(capability, role))}"
        for capability in considered
    )
    return (
        f"ningún proveedor puede cubrir el rol {role.value} ahora mismo ({reasons}). "
        "PUNTO no sustituye el proveedor: no hay fallback"
    )


__all__ = [
    "ANTHROPIC_API_KEY_ENV",
    "DEEPSEEK_API_KEY_ENV",
    "OPENAI_API_KEY_ENV",
    "PROVIDER_FAKE",
    "PROVIDER_OPENAI",
    "ProviderCapabilityRegistry",
    "credential_state_from_environment",
    "default_capabilities",
]
