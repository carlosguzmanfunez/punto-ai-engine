"""AP000-OBS-04: el bloqueo de una Task se ve desde el dashboard, con su causa real.

El caso real: una Task quedó en ``DEVELOPMENT_BLOCKED`` con código ``REPOSITORY_DENIED`` y el
dashboard mostraba el código con un botón «Ver» que no hacía nada. Aquí se mide, con el motor real:

1. el bloqueo **conserva** la evidencia gobernada (código, causa, regla, recurso y acción), escrita
   por la frontera que denegó y no por la interfaz;
2. la API la expone (sin exponer la ruta del repositorio del destino);
3. la página ofrece «Ver», pide el detalle real y muestra esos campos, sin explicar ningún código
   por su cuenta y escapando el texto que viene del motor;
4. los secretos siguen redactados;
5. el arreglo **no** autoriza un repositorio que de verdad no está autorizado;
6. la Task persistida se conserva y no se duplica, y un documento anterior (sin evidencia
   estructurada de bloqueo) sigue cargando.
"""

from __future__ import annotations

import json
from dataclasses import replace
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
from punto.providers.transport import REDACTED
from punto.schemas.audit import AuditEventType
from punto.schemas.build import BuildRequest
from punto.schemas.dev import BlockedEvidence, DevelopmentResult, DevelopmentStatus, PlanStatus
from punto.workspace.target import DevelopmentTargetRegistry
from test_human_console import TARGET_ID, _app, _cambio, _plan, _repos, _target

SOLICITUD: dict[str, Any] = {
    "objective": "mejorar el mapa interactivo de Honduras",
    "target_id": TARGET_ID,
    "scope_paths": ["src"],
}

#: Baseline imposible: reproduce la denegación real sin depender de la configuración de la máquina.
BASELINE_AJENO = "0" * 40


def _estado() -> Path:
    """Fichero de estado durable de la consola de esta prueba (lo fija ``conftest``)."""
    return default_console_state_path()


def _destino_con_baseline_ajeno(tmp_path: Path) -> Any:
    """Destino real cuyo baseline declarado **no** es el del repositorio: la frontera deniega."""
    repo, remoto = _repos(tmp_path)
    return replace(_target(repo, remoto=remoto), baseline_sha=BASELINE_AJENO)


def _destino_con_rama_invalida(tmp_path: Path) -> Any:
    """Destino real cuya rama declarada no es una rama de tarea: la frontera deniega."""
    repo, remoto = _repos(tmp_path)
    return replace(_target(repo, remoto=remoto), work_branch="main")


def _consola(destino: Any) -> tuple[TestClient, AuditLogger]:
    """Consola sobre el destino dado, con el ciclo real."""
    client, audit, _target_obj, _deps = _app(target=destino, respuestas=[])
    return client, audit


class _CicloGuionizado:
    """Ciclo de prueba que devuelve un resultado ya construido (para forzar un bloqueo exacto)."""

    def __init__(self, result: DevelopmentResult) -> None:
        self._result = result
        self.llamadas = 0

    def run(self, request: Any) -> DevelopmentResult:
        """Devuelve el resultado guionizado, con la identidad de la solicitud real."""
        self.llamadas += 1
        return self._result.model_copy(update={"request_id": request.request_id})


def _consola_con_resultado(
    tmp_path: Path | None, result: DevelopmentResult
) -> tuple[TestClient, AuditLogger]:
    """Consola con el ciclo sustituido por un resultado exacto (mismo motor, mismo gate)."""
    destino: Any = None
    if tmp_path is not None:
        repo, remoto = _repos(tmp_path)
        destino = _target(repo, remoto=remoto)
    audit = AuditLogger()
    dependencies = ConsoleDependencies(
        dev_cycle=_CicloGuionizado(result),  # type: ignore[arg-type]
        gates=HumanGate(),
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={} if destino is None else {TARGET_ID: destino},
        run_inline=True,
        environ={},
    )
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencies)
    return TestClient(application), audit


def _resultado_bloqueado(**campos: str) -> DevelopmentResult:
    """Resultado bloqueado con la evidencia estructurada que se le pida."""
    return DevelopmentResult(
        status=DevelopmentStatus.BLOCKED,
        error_kind="REPOSITORY_DENIED",
        error=f"detalle real del bloqueo: {campos.get('detail', 'sin baseline acordado')}",
        blocked=BlockedEvidence(
            code="REPOSITORY_DENIED",
            detail=campos.get("detail", "sin baseline acordado"),
            rule=campos.get("rule", ""),
            resource=campos.get("resource", ""),
            remedy=campos.get("remedy", ""),
        ),
    )


