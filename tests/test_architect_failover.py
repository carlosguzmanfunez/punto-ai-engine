"""ARCHITECT FAILOVER — el rol ARCHITECT continúa con otro proveedor cuando el primario no está
operativo (LIMIT_REACHED/RATE_LIMIT), exactamente por el MISMO mecanismo genérico que ya cubre
BUILDER y VISUAL_QA (``punto.providers.failover`` + ``ProviderRouter.execute``).

Causa real: la Task 0983a418 terminó ``DEVELOPMENT_PLAN_REJECTED / ARCHITECT_UNAVAILABLE`` porque
el ARCHITECT primario (OpenAI/Codex) agotó su límite de uso (``LIMIT_REACHED``) y
``config/providers.yaml`` no declaraba ningún sustituto para ``ARCHITECT`` en su sección
``failover.roles`` — solo ``BUILDER`` y ``VISUAL_QA`` lo tenían. El router y ``DevelopmentCycle``
ya tratan cualquier rol de forma idéntica (confirmado leyendo ``ProviderRouter.execute``/
``_failover``, sin ninguna rama ``if role == ...``); el único cambio de causa raíz es declarativo:
añadir ``ARCHITECT`` a la política de failover. Esta suite demuestra que, con esa configuración,
Planning se recupera solo, en el mismo ciclo, sin gate y sin reasignar el primario.

    pytest tests/test_architect_failover.py -q
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.console_state import default_console_state_path
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.base import ProviderUnavailableError
from punto.providers.contract import (
    FailoverOutcome,
    ProviderErrorKind,
    ProviderRole,
    ProviderStatus,
)
from punto.providers.failover import FailoverCause, FailoverPolicy
from punto.providers.router import ProviderRouter
from punto.providers.transport import TransportError, TransportErrorKind
from punto.schemas.audit import AuditEventType
from punto.workspace.target import DevelopmentTargetRegistry
from test_human_console import TARGET_ID, _cambio, _plan, _repos, _target
from test_provider_failover import Fake, _conectados, _pedir, _registrar

ARCHITECT_POLICY = FailoverPolicy(roles={ProviderRole.ARCHITECT: ("anthropic",)})

SOLICITUD: dict[str, object] = {
    "objective": "unificar la lista de tipos en una sola fuente",
    "target_id": TARGET_ID,
    "acceptance_criteria": ["una sola fuente de tipos"],
    "scope_paths": ["src"],
}

#: El error real observado en producción: el cliente oficial de Codex declara el límite agotado.
_LIMIT_REACHED = TransportError(TransportErrorKind.LIMIT_REACHED, "usage limit reached")


# ==================================================================== A · router: un sustituto
def test_a_architect_limit_reached_continua_con_sustituto_conectado_y_capaz() -> None:
    """LIMIT_REACHED en el ARCHITECT primario: el mismo mecanismo genérico ya lo recupera."""
    gpt = Fake("openai", "gpt-5.6-sol", _LIMIT_REACHED)
    claude = Fake("anthropic", "claude-sonnet-5", _plan())
    router = ProviderRouter()
    _registrar(router, gpt, claude)
    router.configure_failover(ARCHITECT_POLICY, _conectados("anthropic"))

    result = router.execute(ProviderRole.ARCHITECT, _pedir(ProviderRole.ARCHITECT))

    assert result.ok, result.error
    assert result.provider == "anthropic" and result.model == "claude-sonnet-5"
    (record,) = result.failovers
    assert record.primary_provider == "openai" and record.primary_error_kind == "RATE_LIMIT"
    assert record.cause == FailoverCause.RATE_LIMITED.value
    assert record.substitute_provider == "anthropic"
    assert record.outcome is FailoverOutcome.SUCCEEDED
    # El primario configurado del rol no cambia: es una sustitución de UN intento, no permanente.
    assert router.get_provider_for_role(ProviderRole.ARCHITECT) == "openai"


# ============================================== B · primer sustituto falla, el segundo funciona
def test_b_el_primer_sustituto_falla_y_el_segundo_completa_el_planning() -> None:
    """Candidato A falla, candidato B (autorizado, capaz) completa el trabajo del rol."""
    gpt = Fake("openai", "gpt-5.6-sol", _LIMIT_REACHED)
    claude = Fake("anthropic", "claude-sonnet-5", ProviderUnavailableError("caído también"))
    deepseek = Fake("deepseek", "deepseek-v4-pro", _plan())
    router = ProviderRouter()
    _registrar(router, gpt, claude, deepseek)
    router.configure_failover(
        FailoverPolicy(roles={ProviderRole.ARCHITECT: ("anthropic", "deepseek")}),
        _conectados("anthropic", "deepseek"),
    )

    result = router.execute(ProviderRole.ARCHITECT, _pedir(ProviderRole.ARCHITECT))

    assert result.ok and result.provider == "deepseek"
    assert [r.substitute_provider for r in result.failovers] == ["anthropic", "deepseek"]
    assert result.failovers[0].outcome is FailoverOutcome.FAILED
    assert result.failovers[1].outcome is FailoverOutcome.SUCCEEDED
    assert len(claude.calls) == 1 and len(deepseek.calls) == 1


# ==================================== C · ningún sustituto compatible → fallo explícito y solo
# entonces
def test_c_sin_sustituto_compatible_falla_cerrado_tras_agotar_los_candidatos() -> None:
    """Sin ningún candidato conectado/capaz, el fallo es del primario con la causa a la vista."""
    gpt = Fake("openai", "gpt-5.6-sol", _LIMIT_REACHED)
    claude = Fake("anthropic", "claude-sonnet-5", _plan())
    router = ProviderRouter()
    _registrar(router, gpt, claude)
    router.configure_failover(ARCHITECT_POLICY, _conectados())  # nadie conectado

    result = router.execute(ProviderRole.ARCHITECT, _pedir(ProviderRole.ARCHITECT))

    assert not result.ok
    assert result.status is ProviderStatus.FAILED
    assert result.error_kind is ProviderErrorKind.RATE_LIMIT
    assert claude.calls == [], "sin candidato compatible, no se gasta a nadie"
    assert result.failovers[0].outcome is FailoverOutcome.NO_COMPATIBLE_SUBSTITUTE


# ============================================== D · el failover no reasigna nada permanentemente
def test_d_el_failover_no_reasigna_permanentemente_al_primario() -> None:
    """Tras recuperarse, el primario vuelve a responder sin que nadie reconfigure el rol."""
    router = ProviderRouter()
    gpt_agotado = Fake("openai", "gpt-5.6-sol", _LIMIT_REACHED)
    claude = Fake("anthropic", "claude-sonnet-5", _plan())
    _registrar(router, gpt_agotado, claude)
    router.configure_failover(ARCHITECT_POLICY, _conectados("anthropic"))
    durante = router.execute(ProviderRole.ARCHITECT, _pedir(ProviderRole.ARCHITECT))

    router.register_provider(
        "openai", lambda _m: Fake("openai", "gpt-5.6-sol", _plan()), model="gpt-5.6-sol"
    )
    despues = router.execute(ProviderRole.ARCHITECT, _pedir(ProviderRole.ARCHITECT))

    assert durante.provider == "anthropic" and despues.provider == "openai"
    assert router.get_provider_for_role(ProviderRole.ARCHITECT) == "openai"


# ==================================================================== ciclo + consola (real)
def _consola(
    tmp_path: Path, *, architect: Fake, sustituto: Fake, politica: FailoverPolicy = ARCHITECT_POLICY
) -> tuple[TestClient, AuditLogger, ProviderRouter]:
    """Consola real con ``DevelopmentCycle`` real y repositorio Git real, failover en ARCHITECT."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    audit = AuditLogger()
    deepseek = Fake("deepseek", "deepseek-v4-pro", _cambio())
    router = ProviderRouter()
    _registrar(router, architect, sustituto, deepseek)
    router.configure_failover(politica, _conectados("anthropic", "deepseek"))
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
    return TestClient(application), audit, router


