"""ExecutionResult: resultado determinista de la ejecución de una acción."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ExecutionResult(BaseModel):
    """Resultado de intentar ejecutar una acción previamente evaluada.

    En ENGINE-0 la ejecución es simulada y determinista: ningún componente toca
    el sistema de archivos, la red ni servicios externos.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool = Field(
        ...,
        description="True si la acción se completó correctamente.",
    )
    action: str = Field(
        default="",
        description="Acción ejecutada.",
    )
    summary: str = Field(
        default="",
        description="Resumen legible del resultado.",
    )
    cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Costo real incurrido en USD.",
    )
    elapsed_minutes: float = Field(
        default=0.0,
        ge=0.0,
        description="Tiempo real consumido en minutos.",
    )
    artifacts: tuple[str, ...] = Field(
        default=(),
        description="Artefactos generados (rutas declaradas).",
    )
    error: str | None = Field(
        default=None,
        description="Mensaje de error cuando ``success`` es False.",
    )
    verified: bool = Field(
        default=False,
        description="True si el resultado fue verificado por el motor.",
    )


class TaskExecutionRecord(BaseModel):
    """Registro acumulado de ejecución de una tarea."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempts: int = Field(default=0, ge=0, description="Intentos consumidos.")
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="Costo acumulado.")
    total_minutes: float = Field(default=0.0, ge=0.0, description="Minutos acumulados.")
    files_changed: tuple[str, ...] = Field(
        default=(), description="Archivos modificados acumulados."
    )


__all__ = ["ExecutionResult", "TaskExecutionRecord"]
