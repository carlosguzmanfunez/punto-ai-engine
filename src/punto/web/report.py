"""Informe técnico de una sesión web (ENGINE-5.3).

El navegador observa; PUNTO decide. Este módulo convierte lo observado por el probe en el
``WebSessionReport`` que consume Visual QA y calcula el estado técnico con reglas explícitas:

- ``BLOCKED``: no hubo sesión que evaluar. El sandbox, la preparación del proyecto o el navegador
  no llegaron a producir observaciones, así que no hay nada medido. Es la **única** forma de
  bloquear: no se inventan medidas ni se rellena el hueco con una suposición;
- ``FAIL``: la sesión se pudo evaluar y alguna comprobación determinista encontró un problema. Un
  fallo bloqueante sigue siendo ``FAIL`` y no ``BLOCKED``: la página existía y se pudo mirar, y
  Visual QA pedirá cambios en consecuencia;
- ``PASS``: todas las comprobaciones que llegaron a ejecutarse pasaron.

Una comprobación **sin señal** (``ran=False``) no suspende una página ni la aprueba: se declara
como no ejecutada y se informa. Nada se omite en silencio.

Un ``PASS`` aquí no dice que la interfaz sea buena: dice que la página carga, no rompe, no
desborda y cumple los marcadores exigidos. El juicio visual es de Visual QA, y su veredicto final
lo calcula PUNTO sobre estos hechos.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from punto.common import utc_now
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    MAX_CONSOLE_MESSAGES,
    MAX_EXCERPT_CHARS,
    MAX_FAILED_RESOURCES,
    MAX_PAGE_ERRORS,
    Viewport,
    WebCheckOutcome,
    WebCommandKind,
    WebCommandResult,
    WebConsoleMessage,
    WebFailedResource,
    WebObservations,
    WebProjectProfile,
    WebSessionReport,
    WebTechnicalStatus,
)
from punto.web.checks import evaluate_web_checks
from punto.web.sandbox import WebSessionRun

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger


def determine_web_status(
    checks: Sequence[WebCheckOutcome],
) -> tuple[WebTechnicalStatus, tuple[str, ...]]:
    """Calcula el estado técnico a partir de las comprobaciones deterministas.

    Reglas, en este orden:

    1. sin comprobaciones, o sin ninguna **aplicable** → ``BLOCKED``: no había nada que medir;
    2. alguna comprobación aplicable **sin señal** → ``BLOCKED``: PUNTO exigía medirla y no la
       midió, así que no puede certificar nada. Esta regla va **antes** que el fallo medido: culpar
       al producto de una medición que no existe sería tan incorrecto como aprobarlo;
    3. alguna comprobación aplicable y medida que no pasó → ``FAIL``;
    4. todo lo aplicable, medido y en verde → ``PASS``. Lo **no aplicable** no penaliza.

    Args:
        checks: Resultados de las once comprobaciones, en su orden de contrato.

    Returns:
        ``(status, reasons)``: el estado y los motivos, en orden determinista.
    """
    if not checks:
        return WebTechnicalStatus.BLOCKED, ("no se evaluó ninguna comprobación determinista",)

    applicable = tuple(check for check in checks if check.applicable)
    if not applicable:
        return WebTechnicalStatus.BLOCKED, (
            f"ninguna de las {len(checks)} comprobaciones aplica a esta sesión: "
            + ", ".join(check.kind.value for check in checks),
        )

    measured = tuple(check for check in applicable if check.ran)
    if not measured:
        return WebTechnicalStatus.BLOCKED, (
            f"ninguna de las {len(applicable)} comprobaciones aplicables llegó a medirse: "
            + ", ".join(check.kind.value for check in applicable),
        )

    no_signal = tuple(check for check in applicable if not check.ran)
    failed = tuple(check for check in measured if not check.passed)
    blocking = tuple(check for check in failed if check.blocking)
    not_applicable = tuple(check for check in checks if not check.applicable)

    reasons: list[str] = []
    if blocking:
        reasons.append(
            "fallo(s) bloqueante(s): " + ", ".join(check.kind.value for check in blocking)
        )
    if failed:
        reasons.append(
            "comprobación(es) no superada(s): " + ", ".join(check.kind.value for check in failed)
        )
    if no_signal:
        reasons.append(
            "comprobación(es) aplicable(s) sin señal: "
            + ", ".join(check.kind.value for check in no_signal)
        )
    if not_applicable:
        reasons.append(
            "comprobación(es) no aplicable(s): "
            + ", ".join(check.kind.value for check in not_applicable)
        )

    if no_signal:
        return WebTechnicalStatus.BLOCKED, tuple(reasons)
    if failed:
        return WebTechnicalStatus.FAIL, tuple(reasons)
    return WebTechnicalStatus.PASS, tuple(reasons)


def build_web_session_report(
    *,
    task_id: UUID,
    project_id: UUID,
    run: WebSessionRun,
    route: str = "/",
    viewports: Sequence[Viewport] = DEFAULT_VIEWPORTS,
    required_markers: Sequence[str] = (),
    profile: WebProjectProfile | None = None,
    commands: Sequence[WebCommandResult] = (),
    summary: str = "",
    started_at: datetime | None = None,
    runtime: str = "",
    image: str = "",
    audit: AuditLogger | None = None,
) -> WebSessionReport:
    """Evalúa los once checks sobre lo observado y compone el informe técnico.

    Args:
        task_id: Tarea evaluada.
        project_id: Proyecto al que pertenece.
        run: Sesión ya ejecutada por el sandbox web, con sus bytes verificados.
        route: Ruta lógica observada (la primera si la sesión cubrió varias).
        viewports: Viewports que la sesión debía medir, en orden.
        required_markers: Marcadores exigidos por la especificación, en la sintaxis del probe.
        profile: Perfil del proyecto detectado, si se conoce.
        commands: Resultados de las acciones ejecutadas antes de la captura (build, tipos, ...).
        summary: Resumen textual opcional, delante del calculado.
        started_at: Inicio de la sesión, si el llamante lo conoce.
        runtime: Runtime OCI que ejecutó la sesión (``podman``), para dejar constancia.
        image: Imagen del sandbox usada, para dejar constancia.
        audit: Registro de auditoría; si se pasa, se registran los checks y los builds.

    Returns:
        El informe con los checks, los hallazgos y el estado calculado por PUNTO.
    """
    observations = run.observations
    checks, findings = evaluate_web_checks(
        observations,
        required_markers=tuple(required_markers),
        required_viewports=tuple(viewport.name for viewport in viewports),
    )
    status, reasons = determine_web_status(checks)
    routes = _routes(observations, fallback=route)
    parts = [
        f"Sesión web {status.value}: {len(checks)} comprobación(es)",
        f"{len(findings)} hallazgo(s) técnico(s)",
        f"{len(run.artifacts)} captura(s)",
    ]
    if reasons:
        parts.extend(reasons)
    report = WebSessionReport(
        task_id=task_id,
        project_id=project_id,
        status=status,
        summary=" · ".join(([summary] if summary else []) + parts),
        profile=profile,
        runtime=run.runtime,
        commands=tuple(commands),
        viewports=tuple(viewports),
        routes=routes,
        screenshots=run.artifacts,
        checks=checks,
        findings=findings,
        console=_console_messages(observations),
        page_errors=_page_errors(observations),
        failed_resources=_failed_resources(observations),
        evidence=_evidence(run, routes, runtime=runtime, image=image),
        started_at=started_at or utc_now(),
        completed_at=utc_now(),
    )
    _audit_checks(audit, report, checks)
    _audit_commands(audit, report, commands)
    return report


def build_blocked_web_session_report(
    *,
    task_id: UUID,
    project_id: UUID,
    error: str,
    route: str = "",
    viewports: Sequence[Viewport] = (),
    profile: WebProjectProfile | None = None,
    commands: Sequence[WebCommandResult] = (),
    started_at: datetime | None = None,
) -> WebSessionReport:
    """Informe de una sesión que **no** llegó a producir observaciones.

    Se usa cuando el sandbox no está disponible, el proyecto no se prepara o el navegador no
    arranca. No se rellenan checks: no hubo medida, y un check vacío sería una invención.
    """
    reason = error.strip() or "la sesión web no llegó a ejecutarse"
    return WebSessionReport(
        task_id=task_id,
        project_id=project_id,
        status=WebTechnicalStatus.BLOCKED,
        summary=f"Sesión web BLOCKED: {reason}",
        profile=profile,
        commands=tuple(commands),
        viewports=tuple(viewports),
        routes=(route,) if route else (),
        evidence=(f"bloqueo: {reason}",),
        started_at=started_at or utc_now(),
        completed_at=utc_now(),
        error=reason,
    )


def _routes(observations: WebObservations, *, fallback: str) -> tuple[str, ...]:
    """Rutas efectivamente observadas, en orden de aparición y sin duplicados."""
    seen: list[str] = []
    for item in observations.observations:
        if item.route and item.route not in seen:
            seen.append(item.route)
    if seen:
        return tuple(seen)
    return (fallback,) if fallback else ()


def _console_messages(observations: WebObservations) -> tuple[WebConsoleMessage, ...]:
    """Mensajes de consola en nivel de error, acotados por el contrato."""
    messages: list[WebConsoleMessage] = []
    for item in observations.observations:
        for text in item.console_errors:
            messages.append(
                WebConsoleMessage(
                    level="error",
                    text=_excerpt(text),
                    route=item.route,
                    viewport=item.viewport,
                )
            )
            if len(messages) >= MAX_CONSOLE_MESSAGES:
                return tuple(messages)
    return tuple(messages)


def _page_errors(observations: WebObservations) -> tuple[str, ...]:
    """Errores de página, acotados y con su ruta delante."""
    errors: list[str] = []
    for item in observations.observations:
        for text in item.page_errors:
            errors.append(f"{item.route} [{item.viewport.value}] {_excerpt(text)}")
            if len(errors) >= MAX_PAGE_ERRORS:
                return tuple(errors)
    return tuple(errors)


def _failed_resources(observations: WebObservations) -> tuple[WebFailedResource, ...]:
    """Recursos que el navegador no pudo cargar, acotados por el contrato."""
    resources: list[WebFailedResource] = []
    for item in observations.observations:
        for resource in item.failed_resources:
            resources.append(resource)
            if len(resources) >= MAX_FAILED_RESOURCES:
                return tuple(resources)
    return tuple(resources)


def _excerpt(value: str) -> str:
    """Recorta un texto al máximo del contrato, marcando el recorte."""
    text = value.strip()
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    return text[:MAX_EXCERPT_CHARS] + "…"


def _evidence(
    run: WebSessionRun, routes: tuple[str, ...], *, runtime: str = "", image: str = ""
) -> tuple[str, ...]:
    """Evidencia textual del ciclo: qué se ejecutó, con qué y dónde.

    Las notas del probe se acotan una a una: un informe no puede crecer sin límite porque el
    probe haya decidido escribir mucho.
    """
    observations = run.observations
    evidence = [
        f"rutas observadas: {', '.join(routes) if routes else 'ninguna'}",
        f"capturas verificadas: {len(run.screenshots)} de {len(run.artifacts)} artefacto(s)",
    ]
    if runtime:
        evidence.append(f"runtime OCI: {runtime}")
    if image:
        evidence.append(f"imagen del sandbox: {image}")
    if observations.browser:
        evidence.append(f"navegador: {observations.browser}")
    if observations.playwright_version:
        evidence.append(f"playwright: {observations.playwright_version}")
    for name, version in run.runtime:
        evidence.append(f"{name}: {version}")
    evidence.extend(_excerpt(note) for note in run.notes)
    return tuple(evidence)


def _audit_checks(
    audit: AuditLogger | None, report: WebSessionReport, checks: tuple[WebCheckOutcome, ...]
) -> None:
    """Registra el resultado de cada comprobación determinista."""
    if audit is None:
        return
    for check in checks:
        audit.log_browser_check_recorded(
            project_id=report.project_id,
            task_id=report.task_id,
            check=check.kind.value,
            ran=check.ran,
            passed=check.passed,
            blocking=check.blocking,
            findings=check.findings,
            detail=check.detail,
        )


def _audit_commands(
    audit: AuditLogger | None,
    report: WebSessionReport,
    commands: Sequence[WebCommandResult],
) -> None:
    """Registra las acciones de proyecto ya terminadas.

    Solo se emite ``WEB_BUILD_COMPLETED`` para una acción de construcción: llamar «build» a una
    comprobación de tipos sería etiquetar mal lo que pasó. ``WEB_BUILD_STARTED`` es del llamante
    que lanza la construcción, porque es el único que puede afirmar que empezó.
    """
    if audit is None:
        return
    for command in commands:
        if command.kind is not WebCommandKind.BUILD:
            continue
        audit.log_web_build_completed(
            project_id=report.project_id,
            task_id=report.task_id,
            command=" ".join(command.argv),
            status=command.status.value,
            exit_code=command.exit_code,
            duration_ms=command.duration_ms,
            warnings=command.warnings,
            detail=command.detail,
        )


__all__ = [
    "build_blocked_web_session_report",
    "build_web_session_report",
    "determine_web_status",
]
