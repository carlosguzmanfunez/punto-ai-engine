"""Failover de proveedor por indisponibilidad **operativa** (PROVIDER FAILOVER v0).

Hasta esta fase el motor prohibía cambiar de proveedor: si el asignado a un rol fallaba, el
resultado era ``FAILED``/``UNAVAILABLE`` y una persona decidía. La regla existe por un motivo
bueno (un fallback silencioso convierte una auditoría cruzada en la respuesta del mismo modelo de
siempre y puede generar cargos), pero tenía un coste: si el proveedor **primario** de BUILDER se
queda sin créditos, la Task se detenía aunque otro proveedor conectado pudiera hacer el trabajo.

Este módulo abre exactamente una puerta, estrecha y explícita:

- **quién**: solo roles declarados en la configuración (``failover:`` de ``providers.yaml``), y solo
  entre los proveedores que esa configuración admite como sustitutos;
- **cuándo**: solo si el primario falló por una causa operativa **demostrable**: créditos o cuota,
  límite de tasa, proveedor no disponible o sin sesión/credencial. Un fallo del proyecto no llega
  aquí (ocurre después de una respuesta correcta), y una respuesta técnicamente incorrecta del
  proveedor (contenido inválido, negativa, timeout, error desconocido) **no** es indisponibilidad;
- **con quién**: un proveedor registrado, habilitado, ``CONNECTED`` y con las capacidades
  **efectivas** que el rol exige (configuradas ∩ transporte ∩ disponibilidad). Los proveedores de
  pago por uso solo entran si la configuración lo permite: el motor no genera cargos por su cuenta;
- **con qué autoridad**: ninguna adicional. El sustituto recibe **la misma** petición del mismo rol
  y su resultado sigue siendo inteligencia externa no confiable que pasa por las mismas
  validaciones. Failover cambia *quién responde*, jamás *qué está permitido*;
- **sin bucles**: cada proveedor se intenta como mucho una vez por petición y el número de
  sustitutos está acotado; el resultado nunca vuelve a disparar otro failover.

El primario sigue siendo la asignación configurada: el failover no la modifica y cada petición
empieza por él, de modo que en cuanto se recupera vuelve a ser el que responde.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from punto.providers.contract import ProviderErrorKind, ProviderRole

__all__ = [
    "DEFAULT_MAX_SUBSTITUTES",
    "OPERATIONAL_CAUSES",
    "FailoverCause",
    "FailoverPolicy",
    "SubstituteEvaluator",
    "SubstituteVerdict",
    "failover_cause_of",
]

#: Tope de sustitutos que se intentan por petición.
DEFAULT_MAX_SUBSTITUTES: Final[int] = 2


class FailoverCause(StrEnum):
    """Causa operativa que justifica intentar otro proveedor."""

    CREDITS_EXHAUSTED = "CREDITS_EXHAUSTED"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_DISCONNECTED = "PROVIDER_DISCONNECTED"


#: Fallos normalizados que **sí** demuestran indisponibilidad operativa. Todo lo que no esté aquí
#: (``INVALID_RESPONSE``, ``REFUSAL``, ``TIMEOUT``, ``NETWORK``, ``CONFIG``, ``PROCESS_FAILED``,
#: ``UNKNOWN``) no es una prueba de que el proveedor no pueda servir: cambiar de proveedor ante una
#: respuesta mala escondería el problema y una auditoría cruzada dejaría de serlo.
OPERATIONAL_CAUSES: Final[Mapping[ProviderErrorKind, FailoverCause]] = {
    ProviderErrorKind.QUOTA_EXHAUSTED: FailoverCause.CREDITS_EXHAUSTED,
    ProviderErrorKind.RATE_LIMIT: FailoverCause.RATE_LIMITED,
    ProviderErrorKind.UNAVAILABLE: FailoverCause.PROVIDER_UNAVAILABLE,
    ProviderErrorKind.AUTHENTICATION: FailoverCause.PROVIDER_DISCONNECTED,
}


def failover_cause_of(kind: ProviderErrorKind | None) -> FailoverCause | None:
    """Causa operativa de un fallo normalizado, o ``None`` si no justifica un failover."""
    if kind is None:
        return None
    return OPERATIONAL_CAUSES.get(kind)


@dataclass(frozen=True, slots=True)
class FailoverPolicy:
    """Política de failover: qué roles admiten sustituto y cuáles.

    ``roles`` mapea cada rol cubierto a sus sustitutos **en orden de preferencia**; una tupla vacía
    significa «cualquier proveedor registrado, en orden de registro». Es configuración de confianza
    (``providers.yaml``): nada que venga de una petición, de un proveedor o de una Task la modifica.
    """

    roles: Mapping[ProviderRole, tuple[str, ...]] = field(default_factory=dict)
    max_substitutes: int = DEFAULT_MAX_SUBSTITUTES
    #: ``False`` (por defecto): un sustituto de pago por uso (API con clave) no se usa sin decisión
    #: explícita, porque generaría cargos que nadie autorizó.
    allow_metered: bool = False

    def __post_init__(self) -> None:
        """Rechaza una política que permitiría un bucle o no permitiría nada."""
        if self.max_substitutes < 1:
            raise ValueError("max_substitutes debe ser al menos 1")

    def covers(self, role: ProviderRole) -> bool:
        """True si el rol admite failover."""
        return role in self.roles

    def preferred(self, role: ProviderRole) -> tuple[str, ...]:
        """Sustitutos declarados para el rol, en orden de preferencia (vacío = todos)."""
        return tuple(self.roles.get(role, ()))


@dataclass(frozen=True, slots=True)
class SubstituteVerdict:
    """Veredicto sobre un candidato a sustituto: ¿puede hacer el trabajo del rol ahora?"""

    eligible: bool
    reason: str = ""
    #: El transporte del candidato es de pago por uso (API con clave).
    metered: bool = False


#: Quien conoce el estado real de los proveedores (catálogo, sesión, transporte) juzga a un
#: candidato.
#: Recibe el rol, el proveedor y si la petición lleva imágenes. El router no conoce nada de eso.
SubstituteEvaluator = Callable[[ProviderRole, str, bool], SubstituteVerdict]
