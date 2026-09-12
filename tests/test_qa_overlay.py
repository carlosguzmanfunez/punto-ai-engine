"""Pruebas del workspace efímero de QA (ENGINE-4 §9).

El overlay existe para que QA pueda evaluar sin contaminar la rama aprobada del
Developer. Estas pruebas verifican que el aislamiento es real y que el overlay se
destruye siempre.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punto.qa.overlay import QAOverlay, QAOverlayError
from punto.schemas.qa import QATestFile
from qa_support import CORRECT_CLAMP, QA_TEST, build_clamp_project


def qa_file(identifier: str = "QF-1", path: str = "tests/test_qa.py") -> QATestFile:
    """Archivo de prueba con forma de salida del modelo."""
    return QATestFile(id=identifier, path=path, content=QA_TEST, test_case_ids=("QU-1",))


def test_overlay_copies_the_candidate_workspace(tmp_path: Path) -> None:
    """El overlay es una copia: contiene lo mismo que el candidato."""
    source = build_clamp_project(tmp_path, CORRECT_CLAMP)
    destination = tmp_path / "overlay" / "workspace"

    with QAOverlay(source=source, destination=destination) as overlay:
        assert (destination / "clamp_module.py").is_file()
        assert (destination / "tests" / "test_clamp_developer.py").is_file()
        assert "tests/test_clamp_developer.py" in overlay.list_files()


def test_overlay_does_not_touch_the_source(tmp_path: Path) -> None:
    """Escribir en el overlay no modifica ni un archivo del candidato."""
    source = build_clamp_project(tmp_path, CORRECT_CLAMP)
    before = {
        path.relative_to(source).as_posix(): path.read_text(encoding="utf-8")
        for path in source.rglob("*")
        if path.is_file()
    }
    destination = tmp_path / "overlay" / "workspace"

    with QAOverlay(source=source, destination=destination) as overlay:
        overlay.apply((qa_file(),))

    after = {
        path.relative_to(source).as_posix(): path.read_text(encoding="utf-8")
        for path in source.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (source / "tests" / "test_qa.py").exists()


def test_overlay_contains_the_qa_tests_while_alive(tmp_path: Path) -> None:
    """Las pruebas de QA viven en el overlay, no en el proyecto."""
    source = build_clamp_project(tmp_path, CORRECT_CLAMP)
    destination = tmp_path / "overlay" / "workspace"

    with QAOverlay(source=source, destination=destination) as overlay:
        written = overlay.apply((qa_file(),))

        assert written == ("tests/test_qa.py",)
        assert "test_qu_1_below_lower_returns_lower" in overlay.read("tests/test_qa.py")

    assert not destination.exists()


def test_overlay_is_destroyed_even_on_failure(tmp_path: Path) -> None:
    """El overlay se destruye aunque la ejecución falle: no deja restos."""
    source = build_clamp_project(tmp_path, CORRECT_CLAMP)
    destination = tmp_path / "overlay" / "workspace"

    with pytest.raises(RuntimeError), QAOverlay(source=source, destination=destination):
        raise RuntimeError("fallo simulado")

    assert not destination.exists()


def test_destroy_can_be_called_twice(tmp_path: Path) -> None:
    """Destruir dos veces no falla: la limpieza es idempotente."""
    source = build_clamp_project(tmp_path, CORRECT_CLAMP)
    destination = tmp_path / "overlay" / "workspace"
    overlay = QAOverlay(source=source, destination=destination)

    overlay.prepare()
    overlay.destroy()
    overlay.destroy()

    assert not destination.exists()


@pytest.mark.parametrize(
    "path",
    ["src/algo.py", "app/main.py", "../evil.py", "/etc/passwd", ".git/config"],
)
def test_overlay_refuses_non_test_paths(tmp_path: Path, path: str) -> None:
    """El overlay no escribe fuera de la zona de pruebas, ni con traversal."""
    source = build_clamp_project(tmp_path, CORRECT_CLAMP)
    destination = tmp_path / "overlay" / "workspace"

    with (
        QAOverlay(source=source, destination=destination) as overlay,
        pytest.raises(QAOverlayError, match="no puede escribir"),
    ):
        overlay.apply((qa_file(path=path),))
        assert overlay.list_files() == (
            "clamp_module.py",
            "pyproject.toml",
            "tests/test_clamp_developer.py",
        )


def test_overlay_refuses_protected_files(tmp_path: Path) -> None:
    """La configuración constitucional nunca se escribe desde QA."""
    source = build_clamp_project(tmp_path, CORRECT_CLAMP)
    (source / "config").mkdir()
    (source / "config" / "constitution.yaml").write_text("x: 1\n", encoding="utf-8")
    destination = tmp_path / "overlay" / "workspace"

    with (
        QAOverlay(source=source, destination=destination) as overlay,
        pytest.raises(QAOverlayError),
    ):
        overlay.apply((qa_file(path="config/constitution.yaml"),))


def test_overlay_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    """Sobrescribir destruiría la evidencia del Developer: está prohibido."""
    source = build_clamp_project(tmp_path, CORRECT_CLAMP)
    destination = tmp_path / "overlay" / "workspace"

    with (
        QAOverlay(source=source, destination=destination) as overlay,
        pytest.raises(QAOverlayError, match="no sobrescribe"),
    ):
        overlay.apply((qa_file(path="tests/test_clamp_developer.py"),))


def test_apply_is_atomic(tmp_path: Path) -> None:
    """§13: si un archivo del plan no es escribible, no se escribe ninguno."""
    source = build_clamp_project(tmp_path, CORRECT_CLAMP)
    destination = tmp_path / "overlay" / "workspace"
    files = (
        qa_file("QF-1", "tests/test_qa_one.py"),
        qa_file("QF-2", "src/production.py"),
    )

    with QAOverlay(source=source, destination=destination) as overlay:
        with pytest.raises(QAOverlayError):
            overlay.apply(files)
        assert not (destination / "tests" / "test_qa_one.py").exists()


def test_overlay_skips_heavy_and_vcs_directories(tmp_path: Path) -> None:
    """No se copian metadatos ni dependencias: no aportan nada a la ejecución."""
    source = build_clamp_project(tmp_path, CORRECT_CLAMP)
    (source / ".git").mkdir(exist_ok=True)
    (source / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "x.js").write_text("// x\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "x.pyc").write_text("x", encoding="utf-8")
    destination = tmp_path / "overlay" / "workspace"

    with QAOverlay(source=source, destination=destination) as overlay:
        files = overlay.list_files()

    assert not any(item.startswith(".git/") for item in files)
    assert not any(item.startswith("node_modules/") for item in files)
    assert not any(item.startswith("__pycache__/") for item in files)


def test_prepare_requires_an_existing_source(tmp_path: Path) -> None:
    """Un candidato inexistente es un error explícito, no un overlay vacío."""
    overlay = QAOverlay(source=tmp_path / "nope", destination=tmp_path / "out")

    with pytest.raises(QAOverlayError, match="no es un directorio"):
        overlay.prepare()


def test_apply_requires_a_prepared_overlay(tmp_path: Path) -> None:
    """Escribir antes de preparar es un error explícito."""
    overlay = QAOverlay(source=tmp_path, destination=tmp_path / "out")

    with pytest.raises(QAOverlayError, match="no está preparado"):
        overlay.apply((qa_file(),))


def test_declared_test_only_paths_are_honored(tmp_path: Path) -> None:
    """Una raíz declarada por el proyecto habilita escribir ahí."""
    source = build_clamp_project(tmp_path, CORRECT_CLAMP)
    destination = tmp_path / "overlay" / "workspace"

    with QAOverlay(source=source, destination=destination) as overlay:
        written = overlay.apply(
            (qa_file(path="checks/verify_clamp.py"),), test_only_paths=("checks/",)
        )

    assert written == ("checks/verify_clamp.py",)
