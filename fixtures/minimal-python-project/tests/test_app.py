"""Pruebas del fixture mínimo."""

from __future__ import annotations

from app import greet


def test_greet() -> None:
    """El saludo tiene el formato esperado."""
    assert greet("PUNTO") == "Hello, PUNTO!"


def test_greet_uses_the_name() -> None:
    """El nombre forma parte del saludo."""
    assert "Carlos" in greet("Carlos")