# ================================== F · misma Task, mismo ciclo lógico, sin gate por agotamiento
def test_f_el_planning_continua_en_la_misma_task_y_el_mismo_ciclo_sin_gate(tmp_path: Path) -> None:
    """El LIMIT_REACHED del ARCHITECT no crea una Task nueva ni pide una persona: sigue solo."""
    gpt = Fake("openai", "gpt-5.6-sol", _LIMIT_REACHED)
    claude = Fake("anthropic", "claude-sonnet-5", _plan())
    client, _audit, router = _consola(tmp_path, architect=gpt, sustituto=claude)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea
    assert tarea["gates"] == [], "un límite temporal del proveedor no pide una persona"
    listado = client.get("/console/tasks").json()
    assert listado["total"] == 1 and listado["items"][0]["task_id"] == tarea["task_id"]
    assert tarea["runs"] == 1, "una sola ejecución: el failover no reintenta la Task entera"
    (failover,) = tarea["development"]["failovers"]
    assert failover["role"] == "ARCHITECT" and failover["primary_provider"] == "openai"
    assert failover["primary_error_kind"] == "RATE_LIMIT"
    assert failover["substitute_provider"] == "anthropic"
    assert failover["outcome"] == "SUCCEEDED"
    assert router.get_provider_for_role(ProviderRole.ARCHITECT) == "openai"


