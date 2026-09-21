"""Evidencia visual INTERACTIVA (hover): configuración, navegador real, evaluación y ciclo.

Cierra el hueco que dejó VISUAL_QA efectivo: una captura estática no demuestra «al pasar el cursor
sobre un departamento este cambia y aparece su nombre». Aquí se fija, sin llamar a ningún proveedor
real:

- la interacción se declara en la **configuración del destino** (nada en el motor);
- el hover se ejecuta de verdad en un Chrome/Edge real (las pruebas «navegador real» se omiten solo
  si la máquina no tiene ninguno) con eventos de entrada reales, sobre un punto que golpea al
  elemento, y deja antes/después con huellas y hechos deterministas;
- si el elemento no se localiza o el hover no se confirma, el criterio queda ``UNCLEAR`` y **no** se
  llama a VISUAL_QA; un ``PASS`` con capturas idénticas se degrada a ``UNCLEAR``;
- la evidencia queda ligada a la Task/intento con interacción, elemento, capturas y veredicto.

La prueba real con Codex vive en ``tests/integration/test_hover_interaction_live.py``.

    pytest tests/test_visual_interaction.py -q
"""

from __future__ import annotations

import functools
import http.server
import json
import socketserver
import threading
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.acceptance import is_interaction_claim
from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.contract import ProviderRole
from punto.providers.failover import FailoverPolicy
from punto.providers.router import ProviderRouter
from punto.visualqa.dev_evidence import CapturedShot, CaptureError, HeadlessBrowserCapture
from punto.visualqa.interaction import (
    BrowserInteraction,
    InteractionEvidence,
    assess_interaction_claims,
)
from punto.workspace.target import (
    DevelopmentTargetError,
    DevelopmentTargetRegistry,
    VisualInteraction,
    target_from_mapping,
)
from test_human_console import TARGET_ID, _cambio, _plan, _repos, _target
from test_visual_qa_effective import PNG, _evaluador_visual, _Multimodal

CRITERIO_HOVER = "el departamento cambia visualmente al pasar el cursor y aparece su nombre"
SELECTOR = '.map a[aria-label="Ver propiedades en Cortés"] path'


# ============================================================== 1 · configuración del destino
def _mapa_destino(visual: Any) -> dict[str, Any]:
    """Declaración mínima de destino con la sección ``visual`` que se prueba."""
    return {
        "repository": str(Path.cwd()),
        "baseline_sha": "0" * 40,
        "scope_roots": ["src"],
        "visual": visual,
    }


def _interaccion(**cambios: Any) -> dict[str, Any]:
    base = {
        "name": "hover-depto",
        "route": "http://localhost:3000/propiedades",
        "hover": SELECTOR,
        "label": ".map-active-label",
    }
    base.update(cambios)
    return base


def test_1_la_interaccion_se_declara_en_la_configuracion_del_destino() -> None:
    """Ruta, elemento y etiqueta salen del destino: el motor no lleva URLs ni selectores."""
    destino = target_from_mapping(
        "hn",
        _mapa_destino({"routes": ["http://localhost:3000/"], "interactions": [_interaccion()]}),
    )

    (interaccion,) = destino.visual_interactions
    assert interaccion == VisualInteraction(
        name="hover-depto",
        route="http://localhost:3000/propiedades",
        hover=SELECTOR,
        label=".map-active-label",
        index=0,
        settle_ms=800,
    )


@pytest.mark.parametrize(
    "interaccion",
    [
        pytest.param(_interaccion(route="https://ejemplo.com/"), id="ruta-fuera-del-bucle-local"),
        pytest.param(_interaccion(route="file:///etc/passwd"), id="ruta-file"),
        pytest.param(_interaccion(hover=""), id="sin-selector"),
        pytest.param(_interaccion(hover="a\nb"), id="selector-con-control"),
        pytest.param(_interaccion(hover="x" * 301), id="selector-desmesurado"),
        pytest.param(_interaccion(name="Nombre Con Espacios"), id="nombre-invalido"),
        pytest.param(_interaccion(index=-1), id="index-negativo"),
        pytest.param(_interaccion(index=True), id="index-booleano"),
        pytest.param(_interaccion(settle_ms=5), id="settle-muy-corto"),
        pytest.param("no es un objeto", id="forma-rota"),
    ],
)
def test_1b_una_interaccion_invalida_no_se_ignora_en_silencio(interaccion: Any) -> None:
    """Configuración inválida = error explícito (nunca una interacción a medias)."""
    with pytest.raises(DevelopmentTargetError):
        target_from_mapping("hn", _mapa_destino({"interactions": [interaccion]}))


