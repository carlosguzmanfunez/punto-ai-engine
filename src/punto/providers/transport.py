"""Contrato de transporte de proveedor: lo que hay **debajo** del adaptador (SUBSCRIPTION v0).

Un proveedor (OpenAI, Anthropic, DeepSeek) puede hablar con su servicio por más de un camino: la API
con clave, o el cliente de suscripción oficial (Codex con cuenta ChatGPT, Claude Code con cuenta
Claude). ENGINE no sabe cuál está seleccionado: pide un rol al router, el router entrega un
``StructuredModelClient`` y ese cliente delega en el transporte que la configuración eligió.

```
OpenAIProvider    -> CodexTransport | OpenAIAPITransport
AnthropicProvider -> ClaudeCodeTransport | AnthropicAPITransport
DeepSeekProvider  -> transporte existente (API)
```

Reglas que este módulo hace cumplir:

- **sin fallback automático**: si el transporte de suscripción alcanza un límite o falla, el
  resultado es un fallo normalizado. Cambiar a la API de pago es una decisión de configuración, no
  una reacción del motor;
- **sin secretos**: nunca se leen, copian ni almacenan cookies, tokens OAuth, contraseñas ni
  cabeceras de autorización. La sesión la administra el cliente oficial;
- **sin shell construida desde texto**: se ejecuta un ``argv`` controlado, sin ``shell=True``, con
  entorno mínimo por lista blanca;
- **sin ejecución arbitraria**: lo único que viaja al proceso es el texto de la petición como
  argumento; ninguna opción del ``argv`` se construye a partir de la respuesta de un modelo.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol
from uuid import uuid4

from punto.providers.base import ProviderError
from punto.providers.contract import (
    ProviderErrorKind,
    ProviderHealth,
    ProviderRequest,
    ProviderResult,
    ProviderStatus,
    parse_structured_output,
)


class TransportKind(StrEnum):
    """Camino concreto hacia el proveedor.

    El vocabulario es corto pero **abierto**: añadir un transporte futuro (una API compatible, un
    proveedor local) no obliga a tocar ENGINE; solo a implementar este contrato y registrarlo.
    """

    CODEX = "codex"
    CLAUDE_CODE = "claude_code"
    API = "api"
    EXISTING = "existing"


class TransportAuthMode(StrEnum):
    """Cómo se autoriza el transporte."""

    CHATGPT = "chatgpt"
    CLAUDE_ACCOUNT = "claude_account"
    API_KEY = "api_key"


class TransportAuthStatus(StrEnum):
    """Estado de autenticación observado con el mecanismo oficial del cliente."""

    NOT_INSTALLED = "NOT_INSTALLED"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    AUTHENTICATED = "AUTHENTICATED"
    UNAVAILABLE = "UNAVAILABLE"


class TransportErrorKind(StrEnum):
    """Fallo normalizado de un transporte."""

    NOT_INSTALLED = "NOT_INSTALLED"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    LIMIT_REACHED = "LIMIT_REACHED"
    TIMEOUT = "TIMEOUT"
    PROCESS_FAILED = "PROCESS_FAILED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    UNAVAILABLE = "UNAVAILABLE"


class TransportUsageStatus(StrEnum):
    """Información de límites: lo que el cliente oficial diga, o ``UNKNOWN``."""

    KNOWN = "KNOWN"
    LIMIT_REACHED = "LIMIT_REACHED"
    UNKNOWN = "UNKNOWN"


class TransportError(ProviderError):
    """Fallo de un transporte, con su clase normalizada.

    Hereda de :class:`~punto.providers.base.ProviderError` para que el router lo trate como
    cualquier otro fallo de proveedor: se convierte en ``ProviderResult`` y **no** rompe el motor.
    """

    def __init__(self, kind: TransportErrorKind, detail: str) -> None:
        """Construye el error con su clase y un detalle ya saneado."""
        self.kind = kind
        super().__init__(f"{kind.value}: {detail}")


class TransportConfigError(ValueError):
    """La combinación de transporte y modo de autenticación no es válida."""


@dataclass(frozen=True, slots=True)
class TransportCapabilities:
    """Lo que un transporte sabe hacer. Se declara, no se supone."""

    supports_images: bool = False
    supports_json_schema: bool = False
    streaming: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        """Vista serializable."""
        return {
            "supports_images": self.supports_images,
            "supports_json_schema": self.supports_json_schema,
            "streaming": self.streaming,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class TransportUsage:
    """Estado de uso/límites declarado por el cliente oficial, si lo declara."""

    status: TransportUsageStatus = TransportUsageStatus.UNKNOWN
    detail: str = ""
    remaining: str = ""
    resets_at: str = ""

    @property
    def known(self) -> bool:
        """True solo si el cliente oficial informó de límites."""
        return self.status is not TransportUsageStatus.UNKNOWN

    def as_dict(self) -> dict[str, object]:
        """Vista serializable."""
        return {
            "usage_status": self.status.value,
            "detail": self.detail,
            "remaining": self.remaining,
            "resets_at": self.resets_at,
        }


@dataclass(frozen=True, slots=True)
class TransportProcess:
    """Resultado de un proceso del transporte, ya saneado."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    not_installed: bool = False
    duration_ms: int = 0
    env_names: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        """True si el proceso terminó con éxito."""
        return self.exit_code == 0 and not self.timed_out and not self.not_installed


