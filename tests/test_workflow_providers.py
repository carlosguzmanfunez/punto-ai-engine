"""Pruebas del registro de capacidades de proveedor del workflow (ENGINE-6.0).

Estas pruebas fijan las dos reglas que sostienen la neutralidad de proveedor: **no hay
fallback** —pedir un rol que un proveedor no cubre falla, aunque otro proveedor esté
disponible— y **la credencial se mira, no se lee**, comprobado con un valor canario que no
puede aparecer en ninguna capacidad serializada ni en ningún detalle de error.

Ningún caso consulta el entorno real: todas las capacidades se construyen con un ``env``
explícito y sintético. La única excepción es la prueba del camino por defecto (``env=None``),
que parchea ``os.environ`` con ``monkeypatch`` y lo restaura al terminar.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from punto.providers.base import PROVIDER_ANTHROPIC, PROVIDER_DEEPSEEK
from punto.schemas.workflow import (
    CredentialState,
    ProviderCapability,
    RoleName,
    WorkflowFailureCode,
)
from punto.workflow.errors import WorkflowProviderUnavailableError
from punto.workflow.providers import (
    ANTHROPIC_API_KEY_ENV,
    DEEPSEEK_API_KEY_ENV,
    OPENAI_API_KEY_ENV,
    PROVIDER_FAKE,
    PROVIDER_OPENAI,
    ProviderCapabilityRegistry,
    credential_state_from_environment,
    default_capabilities,
)

#: Valor sintético de credencial. No es una credencial real y solo sirve para comprobar que su
#: contenido **no** se filtra a ninguna capacidad ni a ningún mensaje de error.
CANARIO = "sk-canario-sintetico-0123456789abcdef"

_PRESENTE = CredentialState.PRESENT
_PENDIENTE = CredentialState.PENDING_CREDENTIALS


# ---------------------------------------------------------------------------
# Constructores de apoyo
# ---------------------------------------------------------------------------
def _registro(**claves: str) -> ProviderCapabilityRegistry:
    """Registro construido con un entorno sintético explícito, nunca con el real."""
    return default_capabilities(dict(claves))


def _capacidad_falsa(
    *,
    provider: str = PROVIDER_DEEPSEEK,
    rol: RoleName = RoleName.ARCHITECT,
    available: bool = True,
    credential_state: CredentialState = _PRESENTE,
) -> ProviderCapability:
    """Capacidad declarada a mano, para aislar cada condición de ``require``."""
    return ProviderCapability(
        provider=provider,
        role_support=(rol,),
        available=available,
        credential_state=credential_state,
    )


# ---------------------------------------------------------------------------
# require: camino feliz
# ---------------------------------------------------------------------------
def test_require_con_credencial_presente_devuelve_la_capacidad() -> None:
    """Con la variable declarada, el rol se resuelve al proveedor que lo cubre."""
    registro = _registro(DEEPSEEK_API_KEY=CANARIO)

    capacidad = registro.require(RoleName.ARCHITECT)

    assert capacidad.provider == PROVIDER_DEEPSEEK
    assert capacidad.available is True
    assert capacidad.credential_state is _PRESENTE
    assert capacidad.supports(RoleName.ARCHITECT)


def test_require_acepta_el_proveedor_explicito_cuando_cubre_el_rol() -> None:
    """Nombrar el proveedor no cambia la respuesta si de verdad cubre el rol."""
    registro = _registro(DEEPSEEK_API_KEY=CANARIO)

    capacidad = registro.require(RoleName.REVIEWER, PROVIDER_DEEPSEEK)

    assert capacidad.provider == PROVIDER_DEEPSEEK
    assert capacidad.supports(RoleName.REVIEWER)


def test_require_resuelve_los_roles_visuales_con_anthropic() -> None:
    """Los roles de auditoría y visión los cubre Anthropic, no DeepSeek."""
    registro = _registro(ANTHROPIC_API_KEY=CANARIO)

    for rol in (RoleName.CROSS_AUDIT, RoleName.VISUAL_QA):
        capacidad = registro.require(rol)
        assert capacidad.provider == PROVIDER_ANTHROPIC
        assert capacidad.vision is True


# ---------------------------------------------------------------------------
# require: sin credencial y sin disponibilidad
# ---------------------------------------------------------------------------
def test_require_sin_credencial_lanza_con_el_codigo_correcto() -> None:
    """Sin credencial el rol no se ejecuta: error explícito con el código del kernel."""
    with pytest.raises(WorkflowProviderUnavailableError) as excinfo:
        _registro().require(RoleName.ARCHITECT)

    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    assert str(excinfo.value).startswith("WORKFLOW_PROVIDER_UNAVAILABLE: ")
    assert "ARCHITECT" in excinfo.value.detail
    assert PROVIDER_DEEPSEEK in excinfo.value.detail
    assert "sin credencial" in excinfo.value.detail
    assert _PENDIENTE.value in excinfo.value.detail
    assert "no hay fallback" in excinfo.value.detail


def test_require_distingue_sin_credencial_de_declarado_no_disponible() -> None:
    """Los dos motivos se dicen con palabras distintas y no se confunden entre sí."""
    sin_credencial = ProviderCapabilityRegistry((_capacidad_falsa(credential_state=_PENDIENTE),))

    with pytest.raises(WorkflowProviderUnavailableError) as pendiente:
        sin_credencial.require(RoleName.ARCHITECT, PROVIDER_DEEPSEEK)

    assert "sin credencial" in pendiente.value.detail
    assert "declarado no disponible" not in pendiente.value.detail

    no_disponible = ProviderCapabilityRegistry(
        (_capacidad_falsa(available=False, credential_state=_PRESENTE),)
    )

    with pytest.raises(WorkflowProviderUnavailableError) as caido:
        no_disponible.require(RoleName.ARCHITECT, PROVIDER_DEEPSEEK)

    assert "declarado no disponible" in caido.value.detail
    assert "sin credencial" not in caido.value.detail


def test_require_trata_absent_como_sin_credencial() -> None:
    """``ABSENT`` tampoco es una credencial utilizable: se reporta como «sin credencial»."""
    registro = ProviderCapabilityRegistry(
        (_capacidad_falsa(credential_state=CredentialState.ABSENT),)
    )

    with pytest.raises(WorkflowProviderUnavailableError) as excinfo:
        registro.require(RoleName.ARCHITECT)

    assert "sin credencial" in excinfo.value.detail
    assert CredentialState.ABSENT.value in excinfo.value.detail


# ---------------------------------------------------------------------------
# require: no hay fallback
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("env", "role"),
    (
        ({ANTHROPIC_API_KEY_ENV: CANARIO}, RoleName.ARCHITECT),
        ({DEEPSEEK_API_KEY_ENV: CANARIO}, RoleName.VISUAL_QA),
        ({DEEPSEEK_API_KEY_ENV: CANARIO}, RoleName.CROSS_AUDIT),
    ),
)
def test_require_no_sustituye_el_proveedor_que_falta(
    env: Mapping[str, str], role: RoleName
) -> None:
    """Que otro proveedor esté disponible no autoriza a usarlo para este rol."""
    registro = default_capabilities(dict(env))

    with pytest.raises(WorkflowProviderUnavailableError) as excinfo:
        registro.require(role)

    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    assert role.value in excinfo.value.detail
    assert "no hay fallback" in excinfo.value.detail


def test_require_falla_si_el_proveedor_pedido_no_cubre_el_rol() -> None:
    """Con los dos proveedores disponibles, pedir el que no cubre el rol sigue fallando."""
    registro = _registro(DEEPSEEK_API_KEY=CANARIO, ANTHROPIC_API_KEY=CANARIO)

    with pytest.raises(WorkflowProviderUnavailableError) as excinfo:
        registro.require(RoleName.VISUAL_QA, PROVIDER_DEEPSEEK)

    assert "no declara el rol VISUAL_QA" in excinfo.value.detail
    assert PROVIDER_DEEPSEEK in excinfo.value.detail
    assert "no hay fallback" in excinfo.value.detail


def test_require_falla_si_el_proveedor_no_esta_declarado() -> None:
    """Un nombre que no está en la tabla no es un proveedor nuevo: es un error."""
    with pytest.raises(WorkflowProviderUnavailableError) as excinfo:
        _registro().require(RoleName.ARCHITECT, "gemini")

    assert "no está declarado" in excinfo.value.detail
    assert RoleName.ARCHITECT.value in excinfo.value.detail


def test_require_falla_con_openai_porque_no_declara_ningun_rol() -> None:
    """OpenAI está declarado con ``role_support=()``: no cubre ningún rol y no se usa hoy."""
    registro = _registro(OPENAI_API_KEY=CANARIO)

    with pytest.raises(WorkflowProviderUnavailableError) as excinfo:
        registro.require(RoleName.ARCHITECT, PROVIDER_OPENAI)

    assert "no declara el rol ARCHITECT" in excinfo.value.detail
    assert PROVIDER_OPENAI in excinfo.value.detail


def test_require_no_elige_el_doble_de_prueba_de_forma_implicita() -> None:
    """``fake`` cubre todos los roles, pero solo se usa si el llamante lo nombra."""
    solo_falso = ProviderCapabilityRegistry(
        (_capacidad_falsa(provider=PROVIDER_FAKE, rol=RoleName.ARCHITECT),)
    )

    with pytest.raises(WorkflowProviderUnavailableError) as excinfo:
        solo_falso.require(RoleName.ARCHITECT)

    assert PROVIDER_FAKE in excinfo.value.detail
    assert "explícitamente" in excinfo.value.detail
    assert solo_falso.require(RoleName.ARCHITECT, PROVIDER_FAKE).provider == PROVIDER_FAKE


def test_require_explicito_del_doble_de_prueba_es_la_via_para_probar_el_kernel() -> None:
    """El doble de prueba se puede pedir por nombre y está disponible sin credenciales."""
    registro = _registro()

    for rol in (RoleName.ARCHITECT, RoleName.VISUAL_QA):
        capacidad = registro.require(rol, PROVIDER_FAKE)
        assert capacidad.provider == PROVIDER_FAKE
        assert capacidad.available is True
        assert capacidad.credential_state is _PRESENTE
        assert capacidad.supports(rol)


# ---------------------------------------------------------------------------
# consulta de la tabla
# ---------------------------------------------------------------------------
def test_for_role_solo_lista_los_que_declaran_el_rol() -> None:
    """``for_role`` no filtra por disponibilidad: informa, no decide."""
    registro = _registro()

    ingenieria = registro.for_role(RoleName.ARCHITECT)
    visuales = registro.for_role(RoleName.CROSS_AUDIT)

    assert tuple(capacidad.provider for capacidad in ingenieria) == (
        PROVIDER_DEEPSEEK,
        PROVIDER_FAKE,
    )
    assert tuple(capacidad.provider for capacidad in visuales) == (
        PROVIDER_ANTHROPIC,
        PROVIDER_FAKE,
    )
    assert all(capacidad.supports(RoleName.ARCHITECT) for capacidad in ingenieria)
    assert PROVIDER_OPENAI not in {capacidad.provider for capacidad in ingenieria}


def test_capabilities_conserva_el_orden_de_declaracion() -> None:
    """El orden es determinista y es el que decide un ``require`` sin proveedor."""
    registro = _registro()

    assert tuple(capacidad.provider for capacidad in registro.capabilities) == (
        PROVIDER_DEEPSEEK,
        PROVIDER_ANTHROPIC,
        PROVIDER_OPENAI,
        PROVIDER_FAKE,
    )


def test_get_devuelve_la_capacidad_declarada_o_none() -> None:
    """``get`` es una consulta, no una comprobación: no lanza por un nombre desconocido."""
    registro = _registro()

    assert registro.get(PROVIDER_FAKE) is not None
    assert registro.get("gemini") is None


# ---------------------------------------------------------------------------
# credential_state_from_environment
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("entorno", "estados_esperados"),
    (
        ({}, (_PENDIENTE, _PENDIENTE, _PENDIENTE, _PRESENTE)),
        ({DEEPSEEK_API_KEY_ENV: CANARIO}, (_PRESENTE, _PENDIENTE, _PENDIENTE, _PRESENTE)),
        ({ANTHROPIC_API_KEY_ENV: CANARIO}, (_PENDIENTE, _PRESENTE, _PENDIENTE, _PRESENTE)),
        ({OPENAI_API_KEY_ENV: CANARIO}, (_PENDIENTE, _PENDIENTE, _PRESENTE, _PRESENTE)),
        (
            {
                DEEPSEEK_API_KEY_ENV: CANARIO,
                ANTHROPIC_API_KEY_ENV: CANARIO,
                OPENAI_API_KEY_ENV: CANARIO,
            },
            (_PRESENTE, _PRESENTE, _PRESENTE, _PRESENTE),
        ),
    ),
)
def test_credential_state_solo_mira_la_presencia_de_la_variable(
    entorno: Mapping[str, str], estados_esperados: tuple[CredentialState, ...]
) -> None:
    """El estado de cada proveedor sale de si su variable está declarada, y de nada más."""
    capacidades = credential_state_from_environment(dict(entorno))

    assert tuple(capacidad.credential_state for capacidad in capacidades) == estados_esperados
    assert tuple(capacidad.provider for capacidad in capacidades) == (
        PROVIDER_DEEPSEEK,
        PROVIDER_ANTHROPIC,
        PROVIDER_OPENAI,
        PROVIDER_FAKE,
    )


def test_una_variable_vacia_no_acredita_al_proveedor() -> None:
    """Declarada pero vacía es lo mismo que ausente: no hay credencial que usar."""
    capacidades = credential_state_from_environment({DEEPSEEK_API_KEY_ENV: ""})

    assert capacidades[0].credential_state is _PENDIENTE
    assert capacidades[0].available is False


def test_sin_entorno_explicito_se_consulta_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """``env=None`` es el único camino que mira el proceso, y también solo la presencia."""
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, CANARIO)
    monkeypatch.delenv(ANTHROPIC_API_KEY_ENV, raising=False)
    monkeypatch.delenv(OPENAI_API_KEY_ENV, raising=False)

    por_defecto = default_capabilities()

    assert por_defecto.get(PROVIDER_DEEPSEEK) is not None
    assert por_defecto.require(RoleName.ARCHITECT).provider == PROVIDER_DEEPSEEK
    with pytest.raises(WorkflowProviderUnavailableError):
        por_defecto.require(RoleName.CROSS_AUDIT)


# ---------------------------------------------------------------------------
# Declaración de capacidades y regla de oro del secreto
# ---------------------------------------------------------------------------
def test_las_capacidades_declaradas_son_las_que_el_kernel_espera() -> None:
    """La tabla declara roles, visión, salida estructurada y disponibilidad, sin inventar."""
    capacidades = credential_state_from_environment(
        {DEEPSEEK_API_KEY_ENV: CANARIO, ANTHROPIC_API_KEY_ENV: CANARIO}
    )

    deepseek, anthropic, openai, doble = capacidades

    assert deepseek.role_support == (
        RoleName.ARCHITECT,
        RoleName.PLANNER,
        RoleName.DEVELOPER,
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
    )
    assert deepseek.structured_output is True
    assert deepseek.vision is False
    assert deepseek.available is True

    assert anthropic.role_support == (RoleName.CROSS_AUDIT, RoleName.VISUAL_QA)
    assert anthropic.vision is True
    assert anthropic.available is True

    assert openai.role_support == ()
    assert openai.available is False

    assert doble.role_support == tuple(RoleName)
    assert doble.vision is True
    assert doble.structured_output is True
    assert doble.available is True
    assert doble.credential_state is _PRESENTE


def test_ninguna_capacidad_se_declara_verificada_en_vivo() -> None:
    """``live_verified`` es ``False`` en todas: en esta fase no hay ejecución real probada."""
    entorno = {
        DEEPSEEK_API_KEY_ENV: CANARIO,
        ANTHROPIC_API_KEY_ENV: CANARIO,
        OPENAI_API_KEY_ENV: CANARIO,
    }

    capacidades = credential_state_from_environment(entorno)

    assert all(capacidad.live_verified is False for capacidad in capacidades)
    assert all(
        capacidad.live_verified is False
        for capacidad in default_capabilities(entorno).capabilities
    )


def test_el_valor_de_la_credencial_nunca_aparece_en_ninguna_capacidad() -> None:
    """Regla de oro: la credencial se mira, no se copia, no se serializa y no se registra."""
    entorno = {
        DEEPSEEK_API_KEY_ENV: CANARIO,
        ANTHROPIC_API_KEY_ENV: CANARIO,
        OPENAI_API_KEY_ENV: CANARIO,
    }

    capacidades = credential_state_from_environment(entorno)
    registro = default_capabilities(entorno)

    for capacidad in capacidades:
        serializada = capacidad.model_dump_json()
        assert CANARIO not in serializada
        assert "API_KEY" not in serializada

    assert CANARIO not in repr(capacidades)
    assert CANARIO not in repr(registro.capabilities)
    assert CANARIO not in str(registro.require(RoleName.ARCHITECT))


def test_el_valor_de_la_credencial_tampoco_aparece_en_el_error_de_fallo() -> None:
    """El detalle de un fallo nombra proveedores y estados, jamás el contenido de la clave."""
    registro = default_capabilities({DEEPSEEK_API_KEY_ENV: CANARIO})

    with pytest.raises(WorkflowProviderUnavailableError) as excinfo:
        registro.require(RoleName.VISUAL_QA)

    assert CANARIO not in str(excinfo.value)
    assert CANARIO not in excinfo.value.detail
    assert PROVIDER_ANTHROPIC in excinfo.value.detail
