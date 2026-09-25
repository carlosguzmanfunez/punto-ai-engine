"""Entrada productiva al runtime Multi-Task ensamblado (Fase 15), sobre la MISMA aplicación.

- El runtime es UNO por proceso: lo construye y arranca el ``lifespan`` de la aplicación (carga ->
  reconciliación -> admisiones) y lo detiene al apagarse. Ningún handler construye otro.
- ``POST /runtime/tasks`` registra un plan (1..N Tasks y, si el plan lo exige, su Integration
  Task) en el scheduler del proceso. Es idempotente por ``plan_id``.
- ``GET /runtime`` y ``GET /runtime/tasks/{id}`` son lectura: la vista de una Task es la misma
  proyección operacional (F13) que ``/console/operations``.
- ``POST /runtime/tasks/{id}/reconcile`` es la decisión explícita sobre un DevelopmentCycle
  interrumpido: el runtime nunca la infiere.

Los módulos de ``punto.runtime`` se importan al usarse: ``punto.api`` se inicializa al importar
cualquier submódulo suyo y el runtime depende de ``punto.api.console_state``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from punto.api.operational_projection import project_task
from punto.schemas.scheduling import ResourceAccess

if TYPE_CHECKING:
    from punto.runtime.assembly import MultiTaskRuntime

#: Activa el runtime productivo en la aplicación por defecto (``punto.api.app:app``).
RUNTIME_ENABLED_ENV: Final[str] = "PUNTO_MULTITASK_RUNTIME"
_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

RuntimeFactory = Callable[[], "MultiTaskRuntime"]


class RuntimeResourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=40)
    key: str = Field(min_length=1, max_length=240)
    access: ResourceAccess = ResourceAccess.WRITE


class RuntimeTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=2_000)
    target_id: str = Field(min_length=1, max_length=80)
    provider: str = Field(default="", max_length=40)
    resources: list[RuntimeResourceBody] = Field(default_factory=list, max_length=40)
    depends_on: list[str] = Field(default_factory=list, max_length=40)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=40)
    scope_paths: list[str] = Field(default_factory=list, max_length=40)
    context: str = Field(default="", max_length=2_000)


class RuntimeIntegrationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=2_000)
    sources: list[str] = Field(min_length=1, max_length=40)


class RuntimePlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=120)
    tasks: list[RuntimeTaskBody] = Field(min_length=1, max_length=20)
    integration: RuntimeIntegrationBody | None = None


class RuntimeReconcileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["APPLIED", "FAILED"]
    detail: str = Field(min_length=1, max_length=300)


def runtime_enabled(environ: Any) -> bool:
    return str(environ.get(RUNTIME_ENABLED_ENV, "")).strip().lower() in _TRUE


@asynccontextmanager
async def runtime_lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Arranca el runtime del proceso antes de servir y lo detiene al apagar."""
    factory: RuntimeFactory | None = getattr(application.state, "runtime_factory", None)
    runtime = None
    if factory is not None:
        runtime = factory()
        application.state.runtime = runtime
        runtime.start()  # reconcilia ANTES de abrir admisiones; si falla, no se sirve
    try:
        yield
    finally:
        if runtime is not None:
            runtime.shutdown()


def register_runtime(application: FastAPI, factory: RuntimeFactory | None) -> None:
    """Registra la fábrica del runtime del proceso y sus rutas (sin runtime responden 503)."""
    application.state.runtime_factory = factory
    application.state.runtime = None

    def current() -> MultiTaskRuntime | None:
        runtime: MultiTaskRuntime | None = application.state.runtime
        return runtime

    def ready() -> MultiTaskRuntime:
        from punto.runtime.assembly import RuntimeState

        runtime = current()
        if runtime is None or runtime.state is not RuntimeState.READY:
            state = "DISABLED" if runtime is None else runtime.state.value
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"runtime no disponible ({state})"
            )
        return runtime

    @application.get("/runtime", tags=["runtime"], summary="Estado del runtime Multi-Task")
    def runtime_status() -> dict[str, Any]:
        runtime = current()
        if runtime is None:
            return {"state": "DISABLED"}
        return runtime.status()

    @application.post(
        "/runtime/tasks",
        tags=["runtime"],
        status_code=status.HTTP_202_ACCEPTED,
        summary="Registrar un plan de Tasks en el runtime del proceso",
    )
    def submit_runtime_plan(body: RuntimePlanBody) -> dict[str, Any]:
        from punto.runtime.assembly import (
            RuntimeIntegrationSpec,
            RuntimeNotReadyError,
            RuntimePlan,
            RuntimePlanError,
            RuntimeTaskSpec,
        )
        from punto.schemas.scheduling import ResourceReference

        runtime = ready()
        plan = RuntimePlan(
            plan_id=body.plan_id,
            tasks=tuple(
                RuntimeTaskSpec(
                    key=item.key,
                    objective=item.objective,
                    target_id=item.target_id,
                    provider=item.provider,
                    resources=tuple(
                        ResourceReference(kind=ref.kind, key=ref.key, access=ref.access)
                        for ref in item.resources
                    ),
                    depends_on=tuple(item.depends_on),
                    acceptance_criteria=tuple(item.acceptance_criteria),
                    scope_paths=tuple(item.scope_paths),
                    context=item.context,
                )
                for item in body.tasks
            ),
            integration=(
                RuntimeIntegrationSpec(
                    objective=body.integration.objective, sources=tuple(body.integration.sources)
                )
                if body.integration is not None
                else None
            ),
        )
        try:
            submitted = runtime.submit_plan(plan)
        except RuntimeNotReadyError as error:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
        except RuntimePlanError as error:
            code = (
                status.HTTP_409_CONFLICT
                if error.kind == "CONFLICT"
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(code, detail=error.detail) from error
        ids = [*submitted.task_ids.values()]
        if submitted.integration_task_id is not None:
            ids.append(submitted.integration_task_id)
        return {
            **submitted.as_dict(),
            "tasks": [project_task(runtime.task(task_id)) for task_id in ids],
        }

    @application.get("/runtime/tasks/{task_id}", tags=["runtime"], summary="Vista de una Task")
    def get_runtime_task(task_id: UUID) -> dict[str, Any]:
        runtime = ready()
        try:
            record = runtime.task(task_id)
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Task no gestionada") from error
        return project_task(record)

    @application.post(
        "/runtime/tasks/{task_id}/reconcile",
        tags=["runtime"],
        summary="Decidir un DevelopmentCycle interrumpido (APPLIED/FAILED)",
    )
    def reconcile_runtime_task(task_id: UUID, body: RuntimeReconcileBody) -> dict[str, Any]:
        from punto.scheduling.task_scheduler import SchedulerError
        from punto.schemas.workflow import EffectStatus

        runtime = ready()
        try:
            record = runtime.reconcile_dispatch(
                task_id, status=EffectStatus(body.status), detail=body.detail
            )
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Task no gestionada") from error
        except SchedulerError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=error.detail) from error
        return project_task(record)


__all__ = [
    "RUNTIME_ENABLED_ENV",
    "RuntimeFactory",
    "register_runtime",
    "runtime_enabled",
    "runtime_lifespan",
]
