"""Endpoints y página del dashboard de configuración de proveedores (PROVIDER DASHBOARD v0).

Se registran **sobre la aplicación FastAPI que ya existe** (`punto.api.app`): no hay un segundo
backend. La página se sirve desde el mismo origen que la API, así que el navegador no necesita CORS
y la sesión de QA puede hacer el recorrido completo (navegador → dashboard → backend → configuración
de proveedores → respuesta → DOM).

Lo que esta capa **no** hace: no inventa estado, no devuelve secretos y no concede autoridad.
Configurar un proveedor no habilita ninguna acción del motor; el Human Gate sigue donde estaba.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from punto.providers.contract import ProviderRole
from punto.providers.effective import capability_limitations, effective_capabilities_table
from punto.providers.registry import (
    CAPABILITIES,
    InvalidProviderError,
    ProviderRegistry,
    UnknownProviderError,
)
from punto.providers.secrets import SecretStore, SecretStoreError
from punto.providers.transport import TransportUsageStatus

#: Ruta donde vive la página del dashboard.
DASHBOARD_PATH: Final[str] = "/dashboard"

#: Ruta de la página HTML dentro del paquete.
DASHBOARD_HTML: Final[Path] = Path(__file__).resolve().parent / "static" / "dashboard.html"


class TransportChangeRequest(BaseModel):
    """Cambio de transporte de un proveedor."""

    transport: str = Field(min_length=1)
    auth_mode: str = ""


class ModelChangeRequest(BaseModel):
    """Cambio de modelo de un proveedor."""

    model: str = Field(min_length=1)


class ApiKeyRequest(BaseModel):
    """Clave de API de un proveedor.

    Se recibe por el cuerpo de una petición POST y **nunca** se devuelve: la respuesta solo dice si
    quedó configurada. El valor no se registra en ningún log ni viaja a ninguna respuesta.
    """

    api_key: str = Field(min_length=8)


class RoleAssignmentRequest(BaseModel):
    """Asignación de un rol a un proveedor."""

    provider: str = Field(min_length=1)


class CustomProviderRequest(BaseModel):
    """Alta de un proveedor nuevo (genérico, no específico de ningún fabricante)."""

    provider_id: str = Field(min_length=2, max_length=40)
    display_name: str = Field(min_length=1)
    base_url: str = ""
    model: str = Field(min_length=1)
    adapter_type: str = "openai_compatible"
    capabilities: tuple[str, ...] = ("TEXT",)
    api_key: str = ""
    enabled: bool = True


def provider_registry() -> ProviderRegistry:
    """Registro de proveedores del dashboard, con el almacén de secretos por defecto."""
    return ProviderRegistry(secrets=SecretStore())


def register_dashboard(application: FastAPI, registry: ProviderRegistry | None = None) -> None:
    """Registra la página y los endpoints del dashboard en la aplicación existente.

    Args:
        application: Aplicación FastAPI del motor.
        registry: Registro inyectable (pruebas). Por defecto, uno con la configuración real.
    """
    catalog = registry if registry is not None else provider_registry()
    if getattr(application.state, "provider_registry", None) is not None:
        return
    application.state.provider_registry = catalog

    @application.get(DASHBOARD_PATH, response_class=HTMLResponse, tags=["dashboard"])
    def dashboard_page() -> HTMLResponse:
        """Página de configuración de proveedores (Servicios IA)."""
        return HTMLResponse(_page_text())

    @application.get("/providers", tags=["dashboard"], summary="Catálogo de proveedores")
    def list_providers() -> dict[str, Any]:
        """Proveedores con su configuración y su estado real, sin credenciales.

        AP000-OBS-03-R1: además de lo declarado, se expone la capacidad **efectiva** de cada ruta
        (configurada ∩ transporte activo ∩ disponibilidad). Si el transporte no ejecuta algo que la
        configuración declara —por ejemplo, imágenes por Claude Code—, la interfaz puede decirlo en
        vez de ofrecerlo como utilizable.
        """
        providers = list(catalog.status_table())
        efectivas = [dict(item) for item in effective_capabilities_table(catalog=catalog)]
        return {
            "providers": providers,
            "capabilities": list(CAPABILITIES),
            "roles": catalog.roles(),
            "effective_capabilities": efectivas,
            # Fase 13: resumen compacto «⚠ N capacidades limitadas». Una capacidad configurada y
            # no efectiva en el transporte actual NO es desconexión: el estado sigue siendo el real.
            "capability_limitations": capability_limitations(efectivas, providers),
        }

    @application.get("/providers/{provider}", tags=["dashboard"], summary="Ficha de un proveedor")
    def get_provider(provider: str) -> dict[str, Any]:
        """Ficha de un proveedor: configuración, estado y avisos de capacidad."""
        descriptor = _descriptor_or_404(catalog, provider)
        warnings = {
            role.value: list(catalog.role_warnings(role, descriptor.provider))
            for role in ProviderRole
        }
        return {
            **descriptor.as_dict(),
            "auth": catalog.auth_status(descriptor.provider),
            "status": catalog.offline_status(descriptor),
            "usage": catalog.usage_status(descriptor.provider),
            "role_warnings": warnings,
        }

    @application.post("/providers/{provider}/test", tags=["dashboard"], summary="Probar conexión")
    def test_provider(provider: str) -> dict[str, Any]:
        """Comprueba la conexión con la sonda real y más barata del proveedor."""
        _descriptor_or_404(catalog, provider)
        return catalog.test_connection(provider).as_dict()

    @application.post("/providers/{provider}/transport", tags=["dashboard"])
    def set_transport(provider: str, body: TransportChangeRequest) -> dict[str, Any]:
        """Selecciona el transporte de un proveedor (configuración, no autoridad)."""
        _descriptor_or_404(catalog, provider)
        try:
            descriptor = catalog.set_transport(provider, body.transport, auth_mode=body.auth_mode)
        except InvalidProviderError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
        return {
            **descriptor.as_dict(),
            "auth": catalog.auth_status(descriptor.provider),
            "status": catalog.offline_status(descriptor),
        }

    @application.post("/providers/{provider}/model", tags=["dashboard"])
    def set_model(provider: str, body: ModelChangeRequest) -> dict[str, Any]:
        """Configura el modelo de un proveedor."""
        _descriptor_or_404(catalog, provider)
        try:
            descriptor = catalog.set_model(provider, body.model)
        except InvalidProviderError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
        return {**descriptor.as_dict(), "status": catalog.offline_status(descriptor)}

    @application.post("/providers/{provider}/api-key", tags=["dashboard"])
    def set_api_key(provider: str, body: ApiKeyRequest) -> dict[str, Any]:
        """Guarda la clave de API **sin devolverla**: solo ``api_key_configured``."""
        _descriptor_or_404(catalog, provider)
        try:
            descriptor = catalog.set_api_key(provider, body.api_key)
        except SecretStoreError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
        return {
            "provider": descriptor.provider,
            "api_key_configured": descriptor.api_key_configured,
            "status": catalog.offline_status(descriptor),
        }

    @application.delete("/providers/{provider}/api-key", tags=["dashboard"])
    def delete_api_key(provider: str) -> dict[str, Any]:
        """Borra la clave de API de un proveedor."""
        _descriptor_or_404(catalog, provider)
        descriptor = catalog.clear_api_key(provider)
        return {
            "provider": descriptor.provider,
            "api_key_configured": descriptor.api_key_configured,
        }

    @application.post("/providers/{provider}/connect", tags=["dashboard"])
    def connect_provider(provider: str) -> dict[str, Any]:
        """Guía de conexión: comando oficial y estado actual. PUNTO no pide credenciales."""
        descriptor = _descriptor_or_404(catalog, provider)
        info = catalog.connect_info(descriptor.provider)
        return {**info.as_dict(), "status": catalog.offline_status(descriptor)}

    @application.post("/providers/custom", tags=["dashboard"], status_code=201)
    def create_custom_provider(body: CustomProviderRequest) -> dict[str, Any]:
        """Da de alta un proveedor nuevo con el contrato extensible del catálogo."""
        from punto.providers.registry import CustomProviderSpec

        try:
            descriptor = catalog.register_custom(
                CustomProviderSpec(
                    provider_id=body.provider_id,
                    display_name=body.display_name,
                    base_url=body.base_url,
                    model=body.model,
                    adapter_type=body.adapter_type,
                    capabilities=tuple(body.capabilities),
                    api_key=body.api_key,
                    enabled=body.enabled,
                )
            )
        except InvalidProviderError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
        except SecretStoreError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
        return {**descriptor.as_dict(), "status": catalog.offline_status(descriptor)}

    @application.get("/roles", tags=["dashboard"], summary="Asignación de roles")
    def list_roles() -> dict[str, Any]:
        """Asignación vigente, proveedores disponibles y **capacidad efectiva** de cada rol.

        Un rol puede estar asignado a un proveedor que declara la capacidad que el rol necesita y
        cuyo transporte activo, en cambio, no puede ejecutarla. Aquí se dice cuál es el caso, con el
        motivo real, en vez de dejar que la interfaz suponga que la asignación es suficiente.
        """
        efectivas = {
            item["provider"]: item for item in effective_capabilities_table(catalog=catalog)
        }
        roles = catalog.roles()
        return {
            "roles": roles,
            "required_capability": {
                role.value: capability
                for role, capability in _required_capabilities().items()
            },
            "role_capabilities": {
                role: _role_capability_view(role, provider, efectivas)
                for role, provider in roles.items()
            },
            "providers": [descriptor.provider for descriptor in catalog.descriptors()],
        }

    @application.post("/roles/{role}", tags=["dashboard"])
    def assign_role(role: str, body: RoleAssignmentRequest) -> dict[str, Any]:
        """Asigna un rol a un proveedor usando el ``ProviderRouter`` real."""
        try:
            resolved = ProviderRole(role.strip().upper())
        except ValueError as error:
            known = ", ".join(item.value for item in ProviderRole)
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=f"rol desconocido {role!r}; conocidos: {known}"
            ) from error
        _descriptor_or_404(catalog, body.provider)
        try:
            assignment = catalog.assign_role(resolved, body.provider)
        except InvalidProviderError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
        return {
            "roles": assignment,
            "advisories": list(catalog.role_warnings(resolved, body.provider)),
            "authority": "la asignación es configuración: no concede capacidades al motor",
        }

    @application.get("/providers-capabilities", tags=["dashboard"])
    def capabilities() -> JSONResponse:
        """Vocabulario de capacidades y tipos de adaptador admitidos."""
        from punto.providers.registry import ADAPTER_TYPES

        return JSONResponse(
            {
                "capabilities": list(CAPABILITIES),
                "adapter_types": list(ADAPTER_TYPES),
                "usage_states": [item.value for item in TransportUsageStatus],
            }
        )


def _role_capability_view(
    role: str, provider: str, efectivas: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Si la capacidad que exige un rol es efectiva en la ruta que lo atiende, y por qué no.

    ``required`` vacío significa que el rol no exige ninguna capacidad concreta del catálogo; en ese
    caso ``available`` es ``True`` y no hay nada que advertir.
    """
    required = _required_capability_of(role)
    fila = efectivas.get(provider) or {}
    if not required:
        return {"provider": provider, "required": "", "available": True, "detail": ""}
    disponible = required in tuple(fila.get("effective", ()))
    detalle = ""
    if not disponible:
        motivos = tuple(str(item) for item in fila.get("reasons", ()))
        detalle = motivos[0] if motivos else "la capacidad no se pudo comprobar en esa ruta"
    return {
        "provider": provider,
        "required": required,
        "available": disponible,
        "transport": str(fila.get("transport", "")),
        "configured": required in tuple(fila.get("configured", ())),
        "detail": detalle[:300],
    }


def _required_capability_of(role: str) -> str:
    """Capacidad que exige un rol, según el catálogo de roles del motor."""
    try:
        return str(_required_capabilities().get(ProviderRole(role), "") or "")
    except ValueError:
        return ""


def _required_capabilities() -> dict[ProviderRole, str]:
    """Capacidad que necesita cada rol, para la sección de asignación."""
    from punto.providers.registry import ROLE_REQUIRED_CAPABILITY

    return dict(ROLE_REQUIRED_CAPABILITY)


def _descriptor_or_404(catalog: ProviderRegistry, provider: str) -> Any:
    """Ficha del proveedor, o 404 si no está en el catálogo."""
    try:
        return catalog.descriptor(provider)
    except UnknownProviderError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error


def _page_text() -> str:
    """HTML de la página del dashboard.

    Es un único documento autocontenido (CSS y JS incluidos) servido desde el mismo origen que la
    API: sin dependencias de frontend, sin plantillas y sin build.
    """
    if not DASHBOARD_HTML.is_file():
        return "<!doctype html><html><body><p>dashboard no disponible</p></body></html>"
    return DASHBOARD_HTML.read_text(encoding="utf-8")


__all__ = ["DASHBOARD_PATH", "provider_registry", "register_dashboard"]
