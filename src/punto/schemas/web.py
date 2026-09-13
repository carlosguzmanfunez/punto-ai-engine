"""Esquemas de la capa de ejecución web (ENGINE-5.3).

PUNTO puede construir y probar proyectos web, arrancar un navegador real, renderizar páginas y
capturar screenshots. Este módulo es el **contrato** de esa capacidad, y está escrito con tres
reglas fijas:

1. **nada del host como identidad**: un artefacto se identifica por su nombre lógico, su ruta
   lógica, su viewport y el hash de sus bytes, nunca por una ruta absoluta del host. El host que
   construye no tiene por qué ser el host que audita.
2. **PUNTO controla los bytes**: un screenshot viaja como bytes con su hash, y ``ImagePayload``
   se construye desde ahí, con verificación de que los bytes son los que el artefacto declara.
3. **acotado por defecto**: consola, errores, recursos y hallazgos tienen límites explícitos: un
   render defectuoso puede producir miles de mensajes y ninguno se convierte en un artefacto
   ilimitado.

La capa web **no** depende conceptualmente de Next.js: el framework se detecta y se declara, pero
el navegador, los screenshots, los checks y los contratos de Visual QA valen para cualquier
stack web.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.providers.base import ImagePayload
from punto.schemas.enums import FindingSeverity
from punto.schemas.planning import SCHEMA_VERSION

#: Firma de un PNG: los ocho primeros bytes de todo archivo PNG válido.
PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"

#: Alias del tipo ``bytes`` para las firmas de funciones.
#:
#: Dentro de :class:`ScreenshotArtifact` el nombre ``bytes`` es un campo, así que usarlo como
#: tipo en un método del mismo cuerpo lo sombrea. El alias deja el tipo inequívoco.
RawBytes = bytes

#: Media type de los screenshots: PUNTO solo produce PNG.
SCREENSHOT_MEDIA_TYPE: Final[str] = "image/png"

#: Máximo de screenshots en una sesión web.
#:
#: Coincide **a propósito** con el máximo de imágenes por petición multimodal
#: (:data:`~punto.providers.base.MAX_IMAGES`): una sesión que produjera más capturas de las que
#: el contrato multimodal admite dejaría a Visual QA sin poder verlas todas, y evaluar una parte
#: creyendo que es el todo es exactamente lo que el motor no hace. Si la especificación pide más
#: combinaciones ruta x viewport de las que caben, la sesión se bloquea y se dice por qué.
MAX_SCREENSHOTS: Final[int] = 8

#: Máximo de bytes por screenshot (medidos en crudo; el transporte base64 crece ~33 %).
MAX_SCREENSHOT_BYTES: Final[int] = 5_000_000

#: Máximo de mensajes de consola, errores de página y recursos fallidos que se conservan.
MAX_CONSOLE_MESSAGES: Final[int] = 50
MAX_PAGE_ERRORS: Final[int] = 25
MAX_FAILED_RESOURCES: Final[int] = 25

#: Máximo de caracteres de un extracto de salida o de evidencia.
MAX_EXCERPT_CHARS: Final[int] = 2_000

#: Máximo de scripts declarados que se conservan del ``package.json``.
MAX_SCRIPT_NAMES: Final[int] = 40

#: Máximo de dependencias declaradas que se conservan (solo nombres).
MAX_DEPENDENCY_NAMES: Final[int] = 80


class WebFramework(StrEnum):
    """Framework detectado en el proyecto web."""

    NEXTJS = "NEXTJS"
    REACT = "REACT"
    VUE = "VUE"
    SVELTE = "SVELTE"
    ASTRO = "ASTRO"
    NODE = "NODE"
    UNKNOWN = "UNKNOWN"


class PackageManager(StrEnum):
    """Gestor de paquetes detectado por el lockfile o por el campo ``packageManager``."""

    NPM = "NPM"
    PNPM = "PNPM"
    YARN = "YARN"
    BUN = "BUN"
    UNKNOWN = "UNKNOWN"


class WebCommandKind(StrEnum):
    """Acciones conceptuales de la capa web.

    El modelo **no** propone comandos: propone o pide una de estas acciones, y PUNTO la traduce
    a un ``argv`` controlado. No hay shell, no hay comando libre y no hay ``eval``.
    """

    INSTALL_DEPENDENCIES = "INSTALL_DEPENDENCIES"
    TYPECHECK = "TYPECHECK"
    BUILD = "BUILD"
    START_PREVIEW = "START_PREVIEW"
    RUN_TESTS = "RUN_TESTS"
    RUN_PLAYWRIGHT = "RUN_PLAYWRIGHT"
    CAPTURE_SCREENSHOT = "CAPTURE_SCREENSHOT"


class WebCommandStatus(StrEnum):
    """Resultado de una acción web."""

    PASS = "PASS"
    FAIL = "FAIL"
    #: No se pudo ejecutar: sandbox ausente, capacidad no disponible o política.
    BLOCKED = "BLOCKED"


class WebTechnicalStatus(StrEnum):
    """Estado técnico de una sesión web, calculado por PUNTO."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class WebCheckKind(StrEnum):
    """Comprobaciones deterministas del navegador.

    Demuestran **funcionamiento técnico**, no belleza: un PASS aquí no dice que la interfaz sea
    buena, dice que la página carga, no rompe y no desborda.
    """

    PAGE_LOAD_ERROR = "PAGE_LOAD_ERROR"
    CONSOLE_ERROR = "CONSOLE_ERROR"
    PAGE_ERROR = "PAGE_ERROR"
    FAILED_RESOURCE = "FAILED_RESOURCE"
    HORIZONTAL_OVERFLOW = "HORIZONTAL_OVERFLOW"
    VIEWPORT_CLIPPING = "VIEWPORT_CLIPPING"
    BROKEN_IMAGE = "BROKEN_IMAGE"
    MISSING_REQUIRED_ELEMENT = "MISSING_REQUIRED_ELEMENT"
    HYDRATION_ERROR = "HYDRATION_ERROR"
    RESPONSIVE_CHECK = "RESPONSIVE_CHECK"
    ACCESSIBILITY_CHECK = "ACCESSIBILITY_CHECK"


