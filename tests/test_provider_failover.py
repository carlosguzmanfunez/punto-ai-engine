"""PROVIDER FAILOVER v0 — un rol continúa con otro proveedor cuando el primario no está operativo.

El caso que motivó la fase: ``BUILDER=deepseek`` se queda sin créditos, Claude está ``CONNECTED``
con ``CODING`` efectivo, y la Task existente tiene que **continuar** con Claude sin perder su
identidad, su estado ni su historial, y sin que el sustituto gane autoridad alguna.

La suite cubre tres capas, todas con el motor real y sin llamar a nadie:

- **router**: la decisión de failover (causa operativa demostrable, política por rol, candidatos,
  bucles, primario preferido, auditoría, saneado);
- **registro**: el evaluador real de candidatos (``CONNECTED`` + capacidad **efectiva** del rol
  en su transporte activo), con la sesión del cliente oficial simulada y sin lanzar ningún proceso;
- **ciclo + consola**: el ``DevelopmentCycle`` y la Task de verdad, con repositorio Git real.

    pytest tests/test_provider_failover.py -q
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.console_state import default_console_state_path
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers import registry as registry_module
from punto.providers.base import (
    ModelCompletion,
    ProviderAuthenticationError,
    ProviderUnavailableError,
    StructuredModelClient,
)
from punto.providers.contract import (
    FailoverOutcome,
    ModelUsage,
    ProviderErrorKind,
    ProviderRequest,
    ProviderRole,
    ProviderStatus,
    make_request,
)
from punto.providers.deepseek import (
    DeepSeekBalanceError,
    DeepSeekInvalidResponseError,
    DeepSeekRateLimitError,
    DeepSeekServerError,
    DeepSeekTimeoutError,
)
from punto.providers.failover import (
    FailoverCause,
    FailoverPolicy,
    SubstituteVerdict,
    failover_cause_of,
)
from punto.providers.registry import ProviderRegistry
from punto.providers.router import ProviderRouter, classify_provider_error
from punto.providers.secrets import SECRETS_FILE_ENV
from punto.providers.settings import LOCAL_CONFIG_ENV, load_provider_settings
from punto.providers.transport import TransportError, TransportErrorKind
from punto.schemas.audit import AuditEventType
from punto.tools.errors import ProviderRouteError
from punto.workspace.target import DevelopmentTargetRegistry
from test_human_console import TARGET_ID, _cambio, _plan, _repos, _target

#: Credencial sintética con la marca de canario documentado del repositorio.
TEST_KEY = "sk-test-CANARY-0123456789abcdef"

BUILDER_POLICY = FailoverPolicy(roles={ProviderRole.BUILDER: ("anthropic",)})

SOLICITUD: dict[str, Any] = {
    "objective": "unificar la lista de tipos en una sola fuente",
    "target_id": TARGET_ID,
    "acceptance_criteria": ["una sola fuente de tipos"],
    "scope_paths": ["src"],
}


# --------------------------------------------------------------------------- dobles
class Fake(StructuredModelClient):
    """Proveedor guionizado: cada llamada consume el siguiente paso (el último se repite).

    Un paso puede ser un ``dict``/``str`` (respuesta), una excepción (se lanza) o un callable.
    """

    def __init__(self, provider: str, model: str, *script: Any) -> None:
        self._provider = provider
        self._model = model
        self.script: list[Any] = list(script) or [{"changes": []}]
        self.calls: list[dict[str, Any]] = []

    @property
    def provider(self) -> str:
        """Identificador del proveedor."""
        return self._provider

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return self._model

    def complete_json(self, **kwargs: Any) -> ModelCompletion:
        """Ejecuta el siguiente paso del guion."""
        self.calls.append(dict(kwargs))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(step):
            step = step()
        if isinstance(step, BaseException):
            raise step
        content = step if isinstance(step, str) else json.dumps(step)
        return ModelCompletion(
            content=content,
            model=self._model,
            usage=ModelUsage(prompt_tokens=5, completion_tokens=7, total_tokens=12),
            latency_ms=1,
        )

    def redact(self, text: str) -> str:
        """Sanea el canario, como haría el adaptador real con su clave."""
        return text.replace(TEST_KEY, "[REDACTED]")

    def close(self) -> None:
        """No hay recursos que liberar."""


def _registrar(router: ProviderRouter, *fakes: Fake) -> None:
    """Registra cada doble con su nombre y modelo."""
    for fake in fakes:
        router.register_provider(fake.provider, lambda _model, item=fake: item, model=fake.model)


def _conectados(*names: str, metered: Sequence[str] = ()) -> Callable[..., SubstituteVerdict]:
    """Evaluador de pruebas: elegible solo si el candidato está en ``names``."""

    def evaluar(role: ProviderRole, provider: str, needs_vision: bool) -> SubstituteVerdict:
        del role, needs_vision
        if provider not in names:
            return SubstituteVerdict(eligible=False, reason="no está conectado (NOT_AUTHENTICATED)")
        return SubstituteVerdict(eligible=True, metered=provider in metered)

    return evaluar


def _trio(
    *,
    deepseek: Fake,
    anthropic: Fake | None = None,
    openai: Fake | None = None,
    policy: FailoverPolicy | None = BUILDER_POLICY,
    evaluator: Callable[..., SubstituteVerdict] | None = None,
    audit: AuditLogger | None = None,
) -> tuple[ProviderRouter, Fake, Fake, Fake]:
    """Router con los tres proveedores conocidos, la asignación por defecto y failover."""
    claude = anthropic or Fake("anthropic", "claude-sonnet-5", {"changes": ["claude"]})
    gpt = openai or Fake("openai", "gpt-5-codex", {"plan": "ok"})
    router = ProviderRouter(audit=audit)
    _registrar(router, gpt, deepseek, claude)
    router.configure_failover(
        policy, evaluator if evaluator is not None else _conectados("anthropic", "openai")
    )
    return router, gpt, deepseek, claude


def _pedir(role: ProviderRole = ProviderRole.BUILDER, **extra: Any) -> ProviderRequest:
    """Petición normalizada de prueba."""
    return make_request(
        role,
        "implementa el cambio pedido",
        context="TARGET: destino\nOBJECTIVE: unificar tipos",
        request_id="req-failover-1",
        **extra,
    )


def _sin_saldo() -> DeepSeekBalanceError:
    """El 402 real de DeepSeek, con la credencial en el texto para probar el saneado."""
    return DeepSeekBalanceError(f"saldo insuficiente (402): Bearer {TEST_KEY}")


# ------------------------------------------------------------------- 1 · primario disponible
def test_1_con_el_primario_disponible_no_hay_failover_ni_se_toca_al_sustituto() -> None:
    """Un primario que responde sigue siendo el que responde; el sustituto ni se construye."""
    router, _gpt, deepseek, claude = _trio(deepseek=Fake("deepseek", "ds-1", {"changes": []}))

    result = router.execute(ProviderRole.BUILDER, _pedir())

    assert result.ok and result.provider == "deepseek"
    assert result.failovers == ()
    assert len(deepseek.calls) == 1 and claude.calls == []


# --------------------------------------------------------- 2 · primario sin créditos / cuota
@pytest.mark.parametrize(
    ("error", "kind", "cause"),
    [
        (_sin_saldo(), ProviderErrorKind.QUOTA_EXHAUSTED, FailoverCause.CREDITS_EXHAUSTED),
        (
            DeepSeekRateLimitError("límite de tasa agotado (429)"),
            ProviderErrorKind.RATE_LIMIT,
            FailoverCause.RATE_LIMITED,
        ),
        (
            DeepSeekServerError("error del proveedor (503)"),
            ProviderErrorKind.UNAVAILABLE,
            FailoverCause.PROVIDER_UNAVAILABLE,
        ),
        (
            ProviderUnavailableError("proveedor no disponible"),
            ProviderErrorKind.UNAVAILABLE,
            FailoverCause.PROVIDER_UNAVAILABLE,
        ),
        (
            TransportError(TransportErrorKind.LIMIT_REACHED, "usage limit reached"),
            ProviderErrorKind.RATE_LIMIT,
            FailoverCause.RATE_LIMITED,
        ),
    ],
)
def test_2_primario_sin_creditos_o_cuota_continua_con_el_sustituto(
    error: BaseException, kind: ProviderErrorKind, cause: FailoverCause
) -> None:
    """La causa operativa demostrable dispara el failover y queda registrada con su causa real."""
    router, _gpt, deepseek, claude = _trio(deepseek=Fake("deepseek", "ds-1", error))

    result = router.execute(ProviderRole.BUILDER, _pedir())

    assert result.ok, result.error
    assert result.provider == "anthropic" and result.model == "claude-sonnet-5"
    (record,) = result.failovers
    assert record.primary_provider == "deepseek" and record.primary_model == "ds-1"
    assert record.primary_error_kind == kind.value
    assert record.cause == cause.value
    assert record.substitute_provider == "anthropic"
    assert record.outcome is FailoverOutcome.SUCCEEDED
    assert len(deepseek.calls) == 1 and len(claude.calls) == 1
    # El primario sigue siendo la asignación configurada: el failover no la reescribe.
    assert router.get_provider_for_role(ProviderRole.BUILDER) == "deepseek"


def test_2b_el_402_de_deepseek_ya_no_es_un_fallo_desconocido() -> None:
    """Causa arquitectónica: el 402 atravesaba el transporte y el router lo forzaba a UNKNOWN."""
    assert classify_provider_error(_sin_saldo()) is ProviderErrorKind.QUOTA_EXHAUSTED
    assert failover_cause_of(ProviderErrorKind.QUOTA_EXHAUSTED) is FailoverCause.CREDITS_EXHAUSTED
    router, *_ = _trio(deepseek=Fake("deepseek", "ds-1", _sin_saldo()), policy=None)

    result = router.execute(ProviderRole.BUILDER, _pedir())

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_kind is ProviderErrorKind.QUOTA_EXHAUSTED


# ------------------------------------------------------------------ 3 · primario desconectado
def test_3_primario_sin_credencial_o_sin_sesion_continua_con_el_sustituto() -> None:
    """Sin clave (la fábrica falla) o sin sesión oficial, el primario está desconectado."""
    router, _gpt, _deepseek, claude = _trio(deepseek=Fake("deepseek", "ds-1"))

    def sin_clave(_model: str) -> StructuredModelClient:
        raise ProviderAuthenticationError("DEEPSEEK_API_KEY vacía o ausente")

    router.register_provider("deepseek", sin_clave, model="ds-1")
    sin_clave_result = router.execute(ProviderRole.BUILDER, _pedir())

    sin_sesion = Fake(
        "deepseek", "ds-1", TransportError(TransportErrorKind.NOT_AUTHENTICATED, "sin sesión")
    )
    router.register_provider("deepseek", lambda _m: sin_sesion, model="ds-1")
    sin_sesion_result = router.execute(ProviderRole.BUILDER, _pedir())

    for result in (sin_clave_result, sin_sesion_result):
        assert result.ok and result.provider == "anthropic"
        assert result.failovers[0].cause == FailoverCause.PROVIDER_DISCONNECTED.value
        assert result.failovers[0].primary_error_kind == "AUTHENTICATION"
    assert len(claude.calls) == 2


# ----------------------------------------------------------------- 4 · sustituto compatible
def test_4_el_sustituto_recibe_el_mismo_trabajo_del_mismo_rol_sin_nada_mas() -> None:
    """Mismas instrucciones, mismo contexto, mismo esquema, mismo tope y mismo rol."""
    router, _gpt, deepseek, claude = _trio(deepseek=Fake("deepseek", "ds-1", _sin_saldo()))
    esquema = {"type": "object", "properties": {"changes": {"type": "array"}}}

    router.execute(ProviderRole.BUILDER, _pedir(), json_schema=esquema, max_output_tokens=1234)

    primero, segundo = deepseek.calls[0], claude.calls[0]
    for clave in ("system_prompt", "user_prompt", "json_schema", "max_output_tokens"):
        assert primero[clave] == segundo[clave], clave
    assert segundo["max_output_tokens"] == 1234 and segundo["json_schema"] == esquema


def test_4b_solo_entran_los_sustitutos_declarados_y_en_su_orden() -> None:
    """``openai`` está conectado, pero la política solo admite ``anthropic``: no se usa."""
    router, gpt, _deepseek, claude = _trio(
        deepseek=Fake("deepseek", "ds-1", _sin_saldo()),
        anthropic=Fake("anthropic", "claude-sonnet-5", ProviderUnavailableError("caído")),
        evaluator=_conectados("anthropic", "openai"),
    )

    result = router.execute(ProviderRole.BUILDER, _pedir())

    assert gpt.calls == [], "un proveedor no declarado en la política nunca sustituye"
    assert len(claude.calls) == 1 and not result.ok
    assert [r.substitute_provider for r in result.failovers] == ["anthropic"]
    assert result.failovers[0].outcome is FailoverOutcome.FAILED


# ------------------------------------------------- 5 · sustituto sin la capacidad requerida
@pytest.fixture
def catalogo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., ProviderRegistry]:
    """Registro real con la configuración de failover del repositorio y sesión simulada.

    La sesión del cliente oficial se simula en ``auth_status`` (no se lanza ``claude``): el resto —
    descriptor, transporte activo, capacidades efectivas, política— es el código real.
    """
    monkeypatch.setenv(SECRETS_FILE_ENV, str(tmp_path / "secrets.json"))
    monkeypatch.setenv(LOCAL_CONFIG_ENV, str(tmp_path / "providers.local.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def crear(
        *, sesiones: dict[str, str] | None = None, failover: dict[str, Any] | None = None
    ) -> ProviderRegistry:
        estados = {"anthropic": "AUTHENTICATED", **(sesiones or {})}
        monkeypatch.setattr(
            ProviderRegistry,
            "auth_status",
            lambda self, provider: estados.get(provider, "NOT_AUTHENTICATED"),
        )
        config = tmp_path / "cfg"
        config.mkdir(exist_ok=True)
        (config / "providers.yaml").write_text(
            yaml.safe_dump(
                {
                    "roles": {
                        "ARCHITECT": "openai",
                        "BUILDER": "deepseek",
                        "VISUAL_QA": "anthropic",
                    },
                    "failover": failover
                    if failover is not None
                    else {"roles": {"BUILDER": ["anthropic"]}},
                }
            ),
            encoding="utf-8",
        )
        return ProviderRegistry(config_dir=config)

    return crear


def test_5_claude_por_suscripcion_tiene_coding_efectivo_y_es_elegible(
    catalogo: Callable[..., ProviderRegistry],
) -> None:
    """El evaluador real acredita conexión + CODING efectivo en el transporte ``claude_code``."""
    registro = catalogo()

    veredicto = registro._judge_substitute(ProviderRole.BUILDER, "anthropic", False)

    assert veredicto.eligible, veredicto.reason
    assert veredicto.metered is False, "la suscripción no factura por uso"
    assert registro.router_instance().failover_policy() is not None


def test_5b_una_capacidad_que_el_transporte_no_ejecuta_no_cuenta(
    catalogo: Callable[..., ProviderRegistry],
) -> None:
    """VISION está declarada para Anthropic, pero ``claude --print`` es texto: no es efectiva."""
    registro = catalogo()

    veredicto = registro._judge_substitute(ProviderRole.BUILDER, "anthropic", True)

    assert not veredicto.eligible
    assert "VISION" in veredicto.reason and "claude_code" in veredicto.reason


def test_5c_un_sustituto_sin_la_capacidad_del_rol_no_se_usa_y_se_falla_cerrado(
    catalogo: Callable[..., ProviderRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin ``CODING`` efectivo el candidato no es compatible: no se le pide el trabajo."""
    monkeypatch.setitem(
        registry_module.DEFAULT_CAPABILITIES, "anthropic", ("TEXT", "STRUCTURED_OUTPUT")
    )
    router = catalogo().router_instance()
    claude = Fake("anthropic", "claude-sonnet-5", {"changes": []})
    deepseek = Fake("deepseek", "ds-1", _sin_saldo())
    _registrar(router, deepseek, claude)

    result = router.execute(ProviderRole.BUILDER, _pedir())

    assert claude.calls == [], "no se le pidió trabajo a quien no puede hacerlo"
    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_kind is ProviderErrorKind.QUOTA_EXHAUSTED
    (record,) = result.failovers
    assert record.outcome is FailoverOutcome.NO_COMPATIBLE_SUBSTITUTE
    assert "CODING" in record.detail


