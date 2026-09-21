"""Cadena post-development (release) para un no-op verificado + Human Gates operativos.

Defecto A: una Task ``ALREADY_SATISFIED`` no tiene commit propio y toda la cadena de release
(botón, ``/production-gate``, evaluación de autoridad, publicación) exigía ``result.commit_sha``: un
no-op verificado quedaba sin artefacto publicable aunque existiera un HEAD exactamente verificado.

Defecto B: los gates pendientes de intentos anteriores (``PLAN_REQUIRES_HUMAN``,
``EVIDENCE_REQUIRED``) seguían como acciones humanas aunque la evidencia posterior demostraba que su
condición ya no existía.

    pytest tests/test_release_chain_and_gate_reconciliation.py -q
"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from punto.api.console_state import default_console_state_path
from punto.api.gate_reconciliation import assess_gate
from punto.common import utc_now
from punto.policy.human_gate import HumanGate
from punto.policy.target_authority import ReleaseContext, _commit_from_task
from punto.schemas.dev import (
    AuthorityDecisionRecord,
    BuildValidationIssue,
    ClaimEvidence,
    DevelopmentPlan,
    DevelopmentResult,
    DevelopmentStatus,
    NoOpEvidence,
    PlanStatus,
)
from punto.schemas.enums import ApprovalStatus, RiskLevel, TaskStatus
from test_console_rerun import SOLICITUD
from test_console_state import _reiniciar, _tarea_con_gate_pendiente
from test_human_console import (
    TARGET_ID,
    _app,
    _autoridad_completa,
    _cambio,
    _git,
    _plan,
    _repos,
    _target,
)
from test_noop_reconciliation import _commits, _repo_ya_satisfecho

FIXTURE_TASK_2E7822A0 = Path(__file__).parent / "fixtures" / "console-state-task-2e7822a0.json"


# ------------------------------------------------------------------------------- montaje
def _noop(tmp_path: Path) -> tuple[TestClient, Any, Path, Path, dict[str, Any]]:
    """Task real ``ALREADY_SATISFIED``: el estado ya estaba en el baseline, sin commit propio."""
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    client, _audit, _t, deps = _app(target=_target(repo, remoto=remoto), respuestas=[_plan()])
    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    assert tarea["development"]["resolution"] == "ALREADY_SATISFIED", tarea["development"]
    return client, deps, repo, remoto, tarea


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _remote_main(remoto: Path) -> str:
    """SHA que hay en la rama de producción del remoto ('' si no existe)."""
    hecho = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "main"],
        cwd=remoto,
        capture_output=True,
        text=True,
        check=False,
    )
    return hecho.stdout.strip()


def _gates_de_publicacion(client: TestClient) -> list[dict[str, Any]]:
    items = client.get("/console/human-gates").json()["items"]
    return [item for item in items if item["action"] == "deploy_production"]


# ==================================== A · una Task con commit del ciclo sigue funcionando
def test_a_una_task_con_commit_del_ciclo_sigue_por_la_misma_cadena(tmp_path: Path) -> None:
    repo, remoto = _repos(tmp_path)
    client, _audit, _t, _deps = _app(
        target=_target(repo, remoto=remoto), respuestas=[_plan(), _cambio()]
    )
    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    desarrollo = tarea["development"]

    assert desarrollo["publishable_sha"] == desarrollo["commit_sha"] != ""
    assert desarrollo["publishable_source"] == "cycle-commit"
    assert tarea["next_human_action"]["kind"] == "request_production_gate"

    pedido = client.post(f"/console/tasks/{tarea['task_id']}/production-gate").json()
    (gate,) = _gates_de_publicacion(client)
    assert pedido["publication"]["commit_sha"] == desarrollo["commit_sha"]
    aprobado = client.post(
        f"/console/human-gates/{gate['approval_id']}/approve", json={"resolved_by": "humano"}
    ).json()

    assert aprobado["stage"] == "PRODUCTION_VALIDATED", aprobado
    assert _remote_main(remoto) == desarrollo["commit_sha"]


# ===================== B · un no-op verificado publica el SHA existente, sin commit vacío
def test_b_un_no_op_verificado_entra_a_release_con_su_sha_sin_commit_vacio(tmp_path: Path) -> None:
    client, _deps, repo, remoto, tarea = _noop(tmp_path)
    head, commits = _head(repo), _commits(repo)
    desarrollo = tarea["development"]

    assert desarrollo["commit_sha"] == ""
    assert desarrollo["publishable_sha"] == head
    assert desarrollo["publishable_source"] == "verified-head"
    assert desarrollo["artifact_issue"] == ""
    assert tarea["next_human_action"] == {
        "kind": "request_production_gate",
        "publishable_sha": head,
        "publishable_source": "verified-head",
        "label": "Pedir la aprobación de producción",
    }
    condicion = next(
        item
        for item in tarea["release"]["conditions"]
        if item["name"] == "commit_from_governed_task"
    )
    assert condicion["state"] == "SATISFIED", condicion

    pedido = client.post(f"/console/tasks/{tarea['task_id']}/production-gate")
    assert pedido.status_code == 200, pedido.text
    (gate,) = _gates_de_publicacion(client)
    assert gate["status"] == "PENDING" and gate["actionable"] is True
    assert gate["destination"]["publishable_sha"] == head, "el gate está ligado al SHA verificado"
    assert head[:12] in gate["reason"]

    aprobado = client.post(
        f"/console/human-gates/{gate['approval_id']}/approve", json={"resolved_by": "humano"}
    ).json()

    assert aprobado["stage"] == "PRODUCTION_VALIDATED", aprobado
    assert aprobado["publication"]["commit_sha"] == head
    assert _remote_main(remoto) == head, "se publicó exactamente el estado verificado"
    assert _commits(repo) == commits, "no se fabricó ningún commit"


def test_b2_con_autoridad_persistente_el_no_op_publica_su_sha_sin_gate_ni_commit(
    tmp_path: Path,
) -> None:
    """El sobre del destino cubre la cadena: el no-op verificado se libera solo, con su SHA."""
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    head, commits = _head(repo), _commits(repo)
    destino = replace(_target(repo, remoto=remoto), authority=_autoridad_completa())
    client, _audit, _t, _d = _app(target=destino, respuestas=[_plan()])

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["development"]["resolution"] == "ALREADY_SATISFIED"
    assert tarea["release"]["disposition"] == "AUTO" and tarea["gates"] == []
    assert tarea["stage"] == "PRODUCTION_VALIDATED", tarea
    assert tarea["publication"]["commit_sha"] == head
    assert _remote_main(remoto) == head and _commits(repo) == commits


# ============================ C · SHA ambiguo o divergente: falla cerrado, sin publicar
def test_c1_un_plan_sobre_ficheros_sin_confirmar_no_identifica_artefacto(tmp_path: Path) -> None:
    """El estado verificado incluye cambios sin confirmar del plan: ningún commit lo representa."""
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    tipos = repo / "src" / "lib" / "tipos.ts"
    tipos.write_text(
        tipos.read_text(encoding="utf-8") + "// cambio sin confirmar\n", encoding="utf-8"
    )
    client, _audit, _t, _deps = _app(target=_target(repo, remoto=remoto), respuestas=[_plan()])
    remoto_antes = _remote_main(remoto)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    desarrollo = tarea["development"]
    assert desarrollo["resolution"] == "ALREADY_SATISFIED"
    assert desarrollo["publishable_sha"] == ""
    assert "cambios sin confirmar" in desarrollo["artifact_issue"]
    assert tarea["next_human_action"]["kind"] == "none"
    assert "artefacto publicable inequívoco" in tarea["next_human_action"]["blocked_reason"]
    pedido = client.post(f"/console/tasks/{tarea['task_id']}/production-gate")
    assert pedido.status_code == 409 and "sin confirmar" in pedido.text
    assert _gates_de_publicacion(client) == [] and _remote_main(remoto) == remoto_antes
    condicion = next(
        item
        for item in tarea["release"]["conditions"]
        if item["name"] == "commit_from_governed_task"
    )
    assert condicion["state"] == "UNSATISFIED"


def test_c2_si_el_head_diverge_del_estado_verificado_no_se_publica(tmp_path: Path) -> None:
    """Tras verificar, alguien mueve el HEAD: aprobar el gate no publica ningún otro commit."""
    client, _deps, repo, remoto, tarea = _noop(tmp_path)
    verificado, remoto_antes = _head(repo), _remote_main(remoto)
    client.post(f"/console/tasks/{tarea['task_id']}/production-gate")
    (gate,) = _gates_de_publicacion(client)
    (repo / "src" / "lib" / "otro.ts").write_text("export const X = 1;\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=f", "-c", "user.email=f@p.local", "commit", "-m", "el HEAD avanza")
    assert _head(repo) != verificado

    respuesta = client.post(
        f"/console/human-gates/{gate['approval_id']}/approve", json={"resolved_by": "humano"}
    )

    assert respuesta.status_code == 409 and "ya no es el estado verificado" in respuesta.text
    assert _remote_main(remoto) == remoto_antes, "no se publicó nada"
    (despues,) = _gates_de_publicacion(client)
    assert despues["status"] == "PENDING", "la decisión no se registró sobre un estado divergente"
    reevaluada = client.post(f"/console/tasks/{tarea['task_id']}/release")
    assert reevaluada.status_code == 409
    assert "commit_from_governed_task" in reevaluada.json()["detail"]["blockers"]


def _resultado_no_op(**cambios: Any) -> DevelopmentResult:
    evidencia = NoOpEvidence(
        baseline_sha="a" * 40, state_digest="d" * 64, **cambios.pop("evidencia", {})
    )
    return DevelopmentResult(
        status=DevelopmentStatus.COMPLETED,
        resolution="ALREADY_SATISFIED",
        no_op_evidence=evidencia,
        **cambios,
    )


def test_c3_la_seleccion_del_sha_publicable_no_infiere_nunca() -> None:
    sha = "b" * 40
    assert _resultado_no_op(evidencia={"verified_sha": sha}).publishable_artifact == (
        sha,
        "verified-head",
    )
    # legado (persistido antes de verified_sha): solo su baseline declarado
    assert _resultado_no_op().publishable_artifact == ("a" * 40, "legacy-baseline")
    # ambiguo: la evidencia dice explícitamente que no hay artefacto único
    assert _resultado_no_op(evidencia={"artifact_issue": "HEAD distinto"}).publishable_sha == ""
    # sin evidencia de no-op, o con cambios aplicados, o fallido: nada
    assert DevelopmentResult(status=DevelopmentStatus.COMPLETED).publishable_sha == ""
    fallido = DevelopmentResult(
        status=DevelopmentStatus.VERIFICATION_FAILED,
        resolution="ALREADY_SATISFIED",
        no_op_evidence=NoOpEvidence(verified_sha="c" * 40),
    )
    assert fallido.publishable_sha == ""
    # el commit del ciclo manda sobre cualquier otra cosa
    con_commit = DevelopmentResult(status=DevelopmentStatus.COMPLETED, commit_sha="e" * 40)
    assert con_commit.publishable_artifact == ("e" * 40, "cycle-commit")


@pytest.mark.parametrize(
    ("contexto", "estado"),
    [
        ({"commit_sha": "b" * 40, "commit_present": True, "head_sha": "b" * 40}, "SATISFIED"),
        ({"commit_sha": "f" * 40, "commit_present": True, "head_sha": "b" * 40}, "UNSATISFIED"),
        ({"commit_sha": "b" * 40, "commit_present": True, "head_sha": "9" * 40}, "UNSATISFIED"),
        ({"commit_sha": "b" * 40, "commit_present": True, "head_sha": None}, "UNKNOWN"),
        ({"commit_sha": "b" * 40, "commit_present": False, "head_sha": "b" * 40}, "UNSATISFIED"),
        ({"commit_sha": "b" * 40, "commit_present": None, "head_sha": "b" * 40}, "UNKNOWN"),
        ({"commit_sha": "", "commit_present": True, "head_sha": "b" * 40}, "UNSATISFIED"),
    ],
)
def test_c4_la_condicion_de_release_exige_sha_identico_y_head_actual(
    contexto: dict[str, Any], estado: str
) -> None:
    resultado = _resultado_no_op(evidencia={"verified_sha": "b" * 40})

    condicion = _commit_from_task(
        ReleaseContext(
            task_id="t", target=None, policy_decision=None, result=resultado, **contexto
        ),
        resultado,
    )

    assert condicion.state == estado, condicion


# ================================ D/E/F · reconciliación de gates con el estado canónico
def _gate(accion: str, *, pedido: Any = None) -> Any:
    gates = HumanGate()
    gate = gates.request(
        task_id=uuid4(),
        action=accion,
        risk=RiskLevel.HIGH,
        reason=f"{accion}: gate de prueba",
        resume_status=TaskStatus.IN_PROGRESS,
    )
    if pedido is not None:
        gate.requested_at = pedido
    return gate


def _intento(run: int, *, posterior: bool = True) -> Any:
    ahora = utc_now()
    inicio = ahora.replace(year=ahora.year + 1) if posterior else ahora.replace(year=ahora.year - 1)
    return SimpleNamespace(run=run, started_at=inicio)


def _tarea(resultado: DevelopmentResult | None, *, intento: Any, publicacion: Any = None) -> Any:
    return SimpleNamespace(
        result=resultado, attempts=[intento], publication=publicacion, executing=False
    )


def _plan_valido() -> DevelopmentPlan:
    return DevelopmentPlan(
        summary="plan",
        files_to_read=["src/a.ts"],
        files_to_modify=["src/a.ts"],
        verification_commands=["focused"],
        acceptance_mapping=["criterio"],
    )


def _decision_plan(resultado: str = "ALLOW") -> AuthorityDecisionRecord:
    return AuthorityDecisionRecord(
        operation="plan_apply",
        outcome=resultado,
        authority_class="AUTONOMOUS_LOCAL",
        risk="LOW",
        rules=("local-technical-reversible",),
    )


def test_d_el_gate_de_plan_se_supera_cuando_el_plan_posterior_se_valido() -> None:
    gate = _gate("PLAN_REQUIRES_HUMAN")
    resultado = DevelopmentResult(
        status=DevelopmentStatus.COMPLETED,
        plan=_plan_valido(),
        plan_status=PlanStatus.VALID,
        authority_decisions=(_decision_plan(),),
    )

    veredicto = assess_gate(_tarea(resultado, intento=_intento(13)), gate)

    assert veredicto.actionable is False
    assert veredicto.superseded_by == "intento 13"
    assert "plan_apply = ALLOW" in veredicto.cause


@pytest.mark.parametrize(
    "resultado",
    [
        # el plan sigue exigiendo persona
        DevelopmentResult(
            status=DevelopmentStatus.BLOCKED,
            plan=_plan_valido(),
            plan_status=PlanStatus.VALID,
            plan_issues=(BuildValidationIssue(code="PLAN_REQUIRES_HUMAN", detail="x"),),
            authority_decisions=(_decision_plan("REQUIRE_HUMAN"),),
        ),
        # plan rechazado: no hay evidencia de validación
        DevelopmentResult(status=DevelopmentStatus.PLAN_REJECTED, plan_status=PlanStatus.REJECTED),
        # plan válido pero sin decisión de autoridad que lo autorice
        DevelopmentResult(
            status=DevelopmentStatus.COMPLETED, plan=_plan_valido(), plan_status=PlanStatus.VALID
        ),
    ],
)
def test_f_un_gate_de_plan_cuya_condicion_sigue_vigente_permanece_pendiente(
    resultado: DevelopmentResult,
) -> None:
    veredicto = assess_gate(_tarea(resultado, intento=_intento(13)), _gate("PLAN_REQUIRES_HUMAN"))

    assert veredicto.actionable is True, veredicto


def _reclamo(resultado: str) -> ClaimEvidence:
    return ClaimEvidence(
        kind="VISUAL_APPEARANCE", result=resultado, evidence="captura", required=True
    )


def test_e_el_gate_de_evidencia_se_supera_solo_con_evidencia_posterior_satisfecha() -> None:
    gate = _gate("EVIDENCE_REQUIRED")
    satisfecho = DevelopmentResult(
        status=DevelopmentStatus.COMPLETED,
        claims=(_reclamo("SATISFIED"),),
        claims_result="SATISFIED",
    )
    sin_evidencia = DevelopmentResult(
        status=DevelopmentStatus.BLOCKED,
        error_kind="EVIDENCE_REQUIRED",
        claims=(_reclamo("NOT_VERIFIED"),),
        claims_result="EVIDENCE_REQUIRED",
    )
    solo_etapa = DevelopmentResult(status=DevelopmentStatus.COMPLETED, claims_result="NONE")
    insatisfecho = DevelopmentResult(
        status=DevelopmentStatus.VERIFICATION_FAILED,
        claims=(_reclamo("UNSATISFIED"),),
        claims_result="FAILED",
    )

    assert assess_gate(_tarea(satisfecho, intento=_intento(13)), gate).actionable is False
    assert assess_gate(_tarea(sin_evidencia, intento=_intento(13)), gate).actionable is True
    assert assess_gate(_tarea(insatisfecho, intento=_intento(13)), gate).actionable is True, (
        "un criterio medido y NO satisfecho no es evidencia presente"
    )
    assert assess_gate(_tarea(solo_etapa, intento=_intento(13)), gate).actionable is True, (
        "avanzar de etapa no es evidencia: la ausencia no se convierte en presencia"
    )


def test_f_el_gate_del_propio_intento_o_de_un_tipo_desconocido_no_se_supera() -> None:
    satisfecho = DevelopmentResult(
        status=DevelopmentStatus.COMPLETED,
        claims=(_reclamo("SATISFIED"),),
        claims_result="SATISFIED",
    )
    del_propio_intento = assess_gate(
        _tarea(satisfecho, intento=_intento(2, posterior=False)), _gate("EVIDENCE_REQUIRED")
    )
    desconocido = assess_gate(_tarea(satisfecho, intento=_intento(3)), _gate("OTRA_COSA"))
    en_ejecucion = _tarea(satisfecho, intento=_intento(3))
    en_ejecucion.executing = True

    assert del_propio_intento.actionable is True
    assert desconocido.actionable is True, "un tipo sin predicado permanece pendiente (fail-safe)"
    assert assess_gate(en_ejecucion, _gate("EVIDENCE_REQUIRED")).actionable is True


# --------------------------------------------- D/F/G con el ciclo real y reinicios
def _satisfacer_el_repo(target: Any) -> Any:
    """El repositorio ya contiene el estado pedido (alguien lo confirmó): el destino nuevo."""
    repo = target.repository
    (repo / "src" / "lib" / "tipos.ts").write_text(
        "export const TIPOS = ['Casa', 'Apartamento'];\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=f", "-c", "user.email=f@p.local", "commit", "-m", "ya satisfecho")
    return _target(repo, remoto=target.publish_remote)


def test_d_f_g_flujo_real_el_gate_vigente_sigue_y_el_obsoleto_pasa_al_historial(
    tmp_path: Path,
) -> None:
    target, client, tarea = _tarea_con_gate_pendiente(tmp_path)
    gate_id = tarea["gates"][0]

    # F: mientras la condición sigue vigente, el gate es accionable (y un reinicio no lo toca).
    tablero = client.get("/console/human-gates").json()
    assert [item["approval_id"] for item in tablero["operational"]] == [gate_id]
    assert tablero["history"] == [] and tablero["pending"] == 1
    otro, _audit = _reiniciar(target)
    assert otro.get("/console/human-gates").json()["operational"][0]["approval_id"] == gate_id

    # D: un intento posterior valida el plan (el sobre lo autoriza solo).
    nuevo = _satisfacer_el_repo(target)
    cliente, _a, _t, _d = _app(target=nuevo, respuestas=[_plan()])
    reanudada = cliente.post(f"/console/tasks/{tarea['task_id']}/run")
    assert reanudada.status_code == 200, reanudada.text
    # El criterio cartográfico de la Task sigue sin cumplirse (el intento falla): el gate de PLAN se
    # supera igualmente porque **su** condición (el plan exigía persona) desapareció; el resto del
    # fallo no es esa condición.
    assert reanudada.json()["stage"] == "DEVELOPMENT_FAILED", reanudada.json()

    tablero = cliente.get("/console/human-gates").json()
    assert tablero["operational"] == [] and tablero["pending"] == 0
    (historico,) = tablero["history"]
    assert historico["approval_id"] == gate_id and historico["status"] == "SUPERSEDED"
    assert historico["state"] == "SUPERSEDED" and historico["actionable"] is False
    assert historico["superseded_by"].startswith("intento ")
    assert historico["supersession_cause"]
    assert historico["resolved_by"] == "punto-engine" and historico["resolved_at"]
    assert historico["action"] == "PLAN_REQUIRES_HUMAN" and historico["reason"], "original intacto"
    vista = cliente.get(f"/console/tasks/{tarea['task_id']}").json()
    assert vista["gates"] == [gate_id], "el historial de identificadores se conserva"
    assert vista["pending_gates"] == []

    # G: tras un reinicio el gate obsoleto no resucita y sigue en el historial durable.
    reiniciado, _audit = _reiniciar(nuevo)
    otra = reiniciado.get("/console/human-gates").json()
    assert otra["operational"] == [] and otra["pending"] == 0
    assert [item["status"] for item in otra["history"]] == ["SUPERSEDED"]
    assert otra["history"][0]["superseded_by"] == historico["superseded_by"]
    estado = json.loads(default_console_state_path().read_text(encoding="utf-8"))
    (registro,) = estado["gates"]
    assert registro["status"] == "SUPERSEDED" and registro["superseded_by"]


def test_i_un_gate_historico_no_se_puede_aprobar_ni_rechazar(tmp_path: Path) -> None:
    target, _client, tarea = _tarea_con_gate_pendiente(tmp_path)
    gate_id = tarea["gates"][0]
    cliente, _a, _t, _d = _app(target=_satisfacer_el_repo(target), respuestas=[_plan()])
    cliente.post(f"/console/tasks/{tarea['task_id']}/run")
    assert cliente.get("/console/human-gates").json()["history"][0]["status"] == "SUPERSEDED"

    aprobar = cliente.post(f"/console/human-gates/{gate_id}/approve", json={"resolved_by": "h"})
    rechazar = cliente.post(f"/console/human-gates/{gate_id}/reject", json={"resolved_by": "h"})

    assert aprobar.status_code == 409 and rechazar.status_code == 409
    (gate,) = cliente.get("/console/human-gates").json()["items"]
    assert gate["status"] == "SUPERSEDED", "ni aprobado ni rechazado: la historia no se reescribe"


def test_i_2_un_gate_obsoleto_aun_pendiente_no_se_decide_por_una_carrera(tmp_path: Path) -> None:
    """Si el tablero aún lo muestra pendiente, aprobar/rechazar reconcilia antes y lo impide."""
    target, _client, tarea = _tarea_con_gate_pendiente(tmp_path)
    cliente, _a, _t, deps = _app(target=_satisfacer_el_repo(target), respuestas=[_plan()])
    gate_id = tarea["gates"][0]
    # simula la carrera: la tarea ya tiene el resultado posterior pero nadie reconcilió aún
    cliente.post(f"/console/tasks/{tarea['task_id']}/run")
    deps.gates.get(__import__("uuid").UUID(gate_id)).status = ApprovalStatus.PENDING
    deps.gates.get(__import__("uuid").UUID(gate_id)).resolved_at = None

    respuesta = cliente.post(f"/console/human-gates/{gate_id}/approve", json={"resolved_by": "h"})

    assert respuesta.status_code == 409 and "es historia" in respuesta.text
    assert deps.gates.get(__import__("uuid").UUID(gate_id)).status is ApprovalStatus.SUPERSEDED


# ================================== H · idempotencia y concurrencia del gate de publicación
def test_h_pedir_el_gate_de_publicacion_es_idempotente_y_concurrente_no_duplica(
    tmp_path: Path,
) -> None:
    client, _deps, repo, _remoto, tarea = _noop(tmp_path)
    url = f"/console/tasks/{tarea['task_id']}/production-gate"

    with ThreadPoolExecutor(max_workers=8) as pool:
        respuestas = list(pool.map(lambda _i: client.post(url), range(8)))

    assert all(item.status_code == 200 for item in respuestas), [r.text for r in respuestas]
    assert len(_gates_de_publicacion(client)) == 1, "una operación/Task/estado = un solo gate"
    assert client.post(url).status_code == 200
    (gate,) = _gates_de_publicacion(client)
    assert gate["destination"]["publishable_sha"] == _head(repo)
    assert client.get(f"/console/tasks/{tarea['task_id']}").json()["pending_gates"] == [
        gate["approval_id"]
    ]


def test_h_2_un_gate_de_publicacion_de_otro_sha_se_supera_y_se_abre_el_vigente(
    tmp_path: Path,
) -> None:
    """Un intento posterior verifica otro artefacto: el gate del SHA anterior pasa a historial."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _t, _d = _app(target=target, respuestas=[_plan(), _cambio()])
    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    client.post(f"/console/tasks/{tarea['task_id']}/production-gate")
    (primero,) = _gates_de_publicacion(client)

    # el repositorio avanza (otro commit) y un intento posterior verifica el nuevo estado
    (repo / "src" / "lib" / "otro.ts").write_text("export const OTRO = 1;\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=f", "-c", "user.email=f@p.local", "commit", "-m", "el HEAD avanza")
    cliente, _a, _t2, _d2 = _app(target=_target(repo, remoto=remoto), respuestas=[_plan()])
    reanudada = cliente.post(f"/console/tasks/{tarea['task_id']}/run")
    assert reanudada.status_code == 200, reanudada.text
    nuevo_sha = reanudada.json()["development"]["publishable_sha"]
    assert nuevo_sha and nuevo_sha != primero["destination"]["publishable_sha"]

    cliente.post(f"/console/tasks/{tarea['task_id']}/production-gate")
    gates = _gates_de_publicacion(cliente)

    estados = {item["approval_id"]: item["status"] for item in gates}
    assert estados[primero["approval_id"]] == "SUPERSEDED"
    vigentes = [item for item in gates if item["actionable"]]
    assert len(vigentes) == 1
    assert vigentes[0]["destination"]["publishable_sha"] == nuevo_sha


