"""Proveedores de modelos externos (ENGINE-2).

``__init__`` deliberadamente ligero: **no** reexporta nada. Cada proveedor se
importa desde su módulo concreto::

    from punto.providers.deepseek import DeepSeekClient

Regla de frontera: un proveedor **solo** habla con su API. No toca el sistema de
archivos, ni el shell, ni Git, ni decide autoridad. Devuelve datos estructurados
que PUNTO valida y aplica.
"""