def test_5d_un_sustituto_sin_sesion_no_es_compatible(
    catalogo: Callable[..., ProviderRegistry],
) -> None:
    """``CONNECTED`` se exige de verdad: sin sesión de Claude no hay sustituto."""
    registro = catalogo(sesiones={"anthropic": "NOT_AUTHENTICATED"})

    veredicto = registro._judge_substitute(ProviderRole.BUILDER, "anthropic", False)

    assert not veredicto.eligible and "NOT_AUTHENTICATED" in veredicto.reason


# ------------------------------------------------------------ 6 · ningún sustituto disponible
def test_6_sin_ningun_sustituto_compatible_se_falla_cerrado_con_la_causa_explicita() -> None:
    """Nadie conectado: el resultado sigue siendo el fallo del primario, con la causa a la vista."""
    router, _gpt, _deepseek, claude = _trio(
        deepseek=Fake("deepseek", "ds-1", _sin_saldo()), evaluator=_conectados()
    )

    result = router.execute(ProviderRole.BUILDER, _pedir())

    assert not result.ok and claude.calls == []
    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_kind is ProviderErrorKind.QUOTA_EXHAUSTED
    assert result.provider == "deepseek"
    (record,) = result.failovers
    assert record.outcome is FailoverOutcome.NO_COMPATIBLE_SUBSTITUTE
    assert record.cause == "CREDITS_EXHAUSTED"
    assert "anthropic: no está conectado" in record.detail
    assert "ningún sustituto compatible" in result.error


