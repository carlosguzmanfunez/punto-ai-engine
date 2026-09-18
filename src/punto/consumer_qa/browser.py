"""Frontera de navegador de QA Consumer: una sesión real en el sandbox web que ya existe.

Este es el único módulo del consumidor que habla con el navegador, y lo hace **reutilizando** la
capa web de ENGINE-5.3: el mismo sandbox endurecido, la misma imagen con Playwright y Chromium, la
misma comprobación de evidencia y las mismas capturas verificadas por hash. QA Consumer no lanza el
navegador por su cuenta ni monta contenedores a mano: pide una sesión y traduce lo observado.

La sesión arranca la aplicación con el mecanismo de *preview* existente (argv controlado dentro del
contenedor, alias de red propio, espera de disponibilidad y destrucción limpia), ejecuta la
interacción declarada y devuelve hechos: URL final, código HTTP, marcadores ausentes, errores de
consola, excepciones no capturadas, recursos fallidos, acciones y una captura.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from punto.consumer_qa.model import (
    ConsumerQAError,
    QAActionRecord,
    QAEvidence,
    QAExpectation,
    QAExpectationKind,
    QAStep,
)
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    RouteObservation,
    Viewport,
    WebActionOutcome,
)
from punto.web.sandbox import (
    DEFAULT_PREVIEW_PORT,
    WebSandboxBackend,
    WebSandboxEvidenceError,
    WebSandboxSessionError,
    WebSandboxUnavailableError,
    WebSessionRun,
)

#: Viewport por defecto del consumidor: el primero del contrato web (móvil 390x844). El consumidor
#: comprueba una aplicación como la vería una persona en ese tamaño; otro viewport se pide con el
#: argumento ``viewport`` de :func:`punto.consumer_qa.run_consumer_qa`.
DEFAULT_CONSUMER_VIEWPORT: Viewport = DEFAULT_VIEWPORTS[0]


class QASessionUnavailable(ConsumerQAError):
    """El navegador no está disponible en este entorno (dependencia ambiental explícita)."""


@dataclass(frozen=True, slots=True)
class QATarget:
    """La aplicación bajo prueba: dónde vive y cómo se arranca.

    ``preview_argv`` es el servidor de la aplicación, con el mismo mecanismo que usa la capa web:
    se ejecuta **dentro** del contenedor con un ``argv`` controlado, nunca en el host ni por shell.
    """

    workspace: Path
    preview_argv: tuple[tuple[str, ...], ...]
    project_relative: str = "."
    preview_port: int = DEFAULT_PREVIEW_PORT
    #: Imagen del contenedor que ejecuta la aplicación; vacío significa la del sandbox web.
    preview_image: str = ""
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        """Rechaza un objetivo incompleto antes de montar ningún contenedor."""
        if not self.workspace.is_dir():
            raise ConsumerQAError(f"el workspace de la aplicación no existe: {self.workspace}")
        if not self.preview_argv:
            raise ConsumerQAError("el objetivo necesita al menos un comando de arranque")
        for index, argv in enumerate(self.preview_argv):
            if not argv or any(not item for item in argv):
                raise ConsumerQAError(f"preview_argv[{index}] no puede estar vacío")


@dataclass(frozen=True, slots=True)
class QASessionResult:
    """Lo que la sesión observó: la observación de la ruta y su evidencia."""

    observation: RouteObservation
    evidence: QAEvidence
    notes: tuple[str, ...]


def session_actions(
    steps: tuple[QAStep, ...], expectations: tuple[QAExpectation, ...]
) -> tuple[tuple[str, dict[str, str]], ...]:
    """Traduce pasos y expectativas a la interacción que ejecutará el navegador.

    Las expectativas de ``visible`` y ``text_contains`` se comprueban **en el navegador** (con
    ``assert_visible`` y ``assert_text``): la visibilidad y el texto son hechos del DOM, no algo que
    el host pueda deducir. El resto se comprueba en el host con lo que la sesión devolvió.

    Returns:
        Una tupla de ``(etiqueta, acción)`` en orden de ejecución: primero los pasos del usuario y
        después las comprobaciones de expectativas.
    """
    planned: list[tuple[str, dict[str, str]]] = []
    for index, step in enumerate(steps, start=1):
        planned.append(
            (
                f"paso {index} ({step.label()})",
                {"kind": step.kind.value, "target": step.target, "value": step.value},
            )
        )
    for index, expectation in enumerate(expectations, start=1):
        label = f"expectativa {index} ({expectation.label()})"
        if expectation.kind is QAExpectationKind.VISIBLE:
            planned.append(
                (label, {"kind": "assert_visible", "target": expectation.target, "value": ""})
            )
        elif expectation.kind is QAExpectationKind.TEXT_CONTAINS:
            planned.append(
                (
                    label,
                    {
                        "kind": "assert_text",
                        "target": expectation.target,
                        "value": expectation.value,
                    },
                )
            )
    return tuple(planned)


def run_browser_session(
    *,
    start_url: str,
    planned: tuple[tuple[str, dict[str, str]], ...],
    target: QATarget,
    evidence_dir: Path | None,
    artifact_prefix: str,
    viewport: Viewport | None = None,
    backend: WebSandboxBackend | None = None,
) -> QASessionResult:
    """Ejecuta la sesión de navegador y construye la evidencia.

    Raises:
        QASessionUnavailable: si la imagen o el runtime del sandbox no están disponibles.
        ConsumerQAError: si la sesión no se puede completar (el proyecto no arranca, el navegador
            falla, la evidencia no cuadra). Nunca se convierte en ``PASS``.
    """
    chosen_viewport = viewport if viewport is not None else DEFAULT_CONSUMER_VIEWPORT
    engine = backend if backend is not None else WebSandboxBackend()
    try:
        run = engine.run_session(
            workspace=target.workspace,
            project_relative=target.project_relative,
            preview_argv=list(target.preview_argv),
            route=start_url,
            viewports=(chosen_viewport,),
            required_markers=(),
            actions=tuple(action for _, action in planned),
            preview_port=target.preview_port,
            preview_image=target.preview_image,
            timeout_seconds=target.timeout_seconds,
        )
    except WebSandboxUnavailableError as error:
        raise QASessionUnavailable(str(error)) from error
    except (WebSandboxSessionError, WebSandboxEvidenceError) as error:
        raise ConsumerQAError(f"la sesión de navegador no se completó: {error}") from error

    observations = run.observations.observations
    if not observations:
        raise ConsumerQAError("la sesión no devolvió ninguna observación de la ruta")
    observation = observations[0]

    saved_path, saved_note = _persist_screenshot(
        run=run,
        observation=observation,
        evidence_dir=evidence_dir,
        artifact_prefix=artifact_prefix,
    )
    artifact = run.artifact(observation.screenshot_name)
    evidence = QAEvidence(
        url=observation.local_url,
        final_url=observation.final_url,
        final_route=observation.final_route,
        http_status=observation.http_status,
        browser=run.observations.browser,
        playwright_version=run.observations.playwright_version,
        console_errors=observation.console_errors,
        console_warnings=observation.console_warning_count,
        page_errors=observation.page_errors,
        failed_resources=tuple(item.url for item in observation.failed_resources),
        actions=tuple(_action_record(item) for item in observation.actions),
        screenshot_path=str(saved_path) if saved_path is not None else "",
        screenshot_sha256="" if artifact is None else artifact.sha256,
        screenshot_bytes=0 if artifact is None else artifact.bytes,
        screenshot_note=saved_note,
        route_mismatch=observation.route_mismatch,
        load_error=observation.load_error,
    )
    return QASessionResult(observation=observation, evidence=evidence, notes=run.notes)


def _action_record(item: WebActionOutcome) -> QAActionRecord:
    """Traduce una acción observada al registro del consumidor."""
    return QAActionRecord(
        kind=item.kind, target=item.target, status=item.status, detail=item.detail
    )


def _persist_screenshot(
    *,
    run: WebSessionRun,
    observation: RouteObservation,
    evidence_dir: Path | None,
    artifact_prefix: str,
) -> tuple[Path | None, str]:
    """Conserva la captura de la sesión y explica qué pasó con ella.

    Se guarda **una** captura: la del estado final de la sesión. En un ``PASS`` es la prueba de lo
    que se vio; en un ``FAIL`` es la captura del punto de fallo, porque la interacción se detiene en
    la primera acción que no puede ejecutarse.
    """
    data = run.screenshots.get(observation.screenshot_name)
    if not data:
        return None, "la sesión no produjo una captura verificada"
    if evidence_dir is None:
        return None, "los bytes no se persistieron: no se pidió directorio de evidencia"
    destination = evidence_dir / f"{artifact_prefix}-{observation.screenshot_name}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination, ""


__all__ = [
    "DEFAULT_CONSUMER_VIEWPORT",
    "QASessionResult",
    "QASessionUnavailable",
    "QATarget",
    "run_browser_session",
    "session_actions",
]
