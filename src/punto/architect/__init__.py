"""Rol Architect (ENGINE-3).

El Architect decide **qué** sistema hay que construir y lo deja por escrito en
estructuras que PUNTO pueda validar: ``ProjectSpec``, ``ArchitecturePlan`` y
``ProjectCapabilityProfile``.

No escribe archivos del proyecto, no ejecuta nada y no decide autoridad. La
implementación inicial es ``DeepSeekArchitectRunner``; la interfaz
(:class:`~punto.architect.base.ArchitectRunner`) no conoce ningún proveedor.

Este paquete no re-exporta sus símbolos: importar ``punto.architect`` no debe
arrastrar el cliente HTTP ni la cadena del modelo.
"""

from __future__ import annotations

__all__: list[str] = []