def test_6b_sin_evaluador_no_hay_failover_aunque_haya_politica() -> None:
    """Un sustituto que nadie pudo juzgar no se usa: falla cerrado."""
    router, _gpt, _deepseek, claude = _trio(deepseek=Fake("deepseek", "ds-1", _sin_saldo()))
    router.configure_failover(BUILDER_POLICY, None)

    result = router.execute(ProviderRole.BUILDER, _pedir())

    assert claude.calls == [] and not result.ok
    assert result.failovers[0].outcome is FailoverOutcome.NO_COMPATIBLE_SUBSTITUTE


def test_6c_un_evaluador_roto_nunca_habilita_a_un_sustituto() -> None:
    """Si juzgar al candidato lanza, el candidato no se usa."""

    def roto(role: ProviderRole, provider: str, needs_vision: bool) -> SubstituteVerdict:
        raise RuntimeError("boom")

    router, _gpt, _deepseek, claude = _trio(
        deepseek=Fake("deepseek", "ds-1", _sin_saldo()), evaluator=roto
    )

    result = router.execute(ProviderRole.BUILDER, _pedir())

    assert claude.calls == [] and not result.ok
    assert "no se pudo evaluar" in result.failovers[0].detail


def test_6d_un_proveedor_de_pago_por_uso_solo_entra_si_la_politica_lo_permite() -> None:
    """El motor no genera cargos por su cuenta: ``allow_metered`` es una decisión explícita."""
    deepseek_caido = Fake("deepseek", "ds-1", _sin_saldo())
    router, _gpt, _ds, claude = _trio(
        deepseek=deepseek_caido, evaluator=_conectados("anthropic", metered=("anthropic",))
    )

    denegado = router.execute(ProviderRole.BUILDER, _pedir())

    assert claude.calls == [] and not denegado.ok
    assert "pago por uso" in denegado.failovers[0].detail

    permitido_policy = FailoverPolicy(
        roles={ProviderRole.BUILDER: ("anthropic",)}, allow_metered=True
    )
    router.configure_failover(permitido_policy, _conectados("anthropic", metered=("anthropic",)))
    permitido = router.execute(ProviderRole.BUILDER, _pedir())
    assert permitido.ok and permitido.provider == "anthropic"