def test_1c_nombres_repetidos_y_exceso_de_interacciones_se_rechazan() -> None:
    """Máximo cuatro y sin nombres repetidos."""
    with pytest.raises(DevelopmentTargetError):
        target_from_mapping("hn", _mapa_destino({"interactions": [_interaccion(), _interaccion()]}))
    cinco = [_interaccion(name=f"i{n}") for n in range(5)]
    with pytest.raises(DevelopmentTargetError):
        target_from_mapping("hn", _mapa_destino({"interactions": cinco}))


def test_1d_sin_seccion_visual_no_hay_interacciones() -> None:
    """El comportamiento histórico se conserva."""
    destino = target_from_mapping("hn", _mapa_destino(None))

    assert destino.visual_interactions == () and destino.visual_routes == ()


@pytest.mark.parametrize(
    ("frase", "esperado"),
    [
        ("al pasar el cursor sobre un departamento este cambia", True),
        ("el elemento se resalta con hover", True),
        ("aparece un tooltip con el nombre", True),
        ("el mapa se integra visualmente con el diseno actual", False),
        ("los 18 departamentos se representan correctamente", False),
    ],
)
def test_2_un_criterio_de_interaccion_se_distingue_de_uno_estatico(
    frase: str, esperado: bool
) -> None:
    """Solo las transiciones exigen interacción real."""
    assert is_interaction_claim(frase) is esperado


# =========================================================== 3 · navegador REAL (Chrome/Edge)
PAGINA_REAL = """<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0">
<svg viewBox="0 0 300 200" style="width:600px" class="map">
 <a aria-label="Ver propiedades en Cortés"><path d="M20 20 L140 30 L120 120 L30 100 Z"
   class="dep" fill="#f4efe6" stroke="#8a7b6a"/></a>
 <a aria-label="Ver propiedades en Yoro"><path d="M160 30 L280 40 L270 130 L150 120 Z"
   class="dep" fill="#f4efe6" stroke="#8a7b6a"/></a>
</svg>
<p id="lab" role="status" style="font-size:28px">Pasa el cursor sobre un departamento</p>
<style>.dep:hover{fill:#0b5c72}</style>
<script>document.querySelectorAll('.map a').forEach(a=>{
 a.addEventListener('mouseenter',()=>{document.getElementById('lab').textContent=
  'Departamento: '+a.getAttribute('aria-label').replace('Ver propiedades en ','')});
 a.addEventListener('mouseleave',()=>{document.getElementById('lab').textContent=
  'Pasa el cursor sobre un departamento'})})</script></body></html>"""

PAGINA_SIN_EFECTO = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0">
<svg viewBox="0 0 300 200" style="width:600px" class="map">
 <a aria-label="Ver propiedades en Cortés"><path d="M20 20 L140 30 L120 120 L30 100 Z"
   class="dep" fill="#f4efe6" stroke="#8a7b6a"/></a></svg>
<p id="lab" role="status" style="font-size:28px">Pasa el cursor sobre un departamento</p>
</body></html>"""

PAGINA_TAPADA = """<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0">
<svg viewBox="0 0 300 200" style="width:600px" class="map">
 <a aria-label="Ver propiedades en Cortés"><path d="M20 20 L140 30 L120 120 L30 100 Z"
   class="dep" fill="#f4efe6"/></a></svg>
<div style="position:fixed;inset:0;background:rgba(255,255,255,.01)"></div>
</body></html>"""

#: El elemento cubre la esquina donde se aparca el cursor: ya está en hover **antes** de la
#: interacción, así que no hay transición que demostrar.
PAGINA_EN_HOVER_INICIAL = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0"><svg viewBox="0 0 100 100" style="width:900px" class="map">
 <a aria-label="Ver propiedades en Cortés"><path d="M0 0 L100 0 L100 100 L0 100 Z" class="dep"
   fill="#f4efe6"/></a></svg><style>.dep:hover{fill:#0b5c72}</style></body></html>"""

