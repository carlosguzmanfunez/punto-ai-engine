"""EVIDENCE MODALITY ROUTING — un criterio se demuestra con la modalidad que le corresponde.

Causa real (Task 0983a418): «Unificar los tipos de propiedad en una sola fuente de verdad ...
entre filtros, formularios, validaciones y visualizaciones de propiedades» se clasificaba como
``VISUAL_APPEARANCE`` porque la frase contiene la palabra «visualizaciones» — una coincidencia de
subcadena, no un juicio real sobre el aspecto. PUNTO agotaba el presupuesto de evidencia VISUAL
(``EXPAND_FRAMING``) sobre un criterio que ninguna captura puede demostrar: una fuente canónica,
sus consumidores y la ausencia de definiciones paralelas son hechos ESTRUCTURALES, no visuales.

La corrección de causa raíz es la CLASIFICACIÓN: ``STRUCTURAL_CONSISTENCY`` (nueva) se comprueba
ANTES que ``VISUAL_APPEARANCE`` en ``extract_claims`` y se verifica con análisis estático
(``punto.structure``), nunca con VISUAL_QA — así que un criterio estructural JAMÁS entra en el
bucle de recuperación visual: no hay presupuesto que agotar en una modalidad incapaz.

    pytest tests/test_evidence_modality_routing.py -q
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.acceptance import (
    ClaimKind,
    EvidenceModality,
    extract_claims,
    modality_of,
    verify_claims,
)
from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.dashboard import register_dashboard
from punto.api.gate_reconciliation import assess_gate
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.contract import ProviderRole
from punto.providers.router import ProviderRouter
from punto.schemas.audit import AuditEventType
from punto.schemas.decision import ApprovalStatus, HumanApprovalRequest, RiskLevel
from punto.schemas.dev import DevelopmentResult, DevelopmentStatus
from punto.schemas.enums import TaskStatus
from punto.structure import analyze_structural_consistency
from punto.workspace.target import DevelopmentTargetRegistry
from test_human_console import TARGET_ID, _git, _plan, _target
from test_noop_reconciliation import _repo_ya_satisfecho
from test_visual_qa_effective import _Multimodal

#: El criterio real de la Task 0983a418 que disparó el defecto (contiene «visualizaciones»).
CRITERIO_REAL = (
    "Unificar los tipos de propiedad en una sola fuente de verdad para evitar inconsistencias "
    "entre filtros, formularios, validaciones y visualizaciones de propiedades."
)

CRITERIO_SIMPLE = "Unificar los tipos de propiedad en una sola fuente de verdad."


# ================================================== A · clasificación (unidad, la causa raíz)
def test_a_el_criterio_real_se_clasifica_estructural_no_visual() -> None:
    """El defecto real: «visualizaciones» ya no dispara VISUAL_APPEARANCE por subcadena."""
    (claim,) = extract_claims(CRITERIO_REAL)

    assert claim.kind == ClaimKind.STRUCTURAL_CONSISTENCY.value
    assert modality_of(claim.kind) == EvidenceModality.STRUCTURAL.value
    assert claim.capability == "", "lo estructural no exige capacidad VISION: no necesita mirar"


def test_a2_satisfied_sin_ninguna_captura(tmp_path: Path) -> None:
    """SATISFIED se produce con análisis estático puro, sin visual ni atestación humana."""
    (claim,) = extract_claims(CRITERIO_SIMPLE)
    files = {
        "src/lib/tipos.ts": "export const TIPOS_PROPIEDAD = ['Casa', 'Apartamento'];\n",
    }

    evidencia = analyze_structural_consistency(
        claim.sentence,
        files=list(files),
        read_text=lambda p: files[p],
        exists=lambda p: p in files,
    )
    (registro,) = verify_claims([claim], structural={claim.sentence: evidencia})

    assert registro.satisfied
    assert registro.evidence_class == "SATISFIED"


# ==================================== E · un criterio realmente visual sigue siendo visual
def test_e_un_criterio_realmente_visual_conserva_la_ruta_visual() -> None:
    """No es una regresión general: «se integra visualmente» sigue VISUAL_APPEARANCE."""
    (claim,) = extract_claims("El mapa se integra visualmente con el diseño actual del sitio.")

    assert claim.kind == ClaimKind.VISUAL_APPEARANCE.value
    assert modality_of(claim.kind) == EvidenceModality.VISUAL.value
    assert claim.capability == "VISION"


# ==================================== F · lo estructural no exige una modalidad que no necesita
def test_f_lo_estructural_no_exige_capacidad_de_modelo() -> None:
    """Solo se piden las modalidades necesarias: estructural no arrastra un requisito visual."""
    from punto.acceptance import capability_requirements

    (claim,) = extract_claims(CRITERIO_REAL)
    (requisito,) = capability_requirements([claim])

    assert requisito.capability == ""
    assert requisito.available is True


# ==================================== D (unidad) · duplicación detectada, estructural FAILED
def test_d_unidad_duplicacion_detectada_es_failed_no_inconclusive() -> None:
    """Dos fuentes candidatas: FAILED determinista, nunca INCONCLUSIVE (no es subjetivo)."""
    (claim,) = extract_claims(CRITERIO_SIMPLE)
    files = {
        "src/lib/tipos.ts": "export const TIPOS = ['Casa'];\n",
        "src/lib/tipos_legacy.ts": "export const TIPOS = ['Legacy'];\n",
    }
    evidencia = analyze_structural_consistency(
        claim.sentence,
        files=list(files),
        read_text=lambda p: files[p],
        exists=lambda p: p in files,
    )
    (registro,) = verify_claims([claim], structural={claim.sentence: evidencia})

    assert registro.unsatisfied
    assert registro.evidence_class == "FAILED"
    assert "2" in registro.evidence or "tipos_legacy" in registro.evidence


# ======================================================== ciclo + consola (integración real)
def _consola_estructural(
    tmp_path: Path,
    *,
    architect_plan: dict[str, Any],
    builder_script: list[Any],
    extra_setup: Any = None,
) -> tuple[TestClient, AuditLogger, Path, Any]:
    """Consola real con ``DevelopmentCycle`` real. ``extra_setup(repo)`` corre y se comitea ANTES
    de fijar ``baseline_sha``: el ciclo tiene que empezar sobre el árbol final, no uno anterior.
    """
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    if extra_setup is not None:
        extra_setup(repo)
        _git(repo, "add", "-A")
        _git(
            repo,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@punto.local",
            "commit",
            "-m",
            "preparación adicional del fixture",
        )
    target = _target(repo, remoto=remoto)
    audit = AuditLogger()
    architect = _Multimodal("architect", "architect-1", lambda n: json.dumps(architect_plan))
    cola = list(builder_script)

    def builder_responde(n: int) -> str:
        paso = cola.pop(0) if cola else builder_script[-1]
        return json.dumps(paso)

    builder = _Multimodal("anthropic", "claude-sonnet-5", builder_responde)
    router = ProviderRouter()
    for cliente in (architect, builder):
        router.register_provider(cliente.provider, lambda _m, c=cliente: c, model=cliente.model)
    router.assign_role(ProviderRole.ARCHITECT, "architect")
    router.assign_role(ProviderRole.BUILDER, "anthropic")
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
    return TestClient(application), audit, repo, target


def _solicitud(criterio: str) -> dict[str, Any]:
    return {
        "objective": criterio,
        "target_id": TARGET_ID,
        "acceptance_criteria": [criterio],
        "scope_paths": ["src"],
    }


# =============================== C · fuente canónica + consumidores + verificación → SATISFIED
def test_c_fuente_canonica_y_consumidores_reales_completan_sin_gate(tmp_path: Path) -> None:
    """El criterio real de 0983a418: con consumidores reales, se completa solo — sin captura."""

    def _con_consumidores(repo: Path) -> None:
        (repo / "src" / "components").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "components" / "Filtros.tsx").write_text(
            "import { TIPOS } from '@/lib/tipos';\n"
            "export function Filtros() { return TIPOS.length; }\n",
            encoding="utf-8",
        )
        (repo / "src" / "components" / "Formulario.tsx").write_text(
            "import { TIPOS } from '../lib/tipos';\n"
            "export function Formulario() { return TIPOS; }\n",
            encoding="utf-8",
        )
        (repo / "src" / "components" / "Validaciones.tsx").write_text(
            "import { TIPOS } from '../lib/tipos';\nexport function valida() { return TIPOS; }\n",
            encoding="utf-8",
        )
        (repo / "src" / "components" / "Visualizaciones.tsx").write_text(
            "import { TIPOS } from '../lib/tipos';\n"
            "export function Visualizaciones() { return TIPOS; }\n",
            encoding="utf-8",
        )

    client, audit, _repo, _target_obj = _consola_estructural(
        tmp_path,
        architect_plan=_plan(),
        builder_script=[{"changes": []}],
        extra_setup=_con_consumidores,
    )

    tarea = client.post("/console/tasks", json=_solicitud(CRITERIO_REAL)).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    assert tarea["gates"] == [], "un criterio estructural satisfecho no pide una persona"
    assert tarea["development"]["claims_result"] == "SATISFIED"
    eventos = audit.by_type(AuditEventType.DEV_CLAIMS_EVALUATED)
    (metadata,) = [dict(e.metadata) for e in eventos]
    assert audit.by_type(AuditEventType.DEV_VISUAL_CAPTURED) == (), "nunca se capturó pantalla"
    assert audit.by_type(AuditEventType.DEV_VISUAL_ASSESSED) == (), "VISUAL_QA nunca se consultó"
    # J · el grafo reconstruye Criterion -> Modality -> Evidence desde la misma auditoría.
    (estructural,) = metadata["structural"]
    estructural = dict(estructural)
    assert list(estructural["canonical"]) == ["src/lib/tipos.ts"]
    assert set(estructural["consumers_confirmed"]) == {
        "filtros",
        "formularios",
        "validaciones",
        "visualizaciones de propiedades",
    }


# ==================================== D · duplicación real → FAILED → repara → SATISFIED
def test_d_duplicacion_real_repara_y_reevalua_hasta_satisfied(tmp_path: Path) -> None:
    """Dos fuentes reales en el repo: PUNTO repara la duplicación dentro del MISMO ciclo."""
    plan = _plan()
    plan["files_to_modify"] = ["src/lib/tipos.ts", "src/lib/tipos_legacy.ts"]
    plan["summary"] = "retirar la fuente duplicada de tipos"
    reparacion = {
        "summary": "reexportar desde la fuente canónica",
        "root_cause": "dos ficheros declaran una constante TIPOS: no hay una única fuente canónica",
        "evidence": ["src/lib/tipos.ts y src/lib/tipos_legacy.ts exportan TIPOS por separado"],
        "expected_effect": "el criterio de consistencia estructural pasa a SATISFIED",
        "changes": [
            {
                "path": "src/lib/tipos_legacy.ts",
                "operation": "MODIFY",
                "content": "export { TIPOS } from './tipos';\n",
                "reason": "fuente duplicada: reexporta la canónica en vez de declarar la suya",
                "acceptance_criterion": CRITERIO_SIMPLE,
            }
        ],
    }

    def _con_duplicado(repo: Path) -> None:
        (repo / "src" / "lib" / "tipos_legacy.ts").write_text(
            "export const TIPOS = ['Duplicado'];\n", encoding="utf-8"
        )

    client, audit, repo, _target_obj = _consola_estructural(
        tmp_path,
        architect_plan=plan,
        builder_script=[{"changes": []}, reparacion],
        extra_setup=_con_duplicado,
    )

    tarea = client.post("/console/tasks", json=_solicitud(CRITERIO_SIMPLE)).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    assert tarea["gates"] == [], "la reparación ya autorizada no pidió una persona"
    assert tarea["development"]["claims_result"] == "SATISFIED"
    contenido = (repo / "src" / "lib" / "tipos_legacy.ts").read_text(encoding="utf-8")
    assert "export const TIPOS" not in contenido, "la fuente duplicada dejó de declarar la suya"
    # La duplicación sí forzó una reparación real dentro del MISMO ciclo: dos evaluaciones reales
    # de la misma afirmación estructural, FAILED y después SATISFIED — no una reparación supuesta.
    eventos = [
        dict(e.metadata)["result"] for e in audit.by_type(AuditEventType.DEV_CLAIMS_EVALUATED)
    ]
    assert eventos == ["FAILED", "SATISFIED"], "reevidencia real tras la reparación, no supuesta"


# ======================================= G · el presupuesto no se gasta en modalidad incapaz
def test_g_no_se_gasta_presupuesto_de_evidencia_visual_en_un_criterio_estructural(
    tmp_path: Path,
) -> None:
    """Con presupuesto amplio, un criterio estructural no consume ni un intento visual."""
    client, audit, _repo, _target_obj = _consola_estructural(
        tmp_path, architect_plan=_plan(), builder_script=[{"changes": []}]
    )

    tarea = client.post("/console/tasks", json=_solicitud(CRITERIO_SIMPLE)).json()

    assert tarea["development"]["evidence_attempts"] in (0, 1), (
        "el criterio estructural se resuelve en el mismo intento: no hay recuperación visual"
    )
    assert audit.by_type(AuditEventType.DEV_CLAIMS_EVALUATED)
    assert audit.by_type(AuditEventType.DEV_VISUAL_CAPTURED) == (), "nunca se capturó pantalla"
    assert audit.by_type(AuditEventType.DEV_VISUAL_ASSESSED) == (), "VISUAL_QA nunca se consultó"


# =========================================== H · reinicio conserva la evidencia estructural
def test_h_reinicio_conserva_la_evidencia_estructural_y_el_resultado(tmp_path: Path) -> None:
    """El estado durable ya persiste ``ClaimRecord``/``DevelopmentResult``: se conservan."""
    client, _audit, _repo, _target_obj = _consola_estructural(
        tmp_path / "montaje-1", architect_plan=_plan(), builder_script=[{"changes": []}]
    )
    tarea = client.post("/console/tasks", json=_solicitud(CRITERIO_SIMPLE)).json()
    task_id = tarea["task_id"]

    otro, _audit2, _repo2, _target2 = _consola_estructural(
        tmp_path / "montaje-2", architect_plan=_plan(), builder_script=[{"changes": []}]
    )
    recuperada = otro.get(f"/console/tasks/{task_id}").json()

    assert recuperada["task_id"] == task_id and recuperada["recovered"] is True
    assert recuperada["development"]["claims_result"] == tarea["development"]["claims_result"]


# ===================================== I · un gate resuelto por lo estructural es SUPERSEDED
def test_i_un_gate_de_evidencia_pendiente_queda_superseded_nunca_aprobado() -> None:
    """La reconciliación genérica (no nueva) también resuelve el caso de esta cadena: SUPERSEDED."""
    from types import SimpleNamespace

    gate = HumanApprovalRequest(
        task_id=__import__("uuid").uuid4(),
        action="EVIDENCE_REQUIRED",
        risk=RiskLevel.HIGH,
        reason="EVIDENCE_REQUIRED: el criterio estructural no se pudo demostrar con lo visual",
        resume_status=TaskStatus.IN_PROGRESS.value,
    )
    attempt = SimpleNamespace(run=2, started_at=gate.requested_at)
    resultado = DevelopmentResult(
        status=DevelopmentStatus.COMPLETED,
        claims_result="SATISFIED",
        claims=(
            {
                "sentence": CRITERIO_REAL,
                "kind": "STRUCTURAL_CONSISTENCY",
                "result": "SATISFIED",
            },
        ),
    )
    task = SimpleNamespace(
        lineage_status="ACTIVE",
        result=resultado,
        attempts=[attempt],
        executing=False,
        publication=None,
    )

    veredicto = assess_gate(task, gate)

    assert veredicto.actionable is False
    assert "SATISFIED" in veredicto.cause
    assert gate.status is ApprovalStatus.PENDING, "el veredicto no muta el gate: la consola decide"
