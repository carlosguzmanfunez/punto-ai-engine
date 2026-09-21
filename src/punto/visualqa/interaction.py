"""Evidencia visual **interactiva** (hover): interacción real, antes/después y veredicto.

Una captura estática no puede demostrar «al pasar el cursor sobre un departamento este cambia y
aparece su nombre»: ese criterio es una **transición**. Este módulo la demuestra con evidencia
real y determinista, sin inferir nada de una sola imagen:

1. ``BrowserInteraction`` abre la ruta de bucle local en un Chrome/Edge headless real (protocolo
   DevTools sobre ``websockets``, que ya trae uvicorn: no hay un segundo sistema de navegador ni
   dependencias nuevas), localiza el **elemento declarado** por el destino con un selector CSS,
   captura el **estado inicial**, mueve el ratón con eventos de entrada reales (no ``:hover``
   simulado por script) sobre un punto que **de verdad** golpea al elemento, comprueba que el
   navegador lo reporta en ``:hover`` y captura el **estado posterior**;
2. ``assess_interaction_claims`` entrega a VISUAL_QA —por su ruta de capacidad **efectiva**— el
   par antes/después junto con los hechos deterministas de la interacción y valida el veredicto
   con el mismo contrato cerrado (``PASS``/``FAIL``/``UNCLEAR``).

Reglas que no se relajan:

- si el elemento no se localiza, no hay un punto que reciba el cursor o el navegador no reporta
  el hover, la interacción **no es demostrable** y el criterio queda ``UNCLEAR`` (no se llama a
  nadie y nunca se fabrica un ``PASS``);
- un ``PASS`` del modelo se degrada a ``UNCLEAR`` si las capturas antes/después son **idénticas**
  byte a byte: un revisor no puede haber visto un cambio que no ocurrió;
- solo rutas de bucle local; el proceso del navegador y su perfil temporal se eliminan siempre;
- sin autoridad nueva: el veredicto es evidencia, no aprueba gates, no escribe ni publica.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Protocol

from punto.providers.contract import ProviderRole, make_request
from punto.visualqa.dev_evidence import (
    MAX_OBSERVATION_CHARS,
    CapturedShot,
    CaptureError,
    ClaimVerdict,
    HeadlessBrowserCapture,
    VisualAssessment,
    is_loopback_url,
    parse_verdicts,
)
from punto.workspace.target import VisualInteraction

__all__ = [
    "BrowserInteraction",
    "InteractionEvidence",
    "InteractionRunner",
    "assess_interaction_claims",
]

#: Tiempo máximo (s) de cada espera del navegador.
_WAIT_SECONDS: Final[float] = 30.0

#: Puntos de muestreo por eje sobre la caja del elemento para hallar uno que reciba el cursor.
_GRID: Final[int] = 14


@dataclass(frozen=True, slots=True)
class InteractionEvidence:
    """Hechos deterministas de una interacción real: qué se hizo, sobre qué y qué se vio."""

    name: str
    route: str
    viewport: tuple[int, int]
    #: Cuántos elementos coincidían con el selector y cuál se usó (por ``index``).
    matches: int = 0
    #: Descripción legible del elemento objetivo (etiqueta, id, aria-label, texto acotado).
    target: str = ""
    hover_before: bool = False
    #: True solo si el navegador reporta el elemento en ``:hover`` tras el movimiento real.
    hover_applied: bool = False
    #: Texto del elemento ``label`` tras la interacción (si el destino declaró uno y existe).
    label_text: str = ""
    before: CapturedShot | None = None
    after: CapturedShot | None = None
    #: Región que cubren las capturas: ``element+label``, ``element`` o ``viewport``.
    region: str = "viewport"
    #: True si el estado posterior difiere byte a byte del inicial.
    pixels_changed: bool = False
    #: Vacío si la interacción se ejecutó; si no, por qué no es demostrable.
    error: str = ""

    @property
    def usable(self) -> bool:
        """True si hay antes y después de una interacción que el navegador confirmó."""
        return (
            not self.error
            and self.hover_applied
            and self.before is not None
            and (self.after is not None)
        )


class InteractionRunner(Protocol):
    """Ejecuta una interacción declarada y devuelve su evidencia («no demostrable» no lanza)."""

    def run(self, spec: VisualInteraction, viewport: tuple[int, int]) -> InteractionEvidence:
        """Devuelve la evidencia; un fallo se expresa en ``error``."""
        ...  # pragma: no cover - protocolo


# --------------------------------------------------------------------------------- navegador real
_FIND_JS: Final[str] = """
(() => {
  const selector = %(selector)s, index = %(index)d;
  const found = [...document.querySelectorAll(selector)];
  if (!found.length) return { matches: 0 };
  const el = found[index];
  if (!el) return { matches: found.length, missing: true };
  // Instantáneo: con `scroll-behavior: smooth` en la página, un scroll animado deja las
  // coordenadas obsoletas y el hover cae en otro sitio.
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  const box = el.getBoundingClientRect();
  const owner = el.closest('[aria-label]');
  const text = (el.textContent || '').trim();
  const describe = el.tagName.toLowerCase()
    + (el.id ? '#' + el.id : '')
    + (owner ? ' [' + (owner.getAttribute('aria-label') || '').slice(0, 80) + ']' : '')
    + (text ? ' "' + text.slice(0, 60) + '"' : '');
  // Punto del elemento que de verdad recibe el cursor: se muestrea una rejilla sobre su caja, de
  // menor a mayor distancia al centro (orden determinista) y se exige que el elemento esté debajo.
  const cx = box.x + box.width / 2, cy = box.y + box.height / 2, grid = %(grid)d, points = [];
  for (let i = 0; i < grid; i++) for (let j = 0; j < grid; j++) {
    const x = box.x + (i + 0.5) * box.width / grid, y = box.y + (j + 0.5) * box.height / grid;
    points.push([Math.hypot(x - cx, y - cy), x, y]);
  }
  points.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]);
  for (const [, x, y] of points) {
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue;
    const top = document.elementFromPoint(x, y);
    if (top && (top === el || el.contains(top))) {
      return { matches: found.length, target: describe, x, y, hit: true };
    }
  }
  return { matches: found.length, target: describe, hit: false };
})()
"""

_STATE_JS: Final[str] = """
(() => {
  const found = [...document.querySelectorAll(%(selector)s)];
  const el = found[%(index)d];
  const label = %(label)s ? document.querySelector(%(label)s) : null;
  return {
    hover: !!el && el.matches(':hover'),
    label: label ? (label.textContent || '').trim().slice(0, 120) : ''
  };
})()
"""


#: Región de la página que la evidencia debe cubrir: el elemento objetivo **y** la etiqueta donde se
#: espera un texto, con margen. Sin ella una etiqueta fuera del viewport nunca aparecería en la
#: captura y el revisor no podría ver lo que el criterio exige.
_REGION_JS: Final[str] = """
(() => {
  const target = [...document.querySelectorAll(%(selector)s)][%(index)d];
  const label = %(label)s ? document.querySelector(%(label)s) : null;
  const boxes = [target, label].filter(Boolean).map((el) => el.getBoundingClientRect());
  if (!boxes.length) return null;
  const pad = 24;
  const left = Math.min(...boxes.map((b) => b.left)) + scrollX - pad;
  const top = Math.min(...boxes.map((b) => b.top)) + scrollY - pad;
  const right = Math.max(...boxes.map((b) => b.right)) + scrollX + pad;
  const bottom = Math.max(...boxes.map((b) => b.bottom)) + scrollY + pad;
  return { x: Math.max(0, left), y: Math.max(0, top),
           width: right - Math.max(0, left), height: bottom - Math.max(0, top),
           hasLabel: !!label };
})()
"""

#: Alto máximo de la región capturada: por encima se recurre al viewport (no se vuelca la página).
_MAX_REGION_HEIGHT: Final[int] = 2400


class _Cdp:
    """Cliente mínimo del protocolo DevTools sobre un WebSocket síncrono."""

    def __init__(self, connection: Any) -> None:
        self._ws = connection
        self._next = 0

    def call(self, method: str, **params: Any) -> Mapping[str, Any]:
        """Envía un comando y espera **su** respuesta (los eventos intermedios se ignoran)."""
        self._next += 1
        identifier = self._next
        self._ws.send(json.dumps({"id": identifier, "method": method, "params": params}))
        deadline = time.monotonic() + _WAIT_SECONDS
        while time.monotonic() < deadline:
            message = json.loads(self._ws.recv(timeout=_WAIT_SECONDS))
            if message.get("id") == identifier:
                if "error" in message:
                    raise CaptureError(f"DevTools {method}: {message['error'].get('message', '')}")
                result = message.get("result", {})
                return result if isinstance(result, dict) else {}
        raise CaptureError(f"DevTools {method}: sin respuesta")

    def evaluate(self, expression: str) -> Any:
        """Evalúa JavaScript en la página y devuelve su valor (serializable)."""
        outcome = self.call(
            "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=False
        )
        if "exceptionDetails" in outcome:
            raise CaptureError("la página lanzó una excepción al evaluar el selector")
        value = outcome.get("result", {})
        return value.get("value") if isinstance(value, dict) else None

    def screenshot(self, region: Mapping[str, float] | None = None) -> bytes:
        """Captura PNG de la región indicada (coordenadas de página) o del viewport actual."""
        if region is None:
            outcome = self.call("Page.captureScreenshot", format="png", fromSurface=True)
        else:
            outcome = self.call(
                "Page.captureScreenshot",
                format="png",
                fromSurface=True,
                captureBeyondViewport=True,
                clip={**region, "scale": 1},
            )
        return base64.b64decode(str(outcome.get("data", "")))


def _js_string(value: str) -> str:
    """Literal JavaScript seguro para un selector (JSON es un subconjunto de JS)."""
    return json.dumps(value)


class BrowserInteraction:
    """Interacciones hover reales con Chrome/Edge headless (DevTools), sin dependencias nuevas."""

    def __init__(self, browser: str | None = None, *, timeout_seconds: float = 90.0) -> None:
        """``browser`` fija el binario; sin él se usa el mismo criterio que la captura estática."""
        self._locator = HeadlessBrowserCapture(browser)
        self._timeout = timeout_seconds

    def run(self, spec: VisualInteraction, viewport: tuple[int, int]) -> InteractionEvidence:
        """Interacción real sobre la ruta declarada; un fallo es ``error``, nunca un PASS."""
        base = InteractionEvidence(name=spec.name, route=spec.route, viewport=viewport)
        if not is_loopback_url(spec.route):
            return _with(base, error=f"solo se interactúa con URLs de bucle local: {spec.route}")
        try:
            browser = self._locator.find_browser()
        except CaptureError as error:
            return _with(base, error=str(error))
        with tempfile.TemporaryDirectory(
            prefix="punto-cdp-", ignore_cleanup_errors=True
        ) as profile:
            process = subprocess.Popen(
                [
                    browser,
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--user-data-dir={profile}",
                    "--remote-debugging-port=0",
                    f"--window-size={viewport[0]},{viewport[1]}",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
            try:
                return self._interact(process, Path(profile), spec, viewport, base)
            except CaptureError as error:
                return _with(base, error=str(error)[:300])
            except (OSError, ValueError, TimeoutError) as error:
                return _with(base, error=f"fallo del navegador: {type(error).__name__}")
            finally:
                _terminate(process)

    def _interact(
        self,
        process: subprocess.Popen[bytes],
        profile: Path,
        spec: VisualInteraction,
        viewport: tuple[int, int],
        base: InteractionEvidence,
    ) -> InteractionEvidence:
        from websockets.sync.client import connect

        port = _debugging_port(process, profile)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
            pages = [item for item in json.load(response) if item.get("type") == "page"]
        if not pages:
            raise CaptureError("el navegador no expuso ninguna página")
        with connect(str(pages[0]["webSocketDebuggerUrl"]), max_size=None, open_timeout=15) as ws:
            cdp = _Cdp(ws)
            cdp.call("Page.enable")
            cdp.call(
                "Emulation.setDeviceMetricsOverride",
                width=viewport[0],
                height=viewport[1],
                deviceScaleFactor=1,
                mobile=False,
            )
            cdp.call("Page.navigate", url=spec.route)
            find = _FIND_JS % {
                "selector": _js_string(spec.hover),
                "index": spec.index,
                "grid": _GRID,
            }
            located = self._wait_for_target(cdp, find)
            if not located.get("matches"):
                return _with(
                    base, error=f"el selector {spec.hover!r} no localiza ningún elemento en la ruta"
                )
            matches = int(located["matches"])
            if located.get("missing"):
                return _with(
                    base,
                    matches=matches,
                    error=f"index {spec.index} fuera de rango: {matches} elemento(s) coinciden",
                )
            target = str(located.get("target", ""))
            if not located.get("hit"):
                return _with(
                    base,
                    matches=matches,
                    target=target,
                    error="ningún punto del elemento objetivo recibe el cursor",
                )
            time.sleep(spec.settle_ms / 1000)  # hidratación y transiciones del estado inicial
            # Las coordenadas se recalculan **tras** el asentamiento: son las que valen para el
            # movimiento del ratón (layout, fuentes e imágenes pueden haber movido el elemento).
            located = cdp.evaluate(find) or {}
            if not located.get("hit"):
                return _with(
                    base,
                    matches=matches,
                    target=target,
                    error="ningún punto del elemento objetivo recibe el cursor",
                )
            state = _STATE_JS % {
                "selector": _js_string(spec.hover),
                "index": spec.index,
                "label": _js_string(spec.label),
            }
            # Estado inicial: el cursor fuera del elemento (esquina de la página).
            cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=1, y=1)
            time.sleep(spec.settle_ms / 1000)
            initial = cdp.evaluate(state) or {}
            region = self._region(cdp, spec, viewport)
            before = CapturedShot(
                url=spec.route, viewport=viewport, data=cdp.screenshot(region), phase="before"
            )
            # Interacción real: eventos de entrada del navegador sobre el punto que golpea al
            # elemento (no ``:hover`` simulado por script).
            x, y = float(located["x"]), float(located["y"])
            cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=x - 3, y=y - 3)
            cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
            time.sleep(spec.settle_ms / 1000)
            final = cdp.evaluate(state) or {}
            after = CapturedShot(
                url=spec.route, viewport=viewport, data=cdp.screenshot(region), phase="after"
            )
            applied = bool(final.get("hover")) and not bool(initial.get("hover"))
            return InteractionEvidence(
                name=spec.name,
                route=spec.route,
                viewport=viewport,
                matches=matches,
                target=target,
                hover_before=bool(initial.get("hover")),
                hover_applied=applied,
                label_text=str(final.get("label", "")),
                region="element+label"
                if region and spec.label
                else ("element" if region else "viewport"),
                before=before,
                after=after,
                pixels_changed=before.sha256 != after.sha256,
                error=""
                if applied
                else "el navegador no reportó el elemento en :hover tras el movimiento real",
            )

    @staticmethod
    def _region(
        cdp: _Cdp, spec: VisualInteraction, viewport: tuple[int, int]
    ) -> dict[str, float] | None:
        """Región elemento+etiqueta (coordenadas de página), o ``None`` para usar el viewport."""
        found = cdp.evaluate(
            _REGION_JS
            % {
                "selector": _js_string(spec.hover),
                "index": spec.index,
                "label": _js_string(spec.label),
            }
        )
        if not isinstance(found, dict) or not spec.label or not found.get("hasLabel"):
            return None
        width, height = float(found["width"]), float(found["height"])
        if width <= 0 or height <= 0 or height > _MAX_REGION_HEIGHT:
            return None
        return {
            "x": float(found["x"]),
            "y": float(found["y"]),
            "width": min(width, float(viewport[0]) * 2),
            "height": height,
        }

    @staticmethod
    def _wait_for_target(cdp: _Cdp, find: str) -> Mapping[str, Any]:
        """Espera a que la página cargue y el selector aparezca (la hidratación tarda)."""
        deadline = time.monotonic() + _WAIT_SECONDS
        last: Mapping[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                ready = cdp.evaluate("document.readyState") == "complete"
                last = cdp.evaluate(find) if ready else {}
            except CaptureError:
                last = {}
            if isinstance(last, dict) and last.get("matches"):
                return last
            time.sleep(0.3)
        return last if isinstance(last, dict) else {}


def _with(evidence: InteractionEvidence, **changes: Any) -> InteractionEvidence:
    """Copia la evidencia con campos cambiados (dataclass congelada)."""
    return replace(evidence, **changes)


def _debugging_port(process: subprocess.Popen[bytes], profile: Path) -> int:
    """Puerto de DevTools que Chrome publica en ``DevToolsActivePort`` dentro de su perfil."""
    marker = profile / "DevToolsActivePort"
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CaptureError("el navegador terminó antes de exponer DevTools")
        if marker.is_file():
            first = marker.read_text(encoding="utf-8").splitlines()[:1]
            if first and first[0].isdigit():
                return int(first[0])
        time.sleep(0.2)
    raise CaptureError("el navegador no expuso el puerto de DevTools a tiempo")


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Cierra el navegador **y sus hijos** (en Windows Chrome lanza varios procesos)."""
    if process.poll() is None:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=15,
            )
        else:
            process.kill()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - improbable tras kill
        process.kill()


