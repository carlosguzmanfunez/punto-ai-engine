"""Cadena del release autónomo: botón → POST /release → backend → respuesta → render → estado.

Defecto real: al pulsar «Release autónomo» sobre la Task 2e7822a0 (release AUTO, artefacto
verificado) el backend **sí** publicó (push + sonda de producción), pero el Dashboard mostró «Sin
release autónomo (HUMAN_GATE): Cannot set properties of null (setting 'textContent')».

Causa: ``loadGates`` (que se ejecuta *después* de la respuesta 200) escribía
``getElementById("gates-history-summary").textContent`` y el marcado no declaraba ese ``id``: el
nodo era ``null``, la excepción cayó en el ``catch`` del manejador y este la etiquetó como
``HUMAN_GATE`` por defecto. Un error de UI se reclasificó como disposición de release.

Aquí se reproduce con la página REAL en Node (DOM = los ``id`` que el marcado declara) y con las
respuestas REALES del backend, más la idempotencia del release (doble clic / concurrencia).

    pytest tests/test_dashboard_release_chain.py -q
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from punto.schemas.audit import AuditEventType
from test_human_console import (
    TARGET_ID,
    _app,
    _autoridad_completa,
    _fetch_sin_marcador,
    _git,
    _plan,
    _target,
)
from test_noop_reconciliation import _repo_ya_satisfecho
from test_release_chain_and_gate_reconciliation import (
    _head,
    _remote_main,
    _sembrar_estado_de_la_task,
)

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "punto" / "api" / "static" / "dashboard.html"
HARNESS = Path(__file__).parent / "dashboard_harness.js"
NODE = shutil.which("node")
TASK_ID = "2e7822a0-5d67-405e-aeb6-3c07a139cbbf"


# ------------------------------------------------------------------------------ backend real
def _release_app(
    tmp_path: Path, *, autoridad: bool = True, **opciones: Any
) -> tuple[TestClient, Any, Path, Path]:
    """Task 2e7822a0 con su estado durable real (DEVELOPMENT_COMPLETED, no-op verificado)."""
    repo, remoto = _repo_ya_satisfecho(tmp_path)
    _sembrar_estado_de_la_task(repo)
    destino = replace(
        _target(repo, remoto=remoto),
        authority=_autoridad_completa() if autoridad else _target(repo, remoto=remoto).authority,
        scope_roots=("src", "tests"),
    )
    client, audit, _t, _d = _app(target=destino, respuestas=[], **opciones)
    return client, audit, repo, remoto


def _eventos(audit: Any, tipo: AuditEventType) -> int:
    return len(audit.by_type(tipo))


def test_b1_release_auto_publica_una_vez_y_un_segundo_clic_no_repite_nada(tmp_path: Path) -> None:
    client, audit, repo, remoto = _release_app(tmp_path)
    antes = client.get(f"/console/tasks/{TASK_ID}").json()
    assert antes["release"]["disposition"] == "AUTO"
    assert antes["next_human_action"]["kind"] == "release_autonomous"
    assert antes["development"]["publishable_source"] == "legacy-baseline"

    primero = client.post(f"/console/tasks/{TASK_ID}/release")
    empujes = _eventos(audit, AuditEventType.PUBLICATION_PUSHED)
    segundo = client.post(f"/console/tasks/{TASK_ID}/release")

    assert primero.status_code == 200, primero.text
    assert primero.json()["release_outcome"] == "PUBLISHED"
    assert primero.json()["stage"] == "PRODUCTION_VALIDATED"
    assert _remote_main(remoto) == _head(repo), "se publicó exactamente el estado verificado"
    assert segundo.status_code == 200 and segundo.json()["release_outcome"] == "ALREADY_PUBLISHED"
    assert empujes == 1 and _eventos(audit, AuditEventType.PUBLICATION_PUSHED) == 1
    assert _eventos(audit, AuditEventType.PRODUCTION_VERIFIED) == 1
    despues = client.get(f"/console/tasks/{TASK_ID}").json()
    assert despues["next_human_action"]["kind"] == "none", "tras publicar no queda release"
    assert despues["pending_gates"] == [] and len(despues["gates"]) == 2


def test_b2_la_concurrencia_no_duplica_la_publicacion(tmp_path: Path) -> None:
    client, audit, repo, remoto = _release_app(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        respuestas = list(
            pool.map(lambda _i: client.post(f"/console/tasks/{TASK_ID}/release"), range(8))
        )

    codigos = sorted({item.status_code for item in respuestas})
    assert set(codigos) <= {200, 409}, [r.text for r in respuestas]
    assert any(
        item.status_code == 200 and item.json()["release_outcome"] == "PUBLISHED"
        for item in respuestas
    )
    assert _eventos(audit, AuditEventType.PUBLICATION_PUSHED) == 1, "un único push"
    assert _eventos(audit, AuditEventType.PUBLICATION_REQUESTED) == 1
    assert _remote_main(remoto) == _head(repo)


def test_b3_una_publicacion_no_verificada_se_puede_reintentar_y_no_es_already(
    tmp_path: Path,
) -> None:
    """La idempotencia solo aplica a lo VALIDADO: un fallo real sigue siendo reintentable."""
    client, audit, _repo, _remoto = _release_app(tmp_path, fetch=_fetch_sin_marcador)

    primero = client.post(f"/console/tasks/{TASK_ID}/release").json()
    vista = client.get(f"/console/tasks/{TASK_ID}").json()
    segundo = client.post(f"/console/tasks/{TASK_ID}/release").json()

    assert primero["stage"] == "DEPLOYMENT_NOT_VERIFIED", primero["stage"]
    assert vista["next_human_action"]["kind"] == "release_autonomous"
    assert segundo["release_outcome"] == "PUBLISHED", "se reintentó de verdad"
    assert _eventos(audit, AuditEventType.PRODUCTION_VERIFIED) == 0


def test_b4_sin_autoridad_del_destino_el_motor_responde_human_gate_con_su_detalle(
    tmp_path: Path,
) -> None:
    client, audit, _repo, remoto = _release_app(tmp_path, autoridad=False)
    remoto_antes = _remote_main(remoto)

    respuesta = client.post(f"/console/tasks/{TASK_ID}/release")

    assert respuesta.status_code == 409
    detalle = respuesta.json()["detail"]
    assert detalle["disposition"] == "HUMAN_GATE" and detalle["reasons"]
    assert _remote_main(remoto) == remoto_antes
    assert _eventos(audit, AuditEventType.PUBLICATION_PUSHED) == 0


# -------------------------------------------------------------------- marcado ↔ script
def test_el_marcado_declara_todos_los_id_que_el_script_pide_por_nombre_fijo() -> None:
    """La clase de defecto: ``getElementById("x")`` con un ``x`` que el marcado no declara."""
    html = HTML.read_text(encoding="utf-8")
    declarados = set(re.findall(r'\bid="([^"$]+)"', html))
    pedidos = set(re.findall(r'getElementById\("([^"$`]+)"\)', html))

    assert pedidos - declarados == set(), f"ids pedidos y no declarados: {pedidos - declarados}"
    assert "gates-history-summary" in declarados


# ------------------------------------------------------------------------- página real (Node)
def _correr(escenario: str, tmp_path: Path, payload: dict[str, Any]) -> Any:
    if NODE is None:
        pytest.skip("node no está disponible")
    (tmp_path / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
    script = tmp_path / "escenario.js"
    script.write_text(
        f"""
        const {{ boot, readHtml }} = require({json.dumps(HARNESS.as_posix())});
        const PAYLOAD_PATH = {json.dumps((tmp_path / "payload.json").as_posix())};
        const P = JSON.parse(require("fs").readFileSync(PAYLOAD_PATH, "utf-8"));
        const html = readHtml({json.dumps(HTML.as_posix())});
        const ID = P.task_id;
        const rutas = (extra = {{}}) => ({{
          [`POST /console/tasks/${{ID}}/release`]: {{ status: 200, body: P.release_ok }},
          "GET /console/tasks": {{ body: P.tasks_despues }},
          "GET /console/human-gates": {{ body: P.gates }},
          ...extra,
        }});
        (async () => {{
        {escenario}
        }})().catch((e) => {{ console.error(e); process.exit(1); }});
        """,
        encoding="utf-8",
    )
    hecho = subprocess.run(
        [NODE, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert hecho.returncode == 0, hecho.stderr
    return json.loads(hecho.stdout)


@pytest.fixture()
def payload(tmp_path: Path) -> dict[str, Any]:
    """Respuestas REALES del backend para el escenario del defecto (y sus variantes)."""
    client, _audit, _repo, _remoto = _release_app(tmp_path)
    antes = client.get("/console/tasks").json()
    gates = client.get("/console/human-gates").json()
    exito = client.post(f"/console/tasks/{TASK_ID}/release").json()
    repetido = client.post(f"/console/tasks/{TASK_ID}/release").json()
    despues = client.get("/console/tasks").json()
    return {
        "task_id": TASK_ID,
        "tasks_antes": antes,
        "tasks_despues": despues,
        "gates": gates,
        "release_ok": exito,
        "release_repetido": repetido,
        "human_gate": {
            "detail": {
                "message": "la operación no está dentro de la autoridad persistente del destino",
                "disposition": "HUMAN_GATE",
                "reasons": ["sin autorización persistente para: push, deploy"],
                "blockers": ["x"],
            }
        },
    }


def test_p0_los_datos_reales_son_los_del_escenario(payload: dict[str, Any]) -> None:
    (antes,) = payload["tasks_antes"]["items"]
    assert antes["release"]["disposition"] == "AUTO"
    assert antes["next_human_action"]["kind"] == "release_autonomous"
    assert payload["release_ok"]["release_outcome"] == "PUBLISHED"
    assert payload["release_repetido"]["release_outcome"] == "ALREADY_PUBLISHED"


def test_p1_reproduce_el_defecto_elemento_ausente_no_rompe_ni_se_reclasifica(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    """Sin el ``id`` que faltaba, el clic ya no lanza y el éxito se representa como éxito."""
    salida = _correr(
        """
        const pagina = boot({ html, routes: rutas(), dropIds: ["gates-history-summary"] });
        const resultado = await pagina.page.releaseTask(ID);
        console.log(JSON.stringify({ resultado, nota: pagina.text("tarea-note") }));
        """,
        tmp_path,
        payload,
    )

    assert salida["resultado"]["kind"] == "PUBLISHED"
    assert "refreshError" not in salida["resultado"]
    assert "publicado y validado" in salida["nota"] and "HUMAN_GATE" not in salida["nota"]


def test_p2_marcado_real_exito_actualiza_nota_tablero_y_tarjeta(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    salida = _correr(
        """
        const pagina = boot({ html, routes: rutas() });
        pagina.page.consoleState.tasks = P.tasks_antes.items;
        pagina.page.renderTasks();
        const antes = pagina.html("tasks-list").includes("data-release");
        const resultado = await pagina.page.releaseTask(ID);
        console.log(JSON.stringify({ antes, resultado, nota: pagina.text("tarea-note"),
          resumen: pagina.text("gates-history-summary"),
          despues: pagina.html("tasks-list").includes("data-release"),
          llamadas: pagina.calls }));
        """,
        tmp_path,
        payload,
    )

    assert salida["antes"] is True, "AUTO + release_autonomous ⇒ el botón se ofrece"
    assert salida["resultado"]["kind"] == "PUBLISHED"
    assert "Historial de gates (2" in salida["resumen"]
    assert salida["despues"] is False, "publicado y validado: ya no hay release que ofrecer"
    assert salida["llamadas"].count(f"POST /console/tasks/{TASK_ID}/release") == 1


def test_p3_varios_nodos_opcionales_ausentes_no_rompen_ningun_manejador(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    salida = _correr(
        """
        const pagina = boot({ html, routes: rutas(),
          dropIds: ["tarea-note", "gates-history-list", "gates-history-summary", "gates-list"] });
        const resultado = await pagina.page.releaseTask(ID);
        console.log(JSON.stringify({ resultado }));
        """,
        tmp_path,
        payload,
    )

    assert salida["resultado"]["kind"] == "PUBLISHED"


def test_p4_re_render_durante_la_respuesta_no_deja_referencias_obsoletas(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    salida = _correr(
        """
        const capturas = {};
        let pagina;
        const rutasDemora = rutas({
          [`POST /console/tasks/${ID}/release`]: async () => {
            capturas.durante = pagina.html("tasks-list");
            await pagina.page.loadTasks();          // otro refresco repinta la tarjeta a mitad
            capturas.trasRepintar = pagina.html("tasks-list");
            await new Promise((r) => setTimeout(r, 20));
            return { status: 200, body: P.release_ok };
          },
          "GET /console/tasks": { body: P.tasks_antes },
        });
        pagina = boot({ html, routes: rutasDemora });
        pagina.page.consoleState.tasks = P.tasks_antes.items;
        pagina.page.renderTasks();
        const resultado = await pagina.page.releaseTask(ID);
        console.log(JSON.stringify({ capturas, resultado }));
        """,
        tmp_path,
        payload,
    )

    assert "Publicando" in salida["capturas"]["durante"]
    assert "disabled" in salida["capturas"]["durante"], "deshabilitado mientras vuela"
    assert "Publicando" in salida["capturas"]["trasRepintar"], "el repintado conserva el estado"
    assert salida["resultado"]["kind"] == "PUBLISHED"


def test_p5_human_gate_legitimo_del_backend_se_representa_como_tal(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    salida = _correr(
        """
        const pagina = boot({ html, routes: rutas({
          [`POST /console/tasks/${ID}/release`]: { status: 409, body: P.human_gate } }) });
        const resultado = await pagina.page.releaseTask(ID);
        console.log(JSON.stringify({ resultado, nota: pagina.text("tarea-note") }));
        """,
        tmp_path,
        payload,
    )

    assert salida["resultado"]["kind"] == "HUMAN_GATE"
    assert "sin release autónomo (HUMAN_GATE)" in salida["nota"]
    assert "sin autorización persistente" in salida["nota"]


@pytest.mark.parametrize(
    ("ruta", "esperado"),
    [
        ({"status": 500, "body": {"detail": "boom"}}, "ERROR"),
        ({"throws": "Failed to fetch"}, "ERROR"),
        (
            {
                "status": 409,
                "body": {"detail": "el HEAD del destino ya no es el estado verificado"},
            },
            "REFUSED",
        ),
        (
            {"status": 409, "body": {"detail": {"disposition": "DENIED", "reasons": ["política"]}}},
            "DENIED",
        ),
    ],
)
def test_p6_un_error_nunca_se_reclasifica_como_human_gate(
    tmp_path: Path, payload: dict[str, Any], ruta: dict[str, Any], esperado: str
) -> None:
    salida = _correr(
        f"""
        const pagina = boot({{ html, routes: rutas({{
          [`POST /console/tasks/${{ID}}/release`]: {json.dumps(ruta)} }}) }});
        const resultado = await pagina.page.releaseTask(ID);
        console.log(JSON.stringify({{ resultado, nota: pagina.text("tarea-note") }}));
        """,
        tmp_path,
        payload,
    )

    assert salida["resultado"]["kind"] == esperado
    if esperado in {"ERROR", "REFUSED"}:
        assert "HUMAN_GATE" not in salida["nota"]


def test_p7_backend_ya_publicado_se_dice_sin_repetir_nada(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    salida = _correr(
        """
        const pagina = boot({ html, routes: rutas({
          [`POST /console/tasks/${ID}/release`]: { status: 200, body: P.release_repetido } }) });
        const resultado = await pagina.page.releaseTask(ID);
        console.log(JSON.stringify({ resultado }));
        """,
        tmp_path,
        payload,
    )

    assert salida["resultado"]["kind"] == "ALREADY_PUBLISHED"
    assert "no se repitió" in salida["resultado"]["message"]


def test_p8_si_refrescar_falla_se_dice_que_falla_la_vista_no_el_release(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    salida = _correr(
        """
        const pagina = boot({ html,
          routes: rutas({ "GET /console/tasks": { throws: "sin red" } }) });
        const resultado = await pagina.page.releaseTask(ID);
        console.log(JSON.stringify({ resultado, nota: pagina.text("tarea-note") }));
        """,
        tmp_path,
        payload,
    )

    assert salida["resultado"]["kind"] == "PUBLISHED", "el desenlace es el del motor"
    assert "sin red" in salida["resultado"]["refreshError"]
    assert "la vista no se pudo refrescar" in salida["nota"] and "HUMAN_GATE" not in salida["nota"]


def test_p9_doble_clic_envia_una_sola_solicitud(tmp_path: Path, payload: dict[str, Any]) -> None:
    salida = _correr(
        """
        const pagina = boot({ html, routes: rutas({
          [`POST /console/tasks/${ID}/release`]: async () => {
            await new Promise((r) => setTimeout(r, 20));
            return { status: 200, body: P.release_ok }; } }) });
        const [a, b] = await Promise.all([
          pagina.page.releaseTask(ID), pagina.page.releaseTask(ID)]);
        console.log(JSON.stringify({ a: a.kind, b: b.kind,
          posts: pagina.calls.filter((c) => c.startsWith("POST")).length,
          pendientes: [...pagina.page.consoleState.pendingReleases] }));
        """,
        tmp_path,
        payload,
    )

    assert salida["posts"] == 1 and sorted([salida["a"], salida["b"]]) == ["IGNORED", "PUBLISHED"]
    assert salida["pendientes"] == [], "la solicitud en vuelo se limpia"


_ = (TARGET_ID, _git, _plan)
