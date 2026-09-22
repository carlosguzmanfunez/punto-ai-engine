"""QUALITY TAKEOVER POLICY — independiente del failover OPERATIVO, prioridad por configuración.

Continuación de 3c48db6 (BUILDER TAKEOVER): ``execute_alternative`` reutilizaba
``FailoverPolicy.preferred`` — la MISMA lista de candidatos que el failover operativo
(indisponibilidad demostrable: ``RATE_LIMIT``/``QUOTA``/``AUTH``/transporte/``UNAVAILABLE``).
Eso mezclaba dos causas distintas bajo una sola política: un candidato puede ser buen sustituto
por disponibilidad y mal candidato de recuperación de calidad, o al revés.

Corrección: ``TakeoverPolicy`` (``punto.providers.takeover``), declarada en su propia sección de
``providers.yaml`` (``takeover:``, independiente de ``failover:``). La política real de PUNTO
prioriza Codex (``openai``) como recuperación de calidad para BUILDER — sin tocar su prioridad de
ARCHITECT ni la lista de failover operativo — declarada en configuración, nunca en código.

    pytest tests/test_quality_takeover_policy.py -q
"""

from __future__ import annotations

from pathlib import Path

from punto.providers.contract import FailoverOutcome, ProviderErrorKind, ProviderRole
from punto.providers.failover import FailoverPolicy
from punto.providers.router import ProviderRouter
from punto.providers.settings import load_provider_settings
from punto.providers.takeover import TakeoverPolicy
from test_provider_failover import Fake, _conectados, _pedir, _registrar, _sin_saldo


def _router_openai_deepseek_anthropic() -> tuple[ProviderRouter, Fake, Fake, Fake]:
    """Router con los tres proveedores reales del catálogo, sin asignar ningún rol todavía."""
    router = ProviderRouter()
    openai = Fake("openai", "gpt-5.6-sol", {"changes": ["codex respondió"]})
    deepseek = Fake("deepseek", "deepseek-v4-pro", {"changes": ["deepseek respondió"]})
    anthropic = Fake("anthropic", "claude-sonnet-5", {"changes": ["claude respondió"]})
    _registrar(router, openai, deepseek, anthropic)
    return router, openai, deepseek, anthropic


# ========================================== A · DeepSeek falla causalmente → Codex takeover
def test_a_deepseek_falla_causalmente_toma_codex(tmp_path: Path) -> None:
    """BUILDER=deepseek, excluido por causa de calidad: el siguiente es Codex (openai)."""
    router, openai, deepseek, _anthropic = _router_openai_deepseek_anthropic()
    router.assign_role(ProviderRole.BUILDER, "deepseek")
    router.configure_takeover(
        TakeoverPolicy(roles={ProviderRole.BUILDER: ("openai", "anthropic")}),
        _conectados("openai", "anthropic"),
    )

    result = router.execute_alternative(
        ProviderRole.BUILDER, _pedir(ProviderRole.BUILDER), exclude=frozenset({"deepseek"})
    )

    assert result.ok and result.provider == "openai"
    assert deepseek.calls == [] and openai.calls


# ============================================ B · Claude falla causalmente → Codex takeover
def test_b_claude_falla_causalmente_toma_codex() -> None:
    """BUILDER=anthropic, excluido por causa de calidad: el siguiente es Codex (openai)."""
    router, openai, _deepseek, anthropic = _router_openai_deepseek_anthropic()
    router.assign_role(ProviderRole.BUILDER, "anthropic")
    router.configure_takeover(
        TakeoverPolicy(roles={ProviderRole.BUILDER: ("openai",)}), _conectados("openai")
    )

    result = router.execute_alternative(
        ProviderRole.BUILDER, _pedir(ProviderRole.BUILDER), exclude=frozenset({"anthropic"})
    )

    assert result.ok and result.provider == "openai"
    assert anthropic.calls == [] and openai.calls


# ==================================== C · Codex primario → nunca se selecciona a sí mismo
def test_c_codex_primario_nunca_se_selecciona_a_si_mismo() -> None:
    """Si Codex ya es el BUILDER que falló, la exclusión evita que se reelija a sí mismo."""
    router, openai, _deepseek, _anthropic = _router_openai_deepseek_anthropic()
    router.assign_role(ProviderRole.BUILDER, "openai")
    router.configure_takeover(
        TakeoverPolicy(roles={ProviderRole.BUILDER: ("openai", "anthropic")}),
        _conectados("openai", "anthropic"),
    )

    result = router.execute_alternative(
        ProviderRole.BUILDER, _pedir(ProviderRole.BUILDER), exclude=frozenset({"openai"})
    )

    assert result.ok and result.provider == "anthropic", (
        "openai está en la lista de candidatos Y excluido: nunca se elige a sí mismo"
    )
    assert openai.calls == []


# ========================================= D · Codex no disponible → siguiente candidato
def test_d_codex_no_disponible_usa_el_siguiente_candidato_autorizado() -> None:
    """Sin Codex conectado, el takeover sigue con el siguiente candidato declarado."""
    router, openai, deepseek, _anthropic = _router_openai_deepseek_anthropic()
    router.assign_role(ProviderRole.BUILDER, "deepseek")
    router.configure_takeover(
        TakeoverPolicy(roles={ProviderRole.BUILDER: ("openai", "anthropic")}),
        _conectados("anthropic"),  # Codex (openai) NO está en la lista de conectados
    )

    result = router.execute_alternative(
        ProviderRole.BUILDER, _pedir(ProviderRole.BUILDER), exclude=frozenset({"deepseek"})
    )

    assert result.ok and result.provider == "anthropic"
    assert openai.calls == [] and deepseek.calls == []


