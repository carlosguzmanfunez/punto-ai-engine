"""Frontera de contexto del modelo (ENGINE-5.1).

El motor solo puede aceptar un hallazgo del modelo sobre un archivo cuyo contenido el
modelo **recibió**. Ese invariante no puede depender de que un validador y un constructor de
prompt coincidan por casualidad: necesita una única fuente determinista.

Este módulo la construye. Dada una lista de rutas autorizadas y un workspace, devuelve
exactamente:

- ``visible_paths``: las rutas cuyo contenido se envió al modelo, en orden;
- ``content``: el texto que el modelo recibió, tal cual;
- ``omitted_paths``: las rutas que **no** se enviaron, declaradas y no silenciadas;
- ``truncated_paths``: las rutas cuyo contenido se envió recortado;
- ``line_counts``: cuántas líneas de cada ruta visible pudo ver el modelo.

Regla de la frontera, sin excepciones: **una allowlist vacía no autoriza nada**. Si no hay
contexto visible, ningún hallazgo del modelo puede señalar un archivo. Y nada se omite en
silencio: lo que no cabe se declara para que quien decide pueda bloquear.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from punto.qa.paths import normalize_relative_path

#: Máximo de archivos que se envían al modelo en una sola revisión.
MAX_MODEL_VISIBLE_PATHS: Final[int] = 30

#: Máximo de caracteres de un archivo incluido en el contexto de revisión.
MAX_CONTEXT_FILE_CHARS: Final[int] = 40_000

#: Máximo de caracteres del contexto completo. Lo que no cabe se declara como omitido.
MAX_MODEL_CONTEXT_CHARS: Final[int] = 400_000

#: Motivo de bloqueo cuando el contexto necesario no cabe en el presupuesto del modelo.
BLOCKED_CONTEXT_LIMIT: Final[str] = "CONTEXT_LIMIT_EXCEEDED"

#: Marcador visible de archivo declarado que no está en el workspace.
MISSING_FILE_MARKER: Final[str] = "(no existe)"

#: Marcador visible de archivo declarado que no se pudo leer.
UNREADABLE_FILE_MARKER: Final[str] = "(no legible)"


@dataclass(frozen=True, slots=True)
class ModelReviewContext:
    """Lo que el modelo efectivamente recibió, y lo que no."""

    visible_paths: tuple[str, ...] = ()
    omitted_paths: tuple[str, ...] = ()
    truncated_paths: tuple[str, ...] = ()
    content: str = ""
    line_counts: tuple[tuple[str, int], ...] = ()

    @property
    def complete(self) -> bool:
        """True si ningún archivo declarado quedó fuera del contexto."""
        return not self.omitted_paths

    @property
    def visible_set(self) -> frozenset[str]:
        """Conjunto exacto de rutas visibles, para validar hallazgos."""
        return frozenset(self.visible_paths)

    def line_map(self) -> dict[str, int]:
        """Líneas visibles por ruta, con la convención del motor."""
        return dict(self.line_counts)

    def line_count(self, path: str) -> int | None:
        """Líneas visibles de una ruta, o ``None`` si no fue visible."""
        return dict(self.line_counts).get(path)

    def omission_detail(self) -> str:
        """Descripción legible de lo omitido, para evidencia y auditoría."""
        if not self.omitted_paths:
            return ""
        return (
            f"contexto incompleto: {len(self.omitted_paths)} archivo(s) declarado(s) no "
            f"enviado(s) al modelo ({', '.join(self.omitted_paths[:5])}"
            f"{'…' if len(self.omitted_paths) > 5 else ''}); el límite es "
            f"{MAX_MODEL_VISIBLE_PATHS} archivo(s)"
        )

    def annotated_content(self) -> str:
        """Contenido visible, con las omisiones declaradas dentro del propio contexto.

        El modelo nunca debe creer que vio todo el proyecto cuando no fue así.
        """
        if not self.content:
            return "(no se declararon archivos)"
        if not self.omitted_paths:
            return self.content
        return (
            f"{self.content}\n\n"
            f"[contexto parcial: {len(self.omitted_paths)} archivo(s) declarado(s) no "
            f"incluido(s) aquí: {', '.join(self.omitted_paths[:5])}"
            f"{'…' if len(self.omitted_paths) > 5 else ''}]"
        )


def build_model_review_context(
    workspace: Path | str,
    paths: Iterable[str],
    *,
    max_paths: int = MAX_MODEL_VISIBLE_PATHS,
    max_file_chars: int = MAX_CONTEXT_FILE_CHARS,
    max_total_chars: int = MAX_MODEL_CONTEXT_CHARS,
) -> ModelReviewContext:
    """Construye el contexto visible al modelo, de forma pura y determinista.

    Las rutas inválidas se descartan; las repetidas se deduplican conservando el orden. Todo
    lo que no entre en el presupuesto queda en ``omitted_paths``: **nunca** se omite en
    silencio, y quien llama decide si eso basta para bloquear la revisión.

    Args:
        workspace: Raíz del workspace (solo lectura).
        paths: Rutas declaradas, en orden de prioridad.
        max_paths: Máximo de archivos enviados.
        max_file_chars: Máximo de caracteres por archivo.
        max_total_chars: Máximo de caracteres del contexto completo.

    Returns:
        El contexto visible, con sus omisiones y recortes declarados.
    """
    root = Path(workspace)
    ordered: list[str] = []
    for raw in _iter_paths(paths):
        try:
            relative = normalize_relative_path(raw)
        except ValueError:
            continue
        if relative not in ordered:
            ordered.append(relative)

    visible = ordered[:max_paths]
    omitted: list[str] = list(ordered[max_paths:])

    chunks: list[str] = []
    line_counts: list[tuple[str, int]] = []
    truncated: list[str] = []
    total = 0

    for relative in visible:
        body, truncated_at = _read_body(root / relative, relative, max_file_chars)
        block = f"=== {relative} ===\n{body}"
        if chunks and total + len(block) > max_total_chars:
            # No cabe: se declara omitido en vez de recortarlo en silencio.
            omitted.append(relative)
            continue
        total += len(block)
        chunks.append(block)
        line_counts.append((relative, _visible_line_count(body)))
        if truncated_at is not None:
            truncated.append(relative)

    return ModelReviewContext(
        visible_paths=tuple(path for path, _ in line_counts),
        omitted_paths=tuple(omitted),
        truncated_paths=tuple(truncated),
        content="\n\n".join(chunks),
        line_counts=tuple(line_counts),
    )


def missing_paths(context: ModelReviewContext, required: Iterable[str]) -> tuple[str, ...]:
    """Rutas declaradas como obligatorias que **no** llegaron al modelo.

    Es la comprobación que impide aprobar una revisión parcial como si fuera completa.
    """
    visible = context.visible_set
    missing: list[str] = []
    for raw in _iter_paths(required):
        try:
            relative = normalize_relative_path(raw)
        except ValueError:
            continue
        if relative not in visible and relative not in missing:
            missing.append(relative)
    return tuple(missing)


def _iter_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Normaliza la entrada a una tupla de cadenas, sin aceptar un único ``str`` suelto."""
    if isinstance(paths, str):
        return (paths,)
    return tuple(str(item) for item in paths)


