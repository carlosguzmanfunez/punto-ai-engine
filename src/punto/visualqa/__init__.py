"""Visual QA: el rol que mira la interfaz (ENGINE-5.3).

A diferencia de QA, Security o Reviewer, este rol no lee código: mira **capturas**. Sus piezas:

- ``base``: el contrato ``VisualQARunner`` y su presupuesto;
- ``gates``: las reglas que el modelo visual no puede anular;
- ``prompts``: el prompt versionado del revisor visual;
- ``validation``: qué propuesta es utilizable y qué hallazgo es una invención;
- ``claude``: la implementación real sobre Anthropic/Claude.

Los hechos técnicos (carga, consola, recursos, desbordamiento) los mide PUNTO en la capa web y
llegan aquí ya calculados: Visual QA **no** ejecuta nada y no puede contradecir una medición.
"""

__all__: list[str] = []