# ------------------------------------------------ 7 · sin failover por fallo del proyecto/proveedor
@pytest.mark.parametrize(
    "error",
    [
        DeepSeekInvalidResponseError("respuesta vacía"),
        DeepSeekTimeoutError("timeout"),
        RuntimeError("fallo desconocido"),
        TransportError(TransportErrorKind.PROCESS_FAILED, "el cliente terminó con exit=2"),
        TransportError(TransportErrorKind.INVALID_RESPONSE, "salida ilegible"),
    ],
)
def test_7_una_respuesta_incorrecta_o_un_fallo_no_operativo_no_cambia_de_proveedor(
    error: BaseException,
) -> None:
    """Cambiar de proveedor ante una mala respuesta escondería el problema: no se hace."""
    router, _gpt, deepseek, claude = _trio(deepseek=Fake("deepseek", "ds-1", error))

    result = router.execute(ProviderRole.BUILDER, _pedir())

    assert not result.ok and result.provider == "deepseek"
    assert result.failovers == () and claude.calls == []
    assert len(deepseek.calls) == 1


def test_7b_una_respuesta_correcta_del_proveedor_nunca_dispara_failover() -> None:
    """El contenido malo (JSON inválido, cambio rechazado...) es del ciclo, no del router."""
    router, _gpt, _deepseek, claude = _trio(deepseek=Fake("deepseek", "ds-1", "esto no es json {{"))

    result = router.execute(ProviderRole.BUILDER, _pedir())

    assert result.ok and result.provider == "deepseek" and claude.calls == []