class ViewportName(StrEnum):
    """Viewports deterministas iniciales."""

    MOBILE = "MOBILE"
    TABLET = "TABLET"
    DESKTOP = "DESKTOP"


class Viewport(BaseModel):
    """Viewport con dimensiones explícitas y versionadas."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ViewportName = Field(..., description="Nombre determinista del viewport.")
    width: int = Field(..., ge=1, description="Ancho en píxeles CSS.")
    height: int = Field(..., ge=1, description="Alto en píxeles CSS.")


#: Viewports iniciales, en orden determinista. Las medidas son explícitas a propósito: un
#: «móvil» sin números no es un contrato, es una intención.
DEFAULT_VIEWPORTS: Final[tuple[Viewport, ...]] = (
    Viewport(name=ViewportName.MOBILE, width=390, height=844),
    Viewport(name=ViewportName.TABLET, width=768, height=1024),
    Viewport(name=ViewportName.DESKTOP, width=1440, height=900),
)


class WebProjectProfile(BaseModel):
    """Lo que PUNTO detecta por sí mismo del proyecto web.

    No se cree lo que el modelo diga del stack: se lee ``package.json``, los lockfiles y los
    archivos de configuración, y se declara la evidencia de cada hallazgo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    framework: WebFramework = Field(
        default=WebFramework.UNKNOWN, description="Framework detectado."
    )
    package_manager: PackageManager = Field(
        default=PackageManager.UNKNOWN, description="Gestor de paquetes detectado."
    )
    lockfile: str = Field(default="", description="Lockfile encontrado, si lo hay.")
    package_json: str = Field(default="", description="Ruta lógica del ``package.json``.")
    has_typescript: bool = Field(
        default=False, description="True si hay TypeScript configurado."
    )
    typescript_config: str = Field(default="", description="Ruta lógica del ``tsconfig.json``.")
    has_tailwind: bool = Field(default=False, description="True si Tailwind está configurado.")
    tailwind_config: str = Field(default="", description="Ruta lógica de la config de Tailwind.")
    next_config: str = Field(default="", description="Ruta lógica de la config de Next.js.")
    script_names: tuple[str, ...] = Field(
        default=(), description="Nombres de script declarados en ``package.json``."
    )
    dependency_names: tuple[str, ...] = Field(
        default=(), description="Nombres de dependencias declaradas, sin versiones."
    )
    node_requirement: str = Field(
        default="", description="Rango de Node declarado por el proyecto."
    )
    evidence: tuple[str, ...] = Field(
        default=(), description="Qué archivo o campo demuestra cada parte del perfil."
    )

    @property
    def is_web_project(self) -> bool:
        """True si el perfil corresponde a un proyecto web reconocible."""
        return bool(self.package_json) and self.framework is not WebFramework.UNKNOWN

    def has_script(self, name: str) -> bool:
        """True si el proyecto declara ese script."""
        return name in self.script_names