#: La etiqueta queda **debajo** del viewport: solo una captura de región la enseña.
PAGINA_ETIQUETA_LEJOS = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0"><svg viewBox="0 0 300 200" style="width:600px" class="map">
 <a aria-label="Ver propiedades en Cortés"><path d="M20 20 L140 30 L120 120 L30 100 Z" class="dep"
   fill="#f4efe6"/></a></svg><div style="height:900px"></div>
<p id="lab" style="font-size:28px">Pasa el cursor</p><style>.dep:hover{fill:#0b5c72}</style>
<script>document.querySelector('.map a').addEventListener('mouseenter',()=>{
 document.getElementById('lab').textContent='Departamento: Cortés'})</script></body></html>"""

NAVEGADOR = None
try:
    NAVEGADOR = HeadlessBrowserCapture().find_browser()
except CaptureError:
    NAVEGADOR = None

necesita_navegador = pytest.mark.skipif(
    NAVEGADOR is None, reason="esta máquina no tiene Chrome/Edge instalado"
)


@pytest.fixture(scope="module")
def servidor(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Servidor de bucle local con las páginas de prueba: devuelve la URL base."""
    raiz = tmp_path_factory.mktemp("hover")
    for nombre, html in (
        ("real.html", PAGINA_REAL),
        ("sin_efecto.html", PAGINA_SIN_EFECTO),
        ("tapada.html", PAGINA_TAPADA),
        ("en_hover.html", PAGINA_EN_HOVER_INICIAL),
        ("lejos.html", PAGINA_ETIQUETA_LEJOS),
    ):
        (raiz / nombre).write_text(html, encoding="utf-8")

    class _Silencioso(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return None

    servidor = socketserver.TCPServer(
        ("127.0.0.1", 0), functools.partial(_Silencioso, directory=str(raiz))
    )
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{servidor.server_address[1]}"
    finally:
        servidor.shutdown()
        servidor.server_close()


def _especificacion(base: str, pagina: str, **cambios: Any) -> VisualInteraction:
    valores: dict[str, Any] = {
        "name": "hover-depto",
        "route": f"{base}/{pagina}",
        "hover": SELECTOR,
        "label": "#lab",
        "settle_ms": 300,
    }
    valores.update(cambios)
    return VisualInteraction(**valores)


@necesita_navegador
def test_3_el_hover_se_ejecuta_de_verdad_y_deja_antes_y_despues(servidor: str) -> None:
    """Chrome real: el navegador confirma ``:hover``, cambian los píxeles y aparece el nombre."""
    evidencia = BrowserInteraction().run(_especificacion(servidor, "real.html"), (900, 500))

    assert evidencia.usable, evidencia.error
    assert evidencia.matches == 1 and "Cortés" in evidencia.target
    assert evidencia.hover_before is False, "el estado inicial no está en hover"
    assert evidencia.hover_applied is True, "el navegador confirmó el hover real"
    assert evidencia.label_text == "Departamento: Cortés"
    assert evidencia.before is not None and evidencia.after is not None
    assert (evidencia.before.phase, evidencia.after.phase) == ("before", "after")
    assert evidencia.before.data.startswith(b"\x89PNG") and evidencia.after.data.startswith(
        b"\x89PNG"
    )
    assert evidencia.before.sha256 != evidencia.after.sha256
    assert evidencia.pixels_changed is True


def _alto_png(datos: bytes) -> int:
    """Alto en píxeles leído de la cabecera IHDR del PNG."""
    return int.from_bytes(datos[20:24], "big")


@necesita_navegador
def test_3c_la_captura_cubre_el_elemento_y_su_etiqueta_aunque_esta_quede_fuera_del_viewport(
    servidor: str,
) -> None:
    """Sin la región, una etiqueta bajo el pliegue nunca aparecería en la evidencia."""
    evidencia = BrowserInteraction().run(_especificacion(servidor, "lejos.html"), (900, 500))

    assert evidencia.usable, evidencia.error
    assert evidencia.region == "element+label"
    assert evidencia.label_text == "Departamento: Cortés"
    assert evidencia.after is not None and evidencia.before is not None
    assert _alto_png(evidencia.after.data) > 500, (
        "la captura excede el viewport: incluye la etiqueta"
    )
    assert _alto_png(evidencia.before.data) == _alto_png(evidencia.after.data)


@necesita_navegador
def test_3b_un_hover_sin_efecto_visual_se_detecta_con_los_pixeles(servidor: str) -> None:
    """El hover ocurre (el DOM lo confirma) pero no cambia nada: ``pixels_changed`` es falso."""
    evidencia = BrowserInteraction().run(_especificacion(servidor, "sin_efecto.html"), (900, 500))

    assert evidencia.hover_applied is True
    assert evidencia.pixels_changed is False, "antes y después son idénticos"
    assert evidencia.label_text == "Pasa el cursor sobre un departamento"


@necesita_navegador
@pytest.mark.parametrize(
    ("pagina", "cambios", "motivo"),
    [
        pytest.param(
            "real.html",
            {"hover": '.map a[aria-label="Ver propiedades en Atlántida"] path'},
            "no localiza ningún elemento",
            id="selector-sin-coincidencias",
        ),
        pytest.param("real.html", {"index": 7}, "fuera de rango", id="index-fuera-de-rango"),
        pytest.param("tapada.html", {}, "ningún punto del elemento", id="elemento-tapado-por-otro"),
        pytest.param(
            "en_hover.html", {}, "no reportó el elemento en :hover", id="ya-estaba-en-hover"
        ),
    ],
)
def test_4_si_no_se_localiza_el_elemento_no_hay_interaccion_demostrable(
    servidor: str, pagina: str, cambios: dict[str, Any], motivo: str
) -> None:
    """Sin elemento localizado que reciba el cursor, la interacción no es demostrable (UNCLEAR)."""
    evidencia = BrowserInteraction().run(_especificacion(servidor, pagina, **cambios), (900, 500))

    assert not evidencia.usable and motivo in evidencia.error
    assert evidencia.hover_applied is False, "no hay transición demostrada: no se aprueba nada"


@necesita_navegador
def test_4b_solo_se_interactua_con_el_bucle_local() -> None:
    """Ninguna ruta fuera de la máquina."""
    spec = VisualInteraction(
        name="fuera", route="https://ejemplo.com/", hover="a", label="", settle_ms=100
    )

    evidencia = BrowserInteraction().run(spec, (900, 500))

    assert not evidencia.usable and "bucle local" in evidencia.error


# ============================================ 5 · evaluación con dobles (contrato y degradaciones)
def _evidencia(*, cambia: bool = True, error: str = "") -> InteractionEvidence:
    antes = CapturedShot("http://localhost:3000/p", (1280, 900), PNG, phase="before")
    despues = CapturedShot(
        "http://localhost:3000/p", (1280, 900), PNG + (b"\x01" if cambia else b""), phase="after"
    )
    return InteractionEvidence(
        name="hover-depto",
        route="http://localhost:3000/p",
        viewport=(1280, 900),
        matches=18,
        target="path [Ver propiedades en Cortés]",
        hover_before=False,
        hover_applied=not error,
        label_text="Departamento: Cortés",
        before=None if error else antes,
        after=None if error else despues,
        pixels_changed=cambia and not error,
        error=error,
    )


class _RouterDoble:
    """Router de prueba: ruta efectiva (o no) y respuesta fija; cuenta las ejecuciones."""

    def __init__(self, respuesta: str = "", *, ruta: bool = True) -> None:
        from punto.providers.failover import RouteChoice

        self.respuesta = respuesta
        self.ejecuciones: list[Any] = []
        self._ruta = (
            RouteChoice(
                assigned="anthropic",
                provider="openai",
                model="gpt-5.6-sol",
                transport="codex",
                via_failover=True,
            )
            if ruta
            else RouteChoice(assigned="anthropic", reason="anthropic: sin capacidad efectiva")
        )

    def resolve_route(self, role: ProviderRole, *, needs_vision: bool = False) -> Any:
        return self._ruta

    def execute(self, role: ProviderRole, request: Any, **kwargs: Any) -> Any:
        from punto.providers.contract import ProviderResult, ProviderStatus

        self.ejecuciones.append(request)
        return ProviderResult(
            request_id=request.request_id,
            provider="openai",
            model="gpt-5.6-sol",
            status=ProviderStatus.SUCCESS,
            role=role,
            content=self.respuesta,
        )


def _veredicto(valor: str) -> str:
    return json.dumps({"verdicts": [{"claim": 1, "verdict": valor, "observation": "se ve"}]})


def test_5_con_evidencia_real_el_par_antes_despues_llega_a_visual_qa() -> None:
    """Dos imágenes en orden (antes, después) y los hechos deterministas de la interacción."""
    router = _RouterDoble(_veredicto("PASS"))

    resultado = assess_interaction_claims(
        router, [CRITERIO_HOVER], [_evidencia()], request_id="t-1"
    )

    assert resultado.performed and resultado.verdicts[0].verdict == "PASS"
    assert (resultado.provider, resultado.transport) == ("openai", "codex")
    (peticion,) = router.ejecuciones
    assert [imagen.data for imagen in peticion.attachments] == [PNG, PNG + b"\x01"]
    assert "image 1 = BEFORE, image 2 = AFTER" in peticion.context
    assert "browser reports :hover before=False after=True" in peticion.context
    assert [shot.phase for shot in resultado.shots] == ["before", "after"]


def test_5b_un_pass_con_capturas_identicas_se_degrada_a_unclear() -> None:
    """Un revisor no puede haber visto un cambio que no ocurrió: PASS → UNCLEAR."""
    router = _RouterDoble(_veredicto("PASS"))

    resultado = assess_interaction_claims(
        router, [CRITERIO_HOVER], [_evidencia(cambia=False)], request_id="t-1"
    )

    assert resultado.verdicts[0].verdict == "UNCLEAR"
    assert "idénticas" in resultado.verdicts[0].observation


def test_5c_un_fail_del_revisor_no_se_toca() -> None:
    """La degradación solo aplica a PASS."""
    resultado = assess_interaction_claims(
        _RouterDoble(_veredicto("FAIL")),
        [CRITERIO_HOVER],
        [_evidencia(cambia=False)],
        request_id="t",
    )

    assert resultado.verdicts[0].verdict == "FAIL"


def test_6_sin_interaccion_demostrable_no_se_llama_a_visual_qa() -> None:
    """Elemento no localizado / hover no confirmado: nadie evalúa; el motivo es determinista."""
    router = _RouterDoble(_veredicto("PASS"))

    resultado = assess_interaction_claims(
        router,
        [CRITERIO_HOVER],
        [_evidencia(error="el selector 'x' no localiza ningún elemento en la ruta")],
        request_id="t",
    )

    assert not resultado.performed and "no localiza ningún elemento" in resultado.error
    assert router.ejecuciones == []


def test_6b_sin_ruta_visual_efectiva_no_se_llama_a_nadie() -> None:
    """La capacidad efectiva se exige también para las interacciones."""
    router = _RouterDoble(_veredicto("PASS"), ruta=False)

    resultado = assess_interaction_claims(router, [CRITERIO_HOVER], [_evidencia()], request_id="t")

    assert not resultado.performed and "sin ruta visual efectiva" in resultado.error
    assert router.ejecuciones == []


@pytest.mark.parametrize("contenido", ["basura", "", '{"verdicts": []}'])
def test_6c_una_respuesta_dudosa_nunca_es_pass(contenido: str) -> None:
    """Salida ilegible o vacía: UNCLEAR."""
    resultado = assess_interaction_claims(
        _RouterDoble(contenido), [CRITERIO_HOVER], [_evidencia()], request_id="t"
    )

    assert resultado.verdicts[0].verdict == "UNCLEAR"


# ============================================ 7 · ciclo + consola: evidencia ligada a la Task
class _EjecutorFalso:
    """Ejecutor de interacciones de prueba: devuelve una evidencia fija y registra las llamadas."""

    def __init__(self, evidencia: InteractionEvidence) -> None:
        self.evidencia = evidencia
        self.llamadas: list[VisualInteraction] = []

    def run(self, spec: VisualInteraction, viewport: tuple[int, int]) -> InteractionEvidence:
        self.llamadas.append(spec)
        return self.evidencia


class _CapturaEstatica:
    """Captor estático de prueba."""

    def __init__(self) -> None:
        self.llamadas = 0

    def capture(self, urls: Sequence[str], viewport: tuple[int, int]) -> tuple[CapturedShot, ...]:
        self.llamadas += 1
        return (CapturedShot("http://localhost:3000/", viewport, PNG),)


ESPEC = VisualInteraction(
    name="hover-depto",
    route="http://localhost:3000/propiedades",
    hover=SELECTOR,
    label=".map-active-label",
)


def _consola(
    tmp_path: Path,
    *,
    veredicto: str,
    ejecutor: _EjecutorFalso | None,
    interacciones: tuple[VisualInteraction, ...] = (ESPEC,),
    captura: _CapturaEstatica | None = None,
) -> tuple[TestClient, AuditLogger, _Multimodal]:
    """Consola real con ciclo real y VISUAL_QA por Codex (doble); la interacción se inyecta."""
    repo, remoto = _repos(tmp_path)
    target = replace(
        _target(repo, remoto=remoto),
        visual_routes=("http://localhost:3000/propiedades",),
        visual_interactions=interacciones,
    )
    audit = AuditLogger()

    def gpt_responde(imagenes: int) -> str:
        if imagenes:
            return json.dumps(
                {"verdicts": [{"claim": 1, "verdict": veredicto, "observation": "así se ve"}]}
            )
        return json.dumps(_plan())

    gpt = _Multimodal("openai", "gpt-5.6-sol", gpt_responde)
    claude = _Multimodal("anthropic", "claude-sonnet-5", lambda n: "{}")
    deepseek = _Multimodal("deepseek", "deepseek-v4-pro", lambda n: json.dumps(_cambio()))
    router = ProviderRouter()
    for cliente in (gpt, claude, deepseek):
        router.register_provider(cliente.provider, lambda _m, c=cliente: c, model=cliente.model)
    router.configure_failover(
        FailoverPolicy(
            roles={ProviderRole.BUILDER: ("anthropic",), ProviderRole.VISUAL_QA: ("openai",)}
        ),
        _evaluador_visual,
    )
    ciclo = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(max_repair_rounds=2, max_structural_corrections=2),
        audit=audit,
        policy_engine=PolicyEngine.from_config(),
        visual_capture=captura,
        visual_interaction=ejecutor,
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
    return TestClient(aplicacion), audit, gpt


SOLICITUD = {
    "objective": "unificar la lista de tipos en una sola fuente",
    "target_id": TARGET_ID,
    "acceptance_criteria": [CRITERIO_HOVER],
    "scope_paths": ["src"],
}


def test_7_un_pass_de_interaccion_satisface_el_criterio_y_deja_la_evidencia_completa(
    tmp_path: Path,
) -> None:
    """Hover real → antes/después → Codex → PASS → criterio satisfecho, todo ligado a la Task."""
    ejecutor = _EjecutorFalso(_evidencia())
    client, audit, gpt = _consola(tmp_path, veredicto="PASS", ejecutor=ejecutor)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    assert [spec.name for spec in ejecutor.llamadas] == ["hover-depto"]
    (evidencia,) = tarea["development"]["visual_evidence"]
    assert evidencia["request_id"] == tarea["task_id"]
    assert evidencia["verdict"] == "PASS" and evidencia["claim"] == CRITERIO_HOVER
    assert evidencia["interaction"] == "hover:hover-depto"
    assert evidencia["route"] == "http://localhost:3000/p"
    assert evidencia["target_element"] == "path [Ver propiedades en Cortés]"
    assert evidencia["hover_applied"] is True and evidencia["pixels_changed"] is True
    assert (evidencia["provider"], evidencia["model"], evidencia["transport"]) == (
        "openai",
        "gpt-5.6-sol",
        "codex",
    )
    assert [foto["phase"] for foto in evidencia["screenshots"]] == ["before", "after"]
    assert evidencia["screenshots"][0]["sha256"] != evidencia["screenshots"][1]["sha256"]
    assert evidencia["applied_digest"]
    assert tarea["attempts"][0]["visual"] == "openai/codex: PASS=1 (hover)"
    assert len(gpt.imagenes) == 1 and len(gpt.imagenes[0]) == 2, "Codex recibió antes y después"
    assert any(dict(e.metadata).get("interaction") for e in audit.by_type(_ASSESSED))


def test_7b_sin_elemento_localizado_el_criterio_es_unclear_y_no_se_llama_a_codex(
    tmp_path: Path,
) -> None:
    """Interacción no demostrable ⇒ UNCLEAR ⇒ EVIDENCE_REQUIRED; nunca un PASS fabricado."""
    ejecutor = _EjecutorFalso(
        _evidencia(error="el selector 'x' no localiza ningún elemento en la ruta")
    )
    client, _audit, gpt = _consola(tmp_path, veredicto="PASS", ejecutor=ejecutor)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    desarrollo = tarea["development"]
    assert desarrollo["error_kind"] == "EVIDENCE_REQUIRED"
    (evidencia,) = desarrollo["visual_evidence"]
    assert evidencia["verdict"] == "UNCLEAR" and evidencia["provider"] == ""
    assert "no localiza ningún elemento" in evidencia["observation"]
    assert gpt.imagenes == [], "sin interacción demostrable no se le entrega nada al proveedor"
    assert tarea["stage"] == "WAITING_HUMAN" and len(tarea["gates"]) == 1


@pytest.mark.parametrize(
    "escenario",
    [
        pytest.param({"interacciones": ()}, id="destino-sin-interacciones-declaradas"),
        pytest.param({"ejecutor": None}, id="sin-ejecutor"),
    ],
)
def test_7c_sin_interaccion_declarada_o_sin_ejecutor_no_hay_pass(
    tmp_path: Path, escenario: dict[str, Any]
) -> None:
    """Ni interacción declarada ni ejecutor: UNCLEAR con el motivo, sin tocar a Codex."""
    ejecutor = _EjecutorFalso(_evidencia())
    argumentos: dict[str, Any] = {"veredicto": "PASS", "ejecutor": ejecutor} | escenario
    client, _audit, gpt = _consola(tmp_path, **argumentos)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["development"]["error_kind"] == "EVIDENCE_REQUIRED"
    assert tarea["development"]["visual_evidence"][0]["verdict"] == "UNCLEAR"
    assert gpt.imagenes == []
    if "interacciones" in escenario:
        assert ejecutor.llamadas == [], "no hay nada declarado que ejecutar"


def test_7d_capturas_identicas_no_satisfacen_el_criterio_aunque_el_modelo_diga_pass(
    tmp_path: Path,
) -> None:
    """La comprobación determinista de píxeles manda sobre el juicio del modelo."""
    ejecutor = _EjecutorFalso(_evidencia(cambia=False))
    client, _audit, _gpt = _consola(tmp_path, veredicto="PASS", ejecutor=ejecutor)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["development"]["error_kind"] == "EVIDENCE_REQUIRED"
    assert tarea["development"]["visual_evidence"][0]["verdict"] == "UNCLEAR"
    assert tarea["development"]["visual_evidence"][0]["pixels_changed"] is False


def test_7e_un_fail_del_revisor_no_completa_el_desarrollo(tmp_path: Path) -> None:
    """FAIL es un criterio no satisfecho, no un PASS."""
    client, _a, _g = _consola(tmp_path, veredicto="FAIL", ejecutor=_EjecutorFalso(_evidencia()))

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] != "DEVELOPMENT_COMPLETED"
    assert tarea["development"]["visual_evidence"][0]["verdict"] == "FAIL"


def test_8_criterios_estaticos_y_de_interaccion_usan_su_propia_evidencia(tmp_path: Path) -> None:
    """Uno por captura estática y otro por interacción real: dos registros, cada uno con su tipo."""
    ejecutor = _EjecutorFalso(_evidencia())
    captura = _CapturaEstatica()
    client, _audit, gpt = _consola(tmp_path, veredicto="PASS", ejecutor=ejecutor, captura=captura)
    solicitud = SOLICITUD | {
        "acceptance_criteria": [
            "el mapa se integra visualmente con el diseno actual",
            CRITERIO_HOVER,
        ]
    }

    tarea = client.post("/console/tasks", json=solicitud).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    registros = tarea["development"]["visual_evidence"]
    assert sorted(item["interaction"] for item in registros) == ["", "hover:hover-depto"]
    estatico = next(item for item in registros if not item["interaction"])
    assert [foto["phase"] for foto in estatico["screenshots"]] == ["static"]
    assert captura.llamadas == 1 and len(ejecutor.llamadas) == 1
    assert len(gpt.imagenes) == 2, "una evaluación por tipo de evidencia"


def test_9_sin_ampliacion_de_autoridad(tmp_path: Path) -> None:
    """Un PASS de interacción completa el desarrollo local: no resuelve gates ni publica."""
    client, _a, _g = _consola(tmp_path, veredicto="PASS", ejecutor=_EjecutorFalso(_evidencia()))

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["development"]["published"] is False and tarea["development"]["commit_sha"]
    assert tarea["gates"] == [] and client.get("/console/human-gates").json()["pending"] == 0


from punto.schemas.audit import AuditEventType  # noqa: E402

_ASSESSED = AuditEventType.DEV_VISUAL_ASSESSED