class SubprocessRunner(Protocol):
    """Ejecuta un ``argv`` controlado. Se inyecta en las pruebas."""

    def run(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
    ) -> TransportProcess:
        """Ejecuta el proceso y devuelve su resultado saneado."""
        ...  # pragma: no cover - protocolo


#: Variables de entorno que un cliente oficial necesita para encontrar su sesión y su binario.
#: Todo lo demás se descarta: ninguna clave de API viaja a un proceso de suscripción.
ENV_ALLOWLIST: Final[tuple[str, ...]] = (
    "PATH",
    "PATHEXT",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "SystemRoot",
    "windir",
    "TEMP",
    "TMP",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "LANG",
    "LC_ALL",
    "TERM",
    "SHELL",
    "COMSPEC",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
)

#: Nombres que **nunca** se pasan a un proceso de transporte, aunque estén en la lista blanca.
ENV_FORBIDDEN_MARKERS: Final[tuple[str, ...]] = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "COOKIE",
    "CREDENTIAL",
    "AUTHORIZATION",
)

#: Patrones de credencial que se borran de cualquier texto que salga de un transporte.
SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{12,}"),
    re.compile(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\s]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
)

#: Texto con el que se sustituye cualquier credencial detectada.
REDACTED: Final[str] = "[REDACTED]"

#: Timeout por defecto de una ejecución de transporte de suscripción.
DEFAULT_TRANSPORT_TIMEOUT_SECONDS: Final[float] = 300.0

#: Frases con las que los clientes oficiales anuncian que se agotó el límite o la cuota. Es una
#: lectura del **propio** mensaje del cliente (no hay scraping ni endpoints privados): si el cliente
#: no lo dice, el transporte no lo inventa.
LIMIT_MARKERS: Final[tuple[str, ...]] = (
    "usage limit",
    "rate limit",
    "limit reached",
    "limits reached",
    "quota exceeded",
    "too many requests",
    "resets at",
    "out of credits",
    "429",
)


