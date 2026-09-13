"""Frontera de contexto del modelo (ENGINE-5.1 y ENGINE-5.1.1).

El motor solo puede aceptar un hallazgo del modelo sobre un archivo cuyo contenido el
modelo **recibió**. Ese invariante no puede depender de que un validador y un constructor de
prompt coincidan por casualidad: necesita una única fuente determinista.

Este módulo la construye. Dada una lista de rutas autorizadas y un workspace, devuelve
exactamente:

- ``visible_paths``: las rutas cuyo contenido se envió al modelo, en orden;
- ``content``: el texto que el modelo recibió, tal cual;
- ``omitted_paths``: las rutas que **no** se enviaron, declaradas y no silenciadas;
- ``unsafe_paths``: las excluidas porque su destino real **escapa** del workspace;
- ``truncated_paths``: las rutas cuyo contenido se envió recortado;
- ``line_counts``: cuántas líneas de cada ruta visible pudo ver el modelo.

Regla de la frontera, sin excepciones: **una allowlist vacía no autoriza nada**. Si no hay
contexto visible, ningún hallazgo del modelo puede señalar un archivo. Y nada se omite en
silencio: lo que no cabe se declara para que quien decide pueda bloquear.

Frontera de lectura (ENGINE-5.1.1): la comprobación de ruta es **léxica y de destino**. Una
ruta relativa sin ``..`` puede seguir siendo un enlace que sale del workspace, así que antes
de leer nada se resuelve el destino real —siguiendo symlinks y junctions— y se exige que
siga dentro de la raíz resuelta. Un enlace interno se acepta; uno que escape **no se lee
nunca**, y ni su contenido ni su destino real llegan al prompt ni a la evidencia.
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
    unsafe_paths: tuple[str, ...] = ()
    invalid_paths: tuple[str, ...] = ()
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
        """Descripción legible de lo omitido, para evidencia y auditoría.

        Solo nombra rutas **declaradas** por la tarea. El destino real de un enlace que
        escapa no se escribe nunca: sería filtrar la disposición del sistema de archivos.
        """
        if not self.omitted_paths:
            return ""
        listed = ", ".join(self.omitted_paths[:5])
        suffix = "…" if len(self.omitted_paths) > 5 else ""
        detail = (
            f"contexto incompleto: {len(self.omitted_paths)} archivo(s) declarado(s) no "
            f"enviado(s) al modelo ({listed}{suffix}); el límite es "
            f"{MAX_MODEL_VISIBLE_PATHS} archivo(s)"
        )
        if self.unsafe_paths:
            unsafe = ", ".join(self.unsafe_paths[:5])
            unsafe_suffix = "…" if len(self.unsafe_paths) > 5 else ""
            detail = (
                f"{detail}; {len(self.unsafe_paths)} de ellos resuelven fuera del "
                f"workspace y fueron rechazados sin leerlos ({unsafe}{unsafe_suffix})"
            )
        if self.invalid_paths:
            invalid = ", ".join(self.invalid_paths[:5])
            invalid_suffix = "…" if len(self.invalid_paths) > 5 else ""
            detail = (
                f"{detail}; {len(self.invalid_paths)} no son rutas válidas y quedan "
                f"declaradas como inválidas ({invalid}{invalid_suffix})"
            )
        return detail

    def annotated_content(self) -> str:
        """Contenido visible, con las omisiones declaradas dentro del propio contexto.

        El modelo nunca debe creer que vio todo el proyecto cuando no fue así, ni que un
        enlace que sale del workspace es material revisable.
        """
        if not self.omitted_paths:
            return self.content or "(no se declararon archivos)"
        notes = [
            f"contexto parcial: {len(self.omitted_paths)} archivo(s) declarado(s) no "
            f"incluido(s) aquí: {', '.join(self.omitted_paths[:5])}"
            f"{'…' if len(self.omitted_paths) > 5 else ''}"
        ]
        if self.unsafe_paths:
            notes.append(
                f"excluido(s) por resolver fuera del workspace: "
                f"{', '.join(self.unsafe_paths[:5])}"
                f"{'…' if len(self.unsafe_paths) > 5 else ''}"
            )
        if self.invalid_paths:
            notes.append(
                f"declarado(s) como inválido(s): "
                f"{', '.join(self.invalid_paths[:5])}"
                f"{'…' if len(self.invalid_paths) > 5 else ''}"
            )
        body = self.content or "(no se declararon archivos)"
        return f"{body}\n\n[{'; '.join(notes)}]"


def build_model_review_context(
    workspace: Path | str,
    paths: Iterable[str],
    *,
    max_paths: int = MAX_MODEL_VISIBLE_PATHS,
    max_file_chars: int = MAX_CONTEXT_FILE_CHARS,
    max_total_chars: int = MAX_MODEL_CONTEXT_CHARS,
) -> ModelReviewContext:
    """Construye el contexto visible al modelo, de forma pura y determinista.

    Las rutas repetidas se deduplican conservando el orden. Todo lo que no entre en el
    presupuesto queda en ``omitted_paths``: **nunca** se omite en silencio, y quien llama decide
    si eso basta para bloquear la revisión.

    Una ruta **inválida** (carácter de control, traversal, absoluta) no se descarta sin dejar
    rastro: queda declarada en ``invalid_paths`` y en ``omitted_paths``, con una representación
    saneada —nunca con los caracteres de control crudos—. Así una ruta obligatoria inválida
    sigue contando como no cubierta y quien decide puede bloquear.

    Antes de leer cada archivo se comprueba que su destino **real** siga dentro del
    workspace (ENGINE-5.1.1). Un enlace que escape no se lee: queda en ``omitted_paths`` y en
    ``unsafe_paths``, sin que su contenido ni su destino real lleguen a ningún sitio.

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
    resolved_root = _resolved_root(root)
    ordered: list[str] = []
    invalid: list[str] = []
    for raw in _iter_paths(paths):
        try:
            relative = normalize_relative_path(raw)
        except ValueError:
            label = safe_path_label(raw)
            if label not in invalid:
                invalid.append(label)
            continue
        if relative not in ordered:
            ordered.append(relative)

    visible = ordered[:max_paths]
    omitted: list[str] = [*ordered[max_paths:], *invalid]
    unsafe: list[str] = []

    chunks: list[str] = []
    line_counts: list[tuple[str, int]] = []
    truncated: list[str] = []
    total = 0

    for relative in visible:
        contained = _contained(root, resolved_root, relative)
        if contained is None:
            # El enlace sale del workspace: no se lee, no se envía, se declara.
            omitted.append(relative)
            unsafe.append(relative)
            continue
        body, truncated_at = _read_body(contained, max_file_chars)
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
        unsafe_paths=tuple(unsafe),
        invalid_paths=tuple(invalid),
        truncated_paths=tuple(truncated),
        content="\n\n".join(chunks),
        line_counts=tuple(line_counts),
    )


