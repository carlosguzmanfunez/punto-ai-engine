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
    client, audit, llamadas, _captura = _ciclo_evidencia_completo(
        tmp_path,
        veredictos=veredictos,
        max_evidence_attempts=max_evidence_attempts,
        max_repair_rounds=max_repair_rounds,
        respuesta_builder=respuesta_builder,
    )
    return client, audit, llamadas


def _ciclo_evidencia_con_captura(
    tmp_path: Path, *, veredictos: list[str], max_evidence_attempts: int = 2
) -> tuple[TestClient, AuditLogger, _CapturaFalsa]:
    """Como ``_ciclo_evidencia``, pero devuelve el doble de captura (ver la materialidad)."""
    client, audit, _llamadas, captura = _ciclo_evidencia_completo(
        tmp_path, veredictos=veredictos, max_evidence_attempts=max_evidence_attempts
    )
    return client, audit, captura


def _ciclo_evidencia_completo(
    tmp_path: Path,
    *,
    veredictos: list[str],
    max_evidence_attempts: int = 2,
    max_repair_rounds: int = 2,
    respuesta_builder: Any = None,
) -> tuple[TestClient, AuditLogger, list[int], _CapturaFalsa]:
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
    return TestClient(aplicacion), audit, llamadas, captura


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


def test_5_el_segundo_intento_usa_un_encuadre_materialmente_distinto(tmp_path: Path) -> None:
    """La segunda estrategia (INCONCLUSIVE→INCONCLUSIVE) no repite el mismo encuadre."""
    client, _audit, captura = _ciclo_evidencia_con_captura(
        tmp_path, veredictos=["UNCLEAR", "UNCLEAR"], max_evidence_attempts=2
    )

    client.post("/console/tasks", json=_solicitud_visual())

    viewports = [viewport for _urls, viewport in captura.llamadas]
    assert len(viewports) == 2
    assert viewports[0] != viewports[1], "el segundo intento amplía el encuadre, no lo repite"
    assert viewports[1][1] > viewports[0][1], "más alto: más contenido visible en una captura"


def test_6_una_repeticion_equivalente_no_consume_todo_el_presupuesto_en_silencio(
    tmp_path: Path,
) -> None:
    """Presupuesto amplio, sin interacción declarada: se agota la ESCALERA, no el budget."""
    client, _audit, llamadas = _ciclo_evidencia(
        tmp_path, veredictos=["UNCLEAR"] * 6, max_evidence_attempts=6
    )

    tarea = client.post("/console/tasks", json=_solicitud_visual()).json()

    assert tarea["development"]["evidence_attempts"] == 2, (
        "una sola acción distinta disponible (encuadre): no hay una tercera estrategia real, "
        "así que no se gastan los 6 intentos posibles en observaciones equivalentes"
    )
    assert len(llamadas) == 2
    blocked = client.get(f"/console/tasks/{tarea['task_id']}").json()["blocked"]
    assert blocked["rule"] == "evidence-strategies-exhausted"
    assert "estrategias agotadas" in blocked["detail"]


def test_9_gap_y_accion_quedan_en_la_auditoria_durable_por_intento(tmp_path: Path) -> None:
    """Sección 8 (durabilidad/grafo): cada intento queda en DEV_CLAIMS_EVALUATED con su acción."""
    client, audit, _llamadas = _ciclo_evidencia(tmp_path, veredictos=["UNCLEAR", "PASS"])

    client.post("/console/tasks", json=_solicitud_visual())

    eventos = audit.by_type(AuditEventType.DEV_CLAIMS_EVALUATED)
    acciones = [dict(e.metadata).get("evidence_action") for e in eventos]
    assert acciones[0] is None, "el primer intento es la línea base, sin acción de recuperación"
    segunda = dict(acciones[1])
    assert segunda["kind"] == "EXPAND_FRAMING"
    assert segunda["materiality"][0] == "viewport"