# ------------------------------------------- 1 · el bloqueo conserva su evidencia gobernada
def test_el_bloqueo_por_baseline_conserva_causa_regla_recurso_y_accion(tmp_path: Path) -> None:
    """1: la evidencia la escribe la frontera que deniega; la interfaz solo la muestra."""
    destino = _destino_con_baseline_ajeno(tmp_path)
    client, _audit = _consola(destino)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    assert tarea["stage"] == "DEVELOPMENT_FAILED", tarea
    assert tarea["development"]["status"] == "DEVELOPMENT_BLOCKED"

    bloqueo = tarea["blocked"]
    assert bloqueo["code"] == "REPOSITORY_DENIED"
    # Causa real: los dos commits, el del árbol y el declarado.
    assert BASELINE_AJENO[:12] in bloqueo["detail"]
    assert "baseline" in bloqueo["detail"]
    # Regla que lo produjo: el compromiso del árbol acordado.
    assert "baseline_sha" in bloqueo["rule"]
    assert "commit acordado" in bloqueo["rule"]
    # Recurso afectado: la rama de trabajo del destino.
    assert destino.work_branch in bloqueo["resource"]
    assert TARGET_ID in bloqueo["resource"]
    # Acción que corresponde, según la regla que lo produjo.
    assert "baseline_sha" in bloqueo["remedy"]
    # Etapa real del ciclo cuando se detuvo, sin interpretarla.
    assert bloqueo["development_status"] == "DEVELOPMENT_BLOCKED"
    assert bloqueo["plan_status"] == PlanStatus.REJECTED.value
    assert bloqueo["planned"] is False
    assert bloqueo["target_id"] == TARGET_ID

    # No se construyó ni se verificó nada: un bloqueo no deja trabajo a medias.
    assert tarea["development"]["applied"] == []
    assert tarea["development"]["verification"] == []
    assert tarea["development"]["commit_sha"] == ""
    assert tarea["gates"] == []


def test_el_bloqueo_conserva_la_misma_evidencia_en_el_detalle_y_tras_reiniciar(
    tmp_path: Path,
) -> None:
    """1b: la evidencia no se recalcula al mirarla ni se pierde al reiniciar."""
    destino = _destino_con_baseline_ajeno(tmp_path)
    client, _audit = _consola(destino)
    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    detalle = client.get(f"/console/tasks/{tarea['task_id']}").json()
    assert detalle["blocked"] == tarea["blocked"]

    otra, _audit2 = _consola(destino)
    tras_reinicio = otra.get(f"/console/tasks/{tarea['task_id']}").json()
    assert tras_reinicio["task_id"] == tarea["task_id"]
    assert tras_reinicio["stage"] == tarea["stage"]
    assert tras_reinicio["blocked"] == tarea["blocked"]
    assert otra.get("/console/tasks").json()["total"] == 1


def test_el_bloqueo_declara_el_destino_sin_la_ruta_del_repositorio(tmp_path: Path) -> None:
    """2: la API dice qué destino se vio afectado, nunca la ruta real del repositorio."""
    destino = _destino_con_baseline_ajeno(tmp_path)
    client, _audit = _consola(destino)

    respuesta = client.post("/console/tasks", json=SOLICITUD)
    assert str(destino.repository) not in respuesta.text

    visto = respuesta.json()["blocked"]["destination"]
    assert visto["target_id"] == TARGET_ID
    assert visto["work_branch"] == destino.work_branch
    assert visto["scope_roots"] == list(destino.scope_roots)
    assert visto["repository"] == destino.repository.name
    assert visto["name"] == destino.human_name


# ------------------------------------------- 3 · «Ver» muestra la evidencia real
def test_la_pagina_ofrece_ver_y_muestra_los_campos_reales_del_bloqueo() -> None:
    """3: «Ver» pide el detalle real de la tarea y pinta la evidencia que devuelve la API."""
    client, _audit = _consola_con_resultado(None, _resultado_bloqueado())
    pagina = client.get("/console").text

    # El botón existe, pide el detalle real de esa tarea y alterna el panel.
    assert 'data-ver="${task.task_id}"' in pagina
    assert 'data-testid="ver-${task.task_id}"' in pagina
    assert "await api(`/console/tasks/${taskId}`)" in pagina
    assert "openTaskDetail" in pagina
    assert 'data-detail="${task.task_id}"' in pagina

    # El panel muestra exactamente los campos gobernados, sin inventar ninguno.
    for etiqueta in (
        "Bloqueo",
        "Causa real",
        "Regla que lo produjo",
        "Recurso",
        "Qué corresponde",
        "Destino",
        "Rama de trabajo",
        "Etapa del ciclo",
    ):
        assert etiqueta in pagina, etiqueta
    for campo in ("blocked.code", "blocked.detail", "blocked.rule", "blocked.resource"):
        assert campo in pagina, campo
    # Y cuando la frontera no declaró algo, lo dice en vez de rellenarlo.
    assert "no declarada en el resultado" in pagina
    assert "no declarado en el resultado" in pagina
    # Nada de explicaciones propias por código: la interfaz no conoce ningún código de bloqueo.
    for codigo in ("REPOSITORY_DENIED", "SCOPE_VIOLATION", "POLICY_DENIED"):
        assert codigo not in pagina, codigo
    # El texto que viene del motor se escapa antes de entrar en el DOM.
    assert "function esc(" in pagina


