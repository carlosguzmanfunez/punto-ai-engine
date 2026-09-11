"""PUNTO AI ENGINE - Núcleo constitucional determinista (Base Constitucional V0.1).

Este paquete implementa el bootstrap constitucional de ENGINE-0: autoridad,
policy engine, risk engine, human gate, máquina de estados, task manager en
memoria, audit log en memoria, CAMUS determinista y una API FastAPI mínima.

Restricciones de fase (ENGINE-0): sin IA, sin integraciones externas, sin
persistencia externa. Todo el comportamiento es local y determinista.
"""

from punto._version import ENGINE_NAME, ENGINE_VERSION

__all__ = ["ENGINE_NAME", "ENGINE_VERSION", "__version__"]

__version__ = ENGINE_VERSION