def test_7_un_bloqueo_repetido_con_motivo_distinto_reutiliza_el_gate_no_lo_duplica() -> None:
    """AP000-OBS-03-R2: reutilizar un gate pendiente no depende de la igualdad textual del motivo.

    La recuperación activa de evidencia redacta un motivo distinto por intento (observación,
    conteo). Dos bloqueos de la MISMA tarea y MISMA acción son una sola decisión humana pendiente,
    no dos, aunque el texto varíe: lo que identifica la causa es la acción sin resolver, no la
    redacción concreta del último bloqueo.
    """
    from uuid import uuid4

    from punto.api.console import ConsoleTask, _reuse_pending_gate
    from punto.schemas.enums import RiskLevel, TaskStatus

    gates = HumanGate()
    audit = AuditLogger()
    deps = ConsoleDependencies(
        dev_cycle=None,  # type: ignore[arg-type]
        gates=gates,
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={},
        run_inline=True,
        environ={},
    )
    task = ConsoleTask(
        task_id=uuid4(),
        objective="objetivo",
        target_id=TARGET_ID,
        acceptance_criteria=(CRITERIO,),
        scope_paths=(),
        context="",
    )

    primero = gates.request(
        task_id=task.task_id,
        action="EVIDENCE_REQUIRED",
        risk=RiskLevel.HIGH,
        reason="EVIDENCE_REQUIRED: intento 1, observación A",
        resume_status=TaskStatus.IN_PROGRESS,
        policy_outcome="REQUIRE_HUMAN",
    )

    reutilizado = _reuse_pending_gate(
        deps, task, "EVIDENCE_REQUIRED", "EVIDENCE_REQUIRED: intento 2, observación B (distinta)"
    )

    assert reutilizado is not None, "misma tarea+acción pendiente: se reutiliza, no se duplica"
    assert reutilizado.id == primero.id
    assert reutilizado.reason == "EVIDENCE_REQUIRED: intento 2, observación B (distinta)", (
        "el motivo mostrado se refresca al del bloqueo más reciente"
    )
    assert gates.list_for_task(task.task_id) == (reutilizado,)
    assert len(gates.list_pending()) == 1


def test_8_dos_gates_pendientes_ya_creados_se_colapsan_a_uno_al_reconciliar() -> None:
    """Sana el rastro que el defecto AP000-OBS-03-R2 ya dejó: dos PENDING → uno solo accionable."""
    from uuid import uuid4

    from punto.api.console import ConsoleTask, _dedupe_pending_gates
    from punto.schemas.enums import RiskLevel, TaskStatus

    gates = HumanGate()
    audit = AuditLogger()
    deps = ConsoleDependencies(
        dev_cycle=None,  # type: ignore[arg-type]
        gates=gates,
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={},
        run_inline=True,
        environ={},
    )
    task = ConsoleTask(
        task_id=uuid4(),
        objective="objetivo",
        target_id=TARGET_ID,
        acceptance_criteria=(CRITERIO,),
        scope_paths=(),
        context="",
    )

    antiguo = gates.request(
        task_id=task.task_id,
        action="EVIDENCE_REQUIRED",
        risk=RiskLevel.HIGH,
        reason="EVIDENCE_REQUIRED: run 1, observación A",
        resume_status=TaskStatus.IN_PROGRESS,
        policy_outcome="REQUIRE_HUMAN",
    )
    reciente = gates.request(
        task_id=task.task_id,
        action="EVIDENCE_REQUIRED",
        risk=RiskLevel.HIGH,
        reason="EVIDENCE_REQUIRED: run 2, observación B",
        resume_status=TaskStatus.IN_PROGRESS,
        policy_outcome="REQUIRE_HUMAN",
    )
    assert len(gates.list_pending()) == 2, "reproduce el defecto: dos gates operativos equivalentes"

    cambiados = _dedupe_pending_gates(task, deps)

    pendientes = gates.list_pending()
    assert len(pendientes) == 1, "solo uno queda accionable"
    assert pendientes[0].id == reciente.id, "se conserva el más reciente: el estado vigente"
    assert len(cambiados) == 1 and cambiados[0].id == antiguo.id
    superado = gates.get(antiguo.id)
    assert superado is not None
    assert superado.is_superseded
    assert superado.supersession_cause == "duplicate_pending_gate"
    assert superado.superseded_by == f"gate {reciente.id}"
    # Historial íntegro: el gate superado sigue existiendo, con su motivo original intacto.
    assert superado.reason == "EVIDENCE_REQUIRED: run 1, observación A"
    todos = gates.list_for_task(task.task_id)
    assert {item.id for item in todos} == {antiguo.id, reciente.id}
    # Idempotente: reconciliar de nuevo no cambia nada más.
    assert _dedupe_pending_gates(task, deps) == ()


def test_m2_presupuesto_de_evidencia_es_declarativo_y_configurable() -> None:
    config = DevelopmentConfig()
    assert config.max_evidence_attempts >= 1
    distinto = DevelopmentConfig(max_evidence_attempts=5, max_repair_rounds=3)
    assert distinto.max_evidence_attempts != distinto.max_repair_rounds, (
        "evidence_attempts y repair_attempts son presupuestos separados"
    )
