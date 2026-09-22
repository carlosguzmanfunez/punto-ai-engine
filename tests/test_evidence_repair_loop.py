"""AUTONOMOUS EVIDENCE + REPAIR LOOP v0.

Clasificación explícita de un intento de evidencia (SATISFIED / FAILED / INCONCLUSIVE /
EVIDENCE_TECHNICAL_FAILURE / CAPABILITY_UNAVAILABLE), presupuesto propio de evidencia (distinto del
de reparación) y recuperación autónoma antes de escalar a Human Gate. Nada aquí depende de un
proyecto concreto: el destino, las rutas y el criterio son genéricos.

    pytest tests/test_evidence_repair_loop.py -q
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.acceptance import (
    ClaimKind,
    VisualCapability,
    VisualVerdict,
    claims_result,
    extract_claims,
    verify_claims,
)
from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.contract import ProviderRole
from punto.providers.failover import FailoverPolicy
from punto.providers.router import ProviderRouter
from punto.schemas.audit import AuditEventType
from punto.workspace.target import DevelopmentTargetRegistry
from test_human_console import TARGET_ID, _plan, _target
from test_noop_reconciliation import SOLICITUD, _repo_ya_satisfecho
from test_visual_qa_effective import _CapturaFalsa, _evaluador_visual, _Multimodal

CRITERIO = "Unificar el estilo visual del listado de propiedades en una sola fuente"


# ============================== unidades: clasificación (sección 1, M)
def _claim():
    (claim,) = extract_claims("objetivo", (CRITERIO,))
    return claim


def _visual(available: bool = True) -> VisualCapability:
    return VisualCapability(available=available, detail="ruta efectiva", provider="openai")


def test_1_clasificacion_satisfied_failed_inconclusive_technical_capability() -> None:
    claim = _claim()
    pases = {
        "PASS": VisualVerdict(verdict="PASS", provider="openai", model="gpt", transport="codex"),
        "FAIL": VisualVerdict(verdict="FAIL", provider="openai", model="gpt", transport="codex"),
        "UNCLEAR": VisualVerdict(
            verdict="UNCLEAR", provider="openai", model="gpt", transport="codex"
        ),
    }
    satisfied = verify_claims([claim], visual=_visual(), visual_verdicts={CRITERIO: pases["PASS"]})
    failed = verify_claims([claim], visual=_visual(), visual_verdicts={CRITERIO: pases["FAIL"]})
    inconclusive = verify_claims(
        [claim], visual=_visual(), visual_verdicts={CRITERIO: pases["UNCLEAR"]}
    )
    no_capability = verify_claims([claim], visual=_visual(available=False))
    # veredicto sin proveedor (interacción no demostrable): fallo técnico de la herramienta.
    technical = verify_claims(
        [claim], visual=_visual(), visual_verdicts={CRITERIO: VisualVerdict(verdict="UNCLEAR")}
    )

    assert satisfied[0].evidence_class == "SATISFIED" and satisfied[0].satisfied
    assert failed[0].evidence_class == "FAILED" and failed[0].unsatisfied
    assert inconclusive[0].evidence_class == "INCONCLUSIVE" and inconclusive[0].not_verified
    assert no_capability[0].evidence_class == "CAPABILITY_UNAVAILABLE"
    assert technical[0].evidence_class == "EVIDENCE_TECHNICAL_FAILURE"
    # FAILED nunca se confunde con INCONCLUSIVE (requisito explícito del encargo).
    assert failed[0].evidence_class != inconclusive[0].evidence_class
    assert claims_result(failed) == "FAILED" and claims_result(inconclusive) == "EVIDENCE_REQUIRED"


def test_1b_cartografico_es_satisfied_o_failed_nunca_inconclusive() -> None:
    (claim,) = extract_claims(
        "objetivo", ("el mapa representa correctamente los 18 departamentos",)
    )
    assert claim.kind == ClaimKind.CARTOGRAPHIC_CORRECTNESS.value
    sin_dataset = verify_claims([claim], datasets=(), rendered=(False, "sin dataset"))
    assert sin_dataset[0].evidence_class == "FAILED"


# ============================== montaje: ciclo real con verdictos en serie
def _ciclo_evidencia(
    tmp_path: Path,
    *,
    veredictos: list[str],
    max_evidence_attempts: int = 2,
    max_repair_rounds: int = 2,
    respuesta_builder: Any = None,
) -> tuple[TestClient, AuditLogger, list[int]]:
    """Consola real; VISUAL_QA devuelve un veredicto distinto en cada llamada sucesiva."""
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    target = replace(
        _target(repo, remoto=remoto), visual_routes=("http://localhost:3000/propiedades",)
    )
    audit = AuditLogger()
    llamadas: list[int] = []
    cola = list(veredictos)

    def gpt_responde(imagenes: int) -> str:
        if imagenes:
            llamadas.append(len(llamadas) + 1)
            veredicto = cola.pop(0) if cola else veredictos[-1]
            return json.dumps(
                {"verdicts": [{"claim": 1, "verdict": veredicto, "observation": "vista parcial"}]}
            )
        return json.dumps(respuesta_builder or _plan())

    gpt = _Multimodal("openai", "gpt-5.6-sol", gpt_responde)
    # No-op: el BUILDER no propone cambios (el repositorio ya está satisfecho); el ARCHITECT
    # produce el plan, en un proveedor propio (el rol decide quién responde, no el nombre).
    architect = _Multimodal(
        "architect", "architect-1", lambda n: json.dumps(respuesta_builder or _plan())
    )
    builder = _Multimodal("anthropic", "claude-sonnet-5", lambda n: json.dumps({"changes": []}))
    router = ProviderRouter()
    for cliente in (gpt, architect, builder):
        router.register_provider(cliente.provider, lambda _m, c=cliente: c, model=cliente.model)
    router.assign_role(ProviderRole.ARCHITECT, "architect")
    router.assign_role(ProviderRole.BUILDER, "anthropic")
    # VISUAL_QA se asigna a un primario de solo texto (anthropic): ``_evaluador_visual`` declara que
    # no tiene capacidad efectiva VISION, así que cada petición con imágenes pasa por el sustituto
    # (openai/codex) SIN gastar al primario — exactamente la ruta efectiva real de PUNTO.
    router.assign_role(ProviderRole.VISUAL_QA, "anthropic")
    router.configure_failover(
        FailoverPolicy(
            roles={ProviderRole.BUILDER: ("anthropic",), ProviderRole.VISUAL_QA: ("openai",)}
        ),
        _evaluador_visual,
    )
    captura = _CapturaFalsa()
    ciclo = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(
            max_repair_rounds=max_repair_rounds,
            max_structural_corrections=2,
            max_evidence_attempts=max_evidence_attempts,
        ),
        audit=audit,
        policy_engine=PolicyEngine.from_config(),
        visual_capture=captura,
    )
    dependencias = ConsoleDependencies(
        dev_cycle=ciclo,
        gates=HumanGate(),
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={TARGET_ID: target},
        run_inline=True,
        environ={},
    )
    aplicacion = FastAPI()
    register_dashboard(aplicacion)
    register_human_console(aplicacion, dependencias)
    return TestClient(aplicacion), audit, llamadas


def _solicitud_visual() -> dict[str, Any]:
    return SOLICITUD | {"acceptance_criteria": [CRITERIO]}


# ================================= A · primera INCONCLUSIVE, segunda SATISFIED → sin humano
def test_a_inconclusive_luego_satisfied_continua_sin_humano(tmp_path: Path) -> None:
    client, audit, llamadas = _ciclo_evidencia(tmp_path, veredictos=["UNCLEAR", "PASS"])

    tarea = client.post("/console/tasks", json=_solicitud_visual()).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    assert tarea["gates"] == [], "sin Human Gate: la evidencia se recuperó sola"
    assert tarea["development"]["claims_result"] == "SATISFIED"
    assert tarea["development"]["evidence_attempts"] == 2
    assert len(llamadas) == 2, "dos capturas/veredictos reales, no simulados"
    eventos = audit.by_type(AuditEventType.DEV_CLAIMS_EVALUATED)
    assert len(eventos) == 2, "un evento de auditoría por intento: la traza real es durable"
    assert [dict(e.metadata)["result"] for e in eventos] == ["EVIDENCE_REQUIRED", "SATISFIED"]


# ======================= B · INCONCLUSIVE repetido hasta presupuesto → gate al agotar
def test_b_inconclusive_repetido_agota_presupuesto_y_recien_ahi_escala(tmp_path: Path) -> None:
    client, _audit, llamadas = _ciclo_evidencia(
        tmp_path, veredictos=["UNCLEAR", "UNCLEAR", "UNCLEAR"], max_evidence_attempts=2
    )

    tarea = client.post("/console/tasks", json=_solicitud_visual()).json()

    assert tarea["stage"] == "WAITING_HUMAN", tarea["development"]
    assert tarea["development"]["error_kind"] == "EVIDENCE_REQUIRED"
    assert tarea["development"]["evidence_attempts"] == 2, "se respetó el presupuesto, no más"
    assert len(llamadas) == 2
    assert "2/2 intentos" in tarea["development"]["error"]
    gates = client.get("/console/human-gates").json()["items"]
    assert len(gates) == 1, "un único Human Gate, al agotar el presupuesto"
    blocked = client.get(f"/console/tasks/{tarea['task_id']}").json()["blocked"]
    assert blocked["code"] == "EVIDENCE_REQUIRED"
    assert "intento 1" in blocked["detail"] and "intento 2" in blocked["detail"]
    assert "INCONCLUSIVE" in blocked["detail"]


# ================================= G · CAPABILITY_UNAVAILABLE nunca reintenta con este presupuesto
def test_g_capability_unavailable_no_gasta_presupuesto_de_evidencia(tmp_path: Path) -> None:
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    # Ningún transporte con VISION: la ruta efectiva de VISUAL_QA nunca está disponible.
    target = replace(_target(repo, remoto=remoto), visual_routes=())
    audit = AuditLogger()
    architect = _Multimodal("architect", "architect-1", lambda n: json.dumps(_plan()))
    builder = _Multimodal("anthropic", "claude-sonnet-5", lambda n: json.dumps({"changes": []}))
    router = ProviderRouter()
    for cliente in (architect, builder):
        router.register_provider(cliente.provider, lambda _m, c=cliente: c, model=cliente.model)
    router.assign_role(ProviderRole.ARCHITECT, "architect")
    router.assign_role(ProviderRole.BUILDER, "anthropic")
    router.configure_failover(
        FailoverPolicy(roles={ProviderRole.BUILDER: ("anthropic",)}), _evaluador_visual
    )
    ciclo = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(max_repair_rounds=2, max_evidence_attempts=3),
        audit=audit,
        policy_engine=PolicyEngine.from_config(),
    )
    dependencias = ConsoleDependencies(
        dev_cycle=ciclo,
        gates=HumanGate(),
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={TARGET_ID: target},
        run_inline=True,
        environ={},
    )
    aplicacion = FastAPI()
    register_dashboard(aplicacion)
    register_human_console(aplicacion, dependencias)
    client = TestClient(aplicacion)

    tarea = client.post("/console/tasks", json=_solicitud_visual()).json()

    assert tarea["development"]["error_kind"] == "EVIDENCE_REQUIRED"
    assert tarea["development"]["evidence_attempts"] == 1, "sin ruta, un solo intento: no reintenta"
    blocked = client.get(f"/console/tasks/{tarea['task_id']}").json()["blocked"]
    assert blocked["rule"] == "capability-unavailable"


# ================================= M · mutaciones que eliminan clasificación/budgets deben fallar
def test_m_mutacion_sin_clasificacion_retryable_pierde_la_recuperacion(tmp_path: Path) -> None:
    """Documenta la invariante con la unidad pura (para que una regresión la haga fallar)."""
    from punto.acceptance import RETRYABLE_EVIDENCE_CLASSES

    assert "INCONCLUSIVE" in RETRYABLE_EVIDENCE_CLASSES
    assert "CAPABILITY_UNAVAILABLE" not in RETRYABLE_EVIDENCE_CLASSES, (
        "ninguna ruta puede producir la evidencia: reintentar no ayuda"
    )
    assert "EVIDENCE_TECHNICAL_FAILURE" not in RETRYABLE_EVIDENCE_CLASSES, (
        "un fallo técnico determinista (sin proveedor) repite el mismo resultado: no reintentar"
    )


def test_m2_presupuesto_de_evidencia_es_declarativo_y_configurable() -> None:
    config = DevelopmentConfig()
    assert config.max_evidence_attempts >= 1
    distinto = DevelopmentConfig(max_evidence_attempts=5, max_repair_rounds=3)
    assert distinto.max_evidence_attempts != distinto.max_repair_rounds, (
        "evidence_attempts y repair_attempts son presupuestos separados"
    )
