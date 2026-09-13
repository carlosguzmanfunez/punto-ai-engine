"""Soportes de prueba de la capa web y Visual QA (ENGINE-5.3).

Dos cosas que comparten varias pruebas:

1. un generador de **PNG real** (sin dependencias): un PNG mínimo válido con las dimensiones
   pedidas, para que los artefactos se construyan con bytes auténticos y no con relleno;
2. constructores de ``WebSessionReport``, ``VisualSpec`` y ``VisualQATask`` con los que ejercitar
   los checks, los gates y el runner visual sin abrir un navegador.

Nada de este módulo se usa en producción.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from engine52_support import (
    FAKE_KEY,
    FakeAnthropicAPI,
    make_client,
    message_response,
)
from punto.providers.base import ImagePayload
from punto.schemas.visual import (
    RequiredElement,
    VisualQATask,
    VisualSpec,
)
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    Viewport,
    ViewportName,
    WebCheckKind,
    WebCheckOutcome,
    WebSessionReport,
    WebTechnicalStatus,
    build_screenshot_artifact,
)


def png_bytes(width: int, height: int, *, color: tuple[int, int, int] = (16, 24, 40)) -> bytes:
    """PNG RGB real de ``width`` x ``height``, generado sin dependencias externas."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixel = bytes(color)
    raw = b"".join(b"\x00" + pixel * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def screenshots_for(
    routes: tuple[str, ...] = ("/",),
    viewports: tuple[Viewport, ...] = DEFAULT_VIEWPORTS,
    *,
    color: tuple[int, int, int] = (16, 24, 40),
) -> tuple[tuple[Any, ...], dict[str, bytes]]:
    """Artefactos y bytes reales para cada combinación ruta x viewport."""
    artifacts = []
    payloads: dict[str, bytes] = {}
    for route in routes:
        for viewport in viewports:
            data = png_bytes(viewport.width, viewport.height, color=color)
            name = f"{route.strip('/') or 'home'}-{viewport.name.value.lower()}.png"
            artifact = build_screenshot_artifact(
                logical_name=name,
                route=route,
                viewport=viewport,
                data=data,
                browser="chromium 153.0.8010.12",
                playwright_version="1.63.0",
            )
            artifacts.append(artifact)
            payloads[name] = data
    return tuple(artifacts), payloads


def make_session(
    *,
    task_id: UUID | None = None,
    project_id: UUID | None = None,
    routes: tuple[str, ...] = ("/",),
    viewports: tuple[Viewport, ...] = DEFAULT_VIEWPORTS,
    checks: tuple[WebCheckOutcome, ...] | None = None,
    status: WebTechnicalStatus = WebTechnicalStatus.PASS,
    error: str = "",
    findings: tuple[Any, ...] = (),
) -> tuple[WebSessionReport, dict[str, Any]]:
    """Sesión web sintética con screenshots reales y checks en verde por defecto."""
    artifacts, payloads = screenshots_for(routes, viewports)
    if checks is None:
        checks = tuple(
            WebCheckOutcome(kind=kind, ran=True, passed=True, detail="sin problemas")
            for kind in WebCheckKind
        )
    session = WebSessionReport(
        task_id=task_id or uuid4(),
        project_id=project_id or uuid4(),
        status=status,
        summary="sesión web de prueba",
        runtime=(("node", "v24.21.0"), ("npm", "11.19.0")),
        viewports=viewports,
        routes=routes,
        screenshots=artifacts,
        checks=checks,
        findings=findings,
        error=error,
    )
    return session, payloads


def visual_images(
    task: VisualQATask, payloads: dict[str, bytes]
) -> dict[str, ImagePayload]:
    """Convierte los bytes de los artefactos en ``ImagePayload`` verificados.

    Es exactamente lo que hace el backend web en producción: el artefacto produce el payload, no
    una ruta ni un nombre libre.
    """
    return {
        artifact.logical_name: artifact.as_image_payload(payloads[artifact.logical_name])
        for artifact in task.screenshots
        if artifact.logical_name in payloads
    }


def make_spec(
    routes: tuple[str, ...] = ("/",),
    viewports: tuple[Viewport, ...] = DEFAULT_VIEWPORTS,
) -> VisualSpec:
    """Especificación visual exigente y contrastable."""
    return VisualSpec(
        routes=routes,
        viewports=viewports,
        required_elements=(
            RequiredElement(route=routes[0], marker="main", description="Contenido principal"),
        ),
        forbid_horizontal_overflow=True,
        responsive_expectations=("En móvil el contenido no desborda horizontalmente.",),
        accessibility_expectations=("El documento declara título e idioma.",),
        content_expectations=("El titular describe el producto en una frase.",),
        visual_notes=("Jerarquía clara: un titular y una acción principal.",),
    )


def make_visual_task(**overrides: Any) -> VisualQATask:
    """Tarea de Visual QA lista para el runner."""
    routes = overrides.pop("routes", ("/",))
    viewports = overrides.pop("viewports", DEFAULT_VIEWPORTS)
    session, payloads = make_session(
        routes=routes,
        viewports=viewports,
        status=overrides.pop("status", WebTechnicalStatus.PASS),
        checks=overrides.pop("checks", None),
        error=overrides.pop("error", ""),
    )
    base: dict[str, Any] = {
        "task_id": session.task_id,
        "project_id": session.project_id,
        "objective": "Publicar la página de inicio del producto",
        "acceptance_criteria": ("la página carga sin errores y sin desbordamiento",),
        "spec": overrides.pop("spec", make_spec(routes, viewports)),
        "session": session,
        "changed_files": ("app/page.tsx",),
        "context_files": ("app/page.tsx",),
        "source_context": "export default function Page() { return <main /> }",
    }
    base.update(overrides)
    return VisualQATask(**base), payloads  # type: ignore[return-value]


def visual_payload(
    findings: tuple[dict[str, Any], ...] = (),
    *,
    summary: str = "La interfaz cumple la especificación en los tres viewports.",
    notes: str = "Sin objeciones visuales: puede aceptarse.",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propuesta visual limpia con la forma que produce el modelo."""
    proposal: dict[str, Any] = {
        "summary": summary,
        "findings": list(findings),
        "layout_assessment": "El layout mantiene el contenido dentro del viewport.",
        "responsiveness_assessment": "En móvil, tableta y escritorio la composición se adapta.",
        "hierarchy_assessment": "El titular domina y la acción principal queda subordinada.",
        "accessibility_assessment": "Hay título, idioma y contraste suficiente a simple vista.",
        "recommendation_notes": notes,
    }
    if extra:
        proposal.update(extra)
    return proposal


def visual_finding_payload(
    identifier: str = "VQ-1",
    *,
    severity: str = "MEDIUM",
    category: str = "SPACING",
    title: str = "El margen inferior del bloque principal es irregular",
    description: str = "El espacio bajo el titular no coincide con el resto de secciones.",
    route: str = "/",
    viewport: str = "MOBILE",
    evidence: str = "En la captura móvil el bloque queda pegado al borde inferior.",
    recommendation: str = "Igualar el espaciado con el resto de secciones.",
    references_check: str | None = None,
) -> dict[str, Any]:
    """Hallazgo visual con la forma que produce el modelo."""
    payload: dict[str, Any] = {
        "id": identifier,
        "severity": severity,
        "category": category,
        "title": title,
        "description": description,
        "route": route,
        "viewport": viewport,
        "evidence": evidence,
        "recommendation": recommendation,
    }
    if references_check is not None:
        payload["references_check"] = references_check
    return payload


def write_png(directory: Path, name: str, data: bytes) -> Path:
    """Escribe bytes PNG en disco (para pruebas que necesitan un archivo real)."""
    target = directory / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


__all__ = [
    "DEFAULT_VIEWPORTS",
    "FAKE_KEY",
    "FakeAnthropicAPI",
    "ImagePayload",
    "ViewportName",
    "make_client",
    "make_session",
    "make_spec",
    "make_visual_task",
    "message_response",
    "png_bytes",
    "screenshots_for",
    "visual_finding_payload",
    "visual_images",
    "visual_payload",
    "write_png",
]
