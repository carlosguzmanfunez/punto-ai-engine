"""Capa de planificación (ENGINE-3): invariantes deterministas.

Aquí vive lo que **no** decide un modelo: la validación estructural del grafo de
tareas, la comprobación de invariantes del plan y la detección de capacidades que
PUNTO todavía no puede ejecutar.

Separación de responsabilidades de la fase:

- ``punto.schemas.planning``: contratos de datos (qué forma tiene un plan).
- ``punto.planning``: reglas deterministas (qué plan es aceptable).
- ``punto.architect`` y ``punto.planner``: los roles que **proponen** planes.
- ``punto.orchestrator.camus``: quien decide y responde.

Este paquete no importa nada de ``punto.architect`` ni de ``punto.planner``: son
ellos los que dependen de estas reglas, nunca al revés.
"""

from __future__ import annotations

__all__: list[str] = []
