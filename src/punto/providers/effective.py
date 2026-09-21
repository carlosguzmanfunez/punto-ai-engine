"""Capacidades **efectivas** de la ruta activa: lo que de verdad se puede ejecutar.

Hay tres cosas que no se pueden confundir, y la tercera es la única que puede sostener una
verificación:

- **configurada**: lo que el catálogo declara que un proveedor sabe hacer
  (``ProviderDescriptor.capabilities``) y lo que declara la tabla del kernel
  (``ProviderCapability.vision`` / ``structured_output``);
- **del transporte**: lo que acepta el camino realmente seleccionado
  (``TransportCapabilities``): una API con clave acepta imágenes; Claude Code en modo no
  interactivo, no;
- **efectiva**: la intersección de las dos anteriores con la disponibilidad real de la ruta.

Si el transporte activo no acepta imágenes, un criterio de apariencia **no se puede demostrar por
esa ruta**, por mucho que el modelo lo soporte teóricamente. Este módulo es la fuente canónica de
esa intersección: el ciclo, el QA y el dashboard la consultan en vez de suponerla cada uno por su
cuenta.

Fronteras: no concede autoridad, no elige proveedor por su cuenta y **no** sustituye un rol asignado
por otro (el motor prohíbe la sustitución implícita: un proveedor real ausente es un hueco que se
reporta). Cuando falta una capacidad, informa de las rutas autorizadas que sí podrían producirla,
con su transporte, para que la decisión sea de una persona.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from punto.acceptance import VisualCapability
from punto.providers.contract import ProviderRole
from punto.providers.transport import TransportCapabilities

__all__ = [
    "CAPABILITY_STRUCTURED_OUTPUT",
    "CAPABILITY_TEXT",
    "CAPABILITY_VISION",
    "EffectiveCapability",
    "capability_routes",
    "configured_capabilities",
    "effective_capabilities_table",
    "effective_capability",
    "effective_capability_for_role",
    "visual_capability_for_role",
]

#: Capacidades del catálogo que dependen del transporte. Se toman del vocabulario ya existente
#: (``punto.providers.registry.CAPABILITIES``) para no inventar una taxonomía paralela.
CAPABILITY_TEXT: Final[str] = "TEXT"
CAPABILITY_VISION: Final[str] = "VISION"
CAPABILITY_STRUCTURED_OUTPUT: Final[str] = "STRUCTURED_OUTPUT"

#: Qué atributo del transporte acredita cada capacidad del catálogo.
_TRANSPORT_REQUIREMENT: Final[Mapping[str, str]] = {
    CAPABILITY_VISION: "supports_images",
    CAPABILITY_STRUCTURED_OUTPUT: "supports_json_schema",
}


@dataclass(frozen=True, slots=True)
class EffectiveCapability:
    """Capacidad configurada y efectiva de **una** ruta (proveedor + modelo + transporte)."""

    provider: str
    model: str = ""
    transport: str = ""
    role: str = ""
    #: Lo que la configuración declara que el proveedor sabe hacer.
    configured: tuple[str, ...] = ()
    #: Lo que esa ruta puede ejecutar de verdad: configurada ∩ transporte ∩ disponibilidad.
    effective: tuple[str, ...] = ()
    #: Por qué una capacidad configurada no es efectiva (una entrada por hueco).
    reasons: tuple[str, ...] = ()
    #: True si la ruta está disponible (sesión o credencial acreditada por el catálogo).
    available: bool = False
    detail: str = ""

    def has(self, capability: str) -> bool:
        """True si la capacidad es **efectiva** en esta ruta."""
        return capability in self.effective

    @property
    def differs(self) -> bool:
        """True si hay capacidades configuradas que la ruta no puede ejecutar."""
        return bool(set(self.configured) - set(self.effective))

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable, con lo configurado y lo efectivo separados."""
        return {
            "provider": self.provider,
            "model": self.model,
            "transport": self.transport,
            "role": self.role,
            "configured": list(self.configured),
            "effective": list(self.effective),
            "unavailable": sorted(set(self.configured) - set(self.effective)),
            "differs": self.differs,
            "available": self.available,
            "reasons": list(self.reasons),
            "detail": self.detail,
        }


def configured_capabilities(provider: str, *, catalog: Any | None = None) -> tuple[str, ...]:
    """Capacidades **declaradas** de un proveedor, según el catálogo de configuración.

    Se prefiere el catálogo del dashboard (``ProviderRegistry``), que es la configuración viva; si
    no se puede leer —o el proveedor no está en él— se cae a la tabla declarativa del kernel
    (``workflow.providers``) en vez de suponer que no sabe hacer nada.
    """
    if catalog is not None:
        try:
            descriptor = catalog.descriptor(provider)
            return tuple(dict.fromkeys(str(item) for item in descriptor.capabilities))
        except Exception:  # catálogo no legible: se usa la tabla declarativa, no se adivina
            pass
    from punto.workflow.providers import default_capabilities

    declared = default_capabilities().get(provider)
    if declared is None:
        return ()
    capabilities = [CAPABILITY_TEXT]
    if declared.vision:
        capabilities.append(CAPABILITY_VISION)
    if declared.structured_output:
        capabilities.append(CAPABILITY_STRUCTURED_OUTPUT)
    return tuple(capabilities)