# ======================================================= E · reinicio conserva el historial
def test_e_reinicio_conserva_el_historial_del_failover_de_architect(tmp_path: Path) -> None:
    """El estado durable (evidencia del failover incluida) sobrevive a un reinicio real."""
    gpt = Fake("openai", "gpt-5.6-sol", _LIMIT_REACHED)
    claude = Fake("anthropic", "claude-sonnet-5", _plan())
    client, _audit, _router = _consola(tmp_path, architect=gpt, sustituto=claude)
    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    task_id = tarea["task_id"]
    assert default_console_state_path().is_file()

    # «Reinicio»: proceso nuevo (nuevo HumanGate/AuditLogger), MISMO fichero de estado durable.
    reiniciada, _audit2, _router2 = _consola(
        tmp_path / "otra",
        architect=Fake("openai", "gpt-5.6-sol", _plan()),
        sustituto=Fake("anthropic", "claude-sonnet-5", _plan()),
    )
    recuperada = reiniciada.get(f"/console/tasks/{task_id}").json()

    assert recuperada["task_id"] == task_id and recuperada["recovered"] is True
    (failover,) = recuperada["development"]["failovers"]
    assert failover["role"] == "ARCHITECT" and failover["substitute_provider"] == "anthropic"
    assert recuperada["attempts"][0]["failover"] == "openai->anthropic:RATE_LIMITED/SUCCEEDED"


# ==================================== G · grafo/auditoría: primario→causa→sustituto→resultado
def test_g_la_auditoria_reconstruye_primario_causa_sustituto_y_resultado(tmp_path: Path) -> None:
    """Sin segunda fuente: la traza ya existente (``BUILD_PROVIDER_SELECTED``) basta para el
    grafo (Evidence -> Evaluation -> Repair no aplica aquí: es Primary -> Cause -> Substitute).
    """
    gpt = Fake("openai", "gpt-5.6-sol", _LIMIT_REACHED)
    claude = Fake("anthropic", "claude-sonnet-5", _plan())
    client, audit, _router = _consola(tmp_path, architect=gpt, sustituto=claude)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    eventos = [
        dict(e.metadata)
        for e in audit.by_resource(tarea["task_id"])
        if e.event_type is AuditEventType.BUILD_PROVIDER_SELECTED
        and dict(e.metadata).get("role") == "ARCHITECT"
    ]
    seleccion_inicial = next(e for e in eventos if e["provider"] == "openai" and not e["fallback"])
    assert seleccion_inicial["phase"]
    sustitucion = next(e for e in eventos if e.get("primary_provider") == "openai")
    assert sustitucion["provider"] == "anthropic" and sustitucion["fallback"] is True
    assert sustitucion["cause"] == "RATE_LIMITED"
    assert sustitucion["outcome"] == "SUCCEEDED"
    assert sustitucion["authority"] == "sin autoridad adicional: mismo rol, mismas reglas"


# ================ H · mutación: sin política para ARCHITECT no hay recuperación (fail-safe)
def test_h_sin_politica_para_architect_el_limite_sigue_terminando_el_planning(
    tmp_path: Path,
) -> None:
    """Documenta la invariante que hace fallar la mutación: sin declarar el rol, sigue fail-closed.

    Es la prueba que se rompe si alguien retira ``ARCHITECT`` de ``failover.roles`` (la causa real
    del defecto de producción) o si el router deja de ser genérico por rol.
    """
    gpt = Fake("openai", "gpt-5.6-sol", _LIMIT_REACHED)
    claude = Fake("anthropic", "claude-sonnet-5", _plan())
    client, _audit, _router = _consola(
        tmp_path,
        architect=gpt,
        sustituto=claude,
        politica=FailoverPolicy(roles={ProviderRole.BUILDER: ("anthropic",)}),  # ARCHITECT ausente
    )

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] == "DEVELOPMENT_FAILED", tarea
    assert claude.calls == [], "sin política para el rol, el router no gasta a nadie"
    assert tarea["development"]["error_kind"] == "ARCHITECT_UNAVAILABLE"
    assert tarea["development"]["failovers"] == []