# ------------------------------------------------------------------------- evaluación (VISUAL_QA)
_INSTRUCTIONS: Final[str] = (
    "You are the VISUAL_QA reviewer of PUNTO. For each numbered criterion you receive pairs of "
    "screenshots of the RENDERED application: BEFORE and AFTER a real mouse hover over a specific "
    "element, plus the deterministic facts of that interaction. Decide each criterion by "
    "COMPARING the two images: PASS only if the difference between BEFORE and AFTER clearly "
    "demonstrates it (for example the hovered element visibly changes and a name appears); FAIL if "
    "the comparison clearly contradicts it (for example nothing visible changed); UNCLEAR if the "
    "images do not let you decide. Never guess and never infer from the facts alone: the images "
    "decide. Reply with ONLY this JSON: "
    '{"verdicts": [{"claim": <criterion number>, "verdict": "PASS|FAIL|UNCLEAR", '
    f'"observation": "what you SEE change, at most {MAX_OBSERVATION_CHARS} characters"}}]}} '
    "with exactly one entry per criterion."
)


def _facts(index: int, evidence: InteractionEvidence) -> str:
    """Hechos deterministas de una interacción, para el contexto del revisor."""
    return (
        f"INTERACTION {index}: {evidence.name} on {evidence.route} at "
        f"{evidence.viewport[0]}x{evidence.viewport[1]}\n"
        f"  hovered element: {evidence.target}\n"
        f"  browser reports :hover before={evidence.hover_before} after={evidence.hover_applied}\n"
        f"  label text after hover: {evidence.label_text or '(none declared or empty)'}\n"
        f"  screenshots cover: {evidence.region}\n"
        f"  screenshots: image {2 * index - 1} = BEFORE, image {2 * index} = AFTER"
    )


