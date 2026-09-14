"""Interfaz abstracta del DeveloperRunner.

Frontera entre CAMUS y la ejecución real. CAMUS **nunca** ejecuta ``subprocess``,
ni escrituras de archivos, ni Git, ni shell: delega en un ``DeveloperRunner``.

El contrato es deliberadamente **provider-agnostic**: no menciona ni conoce
ningún proveedor de modelo. ENGINE-1 implementa ``LocalDeveloperRunner``
(determinista, sin IA); ENGINE-2 podrá añadir ``DeepSeekDeveloperRunner`` sin
tocar el núcleo ni esta interfaz.

Un ``DeveloperRunner`` **no** decide autoridad. Aplica los límites técnicos que le
fija el ``ExecutionContext`` y rechaza lo que los viole, pero jamás eleva
permisos: la jerarquía es Policy Engine -> CAMUS -> DeveloperRunner.

Reparación (ENGINE-6.1.1): la reparación **no** crea un segundo Developer. El encargo viaja en
``DeveloperTask.repair`` y lo ejecuta este mismo contrato con :meth:`DeveloperRunner.execute`. Lo
único que cambia es que el runner tiene que **declarar** que sabe recibir ese contexto
(:attr:`DeveloperRunner.supports_repair_context`), porque un runner que lo ignorara ejecutaría una
reparación como una tarea normal: sin las reglas duras y sin la autorización acotada del plan.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from punto.developer.backend import (
    ExecutionBackend,
    TrustedLocalBackend,
    require_sandbox_backend,
)
from punto.schemas.execution import ExecutionTrustLevel
from punto.tools.errors import UntrustedExecutionDeniedError

if TYPE_CHECKING:
    from punto.developer.context import ExecutionContext
    from punto.schemas.execution import DeveloperExecutionResult, DeveloperTask


class DeveloperRunner(ABC):
    """Contrato de ejecución de una tarea de desarrollo estructurada."""

    @property
    def name(self) -> str:
        """Nombre del runner, para auditoría y evidencia."""
        return type(self).__name__

    @property
    def generates_code_with_ai(self) -> bool:
        """True si el runner genera código mediante un modelo externo.

        ``LocalDeveloperRunner`` devuelve ``False``: aplica recetas declaradas.
        Un futuro ``DeepSeekDeveloperRunner`` devolverá ``True``, y con ello
        quedará obligado a ``UNTRUSTED_MODEL`` y a un ``SandboxedBackend``.
        """
        return False

    @property
    def trust_level_required(self) -> ExecutionTrustLevel:
        """Nivel de confianza que este runner exige.

        Es la consecuencia directa de :attr:`generates_code_with_ai`: quien genera
        código con IA **no** puede declararse confiable.
        """
        if self.generates_code_with_ai:
            return ExecutionTrustLevel.UNTRUSTED_MODEL
        return ExecutionTrustLevel.TRUSTED_LOCAL

    @property
    def supports_repair_context(self) -> bool:
        """True si el runner sabe recibir el **contexto de reparación** de la tarea.

        El contexto llega en ``DeveloperTask.repair`` (ENGINE-6.1): plan, diagnóstico, defectos,
        snapshot y criterios. No hay un runner de reparación distinto —es **este** mismo runner—,
        pero saber leer ese contexto es una capacidad que se **declara**, nunca se supone: un
        runner que lo ignore ejecutaría una reparación como si fuera una tarea normal, sin las
        reglas duras ni la autorización acotada, que es exactamente lo que el ciclo de reparación
        existe para impedir.

        Por defecto ``False`` (fail-closed). CAMUS y el adaptador de rol exigen esta declaración
        antes de invocar una reparación, así que un runner que no la declare no recibe trabajo de
        reparación y la etapa falla de forma explícita en vez de degradarse en silencio. El runner
        real de DeepSeek todavía no lo declara: recibir y aplicar este contexto es trabajo de una
        fase posterior, y hasta entonces esta propiedad es la frontera que lo dice.
        """
        return False

    def resolve_backend(
        self, context: ExecutionContext, backend: ExecutionBackend | None
    ) -> ExecutionBackend:
        """Selecciona el backend y hace cumplir la frontera de confianza.

        Es el **enforcement en código** de la separación trusted/untrusted:

        - un runner que genera código con IA exige ``UNTRUSTED_MODEL`` y un
          ``SandboxedBackend`` apto; si falta, falla (nunca degrada al host);
        - un runner determinista exige ``TRUSTED_LOCAL`` y usa
          ``TrustedLocalBackend`` por defecto.

        Raises:
            UntrustedExecutionDeniedError: si el nivel de confianza declarado no
                corresponde al runner o el backend no lo admite.
            SandboxRequiredError: si se requiere sandbox y el backend no lo es.
            SandboxUnavailableError: si se requiere sandbox y no hay ninguno.
        """
        required = self.trust_level_required

        if required is ExecutionTrustLevel.UNTRUSTED_MODEL:
            if context.trust_level is not ExecutionTrustLevel.UNTRUSTED_MODEL:
                raise UntrustedExecutionDeniedError(
                    f"{self.name} genera código con IA y exige "
                    f"UNTRUSTED_MODEL, pero el contexto declara "
                    f"{context.trust_level.value}. El trabajo originado por un "
                    "modelo no puede ejecutarse en el host."
                )
            return require_sandbox_backend(backend)

        if context.trust_level is ExecutionTrustLevel.UNTRUSTED_MODEL:
            raise UntrustedExecutionDeniedError(
                f"{self.name} es un runner local determinista y no puede ejecutar "
                "trabajo UNTRUSTED_MODEL."
            )

        if backend is None:
            return TrustedLocalBackend()
        if not backend.supports_trust_level(context.trust_level):
            raise UntrustedExecutionDeniedError(
                f"El backend {backend.name} no admite el nivel de confianza "
                f"{context.trust_level.value}."
            )
        return backend

    @abstractmethod
    def execute(
        self, task: DeveloperTask, context: ExecutionContext
    ) -> DeveloperExecutionResult:
        """Ejecuta ``task`` dentro del workspace de ``context``.

        Args:
            task: Tarea estructurada (archivos, reemplazos, asserts, checks, commit).
            context: Contexto que delimita workspace, rama, comandos y límites.

        Returns:
            La evidencia estructurada de la ejecución. Nunca lanza por un fallo
            de la tarea: los fallos se expresan como ``status`` acompañado de
            ``error``. Sí puede lanzar por un uso incorrecto de la interfaz.
        """
        raise NotImplementedError


__all__ = ["DeveloperRunner"]