def build_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Entorno mínimo para un proceso de transporte, por lista blanca y con vetos explícitos.

    Un proceso de suscripción **no** necesita claves de API: el cliente oficial administra su
    sesión. Pasar el entorno del host sería la vía más fácil para filtrar una credencial, así que
    solo viajan las variables de la lista blanca y ninguna cuyo nombre huela a secreto.
    """
    origin = os.environ if source is None else source
    environment: dict[str, str] = {}
    for name in ENV_ALLOWLIST:
        value = origin.get(name)
        if not value:
            continue
        if any(marker in name.upper() for marker in ENV_FORBIDDEN_MARKERS):
            continue
        environment[name] = value
    return environment


def redact_transport_text(text: str) -> str:
    """Borra credenciales de un texto que sale de un transporte (salida, error, evidencia)."""
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            redacted = redacted.replace(value, REDACTED)
    return redacted


def clip_text(text: str, limit: int = 4000) -> str:
    """Acota un texto, preservando el principio (donde suele estar el diagnóstico)."""
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit] + "\n[recortado]"


def looks_like_limit(text: str) -> bool:
    """True si el cliente oficial dijo que se agotó el límite o la cuota."""
    lowered = text.lower()
    return any(marker in lowered for marker in LIMIT_MARKERS)


class RealSubprocessRunner:
    """Ejecuta procesos reales: ``argv`` controlado, sin shell y con entorno mínimo."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_TRANSPORT_TIMEOUT_SECONDS) -> None:
        """Fija el timeout por defecto del runner."""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds debe ser mayor que cero")
        self._timeout = timeout_seconds

    def run(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
    ) -> TransportProcess:
        """Ejecuta el proceso y devuelve su resultado, sin propagar excepciones de spawn.

        El programa se resuelve con ``shutil.which`` sobre el ``PATH`` del entorno mínimo: en
        Windows un cliente instalado por npm es un ``.cmd`` y ``CreateProcess`` no aplica
        ``PATHEXT`` por su cuenta. Si no se encuentra, se intenta igualmente el nombre lógico para
        que el fallo se clasifique como ``NOT_INSTALLED`` en vez de esconderse.
        """
        arguments = tuple(argv)
        environment = build_environment() if env is None else dict(env)
        program = shutil.which(arguments[0], path=environment.get("PATH")) or arguments[0]
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [program, *arguments[1:]],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
                env=environment,
            )
        except FileNotFoundError:
            return TransportProcess(
                argv=arguments,
                exit_code=127,
                stderr="el ejecutable no está instalado",
                not_installed=True,
                duration_ms=int((time.perf_counter() - started) * 1000),
                env_names=tuple(sorted(environment)),
            )
        except subprocess.TimeoutExpired as expired:
            return TransportProcess(
                argv=arguments,
                exit_code=124,
                stdout=redact_transport_text(_as_text(expired.stdout)),
                stderr=redact_transport_text(_as_text(expired.stderr)) or "timeout",
                timed_out=True,
                duration_ms=int((time.perf_counter() - started) * 1000),
                env_names=tuple(sorted(environment)),
            )
        except OSError as error:
            return TransportProcess(
                argv=arguments,
                exit_code=126,
                stderr=redact_transport_text(str(error)),
                not_installed=True,
                duration_ms=int((time.perf_counter() - started) * 1000),
                env_names=tuple(sorted(environment)),
            )
        return TransportProcess(
            argv=arguments,
            exit_code=completed.returncode,
            stdout=redact_transport_text(completed.stdout or ""),
            stderr=redact_transport_text(completed.stderr or ""),
            duration_ms=int((time.perf_counter() - started) * 1000),
            env_names=tuple(sorted(environment)),
        )