class WebCommandResult(BaseModel):
    """Resultado de una acción web ejecutada dentro del sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: WebCommandKind = Field(..., description="Acción ejecutada.")
    status: WebCommandStatus = Field(..., description="Resultado de la acción.")
    argv: tuple[str, ...] = Field(
        default=(), description="Argumentos exactos ejecutados, sin shell."
    )
    exit_code: int | None = Field(default=None, description="Código de salida observado.")
    duration_ms: int = Field(default=0, ge=0, description="Duración observada.")
    stdout_excerpt: str = Field(default="", description="Extracto acotado de la salida estándar.")
    stderr_excerpt: str = Field(default="", description="Extracto acotado del error estándar.")
    detail: str = Field(default="", description="Motivo del bloqueo o resumen del resultado.")
    warnings: int = Field(
        default=0, ge=0, description="Avisos contados, nunca tratados como fallo."
    )


class WebConsoleMessage(BaseModel):
    """Mensaje de consola del navegador, saneado y acotado."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: str = Field(..., description="Nivel del mensaje (``error``, ``warning``, ...).")
    text: str = Field(default="", description="Texto acotado del mensaje.")
    route: str = Field(default="", description="Ruta lógica donde ocurrió.")
    viewport: ViewportName | None = Field(default=None, description="Viewport donde ocurrió.")


class WebFailedResource(BaseModel):
    """Recurso que el navegador no pudo cargar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(..., description="URL del recurso, sin credenciales.")
    reason: str = Field(default="", description="Motivo declarado por el navegador.")
    status_code: int | None = Field(default=None, description="Código HTTP, si lo hubo.")
    resource_type: str = Field(default="", description="Tipo declarado por el navegador.")


class WebCheckOutcome(BaseModel):
    """Resultado de una comprobación determinista del navegador."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: WebCheckKind = Field(..., description="Comprobación evaluada.")
    applicable: bool = Field(
        default=True,
        description=(
            "True si la especificación de esta sesión exigía la comprobación. Una comprobación "
            "no aplicable no penaliza el veredicto; una aplicable sin señal lo bloquea."
        ),
    )
    ran: bool = Field(default=True, description="True si la comprobación llegó a ejecutarse.")
    passed: bool = Field(default=True, description="True si no encontró problemas bloqueantes.")
    blocking: bool = Field(
        default=False, description="True si el problema encontrado impide el PASS técnico."
    )
    detail: str = Field(default="", description="Motivo determinista del resultado.")
    findings: int = Field(default=0, ge=0, description="Problemas encontrados.")

    @property
    def measured(self) -> bool:
        """True si la comprobación aplicaba y además se midió."""
        return self.applicable and self.ran

    @property
    def no_signal(self) -> bool:
        """True si la comprobación aplicaba pero no hubo señal: no se puede dar PASS.

        Es la distinción que exige esta fase: **no aplicable** y **aplicable sin señal** no son lo
        mismo. Lo primero no penaliza; lo segundo significa que PUNTO no pudo medir algo que sí
        exigía, así que no puede certificar nada.
        """
        return self.applicable and not self.ran

    @property
    def not_applicable(self) -> bool:
        """True si la comprobación no aplicaba a esta sesión."""
        return not self.applicable

    @property
    def state(self) -> str:
        """Estado legible de la comprobación, sin ambigüedad."""
        if self.not_applicable:
            return "NOT_APPLICABLE"
        if self.no_signal:
            return "NO_SIGNAL"
        return "PASS" if self.passed else "FAIL"


