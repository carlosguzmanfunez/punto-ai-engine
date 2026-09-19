"""Coherencia entre la asignación de roles configurada y la capacidad declarada (F-2, PILOT-03R).

El fallo que cierra esta suite: durante PILOT-03 la configuración asignaba ``ARCHITECT`` a
``openai`` y el ``ProviderRouter`` lo ejecutó de verdad, mientras la tabla declarativa de
capacidades declaraba que ``openai`` no cubría **ningún** rol. Dos vistas de la misma verdad
contradiciéndose en silencio.

Estas pruebas fijan la invariante en las dos direcciones:

- con la configuración real del repositorio, la declaración **cubre** cada rol asignado;
- con una asignación divergente inventada, el guardián **detecta** el hueco (la comprobación no
  pasa por vacía).

Y fijan el límite que impide que la declaración se convierta en autoridad: declarar un rol no
asigna nada, no salta el ``ProviderRouter``, no inventa credenciales y no autoriza efectos.
"""

from __future__ import annotations

from collections.abc import Mapping

from punto.providers.contract import ProviderRole
from punto.providers.registry import ProviderRegistry
from punto.schemas.workflow import CredentialState, RoleName
from punto.workflow.providers import (
    ORCHESTRATION_ROLE_NAMES,
    PROVIDER_OPENAI,
    ProviderCapabilityRegistry,
    capability_consistency_gaps,
    credential_state_from_environment,
    default_capabilities,
    workflow_role_of,
)

#: Los tres roles de orquestación que la configuración puede asignar.
ORCHESTRATION_ROLES: tuple[ProviderRole, ...] = (
    ProviderRole.ARCHITECT,
    ProviderRole.BUILDER,
    ProviderRole.VISUAL_QA,
)


def _effective_assignment() -> dict[str, str]:
    """Asignación **efectiva** rol → proveedor, tal como la resuelve el router en runtime.

    Se lee del catálogo real (``config/providers.yaml`` más la configuración local) y no de una
    copia escrita en la prueba: si la prueba repitiera la tabla, no comprobaría nada.
    """
    return dict(ProviderRegistry().router_instance().assignment())


# ---------------------------------------------------------------------------
# La configuración real es coherente con la declaración
# ---------------------------------------------------------------------------
def test_la_asignacion_configurada_esta_cubierta_por_la_declaracion() -> None:
    """Cada rol que la configuración asigna lo declara el proveedor que lo atiende."""
    assignment = _effective_assignment()

    assert assignment, "la configuración no asigna ningún rol: la comprobación sería vacía"
    assert capability_consistency_gaps(assignment, default_capabilities({})) == ()


def test_la_declaracion_cubre_los_tres_roles_de_orquestacion() -> None:
    """La tabla declara, para cada rol asignable, el proveedor configurado."""
    assignment = _effective_assignment()
    declared = default_capabilities({})

    for role in ORCHESTRATION_ROLES:
        provider = assignment[role.value]
        capability = declared.get(provider)
        assert capability is not None, provider
        workflow_role = workflow_role_of(role)
        assert workflow_role is not None
        assert capability.supports(workflow_role), (role.value, provider)


def test_el_rol_declarado_de_openai_es_el_de_la_configuracion() -> None:
    """El caso concreto del hallazgo: ``ARCHITECT`` → ``openai`` está declarado."""
    assignment = _effective_assignment()

    assert assignment[ProviderRole.ARCHITECT.value] == PROVIDER_OPENAI
    capability = default_capabilities({}).get(PROVIDER_OPENAI)

    assert capability is not None
    assert capability.supports(RoleName.ARCHITECT)
    assert capability.live_verified is False, (
        "la tabla declara capacidades desde la configuración; que una ejecución real haya "
        "funcionado se acredita en la evidencia de la fase, no aquí"
    )


# ---------------------------------------------------------------------------
# El guardián detecta la divergencia (no pasa por vacía)
# ---------------------------------------------------------------------------
def test_el_guardian_detecta_un_rol_asignado_a_quien_no_lo_declara() -> None:
    """Si la configuración moviera ARCHITECT a un proveedor sin ese rol, el guardián lo dice."""
    divergente: Mapping[str, str] = {
        "ARCHITECT": "anthropic",
        "BUILDER": "deepseek",
        "VISUAL_QA": "anthropic",
    }

    gaps = capability_consistency_gaps(divergente, default_capabilities({}))

    assert len(gaps) == 1
    assert "ARCHITECT" in gaps[0]
    assert "anthropic" in gaps[0]
    assert RoleName.ARCHITECT.value in gaps[0]


def test_el_guardian_detecta_un_proveedor_no_declarado() -> None:
    """Asignar un rol a un proveedor que no está en la tabla tampoco se acepta."""
    inventado: Mapping[str, str] = {"ARCHITECT": "gemini-pro"}

    gaps = capability_consistency_gaps(inventado, default_capabilities({}))

    assert len(gaps) == 1
    assert "gemini-pro" in gaps[0]
    assert "no está declarado" in gaps[0]


def test_el_guardian_detecta_un_rol_de_orquestacion_desconocido() -> None:
    """Un rol que PUNTO no sabe traducir se reporta en vez de suponerse equivalente."""
    gaps = capability_consistency_gaps({"SUPERVISOR": "openai"}, default_capabilities({}))

    assert len(gaps) == 1
    assert "SUPERVISOR" in gaps[0]
    assert workflow_role_of(ProviderRole.ARCHITECT) is RoleName.ARCHITECT
    assert ORCHESTRATION_ROLE_NAMES.get("SUPERVISOR") is None


# ---------------------------------------------------------------------------
# La declaración describe; no decide ni autoriza
# ---------------------------------------------------------------------------
def test_declarar_un_rol_no_asigna_el_rol() -> None:
    """La declaración no elige proveedor: quien resuelve el rol es el router."""
    assignment = _effective_assignment()
    declared = default_capabilities({})

    assert capability_consistency_gaps(assignment, declared) == ()
    # ``openai`` declara ARCHITECT y ``deepseek`` también declara roles de ingeniería; lo que decide
    # a quién se invoca es la asignación del router, que la declaración no toca.
    assert declared.get("deepseek") is not None
    assert declared.get("deepseek").supports(RoleName.ARCHITECT)  # type: ignore[union-attr]


def test_declarar_un_rol_no_inventa_credenciales() -> None:
    """Sin clave de API declarada, el proveedor sigue sin disponibilidad: nada se inventa."""
    capability = default_capabilities({}).get(PROVIDER_OPENAI)

    assert capability is not None
    assert capability.available is False
    assert capability.credential_state is CredentialState.PENDING_CREDENTIALS


def test_una_capability_no_autoriza_efectos_por_si_sola() -> None:
    """El registro declara capacidad; declarar un rol no concede permisos.

    Se comprueba que un registro con una capacidad que declara roles y está disponible sigue siendo
    **solo** un registro: su API es consultar capacidad y exigirla, y no expone ninguna operación de
    autorización, de efecto ni de política. La autoridad sigue donde estaba: en el motor.
    """
    completa = ProviderCapabilityRegistry(
        credential_state_from_environment({"DEEPSEEK_API_KEY": "x"})
    )
    superficie = {name for name in dir(completa) if not name.startswith("_")}

    assert {"capabilities", "for_role", "get", "require"} <= superficie
    for prohibido in (
        "authorize",
        "grant",
        "execute",
        "apply",
        "set_policy",
        "evaluate",
        "allow",
        "skip_human_gate",
        "expand_resources",
    ):
        assert prohibido not in superficie