def assess_interaction_claims(
    router: Any,
    sentences: Sequence[str],
    evidences: Sequence[InteractionEvidence],
    *,
    request_id: str,
    max_output_tokens: int | None = None,
) -> VisualAssessment:
    """Evalúa criterios de interacción con el par antes/después por la ruta **efectiva**.

    Sin ninguna interacción demostrable (elemento no localizado, hover no confirmado) no se llama
    a nadie: todo queda ``UNCLEAR`` con el motivo. Con evidencia, el veredicto del modelo se valida
    con el contrato cerrado y un ``PASS`` se degrada a ``UNCLEAR`` si no hubo cambio de píxeles.
    """
    usable = [item for item in evidences if item.usable]
    if not sentences:
        return VisualAssessment(error="no hay criterios de interacción que evaluar")
    if not usable:
        reasons = "; ".join(f"{item.name}: {item.error or 'no demostrable'}" for item in evidences)
        reason = f"interacción no demostrable: {reasons or 'no hay interacciones declaradas'}"
        return VisualAssessment(error=reason[:300])
    route = router.resolve_route(ProviderRole.VISUAL_QA, needs_vision=True)
    shots = tuple(shot for item in usable for shot in (item.before, item.after) if shot is not None)
    if not route.available:
        return VisualAssessment(
            shots=shots, error=f"sin ruta visual efectiva: {route.reason}"[:300]
        )
    listing = "\n".join(f"{index}. {text}" for index, text in enumerate(sentences, start=1))
    facts = "\n".join(_facts(index, item) for index, item in enumerate(usable, start=1))
    request = make_request(
        ProviderRole.VISUAL_QA,
        _INSTRUCTIONS,
        context=f"CRITERIA:\n{listing}\n\nINTERACTIONS (images attached in order):\n{facts}",
        attachments=tuple(shot.as_payload(index) for index, shot in enumerate(shots, start=1)),
        metadata={"purpose": "dev-cycle-visual-interaction"},
        request_id=request_id,
    )
    result = router.execute(ProviderRole.VISUAL_QA, request, max_output_tokens=max_output_tokens)
    if not result.ok:
        return VisualAssessment(
            provider=result.provider,
            model=result.model,
            transport=route.transport,
            via_failover=route.via_failover,
            shots=shots,
            error=(
                f"VISUAL_QA no respondió: {result.provider}: {result.error or result.status.value}"
            )[:300],
        )
    verdicts = parse_verdicts(result.content, claims=len(sentences))
    if not any(item.pixels_changed for item in usable):
        verdicts = tuple(
            ClaimVerdict(
                claim=item.claim,
                verdict="UNCLEAR" if item.verdict == "PASS" else item.verdict,
                observation=(
                    "las capturas antes/después son idénticas: no hubo cambio visual que aprobar"
                    if item.verdict == "PASS"
                    else item.observation
                ),
            )
            for item in verdicts
        )
    return VisualAssessment(
        provider=result.provider,
        model=result.model,
        transport=route.transport,
        via_failover=route.via_failover or result.provider != route.assigned,
        shots=shots,
        verdicts=verdicts,
    )
