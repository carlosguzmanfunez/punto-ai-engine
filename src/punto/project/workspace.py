"""Linaje de revisiones del workspace del proyecto (ENGINE-6.2).

ENGINE-6.2 es **secuencial** justamente para poder tener un linaje: cada nodo empieza exactamente en
la revisión que dejó aceptada el anterior, y ningún nodo ve un árbol que no sea el que sus
dependencias produjeron. Eso solo se puede demostrar si el proyecto guarda y comprueba **SHA
concretos**: un nombre de rama se mueve, y una rama no es evidencia de sobre qué contenido se
trabajó.

Este módulo es el puerto que lee la revisión real del workspace. Existe separado del kernel para que
la frontera sea explícita y sustituible en pruebas:

- ``GitWorkspaceLineage`` usa el ``GitWorkspace`` real del motor (el mismo con el que el Developer
  commitea) con un contexto **confiable**: leer la revisión del árbol es orchestación de PUNTO, no
  código de modelo, y por eso corre en local. Solo lee: no crea ramas, no escribe, no commitea.
- ``FixedLineage`` es un linaje determinista de prueba, para fijar el comportamiento del kernel
  (revisión obsoleta, revisión vacía) sin depender de un repositorio de verdad.

Nada de este módulo acepta una revisión «parecida»: o es la misma, o es un fallo cerrado.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from punto.developer.context import ExecutionContext
from punto.schemas.execution import ExecutionTrustLevel
from punto.tools.errors import DeveloperExecutionError
from punto.tools.git import GitWorkspace
from punto.tools.shell import ShellRunner

if TYPE_CHECKING:
    from collections.abc import Callable

#: Identidad sintética del contexto con el que PUNTO lee el linaje.
#:
#: El contexto exige una tarea porque es el contrato de la capa de ejecución, pero esta lectura no
#: es trabajo de una tarea: es orchestación del motor. Se usa un identificador fijo y documentado
#: para que no parezca una tarea real.
_LINEAGE_TASK_ID: UUID = UUID("00000000-0000-4000-8000-000000000000")

#: Rama declarada al leer: nunca se escribe en ella, así que su valor no autoriza nada.
_READ_ONLY_BRANCH = "main"


class ProjectRevisionMismatchError(RuntimeError):
    """El workspace no está en la revisión que el proyecto tenía aceptada."""


class WorkspaceLineage(Protocol):
    """Puerto de lectura del linaje: revisión actual y comprobación de revisión exacta."""

    def head_revision(self) -> str:
        """SHA de la revisión actual del workspace."""
        ...

    def assert_at(self, revision: str) -> None:
        """Comprueba que el workspace está **exactamente** en ``revision``."""
        ...


class GitWorkspaceLineage:
    """Linaje real, leído con el ``GitWorkspace`` del motor sobre el árbol del proyecto."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace).resolve()
        context = ExecutionContext(
            task_id=_LINEAGE_TASK_ID,
            workspace_path=self._workspace,
            branch_name=_READ_ONLY_BRANCH,
            trust_level=ExecutionTrustLevel.TRUSTED_LOCAL,
        )
        self._git = GitWorkspace(context, ShellRunner(context))

    @property
    def workspace(self) -> Path:
        """Raíz del workspace cuyo linaje se lee."""
        return self._workspace

    def head_revision(self) -> str:
        """SHA de la revisión actual, leído de Git.

        Raises:
            ProjectRevisionMismatchError: si el workspace no es un repositorio con un commit.
        """
        try:
            return self._git.head_sha()
        except (DeveloperExecutionError, OSError) as exc:
            raise ProjectRevisionMismatchError(
                f"no se pudo leer la revisión del workspace {self._workspace}: {exc}"
            ) from exc

    def assert_at(self, revision: str) -> None:
        """Comprueba que la revisión actual es exactamente ``revision``.

        Una revisión vacía significa «el proyecto todavía no aceptó ninguna»: en ese caso no hay
            nada
        que comprobar y se acepta el estado actual. Es el caso del primer nodo antes de fijar la
        revisión inicial.

        Raises:
            ProjectRevisionMismatchError: si el árbol está en otra revisión.
        """
        if not revision:
            return
        current = self.head_revision()
        if current != revision:
            raise ProjectRevisionMismatchError(
                f"el workspace está en la revisión {current} y el proyecto tiene aceptada "
                f"{revision}: el nodo no puede empezar desde un contenido que no es el suyo"
            )


class FixedLineage:
    """Linaje determinista de prueba: una revisión fija y un interruptor para simular deriva.

    Existe para poder fijar con pruebas el comportamiento del kernel ante una revisión obsoleta sin
    montar un repositorio: el kernel no debe distinguir un linaje real de uno de prueba, solo
    pedirle la revisión y comprobarla.
    """

    def __init__(
        self,
        revision: str = "",
        *,
        on_mismatch: Callable[[str], None] | None = None,
    ) -> None:
        self._revision = revision
        self._on_mismatch = on_mismatch
        self.reads = 0
        self.checked: list[str] = []

    @property
    def revision(self) -> str:
        """Revisión que el linaje declara."""
        return self._revision

    def move_to(self, revision: str) -> None:
        """Cambia la revisión declarada: simula que el árbol se movió por debajo del proyecto."""
        self._revision = revision

    def head_revision(self) -> str:
        """Devuelve la revisión declarada y cuenta la lectura."""
        self.reads += 1
        return self._revision

    def assert_at(self, revision: str) -> None:
        """Comprueba la revisión declarada, con el mismo contrato que el linaje real."""
        self.checked.append(revision)
        if not revision:
            return
        if self._on_mismatch is not None:
            self._on_mismatch(revision)
        if self._revision != revision:
            raise ProjectRevisionMismatchError(
                f"el workspace está en la revisión {self._revision} y el proyecto tiene aceptada "
                f"{revision}: el nodo no puede empezar desde un contenido que no es el suyo"
            )


__all__ = [
    "FixedLineage",
    "GitWorkspaceLineage",
    "ProjectRevisionMismatchError",
    "WorkspaceLineage",
]
