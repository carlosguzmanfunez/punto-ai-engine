"""Gate vivo: ¿el transporte OpenAI/Codex configurado consume imágenes de verdad?

Prueba **real**, sin dobles del proveedor: un servidor de bucle local sirve una página con un código
aleatorio que solo existe en los píxeles, Chrome/Edge headless la captura y la cadena de PUNTO
(``ProviderRegistry`` → ``resolve_route`` → ``ProviderRouter`` → ``CodexTransport --image``) la
evalúa con la sesión ChatGPT de Codex.

Discrimina: un criterio **verdadero** (el código visible) tiene que dar ``PASS`` y uno **falso** (un
código que no está) no puede dar ``PASS``. Un modelo que no viera la imagen no podría acertar el
código aleatorio.

Sin Codex con sesión, sin navegador o con un Codex que no anuncia ``--image``, la prueba **falla con
la causa** (no hay ``skip`` ni PASS simulado): así queda a la vista qué transporte falta.

    .venv/Scripts/python -m pytest tests/integration/test_codex_vision_live.py -q -s
"""

from __future__ import annotations

import functools
import http.server
import random
import socketserver
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from punto.providers.contract import ProviderRole
from punto.providers.registry import ProviderRegistry
from punto.providers.transports.codex import CodexTransport
from punto.visualqa.dev_evidence import HeadlessBrowserCapture, assess_visual_claims

PAGINA = """<!doctype html><html><body style="font-family:Arial;background:#f4f1ea;margin:0">
<div style="padding:40px"><h1 style="font-size:44px;margin:0 0 20px">Mapa de cobertura nacional</h1>
<p style="font-size:30px">Departamento: <b>Cortés</b> &middot; Código de verificación:
<b style="font-size:46px;color:#0a5">{codigo}</b></p>
<button style="font-size:28px;padding:14px 28px;background:#c0392b;color:white;border:0;
border-radius:8px">Ver propiedades</button></div></body></html>"""


@pytest.fixture
def pagina(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """Servidor de bucle local con una página de código aleatorio: ``(url, código)``."""
    codigo = "QX-" + str(random.randint(10000, 99999))
    (tmp_path / "pagina.html").write_text(PAGINA.format(codigo=codigo), encoding="utf-8")

    class _Silencioso(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return None

    servidor = socketserver.TCPServer(
        ("127.0.0.1", 0), functools.partial(_Silencioso, directory=str(tmp_path))
    )
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{servidor.server_address[1]}/pagina.html", codigo
    finally:
        servidor.shutdown()
        servidor.server_close()


def test_codex_consume_imagenes_por_el_transporte_configurado(pagina: tuple[str, str]) -> None:
    """Captura real → ruta efectiva → Codex con ``--image`` → veredicto discriminante."""
    url, codigo = pagina
    assert CodexTransport(model="x").accepts_images(), (
        "el binario de Codex instalado no anuncia --image: falta un Codex con soporte de imágenes "
        "(o el transporte api) para tener VISION efectiva"
    )
    router = ProviderRegistry().router_instance()
    ruta = router.resolve_route(ProviderRole.VISUAL_QA, needs_vision=True)
    assert ruta.available and ruta.provider == "openai" and ruta.transport == "codex", ruta.reason

    capturas = HeadlessBrowserCapture().capture([url], (1000, 420))
    evaluacion = assess_visual_claims(
        router,
        [
            f"La página muestra el código de verificación {codigo}",
            "La página muestra el código de verificación ZZ-00000",
            "Al pasar el cursor sobre el botón, este cambia de color",
        ],
        capturas,
        request_id="live-codex-vision",
    )

    assert evaluacion.performed, evaluacion.error
    assert (evaluacion.provider, evaluacion.transport) == ("openai", "codex")
    verdictos = {item.claim: item.verdict for item in evaluacion.verdicts}
    print(f"\nproveedor={evaluacion.provider}/{evaluacion.model} transporte={evaluacion.transport}")
    for item in evaluacion.verdicts:
        print(f"  criterio {item.claim}: {item.verdict} - {item.observation}")
    assert verdictos[1] == "PASS", "no leyó el código aleatorio que solo está en los píxeles"
    assert verdictos[2] != "PASS", "aprobó un código que no está en la captura"
    assert verdictos[3] != "PASS", (
        "aprobó una interacción que una captura estática no puede mostrar"
    )
