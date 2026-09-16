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
  código de modelo, y por eso corre en local. Lee y, cuando el proyecto adopta una generación nueva,
  **devuelve el árbol a la revisión aceptada** con los mismos dos primitivos del rollback del
  Developer (``reset --hard`` + ``clean -fd``): nunca crea ramas y nunca commitea.
- ``FixedLineage`` es un linaje determinista de prueba, para fijar el comportamiento del kernel
  (revisión obsoleta, revisión vacía, árbol que hay que devolver) sin depender de un repositorio.

Nada de este módulo acepta una revisión «parecida»: o es la misma, o es un fallo cerrado.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class WorkspaceReconciliation:
    """Efecto de devolver el árbol a la revisión aceptada por el proyecto.

    Existe como tipo —y no como un booleano— porque el efecto es material y hay que poder auditarlo
    con sus dos revisiones: de dónde venía el árbol y a cuál se le devolvió. ``restored=False``
    significa «ya estaba en la revisión aceptada», que es el caso normal cuando no hay trabajo no
    aceptado que descartar.
    """

    requested_revision: str
    previous_revision: str
    restored: bool


class WorkspaceLineage(Protocol):
    """Puerto del linaje: revisión actual, comprobación exacta y vuelta a una revisión aceptada."""

    def head_revision(self) -> str:
        """SHA de la revisión actual del workspace."""
        ...

    def assert_at(self, revision: str) -> None:
        """Comprueba que el workspace está **exactamente** en ``revision``."""
        ...

    def restore(self, revision: str) -> WorkspaceReconciliation:
        """Devuelve el árbol a ``revision``, descartando lo que se hubiera acumulado encima."""
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

    def restore(self, revision: str) -> WorkspaceReconciliation:
        """Devuelve el árbol a ``revision`` descartando lo que se hubiera acumulado encima.

        Existe (ENGINE-6.3) porque **adoptar una generación nueva significa descartar el intento
        sustituido**: el trabajo de un nodo no aceptado vive en el árbol —el child commiteó— aunque
        el proyecto nunca lo aceptó, y el nodo siguiente tiene que empezar exactamente en la
        revisión aceptada. Sin esta vuelta, la replanificación autónoma no puede continuar: el
        proyecto se bloquea con ``PROJECT_WORKSPACE_REVISION_MISMATCH`` justo antes de arrancar el
        primer nodo del plan nuevo.

        Lo que se descarta es el **árbol de trabajo**, nunca la evidencia: los commits siguen en el
        repositorio (dejan de estar en la rama), el resultado durable del Developer sigue en su
        almacén y el nodo sustituido conserva su child, su gasto, su fallo y sus intentos. Se
        reutilizan los dos primitivos con los que el Developer hace su propio rollback
        (``reset --hard`` + ``clean -fd``); ``git clean -fd`` no toca lo ignorado por
        ``.gitignore``, así que un entorno virtual o un directorio de artefactos del workspace
        sobreviven.

        Args:
            revision: revisión aceptada a la que hay que volver. Vacía significa «el proyecto no ha
                aceptado ninguna todavía» y no hay nada que devolver.

        Returns:
            El efecto medido: de qué revisión se venía y si hubo que mover el árbol.

        Raises:
            ProjectRevisionMismatchError: si no se puede leer o mover el árbol, o si después de
                moverlo la revisión no es exactamente la pedida.
        """
        if not revision:
            return WorkspaceReconciliation("", "", False)
        previous = self.head_revision()
        if previous == revision:
            return WorkspaceReconciliation(revision, previous, False)
        try:
            self._git.reset_hard(revision)
            self._git.clean_untracked()
            current = self._git.head_sha()
        except (DeveloperExecutionError, OSError) as exc:
            raise ProjectRevisionMismatchError(
                f"no se pudo devolver el workspace {self._workspace} a la revisión aceptada "
                f"{revision}: {exc}"
            ) from exc
        if current != revision:
            raise ProjectRevisionMismatchError(
                f"el workspace quedó en la revisión {current} después de pedirle {revision}: el "
                "árbol no es el que el proyecto aceptó"
            )
        return WorkspaceReconciliation(revision, previous, True)


class FixedLineage:
    """Linaje determinista de prueba: una revisión fija y un interruptor para simular deriva.

    Existe para poder fijar con pruebas el comportamiento del kernel ante una revisión obsoleta sin
    montar un repositorio: el kernel no debe distinguir un linaje real de uno de prueba, solo
    pedirle la revisión y comprobarla.

    ``restore`` mueve de verdad la revisión declarada —igual que el linaje real mueve el árbol— para
    que una prueba pueda afirmar que el proyecto **devolvió** el workspace antes de continuar; y
    ``on_restore`` permite simular que la vuelta falla.
    """

    def __init__(
        self,
        revision: str = "",
        *,
        on_mismatch: Callable[[str], None] | None = None,
        on_restore: Callable[[str], None] | None = None,
    ) -> None:
        self._revision = revision
        self._on_mismatch = on_mismatch
        self._on_restore = on_restore
        self.reads = 0
        self.checked: list[str] = []
        self.restored: list[WorkspaceReconciliation] = []

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

    def restore(self, revision: str) -> WorkspaceReconciliation:
        """Devuelve la revisión declarada a ``revision`` y deja constancia del efecto.

        ``on_restore`` se invoca **antes** de mover nada: es el interruptor con el que una prueba
        simula que la vuelta del árbol falla, y por eso puede lanzar.

        Raises:
            ProjectRevisionMismatchError: si ``on_restore`` decide simular el fallo.
        """
        if self._on_restore is not None:
            self._on_restore(revision)
        if not revision:
            reconciliation = WorkspaceReconciliation("", self._revision, False)
        elif self._revision == revision:
            reconciliation = WorkspaceReconciliation(revision, self._revision, False)
        else:
            reconciliation = WorkspaceReconciliation(revision, self._revision, True)
            self._revision = revision
        self.restored.append(reconciliation)
        return reconciliation


__all__ = [
    "FixedLineage",
    "GitWorkspaceLineage",
    "ProjectRevisionMismatchError",
    "WorkspaceLineage",
    "WorkspaceReconciliation",
]