def test_7c_un_rol_que_la_politica_no_cubre_no_hace_failover() -> None:
    """La política es por rol: ARCHITECT sin cobertura no se sustituye aunque el primario caiga."""
    router, gpt, _deepseek, claude = _trio(
        deepseek=Fake("deepseek", "ds-1"),
        openai=Fake("openai", "gpt-5-codex", _sin_saldo()),
    )

    result = router.execute(ProviderRole.ARCHITECT, _pedir(ProviderRole.ARCHITECT))

    assert not result.ok and result.failovers == () and claude.calls == []
    assert len(gpt.calls) == 1


# -------------------------------------------------------------------- 9 · sin ampliar autoridad
def test_9_la_peticion_no_puede_ampliar_la_politica() -> None:
    """La política es configuración de confianza: metadata de la petición no habilita nada."""
    router, gpt, _deepseek, claude = _trio(
        deepseek=Fake("deepseek", "ds-1"),
        openai=Fake("openai", "gpt-5-codex", _sin_saldo()),
    )
    peticion = _pedir(
        ProviderRole.ARCHITECT, metadata={"failover": "true", "allow_metered": "true"}
    )

    result = router.execute(ProviderRole.ARCHITECT, peticion)

    assert not result.ok and claude.calls == [] and len(gpt.calls) == 1


def test_9b_lo_que_produce_el_sustituto_sigue_siendo_inteligencia_no_confiable() -> None:
    """El resultado del sustituto no trae autoridad: ``trusted`` sigue en falso."""
    router, *_ = _trio(deepseek=Fake("deepseek", "ds-1", _sin_saldo()))

    vista = router.execute(ProviderRole.BUILDER, _pedir()).as_dict()

    assert vista["trusted"] is False
    assert vista["failovers"][0]["substitute_provider"] == "anthropic"
    assert set(vista["failovers"][0]) == {
        "role",
        "primary_provider",
        "primary_model",
        "primary_error_kind",
        "cause",
        "substitute_provider",
        "substitute_model",
        "outcome",
        "detail",
    }


# ---------------------------------------------------------------------------- 10 · sin bucles
def test_10_cada_proveedor_se_intenta_una_vez_y_el_total_esta_acotado() -> None:
    """Todos caídos: primario + sustitutos declarados, una vez cada uno, sin volver al primario."""
    politica = FailoverPolicy(
        roles={ProviderRole.BUILDER: ("anthropic", "openai", "anthropic", "deepseek")},
        max_substitutes=5,
    )
    router, gpt, deepseek, claude = _trio(
        deepseek=Fake("deepseek", "ds-1", _sin_saldo()),
        anthropic=Fake("anthropic", "claude-sonnet-5", DeepSeekRateLimitError("429")),
        openai=Fake("openai", "gpt-5-codex", ProviderUnavailableError("caído")),
        policy=politica,
    )

    result = router.execute(ProviderRole.BUILDER, _pedir())

    assert (len(deepseek.calls), len(claude.calls), len(gpt.calls)) == (1, 1, 1)
    assert not result.ok
    assert [r.substitute_provider for r in result.failovers] == ["anthropic", "openai"]
    assert all(r.outcome is FailoverOutcome.FAILED for r in result.failovers)


def test_10b_el_tope_de_sustitutos_se_respeta() -> None:
    """``max_substitutes=1``: solo un sustituto, aunque haya más candidatos elegibles."""
    politica = FailoverPolicy(
        roles={ProviderRole.BUILDER: ("anthropic", "openai")}, max_substitutes=1
    )
    router, gpt, _deepseek, claude = _trio(
        deepseek=Fake("deepseek", "ds-1", _sin_saldo()),
        anthropic=Fake("anthropic", "claude-sonnet-5", ProviderUnavailableError("caído")),
        policy=politica,
    )

    router.execute(ProviderRole.BUILDER, _pedir())

    assert len(claude.calls) == 1 and gpt.calls == []


