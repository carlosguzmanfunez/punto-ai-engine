"""Rol Security Agent (ENGINE-5).

Security busca vulnerabilidades, exposiciones y configuraciones inseguras. No confirma
el trabajo: lo ataca. El PASS de Developer y de QA es contexto, nunca prueba.

Los checks deterministas de este paquete inspeccionan datos y **no** ejecutan código del
proyecto, asi que corren en proceso confiable. Cualquier ejecucion del producto seguiria
exigiendo sandbox.

Este paquete no re-exporta sus simbolos: importarlo no debe arrastrar httpx ni la
implementacion de DeepSeek.
"""

from __future__ import annotations

__all__: list[str] = []
