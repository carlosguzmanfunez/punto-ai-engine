"""Rol Planner (ENGINE-3).

El Planner decide **cómo** se divide un sistema ya diseñado en trabajo ejecutable:
milestones, epics y tareas pequeñas, con criterios de aceptación verificables y
dependencias en forma de DAG.

No elige arquitectura, no escribe código, no ejecuta nada y no decide autoridad. La
implementación inicial es ``DeepSeekPlannerRunner``; la interfaz
(:class:`~punto.planner.base.PlannerRunner`) no conoce ningún proveedor.

No debe confundirse con :mod:`punto.orchestrator.planner`, que es el planificador
**determinista de pasos** de ENGINE-0 (sin IA) y sigue existiendo sin cambios.

Este paquete no re-exporta sus símbolos: importar ``punto.planner`` no debe arrastrar
el cliente HTTP ni la cadena del modelo.
"""

from __future__ import annotations

__all__: list[str] = []