def test_la_pagina_muestra_el_bloqueo_tambien_cuando_falta_la_evidencia_estructurada() -> None:
    """3b: con un resultado anterior (solo código y causa) el panel muestra esos dos campos."""
    resultado = DevelopmentResult(
        status=DevelopmentStatus.BLOCKED,
        error_kind="REPOSITORY_DENIED",
        error="el destino está en otro commit y el baseline declarado es otro",
    )

    assert resultado.blocked is None
    pagina, _audit = _consola_con_resultado(None, resultado)
    html = pagina.get("/console").text
    assert "dev.error_kind" in html and "dev.error" in html
    assert "no declarada en el resultado" in html


def test_el_panel_cae_al_codigo_y_la_causa_cuando_no_hay_evidencia_estructurada(
    tmp_path: Path,
) -> None:
    """3c: un bloqueo anterior a esta corrección sigue siendo visible (código y causa reales)."""
    resultado = DevelopmentResult(
        status=DevelopmentStatus.BLOCKED,
        error_kind="REPOSITORY_DENIED",
        error="el destino está en otro commit y el baseline declarado es otro",
    )
    client, _audit = _consola_con_resultado(tmp_path, resultado)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["blocked"] == {}
    assert tarea["development"]["error_kind"] == "REPOSITORY_DENIED"
    assert "baseline" in tarea["development"]["error"]


# ------------------------------------------- 5 · sigue siendo una frontera
def test_un_repositorio_realmente_no_autorizado_sigue_bloqueado(tmp_path: Path) -> None:
    """5: el arreglo no autoriza nada: los dos desajustes reales siguen denegando."""
    destino_baseline = _destino_con_baseline_ajeno(tmp_path / "baseline")
    client, _audit = _consola(destino_baseline)
    por_baseline = client.post("/console/tasks", json=SOLICITUD).json()
    assert por_baseline["blocked"]["code"] == "REPOSITORY_DENIED"
    assert por_baseline["development"]["status"] == "DEVELOPMENT_BLOCKED"
    assert "baseline_sha" in por_baseline["blocked"]["rule"]

    destino_rama = _destino_con_rama_invalida(tmp_path / "rama")
    otro, _audit2 = _consola(destino_rama)
    por_rama = otro.post("/console/tasks", json=SOLICITUD).json()
    assert por_rama["blocked"]["code"] == "REPOSITORY_DENIED"
    assert "rama de tarea" in por_rama["blocked"]["rule"]
    assert "work_branch" in por_rama["blocked"]["remedy"]
    assert por_rama["blocked"]["resource"]

    # Y el repositorio del destino queda intacto y en su rama: un bloqueo no escribe nada.
    assert (destino_rama.repository / "src" / "lib" / "tipos.ts").read_text(
        encoding="utf-8"
    ).startswith("export const TIPOS = ['Casa'];")
    for destino in (destino_baseline, destino_rama):
        assert not list((destino.repository / "src" / "lib").glob("*.nuevo.ts"))


def test_un_destino_no_registrado_tambien_declara_su_regla_y_su_accion() -> None:
    """5b: la evidencia estructurada no es un caso especial: la escribe cada frontera."""
    ciclo = DevelopmentCycle(
        router=ProviderRouter(),
        targets=DevelopmentTargetRegistry({}),
        config=DevelopmentConfig(max_repair_rounds=1, max_structural_corrections=1),
        audit=AuditLogger(),
        policy_engine=PolicyEngine.from_config(),
    )

    resultado = ciclo.run(
        BuildRequest(
            objective="una tarea cualquiera",
            target_repository="destino-inexistente",
            requested_role=ProviderRole.BUILDER,
        )
    )

    assert resultado.error_kind == "TARGET_NOT_REGISTERED"
    assert resultado.blocked is not None
    assert "registrado" in resultado.blocked.rule
    assert resultado.blocked.resource == "destino-inexistente"
    assert resultado.blocked.remedy