# ============== E · RATE_LIMIT sigue usando el failover OPERATIVO, no el takeover de calidad
def test_e_rate_limit_usa_failover_operativo_no_la_politica_de_takeover() -> None:
    """Una indisponibilidad demostrable consulta ``FailoverPolicy``, nunca ``TakeoverPolicy``."""
    router, _openai, deepseek, _anthropic = _router_openai_deepseek_anthropic()
    deepseek.script = [_sin_saldo()]
    router.assign_role(ProviderRole.BUILDER, "deepseek")
    # Listas DELIBERADAMENTE distintas: si el motor mezclara las dos políticas, este resultado
    # sería "openai" (la preferencia de takeover) en vez de "anthropic" (la de failover).
    router.configure_failover(
        FailoverPolicy(roles={ProviderRole.BUILDER: ("anthropic",)}), _conectados("anthropic")
    )
    router.configure_takeover(
        TakeoverPolicy(roles={ProviderRole.BUILDER: ("openai",)}), _conectados("openai")
    )

    result = router.execute(ProviderRole.BUILDER, _pedir(ProviderRole.BUILDER))

    assert result.ok and result.provider == "anthropic", "RATE_LIMIT/QUOTA es failover operativo"
    (record,) = result.failovers
    assert record.outcome is FailoverOutcome.SUCCEEDED
    assert record.primary_error_kind == ProviderErrorKind.QUOTA_EXHAUSTED.value


# ==================== F · la configuración (no un if específico) determina la prioridad
def test_f_el_orden_declarado_en_la_politica_decide_no_un_if_especifico() -> None:
    """Invertir el orden declarado invierte a quién se elige: es dato, no código."""
    router, openai, _deepseek, _anthropic = _router_openai_deepseek_anthropic()
    router.assign_role(ProviderRole.BUILDER, "deepseek")
    router.configure_takeover(
        TakeoverPolicy(roles={ProviderRole.BUILDER: ("anthropic", "openai")}),
        _conectados("openai", "anthropic"),
    )

    result = router.execute_alternative(
        ProviderRole.BUILDER, _pedir(ProviderRole.BUILDER), exclude=frozenset({"deepseek"})
    )

    assert result.ok and result.provider == "anthropic", (
        "con anthropic declarado primero en la política, se elige a él, no a Codex"
    )
    assert openai.calls == []


# ==================================== G · sin reasignación permanente de roles
def test_g_el_takeover_no_reasigna_el_rol_permanentemente() -> None:
    """Tras el takeover, la asignación del rol (Dashboard) sigue siendo la configurada."""
    router, _openai, _deepseek, _anthropic = _router_openai_deepseek_anthropic()
    router.assign_role(ProviderRole.BUILDER, "deepseek")
    router.configure_takeover(
        TakeoverPolicy(roles={ProviderRole.BUILDER: ("openai",)}), _conectados("openai")
    )

    router.execute_alternative(
        ProviderRole.BUILDER, _pedir(ProviderRole.BUILDER), exclude=frozenset({"deepseek"})
    )

    assert router.get_provider_for_role(ProviderRole.BUILDER) == "deepseek"


# ============ H (mutación) · failover y takeover no pueden volver a compartir una política
def test_h_failover_y_takeover_conservan_politicas_independientes() -> None:
    """El router expone dos políticas SEPARADAS: configurar una nunca toca la otra."""
    router = ProviderRouter()

    assert router.failover_policy() is None and router.takeover_policy() is None

    router.configure_failover(FailoverPolicy(roles={ProviderRole.BUILDER: ("anthropic",)}))
    assert router.failover_policy() is not None
    assert router.takeover_policy() is None, "declarar failover no activa takeover por sí solo"

    router.configure_takeover(TakeoverPolicy(roles={ProviderRole.BUILDER: ("openai",)}))
    assert router.takeover_policy() is not None
    assert router.failover_policy() is not None, "declarar takeover no borra el failover vigente"
    assert router.failover_policy().roles != router.takeover_policy().roles


# =========================== F2 · la configuración real de PUNTO prioriza Codex para BUILDER
def test_f2_la_configuracion_real_prioriza_codex_para_builder_takeover() -> None:
    """``config/providers.yaml`` declara Codex primero para la recuperación de BUILDER."""
    settings = load_provider_settings(Path("config"), environ={})

    assert settings.takeover is not None
    assert settings.takeover.covers(ProviderRole.BUILDER)
    candidatos = settings.takeover.preferred(ProviderRole.BUILDER)
    assert candidatos and candidatos[0] == "openai", (
        "Codex es el proveedor prioritario de recuperación causal, por configuración"
    )
    # Y la política de failover OPERATIVO (indisponibilidad) sigue siendo la suya propia.
    assert settings.failover is not None
    assert settings.failover.preferred(ProviderRole.BUILDER) != candidatos