def test_10c_si_el_sustituto_falla_por_su_contenido_no_se_sigue_probando() -> None:
    """Una respuesta mala del sustituto no es indisponibilidad: se detiene ahí."""
    politica = FailoverPolicy(roles={ProviderRole.BUILDER: ("anthropic", "openai")})
    router, gpt, _deepseek, claude = _trio(
        deepseek=Fake("deepseek", "ds-1", _sin_saldo()),
        anthropic=Fake("anthropic", "claude-sonnet-5", DeepSeekInvalidResponseError("vacía")),
        policy=politica,
    )

    result = router.execute(ProviderRole.BUILDER, _pedir())

    assert len(claude.calls) == 1 and gpt.calls == []
    assert result.error_kind is ProviderErrorKind.INVALID_RESPONSE


# ------------------------------------------------------- 11 · el primario vuelve a ser el preferido
def test_11_cuando_el_primario_se_recupera_vuelve_a_responder_el_primario() -> None:
    """Cada petición empieza por el asignado: el failover no lo degrada ni lo memoriza."""
    deepseek = Fake("deepseek", "ds-1", _sin_saldo())
    router, _gpt, _ds, claude = _trio(deepseek=deepseek)

    durante = router.execute(ProviderRole.BUILDER, _pedir())
    deepseek.script = [{"changes": ["deepseek vuelve"]}]
    despues = router.execute(ProviderRole.BUILDER, _pedir())

    assert durante.provider == "anthropic" and durante.failovers
    assert despues.provider == "deepseek" and despues.failovers == ()
    assert len(claude.calls) == 1
    assert router.get_provider_for_role(ProviderRole.BUILDER) == "deepseek"
    assert router.assignment()["BUILDER"] == "deepseek"


# ---------------------------------------------------------------------- 6/8 · auditoría y saneado
def test_12_el_failover_queda_auditado_por_request_id_sin_credenciales() -> None:
    """Primario, causa, sustituto y desenlace en la traza, y el canario nunca aparece."""
    audit = AuditLogger()
    router, *_ = _trio(deepseek=Fake("deepseek", "ds-1", _sin_saldo()), audit=audit)

    result = router.execute(ProviderRole.BUILDER, _pedir())

    (evento,) = audit.by_type(AuditEventType.PROVIDER_FAILOVER)
    meta = dict(evento.metadata)
    assert evento.resource_id == "req-failover-1"
    assert meta["primary_provider"] == "deepseek" and meta["substitute_provider"] == "anthropic"
    assert meta["cause"] == "CREDITS_EXHAUSTED" and meta["outcome"] == "SUCCEEDED"
    assert "sin autoridad adicional" in str(meta["authority"])
    volcado = json.dumps([dict(e.metadata) for e in audit.events()], default=str) + repr(result)
    assert TEST_KEY not in volcado


def test_12b_un_failover_sin_sustituto_se_audita_como_fallo() -> None:
    """El fallo cerrado no se registra como éxito."""
    audit = AuditLogger()
    router, *_ = _trio(
        deepseek=Fake("deepseek", "ds-1", _sin_saldo()), evaluator=_conectados(), audit=audit
    )

    router.execute(ProviderRole.BUILDER, _pedir())

    (evento,) = audit.by_type(AuditEventType.PROVIDER_FAILOVER)
    assert evento.result.value == "FAILURE"
    assert dict(evento.metadata)["outcome"] == "NO_COMPATIBLE_SUBSTITUTE"


# --------------------------------------------------------------------------------- configuración
def test_13_la_configuracion_del_repositorio_declara_failover_solo_para_builder() -> None:
    """``providers.yaml`` es el único sitio donde se declara: BUILDER → anthropic, sin cargos."""
    politica = load_provider_settings().failover

    assert politica is not None
    assert dict(politica.roles) == {ProviderRole.BUILDER: ("anthropic",)}
    assert politica.allow_metered is False


@pytest.mark.parametrize(
    "seccion",
    [
        {"roles": {"NO_EXISTE": ["anthropic"]}},
        {"roles": {"BUILDER": ["inventado"]}},
        {"roles": {"BUILDER": "anthropic"}},
        {"roles": {"BUILDER": ["anthropic"]}, "max_substitutes": 0},
        {"roles": {"BUILDER": ["anthropic"]}, "allow_metered": "si"},
        {"roles": {"BUILDER": ["anthropic"]}, "extra": 1},
        ["no", "es", "un", "mapa"],
    ],
)
def test_13b_una_politica_mal_escrita_no_se_ignora_en_silencio(
    tmp_path: Path, seccion: Any
) -> None:
    """Configuración inválida = error explícito, no un failover a medias."""
    (tmp_path / "providers.yaml").write_text(
        yaml.safe_dump({"failover": seccion}), encoding="utf-8"
    )

    with pytest.raises(ProviderRouteError):
        load_provider_settings(tmp_path, environ={})


