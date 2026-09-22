"""Evidencia visual gobernada del ciclo de desarrollo: captura real + veredicto de VISUAL_QA.

Hasta ahora el ciclo solo *sabía* si había capacidad de imágenes; aunque la hubiera, no producía
ninguna captura ni ejecutaba VISUAL_QA, así que un criterio de apariencia quedaba siempre en
``NOT_VERIFIED`` («no se aportó ninguna imagen»). Este módulo cierra esa cadena con dos piezas, y
ninguna concede autoridad:

- **captura** (:class:`HeadlessBrowserCapture`): PUNTO abre la aplicación **renderizada** en un
  navegador headless y guarda la captura. Solo URLs de bucle local (``localhost``/``127.0.0.1``): el
  ciclo no navega a Internet. Lo que se le entrega al modelo son bytes que controla PUNTO;
- **evaluación** (:func:`assess_visual_claims`): la ruta de VISUAL_QA se resuelve por capacidad
  **efectiva** (``ProviderRouter.resolve_route``: conexión + transporte que de verdad ejecuta
  imágenes), nunca por catálogo. El veredicto del modelo es inteligencia externa no confiable: se
  valida con un contrato cerrado (``PASS``/``FAIL``/``UNCLEAR`` por criterio) y todo lo que no
  encaje —salida ilegible, criterio sin veredicto, veredicto desconocido— se degrada a ``UNCLEAR``
  (nunca a ``PASS``). Un modelo que no puede decidir no satisface el criterio.

Límite honesto: una captura estática solo demuestra lo que se **ve** en ella. Un criterio de
interacción (pasar el cursor, hacer clic) que no es visible en la captura se debe declarar
``UNCLEAR`` y por tanto sigue exigiendo evidencia.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlparse

from punto.providers.base import ImagePayload
from punto.providers.contract import ProviderRequest, ProviderRole, make_request

__all__ = [
    "MAX_OBSERVATION_CHARS",
    "VERDICTS",
    "CaptureError",
    "CapturedShot",
    "ClaimVerdict",
    "HeadlessBrowserCapture",
    "ScreenshotCapture",
    "VisualAssessment",
    "assess_visual_claims",
    "is_loopback_url",
    "parse_verdicts",
]

#: Veredictos que el contrato admite. ``UNCLEAR`` es un veredicto legítimo, no un fallo.
VERDICTS: Final[tuple[str, ...]] = ("PASS", "FAIL", "UNCLEAR")

MAX_OBSERVATION_CHARS: Final[int] = 300
MAX_CLAIMS: Final[int] = 12
PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"

#: Nombre de la variable que fija el navegador (ruta completa), si hace falta.
BROWSER_ENV: Final[str] = "PUNTO_BROWSER"

_BROWSER_NAMES: Final[tuple[str, ...]] = ("chrome", "chromium", "msedge", "google-chrome")
_BROWSER_PATHS: Final[tuple[str, ...]] = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


class CaptureError(RuntimeError):
    """No se pudo capturar la aplicación renderizada (y no se inventa una captura)."""


@dataclass(frozen=True, slots=True)
class CapturedShot:
    """Captura real de una ruta de la aplicación, con su huella."""

    url: str
    viewport: tuple[int, int]
    data: bytes
    media_type: str = "image/png"
    #: ``static`` (captura normal), ``before`` / ``after`` (antes y después de una interacción).
    phase: str = "static"

    @property
    def sha256(self) -> str:
        """Huella del contenido exacto que se le entregó al modelo."""
        return hashlib.sha256(self.data).hexdigest()

    def as_payload(self, index: int) -> ImagePayload:
        """Imagen para el contrato multimodal (bytes controlados por PUNTO)."""
        return ImagePayload(
            data=self.data, media_type=self.media_type, logical_name=f"captura-{index}"
        )


class ScreenshotCapture(Protocol):
    """Captura la aplicación renderizada en URLs de bucle local."""

    def capture(self, urls: Sequence[str], viewport: tuple[int, int]) -> tuple[CapturedShot, ...]:
        """Devuelve una captura por URL o lanza :class:`CaptureError`."""
        ...  # pragma: no cover - protocolo


def is_loopback_url(url: str) -> bool:
    """True solo para ``http(s)://localhost`` o ``127.0.0.1``: nada fuera de la máquina."""
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1"}