def _transport_of(provider: str, *, model: str = "", client: Any | None = None) -> Any | None:
    """Cliente del contrato del proveedor con su transporte activo, o ``None`` si no se pudo."""
    if client is not None:
        return client
    from punto.providers.transport_registry import transport_client

    try:
        return transport_client(provider, model=model)
    except Exception:  # sin transporte resoluble no hay capacidad efectiva que afirmar
        return None


def transport_capabilities(
    provider: str, *, model: str = "", client: Any | None = None
) -> TransportCapabilities | None:
    """Capacidades del transporte activo, o ``None`` si no se pudieron comprobar (fail closed)."""
    resolved = _transport_of(provider, model=model, client=client)
    if resolved is None:
        return None
    consulta = getattr(resolved, "capabilities", None)
    if not callable(consulta):
        return None
    try:
        declaradas = consulta()
    except Exception:
        return None
    # Un transporte que devuelva otra cosa no acredita nada: se falla cerrado.
    return declaradas if isinstance(declaradas, TransportCapabilities) else None


def effective_capability(
    provider: str,
    *,
    model: str = "",
    role: str = "",
    configured: Sequence[str] | None = None,
    catalog: Any | None = None,
    client: Any | None = None,
) -> EffectiveCapability:
    """Capacidad efectiva de una ruta: configurada ∩ transporte ∩ disponibilidad.

    Una capacidad configurada solo entra en ``effective`` si el transporte la acredita. Si el
    transporte no se puede comprobar, **ninguna** capacidad que dependa de él es efectiva (fail
    closed) y el motivo viaja en ``reasons``: nunca se afirma una capacidad que no se ha podido
    demostrar.
    """
    declaradas = (
        tuple(dict.fromkeys(str(item) for item in configured))
        if configured is not None
        else configured_capabilities(provider, catalog=catalog)
    )
    caps = transport_capabilities(provider, model=model, client=client)
    transporte = ""
    resolved = _transport_of(provider, model=model, client=client) if caps is not None else None
    if resolved is not None:
        interna = getattr(resolved, "transport", None)
        kind = getattr(interna, "kind", "")
        transporte = getattr(kind, "value", kind) or ""

    efectivas: list[str] = []
    motivos: list[str] = []
    detalle = ""
    for capacidad in declaradas:
        requisito = _TRANSPORT_REQUIREMENT.get(capacidad)
        if requisito is None:
            # Capacidad que no depende del transporte (TEXT, CODING, …): la ruta la tiene.
            efectivas.append(capacidad)
            continue
        if caps is None:
            motivos.append(
                f"no se pudo comprobar el transporte de {provider!r}: la capacidad {capacidad} no "
                "se considera disponible (fail closed)"
            )
            continue
        if bool(getattr(caps, requisito, False)):
            efectivas.append(capacidad)
            continue
        motivo = str(getattr(caps, "detail", "") or "").strip()
        motivos.append(
            f"{provider!r} declara {capacidad} y su transporte activo no la ejecuta"
            + (f": {motivo}" if motivo else "")
        )
        detalle = detalle or motivo

    return EffectiveCapability(
        provider=provider,
        model=model,
        transport=transporte,
        role=role,
        configured=declaradas,
        effective=tuple(efectivas),
        reasons=tuple(motivos),
        available=bool(efectivas),
        detail=detalle[:200],
    )


def effective_capability_for_role(
    role: ProviderRole | str,
    *,
    router: Any | None = None,
    catalog: Any | None = None,
    client: Any | None = None,
    configured: Sequence[str] | None = None,
) -> EffectiveCapability:
    """Capacidad efectiva de la ruta que el router asignaría a un rol.

    Si no se puede resolver la ruta (rol sin proveedor, transporte irresoluble), devuelve una
    capacidad efectiva **vacía** con el motivo: el llamante decide el estado gobernado.
    """
    rol = role.value if isinstance(role, ProviderRole) else str(role)
    try:
        resolved_router = router if router is not None else _default_router()
        provider = str(resolved_router.get_provider_for_role(_role_of(rol)))
        model = str(resolved_router.model_of(provider) or "")
    except Exception as exc:
        return EffectiveCapability(
            provider="",
            role=rol,
            configured=tuple(configured or ()),
            reasons=(f"no se pudo resolver la ruta de {rol}: {type(exc).__name__}",),
        )
    return effective_capability(
        provider, model=model, role=rol, configured=configured, catalog=catalog, client=client
    )


def _role_of(value: str) -> ProviderRole:
    """Traduce el nombre del rol al vocabulario del router."""
    return ProviderRole(value)


def _default_router() -> Any:
    """Router por defecto del motor (el mismo que usa la configuración declarada)."""
    from punto.providers.registry import ProviderRegistry

    return ProviderRegistry().router_instance()


