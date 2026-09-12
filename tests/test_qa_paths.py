"""Pruebas del guard de rutas de QA (ENGINE-4 §8).

QA genera pruebas, no código de producción. La frontera se decide por ruta, de forma
determinista, y en caso de duda la ruta es de producción.
"""

from __future__ import annotations

import pytest

from punto.qa.paths import (
    PathKind,
    classify_path,
    is_test_only_path,
    normalize_relative_path,
)


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_x.py",
        "tests/integration/test_y.py",
        "test/z.py",
        "__tests__/component.test.ts",
        "spec/thing.spec.ts",
        "src/component.test.ts",
        "src/component.spec.js",
        "conftest.py",
        "tests/test_algo_qa.py",
        "qa/checks.py",
    ],
)
def test_test_paths_are_recognized(path: str) -> None:
    """Las rutas de pruebas se aceptan, en los ecosistemas soportados."""
    assert classify_path(path) is PathKind.TEST_ONLY
    assert is_test_only_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "src/algo.py",
        "app/main.py",
        "lib/util.ts",
        "pkg/service.js",
        "src/tests_helpers/x.py",
        "algo.py",
        "scripts/deploy.sh",
    ],
)
def test_production_paths_are_rejected(path: str) -> None:
    """Cualquier ruta que no sea claramente de pruebas se trata como producción."""
    assert classify_path(path) is PathKind.PRODUCTION
    assert not is_test_only_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "config/constitution.yaml",
        "config/permissions.yaml",
        "constitution.yaml",
    ],
)
def test_protected_paths_are_rejected(path: str) -> None:
    """Los archivos constitucionales están fuera del alcance de QA."""
    assert classify_path(path) is PathKind.PROTECTED


@pytest.mark.parametrize(
    "path",
    [
        "../fuera.py",
        "../../etc/passwd",
        "/etc/passwd",
        "C:/Windows/system32/x.py",
        "~/test_x.py",
        "tests/../../x.py",
        "",
        "   ",
        ".git/config",
        "tests/.git/config",
        "node_modules/x/test_y.py",
        ".venv/lib/test_z.py",
        "tests/.env",
        "tests/id_rsa",
    ],
)
def test_unsafe_paths_are_invalid(path: str) -> None:
    """Traversal, rutas absolutas, metadatos y secretos quedan fuera."""
    assert classify_path(path) is PathKind.INVALID


def test_traversal_detection_is_not_fooled_by_separators() -> None:
    """Una ruta con separadores mezclados tampoco escapa."""
    assert classify_path("tests\\..\\..\\evil.py") is PathKind.INVALID


def test_declared_test_only_paths_extend_the_allowlist() -> None:
    """El proyecto puede declarar explícitamente una ruta como solo-de-pruebas."""
    assert classify_path("checks/verify_algo.py") is PathKind.PRODUCTION
    assert (
        classify_path("checks/verify_algo.py", test_only_paths=("checks/",))
        is PathKind.TEST_ONLY
    )


def test_declared_test_only_path_cannot_override_a_protected_file() -> None:
    """Una declaración del proyecto no puede saltarse la protección constitucional."""
    assert (
        classify_path("config/constitution.yaml", test_only_paths=("config/",))
        is PathKind.PROTECTED
    )


def test_declared_test_only_path_cannot_override_an_invalid_path() -> None:
    """Ni abrir un traversal: la declaración amplía, no desactiva los guards."""
    assert classify_path("../evil.py", test_only_paths=("../",)) is PathKind.INVALID


def test_normalize_relative_path_returns_posix() -> None:
    """La normalización es determinista y en formato posix."""
    assert normalize_relative_path("tests\\sub\\test_x.py") == "tests/sub/test_x.py"
    assert normalize_relative_path("./tests/./test_x.py") == "tests/test_x.py"


@pytest.mark.parametrize("path", ["/abs.py", "C:/x.py", "../x.py", "", "~/"])
def test_normalize_rejects_unsafe_paths(path: str) -> None:
    """La normalización falla cerrada ante rutas inseguras."""
    with pytest.raises(ValueError):
        normalize_relative_path(path)