def _read_body(path: Path, relative: str, max_file_chars: int) -> tuple[str, int | None]:
    """Contenido legible de un archivo, con su recorte declarado si lo hay."""
    if not path.is_file():
        return MISSING_FILE_MARKER, None
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - depende del sistema de archivos
        return UNREADABLE_FILE_MARKER, None
    if len(content) > max_file_chars:
        return (
            f"{content[:max_file_chars]}\n…[recortado de {len(content)} caracteres]",
            max_file_chars,
        )
    return content, None


def _visible_line_count(body: str) -> int:
    """Líneas visibles, con la misma convención que el resto del motor.

    Un archivo que no existe o no se pudo leer cuenta cero: el modelo no vio ninguna línea,
    así que ninguna línea puede citarse como evidencia.
    """
    if body in (MISSING_FILE_MARKER, UNREADABLE_FILE_MARKER):
        return 0
    return body.count("\n") + 1


__all__ = [
    "BLOCKED_CONTEXT_LIMIT",
    "MAX_CONTEXT_FILE_CHARS",
    "MAX_MODEL_CONTEXT_CHARS",
    "MAX_MODEL_VISIBLE_PATHS",
    "MISSING_FILE_MARKER",
    "UNREADABLE_FILE_MARKER",
    "ModelReviewContext",
    "build_model_review_context",
    "missing_paths",
]
