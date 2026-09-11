"""Developer Execution Layer (ENGINE-1).

Frontera explícita de ejecución: CAMUS **no** ejecuta directamente ``subprocess``,
ni escrituras de archivos, ni Git, ni shell. Delega en un
:class:`~punto.developer.base.DeveloperRunner`::

    CAMUS
      -> DeveloperRunner
      -> ExecutionContext
      -> Tool Layer (Filesystem | Shell | Git | Validator)

El ``DeveloperRunner`` es **provider-agnostic**: no conoce ningún proveedor de
modelo. ENGINE-1 solo incluye
:class:`~punto.developer.local.LocalDeveloperRunner`, un ejecutor determinista
que aplica recetas declaradas y **no** genera código con IA.

ENGINE-2 añadirá ``DeepSeekDeveloperRunner`` sobre esta misma interfaz, sin tocar
el núcleo.

``__init__`` deliberadamente ligero: **no** reexporta nada. Cada módulo se importa
desde su ruta concreta::

    from punto.developer.base import DeveloperRunner
    from punto.developer.context import ExecutionContext
    from punto.developer.local import LocalDeveloperRunner
"""
