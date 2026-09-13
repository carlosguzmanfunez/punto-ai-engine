"""Pipeline de roles por etapa (ENGINE-6.0).

Dos decisiones que conviene dejar escritas:

- **Una etapa, varios roles.** ``REVIEW`` no es solo el Reviewer: en esa etapa, y antes de aprobar,
  entran las verificaciones independientes (auditoría cruzada) y la verificación visual cuando
  aplica. Así el orden de estados del encargo (``REVIEW -> APPROVED -> COMPLETED``) se mantiene
  intacto y, a la vez, la aprobación final exige que esas verificaciones hayan pasado.
- **Nada obligatorio que no aplique.** ``VISUAL_QA`` solo entra si la tarea o el perfil lo exigen;
  una tarea sin interfaz no se bloquea por una verificación visual que no tiene objeto.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from punto.schemas.enums import TaskStatus
from punto.schemas.workflow import RoleName, WorkflowRequest

#: Rol o roles que se ejecutan en cada etapa, en orden determinista.
STAGE_ROLES: Final[MappingProxyType[TaskStatus, tuple[RoleName, ...]]] = MappingProxyType(
    {
        TaskStatus.ANALYZING: (RoleName.ARCHITECT,),
        TaskStatus.PLANNING: (RoleName.PLANNER,),
        TaskStatus.READY: (),
        TaskStatus.IN_PROGRESS: (RoleName.DEVELOPER,),
        TaskStatus.QA: (RoleName.QA,),
        TaskStatus.SECURITY: (RoleName.SECURITY,),
        TaskStatus.REVIEW: (RoleName.REVIEWER,),
        TaskStatus.APPROVED: (),
    }
)

#: Siguiente etapa del camino limpio.
NEXT_STAGE: Final[MappingProxyType[TaskStatus, TaskStatus]] = MappingProxyType(
    {
        TaskStatus.NEW: TaskStatus.ANALYZING,
        TaskStatus.ANALYZING: TaskStatus.PLANNING,
        TaskStatus.PLANNING: TaskStatus.READY,
        TaskStatus.READY: TaskStatus.IN_PROGRESS,
        TaskStatus.IN_PROGRESS: TaskStatus.QA,
        TaskStatus.QA: TaskStatus.SECURITY,
        TaskStatus.SECURITY: TaskStatus.REVIEW,
        TaskStatus.REVIEW: TaskStatus.APPROVED,
        TaskStatus.APPROVED: TaskStatus.COMPLETED,
    }
)

#: Verificaciones independientes que se ejecutan dentro de la etapa de revisión, antes de aprobar.
REVIEW_VERIFICATIONS: Final[tuple[RoleName, ...]] = (RoleName.CROSS_AUDIT, RoleName.VISUAL_QA)


def stage_roles(stage: TaskStatus, request: WorkflowRequest) -> tuple[RoleName, ...]:
    """Roles que deben ejecutarse en una etapa, según la petición.

    En ``REVIEW`` se añaden la auditoría cruzada (si se exige) y la verificación visual (si la tarea
    la requiere). En el resto de etapas la lista es fija.
    """
    base = STAGE_ROLES.get(stage, ())
    if stage is not TaskStatus.REVIEW:
        return base
    extra: list[RoleName] = []
    if request.cross_audit_required:
        extra.append(RoleName.CROSS_AUDIT)
    if request.web_visual_required:
        extra.append(RoleName.VISUAL_QA)
    return (*base, *extra)


def next_stage(stage: TaskStatus) -> TaskStatus | None:
    """Etapa siguiente del camino limpio, o ``None`` si no hay ninguna."""
    return NEXT_STAGE.get(stage)


def verification_required(request: WorkflowRequest, role: RoleName) -> bool:
    """True si la verificación indicada es exigible para esta petición."""
    if role is RoleName.CROSS_AUDIT:
        return request.cross_audit_required
    if role is RoleName.VISUAL_QA:
        return request.web_visual_required
    return False


def required_roles(request: WorkflowRequest) -> tuple[RoleName, ...]:
    """Todos los roles que el camino limpio exige para esta petición, en orden."""
    roles: list[RoleName] = []
    for stage in (
        TaskStatus.ANALYZING,
        TaskStatus.PLANNING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.QA,
        TaskStatus.SECURITY,
        TaskStatus.REVIEW,
    ):
        roles.extend(stage_roles(stage, request))
    return tuple(roles)


__all__ = [
    "NEXT_STAGE",
    "REVIEW_VERIFICATIONS",
    "STAGE_ROLES",
    "next_stage",
    "required_roles",
    "stage_roles",
    "verification_required",
]
