"""Rol QA independiente (ENGINE-4).

QA evalúa el trabajo del Developer contra los criterios de aceptación con evidencia
ejecutada, no con opinión. El principio que gobierna el paquete:

    Developer ≠ QA

Quien escribe el código no puede ser la autoridad que decide que ese código cumple el
contrato. Por eso aquí:

- el resultado del Developer es **contexto**, nunca prueba;
- QA solo puede añadir archivos de prueba, en rutas de pruebas, sobre un overlay
  desechable;
- todo código de QA es ``UNTRUSTED_MODEL`` y se ejecuta en un sandbox verificado;
- el estado final lo calcula PUNTO a partir de la evidencia; el modelo no puede
  escribirlo.

Este paquete no re-exporta sus símbolos: importar ``punto.qa`` no debe arrastrar
``httpx`` ni la implementación de DeepSeek.
"""

from __future__ import annotations

__all__: list[str] = []
