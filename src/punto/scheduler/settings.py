"""Configuración declarativa y fail-closed de Multi-Task v0.

Leer estos límites no activa concurrencia: Fase 1 solo congela el contrato que consumirán las
fases de leases y scheduling. La versión v0 no permite ampliar accidentalmente sus techos.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from punto.policy.config_loader import ConfigError, find_config_dir, load_yaml_file

SCHEDULER_FILE_NAME: Final[str] = "scheduler.yaml"


class SchedulerLimits(BaseModel):
    """Límites declarativos conservadores de Multi-Task v0."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    max_active_tasks: int = Field(ge=1, le=2)
    max_writers_per_task: int = Field(ge=1, le=1)
    provider_concurrency: int = Field(ge=1, le=1)


def load_scheduler_limits(config_dir: Path | None = None) -> SchedulerLimits:
    """Carga ``scheduler.yaml``; ausencia, extras o límites fuera de v0 son error duro."""
    root = find_config_dir() if config_dir is None else Path(config_dir)
    path = root / SCHEDULER_FILE_NAME
    raw: Mapping[str, object] = load_yaml_file(path)
    try:
        return SchedulerLimits.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            location = ".".join(str(item) for item in first.get("loc", ()))
            detail = str(first.get("msg", "configuración inválida"))
        else:  # pragma: no cover - Pydantic siempre entrega al menos un detalle.
            location = "scheduler"
            detail = "configuración inválida"
        raise ConfigError(
            f"Configuración de scheduler inválida en {path}: {location}: {detail}"
        ) from exc


__all__ = ["SCHEDULER_FILE_NAME", "SchedulerLimits", "load_scheduler_limits"]
