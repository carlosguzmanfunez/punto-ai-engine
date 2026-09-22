"""TAKEOVER de calidad: quién puede tomar el trabajo de un proveedor que respondió pero no
produjo un cambio material (BUILDER TAKEOVER v0).

Deliberadamente **independiente** de ``punto.providers.failover`` (indisponibilidad OPERATIVA
demostrable: cuota, límite de tasa, desconexión). Son causas distintas y piden políticas
distintas:

- **OPERATIONAL FAILOVER**: el asignado no pudo responder (``RATE_LIMIT``/``QUOTA``/``AUTH``/
  ``TRANSPORT``/``UNAVAILABLE``). ``FailoverPolicy`` decide con quién continuar.
- **QUALITY TAKEOVER**: el asignado respondió con éxito pero el resultado no resuelve la causa
  (``CHANGES_EMPTY`` ante un criterio ``FAILED`` con remedio accionable, un cambio inválido, una
  reparación causal no resuelta). ``TakeoverPolicy`` decide con quién continuar — y puede, y
  normalmente debe, tener un orden de preferencia **distinto** del operativo: un candidato puede
  ser un buen sustituto por disponibilidad y un mal candidato de calidad, o al revés.

Comparten el mismo evaluador de capacidad/conexión (la pregunta «¿puede este proveedor hacer el
trabajo del rol ahora?» no depende de por qué se busca un sustituto) y la misma disciplina: solo
roles declarados, solo candidatos autorizados con capacidad efectiva, sin bucles, sin autoridad
nueva, sin reasignar el rol permanentemente.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from punto.providers.contract import ProviderRole

__all__ = ["DEFAULT_MAX_TAKEOVER_SUBSTITUTES", "TakeoverPolicy"]

#: Tope de candidatos de recuperación que se intentan por causa.
DEFAULT_MAX_TAKEOVER_SUBSTITUTES: Final[int] = 2


@dataclass(frozen=True, slots=True)
class TakeoverPolicy:
    """Política de TAKEOVER: qué roles admiten recuperación de calidad y con quién, en orden.

    ``roles`` mapea cada rol cubierto a sus candidatos de recuperación **en orden de
    preferencia**; una tupla vacía significa «cualquier proveedor registrado, en orden de
    registro». Es configuración de confianza (``providers.yaml``, sección ``takeover:``): nada
    que venga de una petición, de un proveedor o de una Task la modifica.
    """

    roles: Mapping[ProviderRole, tuple[str, ...]] = field(default_factory=dict)
    max_substitutes: int = DEFAULT_MAX_TAKEOVER_SUBSTITUTES
    #: ``False`` (por defecto): un candidato de pago por uso no se usa sin decisión explícita.
    allow_metered: bool = False

    def __post_init__(self) -> None:
        """Rechaza una política que permitiría un bucle o no permitiría nada."""
        if self.max_substitutes < 1:
            raise ValueError("max_substitutes debe ser al menos 1")

    def covers(self, role: ProviderRole) -> bool:
        """True si el rol admite recuperación de calidad."""
        return role in self.roles

    def preferred(self, role: ProviderRole) -> tuple[str, ...]:
        """Candidatos de recuperación declarados para el rol, en orden de preferencia."""
        return tuple(self.roles.get(role, ()))
