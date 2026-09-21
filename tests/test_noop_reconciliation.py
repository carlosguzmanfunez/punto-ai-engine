"""Reconciliación de un no-op: cero cambios NO es éxito por sí solo, pero puede serlo verificado.

Defecto real (Task 2e7822a0, intento 11): el objetivo ya estaba materializado en el repositorio, el
BUILDER devolvió cero cambios (``CHANGES_EMPTY``) y el ciclo lo trató como fallo aunque una Task
puede quedar satisfecha de forma idempotente por el estado actual.

Reglas que se fijan aquí, con el ciclo real y un repositorio Git real:

- ``CHANGES_EMPTY`` **nunca** completa por sí solo: el estado actual tiene que superar la misma
  cadena que un cambio normal (verificaciones, cadena funcional, aceptación, afirmaciones y
  evidencia visual por la ruta efectiva de VISUAL_QA);
- si algo falla o falta evidencia (FAIL/UNCLEAR/sin captura), no se completa;
- el éxito se registra como ``ALREADY_SATISFIED`` con la evidencia de qué se midió, **sin** commit
  vacío y sin ampliar autoridad;
- un cambio normal conserva su comportamiento.

    pytest tests/test_noop_reconciliation.py -q
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import (
    DevelopmentConfig,
    DevelopmentCycle,
    _StateEvaluation,
)
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.contract import ProviderRole
from punto.providers.failover import FailoverPolicy
from punto.providers.router import ProviderRouter
from punto.schemas.audit import AuditEventType
from punto.workspace.target import DevelopmentTargetRegistry
from test_human_console import TARGET_ID, _app, _cambio, _git, _plan, _repos, _target
from test_visual_qa_effective import PNG, _CapturaFalsa, _evaluador_visual, _Multimodal

SOLICITUD = {
    "objective": "unificar la lista de tipos en una sola fuente",
    "target_id": TARGET_ID,
    "acceptance_criteria": ["una sola fuente de tipos"],
    "scope_paths": ["src"],
}
CRITERIO_VISUAL = "el mapa se integra visualmente con el diseno actual"


def _repo_ya_satisfecho(tmp_path: Path) -> tuple[Path, Path]:
    """Repositorio donde el objetivo YA está materializado y confirmado (nada que cambiar)."""
    repo, remoto = _repos(tmp_path)
    (repo / "src" / "lib" / "tipos.ts").write_text(
        "export const TIPOS = ['Casa', 'Apartamento'];\n", encoding="utf-8"
    )
    _git(repo, "add", "src/lib/tipos.ts")
    _git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@punto.local",
        "commit",
        "-m",
        "el objetivo ya estaba materializado",
    )
    return repo, remoto


def _commits(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "--all"))


# ============================================ 1 · cero cambios + criterios satisfechos → no-op
def test_1_cero_cambios_con_el_estado_ya_satisfecho_completa_como_no_op_verificado(
    tmp_path: Path,
) -> None:
    """La Task se completa como ``ALREADY_SATISFIED``: mismas verificaciones, sin commit vacío."""
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    antes = _commits(repo)
    client, audit, _t, _d = _app(target=_target(repo, remoto=remoto), respuestas=[_plan()])

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    desarrollo = tarea["development"]
    assert desarrollo["status"] == "DEVELOPMENT_COMPLETED"
    assert desarrollo["resolution"] == "ALREADY_SATISFIED"
    assert desarrollo["commit_sha"] == "", "no se fabrica un commit vacío"
    assert desarrollo["applied"] == [] and desarrollo["error_kind"] == ""
    assert _commits(repo) == antes, "el repositorio no recibió ningún commit"
    evidencia = desarrollo["no_op_evidence"]
    assert evidencia["verifications"] == ["focused"], "se ejecutó la misma verificación del destino"
    assert evidencia["functional_chain"] == "VERIFIED"
    assert evidencia["baseline_sha"] and evidencia["state_digest"]
    assert "no propuso cambios" in evidencia["reason"]
    assert tarea["attempts"][0]["resolution"] == "ALREADY_SATISFIED"
    assert tarea["attempts"][0]["commit_sha"] == ""
    (evento,) = audit.by_type(AuditEventType.DEV_NOOP_RECONCILED)
    meta = dict(evento.metadata)
    assert meta["satisfied"] is True and list(meta["verifications_passed"]) == ["focused"]
    assert meta["gap"] == "" and evento.result.value == "SUCCESS"


def test_1b_el_no_op_no_amplia_autoridad_ni_publica(tmp_path: Path) -> None:
    """Sin sobre del destino, el no-op no publica solo ni abre gates: no amplía autoridad."""
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    client, _audit, _t, _d = _app(target=_target(repo, remoto=remoto), respuestas=[_plan()])

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["development"]["published"] is False
    assert tarea["gates"] == [] and client.get("/console/human-gates").json()["pending"] == 0
    assert tarea["release"]["autonomous"] is False, "sin sobre del destino no hay release autónomo"
    assert client.post(f"/console/tasks/{tarea['task_id']}/publish").status_code == 409


# ============================================= 2 · cero cambios + verificación FAIL → fallo cerrado
def test_2_cero_cambios_con_el_estado_sin_satisfacer_falla_cerrado(tmp_path: Path) -> None:
    """Cero cambios y la verificación NO pasa: no completa, se explica y se mide una sola vez."""
    repo, remoto = _repos(tmp_path)  # tipos.ts SIN 'Apartamento': el objetivo no está satisfecho
    antes = _commits(repo)
    client, audit, _t, _d = _app(target=_target(repo, remoto=remoto), respuestas=[_plan()])

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] == "DEVELOPMENT_FAILED", tarea["development"]
    desarrollo = tarea["development"]
    assert desarrollo["status"] != "DEVELOPMENT_COMPLETED" and desarrollo["resolution"] == ""
    assert desarrollo["commit_sha"] == "" and _commits(repo) == antes
    assert desarrollo["no_op_evidence"] is None
    (evento,) = audit.by_type(AuditEventType.DEV_NOOP_RECONCILED)  # una vez por estado
    meta = dict(evento.metadata)
    assert meta["satisfied"] is False and "verificaciones fallidas: focused" in meta["gap"]
    assert list(meta["verifications_failed"]) == ["focused"]
    assert evento.result.value == "FAILURE"


def test_2b_con_la_reconciliacion_desactivada_se_conserva_el_comportamiento_anterior(
    tmp_path: Path,
) -> None:
    """``reconcile_noop=False``: ni siquiera un estado satisfecho se da por bueno."""
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    client, audit, _t, deps = _app(target=_target(repo, remoto=remoto), respuestas=[_plan()])
    deps.dev_cycle.config = replace(deps.dev_cycle.config, reconcile_noop=False)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] == "DEVELOPMENT_FAILED"
    assert audit.by_type(AuditEventType.DEV_NOOP_RECONCILED) == ()


# ==================================== 3 · unidades: qué impide dar un no-op por satisfecho
def _ciclo_vacio() -> DevelopmentCycle:
    return DevelopmentCycle(router=ProviderRouter(), targets=DevelopmentTargetRegistry({}))


def _estado(**cambios: Any) -> _StateEvaluation:
    from punto.schemas.dev import CommandEvidence

    verificacion = CommandEvidence(
        name="focused",
        argv=("python", "-c", "pass"),
        exit_code=0,
        duration_ms=1,
        output_excerpt="",
        truncated=False,
        timed_out=False,
        passed=True,
    )
    base: dict[str, Any] = {
        "verification": (verificacion,),
        "chain_ok": True,
        "chain_issues": (),
        "acceptance_ok": True,
        "acceptance_issues": (),
        "acceptance_evidence": (),
        "claims_outcome": "NONE",
        "claim_issues": (),
        "claim_records": (),
    }
    base.update(cambios)
    return _StateEvaluation(**base)


class _Repo:
    def __init__(self, existentes: set[str]) -> None:
        self._existentes = existentes

    def exists(self, path: str) -> bool:
        return path in self._existentes


_PLAN = SimpleNamespace(
    files_to_modify=("src/a.ts",), files_to_create=("src/b.ts",), files_to_delete=("src/c.ts",)
)


def test_3_solo_un_estado_totalmente_verificado_es_un_no_op_aceptable() -> None:
    """Cada condición ausente impide el no-op; solo el conjunto completo lo permite."""
    ciclo = _ciclo_vacio()
    repo = _Repo({"src/a.ts", "src/b.ts"})

    assert ciclo._noop_gap(_estado(), _PLAN, repo) == ""  # type: ignore[arg-type]
    casos = {
        "sin verificaciones": (_estado(verification=()), "no hay verificaciones"),
        "cadena incompleta": (_estado(chain_ok=False), "cadena funcional"),
        "aceptación": (_estado(acceptance_ok=False), "aceptación"),
        "afirmación FAILED": (_estado(claims_outcome="FAILED"), "factuales/semánticos"),
    }
    for nombre, (estado, esperado) in casos.items():
        assert esperado in ciclo._noop_gap(estado, _PLAN, repo), nombre  # type: ignore[arg-type]
    assert "no existen" in ciclo._noop_gap(_estado(), _PLAN, _Repo({"src/a.ts"}))  # type: ignore[arg-type]
    assert "siguen existiendo" in ciclo._noop_gap(
        _estado(),
        _PLAN,
        _Repo({"src/a.ts", "src/b.ts", "src/c.ts"}),  # type: ignore[arg-type]
    )


def test_3b_una_verificacion_fallida_impide_el_no_op() -> None:
    """Una sola verificación en rojo basta para no completar."""
    from punto.schemas.dev import CommandEvidence

    roja = CommandEvidence(
        name="typecheck",
        argv=("tsc",),
        exit_code=2,
        duration_ms=1,
        output_excerpt="error",
        truncated=False,
        timed_out=False,
        passed=False,
    )

    gap = _ciclo_vacio()._noop_gap(
        _estado(verification=(*_estado().verification, roja)),
        _PLAN,  # type: ignore[arg-type]
        _Repo({"src/a.ts", "src/b.ts"}),  # type: ignore[arg-type]
    )

    assert "verificaciones fallidas: typecheck" in gap


# =========================== 4 · criterios visuales: la misma cadena, por la ruta efectiva
def _consola_visual(
    tmp_path: Path,
    *,
    veredicto: str,
    con_captura: bool = True,
    repos: tuple[Path, Path] | None = None,
    plan: dict[str, Any] | None = None,
) -> tuple[TestClient, AuditLogger, _Multimodal, _CapturaFalsa]:
    """Ciclo real: BUILDER sin cambios, repo ya satisfecho, VISUAL_QA por OpenAI/Codex (doble)."""
    repo, remoto = repos or _repo_ya_satisfecho(tmp_path)
    target = replace(
        _target(repo, remoto=remoto),
        visual_routes=("http://localhost:3000/mapa",) if con_captura else (),
    )
    audit = AuditLogger()

    def gpt_responde(imagenes: int) -> str:
        if imagenes:
            return json.dumps(
                {
                    "verdicts": [
                        {"claim": n, "verdict": veredicto, "observation": "así se ve"}
                        for n in (1, 2, 3)
                    ]
                }
            )
        return json.dumps(plan or _plan())

    gpt = _Multimodal("openai", "gpt-5.6-sol", gpt_responde)
    claude = _Multimodal("anthropic", "claude-sonnet-5", lambda n: "{}")
    deepseek = _Multimodal("deepseek", "deepseek-v4-pro", lambda n: json.dumps({"changes": []}))
    router = ProviderRouter()
    for cliente in (gpt, claude, deepseek):
        router.register_provider(cliente.provider, lambda _m, c=cliente: c, model=cliente.model)
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
        config=DevelopmentConfig(max_repair_rounds=2, max_structural_corrections=2),
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
    return TestClient(aplicacion), audit, gpt, captura


def _con_criterio_visual() -> dict[str, Any]:
    return SOLICITUD | {"acceptance_criteria": [CRITERIO_VISUAL]}


def test_4_cero_cambios_con_el_criterio_visual_en_pass_completa_con_evidencia_real(
    tmp_path: Path,
) -> None:
    """Captura + VISUAL_QA efectivo (Codex) sobre el estado actual ⇒ PASS ⇒ no-op verificado."""
    client, _audit, gpt, captura = _consola_visual(tmp_path, veredicto="PASS")

    tarea = client.post("/console/tasks", json=_con_criterio_visual()).json()

    desarrollo = tarea["development"]
    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", desarrollo
    assert desarrollo["resolution"] == "ALREADY_SATISFIED" and desarrollo["commit_sha"] == ""
    assert desarrollo["claims_result"] == "SATISFIED"
    (visual,) = desarrollo["visual_evidence"]
    assert (visual["provider"], visual["transport"], visual["verdict"]) == (
        "openai",
        "codex",
        "PASS",
    )
    assert visual["request_id"] == tarea["task_id"]
    assert desarrollo["no_op_evidence"]["visual_records"] == 1
    assert gpt.imagenes and captura.llamadas, (
        "la evidencia visual se produjo sobre el estado actual"
    )


@pytest.mark.parametrize("veredicto", ["FAIL", "UNCLEAR"])
def test_5_cero_cambios_con_el_criterio_visual_en_fail_o_unclear_no_completa(
    tmp_path: Path, veredicto: str
) -> None:
    """FAIL/UNCLEAR sobre el estado actual: no se completa (UNCLEAR pide evidencia)."""
    client, audit, _gpt, _c = _consola_visual(tmp_path, veredicto=veredicto)

    tarea = client.post("/console/tasks", json=_con_criterio_visual()).json()

    assert tarea["stage"] != "DEVELOPMENT_COMPLETED", tarea["development"]
    assert tarea["development"]["resolution"] == "" and tarea["development"]["commit_sha"] == ""
    (evento,) = audit.by_type(AuditEventType.DEV_NOOP_RECONCILED)
    assert dict(evento.metadata)["satisfied"] is False
    if veredicto == "UNCLEAR":
        assert tarea["development"]["error_kind"] == "EVIDENCE_REQUIRED"
        assert "sin cambios" in tarea["development"]["error"]
        assert tarea["stage"] == "WAITING_HUMAN" and len(tarea["gates"]) == 1


def test_5b_sin_captura_del_estado_actual_no_hay_evidencia_y_no_se_completa(
    tmp_path: Path,
) -> None:
    """Ni rutas visuales declaradas ni captura: el criterio visual sigue exigiendo evidencia."""
    client, _audit, gpt, captura = _consola_visual(tmp_path, veredicto="PASS", con_captura=False)

    tarea = client.post("/console/tasks", json=_con_criterio_visual()).json()

    assert tarea["development"]["error_kind"] == "EVIDENCE_REQUIRED"
    assert tarea["stage"] != "DEVELOPMENT_COMPLETED"
    assert gpt.imagenes == [] and captura.llamadas == []


# ================================================== 6 · un cambio normal conserva su comportamiento
def test_6_un_cambio_normal_sigue_completando_con_su_commit(tmp_path: Path) -> None:
    """Regresión: con cambios propuestos no hay reconciliación y el commit se crea como siempre."""
    repo, remoto = _repos(tmp_path)
    antes = _commits(repo)
    client, audit, _t, _d = _app(
        target=_target(repo, remoto=remoto), respuestas=[_plan(), _cambio()]
    )

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    desarrollo = tarea["development"]
    assert tarea["stage"] == "DEVELOPMENT_COMPLETED"
    assert desarrollo["commit_sha"] and desarrollo["applied"] == ["src/lib/tipos.ts"]
    assert desarrollo["resolution"] == "" and desarrollo["no_op_evidence"] is None
    assert _commits(repo) == antes + 1
    assert audit.by_type(AuditEventType.DEV_NOOP_RECONCILED) == ()


def test_7_el_contrato_del_builder_permite_cero_cambios_solo_con_justificacion() -> None:
    """El BUILDER sabe que cero cambios se mide y que una respuesta vacía nunca basta."""
    from punto.orchestrator.dev_cycle import BUILD_CONTRACT

    assert "empty changes list" in BUILD_CONTRACT
    assert "never accepts an empty answer on its own" in BUILD_CONTRACT


# ======================= 8 · intento 12: el dataset lo usa el código que el plan señala, no el diff
NOMBRE_CORRECTO = "se muestra el nombre correcto del departamento"
DATASET = "src/lib/honduras-departamentos.geojson"


def _repo_con_mapa_ya_materializado(
    tmp_path: Path, *, usa_dataset: bool = True
) -> tuple[Path, Path]:
    """Repositorio del intento 12: el mapa con hover YA está en el baseline y usa el dataset real.

    La cadena es la de Punto Inmobiliario: la vista importa **tipos** de ``@/lib/honduras`` (que es
    quien lee el GeoJSON) y arma sus enlaces con el nombre del departamento; una prueba también
    menciona el dataset. El BUILDER no tiene nada que cambiar: ``changed_paths()`` queda vacío.
    """
    from punto.cartography import EXPECTED_DEPARTMENTS
    from test_semantic_qa import _dataset

    repo, remoto = _repo_ya_satisfecho(tmp_path)
    (repo / "src" / "lib" / "honduras-departamentos.geojson").write_text(
        _dataset(list(EXPECTED_DEPARTMENTS)), encoding="utf-8"
    )
    (repo / "src" / "lib" / "honduras.ts").write_text(
        'import { readFileSync } from "node:fs";\n'
        'export const datos = readFileSync("src/lib/honduras-departamentos.geojson", "utf8");\n'
        "export type DepartmentName = string;\n",
        encoding="utf-8",
    )
    vista = (
        'import type { DepartmentName } from "@/lib/honduras";\n'
        "export function Mapa({ nombres }: { nombres: DepartmentName[] }) {\n"
        "  return nombres.map((n) => (\n"
        "    <a key={n} href={`/propiedades?departamento=${encodeURIComponent(n)}`}>{n}</a>\n"
        "  ));\n}\n"
        if usa_dataset
        else 'const nombres = ["Cortés"];\nexport function Mapa() { return nombres.length; }\n'
    )
    (repo / "src" / "app").mkdir(exist_ok=True)
    (repo / "src" / "app" / "globals.css").write_text(
        ".is-active { opacity: 1; }\n", encoding="utf-8"
    )
    (repo / "src" / "components" / "Mapa.tsx").write_text(vista, encoding="utf-8")
    (repo / "src" / "components" / "Mapa.test.tsx").write_text(
        'import "src/lib/honduras-departamentos.geojson"; // la prueba también lo menciona\n',
        encoding="utf-8",
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
        "el mapa ya estaba materializado",
    )
    return repo, remoto


def _plan_del_mapa() -> dict[str, Any]:
    plan = _plan()
    plan["files_to_modify"] = [
        "src/components/Mapa.tsx",
        "src/app/globals.css",
        "src/components/Mapa.test.tsx",
    ]
    plan["files_to_read"] = ["src/lib/honduras.ts"]
    plan["summary"] = "hover con el nombre del departamento sobre el mapa"
    return plan


def _criterios_del_intento_12() -> dict[str, Any]:
    return SOLICITUD | {"acceptance_criteria": [CRITERIO_VISUAL, NOMBRE_CORRECTO]}


def test_8_el_intento_12_ya_satisfecho_completa_aunque_el_dataset_no_este_en_el_diff(
    tmp_path: Path,
) -> None:
    """Reproduce el intento 12: verificación, cadena y visual PASS, y aun así se bloqueaba.

    El predicado que bloqueaba era ``claims_outcome == FAILED``: el criterio cartográfico se medía
    sobre ``changed_paths()``, vacío en un no-op ⇒ «ningún fichero modificado referencia el
    dataset». Ahora se mide sobre el código que el plan señala más lo que importa.
    """
    repos = _repo_con_mapa_ya_materializado(tmp_path)
    antes = _commits(repos[0])
    client, audit, _gpt, _cap = _consola_visual(
        tmp_path, veredicto="PASS", repos=repos, plan=_plan_del_mapa()
    )

    tarea = client.post("/console/tasks", json=_criterios_del_intento_12()).json()

    desarrollo = tarea["development"]
    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", desarrollo
    assert desarrollo["resolution"] == "ALREADY_SATISFIED" and desarrollo["commit_sha"] == ""
    assert desarrollo["claims_result"] == "SATISFIED"
    assert _commits(repos[0]) == antes, "sin commit vacío"
    (evento,) = audit.by_type(AuditEventType.DEV_NOOP_RECONCILED)
    assert dict(evento.metadata)["satisfied"] is True


def test_8b_si_el_codigo_no_usa_el_dataset_el_no_op_sigue_fallando_cerrado(
    tmp_path: Path,
) -> None:
    """El estado NO satisface el criterio cartográfico: no se fuerza ``ALREADY_SATISFIED``."""
    repos = _repo_con_mapa_ya_materializado(tmp_path, usa_dataset=False)
    client, audit, _gpt, _cap = _consola_visual(
        tmp_path, veredicto="PASS", repos=repos, plan=_plan_del_mapa()
    )

    tarea = client.post("/console/tasks", json=_criterios_del_intento_12()).json()

    desarrollo = tarea["development"]
    assert tarea["stage"] != "DEVELOPMENT_COMPLETED"
    assert desarrollo["resolution"] == "" and desarrollo["commit_sha"] == ""
    assert desarrollo["claims_result"] == "FAILED", "el fallo conserva qué criterio no se cumplió"
    assert any(c["result"] == "UNSATISFIED" for c in desarrollo["claims"])
    assert "criterios factuales/semánticos: FAILED" in desarrollo["error"]
    (evento,) = audit.by_type(AuditEventType.DEV_NOOP_RECONCILED)
    assert dict(evento.metadata)["satisfied"] is False


def test_8c_una_prueba_que_menciona_el_dataset_no_demuestra_que_se_renderice() -> None:
    """Un fichero de pruebas no es superficie renderizada: por sí solo no satisface el criterio."""
    from punto.cartography import is_test_path, renders_from_dataset

    ficheros = {
        "src/components/Mapa.test.tsx": 'import "honduras-departamentos.geojson";\n'
        "const l = `?departamento=${encodeURIComponent(n)}`;\n",
    }
    ok, detalle = renders_from_dataset(list(ficheros), ficheros.__getitem__, DATASET)

    assert ok is False and "ningún fichero de código" in detalle
    assert is_test_path("tests/honduras-map.test.mjs") and is_test_path("src/__tests__/a.ts")
    assert not is_test_path("src/lib/honduras.ts")


def test_8d_la_superficie_sigue_imports_locales_con_alias_y_relativos_acotados() -> None:
    """``runtime_surface``: alias ``@/``, relativos, externos ignorados y profundidad tope."""
    from punto.cartography import runtime_surface

    ficheros = {
        "src/a.tsx": 'import x from "@/lib/b";\nimport r from "react";\nimport c from "./c";\n',
        "src/lib/b.ts": 'export * from "../d";\n',
        "src/c.ts": "export const c = 1;\n",
        "src/d.ts": 'import y from "./e";\n',
        "src/e.ts": "export const e = 1;\n",
    }
    superficie = runtime_surface(
        ["src/a.tsx"], ficheros.__getitem__, lambda p: p in ficheros, depth=2
    )

    assert set(superficie) == {"src/a.tsx", "src/lib/b.ts", "src/c.ts", "src/d.ts"}
    assert "src/e.ts" not in superficie, "la profundidad está acotada"


_ = PNG  # (el doble de captura comparte la imagen mínima con la suite de VISUAL_QA)
