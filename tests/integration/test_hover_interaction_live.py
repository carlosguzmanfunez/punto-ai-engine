"""Gate vivo: hover REAL en un navegador real + VISUAL_QA real (Codex) sobre el antes/después.

Sin dobles del navegador ni del proveedor. Un servidor de bucle local sirve tres páginas:

- ``completa``: al pasar el cursor el departamento cambia de color **y** aparece su nombre;
- ``sin_nombre``: cambia de color pero **no** aparece el nombre (un cambio verdadero pero parcial);
- ``sin_efecto``: el cursor pasa y no cambia nada (un cambio falso).

La cadena de PUNTO es la de producción (``BrowserInteraction`` → ``ProviderRegistry`` →
``resolve_route`` → ``CodexTransport --image``). Lo que se comprueba:

- el hover realmente se ejecuta (el navegador lo confirma) y hay evidencia antes/después;
- Codex distingue un cambio visual verdadero de uno parcial y de uno falso;
- la ausencia de interacción demostrable (selector que no localiza nada) es ``UNCLEAR`` sin llamar a
  Codex.

Sin Chrome/Edge, Codex con sesión o ``--image`` la prueba **falla con la causa** (no hay ``skip``).

    .venv/Scripts/python -m pytest tests/integration/test_hover_interaction_live.py -q -s
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from punto.providers.contract import ProviderRole
from punto.providers.registry import ProviderRegistry
from punto.visualqa.interaction import BrowserInteraction, assess_interaction_claims
from punto.workspace.target import VisualInteraction

MAPA = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;font-family:Arial">
<svg viewBox="0 0 300 200" style="width:700px" class="map">
 <a aria-label="Ver propiedades en Cortés"><path d="M20 20 L140 30 L120 120 L30 100 Z" class="dep"
   fill="#f4efe6" stroke="#8a7b6a"/></a>
 <a aria-label="Ver propiedades en Yoro"><path d="M160 30 L280 40 L270 130 L150 120 Z" class="dep"
   fill="#f4efe6" stroke="#8a7b6a"/></a></svg>
<p id="lab" role="status" style="font-size:30px;padding:0 20px">
Pasa el cursor sobre un departamento</p>
%(estilo)s%(script)s</body></html>"""

ESTILO_COLOR = "<style>.dep:hover{fill:#0b5c72}</style>"
SCRIPT_NOMBRE = """<script>document.querySelectorAll('.map a').forEach(a=>{
 a.addEventListener('mouseenter',()=>{document.getElementById('lab').textContent=
  'Departamento: '+a.getAttribute('aria-label').replace('Ver propiedades en ','')});
 a.addEventListener('mouseleave',()=>{document.getElementById('lab').textContent=
  'Pasa el cursor sobre un departamento'})})</script>"""

PAGINAS = {
    "completa.html": MAPA % {"estilo": ESTILO_COLOR, "script": SCRIPT_NOMBRE},
    "sin_nombre.html": MAPA % {"estilo": ESTILO_COLOR, "script": ""},
    "sin_efecto.html": MAPA % {"estilo": "", "script": ""},
}

CRITERIOS = [
    "Al pasar el cursor sobre un departamento del mapa, ese departamento cambia de color",
    "Al pasar el cursor sobre un departamento del mapa, aparece el nombre de ese departamento",
]

SELECTOR = '.map a[aria-label="Ver propiedades en Cortés"] path'


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Servidor de bucle local con las tres páginas."""
    raiz: Path = tmp_path_factory.mktemp("hover-live")
    for nombre, html in PAGINAS.items():
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


def _evaluar(base: str, pagina: str, router: object) -> tuple[dict[int, str], object]:
    spec = VisualInteraction(
        name="hover-departamento",
        route=f"{base}/{pagina}",
        hover=SELECTOR,
        label="#lab",
        settle_ms=400,
    )
    evidencia = BrowserInteraction().run(spec, (900, 500))
    assert evidencia.usable, evidencia.error
    assert evidencia.hover_applied and evidencia.before is not None and evidencia.after is not None
    evaluacion = assess_interaction_claims(
        router, CRITERIOS, [evidencia], request_id=f"live-hover-{pagina}"
    )
    assert evaluacion.performed, evaluacion.error
    assert (evaluacion.provider, evaluacion.transport) == ("openai", "codex")
    veredictos = {item.claim: item.verdict for item in evaluacion.verdicts}
    print(f"\n[{pagina}] pixels_changed={evidencia.pixels_changed} label={evidencia.label_text!r}")
    for item in evaluacion.verdicts:
        print(f"   criterio {item.claim}: {item.verdict} - {item.observation}")
    return veredictos, evidencia


def test_hover_real_y_codex_distinguen_cambio_verdadero_parcial_y_falso(base: str) -> None:
    """Chrome real (hover) + Codex real: PASS solo donde el cambio ocurre de verdad."""
    router = ProviderRegistry().router_instance()
    ruta = router.resolve_route(ProviderRole.VISUAL_QA, needs_vision=True)
    assert ruta.available and ruta.provider == "openai" and ruta.transport == "codex", ruta.reason

    completa, ev_completa = _evaluar(base, "completa.html", router)
    sin_nombre, _ = _evaluar(base, "sin_nombre.html", router)
    sin_efecto, ev_sin_efecto = _evaluar(base, "sin_efecto.html", router)

    assert ev_completa.pixels_changed is True and ev_completa.label_text == "Departamento: Cortés"
    assert completa == {1: "PASS", 2: "PASS"}, "el cambio verdadero completo debe aprobarse"
    assert sin_nombre[1] == "PASS", "el cambio de color existe"
    assert sin_nombre[2] != "PASS", "el nombre NO aparece: no puede aprobarse"
    assert ev_sin_efecto.pixels_changed is False
    assert "PASS" not in sin_efecto.values(), "sin cambio visual no hay PASS"


def test_sin_interaccion_demostrable_es_unclear_sin_llamar_a_codex(base: str) -> None:
    """Selector que no localiza el elemento: no hay interacción, no se llama a nadie."""
    router = ProviderRegistry().router_instance()
    spec = VisualInteraction(
        name="hover-inexistente",
        route=f"{base}/completa.html",
        hover='.map a[aria-label="Ver propiedades en Atlántida"] path',
        settle_ms=200,
    )

    evidencia = BrowserInteraction().run(spec, (900, 500))
    evaluacion = assess_interaction_claims(router, CRITERIOS, [evidencia], request_id="live-nada")

    assert not evidencia.usable and "no localiza ningún elemento" in evidencia.error
    assert not evaluacion.performed and evaluacion.provider == ""
    assert "no demostrable" in evaluacion.error