class WebFinding(BaseModel):
    """Problema técnico concreto observado en el navegador."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: WebCheckKind = Field(..., description="Comprobación que lo detectó.")
    severity: FindingSeverity = Field(..., description="Gravedad.")
    route: str = Field(default="", description="Ruta lógica donde se observó.")
    viewport: ViewportName | None = Field(default=None, description="Viewport donde se observó.")
    message: str = Field(..., min_length=1, description="Qué se observó.")
    evidence: str = Field(default="", description="Evidencia acotada, nunca bytes de imagen.")


class AccessibilityObservation(BaseModel):
    """Observaciones de accesibilidad básicas, recogidas en el navegador.

    Son comprobaciones automáticas acotadas, no una certificación: pasar esto **no** significa
    cumplir WCAG, significa que no faltan las señales más gruesas.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_title: str = Field(default="", description="Título del documento.")
    html_lang: str = Field(default="", description="Idioma declarado en ``<html lang>``.")
    images_without_alt: tuple[str, ...] = Field(
        default=(), description="Imágenes sin texto alternativo (identificadores lógicos)."
    )
    buttons_without_name: tuple[str, ...] = Field(
        default=(), description="Controles sin nombre accesible."
    )
    inputs_without_label: tuple[str, ...] = Field(
        default=(), description="Campos de formulario sin etiqueta asociada."
    )
    landmarks: tuple[str, ...] = Field(
        default=(), description="Regiones semánticas encontradas (main, nav, ...)."
    )
    heading_order_ok: bool = Field(
        default=True, description="True si los encabezados no saltan niveles."
    )
    axe_violations: tuple[str, ...] = Field(
        default=(), description="Reglas de axe incumplidas, si la herramienta está disponible."
    )


class RouteObservation(BaseModel):
    """Lo que el navegador observó en una ruta y un viewport concretos.

    Es una **observación**, no un veredicto: el probe dentro del sandbox recoge hechos acotados y
    PUNTO decide después qué constituye un fallo. Separar ambos momentos permite probar los
    checks sin abrir un navegador.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: str = Field(..., min_length=1, description="Ruta lógica renderizada.")
    viewport: ViewportName = Field(..., description="Viewport de la observación.")
    local_url: str = Field(default="", description="URL local usada, sin credenciales.")
    http_status: int | None = Field(default=None, description="Código HTTP observado.")
    load_error: str = Field(default="", description="Error de navegación, si lo hubo.")
    timed_out: bool = Field(default=False, description="True si agotó el tiempo de espera.")
    console_errors: tuple[str, ...] = Field(
        default=(), description=f"Mensajes de consola de nivel error (máx. {MAX_CONSOLE_MESSAGES})."
    )
    console_warning_count: int = Field(
        default=0, ge=0, description="Avisos de consola contados, no tratados como fallo."
    )
    page_errors: tuple[str, ...] = Field(
        default=(), description=f"Excepciones no capturadas (máx. {MAX_PAGE_ERRORS})."
    )
    failed_resources: tuple[WebFailedResource, ...] = Field(
        default=(), description="Recursos que no cargaron."
    )
    broken_images: tuple[str, ...] = Field(
        default=(), description="Imágenes que el navegador no pudo pintar."
    )
    scroll_width: int = Field(default=0, ge=0, description="Ancho del documento en píxeles CSS.")
    client_width: int = Field(default=0, ge=0, description="Ancho del viewport en píxeles CSS.")
    missing_markers: tuple[str, ...] = Field(
        default=(), description="Elementos exigidos por la especificación que no aparecieron."
    )
    hydration_signals: tuple[str, ...] = Field(
        default=(), description="Señales estructuradas de fallo de hidratación."
    )
    screenshot_name: str = Field(
        default="", description="Nombre lógico del screenshot producido para esta observación."
    )
    accessibility: AccessibilityObservation | None = Field(
        default=None, description="Observaciones de accesibilidad, si se recogieron."
    )


class WebObservations(BaseModel):
    """Salida completa del probe que corre **dentro** del sandbox web.

    El probe no decide: recoge hechos, cuenta versiones y deja los PNG en el workspace. PUNTO
    convierte esto en comprobaciones deterministas en el host, donde además conoce la
    especificación visual y puede decidir qué es bloqueante.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: tuple[RouteObservation, ...] = Field(
        default=(), description="Observaciones por ruta y viewport."
    )
    runtime: tuple[tuple[str, str], ...] = Field(
        default=(), description="Versiones reales del entorno (node, npm, python, ...)."
    )
    browser: str = Field(default="", description="Navegador y versión observados.")
    playwright_version: str = Field(default="", description="Versión de Playwright usada.")
    node_version: str = Field(default="", description="Versión de Node del sandbox.")
    notes: tuple[str, ...] = Field(default=(), description="Notas acotadas del probe.")

    def for_route(self, route: str) -> tuple[RouteObservation, ...]:
        """Observaciones de una ruta, en orden."""
        return tuple(item for item in self.observations if item.route == route)