# ===================== J · la Task 2e7822a0 con su estado durable real
def _sembrar_estado_de_la_task(repo: Path) -> dict[str, Any]:
    """Escribe el estado durable REAL de la Task (fixture), con el HEAD del repo de prueba."""
    documento: dict[str, Any] = json.loads(FIXTURE_TASK_2E7822A0.read_text(encoding="utf-8"))
    (tarea,) = documento["tasks"]
    tarea["result"]["no_op_evidence"]["baseline_sha"] = _head(repo)
    tarea["result"]["branch"] = "ai/console-fixture"  # la rama de trabajo del destino de la prueba
    default_console_state_path().write_text(json.dumps(documento), encoding="utf-8")
    return documento


def test_j_la_task_2e7822a0_proyecta_solo_la_siguiente_accion_humana_vigente(
    tmp_path: Path,
) -> None:
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    remoto_antes = _remote_main(remoto)
    documento = _sembrar_estado_de_la_task(repo)
    task_id = documento["tasks"][0]["task_id"]
    assert [g["status"] for g in documento["gates"]] == ["PENDING", "PENDING"], "estado real"
    client, audit, _t, _d = _app(target=_target(repo, remoto=remoto), respuestas=[])

    tablero = client.get("/console/human-gates").json()
    tarea = client.get(f"/console/tasks/{task_id}").json()

    # Historial intacto y separado; nada accionable heredado.
    assert tablero["operational"] == [] and tablero["pending"] == 0
    assert {g["action"]: g["status"] for g in tablero["history"]} == {
        "PLAN_REQUIRES_HUMAN": "SUPERSEDED",
        "EVIDENCE_REQUIRED": "SUPERSEDED",
    }
    assert {g["superseded_by"] for g in tablero["history"]} == {"intento 13"}
    assert all(g["actionable"] is False and g["reason"] for g in tablero["history"])
    assert len(tarea["gates"]) == 2 and tarea["pending_gates"] == []
    assert len(tarea["attempts"]) == 11, "los intentos originales se conservan"
    eventos = audit.by_type(
        __import__("punto.schemas.audit", fromlist=["x"]).AuditEventType.HUMAN_GATE_SUPERSEDED
    )
    assert len(eventos) == 2

    # La única acción vigente: pedir la aprobación de producción del SHA exacto verificado.
    assert tarea["stage"] == "DEVELOPMENT_COMPLETED"
    assert tarea["development"]["publishable_sha"] == _head(repo)
    assert tarea["development"]["publishable_source"] == "legacy-baseline"
    assert tarea["next_human_action"]["kind"] == "request_production_gate"
    assert tarea["next_human_action"]["publishable_sha"] == _head(repo)

    # Al pedirla aparece deploy_production ligado a ese SHA, una sola vez.
    assert client.post(f"/console/tasks/{task_id}/production-gate").status_code == 200
    client.post(f"/console/tasks/{task_id}/production-gate")
    (gate,) = _gates_de_publicacion(client)
    assert gate["actionable"] is True and gate["destination"]["publishable_sha"] == _head(repo)
    assert client.get("/console/human-gates").json()["pending"] == 1

    # Un reinicio conserva exactamente esa proyección.
    otro, _audit = _reiniciar(_target(repo, remoto=remoto))
    de_nuevo = otro.get("/console/human-gates").json()
    assert [item["action"] for item in de_nuevo["operational"]] == ["deploy_production"]
    assert len(de_nuevo["history"]) == 2 and de_nuevo["pending"] == 1
    assert _remote_main(remoto) == remoto_antes, "nada se publicó"


