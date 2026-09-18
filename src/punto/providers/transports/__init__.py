"""Transportes concretos de proveedor (SUBSCRIPTION v0).

- ``cli``: base compartida de los transportes que hablan con un cliente oficial por proceso;
- ``codex``: Codex con la cuenta ChatGPT (rol ARCHITECT);
- ``claude_code``: Claude Code con la cuenta Claude (rol VISUAL_QA);
- ``api``: las alternativas de pago explícitas, sobre los adaptadores ``httpx`` que ya existían.

El registro y la selección por configuración viven en :mod:`punto.providers.transport_registry`.
"""

from punto.providers.transports.api import APITransport
from punto.providers.transports.claude_code import ClaudeCodeTransport
from punto.providers.transports.cli import CliTransport
from punto.providers.transports.codex import CodexTransport

__all__ = ["APITransport", "ClaudeCodeTransport", "CliTransport", "CodexTransport"]
