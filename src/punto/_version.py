"""Identidad y versión del motor.

Se aísla en su propio módulo para que cualquier submódulo pueda importar la
versión sin provocar ciclos de importación a través de ``punto/__init__.py``.
"""

from typing import Final

ENGINE_NAME: Final[str] = "PUNTO AI ENGINE"
ENGINE_VERSION: Final[str] = "0.1.0"
ENGINE_PHASE: Final[str] = "ENGINE-0"

__all__ = ["ENGINE_NAME", "ENGINE_PHASE", "ENGINE_VERSION"]