class ScreenshotArtifact(BaseModel):
    """Screenshot capturado, identificado por su contenido y no por una ruta del host.

    Los bytes **no** viven aquí: viven en memoria, bajo control de PUNTO. Este contrato declara
    qué son (nombre lógico, ruta lógica, viewport, dimensiones, media type, tamaño y hash) para
    que el artefacto se pueda auditar sin filtrar imágenes a los informes ni al registro.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_name: str = Field(..., min_length=1, description="Nombre lógico del artefacto.")
    route: str = Field(..., min_length=1, description="Ruta lógica renderizada.")
    viewport: ViewportName = Field(..., description="Viewport de la captura.")
    width: int = Field(..., ge=1, description="Ancho real del PNG en píxeles.")
    height: int = Field(..., ge=1, description="Alto real del PNG en píxeles.")
    media_type: str = Field(default=SCREENSHOT_MEDIA_TYPE, description="Media type del artefacto.")
    bytes: int = Field(..., ge=1, description="Tamaño en bytes de la imagen.")
    sha256: str = Field(..., min_length=64, max_length=64, description="Hash de los bytes.")
    captured_at: datetime = Field(default_factory=utc_now, description="Momento de captura (UTC).")
    browser: str = Field(default="", description="Navegador y versión, si se conocen.")
    playwright_version: str = Field(default="", description="Versión de Playwright usada.")

    def as_image_payload(self, data: RawBytes) -> ImagePayload:
        """Convierte los bytes **verificados** en un ``ImagePayload`` del contrato multimodal.

        Raises:
            ValueError: si los bytes no coinciden con lo que el artefacto declara (tamaño o
                hash). Un par artefacto/bytes descuadrado no se envía a ningún modelo.
        """
        if len(data) != self.bytes:
            raise ValueError(
                f"los bytes de {self.logical_name!r} ocupan {len(data)} y el artefacto declara "
                f"{self.bytes}"
            )
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError(
                f"los bytes de {self.logical_name!r} no coinciden con el hash del artefacto"
            )
        return ImagePayload(
            data=data, media_type=self.media_type, logical_name=self.logical_name
        )


class WebSessionReport(BaseModel):
    """Resultado técnico de una sesión web completa, calculado por PUNTO.

    No es un veredicto visual: es la evidencia de que la página carga, no rompe y no desborda.
    El veredicto visual lo produce el rol de Visual QA a partir de esto.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador del informe.")
    created_at: datetime = Field(default_factory=utc_now, description="Creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea evaluada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    status: WebTechnicalStatus = Field(..., description="Estado técnico calculado por PUNTO.")
    summary: str = Field(default="", description="Resumen del resultado.")

    profile: WebProjectProfile | None = Field(
        default=None, description="Perfil detectado del proyecto."
    )
    runtime: tuple[tuple[str, str], ...] = Field(
        default=(), description="Versiones reales observadas (node, npm, chromium, ...)."
    )
    commands: tuple[WebCommandResult, ...] = Field(
        default=(), description="Acciones ejecutadas y su resultado."
    )
    viewports: tuple[Viewport, ...] = Field(default=(), description="Viewports usados.")
    routes: tuple[str, ...] = Field(default=(), description="Rutas lógicas renderizadas.")
    screenshots: tuple[ScreenshotArtifact, ...] = Field(
        default=(), description="Artefactos capturados, sin bytes."
    )
    checks: tuple[WebCheckOutcome, ...] = Field(
        default=(), description="Comprobaciones deterministas evaluadas."
    )
    findings: tuple[WebFinding, ...] = Field(
        default=(), description="Problemas técnicos observados."
    )
    console: tuple[WebConsoleMessage, ...] = Field(
        default=(), description="Mensajes de consola acotados."
    )
    page_errors: tuple[str, ...] = Field(default=(), description="Errores de página acotados.")
    failed_resources: tuple[WebFailedResource, ...] = Field(
        default=(), description="Recursos que no cargaron."
    )
    evidence: tuple[str, ...] = Field(default=(), description="Notas de evidencia del ciclo.")

    sdk: tuple[tuple[str, str], ...] = Field(
        default=(), description="Productor de la sesión y su versión (detección de deriva)."
    )
    started_at: datetime = Field(default_factory=utc_now, description="Inicio de la sesión.")
    completed_at: datetime | None = Field(default=None, description="Fin de la sesión.")
    error: str = Field(default="", description="Motivo del bloqueo, si lo hubo.")

    @property
    def blocking_checks(self) -> tuple[WebCheckOutcome, ...]:
        """Comprobaciones aplicables que impiden el PASS técnico."""
        return tuple(
            check
            for check in self.checks
            if check.measured and not check.passed and check.blocking
        )

    @property
    def failed_checks(self) -> tuple[WebCheckOutcome, ...]:
        """Comprobaciones aplicables que no pasaron, bloqueen o no."""
        return tuple(check for check in self.checks if check.measured and not check.passed)

    @property
    def no_signal_checks(self) -> tuple[WebCheckOutcome, ...]:
        """Comprobaciones que la sesión exigía y que no llegaron a medirse."""
        return tuple(check for check in self.checks if check.no_signal)

    @property
    def not_applicable_checks(self) -> tuple[WebCheckOutcome, ...]:
        """Comprobaciones que no aplicaban a esta sesión."""
        return tuple(check for check in self.checks if check.not_applicable)

    def command(self, kind: WebCommandKind) -> WebCommandResult | None:
        """Resultado de una acción, o ``None`` si no se ejecutó."""
        for result in self.commands:
            if result.kind is kind:
                return result
        return None

    def screenshot(self, logical_name: str) -> ScreenshotArtifact | None:
        """Artefacto por nombre lógico, o ``None`` si no existe."""
        for artifact in self.screenshots:
            if artifact.logical_name == logical_name:
                return artifact
        return None


def is_valid_png(data: bytes) -> bool:
    """True si los bytes empiezan con la firma PNG y traen la cabecera mínima.

    Se comprueba la firma y que el bloque IHDR esté completo: basta para distinguir un PNG real
    de un archivo vacío, truncado o de un error del navegador guardado como texto.
    """
    if len(data) < 33 or not data.startswith(PNG_SIGNATURE):
        return False
    return data[12:16] == b"IHDR"


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Ancho y alto de un PNG, leídos de su cabecera IHDR.

    Raises:
        ValueError: si los bytes no son un PNG con cabecera utilizable.
    """
    if not is_valid_png(data):
        raise ValueError("los bytes no son un PNG con cabecera IHDR válida")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        raise ValueError(f"dimensiones PNG no válidas: {width}x{height}")
    return width, height


def build_screenshot_artifact(
    *,
    logical_name: str,
    route: str,
    viewport: Viewport,
    data: bytes,
    browser: str = "",
    playwright_version: str = "",
) -> ScreenshotArtifact:
    """Construye el artefacto de un screenshot a partir de sus bytes.

    Valida el PNG y extrae las dimensiones **reales** de la cabecera: el artefacto declara lo que
    la imagen es, no lo que se pidió que fuera.

    Raises:
        ValueError: si los bytes no son un PNG válido.
    """
    width, height = png_dimensions(data)
    return ScreenshotArtifact(
        logical_name=logical_name,
        route=route,
        viewport=viewport.name,
        width=width,
        height=height,
        media_type=SCREENSHOT_MEDIA_TYPE,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        browser=browser,
        playwright_version=playwright_version,
    )


__all__ = [
    "DEFAULT_VIEWPORTS",
    "MAX_CONSOLE_MESSAGES",
    "MAX_DEPENDENCY_NAMES",
    "MAX_EXCERPT_CHARS",
    "MAX_FAILED_RESOURCES",
    "MAX_PAGE_ERRORS",
    "MAX_SCREENSHOTS",
    "MAX_SCREENSHOT_BYTES",
    "MAX_SCRIPT_NAMES",
    "PNG_SIGNATURE",
    "SCREENSHOT_MEDIA_TYPE",
    "AccessibilityObservation",
    "PackageManager",
    "RawBytes",
    "RouteObservation",
    "ScreenshotArtifact",
    "Viewport",
    "ViewportName",
    "WebCheckKind",
    "WebCheckOutcome",
    "WebCommandKind",
    "WebCommandResult",
    "WebCommandStatus",
    "WebConsoleMessage",
    "WebFailedResource",
    "WebFinding",
    "WebFramework",
    "WebObservations",
    "WebProjectProfile",
    "WebSessionReport",
    "WebTechnicalStatus",
    "build_screenshot_artifact",
    "is_valid_png",
    "png_dimensions",
]
