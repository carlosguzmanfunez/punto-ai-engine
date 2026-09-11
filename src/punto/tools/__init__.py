"""Tool Layer: herramientas de ejecución confinadas (ENGINE-1).

``__init__`` deliberadamente ligero: **no** reexporta nada. Cada herramienta se
importa desde su módulo concreto::

    from punto.tools.filesystem import FilesystemTool
    from punto.tools.git import GitWorkspace
    from punto.tools.shell import ShellRunner
    from punto.tools.validator import Validator

Motivo: ``punto.tools.errors`` es un módulo hoja que necesita
``punto.developer.context``, y las herramientas importan el contexto. Reexportar
las herramientas aquí volvería a cargarlas al importar ``punto.tools.errors`` y
cerraría un ciclo de importación en frío — exactamente el defecto que corrigió
ENGINE-0.R3.

Las herramientas no deciden autoridad: aplican los límites técnicos que les fija
el :class:`~punto.developer.context.ExecutionContext`. Quien decide si la acción
está permitida es el Policy Engine, a través de CAMUS.
"""