# ------------------------------------------- 4 · secretos
def test_los_secretos_del_bloqueo_permanecen_redactados(tmp_path: Path) -> None:
    """4: ni la API ni el estado durable publican una credencial que llegue dentro del bloqueo."""
    secreto = "sk-abcdef0123456789"
    resultado = _resultado_bloqueado(
        detail=f"Authorization: Bearer {secreto}",
        rule=f"la regla vio {secreto}",
        resource=f"rama con {secreto}",
        remedy=f"rota la clave {secreto}",
    )
    client, audit = _consola_con_resultado(tmp_path, resultado)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    bloqueo = tarea["blocked"]
    for campo in ("detail", "rule", "resource", "remedy"):
        assert secreto not in bloqueo[campo], campo
        assert REDACTED in bloqueo[campo], campo
    assert secreto not in json.dumps(tarea)
    documento = _estado().read_text(encoding="utf-8") if _estado().is_file() else ""
    assert secreto not in documento
    assert audit.by_type(AuditEventType.CONSOLE_STATE_WRITE_REFUSED)


def test_la_vista_del_bloqueo_no_inventa_los_campos_ausentes(tmp_path: Path) -> None:
    """4b: un campo que la frontera no declaró viaja vacío; la interfaz dice que no está."""
    client, _audit = _consola_con_resultado(tmp_path, _resultado_bloqueado())

    bloqueo = client.post("/console/tasks", json=SOLICITUD).json()["blocked"]

    assert bloqueo["rule"] == ""
    assert bloqueo["resource"] == ""
    assert bloqueo["remedy"] == ""
    assert bloqueo["code"] == "REPOSITORY_DENIED"
    assert bloqueo["detail"]


# ------------------------------------------- 6 · la Task persistida
def test_la_task_bloqueada_se_conserva_y_no_se_duplica(tmp_path: Path) -> None:
    """6: reiniciar no pierde ni duplica la Task bloqueada, y su evidencia sigue ahí."""
    destino = _destino_con_baseline_ajeno(tmp_path)
    client, _audit = _consola(destino)
    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    for _ in range(2):
        assert client.get("/console/tasks").json()["total"] == 1

    otra, _audit2 = _consola(destino)
    listado = otra.get("/console/tasks").json()
    assert listado["total"] == 1
    recuperada = listado["items"][0]
    assert recuperada["task_id"] == tarea["task_id"]
    assert recuperada["blocked"] == tarea["blocked"]
    assert recuperada["recovered"] is True


def test_un_documento_anterior_sin_evidencia_estructurada_sigue_cargando(tmp_path: Path) -> None:
    """6b: compatibilidad con el documento real anterior a esta corrección (Task 2e7822a0)."""
    destino = _destino_con_baseline_ajeno(tmp_path)
    client, _audit = _consola(destino)
    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    documento = json.loads(_estado().read_text(encoding="utf-8"))
    del documento["tasks"][0]["result"]["blocked"]
    _estado().write_text(json.dumps(documento), encoding="utf-8")

    otra, _audit2 = _consola(destino)
    listado = otra.get("/console/tasks").json()
    assert listado["total"] == 1
    recuperada = listado["items"][0]
    assert recuperada["task_id"] == tarea["task_id"]
    assert recuperada["stage"] == "DEVELOPMENT_FAILED"
    # El código y la causa siguen visibles aunque el documento no traiga evidencia estructurada:
    # el panel cae al código y al detalle reales del resultado.
    assert recuperada["development"]["error_kind"] == "REPOSITORY_DENIED"
    assert "baseline" in recuperada["development"]["error"]
    assert recuperada["blocked"] == {}


def test_recuperar_la_task_no_reescribe_el_estado_persistido(tmp_path: Path) -> None:
    """6c: arrancar la consola no toca lo ya escrito (la Task del operador se conserva)."""
    destino = _destino_con_baseline_ajeno(tmp_path)
    client, _audit = _consola(destino)
    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    documento = _estado().read_text(encoding="utf-8")

    otra, _audit2 = _consola(destino)
    assert otra.get(f"/console/tasks/{tarea['task_id']}").json()["task_id"] == tarea["task_id"]
    assert _estado().read_text(encoding="utf-8") == documento, "recuperar no reescribe el estado"


# ------------------------------------------- control: el ciclo real sigue funcionando
def test_el_ciclo_real_sigue_funcionando_con_el_baseline_correcto(tmp_path: Path) -> None:
    """El arreglo es de configuración: con el árbol acordado, el ciclo entra y trabaja normal."""
    repo, remoto = _repos(tmp_path)
    destino = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(target=destino, respuestas=[_plan(), _cambio()])

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea
    assert tarea["blocked"] == {}
    assert tarea["development"]["commit_sha"]