class HeadlessBrowserCapture:
    """Captura con Chrome/Edge headless instalados (``--screenshot``), sin dependencias nuevas."""

    def __init__(self, browser: str | None = None, *, timeout_seconds: float = 90.0) -> None:
        """``browser`` fija el binario; sin él se usa ``PUNTO_BROWSER`` o el que esté instalado."""
        self._browser = browser
        self._timeout = timeout_seconds

    def find_browser(self) -> str:
        """Ruta del navegador, o :class:`CaptureError` si no hay ninguno."""
        candidates = [self._browser or "", os.environ.get(BROWSER_ENV, "")]
        candidates += [shutil.which(name) or "" for name in _BROWSER_NAMES]
        candidates += list(_BROWSER_PATHS)
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate
        raise CaptureError("no hay un navegador instalado (Chrome/Edge) para capturar la app")

    def capture(self, urls: Sequence[str], viewport: tuple[int, int]) -> tuple[CapturedShot, ...]:
        """Una captura por URL de bucle local; cualquier fallo es un error explícito."""
        if not urls:
            raise CaptureError("el destino no declara rutas visuales que capturar")
        outside = [url for url in urls if not is_loopback_url(url)]
        if outside:
            raise CaptureError(f"solo se capturan URLs de bucle local; rechazadas: {outside}")
        browser = self.find_browser()
        width, height = viewport
        shots: list[CapturedShot] = []
        with tempfile.TemporaryDirectory(prefix="punto-shot-") as directory:
            for index, url in enumerate(urls, start=1):
                target = Path(directory) / f"captura-{index}.png"
                try:
                    subprocess.run(
                        [
                            browser,
                            "--headless=new",
                            "--disable-gpu",
                            "--hide-scrollbars",
                            f"--window-size={width},{height}",
                            "--virtual-time-budget=10000",
                            f"--screenshot={target}",
                            url,
                        ],
                        capture_output=True,
                        timeout=self._timeout,
                        check=False,
                        shell=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise CaptureError(
                        f"el navegador no pudo capturar {url}: {type(error).__name__}"
                    ) from error
                data = target.read_bytes() if target.is_file() else b""
                if not data.startswith(PNG_SIGNATURE):
                    raise CaptureError(f"el navegador no produjo una captura válida de {url}")
                shots.append(CapturedShot(url=url, viewport=viewport, data=data))
        return tuple(shots)


@dataclass(frozen=True, slots=True)
class ClaimVerdict:
    """Veredicto de VISUAL_QA sobre un criterio (por su número, empezando en 1)."""

    claim: int
    verdict: str
    observation: str


@dataclass(frozen=True, slots=True)
class VisualAssessment:
    """Resultado de la evaluación: quién la hizo (ruta efectiva), sobre qué y qué dijo."""

    provider: str = ""
    model: str = ""
    transport: str = ""
    via_failover: bool = False
    shots: tuple[CapturedShot, ...] = ()
    verdicts: tuple[ClaimVerdict, ...] = ()
    #: Vacío si la evaluación se hizo; si no, por qué no (sin ruta efectiva, fallo...).
    error: str = ""

    @property
    def performed(self) -> bool:
        """True si un proveedor con capacidad efectiva evaluó las capturas."""
        return not self.error and bool(self.provider)

    def verdict_for(self, claim: int) -> ClaimVerdict | None:
        """Veredicto del criterio ``claim`` (1-based), si lo hay."""
        for item in self.verdicts:
            if item.claim == claim:
                return item
        return None


#: Instrucciones del rol. El veredicto es evidencia, no autoridad: no aprueba nada por sí mismo.
_INSTRUCTIONS: Final[str] = (
    "You are the VISUAL_QA reviewer of PUNTO. You receive screenshots of the RENDERED application "
    "and a numbered list of appearance criteria. Decide each criterion from the IMAGES ONLY: "
    "PASS only if the screenshots clearly demonstrate it; FAIL if they clearly contradict it; "
    "UNCLEAR if they do not let you decide. A static screenshot cannot show hover, click or "
    "animation: a criterion about interaction that is not visible in the images is UNCLEAR. Never "
    "guess and never infer from wording, code or what the change was meant to do. Reply with ONLY "
    'this JSON: {"verdicts": [{"claim": <criterion number>, "verdict": "PASS|FAIL|UNCLEAR", '
    f'"observation": "what you SEE, at most {MAX_OBSERVATION_CHARS} characters"}}]}} '
    "with exactly one entry per criterion."
)


def assess_visual_claims(
    router: Any,
    sentences: Sequence[str],
    shots: Sequence[CapturedShot],
    *,
    request_id: str,
    max_output_tokens: int | None = None,
    guidance: Mapping[str, str] | None = None,
) -> VisualAssessment:
    """Evalúa los criterios de apariencia con la ruta **efectiva** de VISUAL_QA.

    Sin ruta efectiva (nadie puede ejecutar imágenes) no se llama a ningún proveedor y el resultado
    lo dice: el ciclo sigue exigiendo evidencia. Con ruta, el veredicto se valida con un contrato
    cerrado; lo que no encaje es ``UNCLEAR``.
    """
    if not sentences or not shots:
        return VisualAssessment(shots=tuple(shots), error="no hay criterios o capturas que evaluar")
    route = router.resolve_route(ProviderRole.VISUAL_QA, needs_vision=True)
    if not route.available:
        return VisualAssessment(
            shots=tuple(shots), error=f"sin ruta visual efectiva: {route.reason}"[:300]
        )
    hints = guidance or {}
    listing = "\n".join(
        f"{index}. {text}"
        + (f" (previous attempt was INCONCLUSIVE: {hints[text][:200]})" if hints.get(text) else "")
        for index, text in enumerate(sentences, start=1)
    )
    views = "\n".join(
        f"Screenshot {index}: {shot.url} at {shot.viewport[0]}x{shot.viewport[1]}"
        for index, shot in enumerate(shots, start=1)
    )
    request: ProviderRequest = make_request(
        ProviderRole.VISUAL_QA,
        _INSTRUCTIONS,
        context=f"CRITERIA:\n{listing}\n\nSCREENSHOTS (attached, in order):\n{views}",
        attachments=tuple(shot.as_payload(index) for index, shot in enumerate(shots, start=1)),
        metadata={"purpose": "dev-cycle-visual-evidence"},
        request_id=request_id,
    )
    result = router.execute(ProviderRole.VISUAL_QA, request, max_output_tokens=max_output_tokens)
    if not result.ok:
        detail = f"{result.provider or route.provider}: {result.error or result.status.value}"
        return VisualAssessment(
            provider=result.provider,
            model=result.model,
            transport=route.transport,
            via_failover=route.via_failover,
            shots=tuple(shots),
            error=f"VISUAL_QA no respondió: {detail}"[:300],
        )
    return VisualAssessment(
        provider=result.provider,
        model=result.model,
        transport=route.transport,
        via_failover=route.via_failover or result.provider != route.assigned,
        shots=tuple(shots),
        verdicts=parse_verdicts(result.content, claims=len(sentences)),
    )


def parse_verdicts(content: str, *, claims: int) -> tuple[ClaimVerdict, ...]:
    """Interpreta la salida del modelo con un contrato **cerrado**: nada dudoso llega como PASS.

    Devuelve exactamente un veredicto por criterio (1..``claims``). Un criterio sin entrada, con
    entrada duplicada, fuera de rango, con un veredicto fuera de :data:`VERDICTS` o con salida
    ilegible queda ``UNCLEAR``. Las observaciones se acotan.
    """
    parsed: dict[int, ClaimVerdict] = {}
    duplicated: set[int] = set()
    payload = _json_object(content)
    entries = payload.get("verdicts") if payload is not None else None
    if isinstance(entries, list):
        for item in entries[: MAX_CLAIMS * 2]:
            if not isinstance(item, Mapping):
                continue
            number = item.get("claim")
            verdict = str(item.get("verdict", "")).strip().upper()
            if isinstance(number, bool) or not isinstance(number, int):
                continue
            if not 1 <= number <= claims or verdict not in VERDICTS:
                continue
            if number in parsed:
                duplicated.add(number)
                continue
            parsed[number] = ClaimVerdict(
                claim=number,
                verdict=verdict,
                observation=str(item.get("observation", "")).strip()[:MAX_OBSERVATION_CHARS],
            )
    result: list[ClaimVerdict] = []
    for number in range(1, claims + 1):
        found = parsed.get(number)
        if found is None or number in duplicated:
            result.append(
                ClaimVerdict(
                    claim=number,
                    verdict="UNCLEAR",
                    observation="el revisor visual no dio un veredicto válido para este criterio",
                )
            )
        else:
            result.append(found)
    return tuple(result)


def _json_object(text: str) -> Mapping[str, Any] | None:
    """Primer objeto JSON de una respuesta (tolera texto alrededor), o ``None``."""
    candidate = text.strip()
    if not candidate:
        return None
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        loaded = json.loads(candidate[start : end + 1])
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None
