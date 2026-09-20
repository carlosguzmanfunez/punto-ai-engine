"""Autoridad persistente de un destino (AP000-R01).

Un destino puede declarar, **una sola vez** y en configuración confiable, qué operaciones están
previamente autorizadas y bajo qué condiciones. Es la pieza que permite que una operación ordinaria
dentro de esa autoridad continúe sin Human Gate, y que cualquier desviación material vuelva a exigir
una persona.

La capacidad es **general** (el motor la soporta para cualquier destino); la autorización es
**individual** (cada destino declara la suya). Un destino sin sobre explícito no recibe autonomía:
todo queda en ``False`` y la decisión falla cerrado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "KNOWN_DEPLOY_MECHANISMS",
    "ReleaseOperation",
    "TargetAuthority",
]


class ReleaseOperation(StrEnum):
    """Operaciones que un sobre persistente puede pre-autorizar."""

    LOCAL_CHANGES = "local_changes"
    COMMIT = "commit"
    PUSH = "push"
    DEPLOY = "deploy"
    PRODUCTION_RELEASE = "production_release"


#: Mecanismos de despliegue que PUNTO sabe ejecutar **de verdad**. El único que existe hoy es el
#: push gobernado a la rama de producción: la plataforma construye y publica desde ese push. Un
#: destino que declare otro mecanismo no recibe autorización autónoma: no se inventa un ejecutor.
KNOWN_DEPLOY_MECHANISMS: Final[frozenset[str]] = frozenset({"git-push"})


@dataclass(frozen=True, slots=True)
class TargetAuthority:
    """Sobre de autoridad persistente de un destino.

    Args:
        local_changes: el destino autoriza cambios locales gobernados por una Task.
        commit: autoriza confirmar esos cambios en la rama de trabajo.
        push: autoriza empujar el commit a la rama de producción declarada.
        deploy: autoriza el despliegue por el mecanismo declarado.
        production_release: autoriza cerrar la publicación en producción.
        deploy_mechanism: mecanismo autorizado (``git-push``), obligatorio si se autoriza el
            despliegue o la publicación.
        allowed_branches: ramas de destino autorizadas (patrones). Vacío ⇒ solo la rama de
            producción declarada del destino.
        allow_destructive: autoriza cambios destructivos (borrados) dentro de la Task. Por defecto
            **no**: un borrado no autorizado vuelve a exigir persona.
        require_qa: exige la cadena funcional verificada (el QA de este ciclo) antes de publicar.
    """

    local_changes: bool = False
    commit: bool = False
    push: bool = False
    deploy: bool = False
    production_release: bool = False
    deploy_mechanism: str = ""
    allowed_branches: tuple[str, ...] = ()
    allow_destructive: bool = False
    require_qa: bool = True
    #: Campos declarados tal cual, para trazabilidad en la interfaz y la auditoría.
    declared_fields: tuple[str, ...] = field(default=())

    @property
    def declared(self) -> bool:
        """True si el destino declara un sobre explícito (aunque sea para no autorizar nada)."""
        return bool(self.declared_fields)

    def allows(self, operation: ReleaseOperation) -> bool:
        """True si el sobre pre-autoriza esa operación concreta."""
        return bool(getattr(self, operation.value, False))

    @property
    def release_authorized(self) -> bool:
        """True si autoriza la cadena completa de release: push + despliegue + publicación."""
        return self.push and self.deploy and self.production_release

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable: qué autoriza el destino, sin secretos."""
        return {
            "declared": self.declared,
            "local_changes": self.local_changes,
            "commit": self.commit,
            "push": self.push,
            "deploy": self.deploy,
            "production_release": self.production_release,
            "deploy_mechanism": self.deploy_mechanism,
            "allowed_branches": list(self.allowed_branches),
            "allow_destructive": self.allow_destructive,
            "require_qa": self.require_qa,
            "declared_fields": list(self.declared_fields),
        }
