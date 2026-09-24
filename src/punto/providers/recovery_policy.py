"""RECOVERY operacional: con quién continuar cuando el asignado falló de verdad (Fase 8A).

Deliberadamente **independiente** de ``punto.providers.failover`` y de ``punto.providers.takeover``.
Las tres políticas comparten forma y evaluador, pero responden preguntas distintas:

- **OPERATIONAL FAILOVER** (``failover.py``): el asignado no pudo responder AL PRIMER INTENTO de
  una petición nueva. ``FailoverPolicy`` decide, dando por hecho que el asignado configurado
  sigue siendo el punto de partida.
- **QUALITY TAKEOVER** (``takeover.py``): el asignado respondió con éxito pero el resultado no
  resuelve la causa (``CHANGES_EMPTY`` ante un criterio ``FAILED`` con remedio accionable).
  ``TakeoverPolicy`` decide con quién continuar la MISMA Task.
- **OPERATIONAL RECOVERY** (este módulo): un ciclo ya en marcha, con un provider YA asignado y
  ya trabajando, sufre un fallo OPERACIONAL real (auth/cuota/rate-limit/transporte/no-disponible)
  a mitad de la ejecución. A diferencia de ``failover``, el provider causalmente fallido no tiene
  por qué ser el primario configurado (puede ser un sustituto de failover que a su vez falló) —
  por eso ``RecoveryPolicy`` declara el orden GENERAL de rotación entre TODOS los providers
  capaces del rol, y quien excluye al causal es el llamante (``exclude=``), no la política.

Comparten el mismo evaluador de capacidad/conexión (la pregunta «¿puede este proveedor hacer el
trabajo del rol ahora?» no depende de por qué se busca un sustituto) y la misma disciplina: solo
roles declarados, solo candidatos autorizados con capacidad efectiva, sin bucles, sin autoridad
nueva, sin reasignar el rol permanentemente. Este módulo NO invoca ningún proveedor: solo juzga
elegibilidad de conexión/capacidad/coste. El BUSY de un ProviderLease (Fase 2A/7) es ajeno a esta
capa -- el router no conoce leases -- y lo añade quien sí los conoce
(``punto.scheduling.recovery_waits``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from punto.providers.contract import ProviderRole

__all__ = [
    "DEFAULT_MAX_RECOVERY_SUBSTITUTES",
    "RecoveryCandidateJudgment",
    "RecoveryPolicy",
]

#: Tope de candidatos de recuperación operacional que se consideran por causa.
DEFAULT_MAX_RECOVERY_SUBSTITUTES: Final[int] = 2


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    """Política de RECOVERY: orden general de rotación por rol, sin saber quién falló hoy.

    ``roles`` mapea cada rol cubierto a TODOS sus providers capaces, **en orden de preferencia
    general**; una tupla vacía significa «cualquier proveedor registrado, en orden de registro».
    Es configuración de confianza (``providers.yaml``, sección ``recovery:``): nada que venga de
    una petición, de un proveedor o de una Task la modifica. Quién se excluye de este orden por
    ser el causante del fallo de hoy lo decide el llamante, no esta política.
    """

    roles: Mapping[ProviderRole, tuple[str, ...]] = field(default_factory=dict)
    max_substitutes: int = DEFAULT_MAX_RECOVERY_SUBSTITUTES
    #: ``False`` (por defecto): un candidato de pago por uso no se usa sin decisión explícita.
    allow_metered: bool = False

    def __post_init__(self) -> None:
        """Rechaza una política que permitiría un bucle o no permitiría nada."""
        if self.max_substitutes < 1:
            raise ValueError("max_substitutes debe ser al menos 1")

    def covers(self, role: ProviderRole) -> bool:
        """True si el rol admite recuperación operacional."""
        return role in self.roles

    def preferred(self, role: ProviderRole) -> tuple[str, ...]:
        """Orden general declarado para el rol (todos los capaces, ninguno excluido todavía)."""
        return tuple(self.roles.get(role, ()))


@dataclass(frozen=True, slots=True)
class RecoveryCandidateJudgment:
    """Veredicto de un candidato de recuperación, sin conocimiento de leases/BUSY.

    ``eligible=False`` cubre: no conectado, sin capacidad efectiva, o de pago por uso sin
    autorización (``allow_metered=false``). No cubre BUSY -- eso lo añade
    ``punto.scheduling.recovery_waits``, que sí conoce el ``LeaseLedger``.
    """

    provider: str
    eligible: bool
    reason: str = ""
    metered: bool = False
    transport: str = ""
