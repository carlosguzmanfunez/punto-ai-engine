"""Rol Reviewer Agent (ENGINE-5).

El Reviewer evalua la calidad global del cambio y emite el unico veredicto de aprobacion
de la cadena. No ejecuta codigo: QA y Security ya lo hicieron.

Los gates que impiden aprobar un cambio (QA fallido, Security fallido o bloqueado) viven
en codigo determinista, no en el prompt: el modelo no puede anularlos.

Este paquete no re-exporta sus simbolos: importarlo no debe arrastrar httpx ni la
implementacion de DeepSeek.
"""

from __future__ import annotations

__all__: list[str] = []