def test_j_3_con_el_sobre_del_destino_no_hace_falta_decision_humana(tmp_path: Path) -> None:
    """Con la autoridad persistente que cubre release, la acción es el release autónomo (AUTO)."""
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    remoto_antes = _remote_main(remoto)
    documento = _sembrar_estado_de_la_task(repo)
    task_id = documento["tasks"][0]["task_id"]
    destino = replace(
        _target(repo, remoto=remoto),
        authority=_autoridad_completa(),
        scope_roots=("src", "tests"),
    )
    client, _audit, _t, _d = _app(target=destino, respuestas=[])

    tarea = client.get(f"/console/tasks/{task_id}").json()

    assert tarea["release"]["disposition"] == "AUTO", tarea["release"]["reasons"]
    assert tarea["next_human_action"]["kind"] == "release_autonomous"
    assert tarea["next_human_action"]["requires_human_decision"] is False
    assert tarea["pending_gates"] == [] and _remote_main(remoto) == remoto_antes
    # el release autónomo publica exactamente el estado verificado (solo si alguien lo dispara)
    publicada = client.post(f"/console/tasks/{task_id}/release")
    assert publicada.status_code == 200, publicada.text
    assert publicada.json()["publication"]["commit_sha"] == _head(repo)
    assert _remote_main(remoto) == _head(repo)


def test_j_2_el_fixture_del_estado_real_describe_lo_que_dice_el_encargo() -> None:
    """La Task real: intento 13 ALREADY_SATISFIED, sin commit, con los dos gates históricos."""
    documento = json.loads(FIXTURE_TASK_2E7822A0.read_text(encoding="utf-8"))
    (tarea,) = documento["tasks"]
    resultado = tarea["result"]

    assert tarea["task_id"].startswith("2e7822a0") and tarea["stage"] == "DEVELOPMENT_COMPLETED"
    assert resultado["resolution"] == "ALREADY_SATISFIED" and resultado["commit_sha"] == ""
    assert tarea["attempts"][-1]["run"] == 13 or len(tarea["attempts"]) >= 1
    assert resultado["functional_chain_result"] == "VERIFIED"
    assert {g["action"] for g in documento["gates"]} == {"PLAN_REQUIRES_HUMAN", "EVIDENCE_REQUIRED"}


_ = TARGET_ID
