"""Kernel de workflow autónomo (ENGINE-6.0).

``__init__`` deliberadamente ligero: **no** reexporta nada. Cada consumidor importa el
módulo concreto::

    from punto.workflow.budgets import check_budget
    from punto.workflow.errors import WorkflowError
    from punto.workflow.providers import default_capabilities

Motivo: los módulos del kernel se organizan por responsabilidad —errores, presupuesto,
capacidades de proveedor y, más adelante, máquina de estados y motor— y muchos de ellos se
importan entre sí en un solo sentido. Reexportar aquí obligaría a cargar el paquete entero
para usar una sola función y volvería fácil crear un ciclo de importación entre el motor y
los esquemas, que es exactamente el defecto que este proyecto ya corrigió en fases previas.

Regla que gobierna todo lo que vive aquí: **PUNTO decide, el modelo propone**. Ninguna
función de este paquete escribe estado a partir de texto generado por un modelo; los
límites, las transiciones y las capacidades están declarados y se calculan.
"""

__all__: list[str] = []
