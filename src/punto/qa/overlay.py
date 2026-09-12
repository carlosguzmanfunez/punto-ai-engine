"""Workspace efímero de QA (ENGINE-4 §9).

QA evalúa el trabajo del Developer **sin contaminarlo**. La rama aprobada no se toca:
se copia el árbol candidato a un directorio desechable, se añaden allí las pruebas de
QA, se ejecuta el sandbox contra esa copia y se destruye.

    candidate workspace → overlay → pruebas QA → sandbox → evidencia → destruir

Consecuencias deliberadas:

- las pruebas generadas por QA son **efímeras**: no se commitean (§9);
- un fallo de QA no deja el repositorio del Developer en un estado intermedio;
- el overlay es siempre una ruta nueva: si algo falla, se descarta entero.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from punto.qa.paths import PathKind, classify_path, normalize_relative_path
from punto.schemas.qa import QATestFile

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Directorios que no se copian al overlay: no aportan nada a la ejecución de pruebas
#: y pueden ser enormes o contener estado del repositorio.
SKIPPED_DIRECTORIES: frozenset[str] = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache",
     ".pytest_cache", "dist", "build", ".tox", ".nox"}
)


class QAOverlayError(RuntimeError):
    """No se pudo preparar el workspace desechable de QA."""


@dataclass(slots=True)
class QAOverlay:
    """Copia desechable de un workspace candidato con las pruebas de QA aplicadas.

    Se usa como *context manager*: al salir se destruye siempre, incluso si la
    ejecución falla.
    """

    source: Path
    destination: Path
    written: tuple[str, ...] = ()
    _prepared: bool = field(default=False, repr=False)

    def __enter__(self) -> QAOverlay:
        """Copia el árbol candidato y deja el overlay listo."""
        self.prepare()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Destruye el overlay. Nunca deja restos."""
        self.destroy()

    # ------------------------------------------------------------------ ciclo
    def prepare(self) -> None:
        """Copia el workspace candidato al destino desechable.

        Raises:
            QAOverlayError: si el origen no es un directorio o el destino ya existe.
        """
        if self._prepared:
            return
        if not self.source.is_dir():
            raise QAOverlayError(f"el workspace candidato no es un directorio: {self.source}")
        if self.destination.exists():
            raise QAOverlayError(f"el destino del overlay ya existe: {self.destination}")
        try:
            shutil.copytree(
                self.source,
                self.destination,
                ignore=shutil.ignore_patterns(*SKIPPED_DIRECTORIES),
                symlinks=False,
            )
        except OSError as exc:  # pragma: no cover - depende del sistema de archivos
            raise QAOverlayError(f"no se pudo copiar el workspace candidato: {exc}") from exc
        self._prepared = True

    def destroy(self) -> None:
        """Elimina el overlay completo, sin tocar el origen."""
        if self.destination.exists():
            shutil.rmtree(self.destination, ignore_errors=True)
        self._prepared = False

    # ---------------------------------------------------------------- escritura
    def apply(
        self, files: tuple[QATestFile, ...], *, test_only_paths: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        """Escribe los archivos de prueba de QA dentro del overlay.

        Se llama **después** de validar el plan completo (atomicidad, §13): si algo no
        es escribible, no se escribe nada.

        Returns:
            Las rutas escritas, en orden.

        Raises:
            QAOverlayError: si el overlay no está preparado, si una ruta no es de
                pruebas o si el archivo ya existe en el candidato.
        """
        if not self._prepared:
            raise QAOverlayError("el overlay no está preparado")
        self.assert_writable(files, test_only_paths=test_only_paths)

        written: list[str] = []
        try:
            for test_file in files:
                relative = normalize_relative_path(test_file.path)
                target = self.destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(test_file.content, encoding="utf-8")
                written.append(relative)
        except OSError as exc:  # pragma: no cover - depende del sistema de archivos
            raise QAOverlayError(f"no se pudieron escribir las pruebas de QA: {exc}") from exc

        self.written = tuple(written)
        return self.written

    def assert_writable(
        self, files: tuple[QATestFile, ...], *, test_only_paths: tuple[str, ...] = ()
    ) -> None:
        """Comprueba que QA puede escribir todos los archivos, sin escribir ninguno.

        Raises:
            QAOverlayError: si alguna ruta no es de pruebas, ya existe o no es segura.
        """
        for test_file in files:
            kind = classify_path(test_file.path, test_only_paths=test_only_paths)
            if kind is not PathKind.TEST_ONLY:
                raise QAOverlayError(
                    f"QA no puede escribir {test_file.path!r} (clasificada como {kind.value})"
                )
            relative = normalize_relative_path(test_file.path)
            if (self.destination / relative).exists():
                raise QAOverlayError(
                    f"QA no sobrescribe archivos existentes: {relative!r} ya existe en el "
                    "workspace candidato"
                )

    # ------------------------------------------------------------------ lectura
    def list_files(self) -> tuple[str, ...]:
        """Rutas relativas del overlay, en orden determinista."""
        if not self._prepared:
            return ()
        return tuple(
            sorted(
                path.relative_to(self.destination).as_posix()
                for path in self.destination.rglob("*")
                if path.is_file()
            )
        )

    def read(self, relative: str) -> str:
        """Contenido de un archivo del overlay."""
        return (self.destination / normalize_relative_path(relative)).read_text(encoding="utf-8")


def iter_test_paths(files: tuple[QATestFile, ...]) -> Iterator[str]:
    """Rutas normalizadas de los archivos de prueba, en orden."""
    for test_file in files:
        yield normalize_relative_path(test_file.path)


__all__ = [
    "SKIPPED_DIRECTORIES",
    "QAOverlay",
    "QAOverlayError",
    "iter_test_paths",
]
