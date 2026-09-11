"""Proyecto Python mínimo usado como fixture de ENGINE-1.

No forma parte de PUNTO AI ENGINE: es el *target* sobre el que la capa de
ejecución demuestra que puede trabajar. El fixture fuente nunca se modifica; los
tests y las demos copian este directorio a un workspace temporal.
"""

from __future__ import annotations


def greet(name: str) -> str:
    """Devuelve un saludo simple."""
    return f"Hello, {name}!"


def main() -> None:
    """Punto de entrada del fixture."""
    print(greet("PUNTO"))


if __name__ == "__main__":
    main()
