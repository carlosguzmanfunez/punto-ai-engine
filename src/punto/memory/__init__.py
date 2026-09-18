"""Memoria práctica de experiencia de PUNTO (PELL-0).

PUNTO empieza a recordar **cómo resolvió problemas**: qué intentó, qué falló y por qué, qué
corrección funcionó, cómo se comprobó y qué procedimiento es reutilizable. La recuperación es
determinista (etiquetas y palabras, sin embeddings ni servicios externos) y el almacén es un fichero
JSONL local que sobrevive a los reinicios.

Dos invariantes gobiernan el paquete:

- **MEMORIA ≠ AUTORIDAD**: la memoria aconseja; el motor decide. Una experiencia ``VERIFIED`` no
  amplía capabilities, no salta el Human Gate, no modifica un ``ResourceSet``, no reconcilia
  violaciones de arquitectura, no cambia políticas y no ejecuta código por sí sola.
- **sin evidencia no hay conocimiento confiable**: ``CANDIDATE`` es conocimiento registrado y aún no
  demostrado; ``VERIFIED`` exige evidencia declarada; ``FAILED`` se conserva a propósito, porque
  evita repetir caminos que ya sabemos que no funcionan; ``SUPERSEDED`` es historia válida.
"""

from __future__ import annotations

from punto.memory.experience import (
    EXPERIENCE_SCHEMA_VERSION,
    ExperienceMemory,
    ExperienceResult,
    ExperienceSchemaError,
    ExperienceSecretError,
    ExperienceStatus,
    assert_no_secrets,
    problem_fingerprint,
    tokens,
)
from punto.memory.store import ExperienceStore, default_memory_path

__all__ = [
    "EXPERIENCE_SCHEMA_VERSION",
    "ExperienceMemory",
    "ExperienceResult",
    "ExperienceSchemaError",
    "ExperienceSecretError",
    "ExperienceStatus",
    "ExperienceStore",
    "assert_no_secrets",
    "default_memory_path",
    "problem_fingerprint",
    "tokens",
]
