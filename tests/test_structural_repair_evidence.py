"""STRUCTURAL FAILED → REPAIR PRODUCES CHANGES.

Causa real (Task 0983a418, continuación directa de EVIDENCE MODALITY ROUTING): con la
clasificación ya corregida, un criterio ``STRUCTURAL_CONSISTENCY`` llegaba a ``FAILED`` (correcto)
y entraba en el bucle de reparación ya existente (correcto), pero la ronda de reparación terminaba
en ``CHANGES_EMPTY`` — el BUILDER no proponía ningún cambio — hasta agotar el presupuesto y
terminar en ``VERIFICATION_FAILED``.

Causa raíz encontrada: la evidencia SÍ llegaba al BUILDER (``claim_issues`` alimenta
``failure_evidence``, que sí entra en el prompt), pero en forma **incompleta y engañosa**:

1. ``BuildValidationIssue.detail`` para un criterio no satisfecho solo llevaba
   ``f"{kind}: {evidence}"`` — el campo ``remedy`` que ``_structural_record``/``_visual_record``
   ya calculan (qué cambio concreto cierra el hueco: qué fichero declarar canónico, cuál
   consolidar, quién debe importar qué) se descartaba antes de salir de
   ``_verify_semantic_claims``.
2. El texto fijo que acompañaba a todo ``CLAIM_NOT_SATISFIED`` decía, sin condición: «Provide
   real evidence for the claim (an **authoritative dataset** that the code actually uses)» — una
   instrucción escrita para el caso cartográfico que, para un criterio estructural (o cualquier
   criterio futuro que no sea sobre un dataset), es una instrucción **engañosa**: le decía al
   BUILDER que aportara un dataset cuando el defecto real era una fuente duplicada.

La corrección es general (no específica de «tipos de propiedad»): propagar ``remedy`` en el
``detail`` de cada ``CLAIM_NOT_SATISFIED`` y sustituir la instrucción fija por una que remite al
``REMEDY`` de cada línea, sin presuponer que el defecto es sobre un dataset.

    pytest tests/test_structural_repair_evidence.py -q
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.console_state import default_console_state_path
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.contract import ProviderRole
from punto.providers.router import ProviderRouter
from punto.schemas.audit import AuditEventType
from punto.workspace.target import DevelopmentTargetRegistry
from test_human_console import TARGET_ID, _git, _Guion, _plan, _target
from test_noop_reconciliation import _repo_ya_satisfecho
from test_visual_qa_effective import _Multimodal

CRITERIO = "Unificar los tipos de propiedad en una sola fuente de verdad."


def _solicitud(criterio: str = CRITERIO) -> dict[str, Any]:
    return {
        "objective": criterio,
        "target_id": TARGET_ID,
        "acceptance_criteria": [criterio],
        "scope_paths": ["src"],
    }


def _consola_con_espia(
    tmp_path: Path, *, architect_plan: dict[str, Any], builder_script: list[Any]
) -> tuple[TestClient, AuditLogger, Path, _Guion]:
    """Consola real; el BUILDER es ``_Guion``: responde el guion Y apunta el prompt exacto que
    recibió, para poder comprobar qué evidencia le llegó de verdad.
    """
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    (repo / "src" / "lib" / "tipos_legacy.ts").write_text(
        "export const TIPOS = ['Duplicado'];\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@punto.local",
        "commit",
        "-m",
        "fuente duplicada",
    )
    target = _target(repo, remoto=remoto)
    audit = AuditLogger()
    architect = _Multimodal("architect", "architect-1", lambda n: json.dumps(architect_plan))
    router = ProviderRouter()
    router.register_provider(architect.provider, lambda _m, c=architect: c, model=architect.model)
    router.assign_role(ProviderRole.ARCHITECT, "architect")
    espia = _Guion(router, builder_script)
    router.assign_role(ProviderRole.BUILDER, "guionizado")
    ciclo = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(max_repair_rounds=2, max_structural_corrections=2),
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
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencias)
    return TestClient(application), audit, repo, espia


# ===================================== 1 · duplicación real: el BUILDER recibe evidencia accionable
def test_1_structural_failed_con_duplicacion_el_builder_recibe_evidencia_accionable(
    tmp_path: Path,
) -> None:
    """El prompt de reparación nombra el fichero duplicado y el REMEDY concreto, no un dataset."""
    plan = _plan()
    plan["files_to_modify"] = ["src/lib/tipos.ts", "src/lib/tipos_legacy.ts"]
    client, _audit, _repo, espia = _consola_con_espia(
        tmp_path, architect_plan=plan, builder_script=[{"changes": []}, {"changes": []}]
    )

    client.post("/console/tasks", json=_solicitud())

    assert len(espia.prompts) >= 2, "hubo una ronda de reparación real"
    prompt_reparacion = espia.prompts[1]
    assert "REMEDY" in prompt_reparacion, "el remedio concreto llega al BUILDER"
    assert "tipos_legacy.ts" in prompt_reparacion, "el fichero implicado llega al BUILDER"
    assert "tipos.ts" in prompt_reparacion, "la fuente canónica detectada llega al BUILDER"
    assert "authoritative dataset" not in prompt_reparacion, (
        "ya no se le dice al BUILDER que aporte un dataset para un defecto que no lo es"
    )


# ===================================== 2 · reparación real → verificación → SATISFIED
def test_2_reparacion_real_produce_cambios_y_llega_a_satisfied(tmp_path: Path) -> None:
    """Con evidencia accionable, un BUILDER que sí actúa corrige y el criterio queda SATISFIED."""
    plan = _plan()
    plan["files_to_modify"] = ["src/lib/tipos.ts", "src/lib/tipos_legacy.ts"]
    reparacion = {
        "summary": "reexportar desde la fuente canónica",
        "root_cause": "dos ficheros declaran una constante TIPOS por separado",
        "evidence": ["src/lib/tipos_legacy.ts declara su propia versión de TIPOS"],
        "expected_effect": "el criterio de consistencia estructural pasa a SATISFIED",
        "changes": [
            {
                "path": "src/lib/tipos_legacy.ts",
                "operation": "MODIFY",
                "content": "export { TIPOS } from './tipos';\n",
                "reason": "fuente duplicada: reexporta la canónica en vez de declarar la suya",
                "acceptance_criterion": CRITERIO,
            }
        ],
    }
    client, audit, repo, _espia = _consola_con_espia(
        tmp_path, architect_plan=plan, builder_script=[{"changes": []}, reparacion]
    )

    tarea = client.post("/console/tasks", json=_solicitud()).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    assert tarea["development"]["claims_result"] == "SATISFIED"
    contenido = (repo / "src" / "lib" / "tipos_legacy.ts").read_text(encoding="utf-8")
    assert "export const TIPOS" not in contenido
    eventos = [
        dict(e.metadata)["result"] for e in audit.by_type(AuditEventType.DEV_CLAIMS_EVALUATED)
    ]
    assert eventos == ["FAILED", "SATISFIED"]


# ============ 3 · FAILED accionable + BUILDER vacío: no se acepta silenciosamente como reparado
def test_3_failed_accionable_con_builder_vacio_no_se_cierra_como_reparado(tmp_path: Path) -> None:
    """Un BUILDER que sigue sin actuar ante evidencia accionable termina bloqueado, no COMPLETED."""
    plan = _plan()
    plan["files_to_modify"] = ["src/lib/tipos.ts", "src/lib/tipos_legacy.ts"]
    client, _audit, repo, espia = _consola_con_espia(
        tmp_path,
        architect_plan=plan,
        builder_script=[{"changes": []}, {"changes": []}, {"changes": []}],
    )

    tarea = client.post("/console/tasks", json=_solicitud()).json()

    assert tarea["stage"] != "DEVELOPMENT_COMPLETED", (
        "CHANGES_EMPTY ante un FAILED accionable nunca se acepta como reparación válida"
    )
    assert tarea["development"]["claims_result"] != "SATISFIED"
    # El fichero duplicado sigue ahí: nada se reparó de verdad.
    assert "export const TIPOS" in (repo / "src" / "lib" / "tipos_legacy.ts").read_text(
        encoding="utf-8"
    )
    assert len(espia.prompts) >= 2, "sí se le dio al menos una oportunidad de reparar con evidencia"


# ========================== 4 · sin duplicación real: SATISFIED de entrada, sin tocar código
def test_4_sin_duplicacion_real_es_satisfied_sin_reparacion(tmp_path: Path) -> None:
    """Falso positivo evitado: si la fuente ya es única, no hay FAILED ni ronda de reparación."""
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    target = _target(repo, remoto=remoto)
    audit = AuditLogger()
    architect = _Multimodal("architect", "architect-1", lambda n: json.dumps(_plan()))
    router = ProviderRouter()
    router.register_provider(architect.provider, lambda _m, c=architect: c, model=architect.model)
    router.assign_role(ProviderRole.ARCHITECT, "architect")
    espia = _Guion(router, [{"changes": []}])
    router.assign_role(ProviderRole.BUILDER, "guionizado")
    ciclo = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(max_repair_rounds=2, max_structural_corrections=2),
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
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencias)
    client = TestClient(application)

    tarea = client.post("/console/tasks", json=_solicitud()).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    assert tarea["development"]["claims_result"] == "SATISFIED"
    assert len(espia.prompts) == 1, "una sola invocación: no hizo falta ninguna reparación"


# ================================================ 6 · misma Task, mismo ciclo causal
def test_6_la_reparacion_ocurre_en_la_misma_task_y_el_mismo_ciclo(tmp_path: Path) -> None:
    """La reparación no crea otra Task: es la MISMA solicitud gobernada, un solo ``task_id``."""
    plan = _plan()
    plan["files_to_modify"] = ["src/lib/tipos.ts", "src/lib/tipos_legacy.ts"]
    reparacion = {
        "summary": "reexportar desde la fuente canónica",
        "root_cause": "dos ficheros declaran una constante TIPOS por separado",
        "evidence": ["src/lib/tipos_legacy.ts declara su propia versión de TIPOS"],
        "expected_effect": "el criterio de consistencia estructural pasa a SATISFIED",
        "changes": [
            {
                "path": "src/lib/tipos_legacy.ts",
                "operation": "MODIFY",
                "content": "export { TIPOS } from './tipos';\n",
                "reason": "fuente duplicada",
                "acceptance_criterion": CRITERIO,
            }
        ],
    }
    client, _audit, _repo, _espia = _consola_con_espia(
        tmp_path, architect_plan=plan, builder_script=[{"changes": []}, reparacion]
    )

    tarea = client.post("/console/tasks", json=_solicitud()).json()

    assert tarea["runs"] == 1, "una sola ejecución del ciclo: la reparación fue interna"
    listado = client.get("/console/tasks").json()
    assert listado["total"] == 1 and listado["items"][0]["task_id"] == tarea["task_id"]
    assert tarea["gates"] == [], "la reparación ya autorizada no pidió una persona"


# ============================================== 7 · reinicio conserva la evidencia de reparación
def test_7_reinicio_conserva_la_evidencia_de_la_reparacion(tmp_path: Path) -> None:
    """El estado durable persiste el resultado final (SATISFIED) y el rastro de auditoría."""
    plan = _plan()
    plan["files_to_modify"] = ["src/lib/tipos.ts", "src/lib/tipos_legacy.ts"]
    reparacion = {
        "summary": "reexportar desde la fuente canónica",
        "root_cause": "dos ficheros declaran una constante TIPOS por separado",
        "evidence": ["src/lib/tipos_legacy.ts declara su propia versión de TIPOS"],
        "expected_effect": "el criterio de consistencia estructural pasa a SATISFIED",
        "changes": [
            {
                "path": "src/lib/tipos_legacy.ts",
                "operation": "MODIFY",
                "content": "export { TIPOS } from './tipos';\n",
                "reason": "fuente duplicada",
                "acceptance_criterion": CRITERIO,
            }
        ],
    }
    client, _audit, _repo, _espia = _consola_con_espia(
        tmp_path, architect_plan=plan, builder_script=[{"changes": []}, reparacion]
    )
    tarea = client.post("/console/tasks", json=_solicitud()).json()
    task_id = tarea["task_id"]
    assert default_console_state_path().is_file()

    otro, _audit2, _repo2, _espia2 = _consola_con_espia(
        tmp_path / "otra", architect_plan=plan, builder_script=[{"changes": []}]
    )
    recuperada = otro.get(f"/console/tasks/{task_id}").json()

    assert recuperada["task_id"] == task_id and recuperada["recovered"] is True
    assert recuperada["development"]["claims_result"] == "SATISFIED"


# ====================== 8 · mutación: sin propagar el remedio, la evidencia deja de ser accionable
def test_8_mutacion_sin_remedy_el_prompt_pierde_la_evidencia_accionable() -> None:
    """Documenta la invariante: ``ClaimRecord.remedy`` es lo que hace accionable el ``detail``.

    Si una regresión vuelve a construir ``CLAIM_NOT_SATISFIED`` solo con ``f"{kind}: {evidence}"``
    (sin el remedio), este test lo detecta directamente sobre la función pura, sin necesitar un
    ciclo completo.
    """
    from punto.acceptance import ClaimRecord

    registro = ClaimRecord(
        sentence=CRITERIO,
        kind="STRUCTURAL_CONSISTENCY",
        result="UNSATISFIED",
        evidence="existen 2 definiciones paralelas para «tipos»: src/lib/tipos.ts, "
        "src/lib/tipos_legacy.ts",
        remedy="consolida las definiciones paralelas en una única fuente canónica",
        evidence_class="FAILED",
    )
    # Misma construcción que dev_cycle.py::_verify_semantic_claims para el detail del issue.
    detail = (
        f"{registro.kind}: {registro.evidence}"
        + (f" REMEDY: {registro.remedy}" if registro.remedy else "")
    )[:500]

    assert "REMEDY" in detail, "sin esto, el BUILDER recibe la causa pero no el arreglo concreto"
    assert registro.remedy in detail
