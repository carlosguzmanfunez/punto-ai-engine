"""Auditoría cruzada entre proveedores (ENGINE-5.2).

Este paquete contiene el rol que audita con un **proveedor distinto** al que construyó y
evaluó el trabajo. No es un Reviewer con otro nombre: el Reviewer comprueba coherencia con el
mismo proveedor que ya participó; aquí lo que aporta valor es la mirada independiente.

Los módulos:

- ``base``: el contrato ``CrossAuditRunner`` y su presupuesto;
- ``prompts``: el prompt versionado de la auditoría;
- ``validation``: qué propuesta es utilizable y qué hallazgo es una invención;
- ``gates``: las reglas que el modelo no puede anular;
- ``claude``: la implementación real sobre Anthropic/Claude.
"""

__all__: list[str] = []