def resolve_within_workspace(workspace: Path | str, relative: str) -> Path | None:
    """Resuelve ``relative`` y devuelve su ruta absoluta **solo** si sigue dentro del workspace.

    La comprobación se hace sobre el destino real, siguiendo symlinks y junctions: una ruta
    relativa sin ``..`` puede apuntar fuera igualmente. Un enlace interno se acepta; uno que
    escape devuelve ``None``.

    Esta es la regla que usa el contexto del modelo, en un solo sitio, para que Security y
    Reviewer no tengan que decidirla por su cuenta.

    Args:
        workspace: Raíz del workspace.
        relative: Ruta relativa ya declarada por la tarea.

    Returns:
        La ruta absoluta contenida, o ``None`` si escapa, no es resoluble o no es válida.
    """
    try:
        normalized = normalize_relative_path(relative)
    except ValueError:
        return None
    root = Path(workspace)
    return _contained(root, _resolved_root(root), normalized)


def missing_paths(context: ModelReviewContext, required: Iterable[str]) -> tuple[str, ...]:
    """Rutas declaradas como obligatorias que **no** llegaron al modelo.

    Es la comprobación que impide aprobar una revisión parcial como si fuera completa.

    Una ruta obligatoria **inválida** también falta: no puede estar en el contexto visible, así
    que cuenta como no cubierta y se devuelve con una representación saneada. Desaparecer de
    las dos capas (ni visible ni faltante) sería justamente el agujero que esto cierra.
    """
    visible = context.visible_set
    missing: list[str] = []
    for raw in _iter_paths(required):
        try:
            relative = normalize_relative_path(raw)
        except ValueError:
            label = safe_path_label(raw)
            if label not in missing:
                missing.append(label)
            continue
        if relative not in visible and relative not in missing:
            missing.append(relative)
    return tuple(missing)


def safe_path_label(path: str) -> str:
    """Representación segura de una ruta para informes, evidencia y registros.

    Los caracteres de control se escapan (``\\x00``) en lugar de escribirse crudos: un informe
    no debe poder inyectar un salto de línea, un NUL ni una secuencia de terminal en un log. La
    ruta se recorta además a un tamaño razonable.
    """
    escaped = "".join(
        character
        if character >= " " and character != "\x7f"
        else f"\\x{ord(character):02x}"
        for character in path
    )
    return escaped[:200]


def _iter_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Normaliza la entrada a una tupla de cadenas, sin aceptar un único ``str`` suelto."""
    if isinstance(paths, str):
        return (paths,)
    return tuple(str(item) for item in paths)


def _resolved_root(root: Path) -> Path:
    """Raíz del workspace con los enlaces ya seguidos.

    Se resuelve **una** vez y se compara contra ella: si el propio workspace se declaró por un
    enlace, la frontera es su destino real, no su nombre.
    """
    try:
        return root.resolve()
    except (OSError, ValueError):  # pragma: no cover - depende del sistema de archivos
        return root


def _contained(root: Path, resolved_root: Path, relative: str) -> Path | None:
    """Ruta absoluta del archivo, o ``None`` si su destino real escapa del workspace.

    Sigue symlinks y junctions antes de decidir. Nunca devuelve una ruta cuya lectura
    pudiera salir del proyecto, y nunca revela el destino que rechazó.

    ``Path.resolve()`` puede fallar con ``OSError`` (enlace roto, error del sistema de archivos)
    y con ``ValueError`` (una ruta con caracteres inválidos, como un NUL incrustado). Las dos
    cosas significan «no contenida», no un crash: la frontera decide, no se cae.
    """
    try:
        resolved = (root / relative).resolve()
    except (OSError, ValueError):
        return None
    if not resolved.is_relative_to(resolved_root):
        return None
    return resolved


def _read_body(path: Path, max_file_chars: int) -> tuple[str, int | None]:
    """Contenido legible de un archivo ya contenido, con su recorte declarado si lo hay."""
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
    "resolve_within_workspace",
    "safe_path_label",
]
