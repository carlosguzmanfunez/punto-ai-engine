"""Pipeline de roles por etapa (ENGINE-6.0 / 6.0.2).

Dos decisiones que conviene dejar escritas:

- **Una etapa, varios roles.** ``REVIEW`` no es solo el Reviewer: en esa etapa, y antes de aprobar,
  entran las verificaciones independientes (auditoría cruzada) y la verificación visual cuando
  aplica. Así el orden de estados del encargo (``REVIEW -> APPROVED -> COMPLETED``) se mantiene
  intacto y, a la vez, la aprobación final exige que esas verificaciones hayan pasado.
- **Nada obligatorio que no aplique.** ``VISUAL_QA`` solo entra si la tarea o el perfil lo exigen;
  una tarea sin interfaz no se bloquea por una verificación visual que no tiene objeto.

Y una tercera, de ENGINE-6.0.2: **la duda no se resuelve a favor de omitir** (hallazgo V602-06). La
aplicabilidad de la verificación visual tiene tres respuestas —requerida, no requerida y
**desconocida**— y una ruta que no se puede perfilar no se convierte en «no aplica»: se declara
desconocida y el workflow no cierra sin evidencia. Omitir el ``project_path`` tampoco desactiva la
verificación: si no lo declara, se perfila el ``workspace_path``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

from punto.schemas.enums import TaskStatus
from punto.schemas.workflow import RoleName, WorkflowRequest
from punto.web.detection import detect_web_project


class VisualApplicability(StrEnum):
    """Veredicto sobre si la verificación visual es exigible para una petición.

    ``UNKNOWN`` no es un sinónimo de ``NOT_REQUIRED``: significa que PUNTO no pudo decidirlo (no hay
    ruta de proyecto que perfilar, o no se pudo leer). Tratar la duda como «no aplica» permitiría
    desactivar la verificación omitiendo metadatos, que es exactamente lo que no puede ocurrir.
    """

    REQUIRED = "REQUIRED"
    NOT_REQUIRED = "NOT_REQUIRED"
    UNKNOWN = "UNKNOWN"

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
    o el perfil web la exigen). En el resto de etapas la lista es fija.
    """
    base = STAGE_ROLES.get(stage, ())
    if stage is not TaskStatus.REVIEW:
        return base
    extra: list[RoleName] = []
    if request.cross_audit_required:
        extra.append(RoleName.CROSS_AUDIT)
    if visual_qa_required(request):
        extra.append(RoleName.VISUAL_QA)
    return (*base, *extra)


def visual_qa_required(request: WorkflowRequest) -> bool:
    """True si la verificación visual es **exigible** para esta petición.

    No basta con el campo declarado por quien llama: un ``False`` (o su omisión) no puede anular una
    necesidad objetiva. La regla es:

    - ``web_visual_required=True`` ⇒ se exige, sin discusión;
    - y si el proyecto tiene un **perfil web determinista** (se detecta con la capa que ya existe),
      también se exige, aunque el llamante no lo haya dicho.

    La ruta se resuelve con :func:`_project_path`: si no se declara ``project_path``, se perfila el
    ``workspace_path``, así que omitir la ruta no desactiva la verificación (hallazgo V602-06). Un
    proyecto no web no arrastra una verificación que no tiene objeto; una ruta que no se puede
    perfilar devuelve ``UNKNOWN`` y se trata como no exigible **solo** en esta función: el kernel
    exige evidencia antes de cerrar (ver :func:`visual_applicability`).
    """
    if request.web_visual_required:
        return True
    return visual_applicability(request) is VisualApplicability.REQUIRED


def visual_applicability(request: WorkflowRequest) -> VisualApplicability:
    """Aplicabilidad de la verificación visual, con la duda declarada como duda.

    ``UNKNOWN`` cuando no hay ruta que perfilar o la ruta no se puede leer: en ese caso no se sabe
    si el proyecto tiene interfaz, y PUNTO no convierte «no sé» en «no aplica».
    """
    project = _project_path(request)
    if project is None:
        return VisualApplicability.UNKNOWN
    if not project.is_dir():
        return VisualApplicability.UNKNOWN
    try:
        profile = detect_web_project(project)
    except (OSError, ValueError):
        # Un proyecto ilegible no se declara no-web: no se sabe, y fingir que se sabe sería peor que
        # declarar la duda.
        return VisualApplicability.UNKNOWN
    return (
        VisualApplicability.REQUIRED
        if profile.is_web_project
        else VisualApplicability.NOT_REQUIRED
    )


def _project_path(request: WorkflowRequest) -> Path | None:
    """Ruta del proyecto a perfilar.

    ``project_path`` es relativo a ``workspace_path``. Si no se declara, se perfila el propio
    ``workspace_path``: la raíz del workspace ya identifica al proyecto cuando el trabajo se hace
    sobre él, y exigir una segunda ruta para «activar» la verificación sería una puerta trasera.
    """
    base = Path(request.workspace_path) if request.workspace_path else None
    if request.project_path:
        return (base / request.project_path) if base is not None else Path(request.project_path)
    return base


def next_stage(stage: TaskStatus) -> TaskStatus | None:
    """Etapa siguiente del camino limpio, o ``None`` si no hay ninguna."""
    return NEXT_STAGE.get(stage)


def verification_required(request: WorkflowRequest, role: RoleName) -> bool:
    """True si la verificación indicada es exigible para esta petición."""
    if role is RoleName.CROSS_AUDIT:
        return request.cross_audit_required
    if role is RoleName.VISUAL_QA:
        return visual_qa_required(request)
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
    "VisualApplicability",
    "next_stage",
    "required_roles",
    "stage_roles",
    "verification_required",
    "visual_applicability",
    "visual_qa_required",
]
