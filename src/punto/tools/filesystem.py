"""Filesystem Tool: operaciones de archivo confinadas al workspace.

Todas las operaciones pasan por :class:`~punto.developer.context.ExecutionContext`,
que resuelve la ruta y verifica que quede dentro del workspace. Además, cada
escritura:

- rechaza los archivos constitucionalmente protegidos;
- rechaza la escritura de código fuera de una rama de tarea (``ai/...``);
- rechaza escribir dentro de ``.git``, que gestiona ``GitWorkspace``;
- verifica el contenido releyendo el archivo (evidencia, no confianza).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from punto.schemas.execution import FileChange, FileOperation
from punto.tools.errors import ProtectedFileError

if TYPE_CHECKING:
    from punto.developer.context import ExecutionContext

#: Componente cuyo contenido no se escribe nunca desde el tool de archivos.
GIT_DIRECTORY_NAME = ".git"


class FilesystemTool:
    """Acceso a archivos confinado a un workspace autorizado."""

    def __init__(self, context: ExecutionContext) -> None:
        self._context = context

    # ------------------------------------------------------------------ lectura
    @property
    def context(self) -> ExecutionContext:
        """Contexto que delimita el acceso."""
        return self._context

    def read_text(self, path: str) -> str:
        """Lee un archivo de texto del workspace.

        Raises:
            WorkspaceViolationError: si la ruta queda fuera del workspace.
            FileNotFoundError: si el archivo no existe.
        """
        resolved = self._context.resolve_path(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"Archivo no encontrado en el workspace: {path}")
        return resolved.read_text(encoding="utf-8")

    def exists(self, path: str) -> bool:
        """True si la ruta existe y está dentro del workspace."""
        return self._context.resolve_path(path).exists()

    def list_files(self, pattern: str = "**/*") -> tuple[str, ...]:
        """Lista rutas relativas del workspace que casan con ``pattern``.

        El orden es determinista (lexicográfico). Se omiten las entradas que
        resuelven fuera del workspace (por ejemplo, enlaces que escapan) y todo
        lo que cuelga de ``.git``.
        """
        root = self._context.workspace_root
        found: list[str] = []
        for candidate in root.glob(pattern):
            try:
                resolved = candidate.resolve()
            except OSError:  # pragma: no cover - depende del sistema de archivos
                continue
            if not resolved.is_relative_to(root):
                continue
            relative = resolved.relative_to(root).as_posix()
            if relative.split("/", maxsplit=1)[0] == GIT_DIRECTORY_NAME:
                continue
            found.append(relative)
        return tuple(sorted(found))

    # ----------------------------------------------------------------- escritura
    def write_text(self, path: str, content: str, *, overwrite: bool = True) -> FileChange:
        """Escribe ``content`` en ``path`` y verifica el resultado.

        Args:
            path: Ruta relativa al workspace.
            content: Contenido exacto a escribir.
            overwrite: Si es ``False`` y el archivo ya existe, falla.

        Raises:
            WorkspaceViolationError: si la ruta queda fuera del workspace.
            ProtectedFileError: si la ruta es un archivo constitucional o ``.git``.
            BranchPolicyViolationError: si la rama declarada no es de tarea.
            FileExistsError: si ``overwrite`` es ``False`` y el archivo existe.
        """
        resolved = self._assert_writable(path)
        existed = resolved.is_file()
        if existed and not overwrite:
            raise FileExistsError(f"El archivo ya existe en el workspace: {path}")

        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")

        verified = self._verify(resolved, content)
        operation = FileOperation.MODIFIED if existed else FileOperation.CREATED
        return FileChange(
            path=self._context.relative_path(resolved),
            absolute_path=str(resolved),
            operation=operation,
            bytes_written=len(content.encode("utf-8")),
            verified=verified,
            detail="escritura verificada por relectura" if verified else "relectura no coincidente",
        )

    def create_file(self, path: str, content: str = "") -> FileChange:
        """Crea un archivo nuevo. Falla si ya existe."""
        return self.write_text(path, content, overwrite=False)

    def replace_text(self, path: str, old: str, new: str) -> FileChange:
        """Reemplaza ``old`` por ``new`` en un archivo existente.

        ``old`` debe aparecer **exactamente una vez**, de modo que el reemplazo
        sea determinista y no ambiguo.

        Raises:
            FileNotFoundError: si el archivo no existe.
            ValueError: si ``old`` no aparece o aparece más de una vez.
        """
        if not old:
            raise ValueError("El texto a reemplazar no puede estar vacío")

        current = self.read_text(path)
        occurrences = current.count(old)
        if occurrences == 0:
            raise ValueError(f"El texto a reemplazar no aparece en {path!r}")
        if occurrences > 1:
            raise ValueError(
                f"El texto a reemplazar aparece {occurrences} veces en {path!r}; "
                "el reemplazo debe ser inequívoco"
            )
        return self.write_text(path, current.replace(old, new, 1))

    def delete_file(self, path: str) -> FileChange:
        """Elimina un archivo del workspace.

        Raises:
            FileNotFoundError: si el archivo no existe.
            ProtectedFileError: si la ruta está protegida.
        """
        resolved = self._assert_writable(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"Archivo no encontrado en el workspace: {path}")
        size = resolved.stat().st_size
        resolved.unlink()
        return FileChange(
            path=self._context.relative_path(resolved),
            absolute_path=str(resolved),
            operation=FileOperation.DELETED,
            bytes_written=0,
            verified=not resolved.exists(),
            detail=f"archivo eliminado ({size} bytes)",
        )

    # ------------------------------------------------------------------ internos
    def _assert_writable(self, path: str) -> Path:
        """Valida rama, workspace, protección constitucional y ``.git``."""
        self._context.assert_code_writes_allowed()
        resolved = self._context.resolve_path(path)
        self._context.assert_not_protected(resolved)

        relative = self._context.relative_path(resolved)
        if relative.split("/", maxsplit=1)[0] == GIT_DIRECTORY_NAME:
            raise ProtectedFileError(
                relative, "el directorio .git lo gestiona GitWorkspace, no el tool de archivos"
            )
        return resolved

    @staticmethod
    def _verify(resolved: Path, expected: str) -> bool:
        """Relee el archivo para confirmar que el contenido quedó escrito."""
        try:
            return resolved.read_text(encoding="utf-8") == expected
        except OSError:  # pragma: no cover - depende del sistema de archivos
            return False


__all__ = ["GIT_DIRECTORY_NAME", "FilesystemTool"]