def visual_capability_for_role(
    role: ProviderRole | str = ProviderRole.VISUAL_QA,
    *,
    router: Any | None = None,
    catalog: Any | None = None,
    client: Any | None = None,
    configured: Sequence[str] | None = None,
    routes: bool = True,
) -> VisualCapability:
    """Capacidad de evidencia **visual** de la ruta asignada, con lo configurado y lo efectivo.

    Es lo que consume el ciclo para decidir si un criterio de apariencia puede demostrarse con
    imágenes. Cuando la ruta no puede, el detalle dice qué falla y (si ``routes``) qué ruta
    autorizada sí podría, sin cambiar nada por su cuenta.
    """
    capacidad = effective_capability_for_role(
        role, router=router, catalog=catalog, client=client, configured=configured
    )
    disponible = capacidad.has(CAPABILITY_VISION)
    motivo = ""
    if not disponible:
        motivo = capacidad.reasons[0] if capacidad.reasons else "no hay ruta visual disponible"
        alternativas = capability_routes(CAPABILITY_VISION, catalog=catalog) if routes else ()
        if alternativas:
            motivo = f"{motivo}; rutas autorizadas que sí pueden: {', '.join(alternativas)}"
    return VisualCapability(
        available=disponible,
        detail=motivo[:200],
        provider=capacidad.provider,
        configured=CAPABILITY_VISION in capacidad.configured,
        transport=capacidad.transport,
        remedy=(
            ""
            if disponible
            else (
                "aporta una atestación humana explícita o usa una ruta con entrada de imágenes "
                "(transporte 'api')"
            )
        ),
    )


def capability_routes(capability: str, *, catalog: Any | None = None) -> tuple[str, ...]:
    """Rutas **declaradas** del catálogo cuyo transporte sí acredita esa capacidad.

    Es información para quien decide, no una reasignación: el motor prohíbe sustituir el proveedor
    de un rol de forma implícita, así que esto solo enumera alternativas autorizadas.
    """
    if catalog is None:
        try:
            from punto.providers.registry import ProviderRegistry

            catalog = ProviderRegistry()
        except Exception:
            return ()
    rutas: list[str] = []
    try:
        descriptors = tuple(catalog.descriptors())
    except Exception:
        return ()
    for descriptor in descriptors:
        declaradas = tuple(str(item) for item in getattr(descriptor, "capabilities", ()))
        if capability not in declaradas:
            continue
        provider = str(descriptor.provider)
        transporte = str(getattr(descriptor, "transport", "") or "")
        if provider == "fake":  # el doble de prueba no produce evidencia real
            continue
        capacidad = effective_capability(
            provider,
            model=str(getattr(descriptor, "model", "") or ""),
            configured=declaradas,
            catalog=catalog,
        )
        if capacidad.has(capability):
            rutas.append(f"{provider} ({transporte or capacidad.transport})")
    return tuple(rutas)


def effective_capabilities_table(
    *,
    roles: Mapping[str, str] | Iterable[str] | None = None,
    catalog: Any | None = None,
) -> tuple[dict[str, Any], ...]:
    """Tabla de capacidades por proveedor y por rol asignado, para el dashboard/API.

    Cada fila lleva lo **configurado** y lo **efectivo** por separado: si el transporte activo no
    ejecuta algo que la configuración declara, la interfaz puede decirlo en vez de ofrecerlo como
    utilizable. La disponibilidad se lee del catálogo real (sesión o credencial), sin comprobar red.
    """
    if catalog is None:
        try:
            from punto.providers.registry import ProviderRegistry

            catalog = ProviderRegistry()
        except Exception:
            return ()
    if roles is None:
        try:
            asignacion: Mapping[str, str] = catalog.roles()
        except Exception:
            asignacion = {}
    elif isinstance(roles, Mapping):
        asignacion = roles
    else:
        asignacion = {str(item): "" for item in roles}

    filas: list[dict[str, Any]] = []
    for descriptor in tuple(catalog.descriptors()):
        provider = str(descriptor.provider)
        declaradas = tuple(str(item) for item in getattr(descriptor, "capabilities", ()))
        modelo = str(getattr(descriptor, "model", "") or "")
        capacidad = effective_capability(
            provider, model=modelo, configured=declaradas, catalog=catalog
        )
        roles_del_proveedor = sorted(
            role for role, asignado in asignacion.items() if asignado == provider
        )
        fila = capacidad.as_dict()
        fila["roles"] = roles_del_proveedor
        fila["faltan_para_roles"] = [
            {
                "role": role,
                "required": _required_capability(role),
                "available": capacidad.has(_required_capability(role))
                if _required_capability(role)
                else True,
            }
            for role in roles_del_proveedor
        ]
        filas.append(fila)
    return tuple(filas)


def _required_capability(role: str) -> str:
    """Capacidad que exige un rol de orquestación, según el catálogo (vacío si no exige ninguna)."""
    from punto.providers.registry import ROLE_REQUIRED_CAPABILITY

    try:
        return str(ROLE_REQUIRED_CAPABILITY.get(ProviderRole(role), "") or "")
    except ValueError:
        return ""