class ProviderTransport(ABC):
    """Camino concreto hacia un proveedor: ejecuta, comprueba salud y declara autenticación."""

    @property
    @abstractmethod
    def kind(self) -> TransportKind:
        """Tipo de transporte (``codex``, ``claude_code``, ``api``, ...)."""

    @property
    @abstractmethod
    def auth_mode(self) -> TransportAuthMode:
        """Modo de autorización del transporte."""

    @property
    @abstractmethod
    def provider(self) -> str:
        """Proveedor al que sirve (``openai``, ``anthropic``, ...)."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Modelo configurado."""

    @abstractmethod
    def execute(
        self,
        request: ProviderRequest,
        *,
        json_schema: Mapping[str, object] | None = None,
        max_output_tokens: int | None = None,
    ) -> ProviderResult:
        """Ejecuta la petición y devuelve el resultado normalizado.

        Raises:
            TransportError: cualquier fallo propio del transporte, ya normalizado.
        """

    @abstractmethod
    def auth_status(self) -> TransportAuthStatus:
        """Estado de autenticación con el mecanismo oficial del cliente."""

    @abstractmethod
    def health_check(self) -> ProviderHealth:
        """Comprobación mínima del transporte."""

    def usage_status(self) -> TransportUsage:
        """Límites declarados por el cliente oficial. Por defecto, ``UNKNOWN``.

        Un transporte que no puede leer límites **no los inventa**: devuelve ``UNKNOWN``.
        """
        return TransportUsage()

    def capabilities(self) -> TransportCapabilities:
        """Capacidades declaradas del transporte."""
        return TransportCapabilities()

    def close(self) -> None:
        """Libera recursos. Por defecto no hay ninguno."""
        return None


def provider_error_kind_of(kind: TransportErrorKind) -> ProviderErrorKind:
    """Traduce la clase de un fallo de transporte al vocabulario de ``ProviderResult``.

    El vocabulario del transporte es más fino (``NOT_INSTALLED``, ``LIMIT_REACHED``, ...). Aquí se
    proyecta sobre el del contrato sin perder lo que el motor necesita distinguir:
    ``PROCESS_FAILED`` se conserva porque no es lo mismo que «no se sabe qué pasó».
    """
    mapping: dict[TransportErrorKind, ProviderErrorKind] = {
        TransportErrorKind.NOT_INSTALLED: ProviderErrorKind.UNAVAILABLE,
        TransportErrorKind.NOT_AUTHENTICATED: ProviderErrorKind.AUTHENTICATION,
        TransportErrorKind.LIMIT_REACHED: ProviderErrorKind.RATE_LIMIT,
        TransportErrorKind.TIMEOUT: ProviderErrorKind.TIMEOUT,
        TransportErrorKind.PROCESS_FAILED: ProviderErrorKind.PROCESS_FAILED,
        TransportErrorKind.INVALID_RESPONSE: ProviderErrorKind.INVALID_RESPONSE,
        TransportErrorKind.UNAVAILABLE: ProviderErrorKind.UNAVAILABLE,
    }
    return mapping[kind]


def transport_result(
    *,
    transport: ProviderTransport,
    request: ProviderRequest,
    content: str,
    model: str = "",
    usage: object = None,
    duration_ms: int = 0,
    finish_reason: str = "",
) -> ProviderResult:
    """Construye el ``ProviderResult`` de un transporte, con el contenido ya saneado."""
    from punto.schemas.execution import ModelUsage

    resolved_usage = usage if isinstance(usage, ModelUsage) else None
    return ProviderResult(
        request_id=request.request_id or uuid4().hex,
        provider=transport.provider,
        model=model or transport.model,
        status=ProviderStatus.SUCCESS,
        role=request.role,
        content=content,
        structured_output=parse_structured_output(content),
        usage=resolved_usage,
        duration_ms=duration_ms,
        finish_reason=finish_reason,
    )


def _as_text(value: object) -> str:
    """Convierte la salida de un proceso en texto, sin sorpresas."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = [
    "DEFAULT_TRANSPORT_TIMEOUT_SECONDS",
    "ENV_ALLOWLIST",
    "ENV_FORBIDDEN_MARKERS",
    "LIMIT_MARKERS",
    "REDACTED",
    "ProviderTransport",
    "RealSubprocessRunner",
    "SubprocessRunner",
    "TransportAuthMode",
    "TransportAuthStatus",
    "TransportCapabilities",
    "TransportConfigError",
    "TransportError",
    "TransportErrorKind",
    "TransportKind",
    "TransportProcess",
    "TransportUsage",
    "TransportUsageStatus",
    "build_environment",
    "clip_text",
    "looks_like_limit",
    "provider_error_kind_of",
    "redact_transport_text",
    "transport_result",
]
