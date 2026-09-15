"""Paquete de la ejecución de proyectos por grafo de tareas (ENGINE-6.2).

Un **proyecto** es un ``TaskGraph`` durable que el ``ProjectExecutionKernel`` ejecuta nodo a nodo,
encadenando child workflows reales. Este paquete reúne las piezas deterministas de esa ejecución:
el grafo (validación, huella y elección del siguiente nodo) y el almacén durable del agregado
``ProjectRun``.

La separación es la misma que ya tiene el workflow de 6.0, y por el mismo motivo: ``punto.schemas``
declara **qué** se persiste, este paquete decide **qué se hace** con ello, y ``punto.project.store``
se limita a guardarlo y a detectar corrupción sin repararla en silencio. Nada de aquí importa a los
roles (Planner, Developer, QA): son ellos los que dependen de estos contratos, nunca al revés.
"""

from __future__ import annotations

__all__: list[str] = []
