"""Transporte de Codex con la cuenta ChatGPT del usuario (rol ARCHITECT) — SUBSCRIPTION v0.

Codex es el cliente oficial de OpenAI para trabajar con una cuenta ChatGPT. PUNTO **no** automatiza
``chatgpt.com``, no extrae cookies y no copia tokens: si Codex está instalado y autenticado con su
propio mecanismo oficial, este transporte le pide una ejecución no interactiva y normaliza el
resultado.

Interfaz oficial usada:

- ``codex --version``                       — ¿está instalado?
- ``codex login status``                    — ¿hay sesión? (el estado lo declara el cliente)
- ``codex exec --json ...``                 — ejecución no interactiva, salida estructurada

El **App Server** de Codex (``codex app-server``, JSON-RPC) es la vía preferente para una
integración programática rica; este transporte lo deja anotado como la sustitución natural sin
cambiar el contrato: lo que está debajo es un detalle del transporte, no del provider ni de ENGINE.

**Imágenes (comprobado con la CLI real, no supuesto).** ``codex exec --image <FICHERO>`` adjunta
imágenes al prompt inicial. Medido con Codex 0.155 y la sesión ChatGPT: sobre una captura real de
navegador leyó título, departamento, botón, color y un código aleatorio que no estaba en el prompt;
sin imagen respondió ``NO_IMAGE``. Por eso este transporte **no** declara imágenes por catálogo ni
por modelo: lo declara solo si el binario instalado **anuncia** ``--image`` en ``codex exec --help``
(un binario más antiguo, no instalado o sin respuesta no las acepta: fail closed).

Los adjuntos los controla PUNTO: se validan con los límites del contrato multimodal, se escriben en
un directorio temporal **privado** solo para esa ejecución y se borran al terminar. El modelo nunca
recibe una ruta para leer ficheros por su cuenta y el sandbox sigue en ``read-only``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from punto.providers.base import ImageLimits, ImagePayload
from punto.providers.contract import (
    PROVIDER_OPENAI,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderRequest,
    ProviderResult,
)
from punto.providers.transport import (
    SubprocessRunner,
    TransportAuthMode,
    TransportAuthStatus,
    TransportCapabilities,
    TransportError,
    TransportErrorKind,
    TransportKind,
    TransportProcess,
    clip_text,
    transport_result,
)
from punto.providers.transports.cli import CliTransport

#: Binario oficial.
CODEX_BINARY: Final[str] = "codex"

#: ``argv`` oficial del estado de sesión. El cliente responde en texto plano.
LOGIN_STATUS_ARGV: Final[tuple[str, ...]] = ("login", "status")

#: Frases con las que Codex declara que **sí** hay sesión.
AUTHENTICATED_MARKERS: Final[tuple[str, ...]] = (
    "logged in",
    "authenticated",
    "chatgpt",
)

#: Frases con las que Codex declara que **no** hay sesión.
UNAUTHENTICATED_MARKERS: Final[tuple[str, ...]] = (
    "not logged in",
    "not authenticated",
    "no credentials",
    "login required",
    "run `codex login`",
)

#: Flag con el que ``codex exec`` adjunta imágenes al prompt inicial (variádico).
IMAGE_FLAG: Final[str] = "--image"

#: Extensión del fichero temporal según el media type (la CLI decide el formato por la extensión).
IMAGE_SUFFIX: Final[Mapping[str, str]] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

#: Resultado de la detección por binario. Solo se comparte con el runner real: un runner inyectado
#: (pruebas) responde lo que la prueba diga y nunca contamina a los demás.
_IMAGE_SUPPORT: dict[str, bool] = {}

#: Claves donde puede aparecer el texto del modelo en la salida estructurada de ``codex exec``.
TEXT_KEYS: Final[tuple[str, ...]] = ("text", "message", "content", "output_text", "result")


class CodexTransport(CliTransport):
    """Ejecuta Codex con la sesión oficial de la cuenta ChatGPT."""

    #: El prompt viaja por ``stdin``: en Windows el binario es un ``.cmd`` y ``cmd.exe`` trunca los
    #: argumentos con saltos de línea (PILOT-01R · R1).
    prompt_via_stdin: bool = True

    def __init__(
        self,
        *,
        model: str,
        runner: SubprocessRunner | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        """Construye el transporte; el binario es siempre la CLI oficial de Codex."""
        from punto.providers.transport import (
            DEFAULT_TRANSPORT_TIMEOUT_SECONDS,
            RealSubprocessRunner,
        )

        super().__init__(
            model=model,
            binary=CODEX_BINARY,
            runner=runner if runner is not None else RealSubprocessRunner(),
            timeout_seconds=(
                DEFAULT_TRANSPORT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
            ),
        )

    _images: bool | None = None
    _attached: tuple[str, ...] = ()

    @property
    def kind(self) -> TransportKind:
        """Transporte de suscripción de Codex."""
        return TransportKind.CODEX

    @property
    def auth_mode(self) -> TransportAuthMode:
        """La sesión la administra Codex con la cuenta ChatGPT."""
        return TransportAuthMode.CHATGPT

    @property
    def provider(self) -> str:
        """Proveedor al que sirve."""
        return PROVIDER_OPENAI

    def auth_argv(self) -> tuple[str, ...]:
        """``argv`` oficial del estado de sesión."""
        return (self.binary, *LOGIN_STATUS_ARGV)

    def prompt_argv(self) -> tuple[str, ...]:
        """``argv`` de una ejecución no interactiva, **sin** el prompt.

        El prompt no viaja como argumento: se entrega por la entrada estándar. En Windows, ``codex``
        es un ``.cmd`` de npm y Windows interpone ``cmd.exe``, que reparsea la línea de comandos y
        corta el argumento en el primer salto de línea (defecto medido en PILOT-01R · R1). Por
        ``stdin`` el prompt llega íntegro, sin reparseo y sin que el shell lo interprete.
        """
        # ``--image`` es variádico: va justo antes de otro flag (``--model``) para no tragarse el
        # prompt posicional del modo sin ``stdin``.
        attached = (IMAGE_FLAG, *self._attached) if self._attached else ()
        return (
            self.binary,
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            *attached,
            "--model",
            self.model,
        )

    def execution_argv(self, prompt: str) -> tuple[str, ...]:
        """``argv`` completo (con el prompt) para inspección y auditoría.

        Lo que se ejecuta de verdad lo decide :meth:`delivery_argv`, que en este transporte omite el
        prompt porque viaja por ``stdin``.
        """
        return (*self.prompt_argv(), prompt)

    def capabilities(self) -> TransportCapabilities:
        """Capacidades **comprobadas en el binario instalado**, no declaradas por catálogo.

        Las imágenes se acreditan solo si ``codex exec --help`` anuncia ``--image``. Sin binario,
        sin respuesta o con un binario que no lo anuncia, no hay imágenes (fail closed) y el
        detalle dice por qué.
        """
        images = self.accepts_images()
        return TransportCapabilities(
            supports_images=images,
            supports_json_schema=False,
            streaming=False,
            detail=(
                "codex exec adjunta imágenes con --image (anunciado por el binario instalado)"
                if images
                else "el binario de Codex no anuncia --image en `codex exec --help`: sin imágenes; "
                "para evidencia visual usa otro transporte con imágenes"
            ),
        )

    def accepts_images(self) -> bool:
        """True si el binario instalado de Codex anuncia ``--image`` (barato, en caché)."""
        from punto.providers.transport import RealSubprocessRunner

        shared = isinstance(self._runner, RealSubprocessRunner)
        key = shutil.which(self.binary) or self.binary
        if shared and key in _IMAGE_SUPPORT:
            return _IMAGE_SUPPORT[key]
        if self._images is None:
            process = self._run((self.binary, "exec", "--help"))
            self._images = process.ok and IMAGE_FLAG in f"{process.stdout}\n{process.stderr}"
        if shared:
            _IMAGE_SUPPORT[key] = self._images
        return self._images

    def _interpret_auth(self, process: TransportProcess) -> TransportAuthStatus:
        """Traduce la respuesta de ``codex login status`` a un estado del contrato."""
        if process.not_installed:
            return TransportAuthStatus.NOT_INSTALLED
        if process.timed_out:
            return TransportAuthStatus.UNAVAILABLE
        combined = f"{process.stdout}\n{process.stderr}".lower()
        if any(marker in combined for marker in UNAUTHENTICATED_MARKERS):
            return TransportAuthStatus.NOT_AUTHENTICATED
        if process.exit_code == 0 and any(
            marker in combined for marker in AUTHENTICATED_MARKERS
        ):
            return TransportAuthStatus.AUTHENTICATED
        # Una salida que no se entiende no se interpreta como sesión válida.
        return TransportAuthStatus.UNAVAILABLE

    def health_check(self) -> ProviderHealth:
        """Comprueba instalación y sesión con dos procesos baratos."""
        if not self.installed():
            return ProviderHealth(
                provider=self.provider,
                status=ProviderHealthStatus.UNAVAILABLE,
                model=self.model,
                detail=f"NOT_INSTALLED: el binario {self.binary!r} no está instalado",
            )
        status = self.auth_status()
        if status is TransportAuthStatus.AUTHENTICATED:
            return ProviderHealth(
                provider=self.provider,
                status=ProviderHealthStatus.CONNECTED,
                model=self.model,
                detail="codex instalado y con sesión de cuenta ChatGPT",
            )
        if status is TransportAuthStatus.NOT_AUTHENTICATED:
            return ProviderHealth(
                provider=self.provider,
                status=ProviderHealthStatus.AUTH_FAILED,
                model=self.model,
                detail="NOT_AUTHENTICATED: ejecuta `codex login` con tu cuenta ChatGPT",
            )
        return ProviderHealth(
            provider=self.provider,
            status=ProviderHealthStatus.UNAVAILABLE,
            model=self.model,
            detail="UNAVAILABLE: no se pudo leer el estado de sesión de Codex",
        )

    def execute(
        self,
        request: ProviderRequest,
        *,
        json_schema: Mapping[str, object] | None = None,
        max_output_tokens: int | None = None,
    ) -> ProviderResult:
        """Ejecuta el prompt con Codex y normaliza la respuesta.

        Raises:
            TransportError: con la clase normalizada del fallo (no instalado, sin sesión, límite,
                timeout, proceso fallido o respuesta ilegible).
        """
        del json_schema, max_output_tokens  # la CLI no acepta ni esquema ni tope de salida
        if request.attachments and not self.accepts_images():
            raise TransportError(
                TransportErrorKind.UNAVAILABLE,
                "el transporte codex no acepta imágenes: el binario instalado no anuncia --image; "
                "configura un transporte con imágenes para evidencia visual (no se descartan en "
                "silencio)",
            )
        status = self.auth_status()
        if status is TransportAuthStatus.NOT_INSTALLED:
            raise TransportError(
                TransportErrorKind.NOT_INSTALLED, f"el binario {self.binary!r} no está instalado"
            )
        if status is TransportAuthStatus.NOT_AUTHENTICATED:
            raise TransportError(
                TransportErrorKind.NOT_AUTHENTICATED,
                "no hay sesión de Codex: ejecuta `codex login` con tu cuenta ChatGPT",
            )
        if status is TransportAuthStatus.UNAVAILABLE:
            raise TransportError(
                TransportErrorKind.UNAVAILABLE, "no se pudo leer el estado de sesión de Codex"
            )

        started = time.perf_counter()
        if request.attachments:
            process = self._run_with_images(_prompt_of(request), request.attachments)
        else:
            process = self.run_prompt(_prompt_of(request))
        content = extract_codex_text(process.stdout)
        if not content:
            raise TransportError(
                TransportErrorKind.INVALID_RESPONSE,
                "la salida de `codex exec --json` no contenía texto reconocible: "
                f"{clip_text(process.stdout or process.stderr, 300)}",
            )
        return transport_result(
            transport=self,
            request=request,
            content=content,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


    def _run_with_images(self, prompt: str, images: Sequence[ImagePayload]) -> TransportProcess:
        """Ejecuta el prompt con imágenes que PUNTO controla, en un directorio temporal privado.

        Se validan con los límites del contrato multimodal (número, tamaño, tipo) antes de escribir
        nada; el directorio se borra siempre, también si la ejecución falla.
        """
        validated = ImageLimits().validate(images)
        directory = Path(tempfile.mkdtemp(prefix="punto-codex-img-"))
        try:
            paths: list[str] = []
            for index, image in enumerate(validated):
                target = directory / f"imagen-{index + 1}{IMAGE_SUFFIX[image.media_type]}"
                target.write_bytes(image.data)
                paths.append(str(target))
            self._attached = tuple(paths)
            try:
                return self.run_prompt(prompt)
            finally:
                self._attached = ()
        finally:
            shutil.rmtree(directory, ignore_errors=True)


def _prompt_of(request: ProviderRequest) -> str:
    """Instrucciones y contexto en un solo texto, tal como los recibe la CLI."""
    if request.context:
        return f"{request.instructions}\n\n{request.context}"
    return request.instructions


def extract_codex_text(stdout: str) -> str:
    """Extrae el texto del modelo de la salida estructurada de ``codex exec --json``.

    Se recorre la salida línea a línea (es JSON por línea), se buscan las claves de texto conocidas
    y se devuelve **el último** texto encontrado, que es el mensaje final del agente. Si no hay
    ninguno reconocible se devuelve cadena vacía: interpretar una línea de registro como respuesta
    sería inventar contenido.
    """
    found: list[str] = []
    for line in stdout.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        text = _text_in(payload)
        if text:
            found.append(text)
    return found[-1] if found else ""


def _text_in(payload: object, depth: int = 0) -> str:
    """Busca un texto del modelo dentro de un objeto JSON, con profundidad acotada."""
    if depth > 4 or not isinstance(payload, dict):
        return ""
    for key in TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _text_in(value, depth + 1)
            if nested:
                return nested
    for key in ("item", "msg", "data", "delta"):
        nested_value = payload.get(key)
        if isinstance(nested_value, dict):
            nested_text = _text_in(nested_value, depth + 1)
            if nested_text:
                return nested_text
    return ""


__all__ = [
    "AUTHENTICATED_MARKERS",
    "CODEX_BINARY",
    "LOGIN_STATUS_ARGV",
    "UNAUTHENTICATED_MARKERS",
    "CodexTransport",
    "extract_codex_text",
]
