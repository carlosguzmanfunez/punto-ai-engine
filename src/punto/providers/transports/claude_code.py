"""Transporte de Claude Code con la cuenta Claude del usuario (rol VISUAL_QA) — SUBSCRIPTION v0.

Claude Code es el cliente oficial de Anthropic. PUNTO **no** automatiza ``claude.ai``, no copia la
sesión web y no extrae cookies: si Claude Code está instalado y autenticado con su propio mecanismo
oficial, este transporte le pide una ejecución no interactiva y normaliza el resultado.

Interfaz oficial usada (comprobada contra la CLI instalada):

- ``claude --version``                        — ¿está instalado?
- ``claude auth status --json``               — estado de sesión declarado por el cliente
- ``claude -p <prompt> --output-format json`` — ejecución no interactiva en JSON

La sesión la administra Claude Code (``claude auth login``). Este transporte **no** guarda ni lee
contraseñas, cookies ni tokens: solo pregunta por el estado.

Limitación declarada: el modo no interactivo de Claude Code recibe **texto**. Este transporte no
declara soporte de imágenes; para evidencia visual con adjuntos se usa el transporte ``api``
(multimodal) de forma explícita. No se inventa un canal que la interfaz oficial no ofrece.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, Final

from punto.providers.base import PROVIDER_ANTHROPIC
from punto.providers.contract import (
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
    TransportUsage,
    TransportUsageStatus,
    clip_text,
    transport_result,
)
from punto.providers.transports.cli import CliTransport

#: Binario oficial.
CLAUDE_BINARY: Final[str] = "claude"

#: ``argv`` oficial del estado de autenticación. Devuelve JSON por defecto (``--json``).
AUTH_STATUS_ARGV: Final[tuple[str, ...]] = ("auth", "status", "--json")

#: Modos de autenticación que Claude Code declara y que corresponden a una cuenta Claude.
ACCOUNT_AUTH_METHODS: Final[tuple[str, ...]] = ("oauth", "claudeai", "claude_ai", "subscription")


class ClaudeCodeTransport(CliTransport):
    """Ejecuta Claude Code con la sesión oficial de la cuenta Claude."""

    def __init__(
        self,
        *,
        model: str,
        runner: SubprocessRunner | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        """Construye el transporte; el binario es siempre la CLI oficial de Claude Code."""
        from punto.providers.transport import (
            DEFAULT_TRANSPORT_TIMEOUT_SECONDS,
            RealSubprocessRunner,
        )

        super().__init__(
            model=model,
            binary=CLAUDE_BINARY,
            runner=runner if runner is not None else RealSubprocessRunner(),
            timeout_seconds=(
                DEFAULT_TRANSPORT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
            ),
        )

    @property
    def kind(self) -> TransportKind:
        """Transporte de suscripción de Claude Code."""
        return TransportKind.CLAUDE_CODE

    @property
    def auth_mode(self) -> TransportAuthMode:
        """La sesión la administra Claude Code con la cuenta Claude."""
        return TransportAuthMode.CLAUDE_ACCOUNT

    @property
    def provider(self) -> str:
        """Proveedor al que sirve."""
        return PROVIDER_ANTHROPIC

    def auth_argv(self) -> tuple[str, ...]:
        """``argv`` oficial del estado de sesión."""
        return (self.binary, *AUTH_STATUS_ARGV)

    def prompt_argv(self) -> tuple[str, ...]:
        """``argv`` de una ejecución no interactiva, **sin** el prompt.

        El prompt viaja como último argumento (``execution_argv`` lo añade): Claude Code no necesita
        la vía de ``stdin`` que sí usa el transporte de Codex.
        """
        return (
            self.binary,
            "--print",
            "--output-format",
            "json",
            "--model",
            self.model,
        )

    def capabilities(self) -> TransportCapabilities:
        """Claude Code no interactivo trabaja con texto en esta modalidad."""
        return TransportCapabilities(
            supports_images=False,
            supports_json_schema=False,
            streaming=False,
            detail="claude --print es texto; para imágenes usa el transporte api",
        )

    def _interpret_auth(self, process: TransportProcess) -> TransportAuthStatus:
        """Traduce ``claude auth status --json`` a un estado del contrato."""
        if process.not_installed:
            return TransportAuthStatus.NOT_INSTALLED
        if process.timed_out:
            return TransportAuthStatus.UNAVAILABLE
        payload = _load_json(process.stdout)
        if payload is None:
            return TransportAuthStatus.UNAVAILABLE
        logged_in = payload.get("loggedIn")
        if logged_in is True:
            return TransportAuthStatus.AUTHENTICATED
        if logged_in is False:
            return TransportAuthStatus.NOT_AUTHENTICATED
        # Sin una respuesta booleana clara no se afirma que haya sesión.
        return TransportAuthStatus.UNAVAILABLE

    def auth_detail(self) -> str:
        """Método de autenticación declarado por el cliente, como evidencia (nunca un token).

        Es información de estado, no una credencial: ``authMethod`` vale ``none``, ``oauth`` o
        ``apiKey`` según cómo se haya autenticado la persona. Se usa para la tabla del dashboard.
        """
        process = self._run(self.auth_argv())
        payload = _load_json(process.stdout)
        if payload is None:
            return ""
        method = payload.get("authMethod")
        return method if isinstance(method, str) else ""

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
            method = self.auth_detail() or "cuenta"
            return ProviderHealth(
                provider=self.provider,
                status=ProviderHealthStatus.CONNECTED,
                model=self.model,
                detail=f"claude code instalado y con sesión ({method})",
            )
        if status is TransportAuthStatus.NOT_AUTHENTICATED:
            return ProviderHealth(
                provider=self.provider,
                status=ProviderHealthStatus.AUTH_FAILED,
                model=self.model,
                detail="NOT_AUTHENTICATED: ejecuta `claude auth login` con tu cuenta Claude",
            )
        return ProviderHealth(
            provider=self.provider,
            status=ProviderHealthStatus.UNAVAILABLE,
            model=self.model,
            detail="UNAVAILABLE: no se pudo leer el estado de sesión de Claude Code",
        )

    def usage_status(self) -> TransportUsage:
        """Límites de la suscripción.

        Claude Code **no** expone hoy un comando oficial de límites de plan, así que el estado es
        ``UNKNOWN`` salvo que una ejecución haya declarado explícitamente el límite agotado. No se
        inventan métricas ni se hace scraping de la cuenta.
        """
        observed = super().usage_status()
        if observed.known:
            return observed
        return TransportUsage(
            status=TransportUsageStatus.UNKNOWN,
            detail="Claude Code no expone límites de plan por una interfaz oficial",
        )

    def execute(
        self,
        request: ProviderRequest,
        *,
        json_schema: Mapping[str, object] | None = None,
        max_output_tokens: int | None = None,
    ) -> ProviderResult:
        """Ejecuta el prompt con Claude Code y normaliza la respuesta.

        Raises:
            TransportError: con la clase normalizada del fallo (no instalado, sin sesión, límite,
                timeout, proceso fallido o respuesta ilegible).
        """
        del json_schema, max_output_tokens  # la CLI no acepta ni esquema ni tope de salida
        if request.attachments:
            raise TransportError(
                TransportErrorKind.UNAVAILABLE,
                "el transporte claude_code no acepta imágenes; configura el transporte api para "
                "evidencia visual (no se descartan en silencio)",
            )
        status = self.auth_status()
        if status is TransportAuthStatus.NOT_INSTALLED:
            raise TransportError(
                TransportErrorKind.NOT_INSTALLED, f"el binario {self.binary!r} no está instalado"
            )
        if status is TransportAuthStatus.NOT_AUTHENTICATED:
            raise TransportError(
                TransportErrorKind.NOT_AUTHENTICATED,
                "no hay sesión de Claude Code: ejecuta `claude auth login` con tu cuenta Claude",
            )
        if status is TransportAuthStatus.UNAVAILABLE:
            raise TransportError(
                TransportErrorKind.UNAVAILABLE, "no se pudo leer el estado de sesión de Claude Code"
            )

        started = time.perf_counter()
        process = self.run_prompt(_prompt_of(request))
        content, usage, is_error, subtype = extract_claude_result(process.stdout)
        if is_error:
            raise TransportError(
                TransportErrorKind.PROCESS_FAILED,
                f"el cliente declaró error ({subtype or 'sin subtipo'}): {clip_text(content, 300)}",
            )
        if not content:
            raise TransportError(
                TransportErrorKind.INVALID_RESPONSE,
                "la salida JSON de Claude Code no contenía un resultado reconocible: "
                f"{clip_text(process.stdout or process.stderr, 300)}",
            )
        return transport_result(
            transport=self,
            request=request,
            content=content,
            usage=usage,
            duration_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=subtype,
        )


def _prompt_of(request: ProviderRequest) -> str:
    """Instrucciones y contexto en un solo texto, tal como los recibe la CLI."""
    if request.context:
        return f"{request.instructions}\n\n{request.context}"
    return request.instructions


def _load_json(text: str) -> dict[str, Any] | None:
    """Objeto JSON del texto, o ``None`` si no lo es."""
    candidate = text.strip()
    if not candidate:
        return None
    try:
        payload = json.loads(candidate)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def extract_claude_result(
    stdout: str,
) -> tuple[str, object | None, bool, str]:
    """Lee la salida JSON de ``claude --print --output-format json``.

    Returns:
        ``(texto, uso, is_error, subtipo)``. Si la salida no tiene la forma documentada, el texto es
        cadena vacía y ``is_error`` es ``False``: quien llama decide (y falla con
        ``INVALID_RESPONSE``) en vez de recibir contenido inventado.
    """
    payload = _load_json(stdout)
    if payload is None:
        return "", None, False, ""
    text = payload.get("result")
    content = text if isinstance(text, str) else ""
    is_error = payload.get("is_error") is True
    subtype = payload.get("subtype")
    return content, _usage_of(payload.get("usage")), is_error, (
        subtype if isinstance(subtype, str) else ""
    )


def _usage_of(raw: object) -> object | None:
    """Consumo declarado por el cliente, si tiene la forma esperada."""
    if not isinstance(raw, dict):
        return None
    from punto.schemas.execution import ModelUsage

    def _number(key: str) -> int:
        value = raw.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    prompt = _number("input_tokens") or _number("prompt_tokens")
    completion = _number("output_tokens") or _number("completion_tokens")
    if not prompt and not completion:
        return None
    return ModelUsage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
    )


__all__ = [
    "ACCOUNT_AUTH_METHODS",
    "AUTH_STATUS_ARGV",
    "CLAUDE_BINARY",
    "ClaudeCodeTransport",
    "extract_claude_result",
]