def test_13c_sin_seccion_failover_no_hay_failover(tmp_path: Path) -> None:
    """Comportamiento histórico: sin declaración explícita, nadie sustituye a nadie."""
    (tmp_path / "providers.yaml").write_text(yaml.safe_dump({"roles": {}}), encoding="utf-8")

    assert load_provider_settings(tmp_path, environ={}).failover is None
    with pytest.raises(ValueError):
        FailoverPolicy(max_substitutes=0)


# ============================================================================ ciclo + consola
def _consola(
    tmp_path: Path,
    *,
    deepseek: Fake,
    claude: Fake,
    architect: Fake | None = None,
    politica: FailoverPolicy | None = BUILDER_POLICY,
) -> tuple[TestClient, AuditLogger, Path, ProviderRouter]:
    """Consola real con ``DevelopmentCycle`` real, repositorio Git real y failover configurado."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    audit = AuditLogger()
    gpt = architect or Fake("openai", "gpt-5-codex", _plan())
    router = ProviderRouter()
    _registrar(router, gpt, deepseek, claude)
    router.configure_failover(politica, _conectados("anthropic"))
    ciclo = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(max_repair_rounds=2, max_structural_corrections=2),
        audit=audit,
        policy_engine=PolicyEngine.from_config(),
    )
    dependencies = ConsoleDependencies(
        dev_cycle=ciclo,
        gates=HumanGate(),
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={TARGET_ID: target},
        run_inline=True,
        environ={},
    )
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencies)
    return TestClient(application), audit, repo, router


def test_a_aceptacion_builder_sin_creditos_continua_con_claude_en_la_misma_task(
    tmp_path: Path,
) -> None:
    """DeepSeek sin créditos + Claude conectado → Claude ejecuta BUILDER en la misma Task."""
    deepseek = Fake("deepseek", "deepseek-v4-pro", _sin_saldo())
    claude = Fake("anthropic", "claude-sonnet-5", _cambio())
    client, audit, repo, router = _consola(tmp_path, deepseek=deepseek, claude=claude)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    # El DevelopmentCycle continúa con normalidad: verificado, aplicado y commit local.
    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea
    desarrollo = tarea["development"]
    assert desarrollo["status"] == "DEVELOPMENT_COMPLETED" and desarrollo["commit_sha"]
    assert "Apartamento" in (repo / "src" / "lib" / "tipos.ts").read_text(encoding="utf-8")
    # Una sola Task: la misma identidad, sin Task sustituta.
    listado = client.get("/console/tasks").json()
    assert listado["total"] == 1 and listado["items"][0]["task_id"] == tarea["task_id"]
    # Quién ejecutó BUILDER y el failover, registrados en la Task.
    (failover,) = desarrollo["failovers"]
    assert failover["role"] == "BUILDER" and failover["primary_provider"] == "deepseek"
    assert failover["cause"] == "CREDITS_EXHAUSTED"
    assert failover["substitute_provider"] == "anthropic"
    assert failover["outcome"] == "SUCCEEDED"
    intento = tarea["attempts"][0]
    assert intento["provider"] == "anthropic"
    assert intento["failover"] == "deepseek->anthropic:CREDITS_EXHAUSTED/SUCCEEDED"
    # Y en la auditoría de la Task, por su identidad.
    seleccion = [
        dict(e.metadata)
        for e in audit.by_resource(tarea["task_id"])
        if e.event_type is AuditEventType.BUILD_PROVIDER_SELECTED
        and dict(e.metadata).get("primary_provider") == "deepseek"
    ]
    assert seleccion and seleccion[0]["fallback"] is True
    assert seleccion[0]["provider"] == "anthropic"
    # El primario configurado no cambió.
    assert router.get_provider_for_role(ProviderRole.BUILDER) == "deepseek"
    assert len(claude.calls) == 1


def test_b_la_task_y_su_historial_sobreviven_al_reinicio_y_el_primario_vuelve(
    tmp_path: Path,
) -> None:
    """Estado durable: misma Task tras reiniciar, historial intacto y el primario vuelve."""
    deepseek = Fake("deepseek", "deepseek-v4-pro", _sin_saldo())
    claude = Fake("anthropic", "claude-sonnet-5", _cambio())
    client, _audit, _repo, _router = _consola(tmp_path, deepseek=deepseek, claude=claude)
    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    task_id = tarea["task_id"]
    assert tarea["stage"] == "DEVELOPMENT_COMPLETED"
    assert default_console_state_path().is_file()

    # Proceso nuevo sobre el mismo estado durable; DeepSeek ya recuperó el saldo.
    reiniciada, _audit2, _repo2, _router2 = _consola(
        tmp_path / "otra",
        deepseek=Fake("deepseek", "deepseek-v4-pro", _cambio()),
        claude=Fake("anthropic", "claude-sonnet-5", _cambio()),
    )
    recuperada = reiniciada.get(f"/console/tasks/{task_id}").json()
    assert recuperada["task_id"] == task_id and recuperada["recovered"] is True
    assert recuperada["development"]["failovers"][0]["substitute_provider"] == "anthropic"
    resumen = "deepseek->anthropic:CREDITS_EXHAUSTED/SUCCEEDED"
    assert recuperada["attempts"][0]["failover"] == resumen
    assert reiniciada.get("/console/tasks").json()["total"] == 1

    # Reanudar la MISMA Task: la identidad se conserva y el primario vuelve a ser quien responde.
    reanudada = reiniciada.post(f"/console/tasks/{task_id}/run").json()
    assert reanudada["task_id"] == task_id and reanudada["runs"] == 2
    assert reiniciada.get("/console/tasks").json()["total"] == 1
    assert [item["run"] for item in reanudada["attempts"]] == [1, 2]
    assert reanudada["attempts"][0]["failover"], "el historial anterior se conserva"
    assert reanudada["attempts"][1]["failover"] == ""
    assert reanudada["development"]["failovers"] == []
    assert reanudada["attempts"][1]["provider"] in {"deepseek", "openai"}


def test_c_un_fallo_del_proyecto_no_dispara_failover(tmp_path: Path) -> None:
    """La verificación falla con la respuesta de DeepSeek: es del proyecto, no del proveedor."""
    cambio_que_no_verifica = {
        "summary": "no cumple",
        "changes": [
            {
                "path": "src/lib/tipos.ts",
                "operation": "MODIFY",
                "content": "export const TIPOS = ['Casa', 'Oficina'];\n",
                "reason": "cambio que no satisface la verificación",
                "acceptance_criterion": "una sola fuente de tipos",
            }
        ],
    }
    deepseek = Fake("deepseek", "deepseek-v4-pro", cambio_que_no_verifica)
    claude = Fake("anthropic", "claude-sonnet-5", _cambio())
    client, audit, _repo, _router = _consola(tmp_path, deepseek=deepseek, claude=claude)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] != "DEVELOPMENT_COMPLETED", "la verificación real falló"
    assert claude.calls == [], "un fallo del proyecto no cambia de proveedor"
    assert audit.by_type(AuditEventType.PROVIDER_FAILOVER) == ()
    assert tarea["development"]["failovers"] == []
    assert tarea["attempts"][0]["failover"] == ""


def test_d_el_sustituto_no_hereda_autoridad_un_borrado_sigue_exigiendo_persona(
    tmp_path: Path,
) -> None:
    """Human Gate intacto: Claude propone un borrado destructivo y PUNTO pide persona igual."""
    deepseek = Fake("deepseek", "deepseek-v4-pro", _sin_saldo())
    claude = Fake("anthropic", "claude-sonnet-5", _cambio(borrado=True))
    client, _audit, repo, _router = _consola(
        tmp_path,
        deepseek=deepseek,
        claude=claude,
        architect=Fake("openai", "gpt-5-codex", _plan(cierre=True)),
    )

    tarea = client.post(
        "/console/tasks",
        json={"objective": "retirar el fichero obsoleto", "target_id": TARGET_ID},
    ).json()

    assert tarea["stage"] == "WAITING_HUMAN", tarea
    assert len(tarea["gates"]) == 1
    assert (repo / "src" / "lib" / "obsoleto.ts").is_file(), "nada se borró sin autorización"
    assert tarea["development"]["failovers"][0]["outcome"] == "SUCCEEDED"
    UUID(tarea["gates"][0])


def test_e_el_sustituto_no_puede_salirse_del_alcance_ni_tocar_secretos(tmp_path: Path) -> None:
    """Scope y frontera de secretos intactos: un cambio fuera del alcance no se aplica."""
    fuera_de_alcance = {
        "summary": "fuera",
        "changes": [
            {
                "path": ".env.local",
                "operation": "CREATE",
                "content": "DATABASE_URL=postgresql://x:y@host/db\n",
                "reason": "credenciales",
                "acceptance_criterion": "una sola fuente de tipos",
            }
        ],
    }
    deepseek = Fake("deepseek", "deepseek-v4-pro", _sin_saldo())
    claude = Fake("anthropic", "claude-sonnet-5", fuera_de_alcance)
    client, _audit, repo, _router = _consola(tmp_path, deepseek=deepseek, claude=claude)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] != "DEVELOPMENT_COMPLETED", tarea
    assert not (repo / ".env.local").exists()
    assert "Apartamento" not in (repo / "src" / "lib" / "tipos.ts").read_text(encoding="utf-8")


def test_f_sin_sustituto_la_task_falla_cerrada_con_la_causa_explicita(tmp_path: Path) -> None:
    """Sin proveedor compatible la Task queda en fallo de desarrollo con la causa a la vista."""
    deepseek = Fake("deepseek", "deepseek-v4-pro", _sin_saldo())
    claude = Fake("anthropic", "claude-sonnet-5", _cambio())
    client, _audit, _repo, router = _consola(tmp_path, deepseek=deepseek, claude=claude)
    router.configure_failover(BUILDER_POLICY, _conectados())  # Claude desconectado

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] == "DEVELOPMENT_FAILED", tarea
    assert claude.calls == []
    assert tarea["development"]["error_kind"] == "QUOTA_EXHAUSTED"
    assert "ningún sustituto compatible" in tarea["development"]["error"]
    assert tarea["development"]["failovers"][0]["outcome"] == "NO_COMPATIBLE_SUBSTITUTE"
    assert client.get("/console/tasks").json()["total"] == 1
